# SPDX-License-Identifier: Apache-2.0
"""Hold-for-approval — the in-process registry behind a blocked request.

An unmatched host is HELD: ``/authorize`` blocks its worker on a
``threading.Event`` until a human resolves the approval or the window elapses
(-> default-deny). This module owns everything that state needs — the caps that
bound it, the grouping that collapses a retry storm into one card, and the
saturation counters the UI's banner reads — but not the endpoints that drive it,
which are in the ``api_*`` modules.

SINGLE PROCESS ONLY. Every dict below is in-memory, so a held ``/authorize`` and
the ``resolve`` that releases it must share memory; that constraint is what makes
the listeners separate sockets in one process rather than separate services (see
the ``app.py`` module docstring). The Event only WAKES the blocked worker — the
human's decision is read back from the durable approvals row, which is the single
source of truth.

The TOOL ASKS at the bottom are the exception that shows what that constraint is
for. Nothing blocks on one, so they keep no in-process state at all and the row is
the whole ask — which means they alone would survive being served from a second
process. They are here rather than in a module of their own because they share the
saturation account and the queue, and because the ways they differ from a hold are
only legible next to one.
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
# Bound concurrent holds. FOUR caps, and they are two nouns times two scopes — which
# is the whole of the scheme, and the reason the names are what they are:
#
#             |  global            |  per client
#   ----------|--------------------|-----------------------------
#   CARDS     |  MAX_PENDING       |  MAX_PENDING_PER_CLIENT
#   WAITERS   |  MAX_WAITERS       |  MAX_WAITERS_PER_CLIENT
#
# A CARD is a pending approval on the operator's screen. A WAITER is a blocked FastAPI
# threadpool worker. They were the same number until duplicate grouping, which made one
# card able to hold N waiters — and it is that decoupling, not the caps themselves,
# that this shape exists to survive.
#
#   The CARD caps protect ATTENTION. Nobody triages twelve simultaneous questions, and
#   an agent that can put four on the screen can already drown out another's one.
#
#   The WAITER caps protect WORKERS. A held request pins a worker until it is resolved
#   or times out, so an unbounded number stalls ALL /authorize decisions — and this
#   control plane is shared across every sandbox, so one agent could starve governance
#   for all. Keep MAX_WAITERS well under the threadpool size (anyio default ~40) so
#   fast allow/deny decisions always have free workers.
#
# The per-client waiter cap is the one that was MISSING, and its absence was a real
# defect rather than an omission: duplicates join an existing card, so they cost the
# card caps nothing, and the only bound they met was the global waiter cap. One agent
# retrying one host filled the whole pool from a single card and every other sandbox
# was refused. See _reserve_hold for the ordering that fixes it.
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


def _group_key(client: str | None, host: str | None,
               port: int | None, proto: str | None) -> tuple:
    """What makes two held requests THE SAME REQUEST for grouping purposes.

    ``client`` is in the key because a card names one client, and approving one
    sandbox's request must not silently release another's.

    The client CLASS is deliberately not a second key field: it is a pure function of
    the address (``policy._client_class``), so two requests with the same ``client``
    always have the same class and adding it could only ever split a group that the
    address had already joined. That equivalence is what lets a joiner skip writing
    its own approvals row — the card it attaches to was raised under its class.

    ``method`` and ``url`` are deliberately OUT. They are precisely what varies across
    the retries this exists to collapse — a different query string or cache-buster
    each time — so keying on them would defeat grouping in the one case that motivates
    it. Nothing is lost from the record: every joined request writes its own audit
    line carrying its own method and url (see ``authorize``); only the CARD shows one
    representative."""
    return ((client or None), (host or "").lower(),
            port, (proto or "").lower() or None)


def _client_waiters_locked(client: str | None) -> int:
    """How many blocked workers one client is holding, across all of its cards.
    Caller must hold ``_LOCK``.

    Summed over the two registries rather than kept as a third counter, because a
    counter would be a second source of truth for a number that is already implied —
    and the one it could disagree with is the one a cap is checked against. Both dicts
    are keyed by approval id, so this is a join, not a scan of anything new."""
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

    Extracted from ``authorize`` so the cap logic is unit-testable without the
    FastAPI handler (see DESIGN.md). The whole check+reserve runs under ``_LOCK``
    so concurrent holds cannot race past the cap — nor race into creating two cards
    for one key, which is the same problem wearing a different hat.

    **Every cap this request will draw on is checked BEFORE the early return that
    consumes it.** That rule is the fix for a real defect rather than a tidiness
    preference: the join used to return above the per-client check, so a joined waiter
    — which costs a worker exactly like any other — met no per-client bound at all, and
    one agent retrying one host could fill the global pool from a single card and get
    every other sandbox refused. So the order is waiters, then join, then cards:

      1. global waiters   — every request costs one, joined or not
      2. per-client waiters
      3. JOIN and return  — costs no card, so nothing below applies
      4. global cards     — only a new card reaches here
      5. per-client cards

    A rejection is also recorded in ``_SATURATION`` here rather than by the caller,
    so the one place that decides "over the cap" is the one place that reports it —
    the alternative leaves a second call site free to fail closed silently. The scope
    string names WHICH of the four fired, because "one agent is hammering" and "the
    whole control plane is loaded" want different responses from the operator."""
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
    it. Caller must hold ``_LOCK`` — ``resolve`` calls this inside the same critical
    section that wakes the waiters, so no waiter is ever released while the card is
    still joinable.

    That is NOT the same as the card becoming unjoinable the moment it is decided. The
    decision commits OUTSIDE ``_LOCK``, so there is a narrow gap in which a duplicate
    can still join and inherit an outcome it did not wait for. The gap, its bound and
    why it is tolerated are at the call site in ``resolve``; it is also disclosed in
    SECURITY.md.

    Separate from ``_release_hold`` because the two happen at different times: the card
    stops being joinable at the decision (bar that gap), and its slots free as each
    blocked worker wakes and returns. Collapsing them would leave a decided card
    joinable for as long as the slowest waiter took to notice."""
    for key, held in list(_GROUPS.items()):
        if held == approval_id:
            del _GROUPS[key]


def _close_group(approval_id: str) -> None:
    """``_close_group_locked`` for callers that do not already hold ``_LOCK``."""
    with _LOCK:
        _close_group_locked(approval_id)


def _saturation() -> dict:
    """Hold-cap pressure, for the UI banner.

    Two gauges, because there are two global caps and either can be the one about to
    fire. ``in_flight``/``max_waiters`` is blocked workers; ``cards``/``max_pending`` is
    questions on the operator's screen. The banner shows whichever is nearer its limit
    (see ``saturationState`` in app.js) — sending only one would hide the cap that is
    actually about to deny.

    ``in_flight`` is BLOCKED WAITERS and NOT the pending approvals list, which is a
    different set twice over. It diverges after a restart, when the table can carry
    ``pending`` rows with no live hold behind them; and it diverges whenever duplicates
    are grouped, since one card can hold several waiters. A count derived from the
    visible cards would therefore be confidently wrong in the one situation this exists
    to report — "12/16 in flight" reads as an emergency next to three cards until you
    can see both.

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


