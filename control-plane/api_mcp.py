# SPDX-License-Identifier: Apache-2.0
"""MCP gateway policy — the servers the gateway may dial, and what each one's
tools may do.

None of this waits for the gateway, and it was built before it. Tool policy is
CONFIGURATION FIRST — an operator states what a server's tools may do before
anything calls one — which is the opposite of the egress surface, where rules
accumulate from approvals and direct creation was retrofitted (see "Tool policy
gets its own table" in DESIGN.md). So the config surface is the primary path here,
and it is built before the consumer rather than after it.

Everything here is off the AUTHORIZE listener, like everything else that grants.
"""
from __future__ import annotations

import time

import inventory
import policy
import provenance
import store
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


class ServerCreateRequest(BaseModel):
    # Registering a server, not starting one: compose declares it and a human brings
    # the container up. This names it so policy can be written about it.
    server: str
    # The auth descriptor, defaulted to the preferred case — a server holding its own
    # credential, with the gateway injecting nothing. `header` is for a server that
    # refuses that, and then both fields below are required (policy._auth_descriptor_error).
    auth_type: str = "none"
    auth_header: str | None = None
    auth_template: str | None = None
    # NOTE the field that is absent: ``enabled``. A registration cannot arrive
    # pre-enabled, because then one call both introduces a server and opens it — and
    # the audit row for "registered" would be the same row as "turned on".


class ServerEditRequest(BaseModel):
    # The TARGET state, for the reason ``RuleEditRequest`` gives: an absent field
    # meaning "leave this alone" is indistinguishable from one a caller meant to send.
    enabled: bool
    auth_type: str = "none"
    auth_header: str | None = None
    auth_template: str | None = None


class ToolRuleCreateRequest(BaseModel):
    # Which server's tool. Checked for EXISTENCE against ``mcp_servers`` rather than
    # re-validated as a name — a rule naming a server nobody registered is inert, and
    # an inert rule reads as policy in force while deciding nothing.
    server: str
    tool: str
    action: str                    # allow | deny | ask


class ToolRuleEditRequest(BaseModel):
    # ACTION only, and the two absent fields are the point. A tool rule's identity IS
    # (server, tool): changing either does not adjust a rule, it retires one and
    # writes another, which is two changes wearing one audit row. Promoting a tool
    # between deny, ask and allow is the operator's actual workflow, and that is what
    # this is.
    action: str


def _server_view(row) -> dict:
    """One `mcp_servers` row as the API serves it: `enabled` as a boolean SQLite
    cannot store, and the auth descriptor nested so it reads as the one object it is
    rather than three flat columns that happen to share a prefix."""
    return {"server": row["server"], "enabled": bool(row["enabled"]),
            "auth": {"type": row["auth_type"], "header": row["auth_header"],
                     "template": row["auth_template"]},
            "created_at": row["created_at"]}


def _descriptor(req) -> tuple[str, str, str]:
    """The auth descriptor off a request, normalized. Empty strings rather than None,
    so ``policy._auth_descriptor_error`` has one falsy value to test instead of two."""
    return ((getattr(req, "auth_type", "") or "").strip().lower(),
            (getattr(req, "auth_header", "") or "").strip(),
            (getattr(req, "auth_template", "") or "").strip())


