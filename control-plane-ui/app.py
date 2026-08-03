"""
Control-plane UI — the human-facing frontend (step 2b-2).

A distinct container from the control-plane BACKEND, with its own lifecycle. It
exists so the backend (policy + audit — the crown-jewel state) can stay on
`control-net` only and fully `internal`, with ZERO non-internal surface. This
frontend takes the one unavoidable non-internal surface (the host-loopback UI
publish on `control-ui-net`) and holds no state of its own.

It does two things:
  - serves the static approval UI at `/`;
  - reverse-proxies the approvals API + SSE stream to the backend over `control-net`.

The browser only ever talks to this frontend (same-origin), which relays to the
backend the browser cannot reach. Still deliberately dumb — no policy, no storage,
and a governance decision never depends on it (the egress proxy talks to the
backend directly).

BUT it is not indifferent to WHO is calling, because it publishes the one API that
can grant egress: `POST /approvals/{id}/resolve` is self-approval if reached. It
therefore enforces three cheap, structural guards (`_guard` below, plus the relay
allowlist):

  1. **Host allowlist** — closes DNS REBINDING, which is the vector that actually
     matters here. Binding to host loopback is no defense against it: a page the
     operator visits can make its own name resolve to 127.0.0.1, at which point it
     is SAME-ORIGIN with this app — no preflight, and responses are readable, so it
     can list pending approvals (getting the unguessable ids) and resolve them.
     Rebinding structurally requires `Host: <attacker domain>`, and `Host` is a
     forbidden header that JS cannot set, so refusing unexpected Hosts removes the
     class rather than raising its cost.
  2. **Cross-origin state change refused** — closes CSRF. Largely self-blocking
     already (a JSON body forces a preflight that fails for want of CORS headers,
     and `resolve` needs a uuid4 id a blind caller cannot guess), so this is
     belt-and-braces for the case where a future endpoint is less lucky.
  3. **Relay path allowlist** — the backend surface is NOT all equally suitable for
     a browser. `POST /authorize` is the proxy's decision endpoint; reaching it from
     a page means forged audit rows and consumed hold slots. Only the paths this UI
     actually uses are relayed.

What these guards CANNOT do, stated plainly so the boundary is not overclaimed:
they are BROWSER-ENFORCED. A process running on the host sets any header it likes,
so it can still reach this API. That matters here specifically because the agent's
RW workspace bind mount is an acknowledged (delayed) path to host execution — see
DESIGN.md. Authentication would not fix it either, as any credential at rest on the
host is readable by that same process; closing it needs a human-presence gesture
the host cannot replay. What this app does instead is make the attempt VISIBLE: it
strips client-supplied provenance headers and asserts the real peer address, which
the backend records on the approval row and in the audit reason (`_actor` there).
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.background import BackgroundTask

BACKEND = os.environ.get(
    "CONTROL_BACKEND_URL", "http://control-plane:8090").rstrip("/")
UI_INDEX = os.environ.get("CONTROL_UI_INDEX", "/opt/control-plane-ui/index.html")


def _hostnames(env: str, default: str) -> frozenset[str]:
    return frozenset(h.strip().lower() for h in os.environ.get(env, default).split(",")
                     if h.strip())


# Hostnames this app may be addressed as. Compared WITHOUT the port on purpose:
# rebinding needs the attacker's own NAME, so the port is irrelevant to the guard,
# and ignoring it keeps one list valid for both the published host port and the
# in-container healthcheck (which addresses 127.0.0.1:8090 directly).
#
# Widen this only for a deployment that legitimately fronts the UI under another
# name (a reverse proxy, or access from another machine) — and understand that
# doing so re-opens rebinding for that name unless the front end authenticates.
ALLOWED_HOSTNAMES = _hostnames("CONTROL_UI_ALLOWED_HOSTS",
                               "127.0.0.1,localhost,::1")

# Methods that CHANGE STATE, so cross-origin callers must be refused. GET is left
# to the Host guard alone: a cross-origin GET's response is unreadable without CORS
# headers (which this app never sends), and the readable case IS rebinding, which
# guard 1 already blocks.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Exactly the backend paths this UI needs, as (method, pattern). Everything else —
# above all `POST /authorize` — is refused rather than relayed. The approval id is
# bounded and slash-free so nothing path-shaped can be smuggled through it.
_RELAY_ROUTES = (
    ("GET", re.compile(r"^/approvals$")),
    ("GET", re.compile(r"^/approvals/stream$")),
    ("POST", re.compile(r"^/approvals/[A-Za-z0-9._-]{1,128}/resolve$")),
    ("GET", re.compile(r"^/api/audit$")),
    ("GET", re.compile(r"^/api/rules$")),
    ("GET", re.compile(r"^/status$")),
)

# Provenance headers a CLIENT must never be able to set, because the backend reads
# ACTOR_HEADER as this relay's own assertion about the browser. Stripped from every
# inbound request before ACTOR_HEADER is re-added with the real peer address —
# otherwise a caller could self-report as any actor and the audit record would
# faithfully repeat the lie. `host` is stripped for a different reason: httpx must
# set it for the backend.
ACTOR_HEADER = "x-dockade-actor"
_STRIP_REQ = {"host", ACTOR_HEADER, "x-forwarded-for", "x-forwarded-host",
              "x-forwarded-proto", "x-real-ip", "forwarded"}

# Hop-by-hop / framing headers we must not copy through a streaming relay.
_STRIP_RESP = {"content-length", "transfer-encoding", "connection"}

app = FastAPI(title="dockade control-plane UI", version="2b-2")

# timeout=None: the SSE stream (/approvals/stream) is long-lived; other calls are
# fast. Reused across requests for connection pooling to the backend.
_client = httpx.AsyncClient(base_url=BACKEND, timeout=None)  # noqa: S113 (deliberate — see above)


def _assert_host_guard_configured() -> None:
    """Fail CLOSED at import (so the container dies visibly instead of serving) if
    the Host allowlist is empty. An empty list would make `_guard` refuse every
    request, not permit them — but a guard that is silently misconfigured into
    uselessness-or-outage is exactly what the repo's "no silent unsafe defaults"
    rule exists to prevent, and the operator needs to be told which variable did it.
    Mirrors the egress addon's `_assert_guard_configured`."""
    if not ALLOWED_HOSTNAMES:
        raise RuntimeError(
            "CONTROL_UI_ALLOWED_HOSTS is empty — the Host allowlist is what closes "
            "DNS rebinding against this UI, and with no entries every request is "
            "refused. Refusing to start (fail closed). Set it to the hostname(s) "
            "this UI is addressed as, e.g. 127.0.0.1,localhost,::1.")


