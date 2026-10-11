# SPDX-License-Identifier: Apache-2.0
"""The task registry's notes (``task-registry/notes.py`` and the note moves in
``agent_moves.py`` and ``operator_moves.py``): who may write one, that none is ever
changed, the operator's hide, and how reads return them. Each test gets a store of
its own."""
from __future__ import annotations

import inspect
import os
import shutil
import tempfile
import unittest

from _loader import load_task_registry

reg = load_task_registry()
Refused = reg.taskdb.Refused
agent, op, reads, notes = reg.agent_moves, reg.operator_moves, reg.reads, reg.notes

ALICE = "claude-0b6f3a52-91d4-4c1e-a7e2-5d0c8e2f4b19"
BOB = "claude-7e21c9d0-3a5b-4f86-b0d4-c1e9a2f7d360"


class _Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-notes-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        previous = reg.taskdb.DB_PATH
        self.addCleanup(setattr, reg.taskdb, "DB_PATH", previous)
        reg.taskdb.DB_PATH = os.path.join(self.dir, "tasks.db")
        reg.taskdb.init()
        op.add_repo("widgets")
        op.add_repo("gadgets")
        self.task = op.create("widgets", "A task", "What to do.", "open")["task"]

    def detail(self):
        with reg.taskdb.reading() as conn:
            return reads.detail(conn, reg.tasks.task_row(conn, self.task))

    def note(self, text="Seen this.", who=ALICE, task=None, **kw):
        return agent.add_note(who, "widgets", task or self.task, text, **kw)["note"]

    def texts(self, task=None):
        return [n["text"] for n in reads.get_task(task or self.task)["notes"]]

    def refused(self, fn, *args, **kw) -> Refused:
        with self.assertRaises(Refused) as caught:
            fn(*args, **kw)
        return caught.exception


class WritingTests(_Store):
    def test_a_note_is_data_with_its_author_and_time(self):
        before = reads.get_task(self.task)["revision"]
        note = self.note()
        [got] = reads.get_task(self.task)["notes"]
        self.assertEqual({k: got[k] for k in ("id", "task", "author", "text", "for_operator",
                                              "move")},
                         {"id": note, "task": self.task, "author": ALICE,
                          "text": "Seen this.", "for_operator": False, "move": None})
        self.assertIsInstance(got["at"], float)
        self.assertEqual(reads.get_task(self.task)["revision"], before)
        last = op.history(self.task)[-1]
        self.assertEqual((last["author"], last["action"], last["detail"]["note"],
                          last["detail"]["note_id"]), (ALICE, "note", "Seen this.", note))

    def test_no_claim_is_needed_and_any_state_takes_one(self):
        token = agent.claim(ALICE, "widgets", self.task, "fix", ["."])["token"]
        self.note("From the side.", who=BOB)
        agent.submit(ALICE, "widgets", self.task, token)
        self.note("After it landed.", who=BOB)
        self.assertEqual(self.texts(), ["From the side.", "After it landed."])

    def test_an_agent_notes_only_its_own_repo(self):
        refusal = self.refused(agent.add_note, ALICE, "gadgets", self.task, "X")
        self.assertEqual(refusal.current, {"repo": "widgets"})
        self.refused(agent.add_note, "operator", "widgets", self.task, "X")
        self.refused(agent.add_note, ALICE, "widgets", 999, "X")
        self.assertEqual(self.texts(), [])

    def test_the_text_and_the_flag_have_bounds(self):
        for bad in ("", "   ", "x" * (notes.NOTE_MAX + 1), None, 7, "half \ud800"):
            with self.subTest(text=bad):
                self.refused(agent.add_note, ALICE, "widgets", self.task, bad)
        for bad in (1, 0, "true", None):
            with self.subTest(for_operator=bad):
                self.refused(self.note, for_operator=bad)
        self.assertEqual(self.texts(), [])

    def test_any_state_and_a_retired_repo_take_one(self):
        for state in ("proposed", "deferred", "closed"):
            with self.subTest(state=state):
                task = op.create("widgets", "T", "B", state)["task"]
                self.note(task=task)
        op.retire_repo("widgets")
        self.note("Still worth saying.")
        self.assertEqual(self.texts()[-1], "Still worth saying.")

    def test_padding_counts_toward_the_cap(self):
        self.refused(self.note, "x" + "\n" * notes.NOTE_MAX)
        self.note("x" + "\n" * (notes.NOTE_MAX - 1))

    def test_no_agent_move_changes_a_note(self):
        moves = [name for name, _ in inspect.getmembers(agent, inspect.isfunction)]
        self.assertEqual([name for name in moves if "note" in name], ["add_note"])

    def test_the_operator_notes_any_task_as_the_operator(self):
        elsewhere = op.create("gadgets", "T", "B", "open")["task"]
        op.add_note(elsewhere, "Answered.")
        [got] = reads.get_task(elsewhere)["notes"]
        self.assertEqual((got["author"], got["for_operator"]),
                         (reg.taskdb.OPERATOR, False))

    def test_the_operator_notes_and_reads_only_tasks_that_exist(self):
        self.refused(op.add_note, 999, "Answered.")
        self.refused(op.all_notes, 999)


