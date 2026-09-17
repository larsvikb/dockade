#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The MCP wire, on the agent's side of the gateway.

JSON-RPC 2.0 over the streamable-HTTP transport, one POST per message. This module is
the protocol and nothing else: it takes a parsed message and the curated listing and
returns the message to send back. It opens no socket, reads no policy and dials
nothing, which is what lets the whole surface be tested without a server, a control
plane or a network.

STATELESS, and that is a choice with consequences worth stating. No `Mcp-Session-Id`
is issued, so there is no session table to grow, nothing to expire and nothing an
agent can hold across a restart — the transport permits this, and every question this
gateway answers is already answered from the control plane or from the last reconcile
rather than from what a particular client said earlier. The one thing it costs is the
server→client stream: `notifications/tools/list_changed` needs a channel this shape
does not have, so the `listChanged` capability is NOT advertised. Advertising a
capability that is never delivered is worse than a client polling.

What is deliberately absent from the served surface, because each is a way in rather
than a missing feature: no `resources/*` and no `prompts/*` (the gateway brokers tool
calls, and a resource read would be a second, unruled path to a server's data), no
`completion/*`, and no `logging/*`. Each is answered as method-not-found, which is the
honest answer — the capability list says they are not there, and a client that asks
anyway learns the same thing from the error.
"""
from __future__ import annotations

#: Protocol revisions this gateway will speak, newest first. Negotiation is: echo what
#: the client asked for if it is one of these, otherwise answer with the newest and let
#: the client decide whether it can live with that — which is what the spec asks a
#: server to do, and it fails in the safe direction, since a client that cannot is
#: expected to disconnect rather than proceed on an assumption.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

#: What the agent sees this server called, and the prefix the client puts on every tool
#: (`mcp__gateway__…`). It names the SURFACE rather than the system: the client renders
#: a call as "Calling <name>", and `dockade` there claimed the whole system was being
#: invoked when what is being called is one governed door into it.
SERVER_NAME = "gateway"

#: Advertised in `serverInfo` because the spec requires the field. Nothing reads it and
#: nothing should — a client branching on a gateway version would be coupling to an
#: implementation detail of the thing whose whole job is to be substitutable.
SERVER_VERSION = "0.1.0"

# JSON-RPC's own codes. Spelled out rather than inlined: three of the four are returned
# from more than one place, and a transposed digit in an error code is the kind of bug
# that surfaces as a client behaving oddly rather than as anything failing.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _result(message_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


def parse_error() -> dict:
    """The answer to a body that is not JSON at all.

    Separate from ``handle`` because there is nothing to hand it: the id lives inside
    the message that could not be parsed, so the reply carries a null one. This is the
    one response whose id is null by necessity rather than by choice."""
    return _error(None, PARSE_ERROR, "request body is not valid JSON")


def negotiate(requested: object) -> str:
    """The protocol revision to answer `initialize` with."""
    return requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]


def handle(message: object, listing: list[dict], execute=None) -> dict | None:
    """One message in, one message out — or None when there is nothing to say.

    ``execute`` is what runs a tool call — ``(name, arguments) -> CallToolResult`` —
    and it is INJECTED rather than imported so this module keeps no I/O in it: every
    branch below is reachable from a test with no control plane, no server and no
    socket. It also carries the caller's identity, bound by whoever supplies it, so
    nothing here has to be trusted to pass a client address along correctly.

    Absent, `tools/call` is refused. That is the honest answer for a gateway whose
    executing half is not wired up, and it is the same answer an unruled tool gets.

    None means the message was a NOTIFICATION, and the caller answers with an empty
    202. Replying to a notification is a protocol violation, so this returns None for
    every one of them including the ones it does not recognize: an unknown notification
    is something to ignore, and an error carrying the id it does not have would be
    malformed twice over.

    Every other refusal is a JSON-RPC error inside a 200, not an HTTP status. A client
    should never have to read a transport code to learn what the protocol said — the
    same rule the control plane's ``/authorize`` follows for the same reason."""
    if isinstance(message, list):
        # Batching was removed in the 2025-06-18 revision, and supporting it would mean
        # deciding what a partial failure inside a batch means for a surface where one
        # member can be a governed side effect. Refused rather than unpacked.
        return _error(None, INVALID_REQUEST, "JSON-RPC batches are not accepted")
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "a JSON-RPC message is an object")

    method = message.get("method")
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(message.get("id"), INVALID_REQUEST,
                      "a JSON-RPC message needs jsonrpc=\"2.0\" and a method")
    if "id" not in message:
        return None

    message_id = message["id"]
    params = message.get("params")
    params = params if isinstance(params, dict) else {}

    if method == "initialize":
        return _result(message_id, {
            "protocolVersion": negotiate(params.get("protocolVersion")),
            # `tools` and nothing else. The empty object is the honest shape: tools are
            # served, and none of the optional sub-capabilities is — see the module
            # docstring on why `listChanged` is absent while the transport is stateless.
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})

    if method == "ping":
        return _result(message_id, {})

    if method == "tools/list":
        # No `nextCursor`, which is how this transport says "that was all of them". The
        # list is one server's worth of curated entries held in memory; paginating it
        # would add a cursor whose only job is to be discarded.
        return _result(message_id, {"tools": listing})

    if method == "tools/call":
        if execute is None:
            # Answered as an error rather than as a result with `isError`, because the
            # two mean different things to an agent: a result says the call ran and
            # failed, and with no executor nothing ran and nothing could.
            return _error(message_id, INTERNAL_ERROR,
                          "this gateway presents tools but does not execute them yet")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error(message_id, INVALID_PARAMS,
                          "tools/call needs a tool name")
        arguments = params.get("arguments")
        # A JSON-RPC error is reserved for a malformed REQUEST. Everything the executor
        # decides — a deny, an unknown tool, an unreachable server — comes back as a
        # result, because each carries a reason the agent can act on and an error
        # carries none it can use.
        return _result(message_id, execute(name, arguments))

    return _error(message_id, METHOD_NOT_FOUND, f"{method!r} is not served here")
