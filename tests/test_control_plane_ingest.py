# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the egress-audit ingest (``control-plane/app.py``:
``_ingest_row`` / ``_drain_egress_audit``).

Not every egress decision is made by ``/authorize`` — the relay guard, the port
gate, the SNI anti-fronting check and the permanent-lifeline allow are decided
locally in the proxy — so the control plane tails the proxy's audit file and
ingests the lines marked ``central: false``. Two properties carry the design and
are what most of this file exercises:

  1. **Exactly-once.** Rows and the cursor advance in ONE transaction, which is
     the entire reason for pulling rather than being pushed to. Nothing here has
     an idempotency key, so if that transaction can ever half-commit the audit
     table silently grows duplicates.
  2. **No double-counting.** The ``central`` flag is the only thing separating a
     decision this store already recorded from one it has never seen. Every
     governed request produces a proxy line too; ingesting those would duplicate
     the entire log.

The reader is also a trust boundary — it parses a file written by the component
that faces the sandbox, carrying agent-influenced hostnames and URLs — so the
malformed/oversized/truncated cases are correctness tests, not politeness.

Dependency-free: ``fastapi``/``pydantic`` are stubbed (see ``tests/_loader.py``),
and the store is a throwaway SQLite file in a temp dir set before import."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="dockade-cp-ingest-test-")
os.environ["CONTROL_DB"] = os.path.join(_TMP, "control.db")
os.environ["CONTROL_SEED"] = os.path.join(_TMP, "nonexistent-seed.txt")

from _loader import load_control_plane  # noqa: E402 (must set env first)

cp = load_control_plane()


def _line(**over) -> str:
    """A well-formed locally-decided audit line, as the proxy's _audit writes it."""
    rec = {"ts": 1000.0, "decision": "deny", "stage": "sni", "host": "evil.com",
           "client": "172.30.0.2", "reason": "possible domain-fronting",
           "central": False}
    rec.update(over)
    return json.dumps(rec) + "\n"


class _FailsOn:
    """A real connection that raises on the first statement containing `marker`.

    The crash point has to be BETWEEN the row insert and the cursor advance, which
    is the only window where a split transaction differs from a joined one. Failing
    at commit() instead looks identical either way — the first commit raises and
    nothing lands — which is how an earlier version of this test passed while a
    mutation that committed the rows separately survived it."""

    def __init__(self, conn, marker):
        self._conn, self._marker = conn, marker

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def execute(self, sql, *a, **kw):
        if self._marker in sql:
            raise OSError("disk I/O error")
        return self._conn.execute(sql, *a, **kw)


class IngestTestCase(unittest.TestCase):
    def setUp(self):
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM audit")
            conn.execute("DELETE FROM audit_cursor")
            conn.commit()
        self.path = os.path.join(_TMP, f"audit-{self.id()}.jsonl")
        cp.ingest.EGRESS_AUDIT_LOG = self.path
        # The drain mirrors ingested rows to stdout (make logs-cp). Capture it so a
        # test run stays readable, and so the mirror itself can be asserted on.
        self.out = io.StringIO()
        ctx = contextlib.redirect_stdout(self.out)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def write(self, text, mode="a"):
        with open(self.path, mode) as f:
            f.write(text)

    def replace_file(self, text):
        """Swap in a genuinely different file. `os.remove` + recreate is not enough:
        the filesystem happily hands back the just-freed inode, which made the
        rotation test pass or fail on allocator luck."""
        tmp = self.path + ".new"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, self.path)

    def rotate(self):
        """Mimic RotatingFileHandler.doRollover: shift the numbered backups up (oldest
        first), move the active file to `.1`, and leave the active path free — the next
        write recreates it with a NEW inode, exactly as the handler does. Renames (not
        copies) so each file keeps its inode across the shuffle, which is what the drain
        follows it by."""
        n = 1
        while os.path.exists(f"{self.path}.{n}"):
            n += 1
        for i in range(n - 1, 0, -1):
            os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
        os.replace(self.path, f"{self.path}.1")

    def rows(self):
        with cp.store._connect() as conn:
            return conn.execute(
                "SELECT ts, decision, stage, host, port, proto, client, method, "
                "url, reason FROM audit ORDER BY id").fetchall()

    def cursor(self):
        with cp.store._connect() as conn:
            return conn.execute("SELECT inode, offset FROM audit_cursor "
                                "WHERE path=?", (self.path,)).fetchone()


