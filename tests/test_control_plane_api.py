# SPDX-License-Identifier: Apache-2.0
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

import json
import os
import re
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

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
    cp._PENDING_WAITERS.clear()
    cp._PENDING_DEADLINE.clear()
    cp._GROUPS.clear()
    # Module state like those above, and reset for the same reason: it is
    # process-lifetime by design, which across tests means it leaks.
    cp._SATURATION.update(count=0, last_ts=None, last_scope=None, last_host=None,
                          acked=0, acked_ts=None)


def _auth_req(host, **kw):
    return cp.AuthorizeRequest(host=host, **kw)


class _FakeRequest:
    """Stand-in for the Starlette Request that ``resolve`` reads provenance from
    (``_actor``). Headers are lowercased like Starlette's case-insensitive mapping."""

    def __init__(self, peer="172.31.0.3", headers=None):
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.headers = types.SimpleNamespace(get=self._headers.get)
        self.client = types.SimpleNamespace(host=peer) if peer else None


def _resolve(approval_id, action, request=None, **fields):
    """resolve() with a default request, so tests that don't care about provenance
    stay readable. Extra kwargs go on the ResolveRequest (e.g. ``pattern=``)."""
    return cp.resolve(approval_id, cp.ResolveRequest(action=action, **fields),
                      request if request is not None else _FakeRequest())


