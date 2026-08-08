# SPDX-License-Identifier: Apache-2.0
"""
Control plane — governance authority for the governed data-plane proxies.

Step 2b: policy + audit + **hold-for-approval**. The management app the agent can
never reach — it lives on internal networks the sandbox is not attached to.
Governed proxies call it on the control path to authorize connections; unknown
requests are held for a human, who approves/rejects them in a live UI.

The authorize flow (one call from the proxy, `POST /authorize`):
  - host matches a BLOCK rule            -> deny   (audited)
  - host matches an ALLOW rule           -> allow  (audited)
  - no matching rule                     -> HOLD: record a pending approval and
    BLOCK the request until a human resolves it or CONTROL_HOLD_TIMEOUT elapses
    (-> default-deny). The proxy only ever sees allow/deny; the hold is internal.
    A request identical to one already held JOINS it rather than raising a second
    approval, so a retrying agent produces one card and one decision — which is
    also why one click can release several blocked requests (see _group_key).

A human resolves holds over the approvals API (the SSE stream at /approvals/stream
and POST /approvals/{id}/resolve), surfaced by the separate control-plane-ui
frontend; the backend serves no HTML itself. GET /approvals is the non-streaming
form of the same list and is still served here, but the UI does not use it and the
frontend no longer relays it — see _RELAY_ROUTES in control-plane-ui/app.py. Three read-only views
back the rest of that UI: GET /api/audit (recent decisions), GET /api/rules (the
standing policy — see ``api_rules`` for why that one has to be visible) and
GET /api/config (the hold window, so a card can show its countdown):
  - allow-once / deny-once     — decide just this request
  - allow-persist / deny-persist — also write a rule so future connections skip
    the hold (progressive trust; DESIGN.md "auto-approve progressively more"). WHICH
    rule is the operator's choice from a bounded set derived from the requested host
    (``_persist_candidates``), not a string the agent's request can supply.

Resolving a hold is the one privileged action in this system — it is what grants
egress — so every resolution records the PROVENANCE of whoever performed it
(``_actor``), both on the durable approvals row (``resolved_by``) and in the audit
reason. That is detection, not prevention: the self-reported fields are forgeable
by a host-local caller. It exists so a forged approval is at least visible in the
record afterwards, which it previously was not — an operator's click and a
scripted POST were indistinguishable once written.

TWO LISTENERS, on two networks, because the dangerous surface is the management
API and not `/authorize`. Resolving a hold GRANTS egress, so anything that reaches
`resolve` can self-approve, while `/authorize` can only ever answer a policy
question. They are therefore served separately (``main``):

  - the AUTHORIZE listener (CONTROL_AUTHORIZE_PORT, on authorize-net) serves
    exactly POST /authorize and GET /healthz — ``authorize_app`` below. It is the
    only surface the egress proxy has a route to.
  - the MANAGEMENT listener (CONTROL_MANAGE_PORT, bound to the control-net address
    ALONE — a wildcard bind is refused at startup) serves everything else, and is
    reachable only from control-plane-ui.

This is blast-radius containment, not the primary control: the agent is kept off
this service by network topology, by the proxy's relay guard and by the proxy's
port gate. What the split adds is that all three failing at once yields a policy
QUERY rather than a self-approval. See DESIGN.md.

Concurrency model: run under a SINGLE uvicorn worker. `/authorize` and the
resolve endpoint are sync (FastAPI runs them in a threadpool); a held request
blocks its worker on a threading.Event that the resolve endpoint sets. Concurrent
holds are bounded (CONTROL_MAX_PENDING / CONTROL_MAX_PENDING_PER_CLIENT) so a
sandbox cannot pin every worker and stall governance for all sandboxes — over the
cap /authorize fails closed. The SQLite store is the source of truth for the UI
(the SSE stream polls it). Do NOT run multiple workers — the pending-event
registry is in-process. That constraint is also why the two listeners above are
two sockets in ONE process rather than two services: a held /authorize and the
`resolve` that releases it must share memory, precisely because they must not
share a socket.

No egress: this service sits on control-net and authorize-net, both internal. It
must never be given an internet route — it is pure management state (the crown
jewel).
"""
from __future__ import annotations

import asyncio
import glob
import ipaddress
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from typing import NamedTuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("CONTROL_DB", "/var/lib/control-plane/control.db")
SEED_PATH = os.environ.get(
    "CONTROL_SEED", "/etc/control-plane/egress-allowlist.txt")
# How long a held request waits for a human before defaulting to deny.
HOLD_TIMEOUT = float(os.environ.get("CONTROL_HOLD_TIMEOUT", "120"))
# Bound concurrent holds. The two caps look alike and protect DIFFERENT things, which
# is why they count different sets — before duplicate grouping they counted the same
# one, and the distinction was invisible:
#
#   MAX_PENDING counts WAITERS — blocked FastAPI threadpool workers. A held request
#   pins a worker until it is resolved or times out, so an unbounded number would
#   stall ALL /authorize decisions, and this control plane is shared across every
#   sandbox: one agent could starve governance for all. Keep it well under the
#   threadpool size (anyio default ~40) so fast allow/deny decisions always have
#   free workers.
#
#   MAX_PENDING_PER_CLIENT counts CARDS — pending approvals on the operator's screen,
#   for one client. It protects attention, not workers. Duplicate requests join an
#   existing card (see _reserve_hold) and so cost this cap nothing; they still cost a
#   worker, and are still bounded by MAX_PENDING. Set to 0 to disable.
#
# Over either cap /authorize fails CLOSED immediately (deny) instead of registering
# another blocking hold.
MAX_PENDING = int(os.environ.get("CONTROL_MAX_PENDING", "16"))
MAX_PENDING_PER_CLIENT = int(os.environ.get("CONTROL_MAX_PENDING_PER_CLIENT", "4"))

# ── Egress audit ingest ─────────────────────────────────────────────────────
# The proxy's audit file, mounted READ-ONLY from the shared named volume. Not every
# egress decision is made by /authorize — the relay guard, the port gate, the SNI
# anti-fronting check and the permanent-lifeline allow are all decided locally in the
# proxy, on purpose, and a control-plane outage produces local fail-closed denials by
# definition. Those never reached this store, so the UI's "recent decisions" was a
# record of round-trips rather than of decisions, and a domain-fronting refusal — the
# single most alarming thing the proxy can emit — was visible only in `make logs-ep`.
#
# We PULL rather than have the proxy push, and that choice buys the property that
# matters: the cursor lives in this same SQLite, so ingesting rows and advancing the
# cursor are ONE transaction. A crash mid-drain rolls back both, which makes the
# ingest exactly-once with no idempotency key, no UNIQUE index and no dedup pass —
# the tax any at-least-once push (broker or POST) would have imposed. It also
# self-heals across an outage of THIS service, since the file is durable and the
# cursor simply resumes, and it leaves the security-critical proxy image untouched:
# no new dependency, no fire-and-forget task in a hot path.
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
# Cap on any single ingested string. These fields are agent-INFLUENCED (a host or URL
# the sandbox asked for), and this is a trust boundary: the proxy writes them faithfully,
# including a megabyte-long URL if the agent sent one. Truncating here keeps one request
# from bloating the store or the glanceable UI list.
DRAIN_MAX_FIELD = 2048

# How many recent audit rows /api/audit folds into its grouped view (see api_audit).
# Bounds the aggregation regardless of table size; the slice itself rides audit_ts.
AUDIT_GROUP_SCAN = int(os.environ.get("CONTROL_AUDIT_GROUP_SCAN", "5000"))

# The reason the egress proxy writes when it denies because it could not reach THIS
# service (proxies/egress/addon.py, ``_authorize``). Those denials are correct — that
# is fail-closed working — but they are not policy, and until they were told apart an
# operator could not distinguish "your rules refused this" from "governance is down
# and everything is being refused". Both render as a red `deny` row against a host.
#
# Matched as a PREFIX because the proxy appends the underlying exception. Matched at
# all, rather than shared as a constant, because these are separate services in
# separate images with no common module — so a test asserts the two strings still
# agree (tests/test_control_plane_api.py). If they ever drift, the classification
# silently returns to what it was before this existed: an ordinary deny. That is the
# safe direction, and it is why matching is acceptable here at all.
FAIL_CLOSED_REASON = "control-plane unreachable"

