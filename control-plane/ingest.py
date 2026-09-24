# SPDX-License-Identifier: Apache-2.0
"""Audit ingest — draining the data plane's own records into the store.

TWO STREAMS, one mechanism. Both are JSONL files this process mounts READ-ONLY from a
shared named volume, and both exist because their writer knows something this process
structurally cannot:

  egress    Not every egress decision is made by /authorize — the relay guard, the port
            gate, the SNI anti-fronting check and the permanent-lifeline allow are all
            decided locally in the proxy, on purpose, and a control-plane outage
            produces local fail-closed denials by definition. Those never reached this
            store, so the UI's list was a record of round-trips rather than of
            decisions, and a domain-fronting refusal — the single most alarming thing
            the proxy can emit — was visible only in `make logs-ep`.

  tool      How a tool call ENDED. This process writes its claim row BEFORE the call
            runs, so by the time one succeeds or fails the authority has already
            answered and gone: an approved write that GitHub then refuses looks, in
            the trail, exactly like one that landed. Only the gateway sees the reply.

We PULL rather than have either writer push, and that choice buys the property that
matters: the cursor lives in this same SQLite, so ingesting rows and advancing the
cursor are ONE transaction. A crash mid-drain rolls back both, which makes the
ingest exactly-once with no idempotency key, no UNIQUE index and no dedup pass —
the tax any at-least-once push (broker or POST) would have imposed. It also
self-heals across an outage of THIS service, since the file is durable and the
cursor simply resumes, and it leaves the security-critical writers untouched:
no new dependency, no fire-and-forget task in a hot path.

For the tool stream the same argument arrives from the other side and is sharper: its
record is written AFTER an irreversible act, so it cannot fail closed — only buffer
(DESIGN.md). A POST that failed would mean the call happened and nothing recorded it.

What a stream must supply is in ``_Stream``: where its file is, which audit columns it
fills, and how to turn one line into a row. Everything else here — rotation, the
inode-keyed cursor, the bounded block, the partial-line rule — is shared, because none
of it has anything to do with what the lines mean.
"""
from __future__ import annotations

import asyncio
import glob
import json
import math
import os

import policy
import store

EGRESS_AUDIT_LOG = os.environ.get("EGRESS_AUDIT_LOG", "/var/log/egress/audit.jsonl")
# The gateway's outcome stream, on its own volume. Held equal to the gateway's own
# default (`tool-gateway/outcomes.py`) and to both compose mounts by
# tests/test_topology.py — three files, no compiler, and a mismatch is silent: the
# gateway records happily, this finds no file, and the column is simply always empty.
TOOL_AUDIT_LOG = os.environ.get("TOOL_AUDIT_LOG",
                                "/var/log/tool-gateway/audit.jsonl")
# Seconds between drains; 0 disables ingest entirely. An idle pass is a short scan of
# the audit dir and a stat per file (rotation, below), so frequency is nearly free —
# what bounds it from ABOVE is that the
# UI polls /api/audit every 4s, so anything under that keeps the drain out of the
# critical path and total event-to-screen lag stays dominated by a poll the operator
# already lives with. Above it, this interval becomes the lag.
DRAIN_INTERVAL = float(os.environ.get("CONTROL_AUDIT_DRAIN_INTERVAL", "2"))
# Bytes per drain pass. Bounds both memory and how long one transaction holds the
# write lock, so a large backlog (first run against an existing volume) drains over
# several passes instead of stalling startup in a single giant commit.
DRAIN_BLOCK = int(os.environ.get("CONTROL_AUDIT_DRAIN_BLOCK", str(1 << 20)))


# The audit columns each stream fills. Two streams describe different events, so
# neither should have to carry the other's columns as a row of NULLs it has to know to
# write — an egress decision has no tool identity, and an outcome has no host or port.
_EGRESS_COLUMNS = ("ts", "kind", "stage", "host", "port", "proto", "client",
                   "client_class", "method", "url", "reason")