def _hold(host, approval_id="hold-1", client=None):
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
    (which is what a real duplicate does) pass the same client explicitly."""
    with cp._connect() as conn:
        conn.execute(
            "INSERT INTO approvals(id, ts, host, status) VALUES (?,?,?, 'pending')",
            (approval_id, time.time(), host))
        conn.commit()
    cp._reserve_hold(approval_id, threading.Event(),
                     approval_id if client is None else client, host)
    return approval_id


def _rules():
    with cp._connect() as conn:
        return {(r["pattern"], r["action"]) for r in
                conn.execute("SELECT pattern, action FROM rules")}


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
            resolve_resp = _resolve(approval_id, "allow_persist")
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
            _resolve(approval_id, "deny_once")
            t.join(2)
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertFalse(t.is_alive())
        self.assertEqual(result["resp"].decision, "deny")
        # deny_once must NOT persist a rule — the host stays held next time.
        self.assertEqual(cp._decide("nope.com")[0], "hold")


class DuplicateHoldTests(_CPTestCase):
    """Grouping through the real ``authorize`` path, which is the only place its
    concurrency shows: N blocked workers, one card, one decision, N audit lines."""

    def _authorize_many(self, n, host, client="agent-1", urls=None):
        """Start n concurrent authorize() calls for the same request. Returns the
        threads and a results list that fills in as each returns."""
        results = [None] * n

        def worker(i):
            results[i] = cp.authorize(_auth_req(
                host, client=client, url=(urls[i] if urls else None)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        return threads, results

    def _await_card(self, requests):
        """Wait for exactly one pending card carrying ``requests`` blocked waiters."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = cp._list_pending()
            if len(pending) == 1 and pending[0]["requests"] == requests:
                return pending[0]
            time.sleep(0.01)
        raise AssertionError(
            f"never saw one card with {requests} waiters; saw {cp._list_pending()}")

    def test_four_retries_raise_one_card_that_says_it_is_four_requests(self):
        # The motivating case. Before grouping this was four cards, and with the
        # per-client cap at 4 the fifth retry was refused with no card at all.
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 10
        try:
            threads, results = self._authorize_many(4, "dup.example")
            card = self._await_card(4)
            # The count is ON THE CARD because grouping changed what one click means.
            self.assertEqual(card["requests"], 4)
            _resolve(card["id"], "allow_once")
            for t in threads:
                t.join(5)
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertEqual([r.decision for r in results], ["allow"] * 4)
        with cp._connect() as conn:
            rows = conn.execute("SELECT id FROM approvals").fetchall()
        self.assertEqual(len(rows), 1, "duplicates must not each get a durable row")
        # "once" still means no standing rule — it now means four requests, not one.
        self.assertEqual(_rules(), set())
        self.assertEqual(cp._decide("dup.example")[0], "hold")

    def test_a_timeout_tells_every_waiter_it_was_a_timeout(self):
        # Only ONE waiter wins the expiry UPDATE; the rest read the row. Reporting
        # from `did I win the UPDATE` told the losers a human had rejected them —
        # an audit trail inventing a human decision for a timeout is worse than none.
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 0.3
        try:
            threads, results = self._authorize_many(3, "slow.example")
            for t in threads:
                t.join(5)
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertEqual([r.decision for r in results], ["deny"] * 3)
        for r in results:
            self.assertIn("timeout", r.reason)
            self.assertNotIn("rejection", r.reason)
        with cp._connect() as conn:
            statuses = [r[0] for r in conn.execute(
                "SELECT status FROM approvals").fetchall()]
        self.assertEqual(statuses, ["expired"])

    def test_every_joined_request_is_audited_on_its_own_terms(self):
        # Grouping is a concept of the screen and the worker pool, never of the
        # record: each request keeps its own audit line and its own url, and the
        # joiners' reason names the card, so the log explains why four requests
        # produced one approval without anyone having to know about grouping.
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 10
        urls = ["https://dup.example/a", "https://dup.example/b",
                "https://dup.example/c"]
        try:
            threads, results = self._authorize_many(3, "dup.example", urls=urls)
            card = self._await_card(3)
            _resolve(card["id"], "deny_once")
            for t in threads:
                t.join(5)
        finally:
            cp.HOLD_TIMEOUT = saved
        calls = [c for c in cp._audit.call_args_list if c.args[0] == "hold"]
        self.assertEqual(sorted(c.kwargs["url"] for c in calls), sorted(urls))
        reasons = [c.kwargs["reason"] for c in calls]
        self.assertEqual(sum(r == "held for approval" for r in reasons), 1)
        joined = [r for r in reasons if r.startswith("joined hold ")]
        self.assertEqual(len(joined), 2)
        for r in joined:
            self.assertIn(card["id"], r)
        # And a terminal line per request, not per card.
        self.assertEqual(
            sum(1 for c in cp._audit.call_args_list if c.args[0] == "deny"), 3)
        self.assertEqual([r.decision for r in results], ["deny"] * 3)

    def test_a_decided_card_stops_accepting_joiners_at_the_click(self):
        # The window closes with the DECISION, not when the last woken worker happens
        # to return. Asserted while the waiters are still registered, because that gap
        # is the whole risk: a retry landing in it would attach to an already-resolved
        # card and inherit an outcome nobody was shown it beside.
        approval = _hold("clicked.example", "held-1", client="agent-1")
        before = cp._reserve_hold("dup", threading.Event(), "agent-1",
                                  "clicked.example")
        self.assertTrue(before.joined, "joinable while pending")
        _resolve(approval, "deny_once")
        self.assertIn(approval, cp._PENDING_EVENTS, "waiters not drained yet")
        after = cp._reserve_hold("late", threading.Event(), "agent-1",
                                 "clicked.example")
        self.assertFalse(after.joined)
        self.assertEqual(after.approval_id, "late")

    def test_an_expired_card_stops_accepting_joiners_before_its_waiters_drain(self):
        # Same invariant on the other terminal path. A phantom waiter (registered, with
        # no thread that will ever release it) keeps the card in the registry after the
        # real waiter returns, so the group can only have been closed by the EXPIRY
        # path — not incidentally by the last release.
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 0.3
        try:
            threads, _ = self._authorize_many(1, "drain.example")
            card = self._await_card(1)
            phantom = cp._reserve_hold("phantom", threading.Event(), "agent-1",
                                       "drain.example")
            self.assertTrue(phantom.joined)
            for t in threads:
                t.join(5)
            self.assertIn(card["id"], cp._PENDING_EVENTS)
            late = cp._reserve_hold("late", threading.Event(), "agent-1",
                                    "drain.example")
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertFalse(late.joined)

    def test_a_duplicate_arriving_after_the_decision_opens_a_new_card(self):
        # The joining window closes with the decision, so a retry that lands after
        # the click is held again rather than inheriting an outcome nobody was shown
        # it beside.
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 10
        try:
            threads, _ = self._authorize_many(2, "again.example")
            card = self._await_card(2)
            _resolve(card["id"], "deny_once")
            for t in threads:
                t.join(5)
            cp.HOLD_TIMEOUT = 0.2
            late = cp.authorize(_auth_req("again.example", client="agent-1"))
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertEqual(late.decision, "deny")
        self.assertIn("timeout", late.reason)   # held afresh, then defaulted
        with cp._connect() as conn:
            rows = conn.execute("SELECT status FROM approvals ORDER BY ts").fetchall()
        self.assertEqual([r[0] for r in rows], ["denied", "expired"])


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
        # than guess.
        self.assertEqual(body["conflict"],
                         {"pattern": ".example.com", "action": "allow"})
        # And policy is untouched — no half-application.
        self.assertEqual(_rules(), {(".example.com", "allow")})

    def test_the_refused_approval_stays_pending_and_decidable(self):
        # Same reasoning as a rejected pattern: a persist that consumed the approval
        # without writing the rule would be the worst of both. The operator must get
        # to choose again.
        _set_rules([(".example.com", "allow")])
        approval = _hold("b.example.com")
        _resolve(approval, "deny_persist", pattern=".example.com")
        self.assertEqual([p["id"] for p in cp._list_pending()], [approval])
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
                   for o in cp._list_pending()[0]["persist_options"]}
        self.assertEqual(options["a.b.example.com"], None)
        self.assertEqual(options[".example.com"], "allow")