# ── the two listeners (see the module docstring) ────────────────────────────
# The proxy-facing surface binds a WILDCARD on purpose: it is the safe one, it
# only answers policy questions, and the container's healthcheck reaches it over
# loopback. The management surface binds ONE address, and the default is loopback
# so that a deployment which forgets to set it fails VISIBLY (the UI cannot reach
# the backend) instead of silently re-exposing `resolve` to the proxy's network.
AUTHORIZE_BIND = os.environ.get("CONTROL_AUTHORIZE_BIND", "0.0.0.0")  # noqa: S104
AUTHORIZE_PORT = int(os.environ.get("CONTROL_AUTHORIZE_PORT", "8091"))
MANAGE_BIND = os.environ.get("CONTROL_MANAGE_BIND", "127.0.0.1")
MANAGE_PORT = int(os.environ.get("CONTROL_MANAGE_PORT", "8090"))

# Everything except /authorize: the approvals API, the read-only views, /status.
app = FastAPI(title="dockade control plane", version="2b")
# POST /authorize and GET /healthz, and nothing else, ever. Adding a route here
# hands it to the egress proxy — the one component whose compromise this split
# exists to survive — so the question to ask of any new endpoint is not "is it
# read-only" but "would I let a bypassed relay guard call it".
authorize_app = FastAPI(title="dockade control plane (authorize)", version="2b")

# In-memory registry of held requests, keyed by approval id. Single-process only
# (see module docstring). The Event only WAKES the blocked /authorize worker; the
# human's decision is read back from the durable approvals row (the single source
# of truth), so no in-memory outcome is kept. SQLite holds the durable state.
_LOCK = threading.Lock()
_PENDING_EVENTS: dict[str, threading.Event] = {}
# approval_id -> client, so the per-client CARD cap can be counted under _LOCK.
# One entry per card, so counting entries for a client counts that client's cards.
_PENDING_CLIENT: dict[str, str | None] = {}
# approval_id -> how many /authorize workers are blocked on that card. Duplicates
# share a card, so this is 1 for a lone request and N for a retry storm; the global
# cap sums it, because a joined waiter still pins a worker.
_PENDING_WAITERS: dict[str, int] = {}
# Duplicate grouping: group key (see _group_key) -> the approval id blocked requests
# with that key attach to. Present only while that card can still be JOINED — resolve
# and expiry remove it, so a request arriving after a decision opens a fresh card
# instead of silently inheriting an outcome nobody was shown it alongside.
_GROUPS: dict[tuple, str] = {}
# approval_id -> the wall-clock instant this card default-denies. Set ONCE, when the
# card is created, and inherited by every request that joins it: a joiner waits out the
# REMAINDER of the original window rather than starting a fresh one. Otherwise an agent
# retrying on a short loop would push the deadline out forever and the card's countdown
# would be a lie. A request that joins with seconds left gets a fast default-deny, and
# its next retry — arriving after _close_group — opens a new card with a full window.
_PENDING_DEADLINE: dict[str, float] = {}

# Over-cap rejections, for the UI's saturation banner. Kept IN MEMORY, which is a
# deliberate weakening of nothing: the deny itself is written to the audit table
# with its reason (see ``authorize``), so this is a display index over a durable
# record, not the record. Losing it on restart costs a banner, not evidence.
#
# What it exists to fix is that saturation is INVISIBLE and TRANSIENT. Over the cap
# /authorize fails closed without creating an approval row, so no card is ever
# raised: the agent is refused, and an operator watching the queue sees the same
# empty list as when nothing is happening. A live gauge would not help either —
# holds drain in seconds, so by the time anyone looks the count is healthy again.
# The lasting record of the EVENT is what makes it noticeable, and the gauge is
# context beside it.
#
# ``since`` is the process start, and it is load-bearing for honesty: "3 denied
# unheard" means three since this timestamp, never three ever.
#
# ``acked`` is a HIGH-WATER MARK rather than a reset-to-zero, and that choice is what
# makes dismissal race-free: acknowledging "the 2 I have read" leaves a third that
# arrived while the click was in flight still unread, whereas zeroing the counter would
# swallow it. Rejections arrive in bursts, which is precisely when that gap is open.
_STARTED_TS = time.time()
_SATURATION: dict[str, object] = {
    "count": 0, "last_ts": None, "last_scope": None, "last_host": None,
    "acked": 0, "acked_ts": None}


