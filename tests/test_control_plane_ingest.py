# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the audit ingest (``control-plane/ingest.py``: the row mappers
and ``_drain``).

TWO STREAMS share one drain. Most of this file exercises the egress one, because the
rotation/cursor/partial-line machinery is shared and that is where it was written;
``ToolOutcomeRowTests`` covers what is specific to the gateway's outcome stream, which
is entirely in its mapper.

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

from _loader import (  # noqa: E402 (must set env first)
    load_control_plane,
    load_outcomes,
)

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


def _stream(name):
    """One ingest stream by name. The suite points a stream at a temp file, so it has
    to reach the object rather than the module constant it was built from."""
    return next(s for s in cp.ingest.STREAMS if s.name == name)


class IngestTestCase(unittest.TestCase):
    def setUp(self):
        cp.store._init_db()
        with cp.store._connect() as conn:
            conn.execute("DELETE FROM audit")
            conn.execute("DELETE FROM audit_cursor")
            conn.commit()
        self.path = os.path.join(_TMP, f"audit-{self.id()}.jsonl")
        # The STREAM's path, not the module constant: `STREAMS` is built at import and
        # bakes the path into each entry, so rebinding the constant would leave the
        # drain reading the container path and every test silently exercising nothing.
        self.stream = _stream("egress")
        self.addCleanup(setattr, self.stream, "path", self.stream.path)
        self.stream.path = self.path
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
                "SELECT ts, kind, stage, host, port, proto, client, "
                "client_class, method, url, reason FROM audit ORDER BY id").fetchall()

    def cursor(self):
        with cp.store._connect() as conn:
            return conn.execute("SELECT inode, offset FROM audit_cursor "
                                "WHERE path=?", (self.path,)).fetchone()


class RowMappingTests(IngestTestCase):
    """``_egress_row``: what becomes a row, and what is dropped."""

    def test_local_decision_becomes_a_row(self):
        self.assertEqual(
            cp.ingest._egress_row(_line().encode()),
            {"ts": 1000.0, "kind": "deny", "stage": "sni", "host": "evil.com",
             "port": None, "proto": None, "client": "172.30.0.2",
             "client_class": "sandbox", "method": None, "url": None,
             "reason": "possible domain-fronting"})

    def test_the_row_fills_exactly_the_columns_it_is_inserted_under(self):
        """A mapper used to return a positional tuple, and it agreed with its column
        list only by hand-counted index — adding one in the middle moved `reason`
        under the stdout mirror's `r[9]`, which would have printed the agent-supplied
        URL as the decision's reason. Names removed that class of bug; what remains is
        that a name the INSERT does not list is silently dropped by `r.get(c)`, so a
        field added to the mapper alone would vanish without failing anything."""
        row = cp.ingest._egress_row(_line().encode())
        self.assertEqual(set(row), set(cp.ingest._EGRESS_COLUMNS))

    def test_the_class_is_derived_from_the_client_the_proxy_reported(self):
        """The proxy sends no class and should not start: it is deliberately
        client-agnostic about everything except the lifeline. These are its LOCAL
        decisions — the fronting refusal above among them — so deriving the class here
        is what keeps the column populated across the whole audit view, rather than
        empty on exactly the most alarming rows."""
        mcp = cp.ingest._egress_row(_line(client="172.28.0.5").encode())
        self.assertEqual(mcp["client_class"],
                         "mcp")
        # A client in no configured range is recorded as unclassified, not as NULL:
        # the row is still evidence, and "we could not place this caller" is a fact
        # worth keeping.
        odd = cp.ingest._egress_row(_line(client="203.0.113.9").encode())
        self.assertEqual(odd["client_class"],
                         cp.policy.UNCLASSIFIED)

    def test_central_true_is_never_ingested(self):
        """Every governed request writes a proxy line too. Ingesting those would
        duplicate the whole log against the rows /authorize already wrote."""
        self.assertIsNone(cp.ingest._egress_row(_line(central=True).encode()))

    def test_missing_flag_is_not_ingested(self):
        """Absence must read as 'unknown', not as 'local'. A line written by a proxy
        older than this field then under-reports rather than double-counting, which
        is the direction that degrades a view instead of corrupting a record."""
        rec = json.loads(_line())
        del rec["central"]
        self.assertIsNone(cp.ingest._egress_row((json.dumps(rec) + "\n").encode()))

    def test_truthy_non_false_flag_is_not_ingested(self):
        # `is not False`, not `not falsy`: 0 and "" must not read as local either.
        for garbled in (0, "", "false", None, [], "no"):
            with self.subTest(central=garbled):
                self.assertIsNone(cp.ingest._egress_row(_line(central=garbled).encode()))

    def test_unknown_decision_verbs_are_dropped(self):
        """The audit table's vocabulary is allow|deny|hold. The proxy's `startup`
        line lives in the same file and is not a decision."""
        for verb in ("startup", "deny-sni", "", "DENY", "allowish"):
            with self.subTest(kind=verb):
                self.assertIsNone(cp.ingest._egress_row(_line(decision=verb).encode()))
        for verb in ("allow", "deny", "hold"):
            with self.subTest(kind=verb):
                self.assertIsNotNone(cp.ingest._egress_row(_line(decision=verb).encode()))

    def test_unusable_timestamps_are_dropped(self):
        # A row with no usable instant cannot be placed in a time-ordered view.
        # `True` is here because bool is an int subclass, so a naive isinstance
        # check would silently file a decision at 1970-01-01T00:00:01.
        for ts in (None, "1000", float("nan"), float("inf"), True, [1000]):
            with self.subTest(ts=ts):
                self.assertIsNone(cp.ingest._egress_row(_line(ts=ts).encode()))

    @staticmethod
    def _fields(line):
        """The mapped row, which is now a dict at the source. It used to be a tuple
        zipped against a column list here, and positional indexing is what broke when
        `client_class` landed in the middle of it — an assertion about `url` silently
        became one about `reason`. A mapper that returns names removes the class."""
        return cp.ingest._egress_row(line)

    def test_non_integer_port_becomes_null(self):
        for port in ("443", None, 4.5, True):
            with self.subTest(port=port):
                self.assertIsNone(self._fields(_line(port=port).encode())["port"])
        self.assertEqual(self._fields(_line(port=443).encode())["port"], 443)

    def test_agent_influenced_fields_are_truncated(self):
        """host/url come from what the sandbox asked for. The proxy records them
        faithfully; this reader is where an unbounded one stops being our problem."""
        f = self._fields(_line(host="h" * 9000, url="u" * 9000).encode())
        self.assertEqual(len(f["host"]), cp.store.DRAIN_MAX_FIELD)
        self.assertEqual(len(f["url"]), cp.store.DRAIN_MAX_FIELD)

    def test_malformed_lines_are_dropped_not_guessed_at(self):
        for raw in (b"{not json", b"[]", b'"a string"', b"null", b"42",
                    b"\xff\xfe\x00binary"):
            with self.subTest(raw=raw):
                self.assertIsNone(cp.ingest._egress_row(raw))


