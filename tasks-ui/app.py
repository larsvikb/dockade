# SPDX-License-Identifier: Apache-2.0
"""
Tasks UI — the operator's page onto the task registry, a container of its own.

The approval UI is the page that grants egress; this one renders what agents wrote: task
bodies, notes, history. So it is another origin (another loopback port), and it has no
leg on `control-net`: nothing that reads the registry, which parses agent text, is a
step from the management API (DESIGN.md, "Task registry"). Its legs are `tasks-ui-net`,
where the loopback publish arrives, and `tasks-operator-net`, where it dials the
registry's operator listener.

It does two things:
  - serves the static page at `/`;
  - relays the page's reads to the registry's operator listener, which takes no token,
    and answers `/running` itself, so a registry that is down is a state of the page
    rather than a failed request in its console.

It serves one address, its `tasks-ui-net` one, and `guards.check_bind` refuses to start
on any other: the relay is the operator's whole say in the registry, and its other leg's
neighbour parses agent text. The browser guards are ui-common's (`guards.py`, which
says what each closes); the fourth is the relay's table below.
"""
from __future__ import annotations

import os
import re

import guards
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.background import BackgroundTask

#: The name every refusal and startup failure here carries.
NAME = "tasks-ui"
#: The task registry's operator listener. By its pinned address, not its name: this
#: container has a non-internal leg, so a name Docker's resolver does not know is asked
#: of the host's DNS, and `tasks` is one whenever the registry is stopped.
REGISTRY_URL = os.environ.get("TASKS_UI_REGISTRY_URL", "http://172.26.0.2:8111").rstrip("/")
BIND = os.environ.get("TASKS_UI_BIND", "")
PORT = int(os.environ.get("TASKS_UI_PORT", "8091"))
#: The network BIND must sit inside. The default mirrors docker-compose.yml, and
#: tests/test_topology.py holds it equal to tasks-ui-net's subnet.
UI_NET = os.environ.get("TASKS_UI_NET", "172.24.0.0/24")
UI_INDEX = os.environ.get("TASKS_UI_INDEX", "/opt/tasks-ui/index.html")
# The page's modules, served as files so the CSP can say `script-src 'self'`. This list
# is the whole of what the script route hands out; a test holds it equal to the files
# the image copies and to what the modules import from each other.
UI_DIR = os.environ.get("TASKS_UI_DIR", "/opt/tasks-ui")
UI_MODULES = frozenset({"app.js", "escape.js", "list-status.js", "tasks.js", "time.js"})
# Built from the list, never from a request: the name off the URL only picks an entry.
_UI_MODULE_PATHS = {name: os.path.join(UI_DIR, name) for name in UI_MODULES}

# Hostnames this page may be addressed as, compared without the port, for the reason
# `ALLOWED_HOSTNAMES` in control-plane-ui/app.py gives. The healthcheck dials
# TASKS_UI_BIND and names 127.0.0.1 as its Host.
ALLOWED_HOSTNAMES = guards.allowed_hostnames("TASKS_UI_ALLOWED_HOSTS",
                                             "127.0.0.1,localhost,::1")

# The registry's operator API (task-registry/operator_api.py), exactly the routes the
# page calls and nothing else: that listener takes no token, so this table is the whole
# of what a page can do there. Ids are digits, bounded, so no path-shaped segment can be
# smuggled through one. Add a route when a fetch needs it, never in anticipation; a test
# holds this table and tasks.js's fetches equal in both directions. Reads only, so far.
_RELAY_ROUTES = (
    ("GET", re.compile(r"^/api/repos$")),
    ("GET", re.compile(r"^/api/tasks$")),
    ("GET", re.compile(r"^/api/tasks/[0-9]{1,19}$")),
    # A task's every note, hidden ones marked, and its history: the operator's reads,
    # which no agent surface offers (task-registry/DESIGN.md, "Notes").
    ("GET", re.compile(r"^/api/tasks/[0-9]{1,19}/notes$")),
    ("GET", re.compile(r"^/api/tasks/[0-9]{1,19}/history$")),
    # Every plan a task had, superseded ones marked: what a plan replaced.
    ("GET", re.compile(r"^/api/tasks/[0-9]{1,19}/plans$")),
    ("GET", re.compile(r"^/api/notes/flagged$")),
    ("GET", re.compile(r"^/api/search$")),
)

