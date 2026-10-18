# SPDX-License-Identifier: Apache-2.0
"""The operator's side of the task registry: the paged reads, the bulk hide and handle
and the clear in ``task-registry/operator_moves.py``, and the HTTP API over them in
``task-registry/operator_api.py``: the routes it serves, what each answers, and how a
refusal becomes a status. Each test gets a store of its own."""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

import _surface
from _loader import ROOT, load_operator_api, load_task_registry
from test_task_registry_plans import DECISION, MULTI, PLAN

reg = load_task_registry()
api = load_operator_api()
Refused, Missing, Stale = reg.taskdb.Refused, reg.taskdb.Missing, reg.taskdb.Stale
agent, op, reads, plans = reg.agent_moves, reg.operator_moves, reg.reads, reg.plans

#: Answers to ``[DECISION, MULTI]`` that are the options the plan assumes.
ASSUMED = [{"options": [0]}, {"options": [0, 1]}]
#: Answers to ``[DECISION, MULTI]`` that are not, each with the state they lead to.
NOT_ASSUMED = {
    "another option": [{"options": [1]}, ASSUMED[1]],
    "an other text alone": [{"other": "Hold, but only for the llm containers."},
                            ASSUMED[1]],
    "fewer of a multi-select": [ASSUMED[0], {"options": [0]}],
    "more of a multi-select": [ASSUMED[0], {"options": [0, 1, 2]}],
    "the assumed ones and an other text": [ASSUMED[0], {"options": [0, 1],
                                                        "other": "And debug, weekly."}],
    "an other text on a multi-select": [ASSUMED[0], {"other": "None of them."}],
}

ALICE = "claude-0b6f3a52-91d4-4c1e-a7e2-5d0c8e2f4b19"


class ThroughTheRelay:
    """A request as the operator listener sees one the tasks UI relayed: from the UI's
    address, with the browser's asserted, and what the browser sent of itself."""

    SENT: ClassVar[dict] = {"x-dockade-actor": "172.24.0.1",
                            "origin": "http://127.0.0.1:8091", "user-agent": "Mozilla/5.0"}
    #: What ``operator_api.actor`` makes of ``SENT`` from the default peer.
    ACTOR = ('peer=172.26.0.4 via-ui=172.24.0.1 origin=http://127.0.0.1:8091 '
             'ua="Mozilla/5.0"')

    def __init__(self, peer: str | None = "172.26.0.4", headers: dict | None = None):
        self.client = types.SimpleNamespace(host=peer) if peer else None
        self.headers = self.SENT if headers is None else headers


class _Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-operator-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        previous = reg.taskdb.DB_PATH
        self.addCleanup(setattr, reg.taskdb, "DB_PATH", previous)
        reg.taskdb.DB_PATH = os.path.join(self.dir, "tasks.db")
        reg.taskdb.init()
        op.add_repo("widgets")
        self.task = op.create("widgets", "A task", "What to do.", "open")["task"]

    def note(self, text="Seen this.", **kw):
        return agent.add_note(ALICE, "widgets", self.task, text, **kw)["note"]

    def revision(self, task=None):
        return reads.get_task(task or self.task)["revision"]

    def refused(self, fn, *args, **kw) -> Refused:
        with self.assertRaises(Refused) as caught:
            fn(*args, **kw)
        return caught.exception


class PagingTests(_Store):
    def test_a_tasks_notes_come_a_page_at_a_time_hidden_ones_too(self):
        made = [self.note(f"Note {i}") for i in range(op.PAGE + 3)]
        op.hide_note(made[-1])
        first = op.all_notes(self.task)
        self.assertEqual(first["total"], op.PAGE + 3)
        self.assertEqual([n["id"] for n in first["notes"]], made[3:])
        self.assertTrue(first["notes"][-1]["hidden"])
        seen, page = [], first["notes"]
        for _ in range(10):
            if not page:
                break
            seen = [n["id"] for n in page] + seen
            page = op.all_notes(self.task, before=page[0]["id"])["notes"]
        self.assertEqual(seen, made)
        for bad in ("1", 0, True):
            with self.subTest(before=bad):
                self.refused(op.all_notes, self.task, before=bad)

    def test_a_tasks_history_comes_a_page_at_a_time(self):
        for i in range(op.PAGE + 3):
            self.note(f"Note {i}")
        first = op.history(self.task)
        self.assertEqual(first["total"], op.PAGE + 4)
        self.assertEqual([h["detail"]["note"] for h in first["history"]],
                         [f"Note {i}" for i in range(3, op.PAGE + 3)])
        seen, page = [], first["history"]
        for _ in range(10):
            if not page:
                break
            seen = page + seen
            page = op.history(self.task, before=page[0]["id"])["history"]
        self.assertEqual([h["action"] for h in seen], ["create"] + ["note"] * (op.PAGE + 3))
        self.assertEqual([h["id"] for h in seen], sorted(h["id"] for h in seen))
        for bad in ("1", 0, True):
            with self.subTest(before=bad):
                self.refused(op.history, self.task, before=bad)