# ── storage ─────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                id         INTEGER PRIMARY KEY,
                pattern    TEXT NOT NULL UNIQUE,   -- host or .suffix (see _match)
                action     TEXT NOT NULL,          -- 'allow' | 'block'
                source     TEXT NOT NULL,          -- 'seed' | 'operator'
                created_at REAL NOT NULL
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit (
                id       INTEGER PRIMARY KEY,
                ts       REAL NOT NULL,
                decision TEXT NOT NULL,       -- allow | deny | hold
                stage    TEXT,
                host     TEXT,
                port     INTEGER,
                proto    TEXT,
                client   TEXT,
                method   TEXT,
                url      TEXT,
                reason   TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)")
        # This table grows without bound — every decision is kept, and only
        # /api/audit's VIEW is windowed (see api_audit), never the record itself.
        # That is deliberate: the trail is the artifact. There is no automatic
        # rotation; an operator thins it on their own schedule with `make
        # audit-prune`, which deletes rows past a retention window and VACUUMs to
        # return the disk. The `audit_ts` index above is what keeps that DELETE cheap
        # (and `make destroy` remains the separate, whole-store reset).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS approvals (
                id          TEXT PRIMARY KEY,
                ts          REAL NOT NULL,
                host        TEXT NOT NULL,
                port        INTEGER,
                proto       TEXT,
                client      TEXT,
                method      TEXT,
                url         TEXT,
                status      TEXT NOT NULL,    -- pending | allowed | denied | expired
                mode        TEXT,             -- once | persist
                resolved_at REAL,
                resolved_by TEXT              -- provenance of the resolver (_actor)
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status)")
        # Ingest cursor for the egress proxy's audit file (see _drain_egress_audit).
        # A NEW TABLE, deliberately — not a column on an existing one — so it needs
        # no migration on the long-lived store (read the note below this function).
        # `inode` is what distinguishes a rotated/replaced file from an appended one;
        # without it a fresh file inherits the old offset and its first N bytes are
        # never ingested.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_cursor (
                path   TEXT PRIMARY KEY,
                inode  INTEGER NOT NULL,
                offset INTEGER NOT NULL
            )""")
        conn.commit()


# NOTE for whoever adds the next column: there is NO migration step here, and that
# is only safe because of a one-time circumstance. `CREATE TABLE IF NOT EXISTS` is a
# NO-OP on an existing table — it silently does not add columns — and this store is a
# long-lived named volume that deliberately outlives container and image churn, so a
# new column is MISSING on any store created before it and every statement naming it
# then fails at runtime. `resolved_by` got away without one because the single store
# that predated it was migrated in place (ALTER TABLE ADD COLUMN) before this was
# removed, and every store created since gets the column from the DDL above. So a
# future additive column needs its own explicit ALTER for existing volumes; the only
# alternative is `make destroy`, which discards the policy rules and the audit
# history. (Restoring a pre-`resolved_by` backup of the volume would likewise need
# that ALTER run by hand.)


def _seed_if_empty() -> int:
    """Load the seed file into an empty rules table. Idempotent: once any rule
    exists the store is authoritative and the file is never re-read."""
    with _connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] > 0:
            return 0
        try:
            with open(SEED_PATH) as f:
                patterns = [ln.strip().lower() for ln in f
                            if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError:
            return 0
        now = time.time()
        conn.executemany(
            "INSERT OR IGNORE INTO rules(pattern, action, source, created_at) "
            "VALUES (?, 'allow', 'seed', ?)",
            [(p, now) for p in patterns])
        conn.commit()
        return len(patterns)


def _match(host: str, pattern: str) -> bool:
    """Leading dot matches subdomains; bare entry is an exact host match."""
    if pattern.startswith("."):
        return host == pattern[1:] or host.endswith(pattern)
    return host == pattern


def _pattern_scope(pattern: str) -> str:
    """How broadly a stored pattern matches, in words, for the rules view.

    Derived HERE, beside the ``_match`` that implements it, so the UI cannot drift
    from the real semantics. It matters because a leading dot is a SUBDOMAIN WILDCARD
    while looking like an ordinary hostname: a rule persisted for ``.example.com``
    also grants every subdomain, and nothing in the approval flow says so (the
    pattern comes verbatim from the requested host — see the rule-management item in
    DESIGN.md). Naming the scope is the cheap half of that fix."""
    return "host + subdomains" if pattern.startswith(".") else "exact host"


# A wildcard must keep at least this many labels. One label is either a public suffix
# or a bare name, and a standing allow rule for `.com` would end governance for that
# entire TLD in a single click.
_WILDCARD_MIN_LABELS = 2


def _persist_candidates(host: str) -> list[str]:
    """The patterns an operator may persist for a held host, NARROWEST FIRST.

    Exists because ``resolve`` used to store the requested host verbatim. Two facts
    made that sharper than it looks: a leading dot is a subdomain wildcard (``_match``),
    and the host on an approval is chosen by the AGENT — so a request for
    ``.example.com`` persisted a rule covering every subdomain of example.com, and
    nothing in this system revokes a rule. Deriving the candidate set here, in the
    module that defines matching, makes exact-vs-wildcard an *operator choice from a
    bounded set* instead of a string the requester supplies.

    Three at most, in increasing order of breadth — so the first is both the safest and
    the default:

      - the exact host;
      - ``.host`` — that host and its subdomains;
      - ``.<last two labels>`` — the registrable domain, which is what an operator
        usually wants when one service spreads over many hostnames.

    Anything broader is deliberately NOT offered: it needs direct policy editing, which
    is a decision rather than a click.

    Known limitation, stated rather than hidden: with no public-suffix list the
    two-label suffix of ``example.co.uk`` is ``.co.uk``, which grants far more than it
    appears to. That is why the UI shows the chosen pattern VERBATIM in a confirm step
    instead of describing it, and why a human picks."""
    exact = (host or "").strip().lower().strip(".")
    if not exact:
        return []
    out = [exact]
    try:
        ipaddress.ip_address(exact.strip("[]"))
    except ValueError:
        pass
    else:
        return out          # an IP literal has no subdomains to wildcard over
    labels = exact.split(".")
    if not all(labels):
        return out          # malformed (`a..b`): offer the exact string, invent nothing
    for depth in (len(labels), _WILDCARD_MIN_LABELS):
        # `depth > len(labels)` is the single-label case (`localhost`), where the
        # two-label suffix does not exist and taking it anyway would manufacture
        # `.localhost` — precisely the one-label wildcard the floor exists to forbid.
        if depth < _WILDCARD_MIN_LABELS or depth > len(labels):
            continue
        pattern = "." + ".".join(labels[len(labels) - depth:])
        if pattern not in out:
            out.append(pattern)
    return out


def _decide(host: str) -> tuple[str, str]:
    """(decision, reason). Block wins over allow; an unmatched host is HELD for
    human approval (2b) rather than denied outright."""
    # Strip a trailing FQDN dot, matching the proxy's relay guard: `evil.com.` and
    # `evil.com` are the same destination, so without this an explicit operator BLOCK
    # of `evil.com` misses `evil.com.` — it lands in a hold and can be re-prompted
    # indefinitely. (Stored patterns are already dot-normalized on the persist path.)
    host = (host or "").lower().rstrip(".")
    with _connect() as conn:
        rows = conn.execute("SELECT pattern, action FROM rules").fetchall()
    for r in rows:
        if r["action"] == "block" and _match(host, r["pattern"]):
            return "deny", f"blocked by rule ({r['pattern']})"
    for r in rows:
        if r["action"] == "allow" and _match(host, r["pattern"]):
            return "allow", f"allowed by rule ({r['pattern']})"
    return "hold", "no matching rule — held for approval"


def _audit(decision: str, **fields) -> None:
    # Agent-INFLUENCED fields (host/url/... arrive on /authorize from the proxy, which
    # relays whatever the sandbox asked for) are truncated on write — the same
    # trust-boundary cap the ingest path applies (DRAIN_MAX_FIELD). Without it a
    # megabyte-long URL on a single request would bloat the crown-jewel store and the
    # glanceable /api/audit list. Non-string fields (port) and the server-set decision
    # pass through untouched.
    def cap(v):
        return v[:DRAIN_MAX_FIELD] if isinstance(v, str) else v
    with _connect() as conn:
        conn.execute(
            "INSERT INTO audit(ts, decision, stage, host, port, proto, client, "
            "method, url, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), decision, cap(fields.get("stage")), cap(fields.get("host")),
             fields.get("port"), cap(fields.get("proto")), cap(fields.get("client")),
             cap(fields.get("method")), cap(fields.get("url")), cap(fields.get("reason"))))
        conn.commit()
    # Mirror every decision to stdout so `docker compose logs -f control-plane`
    # (make logs-cp) is a live decision feed — the same role the egress proxy's
    # stdout audit plays. The SQLite table above stays the durable, queryable
    # record (served at /api/audit); this line is for live viewing only. Compact
    # and greppable: one line, empty fields omitted, reason after a ' :: '.
    shown = " ".join(
        f"{k}={fields[k]}" for k in
        ("stage", "host", "port", "proto", "client", "method", "url")
        if fields.get(k) is not None)
    reason = fields.get("reason")
    print(f"AUDIT {decision} {shown}" + (f" :: {reason}" if reason else ""),
          flush=True)


# ── egress audit ingest ─────────────────────────────────────────────────────

def _ingest_field(value: object) -> str | None:
    if value is None:
        return None
    return str(value)[:DRAIN_MAX_FIELD]


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
    return (float(ts), _ingest_field(rec.get("decision")),
            _ingest_field(rec.get("stage")), _ingest_field(rec.get("host")),
            port, _ingest_field(rec.get("proto")), _ingest_field(rec.get("client")),
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
    the next pass. Rows and the cursor advance in ONE transaction (see EGRESS_AUDIT_LOG)
    — do not split them."""
    files = _audit_log_files()
    if not files:
        # No file at all — the proxy may not have started, or the volume is absent.
        # Raise (not swallow): _audit_drain_loop reports it once on the transition, so
        # "no ingest at all" can never become a silent steady state.
        os.stat(EGRESS_AUDIT_LOG)
        return 0

    with _connect() as conn:
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
                conn.executemany(
                    "INSERT INTO audit(ts, decision, stage, host, port, proto, "
                    "client, method, url, reason) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                # Mirror to stdout like _audit does, so `make logs-cp` stays a live
                # feed of DECISIONS and not merely of this service's own round-trips.
                # Marked `ingested` because it is: a decision the proxy made, arriving
                # late and out of order relative to the lines around it.
                for r in rows:
                    print(f"AUDIT {r[1]} (ingested) stage={r[2]} host={r[3]} "
                          f"client={r[6]} :: {r[9]}", flush=True)
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


def _group_key(client: str | None, host: str | None,
               port: int | None, proto: str | None) -> tuple:
    """What makes two held requests THE SAME REQUEST for grouping purposes.

    ``client`` is in the key even though the decision is not a function of it
    (``_decide`` reads the host alone): a card names one client, and approving one
    sandbox's request must not silently release another's.

    ``method`` and ``url`` are deliberately OUT. They are precisely what varies across
    the retries this exists to collapse — a different query string or cache-buster
    each time — so keying on them would defeat grouping in the one case that motivates
    it. Nothing is lost from the record: every joined request writes its own audit
    line carrying its own method and url (see ``authorize``); only the CARD shows one
    representative."""
    return ((client or None), (host or "").lower(),
            port, (proto or "").lower() or None)


class HoldSlot(NamedTuple):
    """Outcome of asking for a hold slot. Exactly one of ``refused`` / ``approval_id``
    is set: refused means nothing was reserved and the caller must fail closed."""
    approval_id: str | None
    event: threading.Event | None
    joined: bool          # True -> attached to an existing card; write no new row
    refused: str | None   # deny reason, over one of the caps
    deadline: float       # when to stop waiting; the CARD's, not this request's


def _reserve_hold(approval_id: str, event: threading.Event,
                  client: str | None, host: str | None = None,
                  port: int | None = None, proto: str | None = None) -> HoldSlot:
    """Atomically check the hold caps and either reserve a slot for a NEW card, attach
    this request to an existing card for the same key, or refuse.

    Extracted from ``authorize`` so the cap logic is unit-testable without the
    FastAPI handler (see DESIGN.md). The whole check+reserve runs under ``_LOCK``
    so concurrent holds cannot race past the cap — nor race into creating two cards
    for one key, which is the same problem wearing a different hat.

    Order matters. The global waiter cap is checked FIRST, before the join, because a
    joined request still blocks a worker: grouping must never be a way around the cap
    that protects governance for every other sandbox. The per-client cap is checked
    only on the new-card path, because that cap counts cards.

    A rejection is also recorded in ``_SATURATION`` here rather than by the caller,
    so the one place that decides "over the cap" is the one place that reports it —
    the alternative leaves a second call site free to fail closed silently."""
    key = _group_key(client, host, port, proto)
    with _LOCK:
        def refuse(scope: str) -> HoldSlot:
            _SATURATION["count"] = int(_SATURATION["count"]) + 1  # type: ignore[arg-type]
            _SATURATION["last_ts"] = time.time()
            _SATURATION["last_scope"] = scope
            _SATURATION["last_host"] = host
            return HoldSlot(None, None, False,
                            f"hold capacity exceeded ({scope}) — fail-closed", 0.0)

        if sum(_PENDING_WAITERS.values()) >= MAX_PENDING:
            return refuse("global")

        joined_id = _GROUPS.get(key)
        joined_event = _PENDING_EVENTS.get(joined_id) if joined_id else None
        if joined_id and joined_event is not None:
            _PENDING_WAITERS[joined_id] = _PENDING_WAITERS.get(joined_id, 0) + 1
            return HoldSlot(joined_id, joined_event, True, None,
                            _PENDING_DEADLINE.get(joined_id, 0.0))

        if (client is not None and MAX_PENDING_PER_CLIENT > 0
                and sum(1 for c in _PENDING_CLIENT.values() if c == client)
                >= MAX_PENDING_PER_CLIENT):
            return refuse(f"client {client}")

        deadline = time.time() + HOLD_TIMEOUT
        _PENDING_EVENTS[approval_id] = event
        _PENDING_CLIENT[approval_id] = client
        _PENDING_WAITERS[approval_id] = 1
        _PENDING_DEADLINE[approval_id] = deadline
        _GROUPS[key] = approval_id
        return HoldSlot(approval_id, event, False, None, deadline)


def _close_group_locked(approval_id: str) -> None:
    """Stop new requests JOINING this card, without disturbing the waiters already on
    it. Caller must hold ``_LOCK`` — ``resolve`` calls this inside the same critical
    section that wakes the waiters, so there is no instant in which the card is decided
    and still joinable.

    Separate from ``_release_hold`` because the two happen at different times: the card
    stops being joinable when it is DECIDED, and its slots free as each blocked worker
    wakes and returns. Collapsing them would leave a decided card joinable for as long
    as the slowest waiter took to notice."""
    for key, held in list(_GROUPS.items()):
        if held == approval_id:
            del _GROUPS[key]


def _close_group(approval_id: str) -> None:
    """``_close_group_locked`` for callers that do not already hold ``_LOCK``."""
    with _LOCK:
        _close_group_locked(approval_id)


def _saturation() -> dict:
    """Hold-cap pressure, for the UI banner.

    ``in_flight`` is BLOCKED WAITERS — the set the global cap is actually measured
    against — and NOT the pending approvals list, which is a different set twice over.
    It diverges after a restart, when the table can carry ``pending`` rows with no live
    hold behind them; and it diverges whenever duplicates are grouped, since one card
    can hold several waiters. A count derived from the visible cards would therefore be
    confidently wrong in the one situation this exists to report. ``cards`` is that
    other number, reported beside it rather than instead of it — "12/16 in flight"
    reads as an emergency next to three cards until you can see both.

    Every timestamp here is ABSOLUTE. An elapsed-seconds field would change on every
    tick, and the SSE stream emits on payload change — so it would defeat the
    change-detection, silence the heartbeat, and turn an idle stream into a 1 Hz
    firehose. The client does the arithmetic, as it already does for the countdown."""
    with _LOCK:
        return {
            "in_flight": sum(_PENDING_WAITERS.values()),
            "cards": len(_PENDING_EVENTS),
            "max_pending": MAX_PENDING,
            "rejections": _SATURATION["count"],
            "acknowledged": _SATURATION["acked"],
            "last_ts": _SATURATION["last_ts"],
            "last_scope": _SATURATION["last_scope"],
            "last_host": _SATURATION["last_host"],
            # The window the count covers, and it MOVES to the dismissal: after an
            # acknowledgement the banner reports what has happened since then, so the
            # number and the stamp beside it always describe the same span.
            "since": _SATURATION["acked_ts"] or _STARTED_TS,
        }


def _release_hold(approval_id: str) -> None:
    """Symmetric to ``_reserve_hold``: drop ONE waiter's slot. Under ``_LOCK`` so it is
    consistent with reservation. The human's decision is read from the durable
    approvals row by the waiter, not carried back through here.

    Per-waiter, not per-card: with duplicates grouped, N workers are blocked on one
    approval id and each releases its own slot as it wakes. The card itself is
    unregistered only when the last of them has gone — until then ``resolve`` must
    still find its event, and the global cap must still count the workers it is
    holding."""
    with _LOCK:
        remaining = _PENDING_WAITERS.get(approval_id, 0) - 1
        if remaining > 0:
            _PENDING_WAITERS[approval_id] = remaining
            return
        _PENDING_WAITERS.pop(approval_id, None)
        _PENDING_EVENTS.pop(approval_id, None)
        _PENDING_CLIENT.pop(approval_id, None)
        _PENDING_DEADLINE.pop(approval_id, None)
        _close_group_locked(approval_id)


# Header the control-plane-ui relay uses to assert the BROWSER's address. The relay
# strips any client-supplied copy before setting it (see control-plane-ui/app.py),
# so a caller cannot self-report this value — but it is only as trustworthy as the
# relay, which is why _actor labels it as asserted rather than observed.
ACTOR_HEADER = "x-dockade-actor"
# Bound each recorded self-reported header (User-Agent, Origin) so a hostile one
# cannot bloat the store.
_ACTOR_UA_MAX = 120


def _actor(request) -> str:
    """Compact provenance for whoever resolved an approval, for the durable record.

    The trust level differs per field, so the labels distinguish them:
      - ``peer``   — the socket address this process itself observed. Unforgeable by
        the caller, but for anything arriving through the UI it is the
        control-plane-ui container, so it identifies the RELAY, not the human.
      - ``via-ui`` — the browser address the relay asserts (``ACTOR_HEADER``).
      - ``origin`` / ``ua`` — self-reported by the client, therefore forgeable.
        Recorded anyway because they are usually what betrays a non-browser caller.

    DETECTION, not prevention. A process running on the host can forge every
    self-reported field, and origin/Host checks in the relay are browser-enforced so
    they do not constrain it either. Preventing host-local forgery needs a human
    presence gesture the host cannot replay (WebAuthn user-presence or an
    out-of-band confirm) — see DESIGN.md. Until then the goal is that a forged
    approval leaves a trace that reads differently from an operator's click."""
    if request is None:                      # hand-invoked / non-HTTP caller
        return "unrecorded (no request context)"
    client = getattr(request, "client", None)
    parts = [f"peer={getattr(client, 'host', None) or '?'}"]
    headers = getattr(request, "headers", None) or {}
    asserted = headers.get(ACTOR_HEADER)
    if asserted:
        parts.append(f"via-ui={asserted}")
    origin = headers.get("origin")
    if origin:
        parts.append(f"origin={origin[:_ACTOR_UA_MAX]}")
    ua = headers.get("user-agent")
    if ua:
        parts.append(f'ua="{ua[:_ACTOR_UA_MAX]}"')
    return " ".join(parts)


def _list_pending() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, host, port, proto, client, method, url FROM approvals "
            "WHERE status='pending' ORDER BY ts").fetchall()
        # Every standing rule, so each offered pattern can say whether one already
        # exists for it. Read once for the whole list rather than per candidate — the
        # table is the complete policy and bounded in practice (see ``api_rules``).
        rules = {r["pattern"]: r["action"]
                 for r in conn.execute("SELECT pattern, action FROM rules")}
    with _LOCK:
        waiters = dict(_PENDING_WAITERS)
    # ``persist_options`` travels WITH the approval so the UI offers exactly the
    # patterns ``resolve`` will accept. One definition of the candidate set, beside the
    # matcher it derives from — rather than a second implementation in JavaScript that
    # could drift into offering a pattern the backend then rejects.
    #
    # ``requests`` is how many blocked requests one click decides. It is on the card
    # because grouping changed what "allow once" MEANS — once per card is now several
    # requests — and an operator granting egress to three requests while believing it
    # is one is the exact class of surprise this system exists to prevent. Defaults to
    # 1, not 0: a row with no live waiter is a stale pending row from before a restart
    # (see ``_startup``), and "0 requests" would read as a card that decides nothing.
    # ``existing`` is the action of a standing rule already holding this pattern, or
    # None. Nothing can REPLACE a rule here, so persisting over one with the opposite
    # action is refused by ``resolve`` — this is what lets the confirm panel say so
    # before the click rather than after it. Both halves are needed: the rule can
    # appear between this render and the click, which is the only way the conflict
    # arises at all (see the conflict branch in ``resolve``).
    return [dict(r, requests=max(1, waiters.get(r["id"], 1)),
                 persist_options=[{"pattern": p, "scope": _pattern_scope(p),
                                   "existing": rules.get(p)}
                                  for p in _persist_candidates(r["host"])])
            for r in rows]


