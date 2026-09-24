# SPDX-License-Identifier: Apache-2.0
"""The approval queue and ``resolve`` — the endpoint that turns a held request into
allowed egress, and a tool ask into an approved call.

A human resolves holds over the approvals API (the SSE stream at /approvals/stream
and POST /approvals/{id}/resolve), surfaced by the separate control-plane-ui
frontend; the backend serves no HTML itself. GET /approvals is the non-streaming
form of the same list and is still served here, but the UI does not use it and the
frontend no longer relays it — see _RELAY_ROUTES in control-plane-ui/app.py.
The resolve vocabulary is a DURATION LADDER (``EGRESS_ACTIONS``):
  - allow-once / deny-once     — decide just this request
  - allow-lease                — also allow that exact host, for that client class,
    until ``policy.LEASE_SECONDS`` elapses. The rung between one request and standing
    policy, for a host an agent is about to hit repeatedly; it takes no pattern choice
    because it answers the breadth question by expiring.
  - allow-persist / deny-persist — also write a rule so future connections skip
    the hold (progressive trust; DESIGN.md "auto-approve progressively more"). WHICH
    rule is the operator's choice from a bounded set derived from the requested host
    (``policy._persist_candidates``), not a string the agent's request can supply.
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
    action: str                    # allow_once | allow_persist | deny_once | deny_persist
    # Which pattern a `*_persist` action writes. Must be one of the approval's
    # ``policy._persist_candidates``; omitted means the narrowest of them (the exact
    # host). Ignored by the two `*_once` actions, which write no rule at all.
    pattern: str | None = None


@router.get("/approvals")
def approvals() -> dict:
    return holds._pending_payload()


#: What each surface's cards may be resolved WITH. Per-surface rather than a union,
#: because the two vocabularies mean different things and an action from the wrong one
#: is a caller that has misread which card it is looking at. The tool set has no
#: `*_persist` member at all: persisting an egress decision writes a host pattern from
#: a bounded candidate set, and the argument-shaped analogue for a payload — this call,
#: then this tool with these arguments, then this tool always — is not built. Offering
#: `allow_persist` here would have to mean "allow this tool forever", which is the one
#: rung of that ladder nobody should reach by clicking the same button twice.
#:
#: The egress set is a DURATION LADDER — this request, this host for a while, this
#: pattern forever — and two absences on it are decisions:
#:
#:   - There is no `deny_lease`. An unmatched host is HELD, not denied, so a timed deny
#:     would mean "suppress the card for a while", which is a different feature
#:     (silencing a looping agent) wearing this one's name.
#:   - There is no breadth choice on `allow_lease`. The `_persist_candidates` ladder
#:     exists because a permanent rule needs an operator decision about how wide it is;
#:     a lease answers that by expiring instead, and buying breadth would double the
#:     card's decision surface for it. So a lease is always the exact host.
EGRESS_ACTIONS = ("allow_once", "allow_lease", "allow_persist",
                  "deny_once", "deny_persist")
TOOL_ACTIONS = ("allow", "deny")


@router.post("/approvals/{approval_id}/resolve")
def resolve(approval_id: str, req: ResolveRequest, request: Request) -> JSONResponse:
    """One endpoint, two surfaces, dispatched on which table holds the id.

    The queue is deliberately merged (``holds._pending_payload``), so the operator
    clicks cards of both kinds from one list and the id is all the client sends back.
    That is enough: an approval id belongs to exactly one table, so the card's kind is
    already determined by the time this is called and nothing has to be trusted from
    the request body to find it.

    The egress path below is untouched by the split. Everything tool-shaped lives in
    ``_resolve_tool_ask_request`` rather than as branches threaded through it, because
    this is the endpoint that turns a held request into allowed egress — the one whose
    reasoning is worth being able to read straight through."""
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
    # Captured BEFORE the update so the same value lands on the durable row and, via
    # that row, in the audit reason the blocked authorize() waiter writes.
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
        # The class the request was DECIDED under, read from the durable row — the
        # same discipline ``host`` follows just below, and for the same reason. The
        # rule this writes must be scoped to the population the card was raised for,
        # and the durable row is the only thing that knows that; re-deriving it from
        # the address here would be a second implementation of the classification for
        # the one caller whose answer becomes standing policy.
        client_class = row["client_class"]
        # Settle WHAT a persist writes before anything is written, and settle it from
        # the host on the durable row rather than from the request body — the caller
        # chooses among candidates, it does not supply them (see
        # policy._persist_candidates).
        pattern = None
        # Any grant that OUTLIVES this request — a standing rule or a timed lease —
        # has to be scoped to a client class, and "whoever we could not identify" is
        # not one: it would grant to every future unidentified client, which is
        # precisely the union-of-needs erosion the class dimension exists to stop.
        #
        # The lease is refused for that reason too, even though it expires. Expiry
        # bounds HOW LONG a grant lasts; it does nothing about WHO it covers, and a
        # lease with no class to scope to covers a population rather than a client.
        #
        # Refused before the UPDATE like the two branches below, so the approval stays
        # pending — the operator can still decide this request with `allow_once`, and
        # is never stuck.
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
            # the insert below is INSERT OR IGNORE against UNIQUE(pattern, client_class) — so it
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
            #
            # Scoped to THIS client class, matching the UNIQUE(pattern, client_class)
            # the insert below collides on. A rule for the same pattern in another
            # class is not a conflict — it is a different rule that decides for a
            # different client population, and refusing on it would make one class's
            # policy unwritable because another's already covered the host.
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
            # Same action already present is NOT a conflict — the policy the operator
            # is asking for is already in force. Proceed, and report below that this
            # call wrote nothing, so the card stops claiming a write it did not make.
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
            # INSIDE the `updated` guard, which is the whole of what keeps a lease
            # honest. The conditional UPDATE above is what makes exactly one of this
            # call and the waiter's timeout the decider; writing the lease before it —
            # where the persist path does its VALIDATION — would leave a live grant
            # behind a card that expired and default-denied the request that raised it.
            #
            # Expired rows are swept here rather than by a background timer: this is
            # the only place the table grows, so sweeping on it bounds the size without
            # a second mechanism to reason about. It is not what ends a lease —
            # ``policy._live_lease`` filters on the deadline, so a row that survives
            # the sweep still cannot grant.
            conn.execute("DELETE FROM leases WHERE expires_at <= ?", (now,))
            lease_expires_at = now + policy.LEASE_SECONDS
            # The host from the DURABLE row, normalized the one way `_decide` compares
            # hosts — the same discipline the pattern follows, and here it is
            # load-bearing rather than tidy: a lease is matched by equality, so a host
            # stored in any other shape is a grant no request can ever equal.
            conn.execute(
                "INSERT INTO leases(host, client_class, approval_id, created_at, "
                "expires_at, granted_by) VALUES (?,?,?,?,?,?)",
                (policy._normalize_host(row["host"]), client_class, approval_id,
                 now, lease_expires_at, actor))
        if updated and persist:
            # OR IGNORE stays, even though the conflicting case is now refused above:
            # the check and this insert are not one atomic statement, so a rule could
            # still appear between them. What changes is that the outcome is READ from
            # rowcount instead of assumed — the response reports whether a row was
            # actually written, not whether one was asked for.
            wrote_rule = conn.execute(
                "INSERT OR IGNORE INTO rules(pattern, action, source, created_at, "
                "client_class) VALUES (?,?, 'operator', ?, ?)",
                (pattern, "allow" if outcome == "allow" else "block",
                 time.time(), client_class)).rowcount > 0
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
    with holds._LOCK:
        holds._close_group_locked(approval_id)
        if approval_id in holds._PENDING_EVENTS:
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
    # ``client_class`` is echoed beside ``pattern`` because the two together are the
    # rule: the same pattern persisted from two cards is two different rules, and a
    # confirmation naming only the pattern would read identically for both.
    # ``leased`` and ``lease_expires_at`` are the lease's half of the same honesty:
    # the card reports the DEADLINE it was given rather than adding a configured
    # duration to its own clock, so a page whose `/api/config` is stale — or whose
    # machine's clock is off — cannot show a grant ending at a time it does not.
    return JSONResponse({"ok": True, "outcome": outcome,
                         "persisted": wrote_rule,
                         "already_present": persist and not wrote_rule,
                         "pattern": pattern,
                         "leased": lease_expires_at is not None,
                         "lease_expires_at": lease_expires_at,
                         "client_class": client_class if persist or lease else None})


def _resolve_tool_ask_request(approval_id: str, req: ResolveRequest,
                              request: Request) -> JSONResponse:
    """Answer a tool ask. The tool-shaped half of ``resolve``, and shorter than the
    egress half by everything that exists to release a blocked worker.

    There is no event to set, no group to close and no waiter to wake, because nothing
    is blocked: the agent already has a pending result and an id to come back with, so
    the decision simply lands on the row and waits to be collected. What this does NOT
    do is execute anything — the gateway runs the call when the agent resumes and
    claims the approval, which is what keeps an approved side effect from happening
    with nobody left to receive it.

    It DOES write its own audit row, and that is the asymmetry worth naming: on the
    egress path the released waiter writes the audit line as it returns, so ``resolve``
    itself records nothing. Here there is no waiter, so a decision that wrote no audit
    row would be a human granting capability with nothing in the trail — the one thing
    no governed path may do."""
    action = (getattr(req, "action", "") or "").strip().lower()
    if action not in TOOL_ACTIONS:
        return JSONResponse(
            {"ok": False,
             "detail": f"action must be one of {', '.join(TOOL_ACTIONS)} for a tool "
                       f"ask, not {action!r}",
             "actions": list(TOOL_ACTIONS)}, status_code=400)
    if (getattr(req, "pattern", "") or "").strip():
        # Refused rather than ignored, unlike a `*_once` egress action which shares a
        # vocabulary with the persisting ones. Nothing on this surface persists at all,
        # so a pattern here is a caller that thinks it is writing standing policy —
        # better told than quietly humoured.
        return JSONResponse(
            {"ok": False,
             "detail": "a tool ask persists nothing, so it takes no pattern"},
            status_code=400)

    actor = provenance._actor(request)
    ask = holds._get_tool_ask(approval_id)
    status = holds._resolve_tool_ask(
        approval_id, "allowed" if action == "allow" else "denied", actor)
    if status is None:
        # Lost a race, or the window elapsed between the render and the click. The
        # ask's own status says which, and saying so beats a bare conflict: expired
        # and already-decided call for different things from the operator.
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
    """Server-sent events: push the pending-approval payload whenever it changes.
    Polls SQLite once a second and emits on change, plus a periodic heartbeat so
    proxies/clients can detect a dead stream.

    BUILT IN A WORKER THREAD, which the once-a-second cadence makes look optional
    and is not. This process serves every listener from one event loop (see
    ``main`` in app.py), so anything this generator does synchronously is done
    instead of answering ``/authorize`` — the call every sandbox's egress waits on.
    And the payload is no longer the "brief indexed read" this once described: it takes
    ``holds._LOCK`` twice (``_list_pending``, ``_saturation``), and that lock is
    held by ``_register_tool_ask`` across a SQLite write, which in turn waits out
    the 5 s busy timeout when another writer has the store. Two chains, and the
    loop used to be on both. It also serializes up to ``TOOL_ARGS_MAX`` per tool
    card, which is not free either. A worker thread costs a hop per client per
    second and takes the loop off all of it.

    Change-detection is on the SERIALIZED payload, which is why every field in it
    must be stable while nothing happens — see the note in ``holds._saturation``
    about absolute timestamps. A field that ticks turns this into a 1 Hz emitter."""
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
