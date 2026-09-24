# SPDX-License-Identifier: Apache-2.0
"""The egress proxy's question, ``POST /authorize`` — served on the authorize
listener and nowhere else (see app.py).

The authorize flow (one call from the proxy, `POST /authorize`). Every rule is scoped
to a CLIENT CLASS — the ingress network the caller reached the proxy on, named by
``policy._client_class`` — so "matches" below means matches for the class asking, and
a rule written for one client population decides nothing for another:
  - host matches a BLOCK rule            -> deny   (audited)
  - host matches an ALLOW rule           -> allow  (audited)
  - no matching rule                     -> HOLD: record a pending approval and
    BLOCK the request until a human resolves it or CONTROL_HOLD_TIMEOUT elapses
    (-> default-deny). The proxy only ever sees allow/deny; the hold is internal.
    A request identical to one already held JOINS it rather than raising a second
    approval, so a retrying agent produces one card and one decision — which is
    also why one click can release several blocked requests (see holds._group_key).
"""
from __future__ import annotations

import threading
import time
import uuid

import holds
import policy
import store
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


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


def _decision_scope(status_row) -> str:
    """How far a human's decision reached, for the audit reason a released waiter
    writes. Read from the durable ``mode`` column, so it reports what was RECORDED
    rather than what was asked for.

    A function rather than the inline conditional it replaced, because there are three
    modes now and the middle one is the reason: a lease is neither "this request only"
    nor standing policy, and a log that collapsed it into either would misreport the one
    thing an operator comes to this line to find out. `None` — no resolver row at all —
    is the expiry path, which reached nothing.

    Naming the lease's own deadline here would mean a second read of the ``leases``
    table per released request; the configured duration is the same for every lease and
    the row itself carries the exact instant (``/api/egress/leases``), so this states
    the duration and lets that be the record."""
    mode = status_row["mode"] if status_row else None
    if mode == "persist":
        # Names the PATTERN, because "allow api.example.co.uk" and "allow .co.uk"
        # are the same click and very different policy. Read from the durable
        # column; a row that predates it says only that a rule was written.
        pattern = status_row["pattern"]
        return f"standing rule written: {pattern}" if pattern else "standing rule written"
    if mode == "lease":
        return f"lease written, {policy._short_duration(policy.LEASE_SECONDS)}"
    return "this request only"


