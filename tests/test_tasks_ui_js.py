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
from typing import ClassVar

from _loader import load_operator_api
from test_control_plane_ui_js import (
    _NODE,
    _STRICT,
    ROOT,
    PageChecks,
    _code,
    _module_texts,
    _served_dir,
)
from test_task_registry_operator import ALICE, ThroughTheRelay
from test_task_registry_operator import _Store as _OperatorStore
from test_task_registry_operator import api as _api
from test_task_registry_operator import reg as _reg
from test_task_registry_plans import DECISION, MULTI, PLAN
from test_tasks_ui import tui

TASKS_DIR = ROOT / "tasks-ui"
_agent, _plans, _reads, _Refused = (_reg.agent_moves, _reg.plans, _reg.reads,
                                    _reg.taskdb.Refused)


def _function(src: str, name: str) -> str:
    return re.search(rf"^(?:export )?(?:async )?function {name}\(.*?^\}}", src,
                     re.M | re.S).group(0)


class TasksPageTests(PageChecks, unittest.TestCase):
    UI_DIR = TASKS_DIR
    APP = tui
    LOCAL = frozenset({"/", "/healthz", "/running"})
    MIN_CALLS = 8


_TASKS_PROBE = r"""
import { pathToFileURL } from "node:url";
const t = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/tasks.js`).href);
const m = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/moves.js`).href);
const ids = n => Array.from({length: n}, (_, i) => ({id: 100 - i}));
console.log(JSON.stringify({
  states: m.TASK_STATES, every: t.EVERY_STATE, not_running: m.NOT_RUNNING_STATUS,
  unknown: m.OUTCOME_UNKNOWN_STATUS,
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
      decisions: t.reasonText({why: "refined", since: 1789466400, plan: 6,
                               decisions: 2, review_rounds: 3}),
      one_decision: t.reasonText({why: "refined", since: 1789466400, plan: 6,
                                  decisions: 1, review_rounds: 1}),
      no_decisions: t.reasonText({why: "refined", since: 1789466400, plan: 6,
                                  decisions: 0, review_rounds: 3}),
      null_decisions: t.reasonText({why: "refined", since: 1789466400, plan: 6,
                                    decisions: null, review_rounds: 3}),
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

    def test_the_unknown_outcome_status_is_the_one_the_relay_sends(self):
        self.assertEqual(self.probe["unknown"], 504)
        src = (TASKS_DIR / "app.py").read_text()
        body = re.search(r"def _outcome_unknown.*?(?=\ndef )", src, re.S).group(0)
        self.assertIn("status_code=504", body)

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
        # The plan's decisions, answered or not: a count of what it carries, never said
        # as waiting, and nothing from a registry that sends none.
        self.assertRegex(r["decisions"],
                         rf"^refined since {stamp}, plan 6, 2 decisions, 3 review rounds$")
        self.assertRegex(r["one_decision"],
                         rf"^refined since {stamp}, plan 6, 1 decision, 1 review round$")
        self.assertRegex(r["no_decisions"],
                         rf"^refined since {stamp}, plan 6, no decisions, 3 review rounds$")
        self.assertRegex(r["null_decisions"],
                         rf"^refined since {stamp}, plan 6, 3 review rounds$")
        self.assertNotRegex(r["decisions"], r"wait|answer")
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
        # A bulk handle names the flagged notes a page drew, so a page must fit in one.
        bulk_max = int(re.search(r"^BULK_MAX = (\d+)$", moves, re.M).group(1))
        self.assertLessEqual(self.probe["page_max"], bulk_max)

    def test_the_poll_is_slower_than_the_policy_views(self):
        self.assertGreater(self.probe["poll_ms"], 4000)


_MOVES_PROBE = r"""
import { pathToFileURL } from "node:url";
const t = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/tasks.js`).href);
const m = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/moves.js`).href);
const claim = {holder: "claude-x\u202e", branch: "b", expires_at: 1789466400};
const plan = {id: 6, approach: "A\u200b.", footprint: ["a.py"], tests: "T.",
              out_of_scope: "None.", risks: "R.", review_verdict: "Passes.",
              superseded_at: null};
const task = (state, extra = {}) => ({id: 7, state, revision: 4, title: "T",
                                      body: "B", plan: null, claim: null, ...extra});
const moves = tk => m.movesFor(tk).map(mv => [mv.state, mv.verb, mv.endsClaim]);
const res = (status, text) => ({status, text: async () => text});
const cut = status => ({status, text: async () => { throw new TypeError("cut"); }});
const move = {kind: "move", task: "7", revision: 4, state: "open", verb: "reopen",
              lastNote: 12};
const brk = {kind: "break", task: "7", revision: 4, verb: "break the claim", lastNote: 12};
const note = {kind: "note", task: "7", revision: 4, text: "Seen.", lastNote: 12,
              verb: "add the note"};
const after = (extra, notes = []) => ({task: task("open", {revision: 5, notes, ...extra}),
                                       held: {timeline: {history: [], total: 0},
                                              plans: []}});
// A plan with a single-choice decision and a multi-select one, as a read carries them,
// and the answers an approve of it as assumed leaves.
const option = (label, assumed) => ({label, meaning: `${label}, meant.`, assumed});
const decided = {...plan, decisions: [
  {question: "Hold\u202e or deny?", why: "W.", multi_select: false,
   options: [option("Hold", true), option("Deny", false)]},
  {question: "Which logs?", why: "W.", multi_select: true,
   options: [option("Audit", true), option("Access", true), option("Debug", false)]}]};
const chose = (plan, decision, positions, other = null) => ({plan, decision, question: "Q",
  options: positions.map(position => ({position, label: "L", meaning: "M"})), other,
  author: "operator", at: 1789466400});
const answered = [chose(6, 0, [0]), chose(6, 1, [0, 1])];
const assumed = m.assumedChoices(decided.decisions);
const denied = [{options: [1], other: false, text: ""}, assumed[1]];
const sendBack = {state: "proposed", verb: "send back", endsClaim: false};
const approveW = {kind: "approve", task: "7", revision: 4, plan: 6, verb: "approve",
                  answers: [{options: [0], other: null}, {options: [0, 1], other: null}],
                  predicted: "open", lastNote: 12};
const bare = {...approveW, answers: []};
const reread = extra => ({task: task("open", {revision: 5, plan: decided, answers: answered,
                                              ...extra})});
