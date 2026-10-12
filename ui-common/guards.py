# SPDX-License-Identifier: Apache-2.0
"""
The browser guards every dockade UI serves behind, and the bind and peer checks it is
started with. One implementation, so a UI that renders agent text cannot be the one
that drifted: an app installs them (`install`), and its `main` runs `check_bind`
first and serves the app inside `OnlyFrom`.

A UI on host loopback is not indifferent to WHO is calling: the control-plane UI's
`POST /approvals/{id}/resolve` is self-approval if reached. Three cheap, structural
guards (`request_guard`) stand in front of every route:

  1. **Host allowlist** — closes DNS REBINDING, which is the vector that actually
     matters here. Binding to host loopback is no defense against it: a page the
     operator visits can make its own name resolve to 127.0.0.1, at which point it
     is SAME-ORIGIN with the UI — no preflight, and responses are readable, so it
     can list pending approvals (getting the unguessable ids) and resolve them.
     Rebinding structurally requires `Host: <attacker domain>`, and `Host` is a
     forbidden header that JS cannot set, so refusing unexpected Hosts removes the
     class rather than raising its cost.
  2. **Cross-origin state change refused** — closes CSRF, and it is load-bearing
     rather than belt-and-braces. A JSON body does NOT force a preflight: a `Blob`
     with no type is sent with no `Content-Type`, which is CORS-safelisted, and
     FastAPI parses an untyped body as JSON. Nor is the `resolve` id blind for a
     tool ask — the agent is handed it in the pending result. So the relationship
     has to be checked: `Sec-Fetch-Site` when the browser sends it, else `Origin`
     compared as a full authority (host AND port) against the request's own Host,
     because a page on another localhost port is another origin.
  3. **Refusal to be EMBEDDED** (`frame-ancestors 'none'` in the CSP, plus a
     `Sec-Fetch-Dest` check) — closes CLICKJACKING, which guards 1 and 2 cannot see.
     An attacker page that frames a UI sends a perfectly legitimate
     `Host: 127.0.0.1`, and the framed document's own POST is *same-origin from
     inside the frame*, so both guards pass; cross-origin reads stay blocked, but a
     UI-redress click needs no read — it lands on the real "Allow + persist rule"
     button. See `CSP` / `_FRAMED_DESTS`.

The fourth guard is each app's own: the table of upstream paths it relays.

They are BROWSER-ENFORCED: a process running on the host sets any header it likes.
`control-plane-ui/app.py` says what that means for the approval UI and what it does
instead.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import PlainTextResponse


def allowed_hostnames(variable: str, default: str) -> frozenset[str]:
    """The hostnames a UI may be addressed as, from ``variable``.

    Compared WITHOUT the port on purpose: rebinding needs the attacker's own NAME, so
    the port is irrelevant to the guard, and ignoring it keeps the list valid whatever
    port the host publishes. Widen it only for a deployment that legitimately fronts a
    UI under another name (a reverse proxy, or access from another machine), and
    understand that doing so re-opens rebinding for that name unless the front end
    authenticates.

    Fails CLOSED (so the container dies visibly instead of serving) on an empty list.
    An empty list would make the guard refuse every request, not permit them — but a
    guard that is silently misconfigured into uselessness-or-outage is exactly what the
    repo's "no silent unsafe defaults" rule exists to prevent, and the operator needs
    to be told which variable did it. Mirrors the egress addon's
    `_assert_guard_configured`."""
    hosts = frozenset(h.strip().lower()
                      for h in os.environ.get(variable, default).split(",") if h.strip())
    if not hosts:
        raise RuntimeError(
            f"{variable} is empty — the Host allowlist is what closes "
            "DNS rebinding against this UI, and with no entries every request is "
            "refused. Refusing to start (fail closed). Set it to the hostname(s) "
            "this UI is addressed as, e.g. 127.0.0.1,localhost,::1.")
    return hosts


# Methods that CHANGE STATE, so cross-origin callers must be refused. GET is left
# to the Host guard alone: a cross-origin GET's response is unreadable without CORS
# headers (which no UI here sends), and the readable case IS rebinding, which
# guard 1 already blocks.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Fetch destinations that mean "this response is being EMBEDDED in another page".
# Refused because neither guard above can see the difference: an attacker page that
# frames the UI's loopback origin causes a request with a legitimate `Host: 127.0.0.1`
# (guard 1 passes) using GET, which is deliberately allowed cross-origin (guard 2
# does not apply) — and the framed page's own resolve POST then reads as
# `Sec-Fetch-Site: same-origin`, because from inside the frame it is. The attacker
# cannot READ the framed UI, but clickjacking does not need a read: overlay a decoy,
# and the operator's click lands on the real "Allow + persist rule" button.
#
# `frame-ancestors 'none'` in CSP is the primary control; this check is not
# redundant with it. Both are browser-enforced, but refusing the embedded document
# HERE means the attempt appears as a 403 in the UI's log rather than only in the
# operator's console — visibility being what this repo falls back on wherever
# prevention is browser-dependent (see the provenance note in control-plane-ui/app.py).
_FRAMED_DESTS = frozenset({"iframe", "frame", "embed", "object"})

# Sent on EVERY response, refusals included (see `security_headers`).
#
# `default-src 'none'` then names only what a page genuinely uses: its own script
# (`'self'` — hence app.js being a file), the inline <style> block, the data-URI
# favicon under img-src, and same-origin fetch/EventSource under connect-src. The
# effect worth having is that a page renders AGENT-CONTROLLED strings (a held
# request's host, url and client), so if an escaping mistake ever became script, that
# script could still reach no external origin, load nothing, and submit nowhere.
CSP = ("default-src 'none'; "
       "script-src 'self'; "
       "style-src 'unsafe-inline'; "
       "img-src data:; "
       "connect-src 'self'; "
       "base-uri 'none'; "
       "form-action 'none'; "
       "frame-ancestors 'none'")

SECURITY_HEADERS = {
    "content-security-policy": CSP,
    # Redundant with frame-ancestors on any current browser; kept because it costs a
    # header and covers an old one that honours neither Sec-Fetch-Dest nor CSP 2.
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    # A UI's URLs name approval ids; nothing should carry them outward.
    "referrer-policy": "no-referrer",
}


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


def _authority_of(value: str | None, default_port: int = 80) -> tuple[str, int] | None:
    """``(hostname, port)`` from a `Host` header or an `Origin`, with the port made
    explicit so the two compare: a browser omits a default port from `Origin`, and an
    operator may or may not type one into the address bar. An `Origin` carries its
    scheme, so its default follows it; a `Host` carries none and every UI here serves
    plain HTTP, so its default is 80. Unparseable, or no hostname at all
    (`Origin: null`), is None — which equals nothing."""
    v = (value or "").strip()
    if not v:
        return None
    if "://" not in v:
        v = "//" + v
    try:
        parts = urlsplit(v)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else default_port
    return host, port


def refuse(name: str, detail: str) -> PlainTextResponse:
    """One shape for every refusal, naming the UI that refused. 403 rather than 404:
    these surfaces are loopback-only, so leaking that a path exists costs nothing next
    to an operator being able to tell a guard refusal from a missing route while
    debugging."""
    return PlainTextResponse(f"refused by {name}: {detail}\n",
                             status_code=403)


def request_guard(name: str, allowed: frozenset[str]):
    """Host + framing + cross-origin guards for the UI ``name``, answering to the
    hostnames in ``allowed``: the middleware `install` registers."""

    async def _guard(request: Request, call_next):
        """Host + cross-origin guards, as MIDDLEWARE so they cover every route
        (including `/` and `/healthz`) and cannot be forgotten on a route added later."""
        hostname = _hostname_of(request.headers.get("host"))
        if hostname not in allowed:
            # Also catches a missing Host, which HTTP/1.1 forbids anyway.
            return refuse(name, f"unexpected Host {hostname or '<absent>'!r} "
                                f"(allowed: {sorted(allowed)}) — possible DNS rebinding")
        dest = (request.headers.get("sec-fetch-dest") or "").lower()
        if dest in _FRAMED_DESTS:
            return refuse(name, f"refusing to be embedded (Sec-Fetch-Dest: {dest}) — "
                                f"clickjacking of this page's buttons")
        if request.method.upper() in _UNSAFE_METHODS:
            # Prefer Sec-Fetch-Site when the browser sends it: it states the
            # relationship directly and, like Host and Origin, JS cannot forge it.
            # Absent (older browser, or a non-browser client) fall back to Origin. BOTH
            # absent means no browser is making this request, so there is no CSRF to
            # prevent — let it through, which is also what keeps curl/scripting usable.
            site = (request.headers.get("sec-fetch-site") or "").lower()
            if site:
                if site != "same-origin":
                    return refuse(name,
                                  f"cross-origin state change (Sec-Fetch-Site: {site})")
            else:
                # The FULL authority, not the hostname: `localhost:3000` is on the
                # allowlist's hostname and is a different origin, and a page the agent
                # wrote into the workspace is one dev-server away from being served
                # there. Compared against the request's own Host rather than the
                # allowlist, so the question is "is this page us" and not "is this page
                # on a name we answer to". Scheme is not compared: a UI serves one
                # scheme on the port, so nothing else can share the authority.
                origin = request.headers.get("origin")
                if origin and (_authority_of(origin)
                               != _authority_of(request.headers.get("host"))):
                    return refuse(name, f"cross-origin state change (Origin: {origin})")
        return await call_next(request)

    return _guard


async def security_headers(request: Request, call_next):
    """Set the browser-facing security headers on every response.

    Kept separate from the request guard rather than folded into it because the two
    do different jobs: the guard decides whether to serve at all, this decides what
    the browser is permitted to do with what was served. Plain assignment, not
    `setdefault`: these responses include RELAYED upstream ones, and a header from
    upstream must not be able to weaken the frontend's policy."""
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def install(app, name: str, allowed: frozenset[str]) -> None:
    """Put the guards on ``app``, as the UI ``name`` answering to ``allowed``."""
    app.middleware("http")(request_guard(name, allowed))
    # Registered AFTER the guard on purpose: with Starlette's `app.middleware("http")`
    # the last-registered middleware is the OUTERMOST, so this one wraps the guard and
    # therefore also hardens its 403 refusals — including the refused framing attempt,
    # whose response a browser would otherwise render with no policy at all.
    app.middleware("http")(security_headers)


