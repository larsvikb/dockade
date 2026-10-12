# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the browser guards every UI serves behind (``ui-common/guards.py``):
the Host allowlist, the refusal of a cross-origin state change and of being embedded,
the security headers, ``install`` that puts them on an app, and the bind and peer
checks a UI's ``main`` starts with. Why each exists is the module's docstring.

Run with the control-plane UI's allowlist and network, the case each was written for,
under a name and variables no UI has, so one written into the module shows; that the UI
passes its own is ``tests/test_control_plane_ui.py``. The stubs and fake requests are
that module's."""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

from test_control_plane_ui import (
    UI_BIND,
    UI_NET,
    _FakeRequest,
    _ok_host,
    _passthrough,
    ui,
)

# The module the UI imported: loading it put ui-common/ on the path, after the stubs.
guards = ui.guards
NAME = "some-ui"
GUARD = guards.request_guard(NAME, frozenset({"127.0.0.1", "localhost", "::1"}))


class HostGuardTests(unittest.TestCase):
    """The Host allowlist is the load-bearing anti-rebinding control: rebinding
    structurally requires the attacker's own name in Host, and JS cannot set that
    header, so refusing unexpected Hosts removes the attack class."""

    def _guard(self, request):
        return asyncio.run(GUARD(request, _passthrough))

    def test_loopback_hosts_are_accepted_regardless_of_port(self):
        # The published port and the in-container healthcheck port (8090) differ
        # and must both work off ONE list — hence port-insensitive comparison.
        # The published number is deliberately arbitrary here: it is configurable
        # (DOCKADE_UI_PORT), and pinning it would make this a test of the default.
        for host in ("127.0.0.1:28090", "localhost:28090", "127.0.0.1:8090",
                     "localhost", "[::1]:28090"):
            self.assertEqual(self._guard(_FakeRequest(headers={"host": host})),
                             "REACHED-THE-APP", host)

    def test_rebinding_host_is_refused(self):
        for host in ("evil.com", "evil.com:28090", "attacker.test:28090",
                     "127.0.0.1.evil.com:28090"):
            resp = self._guard(_FakeRequest(headers={"host": host}))
            self.assertEqual(getattr(resp, "status_code", None), 403, host)

    def test_absent_host_is_refused(self):
        resp = self._guard(_FakeRequest(headers={}))
        self.assertEqual(resp.status_code, 403)

    def test_guard_fails_closed_when_allowlist_is_emptied(self):
        # Not a supported configuration (`allowed_hostnames` refuses it) — this pins
        # the direction of failure if it is somehow reached: refuse, never wave through.
        empty = guards.request_guard(NAME, frozenset())
        resp = asyncio.run(empty(_FakeRequest(headers={"host": "127.0.0.1:28090"}),
                                 _passthrough))
        self.assertEqual(resp.status_code, 403)

    def test_an_empty_allowlist_refuses_to_start(self):
        for value in ("", " , ,"):
            with self.subTest(value=value), \
                    mock.patch.dict(os.environ, {"DOCKADE_TEST_HOSTS": value}), \
                    self.assertRaises(RuntimeError) as caught:
                guards.allowed_hostnames("DOCKADE_TEST_HOSTS", "127.0.0.1")
            self.assertIn("DOCKADE_TEST_HOSTS is empty", str(caught.exception))

    def test_the_allowlist_is_read_trimmed_and_lowercased(self):
        with mock.patch.dict(os.environ, {"DOCKADE_TEST_HOSTS": " UI.Example ,, ::1 "}):
            self.assertEqual(guards.allowed_hostnames("DOCKADE_TEST_HOSTS", "127.0.0.1"),
                             {"ui.example", "::1"})
        with mock.patch.dict(os.environ):
            os.environ.pop("DOCKADE_TEST_HOSTS", None)
            self.assertEqual(guards.allowed_hostnames("DOCKADE_TEST_HOSTS", "a,B"),
                             {"a", "b"})


