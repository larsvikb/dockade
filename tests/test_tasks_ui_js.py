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
from html.parser import HTMLParser
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
    # Nothing on this page is parsed as markup (`PageChecks`' sink guard).
    MARKUP_MODULE = None


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
    format_not_ignorable: t.spellHidden("a\uFFFBb"),
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
    approved: t.planHeading({id: 9, author: "a", at: 1789466400, task_revision: 1,
                             review_rounds: 1, approved_at: 1789470000,
                             superseded_at: 1789480000}),
    not_approved: t.planHeading({id: 9, author: "a", at: 1789466400, task_revision: 1,
                                 review_rounds: 1, approved_at: null}),
    notice: {
      none_proposed: t.planNotice(null, {state: "proposed"}),
      none_open: t.planNotice(null, {state: "open"}),
      none_closed: t.planNotice(null, {state: "closed"}),
      current: t.planNotice({id: 1, task_revision: 3, superseded_at: null},
                            {state: "refined", revision: 3}),
      superseded_proposed: t.planNotice({id: 1, superseded_at: 1789466400},
                                        {state: "proposed"}),
      superseded_open: t.planNotice({id: 1, superseded_at: 1789466400}, {state: "open"}),
      // A task edited since its plan was written: said only while it is refined, since
      // an approved task's revision always passes its plan's.
      lagging: t.planNotice({id: 1, task_revision: 3, superseded_at: null},
                            {state: "refined", revision: 5}),
      // Approved before, by its approve or its answers: only a reopen opens it.
      lagging_approved: t.planNotice({id: 1, task_revision: 3, superseded_at: null,
                                      approved_at: 1789466400},
                                     {state: "refined", revision: 5}),
      lagging_answered: t.planNotice({id: 1, task_revision: 3, superseded_at: null},
                                     {state: "refined", revision: 5,
                                      answers: [{plan: 1}]}),
      lagging_open: t.planNotice({id: 1, task_revision: 3, superseded_at: null},
                                 {state: "open", revision: 5}),
      // Among the earlier plans, drawn with no task.
      earlier: t.planNotice({id: 1, task_revision: 3, superseded_at: 1789466400}, null),
      earlier_none: t.planNotice(null, null),
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
      padded_max: t.parseHash("#task=0009223372036854775807"),
      short: t.parseHash("#task=922337203685477580"),
      zeros_only: t.parseHash("#task=000"),
      past_max: t.parseHash("#task=9223372036854775808"),
      longer: t.parseHash("#task=10000000000000000000"),
      empty_task: t.parseHash("#task="),
      bad_all: t.parseHash("#view=all&task=abc"),
      missing: t.parseHash(undefined),
      inbox_none: t.hashFor("inbox", null),
      inbox_task: t.hashFor("inbox", "14"),
      all_none: t.hashFor("all", null),
      all_task: t.hashFor("all", 7),
      round_trip: t.parseHash(t.hashFor("all", "9")),
      repos: t.parseHash("#view=repos"),
      repos_task: t.parseHash("#view=repos&task=4"),
      repos_none: t.hashFor("repos", null),
      repos_round_trip: t.parseHash(t.hashFor("repos", "4")),
      unknown_view: t.hashFor("audit", "4"),
      views: t.VIEWS,
    },
    id_max: t.ID_MAX,
    no_task: [t.badTaskText("a\u202eb"), t.unknownTaskText("999")],
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
        # `Cf` but not default-ignorable: the format class is in the set on its own.
        self.assertEqual(spell["format_not_ignorable"],
                         {"text": "a\\ufffbb", "hidden": 1})

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
        self.assertRegex(plan["approved"], r"· 1 review round · approved \d{4}-\d\d-\d\d "
                                           r"\d\d:\d\d:\d\d · superseded \d{4}-")
        self.assertTrue(plan["not_approved"].endswith("· 1 review round"))

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

    def test_a_refined_task_edited_since_its_plan_says_what_approving_approves(self):
        notice = self.probe["plan"]["notice"]
        self.assertEqual(notice["lagging"], {
            "text": "The task moved on since plan 1 was written (revision 3, now 5): "
                    "approving approves the task as it now is, with this plan.",
            "warn": True})
        for key in ("current", "lagging_open", "earlier", "earlier_none"):
            with self.subTest(case=key):
                self.assertIsNone(notice[key])

    def test_a_refined_task_edited_since_its_approved_plan_says_reopening_opens_it(self):
        notice = self.probe["plan"]["notice"]
        for key in ("lagging_approved", "lagging_answered"):
            with self.subTest(case=key):
                self.assertEqual(notice[key], {
                    "text": "The task moved on since plan 1 was written (revision 3, now "
                            "5): reopening opens the task as it now is, with this plan.",
                    "warn": True})

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
        inbox = {"view": "inbox", "task": None, "bad": None}
        self.assertEqual(h["empty"], inbox)
        self.assertEqual(h["missing"], inbox)
        self.assertEqual(h["task"], {"view": "inbox", "task": "14", "bad": None})
        self.assertEqual(h["all"], {"view": "all", "task": "7", "bad": None})
        # Only digits name a task, as the relay takes one, and only a known view.
        self.assertEqual(h["bad_task"]["task"], None)
        self.assertEqual(h["bad_view"], inbox)
        self.assertEqual((h["inbox_none"], h["inbox_task"], h["all_none"], h["all_task"]),
                         ("", "#task=14", "#view=all", "#view=all&task=7"))
        self.assertEqual(h["round_trip"], {"view": "all", "task": "9", "bad": None})
        # Repos is a view of its own, kept with a task as the others are.
        self.assertEqual(h["repos"], {"view": "repos", "task": None, "bad": None})
        self.assertEqual(h["repos_task"], {"view": "repos", "task": "4", "bad": None})
        self.assertEqual(h["repos_none"], "#view=repos")
        self.assertEqual(h["repos_round_trip"], {"view": "repos", "task": "4", "bad": None})
        self.assertEqual(h["unknown_view"], "#task=4")
        self.assertEqual(h["views"], ["inbox", "all", "repos"])
        # As a row names it, so the row is marked and not read twice; never through a
        # Number, which would round an id past 2^53.
        self.assertEqual(h["zeros"]["task"], "6")
        self.assertEqual(h["long_id"]["task"], "9223372036854775807")
        self.assertEqual(h["padded_max"]["task"], "9223372036854775807")
        self.assertEqual(h["short"]["task"], "922337203685477580")
        # A task parameter that names no task is reported, so the page says so: outside
        # 1 to ID_MAX, or not digits. One past ID_MAX in the last digit, and a digit
        # longer: a Number could tell neither from ID_MAX.
        for key, given in (("zero", "0"), ("zeros_only", "000"),
                           ("past_max", "9223372036854775808"),
                           ("longer", "10000000000000000000"), ("bad_task", "../api"),
                           ("empty_task", "")):
            with self.subTest(hash=key):
                self.assertEqual(h[key], {"view": "inbox", "task": None, "bad": given})
        self.assertEqual(h["bad_all"], {"view": "all", "task": None, "bad": "abc"})
        for key in ("empty", "missing", "task", "all", "bad_view", "round_trip"):
            with self.subTest(hash=key):
                self.assertIsNone(h[key]["bad"])

    def test_the_id_bound_is_the_registrys(self):
        self.assertEqual(self.probe["inbox"]["id_max"], str(_reg.tasks.ID_MAX))

    def test_a_task_the_page_could_not_open_is_named_spelled(self):
        self.assertEqual(self.probe["inbox"]["no_task"],
                         ['The link names no task: "a\\u202eb" is not a task id.',
                          "The registry has no task #999."])

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
// The label table: each move a task offers as [state, main, second, caution], its
// break last. Plan 6 has no decisions in `approved`'s case, approved all the same, as a
// read's `approved_at` says.
const approved = {...plan, approved_at: 1789470000};
const row = l => [l.main, l.second, l.caution];
const labelled = (tk, picked = null) => [
  ...m.movesFor(tk).map(mv => [mv.state, ...row(t.moveLabel(mv, tk, picked))]),
  ...(tk.claim ? [["break", ...row(t.breakLabel(tk.claim))]] : [])];
