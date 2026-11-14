# SPDX-License-Identifier: Apache-2.0
"""Audit ingest — draining the data plane's own records into the store.

TWO STREAMS, one mechanism. Both are JSONL files this process mounts READ-ONLY from a
shared named volume, and both exist because their writer knows something this process
structurally cannot:

  egress    Not every egress decision is made by /authorize — the relay guard, the port
            gate, the SNI anti-fronting check and the permanent-lifeline allow are all
            decided locally in the proxy, on purpose, and a control-plane outage
            produces local fail-closed denials by definition. Without this stream the
            UI's list would record round-trips rather than decisions, and a
            domain-fronting refusal — the single most alarming thing the proxy can
            emit — would be visible only in `make logs-ep`.

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
no new dependency, no fire-and-forget task in a hot path. For the tool stream a push
is ruled out outright: see "A record written after an irreversible act cannot fail
closed" in DESIGN.md.

What a stream must supply is in ``_Stream``: where its file is, which audit columns it
fills, how to turn one line into a row, if the row needs the store to finish, a pass
that does it on the drain's own connection, and how long a line this process knows is
owed may take to arrive before the file is called stalled. Everything else here —
rotation, the inode-keyed cursor, the bounded block, the partial-line rule, the stall
watch and the supervisor that restarts the loop — is shared, because none of it has
anything to do with what the lines mean.
"""
from __future__ import annotations

import asyncio
import glob
import json
import math
import os
import re
import threading
import time

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
# what bounds it from ABOVE is that the UI polls /api/audit every 4s, so anything
# under that keeps the drain out of the critical path and total event-to-screen lag
# stays dominated by a poll the operator already lives with. Above it, this interval
# becomes the lag.
DRAIN_INTERVAL = float(os.environ.get("CONTROL_AUDIT_DRAIN_INTERVAL", "2"))
# Bytes per drain pass. Bounds both memory and how long one transaction holds the
# write lock, so a large backlog (first run against an existing volume) drains over
# several passes instead of stalling startup in a single giant commit.
DRAIN_BLOCK = int(os.environ.get("CONTROL_AUDIT_DRAIN_BLOCK", str(1 << 20)))
# How long a line this process knows is owed may take to reach its file before the file
# is reported STALLED (``_check_growth``), per stream. Constants, not env vars: each
# bounds a REPORT, never a hold, so a wrong value is a late or a false line, not a
# widened grant.
#
# Egress lines are written as the proxy acts on an answer, so the grace is only slack
# for a slow disk and the drain interval. A tool line is written when the CALL ends,
# so its grace is the gateway's GATEWAY_CALL_TIMEOUT plus the same slack. That timeout
# bounds each socket read, not the whole call (urllib's `timeout=`, in
# ``discovery.post``), so a server that keeps trickling bytes can outrun this grace
# and be reported as a stall (#63). The 60 is the gateway's default
# (`tool-gateway/execute.py`), restated because that module is in another image;
# tests/test_topology.py holds this at or above the effective timeout, compose's
# included.
STALL_GRACE = 30.0
TOOL_STALL_GRACE = 60.0 + STALL_GRACE


# The audit columns each stream fills. Two streams describe different events, so
# neither should have to carry the other's columns as a row of NULLs it has to know to
# write — an egress decision has no tool identity, and an outcome has no host or port.
_EGRESS_COLUMNS = ("ts", "kind", "stage", "host", "port", "proto", "client",
                   "client_class", "method", "url", "reason")
_TOOL_COLUMNS = ("ts", "kind", "stage", "client", "client_class", "reason",
                 "server", "tool", "approval_id", "status",
                 "result_bytes", "result_sha256", "duration")


def _insert_for(columns: tuple[str, ...]) -> str:
    """The INSERT for one stream's columns, built from the tuple so the statement
    cannot disagree with the rows fed to it. The interpolated values are the column
    names above — module constants, never a request field — so this is not a query
    built from input."""
    return (f"INSERT INTO audit({', '.join(columns)}) "  # noqa: S608
            f"VALUES ({', '.join('?' * len(columns))})")


class _Stream:
    """One JSONL file this process drains, and the little it needs to know about it.

    ``row`` returns a DICT keyed by column name, not a positional tuple: a tuple agrees
    with its INSERT and with the stdout mirror only by hand-counted index, so a column
    added in the middle silently shifts every value after it. A dict cannot land a value
    in the wrong column."""

    def __init__(self, name, path, columns, row, describe, grace, owed, settle=None):
        self.name = name              # what an operator sees in a failure line
        self.path = path
        self.columns = columns
        self.insert = _insert_for(columns)
        self.row = row                # (line: bytes) -> dict | None
        self.describe = describe      # (row: dict) -> str, for the stdout mirror
        # (conn, row: dict) -> None, run on each mapped row before the insert. Apart
        # from ``row`` so a mapper stays a pure function of its line.
        self.settle = settle
        # Seconds an owed line may take before the file is reported stalled, and the
        # noun the owed lines are counted in in that report (``_check_growth``).
        self.grace = grace
        self.owed = owed


