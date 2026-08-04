# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the control-plane-ui frontend (``control-plane-ui/app.py``).

Two groups, both regression suites for real defects:

**Relay host pinning.** The frontend reverse-proxies to the backend. A request like
``//evil.com/x`` is captured by ``{path:path}`` as ``/evil.com/x``; the old ``"/" +
path`` turned that back into the network-path reference ``//evil.com/x``, which
httpx resolves against the base URL as a DIFFERENT authority (``http://evil.com/x``)
— a caller-controlled host override / SSRF. The fix collapses leading slashes so the
upstream host always stays the backend.

**Browser-facing guards.** This app publishes the only API that can grant egress
(``POST /approvals/{id}/resolve`` is self-approval if reached), on host loopback,
with no auth. Loopback binding is no defense against DNS REBINDING: a page the
operator visits can point its own name at 127.0.0.1 and become same-origin, at which
point it can read the pending approvals (getting the unguessable ids) and resolve
them. So the app now enforces a Host allowlist (closes rebinding), refuses
cross-origin state changes (closes CSRF), relays only an allowlist of backend paths
(so ``POST /authorize`` is unreachable from a page), and replaces client-supplied
provenance headers with the peer address it actually observed.

Dependency-free: like ``tests/_loader.py`` we stub the third-party imports
(``httpx``, the ``fastapi``/``starlette`` bits) before loading the module by path.
The httpx ``build_request`` stub resolves the passed path against the base URL with
stdlib ``urllib.parse.urljoin`` — which follows the SAME RFC-3986 rules httpx does
for network-path references — and records the resulting authority plus headers, so
the tests assert real end-to-end outcomes rather than string shapes."""
from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]

_BASE = "http://control-plane:8090"
_sent: dict = {}


def _install_stubs() -> None:
    # httpx: an AsyncClient whose build_request resolves the path against base_url
    # (RFC-3986, via urljoin) so we can assert the effective upstream authority, and
    # records the outgoing headers so we can assert provenance handling.
    if "httpx" not in sys.modules:
        httpx = types.ModuleType("httpx")

        class _Resp:
            status_code = 200
            headers: ClassVar[dict] = {}

            def aiter_raw(self):
                async def _gen():
                    if False:
                        yield b""
                return _gen()

            async def aclose(self):
                pass

        class AsyncClient:
            def __init__(self, *args, base_url="", **kwargs):
                self._base = str(base_url)

            def build_request(self, method, url, **kwargs):
                _sent["url"] = urljoin(self._base, url)
                _sent["method"] = method
                _sent["headers"] = list(kwargs.get("headers") or [])
                return object()

            async def send(self, req, stream=False):
                return _Resp()

        class RequestError(Exception):
            """Stand-in for the base of httpx's transport errors — what the relay
            catches to answer 502 when the backend is unreachable."""

        httpx.AsyncClient = AsyncClient
        httpx.RequestError = RequestError
        sys.modules["httpx"] = httpx

    # Augment rather than assume ownership: discovery order is arbitrary, so another
    # module may have installed an httpx stub of its own first.
    if not hasattr(sys.modules["httpx"], "RequestError"):
        sys.modules["httpx"].RequestError = type("RequestError", (Exception,), {})

    # Another test module may have installed the shared fastapi stub first (test
    # discovery order is arbitrary), so augment whatever exists rather than assume
    # a clean slate: ensure FastAPI.api_route/.middleware and the responses the UI
    # imports. Response stubs expose .status_code so guard refusals are assertable.
    def _decorator(*_a, **_k):
        return lambda fn: fn

    class _R:
        def __init__(self, *a, **k):
            self.args, self.kwargs = a, k
            self.status_code = k.get("status_code", 200)
            self.body = a[0] if a else None
            # A dict stands in for Starlette's MutableHeaders — `_security_headers`
            # writes onto every response it wraps, refusals included.
            self.headers = dict(k.get("headers") or {})

    if "fastapi" not in sys.modules:
        fa = types.ModuleType("fastapi")

        class FastAPI:
            def __init__(self, *a, **k):
                pass

            get = staticmethod(_decorator)
            post = staticmethod(_decorator)
            api_route = staticmethod(_decorator)
            middleware = staticmethod(_decorator)

        class Request:
            pass

        fa.FastAPI = FastAPI
        fa.Request = Request
        responses = types.ModuleType("fastapi.responses")
        responses.StreamingResponse = _R
        fa.responses = responses
        sys.modules["fastapi"] = fa
        sys.modules["fastapi.responses"] = responses

    fa = sys.modules["fastapi"]
    for meth in ("get", "post", "api_route", "middleware"):
        if not hasattr(fa.FastAPI, meth):
            setattr(fa.FastAPI, meth, staticmethod(_decorator))
    responses = sys.modules["fastapi.responses"]
    # `Response` is the BASE class the relay handler is annotated with (a union
    # annotation makes real FastAPI raise at import — see the note on `proxy`), so the
    # stub must expose it too or the module under test cannot be imported at all.
    for name in ("FileResponse", "StreamingResponse", "PlainTextResponse",
                 "JSONResponse", "Response"):
        if not hasattr(responses, name):
            setattr(responses, name, _R)

    if "starlette.background" not in sys.modules:
        starlette = sys.modules.setdefault("starlette", types.ModuleType("starlette"))
        bg = types.ModuleType("starlette.background")
        bg.BackgroundTask = lambda *a, **k: None
        starlette.background = bg
        sys.modules["starlette.background"] = bg


def _load_ui() -> types.ModuleType:
    _install_stubs()
    path = ROOT / "control-plane-ui" / "app.py"
    spec = importlib.util.spec_from_file_location("dockade_control_plane_ui", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dockade_control_plane_ui"] = module
    spec.loader.exec_module(module)
    return module


ui = _load_ui()

# Permissive stand-in for the relay allowlist, used only by the host-pinning tests
# below: they must keep asserting that the UPSTREAM PINNING holds on its own, not
# because the allowlist happens to reject the hostile paths first.
_ALLOW_ANYTHING = (("GET", re.compile(r"^/.*$")), ("POST", re.compile(r"^/.*$")))


class _FakeRequest:
    """Minimal stand-in for a Starlette Request. Headers are lowercased on the way
    in, matching Starlette's case-insensitive mapping, since the app reads them in
    lowercase."""

    def __init__(self, method="GET", headers=None, peer="127.0.0.1"):
        self.method = method
        self.query_params = {}
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.headers = types.SimpleNamespace(
            get=self._headers.get, items=lambda: list(self._headers.items()))
        self.client = types.SimpleNamespace(host=peer) if peer else None

    async def body(self) -> bytes:
        return b""


def _ok_host(**kw):
    """A request that already satisfies the Host guard, for tests about other guards."""
    headers = {"host": "127.0.0.1:8081", **kw.pop("headers", {})}
    return _FakeRequest(headers=headers, **kw)


async def _passthrough(_request):
    return "REACHED-THE-APP"


class HostGuardTests(unittest.TestCase):
    """The Host allowlist is the load-bearing anti-rebinding control: rebinding
    structurally requires the attacker's own name in Host, and JS cannot set that
    header, so refusing unexpected Hosts removes the attack class."""

    def _guard(self, request):
        return asyncio.run(ui._guard(request, _passthrough))

    def test_loopback_hosts_are_accepted_regardless_of_port(self):
        # The published port (8081) and the in-container healthcheck port (8090)
        # must both work off ONE list — hence port-insensitive comparison.
        for host in ("127.0.0.1:8081", "localhost:8081", "127.0.0.1:8090",
                     "localhost", "[::1]:8081"):
            self.assertEqual(self._guard(_FakeRequest(headers={"host": host})),
                             "REACHED-THE-APP", host)

    def test_rebinding_host_is_refused(self):
        for host in ("evil.com", "evil.com:8081", "attacker.test:8081",
                     "127.0.0.1.evil.com:8081"):
            resp = self._guard(_FakeRequest(headers={"host": host}))
            self.assertEqual(getattr(resp, "status_code", None), 403, host)

    def test_absent_host_is_refused(self):
        resp = self._guard(_FakeRequest(headers={}))
        self.assertEqual(resp.status_code, 403)

    def test_guard_fails_closed_when_allowlist_is_emptied(self):
        # Not a supported configuration (import asserts against it) — this pins the
        # direction of failure if it is somehow reached: refuse, never wave through.
        with mock.patch.object(ui, "ALLOWED_HOSTNAMES", frozenset()):
            resp = self._guard(_FakeRequest(headers={"host": "127.0.0.1:8081"}))
        self.assertEqual(resp.status_code, 403)

    def test_import_refuses_an_empty_allowlist(self):
        with mock.patch.object(ui, "ALLOWED_HOSTNAMES", frozenset()), \
                self.assertRaises(RuntimeError):
            ui._assert_host_guard_configured()


class CrossOriginGuardTests(unittest.TestCase):
    """State-changing requests must not be accepted from another origin."""

    def _guard(self, request):
        return asyncio.run(ui._guard(request, _passthrough))

    def test_cross_site_post_is_refused_via_sec_fetch_site(self):
        resp = self._guard(_ok_host(
            method="POST", headers={"sec-fetch-site": "cross-site"}))
        self.assertEqual(resp.status_code, 403)

    def test_same_origin_post_is_accepted(self):
        self.assertEqual(
            self._guard(_ok_host(method="POST",
                                 headers={"sec-fetch-site": "same-origin"})),
            "REACHED-THE-APP")

    def test_cross_origin_post_is_refused_via_origin_header(self):
        # Older browser with no Sec-Fetch-Site: fall back to Origin.
        resp = self._guard(_ok_host(
            method="POST", headers={"origin": "https://evil.com"}))
        self.assertEqual(resp.status_code, 403)

    def test_same_origin_post_via_origin_header_is_accepted(self):
        self.assertEqual(
            self._guard(_ok_host(method="POST",
                                 headers={"origin": "http://127.0.0.1:8081"})),
            "REACHED-THE-APP")

    def test_post_with_no_browser_headers_is_accepted(self):
        # Neither header means no browser is calling, so there is no CSRF to stop;
        # refusing here would only break curl/scripting without adding safety. The
        # host-local caller this admits is out of reach of any browser-enforced
        # guard anyway (see the module docstring) — it is handled by recording
        # provenance, not by pretending to block it.
        self.assertEqual(self._guard(_ok_host(method="POST")), "REACHED-THE-APP")

    def test_cross_site_GET_is_not_refused_by_the_origin_guard(self):
        # A cross-origin GET's response is unreadable without CORS headers (never
        # sent), and the readable case is rebinding — already refused by Host.
        self.assertEqual(
            self._guard(_ok_host(headers={"sec-fetch-site": "cross-site"})),
            "REACHED-THE-APP")


class RelayAllowlistTests(unittest.TestCase):
    """Only the paths this UI actually uses are relayed — above all NOT
    ``POST /authorize``, reaching which means forged audit rows and consumed hold
    slots on the governance authority."""

    def _proxy(self, captured_path, method="GET"):
        _sent.clear()
        return asyncio.run(ui.proxy(captured_path, _ok_host(method=method)))

    def test_ui_paths_are_relayed(self):
        for path, method in (("approvals", "GET"), ("approvals/stream", "GET"),
                             ("api/audit", "GET"), ("api/rules", "GET"),
                             ("status", "GET"),
                             ("approvals/0123abcd/resolve", "POST")):
            self._proxy(path, method)
            self.assertEqual(urlsplit(_sent["url"]).netloc, "control-plane:8090",
                             f"{method} {path} must relay")

    def test_authorize_is_not_relayed(self):
        # The proxy's decision endpoint: no browser has any business reaching it.
        resp = self._proxy("authorize", "POST")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(_sent, {}, "must not reach the backend at all")

    def test_unknown_and_wrong_method_paths_are_not_relayed(self):
        for path, method in (("healthz", "GET"), ("api/secrets", "GET"),
                             ("approvals", "POST"), ("status", "POST"),
                             ("approvals/x/resolve", "GET"),
                             ("approvals/a/b/resolve", "POST"),
                             # The rules view is READ-only: no mutation path exists
                             # on the backend, and none is relayed either.
                             ("api/rules", "POST")):
            resp = self._proxy(path, method)
            self.assertEqual(getattr(resp, "status_code", None), 403,
                             f"{method} {path} must be refused")


class ProvenanceHeaderTests(unittest.TestCase):
    """The backend records ``ACTOR_HEADER`` as this relay's assertion about the
    browser, so a client must not be able to set it — otherwise the audit record
    would faithfully repeat a forged actor."""

    def _relay_headers(self, request):
        _sent.clear()
        asyncio.run(ui.proxy("approvals", request))
        return _sent["headers"]

    def test_actor_header_is_set_from_the_observed_peer(self):
        headers = self._relay_headers(_ok_host(peer="10.9.9.9"))
        self.assertIn((ui.ACTOR_HEADER, "10.9.9.9"), headers)

    def test_client_supplied_actor_and_forwarding_headers_are_stripped(self):
        headers = self._relay_headers(_ok_host(peer="10.9.9.9", headers={
            ui.ACTOR_HEADER: "the-operator-honest-really",
            "x-forwarded-for": "1.2.3.4",
            "x-real-ip": "1.2.3.4",
            "forwarded": "for=1.2.3.4",
        }))
        names = [k.lower() for k, _ in headers]
        for spoofable in ("x-forwarded-for", "x-real-ip", "forwarded"):
            self.assertNotIn(spoofable, names)
        # Exactly one actor header, carrying the peer rather than the claim.
        actors = [v for k, v in headers if k.lower() == ui.ACTOR_HEADER]
        self.assertEqual(actors, ["10.9.9.9"])

    def test_host_header_is_not_forwarded(self):
        # httpx must set Host for the backend, or the upstream sees the UI's.
        names = [k.lower() for k, _ in self._relay_headers(_ok_host())]
        self.assertNotIn("host", names)


class FramingGuardTests(unittest.TestCase):
    """Clickjacking is the one browser-side vector the Host and cross-origin guards
    cannot see: framing this UI produces a legitimate `Host: 127.0.0.1` on a GET
    (allowed cross-origin by design), and the framed page's own resolve POST really is
    same-origin. Reads stay blocked, but a UI-redress click needs no read — it lands
    on the real "Allow + persist rule" button."""

    def _guard(self, request):
        return asyncio.run(ui._guard(request, _passthrough))

    def test_embedded_destinations_are_refused(self):
        for dest in ("iframe", "frame", "embed", "object"):
            resp = self._guard(_ok_host(headers={"sec-fetch-dest": dest}))
            self.assertEqual(getattr(resp, "status_code", None), 403, dest)

    def test_normal_destinations_are_served(self):
        # The top-level page, the script, and the fetch/EventSource calls.
        for dest in ("document", "script", "empty", ""):
            self.assertEqual(
                self._guard(_ok_host(headers={"sec-fetch-dest": dest})),
                "REACHED-THE-APP", dest or "<absent>")

    def test_embedded_post_is_refused_even_when_same_origin(self):
        # The POST from inside a frame is genuinely same-origin, so guard 2 passes it;
        # this must not be the only thing standing between a frame and `resolve`.
        resp = self._guard(_ok_host(method="POST", headers={
            "sec-fetch-site": "same-origin", "sec-fetch-dest": "iframe"}))
        self.assertEqual(resp.status_code, 403)


def _directives(csp: str) -> dict[str, list[str]]:
    """Parse a CSP into {directive: [sources]} so tests assert on the POLICY rather
    than on a substring of a header value."""
    out = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, rest = part.partition(" ")
        out[name.lower()] = rest.split()
    return out


class _FakeResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = dict(headers or {})


class SecurityHeaderTests(unittest.TestCase):
    """Every response carries the browser-facing policy — refusals included, since a
    refusal is still a document a browser may have framed."""

    def _headers(self, upstream=None, status_code=200):
        async def _next(_request):
            return _FakeResponse(status_code=status_code, headers=upstream or {})
        resp = asyncio.run(ui._security_headers(_ok_host(), _next))
        return resp.headers

    def test_frame_ancestors_none_is_present(self):
        # The load-bearing directive: this is what actually closes clickjacking in a
        # browser (the Sec-Fetch-Dest refusal above is for the log trail).
        csp = _directives(self._headers()["content-security-policy"])
        self.assertEqual(csp.get("frame-ancestors"), ["'none'"])

    def test_script_src_is_self_and_never_unsafe_inline(self):
        # The whole reason app.js is a file. An 'unsafe-inline' script-src would make
        # the rest of this policy decorative against an injection in the pending list,
        # which renders agent-controlled host/url/client strings.
        csp = _directives(self._headers()["content-security-policy"])
        self.assertEqual(csp.get("script-src"), ["'self'"])

    def test_policy_denies_by_default_and_allows_only_what_the_page_uses(self):
        csp = _directives(self._headers()["content-security-policy"])
        self.assertEqual(csp.get("default-src"), ["'none'"])
        self.assertEqual(csp.get("connect-src"), ["'self'"])   # fetch + EventSource
        self.assertEqual(csp.get("img-src"), ["data:"])        # the inline favicon
        self.assertEqual(csp.get("base-uri"), ["'none'"])
        self.assertEqual(csp.get("form-action"), ["'none'"])
        # No external origin is permitted anywhere in the policy — a governance UI
        # must not be able to load or reach a third party even under an injection.
        for sources in csp.values():
            for src in sources:
                self.assertNotIn("//", src)
                self.assertNotIn("*", src)

    def test_supporting_headers_are_set(self):
        headers = self._headers()
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "no-referrer")

    def test_a_relayed_backend_header_cannot_weaken_the_policy(self):
        # These responses include relayed ones, so upstream headers pass through this
        # middleware. Assignment (not setdefault) is what makes the frontend's policy
        # authoritative over anything the backend might send.
        headers = self._headers(upstream={"content-security-policy": "default-src *"})
        self.assertEqual(headers["content-security-policy"], ui._CSP)

    def test_guard_refusals_are_hardened_too(self):
        # `_security_headers` is registered AFTER `_guard`, which under Starlette makes
        # it the OUTER middleware — so it wraps the guard's 403s. If that ordering is
        # ever inverted, a refused framing attempt would render with no policy at all.
        async def _refusing(_request):
            return ui._refuse("nope")

        resp = asyncio.run(ui._security_headers(_ok_host(), _refusing))
        self.assertEqual(resp.status_code, 403)
        self.assertIn("frame-ancestors 'none'", resp.headers["content-security-policy"])


class BackendUnreachableTests(unittest.TestCase):
    """A backend that is down/restarting must produce a legible 502, not a 500.

    Load-bearing for the SSE feed specifically: an EventSource treats ANY non-200 as a
    PERMANENT close (readyState CLOSED, no built-in retry), so this path is what the
    page's hand-rolled reconnect reconnects from. Before it existed the exception
    became a 500 and the page sat blind on "reconnecting…" until a manual reload."""

    def _proxy_with_dead_backend(self, path="approvals", method="GET"):
        async def _boom(_req, stream=False):
            raise sys.modules["httpx"].RequestError("connection refused")

        _sent.clear()
        with mock.patch.object(ui._client, "send", _boom):
            return asyncio.run(ui.proxy(path, _ok_host(method=method)))

    def test_transport_error_becomes_502(self):
        resp = self._proxy_with_dead_backend()
        self.assertEqual(resp.status_code, 502)

    def test_stream_path_also_becomes_502(self):
        # The case that matters: this is the endpoint EventSource is attached to.
        resp = self._proxy_with_dead_backend("approvals/stream")
        self.assertEqual(resp.status_code, 502)

    def test_the_guards_still_run_first(self):
        # A dead backend must not turn a refusal into a 502 — the refusal happens
        # before any upstream call is attempted.
        resp = self._proxy_with_dead_backend("authorize", "POST")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(_sent, {}, "must not reach the backend at all")


class RelayHostPinningTests(unittest.TestCase):
    """The upstream authority must stay the backend. Kept separate from the relay
    allowlist ON PURPOSE: the allowlist would reject these paths first today, so
    these tests neutralise it to prove the PINNING holds on its own — otherwise a
    later widening of the allowlist would silently re-expose the SSRF."""

    def _upstream_host(self, captured_path: str) -> str:
        _sent.clear()
        with mock.patch.object(ui, "_RELAY_ROUTES", _ALLOW_ANYTHING):
            asyncio.run(ui.proxy(captured_path, _ok_host()))
        return urlsplit(_sent["url"]).netloc

    def test_normal_paths_reach_the_backend(self):
        # {path:path} captures these WITHOUT a leading slash.
        for p in ("approvals", "approvals/stream", "api/audit"):
            self.assertEqual(self._upstream_host(p), "control-plane:8090",
                             f"normal path {p!r} must relay to the backend")

    def test_network_path_reference_cannot_override_the_host(self):
        # {path:path} RETAINS one leading slash for a '//host' request, which is the
        # SSRF vector. The upstream authority must stay the backend regardless.
        for p in ("/evil.com/x", "/evil.com", "/attacker:1234/steal"):
            self.assertEqual(
                self._upstream_host(p), "control-plane:8090",
                f"captured path {p!r} must NOT override the upstream host")

    def test_network_path_reference_is_also_refused_by_the_allowlist(self):
        # Belt and braces: with the real allowlist in place it never gets that far.
        _sent.clear()
        resp = asyncio.run(ui.proxy("/evil.com/x", _ok_host()))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(_sent, {})


if __name__ == "__main__":
    unittest.main()
