# SPDX-License-Identifier: Apache-2.0
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


def _http_flow(host, pretty_host, *, scheme="https", port=443, method="GET",
               cid="req-1", peer="1.2.3.4"):
    # ``client_conn`` mirrors _connect_flow. Its ABSENCE here is why nothing noticed
    # that the request hook never sent a client to the control plane: the stub could
    # not have exercised the field, so every plaintext-HTTP decision was recorded
    # against no client at all and the tests were satisfied.
    return SimpleNamespace(
        request=SimpleNamespace(
            host=host, pretty_host=pretty_host, scheme=scheme, port=port,
            method=method, pretty_url=f"{scheme}://{pretty_host}/"),
        client_conn=SimpleNamespace(peername=(peer, 5000), id=cid),
        response=None)


class AuthorizeTests(unittest.TestCase):
    """_authorize: lifeline is decided locally; everything else asks the control
    plane and any failure fails CLOSED.

    ``Verdict.central`` reports whether the CONTROL PLANE recorded the decision, and
    it is checked on every path here because it is the control plane's ingest filter:
    a false positive double-counts a governed request in the audit table, a false
    negative loses a decision from it entirely."""

    def test_permanent_lifeline_never_calls_control_plane(self):
        with mock.patch.object(addon, "_post_authorize",
                               side_effect=AssertionError("must not be called")):
            v = run(addon._authorize("api.anthropic.com", stage="connect"))
        self.assertTrue(v.allowed)
        self.assertIn("permanent lifeline", v.reason)
        # Decided here, so nothing wrote it centrally — this line must be ingested.
        self.assertFalse(v.central)

    def test_control_plane_allow(self):
        with mock.patch.object(addon, "_post_authorize",
                               return_value={"decision": "allow", "reason": "ok"}):
            v = run(addon._authorize("example.com", stage="connect"))
        self.assertTrue(v.allowed)
        self.assertEqual(v.reason, "ok")
        self.assertTrue(v.central)

    def test_control_plane_deny(self):
        with mock.patch.object(addon, "_post_authorize",
                               return_value={"decision": "deny", "reason": "nope"}):
            v = run(addon._authorize("example.com", stage="connect"))
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "nope")
        self.assertTrue(v.central)

    def test_control_plane_unreachable_fails_closed(self):
        with mock.patch.object(addon, "_post_authorize",
                               side_effect=OSError("connection refused")):
            v = run(addon._authorize("example.com", stage="connect"))
        self.assertFalse(v.allowed)
        self.assertIn("fail-closed", v.reason)
        # The outage case: the control plane wrote nothing, so this local line is
        # the only record and must be ingested once the control plane returns.
        self.assertFalse(v.central)

    def test_unexpected_decision_value_is_not_allow(self):
        # Any decision that isn't exactly "allow" must be treated as deny.
        with mock.patch.object(addon, "_post_authorize",
                               return_value={"decision": "hold", "reason": "?"}):
            v = run(addon._authorize("example.com", stage="connect"))
        self.assertFalse(v.allowed)
        # The call still REACHED the control plane, which audits what it returned.
        self.assertTrue(v.central)


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

    def _data(self, sni, cid="c1", peer="172.30.0.2"):
        # `peername` mirrors _connect_flow / _http_flow. Its absence here is why the
        # fronting refusal could be recorded with no client for as long as it was.
        return SimpleNamespace(
            client_hello=SimpleNamespace(sni=sni),
            context=SimpleNamespace(
                client=SimpleNamespace(id=cid, peername=(peer, 5000))),
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

    def test_refusal_is_audited_in_the_shape_the_control_plane_ingests(self):
        """The fronting refusal is the whole reason the ingest exists, so its line
        has to land in the audit table's own vocabulary — not as a `deny-sni` verb
        the schema and the frontend's tag styling have no rule for."""
        addon._conn_authority["c1"] = "example.com"
        with mock.patch.object(addon, "_audit") as audited:
            addon.tls_clienthello(self._data("evil.com"))
        audited.assert_called_once()
        decision, fields = audited.call_args[0][0], audited.call_args[1]
        self.assertEqual(decision, "deny")
        self.assertEqual(fields["stage"], "sni")
        # `host` is the name the CLIENT asserted; the authority it contradicts is
        # named in the reason, so one row carries both sides of the mismatch.
        self.assertEqual(fields["host"], "evil.com")
        self.assertIn("example.com", fields["reason"])
        self.assertEqual(fields["client"], "172.30.0.2")
        # No /authorize call happens on this path at all, by design.
        self.assertFalse(fields["central"])

    def test_refusal_with_no_authority_still_names_a_client_and_reason(self):
        with mock.patch.object(addon, "_audit") as audited:
            addon.tls_clienthello(self._data("evil.com", peer="172.30.0.9"))
        fields = audited.call_args[1]
        self.assertEqual(fields["client"], "172.30.0.9")
        self.assertEqual(fields["host"], "evil.com")
        # Must not render as the string "None" where an authority would be.
        self.assertNotIn("None", fields["reason"])

    def test_tunnelled_connections_are_not_audited(self):
        """Passthrough is the non-event. Auditing it would put a line per TLS
        connection into a file the control plane now reads every 2 seconds."""
        addon._conn_authority["c1"] = "example.com"
        with mock.patch.object(addon, "_audit") as audited:
            addon.tls_clienthello(self._data("example.com"))
            addon.tls_clienthello(self._data(None))
        audited.assert_not_called()


class InternationalizedHostTests(unittest.TestCase):
    """One destination must have ONE spelling everywhere.

    mitmproxy IDNA-*decodes* the authority it parses off the wire, so
    ``request.host`` arrives as Unicode (``bücher.de``) while ``request.pretty_host``
    and the TLS SNI stay ASCII. Nothing downstream reconciles those: the approval
    card renders what it is given, a persisted rule keeps that spelling, and the SNI
    check is a plain string compare. ``_a_label`` folds them at the entry points —
    these tests hold that fold in place, because every symptom below is invisible
    until an internationalized host actually appears."""

    # bücher.de — a real IDN. Its A-label is what any client puts on the wire.
    UNICODE = "bücher.de"
    ASCII = "xn--bcher-kva.de"
    # apple.com with its leading letter swapped for U+0430 CYRILLIC SMALL LETTER A.
    # Written as an escape deliberately: spelled literally it is indistinguishable
    # from the real host HERE too, which is the whole complaint about the card.
    HOMOGLYPH = "\u0430pple.com"
    HOMOGLYPH_ASCII = "xn--pple-43d.com"

    def setUp(self):
        addon._conn_authority.clear()

    def test_a_label_folds_unicode_and_leaves_ascii_alone(self):
        self.assertEqual(addon._a_label(self.UNICODE), self.ASCII)
        self.assertEqual(addon._a_label(self.HOMOGLYPH), self.HOMOGLYPH_ASCII)
        # Already-ASCII names, including an A-label, must survive untouched.
        self.assertEqual(addon._a_label("example.com"), "example.com")
        self.assertEqual(addon._a_label(self.ASCII), self.ASCII)

    def test_a_label_falls_back_rather_than_rejecting_a_dns_valid_host(self):
        """The ``idna`` codec is stricter than DNS and than the rest of this proxy.
        Underscored and over-long labels resolve fine in practice, so raising here
        would turn a spelling helper into an outage for hosts that were never
        internationalized. Spelling is this function's job; gating is not."""
        for host in ("_dmarc.example.com", "a" * 64 + ".example.com", ""):
            self.assertEqual(addon._a_label(host), host)

    def test_connect_asks_the_control_plane_in_ascii(self):
        """The /authorize host is what the operator's card shows and what a persisted
        rule stores. Sent as Unicode, a homoglyph reaches the human looking exactly
        like the host it imitates — and the rule keeps the disguise."""
        flow = _connect_flow(self.HOMOGLYPH, cid="idn1")
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(
                 addon, "_post_authorize",
                 return_value={"decision": "allow", "reason": "ok"}) as authorized:
            run(addon.http_connect(flow))
        self.assertEqual(authorized.call_args[0][0]["host"], self.HOMOGLYPH_ASCII)

    def test_connect_audits_in_ascii(self):
        flow = _connect_flow(self.HOMOGLYPH, cid="idn2")
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize",
                               return_value={"decision": "allow", "reason": "ok"}), \
             mock.patch.object(addon, "_audit") as audited:
            run(addon.http_connect(flow))
        self.assertEqual(audited.call_args[1]["host"], self.HOMOGLYPH_ASCII)

    def test_an_approved_idn_is_not_then_refused_as_domain_fronting(self):
        """The regression this fold exists for. The SNI is ASCII by RFC 6066 and
        mitmproxy decodes it ``ascii`` only, so an authority remembered in Unicode
        could never equal it: every legitimate IDN was approved at CONNECT and then
        killed at the TLS stage, audited as a fronting attempt it never was."""
        flow = _connect_flow(self.UNICODE, cid="idn3")
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize",
                               return_value={"decision": "allow", "reason": "ok"}):
            run(addon.http_connect(flow))
        self.assertEqual(addon._conn_authority.get("idn3"), self.ASCII)

        data = SimpleNamespace(
            client_hello=SimpleNamespace(sni=self.ASCII),   # what TLS carries
            context=SimpleNamespace(
                client=SimpleNamespace(id="idn3", peername=("172.30.0.2", 5000))),
            ignore_connection=False)
        addon.tls_clienthello(data)
        self.assertTrue(data.ignore_connection)

    def test_fronting_an_idn_authority_is_still_refused(self):
        """The fold must not become a way through: matching is still exact, it just
        happens in one alphabet."""
        addon._conn_authority["idn4"] = self.ASCII
        data = SimpleNamespace(
            client_hello=SimpleNamespace(sni="evil.com"),
            context=SimpleNamespace(
                client=SimpleNamespace(id="idn4", peername=("172.30.0.2", 5000))),
            ignore_connection=False)
        addon.tls_clienthello(data)
        self.assertFalse(data.ignore_connection)

    def test_http_gates_one_destination_once(self):
        """``host`` and ``pretty_host`` disagree on spelling for an IDN, so ungated
        they are two set members — two /authorize calls, and two approval cards, for
        a single request to a single host."""
        flow = _http_flow(self.UNICODE, self.ASCII, scheme="http", port=80)
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(
                 addon, "_post_authorize",
                 return_value={"decision": "allow", "reason": "ok"}) as authorized:
            run(addon.request(flow))
        self.assertIsNone(flow.response)
        authorized.assert_called_once()
        self.assertEqual(authorized.call_args[0][0]["host"], self.ASCII)

    def test_http_still_gates_two_genuinely_different_names(self):
        """Folding spellings must not fold DESTINATIONS: a Host header naming a
        different site than the transport target is the fronting case, and it still
        has to clear the control plane on both names."""
        flow = _http_flow(self.UNICODE, "other.example", scheme="http", port=80)
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(
                 addon, "_post_authorize",
                 return_value={"decision": "allow", "reason": "ok"}) as authorized:
            run(addon.request(flow))
        asked = {c[0][0]["host"] for c in authorized.call_args_list}
        self.assertEqual(asked, {self.ASCII, "other.example"})


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

    def test_a_partly_local_decision_is_marked_uncentralised(self):
        """Two names are gated, and they can be decided by different authorities: a
        lifeline host short-circuits locally while the other goes to the control
        plane. The flow is centrally recorded only if EVERY consulted name was, so
        the flag is folded with `and` — taking the last name's answer would mark this
        row as already-audited and drop it from the ingest, losing the decision."""
        flow = _http_flow("api.anthropic.com", "other.example",
                          scheme="http", port=80)
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize",
                               side_effect=self._authorize_by_host({"other.example"})), \
             mock.patch.object(addon, "_audit") as audited:
            run(addon.request(flow))
        self.assertIsNone(flow.response)                    # both names cleared
        self.assertFalse(audited.call_args[1]["central"])

    def test_a_fully_central_decision_is_not_ingested_twice(self):
        # The other side of the fold: nothing local, so /authorize already wrote it.
        flow = _http_flow("allowed.com", "allowed.com", scheme="http", port=80)
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize",
                               side_effect=self._authorize_by_host({"allowed.com"})), \
             mock.patch.object(addon, "_audit") as audited:
            run(addon.request(flow))
        self.assertTrue(audited.call_args[1]["central"])

    def test_locally_denied_requests_still_name_their_client(self):
        """The guard and port-gate denials are audited BEFORE /authorize is reached,
        so they are ingested rows — and an ingested row that cannot say who asked is
        half a record. They read the peer only after the guards until this ingest
        made them visible, which is where the omission would have shown up."""
        flow = _http_flow("allowed.com", "allowed.com", scheme="http", port=8080,
                          peer="172.30.0.7")
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_audit") as audited:
            run(addon.request(flow))
        self.assertIsNotNone(flow.response)                 # port-gated
        fields = audited.call_args[1]
        self.assertEqual(fields["client"], "172.30.0.7")
        self.assertEqual(fields["host"], "allowed.com")
        self.assertFalse(fields["central"])

    def test_the_authorize_call_says_which_sandbox_asked(self):
        """The control plane is SHARED across sandboxes, so an audit row that cannot
        name the client is half a record. ``http_connect`` has always sent it; this
        path never did, so every plaintext-HTTP decision was stored against no client.

        It survived this long because nothing rendered the column — and because the
        flow stub had no ``client_conn`` for a test to have caught it with. Found in
        the first live run after the decisions table grew a client column and showed
        an em dash where the address belonged."""
        seen = []

        def capture(payload):
            seen.append(payload)
            return {"decision": "allow", "reason": "ok"}

        flow = _http_flow("allowed.com", "allowed.com", peer="172.30.0.2")
        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize", side_effect=capture):
            run(addon.request(flow))
        self.assertTrue(seen, "no authorize call was made")
        self.assertEqual(seen[0]["client"], "172.30.0.2")
        # The stage travels too — it is what the decisions table renders as the
        # qualifier distinguishing a plaintext decision from a tunnelled one.
        self.assertEqual(seen[0]["stage"], "http")

    def test_a_connect_and_a_request_report_the_client_the_same_way(self):
        # One derivation, two hooks. They disagreed silently for as long as only one
        # of them was checked.
        calls = []

        def capture(payload):
            calls.append(payload)
            return {"decision": "allow", "reason": "ok"}

        with mock.patch.object(addon.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             mock.patch.object(addon, "_post_authorize", side_effect=capture):
            run(addon.http_connect(_connect_flow("allowed.com", peer="172.30.0.9")))
            run(addon.request(_http_flow("allowed.com", "allowed.com",
                                         peer="172.30.0.9")))
        self.assertEqual([c["client"] for c in calls], ["172.30.0.9", "172.30.0.9"])

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