console.log(JSON.stringify({
  moves: {
    proposed: moves(task("proposed")),
    refined_with_plan: moves(task("refined", {plan})),
    refined_decided: moves(task("refined", {plan: decided, answers: []})),
    refined_answered: moves(task("refined", {plan: decided, answers: answered})),
    re_refined: moves(task("refined", {plan: {...decided, id: 9}, answers: answered})),
    deferred_re_refined: moves(task("deferred", {plan: {...decided, id: 9},
                                                 answers: answered})),
    deferred_decided: moves(task("deferred", {plan: decided})),
    landing_decided: moves(task("landing", {plan: decided})),
    closed_answered: moves(task("closed", {plan: decided, answers: answered})),
    proposed_decided_superseded: moves(task("proposed", {plan: {...decided,
                                                                superseded_at: 1}})),
    proposed_with_plan: moves(task("proposed", {plan})),
    proposed_superseded: moves(task("proposed", {plan: {...plan, superseded_at: 1}})),
    open_claimed: moves(task("open", {claim})),
    landing: moves(task("landing")),
    deferred: moves(task("deferred")),
    closed: moves(task("closed")),
    unknown_state: moves(task("elsewhere")),
    none: m.movesFor(null),
  },
  outcome: {
    landed: m.writeOutcome({status: 200, body: {ok: true}}),
    truncated_200: m.writeOutcome({status: 200, body: undefined}),
    not_ok_200: m.writeOutcome({status: 200, body: {detail: "x"}}),
    stale: m.writeOutcome({status: 409, body: {ok: false, current: {id: 7}}}),
    truncated_409: m.writeOutcome({status: 409, body: undefined}),
    null_current_409: m.writeOutcome({status: 409, body: {ok: false, current: null}}),
    refused_400: m.writeOutcome({status: 400, body: {ok: false, detail: "no"}}),
    refused_404: m.writeOutcome({status: 404, body: {ok: false}}),
    guard_403: m.writeOutcome({status: 403, body: undefined}),
    not_running: m.writeOutcome({status: 502, body: {ok: false}}),
    unknown_504: m.writeOutcome({status: 504, body: {ok: false}}),
    rejected: m.writeOutcome(null),
  },
  check: {
    move_found: m.checkWrite(move, after({})),
    move_not_landed: m.checkWrite(move, {...after({}), task: task("refined", {revision: 4})}),
    move_changed: m.checkWrite(move, after({state: "closed"})),
    break_found: m.checkWrite(brk, after({})),
    break_not_landed: m.checkWrite(brk, {...after({}), task: task("open", {claim})}),
    break_changed: m.checkWrite(brk, after({claim})),
    note_found: m.checkWrite(note, after({}, [{id: 13, author: "operator", text: "Seen."}])),
    note_older: m.checkWrite(note, after({}, [{id: 12, author: "operator", text: "Seen."}])),
    note_by_agent: m.checkWrite(note, after({}, [{id: 13, author: "claude-a", text: "Seen."}])),
    note_other_text: m.checkWrite(note, after({}, [{id: 13, author: "operator", text: "x"}])),
    note_missing: m.checkWrite(note, after({})),
    failed_read: m.checkWrite(move, null),
    failed_note_read: m.checkWrite(note, null),
    approve_found: m.checkWrite(approveW, reread({})),
    approve_found_moved_on: m.checkWrite(approveW, reread({state: "deferred"})),
    approve_sent_back_found: m.checkWrite(
      {...approveW, answers: [{options: [], other: "Only some."}, approveW.answers[1]],
       predicted: "proposed"},
      reread({state: "proposed", plan: {...decided, superseded_at: 2},
              answers: [chose(6, 0, [], "Only some."), chose(6, 1, [0, 1])]})),
    // Sent back, then planned again before the read: its answers are on plan 6 still.
    approve_sent_back_replanned: m.checkWrite(
      {...approveW, answers: [{options: [1], other: null}, approveW.answers[1]],
       predicted: "proposed"},
      reread({state: "refined", plan: {...decided, id: 9},
              answers: [chose(6, 0, [1]), chose(6, 1, [0, 1])]})),
    approve_other_answers: m.checkWrite(approveW, reread({state: "proposed",
      answers: [chose(6, 0, [1]), chose(6, 1, [0, 1])]})),
    approve_other_text: m.checkWrite(approveW, reread({state: "proposed",
      answers: [chose(6, 0, [0], "x"), chose(6, 1, [0, 1])]})),
    approve_one_answer: m.checkWrite(approveW, reread({answers: [chose(6, 0, [0])]})),
    approve_other_plan: m.checkWrite(approveW, reread({plan: {...decided, id: 9},
      answers: [chose(9, 0, [0]), chose(9, 1, [0, 1])]})),
    approve_not_landed: m.checkWrite(approveW, reread({state: "refined", revision: 4,
                                                       answers: []})),
    approve_bare_found: m.checkWrite(bare, {task: task("open", {revision: 5, plan})}),
    approve_bare_changed: m.checkWrite(bare, {task: task("deferred", {revision: 5, plan})}),
    approve_bare_other_plan: m.checkWrite(bare, {task: task("open", {revision: 5,
                                                                     plan: {...plan, id: 9}})}),
    approve_failed_read: m.checkWrite(approveW, null),
  },
  answer: {
    json: await m.readAnswer(res(409, '{"ok": false, "current": {"id": 7}}')),
    text: await m.readAnswer(res(403, "refused by tasks-ui: cross-origin\n")),
    cut: await m.readAnswer(cut(200)),
  },
  last_note: [m.lastNoteId({notes: [{id: 3}, {id: 11}, {id: 9}]}),
              m.lastNoteId({notes: []}), m.lastNoteId(null)],
  labels: {
    approve: t.approveLabel(task("refined", {plan})),
    approve_opens: t.approveLabel(task("refined", {plan: decided}), "open"),
    approve_back: t.approveLabel(task("refined", {plan: decided}), "proposed"),
    reopen: t.moveLabel({state: "open", verb: "reopen", endsClaim: false},
                        task("refined", {plan: decided, answers: answered})),
    reopen_claimed: t.moveLabel({state: "open", verb: "reopen", endsClaim: true},
                                task("refined", {plan: decided, answers: answered,
                                                 claim})),
    reopen_closed: t.moveLabel({state: "open", verb: "reopen", endsClaim: false},
                               task("closed", {plan})),
    send_back: t.moveLabel(sendBack, task("refined", {plan: decided}), assumed),
    send_back_differs: t.moveLabel(sendBack, task("refined", {plan: decided}), denied),
    send_back_other: t.moveLabel(sendBack, task("refined", {plan: decided}),
                                 [assumed[0], {options: [0, 1], other: true, text: "x"}]),
    send_back_text_not_picked: t.moveLabel(sendBack, task("refined", {plan: decided}),
                                           [{...assumed[0], text: "x"}, assumed[1]]),
    send_back_no_choices: t.moveLabel(sendBack, task("refined", {plan: decided})),
    ends_claim: t.moveLabel({state: "closed", verb: "close", endsClaim: true},
                            task("open", {claim})),
    plain: t.moveLabel({state: "deferred", verb: "defer", endsClaim: false}, task("open")),
    break: t.breakLabel(claim),
    no_break: t.breakLabel(null),
    approve_none: t.approveLabel(task("proposed")),
    hidden: t.approveHidden(task("refined", {title: "T\u202e", body: "B\u2066", plan})),
    one_hidden: t.approveHidden(task("refined", {plan})),
    no_hidden: t.approveHidden(task("refined", {plan: {...plan, approach: "A."}})),
    // The plan's one, and the first question's.
    decisions_hidden: t.approveHidden(task("refined", {plan: decided}), assumed),
    option_hidden: t.approveHidden(task("refined", {plan: {...decided, approach: "A.",
      decisions: [{...decided.decisions[1], why: "W\u200b.",
                   options: [option("Au\u2066dit", true), option("B", false)]}]}})),
    // And an "other" text sent, but not one left while "other" is not picked.
    other_hidden: t.approveHidden(task("refined", {plan: decided}),
                                  [{options: [], other: true, text: "x\u200b"}, assumed[1]]),
    other_not_sent: t.approveHidden(task("refined", {plan: decided}),
                                    [{...assumed[0], text: "x\u200b"}, assumed[1]]),
    incomplete: [t.incompleteText([1]), t.incompleteText([0, 1])],
    // A choice for no decision, or none for one: no answers, whatever the rest.
    one_too_many: [m.approveAnswers(decided.decisions, [...assumed, assumed[0]]),
                   m.approveOutcome(decided.decisions, [...assumed, assumed[0]])],
    one_too_few: [m.approveAnswers(decided.decisions, assumed.slice(0, 1)),
                  m.approveOutcome(decided.decisions, assumed.slice(0, 1))],
    no_choices: [m.approveAnswers(decided.decisions, null), m.approveAnswers([], [])],
    approvable: {
      unanswered: m.approvable(task("refined", {plan: decided, answers: []})),
      no_decisions: m.approvable(task("refined", {plan})),
      re_refined: m.approvable(task("refined", {plan: {...decided, id: 9},
                                                answers: answered})),
      answered: m.approvable(task("refined", {plan: decided, answers: answered})),
      open: m.approvable(task("open", {plan: decided})),
      superseded: m.approvable(task("refined", {plan: {...decided, superseded_at: 1}})),
      no_plan: m.approvable(task("refined")),
    },
    open_blocked: t.openBlockedText(task("deferred", {plan: decided})),
    open_blocked_refined: t.openBlockedText(task("refined", {plan: decided})),
    open_blocked_answered: t.openBlockedText(task("deferred", {plan: decided,
                                                               answers: answered})),
    reset: t.resetText({from: 6, to: 9, others: ["Only \u202ethe llm ones."]}),
    reset_bare: t.resetText({from: 6, to: 9, others: []}),
  },
  texts: Object.fromEntries(["landed", "found", "stale", "refused", "not-running",
                             "not-landed", "not-found-yet", "could-not-check", "changed",
                             "unknown"]
    .map(o => [o, t.outcomeText(o === "not-found-yet" ? note : move, o, "no\u202e")])),
  note_landed_text: t.outcomeText(note, "landed").text,
  approved: {
    opened: t.outcomeText(approveW, "landed", undefined, "open"),
    sent_back: t.outcomeText({...approveW, predicted: "proposed"}, "landed", undefined,
                             "proposed"),
    found: t.outcomeText(approveW, "found", undefined, "open"),
    not_predicted: t.outcomeText(approveW, "found", undefined, "proposed"),
    moved_on: t.outcomeText({...approveW, predicted: "proposed"}, "found", undefined,
                            "deferred\u202e"),
    refused: t.outcomeText(approveW, "refused", "plan 6 was answered already", "open"),
    not_landed: t.outcomeText(approveW, "not-landed", undefined, "refined"),
    move_ignores_it: t.outcomeText(move, "landed", undefined, "proposed"),
  },
}));
"""


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class MoveTests(unittest.TestCase):
    """The write path's pure half (``moves.js``) and the labels ``tasks.js`` draws from
    it: which moves a task offers, what each answer means, and how a write whose answer
    was lost is decided from the task read again."""

    probe: dict

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", _MOVES_PROBE],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR)),
                 "TZ": "Europe/Stockholm"},
            capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing moves.js under node failed:\n" + proc.stderr)
        cls.probe = json.loads(proc.stdout)

    def test_each_state_offers_every_other_and_refined_only_with_a_current_plan(self):
        mv = self.probe["moves"]
        self.assertEqual(mv["proposed"], [["open", "move to open", False],
                                          ["landing", "move to landing", False],
                                          ["deferred", "defer", False],
                                          ["closed", "close", False]])
        self.assertEqual([s for s, _, _ in mv["proposed_with_plan"]],
                         ["refined", "open", "landing", "deferred", "closed"])
        self.assertNotIn("refined", [s for s, _, _ in mv["proposed_superseded"]])
        self.assertEqual((mv["unknown_state"], mv["none"]), ([], []))

    def test_a_refined_task_is_sent_back_by_a_move_and_approved_by_none(self):
        # Approving is `approve`, with or without decisions; open is a move from
        # refined only to reopen a plan answered before, which `approve` refuses.
        mv = self.probe["moves"]
        expected = {"proposed": "send back", "landing": "move to landing",
                    "deferred": "defer", "closed": "close"}
        for key in ("refined_with_plan", "refined_decided", "re_refined"):
            with self.subTest(case=key):
                self.assertEqual({s: verb for s, verb, _ in mv[key]}, expected)
        self.assertEqual({s: verb for s, verb, _ in mv["refined_answered"]},
                         expected | {"open": "reopen"})

    def test_open_is_offered_from_no_state_while_decisions_wait(self):
        mv = self.probe["moves"]
        for key in ("deferred_decided", "landing_decided", "deferred_re_refined"):
            with self.subTest(case=key):
                self.assertNotIn("open", [s for s, _, _ in mv[key]])
                self.assertIn("refined", [s for s, _, _ in mv[key]])
        self.assertIn(["open", "reopen", False], mv["closed_answered"])
        # A superseded plan waits on nobody: the registry's guard reads the current one.
        self.assertIn(["open", "move to open", False], mv["proposed_decided_superseded"])
        labels = self.probe["labels"]
        self.assertEqual(labels["open_blocked"],
                         "No move opens this task: plan 6 has decisions not answered. "
                         "Move it back to refined, then approve it with the answers.")
        self.assertEqual((labels["open_blocked_refined"], labels["open_blocked_answered"]),
                         ("", ""))

    def test_a_closed_or_deferred_task_is_reopened(self):
        for state in ("closed", "deferred"):
            with self.subTest(state=state):
                verbs = {s: verb for s, verb, _ in self.probe["moves"][state]}
                self.assertEqual(verbs["open"], "reopen")
        landing = {s: verb for s, verb, _ in self.probe["moves"]["landing"]}
        self.assertEqual(landing["open"], "move to open")

    def test_every_move_of_a_claimed_task_ends_its_claim(self):
        claimed = self.probe["moves"]["open_claimed"]
        self.assertTrue(claimed)
        self.assertTrue(all(ends for _, _, ends in claimed))

    def test_an_answer_is_landed_stale_refused_or_not_running(self):
        o = self.probe["outcome"]
        self.assertEqual((o["landed"], o["stale"], o["not_running"]),
                         ("landed", "stale", "not-running"))
        for key in ("refused_400", "refused_404", "guard_403"):
            with self.subTest(case=key):
                self.assertEqual(o[key], "refused")

    def test_an_answer_there_is_no_trusting_is_unknown(self):
        # The relay's 504, a fetch that rejected, and an answer cut off: none says
        # whether the write landed, so the page reads the task again.
        o = self.probe["outcome"]
        for key in ("unknown_504", "rejected", "truncated_200", "not_ok_200",
                    "truncated_409", "null_current_409"):
            with self.subTest(case=key):
                self.assertEqual(o[key], "unknown")

    def test_a_lost_move_or_break_is_decided_by_the_revision_and_what_changed(self):
        c = self.probe["check"]
        self.assertEqual((c["move_found"], c["break_found"]), ("found", "found"))
        # Unchanged, so not landed, and the revision makes a resend safe.
        self.assertEqual((c["move_not_landed"], c["break_not_landed"]),
                         ("not-landed", "not-landed"))
        self.assertEqual((c["move_changed"], c["break_changed"]), ("changed", "changed"))

    def test_a_lost_note_is_found_only_as_a_newer_operator_note_with_its_text(self):
        c = self.probe["check"]
        self.assertEqual(c["note_found"], "found")
        # Never "safe to resend": it may still be committing.
        for key in ("note_older", "note_by_agent", "note_other_text", "note_missing"):
            with self.subTest(case=key):
                self.assertEqual(c[key], "not-found-yet")

    def test_a_read_that_failed_decides_nothing(self):
        c = self.probe["check"]
        self.assertEqual((c["failed_read"], c["failed_note_read"]),
                         ("could-not-check", "could-not-check"))

    def test_an_answer_is_read_whatever_it_carries(self):
        a = self.probe["answer"]
        self.assertEqual((a["json"]["status"], a["json"]["body"]),
                         (409, {"ok": False, "current": {"id": 7}}))
        # The guard's refusal is text, kept for what it says.
        self.assertNotIn("body", a["text"])
        self.assertEqual(a["text"]["text"], "refused by tasks-ui: cross-origin\n")
        self.assertEqual((a["cut"]["status"], a["cut"].get("body")), (200, None))

    def test_a_notes_check_counts_from_the_newest_note_held(self):
        self.assertEqual(self.probe["last_note"], [11, 0, 0])

    def test_approve_names_the_revision_plan_and_outcome(self):
        labels = self.probe["labels"]
        self.assertEqual(labels["approve"], "approve revision 4 and plan 6: opens it")
        self.assertEqual(labels["approve_opens"], "approve revision 4 and plan 6: opens it")
        self.assertEqual(labels["approve_back"], "approve revision 4 and plan 6 with these "
                                                 "answers: sends it back to proposed")
        self.assertEqual(labels["approve_none"], "")
        self.assertEqual(labels["plain"], "defer")

    def test_a_reopen_from_refined_names_what_it_opens(self):
        labels = self.probe["labels"]
        self.assertEqual(labels["reopen"], "reopen revision 4 and plan 6")
        self.assertEqual(labels["reopen_claimed"],
                         "reopen revision 4 and plan 6 (ends claude-x\\u202e's claim)")
        self.assertEqual(labels["reopen_closed"], "reopen")

    def test_send_back_says_it_sends_no_answers_once_a_choice_differs(self):
        labels = self.probe["labels"]
        self.assertEqual(labels["send_back_differs"], "send back without answering")
        self.assertEqual(labels["send_back_other"], "send back without answering")
        for key in ("send_back", "send_back_text_not_picked", "send_back_no_choices"):
            with self.subTest(case=key):
                self.assertEqual(labels[key], "send back")

    def test_approves_hidden_count_takes_the_decisions_and_the_other_text_sent(self):
        labels = self.probe["labels"]
        self.assertIn("carries 2 invisible", labels["decisions_hidden"])
        # A why, and an option's label and its meaning, which repeats it.
        self.assertIn("carries 3 invisible", labels["option_hidden"])
        self.assertIn("carries 3 invisible", labels["other_hidden"])
        self.assertIn("carries 2 invisible", labels["other_not_sent"])

    def test_an_incomplete_answer_says_which_decisions_wait(self):
        self.assertEqual(self.probe["labels"]["incomplete"], [
            'No approve until every decision is answered: decision 2 not yet. A picked '
            '"other" needs its text.',
            'No approve until every decision is answered: decisions 1, 2 not yet. A '
            'picked "other" needs its text.'])

    def test_only_a_refined_current_plan_not_answered_is_approvable(self):
        # A plan answered before is refused by `approve`: it reopens by a move.
        self.assertEqual(self.probe["labels"]["approvable"], {
            "unanswered": True, "no_decisions": True, "re_refined": True,
            "answered": False, "open": False, "superseded": False, "no_plan": False})

    def test_a_choice_per_decision_or_no_answers(self):
        labels = self.probe["labels"]
        for key in ("one_too_many", "one_too_few"):
            with self.subTest(case=key):
                self.assertEqual(labels[key], [None, "incomplete"])
        # A plan without decisions is approved with no answers.
        self.assertEqual(labels["no_choices"], [None, []])

    def test_another_plan_is_said_with_the_other_text_it_reset(self):
        labels = self.probe["labels"]
        self.assertEqual(labels["reset_bare"],
                         "Another plan: this is plan 9, and plan 6 was drawn before. Read "
                         "it again; its choices are back on what it assumes.")
        self.assertEqual(labels["reset"], labels["reset_bare"]
                         + ' Reset with plan 6: "Only \\u202ethe llm ones."')

    def test_a_lost_approve_is_found_by_the_answers_it_left(self):
        c = self.probe["check"]
        for key in ("approve_found", "approve_found_moved_on", "approve_sent_back_found",
                    "approve_sent_back_replanned", "approve_bare_found"):
            with self.subTest(case=key):
                self.assertEqual(c[key], "found")
        for key in ("approve_other_answers", "approve_other_text", "approve_one_answer",
                    "approve_other_plan", "approve_bare_changed", "approve_bare_other_plan"):
            with self.subTest(case=key):
                self.assertEqual(c[key], "changed")
        self.assertEqual(c["approve_not_landed"], "not-landed")
        self.assertEqual(c["approve_failed_read"], "could-not-check")

    def test_an_approve_says_the_state_it_reached_and_warns_when_not_predicted(self):
        a = self.probe["approved"]
        self.assertEqual(a["opened"], {"text": "Approve: done. The task is open.",
                                       "warn": False})
        self.assertEqual(a["sent_back"], {
            "text": "Approve: done. The task is back in proposed, its plan superseded, "
                    "for a plan written from the answers.", "warn": False})
        self.assertEqual(a["found"]["warn"], False)
        self.assertTrue(a["found"]["text"].endswith("landed. The task is open."))
        self.assertTrue(a["not_predicted"]["warn"])
        self.assertIn("That is not what this page said: it said the answers open it.",
                      a["not_predicted"]["text"])
        self.assertIn("The task is deferred\\u202e. That is not what this page said: it "
                      "said the answers send it back.", a["moved_on"]["text"])
        self.assertTrue(a["moved_on"]["warn"])
        # Only a landing names a state; any other outcome says only its own.
        self.assertEqual(a["refused"]["text"], "Approve: refused. plan 6 was answered already")
        self.assertNotIn("The task is", a["not_landed"]["text"])
        self.assertEqual(a["move_ignores_it"], {"text": "Reopen: done.", "warn": False})

    def test_a_claim_ending_move_and_a_break_name_the_holder_spelled(self):
        labels = self.probe["labels"]
        self.assertEqual(labels["ends_claim"], "close (ends claude-x\\u202e's claim)")
        self.assertEqual(labels["break"], "break claude-x\\u202e's claim")
        self.assertEqual(labels["no_break"], "")

    def test_approve_says_when_what_it_approves_hides_a_character(self):
        labels = self.probe["labels"]
        # Title, body and the plan's parts, counted together.
        self.assertIn("carries 3 invisible or direction-changing characters",
                      labels["hidden"])
        self.assertIn("carries 1 invisible or direction-changing character,",
                      labels["one_hidden"])
        self.assertEqual(labels["no_hidden"], "")

    def test_each_outcome_says_what_to_do_next(self):
        texts = self.probe["texts"]
        self.assertEqual(texts["landed"], {"text": "Reopen: done.", "warn": False})
        self.assertEqual(self.probe["note_landed_text"], "The note: done.")
        for key, text in texts.items():
            with self.subTest(outcome=key):
                self.assertEqual(text["warn"], key not in ("landed", "found"))
        self.assertIn("this is the task as it is now", texts["stale"]["text"])
        # Found after its answer was lost: said so, not passed off as a plain landing.
        self.assertEqual(texts["found"]["text"],
                         "Reopen: no answer came, but the task shows it landed.")
        self.assertIn("cannot apply it twice", texts["not-landed"]["text"])
        self.assertIn("before sending it again", texts["not-found-yet"]["text"])
        self.assertNotIn("cannot apply it twice", texts["not-found-yet"]["text"])
        self.assertIn("not known", texts["could-not-check"]["text"])
        # A refusal's reason is the registry's, spelled like any other text.
        self.assertEqual(texts["refused"]["text"], "Reopen: refused. no\\u202e")


_NOTE_MOVES_PROBE = r"""
import { pathToFileURL } from "node:url";
const t = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/tasks.js`).href);
const m = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/moves.js`).href);
// What each write puts on the wire, with fetch stubbed: its path, method and body.
const sent = [];
globalThis.fetch = (url, opts) => {
  sent.push({url, method: opts.method, body: JSON.parse(opts.body)});
  return Promise.resolve({status: 200, text: async () => '{"ok": true}'});
};
const base = {task: "7", revision: 4, lastNote: 12, lastEntry: 100};
const writes = {
  hide: {...base, kind: "hide", note: 10, hidden: true},
  show: {...base, kind: "hide", note: 10, hidden: false},
  handle: {...base, kind: "handle", note: 10, handled: true, reply: null},
  reply: {...base, kind: "handle", note: 10, handled: true, reply: "Seen."},
  unhandle: {...base, kind: "handle", note: 10, handled: false, reply: null},
  handle_many: {kind: "handle-many", task: null, notes: [9, 5]},
};
const wire = {};
for (const [key, w] of Object.entries(writes)) {
  sent.length = 0;
  await m.sendWrite(w);
  wire[key] = sent[0];
}
sent.length = 0;
let unknown_threw = false;
try { m.sendWrite({...base, kind: "delete"}); } catch (e) { unknown_threw = true; }
const unknown = {threw: unknown_threw, sent: sent.length};

