"""Unit tests for the egress proxy's async decision flow — the parts of
``proxies/egress/addon.py`` that ``boundary-check.sh`` cannot reach: the
control-plane call ``_authorize`` (permanent-lifeline short-circuit + fail-closed)
and the mitmproxy hooks that carry the real anti-domain-fronting logic
(``http_connect``, ``tls_clienthello``, ``request``, ``client_disconnected``).

Kept dependency-free: mitmproxy is stubbed (``tests/_loader.py``), the flow /
context objects the hooks read are small ``SimpleNamespace`` fakes, ``getaddrinfo``
is mocked so the guard runs offline, and ``_post_authorize`` is monkeypatched so
no network round-trip happens. The hooks' ``http.Response.make`` deny sentinel is
the stub's object, so "response set" == "denied"."""
from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest import mock

from _loader import load_egress_addon

addon = load_egress_addon()
# The hooks emit an audit line per decision; silence it so test output is clean.
logging.getLogger("egress").setLevel(logging.CRITICAL)


def run(coro):
    return asyncio.run(coro)


def _connect_flow(host, port=443, cid="conn-1", peer="1.2.3.4"):
    return SimpleNamespace(
        request=SimpleNamespace(host=host, port=port),
        client_conn=SimpleNamespace(peername=(peer, 5000), id=cid),
        response=None)


def _http_flow(host, pretty_host, *, scheme="https", port=443, method="GET"):
    return SimpleNamespace(
        request=SimpleNamespace(
            host=host, pretty_host=pretty_host, scheme=scheme, port=port,
            method=method, pretty_url=f"{scheme}://{pretty_host}/"),
        response=None)


class AuthorizeTests(unittest.TestCase):
    """_authorize: lifeline is decided locally; everything else asks the control
    plane and any failure fails CLOSED."""

    def test_permanent_lifeline_never_calls_control_plane(self):
        with mock.patch.object(addon, "_post_authorize",
                               side_effect=AssertionError("must not be called")):
            allowed, reason = run(addon._authorize("api.anthropic.com",
                                                   stage="connect"))
        self.assertTrue(allowed)
        self.assertIn("permanent lifeline", reason)

    def test_control_plane_allow(self):
        with mock.patch.object(addon, "_post_authorize",
                               return_value={"decision": "allow", "reason": "ok"}):
            allowed, reason = run(addon._authorize("example.com", stage="connect"))
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

    def test_control_plane_deny(self):
        with mock.patch.object(addon, "_post_authorize",
                               return_value={"decision": "deny", "reason": "nope"}):
            allowed, reason = run(addon._authorize("example.com", stage="connect"))
        self.assertFalse(allowed)
        self.assertEqual(reason, "nope")

    def test_control_plane_unreachable_fails_closed(self):
        with mock.patch.object(addon, "_post_authorize",
                               side_effect=OSError("connection refused")):
            allowed, reason = run(addon._authorize("example.com", stage="connect"))
        self.assertFalse(allowed)
        self.assertIn("fail-closed", reason)

    def test_unexpected_decision_value_is_not_allow(self):
        # Any decision that isn't exactly "allow" must be treated as deny.
        with mock.patch.object(addon, "_post_authorize",
                               return_value={"decision": "hold", "reason": "?"}):
            allowed, _ = run(addon._authorize("example.com", stage="connect"))
        self.assertFalse(allowed)


class HttpConnectTests(unittest.TestCase):
    def setUp(self):
        addon._conn_authority.clear()

    def test_allowed_host_records_authority_and_no_deny(self):
        flow = _connect_flow("example.com", cid="c1")
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize",
                               return_value={"decision": "allow", "reason": "ok"}):
            run(addon.http_connect(flow))
        self.assertIsNone(flow.response)                     # not denied
        self.assertEqual(addon._conn_authority.get("c1"), "example.com")

    def test_forbidden_host_denied_before_authority_recorded(self):
        flow = _connect_flow("control-plane", cid="c2")
        with mock.patch.object(addon, "_post_authorize",
                               side_effect=AssertionError("must not authorize")):
            run(addon.http_connect(flow))
        self.assertIsNotNone(flow.response)                  # denied by the guard
        self.assertNotIn("c2", addon._conn_authority)

    def test_bad_port_denied(self):
        flow = _connect_flow("example.com", port=22, cid="c3")
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize",
                               side_effect=AssertionError("must not authorize")):
            run(addon.http_connect(flow))
        self.assertIsNotNone(flow.response)
        self.assertNotIn("c3", addon._conn_authority)

    def test_control_plane_deny_denies_and_records_nothing(self):
        flow = _connect_flow("blocked.com", cid="c4")
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize",
                               return_value={"decision": "deny", "reason": "no"}):
            run(addon.http_connect(flow))
        self.assertIsNotNone(flow.response)
        self.assertNotIn("c4", addon._conn_authority)


