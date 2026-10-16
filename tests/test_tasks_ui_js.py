# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the tasks UI's page (``tasks-ui/``): the structural checks every page
runs (``PageChecks`` in ``tests/test_control_plane_ui_js.py``), ``tasks.js``'s pure half
under node, and how its DOM half gates the registry and draws agent text, from source.

Agent text is what this page is for, so the rendering guards carry most of the weight:
a character that hides or reorders text is spelled out where it sits, and nothing an
agent wrote is parsed as markup."""
from __future__ import annotations

import json
import os
import re
import subprocess
import unittest

from test_control_plane_ui_js import (
    _NODE,
    _STRICT,
    ROOT,
    PageChecks,
    _code,
    _served_dir,
)
from test_tasks_ui import tui

TASKS_DIR = ROOT / "tasks-ui"


class TasksPageTests(PageChecks, unittest.TestCase):
    UI_DIR = TASKS_DIR
    APP = tui
    LOCAL = frozenset({"/", "/healthz", "/running"})
    MIN_CALLS = 8


_TASKS_PROBE = r"""
import { pathToFileURL } from "node:url";
const t = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/tasks.js`).href);
const ids = n => Array.from({length: n}, (_, i) => ({id: 100 - i}));
console.log(JSON.stringify({
  states: t.TASK_STATES, every: t.EVERY_STATE, not_running: t.NOT_RUNNING_STATUS,
  not_running_text: t.NOT_RUNNING_TEXT, poll_ms: t.TASKS_POLL_MS, page_max: t.PAGE_MAX,
  spell: {
    plain: t.spellHidden("fix the relay\n\tthen the tests"),
    bidi: t.spellHidden("approve\u202e;tpircs"),
    zero_width: t.spellHidden("a\u200bb\u2066c\u2069"),
    controls: t.spellHidden("a\rb\u0007c\u2028d"),
    astral: t.spellHidden("tag\u{E0041}"),
    separators: t.spellHidden("a\u2029b"),
    ignorable: t.spellHidden("ok\u2705\ufe0f a\u034fb\u3164c\u{E0101}d"),
    visible: t.spellHidden("em \u2014 dash, caf\u00e9, \u0430"),
    missing: t.spellHidden(undefined),
  },
  note: [t.hiddenNote(0), t.hiddenNote(1), t.hiddenNote(3)],
  older: {
    newest_first: t.olderCursor(ids(50), "newest-first", 0, 120),
    oldest_first: t.olderCursor(ids(50), "oldest-first", 0, 120),
    last_page: t.olderCursor(ids(20), "newest-first", 100, 120),
    exactly_one_page: t.olderCursor(ids(50), "newest-first", 0, 50),
    short_page_stale_total: t.olderCursor(ids(30), "newest-first", 0, 120),
    second_of_three: t.olderCursor(ids(50), "newest-first", 50, 120),
    empty: t.olderCursor([], "newest-first", 0, 3),
    not_a_list: t.olderCursor(null, "newest-first", 0, 3),
    bad_id: t.olderCursor([{id: "7"}], "newest-first", 0, 3),
  },
  children: {
    some: t.childrenLeft([{state: "closed"}, {state: "landing"}, {state: "open"}]),
    done: t.childrenLeft([{state: "closed"}]),
    none: t.childrenLeft([]),
    missing: t.childrenLeft(undefined),
  },
  claim: {
    full: t.claimText({holder: "claude-1234", branch: "fix-relay",
                       expires_at: 1789466400}),
    hostile: t.claimText({holder: "claude-x\u202e", branch: "b\u200b"}),
    none: t.claimText(null),
  },
  badge: [0, 1, 12, null, undefined, "3", 2.5, -1].map(t.countBadge),
  title: [0, 1, 12].map(t.pageTitle),
  plan: {
    keys: t.PLAN_PARTS.map(([key]) => key),
    parts: t.planParts({id: 7, approach: "Rotate.\nIn place.", footprint: ["a.py", "b/"],
                        tests: "Old keys read.", out_of_scope: "None.", risks: "Races.",
                        review_verdict: "Passes.", extra: "not a part"}),
    missing_parts: t.planParts({id: 7}),
    none: t.planParts(null),
    heading: t.planHeading({id: 7, author: "claude-x\u202e", at: 1789466400,
                            task_revision: 4, review_rounds: 2}),
    one_round: t.planHeading({id: 8, author: "a", at: 1789466400, task_revision: 1,
                              review_rounds: 1, superseded_at: 1789470000}),
    no_heading: t.planHeading(undefined),
    no_rounds: t.planHeading({id: 9, author: "a", at: 1789466400, task_revision: 1}),
    notice: {
      none_proposed: t.planNotice(null, "proposed"),
      none_open: t.planNotice(null, "open"),
      none_closed: t.planNotice(null, "closed"),
      current: t.planNotice({id: 1, superseded_at: null}, "refined"),
      superseded_proposed: t.planNotice({id: 1, superseded_at: 1789466400}, "proposed"),
      superseded_open: t.planNotice({id: 1, superseded_at: 1789466400}, "open"),
    },
    earlier: {
      by_number: t.earlierPlans([{id: 3}, {id: 2}, {id: 1}], 2).map(p => p.id),
      by_string: t.earlierPlans([{id: 3}, {id: 2}, {id: 1}], "2").map(p => p.id),
      none_shown: t.earlierPlans([{id: 3}], "").map(p => p.id),
      not_a_list: t.earlierPlans(null, 2),
      holes: t.earlierPlans([null, {id: 1}], 1),
    },
  },
  inbox: {
    sections: t.INBOX_SECTIONS.map(([key]) => key),
    reasons: {
      flagged: t.reasonText({why: "flagged", since: 1789466400, notes: 2}),
      one_note: t.reasonText({why: "flagged", since: 1789466400, notes: 1}),
      landing: t.reasonText({why: "landing", since: 1789466400, branch: "fix\u202e"}),
      landing_bare: t.reasonText({why: "landing", since: 1789466400, branch: null}),
      refined: t.reasonText({why: "refined", since: 1789466400, plan: 6,
                             review_rounds: 3}),
      lapsed: t.reasonText({why: "lapsed", since: 1789466400, holder: "claude-x\u200b",
                            branch: "b"}),
      lapsed_refining: t.reasonText({why: "lapsed", since: 1789466400,
                                     holder: "claude-x", branch: null}),
      quiet: t.reasonText({why: "quiet", since: 1789466400}),
      unknown: t.reasonText({why: "new\u202e", since: 1789466400}),
      none: t.reasonText(null),
    },
    count: [t.sectionCount(3, 3), t.sectionCount(50, 61)],
    hash: {
      empty: t.parseHash(""),
      task: t.parseHash("#task=14"),
      all: t.parseHash("#view=all&task=7"),
      bad_task: t.parseHash("#task=../api"),
      bad_view: t.parseHash("#view=audit"),
      zeros: t.parseHash("#task=006"),
      zero: t.parseHash("#task=0"),
      long_id: t.parseHash("#task=9223372036854775807"),
      missing: t.parseHash(undefined),
      inbox_none: t.hashFor("inbox", null),
      inbox_task: t.hashFor("inbox", "14"),
      all_none: t.hashFor("all", null),
      all_task: t.hashFor("all", 7),
      round_trip: t.parseHash(t.hashFor("all", "9")),
    },
  },
  status: {
    empty: t.tasksStatus(0, false, true, false),
    search_empty: t.tasksStatus(0, false, true, true),
    search_failed: t.tasksStatus(0, true, true, true),
    stale: t.tasksStatus(4, true, true, false),
    cold: t.tasksStatus(0, true, false, false),
    quiet: t.tasksStatus(4, false, true, false),
    inbox_empty: t.inboxStatus(0, false, true),
    inbox_stale: t.inboxStatus(2, true, true),
  },
}));
"""


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class TaskViewTests(unittest.TestCase):
    """The page's pure half (``tasks.js``): agent text with its hidden characters
    spelled out, the two paging orders, and what each list says about itself."""

    probe: dict

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", _TASKS_PROBE],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR)),
                 "TZ": "Europe/Stockholm"},
            capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing tasks.js under node failed:\n" + proc.stderr)
        cls.probe = json.loads(proc.stdout)

    def test_the_states_are_the_registrys(self):
        source = (ROOT / "task-registry" / "tasks.py").read_text()
        states = re.search(r"^STATES = \(([^)]*)\)", source, re.M).group(1)
        self.assertEqual(self.probe["states"], re.findall(r'"([^"]+)"', states))
        api = (ROOT / "task-registry" / "operator_api.py").read_text()
        self.assertIn(f'EVERY_STATE = "{self.probe["every"]}"', api)

    def test_the_not_running_status_is_the_one_the_relay_sends(self):
        self.assertEqual(self.probe["not_running"], 502)
        src = (TASKS_DIR / "app.py").read_text()
        body = re.search(r"def _registry_unreachable.*?(?=\ndef )", src, re.S).group(0)
        self.assertIn("status_code=502", body)
        self.assertIn("make tasks-up", self.probe["not_running_text"])

    def test_prose_keeps_its_line_breaks_and_tabs(self):
        self.assertEqual(self.probe["spell"]["plain"],
                         {"text": "fix the relay\n\tthen the tests", "hidden": 0})

    def test_a_direction_override_is_spelled_out_where_it_sits(self):
        self.assertEqual(self.probe["spell"]["bidi"],
                         {"text": "approve\\u202e;tpircs", "hidden": 1})

    def test_every_invisible_kind_is_spelled_and_counted(self):
        spell = self.probe["spell"]
        self.assertEqual(spell["zero_width"],
                         {"text": "a\\u200bb\\u2066c\\u2069", "hidden": 3})
        self.assertEqual(spell["controls"],
                         {"text": "a\\u000db\\u0007c\\u2028d", "hidden": 3})
        # Outside the BMP, as the surrogate pair JSON would write.
        self.assertEqual(spell["astral"], {"text": "tag\\udb40\\udc41", "hidden": 1})
        self.assertEqual(spell["separators"], {"text": "a\\u2029b", "hidden": 1})

    def test_default_ignorable_characters_are_spelled_too(self):
        # A variation selector can carry a payload no reader sees; an emoji's U+FE0F is
        # spelled with them rather than guessed harmless.
        self.assertEqual(self.probe["spell"]["ignorable"],
                         {"text": "ok\u2705\\ufe0f a\\u034fb\\u3164c\\udb40\\udd01d",
                          "hidden": 4})

    def test_visible_non_ascii_is_left_as_written(self):
        # Prose is full of dashes and accents; a homoglyph is visible, and this view
        # does not vouch for one (the pin view does, where a value must match exactly).
        self.assertEqual(self.probe["spell"]["visible"],
                         {"text": "em \u2014 dash, caf\u00e9, \u0430", "hidden": 0})
        self.assertEqual(self.probe["spell"]["missing"], {"text": "", "hidden": 0})

    def test_the_note_counts_what_was_spelled(self):
        self.assertEqual(self.probe["note"][0], "")
        self.assertIn("1 invisible or direction-changing character spelled",
                      self.probe["note"][1])
        self.assertIn("3 invisible or direction-changing characters", self.probe["note"][2])

    def test_each_paging_order_takes_its_cursor_from_its_own_end(self):
        older = self.probe["older"]
        self.assertEqual(older["newest_first"], 51)   # the last of 100..51
        self.assertEqual(older["oldest_first"], 100)  # the first
        self.assertEqual(older["second_of_three"], 51)

    def test_nothing_older_is_offered_past_the_total(self):
        older = self.probe["older"]
        # A page short of full is the last even when the total, which counts what
        # arrived since, says otherwise.
        for key in ("last_page", "exactly_one_page", "short_page_stale_total", "empty",
                    "not_a_list", "bad_id"):
            with self.subTest(case=key):
                self.assertIsNone(older[key])

    def test_a_parent_counts_every_child_not_closed(self):
        children = self.probe["children"]
        self.assertEqual(children["some"], {"left": 2, "total": 3,
                                            "text": "2 of 3 not closed"})
        self.assertEqual(children["done"]["left"], 0)
        self.assertEqual(children["none"], {"left": 0, "total": 0, "text": ""})
        self.assertEqual(children["missing"], children["none"])

    def test_a_claim_names_its_holder_branch_and_end(self):
        claim = self.probe["claim"]
        self.assertTrue(claim["full"].startswith("held by claude-1234 on fix-relay, until "))
        self.assertRegex(claim["full"], r"until \d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(claim["hostile"], "held by claude-x\\u202e on b\\u200b")
        self.assertEqual(claim["none"], "")

    def test_a_plans_parts_come_in_the_tools_order_labelled(self):
        plan = self.probe["plan"]
        self.assertEqual([p["label"] for p in plan["parts"]],
                         ["approach", "footprint", "tests", "out of scope", "risks",
                          "review verdict"])
        self.assertEqual(plan["parts"][0]["text"], "Rotate.\nIn place.")
        self.assertEqual(plan["parts"][1]["text"], "a.py\nb/")
        self.assertEqual([p["text"] for p in plan["missing_parts"]], [""] * 6)
        self.assertEqual(plan["none"], [])
        # The tool's parts, in its order, but the review rounds, which the heading
        # carries: a part the registry adds is a part this page must show.
        tools = (ROOT / "task-registry" / "agent_tools.py").read_text()
        block = tools[tools.index('"submit_plan": Tool('):tools.index('"submit_work": Tool(')]
        taken = [name for name in re.findall(r'Param\("(\w+)"', block)
                 if name != "review_rounds"]
        self.assertEqual(plan["keys"], taken)

    def test_a_plans_heading_says_whose_when_and_whether_superseded(self):
        plan = self.probe["plan"]
        self.assertRegex(plan["heading"], r"^plan 7 · by claude-x\\u202e · "
                                          r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d · at revision 4 · "
                                          r"2 review rounds$")
        self.assertRegex(plan["one_round"], r"· 1 review round · superseded \d{4}-")
        self.assertEqual(plan["no_heading"], "")
        self.assertNotIn("NaN", plan["no_rounds"])
        self.assertTrue(plan["no_rounds"].endswith("at revision 1"))

    def test_a_missing_or_superseded_plan_is_read_against_the_tasks_state(self):
        # Only a proposed task is refined by an agent; past it, nobody will hand a plan
        # in, so the page must not say one is coming.
        notice = self.probe["plan"]["notice"]
        self.assertEqual(notice["none_proposed"]["warn"], False)
        self.assertIn("an agent that claims the proposal", notice["none_proposed"]["text"])
        for key in ("none_open", "none_closed"):
            with self.subTest(case=key):
                self.assertEqual(notice[key], {
                    "text": "No plan: this task was moved on without one.", "warn": True})
        self.assertIsNone(notice["current"])
        self.assertIn("until an agent refines", notice["superseded_proposed"]["text"])
        self.assertEqual(notice["superseded_open"],
                         {"text": "Superseded: no plan is current.", "warn": True})

    def test_the_earlier_plans_leave_out_the_one_shown_whatever_its_ids_type(self):
        earlier = self.probe["plan"]["earlier"]
        self.assertEqual(earlier["by_number"], [3, 1])
        self.assertEqual(earlier["by_string"], [3, 1])
        self.assertEqual(earlier["none_shown"], [3])
        self.assertEqual(earlier["not_a_list"], [])
        self.assertEqual(earlier["holes"], [])

    def test_the_inboxs_sections_and_reasons_are_the_registrys(self):
        moves = (ROOT / "task-registry" / "operator_moves.py").read_text()
        block = re.search(r"^SECTIONS = \{(.*?)\}", moves, re.M | re.S).group(1)
        self.assertEqual(self.probe["inbox"]["sections"], re.findall(r'"(\w+)": \(', block))
        whys = re.findall(r'"(\w+)"', re.sub(r'"\w+": \(', "(", block))
        tasks_js = (TASKS_DIR / "tasks.js").read_text()
        body = tasks_js[tasks_js.index("export function reasonText("):]
        body = body[:body.index("\n}\n")]
        self.assertEqual(re.findall(r'case "(\w+)":', body), whys)

    def test_each_reason_says_why_and_since_when(self):
        r = self.probe["inbox"]["reasons"]
        stamp = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d"
        self.assertRegex(r["flagged"], rf"^2 notes for you since {stamp}$")
        self.assertRegex(r["one_note"], rf"^1 note for you since {stamp}$")
        self.assertRegex(r["landing"], rf"^landing since {stamp} on fix\\u202e$")
        self.assertRegex(r["landing_bare"], rf"^landing since {stamp}$")
        self.assertRegex(r["refined"], rf"^refined since {stamp}, plan 6, 3 review rounds$")
        self.assertRegex(r["lapsed"],
                         rf"^claim by claude-x\\u200b on b ran out {stamp}$")
        # A claim to refine a task has no branch.
        self.assertRegex(r["lapsed_refining"], rf"^claim by claude-x ran out {stamp}$")
        self.assertRegex(r["quiet"], rf"^nothing written since {stamp}$")
        self.assertEqual(r["unknown"], "new\\u202e")
        self.assertEqual(r["none"], "")

    def test_a_section_says_when_a_page_does_not_hold_it_all(self):
        self.assertEqual(self.probe["inbox"]["count"], ["· 3", "· 50 of 61, oldest first"])

    def test_the_hash_names_a_view_and_a_task_and_nothing_else(self):
        h = self.probe["inbox"]["hash"]
        inbox = {"view": "inbox", "task": None}
        self.assertEqual(h["empty"], inbox)
        self.assertEqual(h["missing"], inbox)
        self.assertEqual(h["task"], {"view": "inbox", "task": "14"})
        self.assertEqual(h["all"], {"view": "all", "task": "7"})
        # Only digits name a task, as the relay takes one, and only a known view.
        self.assertEqual(h["bad_task"], inbox)
        self.assertEqual(h["bad_view"], inbox)
        self.assertEqual((h["inbox_none"], h["inbox_task"], h["all_none"], h["all_task"]),
                         ("", "#task=14", "#view=all", "#view=all&task=7"))
        self.assertEqual(h["round_trip"], {"view": "all", "task": "9"})
        # As a row names it, so the row is marked and not read twice; never through a
        # Number, which would round an id past 2^53.
        self.assertEqual(h["zeros"]["task"], "6")
        self.assertEqual(h["zero"]["task"], "0")
        self.assertEqual(h["long_id"]["task"], "9223372036854775807")

    def test_the_count_is_a_number_or_nothing(self):
        self.assertEqual(self.probe["badge"], ["", "1", "12", "", "", "3", "", ""])

    def test_the_title_carries_the_count_ahead_of_the_pages_name(self):
        self.assertEqual(self.probe["title"],
                         ["dockade tasks", "(1) dockade tasks", "(12) dockade tasks"])
        self.assertIn("<title>dockade tasks</title>",
                      (TASKS_DIR / "index.html").read_text())

    def test_each_list_says_what_its_empty_and_its_stale_mean(self):
        status = self.probe["status"]
        self.assertEqual(status["empty"]["text"], "No tasks here.")
        self.assertEqual(status["search_empty"]["text"], "No task matches that search.")
        # A failed search is a failure, not an empty answer.
        self.assertEqual(status["search_failed"]["level"], "warn")
        self.assertIn("Could not", status["search_failed"]["text"])
        self.assertIn("may be out of date", status["stale"]["text"])
        self.assertEqual(status["cold"]["text"], "Could not load the tasks.")
        self.assertFalse(status["quiet"]["show"])
        self.assertIn("Nothing is your turn", status["inbox_empty"]["text"])
        self.assertEqual(status["inbox_stale"]["level"], "warn")

    def test_a_page_is_the_registrys_page(self):
        reads = (ROOT / "task-registry" / "reads.py").read_text()
        moves = (ROOT / "task-registry" / "operator_moves.py").read_text()
        self.assertIn(f"LIST_MAX = {self.probe['page_max']}\n", reads)
        self.assertIn(f"PAGE = {self.probe['page_max']}\n", moves)

    def test_the_poll_is_slower_than_the_policy_views(self):
        self.assertGreater(self.probe["poll_ms"], 4000)


class TaskPollingSourceTests(unittest.TestCase):
    """The registry can stop on its own: the page asks its server whether it answers,
    and nothing calls it, or polls it, unless it did. Read from the source, as the
    approval page's poll-gating guards are, since only a browser runs ``start()``."""

    APP = (TASKS_DIR / "app.js").read_text()
    TASKS = (TASKS_DIR / "tasks.js").read_text()

    def _function(self, src: str, name: str) -> str:
        return re.search(rf"^(?:export )?(?:async )?function {name}\(.*?^\}}", src,
                         re.M | re.S).group(0)

    def test_every_registry_read_returns_early_unless_it_answers(self):
        for name in ("refreshTasks", "refreshInbox"):
            with self.subTest(function=name):
                body = self._function(self.TASKS, name)
                first = body.split("\n")[1].strip()
                self.assertTrue(first.startswith("if (!registryIsRunning()"), first)

    def test_the_poll_reads_the_registry_only_while_it_answers(self):
        polls = re.findall(r"setInterval\(\(\) => \{\n(.*?)\n  \}, TASKS_POLL_MS\)",
                           self.APP, re.S)
        self.assertEqual(len(polls), 1, "one interval paced by TASKS_POLL_MS in app.js")
        body = [ln.strip() for ln in polls[0].splitlines()]
        self.assertEqual(body, [
            "if (!visible()) return;",
            "if (registryIsRunning()) refreshInbox().then(() => refreshTasks());",
            "else probeRegistry();"])

    def test_startup_asks_its_server_and_not_the_registry(self):
        start = self._function(self.APP, "start")
        self.assertIn("probeRegistry();", start)
        probe = self._function(self.TASKS, "probe")
        self.assertIn('fetch("/running")', probe)
        # Running only on the answer's word, not on the server having answered at all.
        self.assertIn(".running === true", probe)
        # The registry's own reads only after it answered.
        self.assertLess(probe.index('fetch("/running")'), probe.index("if (up)"))
        self.assertLess(probe.index("if (up)"), probe.index("refreshTasks()"))

    def test_one_ask_at_a_time(self):
        # A return to the page while the first ask is out would make a second.
        body = self._function(self.TASKS, "probeRegistry")
        self.assertIn("if (!probing) probing = probe()", body)
        self.assertIn("return probing;", body)

    def test_an_inbox_read_that_finds_it_gone_draws_nothing(self):
        # Checked again after the fetch: a 502 clears the count, and drawing the old
        # inbox would set it back.
        body = self._function(self.TASKS, "refreshInbox")
        after = body[body.index("registryJson(await fetch"):]
        self.assertIn("if (!registryIsRunning()) return;", after)

    def test_coming_back_to_the_page_asks_again_when_it_was_not_running(self):
        back = re.search(r'addEventListener\("visibilitychange", \(\) => \{.*?\n  \}\);',
                         self.APP, re.S).group(0)
        self.assertIn("if (registryIsRunning())", back)
        self.assertIn("else probeRegistry();", back)

    def test_the_hash_opens_a_task_only_once_the_registry_answers(self):
        # At load the hash is read before /running answers: the view at once, the task
        # when probe finds the registry up and reads the hash again.
        body = self._function(self.TASKS, "applyHash")
        self.assertLess(body.index("showView(named.view);"),
                        body.index("if (!registryIsRunning()) return;"))
        self.assertLess(body.index("if (!registryIsRunning()) return;"),
                        body.index("showTask("))
        probe = self._function(self.TASKS, "probe")
        self.assertLess(probe.index("if (up)"), probe.index("applyHash();"))
        go = self._function(self.TASKS, "goTo")
        self.assertIn("} else if (registryIsRunning()) {\n    showTask(task);", go)

    def test_a_task_the_registry_could_not_answer_for_is_read_when_it_does(self):
        body = self._function(self.TASKS, "showTask")
        after = body[body.index("} catch (e) {"):]
        self.assertIn("if (!registryIsRunning()) openTask = null;", after)

    def test_every_list_redraw_marks_the_open_task_again(self):
        for name in ("renderTasks", "renderInbox", "showTask", "closeTask"):
            with self.subTest(function=name):
                self.assertIn("markOpen();", self._function(self.TASKS, name))

    def test_every_open_goes_through_the_hash(self):
        # showTask is called from the hash, and from a click naming what the hash names.
        calls = re.findall(r"^.*\bshowTask\(.*$", self.TASKS, re.M)
        callers = [ln.strip() for ln in calls if "function showTask" not in ln]
        self.assertEqual(sorted(callers), ["showTask(named.task);", "showTask(task);"])
        self.assertIn('window.addEventListener("hashchange", applyHash);', self.TASKS)

    def test_a_502_stops_the_polls(self):
        body = self._function(self.TASKS, "registryJson")
        self.assertIn("res.status === NOT_RUNNING_STATUS", body)
        self.assertIn("setRunning(false)", body)

    def test_the_count_is_in_the_title_and_never_hot(self):
        # A backlog nobody waits on: no alarm, and gone when the registry is.
        self.assertNotRegex(self.TASKS, r"\bhot\b")
        # The count is what waits on the operator, the inbox's first section.
        self.assertIn("if (inboxLoaded) document.title = pageTitle(waiting);", self.TASKS)
        self.assertIn("inbox.waiting.total", self.TASKS)
        self.assertIn("if (!up) document.title = PAGE_TITLE;", self.TASKS)


class TaskRenderingSourceTests(unittest.TestCase):
    """How the page draws agent text, read from the source as the approval page's pin
    panel is: a browser is the only thing that runs it. Every field an agent wrote
    reaches the page through ``spellHidden``, directly or through a helper that calls
    it, and as text."""

    TASKS = (TASKS_DIR / "tasks.js").read_text()
    # A field an agent wrote, read off one of the registry's records.
    FIELD = re.compile(r"\b(?:t|n|h|task|claim|plan|part|reason)(?:\.claim)?\."
                       r"(?:title|body|text|author|holder|branch|progress|footprint|"
                       r"approach|tests|out_of_scope|risks|review_verdict)\b")
    SPELT = ("spellHidden(", "proseBlock(", "textCell(", "authorCell(", "claimText(")

    def test_nothing_is_parsed_as_markup(self):
        self.assertNotRegex(self.TASKS, r"innerHTML|outerHTML|insertAdjacentHTML|"
                                        r"document\.write|DOMParser")

    def test_every_agent_field_goes_through_the_spelling(self):
        code = _code(self.TASKS)
        lines = [ln for ln in code.splitlines() if self.FIELD.search(ln)]
        self.assertGreater(len(lines), 10, "the field pattern found too little")
        for ln in lines:
            if re.match(r"\s*if \(", ln):
                continue        # a condition reads the field; it draws nothing
            with self.subTest(line=ln.strip()):
                self.assertTrue(any(s in ln for s in self.SPELT), ln)

    def test_the_plans_are_drawn_against_what_they_are(self):
        # The task's own plan is read against its state; an earlier plan, against none,
        # since a newer one is the task's; the shown one stays out of the earlier list.
        self.assertIn("planBlock(task.plan, task.state)", self.TASKS)
        self.assertIn("planBlock(plan, null)", self.TASKS)
        self.assertIn("planNotice(null, task.state)", self.TASKS)
        self.assertIn("earlierPlans(plans, btn.dataset.shown)", self.TASKS)
        self.assertIn("more.dataset.shown = String(task.plan.id);", self.TASKS)
        self.assertIn('page.dataset.page === "plans" ? showPlans : showPage', self.TASKS)

    def test_the_helpers_spell_and_set_text(self):
        for name in ("proseBlock", "authorCell"):
            body = re.search(rf"^function {name}\(.*?^\}}", self.TASKS,
                             re.M | re.S).group(0)
            with self.subTest(helper=name):
                self.assertIn("spellHidden(", body)
        prose = re.search(r"^function proseBlock\(.*?^\}", self.TASKS, re.M | re.S)
        self.assertIn("pre.textContent = shown.text", prose.group(0))
        text = re.search(r"^function textCell\(.*?^\}", self.TASKS, re.M | re.S)
        self.assertIn("proseBlock(text)", text.group(0))

    def test_a_history_value_is_spelled_whatever_its_type(self):
        self.assertIn('typeof value === "string" ? proseBlock(value)', self.TASKS)
        self.assertIn("spellHidden(JSON.stringify(value)).text", self.TASKS)


if __name__ == "__main__":
    unittest.main()
