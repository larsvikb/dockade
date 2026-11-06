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

import hashlib
import io
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from typing import ClassVar
from unittest import mock

from _loader import load_outcomes, load_protocol


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


def _detach(outcomes) -> None:
    """Remove and close the file handlers. Removed as well as closed: the logger is
    shared by every module ``load_outcomes`` returns, and a closed FileHandler
    reopens on its next emit."""
    for handler in list(outcomes.logger.handlers):
        outcomes.logger.removeHandler(handler)
        handler.close()


class OutcomeTestCase(unittest.TestCase):
    """A module writing to a real file in a temp dir, because the file IS the
    interface — the control plane reads bytes off a volume, not a Python call."""

    env: ClassVar[dict[str, str]] = {}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "audit.jsonl")
        self.outcomes = load_outcomes({"GATEWAY_AUDIT_LOG": self.path, **self.env})
        with _captured():               # the banner lines, said once per boot
            self.outcomes.setup()
        # Handlers hold an open file. Closed explicitly so the temp dir can be removed
        # on Windows-like semantics and so a leaked handler cannot write into the next
        # test's directory.
        self.addCleanup(self._close)

    def _close(self):
        _detach(self.outcomes)

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
                             reason="403", resume_token="a" * 32, client="172.30.0.5")
        row = self.lines()[0]
        self.assertEqual(row["stage"], "tool-result")
        self.assertEqual(row["status"], "tool-error")
        self.assertEqual(row["server"], "mcp-github")
        self.assertEqual(row["tool"], "create_pull_request")
        self.assertEqual(row["resume_token"], "a" * 32)
        self.assertNotIn("approval_id", row)
        self.assertEqual(row["client"], "172.30.0.5")
        self.assertEqual(row["reason"], "403")
        self.assertIsInstance(row["ts"], float)

    def test_stage_is_present_so_the_ingest_can_be_incurious(self):
        # The drain drops any line it does not fully recognise rather than guessing at
        # it — the discipline `_ingest_row` already applies to the proxy's file. That
        # only works if every line this writes says what kind it is.
        self.outcomes.record("ok", "s", "t")
        self.assertEqual({row["stage"] for row in self.lines()}, {"tool-result"})

    def test_an_absent_token_is_null_rather_than_missing(self):
        # A call policy allowed outright has no approval, and the KEY has to be there
        # anyway: absent, the ingest cannot tell "allowed outright" from a line written
        # by a version that did not carry the field.
        self.outcomes.record("ok", "mcp-github", "get_me")
        row = self.lines()[0]
        self.assertIn("resume_token", row)
        self.assertIsNone(row["resume_token"])

    def test_every_status_the_module_names_survives_a_round_trip(self):
        # The vocabulary is shared with the control plane's ingest across a file and
        # two processes, with no compiler between them.
        for status in self.outcomes.STATUSES:
            with self.subTest(status=status):
                self.outcomes.record(status, "s", "t")
        self.assertEqual([row["status"] for row in self.lines()],
                         list(self.outcomes.STATUSES))


