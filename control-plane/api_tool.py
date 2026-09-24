# SPDX-License-Identifier: Apache-2.0
"""The tool bridge — ``/tool/*``, the MCP gateway's side of the control plane.

The MCP gateway's entire conversation with the control plane, and it is three
questions rather than one: what may this call do, what is configured, and may I now
run the ask a human approved. All three are served on ``tool_app`` — its own socket
on its own network — and none of them grants anything (see ``tool_app`` in app.py).

It is the tool surface's counterpart to ``/authorize`` and deliberately not that
endpoint. The shapes diverge where the surfaces do: `/authorize` answers allow or
deny and resolves a hold INTERNALLY by blocking a worker, while this one answers
allow, deny or `ask` and hands the id back at once, because nothing waits (DESIGN.md,
"An ``ask`` answers immediately"). Sharing the endpoint would mean one handler whose
every branch forked on which surface asked.

What the gateway is TRUSTED with here, stated because it is the trust model rather
than an oversight: it reports the sandbox address it observed, exactly as the egress
proxy does on `/authorize`. A gateway that lied would misattribute an ask's client
and spend another sandbox's per-client budget. It cannot forge a DECISION, which is
the part that matters — policy is read here, and the human's answer lands on a row
the gateway never writes.
"""
from __future__ import annotations

from typing import Any

import holds
import inventory
import policy
import provenance
import store
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


class InventoryRequest(BaseModel):
    # What the gateway OBSERVED, which is a different kind of thing from the
    # operator-facing request models: those carry an operator's decision and must be
    # right, this carries a third party's claim and need only be bounded.
    # `inventory.record` is where that bounding happens — not here, because a shape
    # check is not a cap.
    servers: dict | None = None


class ToolCallRequest(BaseModel):
    """A tool call the gateway is about to make, as a question rather than a report:
    nothing has run when this arrives, which is what makes a `deny` worth anything."""
    server: str
    tool: str
    # The COMPLETE arguments, as the gateway received them. Not a summary and not a
    # subset: on the `ask` path this becomes what a human reads and what the grant is
    # bound to (``holds._canonical_args`` re-serializes it into the one canonical
    # form), so a payload trimmed here would bind an approval to something other than
    # the call. ``Any`` because it is a tool's own JSON — whatever shape that server's
    # schema allows, and this is not the place that judges it. Oversize is refused,
    # not truncated (``holds.TOOL_ARGS_MAX``).
    args: Any = None
    # The SANDBOX's address, relayed by the gateway — the same arrangement as
    # ``AuthorizeRequest.client``, where the proxy reports the peer it observed. It is
    # what the per-client ask cap counts and part of what an identical retry joins on,
    # so the gateway reporting its OWN address instead would make one shared bucket of
    # every sandbox and let one agent's asks answer another's.
    client: str | None = None


class ToolResumeRequest(BaseModel):
    """Resumption: the agent has come back for an ask a human approved.

    ``client`` is checked against the ask's own, so an id that leaked or was guessed
    from another sandbox is answered as unknown rather than claimed — both tiers share
    `sandbox-net`, and the gateway is the only thing that knows which of them is
    calling."""
    client: str | None = None


