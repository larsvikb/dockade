# SPDX-License-Identifier: Apache-2.0
"""The task registry as a service: one process, one store, two listeners, each on its
own network and bound to that network's address alone.

  mcp-net             the agent listener (``agent_api``), which the gateway dials
                      with the secret it holds for server ``tasks``.
  tasks-operator-net  the operator listener (``operator_api``), for the tasks UI. It
                      takes no token: being on this network is the permission.

So where each binds, and whom each answers, is the boundary between the two writers.
``check_binds`` refuses to start on anything but one address inside each listener's
own network: the operator listener served on ``mcp-net`` would hand every MCP server,
and the gateway, the operator's writes, and a wildcard would do the same. Checked here,
before any socket opens, rather than trusting the compose file
(``task-registry/DESIGN.md``, "The operator API"). Each app is then wrapped in
``OnlyFrom``, since the bind alone does not decide which leg a packet arrives on.

One process because both listeners serve one SQLite store; two uvicorn servers on one
event loop, as ``control-plane/app.py`` runs its three.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os

import agent_api
import operator_api
import taskdb

AGENT_BIND = os.environ.get("TASKS_AGENT_BIND", "")
AGENT_PORT = int(os.environ.get("TASKS_AGENT_PORT", "8110"))
OPERATOR_BIND = os.environ.get("TASKS_OPERATOR_BIND", "")
OPERATOR_PORT = int(os.environ.get("TASKS_OPERATOR_PORT", "8111"))

#: The network each listener must bind inside: an allowlist, so a wildcard, a name
#: and an address on any other network are all refused. Defaults mirror
#: docker-compose.yml, and tests/test_topology.py holds them equal to its subnets.
AGENT_NET = os.environ.get("TASKS_AGENT_NET", "172.28.0.0/24")
OPERATOR_NET = os.environ.get("TASKS_OPERATOR_NET", "172.26.0.0/24")


def _network(var: str, value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    try:
        return ipaddress.ip_network(value)
    except ValueError as exc:
        raise SystemExit(f"task-registry: {var}={value!r} is not a network ({exc}). "
                         f"Refusing to start (fail closed).") from exc


def _inside(address: str, net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    try:
        return ipaddress.ip_address(address) in net
    except ValueError:
        return False


def _address(var: str, bind: str, net: ipaddress.IPv4Network | ipaddress.IPv6Network,
             why: str) -> None:
    if not _inside(bind, net):
        raise SystemExit(f"task-registry: {var}={bind!r} is not an address inside {net}. "
                         f"{why} Refusing to start (fail closed).")


def check_binds(agent_bind: str, operator_bind: str, agent_net: str,
                operator_net: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network,
                                            ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Refuse to start unless each listener binds one address inside its own network,
    and the two networks are apart. Returns the two networks, agent's first."""
    agent = _network("TASKS_AGENT_NET", agent_net)
    operator = _network("TASKS_OPERATOR_NET", operator_net)
    if agent.overlaps(operator):
        raise SystemExit(f"task-registry: TASKS_AGENT_NET {agent} and TASKS_OPERATOR_NET "
                         f"{operator} overlap, so the operator listener could bind where "
                         f"the MCP servers are. Refusing to start (fail closed).")
    _address("TASKS_OPERATOR_BIND", operator_bind, operator,
             "The operator listener takes no token, so anything that reaches it writes "
             "as the operator: it is served on its own network's address alone, never "
             "on mcp-net, where every MCP server and the gateway would reach it.")
    _address("TASKS_AGENT_BIND", agent_bind, agent,
             "The agent listener is served on its mcp-net address alone, where the "
             "gateway dials it.")
    return agent, operator


#: What either listener answers a peer outside its network, in the operator API's
#: shape.
OFF_NETWORK = (b'{"ok": false, "detail": "this listener answers its own network only", '
               b'"current": null}')


class OnlyFrom:
    """An app that answers only peers inside ``net``.

    Binding one address does not keep a listener off the other leg: Linux takes a
    packet for any of its addresses on any interface, so a neighbour on ``mcp-net``
    that could route one to 172.26.0.2 there would reach the operator listener. None
    can today, since every service drops every capability (tests/test_topology.py),
    and this holds whether or not that does. A forged source gets nowhere either: the
    reply leaves by the leg that address is on, so the handshake never completes.
    Uvicorn's proxy headers are off, so the peer is the socket's."""

    def __init__(self, app: object, net: ipaddress.IPv4Network | ipaddress.IPv6Network
                 ) -> None:
        self.app, self.net = app, net

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] != "lifespan":
            client = scope.get("client")
            if not client or not _inside(client[0], self.net):
                if scope["type"] == "http":
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": OFF_NETWORK})
                else:
                    # Refused before the handshake, which uvicorn answers with a 403.
                    await send({"type": "websocket.close"})
                return
        await self.app(scope, receive, send)


async def main() -> None:
    """Check the binds, load the secret and open the store, then serve: each step
    before any listener opens, so a registry that cannot run as configured never
    accepts a request."""
    # Here rather than at module scope, so the suite imports this with no uvicorn.
    import uvicorn

    agent_net, operator_net = check_binds(AGENT_BIND, OPERATOR_BIND, AGENT_NET,
                                          OPERATOR_NET)
    agent_app = agent_api.build(agent_api.load_secret(agent_api.SECRET_FILE))
    taskdb.init()
    print(f"task-registry: agent listener on {AGENT_BIND}:{AGENT_PORT}, operator "
          f"listener on {OPERATOR_BIND}:{OPERATOR_PORT}, store {taskdb.DB_PATH}",
          flush=True)
    # SIGTERM stops both: control-plane/app.py's main says how uvicorn does that for
    # several servers on one loop, and that this leans on a uvicorn internal.
    servers = [
        uvicorn.Server(uvicorn.Config(OnlyFrom(agent_app, agent_net), host=AGENT_BIND,
                                      port=AGENT_PORT, proxy_headers=False,
                                      log_level="info")),
        uvicorn.Server(uvicorn.Config(OnlyFrom(operator_api.app, operator_net),
                                      host=OPERATOR_BIND, port=OPERATOR_PORT,
                                      proxy_headers=False, log_level="info")),
    ]
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