class TimelineTests(_Store):
    """The history read carries each note as the entry of the write that posted it, so
    the tasks UI reads one timeline (``tasks-ui/DESIGN.md``)."""

    def posted(self, task=None):
        return [(h["action"], h["note"] and h["note"]["text"])
                for h in op.history(task or self.task)["history"]]

    def test_every_note_is_one_entrys_note_as_it_is_now(self):
        task = agent.propose(ALICE, "widgets", "T", "B")["task"]
        token = agent.claim_plan(ALICE, "widgets", task, ["src"])["token"]
        agent.submit_plan(ALICE, "widgets", task, token, **PLAN, note="Planned.")
        op.move(task, self.revision(task), "open")
        token = agent.claim_work(ALICE, "widgets", task, "fix")["token"]
        agent.send_back(ALICE, "widgets", task, token, "Unclear.")
        token = agent.claim_plan(ALICE, "widgets", task, ["src"])["token"]
        agent.release(ALICE, "widgets", task, token, "Gave up.")
        token = agent.claim_plan(ALICE, "widgets", task, ["src"])["token"]
        agent.submit_plan(ALICE, "widgets", task, token, **PLAN)
        op.move(task, self.revision(task), "open")
        token = agent.claim_work(ALICE, "widgets", task, "fix")["token"]
        agent.submit_work(ALICE, "widgets", task, token, "Done.")
        flagged = agent.add_note(ALICE, "widgets", task, "Land it?", for_operator=True)
        op.add_note(task, "Noted.")
        op.handle_note(flagged["note"], reply="Yes.")
        op.hide_note(flagged["note"])
        self.assertEqual(self.posted(task), [
            ("propose", None), ("claim_plan", None), ("submit_plan", "Planned."),
            ("move", None), ("claim_work", None), ("send_back", "Unclear."),
            ("claim_plan", None), ("release", "Gave up."), ("claim_plan", None),
            ("submit_plan", None), ("move", None), ("claim_work", None),
            ("submit_work", "Done."), ("note", "Land it?"), ("note", "Noted."),
            ("handle_note", "Yes."), ("hide_note", None)])
        entries = op.history(task)["history"]
        asked = next(h["note"] for h in entries if h["action"] == "note")
        self.assertEqual((asked["id"], asked["hidden"], asked["handled"]),
                         (flagged["note"], True, True))
        reply = entries[-2]["note"]
        self.assertEqual((reply["reply_to"], reply["author"], reply["hidden"]),
                         (flagged["note"], reg.taskdb.OPERATOR, False))
        every = op.all_notes(task)["notes"]
        self.assertEqual(sorted(h["note"]["id"] for h in entries if h["note"]),
                         [n["id"] for n in every])

    def test_a_handle_without_a_reply_and_a_hide_carry_none(self):
        note = self.note(for_operator=True)
        op.handle_notes([note])
        op.hide_notes([note])
        self.assertEqual(self.posted(), [("create", None), ("note", "Seen this."),
                                         ("handle_note", None), ("hide_note", None)])

    def test_paging_keeps_each_note_on_its_own_entry(self):
        made = [self.note(f"Note {i}") for i in range(op.PAGE + 3)]
        first = op.history(self.task)
        older = op.history(self.task, before=first["history"][0]["id"])
        self.assertEqual(first["total"], op.PAGE + 4)
        seen = older["history"] + first["history"]
        self.assertEqual([h["note"] and h["note"]["id"] for h in seen], [None, *made])
        self.assertEqual([h["note"]["text"] for h in seen[1:]],
                         [h["detail"]["note"] for h in seen[1:]])

    def test_every_post_is_recorded_beside_the_id_it_returns(self):
        """What the history read relies on: a ``notes.post`` whose id no history row
        names would be a note missing from the timeline."""
        found = 0
        for path in sorted((ROOT / "task-registry").glob("*.py")):
            for fn in ast.walk(ast.parse(path.read_text())):
                if (not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                        or path.name == "notes.py"):
                    continue
                for node in ast.walk(fn):
                    if not (isinstance(node, ast.Call) and _calls(node, "notes", "post")):
                        continue
                    found += 1
                    named = _assigned(fn, node)
                    with self.subTest(file=path.name, function=fn.name):
                        self.assertIsNotNone(named)
                        self.assertTrue(any(
                            _calls(call, "taskdb", "record") and any(
                                kw.arg == _posted_key(call)
                                and isinstance(kw.value, ast.Name) and kw.value.id == named
                                for kw in call.keywords)
                            for call in ast.walk(fn) if isinstance(call, ast.Call)))
        self.assertEqual(found, 5)


def _calls(call: ast.Call, module: str, name: str) -> bool:
    return (isinstance(call.func, ast.Attribute) and call.func.attr == name
            and isinstance(call.func.value, ast.Name) and call.func.value.id == module)


def _posted_key(record: ast.Call) -> str:
    """The key `operator_moves._posted` reads a record's note from, by its action."""
    action = record.args[2] if len(record.args) > 2 else None
    handle = isinstance(action, ast.Constant) and action.value == "handle_note"
    return "reply_id" if handle else "note_id"


def _assigned(fn: ast.FunctionDef, call: ast.Call) -> str | None:
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and any(inner is call for inner in ast.walk(node.value))):
            return node.targets[0].id
    return None


class BulkTests(_Store):
    def test_a_bulk_hide_takes_every_note_or_none(self):
        first, second = self.note("One."), self.note("Two.")
        self.assertIsInstance(self.refused(op.hide_notes, [first, 999]), Missing)
        self.assertEqual(reads.get_task(self.task)["notes_total"], 2)
        self.assertEqual(op.hide_notes([first, second, first]), {"notes": [first, second]})
        self.assertEqual(reads.get_task(self.task)["notes_total"], 0)
        hides = [h for h in op.history(self.task)["history"] if h["action"] == "hide_note"]
        self.assertEqual([(h["author"], h["detail"]["note_id"]) for h in hides],
                         [(reg.taskdb.OPERATOR, first), (reg.taskdb.OPERATOR, second)])
        op.hide_notes([first, second], hidden=False)
        self.assertEqual(reads.get_task(self.task)["notes_total"], 2)

    def test_a_bulk_handle_takes_flagged_notes_only_and_every_one_or_none(self):
        flagged, plain = self.note("Q1", for_operator=True), self.note("FYI")
        other = self.note("Q2", for_operator=True)
        refusal = self.refused(op.handle_notes, [flagged, plain])
        self.assertIn("not marked for the operator", refusal.reason)
        self.assertEqual(op.notes_for_operator()["total"], 2)
        self.assertEqual(op.handle_notes([flagged, other]), {"notes": [flagged, other]})
        self.assertEqual(op.notes_for_operator()["total"], 0)
        last = op.history(self.task)["history"][-1]
        self.assertEqual((last["action"], last["detail"]),
                         ("handle_note", {"note_id": other, "handled": True,
                                          "reply": None, "reply_id": None,
                                          "actor": None}))
        op.handle_notes([flagged], handled=False)
        self.assertEqual(op.notes_for_operator()["total"], 1)

    def test_a_bulk_names_1_to_bulk_max_note_ids(self):
        note = self.note(for_operator=True)
        for fn in (op.hide_notes, op.handle_notes):
            for bad in ([], [note] * (op.BULK_MAX + 1), ["1"], [True], [[note]], note,
                        "1,2", None):
                with self.subTest(fn=fn.__name__, notes=bad):
                    self.refused(fn, bad)
        self.refused(op.hide_notes, [note], hidden="yes")
        self.refused(op.handle_notes, [note], handled=1)
        self.assertEqual(op.notes_for_operator()["total"], 1)
        self.assertEqual(op.hide_notes([note] * op.BULK_MAX), {"notes": [note]})