@router.post("/tool/authorize")
def tool_authorize(req: ToolCallRequest) -> dict:
    """Decide one tool call: allow, deny, or ask (registered, with an id to return
    with).

    Always a 200 and always an answer, like ``authorize``: a policy question has a
    policy answer, and a gateway should never have to read an HTTP status to learn
    what governance said. That includes the refusals — a malformed name, a server
    nobody registered, a disabled one, an oversized payload and a saturated queue all
    come back as `deny` with a reason, because every one of them is a decision the
    gateway has to act on identically.

    NOTHING IS CACHEABLE IN THIS ANSWER, and the gateway must not try: an operator's
    `deny` that waits for a TTL is not a deny (DESIGN.md, "The gateway pulls"). The
    roster below is the half that may be polled; this is the half that may not.

    What is NOT recorded here is the payload of an allowed call. The audit row carries
    the server, the tool and the decision; the arguments reach the store only when a
    human has to read them (an ``ask``, where they are the thing being approved). The
    other half of that record — what a call sent and what came back, at minimum size
    and hash, because the response is the channel that steers an agent — belongs to
    the gateway's own audit stream, ingested the way the proxy's is. Naming the gap is
    the point: it is not covered yet, and this endpoint is not where it lands."""
    # Derived here and carried into the audit row, the same discipline ``authorize``
    # follows: the class that named this caller is the one the record shows. It scopes
    # nothing — a tool rule is keyed on (server, tool), never on a client class, which
    # is exactly the conflation "Per-server identity has two different answers" keeps
    # apart. It is here because the record has to say WHICH population called, and a
    # tool ask's own row cannot: `tool_approvals` has no class column, so `resolve`
    # would have to re-derive it from an address recorded earlier — the reverse of the
    # rule that a decision is recorded under the class it was decided with.
    client_class = policy._client_class(req.client)
    server = (getattr(req, "server", "") or "").strip().lower()
    tool = (getattr(req, "tool", "") or "").strip()
    decision, reason = policy._decide_tool(server, tool)

    if decision in ("allow", "deny"):
        store._audit(decision, stage="tool-call", client=req.client,
                     client_class=client_class, server=server or None, tool=tool or None,
                     reason=f"{tool or '(no tool)'} on {server or '(no server)'}: "
                            f"{reason}")
        return {"decision": decision, "reason": reason}

    # ASK. Registered, not held: the row is the ask, the gateway takes the id back
    # immediately and the agent gets a pending result it can come back with. An
    # identical ask from the same client JOINS the pending one instead of raising a
    # second card, which is what keeps a retrying agent from filling the operator's
    # queue with copies of one question.
    ask = holds._register_tool_ask(server, tool, req.args, req.client)
    if ask.refused is not None:
        # Over a cap, or a payload too large to show a human in full. A deny, and
        # audited as one — the refusal is real and the gateway acts on it exactly as
        # it would on a policy deny. `holds._refuse_tool` has already recorded the
        # cap case in the saturation account, which is what makes a refusal that
        # raises no card visible to an operator at all.
        store._audit("deny", stage="tool-call", client=req.client,
                     client_class=client_class, server=server or None, tool=tool or None,
                     reason=f"{tool} on {server}: {ask.refused}")
        return {"decision": "deny", "reason": ask.refused}

    # 'hold' rather than a new word, and the vocabulary is the reason: `decision` is
    # shared with the audit views, the filter facet and the page's <option> list
    # (audit.KINDS), which have no compiler between them, and "deferred to a
    # human" is what `hold` already means. The reason line carries the difference that
    # matters — nothing is blocked, and the id is how the answer gets collected.
    why = (f"{tool} on {server}: registered as tool ask {ask.approval_id}"
           + (" (joined an identical ask already pending)" if ask.joined else "")
           + " — nothing is blocked; the agent resumes with the id")
    store._audit("hold", stage="tool-call", client=req.client,
                 client_class=client_class, server=server or None, tool=tool or None,
                 approval_id=ask.approval_id, reason=why)
    return {"decision": "ask", "reason": reason, "approval_id": ask.approval_id,
            # ABSOLUTE, like every other time in a payload here: a remaining-seconds
            # field would tick, which turns the SSE change-detector into a 1 Hz
            # emitter and gives the gateway a number that is stale on arrival.
            "deadline": ask.deadline, "joined": ask.joined}


@router.get("/tool/roster")
def tool_roster() -> list[dict]:
    """The enabled servers, their auth descriptors, and the standing policy for each
    one's tools — everything the gateway needs to know what to dial and what to
    present.

    POLLABLE, unlike the decision above, and that split is deliberate: this answers
    "what is configured", which the gateway needs at session start and on change, while
    execution policy is per-call and must never be cached. The gateway re-reads it
    on an interval rather than being pushed to (protocol.py says why `list_changed`
    is deliberately not advertised).

    A DISABLED server is simply absent, which is the whole meaning of the switch — the
    gateway dials what the roster names. It is not reported as disabled, because the
    gateway has nothing different to do with that fact; the operator's view of it is
    ``/api/mcp/servers``, on the management listener where configuration belongs.

    Every rule ships, `deny` rows included. Presentation is the gateway's filter
    (withholding a schema keeps an agent from planning around a capability it cannot
    have), but the two axes are separate: a `deny` is enforced at EXECUTION by the
    decision endpoint above regardless of whether the tool was ever presented, because
    a tool name can arrive from anywhere — a transcript, a `CLAUDE.md`, text injected
    by an earlier tool result.

    The auth descriptor is here and the secret is not. The descriptor is enough to
    build a request and useless to steal; the material lives in a file the gateway
    reads at a path DERIVED from the server name, so nothing in this response — or in
    the store behind it — can point one server at another's credential."""
    with store._connect() as conn:
        servers = conn.execute(
            "SELECT server, enabled, auth_type, auth_header, auth_template, "
            "created_at FROM mcp_servers WHERE enabled=1 ORDER BY server").fetchall()
        rules: dict[str, list[dict]] = {}
        for row in conn.execute(
                "SELECT server, tool, action FROM tool_rules ORDER BY server, tool"):
            rules.setdefault(row["server"], []).append(
                {"tool": row["tool"], "action": row["action"]})
    # ``api_mcp._server_view`` minus ``enabled``: every server here is enabled by
    # definition, and a constant field invites a reader to believe it varies.
    return [{"server": r["server"],
             "auth": {"type": r["auth_type"], "header": r["auth_header"],
                      "template": r["auth_template"]},
             "tools": rules.get(r["server"], [])}
            for r in servers]