@router.get("/api/mcp/servers")
def api_mcp_servers() -> list[dict]:
    """The registered servers, with how many tool rules each one carries.

    Unpaginated, for the reason ``api_rules`` is: this is the complete configuration
    and a truncated view of it hides exactly what the interface exists to show. The
    rule count rides along because it is what makes the list actionable — a server
    with none has nothing its tools may do, which is a fully denied surface rather
    than a broken one, and only the count says which."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT server, enabled, auth_type, auth_header, auth_template, "
            "created_at FROM mcp_servers ORDER BY server").fetchall()
        counts = {r["server"]: r["n"] for r in conn.execute(
            "SELECT server, COUNT(*) AS n FROM tool_rules GROUP BY server")}
    return [dict(_server_view(r), tool_rules=counts.get(r["server"], 0)) for r in rows]


@router.post("/api/mcp/servers")
def create_mcp_server(req: ServerCreateRequest, request: Request) -> JSONResponse:
    """Register a server so policy can be written about it. Starts nothing.

    The control plane cannot enumerate what is running — that would mean a docker
    socket on the crown-jewel container — so the operator names the server, matching
    what `mcp-servers.yml` declares. A typo is therefore possible and is not caught
    here: it produces a registered server the gateway will find nothing behind, which
    the roster reports on the gateway's next poll. That is the right failure — visible and
    granting nothing — and it is why this endpoint validates the name's SHAPE
    (``policy._server_name_error``) rather than pretending to validate its existence.

    Registered is not enabled. A fresh row is `enabled=0` with no tool rules, which is
    a server the gateway will not dial and whose every tool would be denied if it
    did — the two defaults agreeing, rather than one covering for the other."""
    actor = provenance._actor(request)
    server = (getattr(req, "server", "") or "").strip().lower()
    auth_type, header, template = _descriptor(req)

    error = policy._server_name_error(server)
    if error is None:
        error = policy._auth_descriptor_error(auth_type, header, template)
    if error is not None:
        return JSONResponse({"ok": False, "detail": error}, status_code=400)

    with store._connect() as conn:
        if conn.execute("SELECT 1 FROM mcp_servers WHERE server=?",
                        (server,)).fetchone() is not None:
            # Not a silent replace, for the reason ``create_rule`` refuses one: this
            # would overwrite an auth descriptor, and a descriptor changed by a call
            # that reported "registered" is a credential swap with no before in the
            # record. ``edit`` is where that has one.
            return JSONResponse(
                {"ok": False,
                 "detail": f"server {server!r} is already registered; nothing here "
                           f"replaces a registration. Edit it, or revoke it first."},
                status_code=409)
        conn.execute(
            "INSERT INTO mcp_servers(server, enabled, auth_type, auth_header, "
            "auth_template, created_at) VALUES (?, 0, ?, ?, ?, ?)",
            (server, auth_type, header or None, template or None, time.time()))
        conn.commit()

    store._audit("create", stage="mcp-server", server=server,
                 reason=f"MCP server {server} registered by {actor}; disabled, "
                        f"auth {auth_type}, no tools permitted until rules are written")
    return JSONResponse({"ok": True, "created": True, "server": server,
                         "enabled": False,
                         "auth": {"type": auth_type, "header": header or None,
                                  "template": template or None}},
                        status_code=201)


@router.post("/api/mcp/servers/{server}/edit")
def edit_mcp_server(server: str, req: ServerEditRequest,
                    request: Request) -> JSONResponse:
    """Enable or disable a server, and change how the gateway authenticates to it.

    One operation for both, because they are one configuration: a server enabled with
    a descriptor that does not resolve is a surface that fails as though policy
    refused it. Splitting them would also put an ordering trap where there is no need
    for one — enable-then-fix versus fix-then-enable, with a window either way.

    Disabling is the taking-back verb, and it is the blunt one: it stops the gateway
    dialling the server at all, where revoking a single tool rule returns that one
    tool to the default deny. Both directions are here because a governance plane
    that could only grant is the gap ``revoke_rule`` was built to close."""
    actor = provenance._actor(request)
    server = (server or "").strip().lower()
    enabled = bool(getattr(req, "enabled", False))
    auth_type, header, template = _descriptor(req)

    error = policy._auth_descriptor_error(auth_type, header, template)
    if error is not None:
        return JSONResponse({"ok": False, "detail": error}, status_code=400)

    with store._connect() as conn:
        row = conn.execute(
            "SELECT server, enabled, auth_type, auth_header, auth_template, "
            "created_at FROM mcp_servers WHERE server=?", (server,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown server"},
                                status_code=404)
        before = _server_view(row)
        if (before["enabled"] == enabled and row["auth_type"] == auth_type
                and (row["auth_header"] or "") == header
                and (row["auth_template"] or "") == template):
            # Asked for what is already configured — a non-write, reported as one, so
            # the audit trail does not claim the surface moved when it did not.
            return JSONResponse({"ok": True, "changed": False, **before})
        conn.execute(
            "UPDATE mcp_servers SET enabled=?, auth_type=?, auth_header=?, "
            "auth_template=? WHERE server=?",
            (1 if enabled else 0, auth_type, header or None, template or None,
             server))
        conn.commit()
        after = _server_view(conn.execute(
            "SELECT server, enabled, auth_type, auth_header, auth_template, "
            "created_at FROM mcp_servers WHERE server=?", (server,)).fetchone())

    # ONE row carrying both states, as ``edit_rule`` writes one: the two halves of a
    # change with nothing to tie them together is the shape this endpoint exists to
    # avoid. The TEMPLATE is safe to record and the secret is not in it — that is what
    # a descriptor being useless to steal buys.
    store._audit("edit", stage="mcp-server", server=server,
                 reason=f"MCP server {server} edited by {actor}; "
                        f"enabled {before['enabled']} -> {after['enabled']}, "
                        f"auth {before['auth']['type']} -> {after['auth']['type']} "
                        f"(header {before['auth']['header']} -> "
                        f"{after['auth']['header']}, template "
                        f"{before['auth']['template']} -> {after['auth']['template']})")
    return JSONResponse({"ok": True, "changed": True, **after,
                         "previous": before})


@router.post("/api/mcp/servers/{server}/revoke")
def revoke_mcp_server(server: str, request: Request) -> JSONResponse:
    """Remove a registration entirely.

    **Refused while the server still has tool rules**, rather than cascading. A
    cascade behind one POST would delete standing policy the operator cannot see from
    the button they pressed, and there is no undo in this system — the audit row would
    be the only record that a dozen decisions had been made and unmade. Deleting them
    is safe in the sense that removal never grants (an unconfigured tool is denied),
    but safe is not the same as visible, and this is the surface whose whole job is
    visibility. Disabling is the one-click way to stop a server without touching its
    policy, which is what an operator reaching for this usually wants."""
    actor = provenance._actor(request)
    server = (server or "").strip().lower()
    with store._connect() as conn:
        row = conn.execute("SELECT server, enabled FROM mcp_servers WHERE server=?",
                           (server,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown server"},
                                status_code=404)
        rules = conn.execute("SELECT COUNT(*) FROM tool_rules WHERE server=?",
                             (server,)).fetchone()[0]
        if rules:
            return JSONResponse(
                {"ok": False,
                 "detail": f"server {server!r} still has {rules} tool rule(s); "
                           f"nothing here deletes standing policy as a side effect. "
                           f"Revoke them first, or disable the server instead.",
                 "tool_rules": rules},
                status_code=409)
        conn.execute("DELETE FROM mcp_servers WHERE server=?", (server,))
        conn.commit()

    store._audit("revoke", stage="mcp-server", server=server,
                 reason=f"MCP server {server} registration revoked by {actor}; the "
                        f"gateway will no longer dial it")
    return JSONResponse({"ok": True, "server": server})


@router.get("/api/mcp/inventory")
def api_mcp_inventory() -> dict:
    """What each server last said it exposes, for an operator choosing rules.

    Served from MEMORY and empty until a gateway has pushed — including after a
    restart of this process, which is the intended cost rather than a gap. A stored
    copy would survive a server that has been gone for a week and still read as
    current; ``seen_at`` is here so a reader can tell the difference.

    OBSERVATION, not policy. Nothing in this response is permitted, and the rules that
    decide live in ``/api/mcp/servers`` and its rule endpoints. Kept separate for that
    reason and not merely for tidiness: one is a third party's claim, the other is what
    a human decided."""
    return inventory.snapshot()


@router.get("/api/mcp/rules")
def api_mcp_rules() -> list[dict]:
    """Standing tool policy — what each registered server's tools may do.

    Grouped by server, then allow before ask before deny, so the widest grants are
    read first. That is a different order from ``api_rules``, which puts blocks first
    because ``policy._decide`` lets a block win over an allow; nothing here competes,
    since a tool matches at most one rule, so the order can serve the reader instead.

    What this view CANNOT show is the tools a server exposes that have no rule — they
    are denied, and they are also the ones most needing a decision. That join is the
    caller's to make, against ``/api/mcp/inventory``: the two are served separately
    because one is what an operator decided and the other is what a server claimed,
    and merging them here would produce a single list in which those are
    indistinguishable."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, server, tool, action, source, created_at FROM tool_rules "
            "ORDER BY server, CASE action WHEN 'allow' THEN 0 WHEN 'ask' THEN 1 "
            "ELSE 2 END, tool").fetchall()
    return [dict(r) for r in rows]