_TOOL_COLUMNS = ("ts", "kind", "stage", "client", "client_class", "reason",
                 "server", "tool", "approval_id", "status")


def _insert_for(columns: tuple[str, ...]) -> str:
    """The INSERT for one stream's columns, built from the tuple so the statement
    cannot disagree with the rows fed to it. The interpolated values are the column
    names above — module constants, never a request field — so this is not a query
    built from input."""
    return (f"INSERT INTO audit({', '.join(columns)}) "  # noqa: S608
            f"VALUES ({', '.join('?' * len(columns))})")


class _Stream:
    """One JSONL file this process drains, and the little it needs to know about it.

    ``row`` returns a DICT keyed by column name, not a positional tuple. That is the
    one shape change worth making while generalising: the tuple version agreed with its
    INSERT only by hand-counted index, and adding `client_class` in the middle silently
    moved `reason` under the mirror's `r[9]` — which printed an agent-supplied URL as a
    decision's reason. A dict cannot land a value in the wrong column."""

    def __init__(self, name, path, columns, row, describe):
        self.name = name              # what an operator sees in a failure line
        self.path = path
        self.columns = columns
        self.insert = _insert_for(columns)
        self.row = row                # (line: bytes) -> dict | None
        self.describe = describe      # (row: dict) -> str, for the stdout mirror


def _ingest_field(value: object) -> str | None:
    if value is None:
        return None
    return str(value)[:store.DRAIN_MAX_FIELD]


def _parsed(line: bytes) -> dict | None:
    """One line as a dict, or None. Shared by both mappers, and deliberately incurious:
    these are files written by components that face the sandbox, so a line that does not
    parse as a JSON object is dropped rather than guessed at."""
    try:
        rec = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


def _timestamp(rec: dict) -> float | None:
    """The WRITER's own timestamp, not our receipt time: these are historical rows, and
    stamping them on arrival would sort a backlog as if it had all just happened. NaN
    and infinity are refused here rather than left to sort strangely later."""
    ts = rec.get("ts")
    if (not isinstance(ts, (int, float)) or isinstance(ts, bool)
            or not math.isfinite(ts)):
        return None
    return float(ts)


def _egress_row(line: bytes) -> dict | None:
    """Map one line of the proxy's audit file to an ``audit`` row, or None to skip.

    The filter that matters is ``central is False`` — the proxy's marker for "no
    /authorize call recorded this, so my line is the only record". Testing for the
    literal False (not falsiness, not absence) is what makes a line the proxy wrote
    before this field existed, or one with a garbled flag, under-report rather than
    double-count every already-audited request."""
    rec = _parsed(line)
    if rec is None or rec.get("central") is not False:
        return None
    if rec.get("decision") not in ("allow", "deny", "hold"):
        return None
    ts = _timestamp(rec)
    if ts is None:
        return None
    port = rec.get("port")
    if not isinstance(port, int) or isinstance(port, bool):
        port = None
    client = _ingest_field(rec.get("client"))
    # Classified HERE rather than read off the line: the proxy does not send a class
    # and should not start, because it is deliberately client-agnostic about
    # everything except the lifeline (see policy.CLIENT_CLASSES). These rows are the
    # proxy's LOCAL decisions — the relay guard, the port gate, the anti-fronting
    # check, the lifeline allow, the fail-closed denials — so no rule was consulted
    # for them and the class is descriptive rather than load-bearing. Deriving it
    # anyway is what keeps the column populated across the whole audit view, so an
    # operator filtering by client class does not silently lose exactly the alarming
    # rows. It is derived at INGEST, seconds behind the event, not at read time.
    # `decision` on the WIRE, `kind` in the column, and neither is a missed rename:
    # the proxy writes decisions and that is what its line says, while the column holds
    # several kinds of row (audit.KINDS) of which only some are decisions.
    return {"ts": ts, "kind": _ingest_field(rec.get("decision")),
            "stage": _ingest_field(rec.get("stage")),
            "host": _ingest_field(rec.get("host")), "port": port,
            "proto": _ingest_field(rec.get("proto")), "client": client,
            "client_class": policy._client_class(client),
            "method": _ingest_field(rec.get("method")),
            "url": _ingest_field(rec.get("url")),
            "reason": _ingest_field(rec.get("reason"))}


