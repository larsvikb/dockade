# SPDX-License-Identifier: Apache-2.0
"""Guards over the record only the gateway can write.

The control plane audits the DECISION and structurally cannot audit the outcome: its
claim row is written before the call, so by the time one succeeds or fails the
authority has already answered. Everything here is about the half that closes — what
became of a call a human approved.

Two files, split by what they can be wrong about. This one is the SINK: the line's
shape, the caps, and the promise that recording never breaks a call. Whether the right
rows get written at all is in tests/test_tool_execution.py, beside the function that
performs the side effect they describe.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest

from _loader import load_outcomes


class _captured:
    """stdout, as a context manager. `record` and `setup` both print, and what they
    print is asserted here because on the failure path it is the only remaining copy."""

    def __enter__(self):
        import contextlib
        import io
        self._buf = io.StringIO()
        self._ctx = contextlib.redirect_stdout(self._buf)
        self._ctx.__enter__()
        return self._buf

    def __exit__(self, *exc):
        return self._ctx.__exit__(*exc)


class OutcomeTestCase(unittest.TestCase):
    """A module writing to a real file in a temp dir, because the file IS the
    interface — the control plane reads bytes off a volume, not a Python call."""

    env: dict[str, str] = {}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "audit.jsonl")
        self.outcomes = load_outcomes({"GATEWAY_AUDIT_LOG": self.path, **self.env})
        self.outcomes.setup()
        # Handlers hold an open file. Closed explicitly so the temp dir can be removed
        # on Windows-like semantics and so a leaked handler cannot write into the next
        # test's directory.
        self.addCleanup(self._close)

    def _close(self):
        for handler in list(self.outcomes.logger.handlers):
            self.outcomes.logger.removeHandler(handler)
            handler.close()

    def lines(self) -> list[dict]:
        with open(self.path) as f:
            return [json.loads(line) for line in f if line.strip()]


class RecordShapeTests(OutcomeTestCase):

    def test_one_call_is_one_json_line(self):
        # JSONL, and the ingest reads up to the last newline: a record spanning two
        # lines would be drained as two, one of them unparseable and dropped.
        self.outcomes.record("ok", "mcp-github", "get_me", client="172.30.0.5")
        with open(self.path) as f:
            self.assertEqual(len(f.read().strip().splitlines()), 1)
        self.assertEqual(len(self.lines()), 1)

    def test_the_line_carries_what_the_ingest_keys_on(self):
        self.outcomes.record("tool-error", "mcp-github", "create_pull_request",
                             reason="403", approval_id="a" * 32, client="172.30.0.5")
        row = self.lines()[0]
        self.assertEqual(row["stage"], "tool-result")
        self.assertEqual(row["status"], "tool-error")
        self.assertEqual(row["server"], "mcp-github")
        self.assertEqual(row["tool"], "create_pull_request")
        self.assertEqual(row["approval_id"], "a" * 32)
        self.assertEqual(row["client"], "172.30.0.5")
        self.assertEqual(row["reason"], "403")
        self.assertIsInstance(row["ts"], float)

    def test_stage_is_present_so_the_ingest_can_be_incurious(self):
        # The drain drops any line it does not fully recognise rather than guessing at
        # it — the discipline `_ingest_row` already applies to the proxy's file. That
        # only works if every line this writes says what kind it is.
        self.outcomes.record("ok", "s", "t")
        self.assertEqual({row["stage"] for row in self.lines()}, {"tool-result"})

    def test_an_absent_approval_is_null_rather_than_missing(self):
        # A call policy allowed outright has no approval, and the KEY has to be there
        # anyway: absent, the ingest cannot tell "allowed outright" from a line written
        # by a version that did not carry the field.
        self.outcomes.record("ok", "mcp-github", "get_me")
        row = self.lines()[0]
        self.assertIn("approval_id", row)
        self.assertIsNone(row["approval_id"])

    def test_every_status_the_module_names_survives_a_round_trip(self):
        # The vocabulary is shared with the control plane's ingest across a file and
        # two processes, with no compiler between them.
        for status in self.outcomes.STATUSES:
            with self.subTest(status=status):
                self.outcomes.record(status, "s", "t")
        self.assertEqual([row["status"] for row in self.lines()],
                         list(self.outcomes.STATUSES))


class FieldCapTests(OutcomeTestCase):
    env = {"GATEWAY_AUDIT_FIELD_MAX": "32"}

    def test_a_servers_error_text_cannot_grow_the_file_without_bound(self):
        # THE TRUST BOUNDARY. `reason` is third-party text — an error message a server
        # wrote — so without a cap a server answering with a megabyte per call writes a
        # megabyte per call onto the volume, and pushes it through ingest into the
        # crown-jewel store.
        self.outcomes.record("tool-error", "mcp-github", "get_me", reason="x" * 5000)
        self.assertEqual(len(self.lines()[0]["reason"]), 32)

    def test_the_cap_applies_to_every_string_not_only_the_reason(self):
        # `server` and `tool` are charset-bounded upstream TODAY. The cap does not
        # depend on that staying true, because this module cannot see the check that
        # makes it true.
        self.outcomes.record("ok", "s" * 5000, "t" * 5000)
        row = self.lines()[0]
        self.assertEqual((len(row["server"]), len(row["tool"])), (32, 32))

    def test_the_timestamp_is_not_a_string_and_is_left_alone(self):
        self.outcomes.record("ok", "s", "t")
        self.assertIsInstance(self.lines()[0]["ts"], float)


class NeverBreaksTheCallTests(OutcomeTestCase):

    def test_a_sink_that_raises_does_not_propagate(self):
        # By the time this is called the tool call has ALREADY happened, so an
        # exception here could only turn a completed call into an error result for the
        # agent — losing the reply on top of losing the record. A full disk must stop
        # the trail growing, not stop the agent working.
        class Exploding(logging.Handler):
            def emit(self, record):
                raise OSError("no space left on device")

        self._close()
        self.outcomes.logger.addHandler(Exploding())
        self.outcomes.record("ok", "mcp-github", "get_me")   # must not raise

    def test_a_record_that_cannot_be_written_still_reaches_stdout(self):
        # The remaining copy. `make logs-tg` is where a silently un-ingested stream
        # still leaves a trace, which is the difference between degraded and invisible.
        class Exploding(logging.Handler):
            def emit(self, record):
                raise OSError("no space left on device")

        self._close()
        self.outcomes.logger.addHandler(Exploding())
        with _captured() as out:
            self.outcomes.record("tool-error", "mcp-github", "get_me", reason="403")
        self.assertIn("OUTCOME tool-error mcp-github__get_me", out.getvalue())
        self.assertIn("AUDIT FAILED", out.getvalue())

    def test_the_real_file_sink_announces_a_write_it_swallowed(self):
        # THE ONE A DEFAULT WOULD HAVE MISSED. `FileHandler.emit` catches its own
        # exceptions and routes them to `handleError`, so the `except` in `record`
        # never sees a disk-full — the default prints a bare "--- Logging error ---"
        # traceback with nothing to say an audit record was lost. Asserted against the
        # handler this module actually attaches, not a stand-in.
        handler = self.outcomes.logger.handlers[0]
        self.assertIsInstance(handler, self.outcomes._LoudFileHandler)
        with _captured() as out:
            handler.handleError(logging.LogRecord(
                "x", logging.INFO, __file__, 1, "msg", None, None))
        self.assertIn("OUTCOME AUDIT FAILED", out.getvalue())
        self.assertIn(self.path, out.getvalue())


class SetupTests(unittest.TestCase):
    """The two configured states, and the one that must not be reachable by accident."""

    def test_a_configured_path_that_cannot_be_opened_refuses_to_start(self):
        # Not best-effort, and that is the difference from the egress proxy's audit
        # file — for the proxy, stdout is primary and the control plane holds the
        # central record, so its file is a convenience. Here the file is the ONLY path
        # into the durable record, so a path that was configured and does not work is a
        # misconfiguration to surface at start rather than an empty table to discover
        # weeks later.
        outcomes = load_outcomes({"GATEWAY_AUDIT_LOG": "/proc/1/cannot/exist.jsonl"})
        with self.assertRaises(OSError):
            outcomes.setup()

    def test_the_off_switch_is_explicit_and_empty(self):
        # A hand-run container with no volume. Deliberate, announced on stdout, and
        # spelled as something someone had to write down — never the fallback for a
        # path that simply failed.
        outcomes = load_outcomes({"GATEWAY_AUDIT_LOG": ""})
        with _captured() as out:
            outcomes.setup()
        self.assertEqual(outcomes.logger.handlers, [])
        self.assertIn("STDOUT ONLY", out.getvalue())
        self.assertIn("nothing will reach the control plane", out.getvalue())

    def test_the_banner_says_which_state_the_process_is_in(self):
        # Printed on every boot rather than only at the moment it is configured: "no
        # durable outcomes" is exactly the state nobody notices they are in.
        off = load_outcomes({"GATEWAY_AUDIT_LOG": ""})
        off.setup()
        self.assertIn("stdout only", off.describe())

    def test_records_do_not_propagate_to_the_root_logger(self):
        # uvicorn configures the root logger. Propagating would put a second copy of
        # every record in stdout, where it reads as two calls.
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = load_outcomes(
                {"GATEWAY_AUDIT_LOG": os.path.join(tmp, "a.jsonl")})
            outcomes.setup()
            self.assertFalse(outcomes.logger.propagate)
            for handler in list(outcomes.logger.handlers):
                outcomes.logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
