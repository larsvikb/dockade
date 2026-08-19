# SPDX-License-Identifier: Apache-2.0
"""Egress audit ingest — draining the proxy's own decisions into the store.

The proxy's audit file, mounted READ-ONLY from the shared named volume. Not every
egress decision is made by /authorize — the relay guard, the port gate, the SNI
anti-fronting check and the permanent-lifeline allow are all decided locally in the
proxy, on purpose, and a control-plane outage produces local fail-closed denials by
definition. Those never reached this store, so the UI's "recent decisions" was a
record of round-trips rather than of decisions, and a domain-fronting refusal — the
single most alarming thing the proxy can emit — was visible only in `make logs-ep`.

We PULL rather than have the proxy push, and that choice buys the property that
matters: the cursor lives in this same SQLite, so ingesting rows and advancing the
cursor are ONE transaction. A crash mid-drain rolls back both, which makes the
ingest exactly-once with no idempotency key, no UNIQUE index and no dedup pass —
the tax any at-least-once push (broker or POST) would have imposed. It also
self-heals across an outage of THIS service, since the file is durable and the
cursor simply resumes, and it leaves the security-critical proxy image untouched:
no new dependency, no fire-and-forget task in a hot path.
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


# The audit columns an ingested row fills, in the order ``_ingest_row`` returns them.
# ONE definition, used to build the INSERT and to read fields back for the stdout
# mirror, because the two used to agree only by hand-counted index — and adding
# `client_class` in the middle silently moved `reason` under the mirror's `r[9]`,
# which would have printed the agent-supplied URL as the decision's reason.
_AUDIT_COLUMNS = ("ts", "decision", "stage", "host", "port", "proto", "client",
                  "client_class", "method", "url", "reason")
# Built once, from that tuple, so the statement cannot disagree with the rows fed to
# it. The interpolated values are the column names above — a module constant, never a
# request field — so this is not a query built from input.
_AUDIT_INSERT = (f"INSERT INTO audit({', '.join(_AUDIT_COLUMNS)}) "  # noqa: S608
                 f"VALUES ({', '.join('?' * len(_AUDIT_COLUMNS))})")


def _ingest_field(value: object) -> str | None:
    if value is None:
        return None
    return str(value)[:store.DRAIN_MAX_FIELD]


def _ingest_row(line: bytes) -> tuple | None:
    """Map one line of the proxy's audit file to an ``audit`` row, or None to skip.

    Skipping is the default for anything unrecognized. This parses a file written by
    the component that faces the sandbox, so it is deliberately incurious: a line it
    does not fully understand is dropped, never guessed at.

    The filter that matters is ``central is False`` — the proxy's marker for "no
    /authorize call recorded this, so my line is the only record". Testing for the
    literal False (not falsiness, not absence) is what makes a line the proxy wrote
    before this field existed, or one with a garbled flag, under-report rather than
    double-count every already-audited request."""
    try:
        rec = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(rec, dict) or rec.get("central") is not False:
        return None
    if rec.get("decision") not in ("allow", "deny", "hold"):
        return None
    ts = rec.get("ts")
    # The proxy's own timestamp, not our receipt time: these are historical rows, and
    # stamping them on arrival would sort a backlog as if it had all just happened.
    if (not isinstance(ts, (int, float)) or isinstance(ts, bool)
            or not math.isfinite(ts)):
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
    return (float(ts), _ingest_field(rec.get("decision")),
            _ingest_field(rec.get("stage")), _ingest_field(rec.get("host")),
            port, _ingest_field(rec.get("proto")), client,
            policy._client_class(client),
            _ingest_field(rec.get("method")), _ingest_field(rec.get("url")),
            _ingest_field(rec.get("reason")))


def _audit_log_files() -> list[tuple[str, os.stat_result]]:
    """The proxy's audit files, OLDEST CONTENT FIRST: the size-rotated siblings
    (``audit.jsonl.N``; higher N is older) followed by the active file last.

    RotatingFileHandler renames on rollover, so a given file's SUFFIX changes over
    time but its inode does not — callers follow a file by inode, never by name, and
    this only fixes the order to drain in. A sibling missing because a rotation raced
    this scan is skipped and reappears next pass."""
    base = EGRESS_AUDIT_LOG
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


def _drain_egress_audit() -> int:
    """Ingest one bounded block of the proxy's audit log. Returns bytes consumed.

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
    files = _audit_log_files()
    if not files:
        # No file at all — the proxy may not have started, or the volume is absent.
        # Raise (not swallow): _audit_drain_loop reports it once on the transition, so
        # "no ingest at all" can never become a silent steady state.
        os.stat(EGRESS_AUDIT_LOG)
        return 0

    with store._connect() as conn:
        row = conn.execute("SELECT inode, offset FROM audit_cursor WHERE path=?",
                           (EGRESS_AUDIT_LOG,)).fetchone()
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

        is_active = path == EGRESS_AUDIT_LOG
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
            rows = [r for r in (_ingest_row(ln)
                                for ln in block[:consumed].split(b"\n") if ln.strip())
                    if r is not None]
            if rows:
                conn.executemany(_AUDIT_INSERT, rows)
                # Mirror to stdout like _audit does, so `make logs-cp` stays a live
                # feed of DECISIONS and not merely of this service's own round-trips.
                # Marked `ingested` because it is: a decision the proxy made, arriving
                # late and out of order relative to the lines around it.
                for r in rows:
                    f = dict(zip(_AUDIT_COLUMNS, r))
                    print(f"AUDIT {f['decision']} (ingested) stage={f['stage']} "
                          f"host={f['host']} client={f['client']} "
                          f"client_class={f['client_class']} :: {f['reason']}",
                          flush=True)
        conn.execute(
            "INSERT INTO audit_cursor(path, inode, offset) VALUES (?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET inode=excluded.inode, "
            "offset=excluded.offset",
            (EGRESS_AUDIT_LOG, st.st_ino, offset + consumed))
        conn.commit()
    return consumed


# Whether the last drain attempt failed, so the loop can report a transition instead
# of the same line every DRAIN_INTERVAL. A missing file is the NORMAL state before the
# proxy first writes, and a permanently silent ingest is exactly the failure this
# whole change exists to remove — so it is reported once on the way in and once on
# the way out, and never in between.
_drain_failing = False


async def _audit_drain_loop() -> None:
    global _drain_failing
    while True:
        try:
            # Drain until a pass consumes nothing, so a backlog clears in one wake-up
            # rather than one block per interval. "Consumed nothing" — not "consumed
            # less than a block" — is the right stop: a pass almost always stops short
            # of DRAIN_BLOCK because it cuts at the last newline inside it, so the
            # short-read test would sleep with the file still hours behind.
            #
            # Bounded anyway. Each pass strictly advances the cursor so this cannot
            # spin on a fixed file, but a proxy appending faster than we drain would
            # otherwise keep the loop from ever yielding to its own sleep.
            for _ in range(64):
                if await asyncio.to_thread(_drain_egress_audit) == 0:
                    break
            if _drain_failing:
                print(f"control-plane: audit ingest recovered ({EGRESS_AUDIT_LOG})",
                      flush=True)
                _drain_failing = False
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a dead loop must never be silent
            if not _drain_failing:
                print(f"control-plane: audit ingest FAILING ({EGRESS_AUDIT_LOG}): "
                      f"{e!r} — locally-decided egress will not appear in "
                      f"/api/audit until this clears", flush=True)
                _drain_failing = True
        await asyncio.sleep(DRAIN_INTERVAL)