def _tool_row(line: bytes) -> dict | None:
    """Map one line of the gateway's outcome stream to an ``audit`` row, or None.

    Same incuriousness as above, and it matters more here: this file is the only place
    a third party's error text enters the store, so anything not recognised is dropped
    rather than interpreted. ``stage`` is the discriminator — the gateway writes it on
    every record precisely so this can be a whitelist rather than a guess — and the
    status has to be one of the words both sides agreed on, checked here because the
    two are a file apart with no compiler between them.

    ``kind`` is a CONSTANT, not read off the line. An outcome is not a decision the
    writer made, and a stream that could name its own decision word could write `allow`
    into the audit trail — which would let a compromised gateway forge governance
    history rather than merely report a result. The word it gets is `outcome`, and
    `status` is where its own vocabulary lives.

    Not `observe`, though that word is already documented as "not a decision": its
    value depends on being RARE — one writer, changes only, dimmed in the UI — and
    outcome rows are one per tool call."""
    rec = _parsed(line)
    if rec is None or rec.get("stage") != "tool-result":
        return None
    if rec.get("status") not in TOOL_STATUSES:
        return None
    ts = _timestamp(rec)
    if ts is None:
        return None
    client = _ingest_field(rec.get("client"))
    return {"ts": ts, "kind": "outcome", "stage": "tool-result",
            "client": client, "client_class": policy._client_class(client),
            "reason": _ingest_field(rec.get("reason")),
            "server": _ingest_field(rec.get("server")),
            "tool": _ingest_field(rec.get("tool")),
            "approval_id": _ingest_field(rec.get("approval_id")),
            "status": _ingest_field(rec.get("status"))}


def _describe_egress(row: dict) -> str:
    shown = {k: store._printable(v) for k, v in row.items()}
    return (f"{shown['kind']} (ingested) stage={shown['stage']} host={shown['host']} "
            f"client={shown['client']} client_class={shown['client_class']} "
            f":: {shown['reason']}")


def _describe_tool(row: dict) -> str:
    # Shaped like the gateway's own OUTCOME line so the two logs read alike side by
    # side, which is how a broken ingest is spotted: the same call present in
    # `make logs-tg` and absent here.
    shown = {k: store._printable(v) for k, v in row.items()}
    return (f"outcome (ingested) {shown['status']} {shown['server']}__{shown['tool']}"
            + (f" approval_id={shown['approval_id']}" if row["approval_id"] else "")
            + (f" :: {shown['reason']}" if row["reason"] else ""))


#: The gateway's status vocabulary, restated rather than imported: that module lives in
#: another image on another network, so this process cannot import it. Held equal to
#: ``outcomes.STATUSES`` by tests/test_control_plane_ingest.py — a word added there and
#: not here is dropped silently at the door, which is the fail-safe direction but still
#: a gap worth a test rather than a habit.
TOOL_STATUSES = ("ok", "tool-error", "rpc-error", "transport-error", "timeout")

#: Every stream this process drains. Adding one is this tuple plus a mapper; nothing
#: below knows how many there are.
STREAMS = (
    _Stream("egress", EGRESS_AUDIT_LOG, _EGRESS_COLUMNS, _egress_row,
            _describe_egress),
    _Stream("tool outcomes", TOOL_AUDIT_LOG, _TOOL_COLUMNS, _tool_row, _describe_tool),
)


