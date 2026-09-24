#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""How a tool call ENDED — the record only this process can write.

The control plane audits the DECISION: allowed, denied, or held and then granted. It
cannot audit the outcome, and the reason is structural rather than an oversight — its
claim row is written BEFORE the call, so by the time the call succeeds or fails the
authority has already answered and gone. The gateway is the only component that sees
the reply, so if it does not record how the call went, nothing does.

Observed on 2026-09-16, which is why this module exists: an approved
`create_pull_request` was claimed — audited as a grant released, "capability released"
— and then refused by GitHub with a 403. The trail said a human approved a PR being
opened. No PR was opened, and nothing anywhere said so.

A FILE, NOT A POST, and the asymmetry decides it. Authorization is a round trip BEFORE
the side effect: if the control plane cannot be reached the gateway refuses, nothing
ran, and fail-closed is a complete answer. This record is written AFTER the side
effect, and there is no fail-closed for it — a POST that fails means the call happened
and nothing recorded it, which is the hole this exists to close. **A record of
something irreversible cannot fail closed; it can only buffer.** So it lands in a file
the control plane drains on its own schedule (`control-plane/ingest.py`), which
survives a control-plane restart, keeps tool-result latency off the authority's
availability, and adds no write surface to the crown jewel's bridge.

STDOUT TOO, always, for the reason the egress proxy mirrors its own: `make logs-tg`
should be a live feed of outcomes rather than of this service's internals. The file is
the ingestible copy; the stdout line is the one a human watches.

Nothing here decides anything. It is called after the fact, by the one function that
dials a server, and a failure to record must never turn into a failure to answer the
agent — see ``record``.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import time

#: Where the JSONL lands, on a volume the control plane mounts read-only. Set to the
#: EMPTY STRING to run with no file at all; anything else is a path that has to work.
#: See ``setup`` for why those are the only two options.
AUDIT_PATH = os.environ.get("GATEWAY_AUDIT_LOG", "/var/log/tool-gateway/audit.jsonl")

#: Rotation, mirroring the egress proxy's: bounded total bytes, oldest dropped. The
#: control plane's ingest follows files by INODE and drains oldest-first precisely so
#: this cannot strand records — see ``_drain_egress_audit``.
AUDIT_MAX_BYTES = int(os.environ.get("GATEWAY_AUDIT_MAX_BYTES", str(8 << 20)))
AUDIT_BACKUPS = int(os.environ.get("GATEWAY_AUDIT_BACKUPS", "3"))

#: Cap on any single string in a record. These fields are third-party text — an error
#: message a server wrote — and this is a trust boundary: a server that answers with a
#: megabyte-long error must not be able to write a megabyte per call into the volume,
#: nor push that through ingest into the crown-jewel store. The control plane caps
#: again on its side (``store.DRAIN_MAX_FIELD``); this one keeps the FILE bounded,
#: which the other cannot do.
FIELD_MAX = int(os.environ.get("GATEWAY_AUDIT_FIELD_MAX", "512"))

#: The vocabulary of ``status``, and the whole reason this module reads the reply
#: rather than just catching exceptions. MCP fails at three layers and only two of them
#: look like failure:
#:
#:   ok               the server answered and the tool reports success
#:   tool-error       a SUCCESSFUL result carrying `isError: true` — the MCP
#:                    convention, since tool failures come back as results so the model
#:                    can read them. THE 403 ABOVE LANDED HERE. A gateway that only
#:                    recorded "did the HTTP call work" would have logged it as success.
#:   rpc-error        a well-formed reply carrying a JSON-RPC `error` object: the
#:                    request was rejected, as opposed to the tool having run and failed
#:   transport-error  no MCP answer at all — refused, HTTP error, unparseable
#:   timeout          no answer IN TIME, which is not the same thing: the call may have
#:                    landed upstream. For an approved write that distinction is the
#:                    whole question, and it is why the grant is spent either way.
STATUSES = ("ok", "tool-error", "rpc-error", "transport-error", "timeout")

logger = logging.getLogger("tool-gateway.audit")