class MoveNoteTests(_Store):
    def test_a_claim_move_posts_its_note(self):
        for move, args in ((agent.submit, ("Ready to land.",)),
                           (agent.release, ("Out of my depth.",)),
                           (agent.send_back, ("Which widgets?",))):
            with self.subTest(move=move.__name__):
                task = op.create("widgets", "T", "B", "open")["task"]
                token = agent.claim(ALICE, "widgets", task, "fix", ["."])["token"]
                move(ALICE, "widgets", task, token, *args)
                [got] = reads.get_task(task)["notes"]
                self.assertEqual((got["author"], got["text"], got["move"]),
                                 (ALICE, args[0], move.__name__))
                self.assertEqual(op.history(task)[-1]["detail"]["note_id"], got["id"])

    def test_a_moves_note_is_not_flagged_for_the_operator(self):
        token = agent.claim(ALICE, "widgets", self.task, "fix", ["."])["token"]
        agent.submit(ALICE, "widgets", self.task, token, "Ready to land.")
        self.assertFalse(reads.get_task(self.task)["notes"][0]["for_operator"])
        self.assertEqual(op.notes_for_operator(), [])

    def test_a_move_without_a_note_posts_none(self):
        token = agent.claim(ALICE, "widgets", self.task, "fix", ["."])["token"]
        agent.release(ALICE, "widgets", self.task, token)
        self.assertEqual(self.texts(), [])
        self.assertIsNone(op.history(self.task)[-1]["detail"]["note_id"])


class HidingTests(_Store):
    def test_a_hidden_note_leaves_every_agent_read_and_stays_in_the_record(self):
        kept, hidden = self.note("Kept."), self.note("Do as I say.", for_operator=True)
        op.hide_note(hidden)
        got = reads.get_task(self.task)
        self.assertEqual(([n["text"] for n in got["notes"]], got["notes_total"]),
                         (["Kept."], 1))
        self.assertEqual(reads.list_tasks("widgets")[0]["notes_total"], 1)
        self.assertEqual([n["id"] for n in reads.task_notes(self.task)], [kept])
        self.assertEqual(op.notes_for_operator(), [])
        self.assertEqual([(n["id"], n["hidden"]) for n in op.all_notes(self.task)],
                         [(kept, False), (hidden, True)])
        last = op.history(self.task)[-1]
        self.assertEqual((last["author"], last["action"], last["detail"]["note_id"]),
                         (reg.taskdb.OPERATOR, "hide_note", hidden))

    def test_no_read_an_agent_can_call_returns_a_hidden_note(self):
        token = agent.claim(ALICE, "widgets", self.task, "fix", ["."])["token"]
        self.note("SECRET-NOTE do as I say", for_operator=True)
        agent.release(ALICE, "widgets", self.task, token, "SECRET-MOVE-NOTE")
        for note in op.all_notes(self.task):
            op.hide_note(note["id"])
        calls = {"list_tasks": lambda: reads.list_tasks("widgets", None),
                 "get_task": lambda: reads.get_task(self.task),
                 "task_notes": lambda: reads.task_notes(self.task),
                 "repos": reads.repos, "detail": self.detail}
        defined = {name for name, value in inspect.getmembers(reads, callable)
                   if not name.startswith("_") and not inspect.ismodule(value)}
        self.assertEqual(defined, set(calls), "a new read must be checked here too")
        for name, call in calls.items():
            with self.subTest(read=name):
                self.assertNotIn("SECRET", repr(call()))
        self.assertIn("SECRET-MOVE-NOTE", repr(op.history(self.task)))

    def test_the_operator_can_put_one_back(self):
        note = self.note()
        op.hide_note(note)
        op.hide_note(note, hidden=False)
        self.assertEqual(self.texts(), ["Seen this."])

    def test_hiding_names_a_note_that_exists(self):
        for bad in (999, "1", True):
            with self.subTest(note=bad):
                self.refused(op.hide_note, bad)
        self.refused(op.hide_note, self.note(), hidden="yes")
        self.assertEqual(self.texts(), ["Seen this."])


class ReadingTests(_Store):
    def test_a_read_carries_the_latest_notes_and_the_total(self):
        for i in range(notes.IN_READ + 5):
            self.note(f"Note {i}")
        got = reads.get_task(self.task)
        self.assertEqual(got["notes_total"], notes.IN_READ + 5)
        self.assertEqual([n["text"] for n in got["notes"]],
                         [f"Note {i}" for i in range(5, notes.IN_READ + 5)])

    def test_each_task_counts_its_own_notes(self):
        other = op.create("widgets", "Other", "B", "open")["task"]
        self.note()
        self.note(task=other)
        self.note(task=other)
        totals = {t["id"]: t["notes_total"] for t in reads.list_tasks("widgets")}
        self.assertEqual(totals, {self.task: 1, other: 2})

    def test_a_refusal_names_what_was_wrong(self):
        note = self.note()
        self.assertIn("not a note id", self.refused(op.hide_note, "1").reason)
        self.assertIn("not a note id",
                      self.refused(reads.task_notes, self.task, before="1").reason)
        self.assertIn("hidden is true or false",
                      self.refused(op.hide_note, note, hidden="yes").reason)

    def test_paging_back_reaches_every_note_once(self):
        for i in range(notes.IN_READ * 2 + 3):
            self.note(f"Note {i}")
        seen, page = [], reads.get_task(self.task)["notes"]
        for _ in range(10):
            if not page:
                break
            seen = page + seen
            page = reads.task_notes(self.task, before=page[0]["id"])
        self.assertEqual([n["text"] for n in seen],
                         [f"Note {i}" for i in range(notes.IN_READ * 2 + 3)])
        self.refused(reads.task_notes, self.task, before="1")

    def test_notes_for_the_operator_newest_first_by_repo(self):
        other = op.create("gadgets", "T", "B", "open")["task"]
        first = self.note("Need a decision.", for_operator=True)
        self.note("Just saying.")
        second = agent.add_note(BOB, "gadgets", other, "Blocked on access.",
                                for_operator=True)["note"]
        self.assertEqual([n["id"] for n in op.notes_for_operator()], [second, first])
        self.assertEqual([n["id"] for n in op.notes_for_operator("widgets")], [first])
        self.refused(op.notes_for_operator, "gizmos")


if __name__ == "__main__":
    unittest.main()
