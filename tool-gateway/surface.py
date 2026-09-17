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
#: `mcp__gateway__mcp-github__pull_request_read`. Long, and the length is not a cost
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


#: Prepended to the description of a tool ruled `ask`, and to no other.
#:
#: The agent has to learn this from somewhere, and the alternatives are worse. Left
#: unsaid, an `ask` tool is indistinguishable from an `allow` one until it is called,
#: so an agent cannot do the gated work first and the unblocked work while it waits,
#: and cannot tell a human up front that a task will need them. DESIGN.md's rule that
#: the retry instruction travels in the pending result is about not putting it in a
#: `CLAUDE.md` that drifts — a description is re-served from here on every `tools/list`
#: and cannot drift from the policy it describes, which is the property that rule
#: protects.
#:
#: PREPENDED rather than appended so it is not buried under a long server description.
#: The server's own text shares this string and could forge a copy, which is worth
#: noticing and not worth preventing: a tool that lies about needing approval gets
#: approval anyway, and one that lies about not needing it is still refused at the
#: call. The lie is cosmetic because the boundary is not here.
ASK_NOTICE = ("Approval required: calling this returns a pending id rather than a "
              "result, and you finish it with resume_tool_call. ")

#: The gateway's own tools, proxied from no server.
#:
#: A CATEGORY WITH ITS OWN RULE, and the rule is that a native tool must not cause an
#: ungoverned side effect. Resumption sits precisely on that line — it does cause one —
#: and is admissible only because the effect is bound to an id a human explicitly
#: approved, with arguments they read. The next native tool will not inherit that
#: property, which is why the criterion is written down rather than left to be inferred
#: from this one being safe.
#:
#: The name carries NO `__`, and that is structural rather than stylistic: every
#: proxied name is built by ``exposed_name``, which always inserts the separator, so
#: ``split_exposed`` already answers None here. The decode built to keep a call
#: resolving to one (server, tool) pair is therefore also what tells a native tool from
#: a proxied one, with no reserved server name and no second mechanism.
RESUME_TOOL = "resume_tool_call"

NATIVE_TOOLS = [{
    "name": RESUME_TOOL,
    # Blunt about the two things a model would otherwise get wrong: that this RUNS the
    # call rather than collecting a result that already exists, and that it is
    # single-use. A name like `get_result` would have implied both incorrectly, which
    # is why it is not called that.
    "description": (
        "Finish a tool call that was held for human approval. Pass the id from the "
        "pending result. If the approval was granted this RUNS the call and returns "
        "its result, and it can only be done once. If it is still pending it says so "
        "and nothing runs; if it was denied or expired it says that, and retrying "
        "will not change it."),
    "inputSchema": {
        "type": "object",
        "properties": {"approval_id": {
            "type": "string",
            "description": "The id from the pending result, copied verbatim."}},
        "required": ["approval_id"]}}]


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
            action = actions.get(tool["name"])
            if action not in ("allow", "ask"):
                continue
            description = tool.get("description") or ""
            listing.append({"name": exposed_name(server, tool["name"]),
                            "description": (ASK_NOTICE + description
                                            if action == "ask" else description),
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
_SERVERS: dict[str, dict] = {}


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
    global _ENUMERATED, _LISTING, _SERVERS
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
    # The roster itself, kept because EXECUTION needs the auth descriptor and the
    # listing does not carry one. Rebuilt outright rather than merged: unlike the
    # enumeration above, a server missing here is missing because the authority said
    # so, and holding a descriptor for a server an operator disabled would be keeping
    # the means to dial something we have been told not to.
    _SERVERS = {entry["server"]: entry for entry in roster if entry.get("server")}


def listing() -> list[dict]:
    """What `tools/list` answers with, right now.

    The gateway's own tools first, then the curated proxied ones. They are concatenated
    here rather than inside ``curate`` because they are not the same kind of thing: a
    proxied entry is a join of policy against what a server claimed, and a native one
    is neither — it is outside the per-tool policy governing everything else on this
    surface, which is exactly why it needs its own rule (see ``NATIVE_TOOLS``).

    The proxied half is empty until the first successful reconcile, which is the
    correct cold answer: the gateway has not been told what exists, and a tool it
    cannot describe is one it cannot present. It is also harmless, because an empty
    list is not a grant."""
    return NATIVE_TOOLS + _LISTING


def server_entry(server: str) -> dict | None:
    """The roster entry for ``server``, or None if it is not on the current roster.

    None is a REFUSAL rather than a lookup miss, and the caller must treat it as one.
    The entry carries the auth descriptor, so a server that is absent here cannot be
    dialled correctly — and it is absent precisely when the control plane stopped
    naming it, which is an operator disabling or revoking it. Falling back to dialling
    without credentials would turn that switch into a 401 the agent reads as a broken
    tool rather than as a closed one."""
    return _SERVERS.get(server)