def _served(**kw):
    """The grouped rows ``/api/audit`` serves.

    The endpoint returns ``{"rows": [...], "total": n}`` — the total being what the
    view is a window onto. These tests are about the rows unless they say otherwise,
    so the unwrapping lives here rather than in forty assertions."""
    return cp.api_audit(**kw)["rows"]


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
        with cp._connect() as conn:
            conn.execute("DELETE FROM audit")
            for r in rows:
                conn.execute(
                    "INSERT INTO audit(ts, decision, stage, host, port, proto, "
                    "client, method, url, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (r.get("ts", 0.0), r.get("decision", "allow"), r.get("stage"),
                     r.get("host"), r.get("port"), r.get("proto"), r.get("client"),
                     r.get("method"), r.get("url"), r.get("reason")))
            conn.commit()

    def test_a_denial_says_whether_it_was_policy_or_an_outage(self):
        # The two are visually identical in the UI — same red `deny` tag, same host —
        # and mean opposite things: one is governance working, the other governance
        # absent with everything being refused. An evening was spent on that
        # ambiguity, hence the flag.
        self._rows(
            {"host": "a.example", "decision": "deny",
             "reason": "blocked by rule (a.example)"},
            {"host": "b.example", "decision": "deny", "ts": 1.0,
             "reason": "control-plane unreachable, fail-closed (timed out)"})
        got = {r["host"]: r["fail_closed"] for r in _served()}
        self.assertEqual(got, {"a.example": False, "b.example": True})

    def test_the_marker_matches_what_the_proxy_actually_writes(self):
        """The reason text is produced in proxies/egress/addon.py and classified in
        control-plane/app.py — two services, two images, no shared module. Nothing but
        this test connects them, and the drift is silent: a renamed reason simply stops
        being recognised and the row reverts to looking like a policy denial.

        Reads the addon's SOURCE rather than importing it, because the point is to pin
        the literal that ships in the other image."""
        addon = (ROOT / "proxies" / "egress" / "addon.py").read_text()
        produced = re.findall(r'Verdict\(False,\s*f?"([^"{]*)', addon)
        self.assertTrue(
            any(p.startswith(cp.FAIL_CLOSED_REASON) for p in produced),
            f"no fail-closed Verdict reason in addon.py starts with "
            f"{cp.FAIL_CLOSED_REASON!r}; found {produced!r}. If the wording moved, "
            f"move FAIL_CLOSED_REASON with it — otherwise outage denials go back to "
            f"being indistinguishable from policy denials in the UI.")

    def test_an_unrecognised_reason_is_not_marked_as_an_outage(self):
        # The classification must not fire on a word that appears in ordinary reasons
        # too — a host can legitimately be named after the control plane.
        self._rows({"host": "c.example", "decision": "deny",
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
        self._rows({"host": "d.example", "decision": "deny",
                    "reason": "blocked by rule, recorded while "
                              "control-plane unreachable"})
        self.assertFalse(_served()[0]["fail_closed"])

    def test_a_row_says_whose_request_it_was(self):
        # The gap this closes. One control plane serves every sandbox, so a row that
        # records "egress to pypi.org was allowed" without saying which agent asked
        # is not answering the question an audit trail exists for.
        self._rows({"host": "pypi.org", "client": "172.30.0.7", "decision": "allow"})
        self.assertEqual(_served()[0]["client"], "172.30.0.7")

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
            {"ts", "decision", "stage", "host", "client", "reason",
             "n", "first_ts", "fail_closed"})

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
        self.assertEqual(cp.api_audit()["total"], 0)

    def test_the_total_counts_decisions_not_rows(self):
        """The list is a window and used to say so nowhere. Grouping made that worse
        rather than better: forty rows carrying counts read like a complete picture,
        because the counts appear to explain the volume away.

        The total is RAW decisions, so it can be compared against the sum of the
        counts on screen — a total of groups would be a second number that agrees
        with the first and tells the reader nothing."""
        self._rows(*[{"host": "chatty.example", "decision": "deny",
                      "ts": 1000.0 + i} for i in range(30)])
        served = cp.api_audit()
        self.assertEqual(len(served["rows"]), 1)     # one group
        self.assertEqual(served["rows"][0]["n"], 30)
        self.assertEqual(served["total"], 30)        # thirty decisions

    def test_the_total_is_not_bounded_by_the_display_limit(self):
        # The limit shapes what is SHOWN; the total exists precisely to say how much
        # was not. A total that moved with the limit would always equal the rows and
        # could never report truncation.
        self._rows(*[{"host": f"h{i}.example", "ts": float(i)} for i in range(12)])
        self.assertEqual(len(_served(limit=3)), 3)
        self.assertEqual(cp.api_audit(limit=3)["total"], 12)

    def test_identical_decisions_collapse_to_one_row(self):
        """The case that forced this: a client retrying a host that a standing rule
        refuses, once a minute, forever. Ungrouped it writes 1440 rows a day and the
        forty-row list covers under an hour — so a fronting refusal from this morning
        is already off the bottom."""
        self._rows(*[{"host": "chatty.example", "decision": "deny", "stage": "connect",
                      "client": "172.30.0.2", "reason": "blocked by rule",
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
            rows.append({"host": "chatty.example", "decision": "deny", "ts": 100.0 + 2 * i})
            rows.append({"host": "api.example", "decision": "allow", "ts": 101.0 + 2 * i})
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
        base = {"host": "a.example", "decision": "deny", "stage": "connect",
                "client": "172.30.0.2", "reason": "blocked by rule"}
        for field, other in (("decision", "allow"), ("stage", "sni"),
                             ("host", "b.example"), ("client", "172.30.0.9"),
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
        self._rows({"host": "a.example", "client": "172.30.0.2"},
                   {"host": "a.example", "client": "172.30.0.3"})
        self.assertEqual({r["client"] for r in _served()},
                         {"172.30.0.2", "172.30.0.3"})

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
        cp.AUDIT_GROUP_SCAN = 10
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
            cp.AUDIT_GROUP_SCAN = 5000

    def test_rows_older_than_the_scan_are_kept_but_not_shown(self):
        # Cost has to be fixed as the table grows, since this is polled every few
        # seconds against a table that only ever gets longer. The trade is stated
        # rather than hidden: the record keeps everything, this view does not.
        cp.AUDIT_GROUP_SCAN = 5
        try:
            self._rows(*[{"host": f"h{i}.example", "ts": float(i)}
                         for i in range(20)])
            self.assertEqual([r["host"] for r in _served()],
                             [f"h{i}.example" for i in range(19, 14, -1)])
            with cp._connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 20)
        finally:
            cp.AUDIT_GROUP_SCAN = 5000


class PersistCandidateTests(unittest.TestCase):
    """``_persist_candidates`` is what turns the persisted pattern from an
    AGENT-CONTROLLED STRING into an operator choice from a bounded set.

    The old ``resolve`` stored the requested host verbatim, and a leading dot is a
    subdomain wildcard — so a host spelled ``.example.com`` persisted a rule covering
    every subdomain of example.com, permanently (nothing revokes a rule)."""

    def test_the_narrowest_choice_is_first_because_it_is_the_default(self):
        self.assertEqual(
            cp._persist_candidates("a.b.example.com"),
            ["a.b.example.com", ".a.b.example.com", ".example.com"])

    def test_a_two_label_host_offers_only_itself_and_its_subtree(self):
        self.assertEqual(cp._persist_candidates("example.com"),
                         ["example.com", ".example.com"])

    def test_no_candidate_is_ever_a_single_label_wildcard(self):
        # `.com` as a standing allow rule would end governance for the whole TLD, and
        # `.localhost` is the same mistake one label down.
        for host in ("example.com", "a.b.example.com", "localhost", "example.co.uk"):
            for pattern in cp._persist_candidates(host):
                labels = pattern.lstrip(".").split(".")
                if pattern.startswith("."):
                    self.assertGreaterEqual(
                        len(labels), 2, f"{host} offers one-label wildcard {pattern}")
        # A bare name has no subtree to offer at all.
        self.assertEqual(cp._persist_candidates("localhost"), ["localhost"])

    def test_an_ip_literal_gets_no_wildcard(self):
        for host in ("1.2.3.4", "::1", "[::1]"):
            self.assertEqual(
                len(cp._persist_candidates(host)), 1,
                f"{host} has no subdomains — `.1.2.3.4` would be a nonsense rule")

    def test_a_host_the_agent_spelled_as_a_wildcard_cannot_stay_one(self):
        # THE case this function exists for: the leading dot is normalized away, so the
        # exact-host default is an exact host and the wildcard is only ever reachable by
        # an operator picking it.
        self.assertEqual(cp._persist_candidates(".example.com")[0], "example.com")
        self.assertEqual(cp._persist_candidates(".example.com"),
                         cp._persist_candidates("example.com"))

    def test_case_and_a_trailing_fqdn_dot_are_normalized(self):
        self.assertEqual(cp._persist_candidates("EXAMPLE.com."),
                         ["example.com", ".example.com"])

    def test_a_malformed_host_gets_no_invented_wildcards(self):
        self.assertEqual(cp._persist_candidates("a..b"), ["a..b"])
        self.assertEqual(cp._persist_candidates(""), [])
        self.assertEqual(cp._persist_candidates("."), [])

    def test_every_candidate_actually_matches_the_host_it_came_from(self):
        # The property that makes the set safe to offer: picking any of them grants at
        # least this request. Asserted against `_match`, the matcher they are derived
        # from, so a change to either side has to keep them consistent.
        for host in ("example.com", "a.b.example.com", "raw.githubusercontent.com",
                     "localhost", "1.2.3.4"):
            for pattern in cp._persist_candidates(host):
                self.assertTrue(cp._match(host, pattern),
                                f"{pattern} would not even match {host}")

    def test_pending_approvals_carry_the_patterns_resolve_will_accept(self):
        # Sent WITH the approval so the UI cannot offer a pattern the backend rejects.
        # One definition of the set, next to the matcher — not a second one in JS.
        cp._init_db()
        _clear_all()
        _hold("api.example.com", "opts-1")
        (row,) = cp._list_pending()
        self.assertEqual([o["pattern"] for o in row["persist_options"]],
                         cp._persist_candidates("api.example.com"))
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
        self.assertEqual(cp._decide("other.example.com")[0], "allow")

    def test_deny_persist_writes_a_block_rule_for_the_chosen_pattern(self):
        _resolve(_hold("bad.example.com"), "deny_persist", pattern=".example.com")
        self.assertEqual(_rules(), {(".example.com", "block")})
        self.assertEqual(cp._decide("anything.example.com")[0], "deny")

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
        self.assertEqual([r["id"] for r in cp._list_pending()], [approval])
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
        self.assertEqual(cp._decide("x.sneaky.example.com")[0], "hold")


class ProvenanceTests(_CPTestCase):
    """Resolving a hold is the one privileged action here — it grants egress — so
    "who approved this" must reach the durable record and the audit trail. It reached
    NEITHER before: an operator's click and a scripted POST were indistinguishable
    once written. Detection, not prevention (the self-reported fields are forgeable
    by a host-local caller), so what is asserted is that the trace exists and keeps
    observed and asserted values apart."""

    def test_actor_separates_observed_peer_from_self_reported_fields(self):
        actor = cp._actor(_FakeRequest(peer="172.31.0.3", headers={
            cp.ACTOR_HEADER: "127.0.0.1",
            "origin": "http://127.0.0.1:8081",
            "user-agent": "Mozilla/5.0 (X11)"}))
        self.assertIn("peer=172.31.0.3", actor)      # observed by us
        self.assertIn("via-ui=127.0.0.1", actor)     # asserted by the relay
        self.assertIn("origin=http://127.0.0.1:8081", actor)
        self.assertIn('ua="Mozilla/5.0 (X11)"', actor)

    def test_actor_tolerates_a_bare_request(self):
        self.assertIn("peer=?", cp._actor(_FakeRequest(peer=None)))
        self.assertIn("unrecorded", cp._actor(None))

    def test_actor_bounds_a_hostile_user_agent(self):
        actor = cp._actor(_FakeRequest(headers={"user-agent": "A" * 5000}))
        self.assertLess(len(actor), 400)

    def test_resolution_records_provenance_on_the_approval_row(self):
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 5
        try:
            t, _result, approval_id = HoldHandshakeTests._authorize_in_thread(
                self, "provenance.com")
            _resolve(approval_id, "allow_once",
                     _FakeRequest(peer="172.31.0.3",
                                  headers={cp.ACTOR_HEADER: "10.1.2.3",
                                           "user-agent": "curl/8.5.0"}))
            t.join(2)
        finally:
            cp.HOLD_TIMEOUT = saved
        with cp._connect() as conn:
            row = conn.execute(
                "SELECT status, resolved_by FROM approvals WHERE host=?",
                ("provenance.com",)).fetchone()
        self.assertEqual(row["status"], "allowed")
        self.assertIn("via-ui=10.1.2.3", row["resolved_by"])
        # A non-browser caller is exactly what the UA field is for.
        self.assertIn("curl/8.5.0", row["resolved_by"])

    def test_audit_reason_carries_the_actor(self):
        # _audit is mocked by _CPTestCase, so assert on what the waiter passed it —
        # that is the record an operator actually reads back.
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 5
        try:
            t, result, approval_id = HoldHandshakeTests._authorize_in_thread(
                self, "audited.com")
            _resolve(approval_id, "allow_once", _FakeRequest(peer="172.31.0.9"))
            t.join(2)
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertEqual(result["resp"].decision, "allow")
        reasons = [c.kwargs.get("reason", "") for c in cp._audit.call_args_list]
        self.assertTrue(any("human approval" in r and "peer=172.31.0.9" in r
                            for r in reasons),
                        f"actor missing from audit reasons: {reasons}")

    def test_audit_reason_says_whether_standing_policy_was_written(self):
        # A one-off and a persist are the same allow for THIS request and very
        # different afterwards, and the audit line used to say nothing about which had
        # happened — so reading the log could not tell you that a click had changed
        # policy. Read from the durable `mode` column, i.e. what was recorded.
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 5
        try:
            t, result, approval_id = HoldHandshakeTests._authorize_in_thread(
                self, "persisted.example.com")
            _resolve(approval_id, "allow_persist")
            t.join(2)
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertEqual(result["resp"].decision, "allow")
        reasons = [c.kwargs.get("reason", "") for c in cp._audit.call_args_list]
        self.assertTrue(any("standing rule written" in r for r in reasons),
                        f"persist not distinguishable in audit reasons: {reasons}")

    def test_audit_reason_marks_a_one_off_as_one_off(self):
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 5
        try:
            t, _result, approval_id = HoldHandshakeTests._authorize_in_thread(
                self, "oneoff.example.com")
            _resolve(approval_id, "deny_once")
            t.join(2)
        finally:
            cp.HOLD_TIMEOUT = saved
        reasons = [c.kwargs.get("reason", "") for c in cp._audit.call_args_list]
        self.assertTrue(any("this request only" in r for r in reasons), reasons)
        self.assertFalse(any("standing rule" in r for r in reasons), reasons)

    def test_timeout_path_reports_no_actor_rather_than_a_wrong_one(self):
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 0.05
        try:
            resp = cp.authorize(_auth_req("nobody.com", client="a"))
        finally:
            cp.HOLD_TIMEOUT = saved
        self.assertEqual(resp.decision, "deny")
        self.assertIn("timeout", resp.reason)
        self.assertNotIn("peer=", resp.reason)


class RulesViewTests(_CPTestCase):
    """``/api/rules`` exists so standing policy is not invisible from the UI that
    governs it. It is the complete policy, in precedence order, with the wildcard
    semantics spelled out."""

    def test_lists_every_rule_with_source_and_scope(self):
        _set_rules([("example.com", "allow"), ("bad.com", "block"),
                    (".github.com", "allow")])
        rows = cp.api_rules()
        self.assertEqual(len(rows), 3)
        by_pattern = {r["pattern"]: r for r in rows}
        self.assertEqual(by_pattern["example.com"]["action"], "allow")
        self.assertEqual(by_pattern["bad.com"]["action"], "block")
        # Every row carries where it came from, so seed and operator rules are
        # distinguishable in the listing.
        self.assertEqual(by_pattern["example.com"]["source"], "test")

    def test_blocks_are_listed_first_because_block_wins(self):
        _set_rules([("aaa-allow.com", "allow"), ("zzz-block.com", "block")])
        actions = [r["action"] for r in cp.api_rules()]
        # Alphabetically 'allow' < 'block' and aaa- < zzz-, so a naive ordering would
        # invert this. The listing must read in DECISION precedence order.
        self.assertEqual(actions, ["block", "allow"])

    def test_wildcard_scope_is_named_not_left_to_the_reader(self):
        # A leading dot is a subdomain wildcard that LOOKS like a hostname — the
        # thing that makes an over-broad persisted rule easy to miss.
        _set_rules([(".example.com", "allow"), ("example.com", "allow")])
        scope = {r["pattern"]: r["scope"] for r in cp.api_rules()}
        self.assertEqual(scope[".example.com"], "host + subdomains")
        self.assertEqual(scope["example.com"], "exact host")

    def test_scope_agrees_with_the_matcher_that_implements_it(self):
        # Guard against the label drifting from _match's real behaviour.
        self.assertTrue(cp._match("sub.example.com", ".example.com"))
        self.assertFalse(cp._match("sub.example.com", "example.com"))
        self.assertEqual(cp._pattern_scope(".example.com"), "host + subdomains")
        self.assertEqual(cp._pattern_scope("example.com"), "exact host")

    def test_empty_policy_is_an_empty_list_not_an_error(self):
        _set_rules([])
        self.assertEqual(cp.api_rules(), [])


class ConfigViewTests(_CPTestCase):
    """``/api/config`` exists so a pending card can show a COUNTDOWN. Without it the
    UI would have to hardcode the hold window, and a card that cannot say how long is
    left cannot distinguish hold-for-approval from a slow deny."""

    def test_config_reports_the_hold_window(self):
        self.assertEqual(cp.api_config()["hold_timeout"], cp.HOLD_TIMEOUT)

    def test_config_follows_the_operator_setting_rather_than_a_constant(self):
        saved = cp.HOLD_TIMEOUT
        cp.HOLD_TIMEOUT = 45.0
        try:
            self.assertEqual(cp.api_config()["hold_timeout"], 45.0)
        finally:
            cp.HOLD_TIMEOUT = saved

    def test_config_exposes_nothing_but_that(self):
        # A read-only view of NON-SECRET config on the one interface that can grant
        # egress: whatever gets added here has to stay harmless to publish.
        self.assertEqual(set(cp.api_config()), {"hold_timeout"})


class SaturationTests(_CPTestCase):
    """Over the hold cap ``/authorize`` fails closed WITHOUT creating an approval, so
    the refusal never becomes a card and the approvals page shows the same empty list
    as a quiet afternoon. These assert the record that makes it visible."""

    def _over_cap(self, host="pypi.org", client=None):
        saved = cp.MAX_PENDING
        cp.MAX_PENDING = 0
        try:
            return cp.authorize(_auth_req(host, client=client))
        finally:
            cp.MAX_PENDING = saved

    def test_a_rejection_is_recorded_with_what_was_refused(self):
        self._over_cap(host="pypi.org")
        sat = cp._saturation()
        self.assertEqual(sat["rejections"], 1)
        self.assertEqual(sat["last_host"], "pypi.org")
        self.assertEqual(sat["last_scope"], "global")
        self.assertIsNotNone(sat["last_ts"])

    def test_rejections_accumulate_rather_than_overwrite(self):
        # The count is the point: a burst of twenty is a different event from one,
        # and only the count survives the holds draining.
        for _ in range(3):
            self._over_cap()
        self.assertEqual(cp._saturation()["rejections"], 3)

    def test_the_per_client_cap_is_recorded_as_its_own_scope(self):
        saved = cp.MAX_PENDING_PER_CLIENT
        cp.MAX_PENDING_PER_CLIENT = 1
        try:
            _hold("a.example", "held-1")
            cp._PENDING_CLIENT["held-1"] = "172.30.0.9"
            cp.authorize(_auth_req("b.example", client="172.30.0.9"))
        finally:
            cp.MAX_PENDING_PER_CLIENT = saved
        # Distinguishable from a global exhaustion: "one agent is hammering" and
        # "the whole control plane is loaded" want different responses.
        self.assertEqual(cp._saturation()["last_scope"], "client 172.30.0.9")

    def test_nothing_is_recorded_when_the_hold_is_accepted(self):
        _hold("quiet.example", "held-1")
        self.assertEqual(cp._saturation()["rejections"], 0)
        self.assertIsNone(cp._saturation()["last_ts"])

    def test_in_flight_counts_live_holds_not_pending_rows(self):
        # The divergence this exists for: after a restart the table can carry
        # `pending` rows with no live hold behind them. The cap is measured against
        # the in-memory set, so a count taken from the visible cards would be
        # confidently wrong in exactly the situation the banner reports.
        with cp._connect() as conn:
            conn.execute(
                "INSERT INTO approvals(id, ts, host, status) "
                "VALUES ('orphan', 0, 'stale.example', 'pending')")
            conn.commit()
        self.assertEqual(len(cp._list_pending()), 1)
        self.assertEqual(cp._saturation()["in_flight"], 0)
        self.assertEqual(cp._saturation()["cards"], 0)

    def test_in_flight_counts_waiters_and_cards_counts_cards(self):
        # The second divergence, and the reason both numbers are in the payload:
        # duplicates share a card, so "12/16 in flight" beside three cards is not a
        # contradiction. The global cap is measured against the FIRST number.
        _hold("a.example", "held-1", client="172.30.0.2")
        for _ in range(4):
            slot = cp._reserve_hold("ignored", threading.Event(), "172.30.0.2",
                                    "a.example")
            self.assertTrue(slot.joined)
        sat = cp._saturation()
        self.assertEqual(sat["in_flight"], 5)
        self.assertEqual(sat["cards"], 1)

    def test_the_payload_carries_the_holds_and_the_pressure_together(self):
        _hold("a.example", "held-1")
        payload = cp.approvals()
        self.assertEqual(set(payload), {"holds", "saturation"})
        self.assertEqual([h["id"] for h in payload["holds"]], ["held-1"])
        self.assertEqual(payload["saturation"]["in_flight"], 1)
        self.assertEqual(payload["saturation"]["max_pending"], cp.MAX_PENDING)

    def test_every_time_in_the_payload_is_absolute(self):
        # Load-bearing for the SSE stream, which emits on payload CHANGE: an
        # elapsed-seconds field would differ on every 1s tick, so the change
        # detection would fire forever, the heartbeat would never be sent, and an
        # idle page would receive a 1 Hz firehose. Named fields, so adding a
        # "seconds_ago" convenience trips this rather than the stream.
        self._over_cap()
        sat = cp._saturation()
        self.assertEqual(
            set(sat),
            {"in_flight", "cards", "max_pending", "rejections", "acknowledged",
             "last_ts", "last_scope", "last_host", "since"})
        # Both stamps are epoch seconds — comfortably past 2001 — not durations.
        self.assertGreater(sat["last_ts"], 1_000_000_000)
        self.assertGreater(sat["since"], 1_000_000_000)

    def test_an_idle_payload_is_byte_identical_across_ticks(self):
        # The property the test above protects, asserted directly against the
        # comparison the stream actually makes.
        _hold("a.example", "held-1")
        self.assertEqual(json.dumps(cp._pending_payload()),
                         json.dumps(cp._pending_payload()))

    def test_the_counter_says_since_when(self):
        # In-memory, so a restart resets it. The UI can only be honest about that if
        # the payload says which window the count covers.
        self.assertEqual(cp._saturation()["since"], cp._STARTED_TS)

    # ── acknowledgement ──────────────────────────────────────────────────────

    def test_acknowledgement_lives_in_the_control_plane_not_the_page(self):
        # The bug this fixes: dismissal was page state, so a reload brought the
        # banner back. An operator who believes they cleared something and a page
        # that disagrees on refresh is worse than offering no button.
        self._over_cap()
        self._over_cap()
        cp.api_saturation_ack(cp.AckRequest(count=2))
        sat = cp._saturation()
        self.assertEqual(sat["rejections"], 2)
        self.assertEqual(sat["acknowledged"], 2)

    def test_a_rejection_racing_the_click_is_not_swallowed(self):
        # The reason this is a high-water mark rather than a reset. The operator read
        # 2 and clicked; a third landed in the gap. Zeroing the counter would lose it,
        # and rejections come in bursts — exactly when the gap is open.
        self._over_cap()
        self._over_cap()
        self._over_cap()                       # arrives while the click is in flight
        cp.api_saturation_ack(cp.AckRequest(count=2))
        sat = cp._saturation()
        self.assertEqual(sat["rejections"] - sat["acknowledged"], 1)

    def test_acknowledgement_never_goes_backwards(self):
        for _ in range(3):
            self._over_cap()
        cp.api_saturation_ack(cp.AckRequest(count=3))
        cp.api_saturation_ack(cp.AckRequest(count=1))   # a stale tab, or a replay
        self.assertEqual(cp._saturation()["acknowledged"], 3)

    def test_acknowledging_more_than_happened_is_clamped(self):
        # Otherwise a client number silences FUTURE rejections until they catch up —
        # a governance signal suppressed by an unvalidated input. Same reasoning as
        # validating `pattern` in resolve, and the same answer.
        self._over_cap()
        cp.api_saturation_ack(cp.AckRequest(count=10_000))
        self.assertEqual(cp._saturation()["acknowledged"], 1)
        self._over_cap()
        sat = cp._saturation()
        self.assertEqual(sat["rejections"] - sat["acknowledged"], 1)

    def test_negative_acknowledgements_are_floored(self):
        self._over_cap()
        cp.api_saturation_ack(cp.AckRequest(count=-5))
        self.assertEqual(cp._saturation()["acknowledged"], 0)

    def test_the_window_moves_to_the_dismissal(self):
        # The count and the stamp beside it must describe the SAME span, or the
        # banner reads "1 request since 14:02" for something that happened at 15:30.
        self._over_cap()
        before = cp._saturation()["since"]
        self.assertEqual(before, cp._STARTED_TS)
        cp.api_saturation_ack(cp.AckRequest(count=1))
        after = cp._saturation()["since"]
        self.assertNotEqual(after, cp._STARTED_TS)
        self.assertGreaterEqual(after, before)

    def test_an_acknowledgement_that_changes_nothing_leaves_the_window_alone(self):
        self._over_cap()
        cp.api_saturation_ack(cp.AckRequest(count=1))
        stamped = cp._saturation()["since"]
        cp.api_saturation_ack(cp.AckRequest(count=1))   # idempotent replay
        self.assertEqual(cp._saturation()["since"], stamped)


class FreshSchemaTests(unittest.TestCase):
    """A brand-new store must carry ``resolved_by`` from the DDL itself.

    This is what carries the provenance column now that there is no migration step
    (see the NOTE in ``_init_db``): the one store that predated the column was
    migrated in place, so every store from here on is created with it. If it ever
    fell out of the ``CREATE TABLE``, every resolve would fail at runtime, so assert
    it against a genuinely empty database rather than the shared test one."""

    def test_new_store_has_the_provenance_column(self):
        saved = cp.DB_PATH
        cp.DB_PATH = os.path.join(_TMP, "fresh-schema.db")
        try:
            cp._init_db()
            with cp._connect() as conn:
                cols = {r["name"] for r in
                        conn.execute("PRAGMA table_info(approvals)")}
            self.assertIn("resolved_by", cols)
            cp._init_db()          # idempotent: a second run must not fail
        finally:
            cp.DB_PATH = saved


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
    """The control plane serves two listeners, and WHICH one a handler lands on is
    a security property rather than a layout choice.

    The egress proxy has a network route to the authorize listener and none to the
    management one. That topology is in docker-compose.yml, but it is only worth
    anything if the listener the proxy CAN reach stays harmless — so the roster
    below is asserted exactly, and adding to it has to be a deliberate act rather
    than the side effect of putting a new endpoint next to an existing one.

    The stakes are asymmetric: a management route missing from its app is a broken
    UI, noticed in seconds. A management route that also appears on the authorize
    app is a self-approval path for the agent, noticed never."""

    #: Everything the proxy-facing listener may serve. /authorize answers a policy
    #: question; /healthz is what the container's health gate probes.
    AUTHORIZE_ROUTES: ClassVar[set] = {("POST", "/authorize"),
                                       ("GET", "/healthz")}

    def test_the_authorize_listener_serves_exactly_two_routes(self):
        self.assertEqual(_routes(cp.authorize_app), self.AUTHORIZE_ROUTES)

    def test_nothing_but_healthz_is_served_on_both(self):
        shared = _routes(cp.app) & _routes(cp.authorize_app)
        self.assertEqual(shared, {("GET", "/healthz")})

    def test_the_endpoint_that_grants_egress_is_management_only(self):
        # resolve is THE privileged action — it is what turns a held request into
        # allowed egress. If the agent can reach this, the governance plane is a
        # formality, so it gets its own assertion rather than relying on the
        # roster test above to catch it by arithmetic.
        resolve = [r for r in _routes(cp.app) if r[1].endswith("/resolve")]
        self.assertEqual(len(resolve), 1, "resolve is not on the management app")
        self.assertNotIn(resolve[0], _routes(cp.authorize_app))

    def test_the_views_that_read_the_store_are_management_only(self):
        # Not privileged, but they carry the record: pending hosts and clients,
        # the audit history, the standing policy. A bypassed relay guard must not
        # be able to read them either.
        for path in ("/approvals", "/approvals/stream", "/api/audit",
                     "/api/rules", "/api/config", "/status"):
            self.assertIn(("GET", path), _routes(cp.app), path)
            self.assertNotIn(("GET", path), _routes(cp.authorize_app), path)


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
               authorize_bind="0.0.0.0", authorize_port=8091):  # noqa: S104
        with mock.patch.multiple(cp, MANAGE_BIND=manage_bind,
                                 MANAGE_PORT=manage_port,
                                 AUTHORIZE_BIND=authorize_bind,
                                 AUTHORIZE_PORT=authorize_port):
            cp._assert_listeners_separated()

    def test_a_pinned_management_address_is_accepted(self):
        self._check(manage_bind="172.31.0.2")

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


if __name__ == "__main__":
    unittest.main()
