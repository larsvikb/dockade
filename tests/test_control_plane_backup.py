# SPDX-License-Identifier: Apache-2.0
"""The Makefile's BACKUP_PY and RESTORE_PY, the control plane's twins of
``task-registry/backup.py``, run as the Makefile holds them on scratch stores. Each
runs from ``control-plane/``, the image's WORKDIR, so ``import store`` resolves as it
does there. Not under ``-I``: that drops the cwd from the import path, and a program
whose ``import store`` fails writes nothing for a test to find left behind."""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from _loader import ROOT

CONTROL_PLANE = ROOT / "control-plane"
MAKEFILE = (ROOT / "Makefile").read_text()


def _program(name: str) -> str:
    found = re.search(rf"^define {name}\n(.*?)^endef$", MAKEFILE, re.S | re.M)
    if found is None:
        raise AssertionError(f"no `define {name}` ... `endef` block in the Makefile")
    return found.group(1)


def _run(name: str, db: str, stdin: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 (this interpreter, the repo's own program)
        [sys.executable, "-B", "-c", _program(name)], input=stdin,
        capture_output=True, check=False, cwd=CONTROL_PLANE,
        env={"CONTROL_DB": db, "PATH": os.environ.get("PATH", "")})


def _store(path: str, tables: tuple[str, ...]) -> None:
    conn = sqlite3.connect(path)
    for t in tables:
        conn.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


class BackupProgramTests(unittest.TestCase):

    def setUp(self):
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        self.db = os.path.join(self.dir, "control.db")

    def test_a_store_streams_a_snapshot_and_leaves_none(self):
        _store(self.db, ("rules", "approvals", "audit"))
        done = _run("BACKUP_PY", self.db)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith(b"SQLite format 3\0"))
        self.assertFalse(os.path.exists(self.db + ".backup-snapshot"))
        self.assertTrue(os.path.isfile(self.db))

    def test_a_failure_after_the_snapshot_is_written_removes_it(self):
        # No `audit` table: the COUNT fails on the snapshot VACUUM INTO has written.
        _store(self.db, ("rules", "approvals"))
        done = _run("BACKUP_PY", self.db)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(b"OperationalError: no such table: audit", done.stderr)
        self.assertFalse(os.path.exists(self.db + ".backup-snapshot"))


class RestoreProgramTests(unittest.TestCase):

    def setUp(self):
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        self.db = os.path.join(self.dir, "control.db")
        backup = os.path.join(self.dir, "backup.db")
        _store(backup, ("rules", "approvals", "audit", "audit_cursor"))
        with open(backup, "rb") as f:
            self.backup = f.read()

    def test_a_backup_replaces_the_store_and_leaves_no_incoming_file(self):
        done = _run("RESTORE_PY", self.db, self.backup)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(self.db, "rb") as f:
            self.assertEqual(f.read(), self.backup)
        self.assertFalse(os.path.exists(self.db + ".restore-incoming"))

    def test_a_failed_replace_is_a_refusal_that_removes_the_incoming_file(self):
        # A directory where the store goes makes the rename fail: a stand-in for any
        # OSError the rename can raise.
        os.mkdir(self.db)
        done = _run("RESTORE_PY", self.db, self.backup)
        self.assertEqual(done.returncode, 1)
        self.assertIn("restore: REFUSED, store untouched — could not replace "
                      .encode(), done.stderr)
        self.assertNotIn(b"Traceback", done.stderr)
        self.assertFalse(os.path.exists(self.db + ".restore-incoming"))
        self.assertTrue(os.path.isdir(self.db))


if __name__ == "__main__":
    unittest.main()
