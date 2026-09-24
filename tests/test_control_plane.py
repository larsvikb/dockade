# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the control plane's security-load-bearing logic
(``control-plane/policy.py`` and ``holds.py``): the policy decision ``_decide``
(block-wins-over-allow, subdomain semantics, default-hold, and per-client-class
scoping), the per-tool decision ``_decide_tool`` that answers the same question for
the MCP gateway's surface, the address -> class mapping that feeds the first, and
the hold registry (``_reserve_hold`` / ``_release_hold``: the two caps, and duplicate
grouping).

The hold cap is exactly what ``boundary-check.sh`` cannot assert: an over-cap
request returns the same opaque 403 to the agent as any other deny, so the cap's
concurrency/availability behavior is invisible from the sandbox's vantage point
(see DESIGN.md). Here we test the reservation function directly.

Dependency-free: ``fastapi``/``pydantic`` are stubbed (see ``tests/_loader.py``),
and the store is a throwaway SQLite file in a temp dir set before import."""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import threading
import time
import unittest
from typing import ClassVar
from unittest import mock

# The module reads CONTROL_DB at import time — point it at a throwaway file and
# suppress seeding (we drive the rules table directly) before loading.
_TMP = tempfile.mkdtemp(prefix="dockade-cp-test-")
os.environ["CONTROL_DB"] = os.path.join(_TMP, "control.db")
os.environ["CONTROL_SEED"] = os.path.join(_TMP, "nonexistent-seed.txt")

from _loader import load_control_plane  # noqa: E402 (must set env first)

cp = load_control_plane()


CLASS = cp.store.LEGACY_CLIENT_CLASS          # the class these tests decide as


def _set_rules(rules):
    """Replace the rules table with (pattern, action) or (pattern, action, class)
    tuples. The two-element form is the common case — a rule for ``CLASS`` — so the
    class-scoping tests are the ones that have to say so, not every other test."""
    with cp.store._connect() as conn:
        conn.execute("DELETE FROM rules")
        conn.executemany(
            "INSERT INTO rules(pattern, action, source, created_at, client_class) "
            "VALUES (?,?, 'test', 0, ?)",
            [r if len(r) == 3 else (*r, CLASS) for r in rules])
        conn.commit()


def _set_leases(leases):
    """Replace the leases table with (host, seconds_from_now) or
    (host, seconds_from_now, class) tuples.

    The offset is RELATIVE and applied here, so a test says "expired 5 seconds ago"
    (-5) or "half an hour left" (1800) rather than computing an absolute instant — the
    thing under test is the boundary, and an absolute timestamp in the fixture puts
    arithmetic on both sides of it."""
    now = time.time()
    with cp.store._connect() as conn:
        conn.execute("DELETE FROM leases")
        conn.executemany(
            "INSERT INTO leases(host, client_class, approval_id, created_at, "
            "expires_at, granted_by) VALUES (?,?, 'test-approval', ?, ?, 'test')",
            [(host, cls, now, now + offset)
             for host, offset, cls in
             [(le if len(le) == 3 else (*le, CLASS)) for le in leases]])
        conn.commit()


class DecideTests(unittest.TestCase):
    def setUp(self):
        cp.store._init_db()
        _set_rules([])

    def test_unmatched_host_is_held(self):
        decision, reason = cp.policy._decide("unknown.example.com", CLASS)
        self.assertEqual(decision, "hold")
        self.assertIn("held for approval", reason)

    def test_exact_allow_rule(self):
        _set_rules([("example.com", "allow")])
        self.assertEqual(cp.policy._decide("example.com", CLASS)[0], "allow")
        # A bare rule must not authorize a subdomain.
        self.assertEqual(cp.policy._decide("a.example.com", CLASS)[0], "hold")

    def test_block_rule_denies(self):
        _set_rules([("blocked.com", "block")])
        self.assertEqual(cp.policy._decide("blocked.com", CLASS)[0], "deny")

    def test_block_wins_over_allow(self):
        # A host can't be both allowed and blocked by one pattern in one class
        # (UNIQUE(pattern, client_class)), but two DISTINCT patterns can both match a
        # host — e.g. a wildcard allow with a specific-subdomain block. Block wins.
        _set_rules([(".example.com", "allow"), ("evil.example.com", "block")])
        decision, reason = cp.policy._decide("evil.example.com", CLASS)
        self.assertEqual(decision, "deny")
        self.assertIn("blocked", reason)
        # A sibling under the same wildcard is still allowed.
        self.assertEqual(cp.policy._decide("safe.example.com", CLASS)[0], "allow")

    def test_subdomain_allow_matches_apex_and_children(self):
        _set_rules([(".example.com", "allow")])
        self.assertEqual(cp.policy._decide("example.com", CLASS)[0], "allow")
        self.assertEqual(cp.policy._decide("a.b.example.com", CLASS)[0], "allow")
        self.assertEqual(cp.policy._decide("notexample.com", CLASS)[0], "hold")

    def test_decision_is_case_insensitive(self):
        _set_rules([("example.com", "allow")])
        self.assertEqual(cp.policy._decide("EXAMPLE.COM", CLASS)[0], "allow")

    def test_trailing_fqdn_dot_is_normalized(self):
        # `evil.com.` and `evil.com` are the same destination, so an operator BLOCK of
        # `evil.com` must not be evadable with the trailing-dot spelling — which would
        # otherwise miss the rule, land in a hold and be re-promptable indefinitely.
        _set_rules([("evil.com", "block")])
        self.assertEqual(cp.policy._decide("evil.com.", CLASS)[0], "deny")
        # A trailing dot on an allowed host still resolves to allow, not hold.
        _set_rules([("example.com", "allow")])
        self.assertEqual(cp.policy._decide("example.com.", CLASS)[0], "allow")


def _set_tool_rules(rules, servers=()):
    """Replace the tool_rules table with (server, tool, action) tuples, and register
    every server they name — plus any in ``servers`` — as ENABLED.

    The registration is part of the fixture rather than a separate call because a rule
    on an unregistered or disabled server decides nothing (``_decide_tool``), so
    without it every test below would pass for that reason instead of the one it is
    about — the vacuous pass this suite exists to avoid. ``servers`` is for the tests
    whose subject is a server with NO rule on it; the two server-state refusals have
    their own tests."""
    named = dict.fromkeys([*(r[0] for r in rules), *servers])
    with cp.store._connect() as conn:
        conn.execute("DELETE FROM tool_rules")
        conn.execute("DELETE FROM mcp_servers")
        conn.executemany(
            "INSERT INTO mcp_servers(server, enabled, auth_type, created_at) "
            "VALUES (?, 1, 'none', 0)", [(server,) for server in named])
        conn.executemany(
            "INSERT INTO tool_rules(server, tool, action, source, created_at) "
            "VALUES (?,?,?, 'test', 0)", rules)
        conn.commit()


class DecideToolTests(unittest.TestCase):
    """``_decide_tool`` — the gateway's surface. Every test here is a place the tool
    path deliberately does NOT behave like the egress path above, which is the only
    reason this needs its own class: the shared vocabulary (three actions, a
    (thing, scope) key) makes the two look interchangeable, and the whole risk is
    someone later "fixing" one of these divergences into consistency."""

    def setUp(self):
        cp.store._init_db()
        _set_tool_rules([], servers=["mcp-github"])

    def test_an_unconfigured_tool_is_denied_not_held(self):
        # The divergence that matters most. An unmatched HOST is held, because the
        # set of hosts is unbounded and discovered at runtime; a server's tool set is
        # finite and enumerable, so the unknown can be refused for the price of a
        # configuration step. If this ever returns 'hold', the exposed tool list has
        # stopped being a configuration artifact.
        decision, reason = cp.policy._decide_tool("mcp-github", "merge_pull_request")
        self.assertEqual(decision, "deny")
        self.assertIn("denied, not held", reason)

    def test_an_unregistered_server_denies_before_any_rule_is_consulted(self):
        # And says so in its own words rather than borrowing the unconfigured-tool
        # reason: the operator action is registering a server, not writing a rule.
        _set_tool_rules([("mcp-github", "get_me", "allow")])
        decision, reason = cp.policy._decide_tool("mcp-nobody", "get_me")
        self.assertEqual(decision, "deny")
        self.assertIn("is registered", reason)

    def test_a_disabled_server_grants_nothing_its_rules_say(self):
        # The switch means "the gateway will no longer dial this server", and the
        # authority has to answer accordingly: a gateway that asks anyway — buggy,
        # racing a just-flipped switch, or compromised — must get a refusal rather
        # than a grant it is trusted not to act on.
        _set_tool_rules([("mcp-github", "get_me", "allow")])
        with cp.store._connect() as conn:
            conn.execute("UPDATE mcp_servers SET enabled=0 WHERE server='mcp-github'")
            conn.commit()
        decision, reason = cp.policy._decide_tool("mcp-github", "get_me")
        self.assertEqual(decision, "deny")
        self.assertIn("disabled", reason)

    def test_each_action_is_returned_as_itself(self):
        _set_tool_rules([("mcp-github", "get_me", "allow"),
                         ("mcp-github", "create_branch", "deny"),
                         ("mcp-github", "create_pull_request", "ask")])
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "allow")
        self.assertEqual(
            cp.policy._decide_tool("mcp-github", "create_branch")[0], "deny")
        self.assertEqual(
            cp.policy._decide_tool("mcp-github", "create_pull_request")[0], "ask")

    def test_a_rule_decides_only_for_its_own_server(self):
        # Tool names are not namespaced across servers, so the server half of the key
        # is load-bearing: an `issue_read` allowed on one server must not answer for a
        # different server's identically named tool.
        _set_tool_rules([("mcp-github", "issue_read", "allow")])
        self.assertEqual(cp.policy._decide_tool("mcp-github", "issue_read")[0],
                         "allow")
        self.assertEqual(cp.policy._decide_tool("mcp-other", "issue_read")[0], "deny")

    def test_tool_names_are_case_sensitive(self):
        # The opposite of `_decide`, which lowercases the host. A hostname is
        # case-insensitive by DNS; a tool name is an identifier the server chose, so
        # folding case would let one rule decide two distinct tools.
        _set_tool_rules([("mcp-github", "get_me", "allow")])
        self.assertEqual(cp.policy._decide_tool("mcp-github", "GET_ME")[0], "deny")

    def test_there_is_no_wildcard(self):
        # `.example.com` is a subdomain wildcard on the host path. A tool name has no
        # hierarchy, so nothing here may read a leading dot — or any other character —
        # as breadth. Both spellings are just names that do not match.
        _set_tool_rules([(".mcp-github", ".issue", "allow"),
                         ("mcp-github", "issue_", "allow")])
        self.assertEqual(cp.policy._decide_tool("mcp-github", "issue_read")[0], "deny")

    def test_an_unknown_stored_action_fails_closed(self):
        # Unreachable through the API, which validates on write — and handled anyway,
        # because the store is a file on a volume. A hand-edited or corrupted row must
        # never be the thing that grants.
        _set_tool_rules([("mcp-github", "get_me", "allowed"),      # not 'allow'
                         ("mcp-github", "get_teams", "")])
        for tool in ("get_me", "get_teams"):
            decision, reason = cp.policy._decide_tool("mcp-github", tool)
            self.assertEqual(decision, "deny", tool)
            self.assertIn("unknown action", reason)

    def test_a_missing_server_or_tool_is_denied(self):
        for server, tool in (("", "get_me"), ("mcp-github", ""), ("", ""),
                             (None, None)):
            self.assertEqual(cp.policy._decide_tool(server, tool)[0], "deny",
                             (server, tool))

    def test_surrounding_whitespace_does_not_defeat_a_deny(self):
        # A deny that a trailing space could slip past would be the worst possible
        # shape of this bug: the rule reads as policy in force in the UI while the
        # call it names goes through.
        _set_tool_rules([("mcp-github", "delete_file", "deny")])
        self.assertEqual(
            cp.policy._decide_tool(" mcp-github ", " delete_file ")[0], "deny")
        # ...and the same normalization on the allow side, so a stray space is a
        # consistent no-op rather than a silent downgrade to deny.
        _set_tool_rules([("mcp-github", "get_me", "allow")])
        self.assertEqual(cp.policy._decide_tool("mcp-github ", " get_me")[0], "allow")


class LeaseDecisionTests(unittest.TestCase):
    """``_decide``'s third pass: a lease is an allow that expires.

    Every test here is a property the ORDER of the three passes is responsible for, or
    a way the equality match on ``host`` can be got wrong. The expiry boundary and the
    block-beats-lease case are the two that matter most — the first is the whole
    feature, and the second is the invariant that keeps a timed grant from being a way
    around policy an operator wrote down."""

    def setUp(self):
        cp.store._init_db()
        _set_rules([])
        _set_leases([])

    def test_a_live_lease_allows_a_host_no_rule_matches(self):
        _set_leases([("api.example.com", 300)])
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "allow")

    def test_an_expired_lease_holds_rather_than_allowing(self):
        # The boundary in the direction that matters. A lease one second past its
        # deadline must decide NOTHING — if this passes only because the row was
        # deleted, the sweep has become load-bearing, which is exactly what
        # `_live_lease` filtering on the deadline exists to prevent.
        _set_leases([("api.example.com", -1)])
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "hold")

    def test_an_expired_lease_is_still_in_the_table(self):
        # States the above properly: the row is present and inert, not absent. Written
        # as its own test because a future sweeper that deleted eagerly would make the
        # previous test pass for the wrong reason and nothing would say so.
        _set_leases([("api.example.com", -1)])
        cp.policy._decide("api.example.com", CLASS)
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 1)

    def test_a_block_beats_a_live_lease(self):
        # THE invariant. The block pass runs first, so a lease cannot reach a host an
        # operator has blocked — reachable in practice only by blocking a host while a
        # lease for it is already live.
        _set_rules([("api.example.com", "block")])
        _set_leases([("api.example.com", 300)])
        decision, reason = cp.policy._decide("api.example.com", CLASS)
        self.assertEqual(decision, "deny")
        self.assertIn("blocked by rule", reason)

    def test_a_wildcard_block_beats_a_lease_under_it(self):
        # The same property where the block does not name the leased host, which is the
        # shape a lease could plausibly slip past: the block matches by pattern and the
        # lease by equality, so nothing about the two strings is comparable.
        _set_rules([(".example.com", "block")])
        _set_leases([("api.example.com", 300)])
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "deny")

    def test_a_standing_allow_is_the_reason_when_both_match(self):
        # Both grant, so the DECISION cannot differ; what the ordering settles is which
        # one the audit trail records, and the standing rule is the more useful answer
        # to "why was this allowed".
        _set_rules([("api.example.com", "allow")])
        _set_leases([("api.example.com", 300)])
        decision, reason = cp.policy._decide("api.example.com", CLASS)
        self.assertEqual(decision, "allow")
        self.assertIn("allowed by rule", reason)

    def test_the_reason_names_the_remaining_time(self):
        # In the reason because the alternative is a trail showing one human approval
        # followed by traffic with no recorded cause.
        _set_leases([("api.example.com", 125)])
        reason = cp.policy._decide("api.example.com", CLASS)[1]
        self.assertIn("allowed by lease", reason)
        self.assertIn("api.example.com", reason)
        self.assertIn(CLASS, reason)
        # 2m04s or 2m05s depending on where the clock fell between the two calls.
        self.assertRegex(reason, r"2m0[45]s left")

    def test_a_lease_decides_only_for_its_own_class(self):
        _set_leases([("api.example.com", 300, "sandbox")])
        self.assertEqual(cp.policy._decide("api.example.com", "sandbox")[0], "allow")
        self.assertEqual(cp.policy._decide("api.example.com", "mcp")[0], "hold")

    def test_a_lease_does_not_cover_a_subdomain(self):
        # The exact-host property, and the reason there is no breadth ladder on the
        # lease path: `_match`'s leading-dot wildcard is never applied to this column,
        # so a lease for a host grants that host and nothing under or over it.
        _set_leases([("example.com", 300)])
        self.assertEqual(cp.policy._decide("example.com", CLASS)[0], "allow")
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "hold")

    def test_a_leading_dot_lease_is_not_read_as_a_wildcard(self):
        # A host is agent-chosen, so `.example.com` can arrive as one. It is stored and
        # matched verbatim: it grants the odd literal name it is and NOT the subtree a
        # rule with the same spelling would.
        _set_leases([(".example.com", 300)])
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "hold")
        self.assertEqual(cp.policy._decide("example.com", CLASS)[0], "hold")

    def test_the_host_match_is_case_and_fqdn_dot_insensitive(self):
        # `_normalize_host` is what makes this true on both sides. Without it a lease
        # stored from `API.Example.com.` would be a grant no request could ever equal.
        _set_leases([(cp.policy._normalize_host("API.Example.com."), 300)])
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "allow")
        self.assertEqual(cp.policy._decide("API.EXAMPLE.COM.", CLASS)[0], "allow")

    def test_extracting_the_normalizer_did_not_loosen_the_matcher(self):
        # `_decide` used to inline `(host or "").lower().rstrip(".")`; the lease path
        # needs the identical shape, so it became `_normalize_host`. This asserts the
        # extraction changed NOTHING — in particular that no `strip()` came along,
        # which would make a padded host match an allow rule it currently misses.
        _set_rules([("example.com", "allow"), ("evil.com", "block")])
        for host in (" example.com", "example.com ", "\texample.com"):
            self.assertEqual(cp.policy._decide(host, CLASS)[0], "hold", host)
        # And the fail-safe direction on a block: still held, never allowed.
        self.assertEqual(cp.policy._decide(" evil.com", CLASS)[0], "hold")
        # What it DOES normalize, unchanged: case and the trailing FQDN dot.
        self.assertEqual(cp.policy._decide("EXAMPLE.com.", CLASS)[0], "allow")

    def test_a_padded_lease_host_still_matches_itself(self):
        # The consequence of leaving whitespace alone, stated rather than left to be
        # discovered: a lease is stored in the same shape `_decide` computes, so even
        # the odd padded host equals itself. Unreachable in practice — mitmproxy will
        # not hand over a CONNECT authority with a space in it — and defined anyway,
        # because "matched by equality" is only safe if both sides agree exactly.
        _set_leases([(" api.example.com", 300)])
        self.assertEqual(cp.policy._decide(" api.example.com", CLASS)[0], "allow")
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "hold")

    def test_the_longest_lived_lease_is_the_one_reported(self):
        # Unreachable through the API (a live lease decides the request, so no card is
        # raised to grant a second from), which is why the answer is DEFINED here rather
        # than left to whichever row SQLite returned first.
        _set_leases([("api.example.com", 60), ("api.example.com", 900)])
        reason = cp.policy._decide("api.example.com", CLASS)[1]
        self.assertRegex(reason, r"1[45]m\d\ds left")


class ClientClassDecisionTests(unittest.TestCase):
    """A rule decides for ONE client population. This is the least-privilege property
    the whole column exists for: before it, every host an operator ever approved for
    the agent was reachable by every container the egress proxy served — and since
    mcp-net that includes third-party server images holding a credential the sandbox
    must not have."""

    def setUp(self):
        cp.store._init_db()
        _set_rules([])

    def test_an_allow_for_one_class_does_not_decide_for_another(self):
        _set_rules([("api.github.com", "allow", "sandbox")])
        self.assertEqual(cp.policy._decide("api.github.com", "sandbox")[0], "allow")
        self.assertEqual(cp.policy._decide("api.github.com", "mcp")[0], "hold")

    def test_a_block_for_one_class_does_not_deny_another(self):
        # The same isolation in the other direction. Stated separately because a
        # matcher that filtered only the allow pass would still pass the test above
        # while letting one class's block silently deny every other client.
        _set_rules([("evil.com", "block", "sandbox")])
        self.assertEqual(cp.policy._decide("evil.com", "sandbox")[0], "deny")
        self.assertEqual(cp.policy._decide("evil.com", "mcp")[0], "hold")

    def test_block_wins_over_allow_only_within_a_class(self):
        # A block written for `mcp` must not reach into the agent's decision, even
        # though block-wins-over-allow is the strongest precedence rule here. Filtering
        # AFTER the block pass instead of before would fail exactly this.
        _set_rules([(".example.com", "allow", "sandbox"),
                    ("evil.example.com", "block", "mcp")])
        self.assertEqual(cp.policy._decide("evil.example.com", "sandbox")[0], "allow")
        self.assertEqual(cp.policy._decide("evil.example.com", "mcp")[0], "deny")

    def test_the_same_pattern_can_be_allowed_and_blocked_in_different_classes(self):
        # The case UNIQUE(pattern) made unstorable, which is why the migration rebuilds
        # the table rather than adding a column: one host the agent may reach and an
        # MCP server may not is an ordinary policy, not a contradiction.
        _set_rules([("pypi.org", "allow", "sandbox"), ("pypi.org", "block", "mcp")])
        self.assertEqual(cp.policy._decide("pypi.org", "sandbox")[0], "allow")
        self.assertEqual(cp.policy._decide("pypi.org", "mcp")[0], "deny")

    def test_an_unclassified_client_matches_nothing_and_is_held(self):
        _set_rules([("example.com", "allow", "sandbox")])
        decision, _ = cp.policy._decide("example.com", cp.policy.UNCLASSIFIED)
        self.assertEqual(decision, "hold")

    def test_the_hold_reason_names_the_classes_that_do_match(self):
        # "I already approved this host, why am I being asked again?" — the answer is
        # that the rule belongs to another client population, and only the reason line
        # can carry it. Without this the two failure modes (host unknown vs host known
        # to someone else) are one indistinguishable hold.
        _set_rules([("api.github.com", "allow", "sandbox")])
        _, reason = cp.policy._decide("api.github.com", "mcp")
        self.assertIn("mcp", reason)
        self.assertIn("matched only for: sandbox", reason)
        # A genuinely unknown host says no such thing — there is nothing to name.
        _, reason = cp.policy._decide("unheard-of.example", "mcp")
        self.assertNotIn("matched only for", reason)

    def test_the_deciding_rules_class_is_named_in_the_reason(self):
        _set_rules([("example.com", "allow", "sandbox")])
        self.assertIn("for sandbox", cp.policy._decide("example.com", "sandbox")[1])


class ClientClassMappingTests(unittest.TestCase):
    """Peer address -> class. Everything unplaceable lands on UNCLASSIFIED, which
    matches no rule and is therefore held: a caller that cannot be identified gets
    governed, not exempted."""

    def _classify(self, client, spec="sandbox=172.30.0.0/24,mcp=172.28.0.0/24"):
        saved = cp.policy.CLIENT_CLASSES
        cp.policy.CLIENT_CLASSES = cp.policy._parse_client_classes(spec)
        try:
            return cp.policy._client_class(client)
        finally:
            cp.policy.CLIENT_CLASSES = saved

    def test_an_address_maps_to_its_networks_class(self):
        self.assertEqual(self._classify("172.30.0.2"), "sandbox")
        self.assertEqual(self._classify("172.28.0.3"), "mcp")

    def test_an_address_outside_every_range_is_unclassified(self):
        self.assertEqual(self._classify("10.1.2.3"), cp.policy.UNCLASSIFIED)

    def test_a_missing_or_malformed_address_is_unclassified(self):
        for value in (None, "", "not-an-ip", "agent-1", "172.30.0.999"):
            self.assertEqual(self._classify(value), cp.policy.UNCLASSIFIED, value)

    def test_a_bracketed_v6_literal_is_unwrapped_like_the_proxy_does(self):
        self.assertEqual(self._classify("[fd00::2]", "mcp=fd00::/8"), "mcp")

    def test_containment_does_not_cross_address_families(self):
        # A v4 client must not fall into a v6 range or the reverse — which would be a
        # silent mis-scoping rather than an error.
        self.assertEqual(self._classify("172.30.0.2", "mcp=fd00::/8"),
                         cp.policy.UNCLASSIFIED)

    def test_one_class_may_span_several_ranges(self):
        spec = "sandbox=172.30.0.0/24,sandbox=10.9.0.0/16"
        self.assertEqual(self._classify("10.9.4.5", spec), "sandbox")

    def test_the_first_listed_match_wins(self):
        spec = "first=10.0.0.0/8,second=10.0.0.0/8"
        self.assertEqual(self._classify("10.1.1.1", spec), "first")

    def test_an_unparseable_entry_is_dropped_not_fatal(self):
        # Its clients fall through to UNCLASSIFIED and are HELD, so a typo in the
        # config costs approvals rather than granting any.
        spec = "sandbox=not-a-cidr,mcp=172.28.0.0/24"
        self.assertEqual(self._classify("172.30.0.2", spec), cp.policy.UNCLASSIFIED)
        self.assertEqual(self._classify("172.28.0.2", spec), "mcp")

    def test_unclassified_cannot_be_claimed_as_a_class_name(self):
        # Otherwise a config could name a real network `unclassified` and rules
        # written for genuinely-unplaceable clients would start deciding for it.
        spec = f"{cp.policy.UNCLASSIFIED}=10.0.0.0/8"
        self.assertEqual(self._classify("10.1.1.1", spec), cp.policy.UNCLASSIFIED)
        self.assertEqual(cp.policy._parse_client_classes(spec), ())


class MatchTests(unittest.TestCase):
    def test_match_semantics(self):
        self.assertTrue(cp.policy._match("example.com", "example.com"))
        self.assertFalse(cp.policy._match("a.example.com", "example.com"))
        self.assertTrue(cp.policy._match("a.example.com", ".example.com"))
        self.assertTrue(cp.policy._match("example.com", ".example.com"))
        self.assertFalse(cp.policy._match("notexample.com", ".example.com"))


class _HoldRegistryTestCase(unittest.TestCase):
    """Shared fixture for the in-memory hold registry: save the caps, wipe every
    registry between tests. Listed explicitly rather than looped over a collection,
    so a NEW registry that this fixture forgets to clear shows up as a test that
    leaks state rather than as a name in a list nobody reads."""

    #: The four cap names, so save/restore cannot drift from the module as it grows.
    CAPS: ClassVar[tuple] = ("MAX_PENDING", "MAX_PENDING_PER_CLIENT",
                             "MAX_WAITERS", "MAX_WAITERS_PER_CLIENT")

    def setUp(self):
        self._saved = {name: getattr(cp.holds, name) for name in self.CAPS}
        self._wipe()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(cp.holds, name, value)
        self._wipe()

    def _caps(self, *, cards=100, cards_per_client=0,
              waiters=100, waiters_per_client=0):
        """Set ALL FOUR caps, so a test states the ones it is about and neutralizes the
        rest. Keyword-only and fully specified rather than a two-tuple assignment,
        because the failure it prevents actually happened: adding the waiter caps made
        several tests that read as being about the CARD cap start failing on a waiter
        cap they never mentioned. A test refused by a limit it does not name is a test
        whose subject has silently changed."""
        cp.holds.MAX_PENDING = cards
        cp.holds.MAX_PENDING_PER_CLIENT = cards_per_client
        cp.holds.MAX_WAITERS = waiters
        cp.holds.MAX_WAITERS_PER_CLIENT = waiters_per_client

    def _wipe(self):
        cp.holds._PENDING_EVENTS.clear()
        cp.holds._PENDING_CLIENT.clear()
        cp.holds._PENDING_WAITERS.clear()
        cp.holds._PENDING_DEADLINE.clear()
        cp.holds._GROUPS.clear()


class _ToolAskTestCase(unittest.TestCase):
    """Fixture for tool asks. Deliberately NOT ``_HoldRegistryTestCase``: there is no
    in-memory registry to wipe, because nothing blocks on a tool ask and the row is
    the whole of it. What does need resetting is the table, the two caps, the window,
    and the saturation account the two surfaces share."""

    CAPS: ClassVar[tuple] = ("MAX_TOOL_PENDING", "MAX_TOOL_PENDING_PER_CLIENT",
                             "TOOL_HOLD_TIMEOUT", "TOOL_GRANT_TIMEOUT",
                             "TOOL_ARGS_MAX")

    def setUp(self):
        cp.store._init_db()
        self._saved = {name: getattr(cp.holds, name) for name in self.CAPS}
        self._wipe()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(cp.holds, name, value)
        self._wipe()

    def _wipe(self):
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM tool_approvals")
            conn.commit()
        cp.holds._SATURATION.update(count=0, last_ts=None, last_scope=None,
                                    last_host=None, acked=0, acked_ts=None)

    @staticmethod
    def _rows(status="pending"):
        with cp.store._connect() as conn:
            return conn.execute("SELECT * FROM tool_approvals WHERE status=?",
                                (status,)).fetchall()


class ToolAskRegistrationTests(_ToolAskTestCase):
    """Registering an ask, and the joining that keeps a retry from raising a second
    card. The egress analogue is ``_reserve_hold``; almost nothing is shared, because
    a tool ask blocks no worker."""

    def test_an_ask_is_a_row_and_nothing_in_memory(self):
        # The structural claim of the whole split: an ask pins no worker, holds no
        # Event and occupies no slot in the registries that exist to protect the
        # /authorize path. If any of these grew an entry, a queue of asks could starve
        # egress decisions — the exact coupling answering immediately removed.
        ask = cp.holds._register_tool_ask("mcp-github", "create_pull_request",
                                          {"title": "x"}, client="172.30.0.2")
        self.assertIsNotNone(ask.approval_id)
        self.assertFalse(ask.joined)
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(cp.holds._PENDING_EVENTS, {})
        self.assertEqual(cp.holds._PENDING_WAITERS, {})
        self.assertEqual(cp.holds._GROUPS, {})

    def test_an_identical_retry_joins_instead_of_raising_a_second_card(self):
        first = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1},
                                            client="172.30.0.2")
        again = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1},
                                            client="172.30.0.2")
        self.assertTrue(again.joined)
        self.assertEqual(again.approval_id, first.approval_id)
        self.assertEqual(len(self._rows()), 1)

    def test_key_order_is_the_same_ask(self):
        # A model asked to retry commonly reformulates, and a reordered object is the
        # same question. Joining it is what keeps ordinary model behaviour from
        # flooding the human.
        first = cp.holds._register_tool_ask("mcp-github", "issue_write",
                                            {"a": 1, "b": 2})
        again = cp.holds._register_tool_ask("mcp-github", "issue_write",
                                            {"b": 2, "a": 1})
        self.assertTrue(again.joined)
        self.assertEqual(again.approval_id, first.approval_id)

    def test_a_changed_value_is_a_different_ask(self):
        # The half that carries the security weight: an approval given for one set of
        # arguments must never be inherited by another.
        first = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1})
        other = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 2})
        self.assertFalse(other.joined)
        self.assertNotEqual(other.approval_id, first.approval_id)
        self.assertEqual(len(self._rows()), 2)

    def test_the_same_payload_on_another_tool_or_server_does_not_join(self):
        # Server and tool are inside the digest, so a payload approved for one cannot
        # be replayed against another.
        first = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1})
        for server, tool in (("mcp-github", "sub_issue_write"),
                             ("mcp-other", "issue_write")):
            ask = cp.holds._register_tool_ask(server, tool, {"n": 1})
            self.assertFalse(ask.joined, (server, tool))
            self.assertNotEqual(ask.approval_id, first.approval_id)

    def test_another_client_does_not_join(self):
        # A card names one caller. Answering one sandbox's question must not silently
        # answer another's — the reason ``client`` is in ``_group_key`` too.
        first = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1},
                                            client="172.30.0.2")
        other = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1},
                                            client="172.30.0.9")
        self.assertFalse(other.joined)
        self.assertNotEqual(other.approval_id, first.approval_id)

    def test_a_decided_ask_is_not_joinable(self):
        # The tool-side equivalent of closing a group at the decision: a retry
        # arriving after a human answered opens a fresh card rather than inheriting an
        # outcome it was never shown alongside.
        first = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1})
        cp.holds._resolve_tool_ask(first.approval_id, "allowed", "test")
        again = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1})
        self.assertFalse(again.joined)
        self.assertNotEqual(again.approval_id, first.approval_id)


class ToolAskCapTests(_ToolAskTestCase):
    """Two caps, both about ATTENTION — there is no worker to protect here. What they
    bound is the loop hash-joining cannot: an agent opening FRESH asks with slightly
    varied payloads, which floods a human without ever repeating itself."""

    def _ask(self, n, client="172.30.0.2"):
        return cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": n},
                                           client=client)

    def test_over_the_global_cap_fails_closed(self):
        cp.holds.MAX_TOOL_PENDING = 2
        cp.holds.MAX_TOOL_PENDING_PER_CLIENT = 0
        self._ask(1), self._ask(2)
        refused = self._ask(3)
        self.assertIsNone(refused.approval_id)
        self.assertIn("global tool asks", refused.refused)
        self.assertEqual(len(self._rows()), 2)

    def test_over_the_per_client_cap_fails_closed_for_that_client_alone(self):
        cp.holds.MAX_TOOL_PENDING = 100
        cp.holds.MAX_TOOL_PENDING_PER_CLIENT = 2
        self._ask(1), self._ask(2)
        self.assertIsNone(self._ask(3).approval_id)
        # Another sandbox is unaffected, which is the whole point of the scope.
        self.assertIsNotNone(self._ask(3, client="172.30.0.9").approval_id)

    def test_a_zero_per_client_cap_disables_it(self):
        # Same asymmetry the egress caps have: fail-closed on a global cap, disabled
        # on a per-client one, because the fail-closed reading would make every
        # client's first ask impossible.
        cp.holds.MAX_TOOL_PENDING = 100
        cp.holds.MAX_TOOL_PENDING_PER_CLIENT = 0
        for n in range(6):
            self.assertIsNotNone(self._ask(n).approval_id)

    def test_a_refusal_reaches_the_one_saturation_account(self):
        # Unsplit deliberately: over the cap nothing raises a card, so the refusal is
        # invisible in the queue — and an operator should not have to read two banners
        # to learn that governance is refusing things.
        cp.holds.MAX_TOOL_PENDING = 1
        self._ask(1)
        self._ask(2)
        self.assertEqual(cp.holds._saturation()["rejections"], 1)
        self.assertIn("tool asks", cp.holds._saturation()["last_scope"])

    def test_a_joiner_is_never_refused_for_capacity_it_does_not_use(self):
        # The join sits BELOW the caps here and above them in ``_reserve_hold``, and
        # the inversion is deliberate: an egress joiner still pins a worker, while a
        # tool joiner costs no row, no card and no attention. Refusing it would deny a
        # call whose question is already on the screen.
        cp.holds.MAX_TOOL_PENDING = 1
        first = self._ask(1)
        joined = self._ask(1)
        self.assertTrue(joined.joined)
        self.assertEqual(joined.approval_id, first.approval_id)

    def test_an_oversized_payload_is_refused_but_is_not_saturation(self):
        # Refused rather than truncated, because the payload is what a human reads to
        # decide. And NOT counted as pressure: nothing was contended for, so a banner
        # saying governance is loaded would be reporting the wrong event.
        cp.holds.TOOL_ARGS_MAX = 64
        refused = cp.holds._register_tool_ask("mcp-github", "issue_write",
                                              {"body": "x" * 200})
        self.assertIsNone(refused.approval_id)
        self.assertIn("ceiling", refused.refused)
        self.assertEqual(self._rows(), [])
        self.assertEqual(cp.holds._saturation()["rejections"], 0)


class ToolAskLifecycleTests(_ToolAskTestCase):
    """Decide, expire, claim. Four terminal states that must stay distinguishable:
    an agent that cannot tell a denial from an expiry retries a refusal forever, and
    one that cannot tell either from a spent approval re-runs a side effect."""

    def _ask(self, **kw):
        return cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1},
                                           **kw)

    def test_a_decision_is_recorded_once(self):
        ask = self._ask()
        self.assertEqual(
            cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator"),
            "allowed")
        # A second click changes no row and reads back as the non-transition it was,
        # rather than overwriting a decision already made.
        self.assertIsNone(
            cp.holds._resolve_tool_ask(ask.approval_id, "denied", "operator"))
        self.assertEqual(cp.holds._get_tool_ask(ask.approval_id)["status"], "allowed")

    def test_an_unknown_or_invalid_decision_changes_nothing(self):
        ask = self._ask()
        self.assertIsNone(
            cp.holds._resolve_tool_ask(ask.approval_id, "maybe", "operator"))
        self.assertIsNone(cp.holds._resolve_tool_ask("no-such-id", "allowed", "op"))
        self.assertEqual(cp.holds._get_tool_ask(ask.approval_id)["status"], "pending")

    def test_an_ask_past_its_deadline_expires_on_the_next_read(self):
        # Lazy rather than swept by a timer: nothing is blocked, so there is no waiter
        # whose timeout would enforce the window, and a row can simply be read as
        # expired. It also means a restart cannot leave an ask pending forever.
        cp.holds.TOOL_HOLD_TIMEOUT = -1
        ask = self._ask()
        self.assertEqual(cp.holds._get_tool_ask(ask.approval_id)["status"], "expired")

    def test_expired_is_distinct_from_denied(self):
        # Both refuse the call; only one of them means a human decided. An agent — or
        # someone reading the record a month later — has to be able to tell.
        cp.holds.TOOL_HOLD_TIMEOUT = -1
        expired = self._ask()
        cp.holds.TOOL_HOLD_TIMEOUT = 3600
        denied = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 2})
        cp.holds._resolve_tool_ask(denied.approval_id, "denied", "operator")
        self.assertEqual(cp.holds._get_tool_ask(expired.approval_id)["status"],
                         "expired")
        self.assertEqual(cp.holds._get_tool_ask(denied.approval_id)["status"],
                         "denied")

    def test_an_expired_ask_cannot_be_decided_afterwards(self):
        cp.holds.TOOL_HOLD_TIMEOUT = -1
        ask = self._ask()
        cp.holds._expire_tool_asks()
        self.assertIsNone(
            cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator"))

    def _audit_rows(self, approval_id):
        with cp.store._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT kind, stage, server, tool, client, client_class, reason "
                "FROM audit WHERE approval_id=? ORDER BY id", (approval_id,))]

    def test_an_ask_nobody_answered_leaves_a_deny_row(self):
        # The egress expiry writes one through its released waiter; this one wrote
        # nothing, so the trail could not tell a still-pending ask from a lapsed one.
        cp.holds.TOOL_HOLD_TIMEOUT = -1
        ask = self._ask(client="172.30.0.7")
        self.assertEqual(cp.holds._expire_tool_asks(), 1)
        rows = [r for r in self._audit_rows(ask.approval_id) if r["kind"] == "deny"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["stage"], row["server"], row["tool"], row["client"]),
                         ("tool-ask", "mcp-github", "issue_write", "172.30.0.7"))
        self.assertEqual(row["client_class"], cp.policy._client_class("172.30.0.7"))
        self.assertIn("no decision within the tool hold window", row["reason"])
        self.assertIn("will not run", row["reason"])

    def test_a_grant_nobody_redeemed_leaves_a_deny_row_that_says_so(self):
        # The other window, and the one with more to record: a human clicked allow
        # and the agent never came back. Distinct wording, so the record can tell a
        # question nobody answered from an answer nobody collected.
        ask = self._ask()
        cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator")
        cp.holds.TOOL_GRANT_TIMEOUT = -1
        self.assertEqual(cp.holds._expire_tool_asks(), 1)
        rows = [r for r in self._audit_rows(ask.approval_id) if r["kind"] == "deny"]
        self.assertEqual(len(rows), 1)
        self.assertIn("approved but never resumed", rows[0]["reason"])
        self.assertIn("grant lapsed", rows[0]["reason"])

    def test_expiry_is_audited_once_however_often_the_sweep_runs(self):
        # The sweep runs on every read of the queue — once a second from the SSE
        # tick. A row per sweep would bury the one that matters.
        cp.holds.TOOL_HOLD_TIMEOUT = -1
        ask = self._ask()
        cp.holds._expire_tool_asks()
        cp.holds._expire_tool_asks()
        cp.holds._get_tool_ask(ask.approval_id)
        rows = [r for r in self._audit_rows(ask.approval_id) if r["kind"] == "deny"]
        self.assertEqual(len(rows), 1)

    def test_a_spent_approval_gets_no_expiry_row(self):
        # It ran; nothing lapsed. The row would say the opposite.
        ask = self._ask()
        cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator")
        cp.holds._claim_tool_ask(ask.approval_id)
        cp.holds.TOOL_GRANT_TIMEOUT = -1
        self.assertEqual(cp.holds._expire_tool_asks(), 0)
        self.assertEqual(
            [r for r in self._audit_rows(ask.approval_id) if r["kind"] == "deny"], [])

    def test_an_approval_is_claimable_exactly_once(self):
        # The property that makes lazy execution safe. The gateway runs the call on
        # RESUMPTION, and an agent can resume twice — so without a single-use claim
        # one approval could send the mail twice.
        ask = self._ask()
        cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator")
        claimed = cp.holds._claim_tool_ask(ask.approval_id)
        self.assertIsNotNone(claimed)
        self.assertIsNotNone(claimed["claimed_at"])
        self.assertIsNone(cp.holds._claim_tool_ask(ask.approval_id))

    def test_nothing_undecided_or_refused_is_claimable(self):
        pending = self._ask()
        self.assertIsNone(cp.holds._claim_tool_ask(pending.approval_id))
        denied = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 2})
        cp.holds._resolve_tool_ask(denied.approval_id, "denied", "operator")
        self.assertIsNone(cp.holds._claim_tool_ask(denied.approval_id))
        self.assertIsNone(cp.holds._claim_tool_ask("no-such-id"))

    def test_an_approval_that_expired_before_resumption_is_not_claimable(self):
        # Allowed and never collected, past its window: the human's answer does not
        # keep a side effect live indefinitely.
        ask = self._ask()
        cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator")
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_approvals SET status='expired' WHERE id=?",
                         (ask.approval_id,))
            conn.commit()
        self.assertIsNone(cp.holds._claim_tool_ask(ask.approval_id))

    def test_an_unclaimed_grant_expires_on_its_own_window(self):
        # The half the ask window does not cover: a click nobody redeemed. Without
        # this an approval is a STANDING authorization — redeemable by a session the
        # operator has forgotten, against conditions they would no longer approve.
        cp.holds.TOOL_GRANT_TIMEOUT = -1
        ask = self._ask()
        cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator")
        self.assertEqual(cp.holds._get_tool_ask(ask.approval_id)["status"], "expired")
        self.assertIsNone(cp.holds._claim_tool_ask(ask.approval_id))

    def test_the_grant_window_runs_from_the_decision_not_from_the_ask(self):
        # Why it is a second number rather than a reuse of the ask window: an ask
        # answered a moment before ITS deadline still gets a full window to be
        # collected in. Sharing the field would give it none.
        cp.holds.TOOL_HOLD_TIMEOUT = 0.05
        ask = self._ask()
        cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator")
        time.sleep(0.1)
        self.assertIsNotNone(cp.holds._claim_tool_ask(ask.approval_id))

    def test_a_stale_grant_is_refused_by_the_write_not_only_by_the_sweep(self):
        # Expiry is lazy, so the claim carries the window in its own UPDATE. Asserted
        # against a row the sweep has deliberately not seen: the predicate is what
        # makes "a stale grant cannot be redeemed" a property of the write.
        ask = self._ask()
        cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator")
        with mock.patch.object(cp.holds, "_expire_tool_asks", return_value=0):
            cp.holds.TOOL_GRANT_TIMEOUT = -1
            self.assertIsNone(cp.holds._claim_tool_ask(ask.approval_id))
        self.assertIsNone(
            cp.holds._get_tool_ask(ask.approval_id)["claimed_at"])

    def test_a_spent_approval_is_never_relabelled_expired(self):
        # The distinction a duplicate resumption depends on: `spent` says the call
        # happened, `expired` says it never will. An aged claimed row must keep saying
        # the first, however long it sits there.
        ask = self._ask()
        cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator")
        self.assertIsNotNone(cp.holds._claim_tool_ask(ask.approval_id))
        cp.holds.TOOL_GRANT_TIMEOUT = -1
        cp.holds._expire_tool_asks()
        row = cp.holds._get_tool_ask(ask.approval_id)
        self.assertEqual(row["status"], "allowed")
        self.assertIsNotNone(row["claimed_at"])

    def test_expiring_a_grant_keeps_the_time_the_human_decided(self):
        # ``resolved_at`` is what the grant cutoff is measured FROM, so the pending
        # branch's habit of stamping it must not follow the row into this one:
        # overwriting it would destroy the record and move the deadline it defines.
        ask = self._ask()
        cp.holds._resolve_tool_ask(ask.approval_id, "allowed", "operator")
        decided_at = cp.holds._get_tool_ask(ask.approval_id)["resolved_at"]
        cp.holds.TOOL_GRANT_TIMEOUT = -1
        cp.holds._expire_tool_asks()
        row = cp.holds._get_tool_ask(ask.approval_id)
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["resolved_at"], decided_at)

    def test_an_unknown_id_is_none_rather_than_an_error(self):
        self.assertIsNone(cp.holds._get_tool_ask("no-such-id"))

    def test_the_queue_lists_only_what_is_still_pending(self):
        pending = self._ask()
        decided = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 2})
        cp.holds._resolve_tool_ask(decided.approval_id, "denied", "operator")
        listed = cp.holds._list_tool_asks()
        self.assertEqual([a["id"] for a in listed], [pending.approval_id])
        self.assertEqual(listed[0]["kind"], "tool")

    def test_the_stored_payload_is_what_the_digest_covers(self):
        # One serialization for storage, hashing and display, so "the payload is
        # authoritative" means something: what a human reads is byte-for-byte what the
        # grant is bound to.
        ask = cp.holds._register_tool_ask("mcp-github", "issue_write",
                                          {"b": 2, "a": 1})
        stored = cp.holds._get_tool_ask(ask.approval_id)["args_json"]
        with cp.store._connect() as conn:
            digest = conn.execute(
                "SELECT args_digest FROM tool_approvals WHERE id=?",
                (ask.approval_id,)).fetchone()[0]
        self.assertEqual(stored, '{"a":1,"b":2}')
        self.assertEqual(
            digest, cp.holds._args_digest("mcp-github", "issue_write", stored))


class HoldCapTests(_HoldRegistryTestCase):
    """Four caps, two nouns times two scopes: CARDS protect the operator's attention,
    WAITERS protect the threadpool. Over any of them /authorize must fail CLOSED
    instead of registering another worker-blocking hold.

    These tests reserve a DISTINCT host per approval, so each one is a new card and
    nothing here is grouped. The waiter caps' interesting case is the opposite — a
    joined duplicate, which costs a waiter and no card — and it lives with the rest of
    grouping in ``DuplicateGroupingTests``."""

    def _reserve(self, approval_id, client, host=None):
        # A DISTINCT host per approval unless a test asks otherwise, so these tests
        # exercise the caps and not duplicate grouping: same client + same host is one
        # card by design, and a shared default host would have quietly turned every
        # multi-reserve case below into a single group that no cap ever refuses.
        return cp.holds._reserve_hold(approval_id, threading.Event(), client,
                                host or f"{approval_id}.example.com")

    def test_the_global_card_cap_fails_closed_over_limit(self):
        self._caps(cards=2)
        self.assertIsNone(self._reserve("a", "c1").refused)
        self.assertIsNone(self._reserve("b", "c2").refused)
        reason = self._reserve("c", "c3").refused
        self.assertIsNotNone(reason)
        # The scope names the NOUN as well as the scope, because four caps can refuse
        # and the operator's response differs: too many questions on screen is an
        # attention problem, too many blocked workers is a capacity one. Asserted both
        # ways, so the right cap firing is distinguished from any cap firing.
        self.assertIn("global cards", reason)
        self.assertNotIn("waiters", reason)

    def test_the_global_waiter_cap_fails_closed_over_limit(self):
        # Distinct hosts, so every one of these is also a card — the point being that
        # with the card cap out of the way it is the waiter cap that refuses.
        self._caps(waiters=2)
        self.assertIsNone(self._reserve("a", "c1").refused)
        self.assertIsNone(self._reserve("b", "c2").refused)
        reason = self._reserve("c", "c3").refused
        self.assertIsNotNone(reason)
        self.assertIn("global waiters", reason)

    def test_a_card_cap_at_or_above_its_waiter_cap_is_dead(self):
        """Not a rule the code enforces — a property of the two counts that the shipped
        DEFAULTS are chosen to avoid, and that ``_warn_on_dead_caps`` reports at boot for
        the hand-set values this test cannot see. Cards are always <= waiters, so a card
        cap set equal to its waiter cap can never be the first to refuse. Asserted so that
        raising `CONTROL_MAX_WAITERS` alone, and thereby silencing the card cap without
        meaning to, is a visible fact rather than a discovery."""
        self._caps(cards=3, waiters=3)
        for i in range(3):
            self.assertIsNone(self._reserve(f"d{i}", f"c{i}").refused)
        self.assertIn("global waiters", self._reserve("d3", "c3").refused)
        # And the SHIPPED defaults (saved by the fixture before `_caps` overwrote them)
        # keep both alive, on each axis.
        self.assertLess(self._saved["MAX_PENDING"], self._saved["MAX_WAITERS"])
        self.assertLess(self._saved["MAX_PENDING_PER_CLIENT"],
                        self._saved["MAX_WAITERS_PER_CLIENT"])

    def test_per_client_cap_isolates_clients(self):
        self._caps(cards_per_client=2)
        self.assertIsNone(self._reserve("a1", "A").refused)
        self.assertIsNone(self._reserve("a2", "A").refused)
        over = self._reserve("a3", "A").refused
        self.assertIsNotNone(over)
        self.assertIn("client A", over)
        # A different client is unaffected by A's saturation.
        self.assertIsNone(self._reserve("b1", "B").refused)

    def test_per_client_cap_disabled_with_zero(self):
        self._caps()
        for i in range(10):
            self.assertIsNone(self._reserve(f"x{i}", "same-client").refused)

    def test_none_client_bypasses_per_client_cap_but_not_global(self):
        self._caps(cards=3, cards_per_client=1)
        # client=None never counts against the per-client cap...
        self.assertIsNone(self._reserve("n1", None).refused)
        self.assertIsNone(self._reserve("n2", None).refused)
        self.assertIsNone(self._reserve("n3", None).refused)
        # ...but still hits the global cap.
        self.assertIsNotNone(self._reserve("n4", None).refused)

    def test_release_frees_a_global_slot(self):
        self._caps(cards=1)
        self.assertIsNone(self._reserve("a", "c1").refused)
        self.assertIsNotNone(self._reserve("b", "c2").refused)  # full
        cp.holds._release_hold("a")
        self.assertIsNone(self._reserve("b", "c2").refused)  # slot freed

    def test_release_forgets_the_slot(self):
        self._caps(cards=5)
        self._reserve("a", "c1")
        cp.holds._release_hold("a")
        # Released ids are fully forgotten from every registry.
        self.assertNotIn("a", cp.holds._PENDING_EVENTS)
        self.assertNotIn("a", cp.holds._PENDING_CLIENT)
        self.assertNotIn("a", cp.holds._PENDING_WAITERS)
        self.assertNotIn("a", cp.holds._PENDING_DEADLINE)
        self.assertNotIn("a", cp.holds._GROUPS.values())
        # Releasing an already-released id is a harmless no-op.
        cp.holds._release_hold("a")


class DeadCapWarningTests(_HoldRegistryTestCase):
    """``_warn_on_dead_caps`` reports a card cap that can never be the one to refuse.

    The cap ordering was asserted only for the SHIPPED defaults, so an operator raising
    a waiter cap on its own silenced the matching card cap with nothing anywhere saying
    so. The boot line is where that becomes visible, because it is the moment the values
    are read (see "Hold bounds are fail-closed, so their values stay env vars" in
    DESIGN.md for why they are read from the environment at all).

    Asserted on the WARNING's presence and on which scope it names, never on its
    wording."""

    def _warnings(self) -> list[str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cp._warn_on_dead_caps()
        return [ln for ln in buf.getvalue().splitlines() if "WARNING" in ln]

    def test_a_global_card_cap_at_its_waiter_cap_warns(self):
        self._caps(cards=8, waiters=8)
        warnings = self._warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("global", warnings[0])

    def test_a_per_client_card_cap_above_its_waiter_cap_warns(self):
        self._caps(cards=4, waiters=16, cards_per_client=9, waiters_per_client=8)
        warnings = self._warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("per-client", warnings[0])

    def test_the_shipped_defaults_are_silent(self):
        """The fixture saved them before any test overwrote them, so this asserts the
        real deployed configuration and not a value this file chose."""
        for name, value in self._saved.items():
            setattr(cp.holds, name, value)
        self.assertEqual(self._warnings(), [])

    def test_a_zero_waiter_cap_is_not_a_dead_card_cap(self):
        """Zero DISABLES the per-client waiter cap, which leaves the per-client card cap
        as the only bound of its scope — the most alive it ever is. Warning here would
        report a documented setting as a mistake."""
        self._caps(cards=4, waiters=16, cards_per_client=4, waiters_per_client=0)
        self.assertEqual(self._warnings(), [])

    def test_a_zero_card_cap_is_not_warned_about(self):
        """A global zero refuses every hold outright — fail-closed, and a documented way
        to say "stop holding anything". It is not a cap that failed to bind."""
        self._caps(cards=0, waiters=16, cards_per_client=0, waiters_per_client=8)
        self.assertEqual(self._warnings(), [])


class DuplicateGroupingTests(_HoldRegistryTestCase):
    """A retrying agent asks the same question repeatedly. Those requests share ONE
    card and one decision; they do not share a worker, and they never share an audit
    line."""

    def _reserve(self, approval_id, client="c1", host="example.com",
                 port=443, proto="https"):
        return cp.holds._reserve_hold(approval_id, threading.Event(), client,
                                host, port, proto)

    def test_an_identical_request_joins_the_existing_card(self):
        self._caps(cards_per_client=4)
        first = self._reserve("a")
        self.assertFalse(first.joined)
        second = self._reserve("b")
        self.assertTrue(second.joined)
        # The joiner is told the FIRST card's id and event — "b" never becomes a card.
        self.assertEqual(second.approval_id, "a")
        self.assertIs(second.event, first.event)
        self.assertEqual(list(cp.holds._PENDING_EVENTS), ["a"])
        self.assertNotIn("b", cp.holds._PENDING_EVENTS)

    def test_grouping_is_what_keeps_a_retry_storm_under_the_card_cap(self):
        # The motivating case, as a whole: with a per-client cap of 4, a fifth retry
        # used to be refused outright. It now joins, and the operator sees one card.
        self._caps(cards_per_client=4)
        for i in range(20):
            self.assertIsNone(self._reserve(f"r{i}").refused)
        self.assertEqual(len(cp.holds._PENDING_EVENTS), 1)
        self.assertEqual(cp.holds._PENDING_WAITERS["r0"], 20)

    def test_a_joined_waiter_still_costs_a_global_slot(self):
        # The global cap bounds BLOCKED WORKERS, and a joiner blocks one. If grouping
        # were free here it would be a route around the cap that protects governance
        # for every other sandbox — the one thing it must not become.
        self._caps(waiters=3)
        for i in range(3):
            self.assertIsNone(self._reserve(f"j{i}").refused)
        over = self._reserve("j3")
        self.assertIsNotNone(over.refused)
        self.assertIn("global waiters", over.refused)

    def test_a_joined_waiter_also_costs_a_PER_CLIENT_slot(self):
        """The defect this cap was added for, stated as behaviour.

        Grouping is what decoupled cards from waiters, and the per-client cap kept
        counting cards — so a joiner met no per-client bound at all. The whole of the
        bug is that this test used to be impossible to write: every one of these
        twenty reservations is the SAME request, so it costs one card and twenty
        workers, and only a per-client WAITER cap can see it."""
        self._caps(waiters_per_client=3)
        for i in range(3):
            self.assertIsNone(self._reserve(f"j{i}").refused)
        over = self._reserve("j3")
        self.assertIn("client c1 waiters", over.refused)
        # One card the whole time — which is why the card caps could never have caught
        # this, at any setting.
        self.assertEqual(len(cp.holds._PENDING_EVENTS), 1)

    def test_one_client_storming_cannot_starve_another(self):
        """The consequence, and the reason this was a finding rather than a nuisance:
        the control plane is shared across every sandbox, so one agent retrying one
        host used to fill the global pool from a single card and every other sandbox
        was refused until those holds drained."""
        self._caps(waiters=4, waiters_per_client=2)
        self.assertIsNone(self._reserve("s0", client="storm").refused)
        self.assertIsNone(self._reserve("s1", client="storm").refused)
        self.assertIsNotNone(self._reserve("s2", client="storm").refused)
        # The victim still gets in, which is the entire point.
        self.assertIsNone(self._reserve("v0", client="victim").refused)

    def test_releasing_a_joined_waiter_frees_its_per_client_slot(self):
        # The cap counts live waiters, so it has to fall as they wake — otherwise a
        # client is locked out for the process lifetime by a storm that has drained.
        self._caps(waiters_per_client=2)
        self._reserve("k0")
        self._reserve("k1")
        self.assertIsNotNone(self._reserve("k2").refused)
        cp.holds._release_hold("k0")            # one waiter wakes; the card survives
        self.assertIsNone(self._reserve("k3").refused)

    def test_a_different_client_gets_its_own_card(self):
        # The decision is a function of the host alone, but approving one sandbox's
        # request must never release another's.
        self._caps(cards_per_client=4)
        self.assertFalse(self._reserve("a", client="A").joined)
        self.assertFalse(self._reserve("b", client="B").joined)
        self.assertEqual(len(cp.holds._PENDING_EVENTS), 2)

    def test_port_and_proto_split_a_card_but_method_and_url_do_not(self):
        self._caps()
        self.assertFalse(self._reserve("a", port=443).joined)
        self.assertFalse(self._reserve("b", port=80).joined)
        self.assertFalse(self._reserve("c", proto="http", port=443).joined)
        # ...and method/url are not even arguments: retries vary them (cache-busters,
        # query strings), which is exactly why keying on them would defeat grouping in
        # the case it exists for. Same host/port/proto/client is the same card.
        self.assertTrue(self._reserve("d", port=443).joined)
        self.assertEqual(len(cp.holds._PENDING_EVENTS), 3)

    def test_the_host_key_is_case_insensitive_like_the_matcher(self):
        self._caps()
        self.assertFalse(self._reserve("a", host="Example.COM").joined)
        self.assertTrue(self._reserve("b", host="example.com").joined)

    def test_a_closed_group_is_not_joinable_but_its_waiters_survive(self):
        # What makes "one click decides what it showed": the moment a card is decided
        # it stops accepting joiners, while the workers already on it stay registered
        # so resolve() can still find the event and the global cap still counts them.
        self._caps()
        self._reserve("a")
        self._reserve("b")
        cp.holds._close_group("a")
        self.assertIn("a", cp.holds._PENDING_EVENTS)
        self.assertEqual(cp.holds._PENDING_WAITERS["a"], 2)
        after = self._reserve("c")
        self.assertFalse(after.joined)
        self.assertEqual(after.approval_id, "c")

    def test_the_card_frees_only_when_the_last_waiter_leaves(self):
        self._caps()
        self._reserve("a")
        self._reserve("b")
        self._reserve("c")
        cp.holds._release_hold("a")
        self.assertEqual(cp.holds._PENDING_WAITERS["a"], 2)
        self.assertIn("a", cp.holds._PENDING_EVENTS)
        cp.holds._release_hold("a")
        cp.holds._release_hold("a")
        self.assertNotIn("a", cp.holds._PENDING_EVENTS)
        self.assertNotIn("a", cp.holds._PENDING_WAITERS)
        self.assertEqual(cp.holds._GROUPS, {})

    def test_a_joiner_inherits_the_cards_deadline_rather_than_extending_it(self):
        # Otherwise an agent retrying on a loop pushes the deadline out forever and the
        # countdown on the card is a lie.
        self._caps()
        first = self._reserve("a")
        self.assertEqual(self._reserve("b").deadline, first.deadline)
        self.assertAlmostEqual(first.deadline - time.time(), cp.holds.HOLD_TIMEOUT, delta=5)

    def test_a_refusal_reserves_nothing(self):
        self._caps(cards=1)
        self._reserve("a", host="one.example")
        over = self._reserve("b", host="two.example")
        self.assertIsNotNone(over.refused)
        self.assertIsNone(over.approval_id)
        self.assertIsNone(over.event)
        self.assertNotIn("b", cp.holds._PENDING_EVENTS)
        self.assertNotIn("b", cp.holds._PENDING_WAITERS)
        self.assertEqual(sum(cp.holds._PENDING_WAITERS.values()), 1)


if __name__ == "__main__":
    unittest.main()