@router.post("/api/mcp/rules")
def create_mcp_rule(req: ToolRuleCreateRequest, request: Request) -> JSONResponse:
    """Write a standing tool rule.

    An explicit ``deny`` is worth writing even though an unconfigured tool is already
    denied, and that is not redundancy: the row is what distinguishes "reviewed and
    refused" from "never looked at". Those are the same decision to the gateway and
    very different facts to an operator deciding what still needs attention."""
    actor = provenance._actor(request)
    server = (getattr(req, "server", "") or "").strip().lower()
    tool = (getattr(req, "tool", "") or "").strip()
    action = (getattr(req, "action", "") or "").strip().lower()

    error = policy._tool_rule_error(tool, action)
    if error is not None:
        return JSONResponse({"ok": False, "detail": error}, status_code=400)

    with store._connect() as conn:
        if conn.execute("SELECT 1 FROM mcp_servers WHERE server=?",
                        (server,)).fetchone() is None:
            # The tool-surface twin of ``create_rule``'s unknown-class refusal, and
            # the same failure it prevents: a rule for a server nobody registered
            # decides nothing while reading, in this view, as policy in force.
            return JSONResponse(
                {"ok": False,
                 "detail": f"no MCP server named {server!r} is registered; register "
                           f"it before writing policy about its tools"},
                status_code=400)
        existing = conn.execute(
            "SELECT id, action FROM tool_rules WHERE server=? AND tool=?",
            (server, tool)).fetchone()
        if existing is not None and existing["action"] != action:
            return JSONResponse(
                {"ok": False,
                 "detail": f"a rule for {tool!r} on {server!r} already exists and "
                           f"{existing['action']}s it; nothing here replaces a rule. "
                           f"Edit it, or revoke it first.",
                 "conflict": {"id": existing["id"], "server": server, "tool": tool,
                              "action": existing["action"]}},
                status_code=409)
        if existing is not None:
            return JSONResponse({"ok": True, "created": False, "already_present": True,
                                 "id": existing["id"], "server": server, "tool": tool,
                                 "action": action})
        rule_id = conn.execute(
            "INSERT INTO tool_rules(server, tool, action, source, created_at) "
            "VALUES (?,?,?, 'operator', ?)",
            (server, tool, action, time.time())).lastrowid
        conn.commit()

    store._audit("create", stage="tool-policy", server=server, tool=tool,
                 reason=f"tool rule created by {actor}; {tool} on {server} now "
                        f"{action}s")
    return JSONResponse({"ok": True, "created": True, "already_present": False,
                         "id": rule_id, "server": server, "tool": tool,
                         "action": action, "source": "operator"}, status_code=201)


