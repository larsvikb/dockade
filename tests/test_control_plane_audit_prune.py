# SPDX-License-Identifier: Apache-2.0
"""``control-plane/audit_prune.py``, ``make audit-prune``'s program, run as the recipe
runs it on scratch stores built by the app's own ``_init_db``: it drops audit rows and
unspent call tokens past the window, keeps the rest and everything that is policy, and
returns the freed pages. Each run is from ``control-plane/``, the image's WORKDIR, so
``import store`` resolves as it does there; not under ``-I``, which would drop that
directory from the import path."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest

from _loader import ROOT, load_control_plane
from test_sqlite_transaction_control import under_the_announced_default

cp = load_control_plane()

CONTROL_PLANE = ROOT / "control-plane"
DAY = 86400
#: Every table a prune must leave as it found it.
POLICY = ("rules", "approvals", "audit_cursor")


def _prune(db: str, days: str | None = "30") -> str:
    env = {"CONTROL_DB": db, "PATH": os.environ.get("PATH", "")}
    if days is not None:
        env["AUDIT_RETENTION_DAYS"] = days
    return subprocess.run(
        [sys.executable, "-B", "audit_prune.py"], capture_output=True, text=True,
        check=True, cwd=CONTROL_PLANE, env=env).stdout


class _ScratchStore(unittest.TestCase):
    """A fresh store per test, made by the app's ``_init_db`` with no seed."""

    def setUp(self):
        self.db = os.path.join(self.enterContext(tempfile.TemporaryDirectory()),
                               "control.db")
        self.addCleanup(setattr, cp.store, "DB_PATH", cp.store.DB_PATH)
        self.addCleanup(setattr, cp.store, "SEED_PATH", cp.store.SEED_PATH)
        cp.store.DB_PATH = self.db
        cp.store.SEED_PATH = self.db + ".no-seed"
        cp.store._init_db()


class AuditPruneTests(_ScratchStore):
    def _audit(self, *ages_in_days: float) -> None:
        now = time.time()
        with cp.store._connect() as conn:
            conn.executemany("INSERT INTO audit(ts, kind) VALUES (?, 'allow')",
                             [(now - age * DAY,) for age in ages_in_days])
            conn.commit()

    def _audit_ages(self) -> list[int]:
        now = time.time()
        with cp.store._connect() as conn:
            return sorted(round((now - r[0]) / DAY)
                          for r in conn.execute("SELECT ts FROM audit"))

    def test_it_drops_old_call_tokens_and_keeps_recent_ones(self):
        old, now = time.time() - 31 * DAY, time.time()
        with cp.store._connect() as conn:
            for token, at in (("a" * 32, old), ("b" * 32, now)):
                conn.execute("INSERT INTO tool_call_tokens(token, approval_id, server, "
                             "tool, client, created_at) VALUES (?, 'card', 'mcp-github', "
                             "'get_me', NULL, ?)", (token, at))
                conn.execute("INSERT INTO audit(ts, kind) VALUES (?, 'allow')", (at,))
            conn.commit()
        printed = _prune(self.db)
        with cp.store._connect() as conn:
            tokens = [r[0] for r in conn.execute("SELECT token FROM tool_call_tokens")]
            audit = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        self.assertEqual((tokens, audit), (["b" * 32], 1))
        self.assertEqual(printed, "audit-prune: deleted 1 audit row(s) and 1 unspent "
                                  "call token(s) older than 30d, then VACUUM\n")

    def test_a_store_without_the_table_is_still_pruned(self):
        # A control plane older than the table, met by a newer image's program.
        with cp.store._connect() as conn:
            conn.execute("DROP TABLE tool_call_tokens")
            conn.execute("INSERT INTO audit(ts, kind) VALUES (1.0, 'allow')")
            conn.commit()
        self.assertIn("1 audit row(s) and 0 unspent", _prune(self.db))
        self.assertEqual(self._audit_ages(), [])

    def test_the_window_is_the_variable(self):
        self._audit(10)
        self.assertIn("deleted 0 audit row(s)", _prune(self.db, "30"))
        self.assertEqual(self._audit_ages(), [10])
        self.assertIn("deleted 1 audit row(s)", _prune(self.db, "7"))
        self.assertEqual(self._audit_ages(), [])

    def test_unset_the_window_is_thirty_days(self):
        self._audit(29, 31)
        self.assertIn("deleted 1 audit row(s) and 0 unspent call token(s) older than "
                      "30d", _prune(self.db, None))
        self.assertEqual(self._audit_ages(), [29])

    def test_policy_is_left_as_it_was(self):
        old = time.time() - 365 * DAY
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO rules(pattern, action, source, created_at) "
                         "VALUES ('example.com', 'allow', 'operator', ?)", (old,))
            conn.execute("INSERT INTO approvals(id, ts, host, status) VALUES ('h', ?, "
                         "'widgets.example', 'denied')", (old,))
            conn.execute("INSERT INTO audit_cursor(path, inode, offset) "
                         "VALUES ('/audit/x.jsonl', 7, 42)")
            conn.execute("INSERT INTO audit(ts, kind) VALUES (?, 'allow')", (old,))
            conn.commit()

        def tables():
            with cp.store._connect() as conn:
                return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")]  # noqa: S608 (fixed names)
                        for t in POLICY}

        before = tables()
        self.assertTrue(all(before.values()), before)
        self.assertIn("deleted 1 audit row(s)", _prune(self.db))
        self.assertEqual(tables(), before)

    def test_the_freed_pages_go_back_to_the_os(self):
        # A bare DELETE leaves them on the freelist, inside the file.
        with cp.store._connect() as conn:
            conn.executemany("INSERT INTO audit(ts, kind, host) VALUES (1.0, 'allow', ?)",
                             [("x" * 200,)] * 2000)
            conn.commit()
        self.assertIn("deleted 2000 audit row(s)", _prune(self.db))
        with cp.store._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA freelist_count").fetchone()[0], 0)


class AnnouncedAutocommitDefaultTests(_ScratchStore):
    """The prune under the ``autocommit`` default Python's docs announce, ``False``,
    under which ``VACUUM`` refuses to run. In process, as ``_prune``'s subprocess would
    escape the patch; the store is built before it, as today."""

    def test_old_rows_are_pruned_and_vacuumed(self):
        with cp.store._connect() as conn:
            conn.execute("INSERT INTO audit(ts, kind) VALUES (1.0, 'allow')")
            conn.commit()
        with under_the_announced_default():
            self.assertEqual(cp.audit_prune.prune(30), (1, 0))


if __name__ == "__main__":
    unittest.main()