class CrossOriginGuardTests(unittest.TestCase):
    """State-changing requests must not be accepted from another origin."""

    def _guard(self, request):
        return asyncio.run(GUARD(request, _passthrough))

    def test_cross_site_post_is_refused_via_sec_fetch_site(self):
        resp = self._guard(_ok_host(
            method="POST", headers={"sec-fetch-site": "cross-site"}))
        self.assertEqual(resp.status_code, 403)

    def test_every_state_changing_method_is_checked(self):
        # The guard knows nothing of which routes an app declares: a UI that adds a
        # PUT, PATCH or DELETE route is covered by the same check as a POST.
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for headers in ({"sec-fetch-site": "cross-site"},
                            {"origin": "https://evil.com"}):
                with self.subTest(method=method, headers=headers):
                    resp = self._guard(_ok_host(method=method, headers=headers))
                    self.assertEqual(getattr(resp, "status_code", None), 403)

    def test_only_same_origin_passes_via_sec_fetch_site(self):
        # `same-site` is a page on another port of the same host — another UI on
        # loopback, or a dev server the agent's workspace is served from — and `none`
        # is no page at all; neither is this origin.
        for site in ("same-site", "none", "cross-site"):
            with self.subTest(site=site):
                resp = self._guard(_ok_host(method="POST",
                                            headers={"sec-fetch-site": site}))
                self.assertEqual(getattr(resp, "status_code", None), 403)

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
                                 headers={"origin": "http://127.0.0.1:28090"})),
            "REACHED-THE-APP")

    def test_another_localhost_port_is_another_origin(self):
        # The fallback used to compare hostnames against the allowlist, so any
        # localhost origin passed. A page the agent wrote into the workspace, served
        # by the project's own dev server on another port and opened by the operator
        # in a browser without Sec-Fetch-Site, could POST a resolve. The port is part
        # of the origin and is compared.
        for origin in ("http://localhost:3000", "http://127.0.0.1:3000",
                       "http://127.0.0.1"):
            with self.subTest(origin=origin):
                resp = self._guard(_ok_host(method="POST", headers={"origin": origin}))
                self.assertEqual(resp.status_code, 403)

    def test_the_origin_is_compared_to_the_requests_own_host_not_the_allowlist(self):
        # `localhost` and `127.0.0.1` are both allowed Hosts, but a page at one is
        # not the same origin as a page at the other; the UI's own page always sends
        # the Origin its address bar shows, which is the Host it dialled.
        resp = self._guard(_ok_host(method="POST",
                                    headers={"origin": "http://localhost:28090"}))
        self.assertEqual(resp.status_code, 403)

    def test_a_default_port_left_implicit_on_either_side_still_matches(self):
        # Browsers omit :80 from Origin; an operator may or may not type it into Host.
        for host, origin in (("localhost", "http://localhost"),
                             ("localhost:80", "http://localhost"),
                             ("localhost", "http://localhost:80")):
            with self.subTest(host=host, origin=origin):
                req = _FakeRequest(method="POST",
                                   headers={"host": host, "origin": origin})
                self.assertEqual(self._guard(req), "REACHED-THE-APP")

    def test_another_scheme_is_another_origin(self):
        # `https://127.0.0.1` is port 443, the page's own `Host: 127.0.0.1` port 80: a
        # page served over TLS on the same name is not this one.
        for host, origin in (("127.0.0.1", "https://127.0.0.1"),
                             ("127.0.0.1:443", "http://127.0.0.1")):
            with self.subTest(host=host, origin=origin):
                req = _FakeRequest(method="POST", headers={"host": host, "origin": origin})
                self.assertEqual(getattr(self._guard(req), "status_code", None), 403)

    def test_an_opaque_origin_is_refused(self):
        # `Origin: null` — a sandboxed frame, a redirect chain, a data: URL. It is
        # nobody's authority, so it equals nothing.
        resp = self._guard(_ok_host(method="POST", headers={"origin": "null"}))
        self.assertEqual(resp.status_code, 403)

    def test_post_with_no_browser_headers_is_accepted(self):
        # Neither header means no browser is calling, so there is no CSRF to stop;
        # refusing here would only break curl/scripting without adding safety. The
        # host-local caller this admits is out of reach of any browser-enforced
        # guard anyway (see guards.py's docstring) — it is handled by recording
        # provenance, not by pretending to block it.
        self.assertEqual(self._guard(_ok_host(method="POST")), "REACHED-THE-APP")

    def test_cross_site_GET_is_not_refused_by_the_origin_guard(self):
        # A cross-origin GET's response is unreadable without CORS headers (never
        # sent), and the readable case is rebinding — already refused by Host.
        self.assertEqual(
            self._guard(_ok_host(headers={"sec-fetch-site": "cross-site"})),
            "REACHED-THE-APP")