def _classified(client_class: str | None) -> bool:
    """Whether a grant that OUTLIVES the request can be scoped to this card's client.

    Both `*_persist` and `allow_lease` need this, and need it for the same reason: such
    a grant is scoped to a client class, and "whoever we could not identify" is not one
    — ``resolve`` refuses both. One definition rather than the expression twice, so the
    card cannot end up offering one of the two buttons while disabling the other."""
    return bool(client_class) and client_class != policy.UNCLASSIFIED


def _list_pending() -> list[dict]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, host, port, proto, client, client_class, method, url "
            "FROM approvals WHERE status='pending' ORDER BY ts").fetchall()
        # Every standing rule, so each offered pattern can say whether one already
        # exists for it. Read once for the whole list rather than per candidate — the
        # table is the complete policy and bounded in practice (see ``api_rules``).
        #
        # Keyed by (pattern, class), matching the uniqueness ``resolve`` collides on:
        # keyed by pattern alone, a rule in ANOTHER class would be reported as already
        # present, and the card would offer to persist something it described as
        # existing while the request that raised it stayed held.
        rules = {(r["pattern"], r["client_class"]): r["action"]
                 for r in conn.execute(
                     "SELECT pattern, action, client_class FROM rules")}
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
    # ``persistable`` and ``leasable`` are whether each grant action can be offered at
    # all — false for an unclassified client, because a grant that outlives the request
    # has to be scoped to a class and "whoever we could not identify" is not one
    # (``_classified``). ``resolve`` refuses both, and the card should say so before the
    # click rather than after (the same discipline ``existing`` follows for the conflict
    # case). TWO fields for one condition, deliberately: the payload then states what
    # each button needs, rather than making the page know that persist and lease happen
    # to share a precondition.
    # ``kind`` is what the merged queue dispatches on — see ``_pending_payload``. It
    # is stated on both builders rather than defaulted on one, so neither surface is
    # the implicit case that a reader has to infer from the absence of the other.
    #
    # A lease needs NO options field beside these. Its host is the requested one and
    # its duration is the same for every card (``policy.LEASE_SECONDS``, served by
    # ``/api/config``), so there is nothing per-approval for the operator to choose —
    # which is exactly why the lease button is one click where a persist is two.
    return [dict(r, kind="egress", requests=max(1, waiters.get(r["id"], 1)),
                 persistable=_classified(r["client_class"]),
                 leasable=_classified(r["client_class"]),
                 persist_options=[{"pattern": p, "scope": policy._pattern_scope(p),
                                   "existing": rules.get((p, r["client_class"]))}
                                  for p in policy._persist_candidates(r["host"])])
            for r in rows]


