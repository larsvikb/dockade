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
    _module_texts,
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
const move = {kind: "move", task: "7", revision: 4, state: "open", verb: "approve",
              lastNote: 12};
const brk = {kind: "break", task: "7", revision: 4, verb: "break the claim", lastNote: 12};
const note = {kind: "note", task: "7", revision: 4, text: "Seen.", lastNote: 12,
              verb: "add the note"};
const after = (extra, notes = []) => ({task: task("open", {revision: 5, ...extra}),
                                       notes: {notes, total: notes.length}});
console.log(JSON.stringify({
  moves: {
    proposed: moves(task("proposed")),
    refined_with_plan: moves(task("refined", {plan})),
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
  },
  answer: {
    json: await m.readAnswer(res(409, '{"ok": false, "current": {"id": 7}}')),
    text: await m.readAnswer(res(403, "refused by tasks-ui: cross-origin\n")),
    cut: await m.readAnswer(cut(200)),
  },
  last_note: [m.lastNoteId({notes: [{id: 3}, {id: 11}, {id: 9}]}),
              m.lastNoteId({notes: []}), m.lastNoteId(null)],
  labels: {
    approve: t.moveLabel({state: "open", verb: "approve", endsClaim: false},
                         task("refined", {plan})),
    ends_claim: t.moveLabel({state: "closed", verb: "close", endsClaim: true},
                            task("open", {claim})),
    plain: t.moveLabel({state: "deferred", verb: "defer", endsClaim: false}, task("open")),
    break: t.breakLabel(claim),
    no_break: t.breakLabel(null),
    approve_none: t.approveLabel(task("proposed")),
    hidden: t.approveHidden(task("refined", {title: "T\u202e", body: "B\u2066", plan})),
    one_hidden: t.approveHidden(task("refined", {plan})),
    no_hidden: t.approveHidden(task("refined", {plan: {...plan, approach: "A."}})),
  },
  texts: Object.fromEntries(["landed", "found", "stale", "refused", "not-running",
                             "not-landed", "not-found-yet", "could-not-check", "changed",
                             "unknown"]
    .map(o => [o, t.outcomeText(o === "not-found-yet" ? note : move, o, "no\u202e")])),
  note_landed_text: t.outcomeText(note, "landed").text,
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

    def test_a_refined_task_is_approved_or_sent_back(self):
        refined = {s: verb for s, verb, _ in self.probe["moves"]["refined_with_plan"]}
        self.assertEqual(refined, {"proposed": "send back", "open": "approve",
                                   "landing": "move to landing", "deferred": "defer",
                                   "closed": "close"})

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

    def test_approve_names_the_revision_and_plan_it_approves(self):
        labels = self.probe["labels"]
        self.assertEqual(labels["approve"], "approve revision 4 and plan 6")
        self.assertEqual(labels["approve_none"], "")
        self.assertEqual(labels["plain"], "defer")

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
        self.assertEqual(texts["landed"], {"text": "Approve: done.", "warn": False})
        self.assertEqual(self.probe["note_landed_text"], "The note: done.")
        for key, text in texts.items():
            with self.subTest(outcome=key):
                self.assertEqual(text["warn"], key not in ("landed", "found"))
        self.assertIn("this is the task as it is now", texts["stale"]["text"])
        # Found after its answer was lost: said so, not passed off as a plain landing.
        self.assertEqual(texts["found"]["text"],
                         "Approve: no answer came, but the task shows it landed.")
        self.assertIn("cannot apply it twice", texts["not-landed"]["text"])
        self.assertIn("before sending it again", texts["not-found-yet"]["text"])
        self.assertNotIn("cannot apply it twice", texts["not-found-yet"]["text"])
        self.assertIn("not known", texts["could-not-check"]["text"])
        # A refusal's reason is the registry's, spelled like any other text.
        self.assertEqual(texts["refused"]["text"], "Approve: refused. no\\u202e")


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
        self.assertEqual(len(posts), 3)
        routes = [rx for method, rx in tui._RELAY_ROUTES if method == "POST"]
        for path in posts:
            with self.subTest(path=path):
                self.assertTrue(tui._relay_allowed("POST", path), path)
        for rx in routes:
            with self.subTest(route=rx.pattern):
                self.assertTrue(any(rx.match(p) for p in posts), rx.pattern)
        # Every fetch in moves.js is one of them; tasks.js posts nothing.
        self.assertEqual(len(re.findall(r"\bfetch\(", self.MOVES)), 3)
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
        self.assertIn("markWriting();", self._function(self.TASKS, "drawTask"))

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
        stale = stale[:stale.index("} else {")]
        self.assertIn("answer.body.current", stale)
        self.assertNotIn("readTask(", stale)
        # The notes the page holds, since the task's own are what agents read.
        self.assertIn("notes: shownNotes", stale)

    def test_a_lost_answer_is_decided_from_a_read_and_never_resent(self):
        body = self._function(self.TASKS, "runWrite")
        self.assertIn('if (result === "unknown") result = checkWrite(write, drawn);', body)
        self.assertEqual(body.count("sendWrite("), 1)

    def test_its_outcome_is_drawn_only_while_its_task_is_open(self):
        body = self._function(self.TASKS, "runWrite")
        self.assertIn("if (openTask === write.task) {", body)
        self.assertLess(body.index("if (openTask === write.task) {"),
                        body.index("drawTask("))
        self.assertIn("if (result !== \"not-running\") {", body)

    def test_a_note_is_kept_until_it_lands(self):
        body = self._function(self.TASKS, "runWrite")
        self.assertIn('const landed = result === "landed" || result === "found";', body)
        self.assertIn('write.kind === "note" && landed', body)
        form = self._function(self.TASKS, "noteForm")
        self.assertIn("noteDraft.task === String(task.id) ? noteDraft.text", form)

    def test_no_move_is_drawn_in_an_inbox_row(self):
        for name in ("inboxRow", "renderInbox", "renderTasks"):
            with self.subTest(function=name):
                self.assertNotIn("writeButton(", self._function(self.TASKS, name))
                self.assertNotIn("data-write", self._function(self.TASKS, name))
        calls = re.findall(r"\bmovesBlock\(", self.TASKS)
        self.assertEqual(len(calls), 2, "defined once, called once, in drawTask")
        self.assertIn("movesBlock(task)", self._function(self.TASKS, "drawTask"))


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
