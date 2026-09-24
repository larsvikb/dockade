# SPDX-License-Identifier: Apache-2.0
"""
Control plane — governance authority for the governed data-plane proxies.

Step 2b: policy + audit + **hold-for-approval**. The management app the agent can
never reach — it lives on internal networks the sandbox is not attached to.
Governed proxies call it on the control path to authorize connections; unknown
requests are held for a human, who approves/rejects them in a live UI.

This file is the PROCESS — the three listeners, which surface each one serves, the
boot, and the entry point. The routes live one module per surface, each on its own
router, and are mounted on a listener here and nowhere else:

  - ``api_authorize`` — ``POST /authorize``, the egress proxy's question.
  - ``api_tool``      — ``/tool/*``, the MCP gateway's bridge.
  - ``api_approvals`` — the queue and ``resolve``: the endpoint that grants.
  - ``api_egress``    — standing egress rules, and the leases a card granted.
  - ``api_mcp``       — MCP servers and tool rules.
  - ``api_views``     — the audit record, the UI's settings, /status. Grants nothing.
  - ``provenance``    — ``_actor``, recorded by every write that grants.

The machinery those routes drive lives beside them, one module per concern, and
every call is written qualified (``policy._decide``, ``holds._reserve_hold``) so a
reader can see which one is being asked:

  - ``store``   — SQLite: the schema, the audit write, the seed. The crown jewel.
  - ``audit``   — the READ side of that store: the two decision views, their shared
    filters and the record view's paging.
  - ``policy``  — what a rule pattern matches, and what a host decides to.
  - ``holds``   — the in-process registry a blocked request waits on, its caps
    and its duplicate grouping.
  - ``ingest``  — draining the egress proxy's own audit file into the store.
  - ``inventory`` — what each MCP server last said it exposes, held in memory.

They import in that order and never back. The ``api_*`` modules import them and
``provenance`` but never each other, and nothing imports this file.

THREE LISTENERS, on three networks, because the dangerous surface is the management
API and not the questions the enforcers ask it. Both ways of granting egress live on
the management one — `resolve` and `create_rule` — so anything that reaches it can
self-approve, while an enforcer's bridge can only ever answer a policy question.
They are therefore served separately (``main``):

  - the AUTHORIZE listener (CONTROL_AUTHORIZE_PORT, on authorize-net) serves
    exactly POST /authorize and GET /healthz — ``authorize_app`` below. It is the
    only surface the egress proxy has a route to.
  - the TOOL listener (CONTROL_TOOL_PORT, on tool-authorize-net) serves the MCP
    gateway's three questions and nothing else — ``tool_app`` below.
  - the MANAGEMENT listener (CONTROL_MANAGE_PORT, bound to the control-net address
    ALONE — a wildcard bind is refused at startup) serves everything else, and is
    reachable only from control-plane-ui.

The two enforcer bridges are separate networks and separate sockets rather than one
shared "ask policy" surface, because a lateral edge between two enforcers is what
splitting them bought its way out of: the proxy's relay guard is best-effort by
construction, and a bypassed proxy must not gain a route to the gateway's claim
endpoint, which is the one place a side effect gets released. That is also why the
TOOL listener binds ONE address where the authorize listener binds the wildcard —
the asymmetry is not an oversight, it is the whole point (see
``_assert_listeners_separated``).

This is blast-radius containment, not the primary control: the agent is kept off
this service by network topology, by the proxy's relay guard and by the proxy's
port gate. What the split adds is that all three failing at once yields a policy
QUERY rather than a self-approval. See DESIGN.md.

Concurrency model: run under a SINGLE uvicorn worker. `/authorize` and the
resolve endpoint are sync (FastAPI runs them in a threadpool); a held request
blocks its worker on a threading.Event that the resolve endpoint sets. Blocked
workers are bounded (CONTROL_MAX_WAITERS / CONTROL_MAX_WAITERS_PER_CLIENT) so a
sandbox cannot pin every worker and stall governance for all sandboxes; CARDS are
bounded separately (CONTROL_MAX_PENDING / CONTROL_MAX_PENDING_PER_CLIENT), which
protects the operator's attention rather than the pool — see holds.py for why the
two nouns need four caps. Over any of them /authorize fails closed. The SQLite store is the source of truth for the UI
(the SSE stream polls it). Do NOT run multiple workers — the pending-event
registry is in-process (holds.py). That constraint is also why the listeners
above are separate sockets in ONE process rather than separate services: a held
/authorize and the `resolve` that releases it must share memory, precisely because
they must not share a socket.

No egress: this service sits on control-net, authorize-net and tool-authorize-net,
all internal. It must never be given an internet route — it is pure management
state (the crown jewel).
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import time

import api_approvals
import api_authorize
import api_egress
import api_mcp
import api_tool
import api_views
import holds
import ingest
import store
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ── the three listeners (see the module docstring) ──────────────────────────
# The proxy-facing surface binds a WILDCARD on purpose: it is the safe one, it
# only answers policy questions, and the container's healthcheck reaches it over
# loopback. The management surface binds ONE address, and the default is loopback
# so that a deployment which forgets to set it fails VISIBLY (the UI cannot reach
# the backend) instead of silently re-exposing `resolve` to the proxy's network.
AUTHORIZE_BIND = os.environ.get("CONTROL_AUTHORIZE_BIND", "0.0.0.0")  # noqa: S104
AUTHORIZE_PORT = int(os.environ.get("CONTROL_AUTHORIZE_PORT", "8091"))
MANAGE_BIND = os.environ.get("CONTROL_MANAGE_BIND", "127.0.0.1")
MANAGE_PORT = int(os.environ.get("CONTROL_MANAGE_PORT", "8090"))
# The MCP gateway's bridge. Binds ONE address like the management surface, not the
# wildcard the authorize surface uses, and the default is loopback for the same
# reason: a deployment that forgets to set it fails VISIBLY (the gateway cannot
# reach it) rather than silently serving the claim endpoint on authorize-net, where
# the egress proxy could burn an approved ask the agent is coming back for.
#
# The consequence of the authorize listener's wildcard, stated rather than left to be
# discovered: the gateway CAN reach /authorize, since that socket answers on every
# interface. Accepted, because that surface only ever answers a policy question — the
# same property that makes it safe for the proxy. The reverse direction is the one
# that had to be closed, and this bind is what closes it.
TOOL_BIND = os.environ.get("CONTROL_TOOL_BIND", "127.0.0.1")
TOOL_PORT = int(os.environ.get("CONTROL_TOOL_PORT", "8092"))
#: Every spelling of "listen on every interface", including the empty string, which
#: uvicorn treats as one. Listed rather than substring-matched: a substring test
#: would also reject a legitimate address that happens to contain one of these.
_WILDCARDS = ("", "0.0.0.0", "::", "*")  # noqa: S104
#: Where the tool bridge must NOT be served, as CIDRs rather than as a wildcard test.
#: A wildcard is one way to put the claim endpoint on authorize-net; naming that
#: network's address outright is the other, and it passes a wildcard check while
#: producing exactly the outcome the check exists to refuse — the claim endpoint
#: within the egress proxy's reach, with every healthcheck green. This is the whole
#: reason the assertion is in the app and not only in tests/test_topology.py: it is
#: here so as not to trust the compose file, so it cannot be satisfied by a test that
#: reads the compose file.
#:
#: Defaults mirror docker-compose.yml, the same arrangement ``FORBIDDEN_CIDRS`` in
#: proxies/egress/addon.py uses, and tests/test_topology.py holds them equal to the
#: real subnets. control-net is listed alongside authorize-net because the rule is one
#: bridge per enforcer: sharing the management network would not reach the proxy, but
#: it would put the gateway on the operator's path, and neither enforcer belongs on
#: the other's leg.
_TOOL_BIND_FORBIDDEN = os.environ.get(
    "CONTROL_TOOL_BIND_FORBIDDEN", "172.29.0.0/24,172.31.0.0/24")
#: The same refusal for the management surface, which is the more dangerous one to
#: get wrong: `resolve` lives here. A wildcard check alone let
#: ``CONTROL_MANAGE_BIND=172.29.0.2`` — this container's own authorize-net address —
#: start cleanly and serve `resolve` and `create_rule` to the egress proxy, with
#: every healthcheck green. The two enforcers' networks are listed; control-net is
#: not, because that is the address this listener is required to bind.
_MANAGE_BIND_FORBIDDEN = os.environ.get(
    "CONTROL_MANAGE_BIND_FORBIDDEN", "172.29.0.0/24,172.27.0.0/24")

#: The most either enforcer-facing listener accepts in one request body, in bytes.
#: Both peers — the proxy on `/authorize`, the gateway on the tool bridge — relay what
#: the SANDBOX sent, and FastAPI materializes the whole body through the request
#: model before any handler can measure it (``ToolCallRequest.args`` is `Any`, and
#: ``holds.TOOL_ARGS_MAX`` applies only after the parse, on the `ask` path). Uncapped,
#: a few hundred megabytes of `args` was an OOM kill of this process under its 512m
#: limit — governance down on the sandbox's say-so. Not a bypass, since egress fails
#: closed while the container restarts, but the sandbox's one lever against
#: availability, and SECURITY.md puts "pressure becoming permission" in scope.
#:
#: Sized to the peer. An egress question is a host, a port and a URL; the tool bridge
#: carries a tool call's complete arguments, which the gateway has already bounded
#: at its own intake (``GATEWAY_BODY_MAX``), so this is the backstop behind that cap
#: rather than the cap itself. Fail-closed bounds, so env vars (DESIGN.md, "Hold
#: bounds are fail-closed, so their values stay env vars").
AUTHORIZE_BODY_MAX = int(os.environ.get("CONTROL_AUTHORIZE_BODY_MAX", str(64 * 1024)))
TOOL_BODY_MAX = int(os.environ.get("CONTROL_TOOL_BODY_MAX", str(2 * 1024 * 1024)))


# None of the three serves FastAPI's schema or docs pages (/openapi.json, /docs,
# /redoc), which it adds to every app unless told not to; tests/test_topology.py
# holds every app in the repo to that.
#
# Everything except /authorize: the approvals API, the read-only views, /status.
app = FastAPI(title="dockade control plane",
              openapi_url=None, docs_url=None, redoc_url=None)
# POST /authorize and GET /healthz, and nothing else, ever. Adding a route here
# hands it to the egress proxy — the one component whose compromise this split
# exists to survive — so the question to ask of any new endpoint is not "is it
# read-only" but "would I let a bypassed relay guard call it".
authorize_app = FastAPI(title="dockade control plane (authorize)",
                        openapi_url=None, docs_url=None, redoc_url=None)
# The MCP gateway's three questions, and nothing else, ever: what may this call do,
# which servers and tools are configured, and may I now run the ask a human approved.
# The question to ask of any new route here is the one above with a different
# enforcer: "would I let a compromised MCP gateway call it". Nothing on this app may
# GRANT — no rule is written here and no approval is decided here (`resolve` stays on
# the management app, asserted by tests/test_control_plane_api.py).
tool_app = FastAPI(title="dockade control plane (tool)",
                   openapi_url=None, docs_url=None, redoc_url=None)


def _body_cap(cap: int):
    """Middleware refusing a request body over ``cap`` BEFORE FastAPI reads it.

    Decided from ``Content-Length`` alone, and that is enough here where it would not
    be on the gateway's agent-facing listener: both peers of these two apps are the
    stdlib `urllib` in the proxy and the gateway, which always sends the header and
    never chunks. A POST without one is therefore not a peer this listener knows, and
    is refused as such (411) rather than read to find out how big it is. A GET carries
    no body and passes untouched — the roster is fetched that way."""
    async def middleware(request: Request, call_next):
        if request.method.upper() in ("POST", "PUT", "PATCH"):
            declared = request.headers.get("content-length")
            if declared is None or not declared.isdigit():
                return JSONResponse(
                    {"detail": "Content-Length required on this listener"},
                    status_code=411)
            if int(declared) > cap:
                return JSONResponse(
                    {"detail": f"request body of {declared} bytes exceeds the "
                               f"{cap}-byte cap for this listener"},
                    status_code=413)
        return await call_next(request)
    return middleware


# Registered on the two enforcer-facing apps and NOT on the management app: the
# management surface is the operator's, reached through the UI relay, and is not
# what the sandbox can lean on.
authorize_app.middleware("http")(_body_cap(AUTHORIZE_BODY_MAX))
tool_app.middleware("http")(_body_cap(TOOL_BODY_MAX))


# ── what each listener serves ───────────────────────────────────────────────
# A surface module does not know which listener it is on; this block decides, and
# ApiSurfaceSplitTests (tests/test_control_plane_api.py) asserts the result.
authorize_app.include_router(api_authorize.router)
tool_app.include_router(api_tool.router)
app.include_router(api_approvals.router)
app.include_router(api_egress.router)
app.include_router(api_mcp.router)
app.include_router(api_views.router)


@app.get("/healthz")
@authorize_app.get("/healthz")
@tool_app.get("/healthz")
async def healthz() -> dict:
    """``async`` so the probe is answered on the event loop and never queues behind
    a worker thread. ``authorize`` is a plain ``def`` that BLOCKS on a hold for up to
    ``holds.HOLD_TIMEOUT``, and Starlette runs it in a bounded threadpool; a sync
    probe shares that pool. Today ``MAX_WAITERS`` (16) is under the pool's default
    size, so the queue cannot form — but that is an accident of two numbers set in
    different files, and the failure it prevents is compose restarting the control
    plane in the middle of the holds that made it look unhealthy. There is nothing
    to await here, so the fix costs nothing and stops depending on the arithmetic."""
    return {"status": "ok"}


# ── lifecycle ───────────────────────────────────────────────────────────────

def _bootstrap() -> None:
    """Prepare the store. Called by ``main`` BEFORE either listener binds, so no
    request — from the proxy or the UI — can observe an unseeded database. This
    used to be a Starlette startup handler, which worked only because there was a
    single app: with two, each has its own lifespan and the authorize listener
    would race the management one's seed."""
    store._init_db()
    # A held request cannot survive a restart (its blocked connection is gone),
    # so any 'pending' rows from a previous process are stale — expire them.
    #
    # ``tool_approvals`` is deliberately NOT swept here, and the difference is the
    # whole point of that table: nothing is blocked on a tool ask, so a pending one
    # is not stale after a restart — it is a question still waiting for a human, with
    # an agent that can still come back for the answer. Its window is enforced by its
    # own ``deadline`` column instead (``holds._expire_tool_asks``). Sweeping it here
    # would throw away exactly the state that answering immediately was chosen to make
    # durable.
    with store._connect() as conn:
        conn.execute(
            "UPDATE approvals SET status='expired', resolved_at=? "
            "WHERE status='pending'", (time.time(),))
        conn.commit()
    seeded = store._seed_if_empty()
    if seeded:
        print(f"control-plane: seeded {seeded} allow rules from {store.SEED_PATH}",
              flush=True)
    # The hold caps, with their UNITS, once per boot. Not decoration: `CONTROL_MAX_
    # PENDING` used to count blocked requests and now counts cards, so an operator who
    # set it under the old meaning has a different limit than they think. A line that
    # says which noun each number counts is what makes that discoverable without
    # reading holds.py, and it costs one line in a log an operator already tails for
    # the live decision feed.
    print(f"control-plane: hold caps — cards {holds.MAX_PENDING} global / "
          f"{holds.MAX_PENDING_PER_CLIENT} per client, blocked requests "
          f"{holds.MAX_WAITERS} global / {holds.MAX_WAITERS_PER_CLIENT} per client "
          f"(0 = refuse all on a global cap, disabled on a per-client one)", flush=True)
    # The tool surface's bounds, on their own line because they count a different
    # thing: cards only, since nothing blocks on an ask, and a window measured against
    # a human's attention rather than against a proxy's patience.
    print(f"control-plane: tool asks — cards {holds.MAX_TOOL_PENDING} global / "
          f"{holds.MAX_TOOL_PENDING_PER_CLIENT} per client, ask window "
          f"{holds.TOOL_HOLD_TIMEOUT:g}s, grant window "
          f"{holds.TOOL_GRANT_TIMEOUT:g}s, payload ceiling {holds.TOOL_ARGS_MAX}B",
          flush=True)
    _warn_on_dead_caps()


