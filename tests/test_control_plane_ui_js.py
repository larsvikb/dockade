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
# ``cp`` is the loaded control plane, for the two places this page duplicates a backend
# rule and has to be held equal to it: `normalizePattern` and the wildcard floor.
# Imported from the sibling test module rather than loaded again — that module already
# owns the temp-store setup, and loading twice would give two modules with two databases.
from test_control_plane_api import cp  # noqa: E402 (path set above)
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
                 "normalizePattern", "createPreview", "editPreview",
                 "saturationState", "ackCount", "capScope", "requestsLabel",
                 "auditRow", "auditStatus", "rulesStatus", "repeatCount",
                 "leaseLabel", "leaseRemaining", "leaseCountdown", "leasesStatus",
                 "leaseDomain", "groupLeases", "shortActor",
                 "timeWindow", "filterActive", "auditQuery", "eventRow",
                 "historyPager", "renderableHolds",
                 "toolRemaining", "payloadDisclosure", "toolOutcomeMessage",
                 "cardSubject",
                 "fmtTime", "fmtStamp", "fmtInstant",
                 "serverDescriptor", "serverPreview", "serverEditBody"]
  .filter(n => typeof m[n] !== "function");
const _row = {server: "mcp-github", enabled: false,
              auth: {type: "header", header: "Authorization",
                     template: "Bearer {secret}"}};