class ClearTests(_Store):
    def test_the_operator_empties_a_severity_or_a_kind(self):
        task = op.create("widgets", "T", "B", "open", severity="high",
                         kind="finding")["task"]
        revision = op.edit(task, 1, clear=["severity"])["revision"]
        got = reads.get_task(task)
        self.assertEqual((got["severity"], got["kind"], got["revision"]),
                         (None, "finding", 2))
        self.assertEqual(op.history(task)["history"][-1]["detail"],
                         {"revision": 2, "blocked_by": None, "severity": None,
                          "actor": None})
        op.edit(task, revision, title="T2", clear=("kind", "severity"))
        got = reads.get_task(task)
        self.assertEqual((got["title"], got["severity"], got["kind"]), ("T2", None, None))

    def test_clear_names_only_those_fields_and_none_being_set(self):
        for bad in (["title"], ["parent_id"], "severity", None, [1], [["severity"]]):
            with self.subTest(clear=bad):
                self.refused(op.edit, self.task, 1, clear=bad)
        refusal = self.refused(op.edit, self.task, 1, severity="low", clear=["severity"])
        self.assertIn("both set and cleared", refusal.reason)
        self.assertEqual(self.revision(), 1)


class RefusalKindTests(_Store):
    def test_a_missing_task_or_note_and_a_stale_revision_are_told_apart(self):
        self.assertIsInstance(self.refused(op.edit, 999, 1, title="T"), Missing)
        self.assertIsInstance(self.refused(reads.get_task, 999), Missing)
        self.assertIsInstance(self.refused(op.handle_note, 999), Missing)
        stale = self.refused(op.move, self.task, 7, "deferred")
        self.assertIsInstance(stale, Stale)
        self.assertEqual(stale.current["revision"], 1)
        plain = self.refused(op.move, self.task, 1, "nowhere")
        self.assertNotIsInstance(plain, (Missing, Stale))

    def test_an_unknown_repo_is_missing_and_a_retired_one_refused(self):
        unknown = self.refused(op.create, "gizmos", "T", "B", "open")
        self.assertIsInstance(unknown, Missing)
        self.assertEqual(unknown.current, {"repos": ["widgets"]})
        self.assertIsInstance(self.refused(op.rename_repo, "gizmos", "gadgets"), Missing)
        op.retire_repo("widgets")
        retired = self.refused(op.create, "widgets", "T", "B", "open")
        self.assertNotIsInstance(retired, (Missing, Stale))

    def test_a_value_of_the_wrong_type_is_refused_not_raised(self):
        calls = {
            "blocked_by=7": lambda: op.create("widgets", "T", "B", "open", blocked_by=7),
            "create title=None": lambda: op.create("widgets", None, "B", "open"),
            "create body=None": lambda: op.create("widgets", "T", None, "open"),
            "propose title=None": lambda: agent.propose(ALICE, "widgets", None, "B"),
            "propose body=None": lambda: agent.propose(ALICE, "widgets", "T", None),
            "repo name 7": lambda: op.add_repo(7),
            "remote 7": lambda: op.add_repo("gadgets", remote=7),
            "new name 7": lambda: op.rename_repo("widgets", 7),
            "remote a list": lambda: op.set_remote("widgets", ["github.com/a/w"]),
        }
        for label, call in calls.items():
            with self.subTest(call=label):
                self.refused(call)
        self.assertEqual([r["name"] for r in reads.repos()], ["widgets"])
        self.assertEqual(reads.list_tasks("widgets", None)["total"], 1)