class RowMappingTests(IngestTestCase):
    """``_ingest_row``: what becomes a row, and what is dropped."""

    def test_local_decision_becomes_a_row(self):
        row = cp.ingest._ingest_row(_line().encode())
        self.assertEqual(row, (1000.0, "deny", "sni", "evil.com", None, None,
                               "172.30.0.2", None, None,
                               "possible domain-fronting"))

    def test_central_true_is_never_ingested(self):
        """Every governed request writes a proxy line too. Ingesting those would
        duplicate the whole log against the rows /authorize already wrote."""
        self.assertIsNone(cp.ingest._ingest_row(_line(central=True).encode()))

    def test_missing_flag_is_not_ingested(self):
        """Absence must read as 'unknown', not as 'local'. A line written by a proxy
        older than this field then under-reports rather than double-counting, which
        is the direction that degrades a view instead of corrupting a record."""
        rec = json.loads(_line())
        del rec["central"]
        self.assertIsNone(cp.ingest._ingest_row((json.dumps(rec) + "\n").encode()))

    def test_truthy_non_false_flag_is_not_ingested(self):
        # `is not False`, not `not falsy`: 0 and "" must not read as local either.
        for garbled in (0, "", "false", None, [], "no"):
            with self.subTest(central=garbled):
                self.assertIsNone(cp.ingest._ingest_row(_line(central=garbled).encode()))

    def test_unknown_decision_verbs_are_dropped(self):
        """The audit table's vocabulary is allow|deny|hold. The proxy's `startup`
        line lives in the same file and is not a decision."""
        for verb in ("startup", "deny-sni", "", "DENY", "allowish"):
            with self.subTest(decision=verb):
                self.assertIsNone(cp.ingest._ingest_row(_line(decision=verb).encode()))
        for verb in ("allow", "deny", "hold"):
            with self.subTest(decision=verb):
                self.assertIsNotNone(cp.ingest._ingest_row(_line(decision=verb).encode()))

    def test_unusable_timestamps_are_dropped(self):
        # A row with no usable instant cannot be placed in a time-ordered view.
        # `True` is here because bool is an int subclass, so a naive isinstance
        # check would silently file a decision at 1970-01-01T00:00:01.
        for ts in (None, "1000", float("nan"), float("inf"), True, [1000]):
            with self.subTest(ts=ts):
                self.assertIsNone(cp.ingest._ingest_row(_line(ts=ts).encode()))

    def test_non_integer_port_becomes_null(self):
        for port in ("443", None, 4.5, True):
            with self.subTest(port=port):
                self.assertIsNone(cp.ingest._ingest_row(_line(port=port).encode())[4])
        self.assertEqual(cp.ingest._ingest_row(_line(port=443).encode())[4], 443)

    def test_agent_influenced_fields_are_truncated(self):
        """host/url come from what the sandbox asked for. The proxy records them
        faithfully; this reader is where an unbounded one stops being our problem."""
        row = cp.ingest._ingest_row(_line(host="h" * 9000, url="u" * 9000).encode())
        self.assertEqual(len(row[3]), cp.store.DRAIN_MAX_FIELD)
        self.assertEqual(len(row[8]), cp.store.DRAIN_MAX_FIELD)

    def test_malformed_lines_are_dropped_not_guessed_at(self):
        for raw in (b"{not json", b"[]", b'"a string"', b"null", b"42",
                    b"\xff\xfe\x00binary"):
            with self.subTest(raw=raw):
                self.assertIsNone(cp.ingest._ingest_row(raw))