def check_bind(bind: str, net: str, *, name: str, bind_variable: str, net_variable: str,
               why: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Refuse to start unless the page is served on one address inside ``net``, and
    return that network. ``why`` is the UI's reason, said in the refusal."""
    try:
        network = ipaddress.ip_network(net)
    except ValueError as exc:
        raise SystemExit(f"{name}: {net_variable}={net!r} is not a network "
                         f"({exc}). Refusing to start (fail closed).") from exc
    try:
        inside = ipaddress.ip_address(bind) in network
    except ValueError:
        inside = False
    if not inside:
        raise SystemExit(
            f"{name}: {bind_variable}={bind!r} is not an address inside "
            f"{network}. {why} Refusing to start (fail closed).")
    return network


class OnlyFrom:
    """The app, answering only peers inside ``net``: a loopback publish arrives from
    the bridge's own address on it, and the healthcheck from the bound one.

    The bind does not choose the leg a packet arrives on (``OnlyFrom`` in
    ``task-registry/serve.py`` says why), and a UI's neighbours can include a service
    that parses agent text. None can route a packet here today, since every service
    drops every capability; this holds whether or not that does. Outside the app, so
    its refusal carries the security headers itself."""

    def __init__(self, app: object, net: ipaddress.IPv4Network | ipaddress.IPv6Network,
                 name: str) -> None:
        self.app, self.net = app, net
        self._refusal = (f"refused by {name}: this page answers its own network only\n"
                         .encode())

    def _admits(self, client: object) -> bool:
        try:
            return ipaddress.ip_address(client[0]) in self.net
        except (TypeError, IndexError, ValueError):
            return False

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] != "lifespan" and not self._admits(scope.get("client")):
            if scope["type"] == "http":
                headers = [(b"content-type", b"text/plain; charset=utf-8"),
                           *((k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items())]
                await send({"type": "http.response.start", "status": 403,
                            "headers": headers})
                await send({"type": "http.response.body", "body": self._refusal})
            else:
                await send({"type": "websocket.close"})
            return
        await self.app(scope, receive, send)