class LiveFeedTests(OutcomeTestCase):
    """The stdout line is what a human reads in `make logs-tg`, and ``reason`` is a
    third party's text. One record has to be one line there."""

    FORGED = "denied\nOUTCOME ok mcp-github__merge_pull_request resume_token=" + "a" * 32

    def test_a_servers_error_text_cannot_add_a_line_to_the_feed(self):
        # Q10. With the newline kept, the forged half reads as its own record.
        with _captured() as out:
            self.outcomes.record("tool-error", "mcp-github", "get_me", reason=self.FORGED)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("denied\\nOUTCOME ok", lines[0])     # shown, not obeyed

    def test_carriage_returns_and_escape_sequences_are_shown_not_obeyed(self):
        # A terminal does not need a newline to be lied to: `\r` rewrites the line
        # in place and ESC opens a sequence that can clear it.
        with _captured() as out:
            self.outcomes.record("tool-error", "mcp-github", "get_me",
                                 reason="x\rOUTCOME ok\x1b[2K\u2028y")
        line = out.getvalue().rstrip("\n")
        for raw in ("\r", "\x1b", "\u2028"):
            self.assertNotIn(raw, line)
        self.assertIn("\\r", line)
        self.assertIn("\\x1b", line)

    def test_the_file_keeps_what_the_server_sent(self):
        # Escaping is for the terminal. The JSONL is parsed, not displayed, and the
        # ingest owes the store the text as it arrived.
        with _captured():
            self.outcomes.record("tool-error", "mcp-github", "get_me", reason=self.FORGED)
        self.assertEqual(self.lines()[0]["reason"], self.FORGED)

    def test_a_lone_surrogate_is_shown_escaped_and_raises_nothing(self):
        # A server can echo one back from an argument the agent sent, and this print
        # is after the call has run. A strict UTF-8 stdout, the stricter harness: the
        # container's python opens stdout with surrogateescape, which writes U+DC80 to
        # U+DCFF as raw bytes and raises on every other surrogate. A high and a low
        # one, so neither half of the range can go unescaped.
        reason = "bad " + chr(0xD800) + " and " + chr(0xDC80)
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
        with mock.patch("sys.stdout", out):
            self.outcomes.record("tool-error", "mcp-github", "get_me", reason=reason)
        out.flush()
        self.assertEqual(raw.getvalue().decode(),
                         "OUTCOME tool-error mcp-github__get_me :: bad "
                         + chr(92) + "ud800 and " + chr(92) + "udc80\n")
        self.assertEqual(self.lines()[0]["reason"], reason)


class FieldCapTests(OutcomeTestCase):
    env: ClassVar[dict[str, str]] = {"GATEWAY_AUDIT_FIELD_MAX": "32"}

    def test_a_servers_error_text_cannot_grow_the_file_without_bound(self):
        # THE TRUST BOUNDARY. `reason` is third-party text — an error message a server
        # wrote — so without a cap a server answering with a megabyte per call writes a
        # megabyte per call onto the volume, and pushes it through ingest into the
        # crown-jewel store.
        self.outcomes.record("tool-error", "mcp-github", "get_me", reason="x" * 5000)
        self.assertEqual(len(self.lines()[0]["reason"]), 32)

    def test_the_live_feed_is_capped_like_the_file(self):
        # The stdout line is the other copy of the same third-party text, and the only
        # one when the file write fails.
        with _captured() as out:
            self.outcomes.record("tool-error", "mcp-github", "get_me", reason="x" * 5000)
        self.assertLess(len(out.getvalue()), 200)

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


class ContentTestCase(OutcomeTestCase):
    """Both files on, in one temp dir, as the gateway runs."""

    def setUp(self):
        self.content_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.content_dir.cleanup)
        self.content_path = os.path.join(self.content_dir.name, "responses.jsonl")
        self.env = {**self.env, "GATEWAY_CONTENT_LOG": self.content_path}
        super().setUp()

    def _close(self):
        super()._close()
        _detach_content(self.outcomes)

    def bodies(self) -> list[dict]:
        with open(self.content_path) as f:
            return [json.loads(line) for line in f if line.strip()]

    @staticmethod
    def wire(result: dict) -> bytes:
        return load_protocol().encode(result)


def _detach_content(outcomes) -> None:
    for handler in list(outcomes.content_logger.handlers):
        outcomes.content_logger.removeHandler(handler)
        handler.close()


RESULT = {"content": [{"type": "text", "text": "the PR body"}], "isError": False}