// Every state, by what its plan is: none, a superseded one, one answered, one with no
// decisions approved, one never approved, and each claimed.
const kinds = {
  none: {}, superseded: {plan: {...plan, superseded_at: 1}},
  answered: {plan: decided, answers: answered},
  approved: {plan: approved}, unapproved: {plan},
};
const matrix = {};
for (const state of m.TASK_STATES) {
  for (const [kind, extra] of Object.entries(kinds)) {
    matrix[`${state}/${kind}`] = labelled(task(state, extra));
    matrix[`${state}/${kind}/claimed`] = labelled(task(state, {...extra, claim}));
  }
}
const primaries = Object.fromEntries(m.TASK_STATES.flatMap(state => [
  [`${state}/none`, t.primaryMove(task(state))],
  [`${state}/plan`, t.primaryMove(task(state, {plan}))],
  [`${state}/answered`, t.primaryMove(task(state, {plan: decided, answers: answered}))],
]));
console.log(JSON.stringify({
  matrix, primaries, primary_none: t.primaryMove(null),
  table: {
    open_claimed: labelled(task("open", {plan: decided, answers: answered, claim})),
    open_none: labelled(task("open")),
    refined: labelled(task("refined", {plan})),
    refined_answered: labelled(task("refined", {plan: decided, answers: answered})),
    refined_approved: labelled(task("refined", {plan: approved})),
    landing: labelled(task("landing", {plan})),
    deferred_none: labelled(task("deferred")),
    deferred_approved: labelled(task("deferred", {plan: approved})),
    deferred_unapproved: labelled(task("deferred", {plan})),
    closed_answered: labelled(task("closed", {plan: decided, answers: answered})),
    proposed_superseded: labelled(task("proposed", {plan: {...plan, superseded_at: 1}})),
  },
  approved_plan: {
    approved: m.planApproved(task("closed", {plan: approved})),
    answered_approved: m.planApproved(task("closed", {plan: {...decided,
                                                             approved_at: 1789470000},
                                                      answers: answered})),
    // Answers alone are `planAnswered`'s, beside it.
    answered: m.planApproved(task("closed", {plan: decided, answers: answered})),
    never: m.planApproved(task("closed", {plan})),
    null_time: m.planApproved(task("closed", {plan: {...plan, approved_at: null}})),
    superseded: m.planApproved(task("closed", {plan: {...approved, superseded_at: 1}})),
    no_plan: m.planApproved(task("closed")),
  },
  holder: [t.holderText(claim), t.holderText(null)],
  note_labels: [t.noteMoveLabel({kind: "hide", value: false, verb: "unhide"}),
                t.noteMoveLabel({kind: "hide", value: true, verb: "hide"}),
                t.noteMoveLabel({kind: "handle", value: false, verb: "mark not handled"}),
                t.NOTE_SECOND],
  moves: {
    proposed: moves(task("proposed")),
    refined_with_plan: moves(task("refined", {plan})),
    refined_decided: moves(task("refined", {plan: decided, answers: []})),
    refined_answered: moves(task("refined", {plan: decided, answers: answered})),
    refined_approved: moves(task("refined", {plan: approved})),
    refined_answered_approved: moves(task("refined", {plan: {...decided,
                                                             approved_at: 1789470000},
                                                      answers: answered})),
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
    send_back: t.moveLabel(sendBack, task("refined", {plan: decided}), assumed),
    send_back_differs: t.moveLabel(sendBack, task("refined", {plan: decided}), denied),
    send_back_other: t.moveLabel(sendBack, task("refined", {plan: decided}),
                                 [assumed[0], {options: [0, 1], other: true, text: "x"}]),
    send_back_text_not_picked: t.moveLabel(sendBack, task("refined", {plan: decided}),
                                           [{...assumed[0], text: "x"}, assumed[1]]),
    send_back_no_choices: t.moveLabel(sendBack, task("refined", {plan: decided})),
    // An incomplete answer differs from what the plan assumes, so it sends none.
    send_back_incomplete: t.moveLabel(sendBack, task("refined", {plan: decided}),
                                      [assumed[0], {options: [], other: false, text: ""}]),
    send_back_claimed: t.moveLabel({...sendBack, endsClaim: true},
                                   task("refined", {plan: decided, claim}), assumed),
    break: t.breakLabel(claim),
    no_break: t.breakLabel(null),
    no_move: [t.moveLabel(null, task("open")), t.moveLabel(sendBack, null)],
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
      approved: m.approvable(task("refined", {plan: approved})),
      answered_approved: m.approvable(task("refined", {plan: {...decided,
                                                              approved_at: 1789470000},
                                                       answers: answered})),
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
        # refined only to reopen a plan approved before, which `approve` refuses.
        mv = self.probe["moves"]
        expected = {"proposed": "send back", "landing": "move to landing",
                    "deferred": "defer", "closed": "close"}
        for key in ("refined_with_plan", "refined_decided", "re_refined"):
            with self.subTest(case=key):
                self.assertEqual({s: verb for s, verb, _ in mv[key]}, expected)
        for key in ("refined_answered", "refined_approved", "refined_answered_approved"):
            with self.subTest(case=key):
                self.assertEqual({s: verb for s, verb, _ in mv[key]},
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

    def test_approve_says_its_outcome_and_names_the_revision_and_plan_under_it(self):
        # Never a caution: its outcome is its main text, and one sending the task back
        # stays primary, its answers being the point.
        labels = self.probe["labels"]
        opens = {"main": "approve and open", "second": "revision 4 · plan 6",
                 "caution": False}
        self.assertEqual(labels["approve"], opens)
        self.assertEqual(labels["approve_opens"], opens)
        self.assertEqual(labels["approve_back"], {
            "main": "approve with these answers, sending it back",
            "second": "revision 4 · supersedes plan 6; no move restores it",
            "caution": False})
        self.assertIsNone(labels["approve_none"])

    def test_send_back_says_it_sends_no_answers_once_a_choice_differs(self):
        labels = self.probe["labels"]
        cost = "supersedes plan 6; no move restores it"
        for key in ("send_back_differs", "send_back_other", "send_back_incomplete"):
            with self.subTest(case=key):
                self.assertEqual(labels[key], {"main": "send back without answering",
                                               "second": cost, "caution": True})
        for key in ("send_back", "send_back_text_not_picked", "send_back_no_choices"):
            with self.subTest(case=key):
                self.assertEqual(labels[key], {"main": "send back for a new plan",
                                               "second": cost, "caution": True})
        self.assertEqual(labels["send_back_claimed"]["second"],
                         f"{cost} · ends the claim")

    # Each state's moves as [state, main, second, caution], worked out by hand from the
    # scheme (tasks-ui/DESIGN.md, "One scheme of labels"), the break last.
    TABLE: ClassVar[dict] = {
        "open_claimed": [
            ["proposed", "move to proposed",
             "supersedes plan 6; no move restores it · ends the claim", True],
            ["refined", "move to refined", "ends the claim", True],
            ["landing", "mark ready to land", "ends the claim", True],
            ["deferred", "defer", "ends the claim", True],
            ["closed", "close the task", "ends the claim", True],
            ["break", "break the claim", "ends the claim", True]],
        "open_none": [
            ["proposed", "move to proposed", "", False],
            ["landing", "mark ready to land", "", False],
            ["deferred", "defer", "", False],
            ["closed", "close the task", "", False]],
        "refined": [
            ["proposed", "send back for a new plan",
             "supersedes plan 6; no move restores it", True],
            ["landing", "mark ready to land", "", False],
            ["deferred", "defer", "", False],
            ["closed", "close the task", "", False]],
        "refined_answered": [
            ["proposed", "send back for a new plan",
             "supersedes plan 6; no move restores it", True],
            ["open", "reopen", "plan 6 as approved · revision 4", False],
            ["landing", "mark ready to land", "", False],
            ["deferred", "defer", "", False],
            ["closed", "close the task", "", False]],
        "refined_approved": [
            ["proposed", "send back for a new plan",
             "supersedes plan 6; no move restores it", True],
            ["open", "reopen", "plan 6 as approved · revision 4", False],
            ["landing", "mark ready to land", "", False],
            ["deferred", "defer", "", False],
            ["closed", "close the task", "", False]],
        "landing": [
            ["proposed", "move to proposed", "supersedes plan 6; no move restores it", True],
            ["refined", "move to refined", "", False],
            ["open", "move to open", "", False],
            ["deferred", "defer", "", False],
            ["closed", "close the task", "", False]],
        "deferred_none": [
            ["proposed", "move to proposed", "", False],
            ["open", "open without a plan", "", False],
            ["landing", "mark ready to land", "", False],
            ["closed", "close the task", "", False]],
        "deferred_approved": [
            ["proposed", "move to proposed", "supersedes plan 6; no move restores it", True],
            ["refined", "move to refined", "", False],
            ["open", "reopen", "plan 6 as approved · revision 4", False],
            ["landing", "mark ready to land", "", False],
            ["closed", "close the task", "", False]],
        "deferred_unapproved": [
            ["proposed", "move to proposed", "supersedes plan 6; no move restores it", True],
            ["refined", "move to refined", "", False],
            ["open", "open with plan 6",
             "no approve of plan 6 found: move it to refined to approve it", False],
            ["landing", "mark ready to land", "", False],
            ["closed", "close the task", "", False]],
        "closed_answered": [
            ["proposed", "move to proposed", "supersedes plan 6; no move restores it", True],
            ["refined", "move to refined", "", False],
            ["open", "reopen", "plan 6 as approved · revision 4", False],
            ["landing", "mark ready to land", "", False],
            ["deferred", "defer", "", False]],
        "proposed_superseded": [
            ["open", "open without a plan", "", False],
            ["landing", "mark ready to land", "", False],
            ["deferred", "defer", "", False],
            ["closed", "close the task", "", False]],
    }

    def test_each_states_moves_follow_the_scheme(self):
        for key, expected in self.TABLE.items():
            with self.subTest(case=key):
                self.assertEqual(self.probe["table"][key], expected)

    def test_every_move_to_proposed_with_a_current_plan_says_it_supersedes_it(self):
        # And none without one: a superseded plan has nothing left to lose.
        for key, rows in self.probe["matrix"].items():
            for state, _, second, caution in rows:
                if state != "proposed":
                    continue
                with self.subTest(case=key):
                    current = "/none" not in key and "/superseded" not in key
                    self.assertEqual("supersedes plan 6; no move restores it" in second,
                                     current)
                    self.assertTrue(caution or not current)

    def test_every_move_ending_a_claim_says_so_and_only_those_are_cautions(self):
        for key, rows in self.probe["matrix"].items():
            claimed = key.endswith("/claimed")
            for state, _, second, caution in rows:
                with self.subTest(case=key, state=state):
                    self.assertEqual(second.endswith("ends the claim"), claimed)
                    supersedes = "supersedes" in second
                    self.assertEqual(caution, claimed or supersedes)

    def test_the_move_to_open_follows_the_plan_and_no_label_is_a_bare_close(self):
        opens = {"none": "open without a plan", "superseded": "open without a plan",
                 "answered": "reopen", "approved": "reopen", "unapproved": "open with plan 6"}
        for key, rows in self.probe["matrix"].items():
            state, kind = key.split("/")[:2]
            for to, main, _, _ in rows:
                with self.subTest(case=key, to=to):
                    self.assertNotEqual(main, "close")
                    if to == "open":
                        self.assertEqual(main, "move to open" if state == "landing"
                                         else opens[kind])

    def test_a_current_plan_is_approved_by_the_approved_at_its_read_carries(self):
        self.assertEqual(self.probe["approved_plan"], {
            "approved": True, "answered_approved": True, "answered": False,
            "never": False, "null_time": False, "superseded": False, "no_plan": False})

    def test_one_primary_at_most_and_none_on_a_parked_or_finished_task(self):
        # Approve on a refined task the operator can approve, reopen on one whose plan
        # was answered, and closing on landing; nothing elsewhere.
        p = self.probe["primaries"]
        expected = dict.fromkeys(p)
        expected |= {"refined/plan": "approve", "refined/answered": "open",
                     "landing/none": "closed", "landing/plan": "closed",
                     "landing/answered": "closed"}
        self.assertEqual(p, expected)
        self.assertIsNone(self.probe["primary_none"])

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

    def test_only_a_refined_current_plan_never_approved_is_approvable(self):
        # A plan approved before, answered or not, is refused by `approve`: it reopens
        # by a move.
        self.assertEqual(self.probe["labels"]["approvable"], {
            "unanswered": True, "no_decisions": True, "re_refined": True,
            "answered": False, "approved": False, "answered_approved": False,
            "open": False, "superseded": False, "no_plan": False})

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

    def test_a_claimed_tasks_moves_name_the_holder_spelled_once_above_them(self):
        self.assertEqual(self.probe["holder"], [
            "claude-x\\u202e holds this task: a state move or a break ends the claim; an "
            "edit or a note leaves it.", ""])
        labels = self.probe["labels"]
        self.assertEqual(labels["break"], {"main": "break the claim",
                                           "second": "ends the claim", "caution": True})
        self.assertIsNone(labels["no_break"])
        self.assertEqual(labels["no_move"], [None, None])

    def test_a_note_move_unhides_and_a_note_says_it_stays(self):
        unhide, hide, unhandle, stays = self.probe["note_labels"]
        self.assertEqual(unhide, {"main": "unhide", "second": "agents read it again",
                                  "caution": False})
        self.assertEqual(hide, {"main": "hide", "second": "", "caution": False})
        self.assertEqual(unhandle["main"], "mark not handled")
        self.assertEqual(stays, "agents read it; it can be hidden, never removed")

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

    def test_a_note_offers_hide_or_unhide_and_handle_or_unhandle(self):
        mv = self.probe["moves"]
        self.assertEqual(mv["plain"], [["hide", True, "hide", False]])
        self.assertEqual(mv["hidden"], [["hide", False, "unhide", False]])
        # Handle opens the reply box; only a note flagged for the operator has one.
        self.assertEqual(mv["flagged"], [["hide", True, "hide", False],
                                         ["handle", True, "mark handled", True]])
        self.assertEqual(mv["handled"], [["hide", True, "hide", False],
                                         ["handle", False, "mark not handled", False]])
        self.assertEqual(mv["flagged_hidden"], [["hide", False, "unhide", False],
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


_MARKDOWN_PROBE = r"""
import { pathToFileURL } from "node:url";
const md = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/markdown.js`).href);
const cases = JSON.parse(process.env.DOCKADE_MD_CASES);
const parsed = s => md.tokenize(s).map(b => [b.kind, b.level ?? b.ordered ?? null,
                                             b.tokens.map(t => [t.kind, t.text])]);
const joined = s => md.tokenize(s).flatMap(b => b.tokens).map(t => t.text).join("");
// Whether every emphasis and code span a block opens it closes, innermost first, in it:
// what the page's renderer counts on to nest its elements.
const nests = s => md.tokenize(s).every(b => {
  const open = [];
  for (const t of b.tokens) {
    if (t.kind.endsWith("-open")) open.push(t.kind.slice(0, -5));
    else if (t.kind.endsWith("-close") && open.pop() !== t.kind.slice(0, -6)) return false;
  }
  return open.length === 0;
});
// Strings of the characters the tokenizer acts on, from a fixed seed, so a run is the
// same run every time.
let seed = 171;
const next = () => (seed = (seed * 1103515245 + 12345) % 2147483648);
const ALPHABET = ["*", "_", "**", "`", "``", "\\", "#", " ", "\t", "\n", "\r\n", "\r",
                  "-", "1.", "~~~", "```", "<", ">", "[", "](", ")", "!", "&", ";",
                  "a", "b", "(", "\u202e", "\u2028", "<!--", "-->", ".", "|"];
const fuzz = Array.from({length: 3000}, () => Array.from({length: next() % 40},
  () => ALPHABET[next() % ALPHABET.length]).join(""));
// Pathological shapes, each 10^5 characters or so: what a hostile body could hold.
const N = 100000;
const rep = (s, n) => s.repeat(Math.ceil(n / s.length)).slice(0, n);
const shapes = {
  openers_then_other_closers: rep("(*", N / 2) + rep("_)", N / 2),
  alternating: rep("*a _a ", N),
  strong_runs: rep("a**b", N),
  rising_backticks: Array.from({length: 446}, (_, k) => "`".repeat(k + 1)).join(" "),
  deep_nesting: rep("*a ", N / 2) + rep("a* ", N / 2),
  unclosed_comments: "x " + rep("<!-- ", N),
  unclosed_tags: "x " + rep("<a ", N),
  unclosed_links: rep("[a](b \"", N),
  items: rep("- a\n", N),
};
const timing = {};
const highlightTiming = {};
const lit = s => md.highlightRuns(s).map(r => r.text).join("");
for (const [name, s] of Object.entries(shapes)) {
  let start = performance.now();
  const ok = joined(s) === s;
  timing[name] = { ms: performance.now() - start, lossless: ok, length: s.length };
  start = performance.now();
  const lit_ok = lit(s) === s;
  highlightTiming[name] = { ms: performance.now() - start, lossless: lit_ok,
                            length: s.length };
}
const highlights = JSON.parse(process.env.DOCKADE_HL_CASES);
// Runs merge their neighbours of one class, and none is empty.
const merged = s => md.highlightRuns(s).every(
  (r, k, all) => r.text && (k === 0 || all[k - 1].cls !== r.cls));
console.log(JSON.stringify({
  highlighted: Object.fromEntries(Object.entries(highlights).map(
    ([k, s]) => [k, md.highlightRuns(s).map(r => [r.cls, r.text])])),
  highlight_lossless: Object.values(cases).concat(Object.values(highlights), fuzz)
    .filter(s => lit(s) !== s),
  highlight_unmerged: Object.values(cases).concat(Object.values(highlights), fuzz)
    .filter(s => !merged(s)),
  highlight_missing: [md.highlightRuns(undefined), md.highlightRuns(null),
                      md.highlightRuns(7)],
  highlightTiming,
  parsed: Object.fromEntries(Object.entries(cases).map(([k, s]) => [k, parsed(s)])),
  lossless: Object.values(cases).concat(fuzz).filter(s => joined(s) !== s),
  nested: Object.values(cases).concat(fuzz).filter(s => !nests(s)),
  fuzzed: fuzz.length,
  missing: [md.tokenize(undefined), md.tokenize(null), md.tokenize(7)],
  deepest: md.tokenize(shapes.deep_nesting)[0].tokens.filter(t => t.kind === "em-open")
    .length,
  timing,
}));
"""

# Texts the tokenizer reads, by name; each case's expected blocks are in the test that
# names it, as [kind, level or ordered, tokens], each token [kind, text].
_MD = {
    "heading": "# Title\n",
    "heading_six": "###### Six ##\n",
    "heading_tab": "##\tTab",
    "heading_empty": "#",
    "heading_crlf": "a\r\n# h\r\n",
    "heading_hidden": "## T\u202ex\u200b\n",
    "not_heading_hashtag": "#hashtag",
    "not_heading_seven": "####### seven",
    "not_heading_lone_cr": "x\r# no",
    "not_heading_u2028": "x\u2028# no",
    "paragraphs": "a\nb\n\nc",
    "bullets": "- a\n* b\n+ c\n",
    "bullet_tab": "-\tx",
    "nested": "- a\n  - b\n    - c",
    "numbered": "1. a\n2) b\n5. c\n",
    "item_continued": "- a\n  more",
    "emphasis": "*a* _b_ **c** __d__",
    "code_span": "`a` ``b`c`` `\u2066`",
    "lone_backtick": "a ` b",
    "fence": "```py x\u200b\ncode *a*\n```\nafter",
    "fence_tilde": "~~~\nx\n~~~~\n",
    "fence_unclosed": "```\nx\n\n# y\n",
    "fence_crlf": "```\r\nx\r\n```\r\n",
    "fence_backtick_info": "``` a`b",
    # Left as source: each is one token holding its text.
    "src_link": "a [l](http://x)",
    "src_image": "a ![i](http://y)",
    "src_html": "a <img src=x>",
    "src_close_tag": "a </div>",
    "src_comment": "a <!-- *c* -->",
    "src_comment_lines": "a <!--\n*b*\n--> c",
    "src_entity": "&amp; &#x41;",
    "src_escape": "a \\*",
    "src_autolink": "a <http://a.b/*x*>",
    "src_email": "a <1a@b.c>",
    "src_comment_gt": "a <!-- x > *y* -->",
    "src_processing": "a <?x *y* ?>",
    "src_reference": "a [a][b]",
    "src_quote": "> q",
    "src_table": "| t |",
    "src_rule": "---",
    "src_rule_stars": "* * *",
    "src_setext": "===",
    "src_indented": "    code",
    "src_indented_tab": "\tcode",
    "src_html_block": "<div>",
    # Emphasis that renders, and text that stays as written.
    "em_parens": "(**x**)",
    "em_period": "**x**.",
    "em_init": "the __init__ file",
    "em_quotes": "\"*q*\"",
    "text_task_notes": "task_notes",
    "text_init_py": "__init__.py",
    "text_glob_path": "src/*/x*",
    "text_product": "2*3*4",
    "text_glob": "*.py",
    "text_two_globs": "*.py and *.js",
    "text_spaced": "a * b * c",
    # A run that could both open and close does neither; a glob's or a path's run
    # neither opens before a `.` or `/` nor closes after a `/`.
    "text_quoted_globs": "the '**' glob and the '*' one",
    "text_quoted_runs": "matches \"*\" and \"**\" here",
    "text_quoted_pair": "a \"*\" b \"*\" c",
    "text_bracketed": "(*) and [*]",
    "text_glob_dir": "Edit the *.py files under src/*",
    "text_glob_list": "lint *.sh and scripts/*, not tests/*",
    # Each of those rules alone, beside a run that would pair with it.
    "text_both_then_closer": "'*' and b*",
    "text_opener_then_both": "*a and '*'",
    "text_dot_then_closer": "*.py and x*",
    "text_slash_then_closer": "*/x and y*",
    "text_opener_then_slash": "*a in src/*",
    "em_slash_inside": "*a/b*",
    "text_escaped_closer": "*a\\*",
    # Each character a run opens after, or closes before.
    "em_open_after": "[*a*] '*b*' \"*c*\" (*d*)",
    "em_close_before": "*a*, *b*; *c*: *d*! *e*? (*f*) [*g*] \"*h*\" '*i*' *j*.",
    "em_period_space": "**x**. y",
    "text_period_word": "**x**.y",
    "text_open_before_space": "x * a*",
    "text_close_after_space": "*a * b",
    "text_trailing_backslash": "a\\",
    "text_backslash_letter": "a \\q",
    # A closer takes the nearest opener of its own character, and what lies between
    # drops out; a run that opens twice opens the outer first.
    "em_other_between": "*a _b c*",
    "em_crossed": "*a _b* c_",
    "em_strong_em": "***a***",
    # An item: a bullet indented four columns opens one only in a list.
    "indented_bullet": "    - x",
    "list_left": "- a\n\nb\n\n    - c",
    "list_kept": "- a\n\n  b\n\n    - c",
    "list_paragraph": "- a\n\n    more",
    "fence_kept_open": "````\nx\n~~~\n```\n```` y\n````\nz",
    "fence_other_char": "```\n~~~\n```",
    # Nested as deep as markdown.js draws, and one deeper.
    "nest_16": "*a " * 16 + "a* " * 16,
    "nest_17": "*a " * 17 + "a* " * 17,
}

# Texts the spec editor's highlighter colours, by name; each case's runs, as [class,
# text], are in the test that names it.
_HL = {
    "heading": "## Head *em* `c`\nnext",
    "item": "- a **b** c\n12. d\n  * e",
    "spans": "x *em* **strong** `code` y",
    "strong_em": "***a*** _b **c** d_",
    "fence": "```py\n> not a quote **x**\n```\nafter",
    "quote": "> q **bold** `c`\n   > three\n    > four",
    "quote_in_item": "- a\n> q",
    "source": "a [l](u) <b>x</b> &amp; \\* <!-- c -->",
    "table": "| a | b |",
    "html": "<script>alert(1)</script>\n<img src=x onerror=alert(1)>",
    "crlf": "# h\r\n- i\r\n*e*\r\n",
    "lone_cr": "a\r# not a heading",
    "hidden": "a\u202e *b\u200b* c\u2028d",
    "unmatched": "*a **b",
    "em_lines": "*a\nb* c",
}


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class MarkdownTests(unittest.TestCase):
    """``markdown.js``, the tokenizer the detail draws agent markdown from and the spec
    editor's highlighter shares: lossless, the subset and no more, emphasis stricter
    than CommonMark's so paths and globs stay as written, and linear on hostile text."""

    probe: dict

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", _MARKDOWN_PROBE],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR)),
                 "DOCKADE_MD_CASES": json.dumps(_MD),
                 "DOCKADE_HL_CASES": json.dumps(_HL)},
            capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing markdown.js under node failed:\n" + proc.stderr)
        cls.probe = json.loads(proc.stdout)

    def blocks(self, name: str) -> list:
        return self.probe["parsed"][name]

    def test_the_tokens_joined_are_the_text_exactly(self):
        self.assertGreater(self.probe["fuzzed"], 1000)
        self.assertEqual(self.probe["lossless"], [])
        self.assertEqual(self.probe["missing"], [[], [], []])

    def test_every_span_opened_is_closed_in_its_block_innermost_first(self):
        self.assertEqual(self.probe["nested"], [])

    def test_an_atx_heading_drops_its_hashes_and_one_space_or_tab(self):
        self.assertEqual(self.blocks("heading"), [
            ["heading", 1, [["heading-marker", "# "], ["text", "Title"], ["eol", "\n"]]]])
        # Closing hashes are text.
        self.assertEqual(self.blocks("heading_six"), [
            ["heading", 6, [["heading-marker", "###### "], ["text", "Six ##"],
                            ["eol", "\n"]]]])
        self.assertEqual(self.blocks("heading_tab"), [
            ["heading", 2, [["heading-marker", "##\t"], ["text", "Tab"]]]])
        self.assertEqual(self.blocks("heading_empty"), [
            ["heading", 1, [["heading-marker", "#"]]]])
        self.assertEqual(self.blocks("heading_crlf"), [
            ["paragraph", None, [["text", "a"], ["eol", "\r\n"]]],
            ["heading", 1, [["heading-marker", "# "], ["text", "h"], ["eol", "\r\n"]]]])
        self.assertEqual(self.blocks("heading_hidden"), [
            ["heading", 2, [["heading-marker", "## "], ["text", "T\u202ex\u200b"],
                            ["eol", "\n"]]]])

    def test_only_lf_or_crlf_ends_a_line(self):
        for name in ("not_heading_hashtag", "not_heading_seven", "not_heading_lone_cr",
                     "not_heading_u2028"):
            with self.subTest(case=name):
                self.assertEqual(self.blocks(name),
                                 [["paragraph", None, [["text", _MD[name]]]]])

    def test_paragraphs_split_at_blank_lines(self):
        self.assertEqual(self.blocks("paragraphs"), [
            ["paragraph", None, [["text", "a"], ["eol", "\n"], ["text", "b"],
                                 ["eol", "\n"]]],
            ["blank", None, [["eol", "\n"]]],
            ["paragraph", None, [["text", "c"]]]])

    def test_items_drop_a_bullet_and_one_space_and_keep_a_number(self):
        self.assertEqual(self.blocks("bullets"), [
            ["item", False, [["bullet", "- "], ["text", "a"], ["eol", "\n"]]],
            ["item", False, [["bullet", "* "], ["text", "b"], ["eol", "\n"]]],
            ["item", False, [["bullet", "+ "], ["text", "c"], ["eol", "\n"]]]])
        self.assertEqual(self.blocks("bullet_tab"), [
            ["item", False, [["bullet", "-\t"], ["text", "x"]]]])
        self.assertEqual(self.blocks("numbered"), [
            ["item", True, [["number", "1."], ["text", " a"], ["eol", "\n"]]],
            ["item", True, [["number", "2)"], ["text", " b"], ["eol", "\n"]]],
            ["item", True, [["number", "5."], ["text", " c"], ["eol", "\n"]]]])
        self.assertEqual(self.blocks("item_continued"), [
            ["item", False, [["bullet", "- "], ["text", "a"], ["eol", "\n"],
                             ["text", "  more"]]]])

    def test_items_nest_by_their_indentation_which_is_kept(self):
        self.assertEqual(self.blocks("nested"), [
            ["item", False, [["bullet", "- "], ["text", "a"], ["eol", "\n"]]],
            ["item", False, [["indent", "  "], ["bullet", "- "], ["text", "b"],
                             ["eol", "\n"]]],
            ["item", False, [["indent", "    "], ["bullet", "- "], ["text", "c"]]]])

    def test_emphasis_strong_and_code_spans(self):
        self.assertEqual(self.blocks("emphasis"), [["paragraph", None, [
            ["em-open", "*"], ["text", "a"], ["em-close", "*"], ["text", " "],
            ["em-open", "_"], ["text", "b"], ["em-close", "_"], ["text", " "],
            ["strong-open", "**"], ["text", "c"], ["strong-close", "**"], ["text", " "],
            ["strong-open", "__"], ["text", "d"], ["strong-close", "__"]]]])
        # A span closes on a run of its own length only, and a hidden character in it
        # is kept for the page to spell.
        self.assertEqual(self.blocks("code_span"), [["paragraph", None, [
            ["code-open", "`"], ["code", "a"], ["code-close", "`"], ["text", " "],
            ["code-open", "``"], ["code", "b`c"], ["code-close", "``"], ["text", " "],
            ["code-open", "`"], ["code", "\u2066"], ["code-close", "`"]]]])
        self.assertEqual(self.blocks("lone_backtick"),
                         [["paragraph", None, [["text", "a ` b"]]]])

    def test_a_fenced_block_keeps_its_info_string_and_runs_to_its_close(self):
        self.assertEqual(self.blocks("fence"), [
            ["code", None, [["fence", "```"], ["info", "py x\u200b"], ["eol", "\n"],
                            ["code", "code *a*"], ["eol", "\n"], ["fence", "```"],
                            ["eol", "\n"]]],
            ["paragraph", None, [["text", "after"]]]])
        # Closed by a run as long or longer, of its own character.
        self.assertEqual(self.blocks("fence_tilde"), [
            ["code", None, [["fence", "~~~"], ["eol", "\n"], ["code", "x"], ["eol", "\n"],
                            ["fence", "~~~~"], ["eol", "\n"]]]])
        # Unclosed, it runs to the end, a blank line and a heading included.
        self.assertEqual(self.blocks("fence_unclosed"), [
            ["code", None, [["fence", "```"], ["eol", "\n"], ["code", "x"], ["eol", "\n"],
                            ["eol", "\n"], ["code", "# y"], ["eol", "\n"]]]])
        self.assertEqual(self.blocks("fence_crlf"), [
            ["code", None, [["fence", "```"], ["eol", "\r\n"], ["code", "x"],
                            ["eol", "\r\n"], ["fence", "```"], ["eol", "\r\n"]]]])
        # A backtick fence whose info string holds a backtick is no fence.
        self.assertEqual(self.blocks("fence_backtick_info")[0][0], "paragraph")

    def test_every_construct_outside_the_subset_is_source_holding_its_text(self):
        for name in ("src_link", "src_image", "src_html", "src_escape", "src_autolink",
                     "src_email", "src_reference", "src_comment_gt", "src_processing"):
            with self.subTest(case=name):
                self.assertEqual(self.blocks(name), [["paragraph", None, [
                    ["text", "a "], ["source", _MD[name][2:]]]]])
        self.assertEqual(self.blocks("src_entity"), [["paragraph", None, [
            ["source", "&amp;"], ["text", " "], ["source", "&#x41;"]]]])
        # Nothing inside an HTML comment or an autolink is read, over lines too.
        self.assertEqual(self.blocks("src_comment"), [["paragraph", None, [
            ["text", "a "], ["source", "<!-- *c* -->"]]]])
        self.assertEqual(self.blocks("src_comment_lines"), [["paragraph", None, [
            ["text", "a "], ["source", "<!--\n*b*\n-->"], ["text", " c"]]]])
        self.assertEqual(self.blocks("src_close_tag"), [["paragraph", None, [
            ["text", "a "], ["source", "</div>"]]]])
        for name in ("src_quote", "src_table", "src_rule", "src_rule_stars", "src_setext",
                     "src_indented", "src_indented_tab", "src_html_block"):
            with self.subTest(case=name):
                self.assertEqual(self.blocks(name),
                                 [["source", None, [["source", _MD[name]]]]])

    def test_emphasis_opens_and_closes_only_beside_space_or_punctuation(self):
        self.assertEqual(self.blocks("em_parens"), [["paragraph", None, [
            ["text", "("], ["strong-open", "**"], ["text", "x"], ["strong-close", "**"],
            ["text", ")"]]]])
        self.assertEqual(self.blocks("em_period"), [["paragraph", None, [
            ["strong-open", "**"], ["text", "x"], ["strong-close", "**"], ["text", "."]]]])
        # As CommonMark reads it too: a word in the open is a word.
        self.assertEqual(self.blocks("em_init"), [["paragraph", None, [
            ["text", "the "], ["strong-open", "__"], ["text", "init"],
            ["strong-close", "__"], ["text", " file"]]]])
        # A slash inside a span is no path's end.
        self.assertEqual(self.blocks("em_slash_inside"), [["paragraph", None, [
            ["em-open", "*"], ["text", "a/b"], ["em-close", "*"]]]])
        self.assertEqual(self.blocks("em_quotes"), [["paragraph", None, [
            ["text", "\""], ["em-open", "*"], ["text", "q"], ["em-close", "*"],
            ["text", "\""]]]])

    def test_each_character_beside_a_run_is_one_it_opens_after_or_closes_before(self):
        def pairs(name):
            kinds = [k for k, _ in self.blocks(name)[0][2]]
            return kinds.count("em-open"), kinds.count("em-close")
        self.assertEqual(pairs("em_open_after"), (4, 4))
        self.assertEqual(pairs("em_close_before"), (10, 10))
        self.assertEqual(self.blocks("em_period_space"), [["paragraph", None, [
            ["strong-open", "**"], ["text", "x"], ["strong-close", "**"], ["text", ". y"]]]])
        for name in ("text_period_word", "text_open_before_space", "text_close_after_space",
                     "text_trailing_backslash", "text_backslash_letter"):
            with self.subTest(case=name):
                self.assertEqual(self.blocks(name),
                                 [["paragraph", None, [["text", _MD[name]]]]])

    def test_a_closer_takes_its_own_characters_nearest_opener_nested(self):
        self.assertEqual(self.blocks("em_other_between"), [["paragraph", None, [
            ["em-open", "*"], ["text", "a _b c"], ["em-close", "*"]]]])
        self.assertEqual(self.blocks("em_crossed"), [["paragraph", None, [
            ["em-open", "*"], ["text", "a _b"], ["em-close", "*"], ["text", " c_"]]]])
        self.assertEqual(self.blocks("em_strong_em"), [["paragraph", None, [
            ["em-open", "*"], ["strong-open", "**"], ["text", "a"], ["strong-close", "**"],
            ["em-close", "*"]]]])

    def test_an_indented_bullet_is_an_item_only_in_a_list(self):
        self.assertEqual(self.blocks("indented_bullet"),
                         [["source", None, [["source", "    - x"]]]])
        # A paragraph at the margin leaves the list; one indented stays in it.
        self.assertEqual([b[0] for b in self.blocks("list_left")],
                         ["item", "blank", "paragraph", "blank", "source"])
        self.assertEqual([b[0] for b in self.blocks("list_kept")],
                         ["item", "blank", "paragraph", "blank", "item"])
        self.assertEqual(self.blocks("list_paragraph"), [
            ["item", False, [["bullet", "- "], ["text", "a"], ["eol", "\n"]]],
            ["blank", None, [["eol", "\n"]]],
            ["paragraph", None, [["text", "    more"]]]])

    def test_a_fence_closes_on_its_own_character_as_long_or_longer_and_alone(self):
        self.assertEqual(self.blocks("fence_kept_open"), [
            ["code", None, [["fence", "````"], ["eol", "\n"], ["code", "x"], ["eol", "\n"],
                            ["code", "~~~"], ["eol", "\n"], ["code", "```"], ["eol", "\n"],
                            ["code", "```` y"], ["eol", "\n"], ["fence", "````"],
                            ["eol", "\n"]]],
            ["paragraph", None, [["text", "z"]]]])
        self.assertEqual(self.blocks("fence_other_char"), [
            ["code", None, [["fence", "```"], ["eol", "\n"], ["code", "~~~"], ["eol", "\n"],
                            ["fence", "```"]]]])

    def test_paths_globs_and_arithmetic_stay_as_written(self):
        for name in ("text_task_notes", "text_init_py", "text_glob_path", "text_product",
                     "text_glob", "text_two_globs", "text_spaced", "text_quoted_globs",
                     "text_quoted_runs", "text_quoted_pair", "text_bracketed",
                     "text_glob_dir", "text_glob_list", "text_both_then_closer",
                     "text_opener_then_both", "text_dot_then_closer",
                     "text_slash_then_closer", "text_opener_then_slash"):
            with self.subTest(case=name):
                self.assertEqual(self.blocks(name),
                                 [["paragraph", None, [["text", _MD[name]]]]])
        # An escaped delimiter is source, and closes nothing.
        self.assertEqual(self.blocks("text_escaped_closer"), [["paragraph", None, [
            ["text", "*a"], ["source", "\\*"]]]])

    def test_hostile_shapes_take_linear_time(self):
        # Linear is a tenth of a second or so; a search for an opener with no floor
        # takes seconds on the first shape.
        for name, t in self.probe["timing"].items():
            with self.subTest(shape=name):
                self.assertTrue(t["lossless"])
                self.assertGreater(t["length"], 99_000)
                self.assertLess(t["ms"], 1000)
        # Deep nesting is matched, and drawn NEST_MAX deep.
        self.assertEqual(self.probe["deepest"], 16)

    def test_spans_nest_sixteen_deep_and_the_ones_inside_are_text(self):
        # A body at the registry's cap nests about 8000 deep, which Chromium took 0.8 s
        # to lay out (tasks-ui/NOTES.md, "Nested spans cost the square of their depth").
        for name, depth in (("nest_16", 16), ("nest_17", 17)):
            with self.subTest(case=name):
                tokens = self.blocks(name)[0][2]
                kinds = [k for k, _ in tokens]
                self.assertEqual((kinds.count("em-open"), kinds.count("em-close")), (16, 16))
                stars = "".join(t for k, t in tokens if k == "text").count("*")
                self.assertEqual(stars, 2 * (depth - 16))
                # The outermost are the ones kept.
                self.assertEqual(tokens[0], ["em-open", "*"])
                self.assertEqual(tokens[-2], ["em-close", "*"])

    def runs(self, name: str) -> list:
        return self.probe["highlighted"][name]

    def test_the_highlighters_runs_joined_are_the_text_exactly(self):
        self.assertEqual(self.probe["highlight_lossless"], [])
        self.assertEqual(self.probe["highlight_unmerged"], [])
        self.assertEqual(self.probe["highlight_missing"], [[], [], []])
        for name, text in _HL.items():
            with self.subTest(case=name):
                self.assertEqual("".join(t for _, t in self.runs(name)), text)

    def test_the_highlighter_colours_each_construct_by_what_the_detail_draws(self):
        # A heading's line whole, its spans included; its line end plain.
        self.assertEqual(self.runs("heading"), [
            ["heading", "## Head *em* `c`"], ["", "\nnext"]])
        # An item's bullet or number, not its indent.
        self.assertEqual(self.runs("item"), [
            ["marker", "- "], ["", "a "], ["strong", "**b**"], ["", " c\n"],
            ["marker", "12."], ["", " d\n  "], ["marker", "* "], ["", "e"]])
        self.assertEqual(self.runs("spans"), [
            ["", "x "], ["em", "*em*"], ["", " "], ["strong", "**strong**"], ["", " "],
            ["code", "`code`"], ["", " y"]])
        # The innermost span's class, a delimiter taking the span it opens or closes.
        self.assertEqual(self.runs("strong_em"), [
            ["em", "*"], ["strong", "**a**"], ["em", "*"], ["", " "], ["em", "_b "],
            ["strong", "**c**"], ["em", " d_"]])
        self.assertEqual(self.runs("fence"), [
            ["code", "```py"], ["", "\n"], ["code", "> not a quote **x**"], ["", "\n"],
            ["code", "```"], ["", "\nafter"]])
        self.assertEqual(self.runs("unmatched"), [["", "*a **b"]])
        # A line end inside a span is the span's.
        self.assertEqual(self.runs("em_lines"), [["em", "*a\nb*"], ["", " c"]])

    def test_a_quote_line_is_a_quote_whole_and_only_as_the_tokenizer_reads_one(self):
        # Bold and code inside it are the quote's, as the detail draws it as typed; up
        # to three spaces before its `>`, and at four it is indented code, source.
        self.assertEqual(self.runs("quote"), [
            ["quote", "> q **bold** `c`"], ["", "\n"], ["quote", "   > three"],
            ["", "\n"], ["source", "    > four"]])
        self.assertEqual(self.runs("quote_in_item"), [
            ["marker", "- "], ["", "a\n"], ["quote", "> q"]])
        self.assertEqual(self.runs("table"), [["source", "| a | b |"]])

    def test_every_other_construct_is_source_and_html_is_only_text(self):
        self.assertEqual(self.runs("source"), [
            ["", "a "], ["source", "[l](u)"], ["", " "], ["source", "<b>"], ["", "x"],
            ["source", "</b>"], ["", " "], ["source", "&amp;"], ["", " "],
            ["source", "\\*"], ["", " "], ["source", "<!-- c -->"]])
        self.assertEqual(self.runs("html"), [
            ["source", "<script>alert(1)</script>"], ["", "\n"],
            ["source", "<img src=x onerror=alert(1)>"]])

    def test_line_ends_and_hidden_characters_are_kept_as_typed(self):
        self.assertEqual(self.runs("crlf"), [
            ["heading", "# h"], ["", "\r\n"], ["marker", "- "], ["", "i\r\n"],
            ["em", "*e*"], ["", "\r\n"]])
        self.assertEqual(self.runs("lone_cr"), [["", _HL["lone_cr"]]])
        # Not spelled: the mirror holds what the textarea holds.
        self.assertEqual(self.runs("hidden"), [
            ["", "a\u202e "], ["em", "*b\u200b*"], ["", " c\u2028d"]])

    def test_the_highlighter_takes_linear_time_on_hostile_shapes(self):
        for name, t in self.probe["highlightTiming"].items():
            with self.subTest(shape=name):
                self.assertTrue(t["lossless"])
                self.assertGreater(t["length"], 99_000)
                self.assertLess(t["ms"], 1000)

    def test_markdown_js_imports_nothing_and_draws_nothing(self):
        src = (TASKS_DIR / "markdown.js").read_text()
        self.assertNotRegex(src, r"^\s*import\b")
        self.assertNotRegex(_code(src), r"\b(?:document|window)\b")


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
        # Kept in view under the task's strip, which is kept in view at the top: as far
        # down as the strip is high.
        top = re.search(r"\.notice-slot:not\(:empty\) \{ position: sticky; top: (\d+)px;",
                        self.HTML).group(1)
        strip = re.search(r"\.task-strip \{([^}]*)\}", self.HTML).group(1)
        self.assertIn("position: sticky; top: 0;", strip)
        self.assertEqual(re.search(r"\bheight: (\d+)px;", strip).group(1), top)

    def test_read_it_again_reads_the_task_again_through_the_hash(self):
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
    REPOS = (TASKS_DIR / "repos.js").read_text()
    POST = re.compile(r'fetch\(`([^`]+)`,\s*\{\s*method: "POST"')

    def _function(self, src: str, name: str) -> str:
        return re.search(rf"^(?:export )?(?:async )?function {name}\(.*?^\}}", src,
                         re.M | re.S).group(0)

    def test_each_relayed_post_is_a_post_fetch_in_moves_js_or_repos_js_and_back(self):
        # PageChecks matches paths, not methods: this holds the methods. The task and
        # note writes are moves.js's, the repo list's repos.js's.
        posts = [re.sub(r"\$\{[^}]*\}", "1", p)
                 for p in self.POST.findall(self.MOVES) + self.POST.findall(self.REPOS)]
        self.assertEqual(len(posts), 14)
        routes = [rx for method, rx in tui._RELAY_ROUTES if method == "POST"]
        for path in posts:
            with self.subTest(path=path):
                self.assertTrue(tui._relay_allowed("POST", path), path)
        for rx in routes:
            with self.subTest(route=rx.pattern):
                self.assertTrue(any(rx.match(p) for p in posts), rx.pattern)
        # Every fetch in moves.js and repos.js is one of them; tasks.js posts nothing.
        self.assertEqual(len(re.findall(r"\bfetch\(", self.MOVES)), 10)
        self.assertEqual(len(re.findall(r"\bfetch\(", self.REPOS)), 4)
        self.assertNotIn("method:", self.TASKS)

    def test_moves_js_imports_nothing_of_tasks_js(self):
        self.assertNotRegex(self.MOVES, r'from\s+"\./tasks\.js"')
        self.assertNotIn("document", _code(self.MOVES))

    def test_repos_js_imports_nothing_of_tasks_js_and_no_document_at_import(self):
        self.assertNotRegex(self.REPOS, r'from\s+"\./tasks\.js"')
        self.assertEqual(re.findall(r'^import .* from "(\./[^"]+)";$', self.REPOS, re.M),
                         ["./moves.js"])
        # The document only inside its functions: no top-level line reaches it, so
        # `RepoTests` imports it under node.
        top = [ln for ln in _code(self.REPOS).splitlines()
               if ln and not ln[0].isspace() and "document" in ln]
        self.assertEqual(top, [])

    def test_a_repo_write_goes_through_the_one_gate(self):
        # The page's own `writing`, so one write is out page-wide; the view's controls
        # among those markWriting turns off.
        begin = self._function(self.TASKS, "beginRepoWrite")
        self.assertEqual([ln.strip() for ln in begin.splitlines()[1:5]],
                         ["if (writing || !registryIsRunning()) return false;",
                          "writing = write;", "markWriting();", "return true;"])
        self.assertEqual([ln.strip() for ln in
                          self._function(self.TASKS, "endRepoWrite").splitlines()[1:3]],
                         ["writing = null;", "markWriting();"])
        self.assertIn("markRepos();", self._function(self.TASKS, "markWriting"))
        self.assertIn("repoHooks({ begin: beginRepoWrite, end: endRepoWrite, "
                      "busy: () => writing !== null,", self.TASKS)
        run = self._function(self.REPOS, "runRepoWrite")
        self.assertEqual(run.splitlines()[1].strip(),
                         "if (!hooks || !hooks.begin(write)) return;")
        # The list read again inside the gate, before it opens; the lost answer decided
        # from that read, and the write sent once.
        self.assertLess(run.index("sendRepoWrite(write)"), run.index("await hooks.read("))
        self.assertLess(run.index("await hooks.read("),
                        run.index("result = checkRepoWrite(write, read);"))
        self.assertLess(run.index("result = checkRepoWrite(write, read);"),
                        run.index("hooks.end();"))
        self.assertEqual(run.count("sendRepoWrite("), 1)
        self.assertEqual(len(re.findall(r"\brunRepoWrite\(", _code(self.REPOS))), 2,
                         "defined once, called from the view's click")
        mark = self._function(self.REPOS, "markRepos")
        self.assertIn("const out = busy();", mark)
        self.assertIn('for (const el of reposView.querySelectorAll("input")) '
                      "el.disabled = out;", mark)
        self.assertIn("repoAdd.disabled = out || !add;", mark)
        self.assertIn("btn.disabled = out || !write;", mark)
        self.assertIn("if (!btn || busy() || ev.detail > 1) return;", self.REPOS)
        self.assertIn("return Boolean(hooks) && hooks.busy();",
                      self._function(self.REPOS, "busy"))
        # A 502 says not running, and the page's other reads wait for it to answer.
        self.assertIn('if (result === "not-running") hooks.notRunning();', run)
        self.assertIn('if (result !== "not-running") hooks.after();', run)
        self.assertTrue(run.rstrip("}\n ").endswith(
            'if (result !== "not-running") hooks.after();\n  return repoOutcome;'))
        # The rename warning's line, and the remove control, only where they apply.
        self.assertIn("el.hidden = !shown;", mark)
        self.assertIn('if (typeof repo.remote === "string") remoteRow.appendChild('
                      'repoButton("remove", repo.name));',
                      self._function(self.REPOS, "repoRow"))
        # The empty line shows with an empty list, and only then.
        self.assertIn("reposEmpty.hidden = drawnRepos.length > 0;",
                      self._function(self.REPOS, "renderRepos"))
        # A refusal's reason is spelled as the page spells agent text.
        self.assertIn("spell: text => spellHidden(text).text });", self.TASKS)
        # The view is shown as the others are, by its own name.
        self.assertIn('reposSection.hidden = name !== "repos";',
                      self._function(self.TASKS, "showView"))

    def test_a_rename_of_the_lists_repo_takes_the_select_with_it(self):
        # A rename, landed or found, is read with what it renamed; the select follows it
        # by the list read, and a repo gone otherwise starts the list again.
        run = self._function(self.REPOS, "runRepoWrite")
        self.assertIn('const renamed = write.kind === "rename" ? { from: write.repo, '
                      "to: write.send.new_name }\n    : null;", run)
        self.assertIn('const read = result === "not-running" ? null : '
                      "await hooks.read(renamed);", run)
        draw = self._function(self.TASKS, "drawRepos")
        self.assertIn("const kept = followedRepo(chosen, renamed, repos);\n"
                      "  if (kept !== null) tasksRepo.value = kept;\n"
                      "  else if (chosen) resetList();", draw)
        self.assertIn("renderRepos(repos);", draw)
        self.assertTrue(self._function(self.TASKS, "refreshRepos").rstrip("}\n ")
                        .endswith("return list;"))
        self.assertIn("read: refreshRepos,", self.TASKS)
        # A write's other reads after it, as runWrite's.
        self.assertIn("after: () => { refreshInbox(); refreshTasks(); },", self.TASKS)

    def test_a_landed_repo_write_drops_only_its_own_typed_text(self):
        run = self._function(self.REPOS, "runRepoWrite")
        self.assertIn('if (result === "landed" || result === "found") dropTyped(write);',
                      run)
        drop = self._function(self.REPOS, "dropTyped")
        self.assertIn("drafts.delete(write.repo);", drop)
        self.assertIn('} else if (write.kind === "remote" && write.send.remote !== null) {\n'
                      '    draftOf(write.repo).remote = "";', drop)
        self.assertEqual(re.findall(r"\bdrafts\.delete\(", self.REPOS), ["drafts.delete("])

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
        self.assertEqual(len(re.findall(r"\brunWrite\(", _code(self.TASKS))), 4,
                         "defined once, called from the detail, the flagged block and "
                         "the create form")
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
        self.assertIn('if (result === "unknown") {\n    const reread = '
                      'write.kind === "handle-many"\n'
                      '      ? { flagged: await readFlaggedPast(write.notes).catch(() => '
                      'null) }\n'
                      '      : write.kind === "create" ? await readCreated(write) : drawn;\n'
                      '    result = checkWrite(write, reread);', body)
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
        # A bulk is on no task (`task: null`), so the detail never draws its outcome.
        self.assertIn("    outcome = said;\n", body)
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
        # The bulk handle and the create are the writes outside the detail, and each
        # control is the flagged block's or the form's own, never a row's.
        for name in ("inboxRow", "renderInbox", "renderTasks", "renderFlagged",
                     "flaggedRow"):
            with self.subTest(function=name):
                self.assertNotIn("writeButton(", self._function(self.TASKS, name))
                self.assertNotIn("dataset.write", self._function(self.TASKS, name))
        calls = re.findall(r"\bmovesBlock\(", self.TASKS)
        self.assertEqual(len(calls), 2, "defined once, called once, in drawTask")
        self.assertIn("movesBlock(task)",
                      self._function(self.TASKS, "drawTask"))
        self.assertEqual(len(re.findall(r"\bbulkWrite\(", self.TASKS)), 2,
                         "defined once, called once, from the flagged block")
        # A bulk is on no task: the flagged block is part of the list, never on screen
        # with a task open, so there is no open task for it to read again.
        bulk = self._function(self.TASKS, "bulkWrite")
        self.assertIn('return { kind: "handle-many", notes, leftOut, task: null,', bulk)
        self.assertNotIn("openTask", bulk)
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
        for name in ("refreshTasks", "refreshInbox", "readFlagged", "refreshRepos"):
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

    def test_no_poll_reads_the_repo_list_while_the_registry_answers(self):
        # Its rows hold what is typed, so nothing redraws them unasked: the poll's
        # chain never reads the list, and only these do. A poll that finds the registry
        # back reads it through `probe`, while the view is hidden behind the line that
        # says it is down.
        for name in ("refreshInbox", "refreshTasks", "refreshOpenTask", "readFlagged"):
            with self.subTest(function=name):
                body = self._function(self.TASKS, name)
                self.assertNotIn("/api/repos", body)
                self.assertNotIn("refreshRepos(", body)
        self.assertNotIn("refreshRepos", self.APP)
        calls = sorted(ln.strip() for ln in re.findall(r"^.*\brefreshRepos\b.*$",
                                                        self.TASKS, re.M)
                       if "function refreshRepos" not in ln)
        self.assertEqual(calls, [
            "await refreshRepos();",
            "if (entering) refreshRepos();",
            'if (result === "refused") refreshRepos();',
            "notRunning: () => setRunning(false), read: refreshRepos,"])
        # Entering Repos, not a hash change that keeps the view.
        view = self._function(self.TASKS, "showView")
        self.assertIn('const entering = view !== "repos" && name === "repos";', view)
        self.assertLess(view.index("const entering"), view.index("view = name;"))
        # One read draws the view and the select both.
        self.assertEqual(re.findall(r'fetch\("/api/repos"\)', self.TASKS),
                         ['fetch("/api/repos")'])
        probe = self._function(self.TASKS, "probe")
        self.assertLess(probe.index("if (up) {"), probe.index("await refreshRepos();"))

    def test_a_repo_list_read_overtaken_by_a_newer_one_draws_nothing(self):
        # A read answered after a newer one was drawn has the older list: it draws
        # nothing, but returns what it read, so a write whose read it is is settled
        # from it rather than "could not check".
        refresh = self._function(self.TASKS, "refreshRepos")
        self.assertLess(refresh.index("const asked = ++reposAsks;"),
                        refresh.index('fetch("/api/repos")'))
        self.assertIn("  if (list !== null && asked > reposDrawn) {\n"
                      "    reposDrawn = asked;\n    drawRepos(list, following);\n  }",
                      refresh)
        self.assertTrue(refresh.rstrip("}\n ").endswith(
            "if (renamed) following = null;\n  return list;"))
        self.assertEqual(refresh.count("return "), 2,
                         "the early return unless the registry answers, and the list")
        self.assertEqual(re.findall(r"\breposDrawn = ([^;]*);", self.TASKS), ["0", "asked"])
        self.assertEqual(re.findall(r"\brepos = ([^;]*);", self.TASKS), ["[]", "list"])
        self.assertIn("repos = list;", self._function(self.TASKS, "drawRepos"))
        # A rename is followed by whichever read draws first once it is asked, so a
        # read asked on entering Repos that overtakes the write's own follows it too;
        # the write's read, done, stops it.
        self.assertLess(refresh.index("if (renamed) following = renamed;"),
                        refresh.index('fetch("/api/repos")'))
        self.assertEqual(re.findall(r"\bfollowing = ([^;]*);", self.TASKS),
                         ["null", "renamed", "null"])
        draw = self._function(self.TASKS, "drawRepos")
        self.assertIn("const kept = followedRepo(chosen, renamed, repos);", draw)

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
        # Back from All tasks or Repos; and a bulk's outcome lasts until another view.
        view = self._function(self.TASKS, "showView")
        self.assertIn('const back = view !== "inbox" && name === "inbox";', view)
        self.assertIn('if (name !== "inbox") flaggedOutcome = null;', view)
        self.assertLess(view.index("const back"), view.index("view = name;"))
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
        # A task named in the detail opens through the hash too, so back returns to
        # the task it was opened from.
        self.assertIn("goTo(view, named.dataset.task);", self.TASKS)

    def test_leaving_the_task_by_any_hash_change_asks_before_an_edit_is_lost(self):
        # Every way off the task changes the hash, and the hash's handler asks, so the
        # browser's back and forward and a link ask as the back control and Esc do;
        # goTo asks before it sets the hash, so the hashchange after asks nothing more.
        leave = "if (openTask !== null && named.task !== openTask && !mayLeave()) {"
        apply = self._function(self.TASKS, "applyHash")
        self.assertIn(leave, apply)
        self.assertLess(apply.index(leave), apply.index("showView(named.view);"))
        # Declined, the move is undone in history, never rewritten over; every entry
        # the page follows carries its index, a new one the next.
        self.assertIn("    undoLeave(index);\n    return;", apply)
        self.assertIn("appliedIndex = index ?? appliedIndex + 1;\n"
                      "  if (index === null) stampEntry(appliedIndex);", apply)
        undo = self._function(self.TASKS, "undoLeave")
        # A traversal reaches another entry: the same one fires no hashchange.
        self.assertIn("if (index !== null) window.history.go(appliedIndex - index);", undo)
        self.assertIn("else window.history.back();", undo)
        # The URL is only ever replaced keeping the entry's index.
        self.assertEqual(len(re.findall(r"replaceState\(null\b", self.TASKS)), 0)
        go = self._function(self.TASKS, "goTo")
        self.assertIn("if (openTask !== null && task !== openTask && !mayLeave()) return;",
                      go)
        may = self._function(self.TASKS, "mayLeave")
        self.assertIn("if (!draftChanged()) return true;", may)
        self.assertIn("if (!window.confirm(LEAVE_TEXT)) return false;", may)
        self.assertIn("draftChanges(specEdit.base, specEdit.draft).length > 0",
                      self._function(self.TASKS, "draftChanged"))
        # Closing or reloading the page asks by the same rule.
        self.assertIn('window.addEventListener("beforeunload", ev => {\n'
                      "    if (!draftChanged()) return;", self.TASKS)
        # The confirm is the only one on the page: no move asks one.
        self.assertEqual(self.TASKS.count("confirm("), 1)

    def test_the_list_and_a_task_are_never_on_screen_together(self):
        enter_task = self._function(self.TASKS, "enterTask")
        self.assertIn("listView.hidden = true;\n  taskView.hidden = false;", enter_task)
        # The list's offset is kept only when the list is what is left.
        self.assertIn("if (!listView.hidden) listScroll = window.scrollY;", enter_task)
        self.assertIn("window.scrollTo(0, 0);", enter_task)
        self.assertIn("window.scrollTo(0, listScroll);", self._function(self.TASKS, "enterList"))
        self.assertIn("enterList();", self._function(self.TASKS, "closeTask"))
        # Only another task starts at the top: the one open read again keeps its scroll.
        show = self._function(self.TASKS, "showTask")
        self.assertIn("const another = id !== openTask;", show)
        self.assertIn("if (another) enterTask();", show)
        self.assertIn('window.history.scrollRestoration = "manual";', self.TASKS)
        # Back on the list, the row last read stays marked; a list row only.
        mark = self._function(self.TASKS, "markOpen")
        self.assertIn("const marked = openTask ?? lastRead;", mark)
        self.assertIn('listView.querySelectorAll("tr button[data-task]")', mark)
        self.assertNotIn("lastRead", self._function(self.TASKS, "closeTask"))

    def test_a_task_the_page_cannot_open_leaves_the_list_and_its_url(self):
        # Not read at all when no id can be what the hash names; a 404 once read. Either
        # puts the list's URL in place of the task's, so back and a reload pass over it.
        apply = self._function(self.TASKS, "applyHash")
        bad = apply[apply.index("if (named.bad !== null) {"):]
        self.assertLess(bad.index("sayOnList(badTaskText(named.bad));"),
                        bad.index("if (!registryIsRunning()) return;"))
        self.assertIn("window.history.replaceState(window.history.state, \"\",\n"
                      "                                urlFor(hashFor(named.view, null)));",
                      bad)
        show = self._function(self.TASKS, "showTask")
        missing = show[show.index('if (e.message === "404") {'):]
        self.assertIn("window.history.replaceState(window.history.state, \"\", "
                      "urlFor(hashFor(view, null)));", missing)
        self.assertIn("sayOnList(unknownTaskText(id));", missing)
        # The notice lasts until the next open or change of view.
        self.assertIn("sayOnList(null);", show)
        self.assertIn("if (name !== view) sayOnList(null);",
                      self._function(self.TASKS, "showView"))

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


class TaskRelationSourceTests(unittest.TestCase):
    """A task's parent, children and blockers, each a row whose ``#id`` opens its task
    through the hash, the children in a list that collapses. Read from the source, as
    the other detail guards are; the UI harness clicks them."""

    TASKS = (TASKS_DIR / "tasks.js").read_text()
    HTML = (TASKS_DIR / "index.html").read_text()

    def test_each_relation_opens_its_task_its_title_spelled_as_text(self):
        row = _function(self.TASKS, "relatedRow")
        self.assertIn("open.dataset.task = String(t.id);", row)
        self.assertIn("title.textContent = ` ${spellHidden(t.title).text} `;", row)
        self.assertIn("state.textContent = `(${t.state})`;", row)
        self.assertNotIn("innerHTML", row)
        self.assertIn("row.appendChild(relatedRow(t));", _function(self.TASKS, "relatedRows"))
        draw = _function(self.TASKS, "drawTask")
        self.assertIn('line("parent", relatedRow(task.parent))', draw)
        self.assertIn("if (task.blockers && task.blockers.length && !editing) {", draw)
        self.assertIn("task.blockers));", draw)
        # A detail button opens as a list row's `#id` does: through the hash.
        self.assertIn("goTo(view, named.dataset.task);", self.TASKS)

    def test_the_children_are_a_list_open_unless_the_operator_closed_it(self):
        draw = _function(self.TASKS, "drawTask")
        self.assertIn("if (childrenLeft(task.children).total) {\n"
                      "    taskDetail.appendChild(childrenList(task.children));", draw)
        kids = _function(self.TASKS, "childrenList")
        self.assertIn('document.createElement("details")', kids)
        self.assertIn("summary.textContent = `children ${childrenLeft(children).text}`;",
                      kids)
        self.assertIn("list.open = childrenOpen;", kids)
        # Expanded on every load: no storage keeps the choice.
        self.assertIn("\nlet childrenOpen = true;\n", self.TASKS)
        self.assertNotRegex(self.TASKS, r"localStorage|sessionStorage")

    def test_the_choice_is_kept_from_the_lists_own_toggle(self):
        # `toggle` does not bubble: on the detail it would hear nothing, or in the capture
        # phase the folded history's too.
        self.assertIn('list.addEventListener("toggle", () => { childrenOpen = list.open; });',
                      _function(self.TASKS, "childrenList"))
        self.assertEqual(self.TASKS.count('"toggle"'), 1)

    def test_a_button_in_the_detail_is_no_list_row_to_mark(self):
        # markOpen reads the list's rows alone, and the detail is outside the list, so a
        # relation's button, which has no row, is never asked for one.
        self.assertIn('listView.querySelectorAll("tr button[data-task]")',
                      _function(self.TASKS, "markOpen"))
        ancestors = {}

        class Ids(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []

            def handle_starttag(self, tag, attrs):
                attr = dict(attrs)
                if "id" in attr:
                    ancestors[attr["id"]] = [i for _, i in self.stack if i]
                if tag not in ("br", "input", "meta", "link", "img"):
                    self.stack.append((tag, attr.get("id")))

            def handle_endtag(self, tag):
                while self.stack and self.stack.pop()[0] != tag:
                    pass

        Ids().feed(self.HTML)
        self.assertIn("task-view", ancestors["task-detail"])
        self.assertNotIn("list-view", ancestors["task-detail"])

    def test_a_double_clicks_second_click_opens_no_related_task(self):
        # Keyboard activation's detail is 0, so the keyboard still opens.
        self.assertIn('const named = ev.target.closest("button[data-task]");\n'
                      "    if (named) {\n"
                      "      if (ev.detail <= 1) goTo(view, named.dataset.task);\n"
                      "      return;\n"
                      "    }", self.TASKS)

    def test_a_double_clicks_second_click_sends_no_move(self):
        self.assertIn('const btn = ev.target.closest("button[data-write]");\n'
                      "    // A double-click's", self.TASKS)
        self.assertIn("    if (!btn || ev.detail > 1) return;\n"
                      "    const write = writeFor(btn);\n"
                      "    if (write) runWrite(write);", self.TASKS)


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
    SPELT = ("spellHidden(", "proseBlock(", "markdownBlock(", "authorCell(", "claimText(",
             "hiddenIn(")

    # Every module the page is made of, so a helper moving to another module does not
    # move out of these guards' sight.
    MODULES = _module_texts(TASKS_DIR)

    def test_the_guards_read_every_module_the_page_serves(self):
        self.assertEqual(set(self.MODULES), set(tui.UI_MODULES))

    # The one exemption, two lines of `drawMirror`, whose comment says why; held to
    # setting text by `test_the_mirror_alone_draws_agent_text_unspelled_and_only_as_text`.
    UNSPELLED = "drawMirror"
    UNSPELLED_LINES = ("if (!run.cls) return document.createTextNode(run.text);",
                       "span.textContent = run.text;")

    def test_every_agent_field_goes_through_the_spelling(self):
        code = "\n".join(_code(src) for src in self.MODULES.values())
        mirror = _code(_function(self.TASKS, self.UNSPELLED))
        self.assertEqual(code.count(mirror), 1)
        kept = "\n".join(ln for ln in mirror.splitlines()
                         if ln.strip() not in self.UNSPELLED_LINES)
        code = code.replace(mirror, kept)
        lines = [ln for ln in code.splitlines() if self.FIELD.search(ln)]
        self.assertGreater(len(lines), 10, "the field pattern found too little")
        for ln in lines:
            if re.match(r"\s*if \(", ln):
                continue        # a condition reads the field; it draws nothing
            with self.subTest(line=ln.strip()):
                self.assertTrue(any(s in ln for s in self.SPELT), ln)

    def test_the_mirror_alone_draws_agent_text_unspelled_and_only_as_text(self):
        mirror = _function(self.TASKS, self.UNSPELLED)
        code = _code(mirror)
        # Each run a text node or a span it names, its text set as text.
        self.assertEqual(re.findall(r"document\.createElement\(([^)]*)\)", code),
                         ['"span"'])
        self.assertEqual(re.findall(r"\brun\.text\b[^;]*;", code),
                         ["run.text);", "run.text;"])
        self.assertEqual([ln.strip() for ln in code.splitlines() if self.FIELD.search(ln)],
                         list(self.UNSPELLED_LINES))
        # It draws the textarea's value and nothing else: no read of a task, and no
        # write but these.
        self.assertNotRegex(code, r"\b(?:specEdit|shownTask|task|base|draft)\b")
        self.assertEqual(re.findall(r"^\s*((?:const |let )?[\w.]+) = ", code, re.M), [
            "const field", "const mirror", "const value", "const nodes", "const span",
            "span.className", "span.textContent"])
        self.assertIn("if (!run.cls) return document.createTextNode(run.text);", code)
        self.assertIn("span.textContent = run.text;", code)
        self.assertIn("span.className = `hl-${run.cls}`;", code)
        self.assertIn("const nodes = highlightRuns(value).map(run => {", code)
        self.assertIn("document.createTextNode(value + MIRROR_SENTINEL)", code)
        self.assertIn('const MIRROR_SENTINEL = "\\u200b";', self.TASKS)
        self.assertNotRegex(code, r"\.(?:innerHTML|outerHTML|insertAdjacentHTML|"
                                  r"setHTML\w*|srcdoc)\b|createContextualFragment")
        # Its text drawn over only once the mirror holds it: the class goes before the
        # highlighter runs and comes back after the mirror is filled.
        self.assertLess(code.index('field.classList.remove("mirrored");'),
                        code.index("highlightRuns(value)"))
        self.assertLess(code.index("mirror.replaceChildren(...nodes, "),
                        code.index('field.classList.add("mirrored");'))
        self.assertEqual(code.count('classList.add("mirrored")'), 1)
        # Hidden from assistive technology: the textarea is what it reads.
        self.assertIn('mirror.setAttribute("aria-hidden", "true");',
                      _function(self.TASKS, "bodyField"))
        # Drawn on every draw of the form, and on each input before anything else.
        self.assertIn("field.append(mirror, box);\n  drawMirror(box);",
                      _function(self.TASKS, "bodyField"))
        self.assertIn("if (ev.target.matches('textarea[data-spec=\"body\"]')) "
                      "drawMirror(ev.target);\n"
                      "      specEdit.draft = ", self.TASKS)

    def test_the_mirrors_runs_differ_by_colour_alone(self):
        # Weight, style or size would widen glyphs in the mirror and not in the
        # textarea, so lines would break apart: the browser tests see that only when a
        # line is near the edge.
        html = (TASKS_DIR / "index.html").read_text()
        rules = re.findall(r"^\s*\.hl-([a-z]+) \{([^}]*)\}", html, re.M)
        self.assertEqual(sorted(name for name, _ in rules), [
            "code", "em", "heading", "marker", "quote", "source", "strong"])
        for name, body in rules:
            with self.subTest(rule=name):
                self.assertRegex(body, r"^ color: [^;]+; $")
        self.assertEqual(len(re.findall(r"\.hl-", html)), len(rules))

    def test_the_body_is_the_forms_last_field_with_save_under_it(self):
        form = _function(self.TASKS, "specForm")
        order = ['line("title", specControl("title")), specPreviewSlot("title")',
                 'line("severity", specControl("severity"))',
                 'line("blocked by", specControl("blockers"))',
                 'line("body", bodyField())',
                 "row.append(save, discard);",
                 "saving.append(row, count);",
                 'form.append(saving, specPreviewSlot("body"));']
        at = [form.index(s) for s in order]
        self.assertEqual(at, sorted(at))
        self.assertEqual(form.count("specPreviewSlot("), 2)
        self.assertIn('count.className = "held-line warn";', form)
        self.assertIn("if (count) count.textContent = storedCount(base, draft);",
                      _function(self.TASKS, "markEdit"))

    def test_the_plans_are_drawn_against_what_they_are(self):
        # The task's own plan is read against its state and drawn as what changed from
        # the one before; an earlier plan against none, since a newer one is the
        # task's, and whole; the shown one stays out of the earlier list.
        self.assertIn("planBlock(task.plan, task,\n"
                      "                                     planPredecessor(held.plans, "
                      "task.plan.id))", self.TASKS)
        self.assertIn("planBlock(plan, null)", self.TASKS)
        self.assertIn("planNotice(null, task)", self.TASKS)
        self.assertIn("const notice = planNotice(plan, task);",
                      _function(self.TASKS, "planBlock"))
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

    def test_markdown_is_drawn_as_spelled_text_beside_its_source(self):
        block = _function(self.TASKS, "markdownBlock")
        # The source form and the note counting hidden characters are proseBlock's.
        self.assertIn("const wrap = proseBlock(text);", block)
        self.assertIn('for (const block of tokenize(typeof text === "string" ? text : ""))',
                      block)
        self.assertIn("typed.textContent = spellHidden(block.tokens.map(t => t.text)"
                      '.join("")).text;', block)
        self.assertIn("wrap.insertBefore(rendered, source);", block)
        runs = _function(self.TASKS, "markdownRuns")
        self.assertIn("top.appendChild(document.createTextNode("
                      "spellHidden(token.text).text));", runs)
        self.assertIn("info.textContent = spellHidden(token.text).text;", runs)
        # Every element either builds is one it names, never one from the text.
        made = re.findall(r"document\.createElement\(([^)]*)\)", block + runs)
        self.assertEqual(sorted(set(made)), sorted({
            "tag", '"li"', 'inline ? "span" : "div"', '"div"',
            "OPENS[token.kind]", '"span"'}))
        self.assertIn('const OPENS = { "em-open": "em", "strong-open": "strong", '
                      '"code-open": "code" };', self.TASKS)
        self.assertIn('const BLOCK_TAGS = { paragraph: "p", code: "pre", blank: "div", '
                      'source: "div" };', self.TASKS)
        self.assertIn('const INLINE_TAGS = { paragraph: "span", blank: "span" };',
                      self.TASKS)
        self.assertIn("const tag = block.kind === \"heading\" && !inline "
                      "? `h${block.level}` : tags[block.kind];", block)
        # It drops only the ASCII delimiters the plan lists.
        dropped = re.search(r"const DROPPED = new Set\(\[(.*?)\]\);", self.TASKS, re.S)
        self.assertEqual(sorted(re.findall(r'"([^"]+)"', dropped.group(1))), sorted([
            "heading-marker", "bullet", "em-open", "em-close", "strong-open",
            "strong-close", "code-open", "code-close", "fence"]))

    def test_the_detail_draws_its_agent_prose_as_markdown(self):
        self.assertIn("const body = markdownBlock(task.body);",
                      _function(self.TASKS, "drawTask"))
        self.assertIn('part.key === "footprint" ? proseBlock(part.text)\n'
                      "      : markdownBlock(part.text)", _function(self.TASKS, "planBlock"))
        self.assertIn("td.appendChild(markdownBlock(entry.note.text));",
                      _function(self.TASKS, "renderTimeline"))

    def test_what_is_kept_as_drawn_before_has_no_markdown(self):
        # Outside the detail, or not an agent's prose: the flagged list, a plan's diff,
        # the operator's "other" text, the spec form and its preview.
        for name in ("flaggedRow", "diffLine", "markOther", "specForm", "specControl"):
            with self.subTest(function=name):
                self.assertNotIn("markdownBlock(", _function(self.TASKS, name))
        draw = _function(self.TASKS, "drawTask")
        self.assertIn('line("progress", proseBlock(task.claim.progress))', draw)
        self.assertIn("line(\"other\", proseBlock(answer.other))",
                      _function(self.TASKS, "answerBlock"))
        self.assertIn('typeof value === "string" ? proseBlock(value)',
                      _function(self.TASKS, "renderTimeline"))
        self.assertEqual(self.TASKS.count("markdownBlock("), 10)

    def test_one_switch_shows_the_source_and_redraws_nothing(self):
        toggle = _function(self.TASKS, "toggleSource")
        self.assertIn('taskDetail.classList.toggle("source-shown", showSource);', toggle)
        self.assertNotIn("drawTask(", toggle)
        # In the title's row, before the edit's control, which comes and goes.
        draw = _function(self.TASKS, "drawTask")
        self.assertLess(draw.index('head.append(" ", sourceToggle());'),
                        draw.index("if (!editing) {\n    const edit"))
        html = (TASKS_DIR / "index.html").read_text()
        self.assertIn("#task-detail:not(.source-shown) .md-source,\n"
                      "  #task-detail.source-shown .md { display: none; }", html)
        self.assertRegex(html, r"button\.source-toggle \{ width: [0-9.]+em; "
                               r"white-space: nowrap;")


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

# The approve the page builds with an "other" text on the second decision, which it
# checks only for being blank, and how it numbers that decision when one is missing.
_REFUSAL_PROBE = r"""
import { pathToFileURL } from "node:url";
const t = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/tasks.js`).href);
const m = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/moves.js`).href);
const decisions = JSON.parse(process.env.DOCKADE_DECISIONS);
const assumed = m.assumedChoices(decisions);
console.log(JSON.stringify({
  answers: m.approveAnswers(decisions, [assumed[0], {...assumed[1], other: true,
                                                     text: process.env.DOCKADE_TEXT}]),
  incomplete: t.incompleteText([1]),
}));
"""

# How the page draws the operator API's answer to that approve.
_REFUSED_PROBE = r"""
import { pathToFileURL } from "node:url";
const t = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/tasks.js`).href);
const m = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/moves.js`).href);
const body = JSON.parse(process.env.DOCKADE_BODY);
const outcome = m.writeOutcome({status: Number(process.env.DOCKADE_STATUS), body});
console.log(JSON.stringify({outcome, text: t.outcomeText(
  {kind: "approve", verb: "approve", predicted: "proposed"}, outcome, body.detail).text}));
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

    def test_a_refusal_names_the_decision_the_page_numbers(self):
        long = "x" * (_plans.OTHER_MAX + 1)
        page = self.node(_REFUSAL_PROBE, DOCKADE_DECISIONS=json.dumps(self.DECISIONS),
                         DOCKADE_TEXT=long)
        task = self.refined()
        req = _api.Approve(revision=_reads.get_task(task)["revision"],
                           answers=tuple(_api.Answer(**a) for a in page["answers"]))
        reply = _api.approve(task, req, ThroughTheRelay())
        self.assertEqual(page["answers"][1]["other"], long)
        drawn = self.node(_REFUSED_PROBE, DOCKADE_STATUS=str(reply.status_code),
                          DOCKADE_BODY=json.dumps(reply.body))
        self.assertEqual(drawn["outcome"], "refused")
        counted = re.fullmatch(r".*: decision (\d+) not yet\..*", page["incomplete"]).group(1)
        self.assertIn(f"Approve: refused. decision {counted}'s other text is 1 to",
                      drawn["text"])
        self.assertNotIn("answers[", drawn["text"])
        self.assertEqual(_reads.get_task(task)["state"], "refined")

    @staticmethod
    def node(script: str, **env: str) -> dict:
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", script],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR)), **env},
            capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing the page under node failed:\n" + proc.stderr)
        return json.loads(proc.stdout)


class TaskDecisionSourceTests(unittest.TestCase):
    """How the page draws a plan's decisions and builds the approve that answers them,
    from source: a browser is the only thing that runs it. The rule the approve follows
    is held under node (``MoveTests``, ``ApproveRuleTests``)."""

    TASKS = (TASKS_DIR / "tasks.js").read_text()
    HTML = (TASKS_DIR / "index.html").read_text()

    def test_a_decision_and_an_answer_are_spelled_and_drawn_whole(self):
        group = _function(self.TASKS, "decisionGroup")
        # Drawn as markdown, a label inline only, beside its source form
        # (`markdownBlock`); the operator's own "other" text as written.
        for drawn in ("markdownBlock(decision.question)", "markdownBlock(decision.why)",
                      "markdownBlock(option.label, { inline: true })",
                      "markdownBlock(option.meaning)"):
            with self.subTest(field=drawn):
                self.assertIn(drawn, group)
        answer = _function(self.TASKS, "answerBlock")
        for drawn in ("markdownBlock(answer.question)",
                      "markdownBlock(option.label, { inline: true })",
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
        self.assertIn("if (task === null) {\n"
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
        self.assertIn("const predicted = approveOutcome(decisions, picked);", write)
        # The outcome names the approve by what its button said.
        self.assertIn("predicted, verb: approveLabel(shownTask, predicted).main };", write)

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
        self.assertIn("setLabel(backBtn, moveLabel(back, task, picked));", mark)
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


_EDIT_PROBE = r"""
import { pathToFileURL } from "node:url";
const t = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/tasks.js`).href);
const m = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/moves.js`).href);
// A task as a read carries it: a title with a line break, a body with CR LF and a lone
// CR, as a write straight to the registry could store them, and two blockers.
const base = {id: 7, revision: 4, state: "open", title: "Keys\nrotated",
              body: "One.\r\nTwo.\rThree.", severity: "low", kind: null, claim: null,
              blockers: [{id: 2, title: "B", state: "open"}, {id: 5, title: "C",
                                                              state: "closed"}]};
const draft = m.specDraft(base);
const req = changes => m.editRequest(base, {...draft, ...changes});
const sel = m.SPEC_CONTROLS.severity;
// What an edit puts on the wire, with fetch stubbed.
const sent = [];
globalThis.fetch = (url, opts) => {
  sent.push({url, method: opts.method, body: JSON.parse(opts.body)});
  return Promise.resolve({status: 200, text: async () => '{"ok": true}'});
};
const send = {revision: 4, title: "New", clear: ["severity"], blocked_by: [9]};
await m.sendWrite({kind: "edit", task: "7", revision: 4, send, verb: "save the edit"});
const edit = {kind: "edit", task: "7", revision: 4, send, verb: "save the edit",
              lastNote: 0};
const landed = {...base, revision: 5, title: "New", severity: null, blockers: [{id: 9}]};
// Another writer changed the title; the operator only the body.
const theirs = {...base, revision: 6, title: "Theirs"};
const mine = {...draft, body: "Mine."};
const rebased = m.rebaseDraft(base, mine, theirs);
const refined = {...base, state: "refined", plan: {id: 3, task_revision: 2,
                                                   superseded_at: null}};
console.log(JSON.stringify({
  severities: m.SEVERITIES, kinds: m.KINDS, fields: m.SPEC_FIELDS,
  controls: m.SPEC_CONTROLS,
  as_control: {
    textarea: m.asControl("a\r\nb\rc\n\r\n", "textarea"),
    text: m.asControl("a\r\nb\nc\rd", "text"),
    select_known: m.asControl("low", sel),
    select_unknown: m.asControl("critical", sel),
    select_none: m.asControl(null, sel),
    not_text: m.asControl(undefined, "textarea"),
  },
  control_changes: [m.controlChanges("a\r\nb\rc\n", "textarea"),
                    m.controlChanges("a\r\nb\nc", "text"),
                    m.controlChanges("critical", sel), m.controlChanges(null, "text")],
  draft,
  requests: {
    none: req({}),
    title: req({title: "Keys, rotated"}),
    body: req({body: "One.\nTwo.\nThree, four."}),
    severity: req({severity: "high"}),
    severity_none: req({severity: ""}),
    kind: req({kind: "finding"}),
    kind_none_as_it_was: req({kind: ""}),
    blockers_reordered: req({blockers: "5, #2, 2"}),
    blockers_removed: req({blockers: ""}),
    blockers_added: req({blockers: "#2 #5, 9, 9"}),
    blockers_bad: req({blockers: "#2, two"}),
    blockers_bad_and_title: req({title: "Keys", blockers: "#2, two"}),
    all: req({title: "T", body: "B", severity: "", kind: "question", blockers: "#9"}),
    // A stored kind no select offers reads as none, so it is not cleared unasked.
    unknown_kind: m.editRequest({...base, kind: "chore"},
                                {...m.specDraft({...base, kind: "chore"}), title: "T"}),
  },
  draft_changes: [m.draftChanges(base, draft), m.draftChanges(base, {...draft, title: "T",
                                                                     blockers: "#2, x"})],
  parse: ["", "#7, 12", " 7 ,, #12\n", "#7 #7", "#0", "-3", "7.5", "#", "x7",
          "9007199254740993", "#9007199254740991"].map(m.parseBlockers),
  blockers_text: [m.blockersText([7, 12]), m.blockersText([]), m.blockerIds(null)],
  changed_fields: {
    title: m.changedFields(base, theirs),
    blockers_reordered: m.changedFields(base, {...base, blockers: [{id: 5}, {id: 2}]}),
    severity_cleared: m.changedFields(base, {...base, severity: null, kind: undefined}),
    body: m.changedFields(base, {...base, body: "One.\nTwo.\nThree."}),
    blockers: m.changedFields(base, {...base, blockers: []}),
  },
  rebase: {
    draft: rebased, request: m.editRequest(theirs, rebased),
    // The other writer made the operator's own change: nothing is left to send.
    emptied: m.editRequest({...base, revision: 6, body: "Mine."},
                           m.rebaseDraft(base, mine, {...base, revision: 6, body: "Mine."})),
    // Both changed the title: the operator's stays, and saving writes over theirs.
    both: m.rebaseDraft(base, {...draft, title: "Ours"}, theirs).title,
    // Blockers that do not parse are the operator's change, and stay.
    bad_blockers: m.rebaseDraft(base, {...draft, blockers: "#2, x"},
                                {...theirs, blockers: []}).blockers,
  },
  wire: sent[0],
  check: {
    found: m.checkWrite(edit, {task: landed}),
    not_landed: m.checkWrite(edit, {task: {...landed, revision: 4}}),
    other_title: m.checkWrite(edit, {task: {...landed, title: "Newer"}}),
    not_cleared: m.checkWrite(edit, {task: {...landed, severity: "low"}}),
    other_blockers: m.checkWrite(edit, {task: {...landed, blockers: [{id: 9}, {id: 2}]}}),
    failed_read: m.checkWrite(edit, null),
    body_left_alone: m.checkWrite({...edit, send: {revision: 4, title: "New"}},
                                  {task: {...landed, body: "Changed elsewhere."}}),
  },
  texts: {
    found: t.outcomeText(edit, "found").text,
    landed: t.outcomeText(edit, "landed").text,
    not_landed: t.outcomeText(edit, "not-landed").text,
    changed: t.outcomeText(edit, "changed").text,
  },
  labels: {
    save: t.saveLabel(base),
    save_claimed: t.saveLabel({...base, claim: {holder: "claude-x\u202e"}}),
    save_none: t.saveLabel(null),
    amend: t.specAmendText(refined),
    amend_approved: t.specAmendText({...refined, plan: {...refined.plan,
                                                        approved_at: 1789466400}}),
    amend_answered: t.specAmendText({...refined, answers: [{plan: refined.plan.id}]}),
    amend_open: t.specAmendText({...refined, state: "open"}),
    amend_superseded: t.specAmendText({...refined, plan: {...refined.plan,
                                                          superseded_at: 1}}),
    problem: t.blockersProblem("x\u202e"),
    unknown: [t.unknownSpecText({...base, severity: "critical", kind: "chore\u202e"}),
              t.unknownSpecText(base), t.unknownSpecText(null)],
    held: t.HELD_TEXT,
  },
  preview: {
    untouched: t.specPreview(base, draft, "body"),
    sent: t.specPreview(base, {...draft, body: "One.\nTwo.\nThree.\u200b"}, "body"),
    title: t.specPreview(base, {...draft, title: "Keys, rotated"}, "title"),
    one: t.specPreview({...base, body: "a\r\nb"}, {...draft, body: "x"}, "body"),
  },
  count: {
    untouched: t.storedCount(base, draft),
    sent: t.storedCount(base, {...draft, body: "One.\nTwo.\nThree.\u200b"}),
    one: t.storedCount({...base, body: "a\r\nb"}, {...draft, body: "x"}),
    clean: t.storedCount({...base, body: "Plain."}, {...draft, body: "Plain."}),
    // The title's preview sits above save, under its own control: not counted here.
    title: t.storedCount({...base, body: "Plain."},
                         {...draft, body: "Plain.", title: "Keys\u200b"}),
  },
  rebase_text: {
    taken: t.rebaseText(4, 6, ["title"], [], false),
    kept: t.rebaseText(4, 6, ["title", "body"], ["body"], false),
    kept_two: t.rebaseText(4, 6, ["title", "body"], ["title", "body"], false),
    state_only: t.rebaseText(4, 5, [], [], false),
    emptied: t.rebaseText(4, 6, ["body"], ["body"], true),
  },
}));
"""


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class EditTests(unittest.TestCase):
    """The spec edit's pure half (``moves.js``) and what ``tasks.js`` says around it:
    a field compared as its control hands it back, only what changed sent, a draft moved
    onto a newer read without reverting another writer, and a lost edit read back."""

    probe: dict

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", _EDIT_PROBE],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR))},
            capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing moves.js under node failed:\n" + proc.stderr)
        cls.probe = json.loads(proc.stdout)

    def test_the_severities_and_kinds_are_the_registrys(self):
        source = (ROOT / "task-registry" / "tasks.py").read_text()
        for name, key in (("SEVERITIES", "severities"), ("KINDS", "kinds")):
            with self.subTest(name=name):
                values = re.search(rf"^{name} = \(([^)]*)\)", source, re.M).group(1)
                self.assertEqual(self.probe[key], re.findall(r'"([^"]+)"', values))
        controls = self.probe["controls"]
        self.assertEqual(controls["severity"], ["", *self.probe["severities"]])
        self.assertEqual(controls["kind"], ["", *self.probe["kinds"]])
        # The fields are the ones `edit` takes, blockers as `blocked_by`.
        fields = {"blocked_by" if f == "blockers" else f for f in self.probe["fields"]}
        self.assertEqual(fields | {"revision", "clear"},
                         set(_api.EditTask.__annotations__))

    def test_a_value_is_compared_as_its_control_hands_it_back(self):
        got = self.probe["as_control"]
        self.assertEqual(got["textarea"], "a\nb\nc\n\n")
        self.assertEqual(got["text"], "abcd")
        self.assertEqual((got["select_known"], got["select_unknown"], got["select_none"]),
                         ("low", "", ""))
        self.assertEqual(got["not_text"], "")
        self.assertEqual(self.probe["control_changes"], [2, 3, 0, 0])
        self.assertEqual(self.probe["draft"], {
            "title": "Keysrotated", "body": "One.\nTwo.\nThree.", "severity": "low",
            "kind": "", "blockers": "#2, #5"})

    def test_an_edit_sends_only_what_changed(self):
        r = self.probe["requests"]
        self.assertIsNone(r["none"])
        self.assertEqual(r["title"], {"revision": 4, "title": "Keys, rotated"})
        self.assertEqual(r["body"], {"revision": 4, "body": "One.\nTwo.\nThree, four."})
        self.assertEqual(r["severity"], {"revision": 4, "severity": "high"})
        self.assertEqual(r["severity_none"], {"revision": 4, "clear": ["severity"]})
        self.assertEqual(r["kind"], {"revision": 4, "kind": "finding"})
        self.assertIsNone(r["kind_none_as_it_was"])
        self.assertEqual(r["all"], {"revision": 4, "title": "T", "body": "B",
                                    "kind": "question", "blocked_by": [9],
                                    "clear": ["severity"]})
        # A stored value no control offers is not rewritten by an edit of another field.
        self.assertEqual(r["unknown_kind"], {"revision": 4, "title": "T"})

    def test_an_untouched_crlf_body_is_not_sent_and_an_edited_one_goes_as_lf(self):
        r = self.probe["requests"]
        self.assertNotIn("body", r["title"])
        self.assertEqual(r["body"]["body"], "One.\nTwo.\nThree, four.")
        self.assertNotIn("\r", json.dumps(r))

    def test_blockers_go_whole_and_only_when_the_set_differs(self):
        r = self.probe["requests"]
        self.assertIsNone(r["blockers_reordered"])
        self.assertEqual(r["blockers_removed"], {"revision": 4, "blocked_by": []})
        self.assertEqual(r["blockers_added"], {"revision": 4, "blocked_by": [2, 5, 9]})
        # Blockers that do not parse send nothing, whatever else changed.
        self.assertIsNone(r["blockers_bad"])
        self.assertIsNone(r["blockers_bad_and_title"])
        self.assertEqual(self.probe["draft_changes"], [[], ["title", "blockers"]])

    def test_blockers_parse_to_ids_or_name_the_word_that_is_not_one(self):
        parsed = self.probe["parse"]
        self.assertEqual([p["ids"] for p in parsed[:4]], [[], [7, 12], [7, 12], [7, 7]])
        self.assertTrue(all(p["bad"] is None for p in parsed[:4]))
        self.assertEqual([p["bad"] for p in parsed[4:10]],
                         ["#0", "-3", "7.5", "#", "x7", "9007199254740993"])
        self.assertTrue(all(p["ids"] is None for p in parsed[4:10]))
        self.assertEqual(parsed[10], {"ids": [9007199254740991], "bad": None})
        self.assertEqual(self.probe["blockers_text"], ["#7, #12", "", []])
        self.assertEqual(self.probe["labels"]["problem"],
                         '"x\\u202e" is not a task id: blockers are ids, such as #7, 12.')

    def test_the_fields_that_moved_under_a_draft_are_named(self):
        c = self.probe["changed_fields"]
        self.assertEqual(c["title"], ["title"])
        self.assertEqual(c["blockers_reordered"], [])
        self.assertEqual(c["severity_cleared"], ["severity"])
        self.assertEqual(c["body"], ["body"])
        self.assertEqual(c["blockers"], ["blockers"])

    def test_a_rebase_keeps_the_operators_fields_and_takes_the_rest(self):
        rebase = self.probe["rebase"]
        self.assertEqual(rebase["draft"]["title"], "Theirs")
        self.assertEqual(rebase["draft"]["body"], "Mine.")
        # The resend carries the body alone, at the newer revision: another writer's
        # title is not reverted.
        self.assertEqual(rebase["request"], {"revision": 6, "body": "Mine."})
        self.assertIsNone(rebase["emptied"])
        self.assertEqual(rebase["both"], "Ours")
        self.assertEqual(rebase["bad_blockers"], "#2, x")

    def test_an_edit_is_posted_as_the_route_takes_it(self):
        self.assertEqual(self.probe["wire"], {
            "url": "/api/tasks/7/edit", "method": "POST",
            "body": {"revision": 4, "title": "New", "clear": ["severity"],
                     "blocked_by": [9]}})

    def test_a_lost_edit_is_decided_by_the_revision_and_every_field_it_sent(self):
        c = self.probe["check"]
        self.assertEqual(c["found"], "found")
        self.assertEqual(c["not_landed"], "not-landed")
        for key in ("other_title", "not_cleared", "other_blockers"):
            with self.subTest(case=key):
                self.assertEqual(c[key], "changed")
        self.assertEqual(c["failed_read"], "could-not-check")
        # A field it did not send may have moved since: that does not count against it.
        self.assertEqual(c["body_left_alone"], "found")
        texts = self.probe["texts"]
        self.assertEqual(texts["landed"], "Save the edit: done.")
        self.assertIn("the task shows it landed", texts["found"])
        self.assertIn("cannot apply it twice", texts["not_landed"])
        self.assertIn("not as this would have changed it", texts["changed"])

    def test_the_save_names_the_revision_and_a_claim_it_leaves(self):
        labels = self.probe["labels"]
        # The holder is named once, above the moves (`holderText`), not here.
        self.assertEqual(labels["save"], {"main": "save the edit", "second": "revision 4",
                                          "caution": False})
        self.assertEqual(labels["save_claimed"], {
            "main": "save the edit", "second": "revision 4 · the claim stays",
            "caution": False})
        self.assertIsNone(labels["save_none"])

    def test_a_refined_tasks_form_says_the_edit_amends_its_plans_spec(self):
        labels = self.probe["labels"]
        self.assertEqual(labels["amend"],
                         "This task is refined: an edit amends the spec plan 3 was "
                         "written against, at revision 2. Approving after it approves "
                         "the edited task with that plan.")
        for key in ("amend_approved", "amend_answered"):
            with self.subTest(case=key):
                self.assertEqual(labels[key],
                                 "This task is refined: an edit amends the spec plan 3 was "
                                 "written against, at revision 2. Plan 3 was approved "
                                 "before: a reopen after the edit opens the edited task "
                                 "with that plan.")
                self.assertNotIn("Approving", labels[key])
        self.assertEqual((labels["amend_open"], labels["amend_superseded"]), ("", ""))
        self.assertIn("Save or discard the edit first", labels["held"])

    def test_a_stored_value_no_select_offers_is_said_beside_it(self):
        unknown = self.probe["labels"]["unknown"]
        self.assertEqual(unknown[0],
                         'The stored severity "critical" is not one the form offers: it '
                         'shows as none, and stays unless you change it. The stored kind '
                         '"chore\\u202e" is not one the form offers: it shows as none, '
                         "and stays unless you change it.")
        self.assertEqual(unknown[1:], ["", ""])
        self.assertIn("unknownSpecText(specEdit.base)",
                      _function((TASKS_DIR / "tasks.js").read_text(), "specForm"))

    def test_the_preview_shows_what_will_be_stored_spelled(self):
        p = self.probe["preview"]
        # Left alone, a field is stored as it is: its CR spelled, as any hidden one.
        self.assertEqual(p["untouched"], {
            "label": "stored, and left as it is",
            "text": "One.\\u000d\nTwo.\\u000dThree.", "hidden": 2, "lost": ""})
        self.assertEqual(p["sent"], {
            "label": "will be stored", "text": "One.\nTwo.\nThree.\\u200b", "hidden": 1,
            "lost": "2 line breaks of the stored body turned into line feeds by this "
                    "control."})
        self.assertEqual(p["title"]["lost"],
                         "1 line break of the stored title dropped by this control.")
        self.assertEqual(p["one"]["lost"], "1 line break of the stored body turned into "
                                           "line feeds by this control.")

    def test_the_line_beside_save_counts_what_the_bodys_preview_shows(self):
        # Save sits above the body's preview: this line says when there is something
        # in it to read, and is empty when there is not.
        self.assertEqual(self.probe["count"], {
            "untouched": "The body's preview below shows 2 hidden characters.",
            "sent": "The body's preview below shows 1 hidden character and 2 line breaks "
                    "turned into line feeds.",
            "one": "The body's preview below shows 1 line break turned into line feeds.",
            "clean": "", "title": ""})

    def test_a_rebase_says_what_moved_and_what_saving_writes_over(self):
        t = self.probe["rebase_text"]
        self.assertEqual(t["taken"], "The task moved from revision 4 to 6 while you "
                                     "edited it. Your edit takes the new title.")
        self.assertEqual(t["kept"], "The task moved from revision 4 to 6 while you edited "
                                    "it. Your edit takes the new title. It keeps your "
                                    "body over the new one: saving writes over that "
                                    "change.")
        self.assertIn("It keeps your title, body over the new ones: saving writes over "
                      "those changes.", t["kept_two"])
        self.assertEqual(t["state_only"], "The task moved from revision 4 to 5 while you "
                                          "edited it. Its spec did not change.")
        self.assertTrue(t["emptied"].endswith(
            " Nothing is left to save: the task reads as your edit did."))
        self.assertNotIn("Nothing is left", t["kept"])


# What the page sends for each change a draft makes to a task a scratch store holds,
# and what it makes of the task read after.
_EDIT_RULE_PROBE = r"""
import { pathToFileURL } from "node:url";
const m = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/moves.js`).href);
const cases = JSON.parse(process.env.DOCKADE_CASES);
const after = process.env.DOCKADE_AFTER ? JSON.parse(process.env.DOCKADE_AFTER) : null;
const sent = cases.map(([base, changes]) =>
  m.editRequest(base, {...m.specDraft(base), ...changes}));
console.log(JSON.stringify({sent, checks: after && after.map((task, i) => m.checkWrite(
  {kind: "edit", revision: cases[i][0].revision, send: sent[i]}, {task}))}));
"""


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class EditRuleTests(_OperatorStore):
    """The edit the page builds against the registry's own: each body taken by the
    operator API's model and by ``edit`` on a scratch store, which then reads back as
    the page sent it, a field left alone as it was, and as landed by the page's check."""

    CHANGES: ClassVar[list] = [
        {"title": "Keys, rotated"},
        {"body": "One.\nTwo.\nThree, four."},
        {"severity": "", "kind": "finding"},
        {"blockers": ""},
        {"blockers": "#{other}, {other}"},
        {"title": "All", "severity": "high", "kind": "", "blockers": "#{other}"},
    ]

    def node(self, **env: str) -> dict:
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", _EDIT_RULE_PROBE],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR)), **env},
            capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing moves.js under node failed:\n" + proc.stderr)
        return json.loads(proc.stdout)

    def test_each_edit_lands_as_sent_and_leaves_the_rest(self):
        op = _reg.operator_moves
        other = op.create("widgets", "Other", "B.", "open")["task"]
        made = [op.create("widgets", "Keys\nrotated", "One.\r\nTwo.\rThree.", "open",
                          severity="low", kind="question", blocked_by=[self.task])["task"]
                for _ in self.CHANGES]
        before = [_reads.get_task(task) for task in made]
        changes = [{k: v.format(other=other) for k, v in c.items()} for c in self.CHANGES]
        cases = list(zip(before, changes, strict=True))
        sent = self.node(DOCKADE_CASES=json.dumps(cases))["sent"]
        for task, body, was in zip(made, sent, before, strict=True):
            with self.subTest(sent=body):
                self.assertIsNotNone(body)
                reply = _api.edit_task(task, _api.EditTask(**body), ThroughTheRelay())
                self.assertEqual(reply.status_code, 200, reply.body)
                now = _reads.get_task(task)
                for field in ("title", "body", "severity", "kind"):
                    expected = (None if field in body.get("clear", ())
                                else body.get(field, was[field]))
                    self.assertEqual(now[field], expected, field)
                blockers = body.get("blocked_by", [b["id"] for b in was["blockers"]])
                self.assertEqual(sorted(b["id"] for b in now["blockers"]),
                                 sorted(set(blockers)))
        # The CR LF body is sent only by the change that edits it, as LF.
        self.assertEqual([("body" in b) for b in sent],
                         [False, True, False, False, False, False])
        after = [_reads.get_task(task) for task in made]
        checks = self.node(DOCKADE_CASES=json.dumps(cases),
                           DOCKADE_AFTER=json.dumps(after))["checks"]
        self.assertEqual(checks, ["found"] * len(made))
        # Read at the revision it was sent at, it has not landed yet.
        stale = self.node(DOCKADE_CASES=json.dumps(cases),
                          DOCKADE_AFTER=json.dumps(before))["checks"]
        self.assertEqual(stale, ["not-landed"] * len(made))


class TaskEditSourceTests(unittest.TestCase):
    """How the page holds, draws and sends the spec edit, from source: a browser is the
    only thing that runs it. What it sends is held under node (``EditTests``,
    ``EditRuleTests``)."""

    TASKS = (TASKS_DIR / "tasks.js").read_text()
    HTML = (TASKS_DIR / "index.html").read_text()

    def test_the_form_takes_its_values_as_properties(self):
        control = _function(self.TASKS, "specControl")
        self.assertIn("el.value = specEdit.draft[field];", control)
        self.assertIn('opt.textContent = value || "none";', control)
        for name in ("specControl", "specForm", "markEdit"):
            with self.subTest(function=name):
                body = _function(self.TASKS, name)
                self.assertNotRegex(body, r'setAttribute\("(?:value|placeholder)"')
                self.assertNotIn("innerHTML", body)
        # The operator's text is read back from the control, never from markup.
        self.assertIn("[ev.target.dataset.spec]: ev.target.value", self.TASKS)

    def test_the_preview_is_spelled_and_set_as_text(self):
        self.assertIn("return { sent, shown: spellHidden(sent ? draft[field] : stored),",
                      _function(self.TASKS, "storedAs"))
        for name in ("specPreview", "storedCount"):
            with self.subTest(function=name):
                self.assertIn("= storedAs(base, draft, ", _function(self.TASKS, name))
        mark = _function(self.TASKS, "markEdit")
        self.assertIn("text.textContent = preview.text;", mark)
        self.assertIn("hiddenNote(preview.hidden)", mark)
        self.assertIn("blockersProblem(bad)", mark)
        self.assertIn("spellHidden(String(bad)).text", _function(self.TASKS, "blockersProblem"))

    def test_the_edit_is_in_the_in_flight_gate(self):
        mark = _function(self.TASKS, "markWriting")
        for control in ("[data-spec]", "button[data-spec-open]", "button[data-spec-discard]"):
            with self.subTest(control=control):
                self.assertIn(control, mark)
        self.assertLess(mark.index("el.disabled = writing !== null || !drawnIsOpen();"),
                        mark.index("markEdit();"))
        self.assertEqual(_function(self.TASKS, "toggleSpecEdit").splitlines()[1].strip(),
                         "if (writing || !drawnIsOpen()) return;")
        self.assertIn('if (btn.dataset.write === "edit") return editWrite(base);',
                      _function(self.TASKS, "writeFor"))
        # A save with nothing to send is off, after the gate turned it on.
        self.assertIn("if (save && !editRequest(base, draft)) save.disabled = true;",
                      _function(self.TASKS, "markEdit"))

    def test_a_draft_with_changes_holds_every_move_but_a_note(self):
        self.assertIn('const HELD_KINDS = ["move", "approve", "break"];', self.TASKS)
        write_for = _function(self.TASKS, "writeFor")
        self.assertIn("if (HELD_KINDS.includes(btn.dataset.write) && editHolds()) "
                      "return null;", write_for)
        self.assertLess(write_for.index("editHolds()"), write_for.index("approveWrite("))
        mark = _function(self.TASKS, "markEdit")
        self.assertIn("for (const btn of taskDetail.querySelectorAll(HELD_MOVES)) {\n"
                      "    if (held) btn.disabled = true;", mark)
        self.assertIn("said.textContent = held ? HELD_TEXT : \"\";", mark)
        self.assertIn("draftChanges(specEdit.base, specEdit.draft).length > 0",
                      _function(self.TASKS, "editHolds"))
        # Its line keeps its space above the moves, so saying it moves no button.
        moves = _function(self.TASKS, "movesBlock")
        self.assertLess(moves.index('held.dataset.specHeld = "1";'),
                        moves.index('row.className = "moves";'))
        rule = re.search(r"\.held-line \{([^}]*)\}", self.HTML).group(1)
        self.assertRegex(rule, r"\bheight: \d+px;")

    def test_only_a_landed_edit_discard_or_another_task_drops_the_draft(self):
        # Declared, rebased, opened or discarded, and dropped by a landed edit, opening
        # another task, going back to the list and a leave confirmed: nothing else
        # writes it.
        sets = re.findall(r"\bspecEdit = ([^;]*);", self.TASKS)
        self.assertEqual(len(sets), 7, sets)
        self.assertEqual(sets.count("null"), 5, sets)
        run = _function(self.TASKS, "runWrite")
        self.assertIn('if (write.kind === "edit" && landed && specEdit && '
                      "specEdit.task === write.task) {\n    specEdit = null;", run)
        self.assertIn('const landed = result === "landed" || result === "found";', run)
        self.assertIn("if (specEdit && specEdit.task !== id) specEdit = null;",
                      _function(self.TASKS, "showTask"))
        self.assertIn("specEdit = null;", _function(self.TASKS, "closeTask"))
        self.assertIn("specEdit = open ? {", _function(self.TASKS, "toggleSpecEdit"))
        for name in ("refreshOpenTask", "refreshInbox", "refreshTasks", "readFlagged"):
            with self.subTest(function=name):
                self.assertNotIn("specEdit", _function(self.TASKS, name))

    def test_every_draw_past_the_drafts_base_rebases_it(self):
        draw = _function(self.TASKS, "drawTask")
        self.assertLess(draw.index("noteSpecDrawn(task);"), draw.index("specForm(task)"))
        noted = _function(self.TASKS, "noteSpecDrawn")
        self.assertIn("!(Number(task.revision) > Number(specEdit.base.revision))", noted)
        self.assertIn("const rebased = rebaseDraft(base, draft, task);", noted)
        self.assertIn("specEdit = { task: specEdit.task, base: task, draft: rebased,", noted)
        # "Nothing left to save" only when the draft had changes before.
        self.assertIn("mine.length > 0 && !draftChanges(task, rebased).length", noted)

    def test_a_redraw_of_the_read_drawn_keeps_the_line_atop_it(self):
        draw = _function(self.TASKS, "drawTask")
        self.assertIn("const kept = task === shownTask ? noticeSlot : null;", draw)
        self.assertLess(draw.index("const kept ="), draw.index("shownTask = task;"))
        self.assertIn("if (!kept) {\n    noticeSlot = document.createElement(\"div\");", draw)
        # A refusal's notice is put back only in a line made new: a kept one may say
        # something newer since, a poll's "changed".
        self.assertIn('if (!kept && outcome && outcome.task === String(task.id) '
                      '&& outcome.result === "stale") {\n    fillSlot(outcome);', draw)
        self.assertIn("drawTask(shownTask, shownHeld);",
                      _function(self.TASKS, "toggleSpecEdit"))

    def test_the_form_replaces_the_lines_that_show_the_spec(self):
        draw = _function(self.TASKS, "drawTask")
        self.assertIn("if (sk && !editing)", draw)
        self.assertIn("task.blockers.length && !editing", draw)
        self.assertLess(draw.index("taskDetail.appendChild(specForm(task));"),
                        draw.index("taskDetail.appendChild(body);"))
        self.assertIn("specAmendText(task)", _function(self.TASKS, "specForm"))


# The create and the reparent: what each sends, what a lost one's read back decides, and
# what the page says around them.
_CREATE_PROBE = r"""
import { pathToFileURL } from "node:url";
const t = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/tasks.js`).href);
const m = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/moves.js`).href);
const draft = {repo: "widgets", state: "proposed", title: "Rotate keys", body: "In place.",
               severity: "", kind: "", parent: "", blockers: ""};
const req = changes => m.createRequest({...draft, ...changes});
// What each puts on the wire, with fetch stubbed.
const sent = [];
globalThis.fetch = (url, opts) => {
  sent.push({url, method: opts.method, body: JSON.parse(opts.body)});
  return Promise.resolve({status: 200, text: async () => '{"ok": true}'});
};
await m.sendWrite({kind: "create", task: null, send: req({}), bound: 7,
                   verb: "create the task"});
await m.sendWrite({kind: "reparent", task: "7", revision: 4,
                   send: {revision: 4, parent: null}, verb: "remove the parent"});
const task = {id: 7, revision: 4, parent: {id: 3, title: "P", state: "open"}, claim: null};
const orphan = {...task, parent: null};
const reparent = parent => ({kind: "reparent", task: "7", revision: 4,
                             send: {revision: 4, parent}, verb: "make it so"});
// A create sent past task 10, and what reading its repo back may find.
const create = {kind: "create", task: null, send: req({}), bound: 10,
                verb: "create the task"};
const first = {...create, bound: 0};
const row = (id, title = "Rotate keys") => ({id, title});
const full = (id, body = "In place.", title = "Rotate keys") => ({id, title, body});
const others = (top, n) => Array.from({length: n}, (_, i) => row(top - i, "Other"));
const lists = {
  found: {list: {tasks: [row(11), row(10)], total: 12}, candidates: [full(11)]},
  second: {list: {tasks: [row(12), row(11), row(9)], total: 30},
           candidates: [full(12, "Another body."), full(11)]},
  other_body: {list: {tasks: [row(11), row(10)], total: 30},
               candidates: [full(11, "In place, but not.")]},
  other_title: {list: {tasks: [row(11), row(10)], total: 30},
                candidates: [full(11, "In place.", "Rotate keys!")]},
  at_bound: {list: {tasks: [row(10), row(9)], total: 30}, candidates: [full(10)]},
  full_page_past: {list: {tasks: others(60, 50), total: 80}, candidates: []},
  failed_candidate: {list: {tasks: [row(11), row(10)], total: 30}, candidates: null},
  failed_read: {list: null, candidates: null},
};
const created = Object.fromEntries(Object.entries(lists)
  .map(([k, v]) => [k, m.createdTask(create, v)]));
// The repo read back through `readCreated`, fetch stubbed: tasks 12 and 11 past the
// bound have the title sent, 12 with another body; 11's full read fails or not.
const readBack = async failing => {
  const asked = [];
  globalThis.fetch = async url => {
    asked.push(url);
    const json = body => ({status: 200, ok: true, json: async () => body});
    if (url.startsWith("/api/tasks?")) {
      return json({tasks: [row(12), row(11), row(10)], total: 30});
    }
    if (url === "/api/tasks/12") return json(full(12, "Another body."));
    if (url === "/api/tasks/11" && !failing) return json(full(11));
    return {status: 500, ok: false, json: async () => ({ok: false})};
  };
  const reread = await t.readCreated(create);
  return {asked, candidates: reread.candidates && reread.candidates.map(c => c.id),
          check: m.checkWrite(create, reread)};
};
const read_back = {whole: await readBack(false), failed: await readBack(true)};
console.log(JSON.stringify({
  states: m.CREATE_STATES,
  requests: {
    plain: req({}),
    blank_title: req({title: " \u3000\t"}),
    empty_body: req({body: ""}),
    all: req({state: "open", severity: "high", kind: "finding", parent: "#3",
              blockers: "#2, 5 2"}),
    bad_parent: req({parent: "x3"}),
    second_parent: req({parent: "#3 #4"}),
    bad_blockers: req({blockers: "#2, y"}),
    refined: req({state: "refined"}),
    no_repo: req({repo: ""}),
    repo_not_text: req({repo: 5}),
    no_body: req({body: undefined}),
    unknown_values: req({severity: "critical", kind: "chore"}),
    none: m.createRequest(null),
  },
  parse_parent: ["", "#7", "7", " #7 ", "#7, #8", "x", "#0"].map(m.parseParent),
  parent_ids: [m.parentId(task), m.parentId(orphan), m.parentId(null),
               m.parentId({parent: {id: "3"}})],
  reparent_requests: {
    same: m.reparentRequest(task, "#3"), same_bare: m.reparentRequest(task, " 3 "),
    none: m.reparentRequest(task, ""), other: m.reparentRequest(task, "#5"),
    bad: m.reparentRequest(task, "#5 #6"), not_an_id: m.reparentRequest(task, "x"),
    none_from_none: m.reparentRequest(orphan, ""),
    set_from_none: m.reparentRequest(orphan, "#5"), no_task: m.reparentRequest(null, "#5"),
  },
  reparent_checks: {
    set: m.checkWrite(reparent(5), {task: {...task, revision: 5, parent: {id: 5}}}),
    removed: m.checkWrite(reparent(null), {task: {...orphan, revision: 5}}),
    not_landed: m.checkWrite(reparent(5), {task}),
    changed: m.checkWrite(reparent(5), {task: {...task, revision: 5}}),
    changed_removed: m.checkWrite(reparent(null), {task: {...task, revision: 5}}),
    failed: m.checkWrite(reparent(5), null),
  },
  create_checks: {
    ...Object.fromEntries(Object.entries(lists)
      .map(([k, v]) => [k, m.checkWrite(create, v)])),
    nothing_read: m.checkWrite(create, null),
    empty_repo: m.checkWrite(first, {list: {tasks: [], total: 0}, candidates: []}),
    first_found: m.checkWrite(first, {list: {tasks: [row(1)], total: 1},
                                      candidates: [full(1)]}),
    whole_total: m.checkWrite(first, {list: {tasks: others(50, 50), total: 50},
                                      candidates: []}),
    past_whole_page: m.checkWrite(first, {list: {tasks: others(50, 50), total: 51},
                                          candidates: []}),
  },
  created,
  read_back,
  texts: {
    landed: t.createOutcomeText(create, "landed", null, 12),
    landed_edited: t.createOutcomeText(create, "landed", null, 12, true),
    found: t.createOutcomeText(create, "found", null, 11),
    found_first: t.createOutcomeText(first, "found", null, 1),
    found_edited: t.createOutcomeText(create, "found", null, 11, true),
    no_bound: t.createOutcomeText({...create, bound: null}, "no-bound", null, null),
    refused: t.createOutcomeText(create, "refused", "no task 19 in this repo\u202e", null),
    not_running: t.createOutcomeText(create, "not-running", null, null),
    not_found_yet: t.createOutcomeText(create, "not-found-yet", null, null),
    not_found_first: t.createOutcomeText(first, "not-found-yet", null, null),
    could_not_check: t.createOutcomeText(create, "could-not-check", null, null),
    other: t.createOutcomeText(create, "unknown", null, null),
    reparent_found: t.outcomeText(reparent(5), "found").text,
    reparent_not_landed: t.outcomeText(reparent(5), "not-landed").text,
    reparent_landed: t.outcomeText({...reparent(5), verb: "make #5 the parent"},
                                   "landed").text,
  },
  labels: {
    create: t.createLabel(draft, false),
    send_anyway: t.createLabel({...draft, state: "open"}, true),
    create_spelled: t.createLabel({...draft, repo: "a\u202eb"}, false).main,
    create_none: t.createLabel(null, false),
    reparent: t.reparentLabel(task, {revision: 4, parent: 5}),
    reparent_none: t.reparentLabel({...task, claim: {holder: "x"}},
                                   {revision: 4, parent: null}),
    reparent_nothing: t.reparentLabel(task, null),
    preview: t.createPreview("Rotate\u200b keys"),
    preview_none: t.createPreview(undefined),
    problem: t.parentProblem("x\u202e"),
  },
  wire: sent,
}));
"""


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class CreateTests(unittest.TestCase):
    """The create's and the reparent's pure halves (``moves.js``) and what ``tasks.js``
    says around them: only a whole create is sent, a parent is one id or none, and a
    lost create is found only past its bound, by the title and body it sent."""

    probe: dict

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", _CREATE_PROBE],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR))},
            capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing moves.js under node failed:\n" + proc.stderr)
        cls.probe = json.loads(proc.stdout)

    def test_a_create_is_sent_whole_or_not_at_all(self):
        r = self.probe["requests"]
        self.assertEqual(r["plain"], {
            "repo": "widgets", "title": "Rotate keys", "body": "In place.",
            "state": "proposed", "severity": None, "kind": None, "parent": None,
            "blocked_by": []})
        # Every field the operator API's model takes, and only those.
        self.assertEqual(set(r["plain"]), set(_api.CreateTask.__annotations__))
        self.assertEqual(r["empty_body"]["body"], "")
        self.assertEqual(r["all"], {
            "repo": "widgets", "title": "Rotate keys", "body": "In place.",
            "state": "open", "severity": "high", "kind": "finding", "parent": 3,
            "blocked_by": [2, 5]})
        # A value no select offers goes as none, never as typed.
        self.assertEqual((r["unknown_values"]["severity"], r["unknown_values"]["kind"]),
                         (None, None))
        for key in ("blank_title", "bad_parent", "second_parent", "bad_blockers",
                    "refined", "no_repo", "repo_not_text", "no_body", "none"):
            with self.subTest(case=key):
                self.assertIsNone(r[key])

    def test_the_states_are_every_one_but_refined(self):
        states = [s for s in self.probe["states"]]
        self.assertEqual(states, ["proposed", "open", "landing", "deferred", "closed"])

    def test_a_parent_is_one_id_or_none(self):
        self.assertEqual(self.probe["parse_parent"], [
            {"id": None, "bad": None}, {"id": 7, "bad": None}, {"id": 7, "bad": None},
            {"id": 7, "bad": None}, {"id": None, "bad": "#8"}, {"id": None, "bad": "x"},
            {"id": None, "bad": "#0"}])
        self.assertEqual(self.probe["parent_ids"], [3, None, None, None])
        self.assertEqual(self.probe["labels"]["problem"],
                         '"x\\u202e" is not a parent: a parent is one task id, such as '
                         "#7, or nothing for none.")

    def test_a_reparent_names_the_revision_and_only_another_parent(self):
        r = self.probe["reparent_requests"]
        for key in ("same", "same_bare", "bad", "not_an_id", "none_from_none", "no_task"):
            with self.subTest(case=key):
                self.assertIsNone(r[key])
        self.assertEqual(r["none"], {"revision": 4, "parent": None})
        self.assertEqual(r["other"], {"revision": 4, "parent": 5})
        self.assertEqual(r["set_from_none"], {"revision": 4, "parent": 5})

    def test_each_is_posted_as_its_route_takes_it(self):
        self.assertEqual(self.probe["wire"], [
            {"url": "/api/tasks", "method": "POST",
             "body": self.probe["requests"]["plain"]},
            {"url": "/api/tasks/7/reparent", "method": "POST",
             "body": {"revision": 4, "parent": None}}])
        # Required, and null for none: an absent parent would not read as "none".
        self.assertEqual(set(_api.Reparent.__annotations__), {"revision", "parent"})

    def test_a_lost_reparent_is_decided_by_the_revision_and_the_parent(self):
        self.assertEqual(self.probe["reparent_checks"], {
            "set": "found", "removed": "found", "not_landed": "not-landed",
            "changed": "changed", "changed_removed": "changed",
            "failed": "could-not-check"})
        texts = self.probe["texts"]
        self.assertEqual(texts["reparent_landed"], "Make #5 the parent: done.")
        self.assertIn("the task shows it landed", texts["reparent_found"])
        self.assertIn("cannot apply it twice", texts["reparent_not_landed"])

    def test_a_lost_create_is_found_only_past_its_bound_by_title_and_body(self):
        c = self.probe["create_checks"]
        self.assertEqual(c, {
            "found": "found", "second": "found",
            # Not there: the page reaches the bound, so it is not found yet.
            "other_body": "not-found-yet", "other_title": "not-found-yet",
            "at_bound": "not-found-yet",
            # A full page all past it may have pushed it off.
            "full_page_past": "could-not-check",
            "failed_candidate": "could-not-check", "failed_read": "could-not-check",
            "nothing_read": "could-not-check",
            "empty_repo": "not-found-yet", "first_found": "found",
            # A page holding the repo's whole total reaches any bound.
            "whole_total": "not-found-yet", "past_whole_page": "could-not-check"})
        self.assertEqual(self.probe["created"], {
            "found": 11, "second": 11, "other_body": None, "other_title": None,
            "at_bound": None, "full_page_past": None, "failed_candidate": None,
            "failed_read": None})

    def test_a_candidate_that_could_not_be_read_leaves_it_unknown(self):
        # Read whole, the one with the body sent is found; one read failing stands for
        # all of them, since it may be the one: never "not found yet" on what was read.
        back = self.probe["read_back"]
        self.assertEqual(back["whole"], {
            "asked": ["/api/tasks?repo=widgets&state=all", "/api/tasks/12",
                      "/api/tasks/11"],
            "candidates": [12, 11], "check": "found"})
        self.assertEqual(back["failed"], {
            "asked": ["/api/tasks?repo=widgets&state=all", "/api/tasks/12",
                      "/api/tasks/11"],
            "candidates": None, "check": "could-not-check"})

    def test_no_outcome_of_a_lost_create_calls_sending_it_again_safe(self):
        t = self.probe["texts"]
        self.assertEqual(t["landed"], {"text": "Created task #12.", "warn": False})
        self.assertEqual(t["landed_edited"], {
            "text": "Created task #12. The task was created as sent; your edits since "
                    "are kept in the form.", "warn": False})
        self.assertEqual(t["found"], {
            "text": "No answer came, but widgets has task #11 past task #10 with the "
                    "title and body sent, so it landed.", "warn": False})
        self.assertEqual(t["found_first"]["text"],
                         "No answer came, but widgets has task #1 with the title and body "
                         "sent, so it landed.")
        self.assertTrue(t["found_edited"]["text"].endswith(
            " The task was created as sent; your edits since are kept in the form."))
        self.assertEqual(t["no_bound"], {"text": "Could not read the repo; nothing sent.",
                                         "warn": True})
        self.assertEqual(t["refused"], {
            "text": "Create the task: refused. no task 19 in this repo\\u202e",
            "warn": True})
        self.assertEqual(t["not_running"]["text"],
                         "Create the task: not sent. The task registry is not running.")
        self.assertEqual(t["not_found_yet"]["text"],
                         "No answer came, and no task in widgets past task #10 has the "
                         "title and body sent yet. It may still land: look again before "
                         "sending it again, which may make a second task.")
        self.assertIn("no task in widgets has the title", t["not_found_first"]["text"])
        self.assertIn("whether it landed is not known", t["could_not_check"]["text"])
        for key in ("not_found_yet", "could_not_check", "other"):
            with self.subTest(case=key):
                self.assertTrue(t[key]["warn"])
                self.assertIn("which may make a second task", t[key]["text"])
                self.assertNotIn("harmless", t[key]["text"])
                self.assertNotIn("safe", t[key]["text"])

    def test_the_create_names_its_repo_and_state_and_a_resend_warns(self):
        labels = self.probe["labels"]
        self.assertEqual(labels["create"], {
            "main": "create the task in widgets as proposed",
            "second": "a task can be closed, never removed", "caution": False})
        self.assertEqual(labels["send_anyway"], {
            "main": "send anyway — may make a second task",
            "second": "in widgets as open · a task can be closed, never removed",
            "caution": True})
        self.assertEqual(labels["create_spelled"],
                         "create the task in a\\u202eb as proposed")
        self.assertIsNone(labels["create_none"])

    def test_the_reparent_names_the_parent_and_the_revision(self):
        labels = self.probe["labels"]
        self.assertEqual(labels["reparent"], {"main": "make #5 the parent",
                                              "second": "revision 4", "caution": False})
        self.assertEqual(labels["reparent_none"], {
            "main": "remove the parent", "second": "revision 4 · the claim stays",
            "caution": False})
        self.assertIsNone(labels["reparent_nothing"])

    def test_the_preview_says_what_will_be_stored_spelled(self):
        labels = self.probe["labels"]
        self.assertEqual(labels["preview"], {"label": "will be stored",
                                             "text": "Rotate\\u200b keys", "hidden": 1})
        self.assertEqual(labels["preview_none"], {"label": "will be stored", "text": "",
                                                  "hidden": 0})


# The bodies the page builds for drafts and reparents a scratch store holds, and what
# its check makes of the reads after: `DOCKADE_IN` is `{drafts, reparents, checks}`.
_CREATE_RULE_PROBE = r"""
import { pathToFileURL } from "node:url";
const m = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/moves.js`).href);
const given = JSON.parse(process.env.DOCKADE_IN);
console.log(JSON.stringify({
  states: m.CREATE_STATES,
  sent: (given.drafts || []).map(m.createRequest),
  reparents: (given.reparents || []).map(([task, text]) => m.reparentRequest(task, text)),
  checks: (given.checks || []).map(([write, reread]) => m.checkWrite(write, reread)),
}));
"""


def _rule_probe(given: dict) -> dict:
    proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
        [_NODE, "--input-type=module", "-e", _CREATE_RULE_PROBE],
        env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR)),
             "DOCKADE_IN": json.dumps(given)},
        capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise AssertionError("importing moves.js under node failed:\n" + proc.stderr)
    return json.loads(proc.stdout)


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class CreateRuleTests(_OperatorStore):
    """The create the page builds against the registry's own: each body taken by the
    operator API's model and by ``create`` on a scratch store, read back as sent, and
    found by the page's check past the bound read before it, but not with the bound at
    its own id."""

    def _draft(self, **changes: str) -> dict:
        return {"repo": "widgets", "state": "proposed", "title": "Rotate keys",
                "body": "In place.", "severity": "", "kind": "", "parent": "",
                "blockers": "", **changes}

    def _bound(self) -> int:
        page = _reads.list_tasks("widgets", None)
        return max((t["id"] for t in page["tasks"]), default=0)

    def _reread(self, title: str) -> dict:
        page = _reads.list_tasks("widgets", None)
        return {"list": page, "candidates": [_reads.get_task(t["id"]) for t in page["tasks"]
                                             if t["title"] == title]}

    def test_each_create_lands_as_sent_and_is_found_past_its_bound(self):
        other = _reg.operator_moves.create("widgets", "Other", "B.", "open")["task"]
        drafts = [self._draft(),
                  self._draft(body="", state="open"),
                  self._draft(title="With all", severity="low", kind="question",
                              parent=f"#{self.task}", blockers=f"{other}, #{other}",
                              state="deferred"),
                  self._draft(title="Closed at once", body="Done.\nAlready.",
                              state="closed", blockers=f"#{self.task}")]
        sent = _rule_probe({"drafts": drafts})["sent"]
        for body in sent:
            with self.subTest(sent=body):
                self.assertIsNotNone(body)
                bound = self._bound()
                reply = _api.create_task(_api.CreateTask(**body), ThroughTheRelay())
                self.assertEqual(reply.status_code, 200, reply.body)
                made = reply.body["task"]
                now = _reads.get_task(made)
                for field in ("title", "body", "state", "severity", "kind"):
                    self.assertEqual(now[field], body[field], field)
                self.assertEqual(now["parent"] and now["parent"]["id"], body["parent"])
                self.assertEqual(sorted(b["id"] for b in now["blockers"]),
                                 sorted(body["blocked_by"]))
                write = {"kind": "create", "task": None, "send": body, "bound": bound}
                at_it = {**write, "bound": made}
                reread = self._reread(body["title"])
                checks = _rule_probe({"checks": [[write, reread], [at_it, reread]]})
                self.assertEqual(checks["checks"], ["found", "not-found-yet"])

    def test_the_states_offered_are_the_ones_create_takes(self):
        took = []
        for state in _reg.tasks.STATES:
            try:
                _reg.operator_moves.create("widgets", f"In {state}", "B.", state)
            except _Refused:
                continue
            took.append(state)
        self.assertEqual(_rule_probe({})["states"], took)


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class ReparentRuleTests(_OperatorStore):
    """The reparent the page builds against the registry's own: setting, changing and
    removing a parent each land on a scratch store and are found by the page's check;
    read at the revision sent, none has landed."""

    def test_set_change_and_remove_land_and_are_found(self):
        op = _reg.operator_moves
        a = op.create("widgets", "A", "B.", "open")["task"]
        b = op.create("widgets", "B", "B.", "open")["task"]
        child = op.create("widgets", "Child", "B.", "open")["task"]
        for text, expected in ((f"#{a}", a), (f"{b}", b), ("", None)):
            with self.subTest(text=text):
                before = _reads.get_task(child)
                body = _rule_probe({"reparents": [[before, text]]})["reparents"][0]
                self.assertEqual(body, {"revision": before["revision"], "parent": expected})
                reply = _api.reparent(child, _api.Reparent(**body), ThroughTheRelay())
                self.assertEqual(reply.status_code, 200, reply.body)
                after = _reads.get_task(child)
                self.assertEqual(after["parent"] and after["parent"]["id"], expected)
                write = {"kind": "reparent", "task": str(child),
                         "revision": body["revision"], "send": body}
                checks = _rule_probe({"checks": [[write, {"task": after}],
                                                 [write, {"task": before}]]})["checks"]
                self.assertEqual(checks, ["found", "not-landed"])
        # The parent it has now is no reparent at all.
        none = _rule_probe({"reparents": [[_reads.get_task(child), ""]]})["reparents"]
        self.assertEqual(none, [None])


_REPOS_PROBE = r"""
import { pathToFileURL } from "node:url";
const r = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/repos.js`).href);
const t = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/tasks.js`).href);
const acme = {name: "acme", remote: "github.com/acme/acme", retired: false};
const bare = {name: "bare", remote: null, retired: false};
const old = {name: "old", remote: null, retired: true};
// What each puts on the wire, with fetch stubbed.
const sent = [];
globalThis.fetch = (url, opts) => {
  sent.push({url, method: opts.method, body: JSON.parse(opts.body)});
  return Promise.resolve({status: 200, text: async () => '{"ok": true}'});
};
const add = {kind: "add", repo: null, send: {name: "widgets", remote: null}};
const addRemote = {kind: "add", repo: null,
                   send: {name: "widgets", remote: "github.com/acme/widgets"}};
