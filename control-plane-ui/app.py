# SPDX-License-Identifier: Apache-2.0
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
  4. **Refusal to be EMBEDDED** (`frame-ancestors 'none'` in the CSP, plus a
     `Sec-Fetch-Dest` check) — closes CLICKJACKING, which guards 1 and 2 cannot see.
     An attacker page that frames this UI sends a perfectly legitimate
     `Host: 127.0.0.1`, and the framed document's own `resolve` POST is *same-origin
     from inside the frame*, so both guards pass; cross-origin reads stay blocked, but
     a UI-redress click needs no read — it lands on the real "Allow + persist rule"
     button. See `_CSP` / `_FRAMED_DESTS`.

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
# The page's behaviour, served as a separate file rather than inline in the HTML.
# That is what lets the CSP below say `script-src 'self'` instead of
# `'unsafe-inline'` — an inline-script allowance makes the whole policy decorative
# against injection — and it is what makes the frontend's decision logic testable
# (tests/test_control_plane_ui_js.py).
UI_SCRIPT = os.environ.get("CONTROL_UI_SCRIPT", "/opt/control-plane-ui/app.js")


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
#
# EXACTLY the paths it needs, and that word is the maintenance rule rather than a
# description: a default-deny allowlist is only worth what its entries are worth, and
# an entry with no caller cannot be reasoned about, because nothing would break if it
# were wrong. Two sat here unused. `/status` was a harmless plain-text summary whose
# three counts are either already on the page or prose the frontend would have had to
# parse. `GET /approvals` is the non-streaming form of the pending list, superseded by
# the SSE stream the page actually opens — and unlike /status it carries the pending
# hosts, clients and URLs, so an uncalled route was relaying real data.
#
# Both remain reachable on control-net for `docker exec` debugging, which is what they
# are for; only the BROWSER's path to them is gone. Add a route here when a fetch needs
# it, never in anticipation of one — a test asserts this list and app.js agree in both
# directions.
_RELAY_ROUTES = (
    ("GET", re.compile(r"^/approvals/stream$")),
    ("POST", re.compile(r"^/approvals/[A-Za-z0-9._-]{1,128}/resolve$")),
    ("GET", re.compile(r"^/api/audit$")),
    ("GET", re.compile(r"^/api/rules$")),
    ("GET", re.compile(r"^/api/config$")),
    ("POST", re.compile(r"^/api/saturation/ack$")),
)

# Provenance headers a CLIENT must never be able to set, because the backend reads
# ACTOR_HEADER as this relay's own assertion about the browser. Stripped from every
# inbound request before ACTOR_HEADER is re-added with the real peer address —
# otherwise a caller could self-report as any actor and the audit record would
# faithfully repeat the lie. `host` is stripped for a different reason: httpx must
# set it for the backend.
ACTOR_HEADER = "x-dockade-actor"
_SPOOFABLE = {"host", ACTOR_HEADER, "x-forwarded-for", "x-forwarded-host",
              "x-forwarded-proto", "x-real-ip", "forwarded"}

# Hop-by-hop headers (RFC 9110 §7.6.1). They describe the CONNECTION they arrived on
# rather than the message, so a relay must not copy them onto its own connection —
# in EITHER direction. This used to be applied to responses only, which left the
# request side forwarding whatever the browser sent.
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailer", "transfer-encoding",
               "upgrade"}

# Framing this relay owns and must therefore not inherit from its caller.
#
# `content-length` is not hop-by-hop, and that is exactly why it needs saying: this
# relay reads the body and re-sends it, so httpx sets the length from the content it
# is given. A copied value that disagrees with that body is the classic
# request-smuggling primitive, not a cosmetic mismatch — and `transfer-encoding:
# chunked` arriving alongside a fixed-length re-send is the same hazard by the other
# spelling, which is why the hop-by-hop set above is applied to requests now too.
#
# `expect` is here for an unrelated reason: `expect: 100-continue` makes the backend
# wait for an interim response this relay never produces.
_STRIP_FRAMING = {"content-length", "expect"}

_STRIP_REQ = _SPOOFABLE | _HOP_BY_HOP | _STRIP_FRAMING
# Responses need the framing stripped (the body is re-streamed) but not `expect`,
# which is a request header and would be meaningless here.
_STRIP_RESP = _HOP_BY_HOP | {"content-length"}

