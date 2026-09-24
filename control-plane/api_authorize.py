# SPDX-License-Identifier: Apache-2.0
"""The egress proxy's question, ``POST /authorize`` — served on the authorize
listener and nowhere else (see app.py).

``policy._decide`` answers allow, deny or hold for the CLIENT CLASS asking (the
ingress network the caller reached the proxy on), so a rule written for one population
decides nothing for another. Allow and deny are audited and returned. A hold BLOCKS the
request until a human resolves it or CONTROL_HOLD_TIMEOUT elapses and default-denies;
the proxy only ever sees allow or deny. A request identical to one already held JOINS
it (``holds._group_key``), so a retrying agent raises one card, and one click can
release several waiting requests.
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
    rather than what was asked for. No row at all is the expiry path, whose reason
    does not use this.

    A lease states the configured duration, not its own deadline: that would be a
    second read of ``leases`` per released request, and the lease row already carries
    the exact instant (``/api/egress/leases``)."""
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
    # Derived once and carried through every write this request makes: the audit
    # rows, the approvals row, and through that row the rule a persist writes. So the
    # class that DECIDED the request is the one recorded, by construction.
    client_class = policy._client_class(req.client)
    decision, reason = policy._decide(req.host, client_class)

    if decision in ("allow", "deny"):
        store._audit(decision, stage=req.stage, host=req.host, port=req.port,
                     proto=req.proto, client=req.client, client_class=client_class,
                     method=req.method, url=req.url, reason=reason)
        return AuthorizeResponse(decision=decision, reason=reason)

    # HOLD, bounded. The cap check and the reservation are one atomic step, so
    # concurrent holds cannot race past a cap; over the global or per-client cap this
    # fails CLOSED at once rather than blocking another worker.
    slot = holds._reserve_hold(uuid.uuid4().hex, threading.Event(), req.client,
                               req.host, req.port, req.proto)
    if slot.refused is not None:
        store._audit("deny", stage=req.stage, host=req.host, port=req.port,
                     proto=req.proto, client=req.client, client_class=client_class,
                     method=req.method, url=req.url, reason=slot.refused)
        return AuthorizeResponse(decision="deny", reason=slot.refused)
    approval_id, event = slot.approval_id, slot.event

    # The slot is RESERVED from here, and the `finally` is what gives it back. A leaked
    # reservation leaves `_GROUPS` naming this approval id, so every later request with
    # the same (client, host, port, proto) joins a card with no approvals row and no
    # waiter: it blocks out the window and default-denies as if the operator ignored
    # it, and nothing for that destination can be approved until a restart. Enough of
    # them exhaust MAX_WAITERS and fail every sandbox's holds closed. The store write
    # below is the reachable trigger (a full disk, a lock past the busy timeout).
    #
    # That path audits nothing here: the 500 makes the proxy's `_authorize` fail
    # closed, and the proxy records the denial in its own stream, which is ingested.
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
        # Audited PER REQUEST, joined or not, with this request's own method and url:
        # grouping belongs to the screen and the worker pool, never to the record. A
        # joiner's reason names the card, so the log shows why four requests got one
        # decision.
        store._audit("hold", stage=req.stage, host=req.host, port=req.port,
                     proto=req.proto, client=req.client, client_class=client_class,
                     method=req.method, url=req.url,
                     reason=(f"joined hold {approval_id} — duplicate of a request "
                             "already awaiting approval"
                             if slot.joined else "held for approval"))

        # Block until a human resolves this hold or the window elapses. The wakeup is
        # advisory; the DURABLE approvals row is the outcome. This timeout and
        # ``resolve`` each flip the row out of 'pending' with a conditional UPDATE
        # (…WHERE status='pending'), so exactly one of them decides and the other reads
        # its row. Otherwise a resolve landing as the hold times out could leave the
        # row 'allowed', and a rule written, while the agent is told 'deny'.
        # The card's remaining window, not a fresh one (``holds._PENDING_DEADLINE``),
        # so every waiter on a card wakes at once and races harmlessly for the expiry.
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

    # The resolver's provenance, so the log says who granted this egress and not
    # merely that a human did.
    actor = (status_row["resolved_by"] if status_row else None) or "actor unrecorded"
    scope = _decision_scope(status_row)
    # The STATUS, not "did I win the expiry UPDATE": grouped waiters wake together and
    # only one wins it. Testing `expired` alone would tell the losers that a human
    # rejected their request.
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