const rename = {kind: "rename", repo: "acme", send: {new_name: "widgets"}};
const setRemote = {kind: "remote", repo: "bare", send: {remote: "github.com/acme/bare"},
                   was: null};
const remove = {kind: "remote", repo: "acme", send: {remote: null},
                was: "github.com/acme/acme"};
const retire = {kind: "retire", repo: "acme", send: {retired: true}};
const back = {kind: "retire", repo: "old", send: {retired: false}};
// A name is encoded into its path, though the registry's never needs it.
const odd = {kind: "rename", repo: "a/b?c", send: {new_name: "x"}};
for (const w of [add, addRemote, rename, setRemote, remove, retire, back, odd]) {
  await r.sendRepoWrite(w);
}
let unknown = null;
try { r.sendRepoWrite({kind: "delete", repo: "acme", send: {}}); }
catch (e) { unknown = e.message; }
// The runner, with fetch and the page's hooks stubbed: what each answer comes to.
async function runWith(answer, write, listed, open = true) {
  const calls = [];
  let posts = 0;
  globalThis.fetch = () => { posts += 1; return Promise.resolve(answer); };
  r.repoHooks({
    begin: () => { calls.push("begin"); return open; },
    end: () => calls.push("end"), busy: () => false,
    notRunning: () => calls.push("notRunning"),
    read: async renamed => { calls.push(`read:${JSON.stringify(renamed)}`); return listed; },
    after: () => calls.push("after"),
    spell: text => { calls.push("spell"); return `<${text}>`; },
  });
  const out = await r.runRepoWrite({...write, verb: write.verb});
  return {out: out ?? null, calls, sent: posts};
}
const reply = (status, text) => ({status, text: async () => text});
const addW = {kind: "add", repo: null, send: {name: "widgets", remote: null},
              verb: "add widgets, no remote"};