class FramingGuardTests(unittest.TestCase):
    """Clickjacking is the one browser-side vector the Host and cross-origin guards
    cannot see: framing this UI produces a legitimate `Host: 127.0.0.1` on a GET
    (allowed cross-origin by design), and the framed page's own resolve POST really is
    same-origin. Reads stay blocked, but a UI-redress click needs no read — it lands
    on the real "Allow + persist rule" button."""

    def _guard(self, request):
        return asyncio.run(GUARD(request, _passthrough))

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
        resp = asyncio.run(guards.security_headers(_ok_host(), _next))
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
        self.assertEqual(headers["content-security-policy"], guards.CSP)

    def test_guard_refusals_are_hardened_too(self):
        # `install` registers `security_headers` AFTER the guard, which under Starlette
        # makes it the OUTER middleware — so it wraps the guard's 403s. If that ordering
        # is ever inverted, a refused framing attempt would render with no policy at all.
        async def _refusing(_request):
            return guards.refuse(NAME, "nope")

        resp = asyncio.run(guards.security_headers(_ok_host(), _refusing))
        self.assertEqual(resp.status_code, 403)
        self.assertIn("frame-ancestors 'none'", resp.headers["content-security-policy"])


class _RecordingApp:
    """What ``install`` registers, in order: the ``app.middleware("http")`` calls."""

    def __init__(self):
        self.registered: list[tuple[str, object]] = []

    def middleware(self, kind):
        def register(fn):
            self.registered.append((kind, fn))
            return fn
        return register


class InstallTests(unittest.TestCase):
    """The tests above exercise the guard and the headers as plain functions, so
    whether ``install`` REGISTERS them, and in the right order, is invisible to them.
    Leaving the guard out (disabling the Host/CSRF/framing guards) or swapping the two
    would pass every one.

    Order matters: under Starlette the LAST-registered ``app.middleware('http')`` is
    the OUTERMOST, so the headers must be registered AFTER the guard for a refused
    (403) framing/rebinding attempt to still carry the CSP."""

    def setUp(self):
        self.app = _RecordingApp()
        guards.install(self.app, NAME, frozenset({"127.0.0.1"}))

    def test_the_guard_then_the_headers_are_registered_as_http_middleware(self):
        self.assertEqual([kind for kind, _ in self.app.registered], ["http", "http"])
        self.assertIs(self.app.registered[1][1], guards.security_headers)

    def test_the_guard_registered_is_this_uis_with_its_allowlist(self):
        guard = self.app.registered[0][1]
        refused = asyncio.run(guard(_FakeRequest(headers={"host": "localhost"}),
                                    _passthrough))
        self.assertEqual(refused.status_code, 403)
        self.assertTrue(refused.body.startswith(f"refused by {NAME}: "))
        self.assertEqual(asyncio.run(guard(_FakeRequest(headers={"host": "127.0.0.1"}),
                                           _passthrough)), "REACHED-THE-APP")

    def test_a_refusal_carries_the_name_of_the_ui_that_refused(self):
        self.assertEqual(guards.refuse("some-ui", "why").body,
                         "refused by some-ui: why\n")


def _check_bind(bind: str, net: str):
    return guards.check_bind(bind, net, name=NAME, bind_variable="SOME_UI_BIND",
                             net_variable="SOME_UI_NET", why="Its reason.")


