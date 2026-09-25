# SPDX-License-Identifier: Apache-2.0
"""MCP gateway policy — the servers the gateway may dial, and what each one's
tools may do. On the management listener only, like everything that grants.

Tool policy is CONFIGURATION FIRST: an operator states what a server's tools may do
before anything calls one. Egress is the opposite, with rules accumulating from
approvals (control-plane/DESIGN.md, "Tool policy gets its own table"), so here the
config surface is the primary path rather than a retrofit.
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
    # Registers a server, and starts nothing: compose declares it and a human brings
    # the container up.
    server: str
    # The auth descriptor. The default is the preferred case: the server holds its
    # own credential and the gateway injects nothing. `header` is for a server that
    # refuses that, and needs both fields below (``policy._auth_descriptor_error``).
    auth_type: str = "none"
    auth_header: str | None = None
    auth_template: str | None = None
    # No ``enabled``: a registration that arrived enabled would introduce a server and
    # open it in one call, under one audit row.


class ServerEditRequest(BaseModel):
    # The TARGET state, as with ``api_egress.RuleEditRequest``: an absent field meaning
    # "leave this alone" looks the same as one a caller meant to send.
    enabled: bool
    auth_type: str = "none"
    auth_header: str | None = None
    auth_template: str | None = None


class ToolRuleCreateRequest(BaseModel):
    server: str
    tool: str
    action: str                    # allow | deny | ask


class ToolRuleEditRequest(BaseModel):
    # ACTION only. A tool rule's identity is (server, tool): changing either retires
    # one rule and writes another, which is two changes under one audit row.
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

    Unpaginated, like ``api_egress.api_rules``: a truncated view of the complete
    configuration hides what it exists to show. The count is what tells a server with
    no rules, whose every tool is denied, from a broken one."""
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

    The operator names the server, matching `mcp-servers.yml`, because enumerating
    what runs would need a docker socket on this container. So only the name's SHAPE
    is checked (``policy._server_name_error``). A typo registers a server with nothing
    behind it, which the inventory reports as unreachable once it is enabled: visible,
    and granting nothing.

    Registered is not enabled. A fresh row is disabled with no tool rules, so the
    gateway will not dial it and would deny every tool if it did."""
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
            # Not a silent replace, as ``api_egress.create_rule`` refuses one: it would
            # overwrite an auth descriptor, a credential swap with no before in the
            # record. ``edit_mcp_server`` records one.
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

    One operation, because it is one configuration: enabled with a descriptor that
    does not resolve, a server fails as though policy refused it, and two calls would
    leave that window open in either order. Disabling is the blunt way back: the
    gateway stops dialling the server at all, where revoking a tool rule returns one
    tool to deny."""
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

    # ONE row carrying both states, as ``api_egress.edit_rule`` writes. The template
    # is safe to record because the secret is never in it.
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

    **Refused while the server still has tool rules**, rather than cascading. Deleting
    them could not grant (an unconfigured tool is denied), but it would unmake
    standing policy the operator cannot see from the button, with no undo and only an
    audit row as the record. Disabling stops a server without touching its policy,
    which is usually what is wanted."""
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

    From MEMORY (``inventory`` says why), so empty until a gateway has pushed,
    including after a restart; ``seen_at`` says how fresh each entry is. OBSERVATION,
    not policy: nothing here is permitted. It stays apart from the rules because one
    is a server's claim and the other an operator's decision."""
    return inventory.snapshot()


@router.get("/api/mcp/rules")
def api_mcp_rules() -> list[dict]:
    """Standing tool policy — what each registered server's tools may do.

    Grouped by server, then allow, ask, deny, so the widest grants read first.
    ``api_egress.api_rules`` puts blocks first because a block beats an allow; a tool
    matches at most one rule, so nothing competes here.

    Tools with no rule are absent. They are denied, and they are the ones most in
    need of a decision; the caller finds them by joining against
    ``/api/mcp/inventory``."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, server, tool, action, source, created_at FROM tool_rules "
            "ORDER BY server, CASE action WHEN 'allow' THEN 0 WHEN 'ask' THEN 1 "
            "ELSE 2 END, tool").fetchall()
    return [dict(r) for r in rows]


@router.post("/api/mcp/rules")
def create_mcp_rule(req: ToolRuleCreateRequest, request: Request) -> JSONResponse:
    """Write a standing tool rule.

    An explicit ``deny`` is worth writing though an unconfigured tool is already
    denied: the row tells "reviewed and refused" from "never looked at", which the
    gateway treats alike and an operator does not."""
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
            # Checked for EXISTENCE, as ``api_egress.create_rule`` refuses an unknown
            # class: a rule for a server nobody registered decides nothing while
            # reading as policy in force.
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

    Only the action changes (``ToolRuleEditRequest``), so the key is untouched and
    cannot collide with another row."""
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
    """Remove one tool rule, returning that tool to the default: DENY, where a revoked
    egress rule returns its host to a hold. So revoking here can only narrow, and the
    audit reason names where the tool ends up. Nothing seeds this table, so there is
    no seed to refuse, as ``api_egress.revoke_rule`` does."""
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
