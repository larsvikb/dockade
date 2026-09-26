# SPDX-License-Identifier: Apache-2.0
"""Hold-for-approval — the in-process registry behind a blocked request.

An unmatched host is HELD: ``/authorize`` blocks its worker on a
``threading.Event`` until a human resolves the approval or the window elapses
(-> default-deny). This module owns everything that state needs — the caps that
bound it, the grouping that collapses a retry storm into one card, and the
saturation counters the UI's banner reads — but not the endpoints that drive it,
which are in the ``api_*`` modules.

SINGLE PROCESS ONLY. Every dict below is in-memory, so a held ``/authorize`` and
the ``api_approvals.resolve`` that releases it must share memory (see the ``app.py``
module docstring). The Event only WAKES the blocked worker; the human's decision is
read back from the durable approvals row, the single source of truth.

The TOOL ASKS at the bottom block nothing, so they keep no in-process state and the
row is the whole ask. They live here because they share the saturation account and
the queue, and because the ways they differ from a hold only read next to one.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from typing import NamedTuple

import policy
import store

# How long a held request waits for a human before defaulting to deny.
HOLD_TIMEOUT = float(os.environ.get("CONTROL_HOLD_TIMEOUT", "120"))
# Bound concurrent holds. FOUR caps, two nouns times two scopes:
#
#             |  global            |  per client
#   ----------|--------------------|-----------------------------
#   CARDS     |  MAX_PENDING       |  MAX_PENDING_PER_CLIENT
#   WAITERS   |  MAX_WAITERS       |  MAX_WAITERS_PER_CLIENT
#
# A CARD is a pending approval on the operator's screen. A WAITER is a blocked FastAPI
# threadpool worker. Duplicates join an existing card, so one card can hold N waiters,
# and the two need separate bounds.
#
#   The CARD caps protect ATTENTION. Nobody triages twelve simultaneous questions, and
#   an agent that can put four on the screen can already drown out another's one.
#
#   The WAITER caps protect WORKERS. A held request pins a worker until it is resolved
#   or times out, and this control plane is shared by every sandbox, so without them
#   one agent could stall every /authorize decision. Keep MAX_WAITERS well under the
#   threadpool size (anyio default ~40) so fast allow/deny decisions always have free
#   workers. A joiner costs a card nothing, so the per-client WAITER cap is what bounds
#   one agent retrying one host (see ``_reserve_hold`` for the order).
#
# The four defaults are chosen so EACH CAP CAN BE THE FIRST TO FIRE — a cap that can
# never bind is one nobody can reason about, and cards are always <= waiters, so a card
# cap set equal to its waiter cap is dead:
#
#   one agent, 4 distinct hosts        -> per-client cards (4)
#   three clients at 4 cards each      -> global cards (12)
#   one agent, 4 cards, retrying       -> per-client waiters (8)
#   two agents at 8 waiters each       -> global waiters (16)
#
# ZERO means different things by scope, and the asymmetry is deliberate. On a GLOBAL
# cap it refuses everything, which is fail-closed and a plausible way to say "stop
# holding anything at all". On a PER-CLIENT cap it DISABLES the cap, because the
# fail-closed reading would make every client's first hold impossible, which cannot be
# what anyone meant by setting it.
#
# Over any of the four, /authorize fails CLOSED immediately (deny) instead of
# registering another blocking hold.
MAX_PENDING = int(os.environ.get("CONTROL_MAX_PENDING", "12"))
MAX_PENDING_PER_CLIENT = int(os.environ.get("CONTROL_MAX_PENDING_PER_CLIENT", "4"))
MAX_WAITERS = int(os.environ.get("CONTROL_MAX_WAITERS", "16"))
MAX_WAITERS_PER_CLIENT = int(os.environ.get("CONTROL_MAX_WAITERS_PER_CLIENT", "8"))

# In-memory registry of held requests, keyed by approval id. No outcome is kept here
# (see the module docstring).
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
# rather than inheriting an outcome it was never shown alongside.
_GROUPS: dict[tuple, str] = {}
# approval_id -> the wall-clock instant this card default-denies. Set ONCE, when the
# card is created: a joiner waits out the REMAINDER, or an agent retrying on a short
# loop would push the deadline out forever and the countdown would be a lie. Its next
# retry after the card closes opens a new card with a full window.
_PENDING_DEADLINE: dict[str, float] = {}

# Over-cap rejections, for the UI's saturation banner. In memory is enough: each deny
# is written to the audit table with its reason (``api_authorize.authorize``), so
# losing this on restart costs a banner, not evidence.
#
# Saturation is INVISIBLE and TRANSIENT: over a cap /authorize fails closed without
# raising a card, so the queue looks as empty as when nothing is happening, and holds
# drain in seconds, so a live gauge is healthy again by the time anyone looks. A
# lasting count of the EVENT is what makes it noticeable.
#
# ``since`` is the process start: "3 denied unheard" means three since then, never
# three ever. ``acked`` is a HIGH-WATER MARK rather than a reset-to-zero, so a
# rejection arriving while the dismiss click is in flight stays unread — and
# rejections arrive in bursts, which is when that gap is open.
_STARTED_TS = time.time()
_SATURATION: dict[str, object] = {
    "count": 0, "last_ts": None, "last_scope": None, "last_host": None,
    "acked": 0, "acked_ts": None}


def _group_key(client: str | None, host: str | None,
               port: int | None, proto: str | None) -> tuple:
    """What makes two held requests THE SAME REQUEST for grouping purposes.

    ``client`` is in the key because a card names one client, and approving one
    sandbox's request must not silently release another's.

    The client CLASS is not a field: it is a pure function of the address
    (``policy._client_class``), so it could never split a group the address joined.
    That is also what lets a joiner skip writing its own approvals row — the card was
    raised under its class.

    ``method`` and ``url`` are OUT: they are what varies across the retries this
    collapses (a query string, a cache-buster). Every joined request still writes its
    own audit line with its own method and url (``api_authorize.authorize``); only the
    CARD shows one representative."""
    return ((client or None), (host or "").lower(),
            port, (proto or "").lower() or None)


def _client_waiters_locked(client: str | None) -> int:
    """How many blocked workers one client is holding, across all of its cards.
    Caller must hold ``_LOCK``.

    Summed over the two registries rather than kept as a third counter, which would be
    a second source of truth for the number a cap is checked against."""
    return sum(n for approval_id, n in _PENDING_WAITERS.items()
               if _PENDING_CLIENT.get(approval_id) == client)


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

    The whole check+reserve runs under ``_LOCK``, so concurrent holds cannot race past
    a cap, nor into creating two cards for one key.

    **Every cap this request will draw on is checked BEFORE the early return that
    consumes it.** A joined waiter costs a worker like any other, so a join above the
    per-client waiter check would let one agent retrying one host fill the global pool
    from a single card. So the order is waiters, then join, then cards:

      1. global waiters   — every request costs one, joined or not
      2. per-client waiters
      3. JOIN and return  — costs no card, so nothing below applies
      4. global cards     — only a new card reaches here
      5. per-client cards

    A rejection is recorded in ``_SATURATION`` here, so the one place that decides
    "over the cap" is the one place that reports it. The scope string names WHICH of
    the four fired: "one agent is hammering" and "the whole control plane is loaded"
    want different responses from the operator."""
    key = _group_key(client, host, port, proto)
    with _LOCK:
        def refuse(scope: str) -> HoldSlot:
            _SATURATION["count"] = int(_SATURATION["count"]) + 1  # type: ignore[arg-type]
            _SATURATION["last_ts"] = time.time()
            _SATURATION["last_scope"] = scope
            _SATURATION["last_host"] = host
            return HoldSlot(None, None, False,
                            f"hold capacity exceeded ({scope}) — fail-closed", 0.0)

        if sum(_PENDING_WAITERS.values()) >= MAX_WAITERS:
            return refuse("global waiters")

        if (client is not None and MAX_WAITERS_PER_CLIENT > 0
                and _client_waiters_locked(client) >= MAX_WAITERS_PER_CLIENT):
            return refuse(f"client {client} waiters")

        joined_id = _GROUPS.get(key)
        joined_event = _PENDING_EVENTS.get(joined_id) if joined_id else None
        if joined_id and joined_event is not None:
            _PENDING_WAITERS[joined_id] = _PENDING_WAITERS.get(joined_id, 0) + 1
            return HoldSlot(joined_id, joined_event, True, None,
                            _PENDING_DEADLINE.get(joined_id, 0.0))

        if len(_PENDING_EVENTS) >= MAX_PENDING:
            return refuse("global cards")

        if (client is not None and MAX_PENDING_PER_CLIENT > 0
                and sum(1 for c in _PENDING_CLIENT.values() if c == client)
                >= MAX_PENDING_PER_CLIENT):
            return refuse(f"client {client} cards")

        deadline = time.time() + HOLD_TIMEOUT
        _PENDING_EVENTS[approval_id] = event
        _PENDING_CLIENT[approval_id] = client
        _PENDING_WAITERS[approval_id] = 1
        _PENDING_DEADLINE[approval_id] = deadline
        _GROUPS[key] = approval_id
        return HoldSlot(approval_id, event, False, None, deadline)


