"""Unit tests for the control plane's request-handling flow
(``control-plane/app.py``): the ``authorize`` handler's allow/deny/hold
orchestration (including the timeout-defaults-to-deny path and the
resolve-wakes-the-waiter handshake), the ``resolve`` handler (persist-writes-a-
rule, bad-action, and the already-resolved race), and ``_seed_if_empty``.

Feasible dependency-free because the FastAPI stub (``tests/_loader.py``) leaves
the decorated handlers as plain callables, so we invoke ``authorize`` / ``resolve``
directly with stub pydantic models. The hold handshake uses a real background
thread; ``HOLD_TIMEOUT`` is shortened per-test so a bug fails fast instead of
blocking the default 120s."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="dockade-cp-api-test-")
os.environ["CONTROL_DB"] = os.path.join(_TMP, "control.db")
os.environ["CONTROL_SEED"] = os.path.join(_TMP, "nonexistent-seed.txt")

from _loader import load_control_plane  # noqa: E402 (must set env first)

cp = load_control_plane()


def _set_rules(rules):
    with cp._connect() as conn:
        conn.execute("DELETE FROM rules")
        conn.executemany(
            "INSERT INTO rules(pattern, action, source, created_at) "
            "VALUES (?,?, 'test', 0)", rules)
        conn.commit()


def _clear_all():
    with cp._connect() as conn:
        conn.execute("DELETE FROM rules")
        conn.execute("DELETE FROM approvals")
        conn.execute("DELETE FROM audit")
        conn.commit()
    cp._PENDING_EVENTS.clear()
    cp._PENDING_CLIENT.clear()
    cp._PENDING_OUTCOME.clear()


def _auth_req(host, **kw):
    return cp.AuthorizeRequest(host=host, **kw)


class _CPTestCase(unittest.TestCase):
    """Fresh schema + empty tables per test, and ``_audit`` muted — it prints the
    ``make logs-cp`` live feed to stdout, which is just noise here (no test
    asserts on audit rows), and its DB write is a fire-and-forget side effect
    orthogonal to the decision logic under test."""

    def setUp(self):
        cp._init_db()
        _clear_all()
        patch = mock.patch.object(cp, "_audit")
        patch.start()
        self.addCleanup(patch.stop)


class AuthorizeDecisionTests(_CPTestCase):
    def test_allow_rule_returns_allow_without_holding(self):
        _set_rules([("example.com", "allow")])
        resp = cp.authorize(_auth_req("example.com"))
        self.assertEqual(resp.decision, "allow")
        # An allow decision must not create an approval row.
        self.assertEqual(cp._list_pending(), [])

    def test_block_rule_returns_deny(self):
        _set_rules([("blocked.com", "block")])
        resp = cp.authorize(_auth_req("blocked.com"))
        self.assertEqual(resp.decision, "deny")

    def test_over_cap_hold_fails_closed_immediately(self):
        # With no hold slots, an unmatched host must deny at once (not block).
        saved = cp.MAX_PENDING
        cp.MAX_PENDING = 0
        try:
            start = time.monotonic()
            resp = cp.authorize(_auth_req("unknown.com", client="a"))
            elapsed = time.monotonic() - start
        finally:
            cp.MAX_PENDING = saved
        self.assertEqual(resp.decision, "deny")
        self.assertIn("hold capacity exceeded", resp.reason)
        self.assertLess(elapsed, 1.0)                      # did not block

    def test_hold_times_out_to_deny(self):
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 0.05
        try:
            resp = cp.authorize(_auth_req("slow.com", client="a"))
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertEqual(resp.decision, "deny")
        self.assertIn("timeout", resp.reason)
        # The approval row is left as 'expired', not stuck 'pending'.
        with cp._connect() as conn:
            statuses = [r[0] for r in conn.execute(
                "SELECT status FROM approvals WHERE host='slow.com'").fetchall()]
        self.assertEqual(statuses, ["expired"])


class HoldHandshakeTests(_CPTestCase):
    """The full hold path: a blocked authorize is woken by a human resolve."""

    def _authorize_in_thread(self, host, client="agent-1"):
        result = {}

        def worker():
            result["resp"] = cp.authorize(_auth_req(host, client=client))

        t = threading.Thread(target=worker)
        t.start()
        # Wait for the pending approval to register.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = cp._list_pending()
            if pending:
                return t, result, pending[0]["id"]
            time.sleep(0.01)
        t.join(1)
        raise AssertionError("approval never became pending")

    def test_allow_persist_wakes_waiter_and_writes_rule(self):
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 5
        try:
            t, result, approval_id = self._authorize_in_thread("newsite.com")
            resolve_resp = cp.resolve(
                approval_id, cp.ResolveRequest(action="allow_persist"))
            t.join(2)
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertFalse(t.is_alive())
        self.assertEqual(result["resp"].decision, "allow")
        self.assertTrue(resolve_resp.args[0]["ok"])
        # persist wrote an operator allow rule, so a re-decide skips the hold.
        self.assertEqual(cp._decide("newsite.com")[0], "allow")

    def test_deny_once_wakes_waiter_without_writing_rule(self):
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 5
        try:
            t, result, approval_id = self._authorize_in_thread("nope.com")
            cp.resolve(approval_id, cp.ResolveRequest(action="deny_once"))
            t.join(2)
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertFalse(t.is_alive())
        self.assertEqual(result["resp"].decision, "deny")
        # deny_once must NOT persist a rule — the host stays held next time.
        self.assertEqual(cp._decide("nope.com")[0], "hold")


class ResolveTests(_CPTestCase):
    def test_bad_action_is_rejected(self):
        resp = cp.resolve("whatever", cp.ResolveRequest(action="nonsense"))
        self.assertEqual(resp.kwargs.get("status_code"), 400)

    def test_unknown_or_expired_id_is_conflict(self):
        # No pending event registered for this id -> 409.
        resp = cp.resolve("missing-id", cp.ResolveRequest(action="allow_once"))
        self.assertEqual(resp.kwargs.get("status_code"), 409)


class SeedTests(_CPTestCase):
    def test_seed_loads_lowercased_allow_rules_and_is_idempotent(self):
        seed = os.path.join(_TMP, "seed.txt")
        with open(seed, "w") as f:
            f.write("# a comment\n\n Example.COM \n.pypi.org\n")
        saved = cp.SEED_PATH
        cp.SEED_PATH = seed
        try:
            n = cp._seed_if_empty()
            self.assertEqual(n, 2)
            with cp._connect() as conn:
                rows = {(r["pattern"], r["action"], r["source"]) for r in
                        conn.execute("SELECT pattern, action, source FROM rules")}
            self.assertEqual(rows, {("example.com", "allow", "seed"),
                                    (".pypi.org", "allow", "seed")})
            # Idempotent: a second call is a no-op once rules exist.
            self.assertEqual(cp._seed_if_empty(), 0)
        finally:
            cp.SEED_PATH = saved


if __name__ == "__main__":
    unittest.main()