console.log(JSON.stringify({
  missing,
  server: {
    none: m.serverDescriptor("none"),
    bearer: m.serverDescriptor("header"),
    custom: m.serverDescriptor("custom", "  X-Api-Key  ", "  {secret}  "),
    // The preset must ignore whatever sits in the custom fields, or a half-filled
    // form would smuggle values into a descriptor the operator did not pick.
    bearer_with_stale_custom: m.serverDescriptor("header", "X-Evil", "{secret}-nope"),
    preview_none: m.serverPreview("mcp-github", m.serverDescriptor("none")).text,
    preview_bearer: m.serverPreview("mcp-github", m.serverDescriptor("header")).text,
    preview_blank: m.serverPreview("   ", m.serverDescriptor("none")),
    preview_upper: m.serverPreview("Mcp-GitHub", m.serverDescriptor("none")),
    preview_traversal: m.serverPreview("../etc/passwd", m.serverDescriptor("none")),
    preview_single: m.serverPreview("a", m.serverDescriptor("none")).ok,
    preview_half_custom: m.serverPreview("mcp-x", m.serverDescriptor("custom", "H", "")),
    edit_enable: m.serverEditBody(_row, true),
    edit_disable: m.serverEditBody(_row, false),
    edit_no_auth: m.serverEditBody({server: "s", enabled: true}, false),
  },
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
  kinds: {
    known: m.RENDERABLE_KINDS,
    // The queue is one list over two builders, so a payload can carry a kind this
    // script has never seen — a backend one step ahead of a tab left open.
    both: m.renderableHolds([{ id: "a", kind: "egress" },
                             { id: "b", kind: "tool" }]).map(a => a.id),
    unknown_dropped: m.renderableHolds([{ id: "a", kind: "egress" },
                                        { id: "b", kind: "seance" }]).map(a => a.id),
    // No kind at all is the shape every payload had before the tool surface existed.
    legacy_kept: m.renderableHolds([{ id: "a" }]).map(a => a.id),
    empty: m.renderableHolds([]),
    absent: m.renderableHolds(undefined),
  },
  tool: {
    fold: m.PAYLOAD_FOLD_BYTES,
    // Short payloads are readable without a click; long ones fold so one card cannot
    // push every other pending decision off the screen. Neither truncates.
    short: m.payloadDisclosure('{"a":1}'),
    long: m.payloadDisclosure("x".repeat(m.PAYLOAD_FOLD_BYTES + 1)),
    at_fold: m.payloadDisclosure("x".repeat(m.PAYLOAD_FOLD_BYTES)).open,
    missing: m.payloadDisclosure(undefined),
    // Read off the card's own absolute deadline, so it works before /api/config has
    // answered and needs no knowledge of the backend's tool window.
    remaining_mid: m.toolRemaining(1000, 400 * 1000),
    remaining_past: m.toolRemaining(1000, 5000 * 1000),
    remaining_unknown: m.toolRemaining(undefined, 0),
    allowed: m.toolOutcomeMessage({ outcome: "allow" }),
    denied: m.toolOutcomeMessage({ outcome: "deny" }),
    nothing: m.toolOutcomeMessage(undefined),
    // The announcement is the only place a screen-reader user learns WHICH decision
    // arrived, so a tool ask read out as a host describes the wrong sort of thing.
    say_tool: m.pendingAnnouncement(
      [{ kind: "tool", tool: "issue_write", server: "mcp-github" }], 1),
    say_tool_nameless: m.cardSubject({ kind: "tool" }),
    say_egress: m.pendingAnnouncement([{ host: "example.com" }], 1),
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
    // TWO global caps, so two gauges. `max_pending` counts cards and `max_waiters`
    // counts blocked requests; the cards cap is the lower of the two, exactly as the
    // shipped defaults are, so a case has to say which one it is driving.
    const base = { in_flight: 0, cards: 0, max_pending: 12, max_waiters: 16,
                   rejections: 0,
                   last_ts: null, last_scope: null, last_host: null, since: 1e9 };
    const rej = (over) => ({ ...base, rejections: 3, last_host: "pypi.org",
                             last_scope: "client 172.30.0.9 cards",
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
      // Ungrouped, the two numbers agree and the extra clause is noise. `cards: 12`
      // against a card cap of 12 would otherwise be the fuller gauge, so this case
      // raises that cap to keep the WAITER gauge the one being asserted.
      ungrouped_load: m.saturationState(
        { ...base, in_flight: 12, cards: 12, max_pending: 100 }, NOW, 0).text,
      // An older backend sends no `cards` at all.
      no_cards_field: m.saturationState(
        { ...base, in_flight: 12, cards: undefined }, NOW, 0).text,
      // The CARD gauge as the fuller one: 11 cards against 12 is 92%, while 11
      // waiters against 16 is under the warn fraction. Reporting the waiter gauge
      // here would hide the cap that is actually about to deny.
      cards_are_the_fuller_gauge: m.saturationState(
        { ...base, in_flight: 11, cards: 11 }, NOW, 0).text,
      // A cap of 0 refuses everything, and a `0/0` gauge would divide by zero. The
      // rejection notice reports that situation far better, so the gauge drops it.
      zero_cap: m.saturationState(
        { ...base, in_flight: 5, max_pending: 0, max_waiters: 0 }, NOW, 0),
      scopes: ["global cards", "global waiters",
               "client 172.30.0.9 cards", "client 172.30.0.9 waiters",
               "", "nonsense"].map(s => [s, m.capScope(s)]),
      recent: m.saturationState(rej(5000), NOW, 0),
      just_inside: m.saturationState(rej(m.SATURATION_RECENT_MS - 1), NOW, 0),
      just_outside: m.saturationState(rej(m.SATURATION_RECENT_MS + 1), NOW, 0),
      // A rejection with no usable stamp must not be silently downgraded to "past".
      no_stamp: m.saturationState({ ...base, rejections: 1 }, NOW, 0),
      global_scope: m.saturationState(
        { ...rej(5000), last_scope: "global waiters" }, NOW, 0).detail,
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
        // The client class, which is what the decision was actually taken against.
        classed: m.auditRow({ ts: 1e9, decision: "allow", host: "api.github.com",
                              client: "172.28.0.3", client_class: "mcp" }),
        // A row from before the column existed, and one the backend could not place.
        // Both render nothing rather than an invented label — these rows are evidence.
        unclassed: m.auditRow({ ts: 1e9, decision: "allow", host: "a.example",
                                client: "172.30.0.2" }),
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
      outage: {
        // Rows as /api/audit serves them; `fail_closed` is set by the backend.
        none: m.outageSummary([]),
        only_policy: m.outageSummary([{ fail_closed: false, host: "a", n: 9 }]),
        one: m.outageSummary([{ fail_closed: true, host: "a", n: 1 }]),
        // Counts REQUESTS not rows: the view is grouped, so one line can stand for
        // hundreds of refusals, and that is the number conveying the scale.
        grouped: m.outageSummary([{ fail_closed: true, host: "a", n: 12 },
                                  { fail_closed: true, host: "b", n: 3 },
                                  { fail_closed: false, host: "c", n: 900 }]),
        // Same host twice is one host, and a missing flag is not an outage.
        same_host: m.outageSummary([{ fail_closed: true, host: "a", n: 2 },
                                    { fail_closed: true, host: "a", n: 5 }]),
        no_flag: m.outageSummary([{ host: "a", n: 4 }]),
        nothing: m.outageSummary(null),
        // The row marker, from the same payload the renderer reads.
        row_marked: m.auditRow({ ts: 1e9, host: "a", fail_closed: true }).failClosed,
        row_plain: m.auditRow({ ts: 1e9, host: "a" }).failClosed,
      },
      coverage: {
        // rows (with grouped counts), and the total the backend reports.
        truncated: m.coverageSummary([{ n: 12 }, { n: 3 }], 1200),
        complete: m.coverageSummary([{ n: 12 }, { n: 3 }], 15),
        // A total BELOW what is shown is nonsense; say nothing rather than a
        // negative remainder.
        impossible: m.coverageSummary([{ n: 12 }], 3),
        no_total: m.coverageSummary([{ n: 12 }], undefined),
        junk_total: m.coverageSummary([{ n: 5 }], "lots"),
        empty: m.coverageSummary([], 0),
        counts_decisions: m.coverageSummary([{ n: 40 }], 900).shown,
        // With a filter applied the total is the MATCHING set, not the store.
        filtered_truncated: m.coverageSummary([{ n: 12 }], 1200, true),
        filtered_complete: m.coverageSummary([{ n: 12 }], 12, true),
        filtered_empty: m.coverageSummary([], 0, true),
      },
      // ── browsing the record ──────────────────────────────────────────────
      // A fixed clock: 2024-01-01T00:00:00Z is 1704067200s, so a 1h window starts
      // at 1704063600. Asserted arithmetic rather than the machine's mood.
      window: {
        any: m.timeWindow("", 1704067200000),
        hour: m.timeWindow("1h", 1704067200000),
        day: m.timeWindow("24h", 1704067200000),
        week: m.timeWindow("7d", 1704067200000),
        unknown_preset: m.timeWindow("all-of-it", 1704067200000),
        no_clock: m.timeWindow("1h", undefined),
        junk_clock: m.timeWindow("1h", "now"),
        // Sub-second clocks must not leak into the bound (see timeWindow).
        whole_seconds: m.timeWindow("1h", 1704067200999),
        presets: Object.keys(m.AUDIT_WINDOWS),
      },
      filter_active: {
        nothing: m.filterActive({ q: "", decision: "", preset: "" }),
        no_filter_object: m.filterActive(null),
        text: m.filterActive({ q: "evil" }),
        // Whitespace is not a filter: an accidental space must not relabel the view.
        whitespace: m.filterActive({ q: "   " }),
        decision: m.filterActive({ decision: "deny" }),
        window: m.filterActive({ preset: "24h" }),
        unknown_window: m.filterActive({ preset: "forever" }),
      },
      query: {
        bare: m.auditQuery({}, { limit: 40 }),
        everything: m.auditQuery(
          { q: "evil", decision: "deny", preset: "1h" },
          { limit: 100, nowMs: 1704067200000, before: "1704067200.5:42" }),
        // Free text is percent-encoded, or an `&` pasted from a URL splits the
        // query string into parameters the backend would misread or refuse.
        hostile_text: m.auditQuery({ q: "a&b=c #x/y" }, { limit: 40 }),
        trims: m.auditQuery({ q: "  evil  " }, { limit: 40 }),
        no_limit: m.auditQuery({ q: "x" }, {}),
        junk_limit: m.auditQuery({}, { limit: "lots" }),
        // A cursor must survive encoding intact: it is the position in the record,
        // and a mangled one either 400s or silently serves the newest page.
        cursor: m.auditQuery({}, { limit: 5, before: "1704067200.5:42" }),
      },
      event_row: {
        // A plaintext request: method and URL are what identify it.
        http: m.eventRow({ ts: 1e9, id: 7, decision: "deny", host: "a.example",
                           stage: "http", method: "GET", url: "https://a.example/x",
                           port: 80, proto: "http", client: "172.30.0.2",
                           client_class: "sandbox", reason: "blocked by rule" }),
        // A CONNECT tunnel has neither, and is identified by its port.
        tunnel: m.eventRow({ ts: 1e9, id: 8, decision: "allow", host: "b.example",
                             stage: "connect", port: 443, proto: "connect" }),
        // Neither recorded — an empty cell rather than an invented one.
        bare: m.eventRow({ ts: 1e9, id: 9, host: "c.example" }),
        nothing: m.eventRow(null),
        // The shared columns must be IDENTICAL to the folded view's, which is the
        // whole reason eventRow builds on auditRow.
        shares_shaping: (() => {
          const r = { ts: 1e9, decision: "deny", host: "a.example", stage: "http",
                      client_class: "mcp", fail_closed: true };
          const folded = m.auditRow(r), raw = m.eventRow(r);
          return ["ts", "decision", "host", "client", "stagePrefix",
                  "clientClassPrefix", "reason", "failClosed"]
            .every(k => JSON.stringify(folded[k]) === JSON.stringify(raw[k]));
        })(),
      },
      pager: {
        // page, pageSize, shown, total, hasNext, filtered
        first_of_many: m.historyPager(0, 100, 100, 4301, true, false),
        second_of_many: m.historyPager(1, 100, 100, 4301, true, false),
        last: m.historyPager(2, 100, 40, 240, false, false),
        only_page: m.historyPager(0, 100, 12, 12, false, false),
        empty: m.historyPager(0, 100, 0, 0, false, false),
        filtered: m.historyPager(1, 100, 100, 900, true, true),
        no_total: m.historyPager(0, 100, 5, undefined, false, false),
        junk: m.historyPager("x", "y", "z", "t", false, false),
      },
      audit_filtered_status: {
        // The empty sentence must not claim an empty RECORD when a filter is what
        // emptied the list.
        filtered_empty: m.auditStatus(0, false, true, true),
        unfiltered_empty: m.auditStatus(0, false, true, false),
        // A failed poll stays a failed poll — the more urgent fact either way.
        filtered_failed: m.auditStatus(0, true, true, true),
        filtered_has_rows: m.auditStatus(12, false, true, true),
        // A REFUSED filter: the backend's own sentence, and it outranks everything.
        refused: m.auditStatus(12, false, true, true,
                              "since (5) must be before until (2)"),
        refused_over_failed: m.auditStatus(0, true, true, true, "bad cursor"),
        refused_no_body: m.auditStatus(12, false, true, true, null),
      },
      revoke: {
        allow_rule: m.revokePreview({ pattern: ".github.com", action: "allow",
                                      source: "operator" }),
        block_rule: m.revokePreview({ pattern: "evil.example", action: "block",
                                      source: "operator" }),
        seed_rule: m.revokePreview({ pattern: "pypi.org", action: "allow",
                                     source: "seed" }),
        // An action the frontend does not recognise must warn as the DANGEROUS
        // direction, not the safe one.
        unknown_action: m.revokePreview({ pattern: "x", action: "", source: "op" }),
        nothing: m.revokePreview(null),
      },
      // The pattern as the STORE will hold it. Compared against the backend's own
      // normalizer by a test below, which is the whole reason these are echoed as
      // pairs rather than asserted here.
      normalize: ["Example.COM.", "  .Example.com  ", "example.com..", ".", "",
                  "EXAMPLE.com", "10.0.0.7.", ".co.uk"]
        .map(s => [s, m.normalizePattern(s)]),
      wildcard_min_labels: m.WILDCARD_MIN_LABELS,
      create: {
        nothing: m.createPreview("", "allow", "sandbox", []),
        no_class: m.createPreview("example.com", "allow", "", []),
        exact_allow: m.createPreview("pypi.example", "allow", "sandbox", []),
        // Normalization is visible in the preview, because the confirm has to be
        // about the rule that will actually land.
        normalized: m.createPreview(" PyPI.Example. ", "allow", "sandbox", []),
        wildcard_allow: m.createPreview(".example.com", "allow", "sandbox", []),
        exact_block: m.createPreview("evil.example", "block", "sandbox", []),
        // The floor: `.com` as an allow ends governance for a TLD, and as a block
        // only tightens.
        tld_allow: m.createPreview(".com", "allow", "sandbox", []),
        tld_block: m.createPreview(".com", "block", "sandbox", []),
        conflict: m.createPreview("evil.example", "allow", "sandbox",
          [{ pattern: "evil.example", action: "block", client_class: "sandbox" }]),
        redundant: m.createPreview("evil.example", "block", "sandbox",
          [{ pattern: "evil.example", action: "block", client_class: "sandbox" }]),
        // The same pattern in ANOTHER class is not a conflict — uniqueness is the
        // pair, and refusing here would make one class's policy unwritable.
        other_class: m.createPreview("evil.example", "allow", "sandbox",
          [{ pattern: "evil.example", action: "block", client_class: "mcp" }]),
        // An unrecognised action must preview as the SAFER reading.
        unknown_action: m.createPreview("example.com", "", "sandbox", []),
      },
      edit: (() => {
        const rule = { id: 7, pattern: ".example.com", action: "allow",
                       client_class: "sandbox", source: "operator" };
        const blockRule = { id: 7, pattern: "evil.example", action: "block",
                            client_class: "sandbox", source: "operator" };
        return {
          // Narrowing an allow: the subtree stops being allowed and one host starts.
          // Both halves have to be in the text or the confirm describes half an edit.
          narrowed: m.editPreview(rule, "api.example.com", "allow", [rule]),
          // Flipping an action on the same pattern. The rule holds its OWN pattern, so
          // this is the case a naive conflict check would refuse (the backend's `id<>?`).
          flipped: m.editPreview(blockRule, "evil.example", "allow", [blockRule]),
          tightened: m.editPreview(rule, ".example.com", "block", [rule]),
          // Narrowing a BLOCK loosens: the hosts falling out from under it stop being
          // denied. Flagged, which is why the old action matters and not only the new.
          narrowed_block: m.editPreview(blockRule, "one.evil.example", "block",
                                        [blockRule]),
          unchanged: m.editPreview(rule, ".example.com", "allow", [rule]),
          normalized: m.editPreview(rule, "  API.Example.COM.  ", "allow", [rule]),
          tld_allow: m.editPreview(rule, ".com", "allow", [rule]),
          tld_block: m.editPreview(rule, ".com", "block", [rule]),
          // Another rule already holding the target, versus the same pattern in another
          // class — which is not a collision, because uniqueness is the pair.
          conflict: m.editPreview(rule, "evil.example", "allow",
            [rule, { id: 9, pattern: "evil.example", action: "block",
                     client_class: "sandbox" }]),
          other_class: m.editPreview(rule, "evil.example", "allow",
            [rule, { id: 9, pattern: "evil.example", action: "block",
                     client_class: "mcp" }]),
          seed: m.editPreview({ id: 7, pattern: "pypi.org", action: "allow",
                                client_class: "sandbox", source: "seed" },
                              "evil.example", "allow", []),
          // The row went away under the form.
          gone: m.editPreview(undefined, "example.com", "allow", []),
          empty: m.editPreview(rule, "", "allow", [rule]),
          unknown_action: m.editPreview(rule, "api.example.com", "", [rule]),
        };
      })(),
      announce: {
        nothing: m.pendingAnnouncement([], 0),
        no_list: m.pendingAnnouncement(null, 0),
        one: m.pendingAnnouncement([{ host: "github.com" }], 1),
        one_of_many: m.pendingAnnouncement([{ host: "github.com" }], 3),
        several: m.pendingAnnouncement([{ host: "a" }, { host: "b" }], 5),
        nameless: m.pendingAnnouncement([{}], 1),
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
  // The lease helpers. Fixed clocks throughout, like the hold countdown above: a
  // deadline at t=2000 read from a browser clock at t=1000s.
  lease: {
    label_default: m.leaseLabel(1800),
    label_five: m.leaseLabel(300),
    label_hour: m.leaseLabel(3600),
    label_odd: m.leaseLabel(90),
    // Before /api/config answers, and for anything unusable. The button still works —
    // the backend owns the duration — so what is unknown is only what to call it.
    label_unknown: m.leaseLabel(null),
    label_zero: m.leaseLabel(0),
    label_junk: m.leaseLabel("soon"),
    remaining_mid: m.leaseRemaining(2000, 1000 * 1000),
    // The backend serves only live leases, so a past deadline means the two clocks
    // disagree — and "0" is the reading that cannot mislead.
    remaining_past: m.leaseRemaining(1000, 5000 * 1000),
    remaining_unknown: m.leaseRemaining(undefined, 0),
    cell_minutes: m.leaseCountdown(1805),
    cell_seconds: m.leaseCountdown(9),
    cell_at_urgent: m.leaseCountdown(m.COUNTDOWN_URGENT_S),
    cell_outside_urgent: m.leaseCountdown(m.COUNTDOWN_URGENT_S + 1),
    cell_unknown: m.leaseCountdown(null),
    status_empty: m.leasesStatus(0, false, true),
    status_stale: m.leasesStatus(2, true, true),
    status_cold: m.leasesStatus(0, true, false),
    status_quiet: m.leasesStatus(2, false, true),
    status_before_first_load: m.leasesStatus(0, false, false),
  },
  // Folding sibling hosts. The rows are the shape `/api/egress/leases` serves, trimmed
  // to the fields the grouping reads.
  group: {
    threshold: m.LEASE_GROUP_MIN,
    domain_plain: m.leaseDomain("cdn.example.com"),
    domain_deep: m.leaseDomain("a.b.c.example.com"),
    domain_apex: m.leaseDomain("example.com"),
    domain_single_label: m.leaseDomain("localhost"),
    domain_case: m.leaseDomain("CDN.Example.COM"),
    // An address has no domain to group under.
    domain_ipv4: m.leaseDomain("10.1.2.3"),
    domain_ipv6: m.leaseDomain("2001:db8::1"),
    // The public-suffix limitation, asserted so it is a KNOWN answer rather than a
    // surprise. Harmless here — it can only put two rows under one heading.
    domain_public_suffix: m.leaseDomain("shop.example.co.uk"),
    // Four siblings on one class fold into one group.
    siblings: (() => {
      const g = m.groupLeases([
        { id: 1, host: "cdn.example.com", client_class: "sandbox", expires_at: 300 },
        { id: 2, host: "api.example.com", client_class: "sandbox", expires_at: 200 },
        { id: 3, host: "www.example.com", client_class: "sandbox", expires_at: 400 },
      ]);
      return { count: g.length, grouped: g[0].grouped, members: g[0].count,
               domain: g[0].domain, soonest: g[0].soonest,
               order: g[0].leases.map(r => r.id), key: g[0].key };
    })(),
    // One lease is NOT a group of one — that would make every single lease cost a
    // click to read, which is worse than the clutter grouping exists to fix.
    lone: (() => {
      const g = m.groupLeases(
        [{ id: 1, host: "solo.example.com", client_class: "sandbox",
           expires_at: 100 }]);
      return { count: g.length, grouped: g[0].grouped, members: g[0].count };
    })(),
    // Two client populations under one domain are TWO groups. Folding them together
    // would read as one grant covering both tenants.
    split_by_class: (() => {
      const g = m.groupLeases([
        { id: 1, host: "api.example.com", client_class: "sandbox", expires_at: 300 },
        { id: 2, host: "cdn.example.com", client_class: "sandbox", expires_at: 300 },
        { id: 3, host: "api.example.com", client_class: "mcp", expires_at: 200 },
        { id: 4, host: "cdn.example.com", client_class: "mcp", expires_at: 250 },
      ]);
      return { count: g.length,
               keys: g.map(x => x.key),
               classes: g.map(x => x.clientClass) };
    })(),
    // Groups sort by the SOONEST expiry in each, which is the next thing about a group
    // that will actually change.
    order: (() => {
      const g = m.groupLeases([
        { id: 1, host: "a.later.com", client_class: "s", expires_at: 900 },
        { id: 2, host: "b.later.com", client_class: "s", expires_at: 950 },
        { id: 3, host: "a.sooner.com", client_class: "s", expires_at: 60 },
        { id: 4, host: "b.sooner.com", client_class: "s", expires_at: 70 },
      ]);
      return g.map(x => x.domain);
    })(),
    empty: m.groupLeases([]),
    absent: m.groupLeases(undefined),
  },
  // Provenance in a table cell. The long input is a REAL `_actor` string, copied from
  // a live lease — which is how the width problem was found.
  actor: {
    full: m.shortActor(
      'peer=172.31.0.10 via-ui=172.18.0.1 origin=http://localhost:28090 ' +
      'ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
      '(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"'),
    // A direct host-local caller: peer only, nothing asserted.
    peer_only: m.shortActor("peer=172.31.0.9"),
    // No headers at all, so `_actor` cannot even name the socket.
    unknown_peer: m.shortActor("peer=?"),
    // An unrecognized shape falls back whole rather than emptying the cell.
    unrecognized: m.shortActor("actor unrecorded"),
    absent: m.shortActor(undefined),
    blank: m.shortActor("   "),
  },
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

    def test_the_bearer_preset_ignores_stale_custom_fields(self):
        # The custom inputs stay in the DOM when the preset changes back, so a form
        # that read them regardless would register a descriptor the operator did not
        # choose — and a descriptor decides which header a credential is sent in.
        srv = self.probe["server"]
        self.assertEqual(srv["bearer"], srv["bearer_with_stale_custom"])
        self.assertEqual(srv["bearer"]["auth_header"], "Authorization")

    def test_a_none_descriptor_carries_no_header_fields(self):
        # `policy._auth_descriptor_error` refuses a 'none' descriptor carrying header
        # fields — "configuration that says two things at once" — so the form must not
        # build one. Asserted here because the backend's refusal would surface as a 400
        # the operator has to decode.
        self.assertEqual(self.probe["server"]["none"],
                         {"auth_type": "none", "auth_header": None,
                          "auth_template": None})

    def test_a_custom_descriptor_is_trimmed(self):
        # A trailing space in a header NAME is not a header the server will match, and
        # it is invisible in the field it was typed into.
        self.assertEqual(self.probe["server"]["custom"],
                         {"auth_type": "header", "auth_header": "X-Api-Key",
                          "auth_template": "{secret}"})

    def test_an_edit_echoes_the_descriptor_back(self):
        # THE trap in this endpoint. `ServerEditRequest` takes the TARGET state and
        # defaults `auth_type` to "none", so a body carrying only `enabled` does not
        # mean "leave auth alone" — it means "set it to none". Toggling a server off
        # and on would strip its credential descriptor, and the resulting 401 reads
        # like an expired token rather than like a UI bug.
        srv = self.probe["server"]
        for key, enabled in (("edit_enable", True), ("edit_disable", False)):
            with self.subTest(body=key):
                self.assertEqual(srv[key], {"enabled": enabled,
                                            "auth_type": "header",
                                            "auth_header": "Authorization",
                                            "auth_template": "Bearer {secret}"})

    def test_an_edit_on_a_server_with_no_descriptor_sends_none(self):
        # The other direction: absent auth must produce a well-formed 'none', not
        # undefined fields that serialize out of the body entirely.
        self.assertEqual(self.probe["server"]["edit_no_auth"],
                         {"enabled": False, "auth_type": "none",
                          "auth_header": None, "auth_template": None})

    def test_the_preview_refuses_a_name_that_is_not_a_dns_label(self):
        # The name becomes a path segment at the relay and a hostname at the gateway.
        # The backend validates too — this only keeps the form from offering a
        # registration that cannot succeed.
        srv = self.probe["server"]
        self.assertFalse(srv["preview_upper"]["ok"])
        self.assertFalse(srv["preview_traversal"]["ok"])
        self.assertTrue(srv["preview_single"], "a one-character label is legal")
        # Empty is not an error, it is nothing typed yet: no text at all.
        self.assertEqual(srv["preview_blank"], {"ok": False, "text": ""})

    def test_the_preview_names_the_secret_file_it_will_make_load_bearing(self):
        # Derived from the server name with no prefix added (#40). Showing the actual
        # filename is what lets an operator put the token in the right place first
        # time; describing it in prose is what got the path wrong for three files.
        self.assertIn("mcp-github.json", self.probe["server"]["preview_bearer"])
        self.assertNotIn("mcp-mcp-github.json", self.probe["server"]["preview_bearer"])

    def test_the_preview_says_registering_does_not_run_anything(self):
        # Registering is not enabling and enabling is not permitting. The page has to
        # say so, because "register" reads like "turn on" everywhere else.
        self.assertIn("disabled", self.probe["server"]["preview_none"])

    def test_a_half_filled_custom_descriptor_is_not_ok(self):
        # A `header` descriptor with no template builds a header with no credential,
        # and the upstream answer is a 401 — indistinguishable from an expired token.
        self.assertFalse(self.probe["server"]["preview_half_custom"]["ok"])

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

    def test_both_card_kinds_are_rendered(self):
        # The queue is ONE list over two builders, and the page draws both. If this
        # ever drops a kind the backend can raise, that surface's decisions are held
        # by governance and invisible to the human — which is the failure the single
        # merged stream exists to prevent, arriving by the other door.
        self.assertEqual(self.probe["kinds"]["both"], ["a", "b"])
        self.assertEqual(sorted(self.probe["kinds"]["known"]), ["egress", "tool"])

    def test_an_unknown_card_kind_is_dropped_rather_than_drawn(self):
        # A backend one step ahead of the page — in this repo, a container rebuilt
        # while a tab stayed open. Filtered once at the payload, so the count, the
        # announcement and the cards all read the same list: showing fewer cards than
        # exist is survivable, while claiming a number the list does not match is what
        # makes an operator believe they have cleared a queue they have not.
        self.assertEqual(self.probe["kinds"]["unknown_dropped"], ["a"])

    def test_a_card_with_no_kind_is_still_drawn(self):
        # The shape every payload had before the tool surface existed. Dropping it
        # would blank the queue of a page cached across that upgrade.
        self.assertEqual(self.probe["kinds"]["legacy_kept"], ["a"])
        self.assertEqual(self.probe["kinds"]["empty"], [])
        self.assertEqual(self.probe["kinds"]["absent"], [])

    def test_a_payload_is_folded_but_never_shortened(self):
        # The fold is about the QUEUE, not about the payload: a card whose arguments
        # run to pages pushes every other pending decision off the screen, and a
        # decision nobody scrolls to is one nobody makes. The whole string is in the
        # DOM either way — only the disclosure state changes — and the byte count is
        # on the summary so a folded card still says how much has not been read.
        tool = self.probe["tool"]
        self.assertTrue(tool["short"]["open"])
        self.assertFalse(tool["long"]["open"])
        self.assertTrue(tool["at_fold"])          # the fold is a ceiling, not a floor
        self.assertEqual(tool["long"]["bytes"], tool["fold"] + 1)
        self.assertIn(str(tool["fold"] + 1), tool["long"]["summary"])

    def test_a_missing_payload_is_zero_bytes_rather_than_a_crash(self):
        # A card that threw while building would take the whole queue's render with
        # it, which is a governance outage caused by one malformed row.
        self.assertEqual(self.probe["tool"]["missing"]["bytes"], 0)

    def test_a_tool_countdown_reads_its_own_deadline(self):
        # Absolute and per-card, so it counts down before /api/config has answered and
        # does not need the page to know what CONTROL_TOOL_HOLD_TIMEOUT is set to.
        tool = self.probe["tool"]
        self.assertEqual(tool["remaining_mid"], 600)
        self.assertEqual(tool["remaining_past"], 0)     # clamped, never negative
        self.assertIsNone(tool["remaining_unknown"])

    def test_an_allowed_tool_ask_does_not_claim_the_call_ran(self):
        # The gateway executes on RESUMPTION, when the agent comes back and claims the
        # approval. A message reading like a completed action would misreport the one
        # property that keeps an approved side effect from happening with nobody left
        # to receive it.
        tool = self.probe["tool"]
        self.assertIn("returns", tool["allowed"]["text"])
        self.assertEqual(tool["allowed"]["tone"], "ok")
        self.assertIn("not run", tool["denied"]["text"])
        self.assertEqual(tool["nothing"]["tone"], "bad")   # unknown fails to denied

    def test_a_new_tool_ask_is_announced_as_a_tool_and_not_as_a_host(self):
        # The live region is the only place a screen-reader user learns which decision
        # arrived. Before this, a tool ask read out as "an unnamed host": not merely
        # vague but a description of the wrong sort of thing, on the one surface where
        # the subject is what decides whether it needs attention now.
        tool = self.probe["tool"]
        self.assertIn("issue_write on mcp-github", tool["say_tool"])
        self.assertNotIn("host", tool["say_tool"])
        self.assertIn("example.com", tool["say_egress"])
        # A server-less ask still names the tool rather than falling back to a host.
        self.assertEqual(tool["say_tool_nameless"], "an unnamed tool")

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

    def test_the_lease_button_says_how_long_it_grants_for(self):
        lease = self.probe["lease"]
        # Derived from `/api/config`, never written into the markup: a button reading
        # "Allow for 30 min" on a store configured for five is the same class of lie
        # as a countdown that invents its own window.
        self.assertEqual(lease["label_default"], "Allow for 30 min")
        self.assertEqual(lease["label_five"], "Allow for 5 min")
        self.assertEqual(lease["label_hour"], "Allow for 1 h")
        self.assertEqual(lease["label_odd"], "Allow for 90 s")

    def test_the_lease_button_promises_no_number_it_does_not_know(self):
        lease = self.probe["lease"]
        # Before the first /api/config, and for anything unusable. It must not fall
        # back to a DEFAULT number — a wrong duration on a button that grants egress
        # is worse than a vague one, because the operator would have no reason to
        # doubt it.
        for case in ("label_unknown", "label_zero", "label_junk"):
            self.assertEqual(lease[case], "Allow for a while", case)

    def test_a_lease_counts_down_from_its_own_deadline(self):
        lease = self.probe["lease"]
        # The absolute instant comes from the backend, so this works with a stale
        # /api/config and needs no knowledge of the configured duration.
        self.assertEqual(lease["remaining_mid"], 1000)
        # Never negative: the backend serves only live leases, so a past deadline means
        # the clocks disagree rather than that the grant ran over.
        self.assertEqual(lease["remaining_past"], 0)
        self.assertIsNone(lease["remaining_unknown"])

    def test_a_lease_cell_reads_as_a_clock(self):
        lease = self.probe["lease"]
        self.assertEqual(lease["cell_minutes"]["text"], "30m 05s")
        self.assertEqual(lease["cell_seconds"]["text"], "9s")
        # A deadline the page cannot compute says so rather than showing "0s", which
        # would read as a grant about to lapse — the one thing an urgency signal must
        # never say when it does not know.
        self.assertEqual(lease["cell_unknown"]["text"], "unknown")
        self.assertFalse(lease["cell_unknown"]["urgent"])

    def test_a_lease_about_to_lapse_is_flagged_at_the_same_threshold_as_a_hold(self):
        # One threshold, so the same colour means one thing across the page.
        lease = self.probe["lease"]
        self.assertTrue(lease["cell_at_urgent"]["urgent"])
        self.assertFalse(lease["cell_outside_urgent"]["urgent"])

    def test_an_empty_lease_table_is_not_reported_as_a_fault(self):
        lease = self.probe["lease"]
        # No leases is the normal resting state of this table, so the sentence says
        # what that MEANS for the next request rather than that a table is short.
        self.assertTrue(lease["status_empty"]["show"])
        self.assertEqual(lease["status_empty"]["level"], "none")
        self.assertIn("held for approval", lease["status_empty"]["text"])

    def test_a_failed_lease_poll_says_the_table_may_be_wrong(self):
        lease = self.probe["lease"]
        # The rows stay — blanking a table whose subject is what is in force RIGHT NOW
        # would read as "nothing is granted", which is the reassuring reading and the
        # wrong one — so the staleness has to be stated instead.
        self.assertEqual(lease["status_stale"]["level"], "warn")
        self.assertIn("lapsed or been revoked", lease["status_stale"]["text"])
        self.assertIn("unreachable", lease["status_cold"]["text"])

    def test_a_healthy_lease_table_says_nothing(self):
        lease = self.probe["lease"]
        self.assertFalse(lease["status_quiet"]["show"])
        # And before the first response there is nothing to claim in either direction:
        # "none in force" here would be an all-clear the page has not earned.
        self.assertFalse(lease["status_before_first_load"]["show"])

    def test_a_provenance_cell_drops_the_forgeable_fields_and_keeps_the_addresses(self):
        """Found by using it: a live lease's `granted_by` ran to ~180 characters of
        Chrome version string in a middle column, pushing the revoke button off the
        table. `origin` and `ua` go — the two longest fields, and the two the client
        self-reports, so they are evidence to read in the trail rather than an answer
        to "who granted this"."""
        a = self.probe["actor"]
        self.assertEqual(a["full"], "peer=172.31.0.10 via-ui=172.18.0.1")
        self.assertNotIn("Chrome", a["full"])
        self.assertNotIn("origin", a["full"])

    def test_a_provenance_cell_keeps_the_labels_that_carry_the_trust_level(self):
        """Showing a bare address would be shorter and would misrepresent it. `peer` is
        the socket address this process observed and the caller cannot forge it;
        `via-ui` is the relay's ASSERTION about the browser behind it. That difference
        is why `_actor` labels its fields at all, so a cell reading `172.18.0.1` would
        present an assertion as a fact."""
        a = self.probe["actor"]
        self.assertTrue(a["full"].startswith("peer="))
        self.assertIn("via-ui=", a["full"])
        # Nothing asserted: peer alone, still labelled.
        self.assertEqual(a["peer_only"], "peer=172.31.0.9")
        self.assertEqual(a["unknown_peer"], "peer=?")

    def test_an_unrecognized_provenance_string_is_shown_whole(self):
        # A provenance cell that silently emptied itself would be worse than a wide
        # one; the CSS width cap catches whatever length arrives.
        a = self.probe["actor"]
        self.assertEqual(a["unrecognized"], "actor unrecorded")
        # And nothing at all reads as nothing recorded, not as a broken column.
        self.assertEqual(a["absent"], "—")
        self.assertEqual(a["blank"], "—")

    def test_sibling_hosts_fold_under_their_registrable_domain(self):
        g = self.probe["group"]["siblings"]
        self.assertEqual(g["count"], 1)
        self.assertTrue(g["grouped"])
        self.assertEqual(g["members"], 3)
        self.assertEqual(g["domain"], "example.com")
        # The group counts down by its soonest member, not by whichever row the backend
        # happened to return first.
        self.assertEqual(g["soonest"], 200)
        # And within the group, soonest first — so the row about to vanish is the one
        # nearest the summary line that says it is about to vanish.
        self.assertEqual(g["order"], [2, 1, 3])

    def test_a_lone_lease_is_not_a_group_of_one(self):
        # Grouping a single lease would make every one of them cost a click to read,
        # which is worse than the clutter the folding exists to fix.
        g = self.probe["group"]["lone"]
        self.assertEqual(g["count"], 1)
        self.assertFalse(g["grouped"])
        self.assertEqual(g["members"], 1)
        self.assertEqual(self.probe["group"]["threshold"], 2)

    def test_two_client_classes_are_never_folded_together(self):
        """The key is (class, domain), not domain. Two populations under one heading
        would imply they interact — the same mistake `api_rules` avoids by grouping the
        standing rules by class first. A lease for `sandbox` and one for `mcp` are two
        grants to two tenants, and one line for both would read as a single grant
        covering the pair."""
        g = self.probe["group"]["split_by_class"]
        self.assertEqual(g["count"], 2)
        self.assertEqual(sorted(g["classes"]), ["mcp", "sandbox"])
        # The class is IN the key, which is what makes the two groups distinct rather
        # than one group that happens to be rendered twice.
        for key, cls in zip(g["keys"], g["classes"]):
            self.assertTrue(key.startswith(f"{cls}|"), key)

    def test_groups_are_ordered_by_the_one_expiring_soonest(self):
        self.assertEqual(self.probe["group"]["order"],
                         ["sooner.com", "later.com"])

    def test_a_domain_is_the_two_label_suffix_and_carries_no_dot(self):
        g = self.probe["group"]
        self.assertEqual(g["domain_plain"], "example.com")
        self.assertEqual(g["domain_deep"], "example.com")
        self.assertEqual(g["domain_apex"], "example.com")
        self.assertEqual(g["domain_case"], "example.com")
        # No leading dot: nothing here is a pattern and nothing here grants, so the
        # wildcard marker `policy._persist_candidates` adds would be a lie.
        for key in ("domain_plain", "domain_deep", "domain_apex"):
            self.assertFalse(g[key].startswith("."), key)

    def test_a_host_with_no_domain_to_group_under_is_its_own_group(self):
        g = self.probe["group"]
        self.assertEqual(g["domain_single_label"], "localhost")
        self.assertEqual(g["domain_ipv4"], "10.1.2.3")
        self.assertEqual(g["domain_ipv6"], "2001:db8::1")

    def test_the_public_suffix_limitation_is_a_known_answer_here(self):
        """With no public-suffix list the two-label suffix of `example.co.uk` is
        `co.uk`. On the persist path that would be a grant far wider than it looks,
        which is why an operator picks the pattern and sees it verbatim. Here it can
        only put two rows under one heading, so the wrong answer costs an odd grouping
        and nothing else — asserted so that stays a decision rather than a discovery."""
        self.assertEqual(self.probe["group"]["domain_public_suffix"], "co.uk")

    def test_an_empty_or_absent_lease_list_groups_into_nothing(self):
        self.assertEqual(self.probe["group"]["empty"], [])
        self.assertEqual(self.probe["group"]["absent"], [])

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
        self.assertEqual(sat["ungrouped_load"], "12/16 requests held")
        self.assertEqual(sat["no_cards_field"], "12/16 requests held")

    def test_the_gauge_reports_whichever_cap_is_nearer_its_limit(self):
        """Two global caps, and cards are always <= waiters — so the fuller gauge is
        not always the same one. A fixed choice would hide the cap that is about to
        deny, which is the only thing this banner exists to say early."""
        sat = self.probe["saturation"]
        self.assertEqual(sat["cards_are_the_fuller_gauge"], "11/12 cards")

    def test_a_cap_of_zero_is_dropped_rather_than_divided_by(self):
        # Zero on a global cap means "refuse everything", which the rejection notice
        # reports properly. A gauge would render `5/0` or NaN.
        self.assertFalse(self.probe["saturation"]["zero_cap"]["show"])

    def test_the_scope_phrase_names_both_axes(self):
        """`last_scope` carries which cap fired as "<who> <what>", and both halves
        change what the operator should do: cards versus blocked workers is attention
        versus capacity, global versus per-client is "the plane is loaded" versus "one
        agent is hammering"."""
        phrases = dict((k, v) for k, v in self.probe["saturation"]["scopes"])
        self.assertEqual(phrases["global cards"], "the global card cap")
        self.assertEqual(phrases["global waiters"], "the global blocked-request cap")
        self.assertEqual(phrases["client 172.30.0.9 cards"],
                         "the per-client card cap for 172.30.0.9")
        self.assertEqual(phrases["client 172.30.0.9 waiters"],
                         "the per-client blocked-request cap for 172.30.0.9")
        # An absent or unrecognised scope shortens the sentence rather than inventing
        # a phrase around a word this does not know.
        self.assertEqual(phrases[""], "")
        self.assertEqual(phrases["nonsense"], "")

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
        self.assertIn("per-client card cap for 172.30.0.9", sat["recent"]["detail"])
        self.assertIn("the global blocked-request cap", sat["global_scope"])
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

    def test_a_decision_row_carries_which_client_population_asked(self):
        a = self.probe["saturation"]["audit"]
        # The address says which container; the class says which POPULATION, and the
        # class is what the rule was written for. An operator scanning this column
        # wants "agent or MCP server", not a fourth octet.
        self.assertEqual(a["classed"]["clientClassPrefix"], "mcp · ")
        self.assertEqual(a["classed"]["client"], "172.28.0.3")
        # Absent for a row that predates the column, or one the backend could not
        # place. Nothing is rendered rather than a guess: an invented label would be
        # a claim, and these rows are evidence.
        self.assertEqual(a["unclassed"]["clientClassPrefix"], "")
        # The address is still there — losing the class must not lose the row.
        self.assertEqual(a["unclassed"]["client"], "172.30.0.2")

    def test_the_class_prefix_carries_its_own_separator(self):
        # The `denyhttp` lesson again: a CSS margin gives the right pixels and the
        # wrong `textContent`, so a row copied into a ticket reads as one word. This
        # table exists to be quotable evidence.
        a = self.probe["saturation"]["audit"]
        self.assertTrue(a["classed"]["clientClassPrefix"].endswith(" · "))

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
        a security control either — `/authorize` is reachable only from authorize-net,
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

    def test_an_outage_is_not_reported_when_every_denial_was_policy(self):
        """The banner must stay silent in the ordinary case. A warning that is present
        during normal operation is one nobody reads during an abnormal one, and this
        list is mostly denials by design — default-deny means refusals are the
        expected content, not the alarming content."""
        o = self.probe["saturation"]["outage"]
        for case in ("none", "only_policy", "no_flag", "nothing"):
            with self.subTest(case=case):
                self.assertFalse(o[case]["show"])
                self.assertEqual(o[case]["text"], "")

    def test_an_outage_counts_requests_rather_than_rows(self):
        """The decisions view is GROUPED, so one line can stand for hundreds of refused
        requests. Counting lines would report `2` for an outage that denied fifteen
        things, and the scale is the whole point of saying anything."""
        o = self.probe["saturation"]["outage"]["grouped"]
        self.assertTrue(o["show"])
        self.assertEqual(o["level"], "warn")
        self.assertEqual(o["decisions"], 15)   # 12 + 3, not 2 rows
        self.assertEqual(o["hosts"], 2)        # the policy-denied host is not counted

    def test_the_outage_wording_agrees_with_itself_at_one(self):
        # A banner that says "1 requests were denied" reads as broken, and a reader who
        # doubts the sentence doubts the number in it.
        o = self.probe["saturation"]["outage"]["one"]
        self.assertIn("1 request to 1 host", o["text"])
        self.assertIn("was denied", o["text"])
        self.assertNotIn("requests", o["text"])
        self.assertNotIn("they were", o["text"])

    def test_the_outage_banner_says_it_is_not_policy(self):
        """The distinction IS the message. Without those words the banner is just a
        second count of denials, and the reader is left where they started: unable to
        tell a rule refusing traffic from governance being unreachable."""
        text = self.probe["saturation"]["outage"]["grouped"]["text"]
        self.assertIn("not policy", text)
        self.assertIn("could not reach the control plane", text)
        # Scoped to what was served — never a claim about the store, or about now.
        self.assertIn("below", text)

    def test_repeated_hosts_count_once(self):
        o = self.probe["saturation"]["outage"]["same_host"]
        self.assertEqual(o["hosts"], 1)
        self.assertEqual(o["decisions"], 7)

    def test_a_row_carries_the_marker_only_when_the_backend_sets_it(self):
        """Colour is not the only cue (the banner carries words), but the row edge is
        what ties the sentence to the specific lines — and it must default OFF so a
        backend that does not send the field renders exactly as it did before."""
        self.assertIs(self.probe["saturation"]["outage"]["row_marked"], True)
        self.assertIs(self.probe["saturation"]["outage"]["row_plain"], False)

    def test_revoking_an_allow_and_a_block_do_not_read_alike(self):
        """They are opposites. Revoking an allow TIGHTENS — the host reverts to
        unknown and the next request is held. Revoking a block LOOSENS: an explicit
        operator denial becomes a request that can then be approved, possibly by
        someone who never knew it had been deliberately refused. A single "delete
        this?" hides exactly that."""
        r = self.probe["saturation"]["revoke"]
        self.assertIn("no longer be allowed", r["allow_rule"]["text"])
        self.assertIn("no longer be blocked", r["block_rule"]["text"])
        self.assertFalse(r["allow_rule"]["danger"])
        self.assertTrue(r["block_rule"]["danger"])

    def test_the_dangerous_direction_says_it_can_then_be_allowed(self):
        # The consequence an operator is least likely to have in mind: removing a
        # block does not merely stop denying, it opens the host to approval.
        self.assertIn("can then be allowed",
                      self.probe["saturation"]["revoke"]["block_rule"]["text"])

    def test_both_say_the_consequence_rather_than_the_row(self):
        """`Delete .github.com?` asks about a table row. `Requests to .github.com
        will no longer be allowed` asks about the world, which is what the operator
        is actually deciding — the same discipline persistPreview follows."""
        r = self.probe["saturation"]["revoke"]
        for case in ("allow_rule", "block_rule"):
            with self.subTest(case=case):
                self.assertIn("held for approval", r[case]["text"])

    def test_a_seed_rule_is_not_revocable_and_says_where_to_change_it(self):
        """Shown WITH the reason rather than as a missing control, so nobody wonders
        whether the button failed to render. The backend refuses it too — this is the
        explanation, not the enforcement."""
        seed = self.probe["saturation"]["revoke"]["seed_rule"]
        self.assertFalse(seed["allowed"])
        self.assertIn("egress-allowlist.txt", seed["text"])

    def test_an_unrecognised_action_warns_as_the_dangerous_one(self):
        # Under-warning is the failure worth avoiding; an over-warned allow costs the
        # operator one extra sentence.
        r = self.probe["saturation"]["revoke"]
        self.assertTrue(r["unknown_action"]["danger"])
        self.assertTrue(r["nothing"]["danger"])

    # ── writing a rule with no held request behind it ────────────────────────

    def test_the_page_normalizes_a_pattern_exactly_as_the_backend_does(self):
        """Two implementations of one rule, in two languages, with nothing between
        them. They have to agree or the confirm step lies: it quotes what the page
        thinks will be stored, and the backend stores something else. `Example.COM.`
        and `example.com` are the same rule; `.example.com` and `example.com` are not,
        and the difference is one character of punctuation."""
        for typed, in_page in self.probe["saturation"]["normalize"]:
            with self.subTest(pattern=typed):
                self.assertEqual(in_page, cp.policy._normalize_pattern(typed))

    def test_the_wildcard_floor_is_the_same_number_on_both_sides(self):
        # The one piece of the grammar the page duplicates (see createPreview). A page
        # with a lower floor offers a grant the backend refuses; a higher one hides a
        # rule the operator is entitled to write.
        self.assertEqual(self.probe["saturation"]["wildcard_min_labels"],
                         cp.policy._WILDCARD_MIN_LABELS)

    def test_the_preview_says_the_consequence_and_names_the_class(self):
        """Same discipline as revokePreview: the world, not the row. And the class is
        in the sentence because a rule is the pattern AND the class — the same pattern
        written for two classes is two different rules with two different effects."""
        c = self.probe["saturation"]["create"]["exact_allow"]
        self.assertTrue(c["ok"])
        self.assertIn("sandbox", c["text"])
        self.assertIn("pypi.example", c["text"])
        self.assertIn("without being held for approval", c["text"])

    def test_the_preview_quotes_the_pattern_that_will_actually_be_stored(self):
        # The typed string and the stored one differ whenever normalization does
        # anything, and it is the stored one the confirm has to be about.
        c = self.probe["saturation"]["create"]["normalized"]
        self.assertEqual(c["pattern"], "pypi.example")
        self.assertIn("pypi.example", c["text"])

    def test_an_allow_is_the_flagged_direction_and_a_block_is_not(self):
        # An allow grants egress with no hold and no click; a block only tightens, and
        # is revocable. The asymmetry the whole endpoint is shaped around.
        create = self.probe["saturation"]["create"]
        self.assertTrue(create["exact_allow"]["danger"])
        self.assertFalse(create["exact_block"]["danger"])

    def test_a_wildcard_spells_out_the_subtree_rather_than_naming_it(self):
        """A leading dot is the entire grant and it looks like punctuation. `.example
        .com` covers hosts nobody has ever requested, which is precisely what an
        operator typing a domain into a box is least likely to be picturing."""
        c = self.probe["saturation"]["create"]["wildcard_allow"]
        self.assertTrue(c["wild"])
        self.assertIn("every subdomain", c["text"])
        self.assertIn("never been requested", c["text"])

    def test_a_single_label_wildcard_can_be_blocked_but_not_allowed(self):
        create = self.probe["saturation"]["create"]
        self.assertFalse(create["tld_allow"]["ok"])
        self.assertIn(".com", create["tld_allow"]["text"])
        self.assertTrue(create["tld_block"]["ok"])

    def test_a_conflicting_rule_is_reported_with_its_fix(self):
        # Nothing ADDED here replaces a rule, so the next step is to revoke or edit the
        # one in the way — and it is on screen already.
        c = self.probe["saturation"]["create"]["conflict"]
        self.assertFalse(c["ok"])
        self.assertTrue(c["conflict"])
        self.assertIn("Revoke", c["text"])

    def test_an_identical_rule_is_a_no_op_rather_than_an_error(self):
        # The backend answers 200 with created:false, which is easy to misread as a
        # write. Better not to make the call.
        c = self.probe["saturation"]["create"]["redundant"]
        self.assertFalse(c["ok"])
        self.assertTrue(c["redundant"])
        self.assertFalse(c["conflict"])

    def test_the_same_pattern_in_another_class_is_not_a_conflict(self):
        # Uniqueness is the PAIR. Treating this as a conflict would make one class's
        # policy unwritable because another's already covered the host.
        c = self.probe["saturation"]["create"]["other_class"]
        self.assertTrue(c["ok"])
        self.assertFalse(c["conflict"])

    def test_an_incomplete_form_previews_nothing_it_could_be_read_as_agreeing_to(self):
        create = self.probe["saturation"]["create"]
        self.assertFalse(create["nothing"]["ok"])
        self.assertEqual(create["nothing"]["text"], "")
        self.assertFalse(create["no_class"]["ok"])
        self.assertIn("client class", create["no_class"]["text"])

    def test_an_unrecognised_action_previews_as_the_safer_reading(self):
        # An over-warned block costs a sentence; an under-warned allow is the mistake
        # the confirm exists to prevent. Same rule persistPreview follows.
        c = self.probe["saturation"]["create"]["unknown_action"]
        self.assertEqual(c["verb"], "block")
        self.assertFalse(c["danger"])

    def test_an_edit_preview_states_both_halves_of_the_transition(self):
        """The only operation on this page that takes something away and gives something
        back in one click, so a preview naming just the end state describes half of what
        the operator is agreeing to."""
        e = self.probe["saturation"]["edit"]["narrowed"]
        self.assertTrue(e["ok"])
        self.assertIn(".example.com", e["text"])            # what it was
        self.assertIn("no longer be allowed", e["text"])
        self.assertIn("api.example.com", e["text"])         # what it becomes
        self.assertIn("without being held for approval", e["text"])

    def test_an_action_flip_is_previewed_not_refused_as_a_conflict(self):
        """A rule always holds its own pattern, so the conflict check has to exclude the
        row being edited — the frontend half of the backend's ``id<>?`` clause. Without
        it every action flip previews as a collision with itself."""
        e = self.probe["saturation"]["edit"]["flipped"]
        self.assertTrue(e["ok"])
        self.assertFalse(e["conflict"])
        self.assertIn("currently block", e["text"])

    def test_both_loosening_directions_are_flagged_not_only_the_new_action(self):
        """What makes this different from createPreview, which only has a new action to
        judge. Narrowing a BLOCK loosens — the hosts falling out from under it stop being
        denied — so the old action decides the warning as much as the new one."""
        edit = self.probe["saturation"]["edit"]
        self.assertTrue(edit["narrowed"]["danger"])         # ends as an allow
        self.assertTrue(edit["narrowed_block"]["danger"])   # stops blocking some hosts
        self.assertFalse(edit["tightened"]["danger"])       # allow -> block, strictly

    def test_an_edit_that_changes_nothing_is_reported_rather_than_sent(self):
        e = self.probe["saturation"]["edit"]["unchanged"]
        self.assertFalse(e["ok"])
        self.assertTrue(e["unchanged"])
        self.assertIn("Nothing to change", e["text"])

    def test_the_edit_preview_quotes_the_pattern_that_will_be_stored(self):
        e = self.probe["saturation"]["edit"]["normalized"]
        self.assertEqual(e["pattern"], "api.example.com")
        self.assertIn("api.example.com", e["text"])

    def test_the_wildcard_floor_applies_to_an_edit_too(self):
        # The same asymmetry, on the operation that can walk a narrow allow outward one
        # save at a time — which is the shape this floor exists to stop.
        edit = self.probe["saturation"]["edit"]
        self.assertFalse(edit["tld_allow"]["ok"])
        self.assertTrue(edit["tld_block"]["ok"])

    def test_an_edit_onto_another_rule_is_a_conflict_and_not_a_merge(self):
        edit = self.probe["saturation"]["edit"]
        self.assertFalse(edit["conflict"]["ok"])
        self.assertTrue(edit["conflict"]["conflict"])
        self.assertIn("Revoke", edit["conflict"]["text"])
        # And the same pattern in ANOTHER class is not one — uniqueness is the pair.
        self.assertTrue(edit["other_class"]["ok"])
        self.assertFalse(edit["other_class"]["conflict"])

    def test_a_seed_rule_previews_as_uneditable_with_the_reason(self):
        # The backend refuses it too; this is the explanation, not the control — the
        # same division revokePreview draws.
        e = self.probe["saturation"]["edit"]["seed"]
        self.assertFalse(e["ok"])
        self.assertIn("egress-allowlist.txt", e["text"])

    def test_a_rule_that_vanished_under_the_form_says_so(self):
        """Revoked in another tab while the form was open. The alternative is a disabled
        button with an empty preview, which reads as the page being broken."""
        e = self.probe["saturation"]["edit"]["gone"]
        self.assertFalse(e["ok"])
        self.assertIn("no longer in the table", e["text"])

    def test_an_incomplete_or_unrecognised_edit_previews_safely(self):
        edit = self.probe["saturation"]["edit"]
        self.assertFalse(edit["empty"]["ok"])
        self.assertEqual(edit["empty"]["text"], "")
        # Same rule the create form follows: an unknown action reads as the block.
        self.assertEqual(edit["unknown_action"]["verb"], "block")

    def test_the_list_says_when_it_is_a_window(self):
        """Forty rows silently stood for the whole record. Grouping made that worse,
        not better: counts on the rows read as an explanation of the volume, so the
        list looks complete precisely when it is least so."""
        c = self.probe["saturation"]["coverage"]["truncated"]
        self.assertTrue(c["show"])
        self.assertIn("15", c["text"])
        self.assertIn("1,200", c["text"])

    def test_coverage_compares_decisions_with_decisions(self):
        """Both numbers are RAW decisions. Comparing rows against decisions would be
        a ratio of unlike quantities — "2 of 1,200" for a list accounting for fifteen
        of them — and would read as truncation even where there was none."""
        self.assertEqual(
            self.probe["saturation"]["coverage"]["counts_decisions"], 40)

    def test_the_list_stays_quiet_when_it_shows_everything(self):
        """The ordinary state on a quiet system. A permanent "showing 15 of 15" is
        furniture, and furniture is what stops a real message being read."""
        c = self.probe["saturation"]["coverage"]
        for case in ("complete", "impossible", "empty"):
            with self.subTest(case=case):
                self.assertFalse(c[case]["show"])
                self.assertEqual(c[case]["text"], "")

    def test_a_missing_total_makes_no_claim(self):
        """A backend that does not send the field must not produce "0 of 0". The
        frontend also accepts a bare array from such a backend, so this is the state
        that pairs with it."""
        c = self.probe["saturation"]["coverage"]
        for case in ("no_total", "junk_total"):
            with self.subTest(case=case):
                self.assertFalse(c[case]["show"])
                self.assertIsNone(c[case]["total"])

    def test_a_filtered_view_measures_itself_against_the_matching_set(self):
        """"of 1,200 recorded decisions" under an active filter reads as the size of
        the store, which makes a narrowed view look like a shrunken record. The noun is
        the whole fix, and it comes from the BACKEND's answer rather than from the
        controls on screen — an older backend that ignored the parameters served an
        unfiltered list."""
        c = self.probe["saturation"]["coverage"]
        self.assertIn("matching", c["filtered_truncated"]["text"])
        self.assertNotIn("recorded", c["filtered_truncated"]["text"])
        self.assertIn("recorded", c["truncated"]["text"])

    def test_a_complete_filtered_view_says_so_instead_of_going_quiet(self):
        """The exception to "silent when it shows everything". Unfiltered, that silence
        is right — a permanent "12 of 12" is furniture. Under a filter the reader is
        looking at a short list they just narrowed, where silence is indistinguishable
        from truncation, so the completeness is worth one sentence."""
        c = self.probe["saturation"]["coverage"]
        self.assertTrue(c["filtered_complete"]["show"])
        self.assertIn("All 12 matching", c["filtered_complete"]["text"])
        # Nothing on screen means the status line owns the message (see auditStatus);
        # two sentences arguing about an empty list is noise.
        self.assertFalse(c["filtered_empty"]["show"])

    def test_an_empty_filtered_list_does_not_claim_an_empty_record(self):
        """The same empty-versus-stale discipline, one level in. "No decisions recorded
        yet" in front of a full store — because the operator searched a host that never
        asked for anything — reads as governance not running at all."""
        s = self.probe["saturation"]["audit_filtered_status"]
        self.assertIn("No decisions match", s["filtered_empty"]["text"])
        self.assertIn("record itself is not empty", s["filtered_empty"]["text"])
        self.assertIn("No decisions recorded yet", s["unfiltered_empty"]["text"])
        # A failed poll outranks the filter: it is the more urgent fact either way.
        self.assertIn("Could not refresh", s["filtered_failed"]["text"])
        self.assertFalse(s["filtered_has_rows"]["show"])

    def test_a_refused_filter_says_which_one_rather_than_blaming_the_transport(self):
        """The backend spends a 400 and a sentence saying which filter it refused and
        why (`_bad_filter`); this is the only place that sentence can reach the person
        who typed it. Reporting it as "could not refresh" sends them to look at the
        control plane for something the filter bar did — and the rows below are the
        PREVIOUS question's answer, which is the part silence gets wrong."""
        s = self.probe["saturation"]["audit_filtered_status"]
        # Verbatim, not paraphrased: the parameter and the value are the useful part.
        self.assertIn("since (5) must be before until (2)", s["refused"]["text"])
        self.assertTrue(s["refused"]["show"])
        self.assertEqual(s["refused"]["level"], "warn")
        # Outranks a stale poll, unlike the filtered-empty sentence above: a refusal is
        # the one state here the operator can fix from the controls on screen.
        self.assertIn("bad cursor", s["refused_over_failed"]["text"])
        # And absent, it changes nothing — every other state keeps its own wording.
        self.assertFalse(s["refused_no_body"]["show"])

    def test_the_time_window_is_arithmetic_on_a_clock_it_is_given(self):
        """A relative window, computed from a clock passed IN — which is what makes it
        assertable at all, and the same choice the hold countdown made."""
        w = self.probe["saturation"]["window"]
        self.assertIsNone(w["any"])
        self.assertEqual(w["hour"], 1704063600)
        self.assertEqual(w["day"], 1704067200 - 86400)
        self.assertEqual(w["week"], 1704067200 - 604800)
        # Whole seconds: a sub-second clock must not produce a different query string
        # for two identical clicks.
        self.assertEqual(w["whole_seconds"], w["hour"])

    def test_an_unusable_window_becomes_no_window_rather_than_a_wrong_one(self):
        # A bound derived from a junk clock would silently hide the record. "Any time"
        # is the safe direction: it shows more, not less.
        w = self.probe["saturation"]["window"]
        for case in ("unknown_preset", "no_clock", "junk_clock"):
            with self.subTest(case=case):
                self.assertIsNone(w[case])

    def test_the_window_presets_are_the_ones_the_page_offers(self):
        """Two ends, no compiler: an <option> value app.js does not know is a control
        that silently does nothing — the query goes out unfiltered and the table looks
        like an answer."""
        section = re.search(r'<select id="audit-window".*?</select>',
                            INDEX_HTML.read_text(), re.S)
        self.assertIsNotNone(section, "the time facet moved in index.html")
        offered = [v for v in re.findall(r'value="([^"]*)"', section.group(0)) if v]
        self.assertEqual(sorted(offered),
                         sorted(self.probe["saturation"]["window"]["presets"]))

    def test_a_filter_is_only_active_when_it_narrows_something(self):
        f = self.probe["saturation"]["filter_active"]
        self.assertFalse(f["nothing"])
        self.assertFalse(f["no_filter_object"])
        self.assertTrue(f["text"])
        self.assertTrue(f["decision"])
        self.assertTrue(f["window"])
        # Whitespace is not a filter, and an unknown preset narrows nothing — treating
        # either as active would relabel the whole view for no change in its contents.
        self.assertFalse(f["whitespace"])
        self.assertFalse(f["unknown_window"])

    def test_the_query_string_carries_exactly_what_was_asked_for(self):
        q = self.probe["saturation"]["query"]
        self.assertEqual(q["bare"], "limit=40")
        self.assertEqual(
            q["everything"],
            "limit=100&q=evil&decision=deny&since=1704063600"
            "&before=1704067200.5%3A42")
        self.assertEqual(q["trims"], "limit=40&q=evil")
        # An absent or unusable limit is omitted rather than sent as junk — the
        # backend's own default is the better answer than a guess.
        self.assertEqual(q["no_limit"], "q=x")
        self.assertEqual(q["junk_limit"], "")

    def test_free_text_is_encoded_before_it_reaches_the_query_string(self):
        """The search box takes anything, including strings pasted out of a URL. An
        unencoded `&` splits the query into parameters the backend never received as
        typed — so the operator's search silently means something else."""
        q = self.probe["saturation"]["query"]["hostile_text"]
        self.assertEqual(q, "limit=40&q=a%26b%3Dc%20%23x%2Fy")

    def test_the_page_cursor_survives_the_round_trip(self):
        """It is the position in the record. Mangled, the backend either refuses it or
        serves the newest page while the pager claims to be twelve pages back."""
        self.assertEqual(self.probe["saturation"]["query"]["cursor"],
                         "limit=5&before=1704067200.5%3A42")

    def test_an_event_row_identifies_the_request_two_ways(self):
        """The two shapes are alternatives, not columns: a plaintext request has a
        method and a URL, a CONNECT tunnel has neither and is identified by its port.
        Rendering both as columns would give every row two empty cells."""
        e = self.probe["saturation"]["event_row"]
        self.assertEqual(e["http"]["request"], "GET https://a.example/x")
        self.assertEqual(e["tunnel"]["request"], ":443 connect")
        # Neither recorded: an empty cell, not an invented one.
        self.assertEqual(e["bare"]["request"], "")
        self.assertEqual(e["nothing"]["request"], "")

    def test_both_views_shape_their_shared_columns_identically(self):
        """The reason `eventRow` is built ON `auditRow` rather than beside it. The two
        tables show the same five columns, and a second implementation of the stage
        prefix, the class prefix, the em-dash for a missing client or the fail-closed
        marker is one that can drift in a view nobody is currently looking at."""
        self.assertIs(self.probe["saturation"]["event_row"]["shares_shaping"], True)

    def test_the_pager_reports_row_numbers_not_a_page_count(self):
        """"decisions 101 to 200 of 4,301" says where the reader is in the record;
        "page 2" needs the page size before it means anything. Exact, because every
        page but the last is full by construction."""
        p = self.probe["saturation"]["pager"]
        self.assertEqual((p["first_of_many"]["from"], p["first_of_many"]["to"]), (1, 100))
        self.assertEqual((p["second_of_many"]["from"], p["second_of_many"]["to"]),
                         (101, 200))
        # The range separator is an EN DASH, written as an escape here because ruff
        # rejects the raw character in a string as confusable with a hyphen.
        self.assertIn("101\u2013200", p["second_of_many"]["text"])
        self.assertIn("4,301", p["second_of_many"]["text"])
        # A partial last page reports what it actually holds.
        self.assertEqual((p["last"]["from"], p["last"]["to"]), (201, 240))

    def test_the_pager_offers_only_the_directions_that_exist(self):
        """`older` follows the CURSOR, not arithmetic on the total: the record takes an
        insert on every governed request, so a page derived from the two would disagree
        with the record while it was being read."""
        p = self.probe["saturation"]["pager"]
        self.assertTrue(p["first_of_many"]["older"])
        self.assertFalse(p["first_of_many"]["newer"])
        self.assertTrue(p["second_of_many"]["newer"])
        self.assertFalse(p["last"]["older"])
        for k in ("older", "newer"):
            self.assertFalse(p["only_page"][k])
            self.assertFalse(p["junk"][k])

    def test_the_pager_says_nothing_about_an_empty_page(self):
        # The list's own status line already says whether that is an empty record or an
        # unmatched filter; a "0 to 0" beside it would be a second sentence arguing
        # with the first.
        p = self.probe["saturation"]["pager"]
        self.assertEqual(p["empty"]["text"], "")
        self.assertEqual(p["junk"]["text"], "")
        # And it says "matching" for the same reason the coverage line does.
        self.assertIn("matching", p["filtered"]["text"])
        # A backend without the total still gets a usable label.
        self.assertIn("Decisions 1\u20135", p["no_total"]["text"])
        self.assertNotIn(" of ", p["no_total"]["text"])

    def test_an_arriving_approval_is_announced_with_its_host(self):
        """The host IS the decision. "One approval pending" says something is
        waiting; it does not say whether the agent wants the package registry or an
        address nobody recognises, which is the question being asked."""
        a = self.probe["saturation"]["announce"]
        self.assertIn("github.com", a["one"])
        self.assertIn("github.com", a["one_of_many"])
        self.assertIn("3 pending", a["one_of_many"])
        self.assertIn("2 new approvals", a["several"])

    def test_nothing_is_announced_when_nothing_arrived(self):
        """ARRIVALS only. A live region that spoke on every push would read the
        countdown aloud once a second for the life of every hold — which is why this
        is a separate element from the card list rather than an attribute on it."""
        a = self.probe["saturation"]["announce"]
        self.assertEqual(a["nothing"], "")
        self.assertEqual(a["no_list"], "")

    def test_an_unnamed_host_still_announces(self):
        # Degrades to a sentence rather than to "Approval needed for undefined."
        self.assertIn("unnamed host", self.probe["saturation"]["announce"]["nameless"])

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


def _fn_body(src: str, signature: str) -> str:
    """The body of a two-space-indented function inside `start()`.

    The row templates moved out of `refreshAudit` when the decisions view grew a
    second table, so the source guards below name the RENDERER they are about — which
    also means each of them can be asserted for both tables rather than for whichever
    one the poll happened to inline."""
    m = re.search(rf"function {re.escape(signature)}\s*\{{(.*?)\n  \}}", src, re.S)
    if m is None:
        raise AssertionError(f"{signature} not found in app.js — renamed? The source "
                             f"guards below cannot assert a renderer they cannot find, "
                             f"and would otherwise pass by looking at nothing.")
    return m.group(1)


class DecisionsTableSourceTests(unittest.TestCase):
    """`refreshAudit` and the two row renderers live in `start()` and cannot be
    unit-tested, so the parts of them that would fail SILENTLY are asserted against
    the source — the same approach the dismiss handler and the duplicate badge use.

    `self.body` is the poll; `self.rows` and `self.events` are the folded and the raw
    row templates. The shared claims are asserted for BOTH, because the two views
    render the same five columns and the whole reason `eventRow` builds on `auditRow`
    is that they must not be able to disagree."""

    def setUp(self):
        self.src = APP_JS.read_text()
        self.body = re.search(r"async function refreshAudit\(\)\s*\{(.*?)\n  \}",
                              self.src, re.S)
        self.assertIsNotNone(self.body, "refreshAudit not found — renamed?")
        self.rows = _fn_body(self.src, "renderGrouped(rows)")
        self.events = _fn_body(self.src, "renderEvents(rows)")

    def test_the_decisions_table_stamps_the_date_not_only_the_time(self):
        # Forty rows routinely span midnight, and a time-only stamp makes them read
        # as out of order at exactly the moment ordering matters. WHICH formatter this
        # render calls is not reachable from a unit test; the formatters themselves are
        # (see the FormatterTests above), so only the call site needs guarding.
        for view, body in (("folded", self.rows), ("record", self.events)):
            with self.subTest(view=view):
                self.assertIn("fmtStamp(", body)
                self.assertNotIn("fmtTime(", body)

    def test_the_row_carries_the_unambiguous_instant_as_well(self):
        # The visible stamp is LOCAL and states no offset, which is fine on the
        # operator's own screen and not fine once a row is correlated against
        # `make logs-cp` or pasted into an advisory.
        for view, body in (("folded", self.rows), ("record", self.events)):
            with self.subTest(view=view):
                self.assertRegex(body, r'title="\$\{esc\(fmtInstant\(')

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
        # `<tr[^>]*>` rather than `<tr>`: the row carries a conditional `outage`
        # class now. The attribute region is asserted separately just below, so
        # loosening this does not let arbitrary markup onto the row unnoticed.
        row = re.search(r"return `\s*<tr([^>]*)>(.*?)</tr>`", self.rows, re.S)
        self.assertIsNotNone(row, "the row template was restructured")
        self.assertRegex(
            row.group(1),
            r"""^\$\{a\.failClosed \? ' class="outage"' : ""\}$""",
            "the only thing on the row element itself is the outage marker")
        cells = row.group(2).split("<td")[1:]
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
        # The class qualifies the CLIENT, the same way the stage qualifies the host —
        # it is not a sixth column (asserted above) and it must not drift onto the
        # decision cell, which stays uniform for vertical scanning.
        self.assertIn("a.clientClassPrefix", client)
        self.assertNotIn("clientClassPrefix", decision)
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
        self.assertIn('esc(" " + a.repeat)', self.rows)
        self.assertIn("esc(` · first seen ${fmtStamp(a.firstTs)}`)", self.rows)
        # fmtStamp, not fmtTime: a group's span can cover days (the scan behind it is
        # bounded by event count, not by a window), so a bare time reads as today.
        self.assertNotIn("fmtTime(a.firstTs)", self.rows)
        # The record view folds nothing, so a repeat count there would be a "1x" on
        # every row — the annotation exists to mark the exception, not the rule.
        self.assertNotIn("a.repeat", self.events)

    def test_the_client_column_is_rendered_and_escaped(self):
        for view, body in (("folded", self.rows), ("record", self.events)):
            with self.subTest(view=view):
                self.assertIn("esc(a.client)", body)
        # Scoped to the decisions SECTION, not the first <thead> in the file — the
        # policy table also has one, and a reordering of the two sections would
        # otherwise silently point this assertion at the wrong table.
        section = re.search(r'<section id="view-decisions".*?</section>',
                            INDEX_HTML.read_text(), re.S)
        self.assertIsNotNone(section, "the decisions section was renamed")
        self.assertIn("<th>client</th>", section.group(0),
                      "the column exists in the body but has no header")

    def test_the_record_table_body_agrees_with_its_header(self):
        """Same guard as the folded table above, for the view that carries the request.
        A header/body mismatch here shifts every cell one place left, which puts the
        URL under `client` — and an audit row that misattributes a request is worse
        than one that fails to render."""
        section = re.search(r'<table id="audit-events-table".*?</table>',
                            INDEX_HTML.read_text(), re.S)
        self.assertIsNotNone(section, "the record table was renamed")
        headers = [h.strip() for h in re.findall(r"<th>(.*?)</th>", section.group(0))]
        self.assertEqual(headers, ["time", "decision", "host", "client", "request",
                                   "reason"])
        row = re.search(r"return `\s*<tr([^>]*)>(.*?)</tr>`", self.events, re.S)
        self.assertIsNotNone(row, "the record row template was restructured")
        cells = row.group(2).split("<td")[1:]
        self.assertEqual(len(cells), len(headers),
                         "the record table body and header disagree on cell count")
        # The request cell is the fifth, and it is escaped — this is the one
        # agent-controlled unbounded string the page renders outside an approval card.
        self.assertIn("esc(a.request)", cells[4])
        self.assertIn("a.stagePrefix", cells[2])
        self.assertIn("a.clientClassPrefix", cells[3])

    def test_the_policy_table_body_agrees_with_its_header(self):
        """Same guard as the decisions table above, for the table that shows STANDING
        policy. It matters more here since the class column landed: without it two
        rules for one pattern with opposite actions read as a contradiction rather
        than as two scoped rules, and a header/body mismatch would shift every cell
        one place left — putting the class under `source` and reading as ordinary
        data rather than as a broken table."""
        html = INDEX_HTML.read_text()
        section = re.search(r'<section id="view-policy".*?</section>', html, re.S)
        self.assertIsNotNone(section, "the policy section was renamed")
        headers = re.findall(r"<th>(.*?)</th>", section.group(0), re.S)
        self.assertEqual([h.strip() for h in headers],
                         ["action", "pattern", "matches", "for", "source", "added",
                          ""])
        # The row template is built in `refreshRules`, and the `for` cell has to be
        # the fourth — between the wildcard scope and the source.
        body = re.search(r"document\.getElementById\(\"rules\"\)\.innerHTML"
                         r".*?return `<tr>(.*?)</tr>`", APP_JS.read_text(), re.S)
        self.assertIsNotNone(body, "the policy row template was restructured")
        cells = body.group(1).split("<td")[1:]
        self.assertEqual(len(cells), len(headers),
                         "the policy table body and header disagree on cell count")
        self.assertIn("r.client_class", cells[3])
        self.assertIn("esc(r.source)", cells[4])


class RecordViewWiringSourceTests(unittest.TestCase):
    """The filter and paging wiring lives in `start()`, and each property below fails
    SILENTLY if it is dropped — the page keeps rendering a table of real decisions,
    which is exactly what makes a wrong one hard to notice."""

    def setUp(self):
        self.src = APP_JS.read_text()
        self.refresh = re.search(r"async function refreshAudit\(\)\s*\{(.*?)\n  \}",
                                 self.src, re.S)
        self.assertIsNotNone(self.refresh, "refreshAudit not found — renamed?")

    def test_a_filter_change_resets_the_page_position(self):
        """A cursor is a position in ONE result set. Kept across a filter change it
        points into a list that no longer exists, so the operator lands on an arbitrary
        page of the thing they just narrowed — with rows on screen, which reads as an
        answer rather than as a bug."""
        changed = re.search(r"function filtersChanged\(immediate\)\s*\{(.*?)\n  \}",
                            self.src, re.S)
        self.assertIsNotNone(changed, "filtersChanged not found — renamed?")
        self.assertIn("resetPaging()", changed.group(1))
        # Every control goes through it, including the mode switch: the two views page
        # differently, so carrying a cursor across the switch is the same mistake.
        for control in ("auditQEl", "auditDecisionEl", "auditWindowEl", "auditEveryEl"):
            with self.subTest(control=control):
                self.assertRegex(self.src, rf"{control}[^;]*?filtersChanged\(",
                                 f"{control} must go through filtersChanged, or its "
                                 f"change leaves the page position stale")

    def test_the_record_view_sends_the_cursor_it_is_paging_with(self):
        # Without `before`, "older" re-fetches page 1: the buttons work, the table
        # changes nothing, and the pager's label says the reader has moved.
        self.assertRegex(self.refresh.group(1),
                         r"before:\s*events\s*\?\s*auditCursors\[auditPage - 1\]")

    def test_a_page_past_the_end_of_the_record_snaps_back(self):
        """`make audit-prune` deletes rows a cursor still points at, so an empty page is
        reachable with nobody doing anything wrong — and it renders as "nothing here"
        for a record that is not empty, which is the one claim this view must never
        make."""
        self.assertRegex(
            self.refresh.group(1),
            r"if \(events && !rows\.length && auditPage > 0\) \{\s*\n\s*resetPaging\(\);")

    def test_clearing_the_filters_leaves_the_view_switch_alone(self):
        """"Clear filters" clears filters. The glance/record switch selects WHICH record
        the filters narrow, so resetting it would answer a question nobody asked — and
        would silently drop an operator out of the history they were reading."""
        handler = re.search(r"auditClearEl\.addEventListener\(.*?\n  \}\);",
                            self.src, re.S)
        self.assertIsNotNone(handler, "the clear handler moved")
        self.assertNotIn("auditEveryEl", handler.group(0))

    def test_the_two_views_are_fetched_from_two_literal_paths(self):
        """The relay allowlist is matched against the literal each `fetch` begins with,
        so a path assembled from a ternary is one the cross-file guard cannot see — and
        that guard is the only thing between a new route and a 403 an operator finds by
        loading the page."""
        for path in ("/api/audit?", "/api/audit/events?"):
            with self.subTest(path=path):
                self.assertIn(f"fetch(`{path}", self.refresh.group(1))

    def test_only_one_of_the_two_truncation_stories_is_told_at_a_time(self):
        # The glance truncates and says so with the coverage line; the record pages and
        # says so with the pager. Showing both would have them contradict each other,
        # since they measure different things.
        body = self.refresh.group(1)
        self.assertRegex(body, r"renderCoverage\(events \?")
        self.assertRegex(body, r"auditPagerEl\.hidden = !events")


class PollGatingSourceTests(unittest.TestCase):
    """The poll wiring lives at the bottom of `start()` and cannot be unit-tested, so
    the properties that would fail silently are asserted against the source.

    Silently is the operative word for all three: a poller that stops in a hidden tab
    and never resumes looks exactly like a quiet system; an ungated poller costs a
    COUNT(*) every four seconds forever and shows no symptom at all; and a gated SSE
    stream would drop approval arrivals while a hold burns its ~120s fuse, surfacing
    only as a request the operator never saw and the agent was denied for."""

    def setUp(self):
        self.src = APP_JS.read_text()

    def test_both_polls_are_gated_on_tab_visibility(self):
        for poll in ("refreshAudit", "refreshRules"):
            with self.subTest(poll=poll):
                self.assertRegex(
                    self.src,
                    r"setInterval\(\(\) => \{ if \(visible\(\)\) " + poll + r"\(\); \}",
                    f"{poll} must not poll a tab nobody is looking at")

    def test_the_approvals_stream_is_not_gated(self):
        """Deliberately the exception. A hold blocks the agent and default-denies when
        its window elapses, so arrivals must keep landing whether or not the tab is in
        front — the title prefix is how they get noticed. Asserted because "gate the
        pollers" reads like advice that ought to apply to everything."""
        self.assertRegex(self.src, r"\n  connect\(\);",
                         "the stream is no longer started unconditionally")
        self.assertNotRegex(self.src, r"if \(visible\(\)\) connect\(\)")

    def test_returning_to_the_tab_refreshes_immediately(self):
        """Without this the operator faces up to four seconds of stale data at exactly
        the moment their attention returns — and unlabelled stale, because the
        staleness wording is for a FAILED poll, not a skipped one.

        Asserted as "every gated poll appears in the handler" rather than as the
        handler's exact text. The literal form was the previous shape and it made
        adding a third polled list look like a regression in this test rather than the
        omission it would actually be — which is backwards for a guard whose job is to
        notice a poll that was left out."""
        handler = re.search(r'addEventListener\("visibilitychange"[\s\S]*?\}\);',
                            self.src)
        self.assertIsNotNone(handler, "the visibilitychange handler is gone")
        # The polls that are SKIPPED while hidden, so returning has to catch each of
        # them up. Derived from the source rather than listed, so a fourth one cannot
        # be added to the intervals and forgotten here.
        gated = set(re.findall(r"if \(visible\(\)\) (\w+)\(\); \}, \d+\);", self.src))
        self.assertTrue(gated, "no visibility-gated polls found — did they change?")
        for fn in sorted(gated):
            self.assertIn(f"{fn}()", handler.group(0),
                          f"{fn} is skipped while the tab is hidden but not caught up "
                          f"when it comes back")

    def test_an_unknown_visibility_state_keeps_polling(self):
        # Fail toward the OLD behaviour: a host without the API must not silently stop
        # updating the page.
        self.assertRegex(
            self.src, r'visible = \(\) => document\.visibilityState !== "hidden"')


class LeaseTableSourceTests(unittest.TestCase):
    """`refreshLeases` is the third polled list, and it inherits the trap the other two
    each fell into once: a failure path that blanks the table or says nothing.

    It matters more here than on either of them. This table's subject is what is being
    allowed AT THIS MOMENT, so an empty one reads as "nothing is granted" — the
    reassuring answer, and the wrong one when what actually happened is that the page
    stopped being able to tell."""

    def setUp(self):
        self.src = APP_JS.read_text()
        self.body = re.search(r"async function refreshLeases\(\)\s*\{(.*?)\n  \}",
                              self.src, re.S)
        self.assertIsNotNone(self.body, "refreshLeases not found — renamed?")

    def test_a_failed_refresh_keeps_the_rows_and_reports_the_staleness(self):
        body = self.body.group(1)
        self.assertIn("leasesFailed = true", body)
        self.assertIn("renderLeasesStatus(", body)
        self.assertNotRegex(body, r'catch[^}]*innerHTML\s*=\s*""',
                            "a failed poll must not blank the leases table")
        self.assertNotRegex(
            body, r"catch\s*\([^)]*\)\s*\{\s*/\*[^*]*\*/\s*\}",
            "the failure path is a comment, not a reported state")

    def test_a_non_ok_response_is_a_failure_not_a_row_of_json(self):
        self.assertRegex(self.body.group(1), r"if\s*\(!res\.ok\)\s*throw")

    def test_a_recovered_poll_clears_the_warning_and_re_renders(self):
        _, sep, success = self.body.group(1).partition("return;\n    }")
        self.assertTrue(sep, "the failure path no longer returns early")
        self.assertIn("leasesFailed = false", success)
        self.assertIn("renderLeasesStatus(", success)

    def test_the_countdowns_do_not_depend_on_the_poll(self):
        """The rows carry their own absolute deadline so the one-second tick can rewrite
        just the countdown cells. Rebuilding the table on that tick instead would drop a
        click landing on a revoke button at the moment it fired — which is exactly when
        an operator is most likely to be pressing one."""
        tick = re.search(r"function updateLeaseCountdowns\(\)\s*\{(.*?)\n  \}",
                         self.src, re.S)
        self.assertIsNotNone(tick, "updateLeaseCountdowns not found — renamed?")
        body = tick.group(1)
        self.assertIn("dataset.expires", body)
        self.assertNotIn("innerHTML", body)
        self.assertNotIn("fetch(", body)

    def test_a_folded_group_hides_its_rows_rather_than_omitting_them(self):
        """Expanding has to be a flip on rows already in the DOM. Rendering members only
        when open would make the toggle depend on the next four-second poll to produce
        them, so a click could be undone by a fetch already in flight — the same class
        of bug as rebuilding the table on the one-second tick."""
        # Every member is emitted; `open` only decides the `hidden` attribute on it.
        body = self.body.group(1)
        self.assertRegex(body, r"g\.leases\.map\(r => leaseRow\(")
        self.assertNotRegex(body, r"open\s*\?\s*g\.leases\.map",
                            "members must be rendered and hidden, not conditionally "
                            "rendered")
        row = re.search(r"function leaseRow\(([^)]*)\)\s*\{(.*?)\n  \}",
                        self.src, re.S)
        self.assertIsNotNone(row, "leaseRow not found — renamed?")
        self.assertIn("lease-member", row.group(2))
        self.assertRegex(row.group(2), r'!open\s*\?\s*"hidden"')

    def test_expansion_survives_the_poll(self):
        """The table is replaced wholesale every four seconds, so expansion state held
        in the DOM alone would collapse itself on the next tick. It lives in a Set
        outside the render, and the render reads it back."""
        self.assertIn("const expandedLeaseGroups = new Set()", self.src)
        self.assertIn("expandedLeaseGroups.has(g.key)", self.body.group(1))

    def test_a_lapsed_group_does_not_keep_its_expansion(self):
        # The key is (class, domain), which outlives the leases under it: without the
        # prune, a domain leased again half an hour later would silently come back
        # expanded because a previous group with the same key had been opened.
        body = self.body.group(1)
        self.assertIn("expandedLeaseGroups.delete(key)", body)

    def test_the_count_in_the_header_counts_grants_not_rows(self):
        # Folding four hosts into one line must not make the number shrink: the header
        # answers "how much is granted right now", not "how tall is this table".
        self.assertIn("leasesCountEl.textContent = rows.length", self.body.group(1))

    def test_the_full_provenance_survives_in_the_cell_title(self):
        """The shortener is a RENDERING. The stored `granted_by` is evidence and stays
        whole in the store, in the audit reason, and in this cell's tooltip — so
        nothing an operator might need to quote is only in the abbreviated form."""
        row = re.search(r"function leaseRow\(([^)]*)\)\s*\{(.*?)\n  \}",
                        self.src, re.S)
        self.assertIsNotNone(row, "leaseRow not found — renamed?")
        body = row.group(2)
        self.assertRegex(body, r'title="\$\{esc\(r\.granted_by')
        self.assertIn("shortActor(r.granted_by)", body)
        # And it is NOT in a `.ts` cell — that class is `white-space: nowrap` with no
        # width cap, which is what let the string widen the whole table.
        self.assertNotRegex(body, r'<td class="ts">\$\{esc\(r\.granted_by')

    def test_a_group_summary_offers_no_bulk_revoke(self):
        """Ending four grants with one click is a sharper action than the per-lease
        revoke and would need the confirm step this table deliberately does not have.
        Revocation stays on the row that names the thing being revoked."""
        summary = re.search(r'const summary = `<tr class="lease-group">(.*?)`;',
                            self.src, re.S)
        self.assertIsNotNone(summary, "the group summary row is gone — renamed?")
        self.assertNotIn("class=\"revoke\"", summary.group(1))
        self.assertNotIn("data-lease", summary.group(1))

    def test_the_lease_button_is_one_click_and_not_a_confirm_step(self):
        """A persist opens the confirm panel because it names a PATTERN and nothing in
        this UI removes a rule. A lease chooses nothing, expires on its own, and is
        revocable from the table below — so the second click would be friction with no
        question behind it. Asserted because the obvious "safer" edit is to route it
        through `askPersist` like its neighbours, which would then 400: the confirm
        panel sends a `pattern` the lease path does not accept."""
        self.assertRegex(
            self.src,
            r"action\.endsWith\(\"persist\"\)\s*\n\s*\?\s*askPersist\(a, action\)")
        self.assertNotRegex(self.src, r"askPersist\(a, \"allow_lease\"\)")


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


class CreateRuleSourceTests(unittest.TestCase):
    """The add-rule submit handler lives in `start()`, and three of its lines fail in
    ways nothing else here would catch: a form that appears broken, a rule that is not
    the one confirmed, and a picker offering classes that cannot be written to."""

    def setUp(self):
        self.src = APP_JS.read_text()
        self.body = re.search(
            r"ruleFormEl\.addEventListener\(\"submit\".*?\n  \}\);", self.src, re.S)
        self.assertIsNotNone(self.body, "the add-rule submit handler moved — renamed?")

    def test_the_submit_is_always_intercepted(self):
        """The page's CSP sends `form-action 'none'`, so a native submit is refused by
        the browser. That makes the missing `preventDefault` invisible in every other
        test and total in a real one: the click does nothing, silently, and the form
        reads as broken rather than as blocked."""
        self.assertIn("ev.preventDefault()", self.body.group(0))
        self.assertIn("form-action 'none'",
                      (ROOT / "control-plane-ui" / "app.py").read_text())

    def test_the_body_carries_the_pattern_that_was_confirmed(self):
        # `p.pattern` is normalized; the input box is not. Sending the raw value would
        # store a rule the confirm never described — which is the whole failure the
        # preview exists to prevent, reintroduced one line later.
        body = self.body.group(0)
        self.assertRegex(body, r"pattern:\s*p\.pattern")
        self.assertNotRegex(body, r"pattern:\s*rulePatternEl\.value")

    def test_a_write_that_wrote_nothing_is_not_reported_as_a_write(self):
        # The backend answers 200 with created:false when the same rule is already in
        # force. Collapsing that into success is the same mistake the approval cards
        # made before they learned to say "already in place".
        self.assertIn("body.created === false", self.body.group(0))

    def test_the_class_picker_is_filled_from_the_backend(self):
        """Not from the rules table, which is the tempting source because it is already
        loaded: a class with no rules yet would be missing from the picker, and that is
        exactly the class an operator needs to write the FIRST rule for."""
        config = re.search(r"async function refreshConfig\(\)\s*\{(.*?)\n  \}",
                           self.src, re.S)
        self.assertIsNotNone(config, "refreshConfig not found — renamed?")
        self.assertIn("c.client_classes", config.group(1))
        options = re.search(r"function renderClassOptions\(\)\s*\{(.*?)\n  \}",
                            self.src, re.S)
        self.assertIsNotNone(options, "renderClassOptions not found — renamed?")
        self.assertIn("clientClasses.map", options.group(1))
        self.assertNotIn("rulesById", options.group(1))


class EditRuleSourceTests(unittest.TestCase):
    """The edit path lives in `start()` too, and it has three failure modes the pure
    ``editPreview`` tests cannot reach — all of them the kind that leaves the page
    looking like it worked."""

    def setUp(self):
        self.src = APP_JS.read_text()
        self.body = re.search(r"async function submitEdit\(p\)\s*\{.*?\n  \}",
                              self.src, re.S)
        self.assertIsNotNone(self.body, "the edit submit handler moved — renamed?")

    def test_the_body_carries_the_pattern_that_was_confirmed(self):
        # The create path's twin, and the same failure: `p.pattern` is normalized and
        # the input box is not, so sending the raw value stores a rule the confirm never
        # described.
        body = self.body.group(0)
        self.assertRegex(body, r"pattern:\s*p\.pattern")
        self.assertNotRegex(body, r"pattern:\s*rulePatternEl\.value")

    def test_the_client_class_is_never_sent(self):
        """The backend has no such field, so sending one would be silently ignored —
        which is worse than an error, because the UI would appear to offer a re-scope
        that never happens.

        Scoped to the REQUEST body: the success notice reads `body.client_class` back
        off the response to say who the rule decides for, which is the opposite of
        sending one."""
        sent = re.search(r"body:\s*JSON\.stringify\(\{(.*?)\}\)",
                         self.body.group(0), re.S)
        self.assertIsNotNone(sent, "the edit request body moved — restructured?")
        self.assertNotIn("client_class", sent.group(1))

    def test_an_edit_that_wrote_nothing_is_not_reported_as_a_write(self):
        self.assertIn("body.changed === false", self.body.group(0))

    def test_a_config_poll_cannot_unlock_the_class_picker_mid_edit(self):
        """`renderClassOptions` re-enables every control from the config poll, which runs
        on a timer. Without the guard it re-enables the picker under an open edit, and
        the operator can select a class the edit will not apply — the form saying one
        thing while the request says another."""
        options = re.search(r"function renderClassOptions\(\)\s*\{(.*?)\n  \}",
                            self.src, re.S)
        self.assertIsNotNone(options, "renderClassOptions not found — renamed?")
        self.assertRegex(options.group(1),
                         r"editingRuleId !== null\)\s*ruleClassEl\.disabled = true")

    def test_the_row_is_re_read_on_every_preview_rather_than_captured(self):
        """The form holds an id, not a row. Capturing the row at entry would preview
        against a rule that may have been revoked or changed by a poll since — and then
        write over whatever replaced it."""
        current = re.search(r"function currentPreview\(\)\s*\{(.*?)\n  \}",
                            self.src, re.S)
        self.assertIsNotNone(current, "currentPreview not found — renamed?")
        self.assertIn("rulesById.get(editingRuleId)", current.group(1))


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

    def _appended(self, fn):
        """The identifiers one card builder appends, in order.

        Scoped to a NAMED function rather than to the first `el.append` in the file,
        which is what this used to do — and what broke the moment a second card
        builder existed. The looser version did not report the tool card; it reported
        the egress card's badge as missing, which is the failure mode a source-text
        guard is worst at explaining."""
        body = re.search(rf"function {fn}\(a\) \{{(.*?)\n  \}}", self.src, re.S)
        self.assertIsNotNone(body, f"{fn} not found — did it get renamed?")
        appended = re.search(r"el\.append\((.*?)\);", body.group(1), re.S)
        self.assertIsNotNone(appended, f"{fn} never appends its parts")
        return [p.strip() for p in appended.group(1).split(",")]

    def test_the_badge_is_placed_in_the_card_between_the_clock_and_the_buttons(self):
        # Building an element and never appending it is invisible to every test that
        # does not render a DOM, and this page has shipped that exact shape of bug
        # before — a banner that existed in the markup and could not be seen (see the
        # `[hidden]` guard below). Order is part of the claim, not decoration: the
        # badge qualifies what the buttons are about to do, so it has to be adjacent
        # to them rather than up with the metadata.
        parts = self._appended("buildCard")
        self.assertIn("dup", parts, "the badge is built but never attached")
        self.assertEqual(parts.index("dup"), parts.index("actions") - 1)
        self.assertLess(parts.index("cd"), parts.index("dup"))

    def test_a_tool_card_attaches_its_payload_above_the_buttons(self):
        # Same class of bug on the other builder, and it matters more here: the
        # payload is the thing being approved. A card that rendered the tool name and
        # silently dropped the arguments would put an operator one click from allowing
        # a write whose target they never saw.
        parts = self._appended("buildToolCard")
        self.assertIn("details", parts, "the payload is built but never attached")
        self.assertLess(parts.index("details"), parts.index("actions"))
        # And no duplicate badge, because nothing joins a tool card by waiting on it.
        self.assertNotIn("dup", parts)

    def test_the_count_stops_moving_once_the_card_is_being_decided(self):
        # Rewriting the number under a click already in flight is the same lie in the
        # other direction: by then it is history, not what the button will do.
        body = re.search(r"for \(const a of list\)\s*\{(.*?)\n    \}",
                         self.src, re.S).group(1)
        self.assertIn('entry.state === "pending"', body)
        self.assertIn('entry.state === "confirming"', body)


# Paths app.js actually requests, with each `${...}` interpolation replaced by a
# stand-in — and by MORE THAN ONE, because the ids in this app are not all the same
# shape. An approval id is a uuid4 and its route accepts `[A-Za-z0-9._-]`; a rule id
# is an integer and its route is bounded to digits, deliberately, since that segment
# lands in a URL path and a looser class would be a traversal primitive. One
# alphabetic placeholder would therefore report the tighter route as uncallable.
# A call is relayable if ANY stand-in matches.
_ID_STANDINS = ("ID", "1")


def _requested_paths(js: str) -> list[list[str]]:
    raw = re.findall(r"""(?:fetch|EventSource)\(\s*["'`]([^"'`]+)["'`]""", js)
    return [[re.sub(r"\$\{[^}]*\}", stand, r).split("?")[0]
             for stand in _ID_STANDINS] for r in raw]


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
        has its own test below."""
        js = APP_JS.read_text()
        # Served by this container rather than relayed (see control-plane-ui/app.py).
        local = {"/", "/app.js", "/healthz"}
        calls = _requested_paths(js)
        self.assertGreater(len(calls), 3, "no fetch/EventSource calls found — did the "
                                          "page's I/O move somewhere this cannot see?")
        for variants in calls:
            self.assertTrue(
                any(p in local or ui._relay_allowed("GET", p)
                    or ui._relay_allowed("POST", p) for p in variants),
                f"app.js calls {variants[0]}, which control-plane-ui neither serves "
                f"nor relays — add it to _RELAY_ROUTES or it will 403 in the browser")

    def test_every_relayed_route_is_actually_called(self):
        """The converse, and the one that keeps a default-deny allowlist worth reading.
        An entry with no caller cannot be reasoned about: nothing breaks if it is wrong,
        so nobody finds out that it is. Two had already accumulated — `/status`, and
        `GET /approvals`, which carries the pending hosts, clients and URLs and had been
        superseded by the SSE stream.

        Deliberately has NO exception list. A route that must stay without a caller is a
        real possibility, but it should arrive with its reason attached, as a change to
        this test that someone has to justify — not as an entry that quietly stops
        matching anything."""
        js = APP_JS.read_text()
        calls = [p for variants in _requested_paths(js) for p in variants]
        self.assertTrue(calls, "no fetch/EventSource calls found")
        unused = [f"{method} {pattern.pattern}"
                  for method, pattern in ui._RELAY_ROUTES
                  if not any(pattern.match(c) for c in calls)]
        self.assertEqual(
            unused, [],
            "these routes cross the relay but nothing on the page calls them — drop "
            "them, or wire them up; the backend keeps serving them on the "
            "management listener either way")

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