const renameW = {kind: "rename", repo: "acme", send: {new_name: "widgets"},
                 verb: "rename acme to widgets"};
const runs = {
  refused: await runWith(reply(400,
    JSON.stringify({ok: false, current: null, detail: "repo 'w\u034fidgets' exists"})),
    addW, []),
  guard: await runWith(reply(403, "refused by tasks-ui: no"), addW, []),
  lost: await runWith(reply(504, JSON.stringify({ok: false, current: null, detail: "x"})),
    renameW, [{name: "widgets", remote: null, retired: false}]),
  down: await runWith(reply(502, JSON.stringify({ok: false, current: null, detail: "x"})),
    addW, null),
  shut: await runWith(reply(200, '{"ok": true}'), addW, [], false),
};
delete runs.refused.sent; delete runs.guard.sent; delete runs.lost.sent;
delete runs.down.sent;
globalThis.fetch = (url, opts) => {
  sent.push({url, method: opts.method, body: JSON.parse(opts.body)});
  return Promise.resolve({status: 200, text: async () => '{"ok": true}'});
};
// An unrelated repo added between the draw and the read again changes nothing.
const extra = {name: "zzz", remote: null, retired: false};
const list = (...repos) => [...repos, extra];
const check = (w, ...repos) => r.checkRepoWrite(w, list(...repos));
const failed = w => [null, undefined, {repos: []}].map(read => r.checkRepoWrite(w, read));
console.log(JSON.stringify({
  requests: {
    add: r.addRequest("widgets", ""),
    add_remote: r.addRequest("widgets", "github.com/acme/widgets"),
    add_no_remote: r.addRequest("widgets", undefined),
    add_nothing: r.addRequest("", "github.com/acme/widgets"),
    add_not_text: r.addRequest(null, ""),
    add_number: r.addRequest(5, ""),
    add_number_remote: r.addRequest("widgets", 5),
    // Sent as typed: the registry says what is wrong with it.
    add_as_typed: r.addRequest(" Acme", " x"),
    rename: r.renameRequest(acme, "widgets"),
    rename_same: r.renameRequest(acme, "acme"),
    rename_nothing: r.renameRequest(acme, ""),
    rename_not_text: r.renameRequest(acme, null),
    rename_number: r.renameRequest(acme, 7),
    rename_no_repo: r.renameRequest(null, "widgets"),
    remote: r.remoteRequest(bare, "github.com/acme/bare"),
    remote_other: r.remoteRequest(acme, "github.com/acme/other"),
    remote_same: r.remoteRequest(acme, "github.com/acme/acme"),
    remote_nothing: r.remoteRequest(acme, ""),
    remote_not_text: r.remoteRequest(acme, undefined),
    remote_number: r.remoteRequest(acme, 7),
    remote_no_repo: r.remoteRequest(null, "github.com/acme/bare"),
    remove: r.removeRemoteRequest(acme),
    remove_none: r.removeRemoteRequest(bare),
    remove_no_repo: r.removeRemoteRequest(undefined),
    retire: r.retireRequest(acme),
    back: r.retireRequest(old),
    retire_no_repo: r.retireRequest(null),
  },
  wire: sent,
  unknown,
  checks: {
    add: {
      found: check(add, {name: "widgets", remote: null, retired: false}),
      found_remote: check(addRemote, {name: "widgets", remote: "github.com/acme/widgets",
                                      retired: false}),
      not_seen: check(add, acme),
      other_remote: check(add, {name: "widgets", remote: "github.com/acme/other",
                                retired: false}),
      retired: check(add, {name: "widgets", remote: null, retired: true}),
      failed: failed(add),
    },
    rename: {
      found: check(rename, {...acme, name: "widgets"}),
      not_seen: check(rename, acme),
      both: check(rename, acme, {...acme, name: "widgets"}),
      neither: check(rename, bare),
      failed: failed(rename),
    },
    remote: {
      found: check(setRemote, {...bare, remote: "github.com/acme/bare"}),
      not_seen: check(setRemote, bare),
      third: check(setRemote, {...bare, remote: "github.com/acme/third"}),
      gone: check(setRemote, acme),
      removed: check(remove, {...acme, remote: null}),
      not_removed: check(remove, acme),
      removed_third: check(remove, {...acme, remote: "github.com/acme/third"}),
      // An entry with no remote field reads as none.
      removed_absent: check(remove, {name: "acme", retired: false}),
      // A hole in the list is passed over.
      hole: r.checkRepoWrite(setRemote, [null, {...bare, remote: "github.com/acme/bare"}]),
      failed: failed(setRemote),
    },
    retire: {
      found: check(retire, {...acme, retired: true}),
      not_seen: check(retire, acme),
      back: check(back, {...old, retired: false}),
      not_back: check(back, old),
      // An entry with no retired field reads as active.
      back_absent: check(back, {name: "old", remote: null}),
      gone: check(retire, bare),
      failed: failed(retire),
    },
    unknown_kind: r.checkRepoWrite({kind: "delete", repo: "acme", send: {}}, [acme]),
  },
  followed: {
    kept: r.followedRepo("acme", null, [acme, bare]),
    renamed: r.followedRepo("acme", {from: "acme", to: "widgets"}, [bare, {name: "widgets"}]),
    // Not yet landed: the old name is still listed, and stays chosen.
    not_landed: r.followedRepo("acme", {from: "acme", to: "widgets"}, [acme, bare]),
    // Another repo's rename leaves the choice where it is.
    other: r.followedRepo("bare", {from: "acme", to: "widgets"}, [bare, {name: "widgets"}]),
    // A chosen repo gone is not taken to another repo's new name.
    other_gone: r.followedRepo("bare", {from: "acme", to: "widgets"}, [{name: "widgets"}]),
    // Gone, renamed elsewhere or never landed: none.
    gone: r.followedRepo("acme", null, [bare]),
    gone_renamed_away: r.followedRepo("acme", {from: "acme", to: "widgets"}, [bare]),
    nothing_chosen: r.followedRepo("", null, [acme]),
    failed: r.followedRepo("acme", null, null),
  },
  labels: {
    add: r.addLabel({name: "widgets", remote: null}),
    add_remote: r.addLabel({name: "widgets", remote: "github.com/acme/widgets"}),
    add_nothing: r.addLabel(null),
    rename: r.renameLabel(acme, {new_name: "widgets"}),
    rename_nothing: r.renameLabel(acme, null),
    remote: r.remoteLabel(bare, {remote: "github.com/acme/bare"}),
    remote_nothing: r.remoteLabel(bare, null),
    remove: r.removeRemoteLabel(acme),
    retire: r.retireLabel(acme),
    back: r.retireLabel(old),
  },
  warning: [r.renameWarning("widgets"), r.renameWarning(""), r.renameWarning(null)],
  texts: Object.fromEntries([
    ["landed", "landed"], ["found", "found"], ["refused", "refused"],
    ["not_running", "not-running"], ["not_seen", "not-seen"], ["changed", "changed"],
    ["could_not_check", "could-not-check"], ["other", "unknown"],
  ].flatMap(([key, outcome]) => [
    [`add_${key}`, r.repoOutcomeText({...add, verb: "add widgets, no remote"}, outcome,
                                     "repo 'widgets' already exists")],
    [`rename_${key}`, r.repoOutcomeText({...rename, verb: "rename acme to widgets"},
                                        outcome, "repo 'widgets' already exists")],
    [`retire_${key}`, r.repoOutcomeText({...retire, verb: "retire acme: no new tasks"},
                                        outcome, null)],
  ])),
  no_repos: t.NO_REPOS_TEXT,
  runs,
}));
"""


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class RepoTests(unittest.TestCase):
    """The Repos view's pure half (``repos.js``): each write's body and path, and how a
    lost one is settled from the repo list read again, since repos carry no revision."""

    probe: dict

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
            [_NODE, "--input-type=module", "-e", _REPOS_PROBE],
            env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR))},
            capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError("importing repos.js under node failed:\\n" + proc.stderr)
        cls.probe = json.loads(proc.stdout)

    def test_each_request_is_the_body_its_route_takes_or_nothing(self):
        r = self.probe["requests"]
        self.assertEqual(r["add"], {"name": "widgets", "remote": None})
        self.assertEqual(r["add_remote"], {"name": "widgets",
                                           "remote": "github.com/acme/widgets"})
        self.assertEqual(r["add_no_remote"], {"name": "widgets", "remote": None})
        self.assertEqual(r["add_as_typed"], {"name": " Acme", "remote": " x"})
        self.assertEqual(r["rename"], {"new_name": "widgets"})
        self.assertEqual(r["remote"], {"remote": "github.com/acme/bare"})
        self.assertEqual(r["remote_other"], {"remote": "github.com/acme/other"})
        # A removal sends null, never an absent remote, which would not read as "none".
        self.assertEqual(r["remove"], {"remote": None})
        self.assertEqual(r["retire"], {"retired": True})
        self.assertEqual(r["back"], {"retired": False})
        self.assertEqual(r["add_number_remote"], {"name": "widgets", "remote": None})
        for key in ("add_nothing", "add_not_text", "add_number", "rename_same",
                    "rename_nothing", "rename_not_text", "rename_number", "rename_no_repo",
                    "remote_same", "remote_nothing", "remote_not_text", "remote_number",
                    "remote_no_repo", "remove_none", "remove_no_repo", "retire_no_repo"):
            with self.subTest(case=key):
                self.assertIsNone(r[key])
        # Each field the registry's model names, and no other.
        for model, key in ((_api.AddRepo, "add_remote"), (_api.RenameRepo, "rename"),
                           (_api.SetRemote, "remove"), (_api.RetireRepo, "retire")):
            with self.subTest(model=model.__name__):
                self.assertEqual(set(r[key]), set(model.__annotations__))

    def test_each_is_posted_to_its_route_naming_its_repo(self):
        def post(url, body):
            return {"url": url, "method": "POST", "body": body}
        self.assertEqual(self.probe["wire"], [
            post("/api/repos", {"name": "widgets", "remote": None}),
            post("/api/repos", {"name": "widgets", "remote": "github.com/acme/widgets"}),
            post("/api/repos/acme/rename", {"new_name": "widgets"}),
            post("/api/repos/bare/remote", {"remote": "github.com/acme/bare"}),
            post("/api/repos/acme/remote", {"remote": None}),
            post("/api/repos/acme/retire", {"retired": True}),
            post("/api/repos/old/retire", {"retired": False}),
            post("/api/repos/a%2Fb%3Fc/rename", {"new_name": "x"})])
        for call in self.probe["wire"][:-1]:
            with self.subTest(url=call["url"]):
                self.assertTrue(tui._relay_allowed("POST", call["url"]))
        self.assertEqual(self.probe["unknown"], "no such write: delete")

    def test_a_lost_write_is_settled_by_the_named_repo_alone(self):
        c = self.probe["checks"]
        failed = ["could-not-check"] * 3
        self.assertEqual(c["add"], {
            "found": "found", "found_remote": "found", "not_seen": "not-seen",
            "other_remote": "changed", "retired": "changed", "failed": failed})
        self.assertEqual(c["rename"], {
            "found": "found", "not_seen": "not-seen", "both": "changed",
            "neither": "changed", "failed": failed})
        self.assertEqual(c["remote"], {
            "found": "found", "not_seen": "not-seen", "third": "changed",
            "gone": "changed", "removed": "found", "not_removed": "not-seen",
            "removed_third": "changed", "removed_absent": "found", "hole": "found",
            "failed": failed})
        self.assertEqual(c["retire"], {
            "found": "found", "not_seen": "not-seen", "back": "found",
            "not_back": "not-seen", "back_absent": "found", "gone": "changed",
            "failed": failed})
        self.assertEqual(c["unknown_kind"], "changed")

    def test_the_select_follows_a_rename_once_listed_and_lets_a_gone_repo_go(self):
        self.assertEqual(self.probe["followed"], {
            "kept": "acme", "renamed": "widgets", "not_landed": "acme", "other": "bare",
            "other_gone": None,
            "gone": None, "gone_renamed_away": None, "nothing_chosen": None,
            "failed": None})

    def test_each_label_names_its_outcome(self):
        self.assertEqual(self.probe["labels"], {
            "add": "add widgets, no remote",
            "add_remote": "add widgets, remote github.com/acme/widgets",
            "add_nothing": "add a repo",
            "rename": "rename acme to widgets", "rename_nothing": "rename acme",
            "remote": "set bare's remote to github.com/acme/bare",
            "remote_nothing": "set bare's remote",
            "remove": "remove acme's remote github.com/acme/acme",
            "retire": "retire acme: no new tasks", "back": "bring old back"})

    def test_a_rename_says_the_instructions_file_must_name_it(self):
        self.assertEqual(self.probe["warning"], [
            "The repo's instructions file must name widgets before its agents can write "
            "again.",
            "The repo's instructions file must name the new name before its agents can "
            "write again.",
            "The repo's instructions file must name the new name before its agents can "
            "write again."])
        t = self.probe["texts"]
        warning = self.probe["warning"][0]
        self.assertEqual(t["rename_landed"], {
            "text": f"Rename acme to widgets: done. {warning}", "warn": True})
        self.assertEqual(t["rename_found"], {
            "text": "Rename acme to widgets: no answer came, but the repo list shows it "
                    f"landed. {warning}", "warn": True})
        # Only a rename that landed says so.
        for key, said in t.items():
            if key not in ("rename_landed", "rename_found"):
                with self.subTest(case=key):
                    self.assertNotIn("instructions file", said["text"])
        # And the view points at it beside the add form, before any rename.
        html = (TASKS_DIR / "index.html").read_text()
        view = html[html.index('<section id="repos-view"'):]
        self.assertIn("instructions file", view[:view.index("</section>")])

    def test_each_outcome_says_what_happened(self):
        t = self.probe["texts"]
        self.assertEqual(t["add_landed"], {"text": "Add widgets, no remote: done.",
                                           "warn": False})
        self.assertEqual(t["retire_landed"], {"text": "Retire acme: no new tasks: done.",
                                              "warn": False})
        self.assertEqual(t["add_found"], {
            "text": "Add widgets, no remote: no answer came, but the repo list shows it "
                    "landed.", "warn": False})
        self.assertEqual(t["add_refused"], {
            "text": "Add widgets, no remote: refused. repo 'widgets' already exists",
            "warn": True})
        self.assertEqual(t["retire_refused"]["text"], "Retire acme: no new tasks: refused.")
        self.assertEqual(t["retire_not_running"]["text"],
                         "Retire acme: no new tasks: not sent. The task registry is not "
                         "running.")
        # Not seen is safe to send again, and says why, by the write.
        self.assertTrue(t["add_not_seen"]["text"].endswith(
            "sending it again is safe, since a second would be refused."))
        self.assertTrue(t["rename_not_seen"]["text"].endswith(
            "sending it again is safe, since a second would be refused."))
        self.assertTrue(t["retire_not_seen"]["text"].endswith(
            "sending it again is safe, since it sets the same value."))
        self.assertIn("the repo list has changed, but not as this would have",
                      t["add_changed"]["text"])
        self.assertIn("could not be read again, so whether it landed is not known",
                      t["add_could_not_check"]["text"])
        for key in ("changed", "could_not_check", "other"):
            for write in ("add", "rename", "retire"):
                with self.subTest(case=f"{write}_{key}"):
                    self.assertTrue(t[f"{write}_{key}"]["warn"])
                    self.assertNotIn("safe", t[f"{write}_{key}"]["text"])
        for write in ("add", "rename", "retire"):
            for key in ("refused", "not_running", "not_seen"):
                with self.subTest(case=f"{write}_{key}"):
                    self.assertTrue(t[f"{write}_{key}"]["warn"])

    def test_the_runner_draws_the_reason_spelled_and_reads_the_list_after(self):
        runs = self.probe["runs"]
        # A refusal shows the registry's reason, spelled, never its JSON; the relay's
        # own refusal is plain text, shown as said.
        self.assertEqual(runs["refused"]["out"], {
            "text": "Add widgets, no remote: refused. <repo 'w\u034fidgets' exists>",
            "warn": True})
        self.assertEqual(runs["refused"]["calls"],
                         ["begin", "read:null", "spell", "end", "after"])
        self.assertEqual(runs["guard"]["out"]["text"],
                         "Add widgets, no remote: refused. <refused by tasks-ui: no>")
        # A lost rename is settled from the list read after it, the rename passed on.
        self.assertEqual(runs["lost"]["out"]["text"].split(" The repo's")[0],
                         "Rename acme to widgets: no answer came, but the repo list shows "
                         "it landed.")
        self.assertIn('read:{"from":"acme","to":"widgets"}', runs["lost"]["calls"])
        # Not running: no read, no other reads after; the page told.
        self.assertEqual(runs["down"]["calls"], ["begin", "notRunning", "spell", "end"])
        # The gate shut: nothing sent, nothing read.
        self.assertEqual(runs["shut"], {"out": None, "calls": ["begin"], "sent": 0})

    def test_no_repos_points_at_the_repos_view(self):
        self.assertIn("Add one under Repos", self.probe["no_repos"])


_REPO_RULE_PROBE = r"""
import { pathToFileURL } from "node:url";
const r = await import(pathToFileURL(`${process.env.DOCKADE_UI_DIR}/repos.js`).href);
const given = JSON.parse(process.env.DOCKADE_IN);
console.log(JSON.stringify({
  add: r.addRequest(...given.add),
  rename: r.renameRequest(...given.rename),
  remote: r.remoteRequest(...given.remote),
  remove: r.removeRemoteRequest(given.remove),
  retire: r.retireRequest(given.retire),
  checks: (given.checks || []).map(([write, listed]) => r.checkRepoWrite(write, listed)),
}));
"""


def _repo_probe(given: dict) -> dict:
    full = {"add": ["", ""], "rename": [None, ""], "remote": [None, ""], "remove": None,
            "retire": None, **given}
    proc = subprocess.run(  # noqa: S603 (absolute path, fixed args, as _probe)
        [_NODE, "--input-type=module", "-e", _REPO_RULE_PROBE],
        env={**os.environ, "DOCKADE_UI_DIR": str(_served_dir(TASKS_DIR)),
             "DOCKADE_IN": json.dumps(full)},
        capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise AssertionError("importing repos.js under node failed:\\n" + proc.stderr)
    return json.loads(proc.stdout)


@unittest.skipIf(not _NODE and not _STRICT,
                 "node is not installed — skipping app.js unit tests")
class RepoRuleTests(_OperatorStore):
    """The repo writes the page builds against the registry's own: each body taken by
    the operator API's model and route on a scratch store, and the list ``reads.repos``
    gives after it found by the page's check, the one before it not seen."""

    def _repo(self, name: str) -> dict:
        return next(r for r in _reads.repos() if r["name"] == name)

    def _lands(self, write: dict, route, model) -> None:
        before = _reads.repos()
        reply = route(model(**write["send"]), ThroughTheRelay()) if write["repo"] is None \
            else route(write["repo"], model(**write["send"]), ThroughTheRelay())
        self.assertEqual(reply.status_code, 200, reply.body)
        after = _reads.repos()
        checks = _repo_probe({"checks": [[write, after], [write, before]]})["checks"]
        self.assertEqual(checks, ["found", "not-seen"])

    def test_each_write_lands_as_sent_and_is_found(self):
        steps = (
            ("add", ["acme", ""], _api.add_repo, _api.AddRepo, None),
            ("add", ["acme-2", "github.com/acme/two"], _api.add_repo, _api.AddRepo, None),
            ("rename", ["acme", "acme-1"], _api.rename_repo, _api.RenameRepo, "acme"),
            ("remote", ["acme-1", "github.com/acme/one"], _api.set_remote, _api.SetRemote,
             "acme-1"),
            ("remove", "acme-1", _api.set_remote, _api.SetRemote, "acme-1"),
            ("retire", "acme-1", _api.retire_repo, _api.RetireRepo, "acme-1"),
            ("retire", "acme-1", _api.retire_repo, _api.RetireRepo, "acme-1"),
        )
        for kind, given, route, model, name in steps:
            with self.subTest(kind=kind, given=given):
                if kind == "add":
                    send = _repo_probe({"add": given})["add"]
                elif kind in ("rename", "remote"):
                    send = _repo_probe({kind: [self._repo(given[0]), given[1]]})[kind]
                else:
                    send = _repo_probe({kind: self._repo(given)})[kind]
                self.assertIsNotNone(send)
                write = {"kind": "remote" if kind == "remove" else kind, "repo": name,
                         "send": send}
                if write["kind"] == "remote":
                    write["was"] = self._repo(name)["remote"]
                self._lands(write, route, model)
        # Retired and brought back, each by the flag the list read before it did not
        # have.
        self.assertFalse(self._repo("acme-1")["retired"])
        self.assertEqual(self._repo("acme-2")["remote"], "github.com/acme/two")
        self.assertIsNone(self._repo("acme-1")["remote"])


