# SPDX-License-Identifier: Apache-2.0
"""``control-plane/store_backup.py``, the control plane's twin of
``task-registry/backup.py``, run as the Makefile's ``CONTROL_TOOL`` runs it on scratch
stores. Each run is from ``control-plane/``, the image's WORKDIR, so ``import store``
resolves as it does there. Not under ``-I``: that drops the script's directory from the
import path, and a program whose ``import store`` fails writes nothing for a test to
find left behind."""
from __future__ import annotations

import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _loader import ROOT, load_control_plane

cp = load_control_plane()

CONTROL_PLANE = ROOT / "control-plane"
MAKEFILE = (ROOT / "Makefile").read_text()
TABLES = ("rules", "approvals", "audit", "audit_cursor")
REFUSED = "restore: REFUSED, store untouched — "


def _run(db: str, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 (this interpreter, the repo's own program)
        [sys.executable, "-B", "store_backup.py", *args], input=stdin,
        capture_output=True, check=False, cwd=CONTROL_PLANE,
        env={"CONTROL_DB": db, "PATH": os.environ.get("PATH", "")})


def _store(path: str, tables: tuple[str, ...], rows: int = 0) -> None:
    conn = sqlite3.connect(path)
    for t in tables:
        conn.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, x)")
        conn.executemany(f"INSERT INTO {t}(x) VALUES (?)",  # noqa: S608 (fixed names)
                         [(i,) for i in range(rows)])
    conn.commit()
    conn.close()


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _counts(path: str) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 (fixed names)
                for t in ("rules", "approvals", "audit")}
    finally:
        conn.close()


class BackupProgramTests(unittest.TestCase):

    def setUp(self):
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        self.db = os.path.join(self.dir, "control.db")

    def test_a_store_streams_a_snapshot_and_leaves_none(self):
        _store(self.db, ("rules", "approvals", "audit"))
        done = _run(self.db, "snapshot")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith(b"SQLite format 3\0"))
        self.assertFalse(os.path.exists(self.db + ".backup-snapshot"))
        self.assertTrue(os.path.isfile(self.db))

    def test_a_failure_after_the_snapshot_is_written_removes_it(self):
        # No `audit` table: the COUNT fails on the snapshot VACUUM INTO has written.
        _store(self.db, ("rules", "approvals"))
        done = _run(self.db, "snapshot")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(b"OperationalError: no such table: audit", done.stderr)
        self.assertFalse(os.path.exists(self.db + ".backup-snapshot"))

    def test_stdout_holds_the_store_and_nothing_else(self):
        _store(self.db, TABLES, rows=3)
        done = _run(self.db, "snapshot")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(b"backup:", done.stdout)
        self.assertIn(b"backup: schema v0, 3 rules, 3 approvals, 3 audit", done.stderr)
        out = os.path.join(self.dir, "out.db")
        with open(out, "wb") as f:
            f.write(done.stdout)
        self.assertEqual(_counts(out), _counts(self.db))

    def test_a_stale_snapshot_is_replaced(self):
        # Left by an interrupted VACUUM INTO; kept, it fails every later backup.
        _store(self.db, TABLES, rows=1)
        with open(self.db + ".backup-snapshot", "wb") as f:
            f.write(b"junk")
        done = _run(self.db, "snapshot")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith(b"SQLite format 3\0"))
        self.assertFalse(os.path.exists(self.db + ".backup-snapshot"))

    def test_no_store_is_a_failure_that_says_so(self):
        done = _run(self.db, "snapshot")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, b"")
        self.assertIn(b"backup: no store at ", done.stderr)