@router.post("/tool/inventory")
def tool_inventory(req: InventoryRequest, request: Request) -> JSONResponse:
    """What the gateway found when it dialled each enabled server.

    THE ONLY WRITE ON THIS BRIDGE, and it stays inside the criterion that keeps the
    bridge's width honest: none of these endpoints grants. Nothing here writes a rule,
    registers a server or decides an approval — it records a claim, in memory, that
    ``policy._decide_tool`` never reads. A tool listed here is denied exactly as it was
    before the push, until a human writes a rule naming it.

    PUSHED rather than pulled, and that direction is forced. This process has no leg on
    the gateway's networks and must not be given one: dialling the agent-facing service
    from the crown jewel is the lateral edge the gateway's bind guard exists to
    prevent. So the component that can reach both ends does the reaching.

    Audits CHANGES only. A push arrives whenever the roster moves, which during an
    operator's session is often; a row per push would bury the rows worth keeping. What
    IS worth keeping is a server's surface moving — an image bump that starts exposing
    a destructive tool, with no human in the loop, is a supply-chain event and reads as
    one in the log."""
    try:
        _, moved = inventory.record({"servers": req.servers or {}})
    except inventory.InventoryError as exc:
        # A refusal the gateway can print. Not a 500: this is a well-formed request
        # carrying something out of bounds, and the sender needs to say so in its log
        # rather than retry it every poll.
        #
        # The TYPE is the point, not the 400. `InventoryError` carries a contract that
        # its message holds only the caller's own payload shape and this module's
        # constants, so serving it verbatim discloses nothing; anything else raised in
        # there is unexpected and becomes a 500 with no body, rather than having its
        # text relayed. Same arrangement as `audit.FilterError` and
        # `api_views._bad_filter`.
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    for server, line in moved:
        store._audit("observe", stage="mcp-tools", client=provenance._actor(request),
                     server=server, reason=line)
    return JSONResponse({"ok": True, "changed": len(moved)})