def _warn_on_dead_caps() -> None:
    """Name a cap that cannot fire, at the boot that configured it.

    A card holds at least one blocked request, so cards are always <= waiters and a card
    cap set at or above its waiter cap can never be the one to refuse — the waiter cap
    gets there first, and the card number is a limit the operator believes in and does
    not have. ``test_a_card_cap_at_or_above_its_waiter_cap_is_dead`` pins the shipped
    defaults against that, which covers every case except the one the caps exist for:
    being set by hand.

    A WARNING rather than the ``SystemExit`` that ``_assert_listeners_separated`` uses,
    and the asymmetry is the point. A wildcard management bind restores a self-approval
    path, so refusing to start is strictly safer than starting. A dead cap is not a
    containment failure at all — waiters still bound cards, so the hold queue stays
    bounded and only the operator's model of WHICH limit binds is wrong. Refusing to boot
    would answer that by taking the governance authority down, which denies every
    sandbox's egress: a worse outcome than the misconfiguration, and caused by us.

    Zero is exempt at both scopes because zero is meaningful at both — a global zero
    refuses everything, a per-client zero disables that cap (see ``holds.py``). Warning
    on either would be warning that a documented setting works.
    """
    for cards, waiters, scope in (
            (holds.MAX_PENDING, holds.MAX_WAITERS, "global"),
            (holds.MAX_PENDING_PER_CLIENT, holds.MAX_WAITERS_PER_CLIENT, "per-client")):
        if cards > 0 and waiters > 0 and cards >= waiters:
            print(f"control-plane: WARNING — the {scope} card cap ({cards}) is at or "
                  f"above the {scope} blocked-request cap ({waiters}), so it can never "
                  f"refuse: every card holds at least one blocked request, so the "
                  f"request cap always fires first. Lower the card cap to make it bind.",
                  flush=True)