class TlsClientHelloTests(unittest.TestCase):
    """The SNI-vs-CONNECT-authority check (anti-domain-fronting). Passthrough
    (``ignore_connection``) is granted ONLY when the SNI is absent or matches the
    authority recorded at CONNECT."""

    def _data(self, sni, cid="c1"):
        return SimpleNamespace(
            client_hello=SimpleNamespace(sni=sni),
            context=SimpleNamespace(client=SimpleNamespace(id=cid)),
            ignore_connection=False)

    def setUp(self):
        addon._conn_authority.clear()

    def test_sni_matches_authority_tunnels(self):
        addon._conn_authority["c1"] = "example.com"
        data = self._data("example.com")
        addon.tls_clienthello(data)
        self.assertTrue(data.ignore_connection)

    def test_sni_absent_tunnels(self):
        addon._conn_authority["c1"] = "example.com"
        data = self._data(None)
        addon.tls_clienthello(data)
        self.assertTrue(data.ignore_connection)

    def test_sni_mismatch_refuses_passthrough(self):
        addon._conn_authority["c1"] = "example.com"
        data = self._data("evil.com")
        addon.tls_clienthello(data)
        self.assertFalse(data.ignore_connection)            # fails closed

    def test_no_recorded_authority_refuses_when_sni_present(self):
        data = self._data("example.com")                    # nothing recorded
        addon.tls_clienthello(data)
        self.assertFalse(data.ignore_connection)


class RequestTests(unittest.TestCase):
    """``request`` gates BOTH the transport host and the Host/:authority — this
    is what closes domain-fronting on the decrypted/plain-HTTP path."""

    @staticmethod
    def _authorize_by_host(allow):
        def fake(payload):
            host = payload["host"]
            ok = host in allow
            return {"decision": "allow" if ok else "deny",
                    "reason": "ok" if ok else "not allowed"}
        return fake

    def test_both_names_allowed_passes(self):
        flow = _http_flow("allowed.com", "allowed.com")
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize",
                               side_effect=self._authorize_by_host({"allowed.com"})):
            run(addon.request(flow))
        self.assertIsNone(flow.response)

    def test_fronted_host_header_is_denied(self):
        # Transport host is authorized, but the Host header names a different,
        # non-authorized site — must be denied even though request.host is fine.
        flow = _http_flow("allowed.com", "fronted.com")
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize",
                               side_effect=self._authorize_by_host({"allowed.com"})):
            run(addon.request(flow))
        self.assertIsNotNone(flow.response)

    def test_forbidden_authority_denied_by_guard(self):
        flow = _http_flow("allowed.com", "control-plane")
        with mock.patch.object(addon, "_post_authorize",
                               side_effect=AssertionError("must not authorize")):
            run(addon.request(flow))
        self.assertIsNotNone(flow.response)

    def test_bad_port_denied(self):
        flow = _http_flow("allowed.com", "allowed.com", scheme="http", port=8080)
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize",
                               side_effect=AssertionError("must not authorize")):
            run(addon.request(flow))
        self.assertIsNotNone(flow.response)


class ClientDisconnectedTests(unittest.TestCase):
    def test_authority_is_dropped(self):
        addon._conn_authority["c9"] = "example.com"
        addon.client_disconnected(SimpleNamespace(id="c9"))
        self.assertNotIn("c9", addon._conn_authority)

    def test_unknown_client_is_harmless(self):
        addon.client_disconnected(SimpleNamespace(id="never-seen"))  # no raise


if __name__ == "__main__":
    unittest.main()