def _ingest_field(value: object) -> str | None:
    # Escaped before the cap, as ``store._audit`` does: json.loads turns an escaped
    # lone surrogate back into one, and a row SQLite cannot bind would stop the
    # stream at that line for good.
    if value is None:
        return None
    return store._escaped(str(value))[:store.DRAIN_MAX_FIELD]


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
    # proxy's LOCAL decisions, so no rule was consulted for them and the class is
    # descriptive rather than load-bearing. Deriving it anyway keeps the column
    # populated across the whole audit view, so an operator filtering by client class
    # does not silently lose exactly the alarming rows.
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
    history rather than merely report a result. The word it gets is `outcome`, not
    `observe` (why is at ``audit.KINDS``), and `status` is where its own vocabulary
    lives.

    ``approval_id`` is never read off the line. A call is named by a handle the
    control plane minted, its ask's RESUME TOKEN or, for a call a pin answered, its
    CALL TOKEN, and ``_settle_tool_row`` turns that into an id. An id the line carried
    would be the gateway crediting a card on its own word."""
    rec = _parsed(line)
    if rec is None or rec.get("stage") != "tool-result":
        return None
    if rec.get("status") not in TOOL_STATUSES:
        return None
    ts = _timestamp(rec)
    if ts is None:
        return None
    client = _ingest_field(rec.get("client"))
    # Neither token is an audit column: both are carried to ``_settle_tool_row``, which
    # takes them out before the insert.
    return {"ts": ts, "kind": "outcome", "stage": "tool-result",
            "client": client, "client_class": policy._client_class(client),
            "reason": _ingest_field(rec.get("reason")),
            "server": _ingest_field(rec.get("server")),
            "tool": _ingest_field(rec.get("tool")),
            "approval_id": None,
            "resume_token": _ingest_field(rec.get("resume_token")),
            "call_token": _ingest_field(rec.get("call_token")),
            "status": _ingest_field(rec.get("status")),
            **_result_facts(rec)}


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _result_facts(rec: dict) -> dict:
    """What the call returned, as the gateway measured it: its size, its hash, and
    how long the dial took (``outcomes.record``). Each is checked on its own, and one
    that is absent or malformed is NULL with the row kept: the outcome is the half an
    incident asks for first, and a bad fact is no reason to lose it."""
    size, digest, duration = rec.get("bytes"), rec.get("sha256"), rec.get("duration")
    # Bounded by SQLite's INTEGER, which would refuse the row, and so the stream.
    if type(size) is not int or not 0 <= size < 1 << 63:
        size = None
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        digest = None
    try:
        seconds = float(duration) if type(duration) in (int, float) else math.nan
    except OverflowError:                            # an int no float can hold
        seconds = math.nan
    return {"result_bytes": size, "result_sha256": digest,
            "duration": seconds if math.isfinite(seconds) and seconds >= 0 else None}


def _settle_tool_row(conn, row: dict) -> None:
    """Name the card the outcome's call ran under, so ``audit.approval_id`` joins the
    outcome to that card's other rows, and keep both tokens out of the store.

    A RESUME TOKEN names an ask: its id joins the hold, the click and the claim. A
    plain indexed read, NOT ``holds._get_tool_ask``: that runs the expiry pass, which
    would make every outcome line an expiry write.

    A CALL TOKEN names a call a pin answered (``store._mint_call_token``), and is
    SPENT here: its row is deleted on this connection, inside the drain's transaction,
    so a token credits at most one outcome and a drain that rolls back keeps it. The
    pin's card is credited only if the line names the call the token was minted for.

    A token the store did not mint, a line with both, and a line with neither leave
    the id NULL."""
    resume_token = row.pop("resume_token", None)
    call_token = row.pop("call_token", None)
    if resume_token is not None and call_token is None:
        found = conn.execute("SELECT id FROM tool_approvals WHERE resume_token=?",
                             (resume_token,)).fetchone()
        row["approval_id"] = found["id"] if found is not None else None
    elif call_token is not None and resume_token is None:
        found = conn.execute(
            "SELECT approval_id, server, tool, client FROM tool_call_tokens "
            "WHERE token=?", (call_token,)).fetchone()
        if found is None:
            return
        conn.execute("DELETE FROM tool_call_tokens WHERE token=?", (call_token,))
        # Guards against a BUGGY gateway, not a forged one: the gateway wrote the
        # request this row was minted from and the line it is compared with, so a
        # gateway that lies can make the two agree. What it cannot do is name a card:
        # the id comes from this row, which only a pin's answer writes.
        if (found["server"], found["tool"], found["client"]) == (
                row["server"], row["tool"], row["client"]):
            row["approval_id"] = found["approval_id"]


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

#: The two streams, by name for the handlers that owe them lines
#: (``api_authorize.authorize``, ``api_tool.tool_authorize`` and ``tool_claim``).
EGRESS_STREAM = _Stream("egress", EGRESS_AUDIT_LOG, _EGRESS_COLUMNS, _egress_row,
                        _describe_egress, STALL_GRACE, "answered request(s)")
TOOL_STREAM = _Stream("tool outcomes", TOOL_AUDIT_LOG, _TOOL_COLUMNS, _tool_row,
                      _describe_tool, TOOL_STALL_GRACE,
                      "allowed or claimed tool call(s)", _settle_tool_row)

#: Every stream this process drains. Adding one is this tuple plus a mapper; nothing
#: below knows how many there are.
STREAMS = (EGRESS_STREAM, TOOL_STREAM)


def _audit_log_files(base: str) -> list[tuple[str, os.stat_result]]:
    """A stream's audit files, OLDEST CONTENT FIRST: the size-rotated siblings
    (``audit.jsonl.N``; higher N is older) followed by the active file last.

    This only fixes the order to drain in; ``_drain`` follows a file by inode. A
    sibling missing because a rotation raced this scan is skipped and reappears next
    pass."""
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

    Both writers ROTATE by size (a RotatingFileHandler in proxies/egress/addon.py and
    in tool-gateway/outcomes.py): at a cap the active file is renamed aside, a fresh
    one takes its place, and the oldest backup is dropped. So "the log" is the active
    file plus a few rotated siblings, and ingest must drain them OLDEST-FIRST —
    otherwise the rename would strand the un-ingested tail of a file in a sibling this
    loop never reads, silently dropping records, which an audit trail must never do.

    Position is tracked by INODE, not name: a rotation shuffles the .N suffixes but
    never a file's inode. A rotated file never grows again, so once its end is reached
    we step to the next-oldest at offset 0; only the active file is ever appended to.
    Reads only up to the LAST NEWLINE, so a line the writer is mid-append on is left
    for the next pass. Rows and the cursor advance in ONE transaction (see the module
    docstring) — do not split them."""
    files = _audit_log_files(stream.path)
    if not files:
        # No file at all — the writer may not have started, or the volume is absent.
        # Raise (not swallow): _drain_stream reports it once on the transition, so
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
            # No newline yet and the ACTIVE file ends here: the writer is mid-append.
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
            if stream.settle is not None:
                for r in rows:
                    stream.settle(conn, r)
            if rows:
                conn.executemany(
                    stream.insert,
                    [tuple(r.get(c) for c in stream.columns) for r in rows])
                # Mirror to stdout like store._audit does, so `make logs-cp` stays a live
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