# ── tool asks — the same three states, and almost none of the same machinery ──
#
# A tool ask is registered and answered IMMEDIATELY. Nothing above this line applies
# to it, and the reason is one fact: an egress hold blocks a FastAPI worker on an
# Event until a human answers, while the gateway takes an id back at once and hands
# the agent a pending result to come back with (DESIGN.md, "An ``ask`` answers
# immediately").
#
# Everything the registry above exists for follows from that blocked worker — the
# Event that wakes it, the waiter counts, the two WAITER caps that keep a slow
# decision from starving the /authorize path every sandbox depends on. With nothing
# blocked there is no worker to protect, no event to fire and no in-process state to
# keep, so the tool side is DURABLE STATE ONLY: the row is the ask. That is why the
# split here is so lopsided — the "core" the gateway was expected to reuse turns out
# to be mostly machinery for a problem it does not have.
#
# Two things do carry over, and they are the two that were never about workers. The
# CARD caps, because attention is the scarce thing on both surfaces and an agent
# opening asks with slightly varied payloads floods a human exactly as a retry storm
# does. And the saturation accounting, unsplit, because an operator should not have
# to read two banners to learn that governance is refusing things.

# The window a tool ask waits for a human. A second number, and deliberately not
# ``HOLD_TIMEOUT``: that one is bounded by what a blocked agent and a proxy will sit
# through, and this one is bounded by nothing at all, because nothing is waiting on
# it. So it is free to be a human interval — an hour, rather than two minutes.
# Fail-closed like the rest of the bounds, so it stays an env var
# (control-plane/DESIGN.md, "Hold bounds are fail-closed").
TOOL_HOLD_TIMEOUT = float(os.environ.get("CONTROL_TOOL_HOLD_TIMEOUT", "3600"))
# The window an APPROVED ask waits for the gateway to come back and claim it. A third
# number rather than a reuse of the one above, because the two bound different waits:
# ``TOOL_HOLD_TIMEOUT`` runs from the ask and bounds a human's attention, and this one
# runs from ``resolved_at`` and bounds a GRANT. Sharing them would give an ask answered
# one second before its deadline a one-second claim window, which is the bug that
# reusing the field looks like it fixes.
#
# It exists because an unbounded grant is a standing authorization. Nothing is blocked
# on an approved ask either, so without this a click could be redeemed a week later, by
# a session the operator has forgotten, against conditions they would no longer approve
# — the same "a decision is only good for the circumstances it was made in" that keeps
# the decision endpoint uncacheable. Shorter than the ask window on purpose: a human
# deciding is slow, and a gateway that already has its answer is not.
TOOL_GRANT_TIMEOUT = float(os.environ.get("CONTROL_TOOL_GRANT_TIMEOUT", "900"))
# The card caps, tool-side. Their own numbers rather than the egress ones, because
# the two surfaces no longer share a pool and a queue of asks costs no workers.
MAX_TOOL_PENDING = int(os.environ.get("CONTROL_MAX_TOOL_PENDING", "12"))
MAX_TOOL_PENDING_PER_CLIENT = int(
    os.environ.get("CONTROL_MAX_TOOL_PENDING_PER_CLIENT", "4"))
