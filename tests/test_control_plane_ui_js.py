# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the control-plane-ui PAGE SCRIPT (``control-plane-ui/app.js``).

The frontend had no tests at all while it grew the two behaviours that most decide
whether governance actually reaches a human: which lamp the traffic light shows, and
how the pending-approval list is rebuilt when the feed pushes. Both are now pure
functions at the top of ``app.js`` — for exactly this reason — and this module asserts
them, mirroring the split the Python side already uses (assert the decision functions;
leave the I/O to the integration checks).

Two groups:

**Pure helpers, under node.** ``node`` evaluates the module and dumps the results of a
fixed set of calls as JSON; the assertions stay here, in Python, so they read like the
rest of ``tests/``. Skipped when node is absent, the same way ``make lint`` skips a
linter that is not installed — the intrinsic guards below still run. This also asserts
the property that makes the file testable at all: requiring it under node must have NO
side effects, because everything touching the DOM lives inside ``start()``, which runs
only in a browser. If DOM work ever migrates to the top level, ``require`` throws and
these tests fail loudly rather than the file quietly becoming untestable again.

**CSP/markup agreement, no node needed.** ``script-src 'self'`` is only worth sending
while the page has no inline script. That is an invariant spanning two files, so it is
checked rather than trusted: re-inlining the script would not break the page, it would
silently reduce the Content-Security-Policy to decoration.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "control-plane-ui" / "app.js"
INDEX_HTML = ROOT / "control-plane-ui" / "index.html"

# `make test` runs discovery with `-t tests`, so sibling modules import by bare name;
# this keeps the file runnable on its own too. Imported for `_CSP` / `_directives` —
# the policy and its parser live with the app, so this module does not restate them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_control_plane_ui import _directives, ui  # noqa: E402 (path set above)

_NODE = shutil.which("node")
# Missing node SKIPS on a dev machine — running the checks you can run beats running
# none — but FAILS under DOCKADE_REQUIRE_TOOLS, which CI sets. A silently skipped
# test that still reports success is how coverage shrinks without anyone noticing;
# these ten tests are the only coverage app.js has. Mirrors the Makefile's strict mode.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))