class ApproveTests(_Store):
    """``operator_moves.approve``: answering a plan's decisions is approving it, and
    whether the answers are the ones the plan assumed decides where the task goes."""

    def refined(self, decisions=(DECISION, MULTI), who=ALICE):
        task = agent.propose(who, "widgets", "Rotate keys", "Every 90 days.")["task"]
        self.replan(task, decisions, who)
        return task

    def replan(self, task, decisions=(DECISION, MULTI), who=ALICE):
        token = agent.claim_plan(who, "widgets", task, ["."])["token"]
        return agent.submit_plan(who, "widgets", task, token,
                                 **(PLAN | {"decisions": list(decisions)}))["plan"]

    def approve(self, task, answers, **kw):
        return op.approve(task, self.revision(task), answers, **kw)

    def test_every_answer_assumed_opens_the_task(self):
        task = self.refined()
        plan = reads.get_task(task)["plan"]["id"]
        reply = self.approve(task, ASSUMED)
        got = reads.get_task(task)
        self.assertEqual((reply["state"], reply["plan"], reply["revision"]),
                         ("open", plan, got["revision"]))
        self.assertEqual((got["state"], got["plan"]["id"], got["plan"]["superseded_at"]),
                         ("open", plan, None))
        self.assertEqual([(a["plan"], a["decision"], a["question"], a["author"])
                          for a in got["answers"]],
                         [(plan, 0, DECISION["question"], reg.taskdb.OPERATOR),
                          (plan, 1, MULTI["question"], reg.taskdb.OPERATOR)])
        self.assertEqual([[o["label"] for o in a["options"]] for a in got["answers"]],
                         [["Hold"], ["Audit", "Access"]])
        self.assertEqual([a["other"] for a in got["answers"]], [None, None])
        last = op.history(task)["history"][-1]
        self.assertEqual((last["author"], last["action"], last["detail"]["state"],
                          last["detail"]["before"], last["detail"]["plan"]),
                         (reg.taskdb.OPERATOR, "approve", "open", "refined", plan))
        self.assertEqual(last["detail"]["answers"], [
            {"question": DECISION["question"], "options": [DECISION["options"][0]],
             "other": None},
            {"question": MULTI["question"], "options": MULTI["options"][:2],
             "other": None}])

    def test_any_other_answer_sends_it_back_with_its_plan_superseded(self):
        for case, answers in NOT_ASSUMED.items():
            with self.subTest(case):
                task = self.refined()
                plan = reads.get_task(task)["plan"]["id"]
                self.assertEqual(self.approve(task, answers)["state"], "proposed")
                got = reads.get_task(task)
                self.assertEqual((got["state"], got["plan"]["id"]), ("proposed", plan))
                self.assertIsNotNone(got["plan"]["superseded_at"])
                self.assertEqual([([o["position"] for o in a["options"]], a["other"])
                                  for a in got["answers"]],
                                 [(sorted(a.get("options", [])), a.get("other"))
                                  for a in answers])
                detail = op.history(task)["history"][-1]["detail"]
                self.assertEqual(detail["state"], "proposed")
                self.assertEqual([a["other"] for a in detail["answers"]],
                                 [a.get("other") for a in answers])

    def test_the_outcome_is_a_rule_another_surface_can_run(self):
        decisions = [plans.check_decisions([d])[0] for d in (DECISION, MULTI)]
        self.assertEqual(plans.outcome(decisions, ASSUMED), "open")
        for case, answers in NOT_ASSUMED.items():
            with self.subTest(case):
                self.assertEqual(plans.outcome(decisions, answers), "proposed")
        self.assertEqual(plans.outcome([], []), "open")
        self.refused(plans.outcome, decisions, ASSUMED[:1])

    def test_each_rule_of_an_answer_is_refused_at_its_edge(self):
        task = self.refined()
        cases = {
            "no answers": [],
            "one too few": ASSUMED[:1],
            "one too many": [*ASSUMED, ASSUMED[0]],
            "not a list": ASSUMED[0],
            "an answer not an object": [0, ASSUMED[1]],
            "an unknown field": [{"options": [0], "why": "x"}, ASSUMED[1]],
            "options not a list": [{"options": 0}, ASSUMED[1]],
            "a position past the options": [{"options": [2]}, ASSUMED[1]],
            "a negative position": [{"options": [-1]}, ASSUMED[1]],
            "a flag for a position": [{"options": [True]}, ASSUMED[1]],
            "a label for a position": [{"options": ["Hold"]}, ASSUMED[1]],
            "an option twice": [ASSUMED[0], {"options": [0, 0, 1]}],
            "a blank other": [{"other": "  "}, ASSUMED[1]],
            "an other past its cap": [{"other": "x" * (plans.OTHER_MAX + 1)}, ASSUMED[1]],
            "an option and an other on a single choice": [
                {"options": [0], "other": "Hold, mostly."}, ASSUMED[1]],
            "nothing on a single choice": [{}, ASSUMED[1]],
            "nothing on a multi-select": [ASSUMED[0], {"options": []}],
        }
        for case, answers in cases.items():
            with self.subTest(case):
                self.refused(self.approve, task, answers)
        got = reads.get_task(task)
        self.assertEqual((got["state"], got["answers"]), ("refined", []))
        self.assertIsInstance(self.refused(op.approve, task, got["revision"] - 1, ASSUMED),
                              Stale)
        reply = self.approve(task, [{"other": "x" * plans.OTHER_MAX}, ASSUMED[1]])
        self.assertEqual(reply["state"], "proposed")

    def test_only_a_refined_task_whose_plan_is_unanswered_is_approved(self):
        proposed = agent.propose(ALICE, "widgets", "P", "B")["task"]
        for task in (proposed, self.task):
            with self.subTest(state=reads.get_task(task)["state"]):
                self.assertIn("approve takes a refined task",
                              self.refused(self.approve, task, []).reason)
        task = self.refined()
        self.approve(task, ASSUMED)
        op.move(task, self.revision(task), "refined")
        refusal = self.refused(self.approve, task, ASSUMED)
        self.assertIn("answered already", refusal.reason)
        # It reopens by a move, its answers standing.
        op.move(task, self.revision(task), "open")
        self.assertEqual(len(reads.get_task(task)["answers"]), 2)

    def test_a_plan_without_decisions_opens_with_no_answers(self):
        task = self.refined(decisions=())
        self.refused(self.approve, task, ASSUMED[:1])
        self.assertEqual(self.approve(task, [])["state"], "open")
        self.assertEqual(reads.get_task(task)["answers"], [])
        self.assertEqual(op.history(task)["history"][-1]["detail"]["answers"], [])
        # Approved before and moved back: approved again, since it has no answers.
        op.move(task, self.revision(task), "refined")
        self.assertEqual(self.approve(task, [])["state"], "open")
        # And re-refined after being moved back to proposed.
        op.move(task, self.revision(task), "proposed")
        self.replan(task, decisions=())
        self.assertEqual(op.approve(task, self.revision(task))["state"], "open")

    def test_the_answers_reach_the_next_refiner_and_the_implementer(self):
        task = self.refined()
        first = reads.get_task(task)["plan"]["id"]
        sent = [{"other": "Deny, but log each one."}, ASSUMED[1]]
        self.approve(task, sent)
        second = self.replan(task, decisions=[MULTI],
                             who="claude-7e21c9d0-3a5b-4f86-b0d4-c1e9a2f7d360")
        got = reads.get_task(task)
        self.assertEqual(got["plan"]["id"], second)
        self.assertEqual([(a["plan"], a["other"]) for a in got["answers"]],
                         [(first, "Deny, but log each one."), (first, None)])
        self.approve(task, [ASSUMED[1]])
        got = reads.get_task(task)
        self.assertEqual(got["state"], "open")
        self.assertEqual([(a["plan"], a["decision"], a["other"]) for a in got["answers"]],
                         [(second, 0, None), (first, 0, "Deny, but log each one."),
                          (first, 1, None)])
        # The operator's read of the task is the same read.
        self.assertEqual(RouteTests.call(self, api.get_task, task)[1], got)

    def test_no_state_opens_a_task_with_unanswered_decisions_by_move(self):
        task = self.refined()
        refusal = self.refused(op.move, task, self.revision(task), "open")
        self.assertIn("only the operator's approve opens it, from refined", refusal.reason)
        self.assertEqual(refusal.current, {"state": "refined"})
        for state in ("deferred", "landing", "closed"):
            with self.subTest(state=state):
                op.move(task, self.revision(task), state)
                refusal = self.refused(op.move, task, self.revision(task), "open")
                self.assertIn("the operator moves it back to refined first", refusal.reason)
                self.assertEqual(reads.get_task(task)["state"], state)
        op.move(task, self.revision(task), "refined")
        self.approve(task, ASSUMED)
        op.move(task, self.revision(task), "deferred")
        op.move(task, self.revision(task), "open")
        self.assertEqual(reads.get_task(task)["state"], "open")

    def test_a_plan_without_decisions_still_opens_by_move(self):
        task = self.refined(decisions=())
        op.move(task, self.revision(task), "open")
        self.assertEqual(reads.get_task(task)["state"], "open")

    def test_the_route_records_the_actor_and_answers_the_state_reached(self):
        task = self.refined()
        answers = (api.Answer(options=(0,)), api.Answer(options=(0, 1)))
        status, body = RouteTests.call(self, api.approve, task, api.Approve(
            revision=self.revision(task), answers=answers))
        self.assertEqual((status, body["ok"], body["state"]), (200, True, "open"))
        self.assertEqual(op.history(task)["history"][-1]["detail"]["actor"],
                         ThroughTheRelay.ACTOR)
        other = self.refined()
        status, body = RouteTests.call(self, api.approve, other, api.Approve(
            revision=self.revision(other), answers=(api.Answer(other="Neither."),
                                                    api.Answer(options=(2,)))))
        self.assertEqual((status, body["state"]), (200, "proposed"))
        third = self.refined()
        for status, req in ((409, api.Approve(revision=1, answers=())),
                            (400, api.Approve(revision=self.revision(third),
                                              answers=(api.Answer(),)))):
            with self.subTest(status=status):
                got, body = RouteTests.call(self, api.approve, third, req)
                self.assertEqual((got, body["ok"]), (status, False))
        self.assertEqual(RouteTests.call(self, api.approve, 999, api.Approve(
            revision=1))[0], 404)