class ContentRecordTests(ContentTestCase):

    def test_size_and_hash_match_the_body_kept(self):
        self.outcomes.record("ok", "mcp-github", "get_pr", client="172.30.0.5",
                             result=RESULT, duration=0.25)
        [kept] = self.bodies()
        body = kept["body"].encode("utf-8")
        self.assertFalse(kept["truncated"])
        self.assertEqual(kept["bytes"], len(body))
        self.assertEqual(kept["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual((kept["server"], kept["tool"], kept["client"]),
                         ("mcp-github", "get_pr", "172.30.0.5"))

    def test_the_outcome_line_carries_the_records_hash_size_and_time(self):
        # The join: an audit row's hash finds this record, and ts tells apart two
        # records that share one.
        self.outcomes.record("ok", "mcp-github", "get_pr", result=RESULT,
                             duration=0.25)
        [line], [kept] = self.lines(), self.bodies()
        self.assertEqual(line["sha256"], kept["sha256"])
        self.assertEqual(line["bytes"], kept["bytes"])
        self.assertEqual(line["ts"], kept["ts"])
        self.assertEqual(line["duration"], 0.25)

    def test_the_hash_is_of_the_bytes_the_agent_is_sent(self):
        # `protocol.encode`, the serializer the reply goes out in: key order as the
        # server sent it, not sorted, so the hashed text is the reply's own.
        result = {"z": 1, "content": [{"type": "text", "text": "x"}], "a": 2}
        self.outcomes.record("ok", "s", "t", result=result)
        self.assertEqual(self.lines()[0]["sha256"],
                         hashlib.sha256(self.wire(result)).hexdigest())
        self.assertEqual(self.bodies()[0]["body"], self.wire(result).decode())

    def test_non_ascii_is_hashed_as_utf8_and_written_escaped(self):
        result = {"content": [{"type": "text", "text": "r\u00e4ksm\u00f6rg\u00e5s \U0001f600"}]}
        self.outcomes.record("ok", "s", "t", result=result)
        wire = self.wire(result)
        self.assertEqual(self.lines()[0]["bytes"], len(wire))
        self.assertEqual(self.lines()[0]["sha256"], hashlib.sha256(wire).hexdigest())
        with open(self.content_path, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.isascii())
        self.assertEqual(self.bodies()[0]["body"], wire.decode("utf-8"))

    def test_no_result_writes_null_facts_and_no_body(self):
        # The lines written outside `_run` describe no result; the keys are there
        # anyway, so a line always has them.
        self.outcomes.record("transport-error", "(unknown)", "(unknown)")
        line = self.lines()[0]
        self.assertEqual((line["bytes"], line["sha256"], line["duration"]),
                         (None, None, None))
        self.assertEqual(self.bodies(), [])

    def test_a_result_that_cannot_be_sent_has_no_facts_and_raises_nothing(self):
        # NaN and a lone surrogate fail the reply in `app.py` too, so no bytes reached
        # the agent and there is nothing to describe. The outcome is still written.
        for result in ({"x": float("nan")}, {"x": "\ud800"}):
            with self.subTest(result=repr(result)):
                self.outcomes.record("ok", "s", "t", result=result, duration=1.0)
                self.assertIsNone(self.lines()[-1]["sha256"])
                self.assertEqual(self.lines()[-1]["duration"], 1.0)
        self.assertEqual(self.bodies(), [])

    def test_no_body_reaches_stdout(self):
        # `make logs-tg` is a feed a human reads; the body has a file of its own.
        result = {"content": [{"type": "text", "text": "SECRET-ish body text"}]}
        with _captured() as out:
            self.outcomes.record("ok", "s", "t", result=result)
        self.assertNotIn("SECRET-ish", out.getvalue())
        self.assertIn("sha256=" + self.lines()[0]["sha256"], out.getvalue())


class ContentCapTests(ContentTestCase):
    env: ClassVar[dict[str, str]] = {"GATEWAY_CONTENT_RECORD_MAX": "10"}

    def test_an_oversized_body_is_cut_marked_and_keeps_the_whole_size_and_hash(self):
        result = {"content": [{"type": "text", "text": "\u00e5" * 100}]}
        self.outcomes.record("ok", "s", "t", result=result)
        wire = self.wire(result)
        [kept] = self.bodies()
        self.assertTrue(kept["truncated"])
        self.assertEqual(kept["body"], wire.decode("utf-8")[:10])
        self.assertEqual(kept["bytes"], len(wire))
        self.assertEqual(kept["sha256"], hashlib.sha256(wire).hexdigest())

    def test_the_cap_counts_characters_when_they_are_multibyte(self):
        # `{"a":"` and then four-byte characters: the first ten characters are 22
        # bytes, so a cut taken in bytes would keep fewer than the cap.
        result = {"a": "\U0001f600" * 20}
        self.outcomes.record("ok", "s", "t", result=result)
        [kept] = self.bodies()
        self.assertEqual(kept["body"], '{"a":"' + "\U0001f600" * 4)
        self.assertTrue(kept["truncated"])

    def test_a_body_exactly_at_the_cap_is_not_marked(self):
        # On the wire `{"a":"12"}` is 10 characters, the cap, and one more is over it.
        self.outcomes.record("ok", "s", "t", result={"a": "12"})
        self.outcomes.record("ok", "s", "t", result={"a": "123"})
        self.assertEqual([b["truncated"] for b in self.bodies()], [False, True])
        self.assertEqual(self.bodies()[0]["body"], '{"a":"12"}')


class ContentFieldCapTests(ContentTestCase):
    env: ClassVar[dict[str, str]] = {"GATEWAY_AUDIT_FIELD_MAX": "32"}

    def test_the_names_in_a_content_record_are_capped_like_the_outcome_lines(self):
        self.outcomes.record("ok", "s" * 5000, "t" * 5000, client="c" * 5000,
                             result=RESULT)
        kept = self.bodies()[0]
        self.assertEqual((len(kept["server"]), len(kept["tool"]), len(kept["client"])),
                         (32, 32, 32))


class ContentRotationTests(ContentTestCase):
    env: ClassVar[dict[str, str]] = {"GATEWAY_CONTENT_MAX_BYTES": "400",
                                     "GATEWAY_CONTENT_BACKUPS": "2"}

    def test_the_file_rotates_at_its_bound_and_drops_the_oldest(self):
        for n in range(12):
            self.outcomes.record("ok", "s", "t",
                                 result={"n": n, "pad": "x" * 100})
        names = sorted(os.listdir(self.content_dir.name))
        self.assertEqual(names, ["responses.jsonl", "responses.jsonl.1",
                                 "responses.jsonl.2"])
        for name in names:
            self.assertLessEqual(
                os.path.getsize(os.path.join(self.content_dir.name, name)), 400)
        # The outcome file is on its own bound and kept every line.
        self.assertEqual(len(self.lines()), 12)


class ContentSinkFailureTests(ContentTestCase):

    def test_a_failing_content_sink_names_its_path_and_spares_the_outcome(self):
        class Exploding(logging.Handler):
            def emit(self, record):
                raise OSError("no space left on device")

        _detach_content(self.outcomes)
        self.outcomes.content_logger.addHandler(Exploding())
        with _captured() as out:
            self.outcomes.record("ok", "s", "t", result=RESULT)   # must not raise
        self.assertIn("RESPONSE CONTENT FAILED", out.getvalue())
        self.assertIn(self.content_path, out.getvalue())
        self.assertNotIn(self.path, out.getvalue())
        self.assertEqual(len(self.lines()), 1)
        self.assertIsNotNone(self.lines()[0]["sha256"])

    def test_the_real_content_sink_announces_a_write_it_swallowed(self):
        [handler] = self.outcomes.content_logger.handlers
        self.assertIsInstance(handler, self.outcomes._LoudFileHandler)
        with _captured() as out:
            handler.handleError(logging.LogRecord(
                "x", logging.INFO, __file__, 1, "msg", None, None))
        self.assertIn("RESPONSE CONTENT FAILED", out.getvalue())
        self.assertIn(self.content_path, out.getvalue())


class ContentSetupTests(unittest.TestCase):

    def test_a_configured_content_path_that_cannot_be_opened_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = load_outcomes({
                "GATEWAY_AUDIT_LOG": os.path.join(tmp, "audit.jsonl"),
                "GATEWAY_CONTENT_LOG": "/proc/1/cannot/exist.jsonl"})
            self.addCleanup(_detach, outcomes)
            with self.assertRaises(OSError):
                outcomes.setup()

    def test_neither_logger_reaches_the_root_logger(self):
        # Should anything ever give root a handler, a body or a second copy of an
        # outcome could reach stdout.
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = load_outcomes({
                "GATEWAY_AUDIT_LOG": os.path.join(tmp, "audit.jsonl"),
                "GATEWAY_CONTENT_LOG": os.path.join(tmp, "responses.jsonl")})
            outcomes.setup()
            _detach(outcomes)
            _detach_content(outcomes)
        self.assertFalse(outcomes.content_logger.propagate)
        self.assertFalse(outcomes.logger.propagate)

    def test_an_empty_content_path_keeps_no_body_and_still_writes_the_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.jsonl")
            outcomes = load_outcomes({"GATEWAY_AUDIT_LOG": path,
                                      "GATEWAY_CONTENT_LOG": ""})
            with _captured() as out:
                outcomes.setup()
                outcomes.record("ok", "s", "t", result=RESULT)
            _detach(outcomes)
            self.assertEqual(outcomes.content_logger.handlers, [])
            self.assertIn("NOT KEPT", out.getvalue())
            self.assertIn("responses -> not kept", outcomes.describe())
            with open(path) as f:
                line = json.loads(f.read())
            self.assertEqual(os.listdir(tmp), ["audit.jsonl"])
        self.assertEqual(line["sha256"], hashlib.sha256(
            load_protocol().encode(RESULT)).hexdigest())

    def test_the_banner_states_the_window_as_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = load_outcomes({
                "GATEWAY_AUDIT_LOG": os.path.join(tmp, "audit.jsonl"),
                "GATEWAY_CONTENT_LOG": os.path.join(tmp, "c", "responses.jsonl")})
            outcomes.setup()
            _detach(outcomes)
            _detach_content(outcomes)
        self.assertIn(f"{64 << 20}B of responses", outcomes.describe())

    def test_the_defaults_are_the_operators_choice(self):
        # Decision 1 of task 163's plan: 16 MiB x 4 files, 256 KiB per record.
        outcomes = load_outcomes({"GATEWAY_AUDIT_LOG": ""})
        self.assertEqual((outcomes.CONTENT_MAX_BYTES, outcomes.CONTENT_BACKUPS + 1,
                          outcomes.CONTENT_RECORD_MAX), (16 << 20, 4, 256 << 10))

    def test_a_torn_content_record_is_ended_at_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = os.path.join(tmp, "responses.jsonl")
            with open(content, "w") as f:
                f.write('{"ts": 1.0, "bo')
            outcomes = load_outcomes({"GATEWAY_AUDIT_LOG": "",
                                      "GATEWAY_CONTENT_LOG": content})
            with _captured():
                outcomes.setup()
                outcomes.record("ok", "s", "t", result=RESULT)
            _detach_content(outcomes)
            with open(content) as f:
                lines = f.read().splitlines()
        self.assertEqual(json.loads(lines[1])["body"],
                         load_protocol().encode(RESULT).decode())


class SetupTests(unittest.TestCase):
    """The two configured states, and the one that must not be reachable by accident."""

    def test_a_configured_path_that_cannot_be_opened_refuses_to_start(self):
        # Not best-effort: the file is the ONLY path into the durable record, so a
        # path that was configured and does not work is a misconfiguration to surface
        # at start rather than an empty table to discover weeks later.
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
            _detach(outcomes)


class TornRecordTests(unittest.TestCase):
    """``setup`` ends a record an earlier run left half-written (see
    ``outcomes._end_torn_record``)."""

    FRAGMENT = '{"ts": 1.0, "stage": "tool-result", "sta'

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "audit.jsonl")

    def _start_with(self, existing: str | None) -> tuple[list[str], str]:
        """Start on a file holding ``existing`` (None: no file), record one outcome;
        return the file's lines and what was printed."""
        if existing is not None:
            with open(self.path, "w") as f:
                f.write(existing)
        outcomes = load_outcomes({"GATEWAY_AUDIT_LOG": self.path})
        with _captured() as out:
            outcomes.setup()
            outcomes.record("ok", "github", "get_me")
        _detach(outcomes)
        with open(self.path) as f:
            return f.read().splitlines(), out.getvalue()

    def test_a_torn_record_is_ended_so_the_next_one_survives(self):
        lines, out = self._start_with(self.FRAGMENT)
        self.assertEqual(lines[0], self.FRAGMENT)
        self.assertEqual(json.loads(lines[1])["tool"], "get_me")
        self.assertIn("ended mid-record", out)

    def test_a_healthy_start_says_nothing_about_the_file(self):
        whole = '{"ts": 1.0, "stage": "tool-result"}'
        for existing, before in ((None, []), ("", []), (whole + "\n", [whole])):
            with self.subTest(existing=existing):
                lines, out = self._start_with(existing)
                self.assertEqual(lines[:-1], before)
                self.assertEqual(json.loads(lines[-1])["tool"], "get_me")
                self.assertNotIn(self.path, out)
                os.remove(self.path)

    class _FullVolume(io.BytesIO):
        """Opens, seeks and reads; refuses the newline, as a full volume does."""

        def write(self, _):
            raise OSError(28, "No space left on device")

    def test_a_fragment_that_cannot_be_ended_does_not_stop_the_gateway(self):
        # The volume is still full: not a misconfiguration, so ``setup`` still
        # attaches the file and says what the next record will cost. ``open`` is
        # patched in the module's namespace only; the handler opens through logging's.
        with open(self.path, "w") as f:
            f.write(self.FRAGMENT)
        refusals = {
            "at open": OSError(28, "No space left on device"),
            # Where a full volume actually refuses: everything up to the write works.
            "at write": lambda *_: self._FullVolume(self.FRAGMENT.encode()),
        }
        for where, refusal in refusals.items():
            with self.subTest(where=where):
                outcomes = load_outcomes({"GATEWAY_AUDIT_LOG": self.path})
                self.addCleanup(_detach, outcomes)
                with _captured() as out, \
                        mock.patch.object(outcomes, "open", create=True,
                                          side_effect=refusal):
                    outcomes.setup()
                self.assertTrue(outcomes._to_file)
                self.assertIn("No space left on device", out.getvalue())