class TaskCreateSourceTests(unittest.TestCase):
    """How the page holds, draws and sends a create and a reparent, from source: a
    browser is the only thing that runs it. What each sends is held under node
    (``CreateTests``, ``CreateRuleTests``, ``ReparentRuleTests``)."""

    TASKS = (TASKS_DIR / "tasks.js").read_text()
    HTML = (TASKS_DIR / "index.html").read_text()

    def test_the_bound_is_read_inside_the_gate_and_before_the_send(self):
        run = _function(self.TASKS, "runWrite")
        order = ["writing = write;", "markWriting();",
                 'if (write.kind === "create") {\n    write.bound = await '
                 "readBound(write.send.repo).catch(() => null);",
                 'const unsent = write.kind === "create" && write.bound === null;',
                 "if (!unsent) answer = await readAnswer(await sendWrite(write));",
                 'let result = unsent ? "no-bound" : writeOutcome(answer);',
                 "writing = null;"]
        at = [run.index(step) for step in order]
        self.assertEqual(at, sorted(at), order)
        bound = _function(self.TASKS, "readBound")
        self.assertIn("const q = new URLSearchParams({ repo, state: EVERY_STATE });", bound)
        self.assertIn("fetch(`/api/tasks?${q}`)", bound)
        # 0 for an empty repo: the newest id, never the list's length.
        self.assertIn(".reduce((top, t) => (t && Number(t.id) > top ? Number(t.id) : top), "
                      "0)", bound)

    def test_look_again_checks_the_write_kept_as_sent_never_the_draft(self):
        look = _function(self.TASKS, "lookAgain")
        lines = [ln.strip() for ln in look.splitlines()[1:6]]
        self.assertEqual(lines, [
            "if (writing || lostCreate === null || !registryIsRunning()) return;",
            "const write = lostCreate;", "writing = write;", "markWriting();",
            "const reread = await readCreated(write);"])
        self.assertIn("checkWrite(write, reread)", look)
        self.assertIn("createdTask(write, reread)", look)
        self.assertNotIn("createDraft", look)
        self.assertNotIn("createRequest", look)
        self.assertNotRegex(look, r"\b(?:sendWrite|runWrite)\(")
        self.assertLess(look.index("checkWrite("), look.index("writing = null;"))
        # And what it reads with is the kept write's own title, body and bound.
        read = _function(self.TASKS, "readCreated")
        self.assertIn("Number(row.id) > Number(write.bound)", read)
        self.assertIn("row.title === write.send.title", read)
        self.assertNotIn("createDraft", read)

    def test_the_lost_write_is_kept_until_found_discarded_or_replaced(self):
        sets = re.findall(r"\blostCreate = ([^;]*);", self.TASKS)
        self.assertEqual(sets, ["null", "null", "write", "null"], sets)
        finish = _function(self.TASKS, "finishCreate")
        self.assertIn("  if (landed) {\n    lostCreate = null;\n"
                      "    if (asSent) createDraft = null;\n"
                      '  } else if (result === "not-found-yet" || result === '
                      '"could-not-check") {\n    lostCreate = write;\n  }', finish)
        self.assertIn('const landed = (result === "landed" || result === "found") '
                      "&& created !== null;", finish)
        self.assertIn("const asSent = JSON.stringify(draft) === "
                      "JSON.stringify(write.send);", finish)
        self.assertIn('if (result === "refused") refreshRepos();', finish)
        # The draft: opened, typed in, synced to a select, and dropped by a create that
        # still equals it or by discard.
        drafts = re.findall(r"\bcreateDraft(?:\[field\])? = ([^;]*);", self.TASKS)
        self.assertEqual(drafts, ["null", "{ ...createDraft, repo }", "el.value", "null",
                                  "newCreateDraft()",
                                  "{ ...createDraft, [field]: ev.target.value }", "null"],
                         drafts)
        # A rename the page made of the draft's repo, once listed, takes the draft too.
        self.assertIn("const repo = followedRepo(createDraft.repo, renamed, repos);",
                      _function(self.TASKS, "drawRepos"))

    def test_a_create_found_or_landed_opens_its_task_through_the_hash(self):
        run = _function(self.TASKS, "runWrite")
        self.assertIn('const opens = write.kind === "create" ? '
                      "finishCreate(write, result, detail, created)\n    : null;", run)
        self.assertTrue(run.rstrip("}\n ").endswith(
            "if (opens !== null) goTo(view, String(opens));"))
        self.assertIn('let created = result === "landed" && write.kind === "create" '
                      "? answer.body.task : null;", run)
        self.assertIn('if (write.kind === "create") created = createdTask(write, reread);',
                      run)
        self.assertIn("if (opens !== null) goTo(view, String(opens));",
                      _function(self.TASKS, "lookAgain"))

    def test_the_form_and_the_parent_are_in_the_in_flight_gate(self):
        mark = _function(self.TASKS, "markWriting")
        self.assertIn("input[data-parent]", mark)
        self.assertEqual(mark.splitlines()[1].strip(), "markParent();")
        self.assertTrue(mark.rstrip("}\n ").endswith("markCreate();\n  markRepos();"))
        create = _function(self.TASKS, "markCreate")
        self.assertIn('for (const el of createForm.querySelectorAll("[data-create-field], '
                      'button")) {\n    el.disabled = writing !== null;', create)
        self.assertIn("if (createOpen) createOpen.disabled = writing !== null;", create)
        self.assertIn("if (!createRequest(createDraft)) send.disabled = true;", create)
        self.assertIn("setLabel(send, createLabel(createDraft, lostCreate));", create)
        # The handlers ask the gate too.
        self.assertIn("if (writing || createDraft !== null) return;", self.TASKS)
        self.assertIn('const btn = ev.target.closest("button");\n    if (!btn || writing) '
                      "return;", self.TASKS)

    def test_values_are_set_as_properties_and_previews_spelled(self):
        self.assertIn("el.value = createDraft[field];",
                      _function(self.TASKS, "createControl"))
        self.assertIn("input.value = parentText(task);",
                      _function(self.TASKS, "movesBlock"))
        for name in ("createControl", "renderCreate", "markCreate", "markParent",
                     "movesBlock"):
            with self.subTest(function=name):
                body = _function(self.TASKS, name)
                self.assertNotRegex(body, r'setAttribute\("(?:value|placeholder)"')
                self.assertNotIn("innerHTML", body)
        self.assertIn("const shown = spellHidden(text);",
                      _function(self.TASKS, "createPreview"))
        mark = _function(self.TASKS, "markCreate")
        self.assertIn("text.textContent = preview.text;", mark)
        self.assertIn("hiddenNote(preview.hidden)", mark)
        self.assertIn("parentProblem(bad[field])", mark)
        self.assertIn("blockersProblem(bad[field])", mark)
        self.assertIn("spellHidden(String(bad)).text",
                      _function(self.TASKS, "parentProblem"))

    def test_a_reparent_is_offered_only_while_it_builds_a_body(self):
        mark = _function(self.TASKS, "markParent")
        self.assertIn("const send = reparentRequest(shownTask, text);", mark)
        self.assertIn("  if (send) {\n", mark)
        self.assertIn("} else if (bad !== null) {\n    slot.appendChild(noticeLine({ text: "
                      "parentProblem(bad), warn: true }));", mark)
        self.assertIn('if (btn.dataset.write === "reparent") return reparentWrite(base);',
                      _function(self.TASKS, "writeFor"))
        self.assertIn("reparentRequest(shownTask, parentText(shownTask))",
                      _function(self.TASKS, "reparentWrite"))
        # Its text is kept per task until one lands, and it holds nothing.
        self.assertIn('if (write.kind === "reparent" && landed) '
                      "parentDrafts.delete(write.task);", _function(self.TASKS, "runWrite"))
        self.assertIn("parentDrafts.set(String(shownTask.id), ev.target.value);",
                      self.TASKS)
        self.assertIn('const HELD_KINDS = ["move", "approve", "break"];', self.TASKS)

    def test_the_form_is_on_all_tasks_above_the_list(self):
        view = self.HTML[self.HTML.index('<section id="all-view"'):]
        view = view[:view.index("</section>")]
        self.assertLess(view.index('id="create-open"'), view.index('id="tasks"'))
        self.assertLess(view.index('id="create-form"'), view.index('id="tasks"'))
        # Its outcome is outside the form, which a create that lands closes.
        self.assertLess(view.index('id="create-form" hidden></div>'),
                        view.index('id="create-outcome"'))


if __name__ == "__main__":
    unittest.main()