class DrainTests(IngestTestCase):
    """``_drain_egress_audit``: the cursor, and what it guarantees."""

    def test_drains_and_records_a_cursor(self):
        self.write(_line(host="a.example") + _line(host="b.example"))
        consumed = cp.ingest._drain_egress_audit()
        self.assertEqual(consumed, os.path.getsize(self.path))
        self.assertEqual([r["host"] for r in self.rows()],
                         ["a.example", "b.example"])
        self.assertEqual(self.cursor()["offset"], os.path.getsize(self.path))

    def test_second_pass_over_unchanged_file_ingests_nothing(self):
        self.write(_line())
        cp.ingest._drain_egress_audit()
        self.assertEqual(cp.ingest._drain_egress_audit(), 0)
        self.assertEqual(len(self.rows()), 1)

    def test_only_appended_bytes_are_ingested(self):
        self.write(_line(host="first.example"))
        cp.ingest._drain_egress_audit()
        self.write(_line(host="second.example"))
        cp.ingest._drain_egress_audit()
        self.assertEqual([r["host"] for r in self.rows()],
                         ["first.example", "second.example"])

    def test_crash_between_rows_and_cursor_rolls_both_back(self):
        """The exactly-once property, which is the whole reason for pulling with a
        cursor in this same store. Rows are inserted, then the cursor advance dies.
        If those are separate transactions the rows survive with no cursor to match,
        and the next pass ingests them AGAIN — silently, since nothing here carries
        an idempotency key."""
        self.write(_line())
        real_connect = cp.store._connect
        cp.store._connect = lambda: _FailsOn(real_connect(), "INSERT INTO audit_cursor")
        try:
            with self.assertRaises(OSError):
                cp.ingest._drain_egress_audit()
        finally:
            cp.store._connect = real_connect
        self.assertEqual(self.rows(), [])
        self.assertIsNone(self.cursor())
        # ...and the retry after recovery ingests it exactly once, not twice.
        cp.ingest._drain_egress_audit()
        self.assertEqual(len(self.rows()), 1)

    def test_partial_trailing_line_waits_for_its_newline(self):
        """The proxy appends while we read. A record without its newline is being
        written right now — parsing it would file half a decision, and skipping it
        as 'oversized' would lose one.

        The partial line must be the ENTIRE unread region, which is the only case
        that distinguishes waiting from skipping: with any complete line still
        unread ahead of it, the newline-search stops there and the partial tail is
        left alone for free. A first version of this test drained both at once and
        so proved nothing."""
        self.write(_line(host="complete.example"))
        cp.ingest._drain_egress_audit()
        self.assertEqual([r["host"] for r in self.rows()], ["complete.example"])

        self.write('{"ts": 1001.0, "dec')                   # mid-append
        self.assertEqual(cp.ingest._drain_egress_audit(), 0)       # consumed nothing
        self.assertEqual(len(self.rows()), 1)

        self.write('ision": "deny", "host": "late.example", "central": false}\n')
        cp.ingest._drain_egress_audit()
        self.assertEqual([r["host"] for r in self.rows()],
                         ["complete.example", "late.example"])
        self.assertEqual(self.cursor()["offset"], os.path.getsize(self.path))

    def test_empty_file_is_not_an_error(self):
        self.write("", mode="w")
        self.assertEqual(cp.ingest._drain_egress_audit(), 0)

    def test_missing_file_raises_so_the_loop_can_report_it(self):
        """Swallowed here it would be a permanently silent ingest — the exact
        failure shape this change exists to remove. _audit_drain_loop owns the
        once-per-transition reporting."""
        with self.assertRaises(OSError):
            cp.ingest._drain_egress_audit()

    def test_truncation_in_place_restarts_from_zero(self):
        self.write(_line(host="old.example") * 5)
        cp.ingest._drain_egress_audit()
        self.write(_line(host="new.example"), mode="w")     # same inode, smaller
        cp.ingest._drain_egress_audit()
        self.assertEqual([r["host"] for r in self.rows()][-1], "new.example")
        self.assertEqual(self.cursor()["offset"], os.path.getsize(self.path))

    def test_replaced_file_restarts_from_zero(self):
        """A file swapped out for a genuinely different inode with the old one gone
        (no sibling to follow) is the aged-out case: resume on the new file from zero.
        Without the inode check a fresh file would inherit the old offset and its first
        N bytes — the oldest decisions on it — would never be read."""
        self.write(_line(host="old.example") * 5)
        cp.ingest._drain_egress_audit()
        old_inode = self.cursor()["inode"]
        self.replace_file(_line(host="rotated.example") * 5)  # new inode, same size
        cp.ingest._drain_egress_audit()
        self.assertNotEqual(self.cursor()["inode"], old_inode)
        self.assertIn("rotated.example", [r["host"] for r in self.rows()])

    def test_rotation_drains_the_rolled_files_tail_losslessly(self):
        """The property the rotation-aware drain exists for. A file rolled aside while
        it still holds un-ingested lines must have that tail read from the sibling —
        NOT skipped the instant the fresh active file's new inode is noticed. Losing
        those lines would drop decisions from the record silently, which is the one
        thing an audit trail must never do."""
        cp.ingest.DRAIN_BLOCK = 512
        try:
            self.write(_line(host="early.example") * 40)
            self.assertGreater(cp.ingest._drain_egress_audit(), 0)
            self.assertLess(len(self.rows()), 40)          # one pass: a real tail left
            self.rotate()                                  # roll the partly-read file
            self.write(_line(host="fresh.example") * 3)    # fresh active, new inode
            for _ in range(60):
                if cp.ingest._drain_egress_audit() == 0:
                    break
            hosts = [r["host"] for r in self.rows()]
            self.assertEqual(hosts.count("early.example"), 40)   # tail not lost
            self.assertEqual(hosts.count("fresh.example"), 3)
            self.assertEqual(hosts[-3:], ["fresh.example"] * 3)  # oldest-first order
        finally:
            cp.ingest.DRAIN_BLOCK = 1 << 20

    def test_multiple_backlogged_siblings_drain_oldest_first(self):
        """Two rollovers before the reader catches up: both siblings and the active
        file must ingest, in content order, exactly once each."""
        self.write(_line(host="gen1.example"))
        self.rotate()
        self.write(_line(host="gen2.example"))
        self.rotate()
        self.write(_line(host="gen3.example"))
        for _ in range(10):
            if cp.ingest._drain_egress_audit() == 0:
                break
        self.assertEqual([r["host"] for r in self.rows()],
                         ["gen1.example", "gen2.example", "gen3.example"])

    def test_cursor_whose_file_aged_out_warns_and_resumes(self):
        """If a backup is deleted (rotated past the backup count) before the reader
        finishes it, that is a genuine unread gap. It must be reported — not passed
        over in silence — and the reader must resume on the next file, not wedge.

        The aged-out file is simulated by pointing the cursor at an inode no current
        file has. Deleting and recreating the file cannot do this reliably — the
        filesystem may hand the just-freed inode straight back (the allocator luck
        replace_file() exists to sidestep), and then the cursor would 'find' it."""
        self.write(_line(host="ingested.example"))
        cp.ingest._drain_egress_audit()
        self.assertEqual([r["host"] for r in self.rows()], ["ingested.example"])
        with cp.store._connect() as conn:
            conn.execute("UPDATE audit_cursor SET inode = inode + ? WHERE path = ?",
                         (10 ** 9, self.path))             # an inode nothing owns
            conn.commit()
        self.replace_file(_line(host="afterward.example"))  # the file that took over
        cp.ingest._drain_egress_audit()
        self.assertIn("cursor lost its file", self.out.getvalue())
        self.assertIn("afterward.example", [r["host"] for r in self.rows()])

    def test_malformed_line_does_not_wedge_the_cursor(self):
        self.write("{not json\n" + _line(host="after.example"))
        cp.ingest._drain_egress_audit()
        self.assertEqual([r["host"] for r in self.rows()], ["after.example"])
        self.assertEqual(self.cursor()["offset"], os.path.getsize(self.path))

    def test_oversized_line_is_skipped_rather_than_stalling_the_tail(self):
        """One unbounded record must not stop every later decision from arriving."""
        cp.ingest.DRAIN_BLOCK = 512
        try:
            self.write(json.dumps({"ts": 1.0, "decision": "deny",
                                   "host": "x" * 4000, "central": False}) + "\n")
            self.write(_line(host="after.example"))
            for _ in range(20):
                if cp.ingest._drain_egress_audit() == 0:
                    break
            self.assertEqual([r["host"] for r in self.rows()], ["after.example"])
        finally:
            cp.ingest.DRAIN_BLOCK = 1 << 20

    def test_ingested_rows_are_mirrored_to_the_live_log(self):
        """`make logs-cp` is described as a live feed of decisions. An ingested row
        IS a decision, so it belongs there — marked, because it arrives late and out
        of order relative to the lines around it."""
        self.write(_line(host="fronted.example"))
        cp.ingest._drain_egress_audit()
        mirrored = self.out.getvalue()
        self.assertIn("fronted.example", mirrored)
        self.assertIn("(ingested)", mirrored)

    def test_backlog_larger_than_one_block_drains_over_passes(self):
        cp.ingest.DRAIN_BLOCK = 512
        try:
            self.write(_line(host="a.example") * 40)
            passes = 0
            while cp.ingest._drain_egress_audit() > 0:
                passes += 1
            self.assertGreater(passes, 1)          # genuinely multi-block
            self.assertEqual(len(self.rows()), 40)
            self.assertEqual(self.cursor()["offset"], os.path.getsize(self.path))
        finally:
            cp.ingest.DRAIN_BLOCK = 1 << 20


if __name__ == "__main__":
    unittest.main()