_assert_host_guard_configured()


def _hostname_of(value: str | None) -> str | None:
    """Hostname from a `Host` header (a bare authority) or an `Origin` (an absolute
    URL). Routed through urlsplit so port stripping, `[v6]` unwrapping and
    lowercasing are the stdlib's problem rather than ours; a schemeless authority is
    given the `//` prefix that makes it a network-path reference."""
    v = (value or "").strip()
    if not v:
        return None
    if "://" not in v:
        v = "//" + v
    try:
        return urlsplit(v).hostname
    except ValueError:
        return None


def _refuse(detail: str) -> PlainTextResponse:
    """One shape for every guard refusal. 403 rather than 404: this surface is
    loopback-only, so leaking that a path exists costs nothing next to an operator
    being able to tell a guard refusal from a missing route while debugging."""
    return PlainTextResponse(f"refused by control-plane-ui: {detail}\n",
                             status_code=403)


@app.middleware("http")
async def _guard(request: Request, call_next):
    """Host + cross-origin guards, as MIDDLEWARE so they cover every route
    (including `/` and `/healthz`) and cannot be forgotten on a route added later."""
    hostname = _hostname_of(request.headers.get("host"))
    if hostname not in ALLOWED_HOSTNAMES:
        # Also catches a missing Host, which HTTP/1.1 forbids anyway.
        return _refuse(f"unexpected Host {hostname or '<absent>'!r} "
                       f"(allowed: {sorted(ALLOWED_HOSTNAMES)}) — possible DNS rebinding")
    if request.method.upper() in _UNSAFE_METHODS:
        # Prefer Sec-Fetch-Site when the browser sends it: it states the relationship
        # directly and, like Host and Origin, JS cannot forge it. Absent (older
        # browser, or a non-browser client) fall back to Origin. BOTH absent means no
        # browser is making this request, so there is no CSRF to prevent — let it
        # through, which is also what keeps curl/scripting usable.
        site = (request.headers.get("sec-fetch-site") or "").lower()
        if site:
            if site != "same-origin":
                return _refuse(f"cross-origin state change (Sec-Fetch-Site: {site})")
        else:
            origin = request.headers.get("origin")
            if origin and _hostname_of(origin) not in ALLOWED_HOSTNAMES:
                return _refuse(f"cross-origin state change (Origin: {origin})")
    return await call_next(request)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_INDEX, media_type="text/html")


def _relay_allowed(method: str, path: str) -> bool:
    return any(method == m and rx.match(path) for m, rx in _RELAY_ROUTES)


# Annotated as the base Response, NOT `StreamingResponse | PlainTextResponse`: this
# handler genuinely returns either (a relay stream, or a guard refusal), but FastAPI
# derives a response MODEL from the return annotation and only skips that for a
# Response SUBCLASS — a union is not one, so annotating the union makes the app raise
# FastAPIError at import and the container never starts. Caught only by exercising
# real FastAPI; the stubbed unit tests ignore annotations entirely.
@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request) -> Response:
    """Relay an allowlisted path to the backend, streaming the response so the SSE
    approvals feed flows through unbuffered.

    The upstream is PINNED to BACKEND. `{path:path}` retains a leading slash for a
    request like `//evil.com/x` (captured as `/evil.com/x`), and the old `"/" +
    path` turned that back into `//evil.com/x` — a network-path reference httpx
    resolves against the base as a DIFFERENT authority (`http://evil.com/x`), i.e.
    a caller-controlled host override / SSRF primitive. Collapsing to a single
    leading slash keeps the request a plain absolute-path reference, so the host
    always stays BACKEND."""
    upstream_path = "/" + path.lstrip("/")
    method = request.method.upper()
    if not _relay_allowed(method, upstream_path):
        return _refuse(f"{method} {upstream_path} is not a relayed path")
    peer = getattr(request.client, "host", None) or "unknown"
    req = _client.build_request(
        method, upstream_path,
        params=request.query_params,
        content=await request.body(),
        # Strip client-supplied provenance, then assert the real peer ourselves.
        headers=[(k, v) for k, v in request.headers.items()
                 if k.lower() not in _STRIP_REQ] + [(ACTOR_HEADER, peer)],
    )
    resp = await _client.send(req, stream=True)
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in _STRIP_RESP},
        background=BackgroundTask(resp.aclose),
    )