class ToolOutcomesTargetTests(unittest.TestCase):
    """`make tool-outcomes`' script, run as the Makefile holds it, over a stream with
    a line from each gateway: the one that writes the resume token and the one older
    than tokens, which wrote the approval id. The container runs it from the gateway's
    side of the volume, where lines from before an upgrade can still be."""

    @staticmethod
    def _script():
        makefile = (pathlib.Path(__file__).resolve().parent.parent
                    / "Makefile").read_text()
        return re.search(r"^define TOOL_OUTCOMES_PY\n(.*?)^endef$", makefile,
                         re.S | re.M).group(1)

    def test_it_shows_a_token_line_and_a_legacy_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.jsonl")
            with open(path, "w") as f:
                base = {"ts": 1000.0, "stage": "tool-result", "server": "mcp-github",
                        "tool": "create_pr", "client": None, "reason": None}
                f.write(json.dumps({**base, "status": "ok",
                                    "resume_token": "c" * 32}) + "\n")
                f.write(json.dumps({**base, "status": "tool-error",
                                    "approval_id": "a" * 32}) + "\n")
                f.write(json.dumps({**base, "status": "ok",
                                    "resume_token": None}) + "\n")
            shown = subprocess.run(  # noqa: S603 (this interpreter, the repo's own script)
                [sys.executable, "-I", "-c", self._script()], capture_output=True,
                text=True, check=True,
                env={"GATEWAY_AUDIT_LOG": path, "OUTCOMES": "20"}).stdout
        lines = shown.splitlines()
        self.assertIn("token=" + "c" * 32, lines[0])
        self.assertIn("approval=" + "a" * 32, lines[1])
        self.assertTrue(lines[2].endswith("  -"), lines[2])
        self.assertIn("3 of 3", lines[-1])