@router.post("/tool/asks/{approval_id}/claim")
def tool_claim(approval_id: str, req: ToolResumeRequest) -> JSONResponse:
    """Take single-use ownership of an approved ask, and hand back what to run.

    This is the resumption path: the agent returns with the id from its pending
    result, and the gateway executes only after this call succeeds. Executing HERE
    rather than at the human's click is what makes the stranded-caller property
    structural — an approved call nobody comes back for simply never runs, so a side
    effect cannot happen with nobody left to receive it.

    ONE ID, and no listing counterpart. A roster of pending asks would leak approvals
    the caller never raised — the gateway's agent-facing listener is on a network both
    sandbox tiers share — and past the leak it hands the agent a read on the
    operator's queue (DESIGN.md, "One id at a time").

    The response carries the ARGUMENTS, in the canonical form the human read and the
    digest covers. That is the point of returning them rather than having the gateway
    replay its own copy: the arguments that execute are necessarily the approved ones,
    with no second chance for a reformulated payload to ride an old grant.

    An id belonging to a DIFFERENT client is answered as unknown, not as forbidden.
    Both tiers share `sandbox-net`, so an id that leaked between them must not confirm
    it exists — and a 404 makes a guessed id and a real one indistinguishable.

    POLICY IS RE-READ HERE, and the approval alone is not enough to release the call.
    An ask is decided at one moment and redeemed at another, and everything the
    decision rested on can change in between: the server can be disabled — the
    one-click way to stop it without touching its rules (``revoke_mcp_server``) — or
    the rule can be revoked or flipped to `deny`. None of those touch `tool_approvals`,
    so without this check the switch an operator reaches for does not reach the one
    surface that releases a side effect. Same rule as ``_decide_tool``'s own: a gateway
    that asks anyway must get a refusal from the authority rather than a grant it is
    trusted not to act on."""
    ask = holds._get_tool_ask(approval_id)
    if ask is None or (ask["client"] or None) != (req.client or None):
        # One answer to the caller, two rows for the operator. Which of the two it
        # was is exactly what the 404 hides from the sandbox, and exactly what the
        # operator needs: an id claimed by the wrong client is the one sign that an
        # approval id leaked between sandboxes, and an unknown one is a probe or a
        # mangled copy. The foreign row carries the ask's server and tool so it lands
        # in that approval's own history. A PENDING claim writes nothing — it is
        # refused further down, and it is the answer an agent polls for.
        if ask is None:
            store._audit("deny", stage="tool-resume", client=req.client,
                         client_class=policy._client_class(req.client),
                         approval_id=approval_id,
                         reason=f"claim for unknown approval {approval_id} refused")
        else:
            store._audit("deny", stage="tool-resume", client=req.client,
                         client_class=policy._client_class(req.client),
                         server=ask["server"], tool=ask["tool"],
                         approval_id=approval_id,
                         reason=f"claim for approval {approval_id} refused: it was "
                                f"raised by {ask['client'] or 'no client'}, not by "
                                f"this caller — a leaked or guessed id; answered "
                                f"as unknown")
        # ``terminal`` is set here too, so the gateway branches on one field for
        # every refusal below: an id that is unknown to this caller does not become
        # known by asking again, whatever the reason it is unknown.
        return JSONResponse(
            {"ok": False, "detail": "unknown approval", "status": None,
             "spent": False, "terminal": True},
            status_code=404)
    # Derived once and used by every row below, for the reason ``tool_authorize``
    # states: the record has to say which population called. ``ask["client"]`` rather
    # than ``req.client`` only to read as what it is — the check above holds them
    # equal, and the stored one is the value the card was raised under.
    client_class = policy._client_class(ask["client"])

    # Policy BEFORE the claim, so a refusal does not consume the grant: the server may
    # be switched back on, and the approval is then still there to be redeemed. The
    # narrow window this leaves — a disable landing between this read and the UPDATE
    # below — is the same in-flight race a disable always has against a call already
    # on the wire, and closing it here would only move it.
    decision, why = policy._decide_tool(ask["server"], ask["tool"])
    if decision == "deny":
        store._audit("deny", stage="tool-resume", client=ask["client"],
                     client_class=client_class, server=ask["server"], tool=ask["tool"],
                     approval_id=approval_id,
                     reason=f"approved tool ask {approval_id} not released: {why} — "
                            f"the approval stands, the server's state does not")
        # TERMINAL, though the underlying state is reversible. An agent should not sit
        # in a retry loop on a policy refusal, and if the operator does switch the
        # server back on the right move is a fresh ``/tool/authorize`` — a new decision
        # made in the new circumstances — rather than a grant resurrected under them.
        return JSONResponse(
            {"ok": False, "detail": f"not releasable ({why})",
             "status": ask["status"], "spent": False, "terminal": True},
            status_code=409)

    # The claim itself is the atomic conditional UPDATE (`status='allowed' AND
    # claimed_at IS NULL`), so exactly one resumption of one approval ever performs
    # the side effect. Attempted BEFORE reporting a status, for the reason
    # ``_resolve_tool_ask`` orders itself the same way: reading first and then writing
    # would let two concurrent resumptions both read 'allowed' and both proceed.
    claimed = holds._claim_tool_ask(approval_id)
    if claimed is None:
        current = holds._get_tool_ask(approval_id) or ask
        # Four different refusals, and the gateway needs them apart: `pending` means
        # come back later, `denied` and `expired` are terminal and must be
        # unmistakably so — an agent that cannot tell a refusal from a delay retries
        # one forever — and an already-claimed approval is spent rather than refused,
        # which is the answer to a duplicate resumption. Spent, not "its call has run":
        # the claim is written before the call, and a claim whose answer was lost on
        # the way back spent the grant with nothing run.
        spent = current["status"] == "allowed" and current["claimed_at"] is not None
        return JSONResponse(
            {"ok": False,
             "detail": ("this approval has already been claimed, and a claim is "
                        "single-use"
                        if spent else
                        f"not claimable ({current['status']})"),
             "status": current["status"], "spent": spent,
             "terminal": spent or current["status"] in ("denied", "expired")},
            status_code=409)

    # Audited as an ALLOW at the moment capability is actually released, which is here
    # and not at the click: the resolve row records what a human decided, this one
    # records that it is being acted on. Two rows for one grant, deliberately — the
    # gap between them is exactly the window in which an approved call was never run,
    # and a record with only the first could not show it.
    store._audit("allow", stage="tool-resume", client=claimed["client"],
                 client_class=client_class, server=claimed["server"],
                 tool=claimed["tool"], approval_id=approval_id,
                 reason=f"approved tool ask {approval_id} claimed for execution; "
                        f"{claimed['tool']} on {claimed['server']} — single-use, this "
                        f"claim is the only one that can run it")
    return JSONResponse({"ok": True, "status": claimed["status"],
                         "server": claimed["server"], "tool": claimed["tool"],
                         # The canonical string, not re-parsed JSON: it is what the
                         # digest covers and what was shown, so anything that
                         # re-serializes it on the way out could hand the gateway a
                         # payload that differs from the approved one.
                         "args_json": claimed["args_json"],
                         "claimed_at": claimed["claimed_at"]})
