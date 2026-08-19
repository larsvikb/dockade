# SPDX-License-Identifier: Apache-2.0
"""Hold-for-approval — the in-process registry behind a blocked request.

An unmatched host is HELD: ``/authorize`` blocks its worker on a
``threading.Event`` until a human resolves the approval or the window elapses
(-> default-deny). This module owns everything that state needs — the caps that
bound it, the grouping that collapses a retry storm into one card, and the
saturation counters the UI's banner reads — but not the endpoints that drive it,
which are in ``app.py``.

SINGLE PROCESS ONLY. Every dict below is in-memory, so a held ``/authorize`` and
the ``resolve`` that releases it must share memory; that constraint is what makes
the two listeners two sockets in one process rather than two services (see the
``app.py`` module docstring). The Event only WAKES the blocked worker — the
human's decision is read back from the durable approvals row, which is the single
source of truth.
"""
from __future__ import annotations

import os
import threading
import time
from typing import NamedTuple

import policy
import store

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
    # ``persistable`` is whether a `*_persist` can be offered at all. It is false for an
    # unclassified client, because a standing rule has to be scoped to a class and
    # "whoever we could not identify" is not one — ``resolve`` refuses it, and the card
    # should say so before the click rather than after (the same discipline
    # ``existing`` follows for the conflict case).
    return [dict(r, requests=max(1, waiters.get(r["id"], 1)),
                 persistable=bool(r["client_class"])
                 and r["client_class"] != policy.UNCLASSIFIED,
                 persist_options=[{"pattern": p, "scope": policy._pattern_scope(p),
                                   "existing": rules.get((p, r["client_class"]))}
                                  for p in policy._persist_candidates(r["host"])])
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