# Evaluate the module and report a fixed set of calls. Deliberately data-only: no
# assertions live here, so a failure is reported by Python with a normal diff.
# The path arrives by environment rather than argv: `node -e` shifts the argument
# vector (there is no script filename), so an index would be quietly wrong.
_PROBE = r"""
const m = require(process.env.DOCKADE_APP_JS);
const missing = ["lampState", "backoffDelay", "diffPending", "shouldSweep",
                 "holdRemaining", "countdownState", "departure", "persistPreview",
                 "saturationState", "ackCount", "requestsLabel",
                 "auditRow", "auditStatus", "rulesStatus", "repeatCount",
                 "fmtTime", "fmtStamp", "fmtInstant"]
  .filter(n => typeof m[n] !== "function");
console.log(JSON.stringify({
  missing,
  lamp: {
    down_idle: m.lampState(false, 0),
    down_busy: m.lampState(false, 3),
    up_idle: m.lampState(true, 0),
    up_busy: m.lampState(true, 2),
  },
  backoff: [0, 1, 2, 3, 4, 5, 6, 20].map(n => m.backoffDelay(n)),
  backoff_negative: m.backoffDelay(-1),
  limits: {
    min: m.RECONNECT_MIN_MS, max: m.RECONNECT_MAX_MS, stale: m.STALE_MAX_MS,
  },
  diff: {
    fresh: m.diffPending([], [{ id: "a" }, { id: "b" }]),
    unchanged: m.diffPending(["a", "b"], [{ id: "a" }, { id: "b" }]),
    gone_from_middle: m.diffPending(["a", "b", "c"], [{ id: "a" }, { id: "c" }]),
    added_and_gone: m.diffPending(["a", "b"], [{ id: "b" }, { id: "c" }]),
    emptied: m.diffPending(["a"], []),
    // A card kept on screen after leaving the queue (resolved/stale) must not be
    // re-added when the next push still omits it.
    lingering: m.diffPending(["a"], []),
  },
  sweep: {
    idle_fresh: m.shouldSweep(false, 0),
    busy_fresh: m.shouldSweep(true, 0),
    busy_just_under: m.shouldSweep(true, m.STALE_MAX_MS - 1),
    busy_at_cap: m.shouldSweep(true, m.STALE_MAX_MS),
  },
  // The minimum dwell: an idle list must NOT sweep a card that has not been readable
  // for its kind's floor yet, which is the whole point of the parameter.
  dwell: {
    values: m.DWELL_MS,
    expired_idle_immediately: m.shouldSweep(false, 0, m.DWELL_MS.expired),
    expired_idle_just_under: m.shouldSweep(false, m.DWELL_MS.expired - 1,
                                           m.DWELL_MS.expired),
    expired_idle_at_floor: m.shouldSweep(false, m.DWELL_MS.expired,
                                         m.DWELL_MS.expired),
    // A dwell longer than STALE_MAX_MS is a floor, so a hovered list cannot shorten it
    // and the cap cannot either.
    expired_busy_at_stale_cap: m.shouldSweep(true, m.STALE_MAX_MS,
                                             m.DWELL_MS.expired),
    expired_busy_at_floor: m.shouldSweep(true, m.DWELL_MS.expired,
                                         m.DWELL_MS.expired),
    resolved_idle_just_under: m.shouldSweep(false, m.DWELL_MS.resolved - 1,
                                            m.DWELL_MS.resolved),
    resolved_idle_at_floor: m.shouldSweep(false, m.DWELL_MS.resolved,
                                          m.DWELL_MS.resolved),
    // No dwell (the old two-argument behaviour) must be unchanged.
    no_dwell_idle: m.shouldSweep(false, 0, 0),
    no_dwell_busy: m.shouldSweep(true, 0, 0),
  },
  // The hold countdown. Fixed clocks, not Date.now(), so the arithmetic is asserted
  // rather than the machine's mood: a hold requested at t=1000 with a 120s window.
  remaining: {
    at_start: m.holdRemaining(1000, 120, 1000 * 1000),
    halfway: m.holdRemaining(1000, 120, 1060 * 1000),
    at_deadline: m.holdRemaining(1000, 120, 1120 * 1000),
    past_deadline: m.holdRemaining(1000, 120, 5000 * 1000),
    browser_clock_behind: m.holdRemaining(1000, 120, 0),
  },
  countdown: {
    fresh: m.countdownState(120, 120),
    fractional: m.countdownState(43.2, 120),
    at_urgent_boundary: m.countdownState(m.COUNTDOWN_URGENT_S, 120),
    just_outside_urgent: m.countdownState(m.COUNTDOWN_URGENT_S + 1, 120),
    zero: m.countdownState(0, 120),
    unknown_window: m.countdownState(0, 0),
  },
  urgent_at: m.COUNTDOWN_URGENT_S,
  gone: {
    expired: m.departure(0, 120),
    time_left: m.departure(55, 120),
    window_unknown: m.departure(null, null),
  },
  preview: {
    exact: m.persistPreview("allow_persist",
                            { pattern: "example.com", scope: "exact host" }),
    wildcard: m.persistPreview("allow_persist",
                               { pattern: ".example.com",
                                 scope: "host + subdomains" }),
    blocking: m.persistPreview("deny_persist",
                               { pattern: "example.com", scope: "exact host" }),
    nothing: m.persistPreview(null, null),
    // A rule already holding the pattern. The opposite action is the case the backend
    // REFUSES; the same action is merely redundant.
    conflicting: m.persistPreview("deny_persist",
                                  { pattern: ".example.com",
                                    scope: "host + subdomains",
                                    existing: "allow" }),
    conflicting_allow: m.persistPreview("allow_persist",
                                        { pattern: "a.example",
                                          scope: "exact host",
                                          existing: "block" }),
    redundant: m.persistPreview("allow_persist",
                                { pattern: "a.example", scope: "exact host",
                                  existing: "allow" }),
    // A backend that has not been restarted into this change sends no `existing`.
    unannotated: m.persistPreview("deny_persist",
                                  { pattern: "a.example", scope: "exact host" }),
  },
  // Saturation. NOW is fixed at 1_000_000_000_000 ms (= 1e9 s) so every "how long
  // ago" below is arithmetic on constants rather than on the wall clock.
  saturation: (() => {
    const NOW = 1e12;
    const base = { in_flight: 0, max_pending: 16, rejections: 0,
                   last_ts: null, last_scope: null, last_host: null, since: 1e9 };
    const rej = (over) => ({ ...base, rejections: 3, last_host: "pypi.org",
                             last_scope: "client 172.30.0.9",
                             last_ts: (NOW - over) / 1000 });
    return {
      quiet: m.saturationState(base, NOW, 0),
      absent: m.saturationState(null, NOW, 0),
      // Two holds against a cap of 16 is not news; 12 is 75% of it.
      light_load: m.saturationState({ ...base, in_flight: 2 }, NOW, 0),
      near_cap: m.saturationState({ ...base, in_flight: 12 }, NOW, 0),
      at_cap: m.saturationState({ ...base, in_flight: 16 }, NOW, 0),
      // Duplicates grouped: 12 blocked requests on 2 cards. The gauge counts the
      // requests (that is what the cap bounds) and must say so, or it reads as a
      // queue of 12 that has stopped draining.
      grouped_load: m.saturationState(
        { ...base, in_flight: 12, cards: 2 }, NOW, 0).text,
      // Ungrouped, the two numbers agree and the extra clause is noise.
      ungrouped_load: m.saturationState(
        { ...base, in_flight: 12, cards: 12 }, NOW, 0).text,
      // An older backend sends no `cards` at all.
      no_cards_field: m.saturationState({ ...base, in_flight: 12 }, NOW, 0).text,
      recent: m.saturationState(rej(5000), NOW, 0),
      just_inside: m.saturationState(rej(m.SATURATION_RECENT_MS - 1), NOW, 0),
      just_outside: m.saturationState(rej(m.SATURATION_RECENT_MS + 1), NOW, 0),
      // A rejection with no usable stamp must not be silently downgraded to "past".
      no_stamp: m.saturationState({ ...base, rejections: 1 }, NOW, 0),
      global_scope: m.saturationState(
        { ...rej(5000), last_scope: "global" }, NOW, 0).detail,
      no_scope: m.saturationState(
        { ...rej(5000), last_scope: null, last_host: null }, NOW, 0).detail,
      dismissed: m.saturationState(rej(5000), NOW, 3),
      dismissed_then_more: m.saturationState({ ...rej(5000), rejections: 4 }, NOW, 3),
      // Two more after acknowledging three: the banner counts the UNREAD, not the
      // lifetime total, so it must say 2.
      unread_not_total: m.saturationState(
        { ...rej(5000), rejections: 5 }, NOW, 3).text,
      // A rejection outranks the gauge: the event matters more than the level.
      rejection_beats_load: m.saturationState(
        { ...rej(5000), in_flight: 16 }, NOW, 0).level,
      one: m.saturationState({ ...rej(5000), rejections: 1 }, NOW, 0).text,
      // Timestamps. TZ is pinned by the runner (see setUpClass), so these are exact
      // strings rather than "something date-shaped" — which is the whole point of
      // formatting here instead of deferring to the viewer's locale.
      stamps: {
        // 2026-08-06T22:30:05Z, which is 2026-08-07 00:30:05 in Stockholm. The UTC
        // and local DATES differ on purpose: a midday sample would let a UTC-vs-local
        // mix-up pass, and this is the row where getting it wrong misfiles a decision
        // by a day.
        time: m.fmtTime(1786055405),
        stamp: m.fmtStamp(1786055405),
        instant: m.fmtInstant(1786055405),
        // Single-digit month, day, hour, minute and second at once — the case
        // zero-padding exists for, which an unpadded format renders "2026-1-2 3:4:5".
        padded: m.fmtStamp(1767319445),
        bad_time: m.fmtTime("whenever"),
        bad_stamp: m.fmtStamp(undefined),
        // null and "" are the dangerous ones: Number() turns both into 0, so they
        // pass any isFinite check and render as the Unix epoch.
        bad_instant: m.fmtInstant(null),
        null_stamp: m.fmtStamp(null),
        empty_stamp: m.fmtStamp(""),
        null_row_ts: m.auditRow({ ts: null, host: "a.example" }).ts,
      },
      audit: {
        ordinary_stage: m.AUDIT_ORDINARY_STAGE,
        tunnelled: m.auditRow({ ts: 1e9, decision: "allow", stage: "connect",
                                host: "pypi.org", client: "172.30.0.7",
                                reason: "allowed by rule (pypi.org)" }),
        plaintext: m.auditRow({ ts: 1e9, decision: "deny", stage: "http",
                                host: "a.example", client: "172.30.0.2",
                                reason: "no matching rule" }),
        no_stage: m.auditRow({ ts: 1e9, decision: "hold", host: "a.example" }),
        // A stage a future hook might add still shows: the bound is on SHAPE, not on
        // a fixed vocabulary.
        future_stage: m.auditRow({ ts: 1e9, decision: "deny", stage: "tls",
                                   host: "a.example" }).stagePrefix,
        // Case is not part of the bound: it does nothing to make a value blend into
        // the host, and suppressing a future `TLS` would be a silent surprise.
        uppercase_stage: m.auditRow({ ts: 1e9, stage: "TLS",
                                      host: "a.example" }).stagePrefix,
        // Values that must NOT reach the cell, one per excluded character class and
        // each rejected for ONE reason only. Named cases were tried first and were the
        // wrong SHAPE of test: every value failed for several reasons at once, so
        // mutations admitting whitespace, then dots, then colons each survived in turn
        // — the assertions could not say which rule was doing the work.
        rejected: [
          "x".repeat(13),      // too long
          "http ",             // trailing space
          "ht tp",             // inner space
          "evil.example",      // a dot: reads as a hostname beside the real one
          "http:8080",         // a colon
          "a/b",               // a slash
          "<b>http</b>",       // markup
          "http%20x",          // percent-encoding
          "http\\x",           // backslash
          "-http",             // leading punctuation
          "",                  // empty
        ].map(s => [s, m.auditRow({ ts: 1e9, stage: s,
                                    host: "a.example" }).stagePrefix]),
        at_the_length_limit: m.auditRow({ ts: 1e9, stage: "x".repeat(12),
                                          host: "a.example" }).stagePrefix,
        no_client: m.auditRow({ ts: 1e9, decision: "allow", host: "a.example" }),
        junk_ts: m.auditRow({ ts: "soon", decision: "allow", host: "a.example" }).ts,
        empty: m.auditRow({}),
        nothing: m.auditRow(null),
      },
      repeats: {
        // n, and what the row should say about it.
        grouped: m.auditRow({ ts: 1e9, n: 47, first_ts: 1e9 - 2800,
                              host: "chatty.example" }),
        single: m.auditRow({ ts: 1e9, n: 1, first_ts: 1e9, host: "a.example" }),
        absent: m.auditRow({ ts: 1e9, host: "a.example" }),
        // n>1 but no span recorded — the count still stands on its own.
        no_first_ts: m.auditRow({ ts: 1e9, n: 3, host: "a.example" }),
        counts: [47, 1, 0, -5, null, undefined, "many", 2.7, 1e9, true]
          .map(n => [String(n), m.repeatCount({ n })]),
        no_row: m.repeatCount(null),
      },
      audit_status: {
        // rowCount, failed, loaded
        first_load_in_flight: m.auditStatus(0, false, false),
        genuinely_empty: m.auditStatus(0, false, true),
        has_rows: m.auditStatus(12, false, true),
        failed_with_rows: m.auditStatus(12, true, true),
        failed_from_cold: m.auditStatus(0, true, false),
      },
      rules_status: {
        // Same three states, same argument order — the policy view is filled by its
        // own poll and can be stale while the header reads `live`, exactly like the
        // decisions view.
        first_load_in_flight: m.rulesStatus(0, false, false),
        genuinely_empty: m.rulesStatus(0, false, true),
        has_rows: m.rulesStatus(12, false, true),
        failed_with_rows: m.rulesStatus(12, true, true),
        failed_from_cold: m.rulesStatus(0, true, false),
      },
      requests: {
        one: m.requestsLabel(1),
        four: m.requestsLabel(4),
        two: m.requestsLabel(2),
        missing: m.requestsLabel(undefined),
        zero: m.requestsLabel(0),
        junk: m.requestsLabel("lots"),
      },
      ack: {
        counts_what_happened: m.ackCount({ rejections: 7 }),
        nothing_to_ack: m.ackCount(null),
        absent_field: m.ackCount({}),
        never_negative: m.ackCount({ rejections: -2 }),
        junk: m.ackCount({ rejections: "three" }),
      },
    };
  })(),
}));
"""


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class PageScriptTests(unittest.TestCase):
    """The pure decision helpers in app.js."""

    probe: dict

    @classmethod
    def setUpClass(cls) -> None:
        if not _NODE:
            raise AssertionError(
                "node is not installed and DOCKADE_REQUIRE_TOOLS is set — refusing to "
                "report success for the app.js tests, which are the only coverage that "
                "file has. Install node, or drop strict mode to skip them knowingly.")
        # _NODE comes from shutil.which (absolute path, no shell), and both arguments
        # are repo paths — no untrusted input reaches the command line.
        # TZ is PINNED, and not to UTC. The formatters render LOCAL time, so a UTC
        # runner would let a UTC-vs-local mix-up pass unnoticed; a zone two hours off
        # makes that mistake a failing assertion. The offset also has to be one whose
        # local date differs from the UTC date for the sample instants below.
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args — see above)
            [_NODE, "-e", _PROBE],
            env={**os.environ, "DOCKADE_APP_JS": str(APP_JS),
                 "TZ": "Europe/Stockholm"},
            capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError(
                "requiring control-plane-ui/app.js under node failed. The module must "
                "be importable with NO side effects — everything that touches the DOM "
                "belongs inside start(), which only runs in a browser. node said:\n"
                + proc.stderr)
        cls.probe = json.loads(proc.stdout)

    def test_every_helper_is_exported(self):
        # Guards the export block: dropping a name there silently disables its tests.
        self.assertEqual(self.probe["missing"], [])

    def test_the_lamp_treats_being_blind_as_worse_than_being_busy(self):
        lamp = self.probe["lamp"]
        # Red for a dead feed, even with approvals waiting: an unseen hold
        # default-denies when CONTROL_HOLD_TIMEOUT elapses, so a stale amber (let
        # alone green) would understate it.
        self.assertEqual(lamp["down_idle"], "red")
        self.assertEqual(lamp["down_busy"], "red")
        self.assertEqual(lamp["up_busy"], "amber")
        self.assertEqual(lamp["up_idle"], "green")

    def test_backoff_doubles_from_one_second_and_caps(self):
        limits = self.probe["limits"]
        self.assertEqual(self.probe["backoff"][:6],
                         [1000, 2000, 4000, 8000, 16000, 30000])
        self.assertEqual(self.probe["backoff"][-1], limits["max"])
        self.assertEqual(self.probe["backoff"][0], limits["min"])

    def test_backoff_is_monotonic_and_never_zero(self):
        # A zero or shrinking delay would turn a permanently-closed stream into a
        # reconnect loop against a backend that is already struggling.
        delays = self.probe["backoff"]
        self.assertEqual(delays, sorted(delays))
        self.assertTrue(all(d >= self.probe["limits"]["min"] for d in delays))
        # A negative attempt counter must clamp, not produce a sub-millisecond retry.
        self.assertEqual(self.probe["backoff_negative"], self.probe["limits"]["min"])

    def test_a_surviving_approval_is_not_re_added(self):
        # THE property behind keyed rendering: an unchanged push must be a no-op, so a
        # card is never re-created underneath the operator. The old code assigned the
        # whole list to innerHTML on every push (up to 1/s), which discarded in-flight
        # button state and let a removal shift the row of egress-granting buttons
        # upwards between the eye and the click.
        self.assertEqual(self.probe["diff"]["unchanged"], {"add": [], "gone": []})

    def test_new_approvals_are_reported_as_additions(self):
        self.assertEqual(self.probe["diff"]["fresh"]["gone"], [])
        self.assertEqual([a["id"] for a in self.probe["diff"]["fresh"]["add"]],
                         ["a", "b"])

    def test_a_removal_from_the_middle_touches_only_that_card(self):
        diff = self.probe["diff"]["gone_from_middle"]
        self.assertEqual(diff["add"], [])
        self.assertEqual(diff["gone"], ["b"])

    def test_additions_and_removals_are_reported_independently(self):
        diff = self.probe["diff"]["added_and_gone"]
        self.assertEqual([a["id"] for a in diff["add"]], ["c"])
        self.assertEqual(diff["gone"], ["a"])

    def test_an_emptied_queue_reports_every_card_gone(self):
        # What a backend restart looks like: startup expires every stale 'pending' row.
        self.assertEqual(self.probe["diff"]["emptied"], {"add": [], "gone": ["a"]})
        # And a card still on screen afterwards is not resurrected by the next push.
        self.assertEqual(self.probe["diff"]["lingering"]["add"], [])

    def test_a_stale_card_is_not_swept_while_the_list_is_in_use(self):
        sweep = self.probe["sweep"]
        # The misclick guard: while the pointer or focus is inside the list, removing
        # a card could move a button under it, so the removal waits.
        self.assertFalse(sweep["busy_fresh"])
        self.assertFalse(sweep["busy_just_under"])
        # Idle: remove immediately, nothing can shift under anyone.
        self.assertTrue(sweep["idle_fresh"])
        # But a parked cursor must not freeze the list forever.
        self.assertTrue(sweep["busy_at_cap"])


    def test_the_countdown_measures_the_hold_window(self):
        rem = self.probe["remaining"]
        self.assertEqual(rem["at_start"], 120)
        self.assertEqual(rem["halfway"], 60)
        self.assertEqual(rem["at_deadline"], 0)

    def test_a_skewed_browser_clock_cannot_produce_an_absurd_deadline(self):
        # `ts` is the BACKEND's clock and `now` the browser's. They agree in the
        # intended deployment, and where they don't the countdown should be wrong
        # rather than nonsense — never negative, never longer than the whole window.
        rem = self.probe["remaining"]
        self.assertEqual(rem["past_deadline"], 0)
        self.assertEqual(rem["browser_clock_behind"], 120)

    def test_the_countdown_reads_as_a_deadline_and_a_bar(self):
        cd = self.probe["countdown"]
        self.assertEqual(cd["fresh"]["text"], "expires in 120s")
        self.assertEqual(cd["fresh"]["frac"], 1)
        # Rounded UP, so the text never claims less time than there is.
        self.assertEqual(cd["fractional"]["text"], "expires in 44s")
        self.assertAlmostEqual(cd["fractional"]["frac"], 43.2 / 120)

    def test_the_last_seconds_are_called_out(self):
        cd = self.probe["countdown"]
        self.assertTrue(cd["at_urgent_boundary"]["urgent"])
        self.assertFalse(cd["just_outside_urgent"]["urgent"])
        self.assertTrue(cd["zero"]["urgent"])

    def test_zero_says_expiring_not_expired_because_the_backend_decides(self):
        # The browser's clock is not authoritative: a click at 0 may still land, and if
        # it doesn't the 409 path reports that. Claiming "expired" here would be the UI
        # asserting an outcome it doesn't know.
        self.assertEqual(self.probe["countdown"]["zero"]["text"], "expiring now")

    def test_an_unusable_window_cannot_divide_by_zero(self):
        self.assertEqual(self.probe["countdown"]["unknown_window"]["frac"], 0)

    def test_a_departed_card_says_which_way_it_went(self):
        gone = self.probe["gone"]
        # An expiry is a governance outcome — the agent was denied because nobody
        # looked in time — and it used to be indistinguishable from someone else
        # resolving the hold.
        self.assertIn("default-denied", gone["expired"]["text"])
        self.assertIn("120s", gone["expired"]["text"])
        self.assertNotIn("default-denied", gone["time_left"]["text"])
        self.assertIn("resolved elsewhere", gone["time_left"]["text"])
        # With the window unknown we genuinely cannot tell, and say so.
        self.assertIn("or was resolved elsewhere", gone["window_unknown"]["text"])

    def test_the_wording_and_the_dwell_come_from_one_decision(self):
        # The dwell is returned WITH the text it belongs to, so there is no second
        # kind→duration lookup that could disagree with the message on screen.
        gone = self.probe["gone"]
        dwell = self.probe["dwell"]["values"]
        self.assertEqual(gone["expired"]["kind"], "expired")
        self.assertEqual(gone["expired"]["dwellMs"], dwell["expired"])
        self.assertEqual(gone["time_left"]["kind"], "gone")
        self.assertEqual(gone["time_left"]["dwellMs"], dwell["gone"])
        # A card we cannot classify must not claim the expiry wording OR its long dwell.
        self.assertEqual(gone["window_unknown"]["kind"], "gone")
        self.assertEqual(gone["window_unknown"]["dwellMs"], dwell["gone"])

    def test_an_expired_card_cannot_be_swept_before_it_can_be_read(self):
        # THE defect this fixes: the sweep fires as soon as removal cannot move a
        # button under the pointer, so with the cursor anywhere else the
        # `expired — default-denied` marker — the one departure that reports a
        # governance failure — was on screen for about a second.
        dwell = self.probe["dwell"]
        self.assertFalse(dwell["expired_idle_immediately"])
        self.assertFalse(dwell["expired_idle_just_under"])
        self.assertTrue(dwell["expired_idle_at_floor"])

    def test_the_dwell_floor_outlasts_the_parked_cursor_cap(self):
        # STALE_MAX_MS exists so a hovered list cannot freeze the queue forever. A dwell
        # LONGER than it is a deliberate floor, so the cap must not cut it short.
        dwell = self.probe["dwell"]
        self.assertGreater(dwell["values"]["expired"], self.probe["limits"]["stale"])
        self.assertFalse(dwell["expired_busy_at_stale_cap"])
        self.assertTrue(dwell["expired_busy_at_floor"])

    def test_a_resolve_stays_readable_after_the_mouse_leaves(self):
        # Same bug, milder: clicking leaves the pointer inside the list, which is the
        # only reason the outcome message appeared to work — move the mouse away and it
        # went with the card.
        dwell = self.probe["dwell"]
        self.assertFalse(dwell["resolved_idle_just_under"])
        self.assertTrue(dwell["resolved_idle_at_floor"])

    def test_the_dwells_are_ordered_by_how_much_they_matter(self):
        # An expiry is a governance failure; a resolve the operator's own action; a
        # card resolved elsewhere purely informational.
        v = self.probe["dwell"]["values"]
        self.assertGreater(v["expired"], v["resolved"])
        self.assertGreater(v["resolved"], v["gone"])

    def test_no_dwell_behaves_exactly_as_before(self):
        # The gating properties the sweep already had must survive the new parameter.
        dwell = self.probe["dwell"]
        self.assertTrue(dwell["no_dwell_idle"])
        self.assertFalse(dwell["no_dwell_busy"])

    def test_the_persist_preview_names_the_pattern_and_flags_a_wildcard(self):
        prev = self.probe["preview"]
        self.assertEqual(prev["exact"]["pattern"], "example.com")
        self.assertEqual(prev["exact"]["scope"], "exact host")
        self.assertFalse(prev["exact"]["wild"])
        # A leading dot is the whole grant, so the wildcard flag is what the confirm
        # step shouts about — the rule covers hosts nothing has requested yet.
        self.assertTrue(prev["wildcard"]["wild"])
        self.assertEqual(prev["wildcard"]["pattern"], ".example.com")

    def test_the_preview_verb_follows_the_action(self):
        prev = self.probe["preview"]
        self.assertEqual(prev["exact"]["verb"], "allow")
        self.assertEqual(prev["blocking"]["verb"], "block")
        # Missing input defaults to the safer reading: previewing "block" where an
        # allow was meant gets caught by the operator; the reverse is the mistake the
        # confirm step exists to prevent.
        self.assertEqual(prev["nothing"]["verb"], "block")

    def test_the_preview_knows_a_rule_already_holds_the_pattern(self):
        """Nothing in this system replaces a rule, so persisting the opposite action
        writes nothing. That used to be reported as success — the card confirmed a
        standing block while policy still said allow. The backend refuses it now; this
        is what lets the panel say so before the click rather than after it."""
        prev = self.probe["preview"]
        # Opposite action: refused by the backend, so the panel must not present it as
        # an available choice.
        self.assertTrue(prev["conflicting"]["conflict"])
        self.assertFalse(prev["conflicting"]["redundant"])
        self.assertEqual(prev["conflicting"]["existing"], "allow")
        # Both directions, because a response claiming a write that did not happen is
        # the defect regardless of which way it fails.
        self.assertTrue(prev["conflicting_allow"]["conflict"])

    def test_a_rule_already_present_in_the_same_direction_is_only_redundant(self):
        prev = self.probe["preview"]
        # Not a conflict: the policy asked for is already in force. Worth saying, so
        # the operator is not told a rule was written when none was — but nothing to
        # refuse.
        self.assertFalse(prev["redundant"]["conflict"])
        self.assertTrue(prev["redundant"]["redundant"])

    def test_an_unannotated_option_claims_no_conflict(self):
        # A backend that predates this sends no `existing`. Failing open here is right:
        # the backend check is the enforcement, and inventing a conflict would block a
        # legitimate persist against a control plane that simply has not restarted.
        prev = self.probe["preview"]
        self.assertFalse(prev["unannotated"]["conflict"])
        self.assertFalse(prev["unannotated"]["redundant"])
        self.assertIsNone(prev["unannotated"]["existing"])
        # And the pre-existing wildcard caution is untouched by any of this.
        self.assertTrue(prev["wildcard"]["wild"])
        self.assertFalse(prev["wildcard"]["conflict"])

    # ── saturation banner ────────────────────────────────────────────────────

    def test_nothing_is_shown_while_nothing_has_gone_wrong(self):
        sat = self.probe["saturation"]
        # The banner is HIDDEN at zero rather than reporting "0 rejections". An
        # in-memory counter resets on restart, so a rendered zero would be a positive
        # all-clear the page is not entitled to give.
        self.assertFalse(sat["quiet"]["show"])
        self.assertFalse(sat["light_load"]["show"])
        # An absent payload (older backend, or a parse that yielded nothing) must not
        # throw and must not claim health either.
        self.assertFalse(sat["absent"]["show"])

    def test_the_gauge_appears_only_once_the_cap_is_within_reach(self):
        sat = self.probe["saturation"]
        self.assertTrue(sat["near_cap"]["show"])
        self.assertEqual(sat["near_cap"]["level"], "load")
        self.assertIn("12/16", sat["near_cap"]["text"])
        # At the cap the wording moves from conditional to actual, because it is no
        # longer a warning about what would happen.
        self.assertIn("would be denied", sat["near_cap"]["detail"])
        self.assertIn("at the cap", sat["at_cap"]["detail"])

    def test_the_gauge_distinguishes_held_requests_from_cards_on_screen(self):
        sat = self.probe["saturation"]
        # The two diverge as soon as duplicates group, and the divergence is the
        # confusing part: 12 requests on 2 cards looks like a stuck queue unless the
        # banner says which number is which.
        self.assertIn("12/16 requests held on 2 cards", sat["grouped_load"])
        # When they agree, the extra clause would be noise — and an older backend
        # sending no `cards` field must not render "on 0 cards".
        self.assertEqual(sat["ungrouped_load"], "12/16 holds in flight")
        self.assertEqual(sat["no_cards_field"], "12/16 holds in flight")

    def test_a_rejection_is_reported_as_an_event_not_a_level(self):
        sat = self.probe["saturation"]
        r = sat["recent"]
        self.assertTrue(r["show"])
        self.assertEqual(r["level"], "recent")
        # The wording has to name the thing that is invisible everywhere else: the
        # request was refused and NO CARD was ever raised for it.
        self.assertIn("denied unheard", r["text"])
        self.assertIn("no approval card", r["text"])
        self.assertIn("pypi.org", r["detail"])
        # Saturation outranks the gauge — the burst is the news, not the level it
        # left behind.
        self.assertEqual(sat["rejection_beats_load"], "recent")

    def test_the_detail_line_says_which_cap_was_hit(self):
        sat = self.probe["saturation"]
        # Not in the headline (the operator's response is the same either way), but
        # carried here, because "one agent hammering" and "the whole control plane
        # loaded" are different situations.
        self.assertIn("per-client cap for 172.30.0.9", sat["recent"]["detail"])
        self.assertIn("the global cap", sat["global_scope"])
        # A rejection with neither host nor scope must still render a sentence.
        self.assertEqual(sat["no_scope"], "last: unknown host")

    def test_the_notice_persists_after_it_stops_being_recent(self):
        sat = self.probe["saturation"]
        # Emphasis decays; the notice does not. Auto-clearing would re-create the
        # exact failure being reported — that nobody was looking at the time.
        self.assertEqual(sat["just_inside"]["level"], "recent")
        self.assertEqual(sat["just_outside"]["level"], "past")
        self.assertTrue(sat["just_outside"]["show"])
        self.assertEqual(sat["just_outside"]["text"], sat["just_inside"]["text"])

    def test_an_unusable_timestamp_does_not_downgrade_the_alert(self):
        sat = self.probe["saturation"]
        # No stamp means we cannot say it was long ago, so it must not be styled as
        # though it were — but it still shows.
        self.assertTrue(sat["no_stamp"]["show"])
        self.assertIsNone(sat["no_stamp"]["lastTs"])

    def test_dismissing_acknowledges_a_count_not_a_flag(self):
        sat = self.probe["saturation"]
        # Dismissed at 3 of 3: gone. One more arrives: back. A boolean flag here
        # would swallow every rejection after the first dismissal.
        self.assertFalse(sat["dismissed"]["show"])
        self.assertTrue(sat["dismissed_then_more"]["show"])

    def test_the_banner_counts_what_is_unread_not_what_has_ever_happened(self):
        sat = self.probe["saturation"]
        # After acknowledging 3, a 4th reads "1" — and the backend moves `since` to
        # the dismissal, so the number and the window beside it agree. Reporting the
        # lifetime total here would answer a question the operator did not ask.
        self.assertEqual(sat["dismissed_then_more"]["count"], 1)
        self.assertIn("1 request denied", sat["dismissed_then_more"]["text"])
        self.assertIn("2 requests denied", sat["unread_not_total"])

    def test_the_count_reads_as_english_for_one(self):
        self.assertIn("1 request denied", self.probe["saturation"]["one"])

    def test_a_dismissal_acknowledges_everything_currently_reported(self):
        """`ackCount` exists to be the ONE expression behind both the optimistic hide
        and the POST body. Written twice, the two could disagree and the failure would
        be silent: the banner hides, the POST returns 200, and the dismissal simply
        does not persist — which is the bug server-side acknowledgement was added to
        fix. It is also the only part of the dismiss path a unit test can reach, since
        the click handler lives in `start()`."""
        ack = self.probe["saturation"]["ack"]
        self.assertEqual(ack["counts_what_happened"], 7)
        # Nothing on screen, nothing to acknowledge — and never a number the backend
        # would have to clamp.
        self.assertEqual(ack["nothing_to_ack"], 0)
        self.assertEqual(ack["absent_field"], 0)
        self.assertEqual(ack["never_negative"], 0)
        self.assertEqual(ack["junk"], 0)

    # ── duplicate holds on one card ──────────────────────────────────────────

    def test_a_card_says_when_one_click_decides_several_requests(self):
        """Grouping changed what the buttons mean: "Allow once" can release four
        blocked requests. An operator granting egress to four believing it is one is
        the surprise this whole system exists to prevent, so the count is on the card
        — and only when it is news."""
        r = self.probe["saturation"]["requests"]
        self.assertIn("4 identical requests", r["four"])
        self.assertIn("one decision releases all of them", r["four"])
        self.assertIn("2 identical requests", r["two"])
        # Empty on the ordinary card. A badge on every row is one nobody reads on the
        # row where it matters — and "1 identical request" is not even English.
        self.assertEqual(r["one"], "")
        # Absent, zero or junk render nothing rather than a number: a card claiming
        # to decide "0 requests" would be worse than a card that says nothing.
        self.assertEqual(r["missing"], "")
        self.assertEqual(r["zero"], "")
        self.assertEqual(r["junk"], "")


    # ── timestamps ───────────────────────────────────────────────────────────

    def test_a_stamp_reads_the_same_for_every_operator(self):
        """The format is fixed rather than taken from the viewer's locale, and that is
        a correctness property, not a preference. `toLocaleString()` with no argument
        follows the BROWSER's language — so the same audit row read `8/6/2026` for one
        operator and `06/08/2026` for another. Those are different dates, in a table
        whose entire job is to say when something happened.

        Being able to write this assertion at all is the second half of the change: a
        locale-driven format could only ever be tested for shape."""
        s = self.probe["saturation"]["stamps"]
        self.assertEqual(s["stamp"], "2026-08-07 00:30:05")
        self.assertEqual(s["time"], "00:30:05")
        # ISO-8601 ordering, 24-hour, no AM/PM, no ambiguity about which number is
        # the month.
        self.assertRegex(s["stamp"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertNotIn("PM", s["stamp"])

    def test_the_displayed_stamp_is_local_and_the_title_is_absolute(self):
        s = self.probe["saturation"]["stamps"]
        # Same instant, two renderings: local for reading, UTC for correlating. The
        # dates differ here, which is exactly why the tooltip is worth carrying.
        self.assertTrue(s["stamp"].startswith("2026-08-07"), s["stamp"])
        self.assertEqual(s["instant"], "2026-08-06T22:30:05.000Z")

    def test_every_field_is_zero_padded(self):
        # Otherwise columns fail to align and, worse, sort wrong as text — the
        # property ISO ordering was chosen for in the first place.
        self.assertEqual(self.probe["saturation"]["stamps"]["padded"],
                         "2026-01-02 03:04:05")

    def test_an_unusable_timestamp_renders_empty_not_invalid_date(self):
        s = self.probe["saturation"]["stamps"]
        # `new Date(NaN)` stringifies to "Invalid Date", which in an audit cell reads
        # like a finding the system is reporting rather than a missing value.
        #
        # null and "" are the ones that actually bit: Number() maps both to 0, not
        # NaN, so they cleared every plausible numeric guard and rendered
        # "1970-01-01 01:00:00" — a real-looking decision time for a row that has none.
        for key in ("bad_time", "bad_stamp", "bad_instant",
                    "null_stamp", "empty_stamp"):
            self.assertEqual(s[key], "", key)
        self.assertIsNone(s["null_row_ts"],
                          "auditRow had the same hole and must not reintroduce it")

    # ── the decisions table ──────────────────────────────────────────────────

    def test_a_decision_row_carries_who_asked(self):
        a = self.probe["saturation"]["audit"]
        # The whole point of the change: one control plane serves every sandbox, so
        # "allowed egress to pypi.org" is only half a record.
        self.assertEqual(a["tunnelled"]["client"], "172.30.0.7")
        # An em dash, not a blank cell — blank reads as a broken column, whereas the
        # honest statement is that no client was recorded on this row.
        self.assertEqual(a["no_client"]["client"], "—")

    def test_the_stage_shows_only_when_it_is_not_the_ordinary_tunnel(self):
        a = self.probe["saturation"]["audit"]
        # Nearly every governed request is a CONNECT tunnel, so a column of `stage`
        # would be forty repetitions of one word and the one row worth noticing —
        # a plaintext HTTP decision — would not stand out at all.
        self.assertEqual(a["ordinary_stage"], "connect")
        self.assertEqual(a["tunnelled"]["stagePrefix"], "")
        # A PREFIX ON THE HOST — `http · example.com`, reading as the scheme it
        # effectively is. It does not qualify the decision (a deny at the http stage is
        # the same deny as at connect); it describes how the request was made. Keeping
        # it out of the decision cell also leaves that column uniform, which is the one
        # an operator scans vertically.
        #
        # The SEPARATOR is part of the value, not a CSS margin. A margin spaced it on
        # screen while textContent read "denyhttp" — which is what an operator gets
        # copying the row into a ticket, and what a screen reader says out loud. Live
        # testing surfaced it in the first pasted row.
        self.assertEqual(a["plaintext"]["stagePrefix"], "http · ")
        # Absent stage renders nothing rather than inventing "connect".
        self.assertEqual(a["no_stage"]["stagePrefix"], "")

    def test_only_a_short_plain_word_can_prefix_the_host(self):
        """`stage` is unvalidated free text from the API model all the way to this
        cell, and the prefix now sits immediately before the host without wrapping —
        so an unbounded value would run into the host it precedes or push it out of
        view.

        A SHAPE bound, deliberately not a vocabulary one: a stage a future hook adds
        still shows, which is the entire point of displaying the unusual ones. And not
        a security control either — `/authorize` is reachable only from control-net,
        and a compromised proxy could do far worse than mislabel a row."""
        a = self.probe["saturation"]["audit"]
        self.assertEqual(a["future_stage"], "tls · ")
        self.assertEqual(a["uppercase_stage"], "TLS · ")
        self.assertEqual(a["at_the_length_limit"], "x" * 12 + " · ")
        for value, prefix in a["rejected"]:
            self.assertEqual(prefix, "", f"stage {value!r} must not reach the cell")

    def test_a_malformed_row_still_renders(self):
        a = self.probe["saturation"]["audit"]
        # This list is fed from a table the agent influences the contents of, so a
        # missing field must degrade to a readable cell, never to a thrown render
        # that leaves the operator with a blank decisions view.
        self.assertEqual(a["empty"]["decision"], "?")
        self.assertEqual(a["nothing"]["decision"], "?")
        self.assertEqual(a["nothing"]["host"], "")
        self.assertIsNone(a["junk_ts"], "a non-numeric ts must not reach Date()")

    def test_a_repeated_decision_says_how_many_and_over_what_span(self):
        """`/api/audit` groups identical decisions, so a row can stand for many. The
        count without the span is not enough: 47x cannot distinguish a burst from a
        client retrying once a minute all afternoon, and those want different
        responses from whoever reads the row."""
        r = self.probe["saturation"]["repeats"]
        self.assertEqual(r["grouped"]["repeat"], "47x")
        self.assertEqual(r["grouped"]["firstTs"], 1e9 - 2800)

    def test_an_ordinary_row_is_untouched_by_grouping(self):
        """n==1 is the majority case and must render exactly as it did before grouping
        existed. A literal "1x" on every row would be noise on all of them to annotate
        a few — and `absent` covers a payload with no `n` at all, so the frontend
        degrades to the old behaviour rather than to a broken group."""
        r = self.probe["saturation"]["repeats"]
        for case in ("single", "absent"):
            with self.subTest(case=case):
                self.assertEqual(r[case]["repeat"], "")
                self.assertIsNone(r[case]["firstTs"])

    def test_a_count_with_no_span_still_reports_the_count(self):
        r = self.probe["saturation"]["repeats"]
        self.assertEqual(r["no_first_ts"]["repeat"], "3x")
        self.assertIsNone(r["no_first_ts"]["firstTs"])

    def test_only_a_real_count_above_one_groups(self):
        # Same trust posture as the rest of this row: the value comes from a table the
        # agent influences, so anything unusable reads as the ordinary single row.
        r = dict(self.probe["saturation"]["repeats"]["counts"])
        self.assertEqual(r["47"], 47)
        self.assertEqual(r["2.7"], 2, "a fractional count must not reach the cell")
        self.assertEqual(r["1000000000"], 1000000000)
        for junk in ("1", "0", "-5", "null", "undefined", "many", "true"):
            with self.subTest(n=junk):
                self.assertEqual(r[junk], 1)
        self.assertEqual(self.probe["saturation"]["repeats"]["no_row"], 1)

    def test_an_empty_list_and_a_failed_poll_no_longer_look_alike(self):
        """The filed defect, and the reason a bare empty state would not have fixed
        it. The header cannot disambiguate these either: `conn` reports the SSE
        stream, while this table is filled by a poll that can fail independently."""
        s = self.probe["saturation"]["audit_status"]
        # Genuinely empty: say so plainly, no warning styling.
        self.assertTrue(s["genuinely_empty"]["show"])
        self.assertEqual(s["genuinely_empty"]["level"], "none")
        self.assertIn("No decisions recorded yet", s["genuinely_empty"]["text"])
        # Failed: warn, and distinguish "these rows are stale" from "there are none".
        self.assertEqual(s["failed_with_rows"]["level"], "warn")
        self.assertIn("may be out of date", s["failed_with_rows"]["text"])
        self.assertEqual(s["failed_from_cold"]["level"], "warn")
        self.assertIn("unreachable", s["failed_from_cold"]["text"])
        # Healthy with rows: the table speaks for itself.
        self.assertFalse(s["has_rows"]["show"])

    def test_nothing_is_claimed_before_the_first_response(self):
        s = self.probe["saturation"]["audit_status"]
        # "No decisions recorded yet" during the first fetch would be a positive
        # all-clear the page has not earned — the same reasoning that keeps the
        # saturation banner hidden at zero rather than rendering one.
        self.assertFalse(s["first_load_in_flight"]["show"])

    def test_the_policy_view_reports_its_own_staleness(self):
        """The decisions view got this first and the policy view then sat swallowing
        its poll failures for as long — while showing rules that might no longer be in
        force, which is what an operator reads before deciding a hold."""
        s = self.probe["saturation"]["rules_status"]
        self.assertEqual(s["failed_with_rows"]["level"], "warn")
        self.assertEqual(s["failed_from_cold"]["level"], "warn")
        self.assertIn("unreachable", s["failed_from_cold"]["text"])
        self.assertFalse(s["has_rows"]["show"])
        self.assertFalse(s["first_load_in_flight"]["show"])

    def test_an_empty_policy_says_what_happens_next(self):
        # With no rules nothing matches, so `_decide` holds every host. That is a fact
        # about the next request, which is the useful thing to say — not an observation
        # that a table is short.
        s = self.probe["saturation"]["rules_status"]
        self.assertTrue(s["genuinely_empty"]["show"])
        self.assertEqual(s["genuinely_empty"]["level"], "none")
        self.assertIn("held for approval", s["genuinely_empty"]["text"])

    def test_the_two_views_do_not_share_a_sentence(self):
        """The logic is shared on purpose; the WORDING must not be. A stale decisions
        table is old history. A stale policy table misstates what is currently allowed.
        Pointing `rulesStatus` at the decisions text would pass every assertion above
        except this one."""
        a = self.probe["saturation"]["audit_status"]
        r = self.probe["saturation"]["rules_status"]
        for state in ("genuinely_empty", "failed_with_rows", "failed_from_cold"):
            with self.subTest(state=state):
                self.assertNotEqual(a[state]["text"], r[state]["text"])
        # And the policy view's stale wording makes the claim that matters.
        self.assertIn("no longer be what is in force", r["failed_with_rows"]["text"])


class DecisionsTableSourceTests(unittest.TestCase):
    """`refreshAudit` lives in `start()` and cannot be unit-tested, so the parts of
    it that would fail SILENTLY are asserted against the source — the same approach
    the dismiss handler and the duplicate badge use."""

    def setUp(self):
        self.src = APP_JS.read_text()
        self.body = re.search(r"async function refreshAudit\(\)\s*\{(.*?)\n  \}",
                              self.src, re.S)
        self.assertIsNotNone(self.body, "refreshAudit not found — renamed?")

    def test_the_decisions_table_stamps_the_date_not_only_the_time(self):
        # Forty rows routinely span midnight, and a time-only stamp makes them read
        # as out of order at exactly the moment ordering matters. WHICH formatter this
        # render calls is not reachable from a unit test; the formatters themselves are
        # (see the FormatterTests above), so only the call site needs guarding.
        self.assertIn("fmtStamp(", self.body.group(1))
        self.assertNotIn("fmtTime(", self.body.group(1))

    def test_the_row_carries_the_unambiguous_instant_as_well(self):
        # The visible stamp is LOCAL and states no offset, which is fine on the
        # operator's own screen and not fine once a row is correlated against
        # `make logs-cp` or pasted into an advisory.
        self.assertRegex(self.body.group(1), r'title="\$\{esc\(fmtInstant\(')

    def test_a_failed_refresh_keeps_the_rows_and_reports_the_staleness(self):
        # Both halves matter. Clearing on failure would throw away the only data the
        # operator has; not reporting it is the bug being fixed.
        self.assertIn("auditFailed = true", self.body.group(1))
        self.assertIn("renderAuditStatus(", self.body.group(1))
        self.assertNotRegex(
            self.body.group(1),
            r'catch[^}]*innerHTML\s*=\s*""',
            "a failed poll must not blank the table")

    def test_a_recovered_poll_clears_the_warning_and_re_renders(self):
        # The recovery half, which was asserted for neither table until a mutation of
        # the policy one survived. Both strings appear in the failure path too, so the
        # split at the catch's `return` is what makes this about the SUCCESS path.
        _, sep, success = self.body.group(1).partition("return;\n    }")
        self.assertTrue(sep, "the failure path no longer returns early")
        self.assertIn("auditFailed = false", success)
        self.assertIn("renderAuditStatus(", success)

    def test_a_non_ok_response_is_a_failure_not_a_row_of_json(self):
        # `fetch` does not reject on 4xx/5xx. Without this check a 502 from the relay
        # would flow into .json(), throw somewhere less obvious, or worse parse into
        # something that renders as an empty but SUCCESSFUL list.
        #
        # Matches the STATEMENT, not the substring "res.ok": the policy table's version
        # of this test was satisfied by a comment mentioning the check, and survived a
        # mutation that deleted the check itself.
        self.assertRegex(self.body.group(1), r"if\s*\(!res\.ok\)\s*throw")

    def test_each_cell_renders_the_field_its_header_promises(self):
        """A structural guard over the row template, because the render lives in
        `start()` where no unit test reaches — and two mutations proved value tests
        alone are not enough: moving the stage prefix back onto the decision cell, and
        dropping the host entirely, both left every assertion passing.

        Cell ORDER is the claim: the header row promises time, decision, host, client,
        reason, and nothing else checks that the body agrees."""
        row = re.search(r"return `\s*<tr>(.*?)</tr>`", self.body.group(1), re.S)
        self.assertIsNotNone(row, "the row template was restructured")
        cells = row.group(1).split("<td")[1:]
        self.assertEqual(len(cells), 5, "expected five cells, one per header")
        time_, decision, host, client, reason = cells

        self.assertIn("fmtStamp(a.ts)", time_)
        self.assertIn("fmtInstant(a.ts)", time_)
        # The decision cell holds the decision and NOTHING else. It is the column an
        # operator scans vertically, so a variable-width extra makes it ragged — and
        # the stage does not qualify the decision anyway.
        self.assertIn("a.decision", decision)
        self.assertNotIn("stagePrefix", decision)
        # The stage prefixes the HOST, reading as the scheme it effectively is.
        self.assertIn("a.stagePrefix", host)
        self.assertIn("esc(a.host)", host)
        self.assertIn("esc(a.client)", client)
        self.assertIn("esc(a.reason)", reason)
        # Grouping annotates two cells and must not add a sixth (asserted above): the
        # repeat count sits beside the host it repeats, the span beside the reason,
        # which is the column that already carries explanatory text.
        self.assertIn("a.repeat", host)
        self.assertIn("a.firstTs", reason)
        self.assertNotIn("a.repeat", decision, "the decision column stays uniform")

    def test_the_grouping_annotations_carry_their_own_separators(self):
        """The `denyhttp` lesson, which cost a live debug: a CSS margin produced the
        right pixels and the wrong `textContent`, so a row copied into a ticket read as
        one word. A decisions table exists to be quotable evidence, so the space and
        the separator are part of the escaped VALUE, never styling."""
        body = self.body.group(1)
        self.assertIn('esc(" " + a.repeat)', body)
        self.assertIn("esc(` · first seen ${fmtStamp(a.firstTs)}`)", body)
        # fmtStamp, not fmtTime: a group's span can cover days (the scan behind it is
        # bounded by event count, not by a window), so a bare time reads as today.
        self.assertNotIn("fmtTime(a.firstTs)", body)

    def test_the_client_column_is_rendered_and_escaped(self):
        self.assertIn("esc(a.client)", self.body.group(1))
        # Scoped to the decisions SECTION, not the first <thead> in the file — the
        # policy table also has one, and a reordering of the two sections would
        # otherwise silently point this assertion at the wrong table.
        section = re.search(r'<section id="view-decisions".*?</section>',
                            INDEX_HTML.read_text(), re.S)
        self.assertIsNotNone(section, "the decisions section was renamed")
        self.assertIn("<th>client</th>", section.group(0),
                      "the column exists in the body but has no header")


class PolicyTableSourceTests(unittest.TestCase):
    """`refreshRules` lives in `start()` too, and its failure path was a bare
    `catch (e) { /* transient */ }` — the same swallow the decisions table had, left
    in place after that one was fixed. Guarded at the source for the same reason."""

    def setUp(self):
        self.src = APP_JS.read_text()
        self.body = re.search(r"async function refreshRules\(\)\s*\{(.*?)\n  \}",
                              self.src, re.S)
        self.assertIsNotNone(self.body, "refreshRules not found — renamed?")

    def test_a_failed_refresh_keeps_the_rules_and_reports_the_staleness(self):
        body = self.body.group(1)
        self.assertIn("rulesFailed = true", body)
        self.assertIn("renderRulesStatus(", body)
        self.assertNotRegex(body, r'catch[^}]*innerHTML\s*=\s*""',
                            "a failed poll must not blank the policy table")
        # The specific regression: a catch that discards the error and says nothing.
        self.assertNotRegex(
            body, r"catch\s*\([^)]*\)\s*\{\s*/\*[^*]*\*/\s*\}",
            "the failure path is a comment again, not a reported state")

    def test_a_non_ok_response_is_a_failure_not_a_row_of_json(self):
        # `fetch` does not reject on 4xx/5xx, and this one used to call .json()
        # straight off the response — so a 502 from the relay could parse into
        # something that rendered as an empty but SUCCESSFUL policy. On this table
        # that reads as "no standing rules", which is the opposite of the truth.
        #
        # The STATEMENT, not the substring — see the decisions-table twin.
        self.assertRegex(self.body.group(1), r"if\s*\(!res\.ok\)\s*throw")

    def test_a_recovered_poll_clears_the_warning_and_re_renders(self):
        """Setting the failed flag is only half a fix, and both halves were mutants
        that survived the first run. Without the reset the table warns forever after
        one blip; without a render on the success path the warning stays on screen
        until the next failure, and the empty state never appears at all.

        Split at the catch's `return`, so these are asserted on the SUCCESS path
        specifically — both strings also occur in the failure path, where they prove
        nothing."""
        _, sep, success = self.body.group(1).partition("return;\n    }")
        self.assertTrue(sep, "the failure path no longer returns early")
        self.assertIn("rulesFailed = false", success)
        self.assertIn("renderRulesStatus(", success)

    def test_a_failed_poll_does_not_advance_the_change_signature(self):
        """`policySig` drives the "policy changed" badge. It must be assigned only on
        the success path: advancing it after a failure would silently swallow the next
        real change, because the comparison would be against a signature nobody saw."""
        body = self.body.group(1)
        before, _, after = body.partition("rulesLoaded = true")
        self.assertNotIn("policySig =", before,
                         "the signature is updated before the poll is known to work")
        self.assertIn("policySig = sig", after)


class PersistConflictSourceTests(unittest.TestCase):
    """The confirm panel and the resolve handler live in `start()`. Both would fail
    silently here — a disabled button that is never disabled, and a 409 handled as the
    wrong kind of 409 — so both are asserted against the source."""

    def setUp(self):
        self.src = APP_JS.read_text()

    def test_a_conflicting_pattern_cannot_be_confirmed(self):
        body = re.search(r"function renderPreview\(entry\)\s*\{(.*?)\n  \}",
                         self.src, re.S).group(1)
        # Warned about AND disabled. The backend refuses it, so letting the click
        # through spends a round trip to arrive at the same place.
        self.assertIn("entry.confirmBtn.disabled = p.conflict", body)
        self.assertIn("p.conflict", body)

    def test_focus_does_not_fall_off_a_disabled_confirm_button(self):
        # Focusing a disabled button drops focus to the body, stranding a keyboard
        # operator outside the panel that just opened — with Escape bound on the panel
        # and therefore no longer reaching anything.
        body = re.search(r"function askPersist\(a, action\)\s*\{(.*?)\n  \}",
                         self.src, re.S).group(1)
        self.assertRegex(body, r"entry\.confirmBtn\.disabled \?\s*entry\.select")

    def test_a_conflict_409_does_not_mark_the_card_stale(self):
        # Two different 409s reach this handler. "No longer pending" means the card is
        # dead; a persist conflict means the approval is deliberately still pending so
        # the operator can choose again. Treating the second as the first would retire
        # a live card and strand the request until it default-denies.
        conflict = re.search(r"r\.status === 409 && d\.conflict\)\s*\{(.*?)\n      \}",
                             self.src, re.S)
        self.assertIsNotNone(conflict, "the conflict 409 is not distinguished")
        self.assertIn("disableActions(entry, false)", conflict.group(1))
        self.assertNotIn("markStale", conflict.group(1))
        # And the narrower branch must come FIRST, or the general one swallows it.
        self.assertLess(self.src.index("r.status === 409 && d.conflict"),
                        self.src.index("} else if (r.status === 409) {"))

    def test_the_card_distinguishes_a_written_rule_from_one_already_there(self):
        # The old message said "standing rule" for both, on the reasoning that a
        # no-op insert only happened when the identical rule existed. It also happened
        # when the OPPOSITE rule existed, which is the bug this closes.
        #
        # Asserting each branch's CONDITION, not merely that both strings appear: a
        # first version of this checked only that the two phrases were present, and
        # survived a mutation routing `already_present` into the "written" branch —
        # which is the original defect, with the second string left unreachable.
        self.assertRegex(self.src, r"d\.persisted \? ` · standing rule written")
        self.assertRegex(
            self.src,
            r"d\.already_present\s*\?\s*` · standing rule already in place")


class DuplicateBadgeSourceTests(unittest.TestCase):
    """The badge is only honest if it is LIVE, and the liveness lives in `start()`
    where no unit test can reach it — so it is asserted against the source, the same
    way the dismiss handler is.

    What could silently break: `renderPending` updates cards that are added or gone
    and left surviving cards untouched, which is correct for every other field on a
    card and wrong for this one. A retry joining between the render and the click
    would leave the operator clicking a button labelled with a stale number."""

    def setUp(self):
        self.src = APP_JS.read_text()

    def test_the_count_is_refreshed_on_surviving_cards_not_only_new_ones(self):
        body = re.search(r"function renderPending\(list\)\s*\{(.*?)\n  \}",
                         self.src, re.S)
        self.assertIsNotNone(body, "renderPending not found — did it get renamed?")
        # Iterating the incoming list (not just `add`) is the whole point: `add` holds
        # only ids that were not already on screen.
        self.assertRegex(body.group(1), r"for \(const a of list\)")
        self.assertIn("setRequests(entry, a.requests)", body.group(1))

    def test_the_badge_is_placed_in_the_card_between_the_clock_and_the_buttons(self):
        # Building an element and never appending it is invisible to every test that
        # does not render a DOM, and this page has shipped that exact shape of bug
        # before — a banner that existed in the markup and could not be seen (see the
        # `[hidden]` guard below). Order is part of the claim, not decoration: the
        # badge qualifies what the buttons are about to do, so it has to be adjacent
        # to them rather than up with the metadata.
        parts = [p.strip() for p in
                 re.search(r"^    el\.append\((.*)\);", self.src, re.M)
                 .group(1).split(",")]
        self.assertIn("dup", parts, "the badge is built but never attached")
        self.assertEqual(parts.index("dup"), parts.index("actions") - 1)
        self.assertLess(parts.index("cd"), parts.index("dup"))

    def test_the_count_stops_moving_once_the_card_is_being_decided(self):
        # Rewriting the number under a click already in flight is the same lie in the
        # other direction: by then it is history, not what the button will do.
        body = re.search(r"for \(const a of list\)\s*\{(.*?)\n    \}",
                         self.src, re.S).group(1)
        self.assertIn('entry.state === "pending"', body)
        self.assertIn('entry.state === "confirming"', body)


class InlineScriptTests(unittest.TestCase):
    """``script-src 'self'`` is only worth sending while the page has no inline
    script, and that invariant spans two files — so it is checked, not trusted.
    Re-inlining would not break anything visible; it would quietly reduce the CSP to
    decoration on the one page that renders agent-controlled strings."""

    def test_the_page_carries_no_inline_script(self):
        html = INDEX_HTML.read_text()
        self.assertNotIn("<script>", html)
        self.assertNotIn("javascript:", html)
        # No inline handlers either (onclick=, onload=, …), which 'self' also forbids.
        self.assertNotRegex(html, r"<[^>]+\son[a-z]+\s*=")

    def test_the_page_loads_the_script_from_this_origin(self):
        html = INDEX_HTML.read_text()
        self.assertIn('<script src="/app.js"', html)
        # Never a third party: a governance UI must not fetch its own control logic
        # from a CDN, which is also why the favicon is an inline data URI.
        self.assertNotIn("//cdn", html)
        self.assertNotRegex(html, r'src="https?://')

    def test_the_policy_still_permits_what_the_page_actually_uses(self):
        # The favicon is a data: URI in the markup and rewritten to another one at
        # runtime, so img-src must keep allowing data: — the CSP and the page have to
        # be edited together or the icon silently stops rendering.
        html = INDEX_HTML.read_text()
        csp = _directives(ui._CSP)
        self.assertIn("data:image/svg+xml", html)
        self.assertEqual(csp["img-src"], ["data:"])
        self.assertIn("<style>", html)
        self.assertEqual(csp["style-src"], ["'unsafe-inline'"])

    def test_the_hidden_attribute_survives_this_stylesheet(self):
        """The script hides things by setting `.hidden`, which relies on the UA rule
        `[hidden] { display: none }` — and that rule loses to ANY author rule setting
        `display` on the same element, because author beats user-agent at equal
        specificity. `.saturation` and `.countdown` both set `display: flex`, so both
        were visible while the script believed otherwise; the saturation banner shipped
        as a permanently open empty panel offering a Dismiss button.

        Guarding the global override rather than enumerating the elements is the point:
        one rule makes the whole class impossible, whereas a per-element list is a list
        someone has to remember to extend. The page had a narrow
        `[role="tabpanel"][hidden]` patch — the same bug, fixed once, for one element.
        """
        html = INDEX_HTML.read_text()
        self.assertRegex(
            html, r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important",
            "index.html must carry a global `[hidden] { display: none !important; }`. "
            "Without it, any rule that sets `display` on an element the script hides "
            "leaves it on screen — see this test's docstring.")
        # Every element the script toggles `hidden` on, and every one the markup ships
        # hidden, is covered by that one rule — so a NARROWER re-patch is a signal the
        # global rule was lost or misunderstood.
        # Comments stripped first: the rationale above this rule quotes `[hidden] {`
        # in prose, and a scan that reads its own explanation as a violation is worse
        # than no scan.
        rules = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
        narrower = [s for s in re.findall(r"^\s*([^{\n]*\[hidden\][^{\n]*)\{", rules, re.M)
                    if s.strip() != "[hidden]"]
        self.assertEqual(narrower, [],
                         "a scoped [hidden] rule is redundant with the global one; if "
                         "it is there because the global rule stopped working, fix that")

    def test_every_element_the_script_reaches_for_exists_in_the_page(self):
        """Splitting the script out of the markup created a way for the two to drift:
        a `getElementById` that matches nothing returns null, and the very next line
        dereferences it, so a renamed id is a TypeError that only appears in a browser
        — with no test and no linter between the edit and the operator. Same spirit as
        the Makefile's cross-file consistency guards."""
        js = APP_JS.read_text()
        html = INDEX_HTML.read_text()
        ids = set(re.findall(r"""getElementById\(["']([^"']+)["']\)""", js))
        # The per-view ids are built by concatenation (`"view-" + v`), so expand them
        # from the view list rather than pretending the regex could see them.
        views = re.search(r"const VIEWS = \[([^\]]+)\]", js)
        self.assertIsNotNone(views, "VIEWS list not found — did the view wiring move?")
        for view in re.findall(r'"([^"]+)"', views.group(1)):
            ids.update({f"view-{view}", f"tab-{view}"})
        self.assertIn("pending-empty", ids)  # the keyed-render empty state
        for element_id in sorted(ids):
            self.assertRegex(html, rf'id="{re.escape(element_id)}"',
                             f'app.js reaches for #{element_id}, which index.html '
                             f'does not define')

    def test_every_endpoint_the_page_calls_is_served_or_relayed(self):
        """The relay allowlist is deliberately narrow — only the backend paths this UI
        needs cross it, so that `POST /authorize` and anything else never can. The cost
        is a coupling with no compiler between the two ends: adding a `fetch()` to
        app.js without adding its path to `_RELAY_ROUTES` yields a 403 that appears only
        when a real operator loads the page against a real backend. That is exactly how
        `/api/config` (the countdown's window) would have failed.

        Paths only, not methods: the shapes here are simple and the method-level
        allowlist has its own tests in test_control_plane_ui.py. The converse direction
        — a relayed route the page no longer calls — is deliberately not asserted; the
        allowlist still carries `/status` unused, and pruning it is a separate change."""
        js = APP_JS.read_text()
        # Served by this container rather than relayed (see control-plane-ui/app.py).
        local = {"/", "/app.js", "/healthz"}
        calls = re.findall(r"""(?:fetch|EventSource)\(\s*["'`]([^"'`]+)["'`]""", js)
        self.assertGreater(len(calls), 3, "no fetch/EventSource calls found — did the "
                                          "page's I/O move somewhere this cannot see?")
        for raw in calls:
            # `${...}` is an interpolated approval id; the route patterns bound it to a
            # slash-free segment, so any placeholder text stands in for it.
            path = re.sub(r"\$\{[^}]*\}", "ID", raw).split("?")[0]
            self.assertTrue(
                path in local or ui._relay_allowed("GET", path)
                or ui._relay_allowed("POST", path),
                f"app.js calls {path}, which control-plane-ui neither serves nor "
                f"relays — add it to _RELAY_ROUTES or it will 403 in the browser")

    def test_the_sweep_call_site_passes_a_per_card_dwell(self):
        """`shouldSweep` grew a minimum dwell because the `expired — default-denied`
        marker was swept within about a second whenever the cursor was outside the
        pending list. That floor defaults to `0`, which keeps the two-argument
        behaviour intact — and means **dropping the argument at the call site silently
        restores the bug** with every unit test still passing, because they call the
        function directly. So the call site itself is asserted."""
        js = APP_JS.read_text()
        calls = re.findall(r"shouldSweep\(([^)]*)\)", js)
        # The definition is the one mentioning its own parameter names; the rest are
        # real call sites.
        sites = [c for c in calls if "ageMs" not in c]
        self.assertEqual(len(sites), 1, f"unexpected shouldSweep call sites: {calls}")
        self.assertEqual(
            len(sites[0].split(",")), 3,
            "sweep() must pass the departed card's dwell as the third argument, or an "
            "expired hold is swept before it can be read")
        self.assertIn("dwell", sites[0])

    def test_the_dismiss_handler_acknowledges_and_then_believes_the_backend(self):
        """Two lines in `start()` that unit tests cannot reach, and they are each
        other's safety net — which is exactly why both are asserted here.

        The click must acknowledge via `ackCount`. A hardcoded number there hides the
        banner, returns 200, and silently fails to persist — the very bug server-side
        acknowledgement was added to fix. That mistake IS detectable at runtime,
        because the endpoint echoes what it recorded and the banner reappears at once
        if it disagrees. But only while the handler reads that echo rather than
        assuming its own number stuck. Drop the echo and the detector goes with it.

        Source-level and ugly, for the reason the sweep guard above is: the honest
        alternative is making omission impossible, and a call site passing the wrong
        literal cannot be designed away."""
        js = APP_JS.read_text()
        handler = re.search(r"satDismiss\.addEventListener\(.*?\n  \}\);", js, re.S)
        self.assertIsNotNone(handler, "dismiss handler not found — did the wiring move?")
        body = handler.group(0)
        self.assertIn("ackCount(", body,
                      "the dismiss click must acknowledge ackCount(...), not a literal")
        self.assertRegex(
            body, r"localAck\s*=\s*Number\([^;]*\.acknowledged\)",
            "the handler must adopt the acknowledgement the BACKEND recorded; without "
            "that echo a wrong count fails silently instead of re-raising the banner")

    def test_the_script_file_is_the_one_the_app_serves(self):
        # UI_SCRIPT points into the image; assert the repo file the Dockerfile copies
        # there is the one this test suite has been asserting on.
        self.assertTrue(APP_JS.is_file())
        self.assertEqual(Path(ui.UI_SCRIPT).name, APP_JS.name)


if __name__ == "__main__":
    unittest.main()