class BindTests(unittest.TestCase):
    """Where a page may be served: one address inside its network, here the approval
    UI's. Where compose puts it is held in ``tests/test_topology.py``, and which
    arguments each UI passes in its own tests."""

    def refused(self, bind: str = UI_BIND, net: str = UI_NET) -> str:
        with self.assertRaises(SystemExit) as caught:
            _check_bind(bind, net)
        return str(caught.exception)

    def test_an_address_on_control_ui_net_starts(self):
        _check_bind(UI_BIND, UI_NET)

    def test_anything_but_an_address_on_control_ui_net_is_refused(self):
        # A wildcard serves every leg; a name would make the check depend on DNS; the
        # mapped spelling is another family, which uvicorn would bind as such. Then the
        # UI's control-net leg, and the task registry's operator network, which the UI
        # joins for the registry's view.
        for bind in ("", "0.0.0.0", "::", "*", "control-plane-ui", "localhost",  # noqa: S104
                     "127.0.0.1", "::1", "::ffff:172.25.0.2", "172.31.0.10",
                     "172.26.0.3"):
            with self.subTest(bind=bind):
                message = self.refused(bind=bind)
                self.assertTrue(message.startswith(f"some-ui: SOME_UI_BIND={bind!r} "),
                                message)
                self.assertIn(" Its reason. ", message)
                self.assertIn("fail closed", message)

    def test_a_network_that_does_not_parse_is_refused(self):
        # Host bits set is a typo too: 172.25.0.2/24 for 172.25.0.0/24.
        for net in ("", "172.25.0.2/24", "172.25.0.0/33", "control-ui-net"):
            with self.subTest(net=net):
                self.assertTrue(
                    self.refused(net=net).startswith(f"some-ui: SOME_UI_NET={net!r} "))


class OnlyFromTests(unittest.TestCase):
    """The peer check an app is served in: a peer outside its network (here
    control-ui-net) is refused before the app sees the request, with the headers the
    app would have set."""

    def call(self, scope: dict) -> tuple[list[dict], list[dict]]:
        seen, sent = [], []

        async def app(scope, receive, send):
            seen.append(scope)

        async def send(message):
            sent.append(message)

        wrapped = guards.OnlyFrom(app, _check_bind(UI_BIND, UI_NET), NAME)
        asyncio.run(wrapped(scope, None, send))
        return seen, sent

    def test_the_publish_and_the_healthcheck_reach_the_app(self):
        # The bridge's gateway address, and the bound one.
        for peer in ("172.25.0.1", "172.25.0.2"):
            with self.subTest(peer=peer):
                scope = {"type": "http", "client": (peer, 40000)}
                self.assertEqual(self.call(scope), ([scope], []))

    def test_any_other_peer_is_refused_and_the_app_never_runs(self):
        # The registry and the UI's own address on its network, the control plane, the
        # UI's control-net address, loopback, the mapped spelling, a name, none.
        for client in (("172.26.0.2", 1), ("172.26.0.3", 1), ("172.31.0.2", 1),
                       ("172.31.0.10", 1), ("127.0.0.1", 1), ("::ffff:172.25.0.1", 1),
                       ("control-plane-ui", 1), (), None):
            with self.subTest(client=client):
                seen, sent = self.call({"type": "http", "client": client})
                self.assertEqual(seen, [])
                self.assertEqual(sent[0]["status"], 403)
                headers = dict(sent[0]["headers"])
                for name, value in guards.SECURITY_HEADERS.items():
                    self.assertEqual(headers[name.encode()], value.encode())
                self.assertEqual(
                    sent[1]["body"],
                    b"refused by some-ui: this page answers its own network only\n")

    def test_the_servers_own_lifespan_passes(self):
        scope = {"type": "lifespan"}
        self.assertEqual(self.call(scope), ([scope], []))

    def test_a_websocket_from_elsewhere_is_closed_before_its_handshake(self):
        seen, sent = self.call({"type": "websocket", "client": ("172.26.0.2", 1)})
        self.assertEqual((seen, sent), ([], [{"type": "websocket.close"}]))



if __name__ == "__main__":
    unittest.main()