class RouteTests(_Store):
    ROUTES = frozenset({
        ("GET", "/api/repos"), ("POST", "/api/repos"),
        ("POST", "/api/repos/{name}/rename"), ("POST", "/api/repos/{name}/remote"),
        ("POST", "/api/repos/{name}/retire"),
        ("GET", "/api/tasks"), ("POST", "/api/tasks"), ("GET", "/api/search"),
        ("GET", "/api/tasks/{task}"), ("POST", "/api/tasks/{task}/edit"),
        ("POST", "/api/tasks/{task}/move"), ("POST", "/api/tasks/{task}/approve"),
        ("POST", "/api/tasks/{task}/break-claim"),
        ("POST", "/api/tasks/{task}/reparent"), ("GET", "/api/tasks/{task}/history"),
        ("GET", "/api/tasks/{task}/plans"),
        ("GET", "/api/tasks/{task}/notes"), ("POST", "/api/tasks/{task}/notes"),
        ("GET", "/api/notes/flagged"), ("POST", "/api/notes/hide"),
        ("POST", "/api/notes/handle"), ("POST", "/api/notes/{note}/handle"),
        ("GET", "/api/inbox"),
    })

    def call(self, route, *args, request=None):
        # A write takes the request, for whom to record; a read takes none.
        if "request" in inspect.signature(route).parameters:
            args = (*args, request or ThroughTheRelay())
        response = route(*args)
        json.dumps(response.body)
        return response.status_code, response.body

    def test_the_app_serves_exactly_these_routes(self):
        self.assertEqual(len(api.app.routes), len(set(api.app.routes)))
        self.assertEqual(set(api.app.routes), self.ROUTES)

    def test_every_operator_move_and_read_is_served(self):
        served = set(re.findall(r"operator_moves\.(\w+)", inspect.getsource(api)))
        public = {name for name, fn in inspect.getmembers(op, inspect.isfunction)
                  if not name.startswith("_") and fn.__module__ == op.__name__}
        # One note is hidden through the bulk route, as a list of one.
        self.assertEqual(public - served, {"hide_note"},
                         "a new operator move must be served here, or named here")

    def test_a_write_answers_ok_with_what_the_move_returned(self):
        status, body = self.call(api.create_task, api.CreateTask(
            repo="widgets", title="T", body="B", state="proposed", severity="low"))
        self.assertEqual((status, body), (200, {"ok": True, "task": body["task"],
                                                "revision": 1}))
        self.assertEqual(reads.get_task(body["task"])["severity"], "low")
        self.assertEqual(self.call(api.retire_repo, "widgets", api.RetireRepo()),
                         (200, {"ok": True}))

    def test_each_task_route_reaches_its_move(self):
        other = op.create("widgets", "Parent", "B", "open")["task"]
        _, body = self.call(api.edit_task, self.task, api.EditTask(
            revision=1, title="Renamed", blocked_by=(other,)))
        self.assertEqual(body, {"ok": True, "task": self.task, "revision": 2})
        self.assertEqual(reads.get_task(self.task)["blockers"][0]["id"], other)
        self.call(api.edit_task, self.task, api.EditTask(revision=2, blocked_by=()))
        token = agent.claim_work(ALICE, "widgets", self.task, "fix", ["."])["token"]
        self.assertTrue(token)
        _, body = self.call(api.break_claim, self.task, api.AtRevision(revision=4))
        self.assertEqual((body["revision"], reads.get_task(self.task)["claim"]), (5, None))
        self.call(api.reparent, self.task, api.Reparent(revision=5, parent=other))
        self.assertEqual(reads.get_task(self.task)["parent"]["id"], other)
        self.call(api.reparent, self.task, api.Reparent(revision=6, parent=None))
        self.call(api.move_task, self.task, api.MoveTask(revision=7, state="deferred"))
        got = reads.get_task(self.task)
        self.assertEqual((got["title"], got["parent"], got["state"]),
                         ("Renamed", None, "deferred"))
        op.edit(self.task, 8, severity="high")
        self.call(api.edit_task, self.task, api.EditTask(revision=9, clear=("severity",)))
        self.assertIsNone(reads.get_task(self.task)["severity"])

    def test_each_note_route_reaches_its_move(self):
        _, body = self.call(api.add_note, self.task, api.NoteText(text="From the operator."))
        mine = body["note"]
        question = self.note("Which one?", for_operator=True)
        another = self.note("And this?", for_operator=True)
        _, body = self.call(api.handle_note, question, api.HandleNote(reply="The first."))
        self.assertEqual((body["ok"], body["note"]), (True, question))
        self.assertEqual(reads.get_task(self.task)["notes"][-1]["reply_to"], question)
        self.assertEqual(self.call(api.handle_notes, api.HandleNotes(notes=(another,))),
                         (200, {"ok": True, "notes": [another]}))
        self.assertEqual(op.notes_for_operator()["total"], 0)
        self.call(api.handle_notes, api.HandleNotes(notes=(another,), handled=False))
        self.assertEqual(op.notes_for_operator()["total"], 1)
        self.assertEqual(self.call(api.hide_notes, api.HideNotes(notes=(mine,))),
                         (200, {"ok": True, "notes": [mine]}))
        self.call(api.hide_notes, api.HideNotes(notes=(question,), hidden=True))
        self.call(api.hide_notes, api.HideNotes(notes=(mine,), hidden=False))
        _, shown = self.call(api.all_notes, self.task)
        self.assertEqual([(n["id"], n["hidden"]) for n in shown["notes"]][:3],
                         [(mine, False), (question, True), (another, False)])

    def test_each_repo_route_reaches_its_move(self):
        self.call(api.add_repo, api.AddRepo(name="gadgets", remote="github.com/a/g"))
        self.assertEqual(reads.repos()[0]["remote"], "github.com/a/g")
        self.call(api.rename_repo, "gadgets", api.RenameRepo(new_name="gizmos"))
        self.call(api.set_remote, "gizmos", api.SetRemote(remote=None))
        self.call(api.retire_repo, "gizmos", api.RetireRepo(retired=True))
        _, got = self.call(api.repos)
        self.assertEqual(got, [{"name": "gizmos", "remote": None, "retired": True},
                               {"name": "widgets", "remote": None, "retired": False}])
        self.call(api.set_remote, "widgets", api.SetRemote(remote="github.com/a/w"))
        self.call(api.retire_repo, "gizmos", api.RetireRepo(retired=False))
        self.assertEqual([(r["remote"], r["retired"]) for r in reads.repos()],
                         [(None, False), ("github.com/a/w", False)])

    def test_reads_pass_through_with_their_arguments(self):
        op.create("widgets", "Later", "B", "deferred")
        _, open_only = self.call(api.list_tasks, "widgets")
        _, every = self.call(api.list_tasks, "widgets", api.EVERY_STATE)
        self.assertEqual((open_only, every), (reads.list_tasks("widgets"),
                                              reads.list_tasks("widgets", None)))
        self.assertNotEqual(open_only, every)
        newest = every["tasks"][0]["id"]
        _, older = self.call(api.list_tasks, "widgets", api.EVERY_STATE, None, None, newest)
        self.assertEqual(older, reads.list_tasks("widgets", None, before=newest))
        self.assertEqual(older["total"], every["total"])
        self.assertNotIn(newest, [t["id"] for t in older["tasks"]])
        self.assertEqual(self.call(api.list_tasks, "widgets", "deferred", None, None)[1],
                         reads.list_tasks("widgets", "deferred"))
        child = op.create("widgets", "Child", "B", "open", parent=self.task)["task"]
        _, children = self.call(api.list_tasks, "widgets", "open", None, self.task)
        self.assertEqual(children, reads.list_tasks("widgets", parent=self.task))
        self.assertIn(f"'id': {child}", repr(children))
        self.assertNotIn(f"'id': {self.task},", repr(children))
        op.add_repo("gadgets")
        op.create("gadgets", "Later too", "B", "open")
        _, found = self.call(api.search_tasks, "Later", "widgets")
        self.assertEqual(found, reads.search_tasks("Later", "widgets"))
        self.assertEqual(found["total"], 1)
        self.assertEqual(self.call(api.get_task, self.task)[1], reads.get_task(self.task))
        notes = [self.note(f"N{i}", for_operator=True) for i in range(3)]
        op.handle_note(notes[0])
        self.assertEqual(self.call(api.all_notes, self.task, notes[2])[1],
                         op.all_notes(self.task, before=notes[2]))
        self.assertEqual(self.call(api.history, self.task, 3)[1],
                         op.history(self.task, before=3))
        self.assertEqual(self.call(api.notes_for_operator, "widgets", True, None)[1],
                         op.notes_for_operator("widgets", handled=True))
        self.assertEqual(self.call(api.notes_for_operator, None, False, notes[2])[1],
                         op.notes_for_operator(before=notes[2]))
        self.assertEqual(self.call(api.inbox)[1], op.inbox())
        self.assertEqual(op.inbox()["waiting"]["total"], 1)

    def test_a_refusal_answers_its_reason_and_what_is_there_now(self):
        cases = [
            (404, api.get_task, (999,)),
            (404, api.history, (999,)),
            (404, api.all_notes, (999,)),
            (404, api.edit_task, (999, api.EditTask(revision=1, title="T"))),
            (404, api.add_note, (999, api.NoteText(text="Hi."))),
            (404, api.handle_note, (999, api.HandleNote())),
            (404, api.hide_notes, (api.HideNotes(notes=(999,)),)),
            (409, api.move_task, (self.task, api.MoveTask(revision=7, state="closed"))),
            (409, api.break_claim, (self.task, api.AtRevision(revision=7))),
            (400, api.move_task, (self.task, api.MoveTask(revision=1, state="nowhere"))),
            (400, api.break_claim, (self.task, api.AtRevision(revision=1))),
            (400, api.handle_notes, (api.HandleNotes(notes=()),)),
            (400, api.notes_for_operator, (None, False, "1")),
            (404, api.list_tasks, ("gizmos",)),
            (404, api.create_task, (api.CreateTask(repo="gizmos", title="T", body="B",
                                                   state="open"),)),
            (404, api.rename_repo, ("gizmos", api.RenameRepo(new_name="gadgets"))),
            (400, api.create_task, (api.CreateTask(repo="widgets", title="T", body="B",
                                                   state="open", parent=999),)),
        ]
        for status, route, args in cases:
            with self.subTest(route=route.__name__, status=status):
                got, body = self.call(route, *args)
                self.assertEqual((got, body["ok"]), (status, False))
                self.assertTrue(body["detail"])
        _, stale = self.call(api.move_task, self.task,
                             api.MoveTask(revision=7, state="closed"))
        self.assertEqual(stale["current"], reads.get_task(self.task))
        self.assertIn("revision 1", stale["detail"])
        self.assertEqual(self.revision(), 1)


