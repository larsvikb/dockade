# SPDX-License-Identifier: Apache-2.0
"""The tool bridge — ``/tool/*``, the MCP gateway's side of the control plane.

Three questions and one report: may this call run, what is configured, may I now run
the ask a human approved, and what the gateway found on each server. All of it is on
``tool_app``, its own socket on its own network, and none of it grants (see
``tool_app`` in app.py).

The counterpart of ``/authorize``, and separate from it because the answers differ:
that one resolves a hold by blocking a worker, this one answers `ask` with an id at
once, because nothing waits (DESIGN.md, "An ``ask`` answers immediately").

The gateway is trusted to report the sandbox address it observed, as the egress proxy
is on ``/authorize``. One that lied could misattribute an ask and spend another
sandbox's per-client budget. It cannot forge a decision: policy is read here, and the
human's answer lands on a row the gateway never writes.
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
    # What the gateway observed: a third party's claim, which need only be bounded.
    # ``inventory.record`` bounds it; a shape check here is not a cap.
    servers: dict | None = None


class ToolCallRequest(BaseModel):
    """A tool call the gateway is about to make, as a question rather than a report:
    nothing has run when this arrives, which is what makes a `deny` worth anything."""
    server: str
    tool: str
    # The COMPLETE arguments, as the gateway received them. On the `ask` path they are
    # what a human reads and what the grant binds to (``holds._canonical_args``), so a
    # trimmed payload would bind the approval to a different call. Oversize is
    # refused, not truncated (``holds.TOOL_ARGS_MAX``).
    args: Any = None
    # The sandbox's address as the gateway observed it, like
    # ``AuthorizeRequest.client``. The per-client ask cap counts it and an identical
    # retry joins on it, so the gateway's own address would pool every sandbox.
    client: str | None = None


class ToolResumeRequest(BaseModel):
    """Resumption: the agent has come back for an ask a human approved."""
    client: str | None = None


@router.post("/tool/authorize")
def tool_authorize(req: ToolCallRequest) -> dict:
    """Decide one tool call: allow, deny, or ask (registered, with an id to return
    with).

    Always a 200 and always an answer, like ``authorize``. A malformed name, an
    unregistered or disabled server, an oversized payload and a saturated queue all
    come back as `deny` with a reason, since the gateway acts on each the same way.

    NOTHING HERE IS CACHEABLE: an operator's `deny` that waits for a TTL is not a deny
    (DESIGN.md, "The gateway pulls"). The roster is the half that may be polled.

    The audit row has the server, the tool and the decision, not the arguments; those
    reach the store only on an ask, where they are what the human approves. How the
    call ended comes later from the gateway's outcome stream. What it sent and what
    came back are not recorded yet (DESIGN.md, "The response is the channel")."""
    # For the record only: a tool rule is keyed on (server, tool), never on a class
    # (DESIGN.md, "Per-server identity has two different answers"). Derived now,
    # because nothing later can: `tool_approvals` has no class column.
    client_class = policy._client_class(req.client)
    server = (getattr(req, "server", "") or "").strip().lower()
    tool = (getattr(req, "tool", "") or "").strip()
    # A pin answers only what a card could have, and a card refuses a payload past
    # the ceiling rather than show it in part (``holds._register_tool_ask``), so the
    # pins are not offered one either.
    showable = len(holds._canonical_args(req.args)) <= holds.TOOL_ARGS_MAX
    decision, reason = policy._decide_tool(server, tool,
                                           req.args if showable else None)

    if decision in ("allow", "deny"):
        store._audit(decision, stage="tool-call", client=req.client,
                     client_class=client_class, server=server or None, tool=tool or None,
                     reason=f"{tool or '(no tool)'} on {server or '(no server)'}: "
                            f"{reason}")
        return {"decision": decision, "reason": reason}

    # ASK. Registered, not held: the gateway takes the id back at once. An identical
    # ask from the same client joins the pending one rather than raising a second
    # card, so a retrying agent cannot fill the queue with copies of one question.
    ask = holds._register_tool_ask(server, tool, req.args, req.client)
    if ask.refused is not None:
        # Over a cap, or too large to show a human in full: a deny, audited as one.
        # ``holds._refuse_tool`` has already counted a cap refusal for the saturation
        # banner, the only place a refusal that raises no card shows.
        store._audit("deny", stage="tool-call", client=req.client,
                     client_class=client_class, server=server or None, tool=tool or None,
                     reason=f"{tool} on {server}: {ask.refused}")
        return {"decision": "deny", "reason": ask.refused}

    # `hold`, not a new word: the vocabulary (audit.KINDS) is shared with the audit
    # filters and the page's <option> list, with no compiler between them. The reason
    # line says what differs: nothing is blocked.
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

    Pollable, unlike ``tool_authorize``: configuration may be cached, a decision may
    not. The gateway re-reads it on an interval (protocol.py says why `list_changed`
    is not advertised). A disabled server is simply absent; the gateway has nothing to
    do with the fact.

    Every rule ships, `deny` included. The gateway uses them to withhold schemas, but
    a `deny` is enforced at EXECUTION by ``tool_authorize`` whether or not the tool was
    presented, because a tool name can arrive from anywhere — a transcript, a
    `CLAUDE.md`, an earlier tool result.

    The auth descriptor is here and the secret is not. The gateway reads the secret
    from a path DERIVED from the server name, so nothing in this response, or in the
    store behind it, can point one server at another's credential."""
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

    The only write on this bridge, and it grants nothing: it records a claim, in
    memory, that ``policy._decide_tool`` never reads. A tool listed here stays denied
    until a human writes a rule naming it.

    Pushed, not pulled, because this process must have no leg on the gateway's
    networks: dialling the agent-facing service from here is the lateral edge the
    gateway's bind guard exists to prevent.

    Audits CHANGES only. A row per push would bury the one worth keeping: a server's
    surface moving, such as an image bump that starts exposing a destructive tool."""
    try:
        _, moved = inventory.record({"servers": req.servers or {}})
    except inventory.InventoryError as exc:
        # A 400 rather than a 500, so the gateway logs it instead of retrying every
        # poll. Only this TYPE is relayed: its message holds only the caller's payload
        # shape and inventory's constants, the contract `audit.FilterError` has for
        # `api_views._bad_filter`. Anything else raised is a 500 with no body.
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    for server, line in moved:
        store._audit("observe", stage="mcp-tools", actor=provenance._actor(request),
                     server=server, reason=line)
    return JSONResponse({"ok": True, "changed": len(moved)})


@router.post("/tool/asks/{approval_id}/claim")
def tool_claim(approval_id: str, req: ToolResumeRequest) -> JSONResponse:
    """Take single-use ownership of an approved ask, and hand back what to run.

    The gateway executes only after this succeeds, not at the human's click, so an
    approved call nobody comes back for never runs. One id per call and no listing
    (DESIGN.md, "One id at a time").

    The response carries the ARGUMENTS, in the canonical form the human read and the
    digest covers, so what executes is necessarily what was approved rather than the
    gateway's own copy.

    POLICY IS RE-READ HERE; the approval alone does not release the call. Between the
    decision and the claim the server may have been disabled (``edit_mcp_server``)
    or the rule revoked or flipped to `deny`, and none of those touch
    `tool_approvals`. Without this check the switch an operator reaches for would not
    reach the one surface that releases a side effect."""
    ask = holds._get_tool_ask(approval_id)
    if ask is None or (ask["client"] or None) != (req.client or None):
        # Unknown and foreign get the same 404: both tiers share `sandbox-net`, so an
        # id leaked between them must not confirm it exists. The operator gets two
        # different rows, because a foreign claim is the one sign an id leaked, and
        # it carries the ask's server and tool so it joins that approval's history. A
        # PENDING claim writes nothing; it is refused further down, and agents poll it.
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
        # ``terminal`` here too, so the gateway branches on one field for every
        # refusal: an id unknown to this caller does not become known by asking again.
        return JSONResponse(
            {"ok": False, "detail": "unknown approval", "status": None,
             "spent": False, "terminal": True},
            status_code=404)
    # The check above holds ``ask["client"]`` equal to ``req.client``; the stored one
    # is what the card was raised under.
    client_class = policy._client_class(ask["client"])

    # Policy BEFORE the claim, so a refusal does not consume the grant. A disable
    # landing between this read and the UPDATE below is the same race a disable
    # always has with a call already on the wire; closing it here would only move it.
    decision, why = policy._decide_tool(ask["server"], ask["tool"])
    if decision == "deny":
        store._audit("deny", stage="tool-resume", client=ask["client"],
                     client_class=client_class, server=ask["server"], tool=ask["tool"],
                     approval_id=approval_id,
                     reason=f"approved tool ask {approval_id} not released: {why} — "
                            f"the approval stands, the server's state does not")
        # TERMINAL, though the state is reversible: if the server comes back on, the
        # right move is a fresh ``/tool/authorize`` under the new circumstances, not
        # an agent retrying this one in a loop.
        return JSONResponse(
            {"ok": False, "detail": f"not releasable ({why})",
             "status": ask["status"], "spent": False, "terminal": True},
            status_code=409)

    # The claim is one conditional UPDATE (`status='allowed' AND claimed_at IS NULL`),
    # so exactly one resumption performs the side effect. Reading the status first
    # would let two concurrent resumptions both read 'allowed' and both proceed.
    claimed = holds._claim_tool_ask(approval_id)
    if claimed is None:
        current = holds._get_tool_ask(approval_id) or ask
        # `pending` means come back later; `denied` and `expired` must read as
        # terminal, or an agent retries them forever. An approval already claimed is
        # SPENT, which is not "its call has run": the claim is written before the call,
        # and a claim whose answer was lost spent the grant with nothing run.
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

    # An ALLOW here, when capability is released, as well as the resolve row when a
    # human decided. The gap between the two rows is an approved call that never ran,
    # which a record with only the first could not show.
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