class _LoudFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating file sink that SAYS when it could not write.

    Worth a subclass for one method. ``logging.FileHandler.emit`` catches its own
    exceptions and routes them to ``handleError``, whose default prints a bare
    "--- Logging error ---" traceback to stderr — so a full disk would stop the
    outcome stream while the operator's only clue was a logging traceback with no
    hint that an audit record had been lost. The whole point of this module is that a
    missing outcome is never silent, and that promise cannot rest on a default that
    was written for application logs.

    Nothing is re-raised: see ``record``. The call has already happened."""

    def handleError(self, record: logging.LogRecord) -> None:
        print(f"tool-gateway: OUTCOME AUDIT FAILED to write to {AUDIT_PATH} — the "
              f"record below is the only copy", flush=True)

#: Whether ``setup`` has attached a file handler. Read only by ``describe``, so the
#: startup banner can say which of the two configured states this process is in.
_to_file = False


def setup() -> None:
    """Attach the file sink, or FAIL TO START.

    Not best-effort, and that is the difference from the egress proxy's audit file. For
    the proxy, stdout is the primary local stream and the control plane holds the
    authoritative central record, so the file is a convenience. Here the file is the
    ONLY path into the durable record — nothing else carries an outcome anywhere — so a
    path that was configured and does not work is a misconfiguration to surface at
    start, not a degradation to discover weeks later by noticing an empty table.

    The off switch is explicit and empty: ``GATEWAY_AUDIT_LOG=""`` runs with stdout
    only, for a hand-run container with no volume. Same idiom as the egress proxy's
    ``_assert_guard_configured`` — refuse a broken configuration, and make "off" a
    thing someone has to write down."""
    global _to_file
    if not AUDIT_PATH:
        print("tool-gateway: outcome audit is STDOUT ONLY (GATEWAY_AUDIT_LOG is "
              "empty) — nothing will reach the control plane's audit store",
              flush=True)
        return
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
    handler = _LoudFileHandler(
        AUDIT_PATH, maxBytes=AUDIT_MAX_BYTES, backupCount=AUDIT_BACKUPS)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # Never up to the root logger: uvicorn configures that, and a duplicate of every
    # record would land in stdout twice and read as two calls.
    logger.propagate = False
    _to_file = True


def describe() -> str:
    """One line for the startup banner. The empty-string case is deliberate enough to
    be worth saying out loud on every boot rather than only at the moment it is set."""
    return (f"outcomes -> {AUDIT_PATH} (rotating, {AUDIT_MAX_BYTES}B x "
            f"{AUDIT_BACKUPS + 1})" if _to_file else "outcomes -> stdout only")


def _field(value: object) -> object:
    """Strings capped, everything else passed through. ``status`` and the names are
    this module's own vocabulary or charset-bounded upstream; the one genuinely
    unbounded field is ``reason``, and it is why this exists."""
    return value[:FIELD_MAX] if isinstance(value, str) else value


#: What ends or rewrites a terminal line: C0 and C1 controls (newline, carriage
#: return, the ESC that opens an ANSI sequence), DEL, and the Unicode line and
#: paragraph separators.
_UNPRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _printable(value: object) -> str:
    """One field of the stdout line: capped like the file's, and kept on one line.
    ``reason`` is a third party's text, and with its newlines intact a server could
    append a whole forged ``OUTCOME ok …`` line to `make logs-tg` — the feed a human
    reads to see what ran. Escaped rather than stripped, so what was sent stays
    visible."""
    return _UNPRINTABLE.sub(lambda m: m.group().encode("unicode_escape").decode("ascii"),
                            str(_field(value)))


def record(status: str, server: str, tool: str, reason: str | None = None,
           approval_id: str | None = None, client: str | None = None) -> None:
    """Write one outcome. Returns nothing and RAISES NOTHING.

    The swallow is deliberate and is the one place in this file worth arguing about. By
    the time this is called the tool call has already happened — so an exception here
    could only turn a completed call into an error result for the agent, losing the
    reply on top of losing the record. A full disk would stop the agent working rather
    than stop the trail growing, which is the wrong failure in both halves. It is
    reported on stdout instead, where it is visible and where the record itself is
    mirrored, so a silently un-ingested stream still leaves a trace in `make logs-tg`.
    Two paths reach that report, because a failure arrives two ways: a handler that
    raises outright is caught below, and the file sink's own swallowed write errors
    surface through ``_LoudFileHandler.handleError``.

    `stage` is carried so the control plane's ingest can be INCURIOUS about everything
    else — a line it does not fully recognise is dropped rather than guessed at, the
    same discipline `_ingest_row` already applies to the proxy's file."""
    entry = {"ts": round(time.time(), 3), "stage": "tool-result",
             "status": status, "server": _field(server), "tool": _field(tool),
             "approval_id": _field(approval_id), "client": _field(client),
             "reason": _field(reason)}
    line = json.dumps(entry)
    try:
        logger.info(line)
    except Exception as exc:  # noqa: BLE001 — see the docstring: never fail the call
        print(f"tool-gateway: OUTCOME AUDIT FAILED to write ({exc!r}) — the record "
              f"below is the only copy", flush=True)
    # After the file, and unconditionally: if the write above failed this is the
    # remaining copy, and if it succeeded this is the live feed.
    print(f"OUTCOME {status} {_printable(server)}__{_printable(tool)}"
          + (f" approval_id={_printable(approval_id)}" if approval_id else "")
          + (f" client={_printable(client)}" if client else "")
          + (f" :: {_printable(reason)}" if reason else ""), flush=True)
