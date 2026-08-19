# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the egress proxy's security-load-bearing decision helpers
(``proxies/egress/addon.py``): host matching, the permanent lifeline, env
parsing, and — most importantly — the ``_forbidden`` control-plane relay guard
that keeps the agent off the control plane where segmentation cannot.

These assert properties ``boundary-check.sh`` cannot: it observes reachability
from the sandbox, but cannot exercise the guard's name/IP/resolve branches or the
env-parsing edge cases directly. Dependency-free (see ``tests/_loader.py``)."""
from __future__ import annotations

import unittest
from unittest import mock

from _loader import load_egress_addon

addon = load_egress_addon()


class MatchTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(addon._match("example.com", "example.com"))
        self.assertFalse(addon._match("evil.com", "example.com"))

    def test_bare_pattern_is_not_a_suffix_match(self):
        # A bare (dotless) entry must NOT match subdomains — else "example.com"
        # would silently authorize "evil-example.com" / "a.example.com".
        self.assertFalse(addon._match("a.example.com", "example.com"))

    def test_leading_dot_matches_apex_and_subdomains(self):
        self.assertTrue(addon._match("example.com", ".example.com"))
        self.assertTrue(addon._match("a.b.example.com", ".example.com"))
        self.assertFalse(addon._match("example.com", ".other.com"))

    def test_leading_dot_is_not_fooled_by_suffix_lookalike(self):
        # ".example.com" must not match "notexample.com".
        self.assertFalse(addon._match("notexample.com", ".example.com"))


class PermanentLifelineTests(unittest.TestCase):
    def test_default_lifeline_hosts_are_permanent(self):
        self.assertTrue(addon._is_permanent("api.anthropic.com"))
        self.assertTrue(addon._is_permanent("claude.ai"))

    def test_permanent_is_case_insensitive(self):
        self.assertTrue(addon._is_permanent("API.Anthropic.COM"))

    def test_non_lifeline_host_is_not_permanent(self):
        self.assertFalse(addon._is_permanent("example.com"))

    def test_subdomain_of_exact_lifeline_is_not_permanent(self):
        # Defaults are exact (no leading dot), so a subdomain is NOT auto-lifeline
        # — it must go through the control plane like anything else.
        self.assertFalse(addon._is_permanent("evil.api.anthropic.com"))

    def test_empty_host_is_not_permanent(self):
        self.assertFalse(addon._is_permanent(""))


class LifelineScopeTests(unittest.TestCase):
    """``_is_lifeline`` = the host half AND the client half.

    ``_is_permanent`` above says what the lifeline is for; this says who. The two
    were one check until mcp-net gave this proxy a second, differently-trusted
    client population — MCP server containers, which hold credentials and never
    talk to Anthropic. A host-only check hands them the sole allow that skips the
    policy authority."""

    SANDBOX = "172.30.0.2"
    MCP = "172.28.0.2"

    def test_the_sandbox_gets_the_lifeline(self):
        self.assertTrue(addon._is_lifeline("api.anthropic.com", self.SANDBOX))

    def test_an_mcp_server_does_not(self):
        self.assertFalse(addon._is_lifeline("api.anthropic.com", self.MCP))

    def test_the_host_half_still_binds(self):
        # Being on sandbox-net grants the lifeline for lifeline hosts only —
        # everything else the agent asks for is still the control plane's call.
        self.assertFalse(addon._is_lifeline("example.com", self.SANDBOX))

    def test_an_unknown_client_does_not_qualify(self):
        # Falls through to /authorize, which is the fail-safe direction.
        for client in (None, "", "not-an-address", "[::1]"):
            with self.subTest(client=client):
                self.assertFalse(addon._is_lifeline("api.anthropic.com", client))

    def test_a_v4_mapped_sandbox_address_is_the_same_client(self):
        # A dual-stack listener can report the peer this way; the fold in
        # _in_cidrs keeps the grant from turning on a spelling.
        self.assertTrue(addon._is_lifeline("api.anthropic.com",
                                           f"::ffff:{self.SANDBOX}"))

    def test_emptying_the_range_disables_the_lifeline_rather_than_widening_it(self):
        # Deliberately NOT guarded by _assert_guard_configured, unlike
        # EGRESS_FORBIDDEN_CIDRS, because this is the direction it fails in: no
        # client qualifies, so every host goes to the control plane. Losing outage
        # resilience is a cost; it is not a hole, and the difference is why one of
        # these lists is asserted at startup and the other is a knob.
        with mock.patch.object(addon, "LIFELINE_CIDRS", ()):
            self.assertFalse(addon._is_lifeline("api.anthropic.com", self.SANDBOX))


class InCidrsTests(unittest.TestCase):
    NETS = addon._parse_cidrs("DOCKADE_UNSET_LIFELINE", "172.30.0.0/24")

    def test_inside_and_outside(self):
        self.assertTrue(addon._in_cidrs("172.30.0.99", self.NETS))
        self.assertFalse(addon._in_cidrs("172.31.0.2", self.NETS))

    def test_unparseable_or_absent_is_false(self):
        for value in (None, "", "  ", "example.com", "172.30.0.999"):
            with self.subTest(value=value):
                self.assertFalse(addon._in_cidrs(value, self.NETS))

    def test_no_networks_matches_nothing(self):
        self.assertFalse(addon._in_cidrs("172.30.0.2", ()))


class EnvParsingTests(unittest.TestCase):
    def test_hosts_strips_lowercases_and_drops_empties(self):
        got = addon._hosts("DOCKADE_UNSET_HOSTS", "A.com, .B.com , ,c.COM")
        self.assertEqual(got, ("a.com", ".b.com", "c.com"))

    def test_ports_parses_ints_and_drops_blanks(self):
        self.assertEqual(addon._ports("DOCKADE_UNSET_PORTS", "443, 8443 ,"),
                         frozenset({443, 8443}))

    def test_parse_cidrs_keeps_valid_drops_invalid(self):
        nets = addon._parse_cidrs("DOCKADE_UNSET_CIDRS",
                                  "172.31.0.0/24, not-a-cidr, 10.0.0.0/8")
        rendered = {str(n) for n in nets}
        self.assertEqual(rendered, {"172.31.0.0/24", "10.0.0.0/8"})


class ForbiddenGuardTests(unittest.TestCase):
    """The relay guard — the one place segmentation can't cover, so it must be
    airtight. Defaults: FORBIDDEN_HOSTS = control-plane/-ui, FORBIDDEN_CIDRS = the
    two control subnets (control-net 172.31.0.0/24 and authorize-net
    172.29.0.0/24), plus PRIVATE_CIDRS (cloud metadata / link-local, loopback,
    RFC1918) which are hard-blocked just the same so the proxy can never be an
    SSRF pivot to the instance-metadata service or the internal net."""

    def test_both_control_subnets_are_forbidden(self):
        # authorize-net is the one the proxy is actually ATTACHED to, so it is the
        # subnet a relayed connection could really land on; control-net is listed
        # even though the proxy has no route there, because unroutability is a
        # property of docker-compose.yml that this guard cannot verify.
        for ip in ("172.31.0.9", "172.29.0.2"):
            reason = addon._forbidden_reason(ip)
            self.assertIsNotNone(reason, ip)
            self.assertIn("control network", reason, ip)

    def test_the_authorize_listener_is_not_reachable_by_dialing_its_port(self):
        # The split puts /authorize on a port the proxy legitimately talks to, so
        # the guard must refuse the control plane by DESTINATION regardless of
        # port — it never sees one, and must not be understood to allow 8091.
        self.assertIsNotNone(addon._forbidden_reason("172.29.0.2"))
        self.assertIsNotNone(addon._forbidden_reason("control-plane"))

    def test_forbidden_hostname(self):
        self.assertIsNotNone(addon._forbidden_reason("control-plane"))
        self.assertIsNotNone(addon._forbidden_reason("control-plane-ui"))

    def test_forbidden_hostname_normalized(self):
        # Case + a trailing FQDN dot must not slip past the name check.
        self.assertIsNotNone(addon._forbidden_reason("CONTROL-PLANE."))

    def test_literal_ip_in_control_net_is_forbidden(self):
        reason = addon._forbidden_reason("172.31.0.9")
        self.assertIsNotNone(reason)
        self.assertIn("control network", reason)

    def test_cloud_metadata_ip_is_forbidden(self):
        # 169.254.169.254 (the instance-metadata service) is the highest-impact
        # SSRF target reachable via egress-net — it must be hard-blocked, not left
        # to the default-deny policy where one human approval could open it.
        reason = addon._forbidden_reason("169.254.169.254")
        self.assertIsNotNone(reason)
        self.assertIn("private/special-use", reason)

    def test_rfc1918_and_loopback_ips_are_forbidden(self):
        for ip in ("10.0.0.1", "192.168.1.1", "172.16.5.5", "127.0.0.1"):
            self.assertIsNotNone(addon._forbidden_reason(ip), ip)

    def test_public_literal_ip_is_allowed(self):
        # A genuinely public numeric address resolves to itself (no network),
        # so it passes the guard (policy still decides allow/hold/deny).
        self.assertIsNone(addon._forbidden_reason("8.8.8.8"))

    def test_name_resolving_into_control_net_is_forbidden(self):
        # A public name whose DNS is pointed at control-net must be caught by the
        # resolve step, not just the literal-name/IP checks.
        fake = [(2, 1, 6, "", ("172.31.0.5", 0))]
        with mock.patch.object(addon.socket, "getaddrinfo", return_value=fake):
            reason = addon._forbidden_reason("sneaky.example.com")
        self.assertIsNotNone(reason)
        self.assertIn("172.31.0.5", reason)

    def test_name_resolving_into_metadata_ip_is_forbidden(self):
        # DNS-rebinding a name onto the metadata service must be caught by the
        # resolve step too (best-effort, but it covers the private ranges).
        fake = [(2, 1, 6, "", ("169.254.169.254", 0))]
        with mock.patch.object(addon.socket, "getaddrinfo", return_value=fake):
            reason = addon._forbidden_reason("rebind.example.com")
        self.assertIsNotNone(reason)
        self.assertIn("169.254.169.254", reason)

    def test_name_resolving_outside_control_net_is_allowed(self):
        fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with mock.patch.object(addon.socket, "getaddrinfo", return_value=fake):
            self.assertIsNone(addon._forbidden_reason("example.com"))

    def test_unresolvable_name_is_not_forbidden_by_resolve_step(self):
        # getaddrinfo failure must not crash the guard; it simply can't add a
        # resolve-based reason (the name/IP checks already ran).
        with mock.patch.object(addon.socket, "getaddrinfo",
                               side_effect=OSError("no such host")):
            self.assertIsNone(addon._forbidden_reason("nonexistent.invalid"))

    def test_blocked_cidr_classifies_ips(self):
        self.assertIsNone(addon._blocked_cidr("not-an-ip"))
        self.assertIsNone(addon._blocked_cidr("8.8.8.8"))
        # control-net vs private/special-use are labelled distinctly.
        self.assertEqual(addon._cidr_label(addon._blocked_cidr("172.31.0.1")),
                         "control network")
        self.assertEqual(addon._cidr_label(addon._blocked_cidr("169.254.169.254")),
                         "private/special-use")


class EmbeddedIPv4GuardTests(unittest.TestCase):
    """REGRESSION: a blocked v4 destination written as an IPv4-embedding IPv6
    literal must still be caught.

    This was a real bypass of both branches the guard calls deterministic.
    Containment never crosses address families, so ``::ffff:169.254.169.254`` sat
    in none of the v4 PRIVATE_CIDRS; and the resolve branch could not save it
    either, because ``getaddrinfo`` returns that same mapped form straight back —
    while ``connect()`` on a v4-mapped address delivers to the v4 host. One
    approval or allow rule would then have reached cloud metadata (which serves on
    :80, already a permitted HTTP port), and ``boundary-check.sh`` probed only the
    dotted-quad form so nothing failed. Each spelling below must be blocked
    WITHOUT a DNS lookup, so getaddrinfo is patched to raise: these are the
    deterministic branch, not the best-effort one."""

    def _reason_without_dns(self, host):
        with mock.patch.object(addon.socket, "getaddrinfo",
                               side_effect=OSError("no DNS in this test")):
            return addon._forbidden_reason(host)

    def test_v4_mapped_control_net_is_forbidden(self):
        for spelling in ("::ffff:172.31.0.2",      # dotted embedded form
                         "::ffff:ac1f:2",          # same address, hex form
                         "[::ffff:172.31.0.2]",    # as it arrives in an authority
                         "::172.31.0.2",           # deprecated v4-compatible
                         "64:ff9b::172.31.0.2",    # NAT64
                         "2002:ac1f:2::1"):        # 6to4
            reason = self._reason_without_dns(spelling)
            self.assertIsNotNone(reason, spelling)
            self.assertIn("control network", reason, spelling)

    def test_v4_mapped_metadata_and_loopback_are_forbidden(self):
        for spelling in ("::ffff:169.254.169.254", "::ffff:127.0.0.1",
                         "64:ff9b::a9fe:a9fe", "[::1]"):
            reason = self._reason_without_dns(spelling)
            self.assertIsNotNone(reason, spelling)
            self.assertIn("private/special-use", reason, spelling)

    def test_v4_mapped_public_address_still_passes_to_policy(self):
        # The fold must be PRECISE, not a blanket ban on v6 or on mapped forms:
        # a mapped/6to4-wrapped PUBLIC address carries no local reach, so it goes
        # to policy like any other host (which then holds or denies it).
        for spelling in ("::ffff:8.8.8.8", "2002:0808:0808::1",
                         "2606:4700:4700::1111"):
            self.assertIsNone(self._reason_without_dns(spelling), spelling)

    def test_extra_special_use_v4_ranges_are_forbidden(self):
        # 0.0.0.0 is loopback by another spelling (connect(0.0.0.0) -> localhost);
        # the rest are CGNAT/overlay, protocol assignments, benchmarking,
        # multicast, reserved, and the broadcast address.
        for ip in ("0.0.0.0", "100.64.0.1", "192.0.0.1", "198.18.0.1",  # noqa: S104 (a blocked DESTINATION, not a bind address)
                   "224.0.0.1", "255.255.255.255"):
            self.assertIsNotNone(self._reason_without_dns(ip), ip)

    def test_resolve_branch_also_folds_embedded_v4(self):
        # A name that resolves to a MAPPED control-net address must be caught too
        # — getaddrinfo legitimately returns AF_INET6 results.
        fake = [(10, 1, 6, "", ("::ffff:172.31.0.5", 0, 0, 0))]
        with mock.patch.object(addon.socket, "getaddrinfo", return_value=fake):
            reason = addon._forbidden_reason("rebind6.example.com")
        self.assertIsNotNone(reason)
        self.assertIn("control network", reason)

    def test_embedded_ipv4_returns_none_for_plain_addresses(self):
        import ipaddress
        self.assertIsNone(addon._embedded_ipv4(
            ipaddress.ip_address("2606:4700:4700::1111")))
        self.assertIsNone(addon._embedded_ipv4(ipaddress.ip_address("8.8.8.8")))
        # The unspecified address embeds no v4 host (low 32 bits are zero).
        self.assertIsNone(addon._embedded_ipv4(ipaddress.ip_address("::")))

    def test_unbracket_leaves_ordinary_hosts_alone(self):
        self.assertEqual(addon._unbracket("example.com"), "example.com")
        self.assertEqual(addon._unbracket("[::1]"), "::1")
        self.assertEqual(addon._unbracket(" 172.31.0.2 "), "172.31.0.2")


class GuardConfigTests(unittest.TestCase):
    """The relay guard must never be silently disabled by configuration: with no
    forbidden CIDRs the literal-IP and resolve branches vanish, leaving only exact
    hostname matching. Startup must fail CLOSED in that case."""

    def test_startup_ok_with_default_cidrs(self):
        # Defaults include 172.31.0.0/24, so this must not raise.
        addon._assert_guard_configured()

    def test_startup_fails_closed_without_forbidden_cidrs(self):
        with mock.patch.object(addon, "FORBIDDEN_CIDRS", ()), \
                self.assertRaises(RuntimeError):
            addon._assert_guard_configured()


class MetadataProbeTests(unittest.TestCase):
    """The cloud sanity check warns IFF the metadata service is actually reachable from
    the proxy. It is a heads-up, not a control (the relay guard blocks metadata
    regardless); this pins the warn/quiet branch so the signal cannot silently invert."""

    def _run(self, connect):
        log = mock.Mock()
        with mock.patch.object(addon.socket, "create_connection", connect), \
                mock.patch.object(addon, "logger", log):
            addon._probe_metadata_reachable()
        return log

    def test_warns_when_metadata_is_reachable(self):
        log = self._run(mock.Mock(return_value=mock.Mock()))  # connect succeeds
        self.assertTrue(log.warning.called)
        self.assertFalse(log.info.called)

    def test_quiet_when_metadata_is_unreachable(self):
        log = self._run(mock.Mock(side_effect=OSError("no route")))  # both ports fail
        self.assertFalse(log.warning.called)
        self.assertTrue(log.info.called)


class AuditFileRotationTests(unittest.TestCase):
    """The local audit file is size-rotated so it cannot grow without bound on a
    long-lived volume. Pin that _setup_audit_file installs a RotatingFileHandler with
    the configured cap and backup count — a plain FileHandler here would silently
    reintroduce the unbounded growth this closes. (What makes the rename LOSSLESS is
    the control plane's ingest, tested in test_control_plane_ingest.py.)"""

    def test_installs_a_rotating_handler_with_the_configured_caps(self):
        with mock.patch.object(addon.os, "makedirs"), \
                mock.patch.object(addon.logger, "addHandler"), \
                mock.patch.multiple(addon, AUDIT_MAX_BYTES=4096, AUDIT_BACKUPS=3), \
                mock.patch.object(addon.logging.handlers,
                                  "RotatingFileHandler") as rfh:
            addon._setup_audit_file()
        self.assertTrue(rfh.called)
        _, kwargs = rfh.call_args
        self.assertEqual(kwargs["maxBytes"], 4096)
        self.assertEqual(kwargs["backupCount"], 3)

    def test_audit_file_failure_is_not_fatal(self):
        # Best-effort sink: a filesystem error warns and falls back to stdout, never
        # raises — a proxy that cannot open its convenience log must still run.
        log = mock.Mock()
        with mock.patch.object(addon.os, "makedirs",
                               side_effect=OSError("read-only fs")), \
                mock.patch.object(addon, "logger", log):
            addon._setup_audit_file()
        self.assertTrue(log.warning.called)


if __name__ == "__main__":
    unittest.main()
