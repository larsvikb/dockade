# SPDX-License-Identifier: Apache-2.0
"""The operator's inbox (``operator_moves.inbox``): which tasks are the operator's turn,
in which section, for which reasons and since when, worked out from the store as it is.
Each test gets a store of its own and a clock it moves."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from _loader import load_task_registry
from test_task_registry_plans import DECISION, MULTI

reg = load_task_registry()
agent, op, reads = reg.agent_moves, reg.operator_moves, reg.reads

ALICE = "claude-0b6f3a52-91d4-4c1e-a7e2-5d0c8e2f4b19"
START = 1_800_000_000.0
DAY = 24 * 3600

PLAN = {"approach": "A rotate step in the key store.", "footprint": ["src/keys.py"],
        "tests": "A rotation keeps old keys readable.", "out_of_scope": "None.",
        "risks": "A reader mid-rotation.", "review_rounds": 2,
        "review_verdict": "Passes."}


class _Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-inbox-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        previous = reg.taskdb.DB_PATH
        self.addCleanup(setattr, reg.taskdb, "DB_PATH", previous)
        reg.taskdb.DB_PATH = os.path.join(self.dir, "tasks.db")
        reg.taskdb.init()
        # Every clock the store reads: a history row's, a note's, a claim's.
        self.clock = START
        clock = mock.patch.object(time, "time", lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        op.add_repo("widgets")

    def later(self, seconds=60):
        self.clock += seconds
        return self.clock

    def proposal(self, title="Rotate keys", repo="widgets"):
        return agent.propose(ALICE, repo, title, "Every 90 days.")["task"]

    def claim(self, task, footprint=("src",)):
        return agent.claim_plan(ALICE, "widgets", task, list(footprint))["token"]

    def work(self, task, footprint=("src",)):
        return agent.claim_work(ALICE, "widgets", task, "rotate-keys",
                                list(footprint))["token"]

    def refined(self, task=None):
        task = self.proposal() if task is None else task
        agent.submit_plan(ALICE, "widgets", task, self.claim(task), **PLAN)
        return task

    def landing(self):
        task = op.create("widgets", "Ship it", "Do it.", "open")["task"]
        agent.submit_work(ALICE, "widgets", task, self.work(task))
        return task

    def flag(self, task, text="Which key store?"):
        return agent.add_note(ALICE, "widgets", task, text, for_operator=True)["note"]

    def revision(self, task):
        return reads.get_task(task)["revision"]

    def section(self, name):
        return op.inbox()[name]

    def rows(self, name):
        return {r["task"]: r for r in self.section(name)["tasks"]}

    def where(self, task):
        found = [name for name in op.SECTIONS if task in self.rows(name)]
        return found[0] if found else None


class SectionTests(_Store):
    def test_an_empty_store_has_an_empty_inbox(self):
        self.proposal()
        self.assertEqual(op.inbox(), {name: {"tasks": [], "total": 0}
                                      for name in ("waiting", "triage", "drifting")})

    def test_a_flagged_note_waits_on_the_operator_until_handled(self):
        task = self.proposal()
        first = self.flag(task)
        flagged_at = self.clock
        self.later()
        self.flag(task, "And which rotation period?")
        agent.add_note(ALICE, "widgets", task, "Not for the operator.")
        [row] = self.section("waiting")["tasks"]
        self.assertEqual(row["reasons"], [{"why": "flagged", "since": flagged_at,
                                           "notes": 2}])
        self.assertEqual((row["task"], row["repo"], row["title"], row["state"],
                          row["since"]), (task, "widgets", "Rotate keys", "proposed",
                                          flagged_at))
        op.handle_note(first)
        self.assertEqual(self.rows("waiting")[task]["reasons"][0]["notes"], 1)
        self.assertGreater(self.rows("waiting")[task]["since"], flagged_at)

    def test_a_hidden_or_handled_note_waits_on_nobody(self):
        task = self.proposal()
        op.hide_note(self.flag(task))
        op.handle_note(self.flag(task))
        self.assertIsNone(self.where(task))

    def test_a_landing_task_waits_with_its_branch_since_it_was_handed_in(self):
        task = self.landing()
        handed_in = self.clock
        self.later()
        op.edit(task, self.revision(task), body="Done, mostly.")
        agent.add_note(ALICE, "widgets", task, "Pushed.")
        op.move(task, self.revision(task), "landing")
        self.assertEqual(self.rows("waiting")[task]["reasons"],
                         [{"why": "landing", "since": handed_in, "branch": "rotate-keys"}])

    def test_a_task_the_operator_created_landing_is_landing_since_then(self):
        task = op.create("widgets", "Landed elsewhere", "Done.", "landing")["task"]
        made = self.clock
        self.later()
        agent.add_note(ALICE, "widgets", task, "Pushed.")
        self.assertEqual(self.rows("waiting")[task]["reasons"],
                         [{"why": "landing", "since": made, "branch": None}])

    def test_a_hand_in_from_before_plans_is_one_too(self):
        # A schema 1 store recorded submit_work as submit, and history is never
        # rewritten.
        task = self.landing()
        handed_in = self.clock
        with reg.taskdb.writing() as conn:
            conn.execute("UPDATE history SET action = 'submit' WHERE task_id = ? "
                         "AND action = 'submit_work'", (task,))
        self.later()
        op.move(task, self.revision(task), "landing")
        self.assertEqual(self.rows("waiting")[task]["reasons"],
                         [{"why": "landing", "since": handed_in, "branch": "rotate-keys"}])

    def test_a_task_the_operator_moved_to_landing_keeps_the_claims_branch(self):
        held = op.create("widgets", "Held", "Do it.", "open")["task"]
        self.work(held)
        bare = op.create("widgets", "Bare", "Do it.", "open")["task"]
        self.later()
        for task in (held, bare):
            op.move(task, self.revision(task), "landing")
        rows = self.rows("waiting")
        self.assertEqual(rows[held]["reasons"][0]["branch"], "rotate-keys")
        self.assertIsNone(rows[bare]["reasons"][0]["branch"])
        self.assertEqual(rows[bare]["since"], self.clock)

    def test_a_refined_task_waits_for_triage_with_its_plan(self):
        task = self.refined()
        refined_at = self.clock
        self.later()
        op.edit(task, self.revision(task), title="Rotate the keys")
        [row] = self.section("triage")["tasks"]
        plan = reads.get_task(task)["plan"]["id"]
        self.assertEqual(row["reasons"], [{"why": "refined", "since": refined_at,
                                           "plan": plan, "review_rounds": 2,
                                           "decisions": 0}])
        self.assertEqual(row["title"], "Rotate the keys")

    def test_a_refined_reason_counts_its_plans_decisions(self):
        task = self.proposal()
        agent.submit_plan(ALICE, "widgets", task, self.claim(task),
                          **PLAN, decisions=[DECISION, MULTI])
        [reason] = self.rows("triage")[task]["reasons"]
        self.assertEqual(reason["decisions"], 2)

    def test_a_task_claimed_back_for_rework_leaves_the_inbox(self):
        refined, landing = self.refined(), self.landing()
        self.claim(refined)
        self.work(landing)
        self.assertEqual((self.where(refined), self.where(landing)), (None, None))

    def test_the_second_plan_is_since_its_own_hand_in(self):
        task = self.refined()
        self.later()
        self.refined(task)
        self.assertEqual(self.rows("triage")[task]["since"], self.clock)

    def test_parked_and_closed_tasks_are_not_the_operators_turn(self):
        for state in ("deferred", "closed"):
            task = op.create("widgets", state, "B", state)["task"]
            self.later(30 * DAY)
            with self.subTest(state=state):
                self.assertIsNone(self.where(task))

    def test_a_task_is_in_its_highest_section_with_every_reason(self):
        task = self.refined()
        self.later()
        self.flag(task)
        self.assertNotIn(task, self.rows("triage"))
        row = self.rows("waiting")[task]
        self.assertEqual([r["why"] for r in row["reasons"]], ["flagged", "refined"])
        self.assertEqual(row["since"], row["reasons"][1]["since"])

    def test_every_repo_is_in_it(self):
        op.add_repo("gadgets")
        task = self.proposal(repo="gadgets")
        agent.add_note(ALICE, "gadgets", task, "?", for_operator=True)
        self.assertEqual(self.rows("waiting")[task]["repo"], "gadgets")


class DriftingTests(_Store):
    def test_a_claim_that_ran_out_drifts_until_it_is_swept(self):
        task = op.create("widgets", "Held", "Do it.", "open")["task"]
        self.work(task)
        expires = reads.get_task(task)["claim"]["expires_at"]
        self.clock = expires - 1
        self.assertIsNone(self.where(task))
        self.clock = expires
        [row] = self.section("drifting")["tasks"]
        self.assertEqual(row["reasons"], [{"why": "lapsed", "since": expires,
                                           "holder": ALICE, "branch": "rotate-keys"}])
        other = self.proposal()
        self.claim(other)
        self.assertIsNone(self.where(task))

    def test_a_claim_to_refine_that_ran_out_drifts_with_no_branch(self):
        task = self.proposal()
        self.claim(task)
        self.clock = reads.get_task(task)["claim"]["expires_at"]
        self.assertEqual(self.rows("drifting")[task]["reasons"],
                         [{"why": "lapsed", "since": self.clock, "holder": ALICE,
                           "branch": None}])

    def test_an_unheld_task_drifts_after_a_quiet_spell_and_a_write_ends_it(self):
        proposed = self.proposal()
        opened = op.create("widgets", "Open", "Do it.", "open")["task"]
        last = self.clock
        self.later(op.QUIET_SECONDS - 1)
        self.assertEqual(self.section("drifting")["total"], 0)
        self.later(1)
        rows = self.rows("drifting")
        self.assertEqual(set(rows), {proposed, opened})
        self.assertEqual(rows[opened]["reasons"], [{"why": "quiet", "since": last}])
        agent.add_note(ALICE, "widgets", opened, "Still wanted?")
        self.assertEqual(set(self.rows("drifting")), {proposed})

    def test_a_held_task_is_never_quiet(self):
        task = op.create("widgets", "Held", "Do it.", "open")["task"]
        self.work(task)
        self.later(op.QUIET_SECONDS)
        self.assertEqual([r["why"] for r in self.rows("drifting")[task]["reasons"]],
                         ["lapsed"])

    def test_drifting_shows_the_oldest_few_with_the_total(self):
        made = []
        for i in range(op.DRIFTING_MAX + 2):
            made.append(self.proposal(f"Idea {i}"))
            self.later()
        self.later(op.QUIET_SECONDS)
        drifting = self.section("drifting")
        self.assertEqual(drifting["total"], op.DRIFTING_MAX + 2)
        self.assertEqual([r["task"] for r in drifting["tasks"]], made[:op.DRIFTING_MAX])


class OrderTests(_Store):
    def test_what_waits_and_what_is_to_triage_is_all_there_oldest_first(self):
        # Past a page too: a task cut from these would be the operator's turn and
        # nowhere on the page.
        made = []
        for i in range(op.PAGE + 1):
            task = self.proposal(f"Question {i}")
            made.append(task)
            self.later()
        for task in reversed(made):
            self.flag(task)
            self.later()
        refined = [self.refined() for _ in range(op.PAGE + 1)]
        waiting, triage = self.section("waiting"), self.section("triage")
        self.assertEqual(waiting["total"], op.PAGE + 1)
        self.assertEqual([r["task"] for r in waiting["tasks"]], list(reversed(made)))
        self.assertEqual([r["task"] for r in triage["tasks"]], refined)

    def test_a_tie_goes_to_the_older_task(self):
        # Found in another order: flagged notes are gathered before landing tasks.
        landing = self.landing()
        asked = self.proposal()
        self.flag(asked)
        self.assertEqual([r["task"] for r in self.section("waiting")["tasks"]],
                         [landing, asked])


class SnapshotTests(_Store):
    def test_a_write_in_the_middle_of_the_read_is_not_seen(self):
        # A claim taking the refined task back supersedes its plan between the read of
        # the task and the read of its plan; one snapshot still sees the plan.
        task = self.refined()
        entered = op._entered

        def meanwhile(conn, which):
            self.claim(task)
            return entered(conn, which)
        with mock.patch.object(op, "_entered", meanwhile):
            [row] = self.section("triage")["tasks"]
        self.assertEqual(row["reasons"][0]["review_rounds"], 2)
        self.assertEqual(reads.get_task(task)["state"], "proposed")


if __name__ == "__main__":
    unittest.main()
