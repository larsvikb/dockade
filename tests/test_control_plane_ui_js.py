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
                 "holdRemaining", "countdownState", "departure", "persistPreview"]
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
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args — see above)
            [_NODE, "-e", _PROBE],
            env={**os.environ, "DOCKADE_APP_JS": str(APP_JS)},
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

    def test_the_script_file_is_the_one_the_app_serves(self):
        # UI_SCRIPT points into the image; assert the repo file the Dockerfile copies
        # there is the one this test suite has been asserting on.
        self.assertTrue(APP_JS.is_file())
        self.assertEqual(Path(ui.UI_SCRIPT).name, APP_JS.name)


if __name__ == "__main__":
    unittest.main()
