# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the control plane's security-load-bearing logic
(``control-plane/app.py``): the policy decision ``_decide`` (block-wins-over-allow,
subdomain semantics, default-hold) and the hold registry
(``_reserve_hold`` / ``_release_hold``: the two caps, and duplicate grouping).

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


def _set_rules(rules):
    """Replace the rules table with (pattern, action) tuples."""
    with cp.store._connect() as conn:
        conn.execute("DELETE FROM rules")
        conn.executemany(
            "INSERT INTO rules(pattern, action, source, created_at) "
            "VALUES (?,?, 'test', 0)", rules)
        conn.commit()


class DecideTests(unittest.TestCase):
    def setUp(self):
        cp.store._init_db()
        _set_rules([])

    def test_unmatched_host_is_held(self):
        decision, reason = cp.policy._decide("unknown.example.com")
        self.assertEqual(decision, "hold")
        self.assertIn("held for approval", reason)

    def test_exact_allow_rule(self):
        _set_rules([("example.com", "allow")])
        self.assertEqual(cp.policy._decide("example.com")[0], "allow")
        # A bare rule must not authorize a subdomain.
        self.assertEqual(cp.policy._decide("a.example.com")[0], "hold")

    def test_block_rule_denies(self):
        _set_rules([("blocked.com", "block")])
        self.assertEqual(cp.policy._decide("blocked.com")[0], "deny")

    def test_block_wins_over_allow(self):
        # A host can't be both allowed and blocked by one pattern (rules.pattern
        # is UNIQUE), but two DISTINCT patterns can both match a host — e.g. a
        # wildcard allow with a specific-subdomain block. Block must win.
        _set_rules([(".example.com", "allow"), ("evil.example.com", "block")])
        decision, reason = cp.policy._decide("evil.example.com")
        self.assertEqual(decision, "deny")
        self.assertIn("blocked", reason)
        # A sibling under the same wildcard is still allowed.
        self.assertEqual(cp.policy._decide("safe.example.com")[0], "allow")

    def test_subdomain_allow_matches_apex_and_children(self):
        _set_rules([(".example.com", "allow")])
        self.assertEqual(cp.policy._decide("example.com")[0], "allow")
        self.assertEqual(cp.policy._decide("a.b.example.com")[0], "allow")
        self.assertEqual(cp.policy._decide("notexample.com")[0], "hold")

    def test_decision_is_case_insensitive(self):
        _set_rules([("example.com", "allow")])
        self.assertEqual(cp.policy._decide("EXAMPLE.COM")[0], "allow")

    def test_trailing_fqdn_dot_is_normalized(self):
        # `evil.com.` and `evil.com` are the same destination, so an operator BLOCK of
        # `evil.com` must not be evadable with the trailing-dot spelling — which would
        # otherwise miss the rule, land in a hold and be re-promptable indefinitely.
        _set_rules([("evil.com", "block")])
        self.assertEqual(cp.policy._decide("evil.com.")[0], "deny")
        # A trailing dot on an allowed host still resolves to allow, not hold.
        _set_rules([("example.com", "allow")])
        self.assertEqual(cp.policy._decide("example.com.")[0], "allow")


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