class ActorTests(_Store):
    """Who made an operator write: recorded on its history row as ``actor``, as an
    approval's is on its audit row (``control-plane/provenance.py``), while the author
    stays the operator."""

    call = RouteTests.call
    WRITES = frozenset({"create", "edit", "move", "approve", "break_claim", "reparent",
                        "note", "hide_note", "handle_note", "add_repo", "rename_repo",
                        "set_remote", "retire_repo"})

    def rows_since(self, first: int) -> list[dict]:
        # Repo writes name no task, so the history read per task would miss them.
        with reg.taskdb.reading() as conn:
            return [{"author": r["author"], "action": r["action"],
                     "detail": json.loads(r["detail"])}
                    for r in conn.execute("SELECT * FROM history WHERE id > ? ORDER BY id",
                                          (first,))]

    def last_id(self) -> int:
        with reg.taskdb.reading() as conn:
            return conn.execute("SELECT MAX(id) FROM history").fetchone()[0]

    def test_every_operator_write_records_who_made_it_through_the_page(self):
        first = self.last_id()
        _, made = self.call(api.create_task, api.CreateTask(
            repo="widgets", title="T", body="B", state="open"))
        task = made["task"]
        self.call(api.edit_task, task, api.EditTask(revision=1, title="T2"))
        agent.claim_work(ALICE, "widgets", task, "fix", ["."])
        self.call(api.break_claim, task, api.AtRevision(revision=3))
        self.call(api.reparent, task, api.Reparent(revision=4, parent=self.task))
        self.call(api.move_task, task, api.MoveTask(revision=5, state="deferred"))
        planned = agent.propose(ALICE, "widgets", "P", "B")["task"]
        token = agent.claim_plan(ALICE, "widgets", planned, ["."])["token"]
        revision = agent.submit_plan(ALICE, "widgets", planned, token, **PLAN)["revision"]
        self.call(api.approve, planned, api.Approve(revision=revision))
        _, note = self.call(api.add_note, task, api.NoteText(text="Later."))
        self.call(api.hide_notes, api.HideNotes(notes=(note["note"],)))
        flagged = agent.add_note(ALICE, "widgets", task, "Q?", for_operator=True)["note"]
        self.call(api.handle_note, flagged, api.HandleNote(reply="A."))
        self.call(api.handle_notes, api.HandleNotes(notes=(flagged,), handled=False))
        self.call(api.add_repo, api.AddRepo(name="gadgets"))
        self.call(api.rename_repo, "gadgets", api.RenameRepo(new_name="gizmos"))
        self.call(api.set_remote, "gizmos", api.SetRemote(remote="github.com/a/g"))
        self.call(api.retire_repo, "gizmos", api.RetireRepo())
        mine = [r for r in self.rows_since(first) if r["author"] == reg.taskdb.OPERATOR]
        self.assertEqual({r["action"] for r in mine}, self.WRITES)
        for row in mine:
            with self.subTest(action=row["action"]):
                self.assertEqual(row["detail"]["actor"], ThroughTheRelay.ACTOR)
        # The agent's writes in between are its own, under its id, with no actor.
        theirs = [r for r in self.rows_since(first) if r["author"] == ALICE]
        self.assertEqual([r["action"] for r in theirs],
                         ["claim_work", "propose", "claim_plan", "submit_plan", "note"])
        self.assertNotIn("actor", theirs[0]["detail"])

    def test_a_move_called_with_no_request_records_none(self):
        # By hand in the registry's container, say: no request, so nobody to name.
        first = self.last_id()
        op.move(self.task, 1, "deferred")
        op.add_note(self.task, "By hand.")
        rows = self.rows_since(first)
        self.assertEqual([(r["author"], r["action"]) for r in rows],
                         [(reg.taskdb.OPERATOR, "move"), (reg.taskdb.OPERATOR, "note")])
        for row in rows:
            self.assertIn("actor", row["detail"])
            self.assertIsNone(row["detail"]["actor"])

    def test_the_actor_labels_each_field_by_how_far_it_is_trusted(self):
        self.assertEqual(api.actor(ThroughTheRelay()), ThroughTheRelay.ACTOR)
        # Straight to the registry, not through the page: no browser address asserted.
        self.assertEqual(api.actor(ThroughTheRelay(headers={"user-agent": "curl/8"})),
                         'peer=172.26.0.4 ua="curl/8"')
        self.assertEqual(api.actor(ThroughTheRelay(peer=None, headers={})), "peer=?")

    def test_what_the_caller_sent_is_cut_short(self):
        long = "x" * 5000
        got = api.actor(ThroughTheRelay(headers={api.ACTOR_HEADER: long, "origin": long,
                                                 "user-agent": long}))
        cut = "x" * api.ACTOR_FIELD_MAX
        self.assertEqual(got, f'peer=172.26.0.4 via-ui={cut} origin={cut} ua="{cut}"')

    def test_its_header_and_cap_are_the_control_planes_and_the_relays(self):
        provenance = (ROOT / "control-plane" / "provenance.py").read_text()
        relay = (ROOT / "tasks-ui" / "app.py").read_text()
        for source in (provenance, relay):
            self.assertIn(f'ACTOR_HEADER = "{api.ACTOR_HEADER}"\n', source)
        self.assertIn(f"_ACTOR_UA_MAX = {api.ACTOR_FIELD_MAX}\n", provenance)