const note = (extra = {}) => ({id: 10, task: 7, author: "claude-a", text: "x",
                               for_operator: false, handled: false, hidden: false,
                               ...extra});
const moves = n => m.noteMovesFor(n).map(mv => [mv.kind, mv.value, mv.verb,
                                                Boolean(mv.reply)]);
// A task read again after a lost note move: its newest timeline page.
const entry = (id, action, detail) => ({id, action, detail, author: "operator"});
const after = (history, total = history.length) => ({
  task: {id: 7, revision: 4, notes: []}, held: {timeline: {history, total}, plans: []}});
const full = Array.from({length: 50}, (_, i) => entry(200 + i, "update", {}));
const named = {kind: "handle-many", task: null, notes: [9, 5], verb: "handle 2 notes"};
const flagged = ids => ({flagged: {notes: ids.map(id => ({id})), total: ids.length}});
const flood = Array.from({length: 60}, (_, i) => 1000 + i);
console.log(JSON.stringify({
  wire, unknown,
  is_note_move: ["hide", "handle", "handle-many", "note", "move", "break"]
    .map(kind => m.isNoteMove({kind})),
  moves: {
    plain: moves(note()),
    hidden: moves(note({hidden: true})),
    flagged: moves(note({for_operator: true})),
    handled: moves(note({for_operator: true, handled: true})),
    flagged_hidden: moves(note({for_operator: true, hidden: true})),
    no_id: moves({text: "x"}),
    none: moves(null),
  },
  bulk: {
    drawn_order: m.bulkHandle(new Set([5, 3, 9]), [{id: 9}, {id: 4}, {id: 3}, {id: 5}]),
    undrawn: m.bulkHandle(new Set([5, 77]), [{id: 9}, {id: 5}]),
    none: m.bulkHandle(new Set(), [{id: 9}]),
    from_list: m.bulkHandle([9], [{id: 9}]),
    nothing_drawn: m.bulkHandle(new Set([9]), null),
  },
  check: {
    hide_found: m.checkWrite(writes.hide, after([entry(101, "hide_note",
                                                       {note_id: 10, hidden: true})])),
    hide_held: m.checkWrite(writes.hide, after([entry(100, "hide_note",
                                                      {note_id: 10, hidden: true})])),
    hide_other_value: m.checkWrite(writes.hide, after([entry(101, "hide_note",
                                                             {note_id: 10, hidden: false})])),
    hide_other_note: m.checkWrite(writes.hide, after([entry(101, "hide_note",
                                                            {note_id: 11, hidden: true})])),
    show_found: m.checkWrite(writes.show, after([entry(101, "hide_note",
                                                       {note_id: 10, hidden: false})])),
    handle_found: m.checkWrite(writes.handle, after([entry(101, "handle_note",
      {note_id: 10, handled: true, reply: null, reply_id: null})])),
    handle_missing: m.checkWrite(writes.handle, after([])),
    handle_as_hide: m.checkWrite(writes.handle, after([entry(101, "hide_note",
                                                             {note_id: 10, hidden: true})])),
    reply_found: m.checkWrite(writes.reply, after([entry(101, "handle_note",
      {note_id: 10, handled: true, reply: "Seen.", reply_id: 30})])),
    reply_bare_entry: m.checkWrite(writes.reply, after([entry(101, "handle_note",
      {note_id: 10, handled: true, reply: null, reply_id: null})])),
    reply_missing: m.checkWrite(writes.reply, after([])),
    unhandle_found: m.checkWrite(writes.unhandle, after([entry(101, "handle_note",
      {note_id: 10, handled: false, reply: null, reply_id: null})])),
    off_the_page: m.checkWrite(writes.hide, after(full, 300)),
    off_the_page_reply: m.checkWrite(writes.reply, after(full, 300)),
    full_page_all_there: m.checkWrite(writes.hide, after(full, 50)),
    failed_read: m.checkWrite(writes.hide, null),
    no_timeline: m.checkWrite(writes.hide, {task: {id: 7}}),
    bulk_listed: m.checkWrite(named, flagged([9, 2])),
    bulk_one_listed: m.checkWrite(named, flagged([5])),
    bulk_gone: m.checkWrite(named, flagged([2, 1])),
    bulk_flood_beyond: m.checkWrite(named, flagged([...flood, 9])),
    bulk_failed: m.checkWrite(named, {flagged: null}),
    bulk_null: m.checkWrite(named, null),
  },
  last_entry: [m.lastEntryId({history: [{id: 3}, {id: 11}, {id: 9}]}),
               m.lastEntryId({history: []}), m.lastEntryId(null)],
  parked: {
    states: m.PARKED_STATES,
    on: ["open", "deferred", "closed", "landing", undefined]
      .map(task_state => m.onParkedTask({id: 1, task_state})),
    none: m.onParkedTask(null),
    select_all: m.selectAll([{id: 9, task_state: "open"}, {id: 8, task_state: "deferred"},
                             {id: 7, task_state: "closed"}, {id: 6, task_state: "landing"},
                             {id: 5}]),
    select_none: [m.selectAll(null), m.selectAll([{id: 4, task_state: "closed"}])],
    // Drawn open, read since as deferred: parked by the newer read; and the other way.
    select_newer: m.selectAll([{id: 9, task_state: "open"}, {id: 8, task_state: "open"},
                               {id: 7, task_state: "deferred"}],
                              [{id: 9, task_state: "open"}, {id: 8, task_state: "closed"},
                               {id: 7, task_state: "open"}]),
    // Ticks: 9 and 8 while open, 7 while closed, 6 gone from the redraw.
    kept: [...m.keptSelection(new Map([[9, false], [8, false], [7, true], [6, false]]),
      [{id: 9, task_state: "open"}, {id: 8, task_state: "deferred"},
       {id: 7, task_state: "closed"}, {id: 5, task_state: "open"}])],
    kept_none: [...m.keptSelection(null, [{id: 9}])],
    tick_mark: [m.tickMark(9, [{id: 9, task_state: "open"}]),
                m.tickMark(9, [{id: 9, task_state: "closed"}]),
                m.tickMark(9, [{id: 9, task_state: "deferred"}]),
                m.tickMark(9, [{id: 8, task_state: "closed"}]), m.tickMark(9, null)],
    left_out_line: [t.leftOutLine([{id: 15, state: "deferred"}, {id: 3, state: "closed\u202e"}]),
                    t.leftOutLine([]), t.leftOutLine(undefined)],
    // Every ordering of tick, poll, redraw and click, as (ticked parked?, drawn copy,
    // last good read's copy) when the click comes. Note 16 is the one whose task moves.
    orders: Object.fromEntries(Object.entries({
      tick_open_then_click: [false, "open", "open"],
      tick_open_poll_parks_click: [false, "open", "deferred"],
      tick_open_poll_parks_redraw_click: [false, "deferred", "deferred"],
      // A poll learned of the park, but the row is still drawn open: not an informed
      // choice, so its tick marks it open, and it is left out.
      poll_parks_then_tick_click: [false, "open", "deferred"],
      redraw_parked_then_tick_click: [true, "deferred", "deferred"],
      tick_parked_poll_reopens_click: [true, "deferred", "open"],
      tick_open_parks_then_reopens_click: [false, "open", "open"],
      tick_open_closed_elsewhere_click: [false, "open", "closed"],
      tick_open_off_the_read_click: [false, "open", null],
      tick_open_drawn_parked_off_the_read: [false, "deferred", null],
    }).map(([name, [mark, shown, read]]) => [name, m.bulkToSend(
      new Map([[16, mark], [15, false]]),
      [{id: 16, task_state: shown}, {id: 15, task_state: "open"}],
      read === null ? [{id: 15, task_state: "open"}]
        : [{id: 16, task_state: read}, {id: 15, task_state: "open"}])])),
    send_none: m.bulkToSend(null, [{id: 1}], []),
    send_undrawn: m.bulkToSend(new Map([[3, false]]), [{id: 1}], [{id: 3}]),
    redraws: [["hide", "landed"], ["handle", "unknown"], ["handle-many", "refused"],
              ["move", "landed"], ["move", "stale"], ["move", "not-running"],
              ["hide", "not-running"], ["break", "landed"], ["note", "landed"]]
      .map(([kind, result]) => m.redrawsFlagged({kind}, result)),
    redraws_none: m.redrawsFlagged(null, "landed"),
  },
  texts: {
    hide_flagged: t.outcomeText({...writes.hide, forYou: true, taskState: "open",
                                 verb: "hide note #10"}, "landed").text,
    hide_flagged_parked: t.outcomeText({...writes.hide, forYou: true, taskState: "deferred",
                                        verb: "hide note #10"}, "landed").text,
    hide_plain: t.outcomeText({...writes.hide, taskState: "open", verb: "hide note #10"},
                              "landed").text,
    show_flagged: t.outcomeText({...writes.show, forYou: true, taskState: "open",
                                 verb: "show note #10"}, "landed").text,
    show_flagged_parked: t.outcomeText({...writes.show, forYou: true, taskState: "closed",
                                        verb: "show note #10"}, "landed").text,
    show_plain: t.outcomeText({...writes.show, taskState: "open", verb: "show note #10"},
                              "landed").text,
    not_seen: t.outcomeText({...writes.hide, verb: "hide note #10"}, "not-seen"),
    reply_not_found: t.outcomeText({...writes.reply, verb: "reply to note #10"},
                                   "not-found-yet").text,
    note_check: t.outcomeText({...writes.hide, verb: "hide note #10"},
                              "could-not-check").text,
    bulk_refused: t.outcomeText(named, "refused", "note 9 not found").text,
    bulk_landed: t.outcomeText(named, "landed").text,
    bulk_left_out: t.outcomeText({...named, notes: [15], verb: "handle 1 note",
                                  leftOut: [{id: 16, state: "deferred"}]}, "landed").text,
    bulk_left_out_refused: t.outcomeText({...named, verb: "handle 1 note",
                                          leftOut: [{id: 16, state: "deferred"}]},
                                         "refused", "no").text,
    bulk_found: t.outcomeText(named, "found"),
    bulk_not_landed: t.outcomeText(named, "not-landed"),
    bulk_check: t.outcomeText(named, "could-not-check").text,
    move_refused: t.outcomeText({kind: "move", verb: "reopen"}, "refused", "no").text,
  },
  line: {
    same: t.flaggedLine([9, 5], {notes: [{id: 9}, {id: 5}], total: 2}),
    newer: t.flaggedLine([9, 5], {notes: [{id: 12}, {id: 9}, {id: 5}], total: 3}),
    gone: t.flaggedLine([9, 5], {notes: [{id: 9}], total: 1}),
    both: t.flaggedLine([9, 5], {notes: [{id: 12}, {id: 9}], total: 2}),
    none_left: t.flaggedLine([9], {notes: [], total: 0}),
    none_drawn_none_left: t.flaggedLine([], {notes: [], total: 0}),
    failed: t.flaggedLine([9], null),
    // A full page holds the newest only: a drawn note older than all of it may be
    // there still, so it is not counted gone.
    full_page: t.flaggedLine([3, 1000],
      {notes: Array.from({length: 50}, (_, i) => ({id: 1049 - i})), total: 70}),
    parked: t.flaggedLine([9, 5, 4], {notes: [{id: 9, task_state: "deferred"},
      {id: 5, task_state: "open"}, {id: 4, task_state: "closed"}], total: 3}),
    parked_page: t.flaggedLine([], {notes: Array.from({length: 50}, (_, i) =>
      ({id: 1049 - i, task_state: i < 2 ? "deferred" : "open"})), total: 70}),
    // The same notes, but a drawn one's task was deferred since, and another reopened;
    // a move between two unparked states changes no mark.
    moved: t.flaggedLine([{id: 9, task_state: "open"}, {id: 5, task_state: "closed"},
                          {id: 4, task_state: "open"}],
      {notes: [{id: 9, task_state: "deferred"}, {id: 5, task_state: "proposed"},
               {id: 4, task_state: "landing"}], total: 3}),
  },
}));
"""


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class NoteMoveTests(unittest.TestCase):
    """The moves on notes, pure halves under node: what each sends, held to the
    registry's request bodies, which refuse a field they do not know; which moves a
    note offers; what a bulk names; how a lost one is decided; what the flagged list's
    count line says."""

    probe: dict

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", _NOTE_MOVES_PROBE],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR)),
                 "TZ": "Europe/Stockholm"},
            capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing moves.js under node failed:\n" + proc.stderr)
        cls.probe = json.loads(proc.stdout)

    def test_each_note_write_sends_what_its_route_takes(self):
        # Path, method and body, each body held to the request model the registry's
        # route reads: an unknown field is refused there (`Body`), and ids and flags
        # are strict, so a string id or a missing flag would fail only on the host.
        api = load_operator_api()
        routes = {"hide": ("/api/notes/hide", api.HideNotes),
                  "show": ("/api/notes/hide", api.HideNotes),
                  "handle": ("/api/notes/10/handle", api.HandleNote),
                  "reply": ("/api/notes/10/handle", api.HandleNote),
                  "unhandle": ("/api/notes/10/handle", api.HandleNote),
                  "handle_many": ("/api/notes/handle", api.HandleNotes)}
        wire = self.probe["wire"]
        for key, (path, model) in routes.items():
            with self.subTest(write=key):
                sent = wire[key]
                self.assertEqual((sent["url"], sent["method"]), (path, "POST"))
                self.assertEqual(set(sent["body"]), set(model.__annotations__))
                for field, value in sent["body"].items():
                    kind = str(model.__annotations__[field])
                    if "StrictInt" in kind:
                        self.assertTrue(all(type(v) is int for v in value), value)
                    if "StrictBool" in kind:
                        self.assertIs(type(value), bool)
        self.assertEqual(wire["hide"]["body"], {"notes": [10], "hidden": True})
        self.assertEqual(wire["show"]["body"], {"notes": [10], "hidden": False})
        self.assertEqual(wire["handle"]["body"], {"handled": True, "reply": None})
        self.assertEqual(wire["reply"]["body"], {"handled": True, "reply": "Seen."})
        self.assertEqual(wire["unhandle"]["body"], {"handled": False, "reply": None})
        self.assertEqual(wire["handle_many"]["body"], {"notes": [9, 5], "handled": True})
        # Each path is one the registry serves, by the route the relay passes.
        source = (ROOT / "task-registry" / "operator_api.py").read_text()
        for route in ('@app.post("/api/notes/hide")', '@app.post("/api/notes/{note}/handle")',
                      '@app.post("/api/notes/handle")'):
            self.assertIn(route, source)

    def test_an_unknown_write_sends_nothing(self):
        self.assertEqual(self.probe["unknown"], {"threw": True, "sent": 0})

    def test_the_moves_on_notes_are_told_apart_from_the_rest(self):
        self.assertEqual(self.probe["is_note_move"],
                         [True, True, True, False, False, False])

    def test_a_note_offers_hide_or_show_and_handle_or_unhandle(self):
        mv = self.probe["moves"]
        self.assertEqual(mv["plain"], [["hide", True, "hide", False]])
        self.assertEqual(mv["hidden"], [["hide", False, "show", False]])
        # Handle opens the reply box; only a note flagged for the operator has one.
        self.assertEqual(mv["flagged"], [["hide", True, "hide", False],
                                         ["handle", True, "mark handled", True]])
        self.assertEqual(mv["handled"], [["hide", True, "hide", False],
                                         ["handle", False, "mark not handled", False]])
        self.assertEqual(mv["flagged_hidden"], [["hide", False, "show", False],
                                                ["handle", True, "mark handled", True]])
        self.assertEqual((mv["no_id"], mv["none"]), ([], []))

    def test_a_bulk_names_what_is_selected_and_drawn_in_drawn_order(self):
        b = self.probe["bulk"]
        self.assertEqual(b["drawn_order"], [9, 3, 5])
        self.assertEqual(b["undrawn"], [5])
        self.assertEqual((b["none"], b["nothing_drawn"]), ([], []))
        self.assertEqual(b["from_list"], [9])

    def test_a_lost_note_move_is_found_by_its_own_newer_entry(self):
        c = self.probe["check"]
        for key in ("hide_found", "show_found", "handle_found", "reply_found",
                    "unhandle_found"):
            with self.subTest(case=key):
                self.assertEqual(c[key], "found")
        # Not newer than the page held, another value, another note, another move.
        for key in ("hide_held", "hide_other_value", "hide_other_note", "handle_missing",
                    "handle_as_hide"):
            with self.subTest(case=key):
                self.assertEqual(c[key], "not-seen")

    def test_a_lost_reply_is_never_safe_to_send_again(self):
        c = self.probe["check"]
        self.assertEqual((c["reply_missing"], c["reply_bare_entry"]),
                         ("not-found-yet", "not-found-yet"))

    def test_a_note_move_whose_entry_may_be_off_the_page_is_not_checked(self):
        c = self.probe["check"]
        self.assertEqual((c["off_the_page"], c["off_the_page_reply"]),
                         ("could-not-check", "could-not-check"))
        # A full page that is the whole timeline holds every entry newer than those held.
        self.assertEqual(c["full_page_all_there"], "not-seen")
        self.assertEqual((c["failed_read"], c["no_timeline"]),
                         ("could-not-check", "could-not-check"))

    def test_a_lost_bulk_is_decided_by_the_flagged_list_past_its_notes(self):
        c = self.probe["check"]
        # All or none: one named note still listed is none landed.
        self.assertEqual((c["bulk_listed"], c["bulk_one_listed"]),
                         ("not-landed", "not-landed"))
        self.assertEqual(c["bulk_gone"], "found")
        self.assertEqual(c["bulk_flood_beyond"], "not-landed")
        self.assertEqual((c["bulk_failed"], c["bulk_null"]),
                         ("could-not-check", "could-not-check"))

    def test_a_note_moves_check_counts_from_the_newest_entry_held(self):
        self.assertEqual(self.probe["last_entry"], [11, 0, 0])

    def test_each_note_move_outcome_says_what_happened(self):
        x = self.probe["texts"]
        # Only what is true for the note's flags and its task's state.
        self.assertEqual(x["hide_plain"], "Hide note #10: done. Agents no longer read it; "
                                          "it stays on its task, marked hidden.")
        self.assertEqual(x["hide_flagged"],
                         "Hide note #10: done. Agents no longer read it; it stays on its "
                         "task, marked hidden. It leaves the flagged list and the inbox's "
                         "count.")
        # A note on a parked task is in no inbox count to leave.
        self.assertEqual(x["hide_flagged_parked"],
                         "Hide note #10: done. Agents no longer read it; it stays on its "
                         "task, marked hidden. It leaves the flagged list.")
        self.assertEqual(x["show_plain"], "Show note #10: done. Agents read it again.")
        self.assertEqual(x["show_flagged"],
                         "Show note #10: done. Agents read it again. It is back in the "
                         "flagged list and the inbox's count.")
        self.assertEqual(x["show_flagged_parked"],
                         "Show note #10: done. Agents read it again. It is back in the "
                         "flagged list.")
        self.assertIn("harmless", x["not_seen"]["text"])
        self.assertTrue(x["not_seen"]["warn"])
        self.assertIn("the reply is not on the task yet", x["reply_not_found"])
        self.assertIn("timeline", x["note_check"])
        self.assertIn("not known", x["note_check"])
        self.assertEqual(x["bulk_refused"], "Handle 2 notes: refused, so none of them "
                                            "was handled. note 9 not found")
        self.assertEqual(x["bulk_landed"], "Handle 2 notes: done.")
        self.assertFalse(x["bulk_found"]["warn"])
        self.assertIn("none of them is flagged any more", x["bulk_found"]["text"])
        self.assertIn("harmless", x["bulk_not_landed"]["text"])
        self.assertNotIn("cannot apply it twice", x["bulk_not_landed"]["text"])
        self.assertIn("flagged notes could not be read again", x["bulk_check"])
        self.assertEqual(x["move_refused"], "Reopen: refused. no")

    def test_the_count_line_says_what_changed_since_the_rows_were_drawn(self):
        line = self.probe["line"]
        self.assertEqual(line["same"], {"text": "2 not handled", "warn": False,
                                        "show": False})
        self.assertEqual(line["newer"], {"text": "3 not handled · 1 not shown",
                                         "warn": False, "show": True})
        self.assertEqual(line["gone"], {"text": "1 not handled · 1 gone since",
                                        "warn": False, "show": True})
        self.assertEqual(line["both"]["text"], "2 not handled · 1 not shown · 1 gone since")
        self.assertEqual(line["none_left"], {"text": "none left · 1 gone since",
                                             "warn": False, "show": True})
        self.assertEqual(line["none_drawn_none_left"],
                         {"text": "none left", "warn": False, "show": False})
        self.assertTrue(line["failed"]["warn"])
        self.assertFalse(line["failed"]["show"])
        self.assertIn("as last read", line["failed"]["text"])
        self.assertEqual(line["full_page"]["text"], "70 not handled · 49 not shown")
        # Notes waiting with a parked task are counted apart, so this total and the
        # inbox's can be told apart; among a page, said so when the page is not all.
        self.assertEqual(line["parked"]["text"],
                         "3 not handled · 2 on a deferred or closed task")
        self.assertEqual(line["parked_page"]["text"],
                         "70 not handled · 2 on a deferred or closed task among the "
                         "newest 50 · 50 not shown")

    def test_a_task_parked_since_the_draw_is_told_and_never_swept_up(self):
        k = self.probe["parked"]
        # By either copy: a task deferred since the draw, or one the draw had parked.
        self.assertEqual(k["select_newer"], [9])
        # A redraw drops a ticked note whose task was parked since, and keeps one already
        # parked when it was ticked, which the operator chose on its own, with its mark.
        self.assertEqual(sorted(k["kept"]), [[7, True], [9, False]])
        self.assertEqual(k["kept_none"], [])
        # A tick is marked by its row as drawn, never by a park only a poll has seen.
        self.assertEqual(k["tick_mark"], [False, True, True, False, False])
        # Beside the handle, what it would leave out, spelled.
        self.assertEqual(k["left_out_line"],
                         ["#15 left out: its task was deferred; #3 left out: its task was "
                          "closed\\u202e", "", ""])
        # Rows follow a move on notes or on a task, whatever came of it, but not one the
        # registry never got.
        self.assertEqual(k["redraws"], [True, True, True, True, True, False, False,
                                        False, False])
        self.assertFalse(k["redraws_none"])
        line = self.probe["line"]["moved"]
        self.assertEqual(line, {"text": "3 not handled · 1 on a deferred or closed task · "
                                        "2 whose task was deferred, closed or reopened "
                                        "since", "warn": False, "show": True})

    def test_a_note_parked_since_its_tick_is_never_sent_whatever_the_order(self):
        k = self.probe["parked"]
        o = k["orders"]
        left = {"notes": [15], "leftOut": [{"id": 16, "state": "deferred"}]}
        both = {"notes": [16, 15], "leftOut": []}
        self.assertEqual(o["tick_open_then_click"], both)
        # Seen only by a poll, or by a redraw too: left out, and said so.
        self.assertEqual(o["tick_open_poll_parks_click"], left)
        self.assertEqual(o["tick_open_poll_parks_redraw_click"], left)
        self.assertEqual(o["tick_open_closed_elsewhere_click"],
                         {"notes": [15], "leftOut": [{"id": 16, "state": "closed"}]})
        # Ticked on a row drawn open after a poll learned of the park: left out.
        self.assertEqual(o["poll_parks_then_tick_click"], left)
        # Ticked on a row marked parked: chosen as it is, and sent.
        self.assertEqual(o["redraw_parked_then_tick_click"], both)
        self.assertEqual(o["tick_parked_poll_reopens_click"], both)
        # Reopened again before the click: nothing to leave out.
        self.assertEqual(o["tick_open_parks_then_reopens_click"], both)
        # A note the read no longer carries is judged by its drawn copy.
        self.assertEqual(o["tick_open_off_the_read_click"], both)
        self.assertEqual(o["tick_open_drawn_parked_off_the_read"], left)
        self.assertEqual(k["send_none"], {"notes": [], "leftOut": []})
        self.assertEqual(k["send_undrawn"], {"notes": [], "leftOut": []})
        # The outcome names each note left out, whatever the bulk came to.
        self.assertEqual(self.probe["texts"]["bulk_left_out"],
                         "Handle 1 note: done. Note #16 left out: its task was deferred "
                         "since the list was drawn.")
        self.assertTrue(self.probe["texts"]["bulk_left_out_refused"].endswith(
            "Note #16 left out: its task was deferred since the list was drawn."))

    def test_a_note_on_a_parked_task_is_never_swept_up_by_select_all(self):
        k = self.probe["parked"]
        self.assertEqual(k["states"], ["deferred", "closed"])
        self.assertEqual(k["on"], [False, True, True, False, False])
        self.assertFalse(k["none"])
        self.assertEqual(k["select_all"], [9, 6, 5])
        self.assertEqual(k["select_none"], [[], []])
        # The parked states are the ones the inbox holds a flagged note back for.
        moves = (ROOT / "task-registry" / "operator_moves.py").read_text()
        held = re.search(r"t\.state NOT IN \(([^)]*)\)", moves).group(1)
        self.assertEqual(re.findall(r"'([^']+)'", held), k["states"])
        # And the field is the one the registry's flagged read gives each note.
        self.assertIn('"task_state": r["task_state"]', moves)


_CHANGES_PROBE = r"""
import { pathToFileURL } from "node:url";
const t = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/tasks.js`).href);
const d = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/diff.js`).href);
// What a diff's runs make of each side, beside the runs themselves.
const sides = r => ({
  runs: r.runs, rewritten: r.rewritten,
  before: r.runs.filter(x => x.op !== "add").map(x => x.text).join(""),
  after: r.runs.filter(x => x.op !== "del").map(x => x.text).join(""),
});
const words = (n, w) => Array.from({length: n}, (_, i) => `${w}${i}`).join(" ");
const big = [words(d.EDIT_MAX, "a"), words(d.EDIT_MAX, "b")];
const plan = {id: 5, approach: "Relay first, then the page.", footprint: ["a.py", "b/"],
              tests: "Under node.", out_of_scope: "None.", risks: "Races.",
              review_verdict: "Passes."};
