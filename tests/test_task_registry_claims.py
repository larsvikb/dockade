# SPDX-License-Identifier: Apache-2.0
"""The task registry's claims (``task-registry/claims.py`` and the claim moves in
``agent_moves.py`` and ``operator_moves.py``): one holder, the token, expiry and its
sweep, the footprint and its overlaps, the ready queue, and which writes move a task's
revision. Each test gets a store of its own, and a clock it moves by hand."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from _loader import load_task_registry

reg = load_task_registry()
Refused = reg.taskdb.Refused
agent, op, reads, claims = reg.agent_moves, reg.operator_moves, reg.reads, reg.claims

ALICE = "claude-0b6f3a52-91d4-4c1e-a7e2-5d0c8e2f4b19"
BOB = "claude-7e21c9d0-3a5b-4f86-b0d4-c1e9a2f7d360"
START = 1_800_000_000.0


class _Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-claims-test-")
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
        op.add_repo("gadgets")

    def open_task(self, title="A task", repo="widgets", **kw):
        return op.create(repo, title, "What to do.", "open", **kw)["task"]

    def claim(self, task, who=ALICE, repo="widgets", branch="fix-widgets",
              footprint=(".",)):
        return agent.claim(who, repo, task, branch, list(footprint))

    def revision(self, task):
        return reads.get_task(task)["revision"]

    def refused(self, fn, *args, **kw) -> Refused:
        with self.assertRaises(Refused) as caught:
            fn(*args, **kw)
        return caught.exception


class TakingTests(_Store):
    def test_a_claim_returns_its_token_once_and_shows_without_it(self):
        task = self.open_task()
        reply = self.claim(task, footprint=["task-registry/"])
        self.assertEqual(reply["expires_at"], START + claims.CLAIM_SECONDS)
        got = reads.get_task(task)["claim"]
        self.assertEqual((got["holder"], got["branch"], got["footprint"], got["progress"]),
                         (ALICE, "fix-widgets", ["task-registry"], None))
        everything = repr(reads.get_task(task)) + repr(reads.list_tasks("widgets")) + \
            repr(op.history(task)["history"])
        self.assertNotIn(reply["token"], everything)
        self.assertNotIn(claims._hash(reply["token"]), everything)

    def test_only_an_open_or_landing_task_can_be_claimed(self):
        for state in ("proposed", "deferred", "closed"):
            with self.subTest(state=state):
                task = op.create("widgets", "T", "B", state)["task"]
                self.assertEqual(self.refused(self.claim, task).current, {"state": state})

    def test_one_holder_and_no_second_token_for_the_same_id(self):
        task = self.open_task()
        self.claim(task)
        refusal = self.refused(self.claim, task, who=BOB)
        self.assertEqual(refusal.current["holder"], ALICE)
        self.refused(self.claim, task)

    def test_claiming_a_landing_task_reopens_it(self):
        task = self.open_task()
        token = self.claim(task)["token"]
        agent.submit(ALICE, "widgets", task, token)
        self.assertEqual(reads.get_task(task)["state"], "landing")
        self.claim(task, who=BOB)
        self.assertEqual(reads.get_task(task)["state"], "open")

    def test_a_claim_names_a_branch_and_stays_in_its_repo(self):
        task = self.open_task()
        for bad in ("", "-x", "a..b", "a b", "a/", ".x", "x.lock", "a//b", None, 7):
            with self.subTest(branch=bad):
                self.refused(self.claim, task, branch=bad)
        self.assertEqual(self.refused(self.claim, task, repo="gadgets").current,
                         {"repo": "widgets"})
        self.refused(self.claim, task, who="registry-4f2a91c0")
        self.assertIsNone(reads.get_task(task)["claim"])


class HolderTests(_Store):
    def setUp(self):
        super().setUp()
        self.task = self.open_task()
        self.token = self.claim(self.task)["token"]

    def test_only_the_holder_with_the_token_writes_to_the_claim(self):
        self.refused(agent.update, ALICE, "widgets", self.task, "not-the-token")
        self.refused(agent.update, ALICE, "widgets", self.task, None)
        self.assertEqual(self.refused(agent.update, BOB, "widgets", self.task,
                                      self.token).current["holder"], ALICE)
        self.refused(agent.release, BOB, "widgets", self.task, self.token)
        self.refused(agent.update, ALICE, "gadgets", self.task, self.token)
        self.assertEqual(reads.get_task(self.task)["claim"]["holder"], ALICE)

    def test_an_update_renews_and_records_without_moving_the_revision(self):
        before = self.revision(self.task)
        self.clock += 3600
        reply = agent.update(ALICE, "widgets", self.task, self.token,
                             progress="Investigating", branch="widgets-fix")
        self.assertEqual(reply["expires_at"], self.clock + claims.CLAIM_SECONDS)
        self.assertEqual(reply["revision"], before)
        self.clock = START + claims.CLAIM_SECONDS + 1
        self.assertIsNotNone(reads.get_task(self.task)["claim"])
        claim = reads.get_task(self.task)["claim"]
        self.assertEqual((claim["progress"], claim["branch"]), ("Investigating", "widgets-fix"))
        agent.update(ALICE, "widgets", self.task, self.token)
        self.assertEqual(reads.get_task(self.task)["claim"]["progress"], "Investigating")
        self.assertEqual(self.revision(self.task), before)
        self.assertEqual([h["action"] for h in op.history(self.task)["history"]][-2:],
                         ["update", "update"])

    def test_submit_send_back_and_release_end_the_claim(self):
        for move, state in ((agent.submit, "landing"), (agent.release, "open")):
            with self.subTest(move=move.__name__):
                task = self.open_task()
                token = self.claim(task)["token"]
                move(ALICE, "widgets", task, token, note="Done here.")
                got = reads.get_task(task)
                self.assertEqual((got["state"], got["claim"]), (state, None))
                self.assertEqual(op.history(task)["history"][-1]["detail"]["note"], "Done here.")
                self.refused(agent.update, ALICE, "widgets", task, token)
        for empty in (None, "  "):
            with self.subTest(note=empty):
                self.refused(agent.send_back, ALICE, "widgets", self.task, self.token, empty)
        agent.send_back(ALICE, "widgets", self.task, self.token, "Which widgets?")
        self.assertEqual(reads.get_task(self.task)["state"], "proposed")

    def test_an_ending_records_the_branch_to_land(self):
        agent.update(ALICE, "widgets", self.task, self.token, branch="fix-widgets-2")
        agent.submit(ALICE, "widgets", self.task, self.token)
        last = op.history(self.task)["history"][-1]["detail"]
        self.assertEqual((last["branch"], last["holder"]), ("fix-widgets-2", ALICE))

    def test_progress_and_notes_have_bounds(self):
        for bad in ("", "  ", "x" * (claims.PROGRESS_MAX + 1),
                    "x" + " " * claims.PROGRESS_MAX, 7):
            with self.subTest(progress=bad):
                self.refused(agent.update, ALICE, "widgets", self.task, self.token,
                             progress=bad)
        self.refused(agent.release, ALICE, "widgets", self.task, self.token,
                     note="x" * (reg.notes.NOTE_MAX + 1))
        self.assertIsNotNone(reads.get_task(self.task)["claim"])

    def test_taking_and_ending_a_claim_make_an_older_page_stale(self):
        task = self.open_task("Second")
        seen = self.revision(task)
        token = self.claim(task)["token"]
        self.refused(op.move, task, seen, "deferred")
        seen = self.revision(task)
        agent.release(ALICE, "widgets", task, token)
        self.refused(op.move, task, seen, "deferred")


    def test_text_that_cannot_be_stored_is_refused(self):
        bad = "half " + "\ud800"
        self.refused(agent.update, ALICE, "widgets", self.task, self.token, progress=bad)
        self.refused(agent.update, ALICE, "widgets", self.task, self.token, footprint=[bad])
        self.refused(agent.update, ALICE, "widgets", self.task, bad)
        self.refused(agent.release, ALICE, "widgets", self.task, self.token, note=bad)
        self.refused(self.claim, self.open_task(), footprint=[bad])
        self.assertIsNotNone(reads.get_task(self.task)["claim"])


class ExpiryTests(_Store):
    def test_a_lapsed_claim_is_not_held_and_its_holder_is_told(self):
        task = self.open_task()
        token = self.claim(task)["token"]
        self.clock += claims.CLAIM_SECONDS
        got = reads.list_tasks("widgets")["tasks"][0]
        self.assertEqual((got["claim"], got["ready"]), (None, True))
        refusal = self.refused(agent.update, ALICE, "widgets", task, token)
        self.assertIn("lapsed", refusal.reason)
        self.assertEqual(refusal.current["holder"], None)

    def test_a_lapsed_claim_is_not_listed_as_held(self):
        task = self.open_task()
        self.claim(task)
        self.assertEqual(reads.list_tasks("widgets", holder=ALICE)["total"], 1)
        self.clock += claims.CLAIM_SECONDS
        self.assertEqual(reads.list_tasks("widgets", holder=ALICE),
                         {"tasks": [], "total": 0})

    def test_a_holder_whose_task_left_open_is_told_its_state(self):
        task = self.open_task()
        token = self.claim(task)["token"]
        op.move(task, self.revision(task), "deferred")
        refusal = self.refused(agent.update, ALICE, "widgets", task, token)
        self.assertIn("it is deferred", refusal.reason)
        self.assertNotIn("free", refusal.reason)

    def test_an_operator_move_records_a_lapse_it_finds(self):
        task = self.open_task()
        self.claim(task)
        self.clock += claims.CLAIM_SECONDS + 1
        op.move(task, self.revision(task), "deferred")
        actions = [(h["author"], h["action"]) for h in op.history(task)["history"]]
        self.assertEqual(actions[-2:], [(reg.taskdb.REGISTRY, "lapse"),
                                        (reg.taskdb.OPERATOR, "move")])
        self.assertIsNone(op.history(task)["history"][-1]["detail"]["ended_claim"])

    def test_the_next_claim_sweeps_it_and_its_old_holder_learns_who_holds_now(self):
        task = self.open_task()
        token = self.claim(task)["token"]
        before = self.revision(task)
        self.clock += claims.CLAIM_SECONDS + 1
        self.claim(task, who=BOB)
        lapse = [h for h in op.history(task)["history"] if h["action"] == "lapse"]
        self.assertEqual([(h["author"], h["detail"]["holder"], h["detail"]["expired_at"])
                          for h in lapse],
                         [(reg.taskdb.REGISTRY, ALICE, START + claims.CLAIM_SECONDS)])
        self.assertEqual(self.revision(task), before + 2)
        refusal = self.refused(agent.update, ALICE, "widgets", task, token)
        self.assertEqual(refusal.current["holder"], BOB)

    def test_the_claim_length_must_be_a_positive_whole_number(self):
        self.assertEqual(claims._claim_seconds("60"), 60)
        self.assertEqual(claims._claim_seconds(str(claims._CLAIM_SECONDS_MAX)),
                         claims._CLAIM_SECONDS_MAX)
        for bad in ("0", "-5", "1.5", "", "four hours", str(claims._CLAIM_SECONDS_MAX + 1),
                    "9" * 401):
            with self.subTest(raw=bad), self.assertRaises(RuntimeError):
                claims._claim_seconds(bad)


class FootprintTests(_Store):
    def test_paths_are_normalized_and_kept_inside_the_repo(self):
        self.assertEqual(claims.footprint(["./a/b/", "a/b", "c/../d", "."]),
                         ["a/b", "d", "."])
        for bad in ([], (), ["/etc"], [".."], ["a/../../b"], [""], ["a\nb"], "a/b", [7],
                    ["x"] * (claims.FOOTPRINT_MAX + 1)):
            with self.subTest(paths=bad):
                self.refused(claims.footprint, bad)

    def test_an_overlap_is_reported_and_never_refused(self):
        first, second = self.open_task("First"), self.open_task("Second")
        third, elsewhere = self.open_task("Third"), self.open_task("Other", repo="gadgets")
        self.claim(first, footprint=["control-plane", "README.md"])
        self.claim(elsewhere, who=BOB, repo="gadgets", footprint=["control-plane"])
        reply = self.claim(second, who=BOB, footprint=["control-plane/store.py", "docs"])
        self.assertEqual([(o["task"], o["holder"], o["paths"]) for o in reply["overlaps"]],
                         [(first, ALICE, ["control-plane"])])
        self.assertEqual(self.claim(third, footprint=["Makefile"])["overlaps"], [])

    def test_an_update_reports_the_overlaps_of_the_new_footprint(self):
        first, second = self.open_task("First"), self.open_task("Second")
        self.claim(first, footprint=["."])
        token = self.claim(second, who=BOB, footprint=["a"])["token"]
        reply = agent.update(BOB, "widgets", second, token, footprint=["b"])
        self.assertEqual([o["task"] for o in reply["overlaps"]], [first])
        self.assertEqual(reads.get_task(second)["claim"]["footprint"], ["b"])

    def test_an_expired_claim_overlaps_nothing(self):
        first, second = self.open_task("First"), self.open_task("Second")
        self.claim(first, footprint=["a"])
        self.clock += 60
        token = self.claim(second, who=BOB, footprint=["b"])["token"]
        self.clock = START + claims.CLAIM_SECONDS
        reply = agent.update(BOB, "widgets", second, token, footprint=["a"])
        self.assertEqual(reply["overlaps"], [])

    def test_an_update_without_a_footprint_reports_the_held_ones_overlaps(self):
        first, second = self.open_task("First"), self.open_task("Second")
        token = self.claim(first, footprint=["a"])["token"]
        self.claim(second, who=BOB, footprint=["a/b"])
        reply = agent.update(ALICE, "widgets", first, token, progress="Still on it")
        self.assertEqual([o["task"] for o in reply["overlaps"]], [second])


class ReadyQueueTests(_Store):
    def test_ready_is_open_unclaimed_and_unblocked(self):
        blocker = self.open_task("Blocker")
        waiting = self.open_task("Waiting", blocked_by=[blocker])
        claimed = self.open_task("Claimed")
        self.claim(claimed)
        free = self.open_task("Free")
        ready = {t["id"]: t["ready"] for t in reads.list_tasks("widgets")["tasks"]}
        self.assertEqual(ready, {blocker: True, waiting: False, claimed: False, free: True})

    def test_a_blocked_task_can_still_be_claimed_and_says_by_what(self):
        blocker = self.open_task("Blocker")
        waiting = self.open_task("Waiting", blocked_by=[blocker])
        reply = self.claim(waiting)
        self.assertEqual([b["id"] for b in reply["open_blockers"]], [blocker])

    def test_an_agent_finds_its_own_claims(self):
        mine, theirs = self.open_task("Mine"), self.open_task("Theirs")
        self.open_task("Nobody's")
        self.claim(mine)
        self.claim(theirs, who=BOB)
        self.assertEqual([t["id"] for t in
                          reads.list_tasks("widgets", holder=ALICE)["tasks"]], [mine])


class OperatorTests(_Store):
    def test_the_operator_breaks_a_claim_as_of_what_the_page_showed(self):
        task = self.open_task()
        token = self.claim(task)["token"]
        self.refused(op.break_claim, task, 1)
        op.break_claim(task, self.revision(task))
        self.assertIsNone(reads.get_task(task)["claim"])
        last = op.history(task)["history"][-1]
        self.assertEqual((last["author"], last["action"], last["detail"]["holder"]),
                         (reg.taskdb.OPERATOR, "break_claim", ALICE))
        self.refused(agent.update, ALICE, "widgets", task, token)
        self.refused(op.break_claim, task, self.revision(task))

    def test_a_stale_page_is_told_who_holds_the_task_now(self):
        task = self.open_task()
        self.claim(task)
        seen = self.revision(task)
        self.clock += claims.CLAIM_SECONDS
        self.claim(task, who=BOB)
        refusal = self.refused(op.break_claim, task, seen)
        self.assertEqual(refusal.current["claim"]["holder"], BOB)
        self.assertEqual(refusal.current, reads.get_task(task))

    def test_a_move_off_open_ends_the_claim(self):
        for state in ("landing", "deferred", "closed", "proposed"):
            with self.subTest(state=state):
                task = self.open_task()
                token = self.claim(task)["token"]
                op.move(task, self.revision(task), state)
                self.assertIsNone(reads.get_task(task)["claim"])
                self.assertEqual(op.history(task)["history"][-1]["detail"]["ended_claim"],
                                 {"holder": ALICE, "branch": "fix-widgets",
                                  "footprint": ["."]})
                self.refused(agent.update, ALICE, "widgets", task, token)

    def test_a_move_to_open_keeps_the_claim(self):
        task = self.open_task()
        self.claim(task)
        op.move(task, self.revision(task), "open")
        self.assertEqual(reads.get_task(task)["claim"]["holder"], ALICE)


if __name__ == "__main__":
    unittest.main()