def _pending_payload() -> dict:
    """What the approvals view needs, in one object: the holds AND the hold-cap
    pressure beside them.

    Deliberately not a second endpoint. The SSE stream already re-serializes this on
    a 1 s tick and emits on change, so folding saturation in means a rejection reaches
    the banner within a second through the push the page is already listening to —
    no new route, no relay-allowlist entry, and no polling interval to lag behind the
    burst it is meant to report."""
    return {"holds": _list_pending(), "saturation": _saturation()}


# ── API models ──────────────────────────────────────────────────────────────

class AuthorizeRequest(BaseModel):
    host: str
    port: int | None = None
    proto: str | None = None
    client: str | None = None
    method: str | None = None
    url: str | None = None
    stage: str | None = None


class AuthorizeResponse(BaseModel):
    decision: str                  # allow | deny  (hold is resolved internally)
    reason: str


class ResolveRequest(BaseModel):
    action: str                    # allow_once | allow_persist | deny_once | deny_persist
    # Which pattern a `*_persist` action writes. Must be one of the approval's
    # ``_persist_candidates``; omitted means the narrowest of them (the exact host).
    # Ignored by the two `*_once` actions, which write no rule at all.
    pattern: str | None = None


class AckRequest(BaseModel):
    # How many over-cap rejections the operator has read. A count rather than a
    # "dismiss" flag, so a rejection arriving between the render and the click is
    # still unread afterwards. Clamped server-side — see ``api_saturation_ack``.
    count: int