@router.post("/api/mcp/rules/{rule_id}/edit")
def edit_mcp_rule(rule_id: int, req: ToolRuleEditRequest,
                  request: Request) -> JSONResponse:
    """Move a tool between deny, ask and allow — the operator's actual workflow.

    Only the action changes; see ``ToolRuleEditRequest`` for why the identity does
    not. There is no uniqueness clash to handle for the same reason: the key is
    untouched, so this cannot collide with another row."""
    actor = provenance._actor(request)
    action = (getattr(req, "action", "") or "").strip().lower()

    with store._connect() as conn:
        row = conn.execute(
            "SELECT server, tool, action FROM tool_rules WHERE id=?",
            (rule_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown rule"},
                                status_code=404)
        error = policy._tool_rule_error(row["tool"], action)
        if error is not None:
            return JSONResponse({"ok": False, "detail": error}, status_code=400)
        if row["action"] == action:
            return JSONResponse({"ok": True, "changed": False, "id": rule_id,
                                 "server": row["server"], "tool": row["tool"],
                                 "action": action})
        conn.execute("UPDATE tool_rules SET action=? WHERE id=?", (action, rule_id))
        conn.commit()

    store._audit("edit", stage="tool-policy", server=row["server"], tool=row["tool"],
                 reason=f"tool rule edited by {actor}; {row['tool']} on "
                        f"{row['server']} was {row['action']}, now {action}")
    return JSONResponse({"ok": True, "changed": True, "id": rule_id,
                         "server": row["server"], "tool": row["tool"],
                         "action": action, "previous": {"action": row["action"]}})


@router.post("/api/mcp/rules/{rule_id}/revoke")
def revoke_mcp_rule(rule_id: int, request: Request) -> JSONResponse:
    """Remove one tool rule, returning that tool to the default.

    Which is a DENY, not a hold — the one place this differs from revoking an egress
    rule, where the host reverts to being held for approval. Revoking here can
    therefore only narrow, whatever the rule said, and the record names the
    destination rather than leaving it to be inferred from the action removed.

    There is no seed to refuse, as ``revoke_rule`` refuses one: no file seeds this
    table, so every row in it is an operator's."""
    actor = provenance._actor(request)
    with store._connect() as conn:
        row = conn.execute(
            "SELECT server, tool, action FROM tool_rules WHERE id=?",
            (rule_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown rule"},
                                status_code=404)
        conn.execute("DELETE FROM tool_rules WHERE id=?", (rule_id,))
        conn.commit()

    store._audit("revoke", stage="tool-policy", server=row["server"], tool=row["tool"],
                 reason=f"tool rule revoked by {actor}; {row['tool']} on "
                        f"{row['server']} was {row['action']}, now unconfigured and "
                        f"therefore denied")
    return JSONResponse({"ok": True, "id": rule_id, "server": row["server"],
                         "tool": row["tool"], "action": row["action"]})