# ── what goes wrong with ingest, and how it is reported ──────────────────────────────
#
# Four things can stop part of the trail filling: a stream that will not drain, a file
# that stops growing, the watch for that failing, and the loop itself dying. Each is
# reported on its TRANSITIONS, once on the way in and once on the way out, never every
# DRAIN_INTERVAL in between, and each transition is a row of the `ingest` kind
# (``_report``): it marks a gap in the trail, in the trail itself, where an operator
# reading the record will meet it. The state below is what makes a transition one.

#: The stage each report is written under, by what failed. Literals in a dict rather
#: than built from the stream's name, so the stage guard in
#: tests/test_control_plane_ui_js.py can read every one, and the page names what failed
#: from the stage (``auditTarget`` in control-plane-ui/audit.js).
_INGEST_STAGES = {
    "egress": {"stage": "ingest-egress"},
    "tool outcomes": {"stage": "ingest-tool"},
    "loop": {"stage": "ingest-loop"},
}


def _report(about: str, reason: str) -> None:
    """One ingest transition, as an `ingest` audit row whose reason says what changed;
    ``store._audit`` mirrors it to stdout. ``about`` is a stream's name, or "loop".

    Blocking, so never called on the event loop: a locked store holds a write for up to
    its busy timeout, and the loop serves all three listeners. Never raises: the store
    this reports on may be the thing failing, and then the plain line on stdout is all
    that is left (the recovery row's "since" marks the gap afterwards)."""
    try:
        store._audit("ingest", **_INGEST_STAGES[about], reason=reason)
    except Exception:  # noqa: BLE001 — the report must never take the loop down
        print(f"control-plane: {reason}", flush=True)


