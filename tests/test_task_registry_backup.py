# SPDX-License-Identifier: Apache-2.0
"""The task registry's backup (``task-registry/backup.py``) and export
(``task-registry/export.py``) on scratch stores, and the Makefile's targets that run
them in a container held to compose."""
from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
import unittest
from unittest import mock

from _loader import ROOT, load_task_registry_backup
from test_topology import COMPOSE, _block, _service

reg = load_task_registry_backup()
backup, export, op, agent = reg.backup, reg.export, reg.operator_moves, reg.agent_moves

ALICE = "claude-0b6f3a52-91d4-4c1e-a7e2-5d0c8e2f4b19"


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class _Stores(unittest.TestCase):
    """A directory of scratch stores; ``use`` points the registry at one."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-backup-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        previous = reg.taskdb.DB_PATH
        self.addCleanup(setattr, reg.taskdb, "DB_PATH", previous)

    def use(self, name: str, *, init: bool = True) -> str:
        path = os.path.join(self.dir, name, "tasks.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        reg.taskdb.DB_PATH = path
        if init:
            reg.taskdb.init()
        return path

    def fill(self, repo: str = "widgets", n: int = 2) -> list[int]:
        op.add_repo(repo)
        return [op.create(repo, f"Task {i}", f"Body {i}", "open")["task"] for i in range(n)]

    def snapshot(self) -> bytes:
        out = io.BytesIO()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(backup.snapshot(out))
        return out.getvalue()

    def restore(self, data: bytes) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            backup.restore(io.BytesIO(data))
        return err.getvalue()

    def refused(self, data: bytes) -> str:
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            backup.restore(io.BytesIO(data))
        return str(caught.exception.code)

    def rows(self, path: str) -> dict[str, list[tuple]]:
        conn = sqlite3.connect(path)
        try:
            return {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()  # noqa: S608 (fixed names)
                    for t in backup.TABLES}
        finally:
            conn.close()

    def as_v1(self, data: bytes) -> bytes:
        """``data`` as a store from before plans: no plan tables, at v1."""
        path = self.write("v1.db", data)
        conn = sqlite3.connect(path)
        conn.executescript("DROP TABLE plan_paths; DROP TABLE plans; "
                           "PRAGMA user_version = 1;")
        conn.close()
        return _read(path)

    def planned(self, repo: str = "widgets") -> dict:
        """A proposal in ``repo`` refined into a plan; the reply of ``submit_plan``."""
        task = agent.propose(ALICE, repo, "Rotate keys", "Every 90 days.")["task"]
        token = agent.claim(ALICE, repo, task, "rotate-keys", ["."])["token"]
        return agent.submit_plan(ALICE, repo, task, token, "Rotate in place.",
                                 ["src/keys.py", "tests"], "Old keys still read.",
                                 "None.", "A reader mid-rotation.", 2, "Passes.")

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path


class SnapshotTests(_Stores):
    def test_the_snapshot_is_the_store_whole(self):
        src = self.use("live")
        task = self.fill()[0]
        agent.claim(ALICE, "widgets", task, "fix-it", ["."])
        agent.add_note(ALICE, "widgets", task, "Seen.")
        copy = self.write("copy.db", self.snapshot())
        self.assertEqual(self.rows(copy), self.rows(src))

    def test_it_takes_what_is_still_in_the_wal(self):
        # A connection left open keeps the last write in the -wal, unmerged, as a live
        # registry's would be; the snapshot must carry it.
        src = self.use("live")
        self.fill()
        holder = sqlite3.connect(src)
        self.addCleanup(holder.close)
        holder.execute("SELECT 1 FROM tasks").fetchall()
        late = op.create("widgets", "Late", "Written last.", "open")["task"]
        self.assertGreater(os.path.getsize(src + "-wal"), 0)
        conn = sqlite3.connect(self.write("copy.db", self.snapshot()))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT title FROM tasks WHERE id = ?",
                                      (late,)).fetchone(), ("Late",))

    def test_it_is_one_file_and_leaves_none_behind(self):
        src = self.use("live")
        self.fill()
        data = self.snapshot()
        self.assertTrue(data.startswith(b"SQLite format 3\x00"))
        # Bytes 18 and 19 are 1 in rollback mode, 2 in WAL: a restore moves one file.
        self.assertEqual((data[18], data[19]), (1, 1))
        self.assertEqual(sorted(f for f in os.listdir(os.path.dirname(src))
                                if not f.startswith("tasks.db")), [])
        self.assertFalse(os.path.exists(src + ".backup-snapshot"))

    def test_a_snapshot_left_by_an_interrupted_run_is_replaced(self):
        src = self.use("live")
        self.fill()
        self.write(os.path.join("live", "tasks.db.backup-snapshot"), b"half a copy")
        copy = self.write("copy.db", self.snapshot())
        self.assertEqual(self.rows(copy), self.rows(src))

    def test_no_store_yet_writes_nothing_and_says_so(self):
        self.use("empty", init=False)
        out, err = io.BytesIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(backup.snapshot(out))
        self.assertEqual(out.getvalue(), b"")
        self.assertIn("nothing to back up", err.getvalue())

    def test_main_takes_one_command(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(backup.main([]), 2)
            self.assertEqual(backup.main(["snapshot", "extra"]), 2)
            self.assertEqual(backup.main(["export"]), 2)

    def test_main_snapshots_to_stdout_and_restores_from_stdin(self):
        src = self.use("live")
        self.fill()
        before = self.rows(src)
        out = io.TextIOWrapper(io.BytesIO())
        with mock.patch.object(backup.sys, "stdout", out), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(backup.main(["snapshot"]), 0)
        data = out.buffer.getvalue()
        self.use("other")
        with mock.patch.object(backup.sys, "stdin", io.TextIOWrapper(io.BytesIO(data))), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(backup.main(["restore"]), 0)
        self.assertEqual(self.rows(reg.taskdb.DB_PATH), before)


class RestoreTests(_Stores):
    def setUp(self):
        super().setUp()
        self.use("old")
        self.fill("widgets", 2)
        self.backup = self.snapshot()
        self.live = self.use("live")
        self.fill("gadgets", 1)

    def test_it_replaces_the_store(self):
        self.assertIn("2 tasks", self.restore(self.backup))
        self.assertEqual([r["name"] for r in reg.reads.repos()], ["widgets"])
        self.assertFalse(os.path.exists(self.live + ".restore-incoming"))

    def test_it_removes_a_replaced_stores_sidecars(self):
        # Sidecars whose store was deleted by hand: reading the id counters opens the
        # store, which takes its sidecars with it, so here nothing does that first.
        live = self.use("fresh", init=False)
        for side in ("-wal", "-shm"):
            self.write(os.path.join("fresh", "tasks.db" + side), b"left behind" * 100)
        self.restore(self.backup)
        for side in ("-wal", "-shm"):
            self.assertFalse(os.path.exists(live + side), side)

    def test_the_registry_opens_what_it_restored(self):
        self.restore(self.backup)
        reg.taskdb.init()
        self.assertEqual(reg.reads.list_tasks("widgets")["total"], 2)

    def assert_refused(self, data: bytes, why: str) -> None:
        before = _read(self.live)
        self.assertIn(why, self.refused(data))
        self.assertEqual(_read(self.live), before)
        self.assertFalse(os.path.exists(self.live + ".restore-incoming"))

    def with_version(self, version: int) -> bytes:
        path = self.write("versioned.db", self.backup)
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.close()
        return _read(path)

    def test_a_file_that_is_not_sqlite_is_refused(self):
        self.assert_refused(b"not a database at all, but long enough to look" * 20,
                            "not a readable SQLite database")

    def test_another_stores_database_is_refused(self):
        path = os.path.join(self.dir, "control.db")
        conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE rules(x); CREATE TABLE approvals(x); "
                           "CREATE TABLE audit(x); CREATE TABLE audit_cursor(x);")
        conn.close()
        self.assert_refused(_read(path), "not a task registry store")

    def test_a_newer_schema_is_refused(self):
        self.assert_refused(self.with_version(reg.taskdb.SCHEMA_VERSION + 1),
                            "update the image first")

    def test_a_schema_the_registry_would_not_open_is_refused(self):
        self.assert_refused(self.with_version(-1), "opens v1, v2 only")

    def test_a_v1_backup_is_restored_and_taken_forward_at_the_next_start(self):
        self.assertIn("schema-v1 backup", self.restore(self.as_v1(self.backup)))
        reg.taskdb.init()
        with reg.taskdb.reading() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             reg.taskdb.SCHEMA_VERSION)
        self.assertEqual(reg.reads.list_tasks("widgets")["total"], 2)
        self.assertEqual(self.planned()["plan"], 1)

    def test_a_v2_backup_without_its_plan_tables_is_refused(self):
        path = self.write("no-plans.db", self.backup)
        conn = sqlite3.connect(path)
        conn.executescript("DROP TABLE plan_paths; DROP TABLE plans;")
        conn.close()
        self.assert_refused(_read(path), "missing table(s): plan_paths, plans")

    def test_plan_ids_carry_on_past_a_v1_restore(self):
        # A plan id the live store handed out must not name another plan after a
        # restore of a backup from before plans existed.
        reg.taskdb.DB_PATH = self.live
        self.assertEqual(self.planned("gadgets")["plan"], 1)
        self.restore(self.as_v1(self.backup))
        reg.taskdb.init()
        self.assertEqual(self.planned()["plan"], 2)

    def test_a_store_never_versioned_is_one_the_registry_opens(self):
        self.restore(self.with_version(0))
        reg.taskdb.init()
        self.assertEqual(reg.reads.list_tasks("widgets")["total"], 2)

    def test_a_corrupt_file_is_refused(self):
        data = bytearray(self.backup)
        page = int.from_bytes(data[16:18], "big") or 65536
        for at in range(page * 2, len(data), 7):
            data[at] = 0xFF
        self.assert_refused(bytes(data), "REFUSED")

    def test_a_store_it_cannot_replace_is_left_as_it_was(self):
        with mock.patch.object(backup.os, "replace", side_effect=PermissionError("sticky")):
            self.assert_refused(self.backup, "could not replace")

    def next_ids(self) -> tuple[int, int]:
        task = op.create("widgets", "Next", "After the restore.", "open")["task"]
        return task, agent.add_note(ALICE, "widgets", task, "First after.")["note"]

    def test_ids_carry_on_from_the_replaced_store(self):
        # The live store has handed out tasks 1 to 5 and a note since the backup's
        # three: none of those ids may name a different task or note after it.
        self.fill("more", 4)
        agent.add_note(ALICE, "gadgets", 1, "One.")
        self.restore(self.backup)
        self.assertEqual(self.next_ids(), (6, 2))

    def test_ids_carry_on_from_the_backup_when_it_is_further_on(self):
        self.use("old")
        self.fill("extra", 6)
        newer = self.snapshot()
        reg.taskdb.DB_PATH = self.live
        self.restore(newer)
        self.assertEqual(self.next_ids()[0], 9)

    def test_a_broken_store_does_not_stop_its_restore(self):
        with open(self.live, "wb") as f:
            f.write(b"garbage where a store was" * 100)
        self.assertIn("could not read the current store's id counters",
                      self.restore(self.backup))
        self.assertEqual(reg.reads.list_tasks("widgets")["total"], 2)

    def test_a_copy_of_a_wal_store_leaves_no_sidecar_behind(self):
        # The store's own file, its header saying WAL, rather than a snapshot.
        self.use("old")
        wal_copy = _read(reg.taskdb.DB_PATH)
        self.assertEqual((wal_copy[18], wal_copy[19]), (2, 2))
        reg.taskdb.DB_PATH = self.live
        self.restore(wal_copy)
        self.assertEqual(os.listdir(os.path.dirname(self.live)), ["tasks.db"])
        self.assertEqual(reg.reads.list_tasks("widgets")["total"], 2)

    def test_a_volume_without_a_store_is_restored_into(self):
        self.use("fresh", init=False)
        self.assertNotIn("could not read", self.restore(self.backup))
        self.assertEqual(reg.reads.list_tasks("widgets")["total"], 2)

    def test_no_counter_is_taken_back(self):
        # Rows the registry never deletes, deleted here so the backup's counter is
        # ahead of its rows: only the higher of the two counters may stand.
        self.use("old")
        tail = self.fill("extra", 6)
        with reg.taskdb.writing() as conn:
            conn.execute("DELETE FROM history")
            conn.execute(f"DELETE FROM tasks WHERE id IN ({','.join('?' * 3)})", tail[-3:])  # noqa: S608 (placeholders)
        ahead = self.snapshot()
        reg.taskdb.DB_PATH = self.live
        self.restore(ahead)
        self.assertEqual(self.next_ids()[0], 9)


class InlineTests(unittest.TestCase):
    def unspan(self, span: str) -> str:
        fence = re.match(r"`+", span).group(0)
        self.assertTrue(span.endswith(fence))
        inner = span[len(fence):-len(fence)]
        # A run of other length neither opens nor closes it, so a backtick against
        # either fence would make that fence a longer run.
        self.assertNotIn(fence, inner)
        self.assertFalse(inner.startswith("`") or inner.endswith("`"), span)
        # CommonMark's one stripped space from each end, when both have one.
        if inner.startswith(" ") and inner.endswith(" ") and inner.strip(" "):
            inner = inner[1:-1]
        return inner

    def test_a_span_shows_the_text_exactly(self):
        for text in ("plain", "a `tick` in it", "`leading", "trailing`", "``double``",
                     " both ends ", "<b>html</b> [link](x) ![img](y) *em*"):
            with self.subTest(text=text):
                self.assertEqual(self.unspan(export.inline(text)), text)

    def test_hidden_characters_and_line_breaks_are_spelled_out(self):
        self.assertEqual(export.inline("a\nb\N{RIGHT-TO-LEFT OVERRIDE}c"
                                       "\N{ZERO WIDTH SPACE}d\x00e\N{LINE SEPARATOR}f"),
                         "`a\\u000ab\\u202ec\\u200bd\\u0000e\\u2028f`")
        self.assertEqual(export.spelled("\U000e0001"), "\\U000e0001")
        self.assertEqual(export.spelled("a\N{PARAGRAPH SEPARATOR}b"), "a\\u2029b")
        # A format character Unicode does not list as ignorable: the category holds it.
        self.assertEqual(export.spelled("a\N{INTERLINEAR ANNOTATION ANCHOR}b"), "a\\ufff9b")

    def test_what_unicode_draws_as_nothing_is_spelled_out(self):
        # Default_Ignorable_Code_Point, whatever its category: a variation selector
        # run hides a payload after one letter, and is a mark, not a format character.
        for c, shown in (("\N{VARIATION SELECTOR-16}", "\\ufe0f"),
                         ("\N{VARIATION SELECTOR-17}", "\\U000e0100"),
                         ("\N{COMBINING GRAPHEME JOINER}", "\\u034f"),
                         ("\N{HANGUL FILLER}", "\\u3164"),
                         ("\N{MONGOLIAN FREE VARIATION SELECTOR ONE}", "\\u180b")):
            with self.subTest(c=hex(ord(c))):
                self.assertNotIn(unicodedata.category(c), ("Cc", "Cf"))
                self.assertEqual(export.spelled(f"a{c}b"), f"a{shown}b")
        prose = "caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{EM DASH} \N{CHECK MARK}"
        self.assertEqual(export.spelled(prose), prose)

    def test_an_empty_value_is_named(self):
        self.assertEqual(export.inline(""), "(empty)")


class BlockTests(unittest.TestCase):
    def test_no_line_inside_can_close_the_fence(self):
        text = "```\nfour ````\n`````\n## heading\n"
        block = export.block(text)
        fence = block.split("\n", 1)[0].rstrip("text")
        self.assertEqual(fence, "`" * 6)
        self.assertEqual(block, f"{fence}text\n{text}\n{fence}")

    def test_lines_and_tabs_stay_and_the_rest_is_spelled_out(self):
        self.assertEqual(export.block("a\tb\nc\rd\N{RIGHT-TO-LEFT OVERRIDE}"),
                         "```text\na\tb\nc\\u000dd\\u202e\n```")


def _headings(markdown: str) -> list[str]:
    """The headings a CommonMark reader would see: lines starting with ``#`` outside
    every fenced block."""
    found, fence = [], None
    for line in markdown.split("\n"):
        run = re.match(r"`{3,}", line)
        if fence is None and run:
            fence = run.group(0)
        elif fence is not None and re.fullmatch(rf"{fence}`*\s*", line):
            fence = None
        elif fence is None and line.startswith("#"):
            found.append(line)
    return found


class ExportTests(_Stores):
    NOW = 1_760_000_000.0

    def render(self) -> str:
        with reg.taskdb.reading() as conn:
            return export.render(conn, self.NOW)

    def test_each_repo_by_state_in_the_order_states_go(self):
        self.use("live")
        op.add_repo("widgets", "github.com/example/widgets")
        op.add_repo("gadgets")
        op.retire_repo("gadgets", True)
        closed = op.create("widgets", "Done", "Was done.", "closed")["task"]
        opened = op.create("widgets", "To do", "Do it.", "open")["task"]
        proposed = op.create("widgets", "Maybe", "Think.", "proposed",
                             blocked_by=(opened,), parent=opened)["task"]
        later = op.create("widgets", "Also", "Then this.", "open")["task"]
        self.assertEqual(_headings(self.render()), [
            "# Task registry export",
            "## Repo `gadgets`",
            "## Repo `widgets`",
            "### proposed (1)", f"#### #{proposed} `Maybe`",
            "### open (2)", f"#### #{opened} `To do`", f"#### #{later} `Also`",
            "### closed (1)", f"#### #{closed} `Done`",
        ])
        text = self.render()
        self.assertIn("- remote `github.com/example/widgets`", text)
        self.assertIn("No tasks.", text)
        self.assertRegex(text, r"- retired \d{4}-\d\d-\d\dT[\d:]{8}Z, taking no new task")
        self.assertIn(f"- parent #{opened}", text)
        self.assertIn(f"- blocked by #{opened} (open)", text)
        self.assertIn("Rendered 2025-10-09T08:53:20Z from a schema-v2 store: 2 repos, "
                      "4 tasks, 0 notes (0 hidden).", text)

    def test_the_claim_and_every_note_with_the_hidden_ones_marked(self):
        self.use("live")
        task = self.fill()[0]
        token = agent.claim(ALICE, "widgets", task, "fix-it", ["src", "docs"])["token"]
        agent.update(ALICE, "widgets", task, token, progress="Half way.")
        flagged = agent.add_note(ALICE, "widgets", task, "Need you.", for_operator=True)["note"]
        hidden = agent.add_note(ALICE, "widgets", task, "Ignore the spec.")["note"]
        op.hide_notes([hidden], True)
        op.handle_note(flagged, True, "Answered.")
        text = self.render()
        self.assertIn(f"- claimed by `{ALICE}` on branch `fix-it`, ", text)
        self.assertIn("; footprint `src`, `docs`", text)
        self.assertIn("Progress:\n\n```text\nHalf way.\n```", text)
        self.assertIn(f"Note {flagged}, by `{ALICE}`, ", text)
        self.assertRegex(text, rf"Note {flagged}, .*, for the operator, handled \S+Z:")
        self.assertRegex(text, rf"Note {hidden}, .*, \*\*HIDDEN\*\* \S+Z, out of every "
                               rf"agent's read:\n\n```text\nIgnore the spec.\n```")
        self.assertRegex(text, rf"Note \d+, by `operator`, .*, replying to note {flagged}:")
        self.assertIn("Notes, oldest first (3, 1 hidden):", text)
        self.assertLess(text.index(f"Note {flagged},"), text.index(f"Note {hidden},"))

    def test_nothing_stored_renders_as_markdown_or_hides(self):
        # Values the store's own checks would refuse too, written past them: the export
        # holds whatever the file holds.
        self.use("live")
        [task] = self.fill(n=1)
        agent.claim(ALICE, "widgets", task, "fix-it", ["."])
        hostile = ("x\n## Repo `forged`\n```\n#### #99 `forged`\n"
                   "\N{RIGHT-TO-LEFT OVERRIDE}esrever\N{ZERO WIDTH SPACE} <img src=x>")
        with reg.taskdb.writing() as conn:
            conn.execute("UPDATE tasks SET title = ?, body = ?, state = ?, severity = ?, "
                         "kind = ?", (hostile, hostile, "## not a state", "`hi`", hostile))
            conn.execute("UPDATE repos SET name = ?, remote = ?", (hostile, hostile))
            conn.execute("UPDATE claims SET holder = ?, branch = ?, progress = ?, "
                         "footprint = ?", (hostile, "`b`", hostile, '["a`b", "\\n# x"]'))
            conn.execute("INSERT INTO notes(task_id, author, text, for_operator, move, "
                         "created_at) VALUES (?, ?, ?, 0, ?, 0)",
                         (task, hostile, hostile, hostile))
            plan = conn.execute(
                "INSERT INTO plans(task_id, task_revision, author, created_at, approach, "
                "tests, out_of_scope, risks, review_rounds, review_verdict) "
                "VALUES (?, 1, ?, 0, ?, ?, ?, ?, 1, ?)",
                (task, hostile, *[hostile] * 5)).lastrowid
            conn.execute("INSERT INTO plan_paths(plan_id, path) VALUES (?, ?)",
                         (plan, hostile))
        text = self.render()
        self.assertEqual(_headings(text), [
            "# Task registry export", f"## Repo {export.inline(hostile)}",
            "### `## not a state` (1)", f"#### #{task} {export.inline(hostile)}"])
        self.assertIn(f"- remote {export.inline(hostile)}", text)
        self.assertIn(f", kind {export.inline(hostile)},", text)
        self.assertEqual(export.inline(hostile).count("\n"), 0)
        self.assertEqual([c for c in text if c != "\n"
                          and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")], [])
        self.assertIn("severity `` `hi` ``", text)
        self.assertIn("on branch `` `b` ``", text)
        self.assertIn("footprint ``a`b``, `\\u000a# x`", text)
        self.assertIn(f", with {export.inline(hostile)}:", text)
        self.assertIn(f"Plan {plan}, by {export.inline(hostile)}, ", text)
        self.assertIn(f"- footprint {export.inline(hostile)}", text)

    def test_each_plan_with_the_superseded_ones_marked(self):
        self.use("live")
        op.add_repo("widgets")
        first = self.planned()
        task = reg.reads.get_task(first["task"])
        agent.claim(ALICE, "widgets", task["id"], "again", ["."])
        text = self.render()
        self.assertRegex(text, rf"Plan {first['plan']}, by `{ALICE}`, \S+Z, at task "
                               rf"revision {first['revision']}, 2 review round\(s\), "
                               rf"\*\*SUPERSEDED\*\* \S+Z:")
        self.assertIn("- footprint `src/keys.py`, `tests`", text)
        for part, value in (("Approach", "Rotate in place."), ("Tests", "Old keys still read."),
                            ("Out of scope", "None."), ("Risks", "A reader mid-rotation."),
                            ("Review verdict", "Passes.")):
            self.assertIn(f"{part}:\n\n```text\n{value}\n```", text)
        self.assertLess(text.index("Body:"), text.index(f"Plan {first['plan']}"))

    def test_a_v1_store_renders_without_plans(self):
        self.use("live")
        self.fill()
        text = export.main(self.as_v1(self.snapshot()))
        self.assertIn("from a schema-v1 store: 1 repos, 2 tasks", text)
        self.assertNotIn("Plan ", text)

    def test_a_footprint_that_is_not_a_list_of_paths_is_shown_as_stored(self):
        for stored in ("not json", "[]", '{"a": 1}', "[1]"):
            with self.subTest(stored=stored):
                self.assertEqual(export._footprint(stored), export.inline(stored))

    def test_an_unknown_state_follows_the_known_ones(self):
        self.use("live")
        first, second = self.fill()
        with reg.taskdb.writing() as conn:
            conn.execute("UPDATE tasks SET state = 'archived' WHERE id = ?", (first,))
        self.assertEqual(_headings(self.render())[2:],
                         ["### open (1)", f"#### #{second} `Task 1`",
                          "### `archived` (1)", f"#### #{first} `Task 0`"])

    def test_main_reads_a_snapshot_and_nothing_else(self):
        self.use("live")
        self.fill()
        data = self.snapshot()
        self.assertTrue(export.main(data).startswith("# Task registry export\n"))
        # A copy whose header says WAL, which a database in memory cannot be.
        wal_copy = _read(reg.taskdb.DB_PATH)
        self.assertEqual(wal_copy[18], 2)
        self.assertIn("## Repo `widgets`", export.main(wal_copy))
        with self.assertRaises(SystemExit) as caught:
            export.main(b"# a markdown file\n")
        self.assertIn("not a SQLite database", str(caught.exception.code))
        newer = reg.taskdb.SCHEMA_VERSION + 1
        path = self.write("newer.db", data)
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {newer}")
        conn.close()
        with self.assertRaises(SystemExit) as caught:
            export.main(_read(path))
        self.assertIn(f"schema v{newer}", str(caught.exception.code))


class MakefileTargetsAgreeTests(unittest.TestCase):
    """``make backup`` and ``make restore-tasks`` reach the registry's store through
    ``docker run``, so its image, volume and mount path are restated in the Makefile;
    held to compose here, as ``BackupToolTargetsAgreeTests`` holds the control plane's.
    A volume name that drifts is one docker creates empty: a backup with nothing to
    keep, and a restore into a store nothing serves."""

    MAKEFILE = (ROOT / "Makefile").read_text()

    def variable(self, name: str) -> str:
        m = re.search(rf"^{name}\s*:?=\s*((?:.*\\\n)*.*)", self.MAKEFILE, re.M)
        self.assertIsNotNone(m, f"no {name} in the Makefile")
        return m.group(1)

    def recipe(self, target: str) -> str:
        m = re.search(rf"^{target}:.*\n((?:\t.*\n|\n)*)", self.MAKEFILE, re.M)
        self.assertIsNotNone(m, f"no {target} target")
        return m.group(1)

    def test_the_image_is_the_one_compose_builds(self):
        declared = [line.split(":", 1)[1].strip() for line in _service("tasks")
                    if line.strip().startswith("image:")]
        self.assertEqual(declared, [self.variable("TASKS_IMAGE")])

    def test_the_volume_is_the_one_compose_names(self):
        names = [line.split(":", 1)[1].strip()
                 for line in _block(_block(COMPOSE, "volumes", 0), "tasks-state", 2)
                 if line.strip().startswith("name:")]
        self.assertEqual(names, [self.variable("TASKS_VOLUME")])

    def test_it_is_mounted_where_the_service_mounts_it(self):
        [mount] = [line.strip().lstrip("- ") for line in _service("tasks")
                   if line.strip().startswith("- tasks-state:")]
        path = mount.split(":", 1)[1]
        self.assertIn(f"-v $(TASKS_VOLUME):{path} ", self.variable("TASKS_TOOL"))
        self.assertEqual(os.path.dirname(reg.taskdb.DB_PATH), path)

    def test_the_containers_join_no_network_and_only_the_tool_gets_the_store(self):
        tool, render = self.variable("TASKS_TOOL"), self.variable("TASKS_EXPORT")
        for line in (tool, render):
            self.assertIn("--network none", line)
            self.assertIn("$(TASKS_IMAGE)", line)
        self.assertIn("--user $(DOCKADE_UID):$(DOCKADE_GID)", tool)
        self.assertNotIn("-v ", render)

    def test_each_target_runs_its_module(self):
        backup_recipe = self.recipe("backup")
        self.assertIn("$(TASKS_TOOL) backup.py snapshot", backup_recipe)
        self.assertIn("$(TASKS_EXPORT) export.py", backup_recipe)
        self.assertIn("$(TASKS_TOOL) backup.py restore", self.recipe("restore-tasks"))

    def test_restore_tasks_stops_the_registry_before_it_restores(self):
        recipe = self.recipe("restore-tasks")
        self.assertLess(recipe.index("stop tasks"), recipe.index("backup.py restore"))
        self.assertIn("\nrestore-tasks: tasks-tool-preflight ", self.MAKEFILE)

    def test_an_image_without_the_tool_is_named_before_anything_stops(self):
        # `make up` never rebuilds the profile's image, so one from before backup.py
        # is what a host has until `make tasks-up`.
        self.assertIn("--entrypoint test $(TASKS_IMAGE) -f backup.py",
                      self.recipe("tasks-tool-preflight"))
        recipe = self.recipe("backup")
        self.assertIn("if ! $(TASKS_TOOL) backup.py snapshot", recipe)
        self.assertIn("make tasks-up rebuilds it", recipe)

    def test_each_restore_refuses_the_others_backup_before_it_stops_anything(self):
        for target, other in (("restore", "dockade-tasks-*)"),
                              ("restore-tasks", "dockade-control-*)")):
            with self.subTest(target=target):
                recipe = self.recipe(target)
                self.assertLess(recipe.index(other), recipe.index(" stop "))