@router.post("/authorize", response_model=AuthorizeResponse)
def authorize(req: AuthorizeRequest) -> AuthorizeResponse:
    # Derived HERE, once, and carried through every write this request makes — the
    # audit rows, the approvals row, and via that row the rule a persist writes. One
    # derivation rather than several means the value that DECIDED the request is the
    # same one that gets recorded, by construction instead of by two lookups agreeing.
    client_class = policy._client_class(req.client)
    decision, reason = policy._decide(req.host, client_class)

    if decision in ("allow", "deny"):
        # Every decision is audited — no governed path bypasses the log (CLAUDE.md).
        store._audit(decision, stage=req.stage, host=req.host, port=req.port,
                     proto=req.proto, client=req.client, client_class=client_class,
                     method=req.method, url=req.url, reason=reason)
        return AuthorizeResponse(decision=decision, reason=reason)

    # HOLD (bounded): reserve a hold slot atomically with the cap check, so
    # concurrent holds can't race past the cap. Over the global or per-client cap,
    # fail CLOSED immediately rather than registering another worker-blocking hold.
    # A request identical to one already held JOINS it instead of raising a second
    # card (holds._group_key) — a retrying agent used to fill its whole card budget
    # with copies of one question.
    slot = holds._reserve_hold(uuid.uuid4().hex, threading.Event(), req.client,
                               req.host, req.port, req.proto)
    if slot.refused is not None:
        store._audit("deny", stage=req.stage, host=req.host, port=req.port,
                     proto=req.proto, client=req.client, client_class=client_class,
                     method=req.method, url=req.url, reason=slot.refused)
        return AuthorizeResponse(decision="deny", reason=slot.refused)
    approval_id, event = slot.approval_id, slot.event

    # From here the slot is RESERVED, so every exit has to give it back — which is what
    # the `finally` is for, and it is not defensive habit. A reservation that leaks is
    # not merely a lost slot: `_GROUPS` still names this approval id, so every later
    # request with the same (client, host, port, proto) JOINS a card that has no
    # approvals row and no waiter coming for it. Those requests block out the original
    # window, default-deny with a reason that reads as operator inaction, and never
    # raise a card anyone can approve — so one failed write makes that destination
    # permanently un-decidable, and enough of them exhaust MAX_WAITERS and fail every
    # sandbox's holds closed until a restart. The store write below is the reachable
    # trigger (a full disk, a lock held past the busy timeout).
    #
    # Nothing is audited on that path and nothing needs to be: the exception becomes a
    # 500, the proxy's `_authorize` fails closed on it, and the proxy writes the denial
    # to its own stream, which the ingest picks up. The decision is recorded by the
    # component that made it.
    try:
        if not slot.joined:
            now = time.time()
            with store._connect() as conn:
                conn.execute(
                    "INSERT INTO approvals(id, ts, host, port, proto, client, "
                    "client_class, method, url, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?, 'pending')",
                    (approval_id, now, req.host, req.port, req.proto, req.client,
                     client_class, req.method, req.url))
                conn.commit()
        # Audited PER REQUEST either way, with this request's own method and url,
        # because grouping is a concept of the screen and the worker pool — never of
        # the record. The joiner's reason names the card it attached to, so the log
        # explains on its own terms why four requests produced one approval and one
        # decision.
        store._audit("hold", stage=req.stage, host=req.host, port=req.port,
                     proto=req.proto, client=req.client, client_class=client_class,
                     method=req.method, url=req.url,
                     reason=(f"joined hold {approval_id} — duplicate of a request "
                             "already awaiting approval"
                             if slot.joined else "held for approval"))

        # Block until a human resolves this hold or the window elapses. The wakeup is
        # advisory: the DURABLE approvals row is the single source of truth for the
        # outcome. Exactly one of this timeout path and resolve() flips the row out of
        # 'pending' — each via an atomic conditional UPDATE (…WHERE status='pending')
        # that SQLite serializes — so a resolve landing just as the hold times out can
        # no longer leave the row 'allowed' (and persist a rule) while the agent is
        # told 'deny'. Whoever's UPDATE wins decides; the loser reads the winner's row.
        # The card's remaining window, not a fresh one — see holds._PENDING_DEADLINE.
        # Every waiter on a card therefore wakes at the same instant, which is what
        # lets them race harmlessly for the expiry UPDATE below.
        event.wait(max(0.0, slot.deadline - time.time()))
        with store._connect() as conn:
            expired = conn.execute(
                "UPDATE approvals SET status='expired', resolved_at=? "
                "WHERE id=? AND status='pending'", (time.time(), approval_id)).rowcount
            status_row = None if expired else conn.execute(
                "SELECT status, mode, resolved_by, pattern FROM approvals WHERE id=?",
                (approval_id,)).fetchone()
            conn.commit()
        if expired:
            # Only the waiter that WON the expiry closes the group, and it does so
            # before releasing its slot: the card is now decided, so nothing may still
            # join it.
            holds._close_group(approval_id)
    finally:
        holds._release_hold(approval_id)

    # Carry the resolver's provenance (recorded by resolve()) into the audit reason,
    # so the log answers "who granted this egress" and not merely "a human did".
    actor = (status_row["resolved_by"] if status_row else None) or "actor unrecorded"
    # How far the decision REACHED belongs in the audit line: three modes that are the
    # same allow for this request and very different afterwards, and the log said
    # nothing about which had happened. See ``_decision_scope``.
    scope = _decision_scope(status_row)
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
    store._audit(final, stage=req.stage, host=req.host, port=req.port,
                 proto=req.proto, client=req.client, client_class=client_class,
                 method=req.method, url=req.url, reason=why)
    return AuthorizeResponse(decision=final, reason=why)