# Each read route, and the read it serves.
READS = {
    "list_tasks": (reads, "list_tasks"), "search_tasks": (reads, "search_tasks"),
    "get_task": (reads, "get_task"), "repos": (reads, "repos"),
    "task_plans": (reads, "task_plans"),
    "history": (op, "history"), "all_notes": (op, "all_notes"),
    "notes_for_operator": (op, "notes_for_operator"), "inbox": (op, "inbox"),
}
# (route, parameter): what the route turns a value into, where it does not pass it on
# as it came.
MAPPED = {("list_tasks", "state"): {api.EVERY_STATE: None}}


class ReadRouteTests(unittest.TestCase):
    """Each read route takes every parameter of the read it serves, by the same name and
    in the same order, and passes it on: a parameter the read gains, a page cursor
    say, fails here until the route takes it too."""

    def parameters(self, route):
        module, name = READS[route]
        return list(inspect.signature(getattr(module, name)).parameters)

    def served(self, route, args):
        module, name = READS[route]
        signature = inspect.signature(getattr(module, name))
        calls = []

        def record(*a, **k):
            calls.append(signature.bind(*a, **k).arguments)
            return {}
        with mock.patch.object(module, name, record):
            getattr(api, route)(**args)
        [call] = calls
        return call

    def test_every_read_route_is_checked_here(self):
        gets = [path for method, path in api.app.routes if method == "GET"]
        self.assertEqual(len(gets), len(READS))

    def test_each_read_route_passes_every_parameter_of_its_read(self):
        def declared(fn):
            return [(name, p.default) for name, p in inspect.signature(fn).parameters.items()]
        for route, (module, read) in READS.items():
            with self.subTest(route=route):
                # Defaults too: a route defaulting otherwise would answer a request that
                # leaves the parameter out unlike the read it serves.
                self.assertEqual(declared(getattr(api, route)), declared(getattr(module, read)))
                values = {name: object() for name in self.parameters(route)}
                self.assertEqual(self.served(route, values), values)

    def test_a_mapped_parameter_arrives_mapped(self):
        for (route, parameter), mapping in MAPPED.items():
            for given, passed in mapping.items():
                with self.subTest(route=route, given=given):
                    args = {name: object() for name in self.parameters(route)}
                    args[parameter] = given
                    self.assertEqual(self.served(route, args)[parameter], passed)


