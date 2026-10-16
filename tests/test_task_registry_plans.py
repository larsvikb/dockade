# SPDX-License-Identifier: Apache-2.0
"""The task registry's plans (``task-registry/plans.py`` and the moves around them in
``agent_moves.py`` and ``operator_moves.py``): a claim that refines a proposal, the
plan ``submit_plan`` hands in, the ``refined`` state the operator approves from, and
what supersedes a plan. Each test gets a store of its own, and a clock it moves by
hand."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from _loader import load_task_registry

reg = load_task_registry()
Refused = reg.taskdb.Refused
agent, op, reads, claims, plans = (reg.agent_moves, reg.operator_moves, reg.reads,
                                   reg.claims, reg.plans)

ALICE = "claude-0b6f3a52-91d4-4c1e-a7e2-5d0c8e2f4b19"
BOB = "claude-7e21c9d0-3a5b-4f86-b0d4-c1e9a2f7d360"
START = 1_800_000_000.0

PLAN = {"approach": "A rotate step in the key store.", "footprint": ["src/keys.py"],
        "tests": "A rotation keeps old keys readable.", "out_of_scope": "None.",
        "risks": "A reader mid-rotation.", "review_rounds": 2,
        "review_verdict": "Passes."}


class _Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-plans-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        previous = reg.taskdb.DB_PATH
        self.addCleanup(setattr, reg.taskdb, "DB_PATH", previous)
        reg.taskdb.DB_PATH = os.path.join(self.dir, "tasks.db")
        reg.taskdb.init()
        self.clock = START
        clock = mock.patch.object(claims, "now", lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        op.add_repo("widgets")
        self.task = agent.propose(ALICE, "widgets", "Rotate keys", "Every 90 days.")["task"]

    def claim(self, task=None, who=ALICE, footprint=(".",)):
        task = self.task if task is None else task
        return agent.claim_plan(who, "widgets", task,
                                None if footprint is None else list(footprint))["token"]

    def work(self, task=None, who=ALICE, footprint=(".",)):
        task = self.task if task is None else task
        return agent.claim_work(who, "widgets", task, "rotate-keys",
                                None if footprint is None else list(footprint))["token"]

    def plan(self, task=None, who=ALICE, **changes):
        task = self.task if task is None else task
        token = self.claim(task, who)
        return agent.submit_plan(who, "widgets", task, token, **(PLAN | changes))

    def got(self, task=None):
        return reads.get_task(self.task if task is None else task)

    def refused(self, fn, *args, **kw) -> Refused:
        with self.assertRaises(Refused) as caught:
            fn(*args, **kw)
        return caught.exception


class RefiningTests(_Store):
    def test_a_claim_refines_a_proposal_where_it_is(self):
        self.claim()
        got = self.got()
        self.assertEqual((got["state"], got["claim"]["holder"]), ("proposed", ALICE))
        self.assertFalse(got["ready"])

    def test_a_plan_makes_the_task_refined_and_ends_the_claim(self):
        reply = self.plan(note="Ready for you.")
        got = self.got()
        self.assertEqual((got["state"], got["claim"], got["revision"]),
                         ("refined", None, reply["revision"]))
        self.assertEqual({k: got["plan"][k] for k in PLAN}, PLAN)
        self.assertEqual((got["plan"]["id"], got["plan"]["author"],
                          got["plan"]["task_revision"], got["plan"]["superseded_at"]),
                         (reply["plan"], ALICE, reply["revision"], None))
        self.assertEqual((got["notes"][0]["text"], got["notes"][0]["move"]),
                         ("Ready for you.", "submit_plan"))
        last = op.history(self.task)["history"][-1]
        self.assertEqual((last["action"], last["detail"]["plan"],
                          last["detail"]["plan_footprint"], last["detail"]["approach"]),
                         ("submit_plan", reply["plan"], ["src/keys.py"], PLAN["approach"]))

    def test_every_part_of_a_plan_is_required_and_capped(self):
        token = self.claim()
        for part, limit in plans.TEXTS.items():
            for bad in (None, "", "  ", "x" * (limit + 1), 7):
                with self.subTest(part=part, value=repr(bad)[:20]):
                    self.refused(agent.submit_plan, ALICE, "widgets", self.task, token,
                                 **(PLAN | {part: bad}))
        for bad in (0, -1, plans.REVIEW_ROUNDS_MAX + 1, True, "2", None):
            with self.subTest(review_rounds=bad):
                self.refused(agent.submit_plan, ALICE, "widgets", self.task, token,
                             **(PLAN | {"review_rounds": bad}))
        self.assertEqual((self.got()["state"], self.got()["plan"]), ("proposed", None))

    def test_a_plans_footprint_says_where(self):
        token = self.claim()
        for bad in (["."], ["src", "."], ["./"], ["src/.."], [], ["../x"], ["/etc"], None):
            with self.subTest(footprint=bad):
                self.refused(agent.submit_plan, ALICE, "widgets", self.task, token,
                             **(PLAN | {"footprint": bad}))
        agent.submit_plan(ALICE, "widgets", self.task, token,
                          **(PLAN | {"footprint": ["src/", "src", "tests/new_test.py"]}))
        self.assertEqual(self.got()["plan"]["footprint"], ["src", "tests/new_test.py"])

    def test_only_the_holder_with_the_token_hands_in_a_plan(self):
        token = self.claim()
        self.refused(agent.submit_plan, BOB, "widgets", self.task, token, **PLAN)
        self.refused(agent.submit_plan, ALICE, "widgets", self.task, "not-it", **PLAN)
        self.clock += claims.CLAIM_SECONDS
        self.assertIn("lapsed; it is free to claim with claim_plan", self.refused(
            agent.submit_plan, ALICE, "widgets", self.task, token, **PLAN).reason)
        self.assertEqual(self.got()["state"], "proposed")

    def test_each_hand_in_ends_only_its_own_kind_of_claim(self):
        token = self.claim()
        self.assertIn("submit_plan", self.refused(
            agent.submit_work, ALICE, "widgets", self.task, token).reason)
        self.assertIn("release", self.refused(
            agent.send_back, ALICE, "widgets", self.task, token, "Unclear.").reason)
        opened = op.create("widgets", "Open", "B", "open")["task"]
        other = self.work(opened)
        self.assertIn("submit_work", self.refused(
            agent.submit_plan, ALICE, "widgets", opened, other, **PLAN).reason)
        self.assertEqual((self.got()["claim"]["holder"],
                          self.got(opened)["claim"]["holder"]), (ALICE, ALICE))

    def test_a_released_proposal_stays_proposed_and_free(self):
        token = self.claim()
        agent.release(ALICE, "widgets", self.task, token, "Not worth it.")
        got = self.got()
        self.assertEqual((got["state"], got["claim"]), ("proposed", None))
        self.claim(who=BOB)


class EditWhileHeldTests(_Store):
    def test_only_the_holder_edits_a_claimed_proposal_with_its_token(self):
        token = self.claim()
        revision = self.got()["revision"]
        self.assertEqual(self.refused(agent.edit_proposal, BOB, "widgets", self.task,
                                      revision, body="Mine now.").current["holder"], ALICE)
        self.refused(agent.edit_proposal, BOB, "widgets", self.task, revision,
                     body="Mine now.", token=token)
        self.refused(agent.edit_proposal, ALICE, "widgets", self.task, revision,
                     body="Without the token.")
        agent.edit_proposal(ALICE, "widgets", self.task, revision, body="Every 30 days.",
                            token=token)
        self.assertEqual(self.got()["body"], "Every 30 days.")

    def test_an_unclaimed_proposal_is_anyones_to_edit(self):
        agent.edit_proposal(BOB, "widgets", self.task, 1, body="Every 30 days.")
        self.assertEqual(self.got()["body"], "Every 30 days.")

    def test_a_lapsed_token_is_told_so(self):
        token = self.claim()
        self.clock += claims.CLAIM_SECONDS
        refusal = self.refused(agent.edit_proposal, ALICE, "widgets", self.task,
                               self.got()["revision"], body="Late.", token=token)
        self.assertIn("lapsed", refusal.reason)

    def test_a_refined_task_is_frozen_to_agents(self):
        self.plan()
        refusal = self.refused(agent.edit_proposal, ALICE, "widgets", self.task,
                               self.got()["revision"], body="Changed.")
        self.assertEqual(refusal.current, {"state": "refined"})


class SupersedingTests(_Store):
    def test_claiming_a_refined_task_takes_it_back_and_supersedes_its_plan(self):
        first = self.plan()["plan"]
        self.claim(who=BOB)
        got = self.got()
        self.assertEqual(got["state"], "proposed")
        self.assertEqual(got["plan"]["id"], first)
        self.assertIsNotNone(got["plan"]["superseded_at"])
        self.assertEqual(op.history(self.task)["history"][-1]["detail"]["before"],
                         "refined")

    def test_a_second_plan_is_the_current_one_and_the_first_is_kept(self):
        first = self.plan()["plan"]
        second = self.plan(who=BOB, approach="A rotate job instead.")["plan"]
        every = reads.task_plans(self.task)
        self.assertEqual([p["id"] for p in every], [second, first])
        self.assertEqual([p["superseded_at"] is None for p in every], [True, False])
        self.assertEqual(self.got()["plan"]["approach"], "A rotate job instead.")

    def test_every_way_back_to_proposed_supersedes(self):
        self.plan()
        op.move(self.task, self.got()["revision"], "proposed")
        self.assertIsNotNone(self.got()["plan"]["superseded_at"])
        self.plan()
        op.move(self.task, self.got()["revision"], "open")
        token = self.work(footprint=None)
        agent.send_back(ALICE, "widgets", self.task, token, "Which keys?")
        self.assertIsNotNone(self.got()["plan"]["superseded_at"])
        self.assertTrue(all(p["superseded_at"] for p in reads.task_plans(self.task)))

    def test_a_task_never_planned_has_no_plan(self):
        self.assertIsNone(self.got()["plan"])
        self.assertEqual(reads.task_plans(self.task), [])
        self.refused(reads.task_plans, 999)


class ApprovalTests(_Store):
    def test_approving_names_the_revision_and_the_revision_pins_the_plan(self):
        seen = self.plan()["revision"]
        self.plan(who=BOB, approach="Changed after the operator read it.")
        self.refused(op.move, self.task, seen, "open")
        self.assertEqual(self.got()["state"], "refined")
        op.move(self.task, self.got()["revision"], "open")
        self.assertEqual(self.got()["state"], "open")

    def test_refined_needs_a_current_plan(self):
        refusal = self.refused(op.move, self.task, 1, "refined")
        self.assertEqual(refusal.current, {"state": "proposed"})
        self.plan()
        op.move(self.task, self.got()["revision"], "deferred")
        self.assertIsNone(self.got()["plan"]["superseded_at"])
        op.move(self.task, self.got()["revision"], "refined")
        self.assertEqual(self.got()["state"], "refined")

    def test_the_operator_may_open_a_proposal_with_no_plan(self):
        op.move(self.task, 1, "open")
        self.assertEqual((self.got()["state"], self.got()["plan"]), ("open", None))

    def test_a_move_that_changes_the_state_ends_a_refining_claim(self):
        self.claim()
        op.move(self.task, self.got()["revision"], "proposed")
        self.assertEqual(self.got()["claim"]["holder"], ALICE)
        op.move(self.task, self.got()["revision"], "open")
        self.assertIsNone(self.got()["claim"])
        self.assertEqual(op.history(self.task)["history"][-1]["detail"]["ended_claim"],
                         {"holder": ALICE, "footprint": ["."]})

    def test_the_work_claims_the_plans_footprint_when_it_names_none(self):
        self.plan()
        op.move(self.task, self.got()["revision"], "open")
        self.work(who=BOB, footprint=None)
        self.assertEqual(self.got()["claim"]["footprint"], ["src/keys.py"])

    def test_a_claim_with_no_plan_to_take_from_names_a_footprint(self):
        self.assertIn("a footprint is a list",
                      self.refused(self.claim, footprint=None).reason)
        opened = op.create("widgets", "Open", "B", "open")["task"]
        self.assertIn("no plan", self.refused(self.work, opened, footprint=None).reason)
        self.assertIsNone(self.got(opened)["claim"])
        # A refined task's plan is the one a re-refiner replaces, not one to work in.
        self.plan()
        self.assertIn("a footprint is a list",
                      self.refused(self.claim, footprint=None).reason)
        self.assertEqual(self.got()["state"], "refined")


class OverlapTests(_Store):
    def test_a_plan_is_told_of_the_plans_and_claims_it_overlaps(self):
        planned = self.plan(footprint=["src"])["plan"]
        other = agent.propose(BOB, "widgets", "Audit keys", "Log each read.")["task"]
        working = op.create("widgets", "Fix src", "B", "open")["task"]
        self.work(working, who=BOB, footprint=["src/keys.py"])
        reply = self.plan(other, who=BOB, footprint=["src/keys.py", "docs"])
        self.assertEqual(reply["plan_overlaps"], [
            {"task": self.task, "title": "Rotate keys", "state": "refined",
             "plan": planned, "paths": ["src"]}])
        self.assertEqual([(o["task"], o["paths"]) for o in reply["overlaps"]],
                         [(working, ["src/keys.py"])])

    def test_only_a_tasks_current_plan_overlaps(self):
        self.plan(footprint=["src"])
        self.plan(who=BOB, footprint=["docs"])
        other = agent.propose(BOB, "widgets", "Audit keys", "Log each read.")["task"]
        self.assertEqual(self.plan(other, who=BOB, footprint=["src"])["plan_overlaps"], [])

    def test_a_superseded_or_closed_plan_overlaps_nothing(self):
        self.plan(footprint=["src"])
        agent.release(BOB, "widgets", self.task, self.claim(who=BOB))
        closed = agent.propose(BOB, "widgets", "Old keys", "Done once.")["task"]
        self.plan(closed, who=BOB, footprint=["src"])
        op.move(closed, self.got(closed)["revision"], "closed")
        other = agent.propose(BOB, "widgets", "Audit keys", "Log each read.")["task"]
        self.assertEqual(self.plan(other, who=BOB, footprint=["src"])["plan_overlaps"], [])

if __name__ == "__main__":
    unittest.main()