const previous = {id: 3, approach: "Relay first.", footprint: ["a.py"],
                  tests: "Under node.", out_of_scope: "None. ", risks: "",
                  review_verdict: undefined};
const note = {id: 4, text: "Land it?", for_operator: true, handled: true, hidden: true,
              move: null, reply_to: null};
const reply = {id: 7, text: "Yes.", for_operator: false, handled: false, hidden: false,
               move: null, reply_to: 4};
const shown = {id: 7, revision: 5, notes: [{id: 3}]};
console.log(JSON.stringify({
  word: {
    equal: sides(d.wordDiff("the fox", "the fox")),
    insert: sides(d.wordDiff("the fox", "the red fox")),
    delete: sides(d.wordDiff("the red fox", "the fox")),
    replace: sides(d.wordDiff("the quick fox", "the slow fox")),
    whitespace: sides(d.wordDiff("a b\nc", "a  b c")),
    hidden: sides(d.wordDiff("approve it", "approve\u200b it")),
    from_nothing: sides(d.wordDiff("", "new")),
    rephrased: sides(d.wordDiff("Keep it in one change, then test.",
                                "Keep it small, then test.")),
    ceiling: sides(d.wordDiff(...big)),
    under_ceiling: d.wordDiff(words(200, "a"), words(200, "b")).rewritten,
    lines: sides(d.lineDiff("a.py\nb/", "a.py\nb/\nc")),
  },
  big,
  plan: t.planDiff(plan, previous).map(s => [s.label, s.kind, s.whitespaceOnly ?? null,
                                             t.changeNote(s)]),
  footprint: t.planDiff(plan, previous)[1].runs,
  approach: sides(t.planDiff(plan, previous)[0]),
  same_plan: t.planDiff(plan, plan).map(s => s.kind),
  same_hidden: t.planDiff({tests: "run \u202e it"}, {tests: "run \u202e it"})
    .filter(s => s.label === "tests").map(s => [s.kind, s.runs]),
  not_whitespace: ["\ufeff", "\u2028", "\u00a0"].map(c =>
    t.planDiff({tests: `a${c}b`}, {tests: "a b"})
      .filter(s => s.label === "tests").map(s => t.changeNote(s))[0]),
  predecessor: {
    newest: t.planPredecessor([{id: 5}, {id: 3}, {id: 2}], 5),
    by_string: t.planPredecessor([{id: 5}, {id: 3}, {id: 2}], "3"),
    string_ids: t.planPredecessor([{id: "5"}, {id: "3"}], 5),
    unordered: t.planPredecessor([{id: 2}, {id: 5}, {id: 3}], 5),
    first: t.planPredecessor([{id: 5}, {id: 3}, {id: 2}], 2),
    not_held: t.planPredecessor([{id: 5}, {id: 3}], 9),
    none: t.planPredecessor(null, 5),
    holes: t.planPredecessor([null, {id: 5}, {id: 4}], 5),
  },
  entry: {
    note: t.timelineEntry({action: "note", author: "claude-a", ts: 1,
                           detail: {note: "Land it?", note_id: 4, for_operator: true},
                           note}),
    handle: t.timelineEntry({action: "handle_note", author: "operator", ts: 2,
                             detail: {note_id: 4, handled: true, reply: "Yes.",
                                      reply_id: 7, actor: null},
                             note: reply}),
    handle_bare: t.timelineEntry({action: "handle_note", author: "operator", ts: 2,
                                  detail: {note_id: 4, handled: true, reply: null,
                                           reply_id: null}, note: null}),
    hide: t.timelineEntry({action: "hide_note", author: "operator", ts: 3,
                           detail: {note_id: 4, hidden: true}, note: null}),
    plan: t.timelineEntry({action: "submit_plan", author: "claude-a", ts: 4,
                           detail: {note: null, note_id: null, approach: "A."},
                           note: null}),
    none: t.timelineEntry(null),
  },
  update: {
    nothing_shown: t.taskUpdate(null, shown),
    same: t.taskUpdate(shown, {...shown}),
    newer: t.taskUpdate(shown, {...shown, revision: 6}),
    new_note: t.taskUpdate(shown, {...shown, notes: [{id: 3}, {id: 4}]}),
    other_task: t.taskUpdate(shown, {...shown, id: 8, revision: 9}),
    older: t.taskUpdate(shown, {...shown, revision: 4, notes: [{id: 9}]}),
    string_id: t.taskUpdate(shown, {...shown, id: "7", revision: 6}),
  },
}));
"""


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class ChangeTests(unittest.TestCase):
    """What makes a revision quick to approve, the pure halves: a plan as what changed
    from the one before (``diff.js``, ``planDiff``, ``planPredecessor``), one timeline of
    notes and the other writes (``timelineEntry``), and what a poll finds against the
    task drawn (``taskUpdate``)."""

    probe: dict

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", _CHANGES_PROBE],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR)),
                 "TZ": "Europe/Stockholm"},
            capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing diff.js under node failed:\n" + proc.stderr)
        cls.probe = json.loads(proc.stdout)

    def test_a_word_diff_keeps_both_sides_whole(self):
        for key, (before, after) in {
                "equal": ("the fox", "the fox"), "insert": ("the fox", "the red fox"),
                "delete": ("the red fox", "the fox"),
                "replace": ("the quick fox", "the slow fox"),
                "whitespace": ("a b\nc", "a  b c"),
                "hidden": ("approve it", "approve\u200b it"),
                "from_nothing": ("", "new")}.items():
            with self.subTest(case=key):
                got = self.probe["word"][key]
                self.assertEqual((got["before"], got["after"], got["rewritten"]),
                                 (before, after, False))

    def test_a_word_diff_finds_the_words_that_changed(self):
        w = self.probe["word"]
        self.assertEqual(w["equal"]["runs"], [{"op": "same", "text": "the fox"}])
        self.assertEqual([r["text"].strip() for r in w["insert"]["runs"]
                          if r["op"] == "add"], ["red"])
        self.assertEqual([r["op"] for r in w["delete"]["runs"]], ["same", "del", "same"])
        self.assertEqual(w["replace"]["runs"], [
            {"op": "same", "text": "the "}, {"op": "del", "text": "quick"},
            {"op": "add", "text": "slow"}, {"op": "same", "text": " fox"}])
        self.assertEqual(w["from_nothing"]["runs"], [{"op": "add", "text": "new"}])

    def test_a_rephrased_stretch_reads_as_what_went_then_what_came(self):
        # Not word by word: the space between two changed words is part of the change.
        runs = self.probe["word"]["rephrased"]["runs"]
        self.assertEqual([(r["op"], r["text"].strip()) for r in runs], [
            ("same", "Keep it"), ("del", "in one change,"), ("add", "small,"),
            ("same", "then test.")])
        self.assertEqual(self.probe["word"]["rephrased"]["after"],
                         "Keep it small, then test.")

    def test_whitespace_and_a_hidden_character_are_changes(self):
        w = self.probe["word"]
        self.assertEqual([(r["op"], r["text"]) for r in w["whitespace"]["runs"]
                          if r["op"] != "same"],
                         [("del", " "), ("add", "  "), ("del", "\n"), ("add", " ")])
        # Raw text compared: the page spells the run where it draws it.
        self.assertEqual([(r["op"], r["text"]) for r in w["hidden"]["runs"]
                          if r["op"] != "same"],
                         [("del", "approve"), ("add", "approve\u200b")])

    def test_past_the_ceiling_a_text_is_rewritten_whole(self):
        ceiling = self.probe["word"]["ceiling"]
        self.assertTrue(ceiling["rewritten"])
        self.assertEqual(ceiling["runs"], [{"op": "del", "text": self.probe["big"][0]},
                                           {"op": "add", "text": self.probe["big"][1]}])
        self.assertFalse(self.probe["word"]["under_ceiling"])
        src = (TASKS_DIR / "diff.js").read_text()
        self.assertIn("const max = Math.min(n + m, EDIT_MAX);", src)

    def test_a_line_diff_keeps_each_line_whole(self):
        self.assertEqual(self.probe["word"]["lines"]["runs"],
                         [{"op": "same", "text": "a.py\nb/\n"}, {"op": "add", "text": "c\n"}])

    def test_a_plan_diff_says_each_section_unchanged_added_removed_or_changed(self):
        self.assertEqual(self.probe["plan"], [
            ["approach", "changed", False, ""],
            ["footprint", "changed", False, ""],
            ["tests", "unchanged", None, ""],
            ["out of scope", "changed", True, "changed in whitespace only"],
            ["risks", "added", None, "added"],
            ["review verdict", "added", None, "added"]])
        self.assertEqual(self.probe["approach"]["after"], "Relay first, then the page.")
        self.assertEqual(self.probe["same_plan"], ["unchanged"] * 6)

    def test_an_unchanged_section_with_a_hidden_character_keeps_its_text(self):
        # approveHidden counts the whole plan: what it counts must be drawn spelled.
        self.assertEqual(self.probe["same_hidden"],
                         [["unchanged", [{"op": "same", "text": "run \u202e it"}]]])
        src = (TASKS_DIR / "tasks.js").read_text()
        self.assertIn('if (section.kind === "unchanged" && !section.runs.length) {', src)

    def test_a_hidden_character_is_not_whitespace(self):
        self.assertEqual(self.probe["not_whitespace"], ["", "", ""])

    def test_a_plan_diff_takes_the_footprint_path_by_path(self):
        self.assertEqual(self.probe["footprint"], [{"op": "same", "text": "a.py\n"},
                                                   {"op": "add", "text": "b/\n"}])

    def test_a_section_emptied_is_removed(self):
        src = (TASKS_DIR / "tasks.js").read_text()
        self.assertIn('if (text === "") return { label, kind: "removed", '
                      'runs: [{ op: "del", text: was }] };', src)

    def test_the_predecessor_is_the_next_lower_plan_held(self):
        p = self.probe["predecessor"]
        self.assertEqual((p["newest"]["id"], p["by_string"]["id"], p["string_ids"]["id"],
                          p["unordered"]["id"], p["holes"]["id"]), (3, 2, "3", 3, 4))
        # The first plan has none; nor does one the page does not hold, as a refusal's.
        for key in ("first", "not_held", "none"):
            with self.subTest(case=key):
                self.assertIsNone(p[key])

    def test_a_note_is_shown_once_with_its_marks(self):
        e = self.probe["entry"]["note"]
        self.assertEqual(e["note"]["text"], "Land it?")
        # Hidden is said as what it does: the operator still reads it, agents do not.
        self.assertEqual(e["marks"], ["#4", "for you", "handled", "hidden from agents"])
        self.assertEqual(e["folded"], [["for_operator", True]])
        self.assertEqual(e["named"], "")

    def test_a_handle_shows_its_reply_once_and_the_note_it_answers(self):
        e = self.probe["entry"]["handle"]
        self.assertEqual(e["note"]["text"], "Yes.")
        self.assertIn("reply to #4", e["marks"])
        self.assertEqual(e["named"], "#4")
        self.assertEqual([key for key, _ in e["folded"]], ["handled", "actor"])
        bare = self.probe["entry"]["handle_bare"]
        self.assertEqual((bare["note"], bare["named"]), (None, "#4"))
        self.assertEqual([key for key, _ in bare["folded"]],
                         ["handled", "reply", "reply_id"])

    def test_a_hide_names_its_note_in_the_open_and_carries_none(self):
        e = self.probe["entry"]["hide"]
        self.assertEqual((e["note"], e["marks"], e["named"]), (None, [], "#4"))
        self.assertEqual(e["folded"], [["hidden", True]])

    def test_a_write_that_posted_no_note_keeps_its_detail(self):
        e = self.probe["entry"]["plan"]
        self.assertEqual([key for key, _ in e["folded"]], ["note", "note_id", "approach"])
        self.assertIsNone(self.probe["entry"]["none"])

    def test_a_poll_says_changed_new_notes_or_nothing(self):
        u = self.probe["update"]
        self.assertEqual(u["newer"]["kind"], "changed")
        self.assertIn("revision 6, this shows 5", u["newer"]["text"])
        self.assertEqual(u["string_id"]["kind"], "changed")
        self.assertEqual(u["new_note"]["kind"], "notes")
        for key in ("nothing_shown", "same", "other_task", "older"):
            with self.subTest(case=key):
                self.assertIsNone(u[key])


class TaskTimelineSourceTests(unittest.TestCase):
    """The detail's timeline and the line atop it that says the task changed, from
    source: a browser is the only thing that runs them."""

    TASKS = (TASKS_DIR / "tasks.js").read_text()
    HTML = (TASKS_DIR / "index.html").read_text()

    def test_the_poll_reads_the_open_task_only_when_it_can_compare(self):
        body = _function(self.TASKS, "refreshOpenTask")
        self.assertEqual(body.splitlines()[1].strip(),
                         "if (!registryIsRunning() || writing || !drawnIsOpen()) return;")

    def test_an_answer_a_draw_or_a_write_overtook_is_dropped(self):
        body = _function(self.TASKS, "refreshOpenTask")
        check = "if (draws !== asked || writing || !drawnIsOpen()) return;"
        self.assertLess(body.index("const asked = draws;"), body.index("await fetch("))
        self.assertLess(body.index("await fetch("), body.index(check))
        self.assertLess(body.index(check), body.index("taskUpdate("))
        # Every draw counts, the 409's and a write's redraw among them.
        self.assertIn("draws += 1;", _function(self.TASKS, "drawTask"))
        self.assertEqual(len(re.findall(r"\bdraws \+= 1;", self.TASKS)), 1)

    def test_the_poll_fills_the_slot_and_draws_nothing_else(self):
        body = _function(self.TASKS, "refreshOpenTask")
        for drawing in ("drawTask(", "taskDetail", "replaceChildren(", "appendChild(",
                        "noticeSlot =", "outcome =", "shownTask =", "shownHeld ="):
            with self.subTest(drawing=drawing):
                self.assertNotIn(drawing, body)
        self.assertEqual(body.count("fillSlot("), 1)
        # Filled, or its notice replaced, never emptied: only a draw makes a new slot.
        self.assertIn("noticeSlot.replaceChildren(show, text);",
                      _function(self.TASKS, "fillSlot"))
        self.assertEqual(re.findall(r"\bnoticeSlot = ([^;]*);", self.TASKS),
                         ["null", 'document.createElement("div")'])
        self.assertIn("noticeSlot = document.createElement(", _function(self.TASKS, "drawTask"))
        # A refused write's notice outweighs a note's.
        self.assertIn('(update.kind === "notes" && noticeSlot.dataset.kind === "stale")',
                      body)

    def test_the_slot_is_shared_with_a_refused_write_and_keeps_its_space(self):
        draw = _function(self.TASKS, "drawTask")
        self.assertIn('outcome.result === "stale"', draw)
        self.assertIn("fillSlot(outcome);", draw)
        self.assertIn('outcome.result !== "stale"', _function(self.TASKS, "movesBlock"))
        self.assertIn("const said = { task: write.task, result, onNote: isNoteMove(write),",
                      _function(self.TASKS, "runWrite"))
        # Atop the detail, there whether it says anything or not, one fixed line high.
        self.assertLess(draw.index("taskDetail.appendChild(noticeSlot);"),
                        draw.index("taskDetail.appendChild(head);"))
        rule = re.search(r"\.notice-slot \{([^}]*)\}", self.HTML).group(1)
        self.assertRegex(rule, r"\bheight: \d+px;")
        self.assertIn("overflow: hidden;", rule)
        self.assertIn("white-space: nowrap;", rule)
        self.assertRegex(self.HTML, r"\.notice-slot:not\(:empty\) \{ position: sticky; top: 0;")

    def test_show_it_reads_the_task_again_through_the_hash(self):
        self.assertIn('if (ev.target.closest("button[data-show]")) {\n'
                      "      goTo(view, openTask);", self.TASKS)
        self.assertIn('show.dataset.show = "1";', _function(self.TASKS, "fillSlot"))

    def test_the_timeline_reads_oldest_first_earlier_pages_above(self):
        draw = _function(self.TASKS, "drawTask")
        self.assertIn('olderCursor(timeline.history, "oldest-first", 0, timeline.total)',
                      draw)
        self.assertLess(draw.index('pagerButton("earlier", "history", earlier)'),
                        draw.index("taskDetail.appendChild(timelineTable);"))
        page = _function(self.TASKS, "showPage")
        self.assertLess(page.index("holder.appendChild(next);"),
                        page.index("holder.appendChild(table);"))
        self.assertIn("renderTimeline(tbody, page.history);", page)

    def test_the_detail_reads_the_timeline_and_no_notes(self):
        # A task's notes come with its timeline; the flagged list is not a task's.
        read = _function(self.TASKS, "readTask")
        self.assertIn("fetch(`/api/tasks/${path}/history`)", read)
        self.assertNotRegex(self.TASKS, r"/api/tasks/[^`\n]*/notes")

    def test_diff_js_imports_nothing_and_draws_nothing(self):
        src = (TASKS_DIR / "diff.js").read_text()
        self.assertNotRegex(_code(src), r"\bimport\b")
        self.assertNotIn("document", _code(src))


class TaskWriteSourceTests(unittest.TestCase):
    """How the page sends its writes and draws what they came to, from source, as the
    polling guards are: a browser is the only thing that runs it."""

    TASKS = (TASKS_DIR / "tasks.js").read_text()
    MOVES = (TASKS_DIR / "moves.js").read_text()
    POST = re.compile(r'fetch\(`([^`]+)`,\s*\{\s*method: "POST"')

    def _function(self, src: str, name: str) -> str:
        return re.search(rf"^(?:export )?(?:async )?function {name}\(.*?^\}}", src,
                         re.M | re.S).group(0)

    def test_each_relayed_post_is_a_post_fetch_in_moves_js_and_back(self):
        # PageChecks matches paths, not methods: this holds the methods.
        posts = [re.sub(r"\$\{[^}]*\}", "1", p) for p in self.POST.findall(self.MOVES)]
        self.assertEqual(len(posts), 7)
        routes = [rx for method, rx in tui._RELAY_ROUTES if method == "POST"]
        for path in posts:
            with self.subTest(path=path):
                self.assertTrue(tui._relay_allowed("POST", path), path)
        for rx in routes:
            with self.subTest(route=rx.pattern):
                self.assertTrue(any(rx.match(p) for p in posts), rx.pattern)
        # Every fetch in moves.js is one of them; tasks.js posts nothing.
        self.assertEqual(len(re.findall(r"\bfetch\(", self.MOVES)), 7)
        self.assertNotIn("method:", self.TASKS)

    def test_moves_js_imports_nothing_of_tasks_js(self):
        self.assertNotRegex(self.MOVES, r'from\s+"\./tasks\.js"')
        self.assertNotIn("document", _code(self.MOVES))

    def test_one_write_at_a_time(self):
        body = self._function(self.TASKS, "runWrite")
        lines = [ln.strip() for ln in body.splitlines()[1:4]]
        self.assertEqual(lines, ["if (writing || !registryIsRunning()) return;",
                                 "writing = write;", "markWriting();"])
        # Off before the send, back on only once its outcome is drawn.
        self.assertLess(body.index("writing = write;"), body.index("sendWrite(write)"))
        self.assertLess(body.index("sendWrite(write)"), body.index("writing = null;"))
        mark = self._function(self.TASKS, "markWriting")
        self.assertIn("el.disabled = writing !== null || !drawnIsOpen();", mark)
        self.assertIn("textarea[data-reply]", mark)
        self.assertIn("markWriting();", self._function(self.TASKS, "drawTask"))
        # An earlier timeline page's note moves come in gated too.
        self.assertIn("markWriting();", self._function(self.TASKS, "showPage"))
        # The bulk is sent by the one runner too, and nothing else sends.
        self.assertEqual(len(re.findall(r"\bsendWrite\(", _code(self.TASKS))), 1)
        self.assertEqual(len(re.findall(r"\brunWrite\(", _code(self.TASKS))), 3,
                         "defined once, called from the detail and the flagged block")
        self.assertIn("const write = bulkWrite();\n      if (write) runWrite(write);",
                      self.TASKS)
        # The flagged controls are off while any write is out, whatever task is open.
        self.assertIn("markFlagged();", mark)
        flagged = self._function(self.TASKS, "markFlagged")
        self.assertIn("el.disabled = writing !== null;", flagged)
        self.assertIn("flaggedAll.disabled = writing !== null ||", flagged)
        self.assertIn("flaggedHandle.disabled = writing !== null || !named.length;",
                      flagged)

    def test_a_write_is_for_the_task_drawn_and_only_while_the_page_names_it(self):
        # While another task loads, the one still drawn is not what a click is for:
        # its controls go off before the read, and a write is built from what is drawn,
        # its id with its revision, never the id the hash has moved on to.
        show = self._function(self.TASKS, "showTask")
        self.assertLess(show.index("openTask = id;"), show.index("markWriting();"))
        self.assertLess(show.index("markWriting();"), show.index("readTask(id)"))
        self.assertIn("return shownTask !== null && String(shownTask.id) === openTask;",
                      self._function(self.TASKS, "drawnIsOpen"))
        write_for = self._function(self.TASKS, "writeFor")
        self.assertEqual(write_for.splitlines()[1].strip(), "if (!drawnIsOpen()) return null;")
        self.assertIn("task: String(shownTask.id), revision: shownTask.revision", write_for)
        self.assertNotIn("openTask", write_for)
        # The note's draft is keyed to the task drawn too.
        self.assertIn("noteDraft = { task: String(shownTask.id), text: ev.target.value };",
                      self.TASKS)

    def test_a_stale_write_is_drawn_from_the_task_it_carries_and_not_read_again(self):
        body = self._function(self.TASKS, "runWrite")
        stale = body[body.index('} else if (result === "stale") {'):]
        stale = stale[:stale.index("} else if (write.task !== null) {")]
        self.assertIn("answer.body.current", stale)
        self.assertNotIn("readTask(", stale)
        # The timeline and plans the page holds, not read again either.
        self.assertIn("held: shownHeld", stale)

    def test_a_lost_answer_is_decided_from_a_read_and_never_resent(self):
        body = self._function(self.TASKS, "runWrite")
        self.assertIn('if (result === "unknown") {\n    result = checkWrite(write, '
                      'write.kind === "handle-many"\n'
                      '      ? { flagged: await readFlaggedPast(write.notes).catch(() => '
                      'null) } : drawn);', body)
        self.assertEqual(body.count("sendWrite("), 1)
        # A lost bulk reads the list from just past the highest note it named, so a
        # flood flagged since cannot push the named ones off the page read.
        past = self._function(self.TASKS, "readFlaggedPast")
        self.assertIn("before: String(Math.max(...notes) + 1)", past)
        self.assertIn("fetch(`/api/notes/flagged?${q}`)", past)

    def test_its_outcome_is_drawn_only_while_its_task_is_open(self):
        body = self._function(self.TASKS, "runWrite")
        opened = "if (write.task !== null && openTask === write.task) {"
        self.assertIn(opened, body)
        self.assertLess(body.index(opened), body.index("drawTask("))
        self.assertIn("if (result !== \"not-running\") {", body)
        # A bulk's outcome is the flagged block's, a note move's above the timeline.
        self.assertIn('if (write.kind === "handle-many") flaggedOutcome = said;', body)
        self.assertIn('outcome = write.kind === "handle-many" ? null : said;', body)
        self.assertIn("flaggedOutcome.text", self._function(self.TASKS, "renderFlagged"))
        draw = self._function(self.TASKS, "drawTask")
        self.assertIn("outcome.onNote", draw)
        self.assertLess(draw.index("outcome.onNote"), draw.index("timelineHead"))
        self.assertIn("!outcome.onNote", self._function(self.TASKS, "movesBlock"))
        # The slot tells a refusal by `kind` first: an outcome carries none of its own.
        self.assertIn("noticeSlot.dataset.kind = notice.kind || notice.result;",
                      _function(self.TASKS, "fillSlot"))
        self.assertNotRegex(_function(self.TASKS, "runWrite"),
                            r"const said = \{[^}]*\bkind:")

    def test_a_note_is_kept_until_it_lands(self):
        body = self._function(self.TASKS, "runWrite")
        self.assertIn('const landed = result === "landed" || result === "found";', body)
        self.assertIn('write.kind === "note" && landed', body)
        form = self._function(self.TASKS, "noteForm")
        self.assertIn("noteDraft.task === String(task.id) ? noteDraft.text", form)
        # A reply too, by its note, and its box stays open while the draft is kept.
        self.assertIn('if (write.kind === "handle" && write.handled && landed) {\n'
                      "    replyDrafts.delete(String(write.note));", body)
        box = self._function(self.TASKS, "replyBox")
        self.assertIn("box.hidden = !replyDrafts.has(id);", box)
        self.assertIn('text.value = replyDrafts.get(id) ?? "";', box)
        self.assertIn("replyDrafts.set(ev.target.dataset.reply, ev.target.value);",
                      self.TASKS)
        # A reply goes only with text in it.
        self.assertIn("return reply.trim() ? {", self._function(self.TASKS, "noteWrite"))

    def test_no_move_is_drawn_in_an_inbox_row(self):
        # The bulk handle is the one write outside the detail, and its control is the
        # flagged block's own, never a row's.
        for name in ("inboxRow", "renderInbox", "renderTasks", "renderFlagged",
                     "flaggedRow"):
            with self.subTest(function=name):
                self.assertNotIn("writeButton(", self._function(self.TASKS, name))
                self.assertNotIn("dataset.write", self._function(self.TASKS, name))
        calls = re.findall(r"\bmovesBlock\(", self.TASKS)
        self.assertEqual(len(calls), 2, "defined once, called once, in drawTask")
        self.assertIn("movesBlock(task)", self._function(self.TASKS, "drawTask"))
        self.assertEqual(len(re.findall(r"\bbulkWrite\(", self.TASKS)), 2,
                         "defined once, called once, from the flagged block")
        # A bulk re-reads the open task only when a note it names is on it.
        bulk = self._function(self.TASKS, "bulkWrite")
        self.assertIn("const onOpen = flaggedDrawn.some(n => notes.includes(n.id) && "
                      "String(n.task) === openTask);", bulk)
        self.assertIn("task: onOpen ? openTask : null,", bulk)
        # "select all shown" ticks only what `selectAll` gives, and is off when that is
        # nothing; a note on a parked task is ticked on its own.
        self.assertIn("for (const id of selectAll(flaggedDrawn, newestFlagged())) "
                      "flaggedSelected.set(id, false);", self.TASKS)
        # A tick records whether its task was parked then, by the newest state known.
        self.assertIn("if (box.checked) flaggedSelected.set(id, tickMark(id, flaggedDrawn));",
                      self.TASKS)
        # The last good read: a failed one forgets no park it had learned.
        self.assertIn("return flaggedKnown;", self._function(self.TASKS, "newestFlagged"))
        read = self._function(self.TASKS, "readFlagged")
        self.assertIn("if (flaggedRead) flaggedKnown = flaggedRead.notes;", read)
        self.assertEqual(re.findall(r"\bflaggedKnown = ([^;]*);", self.TASKS),
                         ["[]", "flaggedRead.notes"])
        # The bulk and the button's count are built by the same rule, and a poll's
        # count line brings the controls up to date without moving a row.
        self.assertIn("bulkToSend(flaggedSelected, flaggedDrawn, newestFlagged())",
                      self._function(self.TASKS, "bulkWrite"))
        mark = self._function(self.TASKS, "markFlagged")
        self.assertIn("const { notes: named, leftOut } = bulkToSend(flaggedSelected, "
                      "flaggedDrawn,\n                                               "
                      "newestFlagged());", mark)
        self.assertIn("flaggedLeftOut.textContent = leftOutLine(leftOut);", mark)
        line = self._function(self.TASKS, "renderFlaggedLine")
        self.assertTrue(line.rstrip("}\n ").endswith("markFlagged();"))
        self.assertNotIn("flaggedBody", line)
        self.assertIn("leftOut,", self._function(self.TASKS, "bulkWrite"))
        self.assertIn("box.checked = flaggedSelected.has(Number(box.dataset.flaggedNote));",
                      self.TASKS)
        self.assertIn("flaggedAll.disabled = writing !== null || "
                      "!selectAll(flaggedDrawn, newestFlagged()).length;",
                      self._function(self.TASKS, "markFlagged"))
        row = self._function(self.TASKS, "flaggedRow")
        self.assertIn("if (onParkedTask(n)) {", row)
        self.assertIn("spellHidden(String(n.task_state)).text", row)
        # Only a note flagged and not handled is in the lists a hide takes it out of.
        self.assertIn('if (note.for_operator && !note.handled) btn.dataset.forYou = "1";',
                      self._function(self.TASKS, "noteControls"))
        # A hide's outcome is told by the task's state as drawn when it was clicked.
        self.assertIn("taskState: shownTask.state,", self._function(self.TASKS, "noteWrite"))
        # A typed reply is not dropped by the wrong click.
        mark = self._function(self.TASKS, "markReply")
        self.assertIn('button[data-write="handle"][data-value="true"]', mark)
        self.assertIn('if (plain && (replyDrafts.get(box.dataset.replyBox) ?? "").trim()) '
                      "plain.disabled = true;", mark)
        self.assertIn("for (const box of taskDetail.querySelectorAll(\"[data-reply-box]\")) "
                      "markReply(box);", self._function(self.TASKS, "markWriting"))
        # The note moves are on the timeline, on every page of it.
        self.assertIn("td.appendChild(noteControls(entry.note));",
                      self._function(self.TASKS, "renderTimeline"))
        self.assertIn("noteMovesFor(note)", self._function(self.TASKS, "noteControls"))


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
        for name in ("refreshTasks", "refreshInbox", "readFlagged"):
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
            "if (registryIsRunning()) readRegistry();",
            "else probeRegistry();"])
        # In turn, so a registry that stopped costs one failed call.
        self.assertIn("refreshInbox().then(() => refreshTasks()).then(() => "
                      "refreshOpenTask());", self.APP)

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

    def test_the_flagged_rows_are_drawn_only_when_asked_for(self):
        # Read after the inbox, gated again after its fetch, and an answer a later one
        # overtook dropped, so a slow poll cannot put back an older count.
        inbox = self._function(self.TASKS, "refreshInbox")
        self.assertLess(inbox.index("renderInbox(lastInbox);"),
                        inbox.index("await readFlagged(false);"))
        read = self._function(self.TASKS, "readFlagged")
        after = read[read.index('fetch("/api/notes/flagged")'):]
        self.assertIn("if (!registryIsRunning() || asked < flaggedUsed) return;", after)
        # Rows from a read asked to draw, or when none were drawn since the registry
        # last answered; any other read rewrites the count line alone.
        self.assertIn("const drawNow = wanted || flaggedDrawn === null;", read)
        # A draw asked for is made only by an answer to a read asked with it or after:
        # one asked before may predate the write that asked for the draw.
        self.assertLess(read.index("const asked = ++flaggedAsks;"),
                        read.index("if (draw) flaggedWanted = asked;"))
        self.assertIn("const wanted = flaggedWanted !== 0 && asked >= flaggedWanted;", read)
        self.assertIn("if (drawNow) renderFlagged();\n  else renderFlaggedLine();", read)
        self.assertEqual(re.findall(r"\bflaggedDrawn = ([^;]*);", self.TASKS),
                         ["null", "null", "flaggedRead.notes"])
        self.assertIn("if (!up) flaggedDrawn = null;",
                      self._function(self.TASKS, "setRunning"))
        self.assertEqual(len(re.findall(r"\brenderFlagged\(\)", self.TASKS)), 2,
                         "defined once, called once, from readFlagged")
        # Asked to draw: by the runner after a move on notes or on a task, before its
        # controls come back; on coming back from All tasks; on "show them". The poll
        # never asks.
        calls = sorted(ln.strip() for ln in re.findall(r"^.*\breadFlagged\(.*$",
                                                        self.TASKS, re.M)
                       if "function readFlagged" not in ln)
        self.assertEqual(calls, [
            "await readFlagged(false);",
            "if (back) readFlagged(true);",
            "if (redrawsFlagged(write, result)) await readFlagged(true);",
            "readFlagged(true);"])
        run = self._function(self.TASKS, "runWrite")
        self.assertLess(run.index("await readFlagged(true);"), run.index("writing = null;"))
        self.assertIn('if (btn.dataset.flagged === "show") {\n      readFlagged(true);',
                      self.TASKS)
        self.assertIn('const back = view === "all" && name === "inbox";',
                      self._function(self.TASKS, "showView"))
        # Selection is ids, kept for those still drawn but a note parked since.
        self.assertIn("flaggedSelected = keptSelection(flaggedSelected, flaggedRead.notes);",
                      read)
        self.assertLess(read.index("keptSelection("),
                        read.index("flaggedDrawn = flaggedRead.notes;"))
        # The count line compares the notes drawn, task state and all, not their ids.
        self.assertIn("flaggedLine(flaggedDrawn || [], flaggedRead)",
                      self._function(self.TASKS, "renderFlaggedLine"))

    def test_the_flagged_block_sits_above_what_the_poll_redraws(self):
        html = (TASKS_DIR / "index.html").read_text()
        self.assertLess(html.index('<div id="flagged">'), html.index('<div id="inbox">'))
        self.assertLess(html.index('<section id="inbox-view">'),
                        html.index('<div id="flagged">'))
        # Its rows in a box of one height, its count line one line high, and the
        # handle last in its row, wide enough for its count.
        self.assertRegex(html, r"\.flagged-rows \{ height: \d+px; overflow: auto;")
        self.assertRegex(html, r"\.flagged-line \{ height: \d+px;")
        self.assertRegex(html, r"#flagged-handle \{ min-width: ")
        # What the handle would leave out sits after it, one line of fixed width.
        self.assertLess(html.index('id="flagged-handle"'), html.index('id="flagged-left-out"'))
        rule = re.search(r"\.flagged-left-out \{([^}]*)\}", html).group(1)
        for prop in (r"\bwidth: \d+em;", r"\bheight: [\d.]+em;", "overflow: hidden;",
                     "white-space: nowrap;"):
            self.assertRegex(rule, prop)
        self.assertLess(html.index('id="flagged-all"'), html.index('id="flagged-handle"'))
        # "show them" goes first in the line, so a count that changes moves no button.
        line = self._function(self.TASKS, "renderFlaggedLine")
        self.assertLess(line.index("flaggedLineEl.appendChild(show);"),
                        line.index("flaggedLineEl.appendChild(text);"))

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
    # A timeline entry's note, and a diff's run, are agent text too, as are a plan's
    # decisions and their options; an answer carries its question and options' text,
    # and the operator's own "other" text, spelled as any.
    FIELD = re.compile(r"\b(?:t|n|h|task|claim|plan|part|reason|entry|run)"
                       r"(?:\.claim|\.note)?\."
                       r"(?:title|body|text|author|holder|branch|progress|footprint|"
                       r"approach|tests|out_of_scope|risks|review_verdict)\b"
                       r"|\b(?:d|decision)\.(?:question|why)\b"
                       r"|\b(?:o|option)\.(?:label|meaning)\b"
                       r"|\banswer\.(?:question|other|author)\b")
    # `hiddenIn` counts for a warning and draws nothing.
    SPELT = ("spellHidden(", "proseBlock(", "authorCell(", "claimText(", "hiddenIn(")

    # Every module the page is made of, so a helper moving to another module does not
    # move out of these guards' sight.
    MODULES = _module_texts(TASKS_DIR)

    def test_the_guards_read_every_module_the_page_serves(self):
        self.assertEqual(set(self.MODULES), set(tui.UI_MODULES))

    def test_nothing_is_parsed_as_markup(self):
        for name, src in self.MODULES.items():
            with self.subTest(module=name):
                self.assertNotRegex(src, r"innerHTML|outerHTML|insertAdjacentHTML|"
                                         r"document\.write|DOMParser")

    def test_every_agent_field_goes_through_the_spelling(self):
        code = "\n".join(_code(src) for src in self.MODULES.values())
        lines = [ln for ln in code.splitlines() if self.FIELD.search(ln)]
        self.assertGreater(len(lines), 10, "the field pattern found too little")
        for ln in lines:
            if re.match(r"\s*if \(", ln):
                continue        # a condition reads the field; it draws nothing
            with self.subTest(line=ln.strip()):
                self.assertTrue(any(s in ln for s in self.SPELT), ln)

    def test_the_plans_are_drawn_against_what_they_are(self):
        # The task's own plan is read against its state and drawn as what changed from
        # the one before; an earlier plan against none, since a newer one is the
        # task's, and whole; the shown one stays out of the earlier list.
        self.assertIn("planBlock(task.plan, task.state,\n"
                      "                                     planPredecessor(held.plans, "
                      "task.plan.id))", self.TASKS)
        self.assertIn("planBlock(plan, null)", self.TASKS)
        self.assertIn("planNotice(null, task.state)", self.TASKS)
        self.assertIn("more.dataset.shown = String(task.plan.id);", self.TASKS)
        self.assertIn('page.dataset.page === "plans" ? showPlans : showPage', self.TASKS)

    def test_the_earlier_plans_are_the_ones_the_page_holds(self):
        # Read with the task, so the diff and the earlier plans are the same list.
        show = _function(self.TASKS, "showPlans")
        self.assertIn("earlierPlans(shownHeld && shownHeld.plans, btn.dataset.shown)", show)
        self.assertNotIn("fetch(", show)
        read = _function(self.TASKS, "readTask")
        self.assertIn("const plans = task.plan\n"
                      "    ? await registryJson(await fetch(`/api/tasks/${path}/plans`))",
                      read)

    def test_the_helpers_spell_and_set_text(self):
        for name in ("proseBlock", "authorCell"):
            body = re.search(rf"^function {name}\(.*?^\}}", self.TASKS,
                             re.M | re.S).group(0)
            with self.subTest(helper=name):
                self.assertIn("spellHidden(", body)
        prose = re.search(r"^function proseBlock\(.*?^\}", self.TASKS, re.M | re.S)
        self.assertIn("pre.textContent = shown.text", prose.group(0))
        self.assertIn("spellHidden(text).hidden", _function(self.TASKS, "hiddenIn"))

    def test_a_diff_spells_every_run_and_marks_it_by_more_than_colour(self):
        body = _function(self.TASKS, "diffLine")
        self.assertIn("const shown = spellHidden(run.text);", body)
        self.assertIn("el.textContent = shown.text;", body)
        self.assertIn('document.createElement(run.op === "add" ? "ins"\n'
                      '      : run.op === "del" ? "del" : "span")', body)
        self.assertIn("hiddenNote(hidden)", body)
        html = (TASKS_DIR / "index.html").read_text()
        self.assertRegex(html, r"\bins \{ text-decoration: underline;")
        self.assertRegex(html, r"\bdel \{ text-decoration: line-through;")

    def test_a_flagged_row_spells_what_an_agent_wrote(self):
        row = _function(self.TASKS, "flaggedRow")
        lines = [ln for ln in _code(row).splitlines() if self.FIELD.search(ln)]
        self.assertGreaterEqual(len(lines), 2, "the field pattern found too little")
        self.assertIn("authorCell(tr, n.author);", row)
        self.assertIn("text.appendChild(proseBlock(n.text));", row)
        # Its label names the note by id, never by its text.
        self.assertIn('box.setAttribute("aria-label", `select note #${n.id}`);', row)

    def test_a_history_value_is_spelled_whatever_its_type(self):
        self.assertIn('typeof value === "string" ? proseBlock(value)', self.TASKS)
        self.assertIn("spellHidden(JSON.stringify(value)).text", self.TASKS)


_APPROVE_PROBE = r"""
import { pathToFileURL } from "node:url";
const m = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/moves.js`).href);
const decisions = JSON.parse(process.env.DOCKADE_DECISIONS);
// Every choice a decision's group can hold, and some it cannot (an option and "other"
// on a single choice), with "other" texts blank to both sides, blank to one only, and
// words. Each set of options in every order it can be picked in, since the page keeps
// them in click order.
const orders = list => (list.length < 2 ? [list] : list.flatMap(
  (j, k) => orders([...list.slice(0, k), ...list.slice(k + 1)]).map(rest => [j, ...rest])));
const subsets = n => Array.from({length: 2 ** n},
  (_, k) => [...Array(n).keys()].filter(j => k & (1 << j))).flatMap(orders);
const TEXTS = ["", " \t", "\x1f", "\ufeff", "\u3000", "Only the llm containers."];
const each = d => subsets(d.options.length).flatMap(
  options => [false, true].flatMap(other => TEXTS.map(text => ({options, other, text}))));
const cases = [];
for (const a of each(decisions[0])) {
  for (const b of each(decisions[1])) {
    const choices = [a, b];
    cases.push({outcome: m.approveOutcome(decisions, choices),
                answers: m.approveAnswers(decisions, choices),
                raw: choices.map(m.choiceAnswer)});
  }
}
// What an approve puts on the wire, with fetch stubbed.
const sent = [];
globalThis.fetch = (url, opts) => {
  sent.push({url, method: opts.method, body: JSON.parse(opts.body)});
  return Promise.resolve({status: 200, text: async () => '{"ok": true}'});
};
const assumed = m.assumedChoices(decisions);
const wire = {};
for (const [key, choices] of Object.entries({
    assumed,
    other: [{options: [], other: true, text: "Deny, but log each one."}, assumed[1]],
    fewer: [assumed[0], {options: [1], other: false, text: ""}]})) {
  await m.sendWrite({kind: "approve", task: "7", revision: 4, plan: 6,
                     answers: m.approveAnswers(decisions, choices)});
  wire[key] = {...sent.pop(), predicted: m.approveOutcome(decisions, choices)};
}
await m.sendWrite({kind: "approve", task: "7", revision: 4, plan: 6, answers: []});
wire.bare = sent.pop();
// Every code point either side calls blank, lone surrogates aside.
const blank = [];
for (let cp = 0; cp <= 0x10ffff; cp++) {
  if (cp >= 0xd800 && cp <= 0xdfff) continue;
  if (m.blankText(String.fromCodePoint(cp))) blank.push(cp);
}
console.log(JSON.stringify({cases, wire, blank, assumed,
                            empty: m.blankText(""), not_text: m.blankText(null)}));
"""


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class ApproveRuleTests(_OperatorStore):
    """The page's approve against the registry's: every choice two decisions can hold
    run through ``approveOutcome`` under node and ``plans.outcome`` here, which must
    agree; one the page calls incomplete is one ``plans.check_answers`` refuses. And the
    body the page posts, taken by the operator API's model and by ``approve`` on a
    scratch store, reaching the state the page predicted."""

    probe: dict
    DECISIONS: ClassVar[list] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.DECISIONS = _plans.check_decisions([DECISION, MULTI])
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", _APPROVE_PROBE],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR)),
                 "DOCKADE_DECISIONS": json.dumps(cls.DECISIONS)},
            capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing moves.js under node failed:\n" + proc.stderr)
        cls.probe = json.loads(proc.stdout)

    def refined(self) -> int:
        task = _agent.propose(ALICE, "widgets", "Rotate keys", "Every 90 days.")["task"]
        token = _agent.claim_plan(ALICE, "widgets", task, ["."])["token"]
        _agent.submit_plan(ALICE, "widgets", task, token,
                           **(PLAN | {"decisions": [DECISION, MULTI]}))
        return task

    def test_the_page_decides_every_choice_as_the_registry_does(self):
        cases = self.probe["cases"]
        # Ordered sets of 2 options: 1 + 2 + 2; of 3: 1 + 3 + 6 + 6.
        self.assertEqual(len(cases), (5 * 2 * 6) * (16 * 2 * 6))
        seen = set()
        for case in cases:
            seen.add(case["outcome"])
            if case["outcome"] == "incomplete":
                self.assertIsNone(case["answers"])
                with self.assertRaises(_Refused, msg=case["raw"]):
                    _plans.check_answers(self.DECISIONS, case["raw"])
            else:
                self.assertEqual(case["answers"], case["raw"])
                self.assertEqual(_plans.outcome(self.DECISIONS, case["answers"]),
                                 case["outcome"], case["raw"])
        self.assertEqual(seen, {"open", "proposed", "incomplete"})

    def test_a_plan_starts_on_its_assumed_options(self):
        self.assertEqual(self.probe["assumed"],
                         [{"options": [0], "other": False, "text": ""},
                          {"options": [0, 1], "other": False, "text": ""}])
        self.assertEqual(self.probe["wire"]["assumed"]["predicted"], "open")

    def test_blank_is_the_registrys_blank(self):
        python = [cp for cp in range(0x110000)
                  if not 0xD800 <= cp <= 0xDFFF and not chr(cp).strip()]
        self.assertEqual(self.probe["blank"], python)
        self.assertTrue(self.probe["empty"])
        self.assertTrue(self.probe["not_text"])

    def test_the_body_posted_is_the_one_the_operator_api_takes(self):
        wire = self.probe["wire"]
        for key in ("assumed", "other", "fewer", "bare"):
            with self.subTest(write=key):
                sent = wire[key]
                self.assertEqual((sent["url"], sent["method"]),
                                 ("/api/tasks/7/approve", "POST"))
                self.assertEqual(set(sent["body"]), set(_api.Approve.__annotations__))
                self.assertIs(type(sent["body"]["revision"]), int)
                for answer in sent["body"]["answers"]:
                    self.assertLessEqual(set(answer), set(_api.Answer.__annotations__))
                    self.assertTrue(all(type(j) is int for j in answer["options"]))
        self.assertEqual(wire["bare"]["body"], {"revision": 4, "answers": []})
        source = (ROOT / "task-registry" / "operator_api.py").read_text()
        self.assertIn('@app.post("/api/tasks/{task}/approve")', source)

    def test_the_body_posted_reaches_the_state_the_page_predicted(self):
        for key in ("assumed", "other", "fewer"):
            with self.subTest(write=key):
                task = self.refined()
                sent = self.probe["wire"][key]
                body = sent["body"]
                req = _api.Approve(revision=_reads.get_task(task)["revision"],
                                   answers=tuple(_api.Answer(**a) for a in body["answers"]))
                reply = _api.approve(task, req, ThroughTheRelay())
                self.assertEqual((reply.status_code, reply.body["state"]),
                                 (200, sent["predicted"]))
                got = _reads.get_task(task)
                self.assertEqual(got["state"], sent["predicted"])
                # And what a lost answer is read back by: the answers as sent.
                self.assertEqual([([o["position"] for o in a["options"]], a["other"])
                                  for a in got["answers"]],
                                 [(a["options"], a["other"]) for a in body["answers"]])
        self.assertEqual(_reads.get_task(self.refined())["plan"]["decisions"],
                         self.DECISIONS)


class TaskDecisionSourceTests(unittest.TestCase):
    """How the page draws a plan's decisions and builds the approve that answers them,
    from source: a browser is the only thing that runs it. The rule the approve follows
    is held under node (``MoveTests``, ``ApproveRuleTests``)."""

    TASKS = (TASKS_DIR / "tasks.js").read_text()
    HTML = (TASKS_DIR / "index.html").read_text()

    def test_a_decision_and_an_answer_are_spelled_and_drawn_whole(self):
        group = _function(self.TASKS, "decisionGroup")
        for drawn in ("proseBlock(decision.question)", "proseBlock(decision.why)",
                      "proseBlock(option.label)", "proseBlock(option.meaning)"):
            with self.subTest(field=drawn):
                self.assertIn(drawn, group)
        answer = _function(self.TASKS, "answerBlock")
        for drawn in ("proseBlock(answer.question)", "proseBlock(option.label)",
                      "proseBlock(answer.other)", "spellHidden(answer.author).text"):
            with self.subTest(field=drawn):
                self.assertIn(drawn, answer)
        # An "other" text with a hidden character is spelled under its box.
        self.assertIn("if (spellHidden(choice.text).hidden) "
                      "note.appendChild(proseBlock(choice.text));",
                      _function(self.TASKS, "markOther"))
        # No rule cuts an option, a question or an answer short.
        for rule in re.findall(r"(?:fieldset\.decision|\.decision|\.option-label|\.answer|"
                               r"\.decisions)[^{]*\{([^}]*)\}", self.HTML):
            with self.subTest(rule=rule.strip()):
                self.assertNotRegex(rule, r"overflow|max-height|line-clamp|nowrap")

    def test_an_input_carries_positions_never_text(self):
        made = _function(self.TASKS, "choiceInput")
        self.assertIn("input.dataset.decision = String(i);", made)
        self.assertIn("input.dataset.option = option;", made)
        self.assertIn("choiceInput(i, String(j), multi)",
                      _function(self.TASKS, "decisionGroup"))
        self.assertIn('choiceInput(i, "other", multi)', _function(self.TASKS, "otherRow"))
        field = TaskRenderingSourceTests.FIELD
        for name in ("choiceInput", "otherRow", "decisionGroup", "answerBlock"):
            for ln in _code(_function(self.TASKS, name)).splitlines():
                if "setAttribute(" in ln or ".dataset." in ln or ".name =" in ln:
                    with self.subTest(function=name, line=ln.strip()):
                        self.assertNotRegex(ln, field)

    def test_only_a_plan_the_operator_can_approve_draws_choices(self):
        block = _function(self.TASKS, "decisionsBlock")
        self.assertIn("const picked = approvable(task) ? choicesFor(task) : null;", block)
        self.assertIn("decisionGroup(d, i, picked && picked[i])", block)
        # An earlier plan's decisions are text, as is an answered plan's.
        plan = _function(self.TASKS, "planBlock")
        self.assertIn("if (state === null) {\n"
                      "    planDecisions(plan).forEach((d, i) => "
                      "whole.appendChild(decisionGroup(d, i, null)));", plan)
        group = _function(self.TASKS, "decisionGroup")
        self.assertEqual(group.count("if (choice)"), 2)
        self.assertEqual(len(re.findall(r"\bchoiceInput\(", group)), 1)

    def test_the_decisions_and_answers_sit_above_the_plan(self):
        draw = _function(self.TASKS, "drawTask")
        self.assertLess(draw.index("taskDetail.appendChild(body);"),
                        draw.index("taskDetail.appendChild(decisionsBlock(task));"))
        self.assertLess(draw.index("decisionsBlock(task)"), draw.index("planHead"))
        block = _function(self.TASKS, "decisionsBlock")
        self.assertIn("  if (reset) {\n"
                      "    const said = noticeLine({ text: resetText(reset), warn: true });\n"
                      '    said.dataset.planReset = "1";\n'
                      "    block.appendChild(said);", block)
        self.assertLess(block.index("planResets.get("), block.index("planDecisions("))
        self.assertLess(block.index("planDecisions("), block.index("task.answers"))

    def test_the_choices_are_in_the_in_flight_gate(self):
        mark = _function(self.TASKS, "markWriting")
        self.assertIn("input[data-decision]", mark)
        self.assertIn("textarea[data-other]", mark)
        self.assertIn("el.disabled = writing !== null || !drawnIsOpen();", mark)
        self.assertEqual(_function(self.TASKS, "choose").splitlines()[1].strip(),
                         "if (!drawnIsOpen() || !approvable(shownTask)) return;")
        self.assertIn('if (ev.target.matches("textarea[data-other]") && drawnIsOpen()\n'
                      "        && approvable(shownTask)) {", self.TASKS)

    def test_no_approve_is_built_while_an_answer_is_incomplete(self):
        mark = _function(self.TASKS, "markAnswers")
        incomplete = mark.index('if (goes === "incomplete") {')
        otherwise = mark.index("} else {", incomplete)
        self.assertLess(incomplete, mark.index("incompleteText("))
        self.assertLess(mark.index("incompleteText("), otherwise)
        self.assertLess(otherwise, mark.index('writeButton(approveLabel(task, goes), '
                                              '"approve", "")'))
        # That is the one approve the page draws, and the one write it builds.
        self.assertEqual(len(re.findall(r'writeButton\([^;]*"approve"', self.TASKS)), 1)
        self.assertIn('if (btn.dataset.write === "approve") return approveWrite(base);',
                      _function(self.TASKS, "writeFor"))
        write = _function(self.TASKS, "approveWrite")
        self.assertEqual(write.splitlines()[1].strip(),
                         "if (!approvable(shownTask)) return null;")
        self.assertIn("const answers = approveAnswers(decisions, picked);", write)
        self.assertIn("return answers && {", write)
        self.assertIn("predicted: approveOutcome(decisions, picked)", write)

    def test_approve_has_a_row_of_its_own_above_the_other_moves(self):
        moves = _function(self.TASKS, "movesBlock")
        self.assertLess(moves.index('approve.dataset.approveRow = "1";'),
                        moves.index('row.className = "moves";'))
        rule = re.search(r"\.approve-row \{([^}]*)\}", self.HTML).group(1)
        self.assertRegex(rule, r"\bheight: \d+px;")
        self.assertIn("white-space: nowrap;", rule)
        # Each choice relabels both in place: nothing else in the detail is redrawn.
        mark = _function(self.TASKS, "markAnswers")
        self.assertIn('row.replaceChildren(', mark)
        self.assertIn("backBtn.textContent = moveLabel(back, task, picked);", mark)
        self.assertNotIn("drawTask(", mark)
        self.assertNotIn("drawTask(", _function(self.TASKS, "choose"))

    def test_choices_are_kept_by_task_and_plan_and_a_new_plan_is_said(self):
        self.assertIn("const key = `${task.id}:${task.plan.id}`;",
                      _function(self.TASKS, "choicesFor"))
        self.assertEqual(re.findall(r"^\s*(?:let )?choices = ([^;]*);", self.TASKS, re.M),
                         ["new Map()"])
        draw = _function(self.TASKS, "drawTask")
        self.assertLess(draw.index("notePlanDrawn(task);"), draw.index("decisionsBlock("))
        noted = _function(self.TASKS, "notePlanDrawn")
        self.assertIn("if (was === undefined || was === null || plan === null "
                      "|| was === plan) return;", noted)
        self.assertIn("planResets.set(id, { from: was, to: plan, others });", noted)
        # Spent by the first choice, keeping its space, or by a landed approve; no poll
        # touches it, nor the choices.
        choose = _function(self.TASKS, "choose")
        self.assertIn("if (planResets.delete(String(shownTask.id))) {", choose)
        self.assertIn('said.classList.add("spent");', choose)
        self.assertRegex(self.HTML, r"\.decisions \.spent \{ visibility: hidden; \}")
        run = _function(self.TASKS, "runWrite")
        self.assertIn('if (write.kind === "approve" && landed) {\n'
                      "    choices.delete(`${write.task}:${write.plan}`);\n"
                      "    planResets.delete(write.task);", run)
        for name in ("refreshOpenTask", "refreshInbox", "refreshTasks", "readFlagged"):
            with self.subTest(function=name):
                self.assertNotRegex(_function(self.TASKS, name),
                                    r"\bchoices\b|planResets|drawnPlans")

    def test_an_approve_says_the_state_it_reached(self):
        run = _function(self.TASKS, "runWrite")
        self.assertIn('const reached = result === "landed" ? answer.body.state\n'
                      "    : drawn && drawn.task && drawn.task.state;", run)
        self.assertIn("...outcomeText(write, result, detail, reached) };", run)


if __name__ == "__main__":
    unittest.main()