class ToolContentTargetTests(unittest.TestCase):
    """`make tool-content`'s script, run as the Makefile holds it, over an active file
    and a rotated one."""

    SHA = "ab" * 32

    @staticmethod
    def _script():
        makefile = (pathlib.Path(__file__).resolve().parent.parent
                    / "Makefile").read_text()
        return re.search(r"^define TOOL_CONTENT_PY\n(.*?)^endef$", makefile,
                         re.S | re.M).group(1)

    def _run(self, sha: str, files: dict[str, list[str]]):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "responses.jsonl")
            for suffix, lines in files.items():
                with open(path + suffix, "w") as f:
                    f.writelines(line + "\n" for line in lines)
            return subprocess.run(  # noqa: S603 (this interpreter, the repo's own script)
                [sys.executable, "-I", "-c", self._script()], capture_output=True,
                text=True, check=False,
                env={"GATEWAY_CONTENT_LOG": path, "SHA": sha})

    def _record(self, ts, body, sha=None):
        return json.dumps({"ts": ts, "server": "mcp-github", "tool": "get_pr",
                           "client": None, "bytes": len(body),
                           "sha256": sha or self.SHA, "truncated": False,
                           "body": body})

    def test_it_finds_a_hash_in_the_active_and_rotated_files_oldest_first(self):
        shown = self._run(self.SHA, {
            ".2": [self._record(1.0, "oldest")],
            ".1": [self._record(2.0, "other", sha="cd" * 32)],
            "": [self._record(3.0, "newest"),
                 # The hash inside a body, not as the record's own: not a match.
                 self._record(4.0, "mentions " + self.SHA, sha="ef" * 32)]})
        self.assertEqual(shown.returncode, 0, shown.stderr)
        rows = [json.loads(line) for line in shown.stdout.splitlines()]
        self.assertEqual([r["body"] for r in rows], ["oldest", "newest"])
        self.assertIn("2 record(s)", shown.stderr)

    def test_an_unknown_hash_prints_nothing(self):
        shown = self._run("cd" * 32, {"": [self._record(1.0, "body")]})
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(shown.stdout, "")
        self.assertIn("0 record(s)", shown.stderr)

    def test_a_torn_line_carrying_the_hash_is_counted_and_skipped(self):
        shown = self._run(self.SHA, {"": [self._record(1.0, "body")[:-20],
                                          self._record(2.0, "whole")]})
        self.assertEqual([json.loads(line)["body"] for line in
                          shown.stdout.splitlines()], ["whole"])
        self.assertIn("1 unparseable", shown.stderr)

    def test_control_characters_stay_escaped(self):
        # The second line holds DEL and a C1 CSI raw, which JSON parses: the file is
        # the gateway's, but what reaches a terminal does not depend on that.
        body = "a\x1b[2Jb\rc\x7fd\u009b2Je\u2028f"
        raw_line = json.dumps(json.loads(self._record(2.0, body)), ensure_ascii=False)
        self.assertIn("\x7f", raw_line)
        shown = self._run(self.SHA, {"": [self._record(1.0, body), raw_line]})
        out = shown.stdout
        self.assertTrue(out.isascii())
        for raw in ("\x1b", "\r", "\x7f", "\u009b"):
            self.assertNotIn(raw, out)
        self.assertEqual([json.loads(line)["body"] for line in out.splitlines()],
                         [body, body])

    def test_a_hash_given_in_capitals_is_found(self):
        shown = self._run(self.SHA.upper(), {"": [self._record(1.0, "body")]})
        self.assertEqual([json.loads(line)["body"] for line in
                          shown.stdout.splitlines()], ["body"])

    def test_only_numbered_siblings_are_read_as_rotations(self):
        shown = self._run(self.SHA, {".bak": [self._record(1.0, "copy")],
                                     ".1.gz": [self._record(2.0, "packed")],
                                     ".1": [self._record(3.0, "rotated")],
                                     "": [self._record(4.0, "active")]})
        self.assertEqual([json.loads(line)["body"] for line in
                          shown.stdout.splitlines()], ["rotated", "active"])

    def test_a_malformed_hash_is_refused(self):
        for sha in ("", "abc", "g" * 64, self.SHA + "0"):
            with self.subTest(sha=sha):
                shown = self._run(sha, {"": [self._record(1.0, "body")]})
                self.assertNotEqual(shown.returncode, 0)
                self.assertEqual(shown.stdout, "")


if __name__ == "__main__":
    unittest.main()
