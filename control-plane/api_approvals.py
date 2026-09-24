# SPDX-License-Identifier: Apache-2.0
"""The approval queue and ``resolve`` — the endpoint that turns a held request into
allowed egress, and a tool ask into an approved call.

The UI reads the queue from /approvals/stream and answers with POST
/approvals/{id}/resolve, both relayed by control-plane-ui. GET /approvals is the same
list without streaming, kept for `docker exec` debugging; the relay does not forward
it (``_RELAY_ROUTES`` in control-plane-ui/app.py).

An egress card is answered for this request, for this host a while (a lease), or
with a standing rule: ``EGRESS_ACTIONS``. A persisted rule is how trust accrues
(DESIGN.md, on progressive trust), and WHICH rule is the operator's choice from
candidates derived from the held host (``policy._persist_candidates``), never a
string the agent's request can supply.
"""
from __future__ import annotations

import asyncio
import json
import time

import holds
import policy
import provenance
import store
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

router = APIRouter()


class ResolveRequest(BaseModel):
    action: str                    # EGRESS_ACTIONS, or TOOL_ACTIONS on a tool ask
    # Which pattern a `*_persist` action writes: one of the approval's
    # ``policy._persist_candidates``, the narrowest (the exact host) if omitted.
    # Ignored by every other egress action; refused on a tool ask.
    pattern: str | None = None


@router.get("/approvals")
def approvals() -> dict:
    return holds._pending_payload()


#: What each surface's cards may be resolved WITH. Per surface, not a union: an action
#: from the other vocabulary means the caller misread which card it is looking at.
#:
#: The egress set is a DURATION LADDER — this request, this host for a while
#: (``policy.LEASE_SECONDS``), this pattern forever — and two absences are decisions:
#:
#:   - No `deny_lease`. An unmatched host is HELD, not denied, so a timed deny would
#:     mean "suppress the card for a while", a different feature under this one's name.
#:   - No breadth choice on `allow_lease`. A permanent rule needs a decision about how
#:     wide it is; a lease answers that by expiring, so it is always the exact host.
#:
#: The tool set has no `*_persist`: its analogue for a payload (this call, this tool
#: with these arguments, this tool always) is not built, and `allow_persist` would have
#: to mean "allow this tool forever", reached by clicking the same button twice.
EGRESS_ACTIONS = ("allow_once", "allow_lease", "allow_persist",
                  "deny_once", "deny_persist")
TOOL_ACTIONS = ("allow", "deny")