# Ceiling on a payload this will store. REFUSED over it rather than truncated, which
# is the opposite of what ``store.DRAIN_MAX_FIELD`` does to an agent-supplied URL —
# and the asymmetry is the point. A truncated audit field costs evidence detail; a
# truncated payload is shown to a human as the thing they are approving, so the
# hidden tail would be exactly where anything worth hiding went.
TOOL_ARGS_MAX = 8192


def _canonical_args(args: object) -> str:
    """The one serialization of a payload: what gets stored, what gets hashed, and
    what the human is shown.

    ONE form for all three, which is what makes "the payload is authoritative" mean
    something — the string in the record is byte-for-byte the string the digest
    covers, so an approval cannot be bound to something other than what was read.
    Producing it here rather than accepting a caller's rendering also removes the
    question of whose spelling wins.

    Key order is normalized. That is not the reordering DESIGN warns about, which is
    a UI prettifier rearranging what a human sees while the real payload differs;
    here the normalized form IS the payload of record. Nothing is dropped, summarized
    or truncated — an oversized payload is refused instead."""
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

    TWO windows, because an ask waits twice. A `pending` row is waiting for a human
    and falls due at its stored ``deadline``; an `allowed` row nobody has claimed is
    waiting for a GATEWAY, and falls due ``TOOL_GRANT_TIMEOUT`` after the human
    answered. Both are retired here so that "the state is honest at every point anyone
    can observe it" means the same thing for a grant as for a question — otherwise a
    click stays redeemable forever and an approval becomes a standing authorization.

    A CLAIMED row is never touched, whatever its age. It already ran, and relabelling
    it `expired` would cost the one distinction a duplicate resumption depends on:
    `spent` says the call happened, `expired` says it never will.

    ``resolved_at`` is written only by the pending branch. On a grant it is the
    human's decision time, which is what the cutoff is measured FROM, so overwriting
    it would both destroy the record and move the deadline it defines.

    LAZY, and it has to be: nothing is blocked on a tool ask, so there is no waiter
    whose timeout would enforce the window and no reason to run a timer thread for a
    row that can simply be read as expired. Called wherever an ask is read, which is
    what makes the state honest at every point anyone can observe it.

    An expired ask is TERMINAL and distinct from a denied one. Both refuse the call;
    only one of them means a human decided, and an agent — or an operator reading the
    record later — must be able to tell those apart.

    Reads before it writes, and that is not premature: the merged queue is re-read by
    the SSE tick once a SECOND, so this runs 86,400 times a day with nothing to do on
    almost all of them. An unconditional UPDATE would take a write lock on the
    crown-jewel store every time — the file the governance path is reading, where a
    lock contended at the wrong moment becomes a fail-closed deny. The check makes the
    common case a WAL read. It is racy in the harmless direction: a row falling due
    between the two statements is expired on the next read instead of this one.

    Every row retired here gets an AUDIT ROW, as the egress expiry does through its
    released waiter (a `deny` with "no decision within hold timeout"). It did not,
    and the trail could not then tell a still-pending ask from one that had lapsed,
    nor see that a grant a human had clicked was never redeemed — a state change on
    the approval surface with no record, against "everything consequential is
    audited". Retired ROW BY ROW with the same conditional predicate, so the row
    that is audited is exactly the row this call changed: a sibling process that
    expired it first leaves nothing for this one to say."""
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

    Under ``_LOCK`` for the same reason ``_reserve_hold`` is: the check and the write
    have to be one step, or two concurrent asks both pass a cap with one slot left,
    or both open a card for one payload. The lock is enough here where it is not for
    a general database — this is a single process by construction (see the module
    docstring), and the row is the only state involved.

    Order of refusal is size, then caps, then join, and the join sits BELOW the caps
    on purpose — the mirror image of ``_reserve_hold``, where joining comes first
    because a joiner still costs a worker. Here a joiner costs nothing: no card, no
    row, no attention. So it must not be refused for capacity it does not consume."""
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

    Unsplit deliberately: over a cap nothing raises a card, so the refusal is
    invisible in the queue on this surface exactly as it is on the other, and an
    operator should not have to check two banners to learn that governance is
    refusing things. ``last_host`` is left alone rather than filled with a
    tool-shaped value — the banner's subject field is host-shaped, and the scope
    string is where this surface says what it was."""
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


def _claim_tool_ask(approval_id: str) -> dict | None:
    """Take single-use ownership of an APPROVED ask, for the gateway about to run it.
    Returns the ask if this caller now owns it, None if it is not claimable.

    The gateway executes on RESUMPTION rather than at the human's click, which is what
    keeps an approved call from running with nobody left to receive it. That choice
    needs this one: resumption is a request the agent makes, and an agent can make it
    twice. The claim is the conditional UPDATE below, so exactly one resumption of one
    approval ever performs the side effect — a second gets None and can be told the
    ask is spent, which is a different answer from denied and from unknown.

    The grant window is in the UPDATE as well as in ``_expire_tool_asks`` above, and
    the duplication is deliberate: expiry is lazy, so the predicate here is what makes
    "a stale grant cannot be redeemed" a property of the write rather than of having
    been read recently enough. The pass above is what turns the resulting None into an
    `expired` status rather than a bare "not claimable"."""
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

    A separate BUILDER feeding one list (see ``_pending_payload``), not a separate
    view. It carries none of ``_list_pending``'s rim: no ``requests`` count, because
    nothing is blocked and a joiner adds no waiter to report; and no
    ``persist_options``, because the argument-shaped ladder that would derive them
    does not exist yet, so there is nothing an ask can be persisted AS. Both absences
    are the reason this is its own function rather than a branch inside that one."""
    _expire_tool_asks()
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, server, tool, args_json, client, deadline "
            "FROM tool_approvals WHERE status='pending' ORDER BY ts").fetchall()
    return [dict(r, kind="tool") for r in rows]