class RestoreProgramTests(unittest.TestCase):

    def setUp(self):
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        self.db = os.path.join(self.dir, "control.db")
        backup = os.path.join(self.dir, "backup.db")
        _store(backup, TABLES)
        self.backup = _read(backup)

    def test_a_backup_replaces_the_store_and_leaves_no_incoming_file(self):
        done = _run(self.db, "restore", stdin=self.backup)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(_read(self.db), self.backup)
        self.assertFalse(os.path.exists(self.db + ".restore-incoming"))
        # The summary is for a human, and stdout carries nothing else on a restore.
        self.assertIn(b"restore: store replaced from a schema-v0 backup (", done.stdout)

    def test_a_failed_replace_is_a_refusal_that_removes_the_incoming_file(self):
        # A directory where the store goes makes the rename fail: a stand-in for any
        # OSError the rename can raise.
        os.mkdir(self.db)
        done = _run(self.db, "restore", stdin=self.backup)
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"{REFUSED}could not replace ".encode(), done.stderr)
        self.assertNotIn(b"Traceback", done.stderr)
        self.assertFalse(os.path.exists(self.db + ".restore-incoming"))
        self.assertTrue(os.path.isdir(self.db))

    def assert_refused(self, incoming: bytes, reason: str) -> None:
        _store(self.db, TABLES, rows=1)
        before = _read(self.db)
        done = _run(self.db, "restore", stdin=incoming)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(f"{REFUSED}{reason}".encode(), done.stderr)
        self.assertNotIn(b"Traceback", done.stderr)
        self.assertEqual(_read(self.db), before)
        self.assertEqual(sorted(os.listdir(self.dir)), ["backup.db", "control.db"])

    def test_bytes_that_are_not_sqlite_are_refused(self):
        self.assert_refused(b"# a markdown file\n" * 100, "not a readable SQLite database")

    def test_a_store_missing_a_table_is_refused_naming_it(self):
        path = os.path.join(self.dir, "partial.db")
        _store(path, ("rules", "approvals", "audit"))
        incoming = _read(path)
        os.unlink(path)
        self.assert_refused(incoming,
                            "not a control-plane store — missing table(s): audit_cursor")

    def test_a_newer_schema_is_refused(self):
        newer = cp.store.SCHEMA_VERSION + 1
        path = os.path.join(self.dir, "newer.db")
        _store(path, TABLES)
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {newer}")
        conn.close()
        incoming = _read(path)
        os.unlink(path)
        self.assert_refused(incoming, f"backup is schema v{newer}, this control plane "
                                      f"understands v{cp.store.SCHEMA_VERSION}")

    def test_this_builds_schema_is_restored(self):
        path = os.path.join(self.dir, "current.db")
        _store(path, TABLES)
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {cp.store.SCHEMA_VERSION}")
        conn.close()
        incoming = _read(path)
        done = _run(self.db, "restore", stdin=incoming)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(_read(self.db), incoming)

    def test_an_integrity_check_that_reports_is_refused(self):
        # An index pointed at a table with more rows than it holds: integrity_check
        # answers with a list of faults rather than raising.
        path = os.path.join(self.dir, "damaged.db")
        _store(path, TABLES, rows=3)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE extra (x)")
        conn.executemany("INSERT INTO extra(x) VALUES (?)", [(i,) for i in range(5)])
        conn.execute("CREATE INDEX ri ON rules(x)")
        conn.commit()
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET tbl_name='extra', "
                     "sql='CREATE INDEX ri ON extra(x)' WHERE name='ri'")
        conn.commit()
        conn.close()
        incoming = _read(path)
        os.unlink(path)
        self.assert_refused(incoming, "integrity_check says 'wrong # of entries in index ri'")

    def test_a_damaged_wal_mode_file_is_refused_leaving_no_sidecar(self):
        # A WAL-mode header passes the recipe's 15-byte check, and a damaged body is
        # refused as malformed: a torn raw `cp` of a live store is one. Opening it
        # makes a -wal and -shm, which an unlink with the file still open leaves.
        path = os.path.join(self.dir, "wal.db")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        _store(path, TABLES, rows=500)
        incoming = bytearray(_read(path))
        self.assertEqual(incoming[18:20], b"\x02\x02")
        os.unlink(path)
        page = int.from_bytes(incoming[16:18], "big")
        incoming[page:] = b"\xa5" * (len(incoming) - page)
        self.assert_refused(bytes(incoming), "not a readable SQLite database")

    def test_the_old_stores_sidecars_are_removed(self):
        _store(self.db, TABLES)
        for side in ("-wal", "-shm"):
            with open(self.db + side, "wb") as f:
                f.write(b"stale")
        done = _run(self.db, "restore", stdin=self.backup)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(os.listdir(self.dir)), ["backup.db", "control.db"])

    def test_a_sidecar_it_cannot_remove_is_its_own_status(self):
        _store(self.db, TABLES)
        os.makedirs(os.path.join(self.db + "-wal", "inside"))
        done = _run(self.db, "restore", stdin=self.backup)
        self.assertEqual(done.returncode, cp.store_backup.SIDECAR_LEFT, done.stderr)
        self.assertIn(f"restore: RESTORED but could not remove {self.db}-wal".encode(),
                      done.stderr)
        self.assertEqual(done.stdout, b"")
        self.assertEqual(_read(self.db), self.backup)

    def test_any_failed_replace_is_a_refusal(self):
        # In process, so the rename can fail as something other than the directory
        # above: a refusal for every OSError, not one kind.
        self.addCleanup(setattr, cp.store, "DB_PATH", cp.store.DB_PATH)
        cp.store.DB_PATH = self.db
        _store(self.db, TABLES, rows=1)
        before = _read(self.db)
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "denied")), \
                self.assertRaises(SystemExit) as caught:
            cp.store_backup.restore(io.BytesIO(self.backup))
        self.assertTrue(str(caught.exception.code).startswith(
            f"{REFUSED}could not replace {self.db} ("), caught.exception.code)
        self.assertEqual(_read(self.db), before)
        self.assertFalse(os.path.exists(self.db + ".restore-incoming"))


class CommandLineTests(unittest.TestCase):

    def test_no_command_or_an_unknown_one_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as d:
            for args in ((), ("export",), ("snapshot", "restore")):
                with self.subTest(args=args):
                    done = _run(os.path.join(d, "control.db"), *args)
                    self.assertEqual(done.returncode, 2)
                    self.assertIn(b"usage: python3 store_backup.py", done.stderr)
                    self.assertEqual(done.stdout, b"")

    def test_the_makefile_holds_on_the_sidecar_status(self):
        held = re.search(r"^RESTORE_SIDECAR_LEFT\s*:=\s*(\d+)$", MAKEFILE, re.M)
        self.assertIsNotNone(held)
        self.assertEqual(int(held.group(1)), cp.store_backup.SIDECAR_LEFT)

    def test_the_recipes_run_the_module(self):
        def recipe(target: str) -> str:
            m = re.search(rf"^{target}:.*\n((?:\t.*\n|\n)*)", MAKEFILE, re.M)
            self.assertIsNotNone(m, f"no {target} target")
            return m.group(1)
        self.assertIn("$(CONTROL_TOOL) store_backup.py snapshot >", recipe("backup"))
        self.assertIn("$(CONTROL_TOOL) store_backup.py restore <", recipe("restore"))


if __name__ == "__main__":
    unittest.main()
