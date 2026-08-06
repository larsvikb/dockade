# SPDX-License-Identifier: Apache-2.0
"""
Control plane — governance authority for the governed data-plane proxies.

Step 2b: policy + audit + **hold-for-approval**. The management app the agent
can never reach (control-net only; the sandbox has no route there). Governed
proxies call it on the control path to authorize connections; unknown requests
are held for a human, who approves/rejects them in a live UI.

The authorize flow (one call from the proxy, `POST /authorize`):
  - host matches a BLOCK rule            -> deny   (audited)
  - host matches an ALLOW rule           -> allow  (audited)
  - no matching rule                     -> HOLD: record a pending approval and
    BLOCK the request until a human resolves it or CONTROL_HOLD_TIMEOUT elapses
    (-> default-deny). The proxy only ever sees allow/deny; the hold is internal.
    A request identical to one already held JOINS it rather than raising a second
    approval, so a retrying agent produces one card and one decision — which is
    also why one click can release several blocked requests (see _group_key).

A human resolves holds over the approvals API (GET /approvals, the SSE stream at
/approvals/stream, and POST /approvals/{id}/resolve), surfaced by the separate
control-plane-ui frontend; the backend serves no HTML itself. Three read-only views
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

Concurrency model: run under a SINGLE uvicorn worker. `/authorize` and the
resolve endpoint are sync (FastAPI runs them in a threadpool); a held request
blocks its worker on a threading.Event that the resolve endpoint sets. Concurrent
holds are bounded (CONTROL_MAX_PENDING / CONTROL_MAX_PENDING_PER_CLIENT) so a
sandbox cannot pin every worker and stall governance for all sandboxes — over the
cap /authorize fails closed. The SQLite store is the source of truth for the UI
(the SSE stream polls it). Do NOT run multiple workers — the pending-event
registry is in-process.

No egress: this service is on control-net only. It must never be given an
internet route — it is pure management state (the crown jewel).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
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

app = FastAPI(title="dockade control plane", version="2b")

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
    host = (host or "").lower()
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
    with _connect() as conn:
        conn.execute(
            "INSERT INTO audit(ts, decision, stage, host, port, proto, client, "
            "method, url, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), decision, fields.get("stage"), fields.get("host"),
             fields.get("port"), fields.get("proto"), fields.get("client"),
             fields.get("method"), fields.get("url"), fields.get("reason")))
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
# Bound the recorded User-Agent so a hostile one cannot bloat the store.
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
        parts.append(f"origin={origin}")
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

@app.on_event("startup")
def _startup() -> None:
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


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# ── authorize (proxy-facing) ────────────────────────────────────────────────

@app.post("/authorize", response_model=AuthorizeResponse)
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
    # released request. Closing the group in the SAME critical section is what makes
    # decision and joinability change together — a duplicate arriving a moment later
    # opens its own card rather than inheriting an outcome it was never shown beside.
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
def api_audit(limit: int = 50) -> list[dict]:
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
    itself remain the complete record."""
    limit = max(1, min(limit, 500))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, decision, stage, host, client, reason FROM audit "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


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
            "SELECT pattern, action, source, created_at FROM rules "
            "ORDER BY action DESC, pattern").fetchall()
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


@app.get("/status", response_class=PlainTextResponse)
def status() -> str:
    with _connect() as conn:
        rules = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    return (f"dockade control plane (2b) — {rules} rules, {audits} audit rows, "
            f"{pending} pending approvals\n")