def _when(since: float) -> str:
    """A wall-clock time for a recovery row, which may be read long after the opening
    row, or with that row missing."""
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(since))


# Since when each stream's drain has been failing, by stream; absent while it drains. A
# missing file is the NORMAL state before its writer first writes, and a permanently
# silent ingest is exactly the failure this whole mechanism exists to remove.
#
# PER STREAM, not one flag for all of them: a broken egress ingest and a broken tool
# ingest lose different evidence, and one failing must not mute the other's recovery
# line or make its own look already-reported.
_drain_failing: dict[str, float] = {}

#: What is lost while a stream is not draining, named per stream because the operator's
#: next move differs. Vague here would be worse than silent: this line is the only
#: warning that part of the trail has stopped filling.
_STREAM_STAKES = {
    "egress": "locally-decided egress will not appear in /api/audit",
    "tool outcomes": "how tool calls ENDED will not appear in /api/audit — approved "
                     "calls will show as granted with no record of what came of them",
}


# A file that stops GROWING while its writer is still deciding things. The drain
# cannot see that: a writer whose volume is full keeps serving, its file just stays
# the same size, and "nothing new" is also what a quiet writer looks like. What tells
# them apart is a line this process KNOWS is owed: every /authorize answer is one the
# proxy records (``api_authorize.authorize``), and every allowed /tool/authorize and
# granted claim is a call whose end the gateway records (``api_tool``). So a file whose
# inode and size have not moved for its stream's ``grace`` since a line became owed is
# reported STALLED.
#
# A writer failing only SOME of its writes still grows the file and is not caught.
#
# Known false positives, each cleared by the writer's next line: an allow the gateway
# never received owes a line it will never write. That happens when this handler
# answers after the gateway's DECIDE_TIMEOUT, which the gateway refuses without an
# outcome line (``execute.call``), or when the answer is lost on the way back.
_owed: dict[str, int] = {}
_owed_lock = threading.Lock()


def _expect_line(stream_name: str) -> None:
    """Record that the writer of ``stream_name`` owes one more line. Called from the
    request threads, hence the lock."""
    with _owed_lock:
        _owed[stream_name] = _owed.get(stream_name, 0) + 1


class _Growth:
    """What a stream's active file looked like when it last grew, how many lines were
    owed then, and since when more have been owed without it growing."""

    def __init__(self, sig, owed):
        self.sig = sig                # (inode, size) of the active file
        self.owed = owed
        self.owed_since = None        # monotonic time a newer owed line was first seen


_growth: dict[str, _Growth] = {}
# Since when (wall clock) each stream has been reported stalled; absent while it grows.
_stalled: dict[str, float] = {}
# Since when ``_check_growth`` has been raising for each stream; absent while it runs.
_watch_failing: dict[str, float] = {}


def _check_growth(stream: _Stream, now: float) -> None:
    # Zero before the first owed line, so the baseline is taken before anything is owed
    # and one is enough. A stream nothing expects lines for stays at zero and is never
    # reported.
    owed = _owed.get(stream.name, 0)
    try:
        st = os.stat(stream.path)
    except OSError:
        return                         # a missing file is _drain_stream's to report
    sig = (st.st_ino, st.st_size)
    last = _growth.get(stream.name)
    if last is None or sig != last.sig:
        since = _stalled.pop(stream.name, None)
        if since is not None:
            _report(stream.name,
                    f"{stream.name} audit file growing again ({stream.path}), stalled "
                    f"since {_when(since)} — lines written while it was stalled may "
                    f"be missing from /api/audit")
        _growth[stream.name] = _Growth(sig, owed)
        return
    if owed == last.owed:
        return
    if last.owed_since is None:
        last.owed_since = now
        return
    if now - last.owed_since >= stream.grace and stream.name not in _stalled:
        _stalled[stream.name] = time.time()
        _report(stream.name,
                f"{stream.name} audit file STALLED ({stream.path}): "
                f"{owed - last.owed} {stream.owed} and no new line in "
                f"{now - last.owed_since:.0f}s — its writer's volume may be full or "
                f"read-only; lines written during the stall may never reach "
                f"/api/audit, and its writer's stdout has every one")