# ── lifecycle ───────────────────────────────────────────────────────────────

def _bootstrap() -> None:
    """Prepare the store. Called by ``main`` BEFORE either listener binds, so no
    request — from the proxy or the UI — can observe an unseeded database. This
    used to be a Starlette startup handler, which worked only because there was a
    single app: with two, each has its own lifespan and the authorize listener
    would race the management one's seed."""
    _init_db()
    # A held request cannot survive a restart (its blocked connection is gone),
    # so any 'pending' rows from a previous process are stale — expire them.
    with _connect() as conn:
        conn.execute(
            "UPDATE approvals SET status='expired', resolved_at=? "
            "WHERE status='pending'", (time.time(),))
        conn.commit()
    seeded = _seed_if_empty()
    if seeded:
        print(f"control-plane: seeded {seeded} allow rules from {SEED_PATH}",
              flush=True)


def _assert_listeners_separated() -> None:
    """Fail closed on a configuration that undoes the split.

    The management API is only out of the proxy's reach because it binds ONE
    address, on a network the proxy is not attached to. A wildcard bind serves it
    on every interface — including authorize-net — which silently restores exactly
    the self-approval path the split removes, while every healthcheck and every
    page in the UI keeps working. Nothing downstream can detect that, so it is
    refused here (the same shape as the proxy's ``_assert_guard_configured``)."""
    if MANAGE_BIND in ("", "0.0.0.0", "::", "*"):  # noqa: S104
        raise SystemExit(
            f"control-plane: CONTROL_MANAGE_BIND={MANAGE_BIND!r} is a wildcard, "
            f"which would serve the management API (including "
            f"/approvals/{{id}}/resolve) on authorize-net, where the egress proxy "
            f"can reach it. Bind the control-net address instead. Refusing to "
            f"start (fail closed).")
    if MANAGE_BIND == AUTHORIZE_BIND and MANAGE_PORT == AUTHORIZE_PORT:
        raise SystemExit(
            "control-plane: the management and authorize listeners resolve to the "
            f"same socket ({MANAGE_BIND}:{MANAGE_PORT}). Refusing to start.")


@app.get("/healthz")
@authorize_app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# ── authorize (proxy-facing) ────────────────────────────────────────────────

