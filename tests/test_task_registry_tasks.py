# SPDX-License-Identifier: Apache-2.0
"""The task registry's store: the repo list, tasks and their states, how tasks relate
(parent, blockers), and who may make which write (``task-registry/agent_moves.py``,
``operator_moves.py``). Each test gets a store of its own in a temp dir."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from _loader import load_task_registry

reg = load_task_registry()
Refused = reg.taskdb.Refused
agent, op, reads = reg.agent_moves, reg.operator_moves, reg.reads

ALICE = "claude-0b6f3a52-91d4-4c1e-a7e2-5d0c8e2f4b19"
BOB = "claude-7e21c9d0-3a5b-4f86-b0d4-c1e9a2f7d360"


class _Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-tasks-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        previous = reg.taskdb.DB_PATH
        self.addCleanup(setattr, reg.taskdb, "DB_PATH", previous)
        reg.taskdb.DB_PATH = os.path.join(self.dir, "tasks.db")
        reg.taskdb.init()
        op.add_repo("widgets", "github.com/acme/widgets")
        op.add_repo("gadgets")

    def propose(self, title="A task", repo="widgets", who=ALICE, **kw):
        return agent.propose(who, repo, title, "What to do.", **kw)["task"]

    def open_task(self, title="A task", repo="widgets", **kw):
        return op.create(repo, title, "What to do.", "open", **kw)["task"]

    def revision(self, task):
        return reads.get_task(task)["revision"]

    def refused(self, fn, *args, **kw) -> Refused:
        with self.assertRaises(Refused) as caught:
            fn(*args, **kw)
        return caught.exception


class AgentIdTests(unittest.TestCase):
    def test_a_kind_and_a_session_id_is_an_agent(self):
        for good in (ALICE, "opencode-ses_4f2a91c0", "aider-4f2a91c0"):
            with self.subTest(agent=good):
                self.assertEqual(reg.taskdb.check_agent(good), good)

    def test_anything_else_is_refused(self):
        for bad in ("", "operator", "claude", "claude-", "claude-abc", "-4f2a91c0",
                    "Claude-4f2a91c0", "claude 4f2a91c0", "claude-4f2a91c0\n", None, 7,
                    [ALICE]):
            with self.subTest(agent=bad), self.assertRaises(Refused):
                reg.taskdb.check_agent(bad)

    def test_no_agent_can_pass_as_the_operator(self):
        with self.assertRaises(Refused):
            reg.taskdb.check_agent("operator-4f2a91c0")


class RepoListTests(_Store):
    def test_an_unknown_repo_is_refused_with_the_known_names(self):
        refusal = self.refused(self.propose, repo="gizmos")
        self.assertEqual(refusal.current, {"repos": ["gadgets", "widgets"]})

    def test_a_repo_name_is_added_once(self):
        self.refused(op.add_repo, "widgets")

    def test_a_repo_name_and_remote_have_a_shape(self):
        for bad in ("", "Widgets", "-widgets", "wid gets", "acme/widgets"):
            with self.subTest(name=bad):
                self.refused(op.add_repo, bad)
        for bad in ("widgets", "https://github.com/acme/widgets", "github.com/acme widgets"):
            with self.subTest(remote=bad):
                self.refused(op.set_remote, "gadgets", bad)
        for bad in ("github.com/acme", "github.com/../widgets", "../acme/widgets",
                    "github.com/acme/./widgets", "a/b/c/d/e/f"):
            with self.subTest(remote=bad):
                self.refused(op.set_remote, "gadgets", bad)
        op.set_remote("gadgets", "gitlab.com/acme/tools/gadgets")

    def test_a_rename_carries_the_tasks_and_retires_the_old_name(self):
        task = self.propose()
        op.rename_repo("widgets", "sprockets")
        self.assertEqual(reads.get_task(task)["repo"], "sprockets")
        self.refused(self.propose, repo="widgets")
        self.refused(op.rename_repo, "sprockets", "gadgets")

    def test_a_retired_repo_keeps_its_tasks_and_takes_no_new_one(self):
        task = self.propose()
        op.retire_repo("widgets")
        self.refused(self.propose)
        self.refused(op.create, "widgets", "T", "B", "open")
        self.assertEqual([t["id"] for t in reads.list_tasks("widgets", None)["tasks"]], [task])
        op.retire_repo("widgets", retired=False)
        self.propose()

    def test_retiring_takes_true_or_false_only(self):
        self.assertIn("retired is true or false",
                      self.refused(op.retire_repo, "widgets", retired="false").reason)
        self.propose()

    def test_the_repo_list_reads_back(self):
        op.retire_repo("gadgets")
        self.assertEqual(reads.repos(), [
            {"name": "gadgets", "remote": None, "retired": True},
            {"name": "widgets", "remote": "github.com/acme/widgets", "retired": False}])


class ProposeTests(_Store):
    def test_a_proposal_starts_proposed_at_revision_one_under_its_author(self):
        task = self.propose(severity="low", kind="finding")
        got = reads.get_task(task)
        self.assertEqual((got["state"], got["revision"], got["severity"], got["kind"]),
                         ("proposed", 1, "low", "finding"))
        [entry] = op.history(task)["history"]
        self.assertEqual((entry["author"], entry["action"]), (ALICE, "propose"))
        self.assertEqual(entry["detail"]["body"], "What to do.")

    def test_fields_outside_their_bounds_are_refused(self):
        for kw in ({"title": ""}, {"title": "   "}, {"title": "x" * 201},
                   {"severity": "urgent"}, {"kind": "bug"}):
            with self.subTest(**kw):
                self.refused(self.propose, **kw)
        self.refused(agent.propose, ALICE, "widgets", "T", "x" * 32_001)

    def test_a_proposal_needs_an_agent_id(self):
        self.refused(self.propose, who="operator")
        self.assertEqual(reads.list_tasks("widgets", None), {"tasks": [], "total": 0})

    def test_no_agent_write_is_recorded_as_the_operators(self):
        task = self.propose()
        for who in ("operator", "operator-4f2a91c0"):
            with self.subTest(agent=who):
                self.refused(agent.edit_proposal, who, "widgets", task, 1, body="X")
        self.assertEqual([h["author"] for h in op.history(task)["history"]], [ALICE])


class EditProposalTests(_Store):
    def test_an_edit_names_the_revision_it_read(self):
        task = self.propose()
        self.assertEqual(agent.edit_proposal(BOB, "widgets", task, 1, body="Sharper.")
                         ["revision"], 2)
        refusal = self.refused(agent.edit_proposal, ALICE, "widgets", task, 1, body="Mine.")
        self.assertEqual((refusal.current["revision"], refusal.current["body"]),
                         (2, "Sharper."))
        self.assertEqual(refusal.current, reads.get_task(task))
        self.assertEqual([h["author"] for h in op.history(task)["history"]], [ALICE, BOB])

    def test_an_agent_edits_a_spec_only_while_it_is_proposed(self):
        task = self.propose()
        op.move(task, 1, "open")
        refusal = self.refused(agent.edit_proposal, ALICE, "widgets", task, 2, body="Later.")
        self.assertEqual(refusal.current, {"state": "open"})
        self.assertEqual(reads.get_task(task)["body"], "What to do.")

    def test_an_agent_edits_only_its_own_repo(self):
        task = self.propose()
        refusal = self.refused(agent.edit_proposal, ALICE, "gadgets", task, 1, body="X")
        self.assertEqual(refusal.current, {"repo": "widgets"})

    def test_an_edit_that_changes_nothing_is_refused(self):
        task = self.propose()
        self.refused(agent.edit_proposal, ALICE, "widgets", task, 1)
        self.assertEqual(self.revision(task), 1)

    def test_a_refused_edit_writes_nothing(self):
        first, task = self.propose("First"), self.propose("Second")
        self.refused(agent.edit_proposal, ALICE, "widgets", task, 1, body="New",
                     blocked_by=[first, 999])
        got = reads.get_task(task)
        self.assertEqual((got["body"], got["blockers"], got["revision"]),
                         ("What to do.", [], 1))


class OperatorMoveTests(_Store):
    def test_any_state_to_any_state_under_the_operator(self):
        task = self.propose()
        for state in ("open", "landing", "deferred", "closed", "proposed", "open"):
            op.move(task, self.revision(task), state)
            self.assertEqual(reads.get_task(task)["state"], state)
        self.assertEqual({h["author"] for h in op.history(task)["history"][1:]},
                         {reg.taskdb.OPERATOR})
        self.refused(op.move, task, self.revision(task), "done")

    def test_triage_approves_the_spec_as_the_operator_read_it(self):
        task = self.propose()
        read = self.revision(task)
        agent.edit_proposal(ALICE, "widgets", task, read, body="Changed after the read.")
        self.refused(op.move, task, read, "open")
        self.assertEqual(reads.get_task(task)["state"], "proposed")

    def test_the_operator_edits_any_state_and_create_takes_any_state(self):
        task = self.open_task()
        op.edit(task, 1, title="Renamed", severity="high")
        got = reads.get_task(task)
        self.assertEqual((got["title"], got["severity"], got["revision"]), ("Renamed", "high", 2))
        for state in reg.tasks.STATES:
            with self.subTest(state=state):
                created = op.create("gadgets", "T", "B", state)["task"]
                self.assertEqual(reads.get_task(created)["state"], state)
        self.refused(op.create, "gadgets", "T", "B", "done")

    def test_a_stale_edit_or_reparent_is_refused_and_changes_nothing(self):
        parent, task = self.open_task("Parent"), self.open_task("Task")
        op.edit(task, 1, title="Read by another page")
        self.refused(op.edit, task, 1, body="Stale.")
        self.refused(op.reparent, task, 1, parent)
        got = reads.get_task(task)
        self.assertEqual((got["body"], got["parent"], got["revision"]),
                         ("What to do.", None, 2))

    def test_create_keeps_parent_and_blockers_in_the_repo(self):
        elsewhere = self.open_task(repo="gadgets")
        self.refused(self.open_task, parent=elsewhere)
        self.refused(self.open_task, blocked_by=[elsewhere])
        self.assertEqual(reads.list_tasks("widgets", None), {"tasks": [], "total": 0})


class StructureTests(_Store):
    def test_a_parent_lists_its_children(self):
        objective = self.propose("Objective")
        first = self.propose("First", parent=objective)
        second = self.propose("Second", parent=objective)
        got = reads.get_task(objective)
        self.assertEqual([c["id"] for c in got["children"]], [first, second])
        self.assertEqual(reads.get_task(first)["parent"],
                         {"id": objective, "title": "Objective", "state": "proposed"})
        self.assertEqual([t["id"] for t in
                          reads.list_tasks("widgets", None, parent=objective)["tasks"]],
                         [second, first])

    def test_a_parent_and_a_blocker_are_in_the_same_repo(self):
        elsewhere = self.propose(repo="gadgets")
        self.refused(self.propose, parent=elsewhere)
        self.refused(self.propose, blocked_by=[elsewhere])
        self.refused(self.propose, parent=999)
        self.refused(self.propose, blocked_by=[999])

    def test_reparenting_cannot_make_a_loop(self):
        top = self.propose("Top")
        middle = self.propose("Middle", parent=top)
        bottom = self.propose("Bottom", parent=middle)
        self.refused(op.reparent, top, 1, bottom)
        self.refused(op.reparent, top, 1, top)
        op.reparent(bottom, 1, top)
        op.reparent(middle, 1, None)
        self.assertIsNone(reads.get_task(middle)["parent"])
        self.assertEqual([c["id"] for c in reads.get_task(top)["children"]], [bottom])

    def test_blockers_cannot_make_a_cycle(self):
        first = self.propose("First")
        second = self.propose("Second", blocked_by=[first])
        third = self.propose("Third", blocked_by=[second])
        self.refused(agent.edit_proposal, ALICE, "widgets", first, 1, blocked_by=[third])
        self.refused(op.edit, first, 1, blocked_by=[first])
        self.assertEqual(reads.get_task(first)["blockers"], [])

    def test_a_task_is_blocked_until_every_blocker_is_closed(self):
        first, second = self.open_task("First"), self.open_task("Second")
        third = self.open_task("Third", blocked_by=[first, second])

        def blocked():
            return {t["id"]: t["blocked"] for t in reads.list_tasks("widgets")["tasks"]}[third]

        self.assertTrue(blocked())
        op.move(first, 1, "closed")
        self.assertTrue(blocked())
        op.move(second, 1, "landing")
        self.assertTrue(blocked())
        op.move(second, 2, "closed")
        self.assertFalse(blocked())

    def test_blockers_freeze_for_agents_at_open_and_not_for_the_operator(self):
        first = self.propose("First")
        task = self.propose("Second")
        op.move(task, 1, "open")
        self.refused(agent.edit_proposal, ALICE, "widgets", task, 2, blocked_by=[first])
        op.edit(task, 2, blocked_by=[first])
        self.assertEqual([b["id"] for b in reads.get_task(task)["blockers"]], [first])

    def test_an_id_that_is_not_an_int_is_refused_everywhere(self):
        first, task = self.propose("First"), self.propose("Second")
        for bad in (str(task), True, 0, -1, 1.0):
            with self.subTest(task=bad):
                self.refused(agent.edit_proposal, ALICE, "widgets", bad, 1, body="X")
                self.refused(op.reparent, bad, 1, first)
                self.refused(reads.get_task, bad)
        for bad in (str(task), str(first), True):
            with self.subTest(related=bad):
                self.refused(agent.edit_proposal, ALICE, "widgets", task, 1,
                             blocked_by=[bad])
                self.refused(self.propose, parent=bad)
                self.refused(op.reparent, first, 1, bad)
                self.refused(reads.list_tasks, "widgets", None, parent=bad)
        for bad in ("1", True):
            with self.subTest(revision=bad):
                self.refused(agent.edit_proposal, ALICE, "widgets", task, bad, body="X")
        self.assertEqual(self.revision(task), 1)

    def test_a_parent_loop_already_in_the_store_is_refused_not_walked(self):
        top, under = self.propose("Top"), self.propose("Under")
        with reg.taskdb.writing() as conn:
            conn.execute("UPDATE tasks SET parent_id = ? WHERE id = ?", (under, top))
            conn.execute("UPDATE tasks SET parent_id = ? WHERE id = ?", (top, under))
        self.refused(self.propose, parent=under)


class ReadTests(_Store):
    def test_a_list_is_one_repo_in_one_state_open_by_default(self):
        proposed, opened = self.propose("Proposed"), self.open_task("Open")
        self.open_task("Elsewhere", repo="gadgets")
        self.assertEqual([t["id"] for t in reads.list_tasks("widgets")["tasks"]], [opened])
        self.assertEqual([t["id"] for t in reads.list_tasks("widgets", "proposed")["tasks"]],
                         [proposed])
        self.assertEqual([t["id"] for t in reads.list_tasks("widgets", None)["tasks"]],
                         [opened, proposed])
        self.refused(reads.list_tasks, "widgets", "done")

    def test_a_list_comes_a_page_at_a_time_newest_first(self):
        made = [self.open_task(f"Task {i}") for i in range(reads.LIST_MAX + 3)]
        self.propose("Not open")
        first = reads.list_tasks("widgets")
        self.assertEqual(([t["id"] for t in first["tasks"]], first["total"]),
                         (made[3:][::-1], reads.LIST_MAX + 3))
        rest = reads.list_tasks("widgets", before=first["tasks"][-1]["id"])
        self.assertEqual(([t["id"] for t in rest["tasks"]], rest["total"]),
                         (made[:3][::-1], reads.LIST_MAX + 3))
        for bad in ("1", 0, True):
            with self.subTest(before=bad):
                self.refused(reads.list_tasks, "widgets", before=bad)
        self.refused(reads.list_tasks, "widgets", holder="operator")

    def test_an_unknown_task_is_refused(self):
        self.refused(reads.get_task, 999)
        self.refused(op.history, 999)
        self.refused(op.move, 999, 1, "open")

    def test_reads_cross_repos(self):
        task = self.propose(repo="gadgets")
        self.assertEqual(reads.get_task(task)["repo"], "gadgets")


class UnstorableTextTests(_Store):
    """A lone surrogate, which JSON can carry as ``\\ud800``, has no UTF-8 form: it is
    refused like any bad value, not raised as an encoding error."""

    def test_every_text_field_refuses_it(self):
        bad = "half " + "\ud800"
        task = self.propose()
        self.refused(self.propose, title=bad)
        self.refused(agent.propose, ALICE, "widgets", "T", bad)
        self.refused(agent.edit_proposal, ALICE, "widgets", task, 1, body=bad)
        self.refused(op.create, "widgets", bad, "B", "open")
        self.refused(op.edit, task, 1, title=bad)
        self.refused(self.propose, repo="widgets" + "\ud800")
        self.refused(reads.list_tasks, "widgets" + "\ud800")
        self.assertEqual(self.revision(task), 1)


class BoundsTests(_Store):
    def test_an_id_beyond_what_sqlite_holds_is_refused(self):
        for read in (reads.get_task, op.history):
            with self.subTest(read=read.__name__):
                self.refused(read, 2**64)
                self.assertIn("no task", self.refused(read, reg.tasks.ID_MAX).reason)

    def test_a_value_of_the_wrong_type_is_refused_not_raised(self):
        self.refused(self.propose, repo=7)
        self.refused(self.propose, title=7)
        self.refused(agent.propose, ALICE, "widgets", "T", 7)
        self.refused(agent.propose, 7, "widgets", "T", "B")
        self.refused(op.create, "widgets", ["T"], "B", "open")
        self.refused(reads.list_tasks, 7)
        self.refused(reads.search_tasks, "x", repo=7)
        self.refused(self.propose, blocked_by=7)
        task = self.propose()
        self.refused(agent.edit_proposal, ALICE, "widgets", task, 1, blocked_by=7)
        self.assertEqual(reads.list_tasks("widgets", None)["total"], 1)

    def test_padding_counts_toward_a_title(self):
        self.refused(self.propose, title="x" + " " * reg.tasks.TITLE_MAX)
        self.propose(title="x" + " " * (reg.tasks.TITLE_MAX - 1))


class SchemaTests(_Store):
    def test_init_is_idempotent(self):
        task = self.propose()
        reg.taskdb.init()
        self.assertEqual(reads.get_task(task)["title"], "A task")

    def test_a_write_that_raises_leaves_nothing(self):
        with self.assertRaises(Refused), reg.taskdb.writing() as conn:
            reg.taskdb.record(conn, ALICE, "propose", None)
            raise Refused("after the write")
        with reg.taskdb.reading() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM history "
                                          "WHERE author = ?", (ALICE,)).fetchone()[0], 0)

    def test_a_store_from_another_schema_version_is_refused(self):
        conn = sqlite3.connect(reg.taskdb.DB_PATH)
        conn.execute(f"PRAGMA user_version = {reg.taskdb.SCHEMA_VERSION + 1}")
        conn.close()
        with self.assertRaises(RuntimeError):
            reg.taskdb.init()


if __name__ == "__main__":
    unittest.main()
