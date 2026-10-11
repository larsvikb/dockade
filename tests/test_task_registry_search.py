# SPDX-License-Identifier: Apache-2.0
"""The task registry's search (``reads.search_tasks``): what it matches, that a hidden
note never matches, and its bounds. Each test gets a store of its own."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from _loader import load_task_registry

reg = load_task_registry()
Refused = reg.taskdb.Refused
agent, op, reads = reg.agent_moves, reg.operator_moves, reg.reads

ALICE = "claude-0b6f3a52-91d4-4c1e-a7e2-5d0c8e2f4b19"


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-search-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        previous = reg.taskdb.DB_PATH
        self.addCleanup(setattr, reg.taskdb, "DB_PATH", previous)
        reg.taskdb.DB_PATH = os.path.join(self.dir, "tasks.db")
        reg.taskdb.init()
        op.add_repo("widgets")
        op.add_repo("gadgets")

    def task(self, title="A task", body="What to do.", repo="widgets", state="open"):
        return op.create(repo, title, body, state)["task"]

    def note(self, task, text, repo="widgets"):
        return agent.add_note(ALICE, repo, task, text)["note"]

    def found(self, text, **kw):
        return [t["id"] for t in reads.search_tasks(text, **kw)["tasks"]]

    def refused(self, fn, *args, **kw) -> Refused:
        with self.assertRaises(Refused) as caught:
            fn(*args, **kw)
        return caught.exception

    def test_title_body_and_notes_match_newest_first(self):
        by_title = self.task(title="Rotate the token")
        by_body = self.task(body="The TOKEN expires.")
        by_note = self.task()
        noted = self.note(by_note, "Saw a stale TOKEN here.")
        self.note(by_note, "Unrelated.")
        self.task(title="Something else")
        got = reads.search_tasks("Token")
        self.assertEqual(([t["id"] for t in got["tasks"]], got["total"]),
                         ([by_note, by_body, by_title], 3))
        self.assertEqual([(t["matched_notes"], t["matched_notes_total"])
                          for t in got["tasks"]], [([noted], 1), ([], 0), ([], 0)])
        listed = {t["id"]: t for t in reads.list_tasks("widgets")["tasks"]}
        self.assertEqual({k: v for k, v in got["tasks"][0].items()
                          if not k.startswith("matched_")}, listed[by_note])

    def test_case_is_folded_beyond_ascii_and_nothing_is_a_wildcard(self):
        task = self.task(title="Åtgärda straße")
        self.assertEqual(self.found("åTGÄRDA"), [task])
        self.assertEqual(self.found("STRASSE"), [task])
        underscored = self.task(title="a_b")
        self.assertEqual(self.found("_"), [underscored])
        self.assertEqual(self.found("%"), [])

    def test_every_state_and_every_repo_unless_one_is_named(self):
        closed = self.task(title="Old fix", state="closed")
        proposed = self.task(title="New fix", state="proposed")
        elsewhere = self.task(title="Their fix", repo="gadgets")
        self.assertEqual(self.found("fix"), [elsewhere, proposed, closed])
        self.assertEqual(self.found("fix", repo="widgets"), [proposed, closed])
        op.retire_repo("gadgets")
        self.assertEqual(self.found("fix", repo="gadgets"), [elsewhere])
        self.refused(reads.search_tasks, "fix", "gizmos")

    def test_a_hidden_note_never_matches(self):
        only_hidden = self.task()
        op.hide_note(self.note(only_hidden, "The vault code is 1234."))
        both = self.task()
        shown = self.note(both, "The vault is fine.")
        op.hide_note(self.note(both, "The vault is open."))
        got = reads.search_tasks("vault")
        self.assertEqual([(t["id"], t["matched_notes"]) for t in got["tasks"]],
                         [(both, [shown])])
        self.assertEqual(got["total"], 1)

    def test_a_hit_names_its_latest_matching_notes_and_their_total(self):
        task = self.task()
        made = [self.note(task, f"flaky build {i}") for i in range(reg.notes.IN_READ + 3)]
        [hit] = reads.search_tasks("flaky")["tasks"]
        self.assertEqual((hit["matched_notes"], hit["matched_notes_total"]),
                         (made[3:], reg.notes.IN_READ + 3))

    def test_a_search_returns_at_most_a_page_and_the_total(self):
        made = [self.task(title=f"Widget {i}") for i in range(reads.SEARCH_MAX + 3)]
        got = reads.search_tasks("widget")
        self.assertEqual(got["total"], reads.SEARCH_MAX + 3)
        self.assertEqual([t["id"] for t in got["tasks"]], made[3:][::-1])

    def test_the_text_has_bounds(self):
        self.task(body="x" * 300)
        for bad in ("", "   ", "x" * (reads.SEARCH_TEXT_MAX + 1), None, 7, "half \ud800"):
            with self.subTest(text=bad):
                self.refused(reads.search_tasks, bad)
        self.assertEqual(len(self.found("x" * reads.SEARCH_TEXT_MAX)), 1)


if __name__ == "__main__":
    unittest.main()