def _audit_log_files(base: str) -> list[tuple[str, os.stat_result]]:
    """A stream's audit files, OLDEST CONTENT FIRST: the size-rotated siblings
    (``audit.jsonl.N``; higher N is older) followed by the active file last.

    RotatingFileHandler renames on rollover, so a given file's SUFFIX changes over
    time but its inode does not — callers follow a file by inode, never by name, and
    this only fixes the order to drain in. A sibling missing because a rotation raced
    this scan is skipped and reappears next pass."""
    rotated = []
    for path in glob.glob(glob.escape(base) + ".*"):
        suffix = path[len(base) + 1:]
        if suffix.isdigit():                         # .1/.2/... only, not .new etc.
            rotated.append((int(suffix), path))
    rotated.sort(reverse=True)                        # oldest (highest N) first
    out = []
    for path in [p for _, p in rotated] + [base]:
        try:
            out.append((path, os.stat(path)))
        except OSError:
            continue
    return out


def _drain(stream: _Stream) -> int:
    """Ingest one bounded block of one stream. Returns bytes consumed.

    The log is ROTATED by size (RotatingFileHandler in proxies/egress/addon.py): at a
    cap the active file is renamed aside, a fresh one takes its place, and the oldest
    backup is dropped. So "the log" is the active file plus a few rotated siblings, and
    ingest must drain them OLDEST-FIRST — otherwise the rename would strand the
    un-ingested tail of a file in a sibling this loop never reads, silently dropping
    decisions, which an audit trail must never do.

    Position is tracked by INODE, not name: a rotation shuffles the .N suffixes but
    never a file's inode. A rotated file never grows again, so once its end is reached
    we step to the next-oldest at offset 0; only the active file is ever appended to.
    Reads only up to the LAST NEWLINE, so a line the proxy is mid-append on is left for
    the next pass. Rows and the cursor advance in ONE transaction (see the module
    docstring) — do not split them."""
    files = _audit_log_files(stream.path)
    if not files:
        # No file at all — the proxy may not have started, or the volume is absent.
        # Raise (not swallow): _audit_drain_loop reports it once on the transition, so
        # "no ingest at all" can never become a silent steady state.
        os.stat(stream.path)
        return 0

    with store._connect() as conn:
        row = conn.execute("SELECT inode, offset FROM audit_cursor WHERE path=?",
                           (stream.path,)).fetchone()
        # Find the file we were reading by its inode. First run (no row) or a cursor
        # whose file has aged out (deleted before we finished it) both start at the
        # oldest file still present; the latter is a genuine unread gap, so it is
        # reported LOUDLY rather than passed over in silence.
        idx, offset = 0, 0
        if row is not None:
            found = next((i for i, (_, st) in enumerate(files)
                          if st.st_ino == row["inode"]), None)
            if found is None:
                print("control-plane: audit ingest cursor lost its file (inode "
                      f"{row['inode']} gone — a backup rotated out before it drained); "
                      "resuming at the oldest file present, some decisions may be "
                      "un-ingested", flush=True)
            else:
                idx, offset = found, row["offset"]

        path, st = files[idx]
        if st.st_size < offset:
            # Truncated in place (same inode, fewer bytes) — start this file over.
            offset = 0
        # Caught up on a ROTATED file (one with newer files after it): it never grows
        # again, so advance to the next-oldest at 0. Skips fully-drained/empty backups
        # in one pass; lands on the active file, or a backup with bytes still to read.
        while st.st_size == offset and idx < len(files) - 1:
            idx += 1
            path, st, offset = files[idx][0], files[idx][1], 0
        if st.st_size == offset:
            return 0                                  # active file, nothing new

        is_active = path == stream.path
        with open(path, "rb") as f:
            # Between the scan and this open the file could have been rotated out from
            # under the path. fstat the OPEN handle: if the inode moved, bail and let
            # the next pass re-resolve, rather than read one file and credit another.
            if os.fstat(f.fileno()).st_ino != st.st_ino:
                return 0
            f.seek(offset)
            block = f.read(DRAIN_BLOCK)
        cut = block.rfind(b"\n")
        if cut < 0 and len(block) < DRAIN_BLOCK and is_active:
            # No newline yet and the ACTIVE file ends here: the proxy is mid-append.
            # Consume nothing and pick it up next pass — parsing half a record, or
            # dropping it as "oversized", would both be wrong. (A rotated file never
            # grows, so an unterminated tail there is genuine and falls through below.)
            return 0
        if cut < 0:
            # A full block with no newline: a line longer than the block. Skip past
            # it — its fragments fail to parse and are dropped, which self-limits
            # rather than wedging the cursor here and stalling every later line
            # behind one oversized record.
            print(f"control-plane: audit ingest skipping an oversized line at offset "
                  f"{offset} in {path} (>{DRAIN_BLOCK} bytes)", flush=True)
            consumed = len(block)
        else:
            consumed = cut + 1
            rows = [r for r in (stream.row(ln)
                                for ln in block[:consumed].split(b"\n") if ln.strip())
                    if r is not None]
            if rows:
                conn.executemany(
                    stream.insert,
                    [tuple(r.get(c) for c in stream.columns) for r in rows])
                # Mirror to stdout like _audit does, so `make logs-cp` stays a live
                # feed of what HAPPENED and not merely of this service's own
                # round-trips. Marked `ingested` because it is: something another
                # component recorded, arriving late and out of order relative to the
                # lines around it.
                for r in rows:
                    print(f"AUDIT {stream.describe(r)}", flush=True)
        conn.execute(
            "INSERT INTO audit_cursor(path, inode, offset) VALUES (?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET inode=excluded.inode, "
            "offset=excluded.offset",
            (stream.path, st.st_ino, offset + consumed))
        conn.commit()
    return consumed