class DrainTests(IngestTestCase):
    """``_drain``: the cursor, and what it guarantees."""

    def test_drains_and_records_a_cursor(self):
        self.write(_line(host="a.example") + _line(host="b.example"))
        consumed = cp.ingest._drain(self.stream)
        self.assertEqual(consumed, os.path.getsize(self.path))
        self.assertEqual([r["host"] for r in self.rows()],
                         ["a.example", "b.example"])
        self.assertEqual(self.cursor()["offset"], os.path.getsize(self.path))

    def test_second_pass_over_unchanged_file_ingests_nothing(self):
        self.write(_line())
        cp.ingest._drain(self.stream)
        self.assertEqual(cp.ingest._drain(self.stream), 0)
        self.assertEqual(len(self.rows()), 1)

    def test_only_appended_bytes_are_ingested(self):
        self.write(_line(host="first.example"))
        cp.ingest._drain(self.stream)
        self.write(_line(host="second.example"))
        cp.ingest._drain(self.stream)
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
                cp.ingest._drain(self.stream)
        finally:
            cp.store._connect = real_connect
        self.assertEqual(self.rows(), [])
        self.assertIsNone(self.cursor())
        # ...and the retry after recovery ingests it exactly once, not twice.
        cp.ingest._drain(self.stream)
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
        cp.ingest._drain(self.stream)
        self.assertEqual([r["host"] for r in self.rows()], ["complete.example"])

        self.write('{"ts": 1001.0, "dec')                   # mid-append
        self.assertEqual(cp.ingest._drain(self.stream), 0)       # consumed nothing
        self.assertEqual(len(self.rows()), 1)

        self.write('ision": "deny", "host": "late.example", "central": false}\n')
        cp.ingest._drain(self.stream)
        self.assertEqual([r["host"] for r in self.rows()],
                         ["complete.example", "late.example"])
        self.assertEqual(self.cursor()["offset"], os.path.getsize(self.path))

    def test_empty_file_is_not_an_error(self):
        self.write("", mode="w")
        self.assertEqual(cp.ingest._drain(self.stream), 0)

    def test_missing_file_raises_so_the_loop_can_report_it(self):
        """Swallowed here it would be a permanently silent ingest — the exact
        failure shape this change exists to remove. _audit_drain_loop owns the
        once-per-transition reporting."""
        with self.assertRaises(OSError):
            cp.ingest._drain(self.stream)

    def test_truncation_in_place_restarts_from_zero(self):
        self.write(_line(host="old.example") * 5)
        cp.ingest._drain(self.stream)
        self.write(_line(host="new.example"), mode="w")     # same inode, smaller
        cp.ingest._drain(self.stream)
        self.assertEqual([r["host"] for r in self.rows()][-1], "new.example")
        self.assertEqual(self.cursor()["offset"], os.path.getsize(self.path))

    def test_replaced_file_restarts_from_zero(self):
        """A file swapped out for a genuinely different inode with the old one gone
        (no sibling to follow) is the aged-out case: resume on the new file from zero.
        Without the inode check a fresh file would inherit the old offset and its first
        N bytes — the oldest decisions on it — would never be read."""
        self.write(_line(host="old.example") * 5)
        cp.ingest._drain(self.stream)
        old_inode = self.cursor()["inode"]
        self.replace_file(_line(host="rotated.example") * 5)  # new inode, same size
        cp.ingest._drain(self.stream)
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
            self.assertGreater(cp.ingest._drain(self.stream), 0)
            self.assertLess(len(self.rows()), 40)          # one pass: a real tail left
            self.rotate()                                  # roll the partly-read file
            self.write(_line(host="fresh.example") * 3)    # fresh active, new inode
            for _ in range(60):
                if cp.ingest._drain(self.stream) == 0:
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
            if cp.ingest._drain(self.stream) == 0:
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
        cp.ingest._drain(self.stream)
        self.assertEqual([r["host"] for r in self.rows()], ["ingested.example"])
        with cp.store._connect() as conn:
            conn.execute("UPDATE audit_cursor SET inode = inode + ? WHERE path = ?",
                         (10 ** 9, self.path))             # an inode nothing owns
            conn.commit()
        self.replace_file(_line(host="afterward.example"))  # the file that took over
        cp.ingest._drain(self.stream)
        self.assertIn("cursor lost its file", self.out.getvalue())
        self.assertIn("afterward.example", [r["host"] for r in self.rows()])

    def test_malformed_line_does_not_wedge_the_cursor(self):
        self.write("{not json\n" + _line(host="after.example"))
        cp.ingest._drain(self.stream)
        self.assertEqual([r["host"] for r in self.rows()], ["after.example"])
        self.assertEqual(self.cursor()["offset"], os.path.getsize(self.path))

    def test_oversized_line_is_skipped_rather_than_stalling_the_tail(self):
        """One unbounded record must not stop every later decision from arriving."""
        cp.ingest.DRAIN_BLOCK = 512
        try:
            self.write(json.dumps({"ts": 1.0, "kind": "deny",
                                   "host": "x" * 4000, "central": False}) + "\n")
            self.write(_line(host="after.example"))
            for _ in range(20):
                if cp.ingest._drain(self.stream) == 0:
                    break
            self.assertEqual([r["host"] for r in self.rows()], ["after.example"])
        finally:
            cp.ingest.DRAIN_BLOCK = 1 << 20

    def test_ingested_rows_are_mirrored_to_the_live_log(self):
        """`make logs-cp` is described as a live feed of decisions. An ingested row
        IS a decision, so it belongs there — marked, because it arrives late and out
        of order relative to the lines around it."""
        self.write(_line(host="fronted.example"))
        cp.ingest._drain(self.stream)
        mirrored = self.out.getvalue()
        self.assertIn("fronted.example", mirrored)
        self.assertIn("(ingested)", mirrored)

    def test_backlog_larger_than_one_block_drains_over_passes(self):
        cp.ingest.DRAIN_BLOCK = 512
        try:
            self.write(_line(host="a.example") * 40)
            passes = 0
            while cp.ingest._drain(self.stream) > 0:
                passes += 1
            self.assertGreater(passes, 1)          # genuinely multi-block
            self.assertEqual(len(self.rows()), 40)
            self.assertEqual(self.cursor()["offset"], os.path.getsize(self.path))
        finally:
            cp.ingest.DRAIN_BLOCK = 1 << 20


