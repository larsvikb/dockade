# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the control plane's security-load-bearing logic
(``control-plane/app.py``): the policy decision ``_decide`` (block-wins-over-allow,
subdomain semantics, default-hold, and per-client-class scoping), the address ->
class mapping that feeds it, and the hold registry (``_reserve_hold`` /
``_release_hold``: the two caps, and duplicate grouping).

The hold cap is exactly what ``boundary-check.sh`` cannot assert: an over-cap
request returns the same opaque 403 to the agent as any other deny, so the cap's
concurrency/availability behavior is invisible from the sandbox's vantage point
(see DESIGN.md). Here we test the reservation function directly.

Dependency-free: ``fastapi``/``pydantic`` are stubbed (see ``tests/_loader.py``),
and the store is a throwaway SQLite file in a temp dir set before import."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest

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

    def setUp(self):
        self._saved = (cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT)
        self._wipe()

    def tearDown(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = self._saved
        self._wipe()

    def _wipe(self):
        cp.holds._PENDING_EVENTS.clear()
        cp.holds._PENDING_CLIENT.clear()
        cp.holds._PENDING_WAITERS.clear()
        cp.holds._PENDING_DEADLINE.clear()
        cp.holds._GROUPS.clear()


class HoldCapTests(_HoldRegistryTestCase):
    """The hold cap protects a shared threadpool: over cap, /authorize must fail
    CLOSED instead of registering another worker-blocking hold."""

    def _reserve(self, approval_id, client, host=None):
        # A DISTINCT host per approval unless a test asks otherwise, so these tests
        # exercise the caps and not duplicate grouping: same client + same host is one
        # card by design, and a shared default host would have quietly turned every
        # multi-reserve case below into a single group that no cap ever refuses.
        return cp.holds._reserve_hold(approval_id, threading.Event(), client,
                                host or f"{approval_id}.example.com")

    def test_global_cap_fails_closed_over_limit(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 2, 0
        self.assertIsNone(self._reserve("a", "c1").refused)
        self.assertIsNone(self._reserve("b", "c2").refused)
        reason = self._reserve("c", "c3").refused
        self.assertIsNotNone(reason)
        self.assertIn("global", reason)

    def test_per_client_cap_isolates_clients(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 100, 2
        self.assertIsNone(self._reserve("a1", "A").refused)
        self.assertIsNone(self._reserve("a2", "A").refused)
        over = self._reserve("a3", "A").refused
        self.assertIsNotNone(over)
        self.assertIn("client A", over)
        # A different client is unaffected by A's saturation.
        self.assertIsNone(self._reserve("b1", "B").refused)

    def test_per_client_cap_disabled_with_zero(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 100, 0
        for i in range(10):
            self.assertIsNone(self._reserve(f"x{i}", "same-client").refused)

    def test_none_client_bypasses_per_client_cap_but_not_global(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 3, 1
        # client=None never counts against the per-client cap...
        self.assertIsNone(self._reserve("n1", None).refused)
        self.assertIsNone(self._reserve("n2", None).refused)
        self.assertIsNone(self._reserve("n3", None).refused)
        # ...but still hits the global cap.
        self.assertIsNotNone(self._reserve("n4", None).refused)

    def test_release_frees_a_global_slot(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 1, 0
        self.assertIsNone(self._reserve("a", "c1").refused)
        self.assertIsNotNone(self._reserve("b", "c2").refused)  # full
        cp.holds._release_hold("a")
        self.assertIsNone(self._reserve("b", "c2").refused)  # slot freed

    def test_release_forgets_the_slot(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 5, 0
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


class DuplicateGroupingTests(_HoldRegistryTestCase):
    """A retrying agent asks the same question repeatedly. Those requests share ONE
    card and one decision; they do not share a worker, and they never share an audit
    line."""

    def _reserve(self, approval_id, client="c1", host="example.com",
                 port=443, proto="https"):
        return cp.holds._reserve_hold(approval_id, threading.Event(), client,
                                host, port, proto)

    def test_an_identical_request_joins_the_existing_card(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 100, 4
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
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 100, 4
        for i in range(20):
            self.assertIsNone(self._reserve(f"r{i}").refused)
        self.assertEqual(len(cp.holds._PENDING_EVENTS), 1)
        self.assertEqual(cp.holds._PENDING_WAITERS["r0"], 20)

    def test_a_joined_waiter_still_costs_a_global_slot(self):
        # The global cap bounds BLOCKED WORKERS, and a joiner blocks one. If grouping
        # were free here it would be a route around the cap that protects governance
        # for every other sandbox — the one thing it must not become.
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 3, 0
        for i in range(3):
            self.assertIsNone(self._reserve(f"j{i}").refused)
        over = self._reserve("j3")
        self.assertIsNotNone(over.refused)
        self.assertIn("global", over.refused)

    def test_a_different_client_gets_its_own_card(self):
        # The decision is a function of the host alone, but approving one sandbox's
        # request must never release another's.
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 100, 4
        self.assertFalse(self._reserve("a", client="A").joined)
        self.assertFalse(self._reserve("b", client="B").joined)
        self.assertEqual(len(cp.holds._PENDING_EVENTS), 2)

    def test_port_and_proto_split_a_card_but_method_and_url_do_not(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 100, 0
        self.assertFalse(self._reserve("a", port=443).joined)
        self.assertFalse(self._reserve("b", port=80).joined)
        self.assertFalse(self._reserve("c", proto="http", port=443).joined)
        # ...and method/url are not even arguments: retries vary them (cache-busters,
        # query strings), which is exactly why keying on them would defeat grouping in
        # the case it exists for. Same host/port/proto/client is the same card.
        self.assertTrue(self._reserve("d", port=443).joined)
        self.assertEqual(len(cp.holds._PENDING_EVENTS), 3)

    def test_the_host_key_is_case_insensitive_like_the_matcher(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 100, 0
        self.assertFalse(self._reserve("a", host="Example.COM").joined)
        self.assertTrue(self._reserve("b", host="example.com").joined)

    def test_a_closed_group_is_not_joinable_but_its_waiters_survive(self):
        # What makes "one click decides what it showed": the moment a card is decided
        # it stops accepting joiners, while the workers already on it stay registered
        # so resolve() can still find the event and the global cap still counts them.
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 100, 0
        self._reserve("a")
        self._reserve("b")
        cp.holds._close_group("a")
        self.assertIn("a", cp.holds._PENDING_EVENTS)
        self.assertEqual(cp.holds._PENDING_WAITERS["a"], 2)
        after = self._reserve("c")
        self.assertFalse(after.joined)
        self.assertEqual(after.approval_id, "c")

    def test_the_card_frees_only_when_the_last_waiter_leaves(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 100, 0
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
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 100, 0
        first = self._reserve("a")
        self.assertEqual(self._reserve("b").deadline, first.deadline)
        self.assertAlmostEqual(first.deadline - time.time(), cp.holds.HOLD_TIMEOUT, delta=5)

    def test_a_refusal_reserves_nothing(self):
        cp.holds.MAX_PENDING, cp.holds.MAX_PENDING_PER_CLIENT = 1, 0
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
