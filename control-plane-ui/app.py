"""
Control-plane UI — the human-facing frontend (step 2b-2).

A distinct container from the control-plane BACKEND, with its own lifecycle. It
exists so the backend (policy + audit — the crown-jewel state) can stay on
`control-net` only and fully `internal`, with ZERO non-internal surface. This
frontend takes the one unavoidable non-internal surface (the host-loopback UI
publish on `control-ui-net`) and holds no state of its own.

It does two things:
  - serves the static approval UI at `/`;
  - reverse-proxies every other path (the approvals API + the SSE stream) to the
    backend over `control-net`.

The browser only ever talks to this frontend (same-origin), which relays to the
backend the browser cannot reach. Deliberately dumb: no auth, no policy, no
storage — a governance decision never depends on it (the egress proxy talks to
the backend directly). If this container is compromised, it has the soft-egress
`control-ui-net` surface but nothing worth exfiltrating.
"""
from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

BACKEND = os.environ.get(
    "CONTROL_BACKEND_URL", "http://control-plane:8090").rstrip("/")
UI_INDEX = os.environ.get("CONTROL_UI_INDEX", "/opt/control-plane-ui/index.html")

app = FastAPI(title="dockade control-plane UI", version="2b-2")

# timeout=None: the SSE stream (/approvals/stream) is long-lived; other calls are
# fast. Reused across requests for connection pooling to the backend.
_client = httpx.AsyncClient(base_url=BACKEND, timeout=None)  # noqa: S113 (deliberate — see above)

# Hop-by-hop / framing headers we must not copy through a streaming relay.
_STRIP_REQ = {"host"}
_STRIP_RESP = {"content-length", "transfer-encoding", "connection"}


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_INDEX, media_type="text/html")


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request) -> StreamingResponse:
    """Relay everything else to the backend, streaming the response so the SSE
    approvals feed flows through unbuffered.

    The upstream is PINNED to BACKEND. `{path:path}` retains a leading slash for a
    request like `//evil.com/x` (captured as `/evil.com/x`), and the old `"/" +
    path` turned that back into `//evil.com/x` — a network-path reference httpx
    resolves against the base as a DIFFERENT authority (`http://evil.com/x`), i.e.
    a caller-controlled host override / SSRF primitive. Collapsing to a single
    leading slash keeps the request a plain absolute-path reference, so the host
    always stays BACKEND."""
    upstream_path = "/" + path.lstrip("/")
    req = _client.build_request(
        request.method, upstream_path,
        params=request.query_params,
        content=await request.body(),
        headers=[(k, v) for k, v in request.headers.items()
                 if k.lower() not in _STRIP_REQ],
    )
    resp = await _client.send(req, stream=True)
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in _STRIP_RESP},
        background=BackgroundTask(resp.aclose),
    )