class ToolOutcomeRowTests(unittest.TestCase):
    """``_tool_row``: what the gateway's stream may put in the crown-jewel store.

    A trust boundary in the sharpest sense in this repo. This file is written by the
    component that faces the agent, carries a THIRD PARTY's error text, and is the only
    path by which "how a tool call ended" reaches the audit trail. So the mapper is a
    whitelist: recognised stage, recognised status, usable timestamp, and every other
    line dropped rather than interpreted.

    No fixture beyond a line builder — the mapper is pure, and the drain around it is
    the same one the egress tests already exercise."""

    @staticmethod
    def line(**over):
        rec = {"ts": 1000.0, "stage": "tool-result", "status": "ok",
               "server": "mcp-github", "tool": "get_me", "approval_id": None,
               "client": "172.30.0.2", "reason": None}
        rec.update(over)
        return (json.dumps(rec) + "\n").encode()

    def test_an_outcome_becomes_a_row(self):
        self.assertEqual(
            cp.ingest._tool_row(self.line()),
            {"ts": 1000.0, "kind": "outcome", "stage": "tool-result",
             "client": "172.30.0.2", "client_class": "sandbox", "reason": None,
             "server": "mcp-github", "tool": "get_me", "approval_id": None,
             "status": "ok"})

    def test_the_row_fills_exactly_the_columns_it_is_inserted_under(self):
        # A name the INSERT does not list is silently dropped by `r.get(c)`, so a field
        # added to the mapper alone would vanish without failing anything.
        self.assertEqual(set(cp.ingest._tool_row(self.line())),
                         set(cp.ingest._TOOL_COLUMNS))

    def test_the_decision_word_is_a_constant_the_stream_cannot_choose(self):
        # THE ONE THAT MATTERS. A stream that could name its own decision word could
        # write `allow` into the audit trail — letting a compromised gateway forge
        # governance history rather than merely report a result. The word is ours; the
        # stream's own vocabulary lives in `status`.
        for forged in ("allow", "deny", "hold", "revoke", None, 12):
            with self.subTest(kind=forged):
                row = cp.ingest._tool_row(self.line(kind=forged))
                self.assertEqual(row["kind"], "outcome")

    def test_a_line_from_another_stage_is_dropped(self):
        # `stage` is the discriminator, written on every record by the gateway so this
        # can be a whitelist rather than a guess. Anything else is not ours to read.
        for stage in ("tool-call", "sni", "", None, "TOOL-RESULT"):
            with self.subTest(stage=stage):
                self.assertIsNone(cp.ingest._tool_row(self.line(stage=stage)))

    def test_an_unknown_status_is_dropped_rather_than_stored(self):
        # The vocabulary is agreed across a file and two images with no compiler. An
        # unrecognised word must not reach a column an operator filters on — it would
        # be a value no facet can select and no reader can interpret.
        for status in ("succeeded", "OK", "", None, True, "ok "):
            with self.subTest(status=status):
                self.assertIsNone(cp.ingest._tool_row(self.line(status=status)))

    def test_every_status_the_gateway_can_write_is_accepted(self):
        # The other direction, and the failure it prevents is silent: a status the
        # gateway emits and this drops is an outcome that never appears, on exactly the
        # calls that went wrong.
        for status in cp.ingest.TOOL_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(cp.ingest._tool_row(self.line(status=status))["status"],
                                 status)

    def test_the_vocabulary_matches_the_gateways(self):
        # Two modules in two images on two networks; this process cannot import that
        # one. A word added there and not here is dropped at the door — fail-safe, but
        # invisible, so it is held by a test rather than by a habit.
        outcomes = load_outcomes()
        self.assertEqual(set(cp.ingest.TOOL_STATUSES), set(outcomes.STATUSES))

    def test_a_malformed_line_is_dropped_not_guessed_at(self):
        for raw in (b"{not json", b"", b"null", b"[]", b'"tool-result"', b"3"):
            with self.subTest(raw=raw):
                self.assertIsNone(cp.ingest._tool_row(raw))

    def test_an_unusable_timestamp_is_dropped(self):
        # The gateway's own clock, not our receipt time — so a backlog sorts where it
        # happened. NaN and infinity parse as floats and would sort absurdly.
        for ts in ("1000", None, float("nan"), float("inf"), True, [1]):
            with self.subTest(ts=ts):
                self.assertIsNone(cp.ingest._tool_row(self.line(ts=ts)))

    def test_third_party_text_is_truncated_on_the_way_in(self):
        # `reason` is an error message a SERVER wrote. The gateway caps it too, and
        # this cap does not depend on that: the store is what must not be bloated by
        # one call, and this module cannot see the check that happens upstream.
        row = cp.ingest._tool_row(self.line(reason="x" * 9000, tool="t" * 9000))
        self.assertEqual(len(row["reason"]), cp.store.DRAIN_MAX_FIELD)
        self.assertEqual(len(row["tool"]), cp.store.DRAIN_MAX_FIELD)

    def test_the_class_is_derived_from_the_client_the_gateway_reported(self):
        # Same arrangement as the egress rows: the writer sends no class and should not
        # start. Deriving here keeps the column populated across the whole view, so an
        # operator filtering by class does not lose the outcome rows.
        self.assertEqual(cp.ingest._tool_row(self.line())["client_class"], "sandbox")
        # A client the map does not place is `unclassified`, not NULL — the same word
        # the egress side records. NULL means "written before classes existed"; this
        # means "observed, and outside every known range", and an operator filtering
        # the audit view has to be able to tell those apart.
        self.assertEqual(
            cp.ingest._tool_row(self.line(client=None))["client_class"],
            cp.policy._client_class(None))