@router.post("/approvals/{approval_id}/resolve")
def resolve(approval_id: str, req: ResolveRequest, request: Request) -> JSONResponse:
    """One endpoint, two surfaces, dispatched on which table holds the id.

    The queue is merged (``holds._pending_payload``), so the id is all the client
    sends back, and it is enough: an id belongs to exactly one table, so nothing in
    the body is trusted to say which kind of card this is. The tool half is
    ``_resolve_tool_ask_request``, so the egress path below reads straight through."""
    if holds._get_tool_ask(approval_id) is not None:
        return _resolve_tool_ask_request(approval_id, req, request)
    # Normalised the way the tool path normalises its action, so a client that sends
    # `Allow_Once ` is refused for being unknown rather than for its spelling.
    action = (req.action or "").strip().lower()
    if action not in EGRESS_ACTIONS:
        return JSONResponse({"ok": False, "detail": "bad action"}, status_code=400)
    outcome = "allow" if action.startswith("allow") else "deny"
    persist = action.endswith("persist")
    lease = action.endswith("lease")
    # Before the UPDATE, so the durable row carries it, and through that row the audit
    # reason the released ``authorize`` waiter writes.
    actor = provenance._actor(request)

    with holds._LOCK:
        event = holds._PENDING_EVENTS.get(approval_id)
    if event is None:
        # Already resolved, expired, or unknown — nothing to wake.
        return JSONResponse(
            {"ok": False, "detail": "not pending (expired or already resolved)"},
            status_code=409)

    with store._connect() as conn:
        row = conn.execute(
            "SELECT host, client_class FROM approvals WHERE id=?",
            (approval_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown"}, status_code=404)
        # The class the request was DECIDED under, from the durable row, like the host
        # below. The rule this writes is scoped to the population the card was raised
        # for, and re-deriving it from the address would be a second classifier for
        # the one caller whose answer becomes standing policy.
        client_class = row["client_class"]
        # What a persist writes is settled from the durable row's host before anything
        # is written: the caller chooses among candidates, it does not supply them.
        pattern = None
        # A grant that OUTLIVES this request needs a client class, and "whoever we could
        # not identify" is not one: it would cover every future unidentified client. A
        # lease is refused too, because expiry bounds how long, not who. Refused
        # before the UPDATE, as below, so the card stays pending and a `*_once`
        # action still decides it.
        if (persist or lease) and (
                not client_class or client_class == policy.UNCLASSIFIED):
            return JSONResponse(
                {"ok": False,
                 "detail": f"this request came from an unclassified client "
                           f"({row['client_class'] or 'none recorded'}), so no "
                           f"{'standing rule' if persist else 'lease'} can be scoped "
                           f"to it. Decide it with a *_once action, or map its network "
                           f"in CONTROL_CLIENT_CLASSES."}, status_code=400)
        if persist:
            allowed = policy._persist_candidates(row["host"])
            if not allowed:
                return JSONResponse(
                    {"ok": False,
                     "detail": f"no rule pattern can be derived from host "
                               f"{row['host']!r}"}, status_code=400)
            pattern = (req.pattern or "").strip().lower() or allowed[0]
            if pattern not in allowed:
                # Refused BEFORE the UPDATE, so the card stays pending and the operator
                # can choose again, rather than half-applying: decision recorded, rule
                # not.
                return JSONResponse(
                    {"ok": False,
                     "detail": f"pattern {pattern!r} is not one this approval may "
                               f"persist (allowed: {', '.join(allowed)})"},
                    status_code=400)
            # A rule for this pattern may already exist with the OPPOSITE action. The
            # insert below is INSERT OR IGNORE on UNIQUE(pattern, client_class), so it
            # would write nothing while the card confirmed a standing rule. Deny over
            # allow is the dangerous direction: the operator believes a subtree is
            # blocked, and every later request to it is allowed without a hold.
            # Refused before the UPDATE, as above.
            #
            # Reachable only through a rule written WHILE this hold was pending, since
            # every candidate matches the held host and an older rule would have
            # decided it: two sibling hosts held at once, resolved with the same wider
            # pattern in opposite directions. Scoped to THIS class, as the UNIQUE is; a
            # rule in another class decides for a different population.
            existing = conn.execute(
                "SELECT action FROM rules WHERE pattern=? AND client_class=?",
                (pattern, client_class)).fetchone()
            wanted = "allow" if outcome == "allow" else "block"
            if existing is not None and existing["action"] != wanted:
                return JSONResponse(
                    {"ok": False,
                     "detail": f"a standing rule for {pattern!r} already exists for "
                               f"client class {client_class!r} and "
                               f"{existing['action']}s it; this would write "
                               f"{wanted!r} and cannot, because nothing here replaces "
                               f"a rule. Decide this request with a *_once action, or "
                               f"persist a different pattern.",
                     "conflict": {"pattern": pattern, "action": existing["action"],
                                  "client_class": client_class}},
                    status_code=409)
            # The same action already present is no conflict: proceed, and report
            # below that this call wrote nothing.
        wrote_rule = False
        lease_expires_at = None
        now = time.time()
        updated = conn.execute(
            "UPDATE approvals SET status=?, mode=?, resolved_at=?, resolved_by=?, "
            "pattern=? WHERE id=? AND status='pending'",
            ("allowed" if outcome == "allow" else "denied",
             "persist" if persist else "lease" if lease else "once", now, actor,
             pattern if persist else None, approval_id)).rowcount
        if updated and lease:
            # INSIDE the `updated` guard: the conditional UPDATE makes exactly one of
            # this call and the waiter's timeout the decider. Written before it, a lease
            # could outlive a card that expired and default-denied its request.
            #
            # Expired rows are swept here, the only place the table grows, rather than
            # by a timer. The sweep does not end a lease: ``policy._live_lease`` filters
            # on the deadline.
            conn.execute("DELETE FROM leases WHERE expires_at <= ?", (now,))
            lease_expires_at = now + policy.LEASE_SECONDS
            # The durable row's host, normalized as `_decide` compares hosts: a lease is
            # matched by equality, so any other shape is a grant no request can equal.
            conn.execute(
                "INSERT INTO leases(host, client_class, approval_id, created_at, "
                "expires_at, granted_by) VALUES (?,?,?,?,?,?)",
                (policy._normalize_host(row["host"]), client_class, approval_id,
                 now, lease_expires_at, actor))
        if updated and persist:
            # OR IGNORE still, because the check above and this insert are not atomic
            # and a rule could appear between them. The rowcount says whether a row was
            # actually written.
            wrote_rule = conn.execute(
                "INSERT OR IGNORE INTO rules(pattern, action, source, created_at, "
                "client_class) VALUES (?,?, 'operator', ?, ?)",
                (pattern, "allow" if outcome == "allow" else "block",
                 time.time(), client_class)).rowcount > 0
        conn.commit()

    if not updated:
        return JSONResponse(
            {"ok": False, "detail": "raced — no longer pending"}, status_code=409)

    # We won the UPDATE, so the durable row carries the decision the waiters read.
    # Wake them only while the slot is still registered; a missed wake is harmless,
    # because a waiter reads the row either way. ``event.set()`` releases EVERY waiter
    # on the card: one click, one row, one audit line per released request.
    #
    # Closing the group stops further joins. The commit above is outside _LOCK, so a
    # duplicate can still join in the gap and inherit this outcome; it is identical by
    # group key (client/host/port/proto), and a deny is fail-safe. Closing the gap
    # fully would mean holding _LOCK across the commit.
    with holds._LOCK:
        holds._close_group_locked(approval_id)
        if approval_id in holds._PENDING_EVENTS:
            event.set()
    # What was STORED, not what was clicked, so the card confirms what happened:
    #   - ``pattern`` as written (the default is the exact host);
    #   - ``persisted`` from the insert's rowcount, and ``already_present`` when the
    #     same rule was in place;
    #   - ``client_class``, since pattern and class together are the rule;
    #   - ``lease_expires_at`` as the deadline granted, so a stale `/api/config` or an
    #     off clock in the page cannot show a different one.
    return JSONResponse({"ok": True, "outcome": outcome,
                         "persisted": wrote_rule,
                         "already_present": persist and not wrote_rule,
                         "pattern": pattern,
                         "leased": lease_expires_at is not None,
                         "lease_expires_at": lease_expires_at,
                         "client_class": client_class if persist or lease else None})


def _resolve_tool_ask_request(approval_id: str, req: ResolveRequest,
                              request: Request) -> JSONResponse:
    """Answer a tool ask: the tool half of ``resolve``.

    Nothing is blocked, so there is no event, group or waiter; the decision lands on
    the row for the agent to collect. Nothing executes here either: the gateway runs
    the call only when the agent comes back and claims it (``api_tool.tool_claim``).

    Unlike the egress half, this writes its own audit row. There, the released
    waiter writes it; here there is no waiter, so without this row a human would
    grant capability with nothing in the trail."""
    action = (getattr(req, "action", "") or "").strip().lower()
    if action not in TOOL_ACTIONS:
        return JSONResponse(
            {"ok": False,
             "detail": f"action must be one of {', '.join(TOOL_ACTIONS)} for a tool "
                       f"ask, not {action!r}",
             "actions": list(TOOL_ACTIONS)}, status_code=400)
    if (getattr(req, "pattern", "") or "").strip():
        # Refused, not ignored as on a `*_once` egress action: nothing on this surface
        # persists, so a pattern means a caller that thinks it is writing policy.
        return JSONResponse(
            {"ok": False,
             "detail": "a tool ask persists nothing, so it takes no pattern"},
            status_code=400)

    actor = provenance._actor(request)
    ask = holds._get_tool_ask(approval_id)
    status = holds._resolve_tool_ask(
        approval_id, "allowed" if action == "allow" else "denied", actor)
    if status is None:
        # Lost a race, or the window elapsed before the click. The status says which,
        # since expired and already-decided ask different things of the operator.
        current = holds._get_tool_ask(approval_id)
        return JSONResponse(
            {"ok": False,
             "detail": f"not pending ({current['status'] if current else 'unknown'})",
             "status": current["status"] if current else None}, status_code=409)

    store._audit("allow" if action == "allow" else "deny", stage="tool-ask",
                 client=ask["client"], server=ask["server"], tool=ask["tool"],
                 approval_id=approval_id,
                 reason=f"tool ask {action}ed by {actor}; {ask['tool']} on "
                        f"{ask['server']} — the call runs only if the agent returns "
                        f"for it" if action == "allow" else
                        f"tool ask denied by {actor}; {ask['tool']} on "
                        f"{ask['server']} will not run")
    return JSONResponse({"ok": True, "kind": "tool", "outcome": action,
                         "status": status, "server": ask["server"],
                         "tool": ask["tool"]})


@router.get("/approvals/stream")
async def approvals_stream(request: Request) -> StreamingResponse:
    """Server-sent events: the pending payload whenever it changes, polled once a
    second, with a heartbeat otherwise so a client can detect a dead stream.

    The payload is built in a WORKER THREAD. Every listener shares one event loop
    (``main`` in app.py), so blocking here blocks ``/authorize``. And building it can
    block: it takes ``holds._LOCK`` twice (``_list_pending``, ``_saturation``), which
    ``_register_tool_ask`` holds across a SQLite write that can wait out the store's
    5 s busy timeout, and it serializes up to ``TOOL_ARGS_MAX`` per tool card.

    Change is detected on the SERIALIZED payload, so every field must be stable
    while nothing happens; a field that ticks turns this into a 1 Hz emitter
    (``holds._saturation`` on absolute timestamps)."""
    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            payload = await asyncio.to_thread(
                lambda: json.dumps(holds._pending_payload()))
            if payload != last:
                last = payload
                yield f"event: pending\ndata: {payload}\n\n"
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1.0)
    return StreamingResponse(gen(), media_type="text/event-stream")
