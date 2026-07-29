"""Unit tests for the control plane's security-load-bearing logic
(``control-plane/app.py``): the policy decision ``_decide`` (block-wins-over-allow,
subdomain semantics, default-hold) and the hold-cap reservation
(``_reserve_hold`` / ``_release_hold``).

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
    with cp._connect() as conn:
        conn.execute("DELETE FROM rules")
        conn.executemany(
            "INSERT INTO rules(pattern, action, source, created_at) "
            "VALUES (?,?, 'test', 0)", rules)
        conn.commit()


class DecideTests(unittest.TestCase):
    def setUp(self):
        cp._init_db()
        _set_rules([])

    def test_unmatched_host_is_held(self):
        decision, reason = cp._decide("unknown.example.com")
        self.assertEqual(decision, "hold")
        self.assertIn("held for approval", reason)

    def test_exact_allow_rule(self):
        _set_rules([("example.com", "allow")])
        self.assertEqual(cp._decide("example.com")[0], "allow")
        # A bare rule must not authorize a subdomain.
        self.assertEqual(cp._decide("a.example.com")[0], "hold")

    def test_block_rule_denies(self):
        _set_rules([("blocked.com", "block")])
        self.assertEqual(cp._decide("blocked.com")[0], "deny")

    def test_block_wins_over_allow(self):
        # A host can't be both allowed and blocked by one pattern (rules.pattern
        # is UNIQUE), but two DISTINCT patterns can both match a host — e.g. a
        # wildcard allow with a specific-subdomain block. Block must win.
        _set_rules([(".example.com", "allow"), ("evil.example.com", "block")])
        decision, reason = cp._decide("evil.example.com")
        self.assertEqual(decision, "deny")
        self.assertIn("blocked", reason)
        # A sibling under the same wildcard is still allowed.
        self.assertEqual(cp._decide("safe.example.com")[0], "allow")

    def test_subdomain_allow_matches_apex_and_children(self):
        _set_rules([(".example.com", "allow")])
        self.assertEqual(cp._decide("example.com")[0], "allow")
        self.assertEqual(cp._decide("a.b.example.com")[0], "allow")
        self.assertEqual(cp._decide("notexample.com")[0], "hold")

    def test_decision_is_case_insensitive(self):
        _set_rules([("example.com", "allow")])
        self.assertEqual(cp._decide("EXAMPLE.COM")[0], "allow")


class MatchTests(unittest.TestCase):
    def test_match_semantics(self):
        self.assertTrue(cp._match("example.com", "example.com"))
        self.assertFalse(cp._match("a.example.com", "example.com"))
        self.assertTrue(cp._match("a.example.com", ".example.com"))
        self.assertTrue(cp._match("example.com", ".example.com"))
        self.assertFalse(cp._match("notexample.com", ".example.com"))


class HoldCapTests(unittest.TestCase):
    """The hold cap protects a shared threadpool: over cap, /authorize must fail
    CLOSED instead of registering another worker-blocking hold."""

    def setUp(self):
        self._saved = (cp.MAX_PENDING, cp.MAX_PENDING_PER_CLIENT)
        cp._PENDING_EVENTS.clear()
        cp._PENDING_CLIENT.clear()

    def tearDown(self):
        cp.MAX_PENDING, cp.MAX_PENDING_PER_CLIENT = self._saved
        cp._PENDING_EVENTS.clear()
        cp._PENDING_CLIENT.clear()

    def _reserve(self, approval_id, client):
        return cp._reserve_hold(approval_id, threading.Event(), client)

    def test_global_cap_fails_closed_over_limit(self):
        cp.MAX_PENDING, cp.MAX_PENDING_PER_CLIENT = 2, 0
        self.assertIsNone(self._reserve("a", "c1"))
        self.assertIsNone(self._reserve("b", "c2"))
        reason = self._reserve("c", "c3")
        self.assertIsNotNone(reason)
        self.assertIn("global", reason)

    def test_per_client_cap_isolates_clients(self):
        cp.MAX_PENDING, cp.MAX_PENDING_PER_CLIENT = 100, 2
        self.assertIsNone(self._reserve("a1", "A"))
        self.assertIsNone(self._reserve("a2", "A"))
        over = self._reserve("a3", "A")
        self.assertIsNotNone(over)
        self.assertIn("client A", over)
        # A different client is unaffected by A's saturation.
        self.assertIsNone(self._reserve("b1", "B"))

    def test_per_client_cap_disabled_with_zero(self):
        cp.MAX_PENDING, cp.MAX_PENDING_PER_CLIENT = 100, 0
        for i in range(10):
            self.assertIsNone(self._reserve(f"x{i}", "same-client"))

    def test_none_client_bypasses_per_client_cap_but_not_global(self):
        cp.MAX_PENDING, cp.MAX_PENDING_PER_CLIENT = 3, 1
        # client=None never counts against the per-client cap...
        self.assertIsNone(self._reserve("n1", None))
        self.assertIsNone(self._reserve("n2", None))
        self.assertIsNone(self._reserve("n3", None))
        # ...but still hits the global cap.
        self.assertIsNotNone(self._reserve("n4", None))

    def test_release_frees_a_global_slot(self):
        cp.MAX_PENDING, cp.MAX_PENDING_PER_CLIENT = 1, 0
        self.assertIsNone(self._reserve("a", "c1"))
        self.assertIsNotNone(self._reserve("b", "c2"))  # full
        cp._release_hold("a")
        self.assertIsNone(self._reserve("b", "c2"))  # slot freed

    def test_release_forgets_the_slot(self):
        cp.MAX_PENDING, cp.MAX_PENDING_PER_CLIENT = 5, 0
        self._reserve("a", "c1")
        cp._release_hold("a")
        # Released ids are fully forgotten from both registries.
        self.assertNotIn("a", cp._PENDING_EVENTS)
        self.assertNotIn("a", cp._PENDING_CLIENT)
        # Releasing an already-released id is a harmless no-op.
        cp._release_hold("a")


if __name__ == "__main__":
    unittest.main()
