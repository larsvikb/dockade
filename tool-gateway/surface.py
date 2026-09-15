#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The curated tool surface — what the agent is shown, and under what name.

Presentation, not enforcement, and the two must not be confused. This module decides
which tools appear in `tools/list`; `policy._decide_tool` in the control plane decides
whether a call runs, per call, and it is consulted whether or not the tool was ever
presented here. DESIGN.md, "Two axes, not one": withholding a schema keeps an agent
from planning around a capability it cannot have, and that is ergonomics. A tool name
can arrive from a transcript, a `CLAUDE.md`, or text an earlier tool result injected,
so a surface that hid a tool would still have to deny the call.

Which is what makes the curation rule safe to state simply: a tool is shown when a
rule `allow`s or `ask`s it AND the server said it exists. `deny` and unruled are the
same absence — an unconfigured tool is denied, not held — and a rule naming a tool no
server exposes is dead policy that cannot be presented, since there is no schema to
present. None of those three is an error here; they are the ordinary steady state of a
server whose tools an operator has not finished ruling.

THE SNAPSHOT IS IN MEMORY AND NEVER STORED, for the reason the control plane's
inventory is not: it is a claim by a third party, it is re-derived on every reconcile,
and a stored copy is one that can be read back after the thing it described has moved.
"""
from __future__ import annotations

#: What joins a server name to a tool name in what the agent sees. The agent talks to
#: ONE MCP server — this gateway — so names from several backing servers land in one
#: namespace and `pull_request_read` on two servers would be one name serving two
#: tools, which is a policy decision made by whichever was enumerated last.
#:
#: The separator is chosen so the split back is EXACT rather than a best guess. A
#: server name is a DNS label (`discovery.check_name`), so it cannot contain `_` at
#: all; therefore the first `__` in an exposed name is always the join, whatever the
#: tool name goes on to contain. `mcp-github__pull_request_read` decodes to exactly one
#: pair. A single `_` would not have this property, and a delimiter a tool name may
#: contain (`.`, `:`, `-` — all legal in `policy._TOOL_RE`) would not either.
#:
#: It is also what Claude Code itself uses to namespace MCP tools, so the agent sees
#: `mcp__dockade__mcp-github__pull_request_read`. Long, and the length is not a cost
#: worth trading the exactness for.
SEP = "__"


def exposed_name(server: str, tool: str) -> str:
    """The one name the agent knows a tool by."""
    return f"{server}{SEP}{tool}"


def split_exposed(name: str) -> tuple[str, str] | None:
    """``(server, tool)`` back out of an exposed name, or None if it is not one.

    The inverse of ``exposed_name`` and tested as its inverse, because that pairing is
    the whole safety of the scheme: a call arriving for a name must resolve to exactly
    the server and tool the listing meant by it, or policy is being read for one tool
    and run for another.

    None rather than a guess. An agent can send any string — the name it was shown, a
    name from a transcript, a name a tool result suggested — and a decoder that
    salvaged something from a malformed one would be inventing the identity policy is
    then keyed on."""
    server, found, tool = name.partition(SEP)
    if not found or not server or not tool:
        return None
    return server, tool


def curate(roster: list[dict], enumerated: dict[str, list[dict]]) -> list[dict]:
    """The `tools/list` payload: every ruled-and-present tool, under its exposed name.

    ``roster`` is the control plane's answer — enabled servers and their rules.
    ``enumerated`` is what each server said when the gateway dialled it. BOTH are
    required for an entry, and they contribute different halves: the roster says a tool
    may be shown, the server supplies the schema that makes showing it useful.

    What is NOT copied out of a server's entry is as deliberate as what is.
    ``annotations`` stays behind: `readOnlyHint` is server-supplied, therefore
    untrusted, and a client that treats it as a reason to skip its own prompt would be
    taking a third party's word for how dangerous a call is. It has a legitimate home —
    labelling the operator's picker, where a human reads it as a claim — and the agent
    is not it. The same reasoning drops icons and any other field a server volunteers:
    everything served here is third-party text entering the agent's context, so the
    list of fields that cross is a decision rather than a passthrough.

    Sorted by exposed name so the list is stable across reconciles. An order that
    followed enumeration would churn a session's tool list for no reason an operator
    changed."""
    listing = []
    for entry in roster:
        server = entry.get("server") or ""
        actions = {rule["tool"]: rule["action"] for rule in entry.get("tools") or []}
        for tool in enumerated.get(server) or []:
            if actions.get(tool["name"]) not in ("allow", "ask"):
                continue
            listing.append({"name": exposed_name(server, tool["name"]),
                            "description": tool.get("description") or "",
                            "inputSchema": tool.get("inputSchema") or {"type": "object"}})
    return sorted(listing, key=lambda tool: tool["name"])


#: The last thing each server said it exposes, whether or not this round reached it,
#: and the curated listing built from it. Module state rather than a parameter because
#: the two halves are produced on different schedules by different threads: the
#: reconcile loop enumerates on its own timer, and a request has to be answered from
#: whatever the last enumeration saw.
#:
#: NO LOCK, and that is the design rather than an omission. There is exactly one
#: writer — the reconcile thread — and it publishes by REBINDING these names to objects
#: it has finished building, never by mutating what a reader might hold. A reader takes
#: one reference and reads a consistent object even if the next reconcile lands
#: mid-serialization. A lock would add a way for a slow request to stall the loop and
#: buy nothing.
_ENUMERATED: dict[str, list[dict]] = {}
_LISTING: list[dict] = []


def publish(roster: list[dict], results: list[dict]) -> None:
    """Record what this reconcile saw and rebuild the listing from it.

    A server that could not be enumerated KEEPS its previous tools. The alternative —
    an unreachable server emptying out of the listing — would make a restarting
    container or a briefly missing credential look to the agent like a capability that
    was withdrawn, and it would buy no safety: execution is decided per call against
    the control plane, so a tool presented from a stale list is denied exactly as it
    would be if it had never been shown. Same rule ``push_inventory`` follows for the
    same reason, one reader over.

    A server that left the ROSTER does lose its tools, and that is a different event:
    it was disabled or revoked by an operator, which is an answer rather than a
    failure to ask."""
    global _ENUMERATED, _LISTING
    enabled = {entry.get("server") for entry in roster}
    enumerated = {server: tools for server, tools in _ENUMERATED.items()
                  if server in enabled}
    for result in results:
        # `tools` absent means the dial failed — `reconcile` withholds the key rather
        # than sending an empty list, precisely so this can tell the two apart.
        if "tools" in result:
            enumerated[result["server"]] = result["tools"]
    _ENUMERATED = enumerated
    _LISTING = curate(roster, enumerated)


def listing() -> list[dict]:
    """What `tools/list` answers with, right now.

    Empty until the first successful reconcile, which is the correct cold answer: the
    gateway has not been told what exists, and a tool it cannot describe is one it
    cannot present. It is also harmless, because an empty list is not a grant."""
    return _LISTING