@authorize_app.post("/authorize", response_model=AuthorizeResponse)
def authorize(req: AuthorizeRequest) -> AuthorizeResponse:
    decision, reason = _decide(req.host)

    if decision in ("allow", "deny"):
        # Every decision is audited — no governed path bypasses the log (CLAUDE.md).
        _audit(decision, stage=req.stage, host=req.host, port=req.port,
               proto=req.proto, client=req.client, method=req.method,
               url=req.url, reason=reason)
        return AuthorizeResponse(decision=decision, reason=reason)

    # HOLD (bounded): reserve a hold slot atomically with the cap check, so
    # concurrent holds can't race past the cap. Over the global or per-client cap,
    # fail CLOSED immediately rather than registering another worker-blocking hold.
    # A request identical to one already held JOINS it instead of raising a second
    # card (_group_key) — a retrying agent used to fill its whole card budget with
    # copies of one question.
    slot = _reserve_hold(uuid.uuid4().hex, threading.Event(), req.client,
                         req.host, req.port, req.proto)
    if slot.refused is not None:
        _audit("deny", stage=req.stage, host=req.host, port=req.port,
               proto=req.proto, client=req.client, method=req.method,
               url=req.url, reason=slot.refused)
        return AuthorizeResponse(decision="deny", reason=slot.refused)
    approval_id, event = slot.approval_id, slot.event

    if not slot.joined:
        now = time.time()
        with _connect() as conn:
            conn.execute(
                "INSERT INTO approvals(id, ts, host, port, proto, client, method, "
                "url, status) VALUES (?,?,?,?,?,?,?,?, 'pending')",
                (approval_id, now, req.host, req.port, req.proto, req.client,
                 req.method, req.url))
            conn.commit()
    # Audited PER REQUEST either way, with this request's own method and url, because
    # grouping is a concept of the screen and the worker pool — never of the record.
    # The joiner's reason names the card it attached to, so the log explains on its own
    # terms why four requests produced one approval and one decision.
    _audit("hold", stage=req.stage, host=req.host, port=req.port,
           proto=req.proto, client=req.client, method=req.method, url=req.url,
           reason=(f"joined hold {approval_id} — duplicate of a request already "
                   "awaiting approval" if slot.joined else "held for approval"))

    # Block until a human resolves this hold or the window elapses. The wakeup is
    # advisory: the DURABLE approvals row is the single source of truth for the
    # outcome. Exactly one of this timeout path and resolve() flips the row out of
    # 'pending' — each via an atomic conditional UPDATE (…WHERE status='pending')
    # that SQLite serializes — so a resolve landing just as the hold times out can
    # no longer leave the row 'allowed' (and persist a rule) while the agent is
    # told 'deny'. Whoever's UPDATE wins decides; the loser reads the winner's row.
    # The card's remaining window, not a fresh one — see _PENDING_DEADLINE. Every
    # waiter on a card therefore wakes at the same instant, which is what lets them
    # race harmlessly for the expiry UPDATE below.
    event.wait(max(0.0, slot.deadline - time.time()))
    with _connect() as conn:
        expired = conn.execute(
            "UPDATE approvals SET status='expired', resolved_at=? "
            "WHERE id=? AND status='pending'", (time.time(), approval_id)).rowcount
        status_row = None if expired else conn.execute(
            "SELECT status, mode, resolved_by FROM approvals WHERE id=?",
            (approval_id,)).fetchone()
        conn.commit()
    if expired:
        # Only the waiter that WON the expiry closes the group, and it does so before
        # releasing its slot: the card is now decided, so nothing may still join it.
        _close_group(approval_id)
    _release_hold(approval_id)

    # Carry the resolver's provenance (recorded by resolve()) into the audit reason,
    # so the log answers "who granted this egress" and not merely "a human did".
    actor = (status_row["resolved_by"] if status_row else None) or "actor unrecorded"
    # Whether the decision also wrote STANDING POLICY belongs in the audit line: a
    # one-off and a persist are the same allow for this request and very different
    # afterwards, and the log said nothing about which had happened. From the durable
    # ``mode`` column, so it reports what was recorded rather than what was asked for.
    # (Naming the pattern too would need a column on ``approvals``, which has no
    # migration step — see the NOTE above ``_seed_if_empty``. The rule itself is
    # recorded, with its pattern and ``source='operator'``, in the rules table.)
    scope = ("this request only" if not status_row
             else "standing rule written" if status_row["mode"] == "persist"
             else "this request only")
    # Read the STATUS rather than "did I win the expiry UPDATE": with duplicates
    # grouped, several waiters wake together and only one of them wins it. The losers
    # read status='expired' and must report the timeout too — testing `expired` alone
    # would have told every one of them a human had rejected their request.
    status = "expired" if expired or status_row is None else status_row["status"]
    if status == "expired":
        final, why = "deny", "no decision within hold timeout — default-deny"
    elif status == "allowed":
        final, why = "allow", f"human approval ({scope}) [{actor}]"
    else:  # 'denied' (or any non-allowed terminal state) — default-deny
        final, why = "deny", f"human rejection ({scope}) [{actor}]"
    _audit(final, stage=req.stage, host=req.host, port=req.port, proto=req.proto,
           client=req.client, method=req.method, url=req.url, reason=why)
    return AuthorizeResponse(decision=final, reason=why)


# ── approvals (human-facing) ────────────────────────────────────────────────

@app.get("/approvals")
def approvals() -> dict:
    return _pending_payload()


@app.post("/approvals/{approval_id}/resolve")
def resolve(approval_id: str, req: ResolveRequest, request: Request) -> JSONResponse:
    if req.action not in ("allow_once", "allow_persist", "deny_once", "deny_persist"):
        return JSONResponse({"ok": False, "detail": "bad action"}, status_code=400)
    outcome = "allow" if req.action.startswith("allow") else "deny"
    persist = req.action.endswith("persist")
    # Captured BEFORE the update so the same value lands on the durable row and, via
    # that row, in the audit reason the blocked authorize() waiter writes.
    actor = _actor(request)

    with _LOCK:
        event = _PENDING_EVENTS.get(approval_id)
    if event is None:
        # Already resolved, expired, or unknown — nothing to wake.
        return JSONResponse(
            {"ok": False, "detail": "not pending (expired or already resolved)"},
            status_code=409)

    with _connect() as conn:
        row = conn.execute(
            "SELECT host FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown"}, status_code=404)
        # Settle WHAT a persist writes before anything is written, and settle it from
        # the host on the durable row rather than from the request body — the caller
        # chooses among candidates, it does not supply them (see _persist_candidates).
        pattern = None
        if persist:
            allowed = _persist_candidates(row["host"])
            if not allowed:
                return JSONResponse(
                    {"ok": False,
                     "detail": f"no rule pattern can be derived from host "
                               f"{row['host']!r}"}, status_code=400)
            pattern = (req.pattern or "").strip().lower() or allowed[0]
            if pattern not in allowed:
                # Refused BEFORE the UPDATE, so a rejected pattern neither resolves the
                # hold nor consumes it: the approval stays pending and the operator can
                # choose again. (A `*_persist` that half-applied — decision recorded,
                # rule not — would be the worst of both.)
                return JSONResponse(
                    {"ok": False,
                     "detail": f"pattern {pattern!r} is not one this approval may "
                               f"persist (allowed: {', '.join(allowed)})"},
                    status_code=400)
            # A rule for this pattern may ALREADY EXIST with the opposite action, and
            # the insert below is INSERT OR IGNORE against a UNIQUE(pattern) — so it
            # would silently write nothing while this endpoint reported persisted:true
            # and the card confirmed a standing rule. Deny-over-allow is the dangerous
            # direction: the operator believes they have permanently blocked a subtree,
            # and every later request to it is allowed without even raising a hold.
            #
            # Refused BEFORE the UPDATE for the same reason as the branch above — the
            # approval stays pending and decidable, rather than half-applying with the
            # decision recorded and the rule not.
            #
            # Reachable only through a rule created WHILE this hold was pending: every
            # candidate is derived from the held host and matches it, so a pre-existing
            # rule would have decided the request instead of holding it. Two concurrent
            # holds for sibling hosts, resolved with the same broadened pattern in
            # opposite directions, is the shape — which is what a burst of holds across
            # one domain looks like.
            existing = conn.execute(
                "SELECT action FROM rules WHERE pattern=?", (pattern,)).fetchone()
            wanted = "allow" if outcome == "allow" else "block"
            if existing is not None and existing["action"] != wanted:
                return JSONResponse(
                    {"ok": False,
                     "detail": f"a standing rule for {pattern!r} already exists and "
                               f"{existing['action']}s it; this would write "
                               f"{wanted!r} and cannot, because nothing here replaces "
                               f"a rule. Decide this request with a *_once action, or "
                               f"persist a different pattern.",
                     "conflict": {"pattern": pattern, "action": existing["action"]}},
                    status_code=409)
            # Same action already present is NOT a conflict — the policy the operator
            # is asking for is already in force. Proceed, and report below that this
            # call wrote nothing, so the card stops claiming a write it did not make.
        wrote_rule = False
        updated = conn.execute(
            "UPDATE approvals SET status=?, mode=?, resolved_at=?, resolved_by=? "
            "WHERE id=? AND status='pending'",
            ("allowed" if outcome == "allow" else "denied",
             "persist" if persist else "once", time.time(), actor,
             approval_id)).rowcount
        if updated and persist:
            # OR IGNORE stays, even though the conflicting case is now refused above:
            # the check and this insert are not one atomic statement, so a rule could
            # still appear between them. What changes is that the outcome is READ from
            # rowcount instead of assumed — the response reports whether a row was
            # actually written, not whether one was asked for.
            wrote_rule = conn.execute(
                "INSERT OR IGNORE INTO rules(pattern, action, source, created_at) "
                "VALUES (?,?, 'operator', ?)",
                (pattern, "allow" if outcome == "allow" else "block",
                 time.time())).rowcount > 0
        conn.commit()

    if not updated:
        return JSONResponse(
            {"ok": False, "detail": "raced — no longer pending"}, status_code=409)

    # We won the conditional UPDATE above, so the durable row already carries the
    # decision the waiters will read. Wake them — but only while the slot is still
    # registered: if the hold window elapsed and it released between our UPDATE and
    # here, skip, so we don't set a dead event. A missed wake is harmless (the
    # waiter already read, or will read, the decision from the durable row).
    #
    # ``event.set()`` releases EVERY waiter on this card, which is the whole of what
    # grouping does to this endpoint: one click, one durable row, one audit line per
    # released request. Closing the group here stops further joins. The decision
    # committed just above (outside _LOCK), so a duplicate can still slip into the
    # narrow gap before this line and inherit this outcome — but it is identical by
    # group key (client/host/port/proto), so it rides the same grant just made, and a
    # deny is fail-safe. (Fully closing the gap would mean holding _LOCK across the DB
    # commit above.)
    with _LOCK:
        _close_group_locked(approval_id)
        if approval_id in _PENDING_EVENTS:
            event.set()
    # ``pattern`` is echoed so the UI reports what was actually STORED rather than what
    # was clicked — the two differ when the request omitted a pattern (defaulting to the
    # exact host) and, more usefully, it is the string an operator would have to go and
    # delete by hand.
    # ``persisted`` is whether THIS call wrote a rule, read from the insert's rowcount
    # rather than from what was asked for. The two differ when the same rule was
    # already in place, and that difference is precisely what used to be reported as a
    # successful write. ``already_present`` carries the other half, so the UI can say
    # "already in place" instead of either claiming a write or going silent about
    # policy the operator just asked for.
    return JSONResponse({"ok": True, "outcome": outcome,
                         "persisted": wrote_rule,
                         "already_present": persist and not wrote_rule,
                         "pattern": pattern})