# Fetch destinations that mean "this response is being EMBEDDED in another page".
# Refused because neither guard above can see the difference: an attacker page that
# frames http://127.0.0.1:8081 causes a request with a legitimate `Host: 127.0.0.1`
# (guard 1 passes) using GET, which is deliberately allowed cross-origin (guard 2
# does not apply) — and the framed page's own resolve POST then reads as
# `Sec-Fetch-Site: same-origin`, because from inside the frame it is. The attacker
# cannot READ the framed UI, but clickjacking does not need a read: overlay a decoy,
# and the operator's click lands on the real "Allow + persist rule" button.
#
# `frame-ancestors 'none'` in _CSP is the primary control; this check is not
# redundant with it. Both are browser-enforced, but refusing the embedded document
# HERE means the attempt appears as a 403 in this app's log rather than only in the
# operator's console — visibility being what this repo falls back on wherever
# prevention is browser-dependent (see the provenance note above).
_FRAMED_DESTS = frozenset({"iframe", "frame", "embed", "object"})

# Sent on EVERY response, refusals included (see `_security_headers`).
#
# `default-src 'none'` then names only what the page genuinely uses: its own script
# (`'self'` — hence app.js being a file), the inline <style> block, the data-URI
# favicon under img-src, and same-origin fetch/EventSource under connect-src. The
# effect worth having is that the pending list renders AGENT-CONTROLLED strings
# (host, url, client), so if an escaping mistake ever became script, that script
# could still reach no external origin, load nothing, and submit nowhere.
_CSP = ("default-src 'none'; "
        "script-src 'self'; "
        "style-src 'unsafe-inline'; "
        "img-src data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'")

_SECURITY_HEADERS = {
    "content-security-policy": _CSP,
    # Redundant with frame-ancestors on any current browser; kept because it costs a
    # header and covers an old one that honours neither Sec-Fetch-Dest nor CSP 2.
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    # This origin's URLs name approval ids; nothing should carry them outward.
    "referrer-policy": "no-referrer",
}

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
    dest = (request.headers.get("sec-fetch-dest") or "").lower()
    if dest in _FRAMED_DESTS:
        return _refuse(f"refusing to be embedded (Sec-Fetch-Dest: {dest}) — "
                       f"clickjacking of the approval buttons")
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


# Declared AFTER `_guard` on purpose: with Starlette's `@app.middleware("http")` the
# last-registered middleware is the OUTERMOST, so this one wraps the guard and
# therefore also hardens its 403 refusals — including the refused framing attempt,
# whose response a browser would otherwise render with no policy at all.
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Set the browser-facing security headers on every response.

    Kept separate from `_guard` rather than folded into it because the two do
    different jobs: `_guard` decides whether to serve at all, this decides what the
    browser is permitted to do with what was served. Plain assignment, not
    `setdefault`: these responses include RELAYED backend ones, and a header from
    upstream must not be able to weaken the frontend's policy."""
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_INDEX, media_type="text/html")


@app.get("/app.js")
def script() -> FileResponse:
    """The page's behaviour. A local static file — never a CDN reference, for the same
    reason the favicon is an inline data URI: a governance UI must not fetch its own
    control logic from a third party."""
    return FileResponse(UI_SCRIPT, media_type="text/javascript")


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
    try:
        resp = await _client.send(req, stream=True)
    except httpx.RequestError as exc:
        # The backend is down, restarting, or unresolvable. Answer a clean 502 rather
        # than letting the exception surface as a 500 with a traceback.
        #
        # This is load-bearing for the SSE feed, not just tidiness. An EventSource
        # treats ANY non-200 as a PERMANENT close (readyState CLOSED, no built-in
        # retry), so a backend restart used to leave the page silently blind while its
        # status line still read "reconnecting…". app.js now reconnects by hand from
        # CLOSED with backoff; a legible 502 is what it reconnects from.
        return PlainTextResponse(
            f"control-plane backend unreachable: {type(exc).__name__}\n",
            status_code=502)
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in _STRIP_RESP},
        background=BackgroundTask(resp.aclose),
    )