class InvalidRequestTests(unittest.TestCase):
    def test_a_422_names_each_field_and_never_its_value(self):
        status, body = api.invalid([
            {"loc": ("body", "revision"), "msg": "Input should be a valid integer",
             "input": "SECRET-INPUT", "type": "int_type"},
            {"loc": ("body", "notes", 0), "msg": "Input should be a valid integer",
             "input": "\ud800"},
        ])
        self.assertEqual((status, body["ok"], body["current"]), (422, False, None))
        self.assertEqual(body["detail"], "body.revision: Input should be a valid integer; "
                                         "body.notes.0: Input should be a valid integer")
        self.assertNotIn("SECRET", json.dumps(body))

    def test_a_field_the_request_named_is_escaped_and_cut_short(self):
        _, body = api.invalid([{"loc": ("body", "\ud800" + "x" * 300),
                                "msg": "Extra inputs are not permitted"}])
        body["detail"].encode()
        self.assertTrue(body["detail"].startswith("body.\\ud800xx"))
        self.assertLess(len(body["detail"]), 150)

    def test_only_the_first_faults_are_named_and_the_rest_counted(self):
        errors = [{"loc": ("body", "notes", i), "msg": "bad"}
                  for i in range(api.FAULTS_SHOWN + 5)]
        _, body = api.invalid(errors)
        self.assertEqual(body["detail"].count("bad"), api.FAULTS_SHOWN)
        self.assertTrue(body["detail"].endswith("; and 5 more"))
        _, body = api.invalid(errors[:api.FAULTS_SHOWN])
        self.assertNotIn("more", body["detail"])

    def test_the_app_answers_a_malformed_request_itself(self):
        rejected = sys.modules["fastapi.exceptions"].RequestValidationError
        self.assertIs(api.app.exception_handlers[rejected], api.invalid_request)
        exc = rejected([{"loc": ("body", "revision"), "msg": "bad", "input": "SECRET"}])
        response = asyncio.run(api.invalid_request(None, exc))
        self.assertEqual((response.status_code, response.body), api.invalid(exc.errors()))

    def test_what_starlette_turns_away_is_answered_in_the_apis_shape(self):
        turned = sys.modules["starlette.exceptions"].HTTPException
        self.assertIs(api.app.exception_handlers[turned], api.turned_away)
        for status, detail in ((400, "There was an error parsing the body"),
                               (404, "Not Found"), (405, "Method Not Allowed")):
            with self.subTest(status=status):
                exc = turned(status, detail, {"Allow": "GET"} if status == 405 else None)
                response = asyncio.run(api.turned_away(None, exc))
                self.assertEqual((response.status_code, response.body),
                                 (status, {"ok": False, "detail": detail, "current": None}))
                self.assertEqual(response.headers, exc.headers or {})
        self.assertEqual(api.failed(400, "half \ud800")[1]["detail"], "half \\ud800")


class SurfaceTests(unittest.TestCase):
    """The API calls ``operator_moves`` and ``reads`` and nothing else of the store:
    from any other registry module it takes constants and the refusal classes only."""

    CALLABLE = frozenset({"operator_moves", "reads"})
    REFUSALS = frozenset({("taskdb", "Refused"), ("taskdb", "Missing"), ("taskdb", "Stale")})

    def test_the_api_calls_only_the_operators_moves_and_the_reads(self):
        imported, chains = _surface.reached(Path(api.__file__).read_text())
        self.assertNotIn("agent_moves", imported)
        self.assertLessEqual(self.CALLABLE, imported)
        self.assertEqual(_surface.overreach(chains, self.CALLABLE, self.REFUSALS), [])

    def test_the_guard_sees_through_an_alias_and_a_chain(self):
        cases = {
            "import tasks as _t\n_t.update(conn, 1, {})": ("tasks", "update"),
            "from tasks import update as u\nu(conn, 1, {})": ("tasks", "update"),
            "import reads\nreads.tasks.update(conn, 1, {})": ("reads", "tasks", "update"),
            "import operator_moves as om\nom.claims.end(conn, 1)":
                ("operator_moves", "claims", "end"),
            "import tasks\nhand_over(tasks)": ("tasks",),
        }
        for source, chain in cases.items():
            with self.subTest(source=source):
                _, chains = _surface.reached(source)
                self.assertIn(chain, chains)
                self.assertEqual(_surface.overreach(chains, self.CALLABLE), [chain])
        _, fine = _surface.reached("import reads\nimport tasks\nreads.get_task(tasks.ID_MAX)")
        self.assertEqual(_surface.overreach(fine, self.CALLABLE), [])

    def test_every_body_refuses_unknown_fields_and_takes_ids_and_flags_strictly(self):
        bodies = [model for _, model in inspect.getmembers(api, inspect.isclass)
                  if issubclass(model, api.BaseModel) and model is not api.BaseModel]
        self.assertGreater(len(bodies), 10)
        seen = []
        for model in bodies:
            with self.subTest(model=model.__name__):
                self.assertTrue(issubclass(model, api.Body))
                self.assertEqual(model.model_config.get("extra"), "forbid")
                for annotation in vars(model).get("__annotations__", {}).values():
                    self.assertNotRegex(annotation, r"\b(int|bool)\b")
                    seen.append(annotation)
        self.assertIn("tuple[StrictInt, ...]", seen)
        self.assertIn("StrictBool", seen)


if __name__ == "__main__":
    unittest.main()