@app.get("/approvals/stream")
async def approvals_stream(request: Request) -> StreamingResponse:
    """Server-sent events: push the pending-approval payload whenever it changes.
    Polls SQLite once a second (a fast indexed query; the brief sync read is
    negligible on the event loop) and emits on change, plus a periodic heartbeat
    so proxies/clients can detect a dead stream.

    Change-detection is on the SERIALIZED payload, which is why every field in it
    must be stable while nothing happens — see the note in ``_saturation`` about
    absolute timestamps. A field that ticks turns this into a 1 Hz emitter."""
    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            payload = json.dumps(_pending_payload())
            if payload != last:
                last = payload
                yield f"event: pending\ndata: {payload}\n\n"
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1.0)
    return StreamingResponse(gen(), media_type="text/event-stream")


# ── UI + status ─────────────────────────────────────────────────────────────

@app.get("/api/audit")
def api_audit(limit: int = 50) -> dict:
    """Recent decisions, newest first, for the UI's decisions table.

    ``client`` is here because this control plane is SHARED ACROSS SANDBOXES. Without
    it a row records that egress to a host was allowed but not whose request it was,
    which is the question an audit trail exists to answer the moment more than one
    agent is running. It is the peer address the proxy observed — there is no sandbox
    name to map it to, and the raw address is what the saturation banner's detail line
    reports too.

    The column list is deliberately narrower than the table. ``port``/``proto``/
    ``method``/``url`` are recorded and queryable but not served here: the URL in
    particular is agent-controlled and unbounded, and this is a glanceable list of
    forty rows rather than the forensic interface. ``make logs-cp`` and the store
    itself remain the complete record.

    **Rows are grouped, and the group key is exactly what the UI renders.** A client
    that retries a permanently-refused host on a timer — a background exporter or
    updater refused by a standing rule, once a minute, forever — otherwise fills this
    list with 1440 identical rows a day and pushes everything else off the bottom
    within the hour. Two properties follow from keying on the DISPLAYED fields:

      - No two rows here can look identical, because rows that would look the same
        ARE the same group. That is the property that makes the list scannable,
        stated directly rather than approximated.
      - ``client`` is in the key. One host refused for two sandboxes is two facts,
        and attribution is the entire reason that column exists.

    Read a grouped ``client`` as an ADDRESS, not as a sandbox. Docker hands ``.2`` to
    whichever container starts first, so a group spanning days covers however many
    sandboxes held that address over the span — a live sighting of ``.2`` and ``.3``
    really is two concurrent sandboxes, but "400x from 172.30.0.2 since last week" is
    an address's history, not an agent's. Grouping is what introduced this: an
    ungrouped row was one instant, where the peer address is unambiguous. ``first_ts``
    is the visible cue that a long span is in play. Fixing it properly needs a stable
    per-sandbox identity, and the only ways to get one are the Docker socket (which
    this proxy must never hold) or a launcher-to-control-plane path that does not
    exist — neither is worth inventing for a label.

    ``port``/``proto`` are deliberately NOT in the key: they are not displayed, so
    keying on them would split one group into rows a reader cannot tell apart.

    Grouping is a property of this VIEW and never of the record — the ``audit`` table
    keeps every row, and ``n``/``first_ts`` are how the view stays honest about what
    it folded. Note the inner slice: it bounds the work by EVENT COUNT rather than by
    time, so the cost is fixed as the table grows (it rides ``audit_ts``), and the
    span covered adapts on its own — about a day when something is retrying every
    minute, months when nothing is. A time bound would go empty on a quiet system,
    which is the one thing a decisions list must not do."""
    limit = max(1, min(limit, 500))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT decision, stage, host, client, reason, "
            "       COUNT(*) AS n, MAX(ts) AS ts, MIN(ts) AS first_ts "
            "FROM (SELECT * FROM audit ORDER BY ts DESC LIMIT ?) "
            "GROUP BY decision, stage, host, client, reason "
            "ORDER BY ts DESC LIMIT ?", (AUDIT_GROUP_SCAN, limit)).fetchall()
        # What the list is a WINDOW ONTO. Without it the view silently truncates:
        # forty rows look like the whole record, and grouping made that worse rather
        # than better, because the counts on each row appear to explain the volume
        # away. The cost is a COUNT(*) per poll, which is why the frontend stops
        # polling in a hidden tab — that gating is what makes this affordable instead
        # of a scan every four seconds for as long as the page is open. Rides the
        # audit_ts index, so it is a small scan rather than a table read.
        total = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    return {"rows": [_audit_view(r) for r in rows], "total": total}


def _audit_view(row) -> dict:
    """One grouped row as the UI receives it, plus the one thing it cannot work out
    for itself: whether this denial was policy or an outage.

    Classified HERE rather than in the frontend so the marker string lives next to
    the guard test that pins it, and so the browser is not matching on prose. The
    field is additive — a client that ignores it renders exactly what it rendered
    before."""
    out = dict(row)
    reason = out.get("reason") or ""
    out["fail_closed"] = reason.startswith(FAIL_CLOSED_REASON)
    return out


