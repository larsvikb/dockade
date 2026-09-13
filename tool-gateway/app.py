#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""MCP gateway — the governed tool data plane.

Speaks MCP to the sandbox on one side and to the MCP server containers on the
other, exposing a curated tool set under per-tool allow/deny/ask policy held in
the control plane. It is to tool capability what the egress proxy is to network
capability. See DESIGN.md, "MCP gateway".

THIS MODULE IS THE PLACEMENT, NOT THE PROTOCOL. It stands the service up on its
three legs and refuses to start on a configuration that undoes the split; it
serves no tools yet. That order is deliberate and matches how both control-plane
bridges landed — the surface is asserted while it is still cheap to change, and
the placement is the part that is expensive to retrofit because every other
component's guard has to agree with it.

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
import os

from fastapi import FastAPI

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


def main() -> None:
    """Assert placement, then serve.

    The guard runs BEFORE the listener opens, so a misconfigured gateway never
    accepts a connection it should not have accepted. Fail-closed here means the
    container dies and `restart: always` retries it — visible in `docker compose
    ps` as a restart loop, which is the intended operator experience for a
    configuration that cannot be served safely."""
    _assert_bind_is_agent_facing_only()

    # Imported here rather than at module scope so the module stays importable by
    # the test suite without pulling in the server. Same reason control-plane/app.py
    # defers its uvicorn import.
    import uvicorn

    print(f"tool-gateway: serving the agent-facing MCP endpoint on "
          f"{AGENT_BIND}:{AGENT_PORT} (this address only)", flush=True)
    uvicorn.run(app, host=AGENT_BIND, port=AGENT_PORT, log_level="info")


if __name__ == "__main__":
    main()