def _watch_growth(stream: _Stream, now: float) -> None:
    """``_check_growth``, with its own failure reported once in and once out.

    In its own words, not the drain's FAILING: rows may be arriving the whole time, so
    the drain's stakes would be false, and what is actually lost is the stall report."""
    try:
        _check_growth(stream, now)
    except Exception as e:  # noqa: BLE001 — a dead watch must never be silent
        if stream.name not in _watch_failing:
            _watch_failing[stream.name] = time.time()
            _report(stream.name,
                    f"stall watch FAILING for {stream.name} ({stream.path}): {e!r} — "
                    f"a writer that stops would go unreported until this clears")
        return
    since = _watch_failing.pop(stream.name, None)
    if since is not None:
        _report(stream.name, f"stall watch for {stream.name} recovered ({stream.path}), "
                             f"failing since {_when(since)}")


async def _audit_drain_loop(supervisor: _DrainSupervisor | None = None) -> None:
    """Drain and watch every stream, every DRAIN_INTERVAL. Each step is guarded on its
    own, so one stream failing, or its watch, stops neither the other stream nor the
    other step. ``supervisor``, when there is one, is told of each completed pass."""
    while True:
        for stream in STREAMS:
            await _drain_stream(stream)
            # Every pass, whatever the drain did: a stream that will not drain can
            # still be one whose writer has stopped.
            await asyncio.to_thread(_watch_growth, stream, time.monotonic())
        if supervisor is not None:
            await supervisor.passed()
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
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — a dead loop must never be silent
        if stream.name not in _drain_failing:
            _drain_failing[stream.name] = time.time()
            await asyncio.to_thread(
                _report, stream.name,
                f"{stream.name} ingest FAILING ({stream.path}): {e!r} — "
                f"{_STREAM_STAKES[stream.name]} until this clears")
        return
    since = _drain_failing.pop(stream.name, None)
    if since is not None:
        await asyncio.to_thread(_report, stream.name,
                                f"{stream.name} ingest recovered ({stream.path}), "
                                f"failing since {_when(since)}")


class _DrainSupervisor:
    """Owns the drain loop's task, and restarts the loop when it dies.

    Every step in the loop is guarded, so nothing realistic kills it; this is for the
    bug nobody has found. RESTART, never exit: compose runs this service under
    `restart: always`, so a deterministic bug that exited would crash-loop the
    governance authority and fail every sandbox closed, while a stopped drain loses
    nothing until the outage outlasts the writers' rotated backups.

    The task is held here because asyncio keeps only a weak reference to it, and an
    unreferenced task can be collected mid-flight, stopping ingest with no error. The
    pending restart is held for the same reason, and so ``stop`` can cancel it."""

    def __init__(self):
        self.stopped = False
        self.task: asyncio.Task | None = None
        self.restart: asyncio.Task | None = None
        self.down_since: float | None = None   # wall clock of the first death
        self.deaths = 0                         # since the loop last completed a pass

    def start(self) -> None:
        self.task = asyncio.create_task(_audit_drain_loop(self))
        self.task.add_done_callback(self._ended)

    def stop(self) -> None:
        self.stopped = True
        for task in (self.task, self.restart):
            if task is not None:
                task.cancel()

    def _ended(self, task: asyncio.Task) -> None:
        # A done-callback, run on the event loop: no I/O here.
        if task.cancelled():
            return
        # Read on EVERY path: an exception nobody retrieves is logged as "never
        # retrieved" when the task is collected. On the stopped path ``stop``'s cancel
        # has already silenced it; reading it anyway leans on nothing.
        error = task.exception()
        if self.stopped:
            return
        self.deaths += 1
        first = self.down_since is None
        if first:
            self.down_since = time.time()
        self.restart = asyncio.get_running_loop().create_task(
            self._restart(error, first))

    async def _restart(self, error: BaseException | None, first: bool) -> None:
        # Deaths while already down are counted, not reported: a deterministic bug
        # would otherwise write a row every DRAIN_INTERVAL.
        if first:
            await asyncio.to_thread(
                _report, "loop",
                f"ingest loop DIED: {error!r} — no stream is drained or watched until "
                f"it runs again; restarting it every {DRAIN_INTERVAL:g}s")
        # No `stopped` check: ``stop`` cancels this task, at whichever await it is.
        await asyncio.sleep(DRAIN_INTERVAL)
        self.start()

    async def passed(self) -> None:
        """The loop completed a pass: if it had died, say it is running again. Awaited
        in the loop's own task, so the report is written by the loop it is about."""
        if self.down_since is None:
            return
        since, deaths = self.down_since, self.deaths
        self.down_since, self.deaths = None, 0
        await asyncio.to_thread(_report, "loop",
                                f"ingest loop running again after {deaths} death(s), "
                                f"down since {_when(since)}")
