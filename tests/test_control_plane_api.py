# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the control plane's request-handling flow
(``control-plane/app.py`` and the ``api_*`` surfaces it mounts): the ``authorize``
handler's allow/deny/hold orchestration (including the timeout-defaults-to-deny path
and the resolve-wakes-the-waiter handshake), the ``resolve`` handler (persist-writes-a-
rule, bad-action, and the already-resolved race), and ``_seed_if_empty``.

Feasible dependency-free because the FastAPI stub (``tests/_loader.py``) leaves
the decorated handlers as plain callables, so we invoke ``authorize`` / ``resolve``
directly with stub pydantic models. The hold handshake uses a real background
thread; ``HOLD_TIMEOUT`` is shortened per-test so a bug fails fast instead of
blocking the default 120s."""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import types
import typing
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

_TMP = tempfile.mkdtemp(prefix="dockade-cp-api-test-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["CONTROL_DB"] = os.path.join(_TMP, "control.db")
os.environ["CONTROL_SEED"] = os.path.join(_TMP, "nonexistent-seed.txt")

from _loader import load_control_plane  # noqa: E402 (must set env first)

cp = load_control_plane()


# The client class these tests decide as, and an address that maps to it under the
# default CIDR map. Rules are scoped to a class, so a test that writes one and a
# request that must match it have to agree — naming both here keeps that agreement in
# one place rather than in every fixture.
CLASS = cp.store.LEGACY_CLIENT_CLASS
CLASS_IP = "172.30.0.130"

#: Every module that answers a request, as one text: app.py and the ``api_*`` surfaces
#: it mounts. For the source-reading guards, which must see every handler wherever it
#: lives — a glob, so a new surface module is covered without being registered here.
_HANDLER_SOURCE = "\n".join(
    p.read_text() for p in [*sorted((ROOT / "control-plane").glob("api_*.py")),
                            ROOT / "control-plane" / "app.py"])


def _set_rules(rules):
    """(pattern, action) tuples, or (pattern, action, client_class) where the class
    is what a test is actually about."""
    with cp.store._connect() as conn:
        conn.execute("DELETE FROM rules")
        conn.executemany(
            "INSERT INTO rules(pattern, action, source, created_at, client_class) "
            "VALUES (?,?, 'test', 0, ?)",
            [r if len(r) == 3 else (*r, CLASS) for r in rules])
        conn.commit()


def _clear_all():
    with cp.store._connect() as conn:
        conn.execute("DELETE FROM rules")
        conn.execute("DELETE FROM leases")
        conn.execute("DELETE FROM tool_rules")
        conn.execute("DELETE FROM tool_pins")
        conn.execute("DELETE FROM mcp_servers")
        conn.execute("DELETE FROM approvals")
        conn.execute("DELETE FROM tool_approvals")
        conn.execute("DELETE FROM audit")
        conn.commit()
    cp.holds._PENDING_EVENTS.clear()
    cp.holds._PENDING_CLIENT.clear()
    cp.holds._PENDING_WAITERS.clear()
    cp.holds._PENDING_DEADLINE.clear()
    cp.holds._GROUPS.clear()
    # Module state like those above, and reset for the same reason: it is
    # process-lifetime by design, which across tests means it leaks.
    cp.holds._SATURATION.update(count=0, last_ts=None, last_scope=None, last_host=None,
                          acked=0, acked_ts=None)
    cp.inventory._SEEN.clear()


def _auth_req(host, **kw):
    """An /authorize request that classifies as ``CLASS`` unless a test says otherwise.

    The client defaults to a real sandbox-net address rather than being left unset,
    because the class is derived from it: an absent client is UNCLASSIFIED, matches no
    rule and is held, so every decision test would otherwise be testing the
    unclassified path by accident."""
    kw.setdefault("client", CLASS_IP)
    return cp.api_authorize.AuthorizeRequest(host=host, **kw)


class _FakeRequest:
    """Stand-in for the Starlette Request that ``resolve`` reads provenance from
    (``_actor``). Headers are lowercased like Starlette's case-insensitive mapping.

    ``is_disconnected`` is here for ``approvals_stream``, the one handler that reads
    the connection rather than the request: still connected unless a test says
    otherwise, which is what makes the stream produce a tick to assert on."""

    def __init__(self, peer="172.31.0.3", headers=None, disconnected=False):
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.headers = types.SimpleNamespace(get=self._headers.get)
        self.client = types.SimpleNamespace(host=peer) if peer else None
        self._disconnected = disconnected

    async def is_disconnected(self):
        return self._disconnected


def _resolve(approval_id, action, request=None, **fields):
    """resolve() with a default request, so tests that don't care about provenance
    stay readable. Extra kwargs go on the ResolveRequest (e.g. ``pattern=``)."""
    return cp.api_approvals.resolve(
        approval_id, cp.api_approvals.ResolveRequest(action=action, **fields),
        request if request is not None else _FakeRequest())


def _hold(host, approval_id="hold-1", client=None, client_class=CLASS):
    """Register an INDEPENDENT pending approval with no blocked authorize() behind it:
    the durable row plus the in-process slot ``resolve`` looks for. Enough for every
    resolve-side assertion, and no thread to wait on — the wake path has its own tests.

    The in-process half goes through the REAL ``_reserve_hold`` rather than assigning
    to the registries by hand. It used to do the latter, and when the registry grew a
    waiter count and a group key the fixture kept building a half-registered hold that
    the production code would never produce — tests passing against a state that cannot
    occur.

    ``client`` defaults to the approval id — unique, therefore a distinct group key —
    so two ``_hold``s on the same host are two cards. Callers that want them GROUPED
    (which is what a real duplicate does) pass the same client explicitly.

    ``client_class`` is stored EXPLICITLY rather than derived from that client, and
    defaults to a real one: a persist needs a class to scope the rule to, so leaving it
    NULL would make every ``*_persist`` test exercise the unclassified refusal instead
    of the write it means to assert. Tests about that refusal pass None."""
    with cp.store._connect() as conn:
        conn.execute(
            "INSERT INTO approvals(id, ts, host, client, client_class, status) "
            "VALUES (?,?,?,?,?, 'pending')",
            (approval_id, time.time(), host,
             approval_id if client is None else client, client_class))
        conn.commit()
    cp.holds._reserve_hold(approval_id, threading.Event(),
                     approval_id if client is None else client, host)
    return approval_id


class _FailsTheApprovalsInsert:
    """A real connection that refuses one statement: the approvals INSERT.

    Wrapped rather than counted. Failing the Nth ``_connect`` would pass vacuously the
    day ``policy._decide`` opens a second one — the reservation would never be reached,
    the registries would be empty for the wrong reason, and the test would still be
    green. Naming the statement pins the failure to the window the guard is about.

    ``__enter__`` returns SELF, not the wrapped connection: ``sqlite3``'s own returns
    the connection, and delegating to it would hand the caller an unguarded handle."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def execute(self, sql, *args, **kwargs):
        if "INSERT INTO approvals" in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args, **kwargs)


def _rules():
    with cp.store._connect() as conn:
        return {(r["pattern"], r["action"]) for r in
                conn.execute("SELECT pattern, action FROM rules")}


class _CPTestCase(unittest.TestCase):
    """Fresh schema + empty tables per test, and ``_audit`` muted by default — it
    prints the ``make logs-cp`` live feed to stdout, which is noise for a test about
    decision logic, and its DB write is a side effect orthogonal to that.

    A subclass that is ABOUT what lands in the table sets ``audits = True`` and gets
    the real writer. That distinction has to be a switch rather than a second base
    class: the columns a row carries are worth asserting on the way THROUGH the write,
    since a kwarg that reaches ``_audit`` and not the INSERT would satisfy a
    mock-based assertion and leave the column empty."""

    #: Whether this test's audit rows are real. See the class docstring.
    audits = False

    def setUp(self):
        cp.store._init_db()
        _clear_all()
        if not self.audits:
            patch = mock.patch.object(cp.store, "_audit")
            patch.start()
            self.addCleanup(patch.stop)


class AuthorizeDecisionTests(_CPTestCase):
    def test_allow_rule_returns_allow_without_holding(self):
        _set_rules([("example.com", "allow")])
        resp = cp.api_authorize.authorize(_auth_req("example.com"))
        self.assertEqual(resp.decision, "allow")
        # An allow decision must not create an approval row.
        self.assertEqual(cp.holds._list_pending(), [])

    def test_block_rule_returns_deny(self):
        _set_rules([("blocked.com", "block")])
        resp = cp.api_authorize.authorize(_auth_req("blocked.com"))
        self.assertEqual(resp.decision, "deny")

    def test_over_cap_hold_fails_closed_immediately(self):
        # With no hold slots, an unmatched host must deny at once (not block).
        saved = cp.holds.MAX_PENDING
        cp.holds.MAX_PENDING = 0
        try:
            start = time.monotonic()
            resp = cp.api_authorize.authorize(_auth_req("unknown.com", client="a"))
            elapsed = time.monotonic() - start
        finally:
            cp.holds.MAX_PENDING = saved
        self.assertEqual(resp.decision, "deny")
        self.assertIn("hold capacity exceeded", resp.reason)
        self.assertLess(elapsed, 1.0)                      # did not block

    def test_a_failed_store_write_does_not_leak_the_hold_slot(self):
        # The slot is reserved BEFORE the approvals row is written, so a store failure
        # in between used to leave it registered for the life of the process — and the
        # leaked _GROUPS entry was the sharp end: every later request for the same
        # (client, host, port, proto) joined a card with no row and no waiter behind
        # it, blocked out the original window, default-denied with a reason that reads
        # as operator inaction, and never appeared on anyone's screen. That destination
        # became permanently un-decidable, and MAX_WAITERS of them fail every sandbox's
        # holds closed until a restart.
        real = cp.store._connect
        with mock.patch.object(cp.store, "_connect",
                               lambda: _FailsTheApprovalsInsert(real())), \
                self.assertRaises(sqlite3.OperationalError):
            cp.api_authorize.authorize(_auth_req("unknown.com", client=CLASS_IP))

        # Every registry, because a partial release is the same defect wearing fewer
        # entries — and _GROUPS is the one that decides whether the next request for
        # this host gets a card or inherits a phantom.
        self.assertEqual(cp.holds._PENDING_EVENTS, {})
        self.assertEqual(cp.holds._PENDING_WAITERS, {})
        self.assertEqual(cp.holds._PENDING_CLIENT, {})
        self.assertEqual(cp.holds._PENDING_DEADLINE, {})
        self.assertEqual(cp.holds._GROUPS, {})

    def test_a_hold_after_a_failed_store_write_still_gets_its_own_card(self):
        # The consequence, asserted from the outside: with the slot released the next
        # request raises a REAL card rather than joining the leaked one. Without the
        # release this call blocks for the full window and returns a timeout deny, so
        # the short timeout here is what keeps the failure fast instead of a hang.
        real = cp.store._connect
        with mock.patch.object(cp.store, "_connect",
                               lambda: _FailsTheApprovalsInsert(real())), \
                self.assertRaises(sqlite3.OperationalError):
            cp.api_authorize.authorize(_auth_req("unknown.com", client=CLASS_IP))

        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 0.05
        try:
            resp = cp.api_authorize.authorize(_auth_req("unknown.com", client=CLASS_IP))
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertEqual(resp.decision, "deny")
        self.assertIn("timeout", resp.reason)
        # A row exists at all, which is what says a card was raised for the second
        # request rather than it silently inheriting the first one's slot.
        with cp.store._connect() as conn:
            statuses = [r[0] for r in conn.execute(
                "SELECT status FROM approvals WHERE host='unknown.com'").fetchall()]
        self.assertEqual(statuses, ["expired"])

    def test_hold_times_out_to_deny(self):
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 0.05
        try:
            resp = cp.api_authorize.authorize(_auth_req("slow.com", client="a"))
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertEqual(resp.decision, "deny")
        self.assertIn("timeout", resp.reason)
        # The approval row is left as 'expired', not stuck 'pending'.
        with cp.store._connect() as conn:
            statuses = [r[0] for r in conn.execute(
                "SELECT status FROM approvals WHERE host='slow.com'").fetchall()]
        self.assertEqual(statuses, ["expired"])


class HoldHandshakeTests(_CPTestCase):
    """The full hold path: a blocked authorize is woken by a human resolve."""

    def _authorize_in_thread(self, host, client=CLASS_IP):
        result = {}

        def worker():
            result["resp"] = cp.api_authorize.authorize(_auth_req(host, client=client))

        t = threading.Thread(target=worker)
        t.start()
        # Wait for the pending approval to register.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = cp.holds._list_pending()
            if pending:
                return t, result, pending[0]["id"]
            time.sleep(0.01)
        t.join(1)
        raise AssertionError("approval never became pending")

    def test_allow_persist_wakes_waiter_and_writes_rule(self):
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 5
        try:
            t, result, approval_id = self._authorize_in_thread("newsite.com")
            resolve_resp = _resolve(approval_id, "allow_persist")
            t.join(2)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertFalse(t.is_alive())
        self.assertEqual(result["resp"].decision, "allow")
        self.assertTrue(resolve_resp.args[0]["ok"])
        # persist wrote an operator allow rule, so a re-decide skips the hold.
        self.assertEqual(cp.policy._decide("newsite.com", CLASS)[0], "allow")

    def test_deny_once_wakes_waiter_without_writing_rule(self):
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 5
        try:
            t, result, approval_id = self._authorize_in_thread("nope.com")
            _resolve(approval_id, "deny_once")
            t.join(2)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertFalse(t.is_alive())
        self.assertEqual(result["resp"].decision, "deny")
        # deny_once must NOT persist a rule — the host stays held next time.
        self.assertEqual(cp.policy._decide("nope.com", CLASS)[0], "hold")


class DuplicateHoldTests(_CPTestCase):
    """Grouping through the real ``authorize`` path, which is the only place its
    concurrency shows: N blocked workers, one card, one decision, N audit lines."""

    def _authorize_many(self, n, host, client=CLASS_IP, urls=None):
        """Start n concurrent authorize() calls for the same request. Returns the
        threads and a results list that fills in as each returns."""
        results = [None] * n

        def worker(i):
            results[i] = cp.api_authorize.authorize(_auth_req(
                host, client=client, url=(urls[i] if urls else None)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        return threads, results

    def _await_card(self, requests):
        """Wait for exactly one pending card carrying ``requests`` blocked waiters."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = cp.holds._list_pending()
            if len(pending) == 1 and pending[0]["requests"] == requests:
                return pending[0]
            time.sleep(0.01)
        raise AssertionError(
            f"never saw one card with {requests} waiters; saw {cp.holds._list_pending()}")

    def test_four_retries_raise_one_card_that_says_it_is_four_requests(self):
        # The motivating case. Before grouping this was four cards, and with the
        # per-client cap at 4 the fifth retry was refused with no card at all.
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 10
        try:
            threads, results = self._authorize_many(4, "dup.example")
            card = self._await_card(4)
            # The count is ON THE CARD because grouping changed what one click means.
            self.assertEqual(card["requests"], 4)
            _resolve(card["id"], "allow_once")
            for t in threads:
                t.join(5)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertEqual([r.decision for r in results], ["allow"] * 4)
        with cp.store._connect() as conn:
            rows = conn.execute("SELECT id FROM approvals").fetchall()
        self.assertEqual(len(rows), 1, "duplicates must not each get a durable row")
        # "once" still means no standing rule — it now means four requests, not one.
        self.assertEqual(_rules(), set())
        self.assertEqual(cp.policy._decide("dup.example", CLASS)[0], "hold")

    def test_a_timeout_tells_every_waiter_it_was_a_timeout(self):
        # Only ONE waiter wins the expiry UPDATE; the rest read the row. Reporting
        # from `did I win the UPDATE` told the losers a human had rejected them —
        # an audit trail inventing a human decision for a timeout is worse than none.
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 0.3
        try:
            threads, results = self._authorize_many(3, "slow.example")
            for t in threads:
                t.join(5)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertEqual([r.decision for r in results], ["deny"] * 3)
        for r in results:
            self.assertIn("timeout", r.reason)
            self.assertNotIn("rejection", r.reason)
        with cp.store._connect() as conn:
            statuses = [r[0] for r in conn.execute(
                "SELECT status FROM approvals").fetchall()]
        self.assertEqual(statuses, ["expired"])

    def test_every_joined_request_is_audited_on_its_own_terms(self):
        # Grouping is a concept of the screen and the worker pool, never of the
        # record: each request keeps its own audit line and its own url, and the
        # joiners' reason names the card, so the log explains why four requests
        # produced one approval without anyone having to know about grouping.
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 10
        urls = ["https://dup.example/a", "https://dup.example/b",
                "https://dup.example/c"]
        try:
            threads, results = self._authorize_many(3, "dup.example", urls=urls)
            card = self._await_card(3)
            _resolve(card["id"], "deny_once")
            for t in threads:
                t.join(5)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        calls = [c for c in cp.store._audit.call_args_list if c.args[0] == "hold"]
        self.assertEqual(sorted(c.kwargs["url"] for c in calls), sorted(urls))
        reasons = [c.kwargs["reason"] for c in calls]
        self.assertEqual(sum(r == "held for approval" for r in reasons), 1)
        joined = [r for r in reasons if r.startswith("joined hold ")]
        self.assertEqual(len(joined), 2)
        for r in joined:
            self.assertIn(card["id"], r)
        # And a terminal line per request, not per card.
        self.assertEqual(
            sum(1 for c in cp.store._audit.call_args_list if c.args[0] == "deny"), 3)
        self.assertEqual([r.decision for r in results], ["deny"] * 3)

    def test_a_decided_card_stops_accepting_joiners_at_the_click(self):
        # The window closes with the DECISION, not when the last woken worker happens
        # to return. Asserted while the waiters are still registered, because that gap
        # is the whole risk: a retry landing in it would attach to an already-resolved
        # card and inherit an outcome nobody was shown it beside.
        approval = _hold("clicked.example", "held-1", client=CLASS_IP)
        before = cp.holds._reserve_hold("dup", threading.Event(), CLASS_IP,
                                  "clicked.example")
        self.assertTrue(before.joined, "joinable while pending")
        _resolve(approval, "deny_once")
        self.assertIn(approval, cp.holds._PENDING_EVENTS, "waiters not drained yet")
        after = cp.holds._reserve_hold("late", threading.Event(), CLASS_IP,
                                 "clicked.example")
        self.assertFalse(after.joined)
        self.assertEqual(after.approval_id, "late")

    def test_an_expired_card_stops_accepting_joiners_before_its_waiters_drain(self):
        # Same invariant on the other terminal path. A phantom waiter (registered, with
        # no thread that will ever release it) keeps the card in the registry after the
        # real waiter returns, so the group can only have been closed by the EXPIRY
        # path — not incidentally by the last release.
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 0.3
        try:
            threads, _ = self._authorize_many(1, "drain.example")
            card = self._await_card(1)
            phantom = cp.holds._reserve_hold("phantom", threading.Event(), CLASS_IP,
                                       "drain.example")
            self.assertTrue(phantom.joined)
            for t in threads:
                t.join(5)
            self.assertIn(card["id"], cp.holds._PENDING_EVENTS)
            late = cp.holds._reserve_hold("late", threading.Event(), CLASS_IP,
                                    "drain.example")
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertFalse(late.joined)

    def test_a_duplicate_arriving_after_the_decision_opens_a_new_card(self):
        # The joining window closes with the decision, so a retry that lands after
        # the click is held again rather than inheriting an outcome nobody was shown
        # it beside.
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 10
        try:
            threads, _ = self._authorize_many(2, "again.example")
            card = self._await_card(2)
            _resolve(card["id"], "deny_once")
            for t in threads:
                t.join(5)
            cp.holds.HOLD_TIMEOUT = 0.2
            late = cp.api_authorize.authorize(
                _auth_req("again.example", client=CLASS_IP))
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertEqual(late.decision, "deny")
        self.assertIn("timeout", late.reason)   # held afresh, then defaulted
        with cp.store._connect() as conn:
            rows = conn.execute("SELECT status FROM approvals ORDER BY ts").fetchall()
        self.assertEqual([r[0] for r in rows], ["denied", "expired"])


class _HoldAuditCase(_CPTestCase):
    """Real audit rows, and real blocked ``authorize()`` calls to raise a card."""

    audits = True

    def _hold_many(self, n, host, timeout):
        """n concurrent authorize() calls for one (client, host): one card. Returns
        the threads and a results list that fills in as each returns."""
        results = [None] * n

        def worker(i):
            results[i] = cp.api_authorize.authorize(_auth_req(host, client=CLASS_IP))

        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = timeout
        self.addCleanup(setattr, cp.holds, "HOLD_TIMEOUT", saved)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        return threads, results

    def _card(self, requests):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = cp.holds._list_pending()
            if len(pending) == 1 and pending[0]["requests"] == requests:
                return pending[0]["id"]
            time.sleep(0.01)
        raise AssertionError(f"never saw one card with {requests} waiters")

    def _rows(self, host):
        with cp.store._connect() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT kind, approval_id FROM audit WHERE host=? ORDER BY id",
                (host,)).fetchall()]


class HoldAuditApprovalIdTests(_HoldAuditCase):
    """An egress hold's hold row and final row carry the card's approval id, so the
    record view's search by id (``audit.EVENT_SEARCH``) finds the hold and how it
    ended. Rows written before any card exists carry none."""

    def _resolved(self, action, final):
        threads, _ = self._hold_many(1, "held.example", 10)
        card = self._card(1)
        _resolve(card, action)
        for t in threads:
            t.join(5)
        self.assertEqual(self._rows("held.example"), [("hold", card), (final, card)])

    def test_an_allowed_hold_names_its_card_on_both_rows(self):
        self._resolved("allow_once", "allow")

    def test_a_denied_hold_names_its_card_on_both_rows(self):
        self._resolved("deny_once", "deny")

    def test_an_expired_hold_names_its_card_on_both_rows(self):
        threads, results = self._hold_many(1, "held.example", 0.05)
        for t in threads:
            t.join(5)
        self.assertIn("timeout", results[0].reason)
        with cp.store._connect() as conn:
            (card,), = conn.execute("SELECT id FROM approvals").fetchall()
        self.assertEqual(self._rows("held.example"), [("hold", card), ("deny", card)])

    def test_a_joiner_names_the_card_it_joined(self):
        # Its reason names the card, and so does the column a search reads.
        threads, _ = self._hold_many(2, "held.example", 10)
        card = self._card(2)
        _resolve(card, "deny_once")
        for t in threads:
            t.join(5)
        self.assertEqual(sorted(self._rows("held.example")),
                         sorted([("hold", card)] * 2 + [("deny", card)] * 2))

    def test_a_decision_before_any_hold_names_no_card(self):
        # No card exists for these, so there is no id to carry.
        _set_rules([("allowed.example", "allow"), ("blocked.example", "block")])
        cp.api_authorize.authorize(_auth_req("allowed.example"))
        cp.api_authorize.authorize(_auth_req("blocked.example"))
        saved = cp.holds.MAX_PENDING
        cp.holds.MAX_PENDING = 0
        try:
            resp = cp.api_authorize.authorize(_auth_req("capped.example"))
        finally:
            cp.holds.MAX_PENDING = saved
        self.assertIn("hold capacity exceeded", resp.reason)
        self.assertEqual(self._rows("allowed.example"), [("allow", None)])
        self.assertEqual(self._rows("blocked.example"), [("deny", None)])
        self.assertEqual(self._rows("capped.example"), [("deny", None)])

    def test_searching_a_card_id_finds_its_hold_and_how_it_ended(self):
        threads, _ = self._hold_many(1, "held.example", 10)
        card = self._card(1)
        _resolve(card, "allow_once")
        for t in threads:
            t.join(5)
        rows = cp.api_views.api_audit_events(q=card)["rows"]
        self.assertEqual(sorted((r["kind"], r["host"], r["approval_id"]) for r in rows),
                         [("allow", "held.example", card),
                          ("hold", "held.example", card)])

    def test_the_glance_folds_egress_holds_across_cards_and_splits_tool_asks(self):
        """Five expired holds on one host are five cards and one hold line plus one
        deny line in the glance: the id is for search, and a retrying agent is
        repetition. Two tool asks that look alike stay two lines, one per grant."""
        for _ in range(5):
            threads, _ = self._hold_many(1, "held.example", 0.05)
            for t in threads:
                t.join(5)
        with cp.store._connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(DISTINCT approval_id) FROM audit "
                "WHERE host='held.example'").fetchone()[0], 5)
            for card in ("a" * 32, "b" * 32):
                conn.execute(
                    "INSERT INTO audit(ts, kind, stage, client, server, tool, "
                    "approval_id) VALUES (?, 'hold', 'tool-call', ?, 'mcp-github', "
                    "'create_pull_request', ?)", (time.time(), CLASS_IP, card))
            conn.commit()
            rows = cp.audit.grouped(conn, 50, cp.audit.parse(), 500)
        self.assertEqual(sorted((r["kind"], r["n"]) for r in rows
                                if r["host"] == "held.example"),
                         [("deny", 5), ("hold", 5)])
        self.assertEqual([r["n"] for r in rows if r["tool"] == "create_pull_request"],
                         [1, 1])


class LeaseAuditApprovalIdTests(_HoldAuditCase):
    """What rode a lease, and the lease's revoke, carry the card the lease was granted
    from, so searching that card's id finds them beside the hold that raised it."""

    CARD = "c" * 32

    def _leased(self, host):
        """A live lease for ``host`` granted from ``CARD``; its lease id."""
        _hold(host, approval_id=self.CARD)
        _resolve(self.CARD, "allow_lease")
        with cp.store._connect() as conn:
            (lease_id,), = conn.execute("SELECT id FROM leases").fetchall()
        return lease_id

    def _found(self, card=CARD):
        """What searching ``card`` finds, each reason cut to the words that say which
        writer wrote it: a lease's remaining time is not stable enough to compare."""
        return sorted((r["kind"], r["host"], r["approval_id"],
                       " ".join(r["reason"].split()[:3]))
                      for r in cp.api_views.api_audit_events(q=card)["rows"])

    def test_searching_a_card_id_finds_the_requests_that_rode_its_lease(self):
        threads, _ = self._hold_many(1, "leased.example", 10)
        card = self._card(1)
        _resolve(card, "allow_lease")
        for t in threads:
            t.join(5)
        for _ in range(2):
            self.assertEqual(cp.api_authorize.authorize(
                _auth_req("leased.example")).decision, "allow")
        self.assertEqual(self._found(card),
                         [("allow", "leased.example", card, "allowed by lease")] * 2
                         + [("allow", "leased.example", card, "human approval (lease"),
                            ("hold", "leased.example", card, "held for approval")])

    def test_a_rule_allow_for_a_leased_host_names_no_card(self):
        # The rule decided, so the lease is not credited (``policy._ruling_in``).
        self._leased("leased.example")
        _set_rules([("leased.example", "allow")])
        cp.api_authorize.authorize(_auth_req("leased.example"))
        self.assertEqual(self._rows("leased.example"), [("allow", None)])

    def test_revoking_a_live_lease_names_its_card(self):
        lease_id = self._leased("leased.example")
        cp.api_authorize.authorize(_auth_req("leased.example"))
        cp.api_egress.revoke_lease(lease_id, _FakeRequest())
        self.assertEqual(self._rows("leased.example"),
                         [("allow", self.CARD), ("revoke", self.CARD)])
        self.assertEqual(
            self._found(),
            [("allow", "leased.example", self.CARD, "allowed by lease"),
             ("revoke", "leased.example", self.CARD, "lease revoked with")])

    def test_removing_an_expired_lease_names_its_card(self):
        now = time.time()
        with cp.store._connect() as conn:
            lease_id = conn.execute(
                "INSERT INTO leases(host, client_class, approval_id, created_at, "
                "expires_at, granted_by) VALUES ('leased.example', ?, ?, ?, ?, 'x')",
                (CLASS, self.CARD, now - 100, now - 1)).lastrowid
            conn.commit()
        cp.api_egress.revoke_lease(lease_id, _FakeRequest())
        self.assertEqual(
            self._found(),
            [("revoke", "leased.example", self.CARD, "expired lease removed;")])


class ResolveTests(_CPTestCase):
    def test_bad_action_is_rejected(self):
        resp = _resolve("whatever", "nonsense")
        self.assertEqual(resp.kwargs.get("status_code"), 400)

    def test_unknown_or_expired_id_is_conflict(self):
        # No pending event registered for this id -> 409.
        resp = _resolve("missing-id", "allow_once")
        self.assertEqual(resp.kwargs.get("status_code"), 409)


class PersistConflictTests(_CPTestCase):
    """A `*_persist` cannot write over a rule that already holds its pattern.

    The insert is `INSERT OR IGNORE` against `UNIQUE(pattern)`, so before this it
    silently wrote NOTHING while the endpoint reported `persisted: true` and the card
    confirmed a standing rule. Deny-over-allow was the dangerous direction: the
    operator believed a subtree was permanently blocked and every later request to it
    was allowed without even raising a hold."""

    def test_a_persist_that_contradicts_an_existing_rule_is_refused(self):
        _set_rules([(".example.com", "allow")])
        resp = _resolve(_hold("b.example.com"), "deny_persist", pattern=".example.com")
        self.assertEqual(resp.kwargs.get("status_code"), 409)
        body = resp.args[0]
        self.assertFalse(body["ok"])
        # Names the rule standing in the way, so the operator can act on it rather
        # than guess — including WHICH class it is in, since the same pattern can
        # stand in two and only one of them is the obstacle.
        self.assertEqual(body["conflict"],
                         {"pattern": ".example.com", "action": "allow",
                          "client_class": CLASS})
        # And policy is untouched — no half-application.
        self.assertEqual(_rules(), {(".example.com", "allow")})

    def test_the_refused_approval_stays_pending_and_decidable(self):
        # Same reasoning as a rejected pattern: a persist that consumed the approval
        # without writing the rule would be the worst of both. The operator must get
        # to choose again.
        _set_rules([(".example.com", "allow")])
        approval = _hold("b.example.com")
        _resolve(approval, "deny_persist", pattern=".example.com")
        self.assertEqual([p["id"] for p in cp.holds._list_pending()], [approval])
        # A one-off decides the request without touching policy, and now succeeds.
        ok = _resolve(approval, "deny_once")
        self.assertTrue(ok.args[0]["ok"])
        self.assertEqual(_rules(), {(".example.com", "allow")})

    def test_the_conflict_is_refused_in_both_directions(self):
        # allow-over-block fails safe and is merely a lie; it is still refused, because
        # a response claiming a write that did not happen is the defect either way.
        _set_rules([("blocked.example", "block")])
        resp = _resolve(_hold("blocked.example"), "allow_persist",
                        pattern="blocked.example")
        self.assertEqual(resp.kwargs.get("status_code"), 409)
        self.assertEqual(_rules(), {("blocked.example", "block")})

    def test_the_same_rule_already_present_is_not_a_conflict(self):
        # The policy being asked for is already in force, so refusing would be noise.
        # It succeeds — and reports that it wrote nothing.
        _set_rules([(".example.com", "allow")])
        resp = _resolve(_hold("b.example.com"), "allow_persist", pattern=".example.com")
        body = resp.args[0]
        self.assertTrue(body["ok"])
        self.assertFalse(body["persisted"], "no row was written; do not claim one")
        self.assertTrue(body["already_present"])

    def test_persisted_reports_a_write_not_an_intention(self):
        resp = _resolve(_hold("fresh.example"), "allow_persist",
                        pattern="fresh.example")
        body = resp.args[0]
        self.assertTrue(body["persisted"])
        self.assertFalse(body["already_present"])
        self.assertEqual(_rules(), {("fresh.example", "allow")})

    def test_a_once_action_reports_neither(self):
        body = _resolve(_hold("once.example"), "deny_once").args[0]
        self.assertFalse(body["persisted"])
        self.assertFalse(body["already_present"])

    def test_the_offered_patterns_say_which_already_exist(self):
        # The prevention half: the confirm panel can warn BEFORE the click, because
        # each candidate carries any rule already holding it. The backend check still
        # has to exist — the rule can appear between this render and the click, which
        # is the only way the conflict arises at all.
        _set_rules([(".example.com", "allow")])
        _hold("a.b.example.com")
        options = {o["pattern"]: o["existing"]
                   for o in cp.holds._list_pending()[0]["persist_options"]}
        self.assertEqual(options["a.b.example.com"], None)
        self.assertEqual(options[".example.com"], "allow")


class ResponseShapeTests(_CPTestCase):
    """FastAPI derives each endpoint's response_model from its RETURN ANNOTATION and
    validates the handler's output against it. An annotation that disagrees with what
    the function actually returns is therefore not a type-checker nag — it is a 500
    in the browser, at runtime, on a path every other test says is fine.

    Nothing else in this suite can see that. The fastapi stub these tests run against
    is an identity decorator with no validation, which is precisely what keeps them
    dependency-free; the cost is that annotations are invisible. Not hypothetical:
    ``/api/audit`` kept ``-> list[dict]`` after it began returning ``{rows, total}``,
    every test passed, and the decisions view read "the control plane may be
    unreachable" until someone loaded the page.

    The roster is explicit rather than discovered, because the endpoints that cannot
    simply be called — ``authorize`` blocks on a hold, ``resolve`` and ``revoke_rule``
    return a Response directly — need judgement rather than reflection."""

    def test_each_json_endpoint_returns_what_it_declares(self):
        for name, call in (("healthz", cp.healthz),
                           ("approvals", cp.api_approvals.approvals),
                           ("api_rules", cp.api_egress.api_rules),
                           ("api_audit", cp.api_views.api_audit),
                           ("api_config", cp.api_views.api_config),
                           ("tool_roster", cp.api_tool.tool_roster)):
            with self.subTest(endpoint=name):
                # get_type_hints, not __annotations__: the module carries
                # `from __future__ import annotations`, so the raw values are
                # STRINGS. FastAPI resolves them the same way, which is why a
                # mismatch reaches runtime rather than import.
                declared = typing.get_type_hints(call).get("return")
                self.assertIsNotNone(declared, f"{name} declares no return type, so "
                                               f"FastAPI will not validate it")
                # An async endpoint hands back a coroutine; run it for its value.
                # The roster is about what each one RETURNS, and that must not
                # depend on which side of the threadpool it is answered from.
                result = call()
                if asyncio.iscoroutine(result):
                    result = asyncio.run(result)
                # `list[dict]` -> `list`; a bare `dict` has no origin and is its own.
                self.assertIsInstance(result, typing.get_origin(declared) or declared)


#: A lone surrogate: a JSON string can carry it, UTF-8 cannot.
_SURROGATE = chr(0xD800)
#: What the store writes for it: its backslash-u escape.
_ESCAPED = chr(92) + "ud800"


class InvalidRequestTests(unittest.TestCase):
    """``cp.invalid`` and the handler that serves it; its docstring says why. The stub
    cannot show FastAPI routing a validation failure here; a venv with the real one
    does."""

    def test_a_422_names_each_field_and_never_its_value(self):
        status, body = cp.invalid([
            {"loc": ("query", "limit"), "msg": "Input should be a valid integer",
             "input": "SECRET-INPUT", "type": "int_parsing"},
            {"loc": ("body", "pattern", 0), "msg": "Input should be a valid string",
             "input": _SURROGATE},
        ])
        self.assertEqual((status, body["ok"]), (422, False))
        self.assertEqual(body["detail"], "query.limit: Input should be a valid integer; "
                                         "body.pattern.0: Input should be a valid string")
        self.assertNotIn("SECRET", json.dumps(body))
        self.assertNotIn(_SURROGATE, json.dumps(body, ensure_ascii=False))

    def test_a_field_the_request_named_is_escaped_and_cut_short(self):
        _, body = cp.invalid([{"loc": ("body", "args", _SURROGATE + "x" * 300),
                               "msg": "Input should be a valid string"}])
        body["detail"].encode("utf-8")
        self.assertEqual(body["detail"], "body.args.\\ud800" + "x" * 99
                                         + ": Input should be a valid string")

    def test_only_the_first_faults_are_named_and_the_rest_counted(self):
        errors = [{"loc": ("body", "pattern", i), "msg": "bad"}
                  for i in range(cp.FAULTS_SHOWN + 5)]
        _, body = cp.invalid(errors)
        self.assertEqual(body["detail"].count("bad"), cp.FAULTS_SHOWN)
        self.assertTrue(body["detail"].endswith("; and 5 more"))
        _, body = cp.invalid(errors[:cp.FAULTS_SHOWN])
        self.assertNotIn("more", body["detail"])

    def test_every_listener_answers_a_malformed_request_itself(self):
        rejected = cp.RequestValidationError
        for name in ("app", "authorize_app", "tool_app"):
            with self.subTest(listener=name):
                handlers = getattr(cp, name).__dict__.get("exception_handlers", {})
                self.assertIs(handlers.get(rejected), cp.invalid_request)
        exc = rejected([{"loc": ("body", "pattern", 0), "msg": "bad",
                         "input": _SURROGATE}])
        response = asyncio.run(cp.invalid_request(None, exc))
        self.assertEqual((response.status_code, response.body), cp.invalid(exc.errors()))


class HealthProbeTests(unittest.TestCase):
    """The probe is answered on the event loop, for the reason the gateway's is.

    `authorize` is a plain `def` that blocks on a hold for up to `HOLD_TIMEOUT`, and
    Starlette runs it in a bounded threadpool. A sync `/healthz` shares that pool, so
    enough blocked authorizes queue the probe and compose restarts the control plane
    in the middle of the holds that made it look unhealthy — fail-closed egress for
    every sandbox, caused by the healthcheck. `MAX_WAITERS` is under the pool's
    default size today, which is what keeps it theoretical; this stops it depending
    on two numbers in different files agreeing."""

    def test_healthz_is_answered_on_the_event_loop(self):
        self.assertTrue(asyncio.iscoroutinefunction(cp.healthz))

    def test_every_listener_serves_the_same_probe(self):
        # One function, three apps: the split is about which ROUTES each listener
        # carries, and liveness is the one they deliberately share.
        self.assertEqual(asyncio.run(cp.healthz()), {"status": "ok"})


class RevokeRuleTests(_CPTestCase):
    """The other half of a governance plane that could grant but never take back.

    The asymmetry is the point: revoking an ALLOW tightens (with no other rule, the
    host reverts to unknown and is held), revoking a BLOCK loosens — an explicit
    operator denial becomes a request that can be approved by someone who never knew
    it had been refused. The backend treats both as the same operation and the UI
    carries the distinction in its confirm; what the backend owes is that the
    operation is recorded, attributed, and refused for seed rules."""

    def _rule(self, pattern, action="allow", source="operator"):
        with cp.store._connect() as conn:
            cur = conn.execute(
                "INSERT INTO rules(pattern, action, source, created_at) "
                "VALUES (?,?,?,0)", (pattern, action, source))
            conn.commit()
            return cur.lastrowid

    def _patterns(self):
        with cp.store._connect() as conn:
            return {r["pattern"] for r in conn.execute("SELECT pattern FROM rules")}

    def test_an_operator_rule_is_removed(self):
        rid = self._rule("evil.example", "block")
        resp = _revoke_rule(rid, _FakeRequest())
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("evil.example", self._patterns())

    def test_a_seed_rule_is_refused_by_the_backend(self):
        """Refused HERE, not merely hidden in the UI. The UI is a convenience layer
        over a backend that validates every input — and this particular refusal also
        guards a trap: `_seed_if_empty` re-reads the seed file whenever the rules
        table is empty, so a store whose every rule could be revoked would resurrect
        the entire seed allowlist on the next restart."""
        rid = self._rule("pypi.org", "allow", source="seed")
        resp = _revoke_rule(rid, _FakeRequest())
        self.assertEqual(resp.status_code, 403)
        self.assertIn("pypi.org", self._patterns())

    def test_the_refusal_says_where_to_change_it_instead(self):
        # A refusal with no next step is a dead end; the seed file IS the next step.
        rid = self._rule("pypi.org", "allow", source="seed")
        body = _revoke_rule(rid, _FakeRequest()).body
        self.assertIn("egress-allowlist.txt", json.dumps(body))

    def test_the_rules_table_can_never_be_emptied_by_revocation(self):
        """The property that makes the seed refusal load-bearing rather than
        decorative. Stated as behaviour rather than trusted as a consequence."""
        self._rule("seeded.example", "allow", source="seed")
        ids = [self._rule(f"op{i}.example") for i in range(3)]
        for rid in ids:
            _revoke_rule(rid, _FakeRequest())
        self.assertEqual(self._patterns(), {"seeded.example"})
        # And therefore a restart does not re-seed.
        self.assertEqual(cp.store._seed_if_empty(), 0)

    def test_an_unknown_id_is_a_404_not_a_silent_success(self):
        self.assertEqual(
            _revoke_rule(999999, _FakeRequest()).status_code, 404)

    def test_a_stale_revoke_does_not_uncover_a_wider_allow(self):
        # The page shows old.test; the id now holds a block on evil.example.com, under
        # an allow on .example.com. Revoking that block would let it through.
        self._rule(".example.com", "allow")
        rid = self._rule("old.test", "allow")
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM rules WHERE id=?", (rid,))
            conn.execute("INSERT INTO rules(id, pattern, action, source, created_at) "
                         "VALUES (?, 'evil.example.com', 'block', 'operator', 0)",
                         (rid,))
        resp = _revoke_rule(rid, seen={"pattern": "old.test", "action": "allow"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("evil.example.com (block)", resp.body["detail"])
        self.assertEqual(cp.policy._decide("evil.example.com", CLASS)[0], "deny")

    def test_a_revoke_racing_another_write_is_refused(self):
        # As the edit's race test: the DELETE carries the checked row in its WHERE.
        # A rule revoked meanwhile is a 404, as it would have been a moment earlier.
        for other, status in (("UPDATE rules SET action='allow' WHERE id=?", 409),
                              ("DELETE FROM rules WHERE id=?", 404)):
            with self.subTest(other=other):
                rid = self._rule("race.example", "block")

                def other_operator(rule_id, row, req, sql=other):
                    with cp.store._connect() as conn:
                        conn.execute(sql, (rule_id,))
                    return None

                with mock.patch.object(cp.api_egress, "_stale",
                                       side_effect=other_operator):
                    resp = _revoke_rule(rid)
                self.assertEqual(resp.status_code, status)
                with cp.store._connect() as conn:
                    conn.execute("DELETE FROM rules")

    def test_a_revoke_from_a_page_older_than_another_edit_is_refused(self):
        # Another operator turned the block into an allow; this page still offers to
        # revoke the block, and its confirm said so.
        rid = self._rule("example.com", "block")
        _edit_rule(rid, "example.com", "allow")
        resp = _revoke_rule(rid, seen={"action": "block"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self._patterns(), {"example.com"})

    def test_revocation_is_audited_with_provenance(self):
        """Editing standing policy is more consequential than any single egress
        decision, and nothing recorded that it had happened at all. Attribution is
        detection rather than prevention — the fields are forgeable by a host-local
        caller — but a forged revocation is at least visible afterwards."""
        rid = self._rule(".github.com", "allow")
        with mock.patch.object(cp.store, "_audit") as audit:
            _revoke_rule(rid, _FakeRequest(peer="172.31.0.9"))
        self.assertEqual(audit.call_args.args[0], "revoke")
        kwargs = audit.call_args.kwargs
        self.assertEqual(kwargs["host"], ".github.com")
        self.assertIn("peer=172.31.0.9", kwargs["actor"])
        self.assertIn("allow rule revoked", kwargs["reason"])

    def test_the_audit_reason_says_where_the_hosts_land(self):
        """Computed from `_decide` after the revoke, never assumed to be a hold: another
        rule may still decide the host, and then "held" would record the opposite of
        the decision in force. Each case: the rules left standing, the rule revoked,
        and the hosts whose post-revoke `_decide` reason the audit row must carry.
        `fresh.example.com` stands for a subdomain no narrower rule names."""
        cases = (
            ("block under a wider allow", [(".example.com", "allow")],
             ("evil.example.com", "block"), ["evil.example.com"], "allow"),
            ("allow under a wider allow", [(".example.com", "allow")],
             ("api.example.com", "allow"), ["api.example.com"], "allow"),
            ("wildcard over an exact apex allow", [("example.com", "allow")],
             (".example.com", "allow"), ["example.com", "fresh.example.com"], None),
            ("allow under a wider block", [(".example.com", "block")],
             ("api.example.com", "allow"), ["api.example.com"], "deny"),
            ("nothing else left", [], ("evil.example", "block"), ["evil.example"],
             "hold"),
        )
        for name, standing, (pattern, action), hosts, decision in cases:
            with self.subTest(case=name):
                _set_rules(standing)
                rid = self._rule(pattern, action)
                with mock.patch.object(cp.store, "_audit") as audit:
                    _revoke_rule(rid, _FakeRequest())
                reason = audit.call_args.kwargs["reason"]
                self.assertIn(f"{action} rule revoked", reason)
                for host in hosts:
                    self.assertIn(cp.policy._decide(host, CLASS)[1], reason)
                if decision is not None:
                    self.assertEqual(cp.policy._decide(hosts[0], CLASS)[0], decision)
                if decision in ("allow", "deny"):
                    self.assertNotIn("held for approval", reason)

    def test_a_wildcard_whose_apex_and_subdomains_land_alike_reads_as_one_clause(self):
        # Two clauses only when they differ, so the common case stays one line.
        self._rule(".com", "block")
        rid = self._rule(".example.com", "allow")
        with mock.patch.object(cp.store, "_audit") as audit:
            _revoke_rule(rid, _FakeRequest())
        reason = audit.call_args.kwargs["reason"]
        self.assertIn("example.com and its subdomains that no narrower rule or lease "
                      "names: blocked by rule (.com", reason)
        self.assertEqual(reason.count("blocked by rule"), 1)

    def test_the_one_clause_form_keeps_the_subdomains_qualified(self):
        """A narrower rule under the wildcard lands its hosts elsewhere, so "and its
        subdomains" unqualified would state the wrong decision for them: a revoked
        block audited "held" while `api.example.com` is allowed."""
        _set_rules([("api.example.com", "allow")])
        rid = self._rule(".example.com", "block")
        with mock.patch.object(cp.store, "_audit") as audit:
            _revoke_rule(rid, _FakeRequest())
        reason = audit.call_args.kwargs["reason"]
        self.assertIn("example.com and its subdomains that no narrower rule or lease "
                      "names: no matching rule", reason)
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "allow")

    def test_a_wildcard_whose_apex_lands_apart_names_both(self):
        self._rule("example.com", "allow")
        rid = self._rule(".example.com", "allow")
        with mock.patch.object(cp.store, "_audit") as audit:
            _revoke_rule(rid, _FakeRequest())
        reason = audit.call_args.kwargs["reason"]
        self.assertIn("example.com: allowed by rule (example.com", reason)
        self.assertIn("its subdomains that no narrower rule or lease names: no "
                      "matching rule", reason)

    def test_one_decision_for_two_reasons_is_still_two_clauses(self):
        # Apex and subdomains are both allowed, by different rules; one clause would
        # name the wrong rule for half of them.
        _set_rules([("a.example.com", "allow"), (".example.com", "allow")])
        rid = self._rule(".a.example.com", "block")
        with mock.patch.object(cp.store, "_audit") as audit:
            _revoke_rule(rid, _FakeRequest())
        reason = audit.call_args.kwargs["reason"]
        self.assertIn("a.example.com: allowed by rule (a.example.com", reason)
        self.assertIn("its subdomains that no narrower rule or lease names: allowed "
                      "by rule (.example.com", reason)

    def test_a_revoke_reports_its_own_class_not_anothers(self):
        # The same pattern is a separate rule in another class, and decides nothing
        # for this one. Both ways round, so neither class can be the one assumed.
        other = next(c for c in cp.policy._class_names() if c != CLASS)
        for mine, theirs in ((CLASS, other), (other, CLASS)):
            with self.subTest(revoked_in=mine):
                _set_rules([("evil.example", "block", theirs),
                            ("evil.example", "allow", mine)])
                with cp.store._connect() as conn:
                    rid = conn.execute("SELECT id FROM rules WHERE client_class=?",
                                       (mine,)).fetchone()["id"]
                with mock.patch.object(cp.store, "_audit") as audit:
                    _revoke_rule(rid, _FakeRequest())
                reason = audit.call_args.kwargs["reason"]
                self.assertIn(cp.policy._decide("evil.example", mine)[1], reason)
                self.assertIn(f"matched only for: {theirs}", reason)
                self.assertNotIn("blocked by rule", reason)

    def test_a_revoked_allow_stops_deciding_requests(self):
        # The whole point, asserted end to end through _decide rather than by
        # inspecting the table: policy actually changes.
        rid = self._rule("gone.example", "allow")
        self.assertEqual(cp.policy._decide("gone.example", CLASS)[0], "allow")
        _revoke_rule(rid, _FakeRequest())
        self.assertEqual(cp.policy._decide("gone.example", CLASS)[0], "hold")

    def test_a_revoked_block_reverts_to_hold_not_allow(self):
        # The loosening direction, and the reason the UI warns differently about it:
        # with no other rule, it does NOT become allowed, it becomes decidable.
        rid = self._rule("bad.example", "block")
        self.assertEqual(cp.policy._decide("bad.example", CLASS)[0], "deny")
        _revoke_rule(rid, _FakeRequest())
        self.assertEqual(cp.policy._decide("bad.example", CLASS)[0], "hold")


class PatternValidationTests(unittest.TestCase):
    """``policy._normalize_pattern`` and ``policy._rule_error`` — the validation that
    only exists because ``create_rule`` takes a pattern from a caller.

    Every other write path derives its pattern from a host the proxy observed, so
    well-formedness is a property of where the string came from. Here it has to be
    checked, and the failure being guarded against is not a crash: SQLite stores any
    text, ``_match`` compares it to nothing, and the result is a rule that appears in
    the standing-policy view while deciding no request that will ever be made."""

    def test_a_trailing_fqdn_dot_is_removed(self):
        # `_decide` strips it from the HOST before comparing, so a pattern that keeps
        # one can never match — the inert-rule case, which reads as policy in force.
        self.assertEqual(cp.policy._normalize_pattern("Example.COM."), "example.com")

    def test_a_leading_dot_survives_normalization(self):
        # It is the wildcard marker, not punctuation — which is why this cannot be the
        # `.strip('.')` a host goes through.
        self.assertEqual(cp.policy._normalize_pattern("  .Example.com  "),
                         ".example.com")

    def test_a_normalized_pattern_still_matches_the_host_it_names(self):
        # The property the two functions exist for, asserted through the matcher rather
        # than by string comparison.
        p = cp.policy._normalize_pattern(".EXAMPLE.com.")
        self.assertTrue(cp.policy._match("api.example.com", p))

    def test_every_persist_candidate_is_already_a_valid_rule(self):
        """The two write paths tied together. ``_persist_candidates`` produces patterns
        that bypass this validation entirely (a persist never calls it), so if the two
        ever disagree, one path would be storing what the other refuses — and the
        wildcard floor would be enforceable at one entrance only."""
        for host in ("example.com", "api.example.com", "a.b.c.example.com",
                     "localhost", "10.0.0.7"):
            for candidate in cp.policy._persist_candidates(host):
                with self.subTest(host=host, pattern=candidate):
                    self.assertEqual(cp.policy._normalize_pattern(candidate), candidate)
                    self.assertIsNone(cp.policy._rule_error(candidate, "allow"))

    def test_a_single_label_wildcard_is_refused_as_an_allow(self):
        # `.com` as an allow ends governance for a TLD in one call, and nothing
        # afterwards raises a hold to notice it by.
        self.assertIsNotNone(cp.policy._rule_error(".com", "allow"))

    def test_the_same_wildcard_is_permitted_as_a_block(self):
        # The asymmetry is deliberate: a block only tightens, it announces itself the
        # first time anything is denied, and it is revocable. Refusing it would make
        # the broadest blocks the ones this endpoint cannot express.
        self.assertIsNone(cp.policy._rule_error(".com", "block"))

    def test_a_two_label_wildcard_is_permitted_either_way(self):
        for action in ("allow", "block"):
            with self.subTest(action=action):
                self.assertIsNone(cp.policy._rule_error(".example.com", action))

    def test_a_malformed_pattern_is_refused(self):
        for pattern in ("", "a..b", "exa mple.com", "http://example.com",
                        "example.com/path", "*.example.com", "ex@mple.com",
                        "a" * 300):
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(
                    cp.policy._rule_error(cp.policy._normalize_pattern(pattern),
                                          "allow"))

    def test_a_newline_ending_an_inner_label_is_refused(self):
        # `$` matches before a final newline, and normalizing strips only the ends of
        # the whole pattern, so a newline ending any label but the last reaches the
        # label check. Stored, it is a rule that never matches.
        for pattern in ("example\n.com", "a.b\n.com", ".example\n.com"):
            for action in ("allow", "block"):
                with self.subTest(pattern=pattern, action=action):
                    self.assertIsNotNone(
                        cp.policy._rule_error(cp.policy._normalize_pattern(pattern),
                                              action))

    def test_an_ip_literal_is_a_valid_exact_pattern(self):
        self.assertIsNone(cp.policy._rule_error("10.0.0.7", "allow"))

    def test_a_wildcard_over_an_ip_literal_is_refused(self):
        # `.10.0.0.7` would be compared as a suffix and match nothing — inert again,
        # and it passes the label check, so it needs its own refusal.
        self.assertIsNotNone(cp.policy._rule_error(".10.0.0.7", "allow"))

    def test_an_unknown_action_is_refused(self):
        self.assertIsNotNone(cp.policy._rule_error("example.com", "hold"))

    def test_the_configured_class_names_exclude_the_unclassified_one(self):
        # A rule scoped to "whoever we could not identify" would grant to every future
        # unidentified client. `resolve` refuses it explicitly; here it is excluded by
        # construction, and this asserts that construction rather than trusting it.
        self.assertNotIn(cp.policy.UNCLASSIFIED, cp.policy._class_names())
        self.assertIn(CLASS, cp.policy._class_names())


def _create(pattern, action="allow", client_class=CLASS, request=None):
    return cp.api_egress.create_rule(
        cp.api_egress.RuleCreateRequest(pattern=pattern, action=action,
                                        client_class=client_class),
        request if request is not None else _FakeRequest())


def _shown_rule(rule_id, seen=None):
    """The ``RuleExpectation`` fields a page showing the rule as it stands sends;
    ``seen`` overrides fields of that row, for a page that is out of date."""
    with cp.store._connect() as conn:
        row = conn.execute("SELECT pattern, action, client_class FROM rules WHERE id=?",
                           (rule_id,)).fetchone()
    shown = {**(dict(row) if row else {}), **(seen or {})}
    return {"expected_pattern": shown.get("pattern"),
            "expected_action": shown.get("action"),
            "expected_client_class": shown.get("client_class")}


def _edit_rule(rule_id, pattern, action, request=None, seen=None):
    return cp.api_egress.edit_rule(
        rule_id, cp.api_egress.RuleEditRequest(pattern=pattern, action=action,
                                               **_shown_rule(rule_id, seen)),
        request if request is not None else _FakeRequest())


def _revoke_rule(rule_id, request=None, seen=None):
    return cp.api_egress.revoke_rule(
        rule_id, cp.api_egress.RuleRevokeRequest(**_shown_rule(rule_id, seen)),
        request if request is not None else _FakeRequest())


class CreateRuleTests(_CPTestCase):
    """``create_rule`` — the config-first half of policy.

    Every other rule in the store is downstream of something the agent already did:
    the seed file is a declared allowlist, and a `*_persist` approval can only write
    about a host that was requested. So an operator could answer questions and never
    state a position — pre-authorizing a registry meant letting a build block for the
    hold window first, and writing a BLOCK before anything asked for it was not
    expressible at all."""

    def _row(self, pattern, client_class=CLASS):
        with cp.store._connect() as conn:
            return conn.execute(
                "SELECT * FROM rules WHERE pattern=? AND client_class=?",
                (pattern, client_class)).fetchone()

    def test_a_rule_is_written_and_decides_immediately(self):
        # End to end through `_decide` rather than by inspecting the table: the point
        # is that policy changes, not that a row exists.
        self.assertEqual(cp.policy._decide("pypi.example", CLASS)[0], "hold")
        resp = _create("pypi.example", "allow")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(cp.policy._decide("pypi.example", CLASS)[0], "allow")

    def test_a_block_can_be_written_before_anything_asks_for_it(self):
        # The case the resolve path cannot express at all: there is no card to click,
        # because nothing has been requested.
        _create("evil.example", "block")
        self.assertEqual(cp.policy._decide("evil.example", CLASS)[0], "deny")

    def test_the_stored_rule_is_the_normalized_pattern(self):
        _create(" .Example.COM. ", "allow")
        self.assertIsNotNone(self._row(".example.com"))

    def test_the_source_is_server_set_and_not_caller_supplied(self):
        """'seed' is the value ``revoke_rule`` refuses to delete, so a caller that
        could set it could write an UNREVOCABLE rule — and one that would also stop
        ``_seed_if_empty`` from ever re-reading the file, since the table is no longer
        empty. The model has no such field; this asserts the model stays that way."""
        req = cp.api_egress.RuleCreateRequest(pattern="sneaky.example", action="allow",
                                              client_class=CLASS, source="seed")
        cp.api_egress.create_rule(req, _FakeRequest())
        self.assertEqual(self._row("sneaky.example")["source"], "operator")

    def test_an_unconfigured_client_class_is_refused(self):
        """The quiet failure this endpoint is most exposed to: a typo inserts cleanly,
        lists cleanly, and decides nothing — while the operator reads the rules view
        and believes the host is covered."""
        resp = _create("example.com", "allow", client_class="sandox")
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self._row("example.com", "sandox"))
        # And the refusal names the classes that would work, so it has a next step.
        self.assertIn(CLASS, json.dumps(resp.body))

    def test_the_unclassified_pseudo_class_is_refused(self):
        # Same refusal `resolve` makes on its persist path, and for the same reason: a
        # rule keyed to "whoever we could not identify" grants to every future one.
        resp = _create("example.com", "allow",
                       client_class=cp.policy.UNCLASSIFIED)
        self.assertEqual(resp.status_code, 400)

    def test_a_single_label_wildcard_allow_is_refused(self):
        resp = _create(".com", "allow")
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self._row(".com"))

    def test_a_malformed_pattern_is_refused_rather_than_stored_inert(self):
        resp = _create("http://example.com", "allow")
        self.assertEqual(resp.status_code, 400)
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 0)

    def test_a_newline_inside_a_label_is_refused_rather_than_stored_inert(self):
        resp = _create("example\n.com", "allow")
        self.assertEqual(resp.status_code, 400)
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 0)

    def test_the_opposite_action_is_a_conflict_not_a_replacement(self):
        """Nothing in this service replaces a rule. The dangerous direction is
        allow-over-block: the operator believes a subtree is permanently blocked, and
        every later request to it is allowed without even raising a hold."""
        _create("evil.example", "block")
        resp = _create("evil.example", "allow")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self._row("evil.example")["action"], "block")
        # And the conflict is described, so the UI can say which rule is in the way.
        self.assertEqual(resp.body["conflict"]["action"], "block")

    def test_the_conflict_names_the_fix_that_works_for_that_rule(self):
        # The UI shows `detail` verbatim, so it has to be advice the operator can take.
        # An operator rule can be edited or revoked; a seed rule can be neither here
        # (edit_rule and revoke_rule both refuse it), so its fix is the seed file.
        _create("evil.example", "block")
        detail = _create("evil.example", "allow").body["detail"]
        self.assertIn("Edit it, or revoke it first", detail)
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO rules(pattern, action, source, created_at, "
                         "client_class) VALUES ('pypi.org', 'allow', 'seed', 0, ?)",
                         (CLASS,))
            conn.commit()
        detail = _create("pypi.org", "block").body["detail"]
        self.assertIn("policies/egress-allowlist.txt", detail)
        self.assertNotIn("Edit it", detail)

    def test_the_same_rule_twice_reports_a_non_write_rather_than_an_error(self):
        # The policy asked for IS the policy — but the response must not claim a write
        # it did not make, which is the distinction `resolve` already draws.
        first = _create("example.com", "allow")
        second = _create("example.com", "allow")
        self.assertEqual(second.status_code, 200)
        body = second.body
        self.assertFalse(body["created"])
        self.assertTrue(body["already_present"])
        self.assertEqual(body["id"], first.body["id"])

    def test_the_same_pattern_in_another_class_is_a_separate_rule(self):
        # Not a conflict: uniqueness is the PAIR, and refusing here would make one
        # class's policy unwritable because another's already covered the host.
        other = next(c for c in cp.policy._class_names() if c != CLASS)
        self.assertEqual(_create("example.com", "allow").status_code, 201)
        self.assertEqual(_create("example.com", "block", client_class=other)
                         .status_code, 201)
        self.assertEqual(cp.policy._decide("example.com", CLASS)[0], "allow")
        self.assertEqual(cp.policy._decide("example.com", other)[0], "deny")

    def test_creation_is_audited_with_provenance(self):
        """Stronger version of the reason revocation is audited: this writes standing
        policy from nothing, so once the rule is in the table it looks exactly like one
        a human approved at a card. The record is the only thing that can tell them
        apart."""
        with mock.patch.object(cp.store, "_audit") as audit:
            _create(".github.example", "allow",
                    request=_FakeRequest(peer="172.31.0.9"))
        self.assertEqual(audit.call_args.args[0], "create")
        kwargs = audit.call_args.kwargs
        self.assertEqual(kwargs["host"], ".github.example")
        self.assertIn("peer=172.31.0.9", kwargs["actor"])
        # The class the rule governs is part of what was written, so the reason names
        # it; `client_class` stays empty, since no client made this row.
        self.assertIn(f"client class {CLASS}", kwargs["reason"])
        self.assertIsNone(kwargs.get("client_class"))
        # The scope, in words, because a leading dot is a wildcard that looks like a
        # hostname — the same thing the rules view spells out.
        self.assertIn("host + subdomains", kwargs["reason"])

    def test_the_reason_and_the_response_say_where_the_host_lands(self):
        """Computed from `_decide` after the write, not assumed from the action: an
        allow under a wider block is still denied, and "now allows" would record the
        opposite. The response carries the same words, for the page's notice."""
        cases = (
            ("decided by itself", [], "api.example.com", "allow"),
            ("allow under a wider block", [(".example.com", "block")],
             "api.example.com", "deny"),
        )
        for name, standing, host, decision in cases:
            with self.subTest(case=name):
                _set_rules(standing)
                with mock.patch.object(cp.store, "_audit") as audit:
                    resp = _create(host, "allow")
                reason = audit.call_args.kwargs["reason"]
                after = cp.policy._decide(host, CLASS)
                self.assertEqual(after[0], decision)
                self.assertIn(after[1], reason)
                self.assertNotIn("without being held", reason)
                self.assertEqual(resp.status_code, 201)
                self.assertIn(resp.body["landing"], reason)
                self.assertIn(after[1], resp.body["landing"])
                # The decision itself too, so the page need not parse the words.
                self.assertEqual(resp.body["landing_decisions"], [decision])

    def test_a_wildcard_create_reports_apex_and_subtree_decisions(self):
        # A narrower block under the new allow denies its own host; the reason keeps
        # the subdomains qualified, and the decisions are the apex's and the subtree's.
        _set_rules([("evil.example.com", "block"), ("example.com", "block")])
        with mock.patch.object(cp.store, "_audit") as audit:
            resp = _create(".example.com", "allow")
        reason = audit.call_args.kwargs["reason"]
        self.assertIn("its subdomains that no narrower rule or lease names: allowed "
                      "by rule (.example.com", reason)
        self.assertEqual(resp.body["landing_decisions"], ["deny", "allow"])
        self.assertEqual(cp.policy._decide("evil.example.com", CLASS)[0], "deny")

    def test_a_refused_creation_is_not_audited(self):
        # A refusal changed nothing, and a `create` row for a rule that does not exist
        # would be a decision log describing policy that was never in force.
        with mock.patch.object(cp.store, "_audit") as audit:
            _create(".com", "allow")
        audit.assert_not_called()

    def test_a_created_rule_can_be_revoked(self):
        # The round trip: what this writes is an operator rule, so the other half of
        # the governance plane can take it back.
        rid = _create("example.com", "allow").body["id"]
        self.assertEqual(
            _revoke_rule(rid, _FakeRequest()).status_code, 200)
        self.assertEqual(cp.policy._decide("example.com", CLASS)[0], "hold")


def _served(**kw):
    """The grouped rows ``/api/audit`` serves.

    The endpoint returns ``{"rows": [...], "total": n}`` — the total being what the
    view is a window onto. These tests are about the rows unless they say otherwise,
    so the unwrapping lives here rather than in forty assertions."""
    return cp.api_views.api_audit(**kw)["rows"]


class EditRuleTests(_CPTestCase):
    """``edit_rule`` — the third verb, and the one that makes the other two a plane
    rather than a pair.

    Create and revoke could express every END STATE already; what they could not
    express is a TRANSITION. Narrowing `.example.com` to `api.example.com` meant
    revoking and re-creating, which is two audit rows that each describe half of an
    intent, and a window in between where the whole subtree was unknown and every
    request under it was held one card at a time. The window failed closed, so this is
    not a hole being closed — it is an operation that was being simulated by two."""

    def _rule(self, pattern, action="allow", source="operator", client_class=CLASS):
        with cp.store._connect() as conn:
            cur = conn.execute(
                "INSERT INTO rules(pattern, action, source, created_at, client_class) "
                "VALUES (?,?,?,?,?)", (pattern, action, source, 1234.0, client_class))
            conn.commit()
            return cur.lastrowid

    def _edit(self, rule_id, pattern, action, request=None):
        return _edit_rule(rule_id, pattern, action, request)

    def test_an_action_flips_in_one_operation(self):
        # Through `_decide`, like the create tests: the assertion is that policy moved,
        # not that a column did.
        rid = self._rule("evil.example", "block")
        self.assertEqual(cp.policy._decide("evil.example", CLASS)[0], "deny")
        resp = self._edit(rid, "evil.example", "allow")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(cp.policy._decide("evil.example", CLASS)[0], "allow")

    def test_a_pattern_narrows_in_one_operation(self):
        """The motivating case. Afterwards the subtree is unknown again and the one
        host is allowed — and at no point was the rule absent."""
        rid = self._rule(".example.com", "allow")
        self._edit(rid, "api.example.com", "allow")
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "allow")
        self.assertEqual(cp.policy._decide("other.example.com", CLASS)[0], "hold")

    def test_the_rule_keeps_its_id_and_its_age(self):
        """The same rule with different terms, not a new one. ``created_at`` says when
        the policy came into force and the rules view sorts on it; the audit row is
        where the change is dated."""
        rid = self._rule("example.com", "allow")
        self._edit(rid, "api.example.com", "block")
        with cp.store._connect() as conn:
            row = conn.execute("SELECT * FROM rules WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["pattern"], "api.example.com")
        self.assertEqual(row["action"], "block")
        self.assertEqual(row["created_at"], 1234.0)
        self.assertEqual(row["source"], "operator")

    def test_the_stored_pattern_is_normalized(self):
        rid = self._rule("example.com", "allow")
        self._edit(rid, " .Example.NET. ", "allow")
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT pattern FROM rules WHERE id=?",
                             (rid,)).fetchone()["pattern"], ".example.net")

    def test_a_seed_rule_is_refused_by_the_backend(self):
        """Refused for a STRONGER reason than the revoke case. A revoked seed rule at
        least leaves, and `_seed_if_empty` re-reads the file on the next empty-table
        start; an edited one stays, deciding, while the reviewed file under version
        control says something else about the same host."""
        rid = self._rule("pypi.org", "allow", source="seed")
        resp = self._edit(rid, "evil.example", "allow")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("egress-allowlist.txt", json.dumps(resp.body))
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT pattern FROM rules WHERE id=?",
                             (rid,)).fetchone()["pattern"], "pypi.org")

    def test_an_unknown_id_is_a_404_not_a_silent_success(self):
        self.assertEqual(self._edit(999999, "example.com", "allow").status_code, 404)

    def test_a_stale_edit_does_not_land_on_the_rule_created_after_a_revoke(self):
        # The edit writes the typed pattern and action, so landing on another rule
        # would replace it wholesale: here, turn a block into an allow.
        stale = self._rule("old.example", "block")
        _revoke_rule(stale, _FakeRequest())
        fresh = self._rule("evil.example", "block")
        self.assertNotEqual(fresh, stale)
        resp = _edit_rule(stale, "evil.example", "allow", seen={
            "pattern": "old.example", "action": "block", "client_class": CLASS})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(cp.policy._decide("evil.example", CLASS)[0], "deny")

    def test_an_edit_over_another_operators_edit_is_refused(self):
        # Two pages show `evil.example` blocked; one allows it, then the other, still
        # showing the block, narrows it. Without the expectation the second write would
        # silently undo the first operator's decision.
        rid = self._rule("evil.example", "block")
        self._edit(rid, "evil.example", "allow")
        resp = _edit_rule(rid, "api.evil.example", "block", seen={"action": "block"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.body["current"]["action"], "allow")
        self.assertEqual(_rules(), {("evil.example", "allow")})

    def test_an_edit_landing_on_a_different_rule_under_the_same_id_is_refused(self):
        # The id names whatever row holds it now. Reused here by hand; the store
        # does not hand an id on, so this is the backstop for a row that reached it
        # some other way.
        # Same action on both, so only the pattern tells them apart; the stale save
        # would turn the block on evil.example into one on old.example.
        rid = self._rule("old.example", "block")
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM rules WHERE id=?", (rid,))
            conn.execute("INSERT INTO rules(id, pattern, action, source, created_at, "
                         "client_class) VALUES (?, 'evil.example', 'block', "
                         "'operator', 0, ?)", (rid, CLASS))
        resp = _edit_rule(rid, "old.example", "allow",
                          seen={"pattern": "old.example"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("evil.example (block)", resp.body["detail"])
        self.assertEqual(cp.policy._decide("evil.example", CLASS)[0], "deny")

    def test_a_write_landing_after_the_check_is_not_overwritten(self):
        # Another operator's edit commits between `_stale`'s read and this write. The
        # write's own WHERE is what notices.
        rid = self._rule("race.example", "block")

        def other_operator(rule_id, row, req):
            with cp.store._connect() as conn:
                conn.execute("UPDATE rules SET action='allow' WHERE id=?", (rule_id,))
            return None

        with mock.patch.object(cp.api_egress, "_stale", side_effect=other_operator):
            resp = self._edit(rid, "api.race.example", "block")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.body["current"]["action"], "allow")
        self.assertEqual(_rules(), {("race.example", "allow")})

    def test_the_class_is_part_of_what_the_edit_expects(self):
        rid = self._rule("example.com", "block")
        resp = _edit_rule(rid, "example.com", "allow", seen={"client_class": "mcp"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(cp.policy._decide("example.com", CLASS)[0], "deny")

    def test_a_malformed_pattern_is_refused_and_changes_nothing(self):
        rid = self._rule("example.com", "allow")
        resp = self._edit(rid, "http://evil.example", "allow")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(_rules(), {("example.com", "allow")})

    def test_a_newline_inside_a_label_is_refused_and_changes_nothing(self):
        rid = self._rule("example.com", "allow")
        resp = self._edit(rid, "example\n.com", "allow")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(_rules(), {("example.com", "allow")})

    def test_the_wildcard_floor_applies_here_too(self):
        """``policy._rule_error`` is the whole of the validation, exactly as it is for
        ``create_rule`` — there is no ``_persist_candidates`` bounded set behind an
        edit either, so an unvalidated one would be the widest input in the service."""
        rid = self._rule("example.com", "allow")
        self.assertEqual(self._edit(rid, ".com", "allow").status_code, 400)
        # Still permitted as a BLOCK, which is the asymmetry that floor encodes.
        self.assertEqual(self._edit(rid, ".com", "block").status_code, 200)

    def test_colliding_with_another_rule_is_a_conflict_not_a_merge(self):
        """``UNIQUE(pattern, client_class)`` would otherwise surface as an opaque 500,
        and silently merging two rules into one would lose whichever action lost."""
        keep = self._rule("evil.example", "block")
        rid = self._rule("other.example", "allow")
        resp = self._edit(rid, "evil.example", "allow")
        self.assertEqual(resp.status_code, 409)
        # Both rules survive the refusal, unchanged.
        self.assertEqual(_rules(), {("evil.example", "block"),
                                    ("other.example", "allow")})
        self.assertIn(str(keep), json.dumps(resp.body))

    def test_changing_only_the_action_does_not_collide_with_itself(self):
        """The bug the ``id<>?`` clause exists for: the row being edited already holds
        the target pattern, so a naive uniqueness check refuses every action flip."""
        rid = self._rule("evil.example", "block")
        self.assertEqual(self._edit(rid, "evil.example", "allow").status_code, 200)

    def test_a_rule_in_another_class_is_not_a_collision(self):
        # Uniqueness is (pattern, class), and the edit stays inside its own class.
        self._rule("evil.example", "block", client_class="mcp")
        rid = self._rule("other.example", "allow")
        self.assertEqual(self._edit(rid, "evil.example", "allow").status_code, 200)

    def test_an_edit_that_changes_nothing_reports_a_non_write(self):
        """The policy asked for IS the policy — not an error. Reported as a non-write
        so nothing confirms a change, and so the log does not gain a row saying
        standing policy moved on a request that moved nothing."""
        rid = self._rule("example.com", "allow")
        with mock.patch.object(cp.store, "_audit") as audit:
            resp = self._edit(rid, "example.com", "allow")
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.body["changed"], False)
        audit.assert_not_called()

    def test_the_edit_is_audited_once_with_both_states(self):
        """ONE row carrying the before AND the after. That is the whole difference from
        revoke-then-create, which wrote two rows that each described half of an intent
        with nothing tying them together."""
        rid = self._rule(".example.com", "allow")
        with mock.patch.object(cp.store, "_audit") as audit:
            self._edit(rid, "api.example.com", "block",
                       request=_FakeRequest(peer="172.31.0.9"))
        self.assertEqual(audit.call_count, 1)
        self.assertEqual(audit.call_args.args[0], "edit")
        reason = audit.call_args.kwargs["reason"]
        self.assertIn(".example.com", reason)          # what it was
        self.assertIn("api.example.com", reason)       # what it is
        self.assertIn("allow", reason)
        self.assertIn("block", reason)
        self.assertIn("peer=172.31.0.9", audit.call_args.kwargs["actor"])
        # `host` is the NEW pattern: it is what decides from now on.
        self.assertEqual(audit.call_args.kwargs["host"], "api.example.com")

    def test_a_refused_edit_is_not_audited(self):
        rid = self._rule("example.com", "allow")
        with mock.patch.object(cp.store, "_audit") as audit:
            self._edit(rid, ".com", "allow")
        audit.assert_not_called()

    def test_the_client_class_is_not_editable(self):
        """Not an omission. Moving a rule between classes takes policy from one
        population and gives it to another, which is two changes wearing one audit row;
        revoke-then-create says that honestly. This asserts the model stays shut, the
        same way the create tests assert ``source`` does."""
        rid = self._rule("example.com", "allow")
        req = cp.api_egress.RuleEditRequest(
            pattern="example.com", action="allow", client_class="mcp",
            expected_pattern="example.com", expected_action="allow",
            expected_client_class=CLASS)
        cp.api_egress.edit_rule(rid, req, _FakeRequest())
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT client_class FROM rules WHERE id=?",
                             (rid,)).fetchone()["client_class"], CLASS)


class AuditViewTests(_CPTestCase):
    """``/api/audit`` backs the decisions table, and had no tests at all — which is
    how it went this long selecting ``stage`` that nothing rendered while omitting
    ``client``, the one column a shared control plane most needs."""

    def _rows(self, *rows):
        """REPLACE the audit table with these rows. Not via ``_audit`` — that is mocked
        by _CPTestCase, and these tests are about what the ENDPOINT serves, not about
        what the writer records.

        Replacing rather than appending, so a test can call this more than once in a
        loop. Appending made the grouping subtests count leftovers from the previous
        iteration and read 6 where they meant 2."""
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM audit")
            for r in rows:
                conn.execute(
                    "INSERT INTO audit(ts, kind, stage, host, port, proto, "
                    "client, actor, method, url, reason) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (r.get("ts", 0.0), r.get("kind", "allow"), r.get("stage"),
                     r.get("host"), r.get("port"), r.get("proto"), r.get("client"),
                     r.get("actor"), r.get("method"), r.get("url"), r.get("reason")))
            conn.commit()

    def test_a_denial_says_whether_it_was_policy_or_an_outage(self):
        # The two are visually identical in the UI — same red `deny` tag, same host —
        # and mean opposite things: one is governance working, the other governance
        # absent with everything being refused. An evening was spent on that
        # ambiguity, hence the flag.
        self._rows(
            {"host": "a.example", "kind": "deny",
             "reason": "blocked by rule (a.example)"},
            {"host": "b.example", "kind": "deny", "ts": 1.0,
             "reason": "control-plane unreachable, fail-closed (timed out)"})
        got = {r["host"]: r["fail_closed"] for r in _served()}
        self.assertEqual(got, {"a.example": False, "b.example": True})

    def test_the_marker_matches_what_the_proxy_actually_writes(self):
        """The reason text is produced in proxies/egress/addon.py and classified in
        control-plane/api_views.py — two services, two images, no shared module.
        Nothing but this test connects them, and the drift is silent: a renamed reason
        simply stops being recognised and the row reverts to looking like a policy
        denial.

        Reads the addon's SOURCE rather than importing it, because the point is to pin
        the literal that ships in the other image."""
        addon = (ROOT / "proxies" / "egress" / "addon.py").read_text()
        produced = re.findall(r'Verdict\(False,\s*f?"([^"{]*)', addon)
        self.assertTrue(
            any(p.startswith(cp.api_views.FAIL_CLOSED_REASON) for p in produced),
            f"no fail-closed Verdict reason in addon.py starts with "
            f"{cp.api_views.FAIL_CLOSED_REASON!r}; found {produced!r}. If the wording "
            f"moved, move FAIL_CLOSED_REASON with it — otherwise outage denials go "
            f"back to being indistinguishable from policy denials in the UI.")

    def test_an_unrecognised_reason_is_not_marked_as_an_outage(self):
        # The classification must not fire on a word that appears in ordinary reasons
        # too — a host can legitimately be named after the control plane.
        self._rows({"host": "c.example", "kind": "deny",
                    "reason": "blocked by rule (control-plane-mirror.example)"})
        self.assertFalse(_served()[0]["fail_closed"])

    def test_the_marker_must_anchor_at_the_start_of_the_reason(self):
        # A substring search would also match a reason that merely MENTIONS the
        # condition rather than being one. The proxy writes the phrase as a PREFIX and
        # appends the exception, so the anchor is what distinguishes "this request was
        # refused because governance was unreachable" from any future reason that
        # refers to that state while describing something else.
        #
        # Without this the guard is decorative: `startswith` loosened to `in` passed
        # every other test in this class.
        self._rows({"host": "d.example", "kind": "deny",
                    "reason": "blocked by rule, recorded while "
                              "control-plane unreachable"})
        self.assertFalse(_served()[0]["fail_closed"])

    def test_a_row_says_whose_request_it_was(self):
        # The gap this closes. One control plane serves every sandbox, so a row that
        # records "egress to pypi.org was allowed" without saying which agent asked
        # is not answering the question an audit trail exists for.
        self._rows({"host": "pypi.org", "client": "172.30.0.135", "kind": "allow"})
        self.assertEqual(_served()[0]["client"], "172.30.0.135")

    def test_the_served_columns_are_pinned(self):
        # Named, so adding or dropping one is a deliberate act with a failing test
        # attached rather than a silent change of what the operator can see. `stage`
        # was served and rendered by nothing for as long as this endpoint existed.
        # `fail_closed` is DERIVED rather than stored — it is the one field here the
        # audit table does not hold, computed in _audit_view from the reason. Listed
        # alongside the columns anyway, because from the UI's side there is no
        # difference and this set is the contract.
        self._rows({"host": "a.example"})
        self.assertEqual(
            set(_served()[0]),
            {"ts", "kind", "stage", "host", "client", "client_class", "reason",
             "n", "first_ts", "fail_closed",
             # Who acted, shown under the client, so in the group key too: two rules
             # written by different people are two facts.
             "actor",
             # The tool columns. They are in the GLANCE — unlike url/method/port —
             # because an outcome row identifies itself with them: an egress row names
             # a host in that cell, and a tool row names `server__tool`. They are also
             # in the group key, since two `ok` outcomes for different tools are
             # different facts and would otherwise fold into one unattributable "2x".
             "server", "tool", "status"})

    def test_the_unbounded_fields_stay_out_of_the_list_view(self):
        # url is AGENT-CONTROLLED and unbounded; method/port/proto are recorded and
        # queryable but noise in a forty-row glance. They are in the table on purpose
        # and out of this response on purpose.
        self._rows({"host": "a.example", "url": "https://a.example/" + "x" * 4000,
                    "method": "GET", "port": 443, "proto": "connect"})
        served = _served()[0]
        for field in ("url", "method", "port", "proto"):
            self.assertNotIn(field, served)

    def test_newest_first(self):
        self._rows({"host": "old.example", "ts": 100.0},
                   {"host": "new.example", "ts": 200.0})
        self.assertEqual([r["host"] for r in _served()],
                         ["new.example", "old.example"])

    def test_the_limit_is_clamped_at_both_ends(self):
        self._rows(*[{"host": f"h{i}.example", "ts": float(i)} for i in range(12)])
        self.assertEqual(len(_served(limit=5)), 5)
        # Zero or negative would serve nothing and read as "no decisions"; the
        # ceiling stops one request dragging the whole unbounded table across.
        self.assertEqual(len(_served(limit=0)), 1)
        self.assertEqual(len(_served(limit=-3)), 1)
        self.assertEqual(len(_served(limit=100_000)), 12)

    def test_an_empty_table_is_an_empty_list_not_an_error(self):
        # The frontend distinguishes "nothing yet" from "the poll failed", which only
        # works if this reports the first as success.
        self.assertEqual(_served(), [])
        self.assertEqual(cp.api_views.api_audit()["total"], 0)

    def test_the_total_counts_decisions_not_rows(self):
        """The list is a window and used to say so nowhere. Grouping made that worse
        rather than better: forty rows carrying counts read like a complete picture,
        because the counts appear to explain the volume away.

        The total is RAW decisions, so it can be compared against the sum of the
        counts on screen — a total of groups would be a second number that agrees
        with the first and tells the reader nothing."""
        self._rows(*[{"host": "chatty.example", "kind": "deny",
                      "ts": 1000.0 + i} for i in range(30)])
        served = cp.api_views.api_audit()
        self.assertEqual(len(served["rows"]), 1)     # one group
        self.assertEqual(served["rows"][0]["n"], 30)
        self.assertEqual(served["total"], 30)        # thirty decisions

    def test_the_total_is_not_bounded_by_the_display_limit(self):
        # The limit shapes what is SHOWN; the total exists precisely to say how much
        # was not. A total that moved with the limit would always equal the rows and
        # could never report truncation.
        self._rows(*[{"host": f"h{i}.example", "ts": float(i)} for i in range(12)])
        self.assertEqual(len(_served(limit=3)), 3)
        self.assertEqual(cp.api_views.api_audit(limit=3)["total"], 12)

    def test_identical_decisions_collapse_to_one_row(self):
        """The case that forced this: a client retrying a host that a standing rule
        refuses, once a minute, forever. Ungrouped it writes 1440 rows a day and the
        forty-row list covers under an hour — so a fronting refusal from this morning
        is already off the bottom."""
        self._rows(*[{"host": "chatty.example", "kind": "deny", "stage": "connect",
                      "client": "172.30.0.130", "reason": "blocked by rule",
                      "ts": 1000.0 + 60 * i} for i in range(30)])
        served = _served()
        self.assertEqual(len(served), 1)
        self.assertEqual(served[0]["n"], 30)
        # The LATEST occurrence is the row's instant; the span's start is separate,
        # because a bare count cannot tell a burst from a day-long retry loop.
        self.assertEqual(served[0]["ts"], 1000.0 + 60 * 29)
        self.assertEqual(served[0]["first_ts"], 1000.0)

    def test_interleaved_repeats_still_collapse(self):
        """Two periodic sources chop each other's runs apart, which is why this groups
        by key over a window rather than collapsing consecutive rows: run-detection
        would have folded almost nothing on the very log that motivated it."""
        rows = []
        for i in range(10):
            rows.append({"host": "chatty.example", "kind": "deny", "ts": 100.0 + 2 * i})
            rows.append({"host": "api.example", "kind": "allow", "ts": 101.0 + 2 * i})
        self._rows(*rows)
        served = _served()
        self.assertEqual({r["host"]: r["n"] for r in served},
                         {"chatty.example": 10, "api.example": 10})

    def test_a_single_decision_reports_itself_as_one(self):
        # The ordinary row. n==1 is what the frontend keys "render exactly as before".
        self._rows({"host": "a.example", "ts": 5.0})
        self.assertEqual(_served()[0]["n"], 1)
        self.assertEqual(_served()[0]["first_ts"], 5.0)

    def test_the_group_key_is_exactly_what_is_displayed(self):
        """Rows that differ ONLY in a served field must stay apart, and rows that
        differ only in an unserved one must merge — otherwise the list shows entries
        a reader cannot tell apart, which is the failure being fixed."""
        base = {"host": "a.example", "kind": "deny", "stage": "connect",
                "client": "172.30.0.130", "reason": "blocked by rule"}
        for field, other in (("kind", "allow"), ("stage", "sni"),
                             ("host", "b.example"), ("client", "172.30.0.137"),
                             ("actor", "peer=172.31.0.3"),
                             ("reason", "no matching rule")):
            with self.subTest(field=field):
                self._rows(base, {**base, field: other})
                self.assertEqual(len(_served()), 2)
        # port/proto/method/url are recorded but never shown, so keying on them would
        # split one group into rows that render identically.
        self._rows({**base, "port": 443, "proto": "connect", "method": "GET"},
                   {**base, "port": 8443, "proto": "https", "method": "POST"})
        self.assertEqual(len(_served()), 1)

    def test_one_sandbox_never_absorbs_another(self):
        # Attribution is the whole reason the client column exists; folding two
        # sandboxes into one row would quietly undo it.
        self._rows({"host": "a.example", "client": "172.30.0.130"},
                   {"host": "a.example", "client": "172.30.0.131"})
        self.assertEqual({r["client"] for r in _served()},
                         {"172.30.0.130", "172.30.0.131"})

    def test_groups_are_ordered_by_their_latest_occurrence(self):
        self._rows({"host": "chatty.example", "ts": 10.0},
                   {"host": "chatty.example", "ts": 20.0},
                   {"host": "quiet.example", "ts": 15.0})
        self.assertEqual([r["host"] for r in _served()],
                         ["chatty.example", "quiet.example"])

    def test_the_scan_bounds_the_rows_read_and_the_limit_bounds_the_groups_served(self):
        """Two different bounds doing two different jobs, and swapping them is a real
        mistake with no symptom at the default settings — the scan would collapse to
        forty rows while the response grew to five thousand groups.

        Distinguishing them needs a fixture where the two answers differ: the newest
        ten events are all ONE host, behind thirty distinct older ones. Reading the
        scan window and grouping it gives ONE row; reading everything and truncating
        the groups gives ten. A first version of this test used forty distinct hosts,
        where both orderings happen to return the same ten rows, and the swap survived
        it."""
        cp.api_views.AUDIT_GROUP_SCAN = 10
        try:
            self._rows(*([{"host": f"old{i}.example", "ts": float(i)}
                          for i in range(30)]
                         + [{"host": "chatty.example", "ts": 30.0 + i}
                            for i in range(10)]))
            served = _served(limit=50)
            self.assertEqual(len(served), 1)
            self.assertEqual(served[0]["host"], "chatty.example")
            # The count is over the SCAN WINDOW, which is the honest claim: this view
            # summarises recent events, not the whole table.
            self.assertEqual(served[0]["n"], 10)
        finally:
            cp.api_views.AUDIT_GROUP_SCAN = 5000

    def test_rows_older_than_the_scan_are_kept_but_not_shown(self):
        # Cost has to be fixed as the table grows, since this is polled every few
        # seconds against a table that only ever gets longer. The trade is stated
        # rather than hidden: the record keeps everything, this view does not.
        cp.api_views.AUDIT_GROUP_SCAN = 5
        try:
            self._rows(*[{"host": f"h{i}.example", "ts": float(i)}
                         for i in range(20)])
            self.assertEqual([r["host"] for r in _served()],
                             [f"h{i}.example" for i in range(19, 14, -1)])
            with cp.store._connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 20)
        finally:
            cp.api_views.AUDIT_GROUP_SCAN = 5000


def _write_audit(*rows):
    """REPLACE the audit table with these rows, every column writable.

    A second fixture beside ``AuditViewTests._rows`` rather than a widening of it: the
    filter and record tests are about `client_class` and `url`, which that one does not
    write, and the grouping tests depend on their rows being exactly what they say."""
    with cp.store._connect() as conn:
        conn.execute("DELETE FROM audit")
        for r in rows:
            conn.execute(
                "INSERT INTO audit(ts, kind, stage, host, port, proto, client, "
                "client_class, actor, method, url, reason, server, tool, approval_id, "
                "status, result_bytes, result_sha256, duration) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.get("ts", 0.0), r.get("kind", "allow"), r.get("stage"),
                 r.get("host"), r.get("port"), r.get("proto"), r.get("client"),
                 r.get("client_class"), r.get("actor"), r.get("method"), r.get("url"),
                 r.get("reason"),
                 r.get("server"), r.get("tool"), r.get("approval_id"),
                 r.get("status"), r.get("result_bytes"), r.get("result_sha256"),
                 r.get("duration")))
        conn.commit()


class AuditFilterTests(_CPTestCase):
    """Filters on the folded view. What they are FOR is that the audit trail became
    unreadable long before it became large: a chatty client's retries push everything
    else off a forty-row list, and until now the only way past that was `sqlite3`
    against the crown-jewel volume.

    Every refusal here matters more than the matches. A filter that is silently ignored
    answers a different question than the one asked — widened, it reports decisions the
    reader excluded; narrowed, it reports an empty record as a quiet system — and
    neither is visible on screen."""

    def _hosts(self, **kw):
        return [r["host"] for r in cp.api_views.api_audit(**kw)["rows"]]

    def setUp(self):
        super().setUp()
        _write_audit(
            {"host": "pypi.org", "kind": "allow", "client": "172.30.0.130",
             "client_class": "sandbox", "reason": "allowed by rule (pypi.org)",
             "url": "https://pypi.org/simple/", "method": "GET", "ts": 100.0},
            {"host": "evil.example", "kind": "deny", "client": "172.30.0.130",
             "client_class": "sandbox", "reason": "blocked by rule", "ts": 200.0},
            {"host": "slack.com", "kind": "hold", "client": "172.28.0.5",
             "client_class": "mcp", "reason": "held for approval", "ts": 300.0})

    def test_search_matches_the_host(self):
        self.assertEqual(self._hosts(q="evil"), ["evil.example"])

    def test_search_matches_the_client_and_its_class(self):
        # The class is the useful one: "what has the MCP population been doing" is a
        # question about a population, and the address is only how it was worked out.
        self.assertEqual(self._hosts(q="mcp"), ["slack.com"])
        self.assertEqual(self._hosts(q="172.28"), ["slack.com"])

    def test_search_matches_the_reason(self):
        self.assertEqual(self._hosts(q="blocked by rule"), ["evil.example"])

    def test_search_matches_the_actor_in_both_views(self):
        # "What did this operator do" is a question about the actor column, and
        # both views show it. The User-Agent is matched too: the cell's title holds it.
        _write_audit({"host": ".github.com", "kind": "create", "stage": "policy",
                      "actor": 'peer=172.31.0.3 via-ui=10.1.2.3 ua="curl/8.5.0"',
                      "ts": 1.0},
                     {"host": "pypi.org", "client": "172.30.0.130", "ts": 2.0})
        for q in ("via-ui=10.1.2.3", "curl"):
            with self.subTest(q=q):
                self.assertEqual(self._hosts(q=q), [".github.com"])
                self.assertEqual(
                    [r["host"] for r in cp.api_views.api_audit_events(q=q)["rows"]],
                    [".github.com"])

    def test_search_is_case_insensitive(self):
        self.assertEqual(self._hosts(q="EVIL"), ["evil.example"])

    def test_search_does_not_read_the_column_the_view_does_not_show(self):
        """The rule is that a view searches exactly what it DISPLAYS. `url` is not in
        the folded row, so a match on it would put a row on screen whose visible
        content does not contain what was typed, with nothing to explain why — the
        same failure the group key avoids by keying on the displayed fields."""
        self.assertEqual(self._hosts(q="simple"), [])
        # And it IS searchable where it is shown.
        self.assertEqual(
            [r["host"] for r in cp.api_views.api_audit_events(q="simple")["rows"]],
            ["pypi.org"])

    def test_the_record_finds_an_outcome_by_the_approval_id_it_shows(self):
        # The record view shows an outcome row's approval id, so it must search it:
        # an id copied off one row is how an operator pulls up the rest of the story.
        # The glance shows no id, so there it must not match.
        _write_audit(
            {"kind": "outcome", "stage": "tool-result", "server": "mcp-github",
             "tool": "create_pull_request", "status": "ok",
             "approval_id": "d2e24cc51df24451bbed5591d99e33c1", "ts": 1.0},
            {"kind": "outcome", "stage": "tool-result", "server": "mcp-github",
             "tool": "create_pull_request", "status": "ok",
             "approval_id": "1ad86183797045d5941a675492d168e0", "ts": 2.0})
        rows = cp.api_views.api_audit_events(q="d2e24cc5")["rows"]
        self.assertEqual([r["approval_id"] for r in rows],
                         ["d2e24cc51df24451bbed5591d99e33c1"])
        self.assertEqual(cp.api_views.api_audit(q="d2e24cc5")["rows"], [])

    def test_search_is_a_literal_substring_not_a_like_pattern(self):
        # Unescaped, `%` matches every row and `_` matches any character — so the
        # search box would stop meaning "contains this text" for exactly the inputs an
        # operator pastes out of a URL or a rule pattern.
        self.assertEqual(self._hosts(q="%"), [])
        self.assertEqual(self._hosts(q="_"), [])
        _write_audit({"host": "100%.example", "ts": 1.0},
                     {"host": "plain.example", "ts": 2.0})
        self.assertEqual(self._hosts(q="100%"), ["100%.example"])

    def test_search_finds_a_row_with_no_client_recorded(self):
        # `NULL LIKE x` is NULL, not false, so an un-COALESCEd OR drops rows that
        # match on another column. A row with no client must still be findable by host.
        _write_audit({"host": "lonely.example", "ts": 1.0})
        self.assertEqual(self._hosts(q="lonely"), ["lonely.example"])

    def test_the_decision_facet_narrows_to_one_kind(self):
        self.assertEqual(self._hosts(kind="deny"), ["evil.example"])

    def test_the_decision_facet_takes_a_set(self):
        # "everything that was refused or is waiting" is one question, and a
        # single-valued facet would make the UI merge two responses to answer it.
        self.assertEqual(self._hosts(kind="deny,hold"),
                         ["slack.com", "evil.example"])

    def test_an_unknown_decision_is_refused_rather_than_matching_nothing(self):
        resp = cp.api_views.api_audit(kind="allowed")
        self.assertEqual(resp.status_code, 400)
        # And it says what the words are, so the refusal has a next step.
        self.assertIn("allow", json.dumps(resp.body))

    def test_every_word_the_control_plane_writes_is_filterable(self):
        """The vocabulary and the writers are in different files with no compiler
        between them: a new decision word would be recorded, rendered, and quietly
        unfilterable — its facet a 400. Read from the SOURCE rather than exercised,
        because the point is the set of literals that ship."""
        written = set(re.findall(r'store\._audit\(\s*"([a-z]+)"', _HANDLER_SOURCE))
        self.assertTrue(written, "no literal audit decisions found in the handlers")
        self.assertLessEqual(written, set(cp.audit.KINDS))
        # The ingest is the other writer, and it validates against its own tuple —
        # so that tuple is the second half of the vocabulary.
        ingested = re.search(r'rec\.get\("decision"\) not in \(([^)]*)\)',
                             (ROOT / "control-plane" / "ingest.py").read_text())
        self.assertIsNotNone(ingested, "the ingest's decision guard moved")
        self.assertLessEqual(set(re.findall(r'"([a-z]+)"', ingested.group(1))),
                             set(cp.audit.KINDS))

    def test_no_caller_facing_error_interpolates_an_underlying_exception(self):
        """Two exception types are served VERBATIM to a caller — ``audit.FilterError``
        and ``inventory.InventoryError`` — and both carry the same contract in their
        docstrings: interpolate the caller's own parameters and this module's
        constants, nothing else. A code scanning rule flags the ``str(exc)`` on the
        receiving end from the shape alone, and that contract is the entire reason it
        is a false positive rather than a finding.

        Until now the contract was PROSE, which is how a bare ``except ValueError``
        got written next to one of them. This checks the half a machine can see: no
        raise site may interpolate an underlying exception, which is the one that turns
        a validation sentence into a disclosure."""
        leaked = []
        for module, name in (("audit", "FilterError"), ("inventory", "InventoryError")):
            source = (ROOT / "control-plane" / f"{module}.py").read_text()
            for raise_site in re.findall(rf"raise {name}\((.*?)\)\n", source, re.S):
                if re.search(r"\{\s*(exc|err|e)\b|str\(\s*(exc|err|e)\b", raise_site):
                    leaked.append(f"{module}.{name}: {raise_site.strip()[:80]}")
        self.assertEqual(leaked, [], "these raise sites put an underlying exception "
                                     "into a message served verbatim to a caller")

    def test_an_exception_reaches_a_response_only_through_a_named_type(self):
        """The receiving half of the same contract. Serving ``str(exc)`` is safe only
        because the exception is one of ours, raised to be read; catching a bare
        ``ValueError`` widens that to anything the call happened to raise, which is
        exactly what a code scanning rule means by information exposure.

        Bare ``except ValueError`` is legitimate elsewhere in this package — parsing a
        CIDR, a timestamp — so the rule is not "never catch it". It is that a handler
        which RELAYS the message may not. Written after doing it: the inventory
        endpoint shipped with a bare catch, and CodeQL found it before review did."""
        offenders = []
        for match in re.finditer(r"except ([\w.]+) as exc:\n", _HANDLER_SOURCE):
            after = _HANDLER_SOURCE[match.end():match.end() + 900]
            # The handler body ends at the next line that is not indented into it.
            body = after.split("\n    def ")[0]
            if "str(exc)" in body and match.group(1) in ("ValueError", "Exception",
                                                         "BaseException"):
                offenders.append(match.group(1))
        self.assertEqual(offenders, [],
                         "a handler relays str(exc) from a catch-all, so an unexpected "
                         "exception's text would be served to the caller")

    def test_the_page_offers_exactly_the_words_the_backend_knows(self):
        # The third end of the same coupling: an <option> the backend does not know is
        # a 400 on click, and a word missing from the page is a filter no operator can
        # reach. Same shape as the fail-closed marker test above — read the file that
        # ships, do not restate its contents.
        section = re.search(r'<select id="audit-kind".*?</select>',
                            (ROOT / "control-plane-ui" / "index.html").read_text(),
                            re.S)
        self.assertIsNotNone(section, "the kind facet moved in index.html")
        offered = [v for v in re.findall(r'value="([^"]*)"', section.group(0)) if v]
        self.assertEqual(sorted(offered), sorted(cp.audit.KINDS))

    def test_the_time_window_is_half_open(self):
        # `since` inclusive, `until` exclusive, which is what makes two adjacent
        # windows partition the record instead of both claiming the instant between.
        self.assertEqual(self._hosts(since=200.0), ["slack.com", "evil.example"])
        self.assertEqual(self._hosts(until=200.0), ["pypi.org"])

    def test_an_inverted_window_is_refused_not_answered_with_nothing(self):
        self.assertEqual(
            cp.api_views.api_audit(since=300.0, until=100.0).status_code, 400)

    def test_an_unusable_time_bound_is_refused(self):
        # NaN is the one worth naming: every comparison against it is false, so it
        # would answer "nothing happened" for a store full of decisions.
        for bad in (float("nan"), float("inf"), "yesterday"):
            with self.subTest(since=bad):
                self.assertEqual(cp.api_views.api_audit(since=bad).status_code, 400)

    def test_the_total_follows_the_filter(self):
        # The total exists to say how much was NOT shown. Measured against the whole
        # table, a complete filtered view would report itself as truncated — on every
        # filtered query.
        self.assertEqual(cp.api_views.api_audit()["total"], 3)
        self.assertEqual(cp.api_views.api_audit(kind="deny")["total"], 1)

    def test_the_response_says_whether_it_filtered(self):
        # The one thing the browser cannot work out for itself: it knows what it sent,
        # not whether this backend understood it. Without it the coverage line would
        # say "matching" against an older backend that ignored the parameters.
        self.assertFalse(cp.api_views.api_audit()["filtered"])
        self.assertTrue(cp.api_views.api_audit(q="evil")["filtered"])
        # Whitespace is not a filter, and treating it as one would make an accidental
        # space in the box relabel the whole view.
        self.assertFalse(cp.api_views.api_audit(q="   ")["filtered"])

    def test_no_filter_value_ever_reaches_the_sql_text(self):
        """The property the whole filter design rests on, asserted DIRECTLY rather than
        argued from the code: the SQL text a filter becomes is independent of the
        values in it. Every value is a bound parameter, every clause is a literal
        assembled here, and the only interpolation is column names from module
        constants.

        Worth a test of its own because the three query builders carry `noqa: S608`
        suppressions — ruff will not warn again on those lines, so what stops an
        injected string reaching the text has to be something that fails loudly."""
        hostile = "x' OR 1=1 --"
        loud = cp.audit.parse(q=hostile, kind="deny,hold", since=1, until=2)
        plain = cp.audit.parse(q="ordinary", kind="deny,hold", since=3, until=4)
        self.assertEqual(loud.where, plain.where,
                         "the WHERE text changed with the VALUES — something is being "
                         "interpolated that must be bound")
        self.assertNotIn("1=1", loud.where)
        self.assertIn(f"%{hostile}%", loud.params)
        # And the same for the paging cursor, which joins its own clause on.
        self.assertEqual(loud.and_("(id < ?)", 5).where, plain.and_("(id < ?)", 9).where)

    def test_a_classic_payload_is_a_search_term_and_nothing_else(self):
        # End to end through the endpoint, because the unit above proves the text is
        # value-independent and this proves the request path actually goes through it:
        # the payload matches no host, and the table is still there afterwards.
        _write_audit({"host": "a.example", "ts": 1.0}, {"host": "b.example", "ts": 2.0})
        for payload in ("x' OR '1'='1", "'; DROP TABLE audit; --",
                        "1); DELETE FROM rules; --", "\\", "%' --"):
            with self.subTest(payload=payload):
                body = cp.api_views.api_audit(q=payload)
                self.assertEqual(body["rows"], [])
                self.assertEqual(cp.api_views.api_audit_events(q=payload)["rows"], [])
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 2)

    def test_the_escape_character_is_itself_escaped_first(self):
        # Order matters: escaping `%` before `\` would leave the escape marker the
        # first pass introduced looking like an operator's own backslash, and the
        # needle would stop matching the text they typed.
        _write_audit({"host": "back\\slash.example", "ts": 1.0},
                     {"host": "plain.example", "ts": 2.0})
        self.assertEqual(self._hosts(q="back\\slash"), ["back\\slash.example"])

    def test_an_overlong_search_is_refused(self):
        self.assertEqual(
            cp.api_views.api_audit(q="x" * (cp.audit.Q_MAX + 1)).status_code, 400)

    def test_filtering_happens_before_the_fold_so_the_scan_reaches_back(self):
        """The property that makes filtering worth having on a bounded window: the scan
        counts MATCHING events, not events. Unfiltered, a chatty host fills the window
        and everything older is invisible — which is the exact complaint that motivated
        grouping, one level up."""
        cp.api_views.AUDIT_GROUP_SCAN = 10
        try:
            _write_audit(*([{"host": "quiet.example", "kind": "deny", "ts": 1.0}]
                           + [{"host": "chatty.example", "ts": 10.0 + i}
                              for i in range(50)]))
            # Unfiltered, the window is all chatter.
            self.assertEqual(self._hosts(), ["chatty.example"])
            # Filtered, it reaches the one row that matters.
            self.assertEqual(self._hosts(q="quiet"), ["quiet.example"])
        finally:
            cp.api_views.AUDIT_GROUP_SCAN = 5000


class AuditRecordTests(_CPTestCase):
    """``/api/audit/events`` — the record itself, one row per decision, paged.

    The glance answers *what is happening*; this answers *what happened*. It is the
    half that makes "the audit log is the artifact this design exists to keep
    trustworthy" a claim an operator can check from the interface rather than from
    `docker compose exec` and SQL against the volume."""

    def _rows(self, **kw):
        return cp.api_views.api_audit_events(**kw)["rows"]

    def test_the_dropped_columns_are_here(self):
        # The glance omits these on purpose (a forty-row list must stay legible, and
        # `url` is agent-controlled and unbounded); this view cannot answer the
        # question it is for without them.
        _write_audit({"host": "a.example", "method": "GET", "port": 443,
                      "proto": "connect", "url": "https://a.example/x", "ts": 1.0})
        row = self._rows()[0]
        self.assertEqual(
            set(row),
            {"id", "ts", "kind", "stage", "host", "port", "proto", "client",
             "client_class", "actor", "method", "url", "reason", "fail_closed",
             # The tool columns. Egress rows carry NULL in `server` and `tool` — the
             # identity of an egress decision is host/port/url — and `approval_id` only
             # on a held request's rows. They are here because this is the view that
             # answers "which one was it" for a TOOL row, whose identity is in none of
             # the columns above.
             "server", "tool", "approval_id",
             # And how it ended, for the tool rows the gateway's stream fills, with
             # what it returned as the gateway measured it.
             "status", "result_bytes", "result_sha256", "duration"})
        self.assertEqual(row["url"], "https://a.example/x")

    def test_a_tool_outcome_serves_its_result_facts(self):
        _write_audit({"kind": "outcome", "stage": "tool-result", "server": "mcp-github",
                      "tool": "get_pr", "status": "ok", "result_bytes": 42,
                      "result_sha256": "ab" * 32, "duration": 1.5, "ts": 1.0})
        row = self._rows()[0]
        self.assertEqual((row["result_bytes"], row["result_sha256"], row["duration"]),
                         (42, "ab" * 32, 1.5))

    def test_nothing_is_folded(self):
        # The whole difference from the glance: identical decisions are separate
        # facts here, because this is the record and folding is a property of a view.
        _write_audit(*[{"host": "chatty.example", "kind": "deny", "ts": 1.0 + i}
                       for i in range(5)])
        self.assertEqual(len(self._rows()), 5)
        self.assertNotIn("n", self._rows()[0])

    def test_newest_first_with_the_id_breaking_ties(self):
        # Two rows can share a timestamp — one request writes a `hold` and then its
        # outcome, and time.time() need not advance between them — so the order has to
        # be total or a page boundary drops or repeats a row.
        _write_audit({"host": "first.example", "ts": 5.0},
                     {"host": "second.example", "ts": 5.0},
                     {"host": "older.example", "ts": 4.0})
        self.assertEqual([r["host"] for r in self._rows()],
                         ["second.example", "first.example", "older.example"])

    def test_a_denial_still_says_whether_it_was_policy_or_an_outage(self):
        # Same classification as the glance, from the same helper — an outage denial
        # must not read as policy in the view an incident is reconstructed from.
        _write_audit({"host": "b.example", "kind": "deny", "ts": 1.0,
                      "reason": "control-plane unreachable, fail-closed (timed out)"})
        self.assertTrue(self._rows()[0]["fail_closed"])

    def test_paging_walks_the_whole_record_exactly_once(self):
        """The property worth asserting, rather than the mechanics: page through to the
        end and the pages concatenated must equal the ordered record — no gap, no
        repeat. That is what an offset would break under a concurrent insert, and what
        a ts-only cursor would break on a tie."""
        _write_audit(*[{"host": f"h{i}.example", "ts": float(i // 2)}
                       for i in range(20)])          # deliberate ts ties, pairwise
        walked, cursor, pages = [], None, 0
        while True:
            body = cp.api_views.api_audit_events(limit=3, before=cursor)
            walked.extend(r["host"] for r in body["rows"])
            pages += 1
            cursor = body["next"]
            if not cursor:
                break
            self.assertLess(pages, 20, "paging did not terminate")
        self.assertEqual(walked, [r["host"] for r in self._rows(limit=100)])
        self.assertEqual(len(walked), 20)
        self.assertEqual(len(set(walked)), 20)

    def test_the_last_page_reports_no_next(self):
        # How the pager knows to stop. A cursor that always came back would offer an
        # "older" that lands on an empty page.
        _write_audit(*[{"host": f"h{i}.example", "ts": float(i)} for i in range(3)])
        self.assertIsNone(cp.api_views.api_audit_events(limit=3)["next"])
        self.assertIsNotNone(cp.api_views.api_audit_events(limit=2)["next"])

    def test_the_total_does_not_move_while_paging(self):
        # The cursor narrows the query but never the total: a page counter that walked
        # down to zero would read as the record shrinking as it was examined.
        _write_audit(*[{"host": f"h{i}.example", "ts": float(i)} for i in range(10)])
        first = cp.api_views.api_audit_events(limit=4)
        second = cp.api_views.api_audit_events(limit=4, before=first["next"])
        self.assertEqual(first["total"], 10)
        self.assertEqual(second["total"], 10)

    def test_the_filter_survives_paging_and_bounds_the_total(self):
        _write_audit(*[{"host": f"h{i}.example", "ts": float(i),
                        "kind": "deny" if i % 2 else "allow"} for i in range(10)])
        first = cp.api_views.api_audit_events(limit=2, kind="deny")
        second = cp.api_views.api_audit_events(limit=2, kind="deny",
                                               before=first["next"])
        self.assertEqual(first["total"], 5)
        self.assertEqual(second["total"], 5)
        self.assertTrue(all(r["kind"] == "deny"
                            for r in first["rows"] + second["rows"]))

    def test_a_malformed_cursor_is_refused_not_treated_as_the_first_page(self):
        # Serving page 1 while the pager says page 12 is the worst available answer:
        # it looks like data, not like an error.
        # `nan` and `inf` are here because they PARSE: every other string on this list
        # fails float() and would be refused by any implementation, while these two
        # reach the SQL and compare wrongly instead — NaN as an empty page reporting
        # itself as the end of the record, inf as page 1. See audit._finite.
        for bad in ("nonsense", "", ":", "abc:def", "1.0:x",
                    "nan:1", "inf:1", "-inf:1", "1e400:1"):
            with self.subTest(cursor=bad):
                resp = cp.api_views.api_audit_events(before=bad)
                if bad == "":                      # absent, not malformed
                    self.assertIn("rows", resp)
                else:
                    self.assertEqual(resp.status_code, 400)

    def test_the_page_size_is_clamped_at_both_ends(self):
        _write_audit(*[{"host": f"h{i}.example", "ts": float(i)} for i in range(5)])
        self.assertEqual(len(self._rows(limit=0)), 1)
        self.assertEqual(len(self._rows(limit=-9)), 1)
        # The ceiling bounds the response, which carries `url` on every row.
        self.assertLessEqual(len(self._rows(limit=100_000)),
                             cp.audit.EVENTS_LIMIT_MAX)

    def test_the_page_never_asks_for_more_rows_than_this_view_serves(self):
        """The pager's row numbers are arithmetic on the page size the FRONTEND asked
        for ("decisions 101 to 200"), so a ceiling below that request makes every label
        wrong by the difference — silently, because the rows themselves are correct.
        Two ends, one file each, no compiler between them."""
        js = (ROOT / "control-plane-ui" / "audit.js").read_text()
        asked = re.search(r"const EVENTS_LIMIT = (\d+);", js)
        self.assertIsNotNone(asked, "EVENTS_LIMIT moved in audit.js")
        self.assertLessEqual(int(asked.group(1)), cp.audit.EVENTS_LIMIT_MAX,
                             "the page asks for more rows than the backend will serve, "
                             "so the pager's row numbers overstate every page")

    def test_an_empty_record_is_an_empty_page_not_an_error(self):
        body = cp.api_views.api_audit_events()
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["total"], 0)
        self.assertIsNone(body["next"])


class PersistCandidateTests(unittest.TestCase):
    """``_persist_candidates`` is what turns the persisted pattern from an
    AGENT-CONTROLLED STRING into an operator choice from a bounded set.

    The old ``resolve`` stored the requested host verbatim, and a leading dot is a
    subdomain wildcard — so a host spelled ``.example.com`` persisted a rule covering
    every subdomain of example.com, permanently (nothing revokes a rule)."""

    def test_the_narrowest_choice_is_first_because_it_is_the_default(self):
        self.assertEqual(
            cp.policy._persist_candidates("a.b.example.com"),
            ["a.b.example.com", ".a.b.example.com", ".example.com"])

    def test_a_two_label_host_offers_only_itself_and_its_subtree(self):
        self.assertEqual(cp.policy._persist_candidates("example.com"),
                         ["example.com", ".example.com"])

    def test_no_candidate_is_ever_a_single_label_wildcard(self):
        # `.com` as a standing allow rule would end governance for the whole TLD, and
        # `.localhost` is the same mistake one label down.
        for host in ("example.com", "a.b.example.com", "localhost", "example.co.uk"):
            for pattern in cp.policy._persist_candidates(host):
                labels = pattern.lstrip(".").split(".")
                if pattern.startswith("."):
                    self.assertGreaterEqual(
                        len(labels), 2, f"{host} offers one-label wildcard {pattern}")
        # A bare name has no subtree to offer at all.
        self.assertEqual(cp.policy._persist_candidates("localhost"), ["localhost"])

    def test_an_ip_literal_gets_no_wildcard(self):
        for host in ("1.2.3.4", "::1", "[::1]"):
            self.assertEqual(
                len(cp.policy._persist_candidates(host)), 1,
                f"{host} has no subdomains — `.1.2.3.4` would be a nonsense rule")

    def test_a_host_the_agent_spelled_as_a_wildcard_cannot_stay_one(self):
        # THE case this function exists for: the leading dot is normalized away, so the
        # exact-host default is an exact host and the wildcard is only ever reachable by
        # an operator picking it.
        self.assertEqual(cp.policy._persist_candidates(".example.com")[0], "example.com")
        self.assertEqual(cp.policy._persist_candidates(".example.com"),
                         cp.policy._persist_candidates("example.com"))

    def test_case_and_a_trailing_fqdn_dot_are_normalized(self):
        self.assertEqual(cp.policy._persist_candidates("EXAMPLE.com."),
                         ["example.com", ".example.com"])

    def test_a_malformed_host_gets_no_invented_wildcards(self):
        self.assertEqual(cp.policy._persist_candidates("a..b"), ["a..b"])
        self.assertEqual(cp.policy._persist_candidates(""), [])
        self.assertEqual(cp.policy._persist_candidates("."), [])

    def test_every_candidate_actually_matches_the_host_it_came_from(self):
        # The property that makes the set safe to offer: picking any of them grants at
        # least this request. Asserted against `_match`, the matcher they are derived
        # from, so a change to either side has to keep them consistent.
        for host in ("example.com", "a.b.example.com", "raw.githubusercontent.com",
                     "localhost", "1.2.3.4"):
            for pattern in cp.policy._persist_candidates(host):
                self.assertTrue(cp.policy._match(host, pattern),
                                f"{pattern} would not even match {host}")

    def test_pending_approvals_carry_the_patterns_resolve_will_accept(self):
        # Sent WITH the approval so the UI cannot offer a pattern the backend rejects.
        # One definition of the set, next to the matcher — not a second one in JS.
        cp.store._init_db()
        _clear_all()
        _hold("api.example.com", "opts-1")
        (row,) = cp.holds._list_pending()
        self.assertEqual([o["pattern"] for o in row["persist_options"]],
                         cp.policy._persist_candidates("api.example.com"))
        # Each carries the scope in words, since the dot is easy to miss.
        self.assertEqual(row["persist_options"][0]["scope"], "exact host")
        self.assertEqual(row["persist_options"][1]["scope"], "host + subdomains")


class PersistPatternTests(_CPTestCase):
    """The resolve side of the same thing: which pattern actually gets written."""

    def test_persist_without_a_pattern_writes_the_exact_host(self):
        _resolve(_hold("plain.example.com"), "allow_persist")
        self.assertEqual(_rules(), {("plain.example.com", "allow")})

    def test_an_operator_chosen_wildcard_is_written_as_chosen(self):
        resp = _resolve(_hold("api.example.com"), "allow_persist",
                        pattern=".example.com")
        self.assertTrue(resp.args[0]["ok"])
        self.assertEqual(_rules(), {(".example.com", "allow")})
        # And it does what the scope label says: subdomains skip the hold now.
        self.assertEqual(cp.policy._decide("other.example.com", CLASS)[0], "allow")

    def test_deny_persist_writes_a_block_rule_for_the_chosen_pattern(self):
        _resolve(_hold("bad.example.com"), "deny_persist", pattern=".example.com")
        self.assertEqual(_rules(), {(".example.com", "block")})
        self.assertEqual(cp.policy._decide("anything.example.com", CLASS)[0], "deny")

    def test_the_response_names_the_pattern_that_was_stored(self):
        # So the UI reports what was WRITTEN, not what was clicked — and names the
        # string an operator would have to go and delete by hand.
        resp = _resolve(_hold("named.example.com"), "allow_persist")
        self.assertEqual(resp.args[0]["pattern"], "named.example.com")
        resp = _resolve(_hold("named2.example.com", "hold-2"), "allow_persist",
                        pattern=".example.com")
        self.assertEqual(resp.args[0]["pattern"], ".example.com")

    def test_a_once_action_reports_no_pattern_and_writes_none(self):
        resp = _resolve(_hold("once.example.com"), "allow_once")
        self.assertIsNone(resp.args[0]["pattern"])
        self.assertEqual(_rules(), set())

    def test_a_pattern_outside_the_candidate_set_is_refused(self):
        # The whole point of validating server-side: the caller picks FROM the set, it
        # does not supply it. A relay or a scripted POST gets the same answer the UI
        # would.
        for bogus in (".com", "evil.com", ".evil.com", "example.co",
                      ".b.example.com"):
            approval = _hold("a.example.com", f"bogus-{bogus}")
            resp = _resolve(approval, "allow_persist", pattern=bogus)
            self.assertEqual(resp.kwargs.get("status_code"), 400,
                             f"{bogus} must not be persistable")
            self.assertEqual(_rules(), set(), f"{bogus} wrote a rule anyway")

    def test_a_refused_pattern_leaves_the_hold_pending_and_decidable(self):
        # Refused BEFORE the UPDATE, so a rejected pattern must not half-apply: the
        # operator gets to choose again rather than losing the approval to a 409.
        approval = _hold("retry.example.com")
        resp = _resolve(approval, "allow_persist", pattern=".com")
        self.assertEqual(resp.kwargs.get("status_code"), 400)
        self.assertEqual([r["id"] for r in cp.holds._list_pending()], [approval])
        # And the retry works.
        resp = _resolve(approval, "allow_persist")
        self.assertTrue(resp.args[0]["ok"])
        self.assertEqual(_rules(), {("retry.example.com", "allow")})

    def test_a_host_that_yields_no_pattern_refuses_rather_than_guessing(self):
        approval = _hold(".", "dotty")
        resp = _resolve(approval, "allow_persist")
        self.assertEqual(resp.kwargs.get("status_code"), 400)
        self.assertEqual(_rules(), set())

    def test_the_stored_pattern_is_normalized_not_taken_verbatim(self):
        # A host the agent spelled with a leading dot must not become a wildcard rule.
        _resolve(_hold(".sneaky.example.com"), "allow_persist")
        self.assertEqual(_rules(), {("sneaky.example.com", "allow")})
        # Which is a strictly narrower grant: the subtree still holds.
        self.assertEqual(cp.policy._decide("x.sneaky.example.com", CLASS)[0], "hold")


class ProvenanceTests(_CPTestCase):
    """Resolving a hold is the one privileged action here — it grants egress — so
    "who approved this" must reach the durable record and the audit trail. It reached
    NEITHER before: an operator's click and a scripted POST were indistinguishable
    once written. Detection, not prevention (the self-reported fields are forgeable
    by a host-local caller), so what is asserted is that the trace exists and keeps
    observed and asserted values apart."""

    def test_actor_separates_observed_peer_from_self_reported_fields(self):
        actor = cp.provenance._actor(_FakeRequest(peer="172.31.0.3", headers={
            cp.provenance.ACTOR_HEADER: "127.0.0.1",
            "origin": "http://127.0.0.1:28090",
            "user-agent": "Mozilla/5.0 (X11)"}))
        self.assertIn("peer=172.31.0.3", actor)      # observed by us
        self.assertIn("via-ui=127.0.0.1", actor)     # asserted by the relay
        self.assertIn("origin=http://127.0.0.1:28090", actor)
        self.assertIn('ua="Mozilla/5.0 (X11)"', actor)

    def test_actor_tolerates_a_bare_request(self):
        self.assertIn("peer=?", cp.provenance._actor(_FakeRequest(peer=None)))
        self.assertIn("unrecorded", cp.provenance._actor(None))

    def test_actor_bounds_a_hostile_user_agent(self):
        actor = cp.provenance._actor(_FakeRequest(headers={"user-agent": "A" * 5000}))
        self.assertLess(len(actor), 400)

    def test_resolution_records_provenance_on_the_approval_row(self):
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 5
        try:
            t, _result, approval_id = HoldHandshakeTests._authorize_in_thread(
                self, "provenance.com")
            _resolve(approval_id, "allow_once",
                     _FakeRequest(peer="172.31.0.3",
                                  headers={cp.provenance.ACTOR_HEADER: "10.1.2.3",
                                           "user-agent": "curl/8.5.0"}))
            t.join(2)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        with cp.store._connect() as conn:
            row = conn.execute(
                "SELECT status, resolved_by FROM approvals WHERE host=?",
                ("provenance.com",)).fetchone()
        self.assertEqual(row["status"], "allowed")
        self.assertIn("via-ui=10.1.2.3", row["resolved_by"])
        # A non-browser caller is exactly what the UA field is for.
        self.assertIn("curl/8.5.0", row["resolved_by"])

    def test_the_released_waiters_row_carries_the_actor(self):
        # _audit is mocked by _CPTestCase, so assert on what the waiter passed it —
        # that is the record an operator actually reads back.
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 5
        try:
            t, result, approval_id = HoldHandshakeTests._authorize_in_thread(
                self, "audited.com")
            _resolve(approval_id, "allow_once", _FakeRequest(peer="172.31.0.9"))
            t.join(2)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertEqual(result["resp"].decision, "allow")
        [decided] = [c.kwargs for c in cp.store._audit.call_args_list
                     if "human approval" in (c.kwargs.get("reason") or "")]
        self.assertIn("peer=172.31.0.9", decided["actor"])

    def test_audit_reason_says_whether_standing_policy_was_written(self):
        # A one-off and a persist are the same allow for THIS request and very
        # different afterwards, and the audit line used to say nothing about which had
        # happened — so reading the log could not tell you that a click had changed
        # policy. Read from the durable `mode` column, i.e. what was recorded.
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 5
        try:
            t, result, approval_id = HoldHandshakeTests._authorize_in_thread(
                self, "persisted.example.com")
            _resolve(approval_id, "allow_persist")
            t.join(2)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertEqual(result["resp"].decision, "allow")
        reasons = [c.kwargs.get("reason", "") for c in cp.store._audit.call_args_list]
        self.assertTrue(any("standing rule written" in r for r in reasons),
                        f"persist not distinguishable in audit reasons: {reasons}")

    def test_audit_reason_names_the_pattern_a_persist_wrote(self):
        # "allow api.example.co.uk" and "allow .co.uk" are the same click and very
        # different policy. The released waiter's row carried the HOST and the words
        # "standing rule written", so a broad wildcard found in the rules view months
        # later had no row saying which card, click or breadth choice produced it.
        # The pattern is stored on the approval and read back into the reason.
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 5
        try:
            t, result, approval_id = HoldHandshakeTests._authorize_in_thread(
                self, "api.persisted.example.com")
            _resolve(approval_id, "allow_persist", pattern=".example.com")
            t.join(2)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertEqual(result["resp"].decision, "allow")
        reasons = [c.kwargs.get("reason", "") for c in cp.store._audit.call_args_list]
        self.assertTrue(
            any("standing rule written: .example.com" in r for r in reasons),
            f"pattern missing from audit reasons: {reasons}")
        with cp.store._connect() as conn:
            row = conn.execute("SELECT mode, pattern FROM approvals WHERE id=?",
                               (approval_id,)).fetchone()
        self.assertEqual((row["mode"], row["pattern"]),
                         ("persist", ".example.com"))

    def test_a_once_or_lease_decision_stores_no_pattern(self):
        _hold("once.example.com", approval_id="once-1")
        _resolve("once-1", "allow_once")
        with cp.store._connect() as conn:
            self.assertIsNone(conn.execute(
                "SELECT pattern FROM approvals WHERE id='once-1'").fetchone()["pattern"])

    def test_audit_reason_marks_a_one_off_as_one_off(self):
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 5
        try:
            t, _result, approval_id = HoldHandshakeTests._authorize_in_thread(
                self, "oneoff.example.com")
            _resolve(approval_id, "deny_once")
            t.join(2)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        reasons = [c.kwargs.get("reason", "") for c in cp.store._audit.call_args_list]
        self.assertTrue(any("this request only" in r for r in reasons), reasons)
        self.assertFalse(any("standing rule" in r for r in reasons), reasons)

    def test_timeout_path_reports_no_actor_rather_than_a_wrong_one(self):
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 0.05
        try:
            resp = cp.api_authorize.authorize(_auth_req("nobody.com", client="a"))
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertEqual(resp.decision, "deny")
        self.assertIn("timeout", resp.reason)
        self.assertNotIn("peer=", resp.reason)


class PersistClassScopeTests(_CPTestCase):
    """What a `*_persist` writes, now that a rule is scoped.

    The persist path is where the class dimension stops being descriptive: the rule an
    approval writes decides for one client population and no other, so it has to be
    the population the card was raised for, and not "everyone"."""

    def _stored(self):
        with cp.store._connect() as conn:
            return {(r["pattern"], r["action"], r["client_class"]) for r in
                    conn.execute("SELECT pattern, action, client_class FROM rules")}

    def test_a_persist_writes_the_rule_for_the_approvals_class(self):
        _resolve(_hold("api.github.com", client_class="mcp"), "allow_persist")
        self.assertEqual(self._stored(), {("api.github.com", "allow", "mcp")})
        # ...and it decides for that class alone.
        self.assertEqual(cp.policy._decide("api.github.com", "mcp")[0], "allow")
        self.assertEqual(cp.policy._decide("api.github.com", CLASS)[0], "hold")

    def test_the_class_comes_from_the_durable_row_not_from_the_address(self):
        # `resolve` must not re-derive the class from the client address: the durable
        # row is the only thing that knows which population the card was raised for,
        # and a second implementation of the classification — for the one caller whose
        # answer becomes standing policy — is exactly the drift worth refusing. The
        # address here maps to `sandbox`; the rule must still be written for `mcp`.
        approval = _hold("x.example", client="172.30.0.137", client_class="mcp")
        _resolve(approval, "allow_persist")
        self.assertEqual(self._stored(), {("x.example", "allow", "mcp")})

    def test_an_unclassified_client_cannot_write_a_standing_rule(self):
        # "Whoever we could not identify" is not a client population: a rule scoped to
        # it would grant to every future unidentified caller, which is exactly the
        # union-of-needs erosion the class dimension exists to stop.
        for value in (None, cp.policy.UNCLASSIFIED):
            with self.subTest(client_class=value):
                _clear_all()
                resp = _resolve(_hold("x.example", client_class=value),
                                "allow_persist")
                self.assertEqual(resp.kwargs.get("status_code"), 400)
                self.assertFalse(resp.args[0]["ok"])
                self.assertIn("unclassified", resp.args[0]["detail"])
                self.assertEqual(self._stored(), set())

    def test_the_refused_persist_leaves_the_approval_decidable(self):
        # Refused BEFORE the UPDATE, like every other persist refusal here: the
        # operator is never stuck, because `*_once` still decides the request.
        approval = _hold("x.example", client_class=None)
        _resolve(approval, "allow_persist")
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM approvals WHERE id=?",
                             (approval,)).fetchone()[0], "pending")
        self.assertTrue(_resolve(approval, "allow_once").args[0]["ok"])

    def test_the_same_pattern_in_another_class_is_not_a_conflict(self):
        # Under the old UNIQUE(pattern) this could not be stored at all, and the
        # conflict check would have refused it. One host the agent may reach and an
        # MCP server may not is ordinary policy, not a contradiction.
        _set_rules([("pypi.org", "allow", CLASS)])
        resp = _resolve(_hold("pypi.org", client_class="mcp"), "deny_persist")
        self.assertTrue(resp.args[0]["ok"])
        self.assertTrue(resp.args[0]["persisted"])
        self.assertEqual(self._stored(),
                         {("pypi.org", "allow", CLASS), ("pypi.org", "block", "mcp")})

    def test_the_conflict_check_still_fires_within_one_class(self):
        _set_rules([("pypi.org", "allow", "mcp")])
        resp = _resolve(_hold("pypi.org", client_class="mcp"), "deny_persist")
        self.assertEqual(resp.kwargs.get("status_code"), 409)
        self.assertEqual(resp.args[0]["conflict"]["client_class"], "mcp")

    def test_the_response_names_the_class_the_rule_was_written_for(self):
        # The pattern alone does not identify the rule any more: the same pattern
        # persisted from two cards is two rules, and a confirmation naming only the
        # pattern reads identically for both.
        resp = _resolve(_hold("x.example", client_class="mcp"), "allow_persist")
        self.assertEqual(resp.args[0]["client_class"], "mcp")
        # A `*_once` writes no rule, so it names no class.
        _clear_all()
        resp = _resolve(_hold("y.example", client_class="mcp"), "allow_once")
        self.assertIsNone(resp.args[0]["client_class"])


class PendingCardClassTests(_CPTestCase):
    """What the operator's card says about the class, from ``holds._list_pending``."""

    def test_the_card_carries_the_class_the_request_was_decided_under(self):
        _hold("x.example", client="172.28.0.4", client_class="mcp")
        self.assertEqual(cp.holds._list_pending()[0]["client_class"], "mcp")

    def test_existing_is_reported_per_class_not_per_pattern(self):
        # Keyed by pattern alone, a rule in ANOTHER class would be reported as already
        # present — so the card would describe a rule as existing while the request
        # that raised it stayed held, which is the most confusing thing it could say.
        _set_rules([("x.example", "allow", CLASS)])
        _hold("x.example", client_class="mcp")
        options = {o["pattern"]: o["existing"]
                   for o in cp.holds._list_pending()[0]["persist_options"]}
        self.assertIsNone(options["x.example"])
        # The same card for the class that DOES hold the rule reports it.
        _clear_all()
        _set_rules([("x.example", "allow", CLASS)])
        _hold("x.example", client_class=CLASS)
        options = {o["pattern"]: o["existing"]
                   for o in cp.holds._list_pending()[0]["persist_options"]}
        self.assertEqual(options["x.example"], "allow")

    def test_a_card_says_whether_a_rule_can_be_written_at_all(self):
        # So the UI can disable the persist buttons rather than walk the operator
        # through a confirm panel to reach a 400.
        _hold("x.example", "classified", client_class="mcp")
        _hold("y.example", "unplaceable", client_class=cp.policy.UNCLASSIFIED)
        persistable = {h["host"]: h["persistable"] for h in cp.holds._list_pending()}
        self.assertEqual(persistable, {"x.example": True, "y.example": False})


class RulesViewTests(_CPTestCase):
    """``/api/egress/rules`` exists so standing policy is not invisible from the UI that
    governs it. It is the complete policy, in precedence order, with the wildcard
    semantics spelled out."""

    def test_every_rule_says_which_client_class_it_decides_for(self):
        # Without it the listing can show two rows with the same pattern and opposite
        # actions, which reads as a contradiction rather than as two scoped rules.
        _set_rules([("pypi.org", "allow", CLASS), ("pypi.org", "block", "mcp")])
        classes = {(r["pattern"], r["action"]): r["client_class"]
                   for r in cp.api_egress.api_rules()}
        self.assertEqual(classes, {("pypi.org", "allow"): CLASS,
                                   ("pypi.org", "block"): "mcp"})

    def test_rules_are_grouped_by_class_before_precedence(self):
        # `_decide` filters to the asking class FIRST and only then lets a block win,
        # so the listing reads in that order: one class's rules together, blocks first
        # within each.
        _set_rules([("a.example", "allow", "mcp"), ("b.example", "block", "mcp"),
                    ("c.example", "allow", CLASS), ("d.example", "block", CLASS)])
        listed = [(r["client_class"], r["action"]) for r in cp.api_egress.api_rules()]
        self.assertEqual(listed, [("mcp", "block"), ("mcp", "allow"),
                                  (CLASS, "block"), (CLASS, "allow")])

    def test_lists_every_rule_with_source_and_scope(self):
        _set_rules([("example.com", "allow"), ("bad.com", "block"),
                    (".github.com", "allow")])
        rows = cp.api_egress.api_rules()
        self.assertEqual(len(rows), 3)
        by_pattern = {r["pattern"]: r for r in rows}
        self.assertEqual(by_pattern["example.com"]["action"], "allow")
        self.assertEqual(by_pattern["bad.com"]["action"], "block")
        # Every row carries where it came from, so seed and operator rules are
        # distinguishable in the listing.
        self.assertEqual(by_pattern["example.com"]["source"], "test")

    def test_blocks_are_listed_first_because_block_wins(self):
        _set_rules([("aaa-allow.com", "allow"), ("zzz-block.com", "block")])
        actions = [r["action"] for r in cp.api_egress.api_rules()]
        # Alphabetically 'allow' < 'block' and aaa- < zzz-, so a naive ordering would
        # invert this. The listing must read in DECISION precedence order.
        self.assertEqual(actions, ["block", "allow"])

    def test_wildcard_scope_is_named_not_left_to_the_reader(self):
        # A leading dot is a subdomain wildcard that LOOKS like a hostname — the
        # thing that makes an over-broad persisted rule easy to miss.
        _set_rules([(".example.com", "allow"), ("example.com", "allow")])
        scope = {r["pattern"]: r["scope"] for r in cp.api_egress.api_rules()}
        self.assertEqual(scope[".example.com"], "host + subdomains")
        self.assertEqual(scope["example.com"], "exact host")

    def test_scope_agrees_with_the_matcher_that_implements_it(self):
        # Guard against the label drifting from _match's real behaviour.
        self.assertTrue(cp.policy._match("sub.example.com", ".example.com"))
        self.assertFalse(cp.policy._match("sub.example.com", "example.com"))
        self.assertEqual(cp.policy._pattern_scope(".example.com"), "host + subdomains")
        self.assertEqual(cp.policy._pattern_scope("example.com"), "exact host")

    def test_empty_policy_is_an_empty_list_not_an_error(self):
        _set_rules([])
        self.assertEqual(cp.api_egress.api_rules(), [])


class LeaseGrantTests(_CPTestCase):
    """``allow_lease`` — the middle rung of the resolve ladder, from the click side.

    ``LeaseDecisionTests`` in ``test_control_plane.py`` covers what a lease MEANS once
    it is in the table; these cover the write that puts one there, and the two ways it
    must refuse."""

    @staticmethod
    def _leases():
        with cp.store._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT host, client_class, approval_id, expires_at, granted_by "
                "FROM leases ORDER BY id")]

    def test_a_lease_is_written_and_decides_the_next_request(self):
        _hold("api.example.com")
        resp = _resolve("hold-1", "allow_lease")
        self.assertTrue(resp.args[0]["ok"])
        self.assertTrue(resp.args[0]["leased"])
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "allow")

    def test_a_lease_writes_no_standing_rule(self):
        # The distinction the whole feature rests on. A lease that quietly wrote a rule
        # would be `allow_persist` with a confusing name and no expiry anyone could see.
        _hold("api.example.com")
        resp = _resolve("hold-1", "allow_lease")
        self.assertFalse(resp.args[0]["persisted"])
        self.assertFalse(resp.args[0]["already_present"])
        self.assertIsNone(resp.args[0]["pattern"])
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 0)

    def test_the_lease_row_carries_the_card_it_was_granted_from(self):
        # Provenance a rule has no equivalent of, and what makes the trail joinable:
        # the approvals row says what was asked, this says what the answer granted.
        _hold("api.example.com", approval_id="card-7")
        _resolve("card-7", "allow_lease")
        row = self._leases()[0]
        self.assertEqual(row["approval_id"], "card-7")
        self.assertEqual(row["host"], "api.example.com")
        self.assertEqual(row["client_class"], CLASS)

    def test_the_lease_expires_after_the_configured_duration(self):
        saved = cp.policy.LEASE_SECONDS
        cp.policy.LEASE_SECONDS = 300.0
        try:
            before = time.time()
            _hold("api.example.com")
            resp = _resolve("hold-1", "allow_lease")
        finally:
            cp.policy.LEASE_SECONDS = saved
        expires = resp.args[0]["lease_expires_at"]
        self.assertGreaterEqual(expires, before + 300.0)
        self.assertLess(expires, before + 310.0)
        # The returned deadline IS the stored one — the card reports what was written
        # rather than adding the duration to its own clock.
        self.assertEqual(self._leases()[0]["expires_at"], expires)

    def test_the_stored_host_is_normalized_so_it_can_ever_match(self):
        # A lease is matched by EQUALITY, so this is the difference between a grant and
        # a row that decides nothing: `_decide` normalizes the requested host, and a
        # lease stored in any other shape could never equal it.
        _hold("API.Example.COM.")
        _resolve("hold-1", "allow_lease")
        self.assertEqual(self._leases()[0]["host"], "api.example.com")
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "allow")

    def test_a_lease_records_who_granted_it(self):
        # Same provenance discipline as the approvals row and a persisted rule: a grant
        # that decides future requests has to say who made it.
        _hold("api.example.com")
        _resolve("hold-1", "allow_lease", request=_FakeRequest(peer="172.31.0.9"))
        self.assertIn("peer=172.31.0.9", self._leases()[0]["granted_by"])

    def test_the_durable_row_records_the_lease_mode(self):
        # Read by the released waiter to write its audit line (`_decision_scope`), so a
        # mode of 'once' here would report a lease as a one-off in the trail.
        _hold("api.example.com")
        _resolve("hold-1", "allow_lease")
        with cp.store._connect() as conn:
            row = conn.execute(
                "SELECT status, mode FROM approvals WHERE id='hold-1'").fetchone()
        self.assertEqual((row["status"], row["mode"]), ("allowed", "lease"))

    def test_an_unclassified_client_cannot_be_leased_to(self):
        # Expiry bounds how LONG a grant lasts; it does nothing about WHO it covers, so
        # a lease needs a class to be scoped to exactly as a rule does.
        _hold("api.example.com", client_class=None)
        resp = _resolve("hold-1", "allow_lease")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.args[0]["ok"])
        self.assertIn("lease", resp.args[0]["detail"])
        self.assertEqual(self._leases(), [])

    def test_the_refused_lease_leaves_the_card_decidable(self):
        # Refused BEFORE the UPDATE, so the operator can still answer with
        # `allow_once` rather than being stuck with a card that consumed itself.
        _hold("api.example.com", client_class=None)
        _resolve("hold-1", "allow_lease")
        self.assertEqual([a["id"] for a in cp.holds._list_pending()], ["hold-1"])
        self.assertTrue(_resolve("hold-1", "allow_once").args[0]["ok"])

    def test_there_is_no_deny_lease(self):
        # An unmatched host is HELD, not denied, so a timed deny would mean "suppress
        # the card for a while" — a different feature wearing this one's name.
        self.assertNotIn("deny_lease", cp.api_approvals.EGRESS_ACTIONS)
        _hold("api.example.com")
        self.assertEqual(_resolve("hold-1", "deny_lease").status_code, 400)

    def test_a_lease_takes_no_pattern(self):
        # A lease answers the breadth question by expiring, so there is no candidate
        # ladder on this path — a pattern in the body is simply not read, and the host
        # is the exact one from the durable row.
        _hold("api.example.com")
        _resolve("hold-1", "allow_lease", pattern=".example.com")
        self.assertEqual(self._leases()[0]["host"], "api.example.com")
        self.assertEqual(cp.policy._decide("other.example.com", CLASS)[0], "hold")

    def test_granting_a_lease_sweeps_the_expired_ones(self):
        # The only place the table grows, so the only place it needs to shrink. Not
        # what ENDS a lease — `_live_lease` filters on the deadline — just what keeps
        # the table from accumulating a row per grant forever.
        now = time.time()
        with cp.store._connect() as conn:
            conn.execute(
                "INSERT INTO leases(host, client_class, approval_id, created_at, "
                "expires_at, granted_by) VALUES ('old.example.com', ?, 'x', ?, ?, 'y')",
                (CLASS, now - 100, now - 1))
            conn.commit()
        _hold("api.example.com")
        _resolve("hold-1", "allow_lease")
        self.assertEqual([r["host"] for r in self._leases()], ["api.example.com"])

    def test_a_swept_leases_id_is_not_handed_to_the_next(self):
        # A revoke from a page still showing the swept lease must miss, not end the
        # lease that took its place.
        now = time.time()
        with cp.store._connect() as conn:
            swept = conn.execute(
                "INSERT INTO leases(host, client_class, approval_id, created_at, "
                "expires_at, granted_by) VALUES ('old.example.com', ?, 'x', ?, ?, 'y')",
                (CLASS, now - 100, now - 1)).lastrowid
            conn.commit()
        _hold("api.example.com")
        _resolve("hold-1", "allow_lease")
        resp = cp.api_egress.revoke_lease(swept, _FakeRequest())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual([r["host"] for r in self._leases()], ["api.example.com"])

    def test_an_expired_card_does_not_leave_a_live_lease_behind(self):
        # The race the `updated` guard closes. The waiter's timeout and this call race
        # for one conditional UPDATE; if the lease were written outside that guard —
        # where the persist path does its VALIDATION — a card that expired and
        # default-denied would still have granted half an hour of egress.
        _hold("api.example.com")
        with cp.store._connect() as conn:
            conn.execute(
                "UPDATE approvals SET status='expired' WHERE id='hold-1'")
            conn.commit()
        resp = _resolve("hold-1", "allow_lease")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self._leases(), [])
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "hold")

    def test_a_lease_wakes_the_blocked_waiter(self):
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 5
        try:
            result = {}

            def worker():
                result["resp"] = cp.api_authorize.authorize(_auth_req("leased.com",
                                                                      client=CLASS_IP))

            t = threading.Thread(target=worker)
            t.start()
            deadline = time.monotonic() + 5
            approval_id = None
            while time.monotonic() < deadline and approval_id is None:
                pending = cp.holds._list_pending()
                approval_id = pending[0]["id"] if pending else None
                time.sleep(0.01)
            self.assertIsNotNone(approval_id, "approval never became pending")
            _resolve(approval_id, "allow_lease")
            t.join(2)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        self.assertFalse(t.is_alive())
        self.assertEqual(result["resp"].decision, "allow")
        # And the NEXT request needs no card at all, which is the point of the rung.
        self.assertEqual(
            cp.api_authorize.authorize(_auth_req("leased.com")).decision, "allow")


class LeaseCardTests(_CPTestCase):
    """What a pending card says about the lease button before it is clicked."""

    def test_a_classified_card_offers_both_grants(self):
        _hold("api.example.com")
        card = cp.holds._list_pending()[0]
        self.assertTrue(card["leasable"])
        self.assertTrue(card["persistable"])

    def test_an_unclassified_card_offers_neither(self):
        # One condition, two fields (`holds._classified`): the card must not end up
        # offering one button the backend will refuse while disabling the other.
        _hold("api.example.com", client_class=None)
        card = cp.holds._list_pending()[0]
        self.assertFalse(card["leasable"])
        self.assertFalse(card["persistable"])

    def test_a_card_carries_no_lease_options(self):
        # Nothing per-approval for the operator to choose — the host is the requested
        # one and the duration is the same for every card — which is why the lease
        # button is one click where a persist is two.
        _hold("api.example.com")
        card = cp.holds._list_pending()[0]
        self.assertNotIn("lease_options", card)
        self.assertIn("persist_options", card)


class LeaseViewTests(_CPTestCase):
    """``/api/egress/leases`` — "what am I allowing right now", which is a different
    question from the one the rules view answers and is why it is a different endpoint.
    """

    def _insert(self, host, offset, client_class=CLASS):
        now = time.time()
        with cp.store._connect() as conn:
            cur = conn.execute(
                "INSERT INTO leases(host, client_class, approval_id, created_at, "
                "expires_at, granted_by) VALUES (?,?, 'card', ?, ?, 'someone')",
                (host, client_class, now, now + offset))
            conn.commit()
            return cur.lastrowid

    def test_a_live_lease_is_listed(self):
        self._insert("api.example.com", 300)
        rows = cp.api_egress.api_leases()
        self.assertEqual([r["host"] for r in rows], ["api.example.com"])
        self.assertEqual(rows[0]["client_class"], CLASS)
        self.assertEqual(rows[0]["granted_by"], "someone")

    def test_an_expired_lease_is_not_listed(self):
        # An expired lease is not policy, so listing it would put something in the
        # operator's "what is granted" view that grants nothing.
        self._insert("gone.example.com", -1)
        self.assertEqual(cp.api_egress.api_leases(), [])

    def test_the_deadline_is_absolute_not_a_remaining_count(self):
        # A remaining-seconds field would change on every tick, which is what makes the
        # page's four-second poll a firehose and its countdown unable to run between
        # polls. The client does the arithmetic.
        self._insert("api.example.com", 300)
        row = cp.api_egress.api_leases()[0]
        self.assertIn("expires_at", row)
        self.assertNotIn("remaining", row)
        self.assertGreater(row["expires_at"], time.time())

    def test_the_soonest_to_expire_comes_first(self):
        self._insert("later.example.com", 900)
        self._insert("sooner.example.com", 60)
        self.assertEqual([r["host"] for r in cp.api_egress.api_leases()],
                         ["sooner.example.com", "later.example.com"])


class LeaseRevokeTests(_CPTestCase):
    """Revocation is what makes the configured duration an ergonomics number rather
    than a safety floor — without it a lease can only be waited out, and the default
    could not have been half an hour."""

    def _insert(self, host, offset, client_class=CLASS):
        now = time.time()
        with cp.store._connect() as conn:
            cur = conn.execute(
                "INSERT INTO leases(host, client_class, approval_id, created_at, "
                "expires_at, granted_by) VALUES (?,?, 'card', ?, ?, 'someone')",
                (host, client_class, now, now + offset))
            conn.commit()
            return cur.lastrowid

    def test_revoking_stops_the_lease_deciding_requests(self):
        lease_id = self._insert("api.example.com", 900)
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "allow")
        resp = cp.api_egress.revoke_lease(lease_id, _FakeRequest())
        self.assertTrue(resp.args[0]["ok"])
        self.assertTrue(resp.args[0]["was_live"])
        self.assertEqual(cp.policy._decide("api.example.com", CLASS)[0], "hold")

    def test_the_row_is_deleted_rather_than_tombstoned(self):
        # This table is transient by construction, so a dead row would be the only
        # long-lived thing in it and every reader would have to filter for it.
        lease_id = self._insert("api.example.com", 900)
        cp.api_egress.revoke_lease(lease_id, _FakeRequest())
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)

    def test_revocation_is_audited_with_provenance_and_what_it_reverts_to(self):
        lease_id = self._insert("api.example.com", 900)
        with mock.patch.object(cp.store, "_audit") as audit:
            cp.api_egress.revoke_lease(lease_id, _FakeRequest(peer="172.31.0.9"))
        self.assertEqual(audit.call_args.args[0], "revoke")
        kwargs = audit.call_args.kwargs
        self.assertEqual(kwargs["host"], "api.example.com")
        self.assertIn("peer=172.31.0.9", kwargs["actor"])
        self.assertIn(f"client class {CLASS}", kwargs["reason"])
        self.assertIsNone(kwargs.get("client_class"))
        # With no other rule, held, not denied — the same distinction a rule
        # revocation records, and only the reason line carries it. And how much time
        # was cut short, which is the part a rule revocation has no equivalent of.
        self.assertIn("held for approval", kwargs["reason"])
        self.assertRegex(kwargs["reason"], r"\d+m\d\ds left")

    def test_the_reason_says_where_the_host_lands_when_a_rule_still_decides_it(self):
        # A rule written after the lease decides the host once the lease is gone: an
        # allow allows and a block denies, and the reason says so rather than "held".
        for action, decision in (("allow", "allow"), ("block", "deny")):
            with self.subTest(action=action):
                lease_id = self._insert("api.example.com", 900)
                _set_rules([(".example.com", action)])
                with mock.patch.object(cp.store, "_audit") as audit:
                    cp.api_egress.revoke_lease(lease_id, _FakeRequest())
                reason = audit.call_args.kwargs["reason"]
                after = cp.policy._decide("api.example.com", CLASS)
                self.assertEqual(after[0], decision)
                self.assertIn(after[1], reason)
                self.assertNotIn("held for approval", reason)
                self.assertRegex(reason, r"\d+m\d\ds left")

    def test_revoking_an_already_expired_lease_says_so_rather_than_refusing(self):
        # A refusal would look like a bug. The honest answer — it had already ended,
        # and now the row is gone too — is both true and what was being asked for.
        lease_id = self._insert("gone.example.com", -5)
        with mock.patch.object(cp.store, "_audit") as audit:
            resp = cp.api_egress.revoke_lease(lease_id, _FakeRequest())
        self.assertTrue(resp.args[0]["ok"])
        self.assertFalse(resp.args[0]["was_live"])
        self.assertIn("already stopped deciding",
                      audit.call_args.kwargs["reason"])
        # The only place this row names the class, now that `client_class` is empty.
        self.assertIn(f"client class {CLASS}", audit.call_args.kwargs["reason"])

    def test_an_unknown_id_is_a_404_not_a_silent_success(self):
        resp = cp.api_egress.revoke_lease(999999, _FakeRequest())
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.args[0]["ok"])

    def test_revoking_one_lease_leaves_the_others(self):
        keep = self._insert("keep.example.com", 900)
        drop = self._insert("drop.example.com", 900)
        cp.api_egress.revoke_lease(drop, _FakeRequest())
        self.assertEqual(
            [r["host"] for r in cp.api_egress.api_leases()], ["keep.example.com"])
        self.assertTrue(keep)


class ConfigViewTests(_CPTestCase):
    """``/api/config`` exists so a pending card can show a COUNTDOWN. Without it the
    UI would have to hardcode the hold window, and a card that cannot say how long is
    left cannot distinguish hold-for-approval from a slow deny. It carries the client
    classes for the same shape of reason: ``create_rule`` refuses a class it does not
    know, so a page that guessed the list would offer rules that cannot be written."""

    def test_config_reports_the_hold_window(self):
        self.assertEqual(
            cp.api_views.api_config()["hold_timeout"], cp.holds.HOLD_TIMEOUT)

    def test_config_follows_the_operator_setting_rather_than_a_constant(self):
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 45.0
        try:
            self.assertEqual(cp.api_views.api_config()["hold_timeout"], 45.0)
        finally:
            cp.holds.HOLD_TIMEOUT = saved

    def test_config_reports_the_lease_duration(self):
        # Served so the lease BUTTON can label itself. A page that spelled the number
        # into its own markup would keep saying it on a store configured differently,
        # which is why the action is `allow_lease` and not `allow_30m`.
        self.assertEqual(
            cp.api_views.api_config()["lease_seconds"], cp.policy.LEASE_SECONDS)

    def test_config_follows_the_operator_lease_setting_too(self):
        saved = cp.policy.LEASE_SECONDS
        cp.policy.LEASE_SECONDS = 300.0
        try:
            self.assertEqual(cp.api_views.api_config()["lease_seconds"], 300.0)
        finally:
            cp.policy.LEASE_SECONDS = saved

    def test_config_serves_the_pin_lease_duration_for_the_pin_panel(self):
        saved = cp.policy.PIN_LEASE_SECONDS
        cp.policy.PIN_LEASE_SECONDS = 300.0
        self.addCleanup(setattr, cp.policy, "PIN_LEASE_SECONDS", saved)
        self.assertEqual(cp.api_views.api_config()["pin_lease_seconds"], 300.0)

    def test_config_reports_the_classes_a_rule_can_be_scoped_to(self):
        self.assertEqual(cp.api_views.api_config()["client_classes"],
                         list(cp.policy._class_names()))
        self.assertIn(CLASS, cp.api_views.api_config()["client_classes"])

    def test_config_never_offers_the_unclassified_pseudo_class(self):
        # It is not a rule scope — `create_rule` and `resolve` both refuse it — so a
        # page that offered it would present a choice that can only ever 400.
        self.assertNotIn(
            cp.policy.UNCLASSIFIED, cp.api_views.api_config()["client_classes"])

    def test_config_exposes_nothing_but_that(self):
        # A read-only view of NON-SECRET config on the one interface that can grant
        # egress: whatever gets added here has to stay harmless to publish. The class
        # names pass that test — they are network LABELS, and the CIDRs behind them
        # stay here.
        self.assertEqual(set(cp.api_views.api_config()),
                         {"hold_timeout", "lease_seconds", "pin_lease_seconds",
                          "client_classes"})


class SaturationTests(_CPTestCase):
    """Over the hold cap ``/authorize`` fails closed WITHOUT creating an approval, so
    the refusal never becomes a card and the approvals page shows the same empty list
    as a quiet afternoon. These assert the record that makes it visible."""

    def _over_cap(self, host="pypi.org", client=None, cap="MAX_WAITERS"):
        """Drive one rejection by zeroing ``cap``. Zero on a GLOBAL cap refuses
        everything (see the cap block in holds.py), which is what makes it the cheap
        way to exercise the refusal path — and the cap is named rather than assumed,
        because with four of them "over cap" no longer identifies one."""
        saved = getattr(cp.holds, cap)
        setattr(cp.holds, cap, 0)
        try:
            return cp.api_authorize.authorize(_auth_req(host, client=client))
        finally:
            setattr(cp.holds, cap, saved)

    def test_a_rejection_is_recorded_with_what_was_refused(self):
        self._over_cap(host="pypi.org")
        sat = cp.holds._saturation()
        self.assertEqual(sat["rejections"], 1)
        self.assertEqual(sat["last_host"], "pypi.org")
        self.assertEqual(sat["last_scope"], "global waiters")
        self.assertIsNotNone(sat["last_ts"])

    def test_the_scope_names_which_of_the_four_caps_refused(self):
        """Four caps can refuse, and they suggest different responses: cards versus
        blocked workers is attention versus capacity, global versus per-client is "the
        whole plane is loaded" versus "one agent is hammering". A scope that named only
        one axis would collapse two of those four into each other."""
        self._over_cap(cap="MAX_PENDING")
        self.assertEqual(cp.holds._saturation()["last_scope"], "global cards")

    def test_rejections_accumulate_rather_than_overwrite(self):
        # The count is the point: a burst of twenty is a different event from one,
        # and only the count survives the holds draining.
        for _ in range(3):
            self._over_cap()
        self.assertEqual(cp.holds._saturation()["rejections"], 3)

    def test_the_per_client_cap_is_recorded_as_its_own_scope(self):
        saved = cp.holds.MAX_PENDING_PER_CLIENT
        cp.holds.MAX_PENDING_PER_CLIENT = 1
        try:
            _hold("a.example", "held-1")
            cp.holds._PENDING_CLIENT["held-1"] = "172.30.0.137"
            cp.api_authorize.authorize(_auth_req("b.example", client="172.30.0.137"))
        finally:
            cp.holds.MAX_PENDING_PER_CLIENT = saved
        # Distinguishable from a global exhaustion: "one agent is hammering" and
        # "the whole control plane is loaded" want different responses.
        self.assertEqual(cp.holds._saturation()["last_scope"],
                         "client 172.30.0.137 cards")

    def test_the_per_client_waiter_cap_is_recorded_as_its_own_scope(self):
        """The fourth scope, and the one whose absence was the finding. A client that
        fills the pool through ONE card hits nothing the other three caps can see."""
        saved = cp.holds.MAX_WAITERS_PER_CLIENT
        cp.holds.MAX_WAITERS_PER_CLIENT = 1
        try:
            _hold("a.example", "held-1", client="172.30.0.137")
            cp.api_authorize.authorize(_auth_req("a.example", client="172.30.0.137"))
        finally:
            cp.holds.MAX_WAITERS_PER_CLIENT = saved
        self.assertEqual(cp.holds._saturation()["last_scope"],
                         "client 172.30.0.137 waiters")

    def test_nothing_is_recorded_when_the_hold_is_accepted(self):
        _hold("quiet.example", "held-1")
        self.assertEqual(cp.holds._saturation()["rejections"], 0)
        self.assertIsNone(cp.holds._saturation()["last_ts"])

    def test_in_flight_counts_live_holds_not_pending_rows(self):
        # The divergence this exists for: after a restart the table can carry
        # `pending` rows with no live hold behind them. The cap is measured against
        # the in-memory set, so a count taken from the visible cards would be
        # confidently wrong in exactly the situation the banner reports.
        with cp.store._connect() as conn:
            conn.execute(
                "INSERT INTO approvals(id, ts, host, status) "
                "VALUES ('orphan', 0, 'stale.example', 'pending')")
            conn.commit()
        self.assertEqual(len(cp.holds._list_pending()), 1)
        self.assertEqual(cp.holds._saturation()["in_flight"], 0)
        self.assertEqual(cp.holds._saturation()["cards"], 0)

    def test_in_flight_counts_waiters_and_cards_counts_cards(self):
        # The second divergence, and the reason both numbers are in the payload:
        # duplicates share a card, so "12/16 in flight" beside three cards is not a
        # contradiction. Each number is measured against its OWN cap.
        _hold("a.example", "held-1", client="172.30.0.130")
        for _ in range(4):
            slot = cp.holds._reserve_hold("ignored", threading.Event(), "172.30.0.130",
                                    "a.example")
            self.assertTrue(slot.joined)
        sat = cp.holds._saturation()
        self.assertEqual(sat["in_flight"], 5)
        self.assertEqual(sat["cards"], 1)

    def test_the_payload_carries_the_holds_and_the_pressure_together(self):
        _hold("a.example", "held-1")
        payload = cp.api_approvals.approvals()
        self.assertEqual(set(payload), {"holds", "saturation", "withdrawn"})
        self.assertEqual([h["id"] for h in payload["holds"]], ["held-1"])
        self.assertEqual(payload["saturation"]["in_flight"], 1)
        # Both global caps travel, because either can be the one about to fire and the
        # banner shows whichever gauge is fuller. Sending one would hide the other.
        self.assertEqual(payload["saturation"]["max_pending"], cp.holds.MAX_PENDING)
        self.assertEqual(payload["saturation"]["max_waiters"], cp.holds.MAX_WAITERS)

    def test_every_time_in_the_payload_is_absolute(self):
        # Load-bearing for the SSE stream, which emits on payload CHANGE: an
        # elapsed-seconds field would differ on every 1s tick, so the change
        # detection would fire forever, the heartbeat would never be sent, and an
        # idle page would receive a 1 Hz firehose. Named fields, so adding a
        # "seconds_ago" convenience trips this rather than the stream.
        self._over_cap()
        sat = cp.holds._saturation()
        self.assertEqual(
            set(sat),
            {"in_flight", "cards", "max_waiters", "max_pending", "rejections",
             "acknowledged", "last_ts", "last_scope", "last_host", "since"})
        # Both stamps are epoch seconds — comfortably past 2001 — not durations.
        self.assertGreater(sat["last_ts"], 1_000_000_000)
        self.assertGreater(sat["since"], 1_000_000_000)

    def test_an_idle_payload_is_byte_identical_across_ticks(self):
        # The property the test above protects, asserted directly against the
        # comparison the stream actually makes.
        _hold("a.example", "held-1")
        self.assertEqual(json.dumps(cp.holds._pending_payload()),
                         json.dumps(cp.holds._pending_payload()))

    def test_the_counter_says_since_when(self):
        # In-memory, so a restart resets it. The UI can only be honest about that if
        # the payload says which window the count covers.
        self.assertEqual(cp.holds._saturation()["since"], cp.holds._STARTED_TS)

    # ── acknowledgement ──────────────────────────────────────────────────────

    def test_acknowledgement_lives_in_the_control_plane_not_the_page(self):
        # The bug this fixes: dismissal was page state, so a reload brought the
        # banner back. An operator who believes they cleared something and a page
        # that disagrees on refresh is worse than offering no button.
        self._over_cap()
        self._over_cap()
        cp.api_views.api_saturation_ack(cp.api_views.AckRequest(count=2))
        sat = cp.holds._saturation()
        self.assertEqual(sat["rejections"], 2)
        self.assertEqual(sat["acknowledged"], 2)

    def test_a_rejection_racing_the_click_is_not_swallowed(self):
        # The reason this is a high-water mark rather than a reset. The operator read
        # 2 and clicked; a third landed in the gap. Zeroing the counter would lose it,
        # and rejections come in bursts — exactly when the gap is open.
        self._over_cap()
        self._over_cap()
        self._over_cap()                       # arrives while the click is in flight
        cp.api_views.api_saturation_ack(cp.api_views.AckRequest(count=2))
        sat = cp.holds._saturation()
        self.assertEqual(sat["rejections"] - sat["acknowledged"], 1)

    def test_acknowledgement_never_goes_backwards(self):
        for _ in range(3):
            self._over_cap()
        cp.api_views.api_saturation_ack(cp.api_views.AckRequest(count=3))
        # a stale tab, or a replay
        cp.api_views.api_saturation_ack(cp.api_views.AckRequest(count=1))
        self.assertEqual(cp.holds._saturation()["acknowledged"], 3)

    def test_acknowledging_more_than_happened_is_clamped(self):
        # Otherwise a client number silences FUTURE rejections until they catch up —
        # a governance signal suppressed by an unvalidated input. Same reasoning as
        # validating `pattern` in resolve, and the same answer.
        self._over_cap()
        cp.api_views.api_saturation_ack(cp.api_views.AckRequest(count=10_000))
        self.assertEqual(cp.holds._saturation()["acknowledged"], 1)
        self._over_cap()
        sat = cp.holds._saturation()
        self.assertEqual(sat["rejections"] - sat["acknowledged"], 1)

    def test_negative_acknowledgements_are_floored(self):
        self._over_cap()
        cp.api_views.api_saturation_ack(cp.api_views.AckRequest(count=-5))
        self.assertEqual(cp.holds._saturation()["acknowledged"], 0)

    def test_the_window_moves_to_the_dismissal(self):
        # The count and the stamp beside it must describe the SAME span, or the
        # banner reads "1 request since 14:02" for something that happened at 15:30.
        self._over_cap()
        before = cp.holds._saturation()["since"]
        self.assertEqual(before, cp.holds._STARTED_TS)
        cp.api_views.api_saturation_ack(cp.api_views.AckRequest(count=1))
        after = cp.holds._saturation()["since"]
        self.assertNotEqual(after, cp.holds._STARTED_TS)
        self.assertGreaterEqual(after, before)

    def test_an_acknowledgement_that_changes_nothing_leaves_the_window_alone(self):
        self._over_cap()
        cp.api_views.api_saturation_ack(cp.api_views.AckRequest(count=1))
        stamped = cp.holds._saturation()["since"]
        # idempotent replay
        cp.api_views.api_saturation_ack(cp.api_views.AckRequest(count=1))
        self.assertEqual(cp.holds._saturation()["since"], stamped)


class MergedQueueTests(_CPTestCase):
    """One queue over two builders. Everything else about the tool surface splits —
    its own tables, caps and endpoints — and this is the deliberate exception, because
    a partly connected merged view is indistinguishable from an empty one: two streams
    merged in the browser render a silent subset when one drops, and a subset of a
    queue looks exactly like a queue with nothing in it."""

    def test_both_kinds_arrive_in_one_list(self):
        _hold("example.com", "hold-1")
        ask = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1})
        cards = cp.holds._pending_payload()["holds"]
        self.assertEqual({c["kind"] for c in cards}, {"egress", "tool"})
        self.assertIn(ask.approval_id, [c["id"] for c in cards])

    def test_every_card_states_its_kind(self):
        # Stated on both builders rather than defaulted on one, so neither surface is
        # the implicit case a reader has to infer from the absence of the other.
        _hold("example.com", "hold-1")
        cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1})
        self.assertTrue(all(c.get("kind") for c in
                            cp.holds._pending_payload()["holds"]))

    def test_the_queue_is_ordered_by_age_across_both_surfaces(self):
        # The operator's question is which decision has waited longest, and that does
        # not respect which subsystem raised it. Sorting per surface and concatenating
        # would float a seconds-old ask above a minutes-old hold.
        ask = cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1})
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_approvals SET ts=? WHERE id=?",
                         (time.time() - 300, ask.approval_id))
            conn.commit()
        _hold("example.com", "hold-1")
        cards = cp.holds._pending_payload()["holds"]
        self.assertEqual([c["id"] for c in cards], [ask.approval_id, "hold-1"])

    def test_a_tool_card_carries_what_a_human_needs_and_no_egress_rim(self):
        # No `requests` count — nothing is blocked, so a joiner adds no waiter to
        # report. No `persist_options` — the argument-shaped ladder that would derive
        # them does not exist, so there is nothing an ask could be persisted AS.
        cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1})
        card = cp.holds._pending_payload()["holds"][0]
        for field in ("server", "tool", "args_json", "deadline"):
            self.assertIn(field, card)
        for field in ("requests", "persist_options", "persistable"):
            self.assertNotIn(field, card)

    def test_saturation_is_reported_once_for_both(self):
        # Unsplit: over a cap nothing raises a card on either surface, so a refusal is
        # invisible in the queue, and an operator should not have to read two banners.
        self.assertIn("saturation", cp.holds._pending_payload())


class ApprovalStreamTests(_CPTestCase):
    """Where the SSE tick does its work, which is the part of it that is not about
    the operator at all: one event loop serves every listener in this process, so a
    synchronous payload build here is time not spent answering ``/authorize``."""

    def test_the_payload_is_built_off_the_event_loop(self):
        # Asserted on the THREAD rather than on the presence of `to_thread`, because
        # what matters is where the work lands: the build takes `holds._LOCK`, which
        # `_register_tool_ask` holds across a SQLite write that waits out the 5 s busy
        # timeout when another writer has the store. On the loop, that wait is every
        # sandbox's egress decision waiting with it.
        built_on = []

        def record():
            built_on.append(threading.get_ident())
            return {"holds": [], "saturation": {}}

        async def first_tick():
            response = await cp.api_approvals.approvals_stream(_FakeRequest())
            with mock.patch.object(cp.holds, "_pending_payload", record):
                await response.body.__anext__()
            await response.body.aclose()
            return threading.get_ident()

        loop_thread = asyncio.run(first_tick())
        self.assertEqual(len(built_on), 1)
        self.assertNotEqual(built_on[0], loop_thread,
                            "the pending payload was built on the event loop thread")

    def test_a_disconnected_client_ends_the_stream(self):
        # The other half of reading the connection: the generator must stop rather
        # than tick forever for a browser that has gone.
        async def drain():
            response = await cp.api_approvals.approvals_stream(
                _FakeRequest(disconnected=True))
            return [chunk async for chunk in response.body]

        self.assertEqual(asyncio.run(drain()), [])


class ResolveToolAskTests(_CPTestCase):
    """``resolve``, tool side. Dispatched on which table holds the id — the queue is
    merged, so the client sends back only that, and an approval id belongs to exactly
    one table."""

    def _ask(self, **kw):
        return cp.holds._register_tool_ask("mcp-github", "issue_write", {"n": 1},
                                           **kw).approval_id

    def test_an_ask_is_decided_by_its_own_action_set(self):
        ask = self._ask()
        resp = _resolve(ask, "allow")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.body["kind"], "tool")
        self.assertEqual(cp.holds._get_tool_ask(ask)["status"], "allowed")

    def test_the_egress_vocabulary_is_refused_on_a_tool_card(self):
        # Per-surface action sets, not a union. `allow_persist` on a tool ask would
        # have to mean "allow this tool forever", which is promoting the rule in the
        # MCP tab and never a rung reached by clicking the same button twice.
        ask = self._ask()
        for action in ("allow_once", "allow_persist", "deny_once", "deny_persist"):
            resp = _resolve(ask, action)
            self.assertEqual(resp.status_code, 400, action)
        self.assertEqual(cp.holds._get_tool_ask(ask)["status"], "pending")

    def test_the_tool_vocabulary_is_refused_on_an_egress_card(self):
        # All of it, `allow_pinned_lease` included: it ends in the suffix the egress
        # half reads as a lease.
        _hold("example.com", "hold-1")
        for action in cp.api_approvals.TOOL_ACTIONS:
            self.assertEqual(_resolve("hold-1", action).status_code, 400, action)

    def test_a_pattern_on_a_tool_ask_is_refused_not_ignored(self):
        # Nothing on this surface persists, so a pattern is a caller that thinks it is
        # writing standing policy — better told than quietly humoured.
        ask = self._ask()
        resp = _resolve(ask, "allow", pattern="example.com")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(cp.holds._get_tool_ask(ask)["status"], "pending")

    def test_deciding_twice_reports_the_current_status(self):
        # Expired and already-decided call for different things from an operator, so
        # the conflict names which it was rather than saying only "not pending".
        ask = self._ask()
        _resolve(ask, "allow")
        resp = _resolve(ask, "deny")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.body["status"], "allowed")

    def test_resolving_an_ask_wakes_nothing_and_touches_no_egress_state(self):
        # There is no event to set, no group to close and no waiter to release,
        # because nothing is blocked: the agent already holds a pending result and an
        # id to come back with.
        ask = self._ask()
        _resolve(ask, "allow")
        self.assertEqual(cp.holds._PENDING_EVENTS, {})
        self.assertEqual(cp.holds._GROUPS, {})

    def test_a_decision_writes_its_own_audit_row(self):
        # The asymmetry worth asserting: on the egress path the released waiter writes
        # the audit line as it returns, so `resolve` records nothing itself. Here there
        # is no waiter, so without this a human would grant capability with nothing in
        # the trail.
        with mock.patch.object(cp.store, "_audit") as audited:
            _resolve(self._ask(), "allow")
        self.assertEqual(audited.call_args[0][0], "allow")
        self.assertEqual(audited.call_args[1]["stage"], "tool-ask")

    def test_an_approval_is_not_executed_by_the_click(self):
        # The gateway runs the call when the agent RESUMES and claims it, which is what
        # keeps an approved side effect from happening with nobody left to receive it.
        ask = self._ask()
        _resolve(ask, "allow")
        self.assertIsNone(cp.holds._get_tool_ask(ask)["claimed_at"])
        self.assertIsNotNone(cp.holds._claim_tool_ask(ask))


def _register(server="mcp-github", request=None, **kw):
    return cp.api_mcp.create_mcp_server(
        cp.api_mcp.ServerCreateRequest(server=server, **kw),
        request if request is not None else _FakeRequest())


def _edit_request(port=8082, path="/mcp", **kw):
    """An edit body, the endpoint filled in: the endpoint is required on an edit."""
    return cp.api_mcp.ServerEditRequest(port=port, path=path, **kw)


def _enable(server="mcp-github", request=None, **kw):
    """Flip a registered server on, through the endpoint that owns the switch.

    Separate from ``_register`` because a registration cannot arrive pre-enabled, and
    needed by every test whose subject is what a rule DECIDES: a rule on a disabled
    server decides nothing (``policy._decide_tool``), which is the switch working."""
    return cp.api_mcp.edit_mcp_server(
        server, _edit_request(enabled=True, **kw),
        request if request is not None else _FakeRequest())


def _tool_rule(tool, action="allow", server="mcp-github", request=None):
    return cp.api_mcp.create_mcp_rule(
        cp.api_mcp.ToolRuleCreateRequest(server=server, tool=tool, action=action),
        request if request is not None else _FakeRequest())


def _edit_tool_rule(rule_id, action, request=None, seen=None):
    """An edit from a page showing the rule as it stands, as ``_edit_rule``."""
    with cp.store._connect() as conn:
        row = conn.execute("SELECT server, tool, action FROM tool_rules WHERE id=?",
                           (rule_id,)).fetchone()
    shown = {**(dict(row) if row else {}), **(seen or {})}
    return cp.api_mcp.edit_mcp_rule(
        rule_id, cp.api_mcp.ToolRuleEditRequest(
            action=action, expected_server=shown.get("server"),
            expected_tool=shown.get("tool"), expected_action=shown.get("action")),
        request if request is not None else _FakeRequest())


class McpServerRegistrationTests(_CPTestCase):
    """``/api/mcp/servers`` — the half of server configuration the control plane owns.

    The other half is compose, and the split is not negotiable: enumerating or
    starting containers would mean a docker socket on the crown-jewel container. So a
    registration here is a NAME the operator asserts, and everything downstream keys
    on it — the gateway dials it, the secret path is derived from it, and a tool rule
    points at it."""

    def _row(self, server="mcp-github"):
        with cp.store._connect() as conn:
            return conn.execute("SELECT * FROM mcp_servers WHERE server=?",
                                (server,)).fetchone()

    def test_a_registration_is_disabled_and_grants_nothing(self):
        # Both defaults point the same way, and neither is covering for the other: the
        # server is not dialled, and every tool it might expose is denied for want of
        # a rule. A registration that arrived enabled would be one call that both
        # introduces a server and opens it.
        self.assertEqual(_register().status_code, 201)
        row = self._row()
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["auth_type"], "none")
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "deny")

    def test_enabled_cannot_be_set_at_registration(self):
        # Asserts the MODEL stays without the field, the way the egress suite asserts
        # `source` cannot be caller-supplied.
        cp.api_mcp.create_mcp_server(
            cp.api_mcp.ServerCreateRequest(server="mcp-sneaky", enabled=True),
            _FakeRequest())
        self.assertEqual(self._row("mcp-sneaky")["enabled"], 0)

    def test_a_name_that_is_not_dialable_is_refused(self):
        # The name IS the address here — there is no second field to fall back on — so
        # a name no resolver could answer for is a tool surface that never replies.
        for bad in ("mcp github", "-mcp", "mcp-", "mcp/github", "", "x" * 64):
            resp = _register(server=bad)
            self.assertEqual(resp.status_code, 400, bad)

    def test_the_validators_refuse_a_trailing_newline_themselves(self):
        # Asserted on the validators, not through the route, because the route strips
        # first. `inventory.record` does not: it checks the gateway push's keys as
        # sent, so the guarantee has to live here rather than in each caller.
        self.assertIsNotNone(cp.policy._server_name_error("mcp-github\n"))
        self.assertIsNotNone(
            cp.policy._auth_descriptor_error("header", "Authorization\n",
                                             "Bearer {secret}"))

    def test_a_name_is_normalized_rather_than_refused_for_its_case(self):
        # Lowercased on the way in, like an egress pattern, because DNS does not
        # distinguish the two spellings and the charset above only admits one of them.
        # Refusing instead would reject a name that is genuinely dialable.
        self.assertEqual(_register(server=" MCP-GitHub ").status_code, 201)
        self.assertIsNotNone(self._row("mcp-github"))

    def test_a_duplicate_registration_is_a_conflict_not_a_replace(self):
        # A silent replace would overwrite an auth descriptor — a credential swap
        # reported as a registration, with no before in the record.
        _register(auth_type="header", auth_header="Authorization",
                  auth_template="Bearer {secret}")
        resp = _register(auth_type="none")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self._row()["auth_type"], "header")

    def test_a_header_descriptor_needs_exactly_one_secret_placeholder(self):
        # None of them is a stored credential; all of them are a request the gateway
        # cannot build. The zero case is the dangerous one, because the upstream
        # answer is a 401 that reads exactly like a policy refusal or a dead token.
        for template in ("Bearer ", "Bearer {secret} {secret}", ""):
            resp = _register(server="mcp-x", auth_type="header",
                             auth_header="Authorization", auth_template=template)
            self.assertEqual(resp.status_code, 400, template)
            self.assertIn("{secret}", json.dumps(resp.body))

    def test_a_header_descriptor_needs_a_header_name(self):
        resp = _register(auth_type="header", auth_template="Bearer {secret}")
        self.assertEqual(resp.status_code, 400)

    def test_an_injectionless_descriptor_takes_no_header_fields(self):
        # Config that says two things at once, so neither is the source of truth: the
        # gateway injects nothing under 'none', and a header sitting in the row would
        # describe behaviour that does not happen.
        resp = _register(auth_type="none", auth_header="Authorization",
                         auth_template="Bearer {secret}")
        self.assertEqual(resp.status_code, 400)

    def test_an_unknown_auth_type_is_refused(self):
        self.assertEqual(_register(auth_type="oauth").status_code, 400)

    def test_the_endpoint_defaults_to_where_servers_were_always_dialled(self):
        self.assertEqual(_register().body["endpoint"], {"port": 8082, "path": "/mcp"})
        self.assertEqual(cp.api_mcp.api_mcp_servers()[0]["endpoint"],
                         {"port": 8082, "path": "/mcp"})

    def test_a_registration_records_its_own_endpoint(self):
        _register(port=3000, path=" /v1/mcp ")
        row = self._row()
        self.assertEqual((row["port"], row["path"]), (3000, "/v1/mcp"))

    def test_an_endpoint_that_could_move_the_host_is_refused(self):
        # The gateway appends the path straight after the checked address, and the
        # credential goes wherever the URL then points. `@` ends the authority there.
        for port, path in ((8082, "@evil.example/mcp"), (8082, "//evil.example"),
                           (8082, "/mcp?x=1"), (8082, "/mcp#x"), (8082, "/%40x"),
                           (8082, "/a\\b"), (8082, "/../mcp"), (8082, "/./mcp"),
                           (8082, "mcp"), (8082, ""), (8082, "/" + "a" * 128),
                           (0, "/mcp"), (65536, "/mcp"), (True, "/mcp"),
                           (None, "/mcp")):
            with self.subTest(port=port, path=path):
                self.assertEqual(
                    _register(server="mcp-x", port=port, path=path).status_code, 400)
        self.assertIsNone(self._row("mcp-x"))

    def test_a_long_bad_path_is_refused_at_once(self):
        # This runs in the one process that also answers /authorize, so a pattern that
        # backtracks on a pasted path stalls every decision. The first one did. The
        # inputs take it seconds, not forever, so a regression fails here rather than
        # hanging the run.
        started = time.monotonic()
        self.assertIsNotNone(
            cp.policy._endpoint_error(8082, "/mcp/streamable-http-transport?" + "a" * 90))
        self.assertIsNotNone(cp.policy._endpoint_error(8082, "/" + "a" * 28 + "!"))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_the_paths_a_server_actually_uses_are_accepted(self):
        for path in ("/", "/mcp", "/mcp/", "/v1/mcp", "/.well-known/mcp", "/a_b~c-d"):
            with self.subTest(path=path):
                self.assertIsNone(cp.policy._endpoint_error(8082, path))


class McpServerEditTests(_CPTestCase):
    def setUp(self):
        super().setUp()
        _register()

    def test_enabling_and_disabling_reports_both_states(self):
        resp = cp.api_mcp.edit_mcp_server("mcp-github",
                                          _edit_request(enabled=True),
                                          _FakeRequest())
        self.assertTrue(resp.body["changed"])
        self.assertTrue(resp.body["enabled"])
        self.assertFalse(resp.body["previous"]["enabled"])

    def test_asking_for_what_is_already_configured_writes_nothing(self):
        resp = cp.api_mcp.edit_mcp_server("mcp-github",
                                          _edit_request(enabled=False),
                                          _FakeRequest())
        self.assertFalse(resp.body["changed"])

    def test_the_descriptor_travels_with_the_switch(self):
        # One operation for both, because they are one configuration: a server enabled
        # with a descriptor that cannot build a request fails as though policy refused
        # it, and splitting them puts a window either side of the ordering.
        resp = cp.api_mcp.edit_mcp_server(
            "mcp-github",
            _edit_request(enabled=True, auth_type="header",
                          auth_header="Authorization",
                          auth_template="Bearer {secret}"),
            _FakeRequest())
        self.assertEqual(resp.body["auth"]["type"], "header")
        self.assertTrue(resp.body["enabled"])

    def test_a_bad_descriptor_is_refused_before_anything_is_enabled(self):
        resp = cp.api_mcp.edit_mcp_server(
            "mcp-github",
            _edit_request(enabled=True, auth_type="header",
                          auth_header="Authorization",
                          auth_template="Bearer nothing"),
            _FakeRequest())
        self.assertEqual(resp.status_code, 400)
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT enabled FROM mcp_servers WHERE server=?",
                             ("mcp-github",)).fetchone()[0], 0)

    def test_an_unknown_server_is_a_404(self):
        resp = cp.api_mcp.edit_mcp_server("mcp-nope",
                                          _edit_request(enabled=True),
                                          _FakeRequest())
        self.assertEqual(resp.status_code, 404)

    def test_the_endpoint_moves_and_the_audit_row_has_both(self):
        with mock.patch.object(cp.store, "_audit") as audited:
            resp = cp.api_mcp.edit_mcp_server(
                "mcp-github", _edit_request(enabled=False, port=3000, path="/v1/mcp"),
                _FakeRequest())
        self.assertTrue(resp.body["changed"])
        self.assertEqual(resp.body["endpoint"], {"port": 3000, "path": "/v1/mcp"})
        self.assertEqual(resp.body["previous"]["endpoint"],
                         {"port": 8082, "path": "/mcp"})
        self.assertIn(":8082/mcp -> :3000/v1/mcp", audited.call_args[1]["reason"])

    def test_an_edit_without_an_endpoint_is_refused_not_reset(self):
        # The endpoint is required on an edit where auth defaults, so a caller that
        # leaves it out cannot move a server back to 8082 and /mcp without noticing.
        cp.api_mcp.edit_mcp_server(
            "mcp-github", _edit_request(enabled=False, port=3000, path="/v1/mcp"),
            _FakeRequest())
        resp = cp.api_mcp.edit_mcp_server(
            "mcp-github", cp.api_mcp.ServerEditRequest(enabled=True), _FakeRequest())
        self.assertEqual(resp.status_code, 400)
        server = cp.api_mcp.api_mcp_servers()[0]
        self.assertEqual(server["endpoint"], {"port": 3000, "path": "/v1/mcp"})
        self.assertFalse(server["enabled"])

    def test_a_bad_endpoint_is_refused_before_anything_is_enabled(self):
        resp = cp.api_mcp.edit_mcp_server(
            "mcp-github", _edit_request(enabled=True, path="@evil.example/mcp"),
            _FakeRequest())
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(cp.api_mcp.api_mcp_servers()[0]["enabled"])


class McpServerRevokeTests(_CPTestCase):
    def setUp(self):
        super().setUp()
        _register()

    def test_a_registration_with_no_rules_is_removed(self):
        self.assertEqual(cp.api_mcp.revoke_mcp_server("mcp-github",
                                                      _FakeRequest()).status_code, 200)
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0], 0)

    def test_standing_policy_is_never_deleted_as_a_side_effect(self):
        # A cascade behind one POST would remove decisions the operator cannot see
        # from the button they pressed, and nothing here has an undo. Removal is the
        # safe DIRECTION — an unconfigured tool is denied — but safe is not visible,
        # and visibility is this surface's entire job.
        _tool_rule("get_me")
        resp = cp.api_mcp.revoke_mcp_server("mcp-github", _FakeRequest())
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.body["tool_rules"], 1)
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tool_rules").fetchone()[0], 1)

    def test_an_unknown_server_is_a_404(self):
        self.assertEqual(cp.api_mcp.revoke_mcp_server("mcp-nope",
                                                      _FakeRequest()).status_code, 404)


class McpToolRuleTests(_CPTestCase):
    """``/api/mcp/rules`` — stating what a server's tools may do, before anything asks.

    Configuration first, which is the mirror image of the egress surface: there, rules
    accumulate from approvals and direct creation was the retrofit. Here there is
    nothing to accumulate from — an unconfigured tool is denied outright — so this
    endpoint is the primary path rather than the missing verb."""

    def setUp(self):
        super().setUp()
        _register()
        # Enabled, because what a rule DECIDES is what most of these tests assert and
        # a disabled server's rules decide nothing. Registration is the other half and
        # is deliberately not enough on its own.
        _enable()

    def test_a_rule_decides_immediately(self):
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "deny")
        self.assertEqual(_tool_rule("get_me", "allow").status_code, 201)
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "allow")

    def test_a_rule_on_a_disabled_server_decides_nothing(self):
        # Standing policy survives the switch — the rule is still there, still listed,
        # still what the server's tools may do WHEN it is on — and it grants nothing
        # while it is off. That is the difference between disabling a server and
        # revoking its rules, and both verbs exist because both are wanted.
        _tool_rule("get_me", "allow")
        cp.api_mcp.edit_mcp_server("mcp-github",
                                   _edit_request(enabled=False),
                                   _FakeRequest())
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "deny")
        self.assertEqual([r["tool"] for r in cp.api_mcp.api_mcp_rules()], ["get_me"])

    def test_a_rule_for_an_unregistered_server_is_refused(self):
        # The tool-surface twin of the unknown-client-class refusal: the row would
        # insert cleanly, list cleanly and decide nothing, while reading as policy in
        # force in the one view built to show what is in force.
        resp = _tool_rule("get_me", server="mcp-nobody")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("register it", json.dumps(resp.body))

    def test_an_explicit_deny_is_worth_writing(self):
        # It changes nothing for the gateway and everything for the operator: this is
        # what distinguishes "reviewed and refused" from "never looked at", which is
        # the distinction that makes materialising discovered rows unnecessary.
        _tool_rule("delete_file", "deny")
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT action FROM tool_rules WHERE tool=?",
                             ("delete_file",)).fetchone()[0], "deny")

    def test_an_ask_rule_is_storable_before_any_registry_exists(self):
        # `ask` has no approvals table behind it yet. Storing one is still correct —
        # policy is configuration — and the gateway is what will act on it.
        self.assertEqual(_tool_rule("create_pull_request", "ask").status_code, 201)
        self.assertEqual(
            cp.policy._decide_tool("mcp-github", "create_pull_request")[0], "ask")

    def test_the_action_vocabulary_is_the_tool_one(self):
        # 'block' and 'hold' are the EGRESS words. Accepting one here would store an
        # action `_decide_tool` does not recognize, which it fails closed on — a rule
        # that reads as policy and denies whatever it says.
        for bad in ("block", "hold", "", "permit"):
            self.assertEqual(_tool_rule("get_me", bad).status_code, 400, bad)

    def test_the_action_is_normalized_but_the_tool_name_is_not(self):
        # Two different answers in one request, and both are deliberate. An action is
        # a fixed vocabulary, so case and padding are noise. A tool name is the
        # server's own identifier, compared byte-for-byte by ``_decide_tool``, so
        # folding its case here would store a rule for a tool that does not exist.
        self.assertEqual(_tool_rule("getMe", " ALLOW ").status_code, 201)
        self.assertEqual(cp.policy._decide_tool("mcp-github", "getMe")[0], "allow")
        self.assertEqual(cp.policy._decide_tool("mcp-github", "getme")[0], "deny")

    def test_a_tool_name_outside_the_charset_is_refused(self):
        for bad in ("get me", "get\nme", "x" * 129, "", "tool$"):
            self.assertEqual(_tool_rule(bad).status_code, 400, bad)

    def test_the_validator_refuses_a_trailing_newline_itself(self):
        # On the validator, because the route strips the name before it gets here.
        self.assertIsNotNone(cp.policy._tool_rule_error("getMe\n", "allow"))

    def test_a_conflicting_action_is_a_conflict_and_the_same_one_is_a_non_write(self):
        _tool_rule("get_me", "allow")
        clash = _tool_rule("get_me", "deny")
        self.assertEqual(clash.status_code, 409)
        self.assertEqual(clash.body["conflict"]["action"], "allow")
        again = _tool_rule("get_me", "allow")
        self.assertTrue(again.body["already_present"])
        self.assertFalse(again.body["created"])

    def test_the_source_is_server_set(self):
        cp.api_mcp.create_mcp_rule(
            cp.api_mcp.ToolRuleCreateRequest(server="mcp-github", tool="get_me",
                                             action="allow", source="seed"),
            _FakeRequest())
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT source FROM tool_rules").fetchone()[0],
                "operator")

    def test_promoting_a_tool_is_one_operation(self):
        # deny -> ask -> allow is the workflow this endpoint exists for, and each step
        # keeps the rule's identity, so there is no window where the tool is
        # unconfigured and no second row in the record.
        rule_id = _tool_rule("issue_write", "deny").body["id"]
        for action in ("ask", "allow"):
            resp = _edit_tool_rule(rule_id, action)
            self.assertTrue(resp.body["changed"])
            self.assertEqual(
                cp.policy._decide_tool("mcp-github", "issue_write")[0], action)

    def test_an_edit_to_the_same_action_writes_nothing(self):
        rule_id = _tool_rule("get_me", "allow").body["id"]
        resp = _edit_tool_rule(rule_id, "allow")
        self.assertFalse(resp.body["changed"])

    def test_an_edit_validates_the_action_and_the_rule_id(self):
        rule_id = _tool_rule("get_me", "allow").body["id"]
        self.assertEqual(_edit_tool_rule(rule_id, "block").status_code, 400)
        self.assertEqual(_edit_tool_rule(9999, "allow").status_code, 404)

    def test_an_edit_from_a_stale_table_is_refused(self):
        # Another tab moved the tool to deny; this one, still showing allow, asks.
        # Without the expectation the ask would undo the deny unseen.
        rule_id = _tool_rule("get_me", "allow").body["id"]
        _edit_tool_rule(rule_id, "deny")
        resp = _edit_tool_rule(rule_id, "ask", seen={"action": "allow"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.body["current"]["action"], "deny")
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "deny")

    def test_a_write_landing_after_the_check_is_not_overwritten(self):
        # Another tab's edit commits between the check and the write; injected at the
        # validation call that runs between them.
        rule_id = _tool_rule("get_me", "ask").body["id"]
        real = cp.api_mcp.policy._tool_rule_error

        def other_tab(tool, action):
            with cp.store._connect() as conn:
                conn.execute("UPDATE tool_rules SET action='deny' WHERE id=?",
                             (rule_id,))
            return real(tool, action)

        with mock.patch.object(cp.api_mcp.policy, "_tool_rule_error",
                               side_effect=other_tab):
            resp = _edit_tool_rule(rule_id, "allow")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "deny")

    def test_the_server_is_part_of_what_the_edit_expects(self):
        # The same tool and action on another server, under the id the page showed:
        # only the server tells the two rules apart.
        _register(server="mcp-other")
        _enable(server="mcp-other")
        rule_id = _tool_rule("get_me", "ask", server="mcp-other").body["id"]
        resp = _edit_tool_rule(rule_id, "allow", seen={"server": "mcp-github"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(cp.policy._decide_tool("mcp-other", "get_me")[0], "ask")

    def test_an_edit_landing_on_a_different_tool_under_the_same_id_is_refused(self):
        # The finding's case with the id reused by hand, as on the egress side: the
        # page shows get_me, the id now holds delete_repo, and "allow" would allow it.
        # Both ask, so only the tool name tells them apart.
        rule_id = _tool_rule("get_me", "ask").body["id"]
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM tool_rules WHERE id=?", (rule_id,))
            conn.execute("INSERT INTO tool_rules(id, server, tool, action, source, "
                         "created_at) VALUES (?, 'mcp-github', 'delete_repo', 'ask', "
                         "'operator', 0)", (rule_id,))
        resp = _edit_tool_rule(rule_id, "allow", seen={"tool": "get_me"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("delete_repo", resp.body["detail"])
        self.assertEqual(cp.policy._decide_tool("mcp-github", "delete_repo")[0],
                         "ask")

    def test_a_stale_edit_does_not_land_on_the_rule_created_after_a_revoke(self):
        # Tab A shows get_me; tab B revokes it and denies delete_repo. Had the new rule
        # taken get_me's freed id, tab A's "allow" would have allowed delete_repo.
        stale = _tool_rule("get_me", "ask").body["id"]
        cp.api_mcp.revoke_mcp_rule(stale, _FakeRequest())
        fresh = _tool_rule("delete_repo", "deny").body["id"]
        self.assertNotEqual(fresh, stale)
        resp = _edit_tool_rule(stale, "allow", seen={
            "server": "mcp-github", "tool": "get_me", "action": "ask"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(cp.policy._decide_tool("mcp-github", "delete_repo")[0],
                         "deny")

    def test_revoking_returns_the_tool_to_denied_not_to_held(self):
        # The one place this differs from revoking an egress rule, where the host
        # reverts to being HELD for approval. Here it reverts to the default deny, so
        # a revoke can only ever narrow.
        rule_id = _tool_rule("get_me", "allow").body["id"]
        self.assertEqual(
            cp.api_mcp.revoke_mcp_rule(rule_id, _FakeRequest()).status_code, 200)
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "deny")

    def test_the_view_groups_by_server_then_widest_first(self):
        _register(server="mcp-other")
        _tool_rule("b_deny", "deny", server="mcp-other")
        _tool_rule("a_ask", "ask", server="mcp-other")
        _tool_rule("z_allow", "allow")
        _tool_rule("a_deny", "deny")
        served = [(r["server"], r["action"]) for r in cp.api_mcp.api_mcp_rules()]
        self.assertEqual(served, [("mcp-github", "allow"), ("mcp-github", "deny"),
                                  ("mcp-other", "ask"), ("mcp-other", "deny")])

    def test_the_server_view_counts_the_rules(self):
        # What makes the list actionable: a server with no rules has a fully denied
        # surface rather than a broken one, and only the count says which.
        _tool_rule("get_me", "allow")
        _tool_rule("get_teams", "ask")
        served = {s["server"]: s["tool_rules"] for s in cp.api_mcp.api_mcp_servers()}
        self.assertEqual(served, {"mcp-github": 2})


class McpToolRuleRevokeTests(_CPTestCase):
    """Revoking a tool rule names, in its audit and its answer, the row it deleted."""

    audits = True

    def setUp(self):
        super().setUp()
        _register()
        _enable()
        self.rule_id = _tool_rule("get_me", "allow").body["id"]

    def _revoke_with_an_edit_after_the_read(self):
        """Revoke, with another writer's allow-to-deny edit tried right after the
        revoke's own SELECT. Returns the response and whether the edit landed, or
        None if the hook never fired."""
        rule_id = self.rule_id
        landed = []

        class _EditsAfterTheRead:
            """Wraps the connection rather than patching it: sqlite3.Connection.execute
            is read-only, so the edit has to be injected from outside the object."""

            def __init__(self, conn):
                object.__setattr__(self, "_conn", conn)

            def execute(self, sql, *a):
                cur = self._conn.execute(sql, *a)
                if not landed and sql.startswith("SELECT server, tool, action "
                                                 "FROM tool_rules"):
                    # No busy wait: against the revoke's lock this must fail now,
                    # not stall for store._connect's timeout.
                    other = sqlite3.connect(cp.store.DB_PATH, timeout=0)
                    try:
                        other.execute("UPDATE tool_rules SET action='deny' WHERE id=?",
                                      (rule_id,))
                        other.commit()
                        landed.append(True)
                    except sqlite3.OperationalError as e:
                        # Only the lock counts: a renamed table or a moved
                        # DB_PATH would otherwise pass without the fix.
                        if e.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY:
                            raise
                        landed.append(False)
                    finally:
                        other.close()
                return cur

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def __setattr__(self, name, value):
                setattr(self._conn, name, value)

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, *exc):
                return self._conn.__exit__(*exc)

        real_connect = cp.store._connect
        with mock.patch.object(cp.store, "_connect",
                               lambda: _EditsAfterTheRead(real_connect())):
            resp = cp.api_mcp.revoke_mcp_rule(rule_id, _FakeRequest())
        return resp, (landed[0] if landed else None)

    def test_the_audit_names_the_action_of_the_row_deleted(self):
        resp, landed = self._revoke_with_an_edit_after_the_read()
        deleted = "deny" if landed else "allow"
        self.assertEqual(resp.status_code, 200)
        with cp.store._connect() as conn:
            reason = conn.execute("SELECT reason FROM audit WHERE kind='revoke' "
                                  "ORDER BY id DESC LIMIT 1").fetchone()["reason"]
        self.assertIn(f"was {deleted},", reason)
        self.assertEqual(resp.body["action"], deleted)
        # And it was the lock that kept them in step, not the hook missing its moment.
        self.assertIsNotNone(landed, "the hook never saw the revoke's SELECT")
        self.assertFalse(landed, "the edit landed between the read and the delete")

    def test_revoking_an_unknown_rule_is_a_404(self):
        resp = cp.api_mcp.revoke_mcp_rule(self.rule_id + 1, _FakeRequest())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "allow")


#: The digest of a tool's arguments a call is made under unless a test says otherwise,
#: and the digest of the same tool's arguments after an upgrade moved them.
_SCHEMA = "5c" * 32
_MOVED = "e1" * 32


def _tool_call(server="mcp-github", tool="get_me", args=None, client=CLASS_IP,
               schema_digest=_SCHEMA):
    """One tool call put to the bridge. ``client`` defaults to a real sandbox-net
    address for the reason ``_auth_req`` does: it is what the per-client cap counts
    and what an identical retry joins on."""
    return cp.api_tool.tool_authorize(
        cp.api_tool.ToolCallRequest(server=server, tool=tool, args=args, client=client,
                                    schema_digest=schema_digest))


def _ask_id(answer):
    """The operator's id for the ask an `ask` answer raised. Read from the store by
    the answer's resume token, because the answer itself carries no id."""
    with cp.store._connect() as conn:
        row = conn.execute("SELECT id FROM tool_approvals WHERE resume_token=?",
                           (answer["resume_token"],)).fetchone()
    return row["id"]


def _token(approval_id):
    """The resume token of the ask ``approval_id`` names: the agent's handle on it.
    Raises if there is no such ask, so a test cannot pass an id where a token goes
    by accident; ``_claim_with`` and ``_withdraw_with`` take a raw value."""
    with cp.store._connect() as conn:
        row = conn.execute("SELECT resume_token FROM tool_approvals WHERE id=?",
                           (approval_id,)).fetchone()
    return row["resume_token"]


def _claim_with(handle, client=CLASS_IP):
    return cp.api_tool.tool_claim(handle,
                                  cp.api_tool.ToolResumeRequest(client=client))


def _withdraw_with(handle, client=CLASS_IP):
    return cp.api_tool.tool_withdraw(handle,
                                     cp.api_tool.ToolResumeRequest(client=client))


def _claim(approval_id, client=CLASS_IP):
    """Claim the ask ``approval_id`` names as its agent does: by its resume token."""
    return _claim_with(_token(approval_id), client)


def _withdraw(approval_id, client=CLASS_IP):
    """Withdraw the ask ``approval_id`` names as its agent does: by its token."""
    return _withdraw_with(_token(approval_id), client)


class _ToolBridgeTestCase(_CPTestCase):
    """A registered, enabled server, which is the state every question on this bridge
    is asked in. The two server-state refusals get their own tests."""

    def setUp(self):
        super().setUp()
        _register()
        _enable()


class ToolAuthorizeDecisionTests(_ToolBridgeTestCase):
    """``POST /tool/authorize`` — the gateway's per-call question, and the tool
    surface's counterpart to ``/authorize``.

    Its whole shape is the divergence: an egress hold is resolved INSIDE the control
    plane by blocking a worker until a human answers, while an `ask` here is
    registered and answered immediately with an id to come back with. Nothing waits,
    so nothing can be stranded."""

    def test_an_allowed_tool_is_allowed_without_registering_anything(self):
        _tool_rule("get_me", "allow")
        self.assertEqual(_tool_call()["decision"], "allow")
        self.assertEqual(cp.holds._list_tool_asks(), [])

    def test_an_unconfigured_tool_is_denied_and_raises_no_card(self):
        # The divergence from the egress surface, asserted at the endpoint and not
        # only in ``policy._decide_tool``: an unmatched HOST is held because the set
        # of hosts is unbounded, while a server's tool set is finite and enumerable,
        # so refusing the unknown costs a configuration step and keeps the exposed
        # tool list a configuration artifact.
        self.assertEqual(_tool_call(tool="merge_pull_request")["decision"], "deny")
        self.assertEqual(cp.holds._list_tool_asks(), [])

    def test_a_denied_tool_is_denied_whether_or_not_it_was_ever_presented(self):
        # Presentation and execution are two axes. Withholding a schema is
        # ergonomics; this is the boundary, and it has to hold for a tool name that
        # arrived from anywhere — a transcript, a CLAUDE.md, an earlier tool result.
        _tool_rule("delete_file", "deny")
        self.assertEqual(_tool_call(tool="delete_file")["decision"], "deny")

    def test_an_unregistered_or_disabled_server_is_denied_by_the_authority(self):
        _tool_rule("get_me", "allow")
        self.assertEqual(_tool_call(server="mcp-nobody")["decision"], "deny")
        cp.api_mcp.edit_mcp_server("mcp-github",
                                   _edit_request(enabled=False),
                                   _FakeRequest())
        self.assertEqual(_tool_call()["decision"], "deny")

    def test_every_outcome_is_an_answer_rather_than_an_error(self):
        # A gateway must never have to read an HTTP status to learn what governance
        # said, so a refused name, an unknown server and a granted call all come back
        # the same way: a dict with a decision in it.
        _tool_rule("get_me", "allow")
        for kw in ({}, {"server": "mcp-nobody"}, {"tool": "nope"},
                   {"server": "", "tool": ""}):
            answer = _tool_call(**kw)
            with self.subTest(**kw):
                self.assertIn(answer["decision"], ("allow", "deny", "ask"))
                self.assertTrue(answer["reason"])

    def test_an_ask_comes_back_with_an_id_and_a_deadline_and_blocks_nothing(self):
        _tool_rule("create_pull_request", "ask")
        answer = _tool_call(tool="create_pull_request", args={"title": "x"})
        self.assertEqual(answer["decision"], "ask")
        self.assertFalse(answer["joined"])
        ask = cp.holds._get_tool_ask(_ask_id(answer))
        self.assertEqual(ask["status"], "pending")
        # Absolute, not a remaining-seconds count: a ticking field is stale on
        # arrival and would turn the SSE change-detector into a 1 Hz emitter.
        self.assertGreater(answer["deadline"], time.time())
        # Nothing blocked, so none of the egress registry exists for this.
        self.assertEqual(cp.holds._PENDING_EVENTS, {})

    def test_the_payload_reaches_the_card_that_a_human_will_read(self):
        _tool_rule("create_pull_request", "ask")
        _tool_call(tool="create_pull_request", args={"b": 2, "a": 1})
        card = cp.holds._pending_payload()["holds"][0]
        # The canonical form: key order normalized, nothing dropped or truncated.
        self.assertEqual(card["args_json"], '{"a":1,"b":2}')

    def test_an_identical_retry_joins_instead_of_raising_a_second_card(self):
        # What keeps a retrying agent from filling the operator's queue with copies
        # of one question. Reformulated key order is the same question.
        _tool_rule("create_pull_request", "ask")
        first = _tool_call(tool="create_pull_request", args={"a": 1, "b": 2})
        again = _tool_call(tool="create_pull_request", args={"b": 2, "a": 1})
        self.assertEqual(again["resume_token"], first["resume_token"])
        self.assertTrue(again["joined"])
        self.assertEqual(len(cp.holds._list_tool_asks()), 1)

    def test_a_different_argument_is_a_different_ask(self):
        # The half that matters: an approval a human gave for one payload must never
        # execute another, so any difference in a VALUE has to open its own card.
        _tool_rule("create_pull_request", "ask")
        first = _tool_call(tool="create_pull_request", args={"title": "one"})
        other = _tool_call(tool="create_pull_request", args={"title": "two"})
        self.assertNotEqual(_ask_id(other), _ask_id(first))

    def test_one_sandbox_never_joins_another_sandboxs_ask(self):
        _tool_rule("create_pull_request", "ask")
        mine = _tool_call(tool="create_pull_request", args={"a": 1})
        theirs = _tool_call(tool="create_pull_request", args={"a": 1},
                            client="172.30.0.137")
        self.assertNotEqual(_ask_id(theirs), _ask_id(mine))

    def test_an_oversized_payload_is_denied_rather_than_shown_in_part(self):
        # Refused, not truncated: this payload is what a human is shown as the thing
        # they are approving, so the hidden tail would be exactly where anything
        # worth hiding went.
        _tool_rule("create_pull_request", "ask")
        answer = _tool_call(tool="create_pull_request",
                            args={"body": "x" * (cp.holds.TOOL_ARGS_MAX + 1)})
        self.assertEqual(answer["decision"], "deny")
        self.assertIn("ceiling", answer["reason"])
        self.assertEqual(cp.holds._list_tool_asks(), [])

    def test_a_saturated_queue_denies_and_is_reported_in_the_one_banner(self):
        # Over a cap nothing raises a card, so the refusal is invisible in the queue
        # — which is what the saturation account exists to fix, unsplit across both
        # surfaces so an operator reads one banner rather than two.
        _tool_rule("create_pull_request", "ask")
        with mock.patch.object(cp.holds, "MAX_TOOL_PENDING", 1):
            self.assertEqual(
                _tool_call(tool="create_pull_request", args={"n": 1})["decision"],
                "ask")
            answer = _tool_call(tool="create_pull_request", args={"n": 2})
        self.assertEqual(answer["decision"], "deny")
        self.assertIn("fail-closed", answer["reason"])
        saturation = cp.holds._saturation()
        self.assertEqual(saturation["rejections"], 1)
        self.assertIn("tool asks", saturation["last_scope"])

    def test_a_per_client_flood_cannot_starve_the_other_sandbox(self):
        _tool_rule("create_pull_request", "ask")
        with mock.patch.object(cp.holds, "MAX_TOOL_PENDING_PER_CLIENT", 1):
            _tool_call(tool="create_pull_request", args={"n": 1})
            mine = _tool_call(tool="create_pull_request", args={"n": 2})
            theirs = _tool_call(tool="create_pull_request", args={"n": 3},
                                client="172.30.0.137")
        self.assertEqual(mine["decision"], "deny")
        self.assertEqual(theirs["decision"], "ask")

    def test_every_decision_is_audited_with_the_caller_and_its_class(self):
        # No governed path bypasses the log, and the record has to say WHICH
        # population called: this control plane is shared across sandboxes.
        _tool_rule("get_me", "allow")
        _tool_rule("create_pull_request", "ask")
        for kw, decision in (({}, "allow"),
                             ({"tool": "nope"}, "deny"),
                             ({"tool": "create_pull_request"}, "hold")):
            with mock.patch.object(cp.store, "_audit") as audited:
                _tool_call(**kw)
            with self.subTest(**kw):
                self.assertEqual(audited.call_args[0][0], decision)
                self.assertEqual(audited.call_args[1]["stage"], "tool-call")
                self.assertEqual(audited.call_args[1]["client"], CLASS_IP)
                self.assertEqual(audited.call_args[1]["client_class"], CLASS)

    def test_a_registered_ask_is_audited_as_a_hold_naming_its_id(self):
        # 'hold' rather than a new decision word: the vocabulary is shared with the
        # audit views, the filter facet and the page's <option> list, and "deferred to
        # a human" is what `hold` already means. The reason carries the difference.
        _tool_rule("create_pull_request", "ask")
        with mock.patch.object(cp.store, "_audit") as audited:
            answer = _tool_call(tool="create_pull_request", args={"n": 1})
        reason = audited.call_args[1]["reason"]
        self.assertIn(_ask_id(answer), reason)
        self.assertIn("nothing is blocked", reason)

    def test_the_audit_words_this_endpoint_writes_are_all_filterable(self):
        # The same coupling ``test_every_word_the_control_plane_writes_is_filterable``
        # asserts for the file as a whole, narrowed to the words added here — a new
        # decision word would be recorded, rendered, and quietly unfilterable.
        self.assertLessEqual({"allow", "deny", "hold"}, set(cp.audit.KINDS))

    def test_an_ask_answer_carries_the_grant_window_and_nothing_else_does(self):
        # The held result is where the agent plans its return, so the window an
        # approval stays redeemable travels with the ask. Allow and deny release or
        # refuse at once: there is no grant to wait on.
        _tool_rule("create_pull_request", "ask")
        answer = _tool_call(tool="create_pull_request", args={"title": "x"})
        self.assertEqual(answer["decision"], "ask")
        self.assertEqual(answer["grant_window"], cp.holds.TOOL_GRANT_TIMEOUT)
        _tool_rule("get_me", "allow")
        _tool_rule("delete_file", "deny")
        for tool in ("get_me", "delete_file"):
            with self.subTest(tool=tool):
                self.assertNotIn("grant_window", _tool_call(tool=tool))


def _pin(pins, tool="create_pull_request", server="mcp-github", expires_at=None,
         schema_digest=_SCHEMA, approval_id="a1"):
    """Write a pin as the card would, and return its id: a row in place before the
    card under test is answered."""
    with cp.store._connect() as conn:
        pin_id = conn.execute(
            "INSERT INTO tool_pins(server, tool, pins_json, approval_id, created_at, "
            "granted_by, expires_at, schema_digest) "
            "VALUES (?,?,?,?, 0, 'peer=172.31.0.3', ?, ?)",
            (server, tool, cp.policy._canonical_pins(pins), approval_id, expires_at,
             schema_digest)).lastrowid
        conn.commit()
    return pin_id


_PINNED = {"owner": "acme", "repo": "dockade"}
_PR_ARGS = {**_PINNED, "title": "t", "body": "b", "head": "topic", "base": "main"}


class PinnedAllowBridgeTests(_ToolBridgeTestCase):
    """``/tool/authorize`` with a pin: the call runs without a card, and the record
    says which pin answered it."""

    audits = True

    def setUp(self):
        super().setUp()
        _tool_rule("create_pull_request", "ask")
        self.pin_id = _pin(_PINNED)

    def test_a_pinned_call_is_allowed_and_raises_no_card(self):
        answer = _tool_call(tool="create_pull_request", args=_PR_ARGS)
        self.assertEqual(answer["decision"], "allow")
        self.assertNotIn("approval_id", answer)
        self.assertEqual(cp.holds._list_tool_asks(), [])

    def test_the_record_names_the_pin_and_carries_its_card(self):
        _tool_call(tool="create_pull_request", args=_PR_ARGS)
        with cp.store._connect() as conn:
            row = conn.execute("SELECT kind, stage, server, tool, approval_id, reason "
                               "FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((row["kind"], row["stage"], row["server"], row["tool"]),
                         ("allow", "tool-call", "mcp-github", "create_pull_request"))
        self.assertEqual(row["approval_id"], "a1")
        self.assertIn(f"by pin {self.pin_id}", row["reason"])
        # Field names, never values: the values are agent-authored.
        self.assertNotIn("dockade", row["reason"])

    def test_a_call_that_misses_the_pin_raises_a_card_as_before(self):
        answer = _tool_call(tool="create_pull_request",
                            args={**_PR_ARGS, "repo": "widgets"})
        self.assertEqual(answer["decision"], "ask")
        self.assertEqual(len(cp.holds._list_tool_asks()), 1)

    def test_a_payload_too_large_for_a_card_is_not_offered_to_the_pins(self):
        # The card refuses it rather than show it in part, and a pin answers only what
        # a card could have.
        answer = _tool_call(tool="create_pull_request",
                            args={**_PR_ARGS, "body": "x" * cp.holds.TOOL_ARGS_MAX})
        self.assertEqual(answer["decision"], "deny")
        self.assertIn("ceiling", answer["reason"])

    def test_a_payload_at_the_ceiling_is_still_answered(self):
        args = {**_PR_ARGS, "body": ""}
        args["body"] = "x" * (cp.holds.TOOL_ARGS_MAX - len(cp.holds._canonical_args(args)))
        self.assertEqual(len(cp.holds._canonical_args(args)), cp.holds.TOOL_ARGS_MAX)
        self.assertEqual(_tool_call(tool="create_pull_request", args=args)["decision"],
                         "allow")


class PinAuditApprovalIdTests(_ToolBridgeTestCase):
    """What a pin allowed, and the pin's revoke, carry the card the pin was granted
    from, so searching that card's id finds them beside the card's own rows."""

    audits = True
    CARD = "d" * 32
    LATER = "e" * 32

    def setUp(self):
        super().setUp()
        self.rule_id = _tool_rule("create_pull_request", "ask").body["id"]

    def _found(self, card):
        """What searching ``card`` finds, as (kind, stage, approval_id)."""
        return sorted((r["kind"], r["stage"], r["approval_id"])
                      for r in cp.api_views.api_audit_events(q=card)["rows"])

    def _calls(self):
        """The tool-call rows, oldest first, as (kind, approval_id)."""
        with cp.store._connect() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT kind, approval_id FROM audit WHERE stage='tool-call' "
                "ORDER BY id").fetchall()]

    def test_searching_a_card_id_finds_the_calls_its_pin_allowed(self):
        card = _ask_id(_tool_call(tool="create_pull_request", args=_PR_ARGS))
        _resolve(card, "allow_pinned", pins=["owner", "repo"])
        self.assertTrue(_claim(card).body["ok"])
        for title in ("another", "a third"):
            self.assertEqual(_tool_call(tool="create_pull_request",
                                        args={**_PR_ARGS, "title": title})["decision"],
                             "allow")
        self.assertEqual(self._found(card),
                         [("allow", "tool-ask", card), ("allow", "tool-call", card),
                          ("allow", "tool-call", card), ("allow", "tool-resume", card),
                          ("create", "tool-policy", card), ("hold", "tool-call", card)])

    def test_a_call_the_pin_does_not_answer_is_not_credited_to_it(self):
        _pin(_PINNED, approval_id=self.CARD)
        missed = _ask_id(_tool_call(tool="create_pull_request",
                                    args={**_PR_ARGS, "repo": "widgets"}))
        _edit_tool_rule(self.rule_id, "allow")
        _tool_call(tool="create_pull_request", args=_PR_ARGS)
        # The miss raised its own card and names it; the rule decided the last call.
        self.assertEqual(self._calls(), [("hold", missed), ("allow", None)])

    def test_revoking_a_live_pin_names_its_card(self):
        pin_id = _pin(_PINNED, approval_id=self.CARD)
        _tool_call(tool="create_pull_request", args=_PR_ARGS)
        cp.api_mcp.revoke_mcp_pin(pin_id, _FakeRequest())
        self.assertEqual(self._found(self.CARD),
                         [("allow", "tool-call", self.CARD),
                          ("revoke", "tool-policy", self.CARD)])

    def test_removing_an_expired_pin_names_its_card(self):
        pin_id = _pin(_PINNED, expires_at=time.time() - 1, approval_id=self.CARD)
        cp.api_mcp.revoke_mcp_pin(pin_id, _FakeRequest())
        [row] = cp.api_views.api_audit_events(q=self.CARD)["rows"]
        self.assertEqual((row["kind"], row["approval_id"]), ("revoke", self.CARD))
        self.assertIn(f"expired pin {pin_id} removed", row["reason"])

    def test_the_glance_folds_one_pins_allows_and_splits_on_a_new_card(self):
        # A pin's allows are one human decision, so they fold; the pin rebound from
        # a later card is a second decision, so its allows are a line of their own.
        pin_id = _pin(_PINNED, approval_id=self.CARD)
        for _ in range(2):
            _tool_call(tool="create_pull_request", args=_PR_ARGS)

        def allow_lines():
            with cp.store._connect() as conn:
                return sorted(r["n"] for r in cp.audit.grouped(
                    conn, 50, cp.audit.parse(), 500)
                    if r["kind"] == "allow" and r["stage"] == "tool-call")

        self.assertEqual(allow_lines(), [2])
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_pins SET approval_id=? WHERE id=?",
                         (self.LATER, pin_id))
            conn.commit()
        _tool_call(tool="create_pull_request", args=_PR_ARGS)
        self.assertEqual(allow_lines(), [1, 2])


class McpPinTests(_CPTestCase):
    """``/api/mcp/pins`` and taking a pin back, and what a pin does to its rule's
    verbs."""

    audits = True

    def setUp(self):
        super().setUp()
        _register()
        _enable()
        self.rule_id = _tool_rule("create_pull_request", "ask").body["id"]
        self.pin_id = _pin(_PINNED)

    def _edit_rule(self, action):
        return _edit_tool_rule(self.rule_id, action)

    def _decides(self):
        return {p["id"]: p["decides"] for p in cp.api_mcp.api_mcp_pins()}

    def test_the_view_serves_the_pin_set_and_its_provenance(self):
        [served] = cp.api_mcp.api_mcp_pins()
        self.assertEqual(served["pins"], _PINNED)
        # As stored, for the page to show without a parse that rounds large integers.
        self.assertEqual(served["pins_json"], cp.policy._canonical_pins(_PINNED))
        self.assertEqual((served["server"], served["tool"], served["rule"]),
                         ("mcp-github", "create_pull_request", "ask"))
        self.assertEqual(served["approval_id"], "a1")
        self.assertIsNone(served["expires_at"])
        self.assertTrue(served["decides"])

    def test_the_view_says_a_pin_decides_only_when_the_decision_would_read_it(self):
        for action in ("allow", "deny"):
            with self.subTest(action=action):
                self._edit_rule(action)
                self.assertEqual(self._decides(), {self.pin_id: False})
        self._edit_rule("ask")
        self.assertEqual(self._decides(), {self.pin_id: True})
        cp.api_mcp.edit_mcp_server("mcp-github",
                                   _edit_request(enabled=False),
                                   _FakeRequest())
        self.assertEqual(self._decides(), {self.pin_id: False})

    def test_an_unreadable_row_is_listed_as_deciding_nothing(self):
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_pins SET pins_json='{}' WHERE id=?",
                         (self.pin_id,))
            conn.commit()
        [served] = cp.api_mcp.api_mcp_pins()
        self.assertIsNone(served["pins"])
        self.assertFalse(served["decides"])

    def test_editing_the_rule_keeps_its_pins(self):
        self._edit_rule("deny")
        self._edit_rule("ask")
        self.assertEqual(
            _tool_call(tool="create_pull_request", args=_PR_ARGS)["decision"], "allow")

    def test_revoking_a_pin_sends_its_calls_back_to_a_card(self):
        resp = cp.api_mcp.revoke_mcp_pin(self.pin_id, _FakeRequest())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(cp.api_mcp.api_mcp_pins(), [])
        self.assertEqual(
            _tool_call(tool="create_pull_request", args=_PR_ARGS)["decision"], "ask")

    def test_a_revoked_pin_is_audited_with_its_fields_and_actor(self):
        cp.api_mcp.revoke_mcp_pin(self.pin_id, _FakeRequest())
        with cp.store._connect() as conn:
            row = conn.execute("SELECT kind, stage, tool, actor, reason FROM audit "
                               "WHERE kind='revoke' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((row["kind"], row["stage"], row["tool"]),
                         ("revoke", "tool-policy", "create_pull_request"))
        self.assertIsNotNone(row["actor"])
        self.assertNotIn("peer=", row["reason"])
        self.assertIn(f"pin {self.pin_id}", row["reason"])
        self.assertIn("owner, repo", row["reason"])

    def test_revoking_an_unknown_pin_is_a_404(self):
        self.assertEqual(
            cp.api_mcp.revoke_mcp_pin(self.pin_id + 1, _FakeRequest()).status_code, 404)

    def _expire(self, pin_id, offset):
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_pins SET expires_at=? WHERE id=?",
                         (time.time() + offset, pin_id))
            conn.commit()

    def _revoke_reason(self):
        with cp.store._connect() as conn:
            return conn.execute("SELECT reason FROM audit WHERE kind='revoke' "
                                "ORDER BY id DESC LIMIT 1").fetchone()["reason"]

    def test_the_view_serves_a_timed_pins_deadline_and_leaves_out_an_expired_one(self):
        deadline = time.time() + 600
        self._expire(self.pin_id, 600)
        [served] = cp.api_mcp.api_mcp_pins()
        self.assertAlmostEqual(served["expires_at"], deadline, delta=5)
        self._expire(self.pin_id, -1)
        self.assertEqual(cp.api_mcp.api_mcp_pins(), [])

    def test_revoking_a_timed_pin_names_the_time_it_had_left(self):
        self._expire(self.pin_id, 7200)
        resp = cp.api_mcp.revoke_mcp_pin(self.pin_id, _FakeRequest())
        self.assertTrue(resp.body["was_live"])
        self.assertIn("revoked with 1h59m left", self._revoke_reason())

    def test_revoking_an_expired_pin_says_it_had_already_stopped(self):
        # The page listed it before it lapsed; the click still removes it.
        self._expire(self.pin_id, -1)
        resp = cp.api_mcp.revoke_mcp_pin(self.pin_id, _FakeRequest())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.body["was_live"])
        self.assertIn("expired pin", self._revoke_reason())

    def test_a_live_timed_pin_holds_its_rule_as_a_permanent_one_does(self):
        self._expire(self.pin_id, 600)
        self.assertEqual(
            cp.api_mcp.revoke_mcp_rule(self.rule_id, _FakeRequest()).status_code, 409)

    def test_a_refused_rule_revoke_counts_only_live_pins_and_keeps_the_lapsed_one(self):
        lapsed = _pin({"owner": "acme"}, expires_at=time.time() - 1)
        refused = cp.api_mcp.revoke_mcp_rule(self.rule_id, _FakeRequest())
        self.assertEqual((refused.status_code, refused.body["tool_pins"]), (409, 1))
        with cp.store._connect() as conn:
            ids = {r["id"] for r in conn.execute("SELECT id FROM tool_pins")}
        self.assertEqual(ids, {self.pin_id, lapsed})

    def test_a_pin_at_its_deadline_has_stopped_answering(self):
        # `> now`, not `>=`, in the decision and the view alike.
        deadline = time.time() + 600
        self._expire(self.pin_id, 600)
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_pins SET expires_at=?", (deadline,))
            conn.commit()
        with mock.patch("time.time", return_value=deadline):
            self.assertEqual(cp.api_mcp.api_mcp_pins(), [])
            self.assertEqual(cp.policy._decide_tool(
                "mcp-github", "create_pull_request", _PR_ARGS, _SCHEMA)[0], "ask")

    def test_a_live_timed_pin_answers_again_when_its_rule_asks_again(self):
        # Editing the rule keeps its pins, and a timed one keeps its deadline.
        self._expire(self.pin_id, 600)
        self._edit_rule("deny")
        self.assertEqual(
            _tool_call(tool="create_pull_request", args=_PR_ARGS)["decision"], "deny")
        self._edit_rule("ask")
        self.assertEqual(
            _tool_call(tool="create_pull_request", args=_PR_ARGS)["decision"], "allow")

    def test_a_deadline_that_is_not_a_number_cannot_be_stored(self):
        # SQLite orders text above every number, so a text deadline would read as live
        # in every query on this table. The column refuses it, hand edits included.
        with cp.store._connect() as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
                conn.execute("UPDATE tool_pins SET expires_at='2099-01-01'")
            # A number written as text is a number: the column's REAL affinity.
            conn.execute("UPDATE tool_pins SET expires_at='4102444800'")
            self.assertEqual(conn.execute(
                "SELECT typeof(expires_at) FROM tool_pins").fetchone()[0], "real")

    def test_expired_pins_do_not_hold_their_rule_and_go_with_it(self):
        # An expired pin cannot come back with the next `ask` rule, so it holds
        # nothing back; a pin on another tool is not touched.
        _tool_rule("update_pull_request", "ask")
        other = _pin(_PINNED, tool="update_pull_request", expires_at=time.time() - 1)
        self._expire(self.pin_id, -1)
        self.assertEqual(
            cp.api_mcp.revoke_mcp_rule(self.rule_id, _FakeRequest()).status_code, 200)
        with cp.store._connect() as conn:
            left = [r["id"] for r in conn.execute("SELECT id FROM tool_pins")]
        self.assertEqual(left, [other])
        with cp.store._connect() as conn:
            reason = conn.execute("SELECT reason FROM audit WHERE kind='revoke' "
                                  "ORDER BY id DESC LIMIT 1").fetchone()["reason"]
        self.assertIn("1 expired pin(s) removed with it", reason)

    def test_a_rule_with_pins_cannot_be_revoked_until_they_are(self):
        # A pin left behind would come back, unasked, with the next `ask` rule for
        # the same tool.
        refused = cp.api_mcp.revoke_mcp_rule(self.rule_id, _FakeRequest())
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.body["tool_pins"], 1)
        self.assertEqual(cp.policy._decide_tool("mcp-github",
                                                "create_pull_request")[0], "ask")
        cp.api_mcp.revoke_mcp_pin(self.pin_id, _FakeRequest())
        self.assertEqual(
            cp.api_mcp.revoke_mcp_rule(self.rule_id, _FakeRequest()).status_code, 200)


class PinFromCardTests(_ToolBridgeTestCase):
    """``allow_pinned``: allowing a tool ask and pinning the fields an operator ticked,
    so that later calls carrying the same values run without a card."""

    audits = True

    def setUp(self):
        super().setUp()
        self.rule_id = _tool_rule("create_pull_request", "ask").body["id"]
        self.ask = _ask_id(_tool_call(tool="create_pull_request", args=_PR_ARGS))

    def _pin_rows(self):
        with cp.store._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT server, tool, pins_json, approval_id, granted_by "
                "FROM tool_pins ORDER BY id")]

    def test_the_card_offers_what_resolve_will_accept(self):
        [card] = cp.holds._pending_payload()["holds"]
        options = card["pin_options"]
        self.assertIsNone(options["refused"])
        self.assertEqual({o["field"] for o in options["fields"]}, set(_PR_ARGS))

    def test_pinning_allows_this_call_and_answers_the_next_one(self):
        resp = _resolve(self.ask, "allow_pinned", pins=["owner", "repo"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.body["pin"]["fields"], ["owner", "repo"])
        self.assertTrue(resp.body["pin"]["created"])
        self.assertEqual(cp.holds._get_tool_ask(self.ask)["status"], "allowed")
        self.assertTrue(_claim(self.ask).body["ok"])
        # A different PR on the same repository runs without a card.
        later = _tool_call(tool="create_pull_request",
                           args={**_PR_ARGS, "title": "another", "head": "other"})
        self.assertEqual(later["decision"], "allow")

    def test_the_values_come_from_the_stored_ask_and_the_pin_names_its_card(self):
        _resolve(self.ask, "allow_pinned", pins=["repo"])
        [pin] = self._pin_rows()
        self.assertEqual(pin["pins_json"], cp.policy._canonical_pins({"repo": "dockade"}))
        self.assertEqual((pin["approval_id"], pin["tool"]),
                         (self.ask, "create_pull_request"))
        self.assertTrue(pin["granted_by"])

    def test_a_field_the_ask_does_not_offer_is_refused_and_nothing_is_written(self):
        for pins in (["reviewers"], ["owner", "nope"], [], ["owner", "owner"], "owner",
                     [{"owner": "x"}], None):
            with self.subTest(pins=pins):
                self.assertEqual(
                    _resolve(self.ask, "allow_pinned", pins=pins).status_code, 400)
        self.assertEqual(cp.holds._get_tool_ask(self.ask)["status"], "pending")
        self.assertEqual(self._pin_rows(), [])

    def test_pins_on_any_other_action_are_refused(self):
        for action in ("allow", "deny"):
            with self.subTest(action=action):
                self.assertEqual(
                    _resolve(self.ask, action, pins=["owner"]).status_code, 400)
        self.assertEqual(cp.holds._get_tool_ask(self.ask)["status"], "pending")

    def test_a_rule_moved_while_the_card_was_pending_refuses_the_pin_only(self):
        for action in ("deny", "allow"):
            with self.subTest(action=action):
                _edit_tool_rule(self.rule_id, action)
                resp = _resolve(self.ask, "allow_pinned", pins=["owner"])
                self.assertEqual(resp.status_code, 409)
                self.assertTrue(resp.body["pin_refused"])
                self.assertIn(f"is ruled '{action}'", resp.body["detail"])
        self.assertEqual(self._pin_rows(), [])
        # The card is still decidable, and a plain allow still works.
        self.assertEqual(_resolve(self.ask, "allow").status_code, 200)

    def test_a_pin_already_in_place_allows_the_call_and_writes_nothing(self):
        _resolve(self.ask, "allow_pinned", pins=["owner", "repo"])
        second = _ask_id(_tool_call(tool="create_pull_request",
                            args={**_PR_ARGS, "repo": "widgets"}))
        # The same values as the first pin, from a call it did not answer.
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_pins SET pins_json=?",
                         (cp.policy._canonical_pins({"owner": "acme",
                                                     "repo": "widgets"}),))
            conn.commit()
        resp = _resolve(second, "allow_pinned", pins=["owner", "repo"])
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.body["pin"]["created"])
        self.assertEqual(len(self._pin_rows()), 1)

    def test_an_answered_card_cannot_be_pinned(self):
        _resolve(self.ask, "deny")
        resp = _resolve(self.ask, "allow_pinned", pins=["owner"])
        self.assertEqual(resp.status_code, 409)
        self.assertNotIn("pin_refused", resp.body)
        self.assertEqual(self._pin_rows(), [])

    def test_the_record_has_the_answer_and_the_policy_write_under_one_id(self):
        _resolve(self.ask, "allow_pinned", pins=["owner", "repo"])
        with cp.store._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT kind, stage, actor, reason FROM audit WHERE approval_id=? "
                "AND stage IN ('tool-ask', 'tool-policy') ORDER BY id", (self.ask,))]
        self.assertEqual([(r["kind"], r["stage"]) for r in rows],
                         [("allow", "tool-ask"), ("create", "tool-policy")])
        for row in rows:
            self.assertIn("owner, repo", row["reason"])
            self.assertIsNotNone(row["actor"])
            self.assertNotIn("acme", row["reason"])


class TimedPinFromCardTests(_ToolBridgeTestCase):
    """``allow_pinned_lease``: a pin from a card that answers for
    ``policy.PIN_LEASE_SECONDS``, and what a second grant of the same values does to
    it. One row per pin set, so a second grant under the same digest only ever widens
    the one in place (``SchemaBoundPinFromCardTests`` has the rebind)."""

    audits = True

    def setUp(self):
        super().setUp()
        saved = cp.policy.PIN_LEASE_SECONDS
        cp.policy.PIN_LEASE_SECONDS = 300.0
        self.addCleanup(setattr, cp.policy, "PIN_LEASE_SECONDS", saved)
        self.rule_id = _tool_rule("create_pull_request", "ask").body["id"]
        self.ask = _ask_id(_tool_call(tool="create_pull_request", args=_PR_ARGS))

    def _pins(self):
        with cp.store._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, expires_at FROM tool_pins ORDER BY id")]

    def _allow_reason(self):
        with cp.store._connect() as conn:
            return conn.execute(
                "SELECT reason FROM audit WHERE approval_id=? AND stage='tool-ask' "
                "AND kind='allow'", (self.ask,)).fetchone()["reason"]

    def _policy_rows(self):
        with cp.store._connect() as conn:
            return [(r["kind"], r["reason"]) for r in conn.execute(
                "SELECT kind, reason FROM audit WHERE approval_id=? "
                "AND stage='tool-policy' ORDER BY id", (self.ask,))]

    def _later_call(self):
        return _tool_call(tool="create_pull_request",
                          args={**_PR_ARGS, "title": "another"})["decision"]

    def test_a_timed_pin_answers_until_its_deadline_and_not_after(self):
        before = time.time()
        resp = _resolve(self.ask, "allow_pinned_lease", pins=["owner", "repo"])
        self.assertEqual(resp.status_code, 200)
        pin = resp.body["pin"]
        self.assertTrue(pin["created"])
        self.assertGreaterEqual(pin["expires_at"], before + 300)
        self.assertLess(pin["expires_at"], before + 310)
        # What the card is told is what was stored.
        self.assertEqual(self._pins(), [{"id": pin["id"],
                                         "expires_at": pin["expires_at"]}])
        self.assertEqual(self._later_call(), "allow")
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_pins SET expires_at=?", (time.time() - 1,))
            conn.commit()
        self.assertEqual(self._later_call(), "ask")

    def test_the_policy_row_says_how_long_the_pin_answers_for(self):
        _resolve(self.ask, "allow_pinned_lease", pins=["owner", "repo"])
        [(kind, reason)] = self._policy_rows()
        self.assertEqual(kind, "create")
        self.assertIn("for 5m00s while its rule asks", reason)

    def test_a_timed_pin_is_refused_where_a_permanent_one_is(self):
        for pins in ([], ["reviewers"], None):
            with self.subTest(pins=pins):
                self.assertEqual(_resolve(self.ask, "allow_pinned_lease",
                                          pins=pins).status_code, 400)
        _edit_tool_rule(self.rule_id, "deny")
        resp = _resolve(self.ask, "allow_pinned_lease", pins=["owner"])
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.body["pin_refused"])
        self.assertEqual(self._pins(), [])

    def test_a_timed_pin_on_values_pinned_for_good_leaves_them_permanent(self):
        permanent = _pin(_PINNED)
        resp = _resolve(self.ask, "allow_pinned_lease", pins=["owner", "repo"])
        pin = resp.body["pin"]
        self.assertEqual((pin["id"], pin["created"], pin["extended"], pin["expires_at"]),
                         (permanent, False, False, None))
        self.assertEqual(self._pins(), [{"id": permanent, "expires_at": None}])
        self.assertEqual(self._policy_rows(), [])
        # The record says the grant has no end, and that the click asked for one.
        self.assertIn("already in place for good; a timed pin was asked for",
                      self._allow_reason())

    def test_a_permanent_pin_on_a_timed_one_removes_its_expiry(self):
        timed = _pin(_PINNED, expires_at=time.time() + 60)
        resp = _resolve(self.ask, "allow_pinned", pins=["owner", "repo"])
        self.assertTrue(resp.body["pin"]["extended"])
        self.assertIsNone(resp.body["pin"]["expires_at"])
        self.assertEqual(self._pins(), [{"id": timed, "expires_at": None}])
        [(kind, reason)] = self._policy_rows()
        self.assertEqual(kind, "edit")
        self.assertIn("with no expiry", reason)
        self.assertIn("left before it", reason)

    def test_an_extended_pin_names_the_card_and_resolver_that_widened_it(self):
        _pin(_PINNED, expires_at=time.time() + 60)        # granted from 172.31.0.3
        _resolve(self.ask, "allow_pinned", _FakeRequest(peer="172.31.0.9"),
                 pins=["owner", "repo"])
        with cp.store._connect() as conn:
            row = conn.execute("SELECT approval_id, granted_by FROM tool_pins").fetchone()
        self.assertEqual(row["approval_id"], self.ask)
        self.assertIn("172.31.0.9", row["granted_by"])

    def test_a_second_timed_pin_renews_the_first(self):
        timed = _pin(_PINNED, expires_at=time.time() + 60)
        resp = _resolve(self.ask, "allow_pinned_lease", pins=["owner", "repo"])
        self.assertTrue(resp.body["pin"]["extended"])
        self.assertGreater(resp.body["pin"]["expires_at"], time.time() + 290)
        self.assertEqual(self._pins()[0]["id"], timed)
        [(kind, reason)] = self._policy_rows()
        self.assertEqual(kind, "edit")
        self.assertIn("for 5m00s", reason)

    def test_a_renewal_never_shortens_a_pin(self):
        # Reachable when the duration is configured shorter than it was.
        deadline = time.time() + 3600
        _pin(_PINNED, expires_at=deadline)
        resp = _resolve(self.ask, "allow_pinned_lease", pins=["owner", "repo"])
        self.assertFalse(resp.body["pin"]["extended"])
        self.assertEqual(resp.body["pin"]["expires_at"], deadline)
        self.assertEqual(self._pins()[0]["expires_at"], deadline)
        self.assertRegex(self._allow_reason(), r"already in place with (59|60)m\d\ds left")

    def test_a_lapsed_pin_is_swept_and_granted_again_under_a_new_id(self):
        # AUTOINCREMENT: the swept row had the highest id, and it is not handed on.
        lapsed = _pin(_PINNED, expires_at=time.time() - 1)
        resp = _resolve(self.ask, "allow_pinned_lease", pins=["owner", "repo"])
        self.assertTrue(resp.body["pin"]["created"])
        [row] = self._pins()
        self.assertGreater(row["id"], lapsed)


class SchemaBoundPinBridgeTests(_ToolBridgeTestCase):
    """``/tool/authorize`` with the digest of the tool's arguments the gateway served:
    a pin answers only a call made under the digest it was pinned under, the hold row
    names a pin the call met under another, and a card joins only its own digest."""

    audits = True

    def setUp(self):
        super().setUp()
        _tool_rule("create_pull_request", "ask")
        self.pin_id = _pin(_PINNED)

    def _hold_reason(self, approval_id):
        with cp.store._connect() as conn:
            return conn.execute(
                "SELECT reason FROM audit WHERE kind='hold' AND approval_id=?",
                (approval_id,)).fetchone()["reason"]

    def test_a_call_under_the_pins_digest_is_answered(self):
        self.assertEqual(_tool_call(tool="create_pull_request", args=_PR_ARGS)
                         ["decision"], "allow")

    def test_a_call_under_a_moved_digest_gets_a_card_naming_the_pin(self):
        answer = _tool_call(tool="create_pull_request", args=_PR_ARGS,
                            schema_digest=_MOVED)
        self.assertEqual(answer["decision"], "ask")
        reason = self._hold_reason(_ask_id(answer))
        self.assertIn(f"pin {self.pin_id} on owner, repo matches the call's values but "
                      f"was pinned under other arguments of the tool", reason)
        self.assertIn(f"registered as tool ask {_ask_id(answer)}", reason)

    def test_a_call_with_no_digest_or_a_malformed_one_gets_a_card(self):
        for digest in (None, "", "5C" * 32, "5c" * 31, _SCHEMA + "\n", 7):
            with self.subTest(digest=digest):
                answer = _tool_call(tool="create_pull_request", args=_PR_ARGS,
                                    schema_digest=digest)
                self.assertEqual(answer["decision"], "ask")
                self.assertIn("the gateway sent no digest",
                              self._hold_reason(_ask_id(answer)))
                _resolve(_ask_id(answer), "deny")

    def test_a_retry_under_a_new_digest_opens_a_new_card(self):
        args = {**_PR_ARGS, "repo": "widgets"}
        first = _tool_call(tool="create_pull_request", args=args)
        same = _tool_call(tool="create_pull_request", args=args)
        moved = _tool_call(tool="create_pull_request", args=args,
                           schema_digest=_MOVED)
        self.assertTrue(same["joined"])
        self.assertEqual(_ask_id(same), _ask_id(first))
        self.assertFalse(moved["joined"])
        self.assertNotEqual(_ask_id(moved), _ask_id(first))
        self.assertEqual(len(cp.holds._list_tool_asks()), 2)

    def test_the_card_stores_the_digest_its_call_was_made_under(self):
        answer = _tool_call(tool="create_pull_request", args=_PR_ARGS,
                            schema_digest=_MOVED)
        self.assertEqual(cp.holds._get_tool_ask(_ask_id(answer))["schema_digest"],
                         _MOVED)


class SchemaBoundPinFromCardTests(_ToolBridgeTestCase):
    """Pinning from a card binds the pin to the card's digest, and pinning the same
    fields from a card raised under another digest rebinds it: a new grant, for as
    long as that click says."""

    audits = True

    def setUp(self):
        super().setUp()
        saved = cp.policy.PIN_LEASE_SECONDS
        cp.policy.PIN_LEASE_SECONDS = 300.0
        self.addCleanup(setattr, cp.policy, "PIN_LEASE_SECONDS", saved)
        _tool_rule("create_pull_request", "ask")

    def _card(self, digest=_SCHEMA, **changes):
        return _ask_id(_tool_call(tool="create_pull_request", args={**_PR_ARGS, **changes},
                          schema_digest=digest))

    def _pins(self):
        with cp.store._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, approval_id, granted_by, expires_at, schema_digest "
                "FROM tool_pins ORDER BY id")]

    def _options(self, approval_id):
        [card] = [c for c in cp.holds._pending_payload()["holds"]
                  if c["id"] == approval_id]
        return card["pin_options"]

    def _call(self, digest):
        return _tool_call(tool="create_pull_request",
                          args={**_PR_ARGS, "title": "another"},
                          schema_digest=digest)["decision"]

    def _policy_rows(self, approval_id):
        with cp.store._connect() as conn:
            return [(r["kind"], r["reason"]) for r in conn.execute(
                "SELECT kind, reason FROM audit WHERE approval_id=? "
                "AND stage='tool-policy' ORDER BY id", (approval_id,))]

    def test_a_card_with_no_digest_offers_no_pin_and_refuses_one(self):
        # A pin its call meets first, which a card with no digest names no rebind of.
        pinned = self._card()
        _resolve(pinned, "allow_pinned", pins=["owner", "repo"])
        before = self._pins()
        ask = self._card(digest=None)
        options = self._options(ask)
        self.assertEqual(options["fields"], [])
        self.assertEqual(options["stale"], [])
        self.assertIn("no digest of the tool's arguments", options["refused"])
        resp = _resolve(ask, "allow_pinned", pins=["owner", "repo"])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.body["detail"], options["refused"])
        self.assertEqual(self._pins(), before)
        self.assertEqual(_resolve(ask, "allow").status_code, 200)

    def test_a_pin_is_bound_to_its_cards_digest(self):
        ask = self._card(digest=_MOVED)
        _resolve(ask, "allow_pinned", pins=["owner", "repo"])
        [pin] = self._pins()
        self.assertEqual((pin["approval_id"], pin["schema_digest"]), (ask, _MOVED))
        self.assertEqual(self._call(_MOVED), "allow")
        self.assertEqual(self._call(_SCHEMA), "ask")

    def test_the_card_names_a_pin_its_call_met_under_another_digest(self):
        first = self._card()
        _resolve(first, "allow_pinned", pins=["owner", "repo"])
        [pin] = self._pins()
        moved = self._card(digest=_MOVED)
        self.assertEqual(self._options(moved)["stale"],
                         [{"id": pin["id"], "fields": ["owner", "repo"],
                           "refused": None}])
        # A card raised under the pin's own digest is not met by it but answered, so
        # only a call that missed it on other values has a card, and names none.
        other = self._card(repo="widgets")
        self.assertEqual(self._options(other)["stale"], [])

    def test_a_rebound_pin_answers_under_the_new_digest_and_asks_under_the_old(self):
        first = self._card()
        _resolve(first, "allow_pinned", pins=["owner", "repo"])
        [before] = self._pins()
        self.assertEqual(self._call(_MOVED), "ask")
        moved = cp.holds._list_tool_asks()[-1]["id"]
        resp = _resolve(moved, "allow_pinned", pins=["owner", "repo"],
                        request=_FakeRequest(peer="172.31.0.9"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.body["pin"]["rebound"])
        self.assertFalse(resp.body["pin"]["created"])
        # The row names this card and the resolver who rebound it.
        self.assertNotEqual(before["granted_by"], "peer=172.31.0.9")
        self.assertEqual(self._pins(), [{**before, "approval_id": moved,
                                         "granted_by": "peer=172.31.0.9",
                                         "schema_digest": _MOVED}])
        self.assertEqual(self._call(_MOVED), "allow")
        self.assertEqual(self._call(_SCHEMA), "ask")
        [(kind, reason)] = self._policy_rows(moved)
        self.assertEqual(kind, "edit")
        self.assertIn(f"pin {before['id']} rebound to the tool's current arguments "
                      f"from this ask (no expiry before it)", reason)
        self.assertIn("with no expiry, while its rule asks", reason)

    def test_a_timed_rebind_of_a_permanent_pin_is_timed_and_audited_as_an_edit(self):
        first = self._card()
        _resolve(first, "allow_pinned", pins=["owner", "repo"])
        moved = self._card(digest=_MOVED)
        now = time.time()
        resp = _resolve(moved, "allow_pinned_lease", pins=["owner", "repo"])
        pin = resp.body["pin"]
        self.assertTrue(pin["rebound"])
        self.assertGreaterEqual(pin["expires_at"], now + 300)
        self.assertLess(pin["expires_at"], now + 310)
        self.assertEqual([p["expires_at"] for p in self._pins()], [pin["expires_at"]])
        [(kind, reason)] = self._policy_rows(moved)
        self.assertEqual(kind, "edit")
        self.assertIn("for 5m00s while its rule asks", reason)

    def test_a_rebind_of_a_timed_pin_says_how_long_it_had_left(self):
        first = self._card()
        _resolve(first, "allow_pinned_lease", pins=["owner", "repo"])
        moved = self._card(digest=_MOVED)
        _resolve(moved, "allow_pinned", pins=["owner", "repo"])
        [(kind, reason)] = self._policy_rows(moved)
        self.assertEqual(kind, "edit")
        self.assertRegex(reason, r"from this ask \(\dm\d\ds left before it\)")
        self.assertIn("with no expiry, while its rule asks", reason)
        self.assertEqual([p["expires_at"] for p in self._pins()], [None])

    def test_a_card_raised_before_the_one_a_pin_was_bound_from_cannot_rebind_it(self):
        old = self._card()
        later = self._card(digest=_MOVED)
        with cp.store._connect() as conn:          # strictly earlier, whatever the clock
            conn.execute("UPDATE tool_approvals SET ts = ts - 10 WHERE id=?", (old,))
            conn.commit()
        _resolve(later, "allow_pinned", pins=["owner", "repo"])
        [pin] = self._pins()
        [stale] = self._options(old)["stale"]
        self.assertEqual(stale["id"], pin["id"])
        self.assertIn("raised after this one", stale["refused"])
        resp = _resolve(old, "allow_pinned_lease", pins=["owner", "repo"])
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.body["pin_refused"])
        self.assertEqual(resp.body["detail"], stale["refused"])
        self.assertEqual(self._pins(), [pin])
        self.assertEqual(cp.holds._get_tool_ask(old)["status"], "pending")
        # Other fields are a new pin, not a rebind, and the card can still be allowed.
        self.assertEqual(_resolve(old, "allow_pinned", pins=["repo"]).status_code, 200)
        self.assertEqual(len(self._pins()), 2)


class SchemaBoundPinViewTests(_CPTestCase):
    """``/api/mcp/pins`` with pins bound to a digest: a row bound to none decides
    nothing, and a pin whose tool's arguments the inventory saw move says so."""

    def setUp(self):
        super().setUp()
        _register()
        _enable()
        _tool_rule("create_pull_request", "ask")
        self.pin_id = _pin(_PINNED)

    def _observe(self, digest):
        cp.inventory.record({"servers": {"mcp-github": {
            "status": "ok", "tools": [{"name": "create_pull_request",
                                       "digest": digest}]}}})

    def test_a_pin_under_the_inventorys_digest_has_not_moved(self):
        self._observe(_SCHEMA)
        [served] = cp.api_mcp.api_mcp_pins()
        self.assertEqual((served["decides"], served["bound"], served["schema_moved"]),
                         (True, True, False))

    def test_a_pin_under_an_older_digest_says_its_schema_moved(self):
        # Display only: `decides` stays the rule's conditions, since which digest a
        # call is made under is per call.
        self._observe(_MOVED)
        [served] = cp.api_mcp.api_mcp_pins()
        self.assertEqual((served["decides"], served["schema_moved"]), (True, True))

    def test_without_an_observed_digest_nothing_is_said_to_have_moved(self):
        [served] = cp.api_mcp.api_mcp_pins()
        self.assertFalse(served["schema_moved"])
        cp.inventory.record({"servers": {"mcp-github": {
            "status": "ok", "tools": [{"name": "create_pull_request"}]}}})
        [served] = cp.api_mcp.api_mcp_pins()
        self.assertFalse(served["schema_moved"])

    def test_a_row_bound_to_no_digest_decides_nothing(self):
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_pins SET schema_digest=NULL")
            conn.commit()
        self._observe(_MOVED)
        [served] = cp.api_mcp.api_mcp_pins()
        self.assertEqual((served["decides"], served["bound"], served["schema_moved"]),
                         (False, False, False))


class ToolRosterTests(_ToolBridgeTestCase):
    """``GET /tool/roster`` — what the gateway may dial and what it may present.

    The pollable half of the bridge, and the split from the decision endpoint is the
    point: this answers "what is configured", which is needed at session start and on
    change, while execution policy is per-call and must never be cached."""

    def test_an_enabled_server_arrives_with_its_rules(self):
        _tool_rule("get_me", "allow")
        _tool_rule("create_pull_request", "ask")
        roster = cp.api_tool.tool_roster()
        self.assertEqual([s["server"] for s in roster], ["mcp-github"])
        self.assertEqual(roster[0]["tools"],
                         [{"tool": "create_pull_request", "action": "ask"},
                          {"tool": "get_me", "action": "allow"}])

    def test_a_disabled_server_is_absent_rather_than_reported_as_disabled(self):
        # The whole meaning of the switch: the gateway dials what the roster names,
        # and has nothing different to do with the fact that a server exists but is
        # off. The operator's view of that is /api/mcp/servers.
        _tool_rule("get_me", "allow")
        cp.api_mcp.edit_mcp_server("mcp-github",
                                   _edit_request(enabled=False),
                                   _FakeRequest())
        self.assertEqual(cp.api_tool.tool_roster(), [])
        self.assertEqual(
            [s["server"] for s in cp.api_mcp.api_mcp_servers()], ["mcp-github"])

    def test_an_enabled_server_with_no_rules_is_still_named(self):
        # A fully denied surface rather than a broken one — and the gateway still has
        # to dial it, because enumerating its tools is what turns "never looked at"
        # into a decision an operator can make.
        self.assertEqual(cp.api_tool.tool_roster()[0]["tools"], [])

    def test_deny_rules_ship_too(self):
        # Presentation is the gateway's filter; enforcement is the decision
        # endpoint's. Serving the deny rows is what lets the gateway tell "reviewed
        # and refused" from "never configured" without asking again.
        _tool_rule("delete_file", "deny")
        self.assertEqual(cp.api_tool.tool_roster()[0]["tools"],
                         [{"tool": "delete_file", "action": "deny"}])

    def test_the_descriptor_travels_and_the_secret_does_not(self):
        # Enough to build a request, useless to steal. The material lives in a file
        # at a path DERIVED from the server name, so nothing here — and nothing in
        # the store behind it — can point one server at another's credential.
        cp.api_mcp.edit_mcp_server(
            "mcp-github",
            _edit_request(enabled=True, auth_type="header",
                          auth_header="Authorization",
                          auth_template="Bearer {secret}"),
            _FakeRequest())
        auth = cp.api_tool.tool_roster()[0]["auth"]
        self.assertEqual(auth, {"type": "header", "header": "Authorization",
                                "template": "Bearer {secret}"})
        self.assertNotIn("secret", json.dumps(cp.api_tool.tool_roster()).replace(
            "{secret}", ""))

    def test_the_endpoint_travels(self):
        cp.api_mcp.edit_mcp_server(
            "mcp-github", _edit_request(enabled=True, port=3000, path="/v1/mcp"),
            _FakeRequest())
        self.assertEqual(cp.api_tool.tool_roster()[0]["endpoint"],
                         {"port": 3000, "path": "/v1/mcp"})

    def test_no_server_is_an_empty_list_not_an_error(self):
        cp.api_mcp.revoke_mcp_server("mcp-github", _FakeRequest())
        self.assertEqual(cp.api_tool.tool_roster(), [])

    def test_the_roster_carries_no_constant_enabled_field(self):
        # Every server here is enabled by definition, and a field that never varies
        # invites a reader to believe it does.
        self.assertNotIn("enabled", cp.api_tool.tool_roster()[0])


class ToolClaimTests(_ToolBridgeTestCase):
    """``POST /tool/asks/{resume_token}/claim`` — resumption, and the only place this
    bridge releases anything.

    The gateway executes HERE rather than at the human's click, which is what makes
    the stranded-caller property structural: an approved call nobody comes back for
    simply never runs."""

    def _ask(self, args=None, client=CLASS_IP):
        _tool_rule("create_pull_request", "ask")
        return _ask_id(_tool_call(tool="create_pull_request",
                          args={"title": "x"} if args is None else args,
                          client=client))

    def test_an_approved_ask_hands_back_exactly_what_the_human_read(self):
        ask = self._ask(args={"b": 2, "a": 1})
        _resolve(ask, "allow")
        resp = _claim(ask)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.body["server"], "mcp-github")
        self.assertEqual(resp.body["tool"], "create_pull_request")
        # The canonical string rather than re-parsed JSON: it is what the digest
        # covers and what was shown, so the arguments that execute are necessarily
        # the approved ones.
        self.assertEqual(resp.body["args_json"], '{"a":1,"b":2}')

    def test_a_pending_ask_is_refused_as_a_delay_not_as_a_refusal(self):
        # An agent that cannot tell "come back later" from "no" retries a refusal
        # forever, or abandons a call a human is about to approve.
        resp = _claim(self._ask())
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.body["status"], "pending")
        self.assertFalse(resp.body["terminal"])

    def test_a_denied_or_expired_ask_is_terminal_and_says_so(self):
        denied = self._ask(args={"n": 1})
        _resolve(denied, "deny")
        resp = _claim(denied)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.body["status"], "denied")
        self.assertTrue(resp.body["terminal"])

        expiring = self._ask(args={"n": 2})
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_approvals SET deadline=? WHERE id=?",
                         (time.time() - 1, expiring))
            conn.commit()
        resp = _claim(expiring)
        self.assertEqual(resp.body["status"], "expired")
        self.assertTrue(resp.body["terminal"])

    def test_a_grant_is_single_use(self):
        # The gateway can be asked to resume twice — by a retrying agent, or by one
        # that reformulated and fell back to the id. Exactly one claim may run the
        # side effect, and the second answer has to be "spent", which is a different
        # fact from "denied".
        ask = self._ask()
        _resolve(ask, "allow")
        self.assertEqual(_claim(ask).status_code, 200)
        resp = _claim(ask)
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.body["spent"])
        self.assertTrue(resp.body["terminal"])
        # Spent is all this endpoint knows. The claim is written before the call, so
        # a claim whose answer was lost spent the grant with nothing run.
        self.assertNotIn("has run", resp.body["detail"])

    def test_another_sandboxs_id_is_unknown_rather_than_forbidden(self):
        # Both tiers share sandbox-net, so an id that leaked between them must not
        # confirm that it exists: a guessed id and a real one answer identically.
        ask = self._ask()
        _resolve(ask, "allow")
        resp = _claim(ask, client="172.30.0.137")
        self.assertEqual(resp.status_code, 404)
        self.assertIsNone(resp.body["status"])
        # ...and the approval is still there, unclaimed, for the client that raised it.
        self.assertEqual(_claim(ask).status_code, 200)

    def test_an_unknown_id_is_a_terminal_404(self):
        # Terminal like the refusals above, so the gateway reads one field for every
        # answer: an id unknown to this caller does not become known by asking again.
        resp = _claim_with("no-such-approval")
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(resp.body["terminal"])

    def test_the_claim_is_audited_as_the_moment_capability_is_released(self):
        # Two rows for one grant, deliberately: `resolve` records what a human
        # decided, this records that it is being acted on. The gap between them is
        # exactly the window in which an approved call was never run.
        ask = self._ask()
        _resolve(ask, "allow")
        with mock.patch.object(cp.store, "_audit") as audited:
            _claim(ask)
        self.assertEqual(audited.call_args[0][0], "allow")
        self.assertEqual(audited.call_args[1]["stage"], "tool-resume")
        self.assertIn(ask, audited.call_args[1]["reason"])

    def test_auditing_the_split_does_not_leak_it_to_the_caller(self):
        # Two rows, one answer: the branches were split to write different records,
        # and the response must not follow them apart.
        ask = self._ask()
        foreign = _claim(ask, client="172.30.0.137")
        unknown = _claim_with("f" * 32, client="172.30.0.137")
        self.assertEqual((foreign.status_code, foreign.body),
                         (unknown.status_code, unknown.body))

    def test_a_refused_claim_is_not_audited_as_a_release(self):
        # The ask is raised OUTSIDE the patch: registering it audits a hold of its
        # own, and counting that here would report the release this asserts is absent.
        ask = self._ask()
        with mock.patch.object(cp.store, "_audit") as audited:
            _claim(ask)
        self.assertEqual(audited.call_count, 0)

    def test_nothing_here_can_decide_an_ask(self):
        # The criterion for this whole bridge: a caller reaching it cannot GRANT.
        # A claim releases only what a human already approved, so a pending ask
        # stays pending no matter how often it is claimed.
        ask = self._ask()
        for _ in range(3):
            _claim(ask)
        self.assertEqual(cp.holds._get_tool_ask(ask)["status"], "pending")

    def test_disabling_the_server_stops_a_call_a_human_already_approved(self):
        # The switch has to reach THIS surface, not only the decision endpoint. It is
        # the one-click way to stop a server without touching its rules, and it does
        # not write to `tool_approvals` — so without a policy read here the operator's
        # stop button leaves every outstanding grant redeemable.
        ask = self._ask()
        _resolve(ask, "allow")
        cp.api_mcp.edit_mcp_server("mcp-github",
                                   _edit_request(enabled=False),
                                   _FakeRequest())
        resp = _claim(ask)
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.body["terminal"])
        self.assertFalse(resp.body["spent"])
        self.assertIn("disabled", resp.body["detail"])

    def test_revoking_or_denying_the_rule_stops_an_approved_call(self):
        # The same reasoning one level down: the approval was for a call the policy
        # then stopped permitting, and an id is not a grant that outlives its rule.
        # Two shapes of the same operator move. Nothing REPLACES a rule, so flipping
        # one to `deny` is a revoke followed by a write — both halves land here.
        def _flip_to_deny(rule_id):
            cp.api_mcp.revoke_mcp_rule(rule_id, _FakeRequest())
            _tool_rule("create_pull_request", "deny")

        for undo in (lambda rid: cp.api_mcp.revoke_mcp_rule(rid, _FakeRequest()),
                     _flip_to_deny):
            with self.subTest(undo=undo):
                rule_id = _tool_rule("create_pull_request", "ask").body["id"]
                ask = _ask_id(_tool_call(tool="create_pull_request",
                                 args={"title": "x"}))
                _resolve(ask, "allow")
                undo(rule_id)
                self.assertEqual(_claim(ask).status_code, 409)

    def test_a_policy_refusal_leaves_the_grant_to_be_redeemed_later(self):
        # Checked BEFORE the claim, so a refusal does not consume the approval: the
        # server can be switched back on, and the human's answer is still there.
        ask = self._ask()
        _resolve(ask, "allow")
        cp.api_mcp.edit_mcp_server("mcp-github",
                                   _edit_request(enabled=False),
                                   _FakeRequest())
        self.assertEqual(_claim(ask).status_code, 409)
        self.assertIsNone(cp.holds._get_tool_ask(ask)["claimed_at"])
        _enable()
        self.assertEqual(_claim(ask).status_code, 200)

    def test_a_call_stopped_by_policy_is_audited_as_a_deny_not_a_release(self):
        # The record has to distinguish "approved and run" from "approved and
        # refused at the door" — the second is the interesting one, because it is the
        # only evidence the operator's switch did anything.
        ask = self._ask()
        _resolve(ask, "allow")
        cp.api_mcp.edit_mcp_server("mcp-github",
                                   _edit_request(enabled=False),
                                   _FakeRequest())
        with mock.patch.object(cp.store, "_audit") as audited:
            _claim(ask)
        self.assertEqual(audited.call_args[0][0], "deny")
        self.assertEqual(audited.call_args[1]["stage"], "tool-resume")
        self.assertIn(ask, audited.call_args[1]["reason"])

    def test_every_release_row_records_which_population_called(self):
        # `client_class` is a searchable and grouped audit field, so an operator
        # filtering by class must not lose the row where capability was actually
        # released — a tool ask's own row cannot carry the class.
        ask = self._ask()
        _resolve(ask, "allow")
        with mock.patch.object(cp.store, "_audit") as audited:
            _claim(ask)
        self.assertEqual(audited.call_args[1]["client_class"],
                         cp.policy._client_class(CLASS_IP))

    def _lapse(self, ask):
        """Approve ``ask`` and let the grant window pass, as expiry reads it."""
        _resolve(ask, "allow")
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_approvals SET resolved_at=? WHERE id=?",
                         (time.time() - cp.holds.TOOL_GRANT_TIMEOUT - 1, ask))
            conn.commit()

    def _assert_no_provenance(self, body):
        # ``resolved_by`` is the operator's peer and UA; the flag is all of it the
        # agent gets.
        self.assertNotIn("resolved_by", body)
        self.assertNotIn("peer=", json.dumps(body))

    def test_a_lapsed_grant_is_told_apart_from_an_unanswered_ask(self):
        # Both rows read `expired`; only one had a human answer it, and the agent
        # must not be told nobody did.
        lapsed = self._ask(args={"n": 1})
        self._lapse(lapsed)
        resp = _claim(lapsed)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.body["status"], "expired")
        self.assertTrue(resp.body["terminal"])
        self.assertIs(resp.body["lapsed"], True)
        self._assert_no_provenance(resp.body)

        unanswered = self._ask(args={"n": 2})
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_approvals SET deadline=? WHERE id=?",
                         (time.time() - 1, unanswered))
            conn.commit()
        resp = _claim(unanswered)
        self.assertEqual(resp.body["status"], "expired")
        self.assertIs(resp.body["lapsed"], False)
        self._assert_no_provenance(resp.body)

    def test_only_an_expired_grant_reads_as_lapsed(self):
        # A human answered each of these too, so ``resolved_by`` alone is not the
        # predicate: an approved-then-withdrawn ask is not a lapse.
        denied = self._ask(args={"n": 1})
        _resolve(denied, "deny")
        withdrawn = self._ask(args={"n": 2})
        _withdraw(withdrawn)
        dropped = self._ask(args={"n": 3})
        _resolve(dropped, "allow")
        _withdraw(dropped)
        for ask, status in ((denied, "denied"), (withdrawn, "withdrawn"),
                            (dropped, "withdrawn")):
            with self.subTest(status=status):
                resp = _claim(ask)
                self.assertEqual(resp.body["status"], status)
                self.assertIs(resp.body["lapsed"], False)
                self._assert_no_provenance(resp.body)

    def test_a_lapsed_grant_refused_by_policy_still_reads_as_lapsed(self):
        # The policy check comes before the claim and answers for any status, so a
        # lapsed grant whose server was then disabled reaches the agent through it.
        ask = self._ask()
        self._lapse(ask)
        cp.api_mcp.edit_mcp_server("mcp-github",
                                   _edit_request(enabled=False),
                                   _FakeRequest())
        resp = _claim(ask)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("not releasable", resp.body["detail"])
        self.assertEqual(resp.body["status"], "expired")
        self.assertIs(resp.body["lapsed"], True)
        self._assert_no_provenance(resp.body)


class ToolWithdrawTests(_ToolBridgeTestCase):
    """``POST /tool/asks/{resume_token}/withdraw`` — the agent taking back its own ask.

    The property that makes it admissible on this bridge is that it only ever REDUCES:
    it retires a question or an unclaimed grant, and nothing it can be asked releases
    a call."""

    def _ask(self, args=None, client=CLASS_IP):
        _tool_rule("create_pull_request", "ask")
        return _ask_id(_tool_call(tool="create_pull_request",
                          args={"title": "x"} if args is None else args,
                          client=client))

    def test_a_pending_ask_leaves_the_operators_queue(self):
        ask = self._ask()
        resp = _withdraw(ask)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.body["status"], "withdrawn")
        self.assertEqual(cp.holds._get_tool_ask(ask)["status"], "withdrawn")
        self.assertNotIn(ask, [a["id"] for a in cp.holds._list_tool_asks()])

    def test_a_withdrawn_ask_can_no_longer_be_approved_or_claimed(self):
        ask = self._ask()
        _withdraw(ask)
        self.assertIsNone(cp.holds._resolve_tool_ask(ask, "allowed", "test"))
        resp = _claim(ask)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.body["status"], "withdrawn")
        self.assertTrue(resp.body["terminal"])

    def test_an_approved_ask_nobody_claimed_can_be_withdrawn(self):
        # Declining a grant is strictly safe, and otherwise it stays redeemable until
        # TOOL_GRANT_TIMEOUT.
        ask = self._ask()
        _resolve(ask, "allow")
        decided = cp.holds._get_tool_ask(ask)["resolved_at"]
        self.assertEqual(_withdraw(ask).status_code, 200)
        self.assertEqual(_claim(ask).status_code, 409)
        # The human's decision time stays: it records what was decided, and the audit
        # row records that it was dropped.
        self.assertEqual(cp.holds._get_tool_ask(ask)["resolved_at"], decided)

    def test_a_spent_approval_is_left_alone_and_says_so(self):
        # "Its call may have run" must not read as "dropped".
        ask = self._ask()
        _resolve(ask, "allow")
        _claim(ask)
        resp = _withdraw(ask)
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.body["spent"])
        self.assertTrue(resp.body["terminal"])
        self.assertEqual(cp.holds._get_tool_ask(ask)["status"], "allowed")

    def test_a_closed_ask_is_a_terminal_no_op_naming_its_status(self):
        denied = self._ask(args={"n": 1})
        _resolve(denied, "deny")
        twice = self._ask(args={"n": 2})
        _withdraw(twice)
        for ask, status in ((denied, "denied"), (twice, "withdrawn")):
            with self.subTest(status=status):
                resp = _withdraw(ask)
                self.assertEqual(resp.status_code, 409)
                self.assertEqual(resp.body["status"], status)
                self.assertTrue(resp.body["terminal"])
                self.assertFalse(resp.body["spent"])

    def test_a_stale_grant_reads_as_expired_not_withdrawn(self):
        # The grant window is in the UPDATE, as it is in the claim: a grant past it is
        # expired, and must not be rewritten as something the agent did.
        ask = self._ask()
        _resolve(ask, "allow")
        with mock.patch.object(cp.holds, "TOOL_GRANT_TIMEOUT", -1):
            resp = _withdraw(ask)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.body["status"], "expired")
        self.assertIs(resp.body["lapsed"], True)

    def test_another_sandboxs_id_is_unknown_and_its_ask_survives(self):
        ask = self._ask()
        foreign = _withdraw(ask, client="172.30.0.137")
        unknown = _withdraw_with("f" * 32, client="172.30.0.137")
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual((foreign.status_code, foreign.body),
                         (unknown.status_code, unknown.body))
        self.assertEqual(cp.holds._get_tool_ask(ask)["status"], "pending")

    def test_withdrawing_frees_the_per_client_slot_after_the_cooldown(self):
        # Not at once: an ask-withdraw loop would otherwise raise cards without limit
        # (``holds.TOOL_WITHDRAW_COOLDOWN``).
        with mock.patch.object(cp.holds, "MAX_TOOL_PENDING_PER_CLIENT", 1), \
                mock.patch.object(cp.holds, "TOOL_WITHDRAW_COOLDOWN", 60):
            first = self._ask(args={"n": 1})
            _withdraw(first)
            refused = _tool_call(tool="create_pull_request", args={"n": 2})
            self.assertEqual(refused["decision"], "deny")
            self.assertIn("withdrawn in the last 60s", refused["reason"])
            with cp.store._connect() as conn:
                conn.execute("UPDATE tool_approvals SET resolved_at=? WHERE id=?",
                             (time.time() - 61, first))
                conn.commit()
            self.assertEqual(
                _tool_call(tool="create_pull_request", args={"n": 2})["decision"],
                "ask")

    def test_withdrawing_says_whether_a_human_had_approved(self):
        # The audit row prints the status it withdrew from; the agent is told the
        # same, so a dropped approval is not described as a question still asked.
        approved = self._ask(args={"n": 1})
        _resolve(approved, "allow")
        pending = self._ask(args={"n": 2})
        for ask, was in ((approved, "allowed"), (pending, "pending")):
            with self.subTest(was=was):
                resp = _withdraw(ask)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.body["was"], was)


class WithdrawnPayloadTests(_ToolBridgeTestCase):
    """The pending payload's ``withdrawn`` list: what lets a tool card that left the
    queue say the agent withdrew it, rather than hedge (``holds._list_withdrawn``)."""

    def _ask(self, n, client=CLASS_IP):
        _tool_rule("create_pull_request", "ask")
        return _ask_id(_tool_call(tool="create_pull_request", args={"n": n},
                                  client=client))

    def _withdrawn_at(self, approval_id, at):
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_approvals SET resolved_at=? WHERE id=?",
                         (at, approval_id))
            conn.commit()

    def test_an_ask_withdrawn_while_pending_is_named_and_off_the_queue(self):
        ask = self._ask(1)
        _withdraw(ask)
        payload = cp.holds._pending_payload()
        self.assertEqual(payload["withdrawn"], [ask])
        self.assertNotIn(ask, [c["id"] for c in payload["holds"]])

    def test_only_a_withdrawal_while_pending_is_named(self):
        # Approved then withdrawn: the card left on the operator's own resolve, so
        # there is nothing to explain. Denied and expired asks were never withdrawn.
        approved = self._ask(1)
        _resolve(approved, "allow")
        _withdraw(approved)
        denied = self._ask(2)
        _resolve(denied, "deny")
        expired = self._ask(3)
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_approvals SET deadline=? WHERE id=?",
                         (time.time() - 1, expired))
            conn.commit()
        self.assertEqual(cp.holds._get_tool_ask(approved)["status"], "withdrawn")
        self.assertEqual(cp.holds._pending_payload()["withdrawn"], [])
        self.assertEqual(cp.holds._get_tool_ask(expired)["status"], "expired")

    def test_capped_at_the_tool_card_cap_newest_kept(self):
        with mock.patch.object(cp.holds, "TOOL_WITHDRAW_COOLDOWN", 0):
            asks = [self._ask(n) for n in range(3)]
        for at, ask in enumerate(asks):
            _withdraw(ask)
            self._withdrawn_at(ask, 1000.0 + at)
        with mock.patch.object(cp.holds, "MAX_TOOL_PENDING", 2):
            self.assertEqual(cp.holds._list_withdrawn(), [asks[2], asks[1]])
        # A negative SQLite LIMIT means no limit, so an unclamped cap would send every
        # row the table has ever held.
        with mock.patch.object(cp.holds, "MAX_TOOL_PENDING", -1):
            self.assertEqual(cp.holds._list_withdrawn(), [])

    def test_the_list_is_stable_while_nothing_happens(self):
        # The stream emits on serialized change, so a field that moved on its own would
        # make every idle page a 1 Hz emitter.
        _withdraw(self._ask(1))
        self._ask(2)
        first = json.dumps(cp.holds._pending_payload(), sort_keys=True)
        self.assertEqual(json.dumps(cp.holds._pending_payload(), sort_keys=True), first)

    def test_the_read_rides_an_index_with_no_sort(self):
        # The table is never pruned, so without the index each listener would scan and
        # sort every withdrawn row ever written, once a second. The plan is of the
        # statement `_list_withdrawn` actually runs, as SQLite expanded it.
        statements = []
        connect = cp.store._connect

        def traced():
            conn = connect()
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(cp.store, "_connect", traced):
            cp.holds._list_withdrawn()
        [query] = [q for q in statements if q.lstrip().upper().startswith("SELECT")]
        with cp.store._connect() as conn:
            plan = " | ".join(r["detail"] for r in conn.execute(
                "EXPLAIN QUERY PLAN " + query))
        self.assertIn("tool_approvals_status_resolved", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_the_withdrawn_list_is_read_after_the_queue(self):
        # Read before, a withdrawal landing between the two reads would drop the card
        # from the queue with its id missing from the list, and it would keep the
        # vaguer wording for good. Read after, the worst case is an id in both.
        calls = []
        with mock.patch.object(cp.holds, "_list_tool_asks",
                               lambda: calls.append("queue") or []), \
                mock.patch.object(cp.holds, "_list_withdrawn",
                                  lambda: calls.append("withdrawn") or []):
            cp.holds._pending_payload()
        self.assertEqual(calls, ["queue", "withdrawn"])


class WithdrawAuditTests(_ToolBridgeTestCase):
    """What a withdrawal leaves in the trail, asserted through the table."""

    audits = True

    def _ask(self):
        _tool_rule("create_pull_request", "ask")
        return _ask_id(_tool_call(tool="create_pull_request", args={"title": "x"}))

    def _rows(self, approval_id):
        with cp.store._connect() as conn:
            return conn.execute(
                "SELECT kind, stage, client, server, tool, reason FROM audit "
                "WHERE stage='tool-withdraw' AND approval_id=?",
                (approval_id,)).fetchall()

    def test_a_withdrawal_is_one_row_against_the_ask(self):
        ask = self._ask()
        _withdraw(ask)
        [(kind, stage, client, server, tool, reason)] = self._rows(ask)
        self.assertEqual((kind, stage, client), ("withdraw", "tool-withdraw", CLASS_IP))
        self.assertEqual((server, tool), ("mcp-github", "create_pull_request"))
        self.assertIn("pending", reason)

    def test_a_foreign_withdrawal_is_audited_as_a_leaked_id(self):
        ask = self._ask()
        _withdraw(ask, client="172.30.0.137")
        [(kind, _stage, client, _server, _tool, reason)] = self._rows(ask)
        self.assertEqual((kind, client), ("deny", "172.30.0.137"))
        self.assertIn(CLASS_IP, reason)

    def test_a_no_op_withdrawal_writes_nothing(self):
        # Repeating a withdrawal is harmless, and should not look like a second one.
        ask = self._ask()
        _withdraw(ask)
        _withdraw(ask)
        self.assertEqual(len(self._rows(ask)), 1)


class RefusedClaimAuditTests(_ToolBridgeTestCase):
    """A claim refused as UNKNOWN still leaves a row. The caller gets one 404 for an
    id that does not exist and for one that belongs to another client, so the
    difference is only ever visible here — and the second is how a leaked approval id
    shows up at all. Asserted through the table for the reason ``audits`` exists."""

    audits = True

    def _ask(self):
        _tool_rule("create_pull_request", "ask")
        return _ask_id(_tool_call(tool="create_pull_request", args={"title": "x"}))

    def _resume_rows(self, approval_id):
        with cp.store._connect() as conn:
            return conn.execute(
                "SELECT kind, stage, client, server, tool, reason FROM audit "
                "WHERE stage='tool-resume' AND approval_id=?",
                (approval_id,)).fetchall()

    def test_a_claim_from_another_client_is_audited_against_the_ask(self):
        # The leaked-id signal. The caller is told "unknown"; the operator is told
        # whose id it was, in a row that joins the ask's own history by id.
        ask = self._ask()
        _resolve(ask, "allow")
        _claim(ask, client="172.30.0.137")
        [(kind, stage, client, server, tool, reason)] = self._resume_rows(ask)
        self.assertEqual((kind, stage, client), ("deny", "tool-resume", "172.30.0.137"))
        self.assertEqual((server, tool), ("mcp-github", "create_pull_request"))
        self.assertIn(CLASS_IP, reason)                     # whose id it was

    def test_an_unknown_value_is_audited_with_no_id_and_the_value_in_the_reason(self):
        # It names no ask, so the row joins none: a value written to `approval_id`
        # would read as one in the column the trail is queried by.
        _claim_with("f" * 32, client="172.30.0.137")
        with cp.store._connect() as conn:
            [row] = conn.execute(
                "SELECT kind, client, server, tool, approval_id, reason FROM audit "
                "WHERE stage='tool-resume'").fetchall()
        self.assertEqual((row["kind"], row["client"]), ("deny", "172.30.0.137"))
        self.assertEqual((row["server"], row["tool"], row["approval_id"]),
                         (None, None, None))
        self.assertIn("unknown", row["reason"])
        self.assertIn("f" * 32, row["reason"])

    def test_polling_a_pending_ask_writes_nothing(self):
        # What keeps the new rows from becoming a flood: an agent waiting on a human
        # re-claims its own pending id, and that answer is a delay, not a refusal.
        ask = self._ask()
        for _ in range(3):
            _claim(ask)
        self.assertEqual(self._resume_rows(ask), [])


class ResumeTokenSeparationTests(_ToolBridgeTestCase):
    """Two handles on one ask: the agent's resume token, which claim and withdraw take,
    and the operator's id, which ``resolve`` takes and which nothing the agent side
    receives may carry. Asserted on every answer and every audit row an ask's life
    writes, since one wide select or one f-string is all a leak takes."""

    audits = True

    def setUp(self):
        super().setUp()
        _tool_rule("create_pull_request", "ask")

    @staticmethod
    def _raise(n, client=CLASS_IP):
        return _tool_call(tool="create_pull_request", args={"n": n}, client=client)

    @staticmethod
    def _text(resp):
        return json.dumps(resp.body if hasattr(resp, "body") else resp)

    def test_no_answer_the_agent_side_gets_carries_the_id(self):
        fresh = self._raise(1)
        joined = self._raise(1)
        ask = _ask_id(fresh)
        self.assertTrue(joined["joined"])
        self.assertEqual(joined["resume_token"], fresh["resume_token"])
        answers = {"ask": fresh, "joined ask": joined,
                   "pending claim": _claim(ask),
                   "foreign claim": _claim(ask, client="172.30.0.137"),
                   "unknown claim": _claim_with("f" * 32),
                   "id claim": _claim_with(ask)}
        _resolve(ask, "allow")
        answers["claim"] = _claim(ask)
        answers["spent claim"] = _claim(ask)
        answers["spent withdraw"] = _withdraw(ask)
        other = _ask_id(self._raise(2))
        answers["withdraw"] = _withdraw(other)
        answers["closed withdraw"] = _withdraw(other)
        refused = _ask_id(self._raise(3))
        _resolve(refused, "allow")
        cp.api_mcp.edit_mcp_server("mcp-github", _edit_request(enabled=False),
                                   _FakeRequest())
        answers["policy-refused claim"] = _claim(refused)
        self.assertEqual(answers["claim"].status_code, 200)
        for name, answer in answers.items():
            with self.subTest(answer=name):
                for approval_id in (ask, other, refused):
                    self.assertNotIn(approval_id, self._text(answer))

    def test_an_operator_id_is_unknown_to_claim_and_withdraw_and_says_so(self):
        ask = _ask_id(self._raise(1))
        _resolve(ask, "allow")
        for presented in (_claim_with(ask), _withdraw_with(ask)):
            self.assertEqual(presented.status_code, 404)
            self.assertTrue(presented.body["terminal"])
            self.assertEqual(presented.body, _claim_with("f" * 32).body)
        with cp.store._connect() as conn:
            rows = conn.execute(
                "SELECT kind, stage, server, tool, reason FROM audit "
                "WHERE approval_id=? AND stage IN ('tool-resume', 'tool-withdraw') "
                "ORDER BY id", (ask,)).fetchall()
        self.assertEqual([(r["kind"], r["stage"], r["server"], r["tool"]) for r in rows],
                         [("deny", "tool-resume", "mcp-github", "create_pull_request"),
                          ("deny", "tool-withdraw", "mcp-github",
                           "create_pull_request")])
        for row in rows:
            self.assertIn("operator's approval id", row["reason"])
        # Nothing was claimed or withdrawn by the id: the token still redeems it.
        self.assertEqual(_claim(ask).status_code, 200)

    def test_resolve_given_a_token_decides_nothing(self):
        answer = self._raise(1)
        resp = _resolve(answer["resume_token"], "allow")
        self.assertNotEqual(resp.status_code, 200)
        self.assertEqual(cp.holds._get_tool_ask(_ask_id(answer))["status"], "pending")
        self.assertEqual(_claim(_ask_id(answer)).body["status"], "pending")

    def test_the_token_reaches_no_operator_surface(self):
        # Every row an ask's life writes, through each branch: the hold, a join, a
        # pending poll, a foreign and an id-presented claim, the click, a
        # policy-refused claim, the release, a withdrawal, and an expiry.
        tokens = []
        first = self._raise(1)
        tokens.append(first["resume_token"])
        ask = _ask_id(first)
        self._raise(1)
        _claim(ask)
        _claim(ask, client="172.30.0.137")
        _withdraw(ask, client="172.30.0.137")
        _claim_with(ask)
        payload = json.dumps(cp.holds._pending_payload())
        resolved = _resolve(ask, "allow")
        cp.api_mcp.edit_mcp_server("mcp-github", _edit_request(enabled=False),
                                   _FakeRequest())
        _claim(ask)
        _enable()
        _claim(ask)
        dropped = self._raise(2)
        tokens.append(dropped["resume_token"])
        _withdraw(_ask_id(dropped))
        lapsing = self._raise(3)
        tokens.append(lapsing["resume_token"])
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_approvals SET deadline=? WHERE id=?",
                         (time.time() - 1, _ask_id(lapsing)))
            conn.commit()
        cp.holds._expire_tool_asks()
        with cp.store._connect() as conn:
            audit = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit")])
            kinds = {(r["kind"], r["stage"]) for r in conn.execute(
                "SELECT kind, stage FROM audit")}
        # The branches above did write their rows, so their absence below is evidence.
        self.assertLessEqual({("hold", "tool-call"), ("deny", "tool-resume"),
                              ("deny", "tool-withdraw"), ("allow", "tool-resume"),
                              ("withdraw", "tool-withdraw"), ("deny", "tool-ask")},
                             kinds)
        for token in tokens:
            with self.subTest(token=token):
                self.assertNotIn(token, payload)
                self.assertNotIn(token, self._text(resolved))
                self.assertNotIn(token, audit)


class ToolCorrelationColumnTests(_ToolBridgeTestCase):
    """``audit.server`` / ``audit.tool`` / ``audit.approval_id`` — the trail as
    something to QUERY rather than to grep.

    Every column the audit table had was egress vocabulary, so a tool decision borrowed
    it by writing "create_pull_request on mcp-github: ..." into ``reason``. That reads
    well and joins to nothing. One tool ask writes rows at three separate moments here —
    the hold, the human's click, the claim — and with the id only inside prose, the
    single question an operator has after an approval ("what came of it?") is a string
    search whose recall depends on wording that has been reworded before.

    So the property under test is ONE APPROVAL, ONE KEY: every row that belongs to an
    ask carries its id in a column. Asserted through the endpoints rather than on
    ``_audit`` calls, because the value has to survive the write — a kwarg that reaches
    ``_audit`` and not the table would pass a mock-based test and leave the column
    empty."""

    audits = True

    def _rows_for(self, approval_id):
        with cp.store._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT kind, stage, server, tool, approval_id FROM audit "
                "WHERE approval_id=? ORDER BY id", (approval_id,))]

    def test_one_ask_is_three_rows_reachable_by_its_id_alone(self):
        # The end-to-end property, and the one the UI's outcome-on-the-card needs: the
        # hold, the human's answer and the release are selectable together WITHOUT
        # parsing a sentence. The stages are asserted as a set because what matters is
        # that none of the three is missing, not the order they were written in.
        _tool_rule("create_pull_request", "ask")
        approval_id = _ask_id(_tool_call(tool="create_pull_request",
                                 args={"title": "x"}))
        _resolve(approval_id, "allow")
        _claim(approval_id)
        rows = self._rows_for(approval_id)
        self.assertEqual({r["stage"] for r in rows},
                         {"tool-call", "tool-ask", "tool-resume"})
        for row in rows:
            with self.subTest(stage=row["stage"]):
                self.assertEqual(row["server"], "mcp-github")
                self.assertEqual(row["tool"], "create_pull_request")

    def test_every_row_of_one_ask_carries_the_same_client_class(self):
        # Filtering the audit by class must not drop the human's decision: the hold
        # and the answer both belong to the one agent that asked.
        _tool_rule("create_pull_request", "ask")
        approval_id = _ask_id(_tool_call(tool="create_pull_request",
                                 args={"title": "x"}))
        _resolve(approval_id, "deny")
        with cp.store._connect() as conn:
            rows = conn.execute("SELECT stage, client_class FROM audit "
                                "WHERE approval_id=?", (approval_id,)).fetchall()
        self.assertEqual({r["stage"] for r in rows}, {"tool-call", "tool-ask"})
        classes = {r["client_class"] for r in rows}
        self.assertEqual(len(classes), 1, classes)
        self.assertIsNotNone(classes.pop())

    def test_a_decided_call_names_its_tool_without_an_approval(self):
        # `allow` and `deny` answer immediately, so there is no id to carry — and the
        # server/tool pair still has to be a column, because "everything that touched
        # this server" must not silently omit the calls that never became asks. Those
        # are the rows that grow with every tool an operator allows.
        _tool_rule("get_me", "allow")
        _tool_call(tool="get_me")
        _tool_call(tool="merge_pull_request")            # unconfigured, so denied
        with cp.store._connect() as conn:
            rows = {r["tool"]: (r["kind"], r["server"], r["approval_id"])
                    for r in conn.execute(
                        "SELECT kind, server, tool, approval_id FROM audit "
                        "WHERE stage='tool-call'")}
        self.assertEqual(rows, {"get_me": ("allow", "mcp-github", None),
                                "merge_pull_request": ("deny", "mcp-github", None)})

    def test_a_call_naming_no_server_records_null_rather_than_a_blank(self):
        # The bridge accepts an empty server/tool and denies them (the `(no tool)`
        # wording in the reason). An empty STRING in a column would be a third state
        # beside "absent" and "present" that every reader would have to know about, and
        # the one thing a record must not do is assert something nothing observed.
        _tool_call(server="", tool="")
        with cp.store._connect() as conn:
            row = conn.execute("SELECT server, tool FROM audit "
                               "WHERE stage='tool-call'").fetchone()
        self.assertEqual((row["server"], row["tool"]), (None, None))

    def test_a_surface_change_names_the_server_it_concerns(self):
        # The `observe` row is NOT a decision, and it still belongs to a server — so a
        # filter for one server must not drop exactly the supply-chain rows. The name
        # travels beside the line from `inventory.changes` rather than being read back
        # out of it, which is the whole point of the column.
        cp.api_tool.tool_inventory(cp.api_tool.InventoryRequest(servers={
            "mcp-github": {"status": "ok", "tools": [{"name": "get_me"}]}}),
            _FakeRequest())
        cp.api_tool.tool_inventory(cp.api_tool.InventoryRequest(servers={
            "mcp-github": {"status": "ok", "tools": [{"name": "get_me"},
                                                     {"name": "delete_repository"}]}}),
            _FakeRequest())
        with cp.store._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT server, reason FROM audit WHERE stage='mcp-tools' "
                "ORDER BY id")]
        self.assertEqual([r["server"] for r in rows], ["mcp-github", "mcp-github"])
        self.assertIn("delete_repository", rows[-1]["reason"])

    def test_writing_a_rule_is_filed_under_the_tool_it_governs(self):
        # Policy rows and the calls they decide, selectable together. Without this, "why
        # did this tool start being allowed" and "what did it do" are two searches over
        # two wordings, and only one of them is in the same vocabulary as the decision.
        rule_id = _tool_rule("get_me", "allow").body["id"]
        cp.api_mcp.revoke_mcp_rule(rule_id, _FakeRequest())
        with cp.store._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT kind, server, tool FROM audit WHERE stage='tool-policy' "
                "ORDER BY id")]
        self.assertEqual(rows, [{"kind": "create", "server": "mcp-github",
                                 "tool": "get_me"},
                                {"kind": "revoke", "server": "mcp-github",
                                 "tool": "get_me"}])

    def test_an_egress_decision_leaves_the_tool_columns_empty(self):
        # The columns are additive, not a re-interpretation of the table. An egress row
        # whose identity is host/port/url must not acquire a tool identity it never had
        # — that would make `WHERE server IS NOT NULL` stop meaning "tool rows".
        _set_rules([("example.com", "allow")])
        cp.api_authorize.authorize(_auth_req("example.com"))
        with cp.store._connect() as conn:
            row = conn.execute("SELECT server, tool, approval_id FROM audit "
                               "WHERE host='example.com'").fetchone()
        self.assertEqual((row["server"], row["tool"], row["approval_id"]),
                         (None, None, None))


class ActorColumnTests(_ToolBridgeTestCase):
    """``audit.actor`` beside ``audit.client``: who performed the act, and which
    sandbox's request the row concerns.

    Before the column the operator was in ``reason`` prose on six kinds of row and the
    gateway was in ``client`` on one, so "what did this operator do" was a text search
    and a search for a sandbox could match an inventory push."""

    audits = True
    OPERATOR = "172.31.0.9"

    def _operator(self):
        return _FakeRequest(peer=self.OPERATOR)

    def _rows(self, where, *params):
        with cp.store._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT kind, stage, host, client, client_class, actor, reason "  # noqa: S608
                "FROM audit "
                f"WHERE {where} ORDER BY id", params)]

    def test_every_configuration_change_names_its_actor_and_no_client(self):
        # No sandbox asked for any of these, so `client` is empty and so is the class
        # derived from it. An egress rule's class is in its reason instead.
        op = self._operator()
        rule = _create(".example.com", "allow", request=op).body["id"]
        _edit_rule(rule, "example.com", "allow", op)
        _revoke_rule(rule, op)
        cp.api_egress.revoke_lease(
            LeaseRevokeTests._insert(self, "api.example.com", 900), op)
        tool = _tool_rule("get_me", "allow", request=op).body["id"]
        _edit_tool_rule(tool, "ask", op)
        cp.api_mcp.revoke_mcp_rule(tool, op)
        _register("mcp-other", request=op)
        cp.api_mcp.edit_mcp_server("mcp-other",
                                   _edit_request(enabled=True), op)
        cp.api_mcp.revoke_mcp_server("mcp-other", op)

        rows = self._rows("stage IN ('policy', 'tool-policy', 'mcp-server')")
        mine = [r for r in rows if f"peer={self.OPERATOR}" in (r["actor"] or "")]
        self.assertEqual(len(mine), 10)
        for row in rows:                       # setUp's register and enable included
            with self.subTest(stage=row["stage"], kind=row["kind"]):
                self.assertIsNotNone(row["actor"])
                self.assertEqual((row["client"], row["client_class"]), (None, None))
                # Said once, in its column; the reason says what happened.
                self.assertNotIn("peer=", row["reason"])

    def test_a_human_answer_to_a_tool_ask_keeps_the_asker_and_names_the_answerer(self):
        _tool_rule("create_pull_request", "ask")
        ask = _ask_id(_tool_call(tool="create_pull_request", args={"title": "x"}))
        _resolve(ask, "deny", self._operator())
        [row] = self._rows("stage='tool-ask' AND approval_id=?", ask)
        self.assertEqual((row["client"], row["client_class"]),
                         (CLASS_IP, cp.policy._client_class(CLASS_IP)))
        self.assertIn(f"peer={self.OPERATOR}", row["actor"])
        self.assertNotIn("peer=", row["reason"])

    def test_an_egress_request_a_human_decided_names_the_human(self):
        saved = cp.holds.HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT = 5
        try:
            t, _result, approval_id = HoldHandshakeTests._authorize_in_thread(
                self, "decided.example.com")
            _resolve(approval_id, "deny_once", self._operator())
            t.join(2)
        finally:
            cp.holds.HOLD_TIMEOUT = saved
        hold, decided = self._rows("host='decided.example.com'")
        self.assertEqual((hold["kind"], hold["actor"]), ("hold", None))
        self.assertEqual((decided["kind"], decided["client"]), ("deny", CLASS_IP))
        self.assertIn(f"peer={self.OPERATOR}", decided["actor"])
        self.assertNotIn("peer=", decided["reason"])

    def test_rows_nobody_acted_on_carry_no_actor(self):
        # Policy answered, or a window ran out. An actor here would claim a human was
        # involved in a decision no human saw.
        _set_rules([("example.com", "allow")])
        cp.api_authorize.authorize(_auth_req("example.com", stage="connect"))
        _tool_rule("get_me", "allow")
        _tool_call(tool="get_me")
        _tool_rule("create_pull_request", "ask")
        saved = cp.holds.HOLD_TIMEOUT, cp.holds.TOOL_HOLD_TIMEOUT
        cp.holds.HOLD_TIMEOUT, cp.holds.TOOL_HOLD_TIMEOUT = 0.05, -1
        try:
            cp.api_authorize.authorize(_auth_req("nobody.example.com", stage="connect"))
            _tool_call(tool="create_pull_request", args={"title": "x"})
            cp.holds._expire_tool_asks()
        finally:
            cp.holds.HOLD_TIMEOUT, cp.holds.TOOL_HOLD_TIMEOUT = saved
        rows = self._rows("stage NOT IN ('policy', 'tool-policy', 'mcp-server')")
        self.assertEqual({(r["stage"], r["kind"]) for r in rows},
                         {("connect", "allow"), ("connect", "hold"),
                          ("connect", "deny"), ("tool-call", "allow"),
                          ("tool-call", "hold"), ("tool-ask", "deny")})
        for row in rows:
            with self.subTest(stage=row["stage"], kind=row["kind"]):
                self.assertIsNone(row["actor"])

    def test_no_handler_builds_a_reason_with_the_actor_in_it(self):
        # The tests above exercise the writers that exist; this covers the next one.
        # Every actor a handler holds is a local named for what it is, so one
        # interpolated into an f-string is an actor on its way into a reason.
        for name in ("{actor}", "{resolved_by}", "{granted_by}"):
            with self.subTest(name=name):
                self.assertNotIn(name, _HANDLER_SOURCE)

    def test_an_inventory_push_names_the_gateway_as_actor_not_as_client(self):
        # The one row that used to put an actor in `client`: a formatted provenance
        # string where every other row has a sandbox address.
        cp.api_tool.tool_inventory(cp.api_tool.InventoryRequest(servers={
            "mcp-github": {"status": "ok", "tools": [{"name": "get_me"}]}}),
            _FakeRequest(peer="172.29.0.4"))
        [row] = self._rows("stage='mcp-tools'")
        self.assertEqual((row["client"], row["client_class"]), (None, None))
        self.assertIn("peer=172.29.0.4", row["actor"])


class ToolArgumentsChangedAuditTests(_ToolBridgeTestCase):
    """A tool's arguments moving is an `observe` row on `mcp-tools`, as a new tool is:
    a plain `allow` on a dispatcher widens when its enum grows, and the rule is not
    bound to the schema, so this row is what records it."""

    audits = True

    def _push(self, digest: str) -> None:
        cp.api_tool.tool_inventory(cp.api_tool.InventoryRequest(servers={
            "mcp-github": {"status": "ok", "tools": [
                {"name": "pull_request_read", "digest": digest}]}}), _FakeRequest())

    def test_a_moved_digest_is_an_observe_row_naming_server_and_tool(self):
        _tool_rule("pull_request_read", "allow")
        self._push("a" * 64)
        self._push("a" * 64)
        self._push("b" * 64)
        with cp.store._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT kind, server, reason FROM audit WHERE stage='mcp-tools' "
                "ORDER BY id")]
        self.assertEqual([(r["kind"], r["server"]) for r in rows],
                         [("observe", "mcp-github")] * 2)
        self.assertEqual(rows[-1]["reason"],
                         "mcp-github changed the arguments of pull_request_read")
        self.assertNotIn("b" * 64, rows[-1]["reason"])


class TwoServersOneToolNameTests(_ToolBridgeTestCase):
    """The reason ``server`` is a COLUMN and ``tool_rules`` is UNIQUE on the pair.

    Tool names are not namespaced across servers: two servers can each expose an
    `issue_read`, and nothing in the name says which. If the audit trail could not tell
    them apart, every query about one server would quietly merge in the other's history
    — and policy would be worse than quiet, since one server's rule would decide the
    other's identically named tool.

    The multi-server path has never run against real containers (only one is
    registered in `mcp-servers.yml`), which is exactly why it is asserted here."""

    audits = True

    def setUp(self):
        super().setUp()
        _register("mcp-other")
        _enable("mcp-other")

    def test_a_rule_on_one_server_does_not_decide_the_others_tool(self):
        # The boundary, not the bookkeeping. `issue_read` is allowed on one server and
        # unconfigured on the other, and unconfigured is DENIED — so a leak here is a
        # call running against a server no one ruled.
        _tool_rule("issue_read", "allow", server="mcp-github")
        self.assertEqual(_tool_call(server="mcp-github", tool="issue_read")["decision"],
                         "allow")
        self.assertEqual(_tool_call(server="mcp-other", tool="issue_read")["decision"],
                         "deny")

    def test_both_servers_can_hold_a_rule_for_the_same_tool_name(self):
        # UNIQUE(server, tool), not UNIQUE(tool). A constraint on the name alone would
        # make the second write fail or silently no-op, leaving one server unruled
        # while the operator believes they configured it.
        _tool_rule("issue_read", "allow", server="mcp-github")
        _tool_rule("issue_read", "deny", server="mcp-other")
        self.assertEqual(_tool_call(server="mcp-github", tool="issue_read")["decision"],
                         "allow")
        self.assertEqual(_tool_call(server="mcp-other", tool="issue_read")["decision"],
                         "deny")

    def test_the_audit_rows_are_told_apart_by_the_column(self):
        # And not by parsing the reason prose, which names both but is a sentence.
        _tool_rule("issue_read", "allow", server="mcp-github")
        _tool_rule("issue_read", "deny", server="mcp-other")
        _tool_call(server="mcp-github", tool="issue_read")
        _tool_call(server="mcp-other", tool="issue_read")
        with cp.store._connect() as conn:
            rows = {r["server"]: r["kind"] for r in conn.execute(
                "SELECT server, kind FROM audit WHERE tool='issue_read'")}
        self.assertEqual(rows, {"mcp-github": "allow", "mcp-other": "deny"})


class LiveFeedTests(_CPTestCase):
    """``store._audit``'s stdout mirror, which is `make logs-cp`. Its fields come from
    the far side of every boundary the store has — a host the agent named, a tool
    name, a third party's error — so one row has to stay one line."""

    audits = True

    def test_a_field_cannot_add_a_line_to_the_feed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cp.store._audit("deny", stage="http", host="a.example\nAUDIT allow b",
                            reason="refused\r\x1bAUDIT allow c")
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("a.example\\nAUDIT allow b", lines[0])
        self.assertNotIn("\x1b", lines[0])

    def test_a_lone_surrogate_is_printed_escaped(self):
        # A strict UTF-8 stdout, the stricter harness: a StringIO prints a surrogate
        # whole, while the container's python opens stdout with surrogateescape, which
        # writes U+DC80 to U+DCFF as raw bytes and raises on every other surrogate. A
        # high and a low one, so neither half of the range can go unescaped.
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
        with contextlib.redirect_stdout(out):
            cp.store._audit("deny", stage="http", host="a" + _SURROGATE + ".example",
                            reason="r" + chr(0xDFFF))
        out.flush()
        self.assertEqual(raw.getvalue().decode(),
                         f"AUDIT deny stage=http host=a{_ESCAPED}.example "
                         f":: r{chr(92)}udfff\n")


class LoneSurrogateTests(_ToolBridgeTestCase):
    """A lone surrogate in a request that passes validation, on every enforcer route.
    It has no UTF-8 form, so SQLite cannot bind it: each route answers rather than
    500s, and the attempt is audited with the surrogate escaped."""

    audits = True

    def _audit_rows(self, *columns):
        # The column names are this class's own literals.
        with cp.store._connect() as conn:
            return [tuple(r) for r in conn.execute(
                f"SELECT {', '.join(('kind', *columns, 'reason'))} "  # noqa: S608
                "FROM audit ORDER BY id")]

    @staticmethod
    def _forget_audit():
        """Drop the rows setUp wrote, so a test reads only its own."""
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM audit")
            conn.commit()

    def _count(self, table):
        with cp.store._connect() as conn:
            return conn.execute(
                f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608

    def test_an_escaped_field_is_capped_after_the_escape(self):
        # Capped first, a field of surrogates would be stored at six times the cap.
        n = cp.store.DRAIN_MAX_FIELD
        self._forget_audit()
        cp.store._audit("deny", stage="http", host=_SURROGATE * n)
        with cp.store._connect() as conn:
            (host,), = conn.execute("SELECT host FROM audit").fetchall()
        self.assertEqual(host, (_ESCAPED * n)[:n])

    def test_authorize_refuses_each_field_and_audits_it(self):
        # The host is unmatched, so every field but `host` reaches the hold path if
        # the guard does not stop it first: the empty registry is what shows the
        # guard sits ahead of the slot.
        fields = {"host": "unmatched.example", "proto": "https", "client": CLASS_IP,
                  "method": "GET", "url": "https://unmatched.example/", "stage": "http"}
        for name in fields:
            with self.subTest(field=name):
                self._forget_audit()
                sent = {**fields, name: fields[name] + _SURROGATE}
                resp = cp.api_authorize.authorize(cp.api_authorize.AuthorizeRequest(
                    port=443, **sent))
                self.assertEqual((resp.decision, resp.reason),
                                 ("deny", cp.store.UNENCODABLE))
                self.assertEqual(self._audit_rows(*fields), [
                    ("deny", *[v.replace(_SURROGATE, _ESCAPED)
                               for v in sent.values()], cp.store.UNENCODABLE)])
                self.assertEqual(self._count("approvals"), 0)
                self.assertEqual((cp.holds._GROUPS, cp.holds._PENDING_EVENTS,
                                  cp.holds._PENDING_WAITERS), ({}, {}, {}))

    def test_tool_authorize_refuses_each_name_and_audits_it(self):
        _tool_rule("get_me", "allow")
        fields = {"server": "mcp-github", "tool": "get_me", "client": CLASS_IP}
        for name in fields:
            with self.subTest(field=name):
                self._forget_audit()
                sent = {**fields, name: fields[name] + _SURROGATE}
                self.assertEqual(_tool_call(**sent),
                                 {"decision": "deny", "reason": cp.store.UNENCODABLE})
                shown = {k: v.replace(_SURROGATE, _ESCAPED) for k, v in sent.items()}
                self.assertEqual(self._audit_rows(*fields), [
                    ("deny", *shown.values(),
                     f"{shown['tool']} on {shown['server']}: {cp.store.UNENCODABLE}")])
                self.assertEqual(self._count("tool_approvals"), 0)

    def test_args_holding_one_are_refused_where_an_ask_would_store_them(self):
        _tool_rule("create_pull_request", "ask")
        why = "tool arguments hold a lone surrogate, which has no UTF-8 form"
        for args in ({_SURROGATE: 1}, {"a": [{"b": _SURROGATE}]}):
            with self.subTest(args=ascii(args)):
                self._forget_audit()
                self.assertEqual(_tool_call(tool="create_pull_request", args=args),
                                 {"decision": "deny", "reason": why})
                self.assertEqual(self._audit_rows("server", "tool"), [
                    ("deny", "mcp-github", "create_pull_request",
                     f"create_pull_request on mcp-github: {why}")])
                self.assertEqual(self._count("tool_approvals"), 0)
                self.assertEqual(cp.holds._saturation()["rejections"], 0)

    def test_args_holding_one_still_run_under_allow_since_nothing_stores_them(self):
        _tool_rule("get_me", "allow")
        self._forget_audit()
        self.assertEqual(_tool_call(args={"a": [{"b": _SURROGATE}]})["decision"],
                         "allow")
        self.assertEqual(self._audit_rows("tool"), [
            ("allow", "get_me", "get_me on mcp-github: 'allow' by rule (get_me on "
                                "mcp-github)")])

    def test_a_pin_answers_args_holding_one_in_an_unpinned_field(self):
        # Allowed with one row and no ask: nothing stores the args on this path. A
        # change that did would have to refuse them, as the ask path does.
        _tool_rule("create_pull_request", "ask")
        pin_id = _pin(_PINNED)
        self._forget_audit()
        answer = _tool_call(tool="create_pull_request",
                            args={**_PR_ARGS, "body": _SURROGATE})
        self.assertEqual(answer["decision"], "allow")
        (row,) = self._audit_rows("tool")
        self.assertEqual(row[:2], ("allow", "create_pull_request"))
        self.assertIn(f"by pin {pin_id}", row[2])
        self.assertEqual(self._count("tool_approvals"), 0)

    def test_claim_and_withdraw_answer_the_unknown_404_and_audit_it(self):
        _tool_rule("create_pull_request", "ask")
        answer = _tool_call(tool="create_pull_request", args={"n": 1})
        approval_id = _ask_id(answer)
        for verb, call in (("claim", _claim_with), ("withdrawal", _withdraw_with)):
            with self.subTest(verb=verb):
                self._forget_audit()
                response = call(answer["resume_token"], CLASS_IP + _SURROGATE)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(self._audit_rows("client", "approval_id"), [
                    ("deny", CLASS_IP + _ESCAPED, approval_id,
                     f"{verb} for approval {approval_id} refused: it was raised by "
                     f"{CLASS_IP}, not by this caller — a leaked or guessed resume "
                     f"token; answered as unknown")])


class ToolFieldCapTests(_ToolBridgeTestCase):
    """The tool columns are agent-INFLUENCED, and nothing upstream bounds them.

    ``ToolCallRequest.tool`` is a bare ``str`` (app.py) — it arrives from the gateway,
    which relays what the agent named, and no validator caps its length. Before these
    were columns the text landed only inside ``reason``, which ``store._audit`` has
    always capped; now it lands in two places, and the cap has to cover both or one
    oversized call bloats the crown-jewel store and the glanceable list."""

    audits = True

    def test_an_over_long_tool_name_is_capped_rather_than_stored_whole(self):
        _tool_call(tool="x" * 9000)
        with cp.store._connect() as conn:
            row = conn.execute("SELECT tool, reason FROM audit "
                               "WHERE stage='tool-call'").fetchone()
        self.assertEqual(len(row["tool"]), cp.store.DRAIN_MAX_FIELD)
        self.assertLessEqual(len(row["reason"]), cp.store.DRAIN_MAX_FIELD)

    def test_an_over_long_server_name_is_capped_too(self):
        # Refused by policy long before it could be dialled — a server name is held to
        # a DNS label — but the REFUSAL is still audited, and the row is what is capped.
        _tool_call(server="s" * 9000, tool="get_me")
        with cp.store._connect() as conn:
            row = conn.execute("SELECT server FROM audit "
                               "WHERE stage='tool-call'").fetchone()
        self.assertEqual(len(row["server"]), cp.store.DRAIN_MAX_FIELD)


class OutcomeFoldingTests(_CPTestCase):
    """What the GLANCE may fold, once rows can stand for side effects.

    Folding is right for a decision repeated: fifty CONNECTs allowed by one rule is one
    fact fifty times, and a list that showed them all would answer nothing. It is not
    obviously right for an outcome, because an outcome records that something HAPPENED
    — and two pull requests opened is not one event seen twice.

    The line drawn here is narrower than "never fold outcomes": what must never merge is
    two SPENT GRANTS."""

    def _outcomes(self, *rows):
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM audit")
            for i, r in enumerate(rows):
                conn.execute(
                    "INSERT INTO audit(ts, kind, stage, client, server, tool, "
                    "status, approval_id) VALUES (?,'outcome','tool-result',?,?,?,?,?)",
                    (float(i), "172.30.0.130", r.get("server", "mcp-github"),
                     r.get("tool", "get_me"), r.get("status", "ok"),
                     r.get("approval_id")))
            conn.commit()

    def _grouped(self):
        with cp.store._connect() as conn:
            return cp.audit.grouped(conn, 50, cp.audit.parse(), 500)

    def test_two_approved_calls_are_never_one_line(self):
        # THE ONE THAT MATTERS. A grant is single-use, so two approval ids are two
        # distinct human decisions and two side effects. Folded, the count would be the
        # only trace that a second approval ever happened — and the count is exactly
        # what a reader skims past.
        self._outcomes({"tool": "create_pull_request", "approval_id": "a" * 32},
                       {"tool": "create_pull_request", "approval_id": "b" * 32})
        rows = self._grouped()
        # Two rows, each standing for exactly one call. The ids themselves are NOT
        # served here — the glance carries what it displays — so the assertion is the
        # one an operator can actually see: no count above 1 absorbed a second grant.
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["n"] for r in rows}, {1})
        self.assertNotIn("approval_id", rows[0].keys())

    def test_unapproved_calls_still_fold(self):
        # The high-volume read-only rows, which are what the glance exists to keep
        # quiet. Their approval_id is NULL and SQLite groups NULLs together.
        self._outcomes({"tool": "actions_list"}, {"tool": "actions_list"},
                       {"tool": "actions_list"})
        rows = self._grouped()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["n"], 3)

    def test_two_tools_do_not_fold_into_one_unattributable_count(self):
        # Both `ok`, both with a NULL host and a NULL reason — so before the tool
        # columns joined the group key these folded into one row saying "2x" with
        # nothing on it to say what it stood for.
        self._outcomes({"tool": "get_me"}, {"tool": "actions_list"})
        self.assertEqual({r["tool"] for r in self._grouped()},
                         {"get_me", "actions_list"})

    def test_a_failure_does_not_fold_into_the_successes(self):
        # `status` is in the key, so the one row worth noticing cannot be absorbed by
        # the run of ordinary ones around it.
        self._outcomes({}, {}, {"status": "tool-error"})
        rows = {r["status"]: r["n"] for r in self._grouped()}
        self.assertEqual(rows, {"ok": 2, "tool-error": 1})

    def test_the_same_tool_on_two_servers_stays_apart(self):
        # Tool names are not namespaced across servers, so the pair is the identity —
        # the same reason `tool_rules` is UNIQUE on it.
        self._outcomes({"server": "mcp-github", "tool": "issue_read"},
                       {"server": "mcp-other", "tool": "issue_read"})
        self.assertEqual({r["server"] for r in self._grouped()},
                         {"mcp-github", "mcp-other"})


class _FreshStoreTestCase(unittest.TestCase):
    """Base for tests that need a genuinely empty database rather than the shared one:
    schema questions cannot be asked of a store the rest of the suite has been
    writing to. ``name`` is per-test so a leftover file never decides the answer."""

    def _use_store(self, name):
        saved = cp.store.DB_PATH
        path = os.path.join(_TMP, name)
        if os.path.exists(path):
            os.unlink(path)
        cp.store.DB_PATH = path
        self.addCleanup(setattr, cp.store, "DB_PATH", saved)
        return path


class FreshSchemaTests(_FreshStoreTestCase):
    """A brand-new store must carry every column the code names, from the DDL itself.

    ``_migrate`` covers the stores that already exist; this covers the ones created
    from here on, and the two have to agree. If a column fell out of the
    ``CREATE TABLE``, every statement naming it would fail at runtime on a fresh
    deployment while every migrated one kept working — the worst shape of bug this
    schema can have."""

    def test_new_store_has_the_provenance_column(self):
        self._use_store("fresh-schema.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(approvals)")}
        self.assertIn("resolved_by", cols)
        self.assertIn("pattern", cols)
        cp.store._init_db()          # idempotent: a second run must not fail

    def test_new_store_has_the_tool_policy_table(self):
        self._use_store("fresh-tool-rules.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            cols = {r["name"] for r in
                    conn.execute("PRAGMA table_info(tool_rules)")}
        self.assertEqual(cols, {"id", "server", "tool", "action", "source",
                                "created_at"})

    def test_new_store_has_the_leases_table(self):
        # An EXACT set, like the two below it. What the exactness asserts here is that
        # no `pattern` or `action` column has crept in: a lease matches one host by
        # equality and only ever allows, and either column would be an invitation to
        # give the timed path a breadth ladder or a timed deny.
        self._use_store("fresh-leases.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(leases)")}
        self.assertEqual(cols, {"id", "host", "client_class", "approval_id",
                                "created_at", "expires_at", "granted_by"})

    def test_new_store_has_the_pins_table(self):
        # Exact, for the reason the leases set is: no `action` column, because a pin
        # only ever allows — a pinned deny is dodged by any spelling the server reads
        # as the same value.
        self._use_store("fresh-pins.db")
        cp.store._init_db()
        insert = ("INSERT INTO tool_pins(server, tool, pins_json, approval_id, "
                  "created_at, granted_by) VALUES ('s', 't', '{\"a\":1}', ?, 0, 'g')")
        with cp.store._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tool_pins)")}
            conn.execute(insert, ("a1",))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(insert, ("a2",))     # the same pin set from another card
        self.assertEqual(cols, {"id", "server", "tool", "pins_json", "approval_id",
                                "created_at", "granted_by", "expires_at",
                                "schema_digest"})

    def test_a_fresh_store_lets_two_leases_hold_the_same_host(self):
        # The absent UNIQUE, asserted rather than assumed. Unreachable through the API
        # — a live lease decides the request, so no card is raised to grant a second
        # from — and the schema says so by not constraining it, which is what keeps a
        # future insert path from having to choose between ignoring and extending.
        self._use_store("fresh-leases-unique.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            for _ in range(2):
                conn.execute(
                    "INSERT INTO leases(host, client_class, approval_id, created_at, "
                    "expires_at, granted_by) VALUES ('x.example','a','c',0,1,'t')")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 2)

    def test_new_store_has_the_tool_approvals_table(self):
        self._use_store("fresh-tool-approvals.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            cols = {r["name"] for r in
                    conn.execute("PRAGMA table_info(tool_approvals)")}
        # ``deadline`` is the one with no counterpart on ``approvals``, where the
        # window lives in memory because a blocked worker enforces it. Nothing is
        # blocked here, so the window has to be durable or a restart would leave asks
        # pending forever.
        self.assertEqual(cols, {"id", "ts", "server", "tool", "args_json",
                                "args_digest", "client", "status", "deadline",
                                "resolved_at", "resolved_by", "claimed_at",
                                "schema_digest", "resume_token"})

    def test_new_store_has_the_mcp_server_table_and_no_secret_reference(self):
        # Asserted as an EXACT set, and the exactness is the assertion: a column
        # holding a path or a handle to the credential is what must never appear here,
        # because a stored free-text reference is what would let a forged config write
        # point one server at another's secret. It is also what keeps the property
        # that a crown-jewel backup contains no credential. `path` is the URL path the
        # server is dialled on, held to a shape that names no file
        # (``policy._endpoint_error``).
        self._use_store("fresh-mcp-servers.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            cols = {r["name"] for r in
                    conn.execute("PRAGMA table_info(mcp_servers)")}
        self.assertEqual(cols, {"server", "enabled", "auth_type", "auth_header",
                                "auth_template", "created_at", "port", "path"})

    def test_the_tool_policy_key_is_the_server_tool_pair(self):
        # Tool names are not namespaced across servers, so uniqueness has to be the
        # pair. If it were `tool` alone, a second server exposing an identically named
        # tool could not be given its own rule: the INSERT would be ignored and the
        # first server's action would silently decide for both.
        self._use_store("fresh-tool-rules-key.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.executemany(
                "INSERT INTO tool_rules(server, tool, action, source, created_at) "
                "VALUES (?,?,?, 'operator', 0)",
                [("mcp-github", "issue_read", "allow"),
                 ("mcp-other", "issue_read", "deny")])
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tool_rules").fetchone()[0], 2)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tool_rules(server, tool, action, source, "
                    "created_at) VALUES ('mcp-github','issue_read','deny',"
                    "'operator',0)")

    def test_new_store_carries_client_class_on_every_table_that_records_one(self):
        self._use_store("fresh-class-schema.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            for table in ("rules", "audit", "approvals"):
                cols = {r["name"] for r in
                        conn.execute(f"PRAGMA table_info({table})")}
                self.assertIn("client_class", cols, table)

    def test_a_fresh_stores_rules_default_matches_the_migrations(self):
        # The DDL default is mirrored from the migration deliberately, so a fresh
        # store and a migrated one behave identically on an insert that omits the
        # column. Divergence there would be invisible until a deployment that had
        # never migrated hit an insert path the migrated ones had exercised for
        # months.
        self._use_store("fresh-default.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO rules(pattern, action, source, created_at) "
                         "VALUES ('x.example', 'allow', 'test', 0)")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT client_class FROM rules").fetchone()[0],
                cp.store.LEGACY_CLIENT_CLASS)

    def test_a_fresh_store_keys_uniqueness_on_the_pattern_and_the_class(self):
        self._use_store("fresh-unique.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO rules(pattern, action, source, created_at, "
                         "client_class) VALUES ('x.example','allow','test',0,'a')")
            # Same pattern, different class: a different rule, and storable.
            conn.execute("INSERT INTO rules(pattern, action, source, created_at, "
                         "client_class) VALUES ('x.example','block','test',0,'b')")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 2)
            # Same pattern AND class is still one rule — the constraint `resolve`
            # relies on for INSERT OR IGNORE to mean "already in force".
            with self.assertRaises(Exception):
                conn.execute("INSERT INTO rules(pattern, action, source, "
                             "created_at, client_class) "
                             "VALUES ('x.example','allow','test',0,'a')")


class MigrationTests(_FreshStoreTestCase):
    """Upgrading a store that already exists.

    This is the case with the most to lose and the least test coverage available by
    accident: the store is a long-lived named volume holding the policy rules and the
    audit history — the crown jewels — and the alternative to migrating it is `make
    destroy`. So the pre-change schema is built here BY HAND, exactly as the shipped
    code wrote it, and ``_init_db`` is asked to bring it forward."""

    # The rules/audit/approvals DDL as it stood before client_class, reproduced
    # rather than referenced: the point is to start from what is actually on disk in
    # an existing deployment, which no current code path can produce any more.
    _OLD_SCHEMA = (
        """CREATE TABLE rules (
               id INTEGER PRIMARY KEY, pattern TEXT NOT NULL UNIQUE,
               action TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL)""",
        """CREATE TABLE audit (
               id INTEGER PRIMARY KEY, ts REAL NOT NULL, decision TEXT NOT NULL,
               stage TEXT, host TEXT, port INTEGER, proto TEXT, client TEXT,
               method TEXT, url TEXT, reason TEXT)""",
        """CREATE TABLE approvals (
               id TEXT PRIMARY KEY, ts REAL NOT NULL, host TEXT NOT NULL,
               port INTEGER, proto TEXT, client TEXT, method TEXT, url TEXT,
               status TEXT NOT NULL, mode TEXT, resolved_at REAL, resolved_by TEXT)""",
    )

    def _old_store(self, name, rules=(("example.com", "allow", "operator"),)):
        self._use_store(name)
        with cp.store._connect() as conn:
            for ddl in self._OLD_SCHEMA:
                conn.execute(ddl)
            conn.executemany(
                "INSERT INTO rules(pattern, action, source, created_at) "
                "VALUES (?,?,?, 0)", rules)
            conn.execute(
                "INSERT INTO audit(ts, decision, host, client) "
                "VALUES (1.0, 'allow', 'old.example', '172.30.0.130')")
            conn.commit()

    def test_existing_rules_survive_and_are_scoped_to_the_agent(self):
        # The whole risk of this migration in one assertion. Every rule in an existing
        # store was approved while the proxy had ONE client population, so scoping
        # them to the agent is what they already meant; losing them, or widening them
        # to every client, are the two ways to get this wrong.
        self._old_store("migrate-rules.db",
                        rules=(("example.com", "allow", "operator"),
                               ("evil.com", "block", "operator"),
                               ("pypi.org", "allow", "seed")))
        cp.store._init_db()
        with cp.store._connect() as conn:
            rows = {r["pattern"]: (r["action"], r["source"], r["client_class"])
                    for r in conn.execute(
                        "SELECT pattern, action, source, client_class FROM rules")}
        self.assertEqual(rows, {
            "example.com": ("allow", "operator", cp.store.LEGACY_CLIENT_CLASS),
            "evil.com": ("block", "operator", cp.store.LEGACY_CLIENT_CLASS),
            "pypi.org": ("allow", "seed", cp.store.LEGACY_CLIENT_CLASS)})

    def test_rule_ids_are_carried_over_not_regenerated(self):
        # The UI's revoke button keys on the id, so renumbering during a migration
        # would aim a click the operator has already made at a different rule.
        self._old_store("migrate-ids.db",
                        rules=(("a.example", "allow", "operator"),
                               ("b.example", "allow", "operator")))
        with cp.store._connect() as conn:
            before = {r["id"]: r["pattern"]
                      for r in conn.execute("SELECT id, pattern FROM rules")}
        cp.store._init_db()
        with cp.store._connect() as conn:
            after = {r["id"]: r["pattern"]
                     for r in conn.execute("SELECT id, pattern FROM rules")}
        self.assertEqual(before, after)

    def test_the_old_unique_pattern_constraint_is_gone(self):
        # The reason this is a REBUILD and not an ADD COLUMN. Under the old
        # constraint a second class could never have a rule for a host the first
        # already covered: `resolve`'s INSERT OR IGNORE would write nothing and report
        # the rule already present, leaving that client held forever on a host the
        # operator believed they had just approved.
        self._old_store("migrate-unique.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO rules(pattern, action, source, created_at, "
                         "client_class) VALUES ('example.com','allow','operator',0,"
                         "'mcp')")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rules WHERE pattern='example.com'")
                .fetchone()[0], 2)

    def test_audit_and_approvals_gain_a_nullable_column(self):
        # Records, not constraints: a row written before classes existed genuinely has
        # no class, and NULL says so where a backfilled name would put a claim in the
        # audit trail that nothing ever observed.
        self._old_store("migrate-audit.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            self.assertIsNone(
                conn.execute("SELECT client_class FROM audit").fetchone()[0])
            # And the column is writable on new rows, which is the point of adding it.
            conn.execute("INSERT INTO audit(ts, kind, host, client_class) "
                         "VALUES (2.0, 'allow', 'new.example', 'mcp')")
            conn.commit()

    def test_a_migrated_store_decides_exactly_as_it_did_for_the_agent(self):
        # Behavioural, not structural: after the migration the agent's own traffic
        # must be decided by the same rules as before. A migration that preserved the
        # rows but changed what they matched would be worse than one that failed.
        self._old_store("migrate-decide.db",
                        rules=(("example.com", "allow", "operator"),
                               ("evil.com", "block", "operator")))
        cp.store._init_db()
        self.assertEqual(cp.policy._decide("example.com", CLASS)[0], "allow")
        self.assertEqual(cp.policy._decide("evil.com", CLASS)[0], "deny")
        # ...and decides nothing for the population that did not exist before it.
        self.assertEqual(cp.policy._decide("example.com", "mcp")[0], "hold")

    def test_a_migrated_rules_table_has_the_same_schema_as_a_fresh_one(self):
        # Divergence is the failure mode with no symptom: both stores work,
        # differently, until one hits an insert path the other has not. One string
        # (`store._RULES_DDL`) is behind both, through v11's rebuild, so the stored SQL
        # matches byte for byte, as for the pins.
        self._old_store("migrate-schema.db")
        cp.store._init_db()
        migrated = self._rules_sql()
        self._use_store("fresh-for-compare.db")
        cp.store._init_db()
        self.assertEqual(migrated, self._rules_sql())

    @staticmethod
    def _rules_sql():
        with cp.store._connect() as conn:
            return conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'"
            ).fetchone()[0]

    def test_a_new_table_reaches_an_existing_store_without_a_step(self):
        # Why `tool_rules` appends no `_STEPS` entry, asserted rather than argued:
        # `CREATE TABLE IF NOT EXISTS` is a no-op only on a table that already
        # exists, so it CREATES a wholly new one on a long-lived store just as it
        # does on a fresh one. That is the difference between adding a table and
        # adding a column — the latter is silently skipped, which is what the NOTE
        # under `_init_db` is about. If this ever fails, the gateway's policy table
        # is missing on exactly the stores that have real history in them.
        self._old_store("migrate-new-table.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            # EVERY new table, because the decisions below read them all: a rule grants
            # nothing for a server that is not registered and enabled, and a pin
            # nothing without an `ask` rule.
            conn.execute("INSERT INTO mcp_servers(server, enabled, auth_type, "
                         "created_at) VALUES ('mcp-github', 1, 'none', 0)")
            conn.execute("INSERT INTO tool_rules(server, tool, action, source, "
                         "created_at) VALUES ('mcp-github','get_me','allow',"
                         "'operator',0), ('mcp-github','issue_write','ask',"
                         "'operator',0)")
            conn.execute("INSERT INTO tool_pins(server, tool, pins_json, approval_id, "
                         "created_at, granted_by, schema_digest) VALUES ('mcp-github',"
                         "'issue_write','{\"method\":\"create\"}','a1',0,'g',?)",
                         (_SCHEMA,))
            conn.commit()
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "allow")
        self.assertEqual(cp.policy._decide_tool("mcp-github", "issue_write",
                                                {"method": "create"}, _SCHEMA)[0],
                         "allow")

    def test_migration_is_idempotent(self):
        self._old_store("migrate-twice.db")
        cp.store._init_db()
        cp.store._init_db()
        cp.store._init_db()
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 1)
            # And it leaves no scaffolding behind for the next one to trip over.
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("rules_migrating", names)

    def test_a_fresh_store_needs_no_migration_at_all(self):
        # `_migrate` runs on every start, including the first. On an empty file there
        # is nothing to inspect and it must be a clean no-op rather than an error.
        self._use_store("migrate-fresh.db")
        cp.store._migrate()
        cp.store._init_db()
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 0)

    def test_a_failed_migration_leaves_the_rules_intact(self):
        # The crash window the explicit BEGIN/COMMIT exists to close: without a
        # transaction around the copy and the DROP, a failure between them loses every
        # policy rule in the store. Injected at the rename, which is after the DROP.
        self._old_store("migrate-crash.db",
                        rules=(("keep.example", "allow", "operator"),))
        real_connect = cp.store._connect

        class _FailsOnRename:
            """Wraps the connection rather than patching it: sqlite3.Connection.execute
            is read-only, so the fault has to be injected from outside the object."""

            def __init__(self, conn):
                object.__setattr__(self, "_conn", conn)

            def execute(self, sql, *a):
                if "RENAME TO rules" in sql:
                    raise RuntimeError("injected failure mid-migration")
                return self._conn.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def __setattr__(self, name, value):
                setattr(self._conn, name, value)

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, *exc):
                return self._conn.__exit__(*exc)

        with mock.patch.object(cp.store, "_connect",
                               lambda: _FailsOnRename(real_connect())), \
                self.assertRaises(RuntimeError):
            cp.store._migrate()
        # The rules table is still there, still holding the rule, still on the old
        # schema — a failed migration that can be retried, not a destroyed store.
        with cp.store._connect() as conn:
            rows = [r["pattern"] for r in conn.execute("SELECT pattern FROM rules")]
        self.assertEqual(rows, ["keep.example"])
        # And a retry after the fault clears completes it.
        cp.store._init_db()
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT client_class FROM rules").fetchone()[0],
                cp.store.LEGACY_CLIENT_CLASS)


class SchemaVersionTests(_FreshStoreTestCase):
    """What decides that a migration step runs, now that the schema is not the thing
    being interrogated.

    ``MigrationTests`` above covers what step 1 DOES; this covers the loop that picks
    steps. The failure it exists to catch has no symptom at start-up: a step that runs
    a second time on a store already past it completes without error and silently
    rewrites data — for step 1, every per-class rule an operator made would be dragged
    back to the legacy class."""

    def _old_store(self, name):
        """A store on the pre-client_class schema — the same hand-built shape
        ``MigrationTests`` starts from, which is where its DDL is explained."""
        self._use_store(name)
        with cp.store._connect() as conn:
            for ddl in MigrationTests._OLD_SCHEMA:
                conn.execute(ddl)
            conn.execute("INSERT INTO rules(pattern, action, source, created_at) "
                         "VALUES ('example.com','allow','operator',0)")
            conn.commit()

    @staticmethod
    def _version():
        with cp.store._connect() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def test_a_fresh_store_is_stamped_at_the_current_version(self):
        # A fresh store gets today's schema from the DDL, so it must come out stamped
        # as current — not 0. Stamped 0, the next release would run every step in
        # ``_STEPS`` over a store that already has their result.
        self._use_store("version-fresh.db")
        cp.store._init_db()
        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION)

    def test_a_migrated_store_is_stamped_at_the_current_version(self):
        self._old_store("version-migrated.db")
        self.assertEqual(self._version(), 0)
        cp.store._init_db()
        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION)

    def test_an_unstamped_store_past_a_step_is_placed_not_re_run(self):
        # The store the PREVIOUS release left behind: migrated by the old
        # shape-keyed code, so it has client_class and no stamp. `_detect_version`
        # has to place it at 1 from its schema. If it placed it at 0, step 1 would
        # re-run and re-scope every rule to the legacy class — including the
        # per-class rules that are the whole reason the column exists.
        self._old_store("version-unstamped.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO rules(pattern, action, source, created_at, "
                         "client_class) VALUES ('tools.example','allow','operator',"
                         "0,'mcp')")
            conn.execute("PRAGMA user_version = 0")     # back to unstamped
            conn.commit()

        cp.store._init_db()

        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION)
        with cp.store._connect() as conn:
            rows = {r["pattern"]: r["client_class"]
                    for r in conn.execute("SELECT pattern, client_class FROM rules")}
        self.assertEqual(rows, {"example.com": cp.store.LEGACY_CLIENT_CLASS,
                                "tools.example": "mcp"})

    def test_a_failed_step_leaves_the_version_where_it_was(self):
        # The stamp is written inside the step's transaction, so a failure must roll
        # it back with the schema change. A stamp that survived a failed step would
        # be the worst outcome available: the store would be recorded as migrated,
        # the next start would skip the step, and nothing would ever complete it.
        self._old_store("version-failed-step.db")

        def boom(conn):
            conn.execute("ALTER TABLE rules ADD COLUMN half_applied TEXT")
            raise RuntimeError("injected failure mid-step")

        with mock.patch.object(cp.store, "_STEPS", ((1, "boom", boom),)), \
                self.assertRaises(RuntimeError):
            cp.store._migrate()

        self.assertEqual(self._version(), 0)
        with cp.store._connect() as conn:
            self.assertNotIn("half_applied", cp.store._columns(conn, "rules"))

    def test_an_old_store_gains_the_leases_table(self):
        # Step 2 is the cheapest shape a step has — a new table — but it still has to
        # RUN on the stores already in the field. Without it, `resolve` would 500 on
        # every lease and `_decide` on every request, on exactly the deployments that
        # have been governing egress the longest.
        self._old_store("version-leases.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(leases)")}
        self.assertEqual(cols, {"id", "host", "client_class", "approval_id",
                                "created_at", "expires_at", "granted_by"})
        # And the rules the earlier step migrated are untouched by it.
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 1)

    def test_a_migrated_and_a_fresh_store_agree_on_the_leases_table(self):
        # The v1 rules rebuild kept two near-copies of its DDL and needed a test to
        # hold them equal; this table shares ONE string (`store._LEASES_DDL`), so what
        # is asserted is that both paths really use it rather than that two copies
        # match.
        self._old_store("agree-migrated.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            migrated = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='leases'").fetchone()[0]
        self._use_store("agree-fresh.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            fresh = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='leases'").fetchone()[0]
        self.assertEqual(migrated, fresh)

    def test_an_old_store_gains_the_tool_correlation_columns(self):
        # Step 3 on the stores already in the field. Without it every `_audit` call
        # fails on the INSERT naming columns that are not there — which is every
        # decision, egress included, on exactly the deployments that have been
        # governing the longest.
        self._old_store("version-correlation.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit)")}
        self.assertLessEqual({"server", "tool", "approval_id"}, cols)

    def test_an_audit_row_that_predates_the_columns_is_not_backfilled(self):
        # NULL means "nothing observed this", and that is the honest value for a row
        # written before the gateway reported one. Some of them DO name a server inside
        # their reason prose — filling the column from that would be the record
        # asserting something it derived, in the one table whose worth is that it
        # only holds what was seen.
        self._old_store("version-correlation-rows.db")
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO audit(ts, decision, stage, reason) VALUES "
                         "(1.0, 'hold', 'tool-call', 'get_me on mcp-github: ask')")
            conn.commit()
        cp.store._init_db()
        with cp.store._connect() as conn:
            row = conn.execute("SELECT server, tool, approval_id, reason FROM audit "
                               "WHERE stage='tool-call'").fetchone()
        self.assertEqual((row["server"], row["tool"], row["approval_id"]),
                         (None, None, None))
        self.assertIn("mcp-github", row["reason"])      # and the prose is untouched

    def test_a_migrated_and_a_fresh_store_agree_on_the_audit_columns(self):
        # The DDL and the step are two spellings of one schema with nothing tying
        # them: a column added to `_init_db` alone is missing on every store in the
        # field, and one added to the step alone is missing from every new one. The
        # ORDER legitimately differs — `ALTER TABLE` appends, and `client_class` sits
        # mid-table in the DDL — so the SET is what has to match.
        def columns_of(name):
            self._use_store(name)
            cp.store._init_db()
            with cp.store._connect() as conn:
                return {r["name"] for r in conn.execute("PRAGMA table_info(audit)")}

        self._old_store("audit-agree-migrated.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            migrated = {r["name"] for r in conn.execute("PRAGMA table_info(audit)")}
        self.assertEqual(migrated, columns_of("audit-agree-fresh.db"))

    def test_an_old_store_gains_the_outcome_status_column(self):
        # Step 4 on the stores already in the field. `_audit`'s INSERT names it, so
        # without this EVERY audit write fails — egress included — on exactly the
        # deployments that have been governing the longest.
        self._old_store("version-status.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            self.assertIn("status", {r["name"] for r in
                                     conn.execute("PRAGMA table_info(audit)")})

    def test_status_is_a_step_of_its_own_rather_than_part_of_v3(self):
        # The split was deliberate: when the correlation columns landed nothing could
        # write this one, and a column no writer fills is schema documenting an
        # intention rather than a record. Asserted so a later tidy-up does not merge
        # them — a store already stamped v3 would then never gain the column.
        self.assertEqual([label for v, label, _ in cp.store._STEPS if v == 4],
                         ["tool outcome status"])

    def test_an_old_store_renames_the_decision_column(self):
        # Step 5 on the stores already in the field. `_audit`'s INSERT names `kind`, so
        # without it EVERY audit write fails — egress included — on exactly the
        # deployments with the most history to lose.
        self._old_store("version-kind.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit)")}
        self.assertIn("kind", cols)
        self.assertNotIn("decision", cols)

    def test_the_rename_carries_the_rows_rather_than_reinventing_them(self):
        # THE ONE THAT MATTERS on this table. A RENAME keeps the data in place, where a
        # new-column-and-copy has a window in which the two disagree and a half-run
        # migration leaves rows whose kind was invented by a migration rather than
        # recorded by a writer. On the audit trail that is the worst available failure.
        self._old_store("version-kind-rows.db")
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO audit(ts, decision, host, reason) "
                         "VALUES (1.0, 'hold', 'evil.example', 'held for a human')")
            conn.commit()
        cp.store._init_db()
        with cp.store._connect() as conn:
            row = conn.execute("SELECT kind, host, reason FROM audit "
                               "WHERE host='evil.example'").fetchone()
        self.assertEqual((row["kind"], row["reason"]), ("hold", "held for a human"))

    def test_v6_adds_the_persisted_pattern_to_approvals(self):
        # A store from before the column, brought forward: the column exists, old
        # rows read NULL, and `_decision_scope` on such a row still says a rule was
        # written without inventing which.
        self._old_store("version-pattern.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO approvals(id, ts, host, status, mode) "
                         "VALUES ('old-persist', 1.0, 'old.example', 'allowed', "
                         "'persist')")
            conn.commit()
            row = conn.execute("SELECT status, mode, resolved_by, pattern FROM "
                               "approvals WHERE id='old-persist'").fetchone()
        self.assertIsNone(row["pattern"])
        self.assertEqual(cp.api_authorize._decision_scope(row), "standing rule written")

    def test_v7_adds_the_actor_without_reading_it_out_of_old_reasons(self):
        # The actor of an old config row IS in its reason, and stays there only: a
        # column filled by parsing sentences would record what a regex found.
        self._old_store("version-actor.db")
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO audit(ts, decision, host, reason) VALUES "
                         "(1.0, 'create', '.example.com', "
                         "'allow rule created by peer=172.31.0.9; .example.com')")
            conn.commit()
        cp.store._init_db()
        with cp.store._connect() as conn:
            row = conn.execute("SELECT actor, reason FROM audit "
                               "WHERE host='.example.com'").fetchone()
        self.assertIsNone(row["actor"])
        self.assertIn("peer=172.31.0.9", row["reason"])

    # `tool_pins` as #155 created it, before `expires_at`.
    _PINS_V7 = """CREATE TABLE tool_pins (
        id INTEGER PRIMARY KEY, server TEXT NOT NULL, tool TEXT NOT NULL,
        pins_json TEXT NOT NULL, approval_id TEXT NOT NULL, created_at REAL NOT NULL,
        granted_by TEXT NOT NULL, UNIQUE(server, tool, pins_json))"""

    def _v7_store(self, name, pins_table):
        """A store at v7, with ``tool_pins`` as #155 made it or, where
        ``pins_table`` is False, from before it existed."""
        self._old_store(name)
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("DROP TABLE tool_pins")
            if pins_table:
                conn.execute(self._PINS_V7)
                conn.execute("INSERT INTO tool_pins(server, tool, pins_json, "
                             "approval_id, created_at, granted_by) "
                             "VALUES ('s', 't', '{\"repo\":\"dockade\"}', 'a', 0, 'g')")
            conn.execute("PRAGMA user_version = 7")
            conn.commit()

    def test_v8_drops_the_pins_audits_each_and_recreates_the_table(self):
        # Discarding a pin only sends its calls back to a card, which is why this one
        # step may drop policy rather than carry it.
        self._v7_store("version-pin-expiry.db", pins_table=True)
        cp.store._init_db()
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tool_pins").fetchone()[0], 0)
            self.assertIn("expires_at", cp.store._columns(conn, "tool_pins"))
            self.assertIn("AUTOINCREMENT", conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='tool_pins'").fetchone()[0])
            [row] = conn.execute(
                "SELECT kind, stage, server, tool, approval_id, reason FROM audit "
                "WHERE stage='tool-policy'").fetchall()
        self.assertEqual((row["kind"], row["server"], row["tool"], row["approval_id"]),
                         ("revoke", "s", "t", "a"))
        self.assertIn("dropped by schema v8", row["reason"])
        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION)
        # Numbering continues past the dropped pin, so "pin 1" names one pin.
        with cp.store._connect() as conn:
            new_id = conn.execute(
                "INSERT INTO tool_pins(server, tool, pins_json, approval_id, "
                "created_at, granted_by) VALUES ('s', 't', '{\"a\":1}', 'b', 0, 'g')"
                ).lastrowid
        self.assertEqual(new_id, 2)

    def test_a_migrated_and_a_fresh_store_agree_on_the_pins_table(self):
        # One string (`store._TOOL_PINS_DDL`) behind both, as for the leases.
        self._v7_store("pins-agree-migrated.db", pins_table=True)
        cp.store._init_db()
        with cp.store._connect() as conn:
            migrated = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='tool_pins'").fetchone()[0]
        self._use_store("pins-agree-fresh.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            fresh = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='tool_pins'").fetchone()[0]
        self.assertEqual(migrated, fresh)

    def test_v8_on_a_store_from_before_the_pins_table(self):
        # Stamped v7 by #149-#154, which had no `tool_pins`: the step has nothing to
        # alter, and the DDL creates the table with the column.
        self._v7_store("version-pin-expiry-no-table.db", pins_table=False)
        cp.store._init_db()
        with cp.store._connect() as conn:
            self.assertIn("expires_at", cp.store._columns(conn, "tool_pins"))
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM audit WHERE stage='tool-policy'").fetchone()[0], 0)

    # `leases` as v2 created it, before AUTOINCREMENT.
    _LEASES_V8 = """CREATE TABLE leases (
        id INTEGER PRIMARY KEY, host TEXT NOT NULL, client_class TEXT NOT NULL,
        approval_id TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
        granted_by TEXT NOT NULL)"""

    def test_v9_rebuilds_the_leases_keeping_each_under_its_id(self):
        self._old_store("version-lease-ids.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("DROP TABLE leases")
            conn.execute(self._LEASES_V8)
            conn.executemany(
                "INSERT INTO leases(id, host, client_class, approval_id, created_at, "
                "expires_at, granted_by) VALUES (?, ?, ?, ?, 0, ?, 'g')",
                [(1, "lapsed.test", CLASS, "a", 1.0),
                 (2, "live.test", CLASS, "b", time.time() + 900)])
            conn.execute("PRAGMA user_version = 8")
            conn.commit()
        cp.store._init_db()
        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION)
        with cp.store._connect() as conn:
            self.assertEqual(
                [(r["id"], r["host"], r["approval_id"]) for r in conn.execute(
                    "SELECT id, host, approval_id FROM leases ORDER BY id")],
                [(1, "lapsed.test", "a"), (2, "live.test", "b")])
            self.assertIn("AUTOINCREMENT", conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='leases'").fetchone()[0])
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='leases_v8'").fetchone())
        # Carried, so the live one still decides and nothing was narrowed.
        self.assertEqual(cp.policy._decide("live.test", CLASS)[0], "allow")
        # The highest id swept, and the next lease does not take it.
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM leases WHERE id=2")
            new_id = conn.execute(
                "INSERT INTO leases(host, client_class, approval_id, created_at, "
                "expires_at, granted_by) VALUES ('next.test', ?, 'c', 0, 0, 'g')",
                (CLASS,)).lastrowid
        self.assertEqual(new_id, 3)

    def test_v9_on_a_store_missing_the_leases_table_still_boots(self):
        # A damaged store, not one any release leaves: refusing to boot over it would
        # leave hand SQL as the only way out, and the DDL creates the table anyway.
        self._old_store("version-lease-ids-no-table.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("DROP TABLE leases")
            conn.execute("PRAGMA user_version = 8")
            conn.commit()
        cp.store._init_db()
        with cp.store._connect() as conn:
            self.assertIn("AUTOINCREMENT", conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='leases'").fetchone()[0])

    # `mcp_servers` as it was before v10.
    _MCP_SERVERS_V9 = """CREATE TABLE mcp_servers (
        server TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
        auth_type TEXT NOT NULL DEFAULT 'none', auth_header TEXT, auth_template TEXT,
        created_at REAL NOT NULL)"""

    def test_v10_keeps_each_server_where_it_was_dialled(self):
        self._old_store("version-server-endpoint.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("DROP TABLE mcp_servers")
            conn.execute(self._MCP_SERVERS_V9)
            conn.execute("INSERT INTO mcp_servers(server, enabled, created_at) "
                         "VALUES ('mcp-github', 1, 0)")
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
        cp.store._init_db()
        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION)
        [server] = cp.api_mcp.api_mcp_servers()
        self.assertEqual(server["endpoint"], {"port": 8082, "path": "/mcp"})
        self.assertTrue(server["enabled"])
        # The step's defaults are the API's, so a migrated row and a fresh
        # registration with no endpoint given are dialled the same way.
        self.assertEqual((cp.policy.DEFAULT_PORT, cp.policy.DEFAULT_PATH),
                         (8082, "/mcp"))

    def test_a_migrated_and_a_fresh_store_agree_on_the_server_columns(self):
        # Compared column by column rather than by the stored SQL: v10 ALTERs the
        # columns in, so its text differs from the CREATE even where the schema agrees.
        def columns(name, migrate):
            self._old_store(name) if migrate else self._use_store(name)
            cp.store._init_db()
            with cp.store._connect() as conn:
                if migrate:
                    conn.execute("DROP TABLE mcp_servers")
                    conn.execute(self._MCP_SERVERS_V9)
                    conn.execute("PRAGMA user_version = 9")
                    conn.commit()
            cp.store._init_db()
            with cp.store._connect() as conn:
                return {tuple(r)[1:] for r in
                        conn.execute("PRAGMA table_info(mcp_servers)")}

        self.assertEqual(columns("agree-servers-migrated.db", True),
                         columns("agree-servers-fresh.db", False))

    # `rules` as v1 rebuilt it and `tool_rules` as it arrived, before AUTOINCREMENT.
    _RULES_V10 = """CREATE TABLE rules (
        id INTEGER PRIMARY KEY, pattern TEXT NOT NULL, action TEXT NOT NULL,
        source TEXT NOT NULL, created_at REAL NOT NULL,
        client_class TEXT NOT NULL DEFAULT 'sandbox', UNIQUE(pattern, client_class))"""
    _TOOL_RULES_V10 = """CREATE TABLE tool_rules (
        id INTEGER PRIMARY KEY, server TEXT NOT NULL, tool TEXT NOT NULL,
        action TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
        UNIQUE(server, tool))"""

    def _v10_store(self, name, tool_rules_table=True):
        """A store at v10, with both policy tables as v10 left them or, where
        ``tool_rules_table`` is False, from before ``tool_rules`` existed. The ids have
        gaps, as revokes leave them, so a copy that renumbered would be seen. One rule
        is scoped to a class other than the column's default, so a copy that left
        ``client_class`` out would be seen too."""
        self._old_store(name)
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("DROP TABLE rules")
            conn.execute(self._RULES_V10)
            conn.executemany(
                "INSERT INTO rules(id, pattern, action, source, created_at, "
                "client_class) VALUES (?, ?, ?, 'operator', 0, ?)",
                [(2, "kept.test", "allow", CLASS), (3, "tools.test", "allow", "mcp"),
                 (5, "blocked.test", "block", CLASS)])
            conn.execute("DROP TABLE tool_rules")
            if tool_rules_table:
                conn.execute(self._TOOL_RULES_V10)
                conn.executemany(
                    "INSERT INTO tool_rules(id, server, tool, action, source, "
                    "created_at) VALUES (?, 'mcp-github', ?, ?, 'operator', 0)",
                    [(2, "get_me", "allow"), (5, "delete_repo", "deny")])
            conn.execute("PRAGMA user_version = 10")
            conn.commit()

    def test_v11_rebuilds_both_policy_tables_keeping_each_rule_under_its_id(self):
        self._v10_store("version-rule-ids.db")
        cp.store._init_db()
        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION)
        with cp.store._connect() as conn:
            self.assertEqual(
                [tuple(r) for r in conn.execute(
                    "SELECT id, pattern, action, client_class FROM rules ORDER BY id")],
                [(2, "kept.test", "allow", CLASS), (3, "tools.test", "allow", "mcp"),
                 (5, "blocked.test", "block", CLASS)])
            self.assertEqual(
                [tuple(r) for r in conn.execute(
                    "SELECT id, tool, action FROM tool_rules ORDER BY id")],
                [(2, "get_me", "allow"), (5, "delete_repo", "deny")])
            for table in ("rules", "tool_rules"):
                self.assertIn("AUTOINCREMENT", conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?",
                    (table,)).fetchone()[0])
                self.assertIsNone(conn.execute(
                    "SELECT name FROM sqlite_master WHERE name=?",
                    (f"{table}_v10",)).fetchone())
        # Carried, so policy decides as it did.
        self.assertEqual(cp.policy._decide("kept.test", CLASS)[0], "allow")
        self.assertEqual(cp.policy._decide("blocked.test", CLASS)[0], "deny")
        self.assertEqual(cp.policy._decide("tools.test", "mcp")[0], "allow")
        self.assertEqual(cp.policy._decide("tools.test", CLASS)[0], "hold")
        _register()
        _enable()
        self.assertEqual(cp.policy._decide_tool("mcp-github", "get_me")[0], "allow")
        # The highest id revoked, and the next rule does not take it.
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM rules WHERE id=5")
            conn.execute("DELETE FROM tool_rules WHERE id=5")
            new_rule = conn.execute(
                "INSERT INTO rules(pattern, action, source, created_at, client_class) "
                "VALUES ('next.test', 'allow', 'operator', 0, ?)", (CLASS,)).lastrowid
            new_tool_rule = conn.execute(
                "INSERT INTO tool_rules(server, tool, action, source, created_at) "
                "VALUES ('mcp-github', 'get_teams', 'ask', 'operator', 0)").lastrowid
        self.assertEqual((new_rule, new_tool_rule), (6, 6))

    def test_a_failed_v11_leaves_both_policy_tables_as_they_were(self):
        # Failed at the second table, after the first is already rebuilt: the
        # transaction has to take back the first table's rebuild too.
        self._v10_store("version-rule-ids-failed.db")
        with mock.patch.object(cp.store, "_TOOL_RULES_DDL", "CREATE TABLE broken ("), \
                self.assertRaises(sqlite3.OperationalError):
            cp.store._init_db()
        self.assertEqual(self._version(), 10)
        with cp.store._connect() as conn:
            for table, ids in (("rules", [2, 3, 5]), ("tool_rules", [2, 5])):
                self.assertEqual([r[0] for r in conn.execute(
                    f"SELECT id FROM {table} ORDER BY id")], ids)  # noqa: S608
                self.assertNotIn("AUTOINCREMENT", conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?",
                    (table,)).fetchone()[0])
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_v10'").fetchone())
        cp.store._init_db()                             # and a retry completes it
        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION)

    def test_v11_on_a_store_from_before_the_tool_rules_table(self):
        self._v10_store("version-rule-ids-no-tool-rules.db", tool_rules_table=False)
        cp.store._init_db()
        with cp.store._connect() as conn:
            self.assertIn("AUTOINCREMENT", conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='tool_rules'").fetchone()[0])

    def test_a_migrated_and_a_fresh_store_agree_on_the_tool_rules_table(self):
        self._v10_store("tool-rules-agree-migrated.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            migrated = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='tool_rules'").fetchone()[0]
        self._use_store("tool-rules-agree-fresh.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            fresh = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='tool_rules'").fetchone()[0]
        self.assertEqual(migrated, fresh)

    def test_a_store_already_renamed_is_left_alone(self):
        # `ALTER TABLE ... RENAME COLUMN` raises rather than no-ops if it runs twice.
        # The version stamp should make that unreachable; the guard is there because
        # this is the table where being wrong is least recoverable.
        self._old_store("version-kind-twice.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("PRAGMA user_version = 4")     # pretend v5 never ran
            conn.commit()
        cp.store._init_db()                             # must not raise
        with cp.store._connect() as conn:
            self.assertIn("kind", {r["name"] for r in
                                   conn.execute("PRAGMA table_info(audit)")})

    def test_a_store_newer_than_the_code_refuses_to_boot_and_is_left_alone(self):
        # Older code can read newer policy more widely than it was written, so the
        # way back past a step is a restore, never a quiet re-stamp.
        self._use_store("version-newer.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute(f"PRAGMA user_version = {cp.store.SCHEMA_VERSION + 1}")
            conn.commit()
        with self.assertRaisesRegex(RuntimeError, "newer than this code"):
            cp.store._init_db()
        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION + 1)

    def test_the_steps_are_contiguous_and_end_at_the_declared_version(self):
        # ``SCHEMA_VERSION`` and ``_STEPS`` are two halves of one fact, and only this
        # holds them together. A step appended without the bump would never run (its
        # version is reachable, but nothing stamps past it on a fresh store); a bump
        # without the step would stamp stores as having applied one that does not
        # exist, so it could never be added later.
        versions = [v for v, _, _ in cp.store._STEPS]
        self.assertEqual(versions, list(range(1, cp.store.SCHEMA_VERSION + 1)))


class SchemaDigestMigrationTests(_FreshStoreTestCase):
    """v12, which binds pins to the digest of the tool's arguments: every pin made
    before it is dropped and audited (the operator's choice, over binding each to the
    first call that meets it), and an ask from before it keeps no digest."""

    # `tool_pins` and `tool_approvals` as v8 and the tool bridge left them, before v12.
    _PINS_V11 = """CREATE TABLE tool_pins (
        id INTEGER PRIMARY KEY AUTOINCREMENT, server TEXT NOT NULL, tool TEXT NOT NULL,
        pins_json TEXT NOT NULL, approval_id TEXT NOT NULL, created_at REAL NOT NULL,
        granted_by TEXT NOT NULL,
        expires_at REAL CHECK (expires_at IS NULL OR typeof(expires_at) = 'real'),
        UNIQUE(server, tool, pins_json))"""
    _ASKS_V11 = """CREATE TABLE tool_approvals (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, server TEXT NOT NULL, tool TEXT NOT NULL,
        args_json TEXT NOT NULL, args_digest TEXT NOT NULL, client TEXT,
        status TEXT NOT NULL, deadline REAL NOT NULL, resolved_at REAL,
        resolved_by TEXT, claimed_at REAL)"""

    def _v11_store(self, name):
        """A store at v11 holding two pins, the higher one revoked already so the
        sequence is past every row, and one pending ask."""
        self._use_store(name)
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("DROP TABLE tool_pins")
            conn.execute(self._PINS_V11)
            conn.executemany(
                "INSERT INTO tool_pins(id, server, tool, pins_json, approval_id, "
                "created_at, granted_by, expires_at) VALUES (?, 'mcp-github', ?, ?, ?, "
                "0, 'g', ?)",
                [(4, "issue_read", '{"method":"get"}', "card-4", None),
                 (7, "issue_write", '{"method":"create"}', "card-7", 4102444800.0),
                 (9, "issue_write", '{"method":"update"}', "card-9", None)])
            conn.execute("DELETE FROM tool_pins WHERE id=9")
            conn.execute("DROP TABLE tool_approvals")
            conn.execute(self._ASKS_V11)
            conn.execute(
                "INSERT INTO tool_approvals(id, ts, server, tool, args_json, "
                "args_digest, client, status, deadline) VALUES ('old-ask', ?, "
                "'mcp-github', 'issue_read', '{\"method\":\"get\"}', 'd', NULL, "
                "'pending', ?)", (time.time(), time.time() + 3600))
            conn.execute("PRAGMA user_version = 11")
            conn.commit()

    @staticmethod
    def _version():
        with cp.store._connect() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    @staticmethod
    def _table(name):
        with cp.store._connect() as conn:
            return [tuple(r) for r in conn.execute(f"PRAGMA table_info({name})")]

    def test_v12_is_the_pin_binding_step(self):
        self.assertEqual(cp.store._STEPS[11][:2],
                         (12, "pins bound to the tool's arguments"))

    def test_v12_drops_every_pin_audits_each_and_keeps_counting(self):
        self._v11_store("schema-digest-pins.db")
        cp.store._init_db()
        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION)
        with cp.store._connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tool_pins").fetchone()[0], 0)
            rows = [dict(r) for r in conn.execute(
                "SELECT kind, stage, server, tool, approval_id, reason FROM audit "
                "WHERE stage='tool-policy' ORDER BY id")]
            new_id = conn.execute(
                "INSERT INTO tool_pins(server, tool, pins_json, approval_id, "
                "created_at, granted_by, schema_digest) VALUES ('s', 't', "
                "'{\"a\":1}', 'b', 0, 'g', ?)", (_SCHEMA,)).lastrowid
        self.assertEqual(
            [(r["kind"], r["server"], r["tool"], r["approval_id"]) for r in rows],
            [("revoke", "mcp-github", "issue_read", "card-4"),
             ("revoke", "mcp-github", "issue_write", "card-7")])
        self.assertTrue(rows[0]["reason"].startswith("pin 4 dropped by schema v12"))
        self.assertTrue(rows[1]["reason"].startswith("pin 7 dropped by schema v12"))
        # Past the revoked pin 9 too, so no id a page or the audit named comes back.
        self.assertEqual(new_id, 10)

    def test_v12_keeps_a_pending_ask_which_cannot_then_be_pinned(self):
        self._v11_store("schema-digest-asks.db")
        cp.store._init_db()
        [card] = cp.holds._list_tool_asks()
        self.assertEqual((card["id"], card["schema_digest"]), ("old-ask", None))
        self.assertIn("no digest", card["pin_options"]["refused"])

    def test_a_store_below_v8_loses_its_pins_once_to_v8(self):
        self._use_store("schema-digest-below-v8.db")
        with cp.store._connect() as conn:
            for ddl in MigrationTests._OLD_SCHEMA:
                conn.execute(ddl)
            conn.execute(SchemaVersionTests._PINS_V7)
            conn.execute("INSERT INTO tool_pins(server, tool, pins_json, approval_id, "
                         "created_at, granted_by) VALUES ('s', 't', "
                         "'{\"repo\":\"dockade\"}', 'a', 0, 'g')")
            conn.commit()
        cp.store._init_db()
        self.assertEqual(self._version(), cp.store.SCHEMA_VERSION)
        with cp.store._connect() as conn:
            reasons = [r["reason"] for r in conn.execute(
                "SELECT reason FROM audit WHERE stage='tool-policy'")]
            self.assertIn("schema_digest", cp.store._columns(conn, "tool_pins"))
            self.assertIn("schema_digest", cp.store._columns(conn, "tool_approvals"))
        self.assertEqual(len(reasons), 1)
        self.assertIn("dropped by schema v8", reasons[0])

    def test_v12_skips_a_table_the_store_does_not_have(self):
        # A store stamped before either table existed: the DDL after the steps
        # creates both with the column.
        self._use_store("schema-digest-no-tables.db")
        with cp.store._connect() as conn:
            for ddl in MigrationTests._OLD_SCHEMA:
                conn.execute(ddl)
            cp.store._step_12_schema_digest(conn)
            self.assertEqual(cp.store._columns(conn, "tool_pins"), set())
            self.assertEqual(cp.store._columns(conn, "tool_approvals"), set())

    def test_a_migrated_and_a_fresh_store_agree_on_both_tables(self):
        # Compared column by column, as v10's: v12 ALTERs the column in, so the stored
        # SQL differs from the CREATE even where the schema agrees. The column is last
        # in the DDL, so the order agrees too.
        self._v11_store("schema-digest-agree-migrated.db")
        cp.store._init_db()
        migrated = {t: self._table(t) for t in ("tool_pins", "tool_approvals")}
        self._use_store("schema-digest-agree-fresh.db")
        cp.store._init_db()
        self.assertEqual(migrated,
                         {t: self._table(t) for t in ("tool_pins", "tool_approvals")})


class ResumeTokenMigrationTests(_FreshStoreTestCase):
    """v13, which gives each ask a resume token apart from its id. Only a PENDING row
    gets one: no token was issued for any other, and the operator chose to fail closed
    on the handles agents hold from before (task 138's decision)."""

    _ASKS_V12 = SchemaDigestMigrationTests._ASKS_V11[:-1] + ", schema_digest TEXT)"

    def _v12_store(self, name):
        self._use_store(name)
        cp.store._init_db()
        now = time.time()
        with cp.store._connect() as conn:
            conn.execute("DROP INDEX IF EXISTS tool_approvals_resume_token")
            conn.execute("DROP TABLE tool_approvals")
            conn.execute(self._ASKS_V12)
            conn.executemany(
                "INSERT INTO tool_approvals(id, ts, server, tool, args_json, "
                "args_digest, client, status, deadline, resolved_at) VALUES "
                "(?, ?, 'mcp-github', 'issue_read', '{}', ?, ?, ?, ?, ?)",
                [("p1", now, "d1", CLASS_IP, "pending", now + 3600, None),
                 ("p2", now, "d2", CLASS_IP, "pending", now + 3600, None),
                 ("a1", now, "d3", CLASS_IP, "allowed", now + 3600, now),
                 ("x1", now, "d4", CLASS_IP, "denied", now + 3600, now)])
            conn.execute("PRAGMA user_version = 12")
            conn.commit()

    @staticmethod
    def _tokens():
        with cp.store._connect() as conn:
            return {r["id"]: r["resume_token"] for r in conn.execute(
                "SELECT id, resume_token FROM tool_approvals")}

    def test_v13_is_the_thirteenth_step(self):
        self.assertEqual(cp.store._STEPS[12][:2], (13, "resume tokens on tool asks"))

    def test_pending_rows_get_distinct_tokens_and_every_other_row_keeps_null(self):
        self._v12_store("resume-token-mint.db")
        cp.store._init_db()
        tokens = self._tokens()
        self.assertRegex(tokens["p1"], r"^[0-9a-f]{32}$")
        self.assertRegex(tokens["p2"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(tokens["p1"], tokens["p2"])
        self.assertNotIn(tokens["p1"], ("p1", "p2"))
        self.assertEqual((tokens["a1"], tokens["x1"]), (None, None))

    def test_an_identical_retry_joins_a_migrated_pending_ask_and_gets_its_token(self):
        self._v12_store("resume-token-join.db")
        cp.store._init_db()
        _register()
        _enable()
        _tool_rule("issue_read", "ask")
        with cp.store._connect() as conn:
            conn.execute("UPDATE tool_approvals SET args_digest=? WHERE id='p1'",
                         (cp.holds._args_digest("mcp-github", "issue_read", "{}"),))
            conn.commit()
        answer = _tool_call(tool="issue_read", args={}, schema_digest=None)
        self.assertTrue(answer["joined"])
        self.assertEqual(answer["resume_token"], self._tokens()["p1"])

    def test_a_handle_from_before_the_upgrade_is_answered_as_unknown(self):
        # Fail closed: the approved, unclaimed ask's agent holds its id, and the id is
        # no longer a handle. It lapses at its grant window.
        self._v12_store("resume-token-fail-closed.db")
        cp.store._init_db()
        _register()
        _enable()
        resp = _claim_with("a1")
        self.assertEqual(resp.status_code, 404)
        self.assertIsNone(cp.holds._get_tool_ask("a1")["claimed_at"])

    def test_a_migrated_and_a_fresh_table_agree(self):
        self._v12_store("resume-token-agree-migrated.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            migrated = ([tuple(r) for r in conn.execute(
                "PRAGMA table_info(tool_approvals)")],
                [tuple(r)[1:] for r in conn.execute(
                    "PRAGMA index_list(tool_approvals)")])
        self._use_store("resume-token-agree-fresh.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            fresh = ([tuple(r) for r in conn.execute(
                "PRAGMA table_info(tool_approvals)")],
                [tuple(r)[1:] for r in conn.execute(
                    "PRAGMA index_list(tool_approvals)")])
        self.assertEqual(sorted(migrated[1]), sorted(fresh[1]))
        self.assertEqual(migrated[0], fresh[0])
        self.assertIn(("tool_approvals_resume_token", 1, "c", 0), fresh[1])

    def test_the_token_is_unique(self):
        self._use_store("resume-token-unique.db")
        cp.store._init_db()
        insert = ("INSERT INTO tool_approvals(id, ts, server, tool, args_json, "
                  "args_digest, status, deadline, resume_token) VALUES "
                  "(?, 0, 's', 't', '{}', 'd', 'pending', 0, ?)")
        with cp.store._connect() as conn:
            conn.execute(insert, ("one", "ab" * 16))
            conn.execute(insert, ("two", None))
            conn.execute(insert, ("three", None))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(insert, ("four", "ab" * 16))

    def test_v13_skips_a_store_without_the_table(self):
        self._use_store("resume-token-no-table.db")
        with cp.store._connect() as conn:
            for ddl in MigrationTests._OLD_SCHEMA:
                conn.execute(ddl)
            cp.store._step_13_resume_token(conn)
            self.assertEqual(cp.store._columns(conn, "tool_approvals"), set())


class ResultFactsMigrationTests(_FreshStoreTestCase):
    """v14, which gives ``audit`` what a tool call returned: ``result_bytes``,
    ``result_sha256`` and ``duration``. Additive; every existing row keeps NULL."""

    def _v13_store(self, name):
        self._use_store(name)
        cp.store._init_db()
        # The table as v13's DDL made it: today's, cut after `status`. Rebuilt rather
        # than DROP COLUMN, which cannot edit a definition holding comments.
        with cp.store._connect() as conn:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='audit'"
                               ).fetchone()[0]
            v13 = re.sub(r"status(\s+)TEXT,.*\)\s*$", r"status\1TEXT\n            )",
                         ddl, flags=re.S)
            self.assertNotIn("result_bytes", v13)
            conn.execute("DROP TABLE audit")
            conn.execute(v13)
            conn.execute("INSERT INTO audit(ts, kind, stage, server, tool, status) "
                         "VALUES (1.0, 'outcome', 'tool-result', 'mcp-github', "
                         "'get_pr', 'ok')")
            conn.execute("INSERT INTO audit(ts, kind, host) "
                         "VALUES (2.0, 'allow', 'a.example')")
            conn.execute("PRAGMA user_version = 13")
            conn.commit()

    @staticmethod
    def _audit_columns():
        with cp.store._connect() as conn:
            return [tuple(r) for r in conn.execute("PRAGMA table_info(audit)")]

    def test_v14_is_the_current_step(self):
        self.assertEqual(cp.store.SCHEMA_VERSION, 14)
        self.assertEqual(cp.store._STEPS[-1][:2],
                         (14, "tool result size, hash and duration"))

    def test_the_columns_are_added_as_null_and_the_rows_kept(self):
        self._v13_store("result-facts-migrate.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT ts, kind, status, result_bytes, result_sha256, duration "
                "FROM audit ORDER BY ts")]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 14)
        self.assertEqual(rows, [
            {"ts": 1.0, "kind": "outcome", "status": "ok", "result_bytes": None,
             "result_sha256": None, "duration": None},
            {"ts": 2.0, "kind": "allow", "status": None, "result_bytes": None,
             "result_sha256": None, "duration": None}])

    def test_a_migrated_and_a_fresh_table_agree(self):
        self._v13_store("result-facts-agree-migrated.db")
        cp.store._init_db()
        migrated = self._audit_columns()
        self._use_store("result-facts-agree-fresh.db")
        cp.store._init_db()
        self.assertEqual(migrated, self._audit_columns())
        self.assertEqual([(c[1], c[2]) for c in migrated[-3:]],
                         [("result_bytes", "INTEGER"), ("result_sha256", "TEXT"),
                          ("duration", "REAL")])

    def test_v14_leaves_a_store_that_has_the_columns_alone(self):
        self._use_store("result-facts-again.db")
        cp.store._init_db()
        with cp.store._connect() as conn:
            cp.store._step_14_result_facts(conn)
        self.assertEqual([c[1] for c in self._audit_columns()].count("duration"), 1)


class LegacyClassNameTests(unittest.TestCase):
    """``store.LEGACY_CLIENT_CLASS`` and the class names in ``policy`` are two
    spellings of one thing, and nothing in the code ties them: ``store`` is the bottom
    of the dependency order and must not import ``policy``. If they drift, every rule
    a migration wrote becomes dead — matching a class no client is ever placed in —
    and the symptom is that the agent is suddenly held for hosts it has always
    reached. So the suite is the tie."""

    def test_the_backfill_class_is_one_the_default_map_can_produce(self):
        names = {name for name, _ in
                 cp.policy._parse_client_classes(cp.policy.CLIENT_CLASSES_DEFAULT)}
        self.assertIn(cp.store.LEGACY_CLIENT_CLASS, names)

    def test_the_backfill_class_is_the_one_the_sandbox_lands_in(self):
        # Stronger than membership: it must be the class the AGENT is placed in,
        # since that is whose approvals the backfilled rules were.
        self.assertEqual(cp.policy._client_class(CLASS_IP),
                         cp.store.LEGACY_CLIENT_CLASS)


class SeedTests(_CPTestCase):
    def test_seed_loads_lowercased_allow_rules_and_is_idempotent(self):
        seed = os.path.join(_TMP, "seed.txt")
        with open(seed, "w") as f:
            f.write("# a comment\n\n Example.COM \n.pypi.org\n")
        saved = cp.store.SEED_PATH
        cp.store.SEED_PATH = seed
        try:
            n = cp.store._seed_if_empty()
            self.assertEqual(n, 2)
            with cp.store._connect() as conn:
                rows = {(r["pattern"], r["action"], r["source"]) for r in
                        conn.execute("SELECT pattern, action, source FROM rules")}
            self.assertEqual(rows, {("example.com", "allow", "seed"),
                                    (".pypi.org", "allow", "seed")})
            # Idempotent: a second call is a no-op once rules exist.
            self.assertEqual(cp.store._seed_if_empty(), 0)
        finally:
            cp.store.SEED_PATH = saved


def _routes(fastapi_app) -> set:
    """(method, path) pairs the stub recorded for one app — see tests/_loader.py.
    Defaulting to empty rather than raising would let a stub regression read as
    'no dangerous routes', so an app that recorded nothing is an error here."""
    recorded = getattr(fastapi_app, "routes", None)
    if not recorded:
        raise AssertionError(
            "the FastAPI stub recorded no routes — route recording is broken, so "
            "the API-surface split below is not actually being asserted")
    return set(recorded)


class ApiSurfaceSplitTests(unittest.TestCase):
    """The control plane serves three listeners, and WHICH one a handler lands on is
    a security property rather than a layout choice.

    Each enforcer has a network route to its own bridge and none to the management
    listener or to the other enforcer's. That topology is in docker-compose.yml, but
    it is only worth anything if the listeners an enforcer CAN reach stay harmless —
    so each roster below is asserted exactly, and adding to one has to be a deliberate
    act rather than the side effect of putting a new endpoint next to an existing one.

    The stakes are asymmetric: a management route missing from its app is a broken
    UI, noticed in seconds. A management route that also appears on an enforcer's
    app is a self-approval path, noticed never."""

    #: Everything the proxy-facing listener may serve. /authorize answers a policy
    #: question; /healthz is what the container's health gate probes.
    AUTHORIZE_ROUTES: ClassVar[set] = {("POST", "/authorize"),
                                       ("GET", "/healthz")}
    #: Everything the gateway-facing listener may serve: decide a call, read the
    #: roster, claim an ask a human approved, withdraw one the agent no longer wants,
    #: report what the servers expose. Five rather than one, and the criterion that
    #: keeps that width honest is unchanged — none of them GRANTS. The claim releases
    #: only what was already decided elsewhere, a withdrawal only retires an ask, and
    #: the inventory records a server's claim about itself, in memory, that
    #: `_decide_tool` never reads. A tool that arrives on it is denied exactly as it
    #: was before, until a human writes a rule naming it.
    TOOL_ROUTES: ClassVar[set] = {("POST", "/tool/authorize"),
                                  ("GET", "/tool/roster"),
                                  ("POST", "/tool/asks/{resume_token}/claim"),
                                  ("POST", "/tool/asks/{resume_token}/withdraw"),
                                  ("POST", "/tool/inventory"),
                                  ("GET", "/healthz")}

    def test_the_authorize_listener_serves_exactly_two_routes(self):
        self.assertEqual(_routes(cp.authorize_app), self.AUTHORIZE_ROUTES)

    def test_the_tool_listener_serves_exactly_its_five_routes(self):
        self.assertEqual(_routes(cp.tool_app), self.TOOL_ROUTES)

    def test_the_two_enforcer_bridges_share_nothing_but_healthz(self):
        # Separate nets AND separate sockets: a bypassed egress proxy must not reach
        # the gateway's claim endpoint, which is the one place this bridge releases a
        # side effect.
        shared = _routes(cp.authorize_app) & _routes(cp.tool_app)
        self.assertEqual(shared, {("GET", "/healthz")})

    def test_nothing_but_healthz_is_served_on_both(self):
        shared = _routes(cp.app) & _routes(cp.authorize_app)
        self.assertEqual(shared, {("GET", "/healthz")})

    def test_the_management_app_shares_nothing_with_the_tool_bridge(self):
        shared = _routes(cp.app) & _routes(cp.tool_app)
        self.assertEqual(shared, {("GET", "/healthz")})

    def test_the_endpoint_that_grants_egress_is_management_only(self):
        # resolve is THE privileged action — it is what turns a held request into
        # allowed egress, and a tool ask into an approved call. If either enforcer
        # can reach this, the governance plane is a formality, so it gets its own
        # assertion rather than relying on the roster tests above to catch it by
        # arithmetic.
        resolve = [r for r in _routes(cp.app) if r[1].endswith("/resolve")]
        self.assertEqual(len(resolve), 1, "resolve is not on the management app")
        self.assertNotIn(resolve[0], _routes(cp.authorize_app))
        self.assertNotIn(resolve[0], _routes(cp.tool_app))

    def test_the_endpoints_that_write_standing_policy_are_management_only(self):
        # The other way to grant egress, and the stronger one: a rule written here
        # decides future requests with no hold and no click, where `resolve` can only
        # answer a question something already asked. Asserted as its own roster rather
        # than by arithmetic, for the same reason as the test above — and by SUFFIX, so
        # a third mutation added later has to be named here or fail this.
        writes = {r for r in _routes(cp.app)
                  if r[0] == "POST" and "/api/egress/rules" in r[1]}
        self.assertEqual(writes, {("POST", "/api/egress/rules"),
                                  ("POST", "/api/egress/rules/{rule_id}/revoke"),
                                  ("POST", "/api/egress/rules/{rule_id}/edit")})
        self.assertEqual(writes & _routes(cp.authorize_app), set())
        self.assertEqual(writes & _routes(cp.tool_app), set())

    def test_the_endpoints_that_write_tool_policy_are_management_only(self):
        # The gateway's surface, held to the same roster as the egress one. The
        # gateway READS policy over a bridge of its own — a narrow one that cannot
        # grant — and these are the writes that must never appear on it, nor on the
        # authorize listener the egress proxy can already reach.
        writes = {r for r in _routes(cp.app)
                  if r[0] == "POST" and "/api/mcp/" in r[1]}
        self.assertEqual(writes, {("POST", "/api/mcp/servers"),
                                  ("POST", "/api/mcp/servers/{server}/edit"),
                                  ("POST", "/api/mcp/servers/{server}/revoke"),
                                  ("POST", "/api/mcp/rules"),
                                  ("POST", "/api/mcp/rules/{rule_id}/edit"),
                                  ("POST", "/api/mcp/rules/{rule_id}/revoke"),
                                  ("POST", "/api/mcp/pins/{pin_id}/revoke")})
        self.assertEqual(writes & _routes(cp.authorize_app), set())
        self.assertEqual(writes & _routes(cp.tool_app), set())

    def test_the_views_that_read_the_store_are_management_only(self):
        # Not privileged, but they carry the record: pending hosts and clients,
        # the audit history, the standing policy. A bypassed relay guard must not
        # be able to read them either.
        for path in ("/approvals", "/approvals/stream", "/api/audit",
                     "/api/audit/events", "/api/egress/rules", "/api/mcp/servers",
                     "/api/mcp/rules", "/api/mcp/pins", "/api/config", "/status"):
            self.assertIn(("GET", path), _routes(cp.app), path)
            self.assertNotIn(("GET", path), _routes(cp.authorize_app), path)
            # Nor on the gateway's bridge. It reads policy — that is what the roster
            # is — but the queue, the audit record and the standing rules are the
            # operator's view, and one of them is a read on the operator's own
            # pending decisions.
            self.assertNotIn(("GET", path), _routes(cp.tool_app), path)


class ListenerSeparationTests(unittest.TestCase):
    """``_assert_listeners_separated`` — the startup fail-closed for a config that
    silently undoes the split. A wildcard management bind serves `resolve` on
    authorize-net while every healthcheck and every page in the UI keeps working,
    so nothing downstream can detect it."""

    # S104 (bind-all-interfaces) is suppressed throughout this class rather than
    # avoided: the wildcard IS the subject here. The default below mirrors the real
    # authorize listener, which binds the wildcard on purpose, and the literals in
    # the test are the spellings the guard has to reject.
    def _check(self, *, manage_bind, manage_port=8090,
               authorize_bind="0.0.0.0", authorize_port=8091,  # noqa: S104
               tool_bind="172.27.0.2", tool_port=8092):
        with mock.patch.multiple(cp, MANAGE_BIND=manage_bind,
                                 MANAGE_PORT=manage_port,
                                 AUTHORIZE_BIND=authorize_bind,
                                 AUTHORIZE_PORT=authorize_port,
                                 TOOL_BIND=tool_bind,
                                 TOOL_PORT=tool_port):
            cp._assert_listeners_separated()

    def test_a_pinned_management_address_is_accepted(self):
        self._check(manage_bind="172.31.0.2")

    def test_a_wildcard_tool_bind_is_refused(self):
        # The gateway's bridge carries the claim endpoint, which releases an approved
        # call. On the wildcard it would answer on authorize-net too, where the
        # egress proxy could spend an approval the agent is coming back for — the
        # lateral edge between two enforcers that the second bridge exists to prevent.
        for spelling in ("0.0.0.0", "::", "*", ""):  # noqa: S104
            with self.assertRaises(SystemExit, msg=spelling) as caught:
                self._check(manage_bind="172.31.0.2", tool_bind=spelling)
            self.assertIn("CONTROL_TOOL_BIND", str(caught.exception), spelling)

    def test_a_tool_bind_on_another_enforcers_network_is_refused(self):
        # The spelling a wildcard check misses, and the whole reason the assertion
        # lives in the app: naming authorize-net's address outright puts the claim
        # endpoint within the egress proxy's reach with every healthcheck green.
        # control-net is refused for the neighbouring reason — one bridge per
        # enforcer, and neither belongs on the other's leg.
        for address in ("172.29.0.2", "172.31.0.2"):
            with self.assertRaises(SystemExit, msg=address) as caught:
                self._check(manage_bind="172.31.0.2", tool_bind=address)
            self.assertIn("CONTROL_TOOL_BIND", str(caught.exception), address)

    def test_a_management_bind_on_an_enforcers_network_is_refused(self):
        # The management listener is the one that GRANTS, and it had only the
        # wildcard check: CONTROL_MANAGE_BIND=172.29.0.2 — this container's own
        # authorize-net address — started cleanly and served `resolve` to the egress
        # proxy. Both enforcers' networks are refused; control-net is the address it
        # is required to bind, so that one passes (asserted by the first test above).
        for address in ("172.29.0.2", "172.27.0.2"):
            with self.assertRaises(SystemExit, msg=address) as caught:
                self._check(manage_bind=address)
            self.assertIn("CONTROL_MANAGE_BIND", str(caught.exception), address)
            self.assertIn("resolve", str(caught.exception), address)

    def test_the_management_forbidden_list_is_a_typo_away_from_nothing_and_says_so(self):
        with mock.patch.object(cp, "_MANAGE_BIND_FORBIDDEN",
                               "172.29.0.0/24,not-a-cidr"), \
                self.assertRaises(SystemExit) as caught:
            self._check(manage_bind="172.31.0.2")
        self.assertIn("CONTROL_MANAGE_BIND_FORBIDDEN", str(caught.exception))

    def test_the_forbidden_list_is_a_typo_away_from_nothing_and_says_so(self):
        # Fatal rather than tolerant: this list has two members, and dropping either
        # silently removes the guard.
        with mock.patch.object(cp, "_TOOL_BIND_FORBIDDEN",
                               "172.29.0.0/24,not-a-cidr"), \
                self.assertRaises(SystemExit) as caught:
            self._check(manage_bind="172.31.0.2")
        self.assertIn("CONTROL_TOOL_BIND_FORBIDDEN", str(caught.exception))

    def test_a_hostname_bind_is_not_read_as_inside_a_forbidden_net(self):
        # Resolving a name here would make the guard depend on DNS — the class of
        # check the relay guard exists because it cannot trust. A name is simply not
        # an address, so it falls through to the other two refusals.
        self._check(manage_bind="172.31.0.2", tool_bind="control-plane")

    def test_the_tool_listener_may_not_share_a_port_with_either_other(self):
        # Same port on different addresses is refused as well as the same socket:
        # with a wildcard in the mix — and the authorize listener is one — which app
        # answers depends on which bind is more specific for the address dialled,
        # which is not a property an operator reading a firewall rule can see.
        for port in (8090, 8091):
            with self.assertRaises(SystemExit, msg=str(port)):
                self._check(manage_bind="172.31.0.2", tool_port=port)

    def test_every_wildcard_spelling_is_refused(self):
        # All four reach every interface, authorize-net included. Listing them
        # explicitly beats a substring test, which would also reject a legitimate
        # address that merely contains one of these.
        for spelling in ("0.0.0.0", "::", "*", ""):  # noqa: S104
            with self.assertRaises(SystemExit, msg=spelling) as caught:
                self._check(manage_bind=spelling)
            self.assertIn("CONTROL_MANAGE_BIND", str(caught.exception), spelling)

    def test_the_two_listeners_may_not_be_the_same_socket(self):
        # Same address AND same port means one listener serving both apps' worth
        # of surface to whoever can reach it.
        with self.assertRaises(SystemExit):
            self._check(manage_bind="172.31.0.2", manage_port=9000,
                        authorize_bind="172.31.0.2", authorize_port=9000)

    def test_the_same_address_on_different_ports_is_fine(self):
        # This is a single-network deployment, not a wildcard: the split still
        # holds because the ports differ and only one is routed to the proxy.
        self._check(manage_bind="172.31.0.2", manage_port=8090,
                    authorize_bind="172.31.0.2", authorize_port=8091)


class ActorHeaderAgreementTests(unittest.TestCase):
    def test_actor_header_agrees_with_the_relay(self):
        """The backend reads ``ACTOR_HEADER`` as the relay's assertion about the
        browser; the relay (``control-plane-ui/app.py`` — a different image, no shared
        module) strips any client-supplied copy and re-adds it with the peer address. If
        the two names drift, provenance silently loses ``via-ui=`` AND the relay's
        spoof-strip stops covering the header the backend trusts, so a caller could
        self-report an actor the audit records as relay-asserted. Read the relay's
        SOURCE, the same arrangement as the fail-closed-marker test above."""
        ui_src = (ROOT / "control-plane-ui" / "app.py").read_text()
        m = re.search(r'^ACTOR_HEADER\s*=\s*"([^"]+)"', ui_src, re.MULTILINE)
        self.assertIsNotNone(m, "ACTOR_HEADER not found in control-plane-ui/app.py")
        self.assertEqual(m.group(1), cp.provenance.ACTOR_HEADER)


class BodyCapTests(unittest.TestCase):
    """The enforcer-facing listeners refuse an oversized body before FastAPI reads it.

    Both peers relay what the sandbox sent, and the request model materializes the
    whole body before any handler runs — so the cap has to sit in front of the app,
    as middleware, and decide from the header. Both peers are stdlib urllib, which
    always sends Content-Length and never chunks, so a POST without one is refused as
    a peer this listener does not know rather than read to find out its size."""

    class _Request:
        def __init__(self, method="POST", headers=None):
            self.method = method
            self._headers = {k.lower(): v for k, v in (headers or {}).items()}
            self.headers = types.SimpleNamespace(get=self._headers.get)

    @staticmethod
    async def _reached(_request):
        return "REACHED-THE-APP"

    def _run(self, cap, **kw):
        return asyncio.run(cp._body_cap(cap)(self._Request(**kw), self._reached))

    def test_a_body_within_the_cap_reaches_the_app(self):
        self.assertEqual(self._run(100, headers={"content-length": "100"}),
                         "REACHED-THE-APP")

    def test_a_body_over_the_cap_is_refused_with_413(self):
        resp = self._run(100, headers={"content-length": "101"})
        self.assertEqual(resp.status_code, 413)

    def test_a_post_without_a_length_is_refused_with_411(self):
        for headers in ({}, {"content-length": "abc"}, {"content-length": "-1"}):
            with self.subTest(headers=headers):
                self.assertEqual(self._run(100, headers=headers).status_code, 411)

    def test_a_get_carries_no_body_and_passes(self):
        # The roster is fetched by GET, with no Content-Length at all.
        self.assertEqual(self._run(100, method="GET"), "REACHED-THE-APP")

    def test_the_caps_are_sized_to_their_peers(self):
        # An egress question is a host, a port and a URL; the tool bridge carries a
        # tool call's complete arguments behind the gateway's own megabyte cap.
        self.assertEqual(cp.AUTHORIZE_BODY_MAX, 64 * 1024)
        self.assertEqual(cp.TOOL_BODY_MAX, 2 * 1024 * 1024)
        self.assertGreater(cp.TOOL_BODY_MAX, 1024 * 1024,
                           "the tool bridge must accept what the gateway lets through")


if __name__ == "__main__":
    unittest.main()