def _forbidden_nets(var: str, value: str,
                    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """The networks a listener must not be served on, parsed from one of the two
    ``*_BIND_FORBIDDEN`` lists (``var`` names it for the error).

    An unparseable entry is FATAL here, unlike the addon's tolerant CIDR parsing: that
    one drops a bad entry because its list is long and mostly redundant, while these
    have two members each and dropping either silently removes the guard. A typo in a
    hand-set override must not read as "nothing is forbidden"."""
    nets = []
    for raw in (part.strip() for part in value.split(",")):
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError as exc:
            raise SystemExit(
                f"control-plane: {var} entry {raw!r} is not a CIDR ({exc}), so the "
                f"bind guard cannot be evaluated. Refusing to start (fail closed).") from exc
    return tuple(nets)


def _forbidden_tool_nets() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _forbidden_nets("CONTROL_TOOL_BIND_FORBIDDEN", _TOOL_BIND_FORBIDDEN)


def _forbidden_manage_nets() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _forbidden_nets("CONTROL_MANAGE_BIND_FORBIDDEN", _MANAGE_BIND_FORBIDDEN)


def _bind_within(bind: str, net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    """Whether ``bind`` names an address inside ``net``.

    A bind that is not an address at all — a hostname, or a wildcard the caller has
    already rejected — is not "inside" anything and answers False. Resolving a name
    here would make the guard depend on DNS, which is the class of check the relay
    guard exists because it cannot trust."""
    try:
        return ipaddress.ip_address(bind) in net
    except ValueError:
        return False


def _assert_listeners_separated() -> None:
    """Fail closed on a configuration that undoes the split.

    The management API is only out of the proxy's reach because it binds ONE
    address, on a network the proxy is not attached to. A wildcard bind serves it
    on every interface — including authorize-net — which silently restores exactly
    the self-approval path the split removes, while every healthcheck and every
    page in the UI keeps working. Nothing downstream can detect that, so it is
    refused here (the same shape as the proxy's ``_assert_guard_configured``).

    Three refusals, because a bind can undo the split three ways: a wildcard, a
    concrete address on another enforcer's network, and a shared port. The first two
    are the same mistake spelled differently and one check does not imply the
    other — and both binds get both, the management one included, because it is
    the listener that grants."""
    if MANAGE_BIND in _WILDCARDS:
        raise SystemExit(
            f"control-plane: CONTROL_MANAGE_BIND={MANAGE_BIND!r} is a wildcard, "
            f"which would serve the management API (including "
            f"/approvals/{{id}}/resolve) on authorize-net, where the egress proxy "
            f"can reach it. Bind the control-net address instead. Refusing to "
            f"start (fail closed).")
    if TOOL_BIND in _WILDCARDS:
        raise SystemExit(
            f"control-plane: CONTROL_TOOL_BIND={TOOL_BIND!r} is a wildcard, which "
            f"would serve the gateway's bridge (including the claim endpoint that "
            f"releases an approved call) on authorize-net, where the egress proxy "
            f"can reach it — a lateral edge between two enforcers. Bind the "
            f"tool-authorize-net address instead. Refusing to start (fail closed).")
    # The other spelling of the same mistake, and the one a wildcard test misses: an
    # address on another enforcer's network. Refused for the reason the wildcard is,
    # because the outcome is the same one.
    for net in _forbidden_manage_nets():
        if _bind_within(MANAGE_BIND, net):
            raise SystemExit(
                f"control-plane: CONTROL_MANAGE_BIND={MANAGE_BIND!r} is inside {net}, "
                f"which is an enforcer's network — serving the management API "
                f"(including /approvals/{{id}}/resolve) there puts self-approval "
                f"within that enforcer's reach, and nothing downstream can detect "
                f"it. Bind the control-net address instead. Refusing to start "
                f"(fail closed).")
    for net in _forbidden_tool_nets():
        if _bind_within(TOOL_BIND, net):
            raise SystemExit(
                f"control-plane: CONTROL_TOOL_BIND={TOOL_BIND!r} is inside {net}, "
                f"which is another enforcer's network — serving the claim endpoint "
                f"there is the lateral edge the separate bridge exists to remove, and "
                f"nothing downstream can detect it. Bind the tool-authorize-net "
                f"address instead. Refusing to start (fail closed).")
    # Every listener gets its OWN PORT, checked across all three pairs and without
    # regard to the addresses. Same address and same port is one socket serving two
    # apps' worth of surface, which is the obvious case; same port on different
    # addresses is the subtle one and is refused too, because with a wildcard in the
    # mix — and the authorize listener is one — which app answers depends on which
    # bind is more specific for the address dialled. That is not a property an
    # operator reading a healthcheck or a firewall rule can see, and nothing here
    # needs it.
    for (a_name, a_bind, a_port), (b_name, b_bind, b_port) in (
            (("management", MANAGE_BIND, MANAGE_PORT),
             ("authorize", AUTHORIZE_BIND, AUTHORIZE_PORT)),
            (("management", MANAGE_BIND, MANAGE_PORT),
             ("tool", TOOL_BIND, TOOL_PORT)),
            (("authorize", AUTHORIZE_BIND, AUTHORIZE_PORT),
             ("tool", TOOL_BIND, TOOL_PORT))):
        if a_port == b_port:
            raise SystemExit(
                f"control-plane: the {a_name} listener ({a_bind}:{a_port}) and the "
                f"{b_name} listener ({b_bind}:{b_port}) share a port, so which "
                f"surface answers depends on which bind is more specific. Give each "
                f"listener its own port. Refusing to start.")


# ── entry point ─────────────────────────────────────────────────────────────

async def main() -> None:
    """Serve both listeners from one process and one event loop.

    ``uvicorn`` is imported HERE rather than at module scope so the module stays
    importable with only the stdlib — the unit suite loads this file directly with
    stub fastapi/pydantic and installs no packages (see tests/_loader.py)."""
    import uvicorn

    _assert_listeners_separated()
    _bootstrap()
    print(f"control-plane: authorize on {AUTHORIZE_BIND}:{AUTHORIZE_PORT}, "
          f"tool on {TOOL_BIND}:{TOOL_PORT}, "
          f"management on {MANAGE_BIND}:{MANAGE_PORT}", flush=True)

    # The ingest is a plain task on this loop rather than a lifespan hook, for the
    # same reason _bootstrap is not one: it belongs to the PROCESS, not to either
    # app. Holding the reference matters — asyncio keeps only a weak one, so a
    # create_task whose result nobody holds can be collected mid-flight and the
    # ingest would stop with no error anywhere.
    drain = None
    if ingest.DRAIN_INTERVAL > 0:
        drain = asyncio.create_task(ingest._audit_drain_loop())
    else:
        print("control-plane: audit ingest DISABLED "
              "(CONTROL_AUDIT_DRAIN_INTERVAL=0); locally-decided egress will "
              "appear only in the proxy's own log", flush=True)

    # Servers sharing one process means sharing one set of signal handlers,
    # and SIGTERM has to stop ALL of them or `docker compose down` waits out the grace
    # period and SIGKILLs the governance authority with holds in flight.
    #
    # uvicorn already handles this, and the mechanism is worth naming because it is
    # not obvious: each ``serve()`` wraps itself in ``capture_signals()``, so the
    # second server's handler replaces the first's — but on exit it restores what
    # it replaced and re-raises the signal it caught, which then reaches the first.
    # Measured with all three listeners (NOTES.md): SIGTERM logged a clean shutdown
    # for each and the process was gone inside a second. The chain is per-server
    # rather than pairwise (each ``capture_signals`` restores and re-raises for
    # exactly one ``serve()``), and it leans on a uvicorn internal — re-measure it
    # when the uvicorn pin moves. An earlier version of this function added handlers of
    # its own to "fix" the overwrite; they were inert — uvicorn installs via
    # ``signal.signal``, which displaces asyncio's — and removing them changed
    # nothing, so they are gone rather than kept as insurance.
    servers = [
        uvicorn.Server(uvicorn.Config(
            authorize_app, host=AUTHORIZE_BIND, port=AUTHORIZE_PORT,
            log_level="info")),
        uvicorn.Server(uvicorn.Config(
            tool_app, host=TOOL_BIND, port=TOOL_PORT, log_level="info")),
        uvicorn.Server(uvicorn.Config(
            app, host=MANAGE_BIND, port=MANAGE_PORT, log_level="info")),
    ]
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        if drain is not None:
            drain.cancel()


if __name__ == "__main__":
    asyncio.run(main())