# Whether each stream's last drain attempt failed, so the loop can report a transition
# instead of the same line every DRAIN_INTERVAL. A missing file is the NORMAL state
# before its writer first writes, and a permanently silent ingest is exactly the failure
# this whole mechanism exists to remove — so it is reported once on the way in and once
# on the way out, and never in between.
#
# PER STREAM, not one flag for all of them: a broken egress ingest and a broken tool
# ingest lose different evidence, and one failing must not mute the other's recovery
# line or make its own look already-reported.
_drain_failing: dict[str, bool] = {}

#: What is lost while a stream is not draining, named per stream because the operator's
#: next move differs. Vague here would be worse than silent: this line is the only
#: warning that part of the trail has stopped filling.
_STREAM_STAKES = {
    "egress": "locally-decided egress will not appear in /api/audit",
    "tool outcomes": "how tool calls ENDED will not appear in /api/audit — approved "
                     "calls will show as granted with no record of what came of them",
}


async def _audit_drain_loop() -> None:
    while True:
        for stream in STREAMS:
            await _drain_stream(stream)
        await asyncio.sleep(DRAIN_INTERVAL)


async def _drain_stream(stream: _Stream) -> None:
    """One stream, drained to empty, with its own failure reported once.

    Each stream is attempted independently every pass: one whose volume is missing must
    not stop the other being ingested, which a single try around the loop would do."""
    try:
        # Drain until a pass consumes nothing, so a backlog clears in one wake-up
        # rather than one block per interval. "Consumed nothing" — not "consumed
        # less than a block" — is the right stop: a pass almost always stops short
        # of DRAIN_BLOCK because it cuts at the last newline inside it, so the
        # short-read test would sleep with the file still hours behind.
        #
        # Bounded anyway. Each pass strictly advances the cursor so this cannot
        # spin on a fixed file, but a writer appending faster than we drain would
        # otherwise keep the loop from ever yielding to its own sleep.
        for _ in range(64):
            if await asyncio.to_thread(_drain, stream) == 0:
                break
        if _drain_failing.get(stream.name):
            print(f"control-plane: {stream.name} ingest recovered ({stream.path})",
                  flush=True)
            _drain_failing[stream.name] = False
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — a dead loop must never be silent
        if not _drain_failing.get(stream.name):
            print(f"control-plane: {stream.name} ingest FAILING ({stream.path}): "
                  f"{e!r} — {_STREAM_STAKES[stream.name]} until this clears",
                  flush=True)
            _drain_failing[stream.name] = True
