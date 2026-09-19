#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""MCP gateway — the governed tool data plane.

Speaks MCP to the sandbox on one side and to the MCP server containers on the
other, exposing a curated tool set under per-tool allow/deny/ask policy held in
the control plane. It is to tool capability what the egress proxy is to network
capability. See DESIGN.md, "MCP gateway".

THIS MODULE IS THE PLACEMENT. It stands the service up on its three legs and
refuses to start on a configuration that undoes the split, and it owns nothing
else: the wire is `protocol.py`, what the agent may see is `surface.py`, and what
the servers said is `discovery.py`. That order is deliberate and matches how both
control-plane bridges landed — the surface is asserted while it is still cheap to
change, and the placement is the part that is expensive to retrofit because every
other component's guard has to agree with it.

It presents tools AND runs them, and neither half decides anything: every call
is authorised by the control plane first (`execute.py`), and an unconfigured
tool is denied rather than held.

Three legs, and the asymmetry between them is the whole design:

  sandbox-net (172.30.0.0/24)        the AGENT-facing MCP listener. Binds ONE
                                     address, the pinned leg below.
  mcp-net (172.28.0.0/24)            dialled OUT to reach server containers.
                                     NOTHING is served here.
  tool-authorize-net (172.27.0.0/24) dialled OUT to reach the control plane's
                                     /tool/* bridge. Nothing served here either.

The single-address bind is the load-bearing part. A wildcard would serve the
agent-facing MCP endpoint on mcp-net too, and a server container is exactly the
thing that must not be able to call it: those containers hold write-capable
credentials, and one that could drive the gateway's own tool endpoint would move
laterally into approved tool calls it was never granted. The agent already
reaches this listener by design, so the wildcard buys nothing and costs that.

NO EGRESS LEG, and that is not an omission. The gateway only ever dials siblings.
The SERVERS need real internet — from containers holding credentials — which is
why they go through the egress proxy and why that hop is governed. Giving the
gateway its own route out would put an unaudited path next to the credentials it
brokers.

Concurrency model: a single uvicorn server on one event loop. Nothing here holds
a request open — an `ask` is registered with the control plane and answered
immediately (see DESIGN.md, "An `ask` answers immediately"), so the gateway never
pins a worker waiting for a human.
"""
from __future__ import annotations

import ipaddress
import json
import os
import threading
import time

import discovery
import execute
import outcomes
import protocol
import surface
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

#: The agent-facing MCP listener. Bound to ONE address — see the module docstring
#: for why a wildcard here is a lateral edge rather than a convenience.
AGENT_BIND = os.environ.get("GATEWAY_AGENT_BIND", "")
AGENT_PORT = int(os.environ.get("GATEWAY_AGENT_PORT", "8100"))

#: Spellings of "every interface" that uvicorn treats as one. Listed rather than
#: substring-matched, for the reason control-plane/app.py lists them: a substring
#: test calls a legitimate address containing "::" a wildcard.
_WILDCARDS = ("", "0.0.0.0", "::", "*")  # noqa: S104

#: Networks the agent-facing listener must never be served on. Defaults mirror
#: docker-compose.yml and are held equal to the real subnets by
#: tests/test_topology.py, the same arrangement the control plane's
#: CONTROL_TOOL_BIND_FORBIDDEN uses.
#:
#: mcp-net is the one this guard exists for. The three control subnets are listed
#: beside it because the rule is that the agent-facing surface lives on the agent's
#: network and nowhere else — the gateway has no leg on control-net or
#: authorize-net at all, so those entries are redundant against today's compose and
#: kept because unroutability is a property of the compose file that this process
#: cannot verify.
_BIND_FORBIDDEN = os.environ.get(
    "GATEWAY_BIND_FORBIDDEN",
    "172.28.0.0/24,172.27.0.0/24,172.29.0.0/24,172.31.0.0/24")


def _forbidden_nets() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """The networks the agent listener must not be served on, parsed.

    An unparseable entry is FATAL, matching control-plane/app.py's
    ``_forbidden_tool_nets`` and for its reason: this list is short and every member
    is load-bearing, so dropping one silently would remove the guard while leaving a
    process that starts and looks healthy."""
    nets = []
    for raw in (part.strip() for part in _BIND_FORBIDDEN.split(",")):
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError as exc:
            raise SystemExit(
                f"tool-gateway: GATEWAY_BIND_FORBIDDEN entry {raw!r} is not a CIDR "
                f"({exc}), so the bind guard cannot be evaluated. Refusing to start "
                f"(fail closed).") from exc
    return tuple(nets)


def _bind_within(bind: str, net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    """Whether ``bind`` names an address inside ``net``.

    A hostname is not "inside" anything and answers False rather than being
    resolved. A guard that depended on DNS would be the class of check the relay
    guard exists because it cannot trust."""
    try:
        return ipaddress.ip_address(bind) in net
    except ValueError:
        return False


def _assert_bind_is_agent_facing_only() -> None:
    """Fail closed on a bind that would expose the agent surface elsewhere.

    Two refusals, because the mistake has two spellings and neither check implies
    the other — the same pair control-plane/app.py refuses for its tool bridge. A
    wildcard serves every interface; a concrete address on mcp-net serves exactly
    the one that matters. Both end with a server container able to reach the
    agent-facing endpoint, and nothing downstream can detect either: the agent's own
    calls keep working and every healthcheck stays green."""
    if AGENT_BIND in _WILDCARDS:
        raise SystemExit(
            f"tool-gateway: GATEWAY_AGENT_BIND={AGENT_BIND!r} is a wildcard, which "
            f"would serve the agent-facing MCP endpoint on mcp-net, where the server "
            f"containers can reach it — a compromised server could then drive approved "
            f"tool calls laterally. Bind the sandbox-net address instead. Refusing to "
            f"start (fail closed).")
    for net in _forbidden_nets():
        if _bind_within(AGENT_BIND, net):
            raise SystemExit(
                f"tool-gateway: GATEWAY_AGENT_BIND={AGENT_BIND!r} is inside {net}, "
                f"which is not the agent's network. The agent-facing surface belongs "
                f"on sandbox-net and nowhere else. Refusing to start (fail closed).")


app = FastAPI(title="dockade MCP gateway", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness only, and reachable by the agent — which is fine and worth saying.

    This listener is the agent's by design, so there is no surface here it should
    not see. It reports nothing about policy, nothing about which servers are
    enabled, and nothing about credentials; the roster is a control-plane surface
    and reaches the agent only through a governed tool call.

    Bound to a single address, so the compose healthcheck dials that address rather
    than loopback — see the comment beside it in docker-compose.yml."""
    return {"ok": True}


#: The agent's MCP endpoint. One path, matching the convention the server containers
#: are dialled on (`discovery.MCP_PATH`), so there is one spelling of "the MCP endpoint"
#: in this system rather than one per direction.
MCP_PATH = os.environ.get("GATEWAY_MCP_SERVE_PATH", "/mcp")

#: The most this listener will read of one request, in bytes. The body is the agent's
#: to write, and everything downstream buffers it whole: `json.loads` here, then the
#: complete `arguments` POSTed to the control plane's tool bridge for the decision,
#: then materialized by its request model. With no cap, a few hundred megabytes of
#: `arguments` was a sandbox-reachable OOM kill of the governance authority — not a
#: bypass (egress fails closed while it restarts), but the one lever the sandbox had
#: to take governance down on demand. A megabyte is far beyond any tool call worth
#: governing and far below the control plane's memory limit. Fail-closed, so an env
#: var rather than a config surface (DESIGN.md, "Hold bounds are fail-closed").
BODY_MAX = int(os.environ.get("GATEWAY_BODY_MAX", str(1024 * 1024)))


class BodyTooLarge(Exception):
    """The request body passed ``BODY_MAX``. Raised from ``read_body`` so the endpoint
    can answer 413 without having held the oversized part in memory."""


async def read_body(request: Request, cap: int | None = None) -> bytes:
    """The request body, or ``BodyTooLarge`` the moment it exceeds ``cap``.

    Streamed rather than `await request.body()`, because a Content-Length check alone
    trusts a header the sender controls: a chunked request carries none, and a lying
    one is caught only after the whole body has been buffered. Reading chunk by chunk
    and stopping at the cap bounds the memory this process spends on a request to the
    cap itself, whatever the headers said."""
    cap = BODY_MAX if cap is None else cap
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise BodyTooLarge(size)
        chunks.append(chunk)
    return b"".join(chunks)


@app.post(MCP_PATH)
async def mcp(request: Request) -> Response:
    """One JSON-RPC message in, one out. The decisions are in ``protocol.handle``.

    Deliberately thin, and the thinness is the point: everything worth asserting about
    this surface — what is served, what is refused, what a notification does — is
    reachable from a unit test because none of it is in here.

    NO `GET` COUNTERPART, so a client asking for the server→client SSE stream gets the
    405 the transport specifies for a server that does not offer one. That is honest
    rather than minimal: the stream's only use here would be `list_changed`, which is
    not advertised (see protocol.py). An unimplemented handler returning 200 and never
    sending an event would look to a client like a healthy stream that says nothing.

    NO ORIGIN CHECK, and the reason it is unnecessary is placement rather than
    diligence. The DNS-rebinding guidance for local MCP servers protects a listener a
    browser can reach; this one binds the sandbox-net address alone, on an internal
    network with no host port published, so the only thing that can reach it is the
    agent — which needs no rebinding trick to send whatever it likes here, and is
    exactly the party this surface exists to govern."""
    try:
        message = json.loads(await read_body(request))
    except BodyTooLarge:
        # Refused before anything downstream sees it (see ``BODY_MAX``). 413 rather
        # than a JSON-RPC error: the message was never read, so there is no id to
        # answer under and no claim about its content to make.
        return JSONResponse(
            {"error": f"request body exceeds {BODY_MAX} bytes and was not read"},
            status_code=413)
    except ValueError:
        # The other case answered with a non-200: an unparseable body is a transport
        # failure rather than a protocol answer, and there is no id to reply under.
        return JSONResponse(protocol.parse_error(), status_code=400)
    # The peer this listener observed, relayed to the control plane as the calling
    # SANDBOX — the same arrangement the egress proxy has on `/authorize`, and the same
    # trust: a gateway that reported its own address instead would make one shared
    # bucket of every sandbox's ask cap and let one agent's asks answer another's.
    client = request.client.host if request.client else None
    # OFF THE EVENT LOOP. A tool call is blocking stdlib HTTP to the control plane and
    # then to a server, and a server call crosses the internet through the egress proxy
    # — which may itself be holding it for a human. Run inline, one slow call would
    # stall every other request this process is serving, `/healthz` included, and the
    # symptom would be an unhealthy gateway rather than a slow tool.
    answer = await run_in_threadpool(
        protocol.handle, message, surface.listing(),
        lambda name, arguments: execute.call(name, arguments, client))
    if answer is None:
        # A notification. Accepted with no body, which is what the transport asks for
        # and what keeps a client from waiting on a reply that is not coming.
        return Response(status_code=202)
    return JSONResponse(answer)


#: The BACKSTOP, not the cadence. A report is normally triggered by the roster
#: changing; this is the longest the gateway will stay silent regardless, so that what
#: changes without an operator — a server restarting, an image bump adding tools, an
#: outage that has not lifted — still surfaces. Long because dialling every container
#: holding a credential is the expensive half, and nothing here is a control: an
#: unconfigured tool is denied whether or not this report has run.
DISCOVERY_INTERVAL = float(os.environ.get("GATEWAY_DISCOVERY_INTERVAL", "300"))

#: How often the ROSTER is re-read. Short, because it is one request to a sibling
#: answering two indexed selects — cheaper than the three four-second polls this
#: control plane already serves the UI — and because an operator who has just
#: registered a server is watching for the result.
#:
#: It doubles as the cold-start retry, which is why there is no third number. The
#: gateway has no `depends_on` (deliberately, so it serves whether or not the
#: authority is up) and the control plane's healthcheck probes its authorize listener
#: rather than the tool bridge, so nothing in compose orders the two: the first poll
#: races a cold start and loses. At this cadence it simply wins the next one.
ROSTER_INTERVAL = float(os.environ.get("GATEWAY_ROSTER_INTERVAL", "10"))


def _reconcile_forever(stop: threading.Event) -> None:
    """Report the gap between what the servers expose and what policy decides.

    A DAEMON loop that cannot fail the process, and — since the process would carry
    on without it — cannot be allowed to die either: ``discovery.poll`` swallows its
    own errors and returns them as text, the rest of an iteration is guarded below so
    an exception from any one server's reply costs one tick rather than every future
    one, and the sleep is on an Event so a shutdown is not held for the interval.

    POLLING AND SPEAKING ARE DIFFERENT RATES, and the split is the design. The roster
    is re-read every ROSTER_INTERVAL; the servers are dialled — and a report printed —
    only when the roster CHANGED, when reachability flipped, or when DISCOVERY_INTERVAL
    has passed since the last thing said. Enumeration is a request to every container
    holding a credential, so it follows an operator action rather than a short timer,
    while the slow tick still catches what changes without one: a server restarting, or
    an image bump adding tools.

    The quiet is bounded on purpose. A persistent outage is not announced every ten
    seconds, but it IS announced every slow tick, because a diagnostic that goes silent
    exactly when something is broken is the failure this module exists to avoid."""
    enumerated = None   # digest of the roster the last report was built from
    reachable = None    # None until the control plane has ever answered
    deadline = 0.0      # monotonic time the slow tick next falls due
    while True:
        try:
            enumerated, reachable, deadline = _reconcile_once(
                enumerated, reachable, deadline)
        except Exception as exc:  # noqa: BLE001 — a dead thread is the failure here
            # The thread is the only thing that refreshes the agent-facing listing and
            # the control plane's inventory. Dead, it leaves both frozen at their last
            # state while `/healthz` stays green — a diagnostic gone silent exactly
            # when something is broken. Say so, on the slow tick like any other
            # persistent fault, and try again next time.
            print(f"tool-gateway: reconcile failed ({type(exc).__name__}: {exc}); "
                  f"the listing and inventory are unchanged until the next attempt",
                  flush=True)
            deadline = time.monotonic() + DISCOVERY_INTERVAL
        if stop.wait(ROSTER_INTERVAL):
            return


def _reconcile_once(enumerated, reachable, deadline):
    """One iteration of ``_reconcile_forever``: poll, and speak if there is news.
    Returns the three pieces of state the next iteration needs. Split out so the
    loop's guard wraps the whole body and nothing else."""
    roster, failure = discovery.poll()
    due = time.monotonic() >= deadline
    if roster is None:
        # Flipping into failure is news; staying there is not, until the tick.
        if reachable is not False or due:
            print(failure, flush=True)
            deadline = time.monotonic() + DISCOVERY_INTERVAL
        reachable = False
    else:
        digest = discovery.roster_digest(roster)
        if reachable is not True or digest != enumerated or due:
            results = discovery.reconcile_all(roster)
            # Published FIRST, before either reader is told. The agent-facing
            # listing is the one consumer served from this process's own memory,
            # so it is the one that must not be left a reconcile behind by a
            # failure in the reporting below.
            surface.publish(roster, results)
            for line in discovery.format_report(results):
                print(line, flush=True)
            # Pushed on the same trigger as the report, because they are the same
            # observation going to two readers — the log for whoever is watching a
            # terminal, the control plane for whoever is choosing rules in the UI.
            # A refusal is printed rather than raised: the inventory is the
            # convenience half, and losing it must not cost the diagnostic half.
            refused = discovery.push_inventory(results)
            if refused:
                print(refused, flush=True)
            enumerated = digest
            deadline = time.monotonic() + DISCOVERY_INTERVAL
        reachable = True
    return enumerated, reachable, deadline


def main() -> None:
    """Assert placement, then serve.

    The guard runs BEFORE the listener opens, so a misconfigured gateway never
    accepts a connection it should not have accepted. Fail-closed here means the
    container dies and `restart: always` retries it — visible in `docker compose
    ps` as a restart loop, which is the intended operator experience for a
    configuration that cannot be served safely."""
    _assert_bind_is_agent_facing_only()
    # BEFORE the listener too, and for the same reason the bind guard is: a configured
    # audit path that cannot be opened is a misconfiguration, and a gateway that served
    # tool calls while failing to record how they ended would be the quiet version of
    # the gap this stream exists to close. Raises; `restart: always` makes it visible.
    outcomes.setup()

    # Imported here rather than at module scope so the module stays importable by
    # the test suite without pulling in the server. Same reason control-plane/app.py
    # defers its uvicorn import.
    import uvicorn

    print(f"tool-gateway: serving the agent-facing MCP endpoint on "
          f"{AGENT_BIND}:{AGENT_PORT} (this address only); {outcomes.describe()}",
          flush=True)

    # Started AFTER the guard and before the listener. A daemon thread rather than a
    # FastAPI startup hook: the work is blocking stdlib HTTP, so on the event loop it
    # would stall the listener for as long as a server takes to answer.
    stop = threading.Event()
    threading.Thread(target=_reconcile_forever, args=(stop,),
                     name="reconcile", daemon=True).start()
    try:
        uvicorn.run(app, host=AGENT_BIND, port=AGENT_PORT, log_level="info")
    finally:
        stop.set()


if __name__ == "__main__":
    main()
