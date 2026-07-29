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
    airtight. Defaults: FORBIDDEN_HOSTS = control-plane/-ui, FORBIDDEN_CIDRS =
    172.31.0.0/24."""

    def test_forbidden_hostname(self):
        self.assertIsNotNone(addon._forbidden_reason("control-plane"))
        self.assertIsNotNone(addon._forbidden_reason("control-plane-ui"))

    def test_forbidden_hostname_normalized(self):
        # Case + a trailing FQDN dot must not slip past the name check.
        self.assertIsNotNone(addon._forbidden_reason("CONTROL-PLANE."))

    def test_literal_ip_in_control_net_is_forbidden(self):
        self.assertIsNotNone(addon._forbidden_reason("172.31.0.9"))

    def test_literal_ip_outside_control_net_needs_no_dns(self):
        # A numeric address outside the range resolves to itself (no network),
        # so it is allowed through the guard.
        self.assertIsNone(addon._forbidden_reason("10.0.0.1"))

    def test_name_resolving_into_control_net_is_forbidden(self):
        # A public name whose DNS is pointed at control-net must be caught by the
        # resolve step, not just the literal-name/IP checks.
        fake = [(2, 1, 6, "", ("172.31.0.5", 0))]
        with mock.patch.object(addon.socket, "getaddrinfo", return_value=fake):
            reason = addon._forbidden_reason("sneaky.example.com")
        self.assertIsNotNone(reason)
        self.assertIn("172.31.0.5", reason)

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

    def test_ip_in_forbidden_rejects_garbage(self):
        self.assertFalse(addon._ip_in_forbidden("not-an-ip"))
        self.assertTrue(addon._ip_in_forbidden("172.31.0.1"))
        self.assertFalse(addon._ip_in_forbidden("8.8.8.8"))


if __name__ == "__main__":
    unittest.main()