def _close_group_locked(approval_id: str) -> None:
    """Stop new requests JOINING this card, without disturbing the waiters already on
    it. Caller must hold ``_LOCK``: ``api_approvals.resolve`` calls this in the same
    critical section that wakes the waiters, so none is released while the card is
    still joinable.

    The decision itself commits OUTSIDE ``_LOCK``, so a duplicate can still join in a
    narrow gap and inherit an outcome it did not wait for. The gap, its bound and why
    it is tolerated are at the call site in ``api_approvals.resolve``, and in
    SECURITY.md.

    Separate from ``_release_hold`` because the two happen at different times: the card
    stops being joinable at the decision, and its slots free as each blocked worker
    wakes. Collapsing them would leave a decided card joinable until the slowest
    waiter noticed."""
    for key, held in list(_GROUPS.items()):
        if held == approval_id:
            del _GROUPS[key]


def _close_group(approval_id: str) -> None:
    """``_close_group_locked`` for callers that do not already hold ``_LOCK``."""
    with _LOCK:
        _close_group_locked(approval_id)


def _saturation() -> dict:
    """Hold-cap pressure, for the UI banner.

    Two gauges, because either global cap can be the one about to fire.
    ``in_flight``/``max_waiters`` is blocked workers; ``cards``/``max_pending`` is
    questions on the operator's screen. The banner shows whichever is nearer its limit
    (``saturationState`` in control-plane-ui/app.js).

    ``in_flight`` counts BLOCKED WAITERS, not visible cards: one card can hold several
    waiters, so "12/16 in flight" beside three cards is the true, alarming number.

    Every timestamp here is ABSOLUTE. An elapsed-seconds field would change on every
    tick, and the SSE stream emits on payload change — so it would defeat the
    change-detection, silence the heartbeat, and turn an idle stream into a 1 Hz
    firehose. The client does the arithmetic, as it already does for the countdown."""
    with _LOCK:
        return {
            "in_flight": sum(_PENDING_WAITERS.values()),
            "cards": len(_PENDING_EVENTS),
            "max_waiters": MAX_WAITERS,
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

    Per-waiter, not per-card: N grouped workers each release their own slot as they
    wake. The card is unregistered only when the last has gone — until then
    ``api_approvals.resolve`` must still find its event, and the global cap must still
    count the workers it holds."""
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


def _classified(client_class: str | None) -> bool:
    """Whether a grant that OUTLIVES the request can be scoped to this card's client.

    Both `*_persist` and `allow_lease` are scoped to a client class, and "whoever we
    could not identify" is not one — ``api_approvals.resolve`` refuses both. One
    definition, so the card cannot offer one button while disabling the other."""
    return bool(client_class) and client_class != policy.UNCLASSIFIED


def _list_pending() -> list[dict]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, host, port, proto, client, client_class, method, url "
            "FROM approvals WHERE status='pending' ORDER BY ts").fetchall()
        # Every standing rule, read once, so each offered pattern can say whether one
        # already exists. Keyed by (pattern, class), the uniqueness
        # ``api_approvals.resolve`` collides on: by pattern alone, a rule in ANOTHER
        # class would read as already present while the request stayed held.
        rules = {(r["pattern"], r["client_class"]): r["action"]
                 for r in conn.execute(
                     "SELECT pattern, action, client_class FROM rules")}
    with _LOCK:
        waiters = dict(_PENDING_WAITERS)
    # What each card carries beyond its row:
    #
    #   ``persist_options``  exactly the patterns ``api_approvals.resolve`` accepts,
    #       from the one candidate set beside the matcher, so the page cannot drift
    #       into offering a pattern the backend rejects.
    #   ``requests``  how many blocked requests one click decides: with duplicates
    #       grouped, "allow once" can grant several. At least 1 — a pending row always
    #       has a waiter in this process (``app._bootstrap`` expires any left by a
    #       previous one), and "0 requests" would read as a card that decides nothing.
    #   ``existing``  the action of a standing rule already holding a pattern, so the
    #       confirm panel can warn before ``api_approvals.resolve`` refuses to replace
    #       it. The rule can still appear between render and click; resolve covers that.
    #   ``persistable`` / ``leasable``  whether each grant can be offered at all
    #       (``_classified``). Two fields for one condition, so the payload says what
    #       each button needs and the page need not know they share it.
    #   ``kind``  what the merged queue dispatches on (``_pending_payload``), stated on
    #       both builders so neither surface is the implicit one.
    #
    # A lease needs no options field: its host is the requested one and its duration
    # is the same for every card (``policy.LEASE_SECONDS``), which is why the lease
    # button is one click where a persist is two.
    return [dict(r, kind="egress", requests=max(1, waiters.get(r["id"], 1)),
                 persistable=_classified(r["client_class"]),
                 leasable=_classified(r["client_class"]),
                 persist_options=[{"pattern": p, "scope": policy._pattern_scope(p),
                                   "existing": rules.get((p, r["client_class"]))}
                                  for p in policy._persist_candidates(r["host"])])
            for r in rows]


# ── tool asks — the same three states, and almost none of the same machinery ──
#
# A tool ask is registered and answered IMMEDIATELY: the gateway takes an id back at
# once and hands the agent a pending result to come back with (DESIGN.md, "An `ask`
# answers immediately"). Everything the registry above exists for follows from a
# blocked worker — the Event, the waiter counts, the WAITER caps — so none of it
# applies, and the tool side is DURABLE STATE ONLY: the row is the ask.
#
# Two things carry over, the two that were never about workers: the CARD caps,
# because an agent opening asks with slightly varied payloads floods a human exactly
# as a retry storm does; and the saturation account, unsplit, so an operator reads
# one banner.

# The window a tool ask waits for a human. Not ``HOLD_TIMEOUT``, which is bounded by
# what a blocked agent and a proxy will sit through: nothing waits on an ask, so this
# can be a human interval — an hour, not two minutes. An env var like the other
# bounds (control-plane/DESIGN.md, "Hold bounds are fail-closed").
TOOL_HOLD_TIMEOUT = float(os.environ.get("CONTROL_TOOL_HOLD_TIMEOUT", "3600"))
# The window an APPROVED ask waits for the gateway to claim it, counted from
# ``resolved_at``. Its own number: reusing the one above would give an ask answered a
# second before its deadline a one-second claim window.
#
# Without it an unbounded grant is a standing authorization, redeemable a week later
# by a session the operator has forgotten. Shorter than the ask window: a human
# deciding is slow, and a gateway that already has its answer is not.
TOOL_GRANT_TIMEOUT = float(os.environ.get("CONTROL_TOOL_GRANT_TIMEOUT", "900"))
# The card caps, tool-side. Their own numbers, since the two surfaces share no pool
# and a queue of asks costs no workers.
MAX_TOOL_PENDING = int(os.environ.get("CONTROL_MAX_TOOL_PENDING", "12"))
MAX_TOOL_PENDING_PER_CLIENT = int(
    os.environ.get("CONTROL_MAX_TOOL_PENDING_PER_CLIENT", "4"))
# Ceiling on a payload this will store. REFUSED over it, not truncated as
# ``store.DRAIN_MAX_FIELD`` truncates a URL: a truncated payload is shown to a human
# as the thing they are approving, and the hidden tail is where anything worth hiding
# would go.
TOOL_ARGS_MAX = 8192


def _canonical_args(args: object) -> str:
    """The one serialization of a payload: what gets stored, what gets hashed, and
    what the human is shown.

    ONE form for all three, so the string in the record is byte-for-byte the string
    the digest covers, and an approval cannot be bound to something other than what
    was read.

    Key order is normalized. That is not the reordering control-plane-ui/DESIGN.md
    forbids, which is a view differing from the real payload: here the normalized form
    IS the payload of record."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _args_digest(server: str, tool: str, args_json: str) -> str:
    """What binds a grant to a payload, and what an identical retry joins on.

    Over the canonical form, so key order cannot make one ask look like two: a model
    that reformulates ``{"a":1,"b":2}`` as ``{"b":2,"a":1}`` is asking the same
    question and should attach to the pending card rather than raise a second one.
    Any difference in a VALUE lands on a different digest, which is the half that
    matters — an approval a human gave for one set of arguments must never execute
    another.

    The server and tool are inside the hash rather than beside it, so a payload
    approved for one tool cannot be replayed against another."""
    return hashlib.sha256(
        f"{server}\x00{tool}\x00{args_json}".encode()).hexdigest()


class ToolAsk(NamedTuple):
    """Outcome of registering an ask. Exactly one of ``refused`` / ``approval_id`` is
    set; ``joined`` means an identical ask was already pending and this attached to
    it rather than raising a second card."""
    approval_id: str | None
    joined: bool
    refused: str | None
    deadline: float


def _expire_tool_asks() -> int:
    """Retire every ask whose window has run out. Returns how many.

    TWO windows, because an ask waits twice. A `pending` row waits for a human and
    falls due at its ``deadline``; an `allowed` row nobody has claimed waits for a
    GATEWAY, and falls due ``TOOL_GRANT_TIMEOUT`` after the human answered.

    LAZY, and called wherever an ask is read: nothing blocks on a tool ask, so there
    is no waiter whose timeout could enforce the window, and a row can simply be read
    as expired. That keeps the state honest at every point anyone can observe it.

    A CLAIMED row is never touched, whatever its age: `spent` says the call happened,
    `expired` says it never will, and a duplicate resumption depends on the difference.
    An expired ask is likewise distinct from a denied one — only one means a human
    decided.

    ``resolved_at`` is written only by the pending branch. On a grant it is the
    human's decision time, which the cutoff is measured FROM.

    Reads before it writes: the SSE tick re-reads the queue once a second, and an
    unconditional UPDATE would take a write lock on the crown-jewel store every time,
    where a lock contended at the wrong moment becomes a fail-closed deny. The check
    makes the common case a WAL read. A row falling due between the two statements is
    expired on the next read instead.

    Every retired row gets an AUDIT ROW, as the egress expiry does through its
    released waiter. Retired row by row with the same conditional predicate, so the
    row audited is exactly the row this call changed."""
    now = time.time()
    grant_cutoff = now - TOOL_GRANT_TIMEOUT
    with store._connect() as conn:
        # One probe for both windows, so the nothing-to-do case stays a single read.
        due = conn.execute(
            "SELECT id, server, tool, client, status FROM tool_approvals "
            "WHERE (status='pending' AND deadline <= ?) "
            "OR (status='allowed' AND claimed_at IS NULL "
            "    AND resolved_at IS NOT NULL AND resolved_at <= ?)",
            (now, grant_cutoff)).fetchall()
        if not due:
            return 0
        retired = []
        for row in due:
            if row["status"] == "pending":
                changed = conn.execute(
                    "UPDATE tool_approvals SET status='expired', resolved_at=? "
                    "WHERE id=? AND status='pending' AND deadline <= ?",
                    (now, row["id"], now)).rowcount
                why = (f"no decision within the tool hold window "
                       f"({policy._short_duration(TOOL_HOLD_TIMEOUT)}) — default-deny; "
                       f"{row['tool']} on {row['server']} will not run")
            else:
                # Separate statement rather than one OR'd UPDATE, because the SET
                # clauses differ: see the ``resolved_at`` paragraph above.
                changed = conn.execute(
                    "UPDATE tool_approvals SET status='expired' "
                    "WHERE id=? AND status='allowed' AND claimed_at IS NULL "
                    "AND resolved_at IS NOT NULL AND resolved_at <= ?",
                    (row["id"], grant_cutoff)).rowcount
                why = (f"approved but never resumed within "
                       f"{policy._short_duration(TOOL_GRANT_TIMEOUT)} — grant lapsed; "
                       f"{row['tool']} on {row['server']} will not run")
            if changed:
                retired.append((row, why))
        conn.commit()
    for row, why in retired:
        store._audit("deny", stage="tool-ask", client=row["client"],
                     client_class=policy._client_class(row["client"]),
                     server=row["server"], tool=row["tool"], approval_id=row["id"],
                     reason=why)
    return len(retired)


def _register_tool_ask(server: str, tool: str, args: object,
                       client: str | None = None) -> ToolAsk:
    """Raise a card for a tool call, or attach to the identical one already pending.

    Under ``_LOCK`` for the same reason as ``_reserve_hold``: otherwise two concurrent
    asks both pass a cap with one slot left, or both open a card for one payload. A
    process lock is enough because this is a single process by construction.

    Order is size, then join, then caps — the join goes before the caps because a
    joiner here costs nothing (no card, no row, no attention), so it must not be
    refused for capacity it does not consume."""
    args_json = _canonical_args(args)
    if len(args_json) > TOOL_ARGS_MAX:
        # Not a cap rejection: nothing was contended for, and reporting it in the
        # saturation banner would tell an operator that governance is under pressure
        # when one caller sent something oversized.
        return ToolAsk(None, False,
                       f"tool arguments are {len(args_json)} characters; the ceiling is "
                       f"{TOOL_ARGS_MAX} — an ask a human cannot be shown in full is "
                       f"refused rather than shown in part", 0.0)
    _expire_tool_asks()
    digest = _args_digest(server, tool, args_json)
    now = time.time()
    with _LOCK, store._connect() as conn:
        pending = conn.execute(
            "SELECT id, client, deadline, args_digest FROM tool_approvals "
            "WHERE status='pending' ORDER BY ts").fetchall()
        # The client is in the join key for the reason it is in ``_group_key``: a card
        # names one caller, and answering one sandbox's question must not silently
        # answer another's.
        for row in pending:
            if row["client"] == client and row["args_digest"] == digest:
                return ToolAsk(row["id"], True, None, row["deadline"])

        if len(pending) >= MAX_TOOL_PENDING:
            return _refuse_tool("global tool asks")
        if (client is not None and MAX_TOOL_PENDING_PER_CLIENT > 0
                and sum(1 for r in pending if r["client"] == client)
                >= MAX_TOOL_PENDING_PER_CLIENT):
            return _refuse_tool(f"client {client} tool asks")

        approval_id = uuid.uuid4().hex
        deadline = now + TOOL_HOLD_TIMEOUT
        conn.execute(
            "INSERT INTO tool_approvals(id, ts, server, tool, args_json, "
            "args_digest, client, status, deadline) "
            "VALUES (?,?,?,?,?,?,?, 'pending', ?)",
            (approval_id, now, server, tool, args_json, digest, client, deadline))
        conn.commit()
        return ToolAsk(approval_id, False, None, deadline)


def _refuse_tool(scope: str) -> ToolAsk:
    """Record an over-cap tool ask in the ONE saturation account and refuse it.
    Caller holds ``_LOCK``.

    One account for both surfaces, so an operator reads one banner. ``last_host`` is
    left alone: the banner's subject field is host-shaped, and the scope string says
    what this was."""
    _SATURATION["count"] = int(_SATURATION["count"]) + 1  # type: ignore[arg-type]
    _SATURATION["last_ts"] = time.time()
    _SATURATION["last_scope"] = scope
    return ToolAsk(None, False,
                   f"tool ask capacity exceeded ({scope}) — fail-closed", 0.0)


def _get_tool_ask(approval_id: str) -> dict | None:
    """One ask by id, expiry applied first. None if there is no such ask.

    ONE at a time, and there is deliberately no listing counterpart for the agent:
    the gateway's agent-facing listener is on a network both tiers share, so a roster
    would leak approvals the caller never raised — and past the leak it hands the
    agent a read on the operator's queue (DESIGN.md, "One id at a time")."""
    _expire_tool_asks()
    with store._connect() as conn:
        row = conn.execute(
            "SELECT id, ts, server, tool, args_json, client, status, deadline, "
            "resolved_at, claimed_at FROM tool_approvals WHERE id=?",
            (approval_id,)).fetchone()
    return dict(row) if row is not None else None


def _resolve_tool_ask(approval_id: str, decision: str, actor: str) -> str | None:
    """Record the human's answer. Returns the new status, or None if the ask was not
    pending — already decided, expired, or never existed.

    Conditional on ``status='pending'`` inside the UPDATE rather than checked first,
    so two clicks on one card cannot both land: the second changes no row and reads
    back as the non-transition it was."""
    if decision not in ("allowed", "denied"):
        return None
    with store._connect() as conn:
        changed = conn.execute(
            "UPDATE tool_approvals SET status=?, resolved_at=?, resolved_by=? "
            "WHERE id=? AND status='pending'",
            (decision, time.time(), actor, approval_id)).rowcount
        conn.commit()
    return decision if changed else None


class PinnedAnswer(NamedTuple):
    """Outcome of allowing an ask and pinning it. ``status`` is None when nothing was
    written; then ``refused`` says why if the card is still pending and decidable, and
    is None if it is not pending at all. ``created`` is False when the same pin was
    already in place."""
    status: str | None
    refused: str | None
    pin_id: int | None
    created: bool


def _resolve_tool_ask_pinned(approval_id: str, actor: str,
                             pins_json: str) -> PinnedAnswer:
    """Allow the ask and write its pin in ONE transaction, or do neither.

    The tool's rule is read inside that transaction, and the pin is refused unless it
    is `ask`. A rule moved or revoked while the card was pending would otherwise get a
    pin that decides nothing, and one that comes back with the next `ask` rule for the
    same tool. BEGIN IMMEDIATE takes the write lock before the read, so a revoke cannot
    land between the check and the insert; ``api_mcp.revoke_mcp_rule`` closes the other
    direction in its DELETE."""
    now = time.time()
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ask = conn.execute(
                "SELECT server, tool FROM tool_approvals "
                "WHERE id=? AND status='pending'", (approval_id,)).fetchone()
            if ask is None:
                conn.rollback()
                return PinnedAnswer(None, None, None, False)
            rule = conn.execute(
                "SELECT action FROM tool_rules WHERE server=? AND tool=?",
                (ask["server"], ask["tool"])).fetchone()
            if rule is None or rule["action"] != "ask":
                conn.rollback()
                now_ruled = ("has no rule" if rule is None
                             else f"is ruled {rule['action']!r}")
                return PinnedAnswer(
                    None, f"{ask['tool']} on {ask['server']} {now_ruled} now, and a pin "
                          f"decides only under 'ask'; allow this call without a pin "
                          f"instead", None, False)
            conn.execute(
                "UPDATE tool_approvals SET status='allowed', resolved_at=?, "
                "resolved_by=? WHERE id=? AND status='pending'",
                (now, actor, approval_id))
            inserted = conn.execute(
                "INSERT OR IGNORE INTO tool_pins(server, tool, pins_json, approval_id, "
                "created_at, granted_by) VALUES (?,?,?,?,?,?)",
                (ask["server"], ask["tool"], pins_json, approval_id, now, actor))
            created = inserted.rowcount > 0
            pin_id = inserted.lastrowid if created else conn.execute(
                "SELECT id FROM tool_pins WHERE server=? AND tool=? AND pins_json=?",
                (ask["server"], ask["tool"], pins_json)).fetchone()["id"]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return PinnedAnswer("allowed", None, pin_id, created)


def _claim_tool_ask(approval_id: str) -> dict | None:
    """Take single-use ownership of an APPROVED ask, for the gateway about to run it.
    Returns the ask if this caller now owns it, None if it is not claimable.

    The gateway executes on RESUMPTION rather than at the human's click, so an approved
    call never runs with nobody left to receive it — and an agent can resume twice.
    The claim is the conditional UPDATE below, so exactly one resumption performs the
    side effect; a second gets None and can be told the ask is spent.

    The grant window is in the UPDATE as well as in ``_expire_tool_asks``: expiry is
    lazy, so this predicate is what makes "a stale grant cannot be redeemed" a
    property of the write, not of having been read recently enough."""
    _expire_tool_asks()
    now = time.time()
    with store._connect() as conn:
        changed = conn.execute(
            "UPDATE tool_approvals SET claimed_at=? "
            "WHERE id=? AND status='allowed' AND claimed_at IS NULL "
            "AND resolved_at IS NOT NULL AND resolved_at > ?",
            (now, approval_id, now - TOOL_GRANT_TIMEOUT)).rowcount
        conn.commit()
    return _get_tool_ask(approval_id) if changed else None


def _list_tool_asks() -> list[dict]:
    """Pending asks, oldest first — the tool half of the operator's queue.

    A separate builder feeding one list (``_pending_payload``). No ``requests``,
    because a joiner adds no waiter. ``pin_options`` is the tool side's
    ``persist_options``: what a pin may be made of, derived here so the card offers
    exactly what ``resolve`` will accept (``policy._pin_candidates``)."""
    _expire_tool_asks()
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, server, tool, args_json, client, deadline "
            "FROM tool_approvals WHERE status='pending' ORDER BY ts").fetchall()
    return [dict(r, kind="tool", pin_options=policy._pin_candidates(r["args_json"]))
            for r in rows]


def _pending_payload() -> dict:
    """What the approvals view needs, in one object: the holds AND the hold-cap
    pressure beside them.

    Not a second endpoint: the SSE stream already re-serializes this each second and
    emits on change, so a rejection reaches the banner within a second through the
    push the page already listens to.

    **ONE list over two builders**, where everything else about tool policy splits:
    two streams merged in the browser would render a silent subset when one dropped,
    and a subset of a queue looks exactly like an empty one (control-plane/DESIGN.md,
    "`approvals` splits the same way; the operator's queue does not").

    Ordered by ``ts`` ACROSS the two, because the operator's question is which
    decision has waited longest, whichever subsystem raised it."""
    holds_and_asks = _list_pending() + _list_tool_asks()
    holds_and_asks.sort(key=lambda card: card["ts"])
    return {"holds": holds_and_asks, "saturation": _saturation()}