def _pending_payload() -> dict:
    """What the approvals view needs, in one object: the holds AND the hold-cap
    pressure beside them.

    Deliberately not a second endpoint. The SSE stream already re-serializes this on
    a 1 s tick and emits on change, so folding saturation in means a rejection reaches
    the banner within a second through the push the page is already listening to —
    no new route, no relay-allowlist entry, and no polling interval to lag behind the
    burst it is meant to report.

    **ONE list over two builders**, and this is the merge the tool surface does not
    get to skip. Everything else about tool policy splits — its own table, its own
    caps, its own endpoints — because two surfaces answering different questions
    should not share a row shape. The queue is the exception, and the reason is a
    failure mode rather than a preference: a partly connected merged view is
    indistinguishable from an empty one. With one stream, "disconnected" is a single
    honest boolean the page can show; with two merged in the browser, one dropping
    renders a silent subset — and a subset of a queue looks exactly like a queue with
    nothing in it. "How many decisions are waiting, how long have I got, is the queue
    at capacity" is asked constantly and cannot be answered one surface at a time.

    Ordered by ``ts`` ACROSS the two, because the operator's question is which
    decision has been waiting longest, and that does not respect which subsystem
    raised it. Sorting per surface and concatenating would put a five-second-old tool
    ask above a two-minute-old hold whenever the tool list came first."""
    holds_and_asks = _list_pending() + _list_tool_asks()
    holds_and_asks.sort(key=lambda card: card["ts"])
    return {"holds": holds_and_asks, "saturation": _saturation()}