# What a page may not send on to the registry: a claim about who or where it is, a
# hop-by-hop header, or framing this relay owns. The registry reads none of the first,
# but a relay copying the others onto its own connection is a smuggling primitive
# whichever upstream it is. The approval UI strips the same set, and says why for each
# (`_STRIP_REQ` in control-plane-ui/app.py); a test holds the two equal.
_STRIP_REQ = {"host", "x-dockade-actor", "x-forwarded-for", "x-forwarded-host",
              "x-forwarded-proto", "x-real-ip", "forwarded",
              "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailer", "transfer-encoding", "upgrade",
              "content-length", "expect"}

# No schema or docs pages: what this server hands out stays the list a human wrote
# (tests/test_topology.py).
app = FastAPI(title="dockade tasks UI", openapi_url=None, docs_url=None, redoc_url=None)
guards.install(app, NAME, ALLOWED_HOSTNAMES)

# Bounded: nothing here streams, and a stopped registry's address answers nothing at
# all, which the page must learn in seconds rather than never.
_client = httpx.AsyncClient(base_url=REGISTRY_URL,
                            timeout=httpx.Timeout(10.0, connect=2.0))


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# Revalidated on every load, for the reason `_REVALIDATE` in control-plane-ui/app.py
# gives: with validators and no policy a browser may run a stale script.
_REVALIDATE = {"cache-control": "no-cache"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_INDEX, media_type="text/html", headers=_REVALIDATE)


@app.get("/{name}.js")
def script(name: str) -> Response:
    """The page's behaviour, one module per request, from this container only."""
    path = _UI_MODULE_PATHS.get(f"{name}.js")
    if path is None:
        return PlainTextResponse("not found\n", status_code=404)
    return FileResponse(path, media_type="text/javascript", headers=_REVALIDATE)


def _relay_allowed(method: str, path: str) -> bool:
    return any(method == m and rx.match(path) for m, rx in _RELAY_ROUTES)


@app.get("/running")
async def running() -> dict:
    """Whether the task registry answers, asked by the page before it calls it. The
    registry is optional and can stop on its own, so not running is a state rather
    than an error. Its repo list is the cheapest read it serves."""
    try:
        resp = await _client.get("/api/repos")
    except httpx.RequestError:
        return {"running": False}
    return {"running": resp.status_code == 200}


# Annotated as the base Response, not a union, for the reason given on `proxy` in
# control-plane-ui/app.py: FastAPI raises at import on a union return annotation.
@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request) -> Response:
    """Relay an allowlisted path to the registry.

    The upstream is PINNED to REGISTRY_URL: `{path:path}` keeps a leading slash of a
    request like `//evil.com/x`, and `"/" + path` would make that a network-path
    reference httpx resolves as another authority. Collapsing to one leading slash
    keeps the request an absolute-path reference."""
    upstream_path = "/" + path.lstrip("/")
    method = request.method.upper()
    if not _relay_allowed(method, upstream_path):
        return guards.refuse(NAME, f"{method} {upstream_path} is not a relayed path")
    req = _client.build_request(
        method, upstream_path,
        params=request.query_params,
        content=await request.body(),
        headers=[(k, v) for k, v in request.headers.items()
                 if k.lower() not in _STRIP_REQ],
    )
    try:
        resp = await _client.send(req, stream=True)
    except httpx.RequestError as exc:
        return _registry_unreachable(exc)
    # The registry parses agent text. What it answers is JSON, and none of its other
    # headers is this origin's to carry.
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers={"content-type": "application/json"},
        background=BackgroundTask(resp.aclose),
    )


def _registry_unreachable(exc: Exception) -> JSONResponse:
    """The task registry did not answer: stopped, starting, or not on this network.
    In the operator API's shape, and a status the registry itself never sends, so the
    page can tell it from a refusal."""
    return JSONResponse({"ok": False, "current": None,
                         "detail": f"the task registry did not answer "
                                   f"({type(exc).__name__})"}, status_code=502)


def main() -> None:
    # Here rather than at module scope, so the suite imports this with no uvicorn.
    import uvicorn

    network = guards.check_bind(
        BIND, UI_NET, name=NAME, bind_variable="TASKS_UI_BIND",
        net_variable="TASKS_UI_NET",
        why="The relay is the operator's say in the task registry, so it is served on its "
            "tasks-ui-net address alone, where the loopback publish arrives, and not on "
            "the registry's network, whose other member parses agent text.")
    # Proxy headers off: the peer OnlyFrom admits is the socket's.
    uvicorn.run(guards.OnlyFrom(app, network, NAME), host=BIND, port=PORT,
                proxy_headers=False, log_level="info")


if __name__ == "__main__":
    main()
