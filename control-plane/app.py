# SPDX-License-Identifier: Apache-2.0
"""
Control plane — governance authority for the governed data-plane proxies.

Step 2b: policy + audit + **hold-for-approval**. The management app the agent can
never reach — it lives on internal networks the sandbox is not attached to.
Governed proxies call it on the control path to authorize connections; unknown
requests are held for a human, who approves/rejects them in a live UI.

This file is the HTTP surface — the two apps, the request models, the routes and
the process entry point. The machinery each route drives lives beside it, one
module per concern, and every call below is written qualified (``policy._decide``,
``holds._reserve_hold``) so a reader can see which one is being asked:

  - ``store``   — SQLite: the schema, the audit write, the seed. The crown jewel.
  - ``policy``  — what a rule pattern matches, and what a host decides to.
  - ``holds``   — the in-process registry a blocked request waits on, its caps
    and its duplicate grouping.
  - ``ingest``  — draining the egress proxy's own audit file into the store.

They import in that order and never back: nothing under this file imports this
file.

The authorize flow (one call from the proxy, `POST /authorize`):
  - host matches a BLOCK rule            -> deny   (audited)
  - host matches an ALLOW rule           -> allow  (audited)
  - no matching rule                     -> HOLD: record a pending approval and
    BLOCK the request until a human resolves it or CONTROL_HOLD_TIMEOUT elapses
    (-> default-deny). The proxy only ever sees allow/deny; the hold is internal.
    A request identical to one already held JOINS it rather than raising a second
    approval, so a retrying agent produces one card and one decision — which is
    also why one click can release several blocked requests (see holds._group_key).

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
    (``policy._persist_candidates``), not a string the agent's request can supply.

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
registry is in-process (holds.py). That constraint is also why the two listeners
above are two sockets in ONE process rather than two services: a held /authorize
and the `resolve` that releases it must share memory, precisely because they must
not share a socket.

No egress: this service sits on control-net and authorize-net, both internal. It
must never be given an internet route — it is pure management state (the crown
jewel).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid

import holds
import ingest
import policy
import store
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

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


# ── provenance ──────────────────────────────────────────────────────────────

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
    # ``policy._persist_candidates``; omitted means the narrowest of them (the exact
    # host). Ignored by the two `*_once` actions, which write no rule at all.
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
    store._init_db()
    # A held request cannot survive a restart (its blocked connection is gone),
    # so any 'pending' rows from a previous process are stale — expire them.
    with store._connect() as conn:
        conn.execute(
            "UPDATE approvals SET status='expired', resolved_at=? "
            "WHERE status='pending'", (time.time(),))
        conn.commit()
    seeded = store._seed_if_empty()
    if seeded:
        print(f"control-plane: seeded {seeded} allow rules from {store.SEED_PATH}",
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
    decision, reason = policy._decide(req.host)

    if decision in ("allow", "deny"):
        # Every decision is audited — no governed path bypasses the log (CLAUDE.md).
        store._audit(decision, stage=req.stage, host=req.host, port=req.port,
                     proto=req.proto, client=req.client, method=req.method,
                     url=req.url, reason=reason)
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
                     proto=req.proto, client=req.client, method=req.method,
                     url=req.url, reason=slot.refused)
        return AuthorizeResponse(decision="deny", reason=slot.refused)
    approval_id, event = slot.approval_id, slot.event

    if not slot.joined:
        now = time.time()
        with store._connect() as conn:
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
    store._audit("hold", stage=req.stage, host=req.host, port=req.port,
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
    # The card's remaining window, not a fresh one — see holds._PENDING_DEADLINE.
    # Every waiter on a card therefore wakes at the same instant, which is what lets
    # them race harmlessly for the expiry UPDATE below.
    event.wait(max(0.0, slot.deadline - time.time()))
    with store._connect() as conn:
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
        holds._close_group(approval_id)
    holds._release_hold(approval_id)

    # Carry the resolver's provenance (recorded by resolve()) into the audit reason,
    # so the log answers "who granted this egress" and not merely "a human did".
    actor = (status_row["resolved_by"] if status_row else None) or "actor unrecorded"
    # Whether the decision also wrote STANDING POLICY belongs in the audit line: a
    # one-off and a persist are the same allow for this request and very different
    # afterwards, and the log said nothing about which had happened. From the durable
    # ``mode`` column, so it reports what was recorded rather than what was asked for.
    # (Naming the pattern too would need a column on ``approvals``, which has no
    # migration step — see the NOTE below ``store._init_db``. The rule itself is
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
    store._audit(final, stage=req.stage, host=req.host, port=req.port,
                 proto=req.proto, client=req.client, method=req.method, url=req.url,
                 reason=why)
    return AuthorizeResponse(decision=final, reason=why)


# ── approvals (human-facing) ────────────────────────────────────────────────

@app.get("/approvals")
def approvals() -> dict:
    return holds._pending_payload()


@app.post("/approvals/{approval_id}/resolve")
def resolve(approval_id: str, req: ResolveRequest, request: Request) -> JSONResponse:
    if req.action not in ("allow_once", "allow_persist", "deny_once", "deny_persist"):
        return JSONResponse({"ok": False, "detail": "bad action"}, status_code=400)
    outcome = "allow" if req.action.startswith("allow") else "deny"
    persist = req.action.endswith("persist")
    # Captured BEFORE the update so the same value lands on the durable row and, via
    # that row, in the audit reason the blocked authorize() waiter writes.
    actor = _actor(request)

    with holds._LOCK:
        event = holds._PENDING_EVENTS.get(approval_id)
    if event is None:
        # Already resolved, expired, or unknown — nothing to wake.
        return JSONResponse(
            {"ok": False, "detail": "not pending (expired or already resolved)"},
            status_code=409)

    with store._connect() as conn:
        row = conn.execute(
            "SELECT host FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown"}, status_code=404)
        # Settle WHAT a persist writes before anything is written, and settle it from
        # the host on the durable row rather than from the request body — the caller
        # chooses among candidates, it does not supply them (see
        # policy._persist_candidates).
        pattern = None
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
    must be stable while nothing happens — see the note in ``holds._saturation``
    about absolute timestamps. A field that ticks turns this into a 1 Hz emitter."""
    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            payload = json.dumps(holds._pending_payload())
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
    with store._connect() as conn:
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

    Ordered blocks first, because block wins over allow in ``policy._decide`` — the
    listing reads in precedence order rather than alphabetically. Read-only on purpose:
    this change makes policy visible, it does not add mutation (see the rule-management
    item in DESIGN.md for what revocation still needs)."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, pattern, action, source, created_at FROM rules "
            "ORDER BY action DESC, pattern").fetchall()
    # `id` is served so revocation can key on it. Not cosmetic: patterns are the one
    # field a revoke could plausibly key on instead, and they carry a live
    # normalization gap (``policy._match`` lowercases but does not strip a trailing
    # FQDN dot — see DESIGN.md), so a pattern-keyed delete inherits every such mismatch
    # and can miss the row the operator is looking at. An id cannot.
    return [dict(r, scope=policy._pattern_scope(r["pattern"])) for r in rows]