class ToolOutcomeDrainTests(IngestTestCase):
    """The outcome stream through the REAL drain and into the store.

    The mapper tests above are pure; this is the one that proves the generalisation
    actually wired a second stream up — that it has its own cursor row, its own
    columns, and does not collide with the egress one."""

    def setUp(self):
        super().setUp()
        self.tool_path = os.path.join(_TMP, f"tool-{self.id()}.jsonl")
        self.tool = _stream("tool outcomes")
        self.addCleanup(setattr, self.tool, "path", self.tool.path)
        self.tool.path = self.tool_path

    def write(self, *records):
        with open(self.tool_path, "a") as f:
            for rec in records:
                f.write(json.dumps({"ts": 1000.0, "stage": "tool-result",
                                    "status": "ok", "server": "mcp-github",
                                    "tool": "get_me", "client": "172.30.0.2",
                                    **rec}) + "\n")

    def rows(self):
        with cp.store._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT kind, stage, server, tool, approval_id, status, reason "
                "FROM audit ORDER BY id")]

    def test_an_outcome_reaches_the_store(self):
        self.write({"status": "tool-error", "approval_id": "a" * 32,
                    "reason": "Resource not accessible by personal access token"})
        cp.ingest._drain(self.tool)
        self.assertEqual(self.rows(), [
            {"kind": "outcome", "stage": "tool-result", "server": "mcp-github",
             "tool": "get_me", "approval_id": "a" * 32, "status": "tool-error",
             "reason": "Resource not accessible by personal access token"}])

    def test_the_two_streams_keep_separate_cursors(self):
        # `audit_cursor` is keyed by path, which is what lets a second stream be a
        # second row rather than a second table. Sharing one would make each drain
        # rewind or skip the other's file.
        self.write({})
        with open(self.path, "a") as f:
            f.write(_line() + "\n")
        cp.ingest._drain(self.tool)
        cp.ingest._drain(self.stream)
        with cp.store._connect() as conn:
            paths = {r["path"] for r in conn.execute("SELECT path FROM audit_cursor")}
        self.assertEqual(paths, {self.tool_path, self.path})
        self.assertEqual(len(self.rows()), 2)

    def test_a_second_pass_ingests_nothing(self):
        # Exactly-once, for the new stream specifically: the shared drain advances its
        # cursor in the same transaction as the rows, and nothing here has an
        # idempotency key to fall back on.
        self.write({}, {})
        cp.ingest._drain(self.tool)
        self.assertEqual(cp.ingest._drain(self.tool), 0)
        self.assertEqual(len(self.rows()), 2)

    def test_unrecognised_lines_advance_the_cursor_without_storing(self):
        # A dropped line must not wedge the tail behind it. The gateway writes only
        # outcome records today, but the file is on a shared volume and the drain must
        # be incurious rather than fragile.
        self.write({})
        with open(self.tool_path, "a") as f:
            f.write('{"stage":"something-else","ts":1.0}\n')
            f.write("not json at all\n")
        self.write({"status": "timeout"})
        cp.ingest._drain(self.tool)
        self.assertEqual([r["status"] for r in self.rows()], ["ok", "timeout"])
        self.assertEqual(cp.ingest._drain(self.tool), 0)

    def test_the_mirror_reads_like_the_gateways_own_line(self):
        # `make logs-cp` and `make logs-tg` should show the same call in the same
        # shape, because that is how a broken ingest is spotted: present in one, absent
        # in the other.
        self.write({"status": "tool-error", "approval_id": "b" * 32, "reason": "403"})
        cp.ingest._drain(self.tool)
        printed = self.out.getvalue()
        self.assertIn("outcome (ingested) tool-error mcp-github__get_me", printed)
        self.assertIn("approval_id=" + "b" * 32, printed)
        self.assertIn(":: 403", printed)


if __name__ == "__main__":
    unittest.main()
