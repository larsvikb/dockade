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

# Evaluate the module and report a fixed set of calls. Deliberately data-only: no
# assertions live here, so a failure is reported by Python with a normal diff.
# The path arrives by environment rather than argv: `node -e` shifts the argument
# vector (there is no script filename), so an index would be quietly wrong.
_PROBE = r"""
const m = require(process.env.DOCKADE_APP_JS);
const missing = ["lampState", "backoffDelay", "diffPending", "shouldSweep"]
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
}));
"""


@unittest.skipUnless(_NODE, "node is not installed — skipping app.js unit tests")
class PageScriptTests(unittest.TestCase):
    """The pure decision helpers in app.js."""

    probe: dict

    @classmethod
    def setUpClass(cls) -> None:
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

    def test_the_script_file_is_the_one_the_app_serves(self):
        # UI_SCRIPT points into the image; assert the repo file the Dockerfile copies
        # there is the one this test suite has been asserting on.
        self.assertTrue(APP_JS.is_file())
        self.assertEqual(Path(ui.UI_SCRIPT).name, APP_JS.name)


if __name__ == "__main__":
    unittest.main()