@app.get("/api/config")
def api_config() -> dict:
    """The settings the UI cannot behave correctly without knowing. Read-only, and
    non-secret by construction — nothing here decides anything.

    Just the hold window today, and that one is load-bearing: a held request BLOCKS the
    agent and default-denies after ``holds.HOLD_TIMEOUT``, so a card that cannot say
    how long is left cannot distinguish hold-for-approval from a slow deny. Sent rather
    than hardcoded in the page, so the number the operator sets is the number they
    see."""
    return {"hold_timeout": holds.HOLD_TIMEOUT}


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
    with holds._LOCK:
        total = int(holds._SATURATION["count"])  # type: ignore[arg-type]
        acked = max(0, min(int(req.count), total))
        if acked > int(holds._SATURATION["acked"]):  # type: ignore[arg-type]
            holds._SATURATION["acked"] = acked
            holds._SATURATION["acked_ts"] = time.time()
        return {"ok": True, "acknowledged": holds._SATURATION["acked"],
                "rejections": total}


@app.post("/api/rules/{rule_id}/revoke")
def revoke_rule(rule_id: int, request: Request) -> JSONResponse:
    """Remove one operator-created rule. The other half of a governance plane that
    could grant but never take back.

    **Seed rules are refused, and refused HERE rather than merely hidden in the UI.**
    Their source of truth is ``policies/egress-allowlist.txt``, a reviewed file under
    version control, and a click that left the file disagreeing with the store would
    make the file a lie. It also removes a trap for free: ``store._seed_if_empty``
    re-reads that file whenever the rules table is empty, so a store whose every rule
    could be revoked would silently resurrect the whole seed allowlist on the next
    restart. With seed rules undeletable the table cannot reach that state.

    The consequence of retiring a TRANSITIONAL seed entry (npm, PyPI, GitHub — see
    DESIGN.md) is therefore that it happens as a migration shipped beside the code
    that replaces it, not as an operator action. That is the right shape for it: it is
    a versioned, reviewed change to a declared policy.

    Deletion rather than a tombstone. The audit row below IS the history — a rules
    table carrying dead rows would have to be filtered by every reader of it,
    including ``policy._decide``, which is the one place a mistake is unrecoverable.

    Provenance is recorded exactly as ``resolve`` records it, and for the same reason:
    editing standing policy is more consequential than any single egress decision, and
    until now nothing recorded that it had happened at all. Detection, not prevention
    — the fields are forgeable by a host-local caller (see ``_actor``)."""
    actor = _actor(request)
    with store._connect() as conn:
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
    store._audit("revoke", stage="policy", host=row["pattern"],
                 reason=f"{row['action']} rule revoked by {actor}; {row['pattern']} is "
                        f"now unknown and will be held for approval")
    return JSONResponse({"ok": True, "pattern": row["pattern"],
                         "action": row["action"]})


@app.get("/status", response_class=PlainTextResponse)
def status() -> str:
    with store._connect() as conn:
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
    if ingest.DRAIN_INTERVAL > 0:
        drain = asyncio.create_task(ingest._audit_drain_loop())
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