@app.get("/api/rules")
def api_rules() -> list[dict]:
    """Read-only view of the policy store — the rules that decide every request.

    Exists because standing policy was INVISIBLE from the interface built to govern
    it: the UI showed pending approvals and recent decisions, but never the rules, so
    answering "what have I permanently allowed?" meant `docker compose exec` and SQL
    against the volume. Policy that accumulates unseen drifts, and a `*_persist`
    approval writes to it with no way to review the result.

    Deliberately UNPAGINATED: this is the complete policy, and a silently truncated
    view of it would be worse than none — the whole point is that nothing standing is
    hidden. Rules are operator/seed-created and bounded in practice, unlike the audit
    table (which is capped for exactly the opposite reason: it grows without bound and
    nobody needs all of it at once).

    Ordered blocks first, because block wins over allow in ``_decide`` — the listing
    reads in precedence order rather than alphabetically. Read-only on purpose: this
    change makes policy visible, it does not add mutation (see the rule-management
    item in DESIGN.md for what revocation still needs)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, pattern, action, source, created_at FROM rules "
            "ORDER BY action DESC, pattern").fetchall()
    # `id` is served so revocation can key on it. Not cosmetic: patterns are the one
    # field a revoke could plausibly key on instead, and they carry a live
    # normalization gap (``_match`` lowercases but does not strip a trailing FQDN dot
    # — see DESIGN.md), so a pattern-keyed delete inherits every such mismatch and can
    # miss the row the operator is looking at. An id cannot.
    return [dict(r, scope=_pattern_scope(r["pattern"])) for r in rows]


@app.get("/api/config")
def api_config() -> dict:
    """The settings the UI cannot behave correctly without knowing. Read-only, and
    non-secret by construction — nothing here decides anything.

    Just the hold window today, and that one is load-bearing: a held request BLOCKS the
    agent and default-denies after ``HOLD_TIMEOUT``, so a card that cannot say how long
    is left cannot distinguish hold-for-approval from a slow deny. Sent rather than
    hardcoded in the page, so the number the operator sets is the number they see."""
    return {"hold_timeout": HOLD_TIMEOUT}


@app.post("/api/saturation/ack")
def api_saturation_ack(req: AckRequest) -> dict:
    """Acknowledge over-cap rejections the operator has read.

    Server-side because a dismissal held in the page is not a dismissal: reloading
    restored the banner, which is worse than not offering the button — the operator
    believes they have cleared something and the state disagrees.

    A HIGH-WATER MARK, not a reset. Acknowledging "the 2 I read" leaves a third that
    arrived while the click was in flight still unread; zeroing the counter would
    swallow it, and rejections arrive in bursts, which is exactly when that window is
    open. Monotonic for the same reason — a lower count never un-acknowledges.

    Clamped to what has actually happened. An acknowledgement above the current total
    would suppress FUTURE rejections until they caught up, which is a governance signal
    silenced by an unvalidated client number — the same reasoning that validates
    ``pattern`` in ``resolve``, and the same answer.

    Deliberately NOT audited. The rejections themselves are already in the audit table
    with their reasons; this changes what the banner displays and touches no evidence,
    so a row here would put a non-decision in the decisions log for no gain."""
    with _LOCK:
        total = int(_SATURATION["count"])  # type: ignore[arg-type]
        acked = max(0, min(int(req.count), total))
        if acked > int(_SATURATION["acked"]):  # type: ignore[arg-type]
            _SATURATION["acked"] = acked
            _SATURATION["acked_ts"] = time.time()
        return {"ok": True, "acknowledged": _SATURATION["acked"],
                "rejections": total}


@app.post("/api/rules/{rule_id}/revoke")
def revoke_rule(rule_id: int, request: Request) -> JSONResponse:
    """Remove one operator-created rule. The other half of a governance plane that
    could grant but never take back.

    **Seed rules are refused, and refused HERE rather than merely hidden in the UI.**
    Their source of truth is ``policies/egress-allowlist.txt``, a reviewed file under
    version control, and a click that left the file disagreeing with the store would
    make the file a lie. It also removes a trap for free: ``_seed_if_empty`` re-reads
    that file whenever the rules table is empty, so a store whose every rule could be
    revoked would silently resurrect the whole seed allowlist on the next restart.
    With seed rules undeletable the table cannot reach that state.

    The consequence of retiring a TRANSITIONAL seed entry (npm, PyPI, GitHub — see
    DESIGN.md) is therefore that it happens as a migration shipped beside the code
    that replaces it, not as an operator action. That is the right shape for it: it is
    a versioned, reviewed change to a declared policy.

    Deletion rather than a tombstone. The audit row below IS the history — a rules
    table carrying dead rows would have to be filtered by every reader of it,
    including ``_decide``, which is the one place a mistake is unrecoverable.

    Provenance is recorded exactly as ``resolve`` records it, and for the same reason:
    editing standing policy is more consequential than any single egress decision, and
    until now nothing recorded that it had happened at all. Detection, not prevention
    — the fields are forgeable by a host-local caller (see ``_actor``)."""
    actor = _actor(request)
    with _connect() as conn:
        row = conn.execute(
            "SELECT pattern, action, source FROM rules WHERE id=?",
            (rule_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown rule"},
                                status_code=404)
        if row["source"] == "seed":
            return JSONResponse(
                {"ok": False,
                 "detail": f"{row['pattern']} came from the policy seed and cannot "
                           f"be revoked here — edit policies/egress-allowlist.txt"},
                status_code=403)
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        conn.commit()

    # What the host reverts TO is the useful half of this record. Both actions land on
    # `hold` — an unmatched host is held for approval — but from opposite directions,
    # and only the reason line says which.
    _audit("revoke", stage="policy", host=row["pattern"],
           reason=f"{row['action']} rule revoked by {actor}; {row['pattern']} is now "
                  f"unknown and will be held for approval")
    return JSONResponse({"ok": True, "pattern": row["pattern"],
                         "action": row["action"]})


@app.get("/status", response_class=PlainTextResponse)
def status() -> str:
    with _connect() as conn:
        rules = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    return (f"dockade control plane (2b) — {rules} rules, {audits} audit rows, "
            f"{pending} pending approvals\n")


# ── entry point ─────────────────────────────────────────────────────────────

async def main() -> None:
    """Serve both listeners from one process and one event loop.

    ``uvicorn`` is imported HERE rather than at module scope so the module stays
    importable with only the stdlib — the unit suite loads this file directly with
    stub fastapi/pydantic and installs no packages (see tests/_loader.py)."""
    import uvicorn

    _assert_listeners_separated()
    _bootstrap()
    print(f"control-plane: authorize on {AUTHORIZE_BIND}:{AUTHORIZE_PORT}, "
          f"management on {MANAGE_BIND}:{MANAGE_PORT}", flush=True)

    # The ingest is a plain task on this loop rather than a lifespan hook, for the
    # same reason _bootstrap is not one: it belongs to the PROCESS, not to either
    # app. Holding the reference matters — asyncio keeps only a weak one, so a
    # create_task whose result nobody holds can be collected mid-flight and the
    # ingest would stop with no error anywhere.
    drain = None
    if DRAIN_INTERVAL > 0:
        drain = asyncio.create_task(_audit_drain_loop())
    else:
        print("control-plane: audit ingest DISABLED "
              "(CONTROL_AUDIT_DRAIN_INTERVAL=0); locally-decided egress will "
              "appear only in the proxy's own log", flush=True)

    # Two servers sharing one process means sharing one set of signal handlers,
    # and SIGTERM has to stop BOTH or `docker compose down` waits out the grace
    # period and SIGKILLs the governance authority with holds in flight.
    #
    # uvicorn already handles this, and the mechanism is worth naming because it is
    # not obvious: each ``serve()`` wraps itself in ``capture_signals()``, so the
    # second server's handler replaces the first's — but on exit it restores what
    # it replaced and re-raises the signal it caught, which then reaches the first.
    # Measured on the pinned 0.34.0 (NOTES.md): SIGTERM logs two clean shutdowns
    # and the process is gone inside a second. An earlier version of this function
    # added handlers of its own to "fix" the overwrite; they were inert — uvicorn
    # installs via ``signal.signal``, which displaces asyncio's — and removing them
    # changed nothing, so they are gone rather than kept as insurance.
    servers = [
        uvicorn.Server(uvicorn.Config(
            authorize_app, host=AUTHORIZE_BIND, port=AUTHORIZE_PORT,
            log_level="info")),
        uvicorn.Server(uvicorn.Config(
            app, host=MANAGE_BIND, port=MANAGE_PORT, log_level="info")),
    ]
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        if drain is not None:
            drain.cancel()


if __name__ == "__main__":
    asyncio.run(main())
