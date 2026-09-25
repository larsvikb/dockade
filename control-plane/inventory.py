#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""What each MCP server says it exposes. Held in MEMORY, and deliberately nowhere else.

The control plane cannot discover tools: it has no leg on `mcp-net`, and enumerating
what runs would mean a docker socket on the crown-jewel container. The gateway is the
only component that ever talks to a server, so this is what it reports back — the
operator-facing half of "The control plane learns a server's tools from the gateway"
in DESIGN.md.

NOT PERSISTED, and every reason points the same way. It is derived data, rebuildable
by asking the servers again, so a restart costs one poll. A stored copy would go stale
against a server that has been down for a week while still reading as authoritative.
And it is SERVER-AUTHORED text: putting it in the store would put it in `make backup`,
which is the operator's own decisions and nothing else.

THE INVARIANT THIS MODULE EXISTS UNDER: nothing here is an input to
``policy._decide_tool``. A tool in this map is not permitted, not registered, and not
half-registered — it is a thing a server claimed, and claims decide nothing.

So the trust gradient runs the other way from everywhere else in this process:
`mcp_servers` and `tool_rules` hold what an OPERATOR decided and must be right; this
holds what a SERVER said and need only be bounded.
"""
from __future__ import annotations

import threading
import time

import policy

#: Bounds, enforced HERE rather than trusted from the sender. The gateway is a sibling
#: we wrote, but it relays what a third-party container answered, and "the gateway
#: would not send that" is not a bound. With tool names capped by ``policy._TOOL_RE``
#: at 128 characters, these two numbers are what make the memory this can occupy a
#: number someone can state rather than a function of what a server felt like saying.
MAX_SERVERS = 64
MAX_TOOLS_PER_SERVER = 256

class InventoryError(ValueError):
    """A push the gateway must be TOLD about rather than have silently dropped.

    Same contract as ``audit.FilterError``: **the message is served VERBATIM to the
    caller**, so interpolate only the caller's own payload's shape and this module's
    constants. TYPED rather than a bare ``ValueError`` so ``api_tool`` can catch exactly
    the errors written to be read, and let anything unexpected become a 500 with no
    body."""


_LOCK = threading.Lock()
#: server -> {"tools": [name, ...], "read_only": [name, ...], "unnameable": int,
#:            "enumerated": bool, "status": str, "seen_at": float}
_SEEN: dict[str, dict] = {}


def _clean_tools(raw: list) -> tuple[list[str], list[str], int]:
    """The tool names worth keeping, the read-only subset, and how many were dropped.

    A name outside ``policy._TOOL_RE`` is dropped rather than stored, because a rule
    cannot be written for it — ``_decide_tool`` compares byte-for-byte against a column
    that charset bounds, so such a tool is unreachable rather than ungoverned, and
    listing it in a picker would offer a choice that cannot be made. The COUNT is kept
    so the surface can say so instead of quietly showing fewer tools than the server
    has.

    ``readOnlyHint`` is carried and the rest of the annotation block is not. It is the
    one field a human picking rules actually reads, and it is a HINT — the server's own
    claim about itself, which is why it lives here with the rest of what a server says
    and never in `tool_rules`."""
    names: list[str] = []
    read_only: list[str] = []
    unnameable = 0
    for tool in raw[:MAX_TOOLS_PER_SERVER]:
        name = (tool.get("name") or "").strip() if isinstance(tool, dict) else ""
        if not policy._TOOL_RE.match(name):
            unnameable += 1
            continue
        if name in names:
            continue
        names.append(name)
        if (tool.get("annotations") or {}).get("readOnlyHint"):
            read_only.append(name)
    unnameable += max(0, len(raw) - MAX_TOOLS_PER_SERVER)
    return sorted(names), sorted(read_only), unnameable


def changes(before: dict, after: dict) -> list[tuple[str, str]]:
    """One ``(server, line)`` per server whose exposed tools moved, for the audit.

    The name is returned BESIDE the line rather than left to be read out of it. The
    line names the server too — it has to, being what a human reads — but the caller
    writes the audit table's ``server`` column from this, and re-deriving a column by
    parsing a sentence is exactly what that column exists to stop.

    NAMES, not prose. A tool name is charset-bounded by ``policy._TOOL_RE``; a
    description is unbounded text a third party wrote, and this is the one part of the
    inventory that becomes durable. Naming what appeared is the whole value — an image
    bump that starts exposing a destructive tool, with no human in the loop, is a
    supply-chain event and the reason this is audited at all.

    Only CHANGES. A push on every roster poll saying the same thing is not a record, it
    is a way to make the record unreadable."""
    lines = []
    for server in sorted(set(before) | set(after)):
        if server not in after:
            # LEAVING THE ROSTER IS NOT A SURFACE CHANGE. The roster carries enabled,
            # registered servers, so a server vanishing from it means an operator
            # disabled or revoked it — an action already audited on the management
            # listener, with their identity on the row. Repeating it here, worded as
            # though the SERVER had dropped its tools, is a second row that says
            # something untrue about a third party.
            continue
        was = set(before.get(server, {}).get("tools", ()))
        now = set(after.get(server, {}).get("tools", ()))
        if was == now:
            # Also the case that keeps a never-enumerated server quiet. Enabling one
            # with a bad credential gives it an empty tool list and no previous entry,
            # so both sides are empty and it falls out here — rather than reaching the
            # first-sighting line below and claiming it "exposes 0 tools", which would
            # say the server offers nothing when the truth is nobody could ask. This
            # check is the only guard for that case; nothing below repeats it.
            continue
        if server not in before:
            # FIRST SIGHTING, which is not a change and must not read as one. This map
            # is in memory, so it empties on every restart of this process — and a
            # restart naming a server's whole surface as newly appeared is a supply-
            # chain alarm for an event that did not happen. Recorded, because knowing
            # when the gateway first reported a server is worth a row; counted rather
            # than enumerated, because the names are only interesting as a DELTA and
            # the inventory itself is where the full list is read.
            lines.append((server, f"{server} first seen exposing {len(now)} tool(s)"))
            continue
        added, gone = sorted(now - was), sorted(was - now)
        parts = []
        if added:
            parts.append(f"now exposes {', '.join(added)}")
        if gone:
            parts.append(f"no longer exposes {', '.join(gone)}")
        lines.append((server, f"{server} {' and '.join(parts)}"))
    return lines


def record(payload: dict) -> tuple[dict, list[tuple[str, str]]]:
    """Replace the inventory wholesale. Returns the stored view and what changed.

    A SNAPSHOT, not a delta. The gateway computes the whole picture every pass, so a
    delta protocol would add sequence numbers and reconciliation on both sides to save
    nothing — and a snapshot is self-healing: a dropped push leaves the previous one
    standing until the next, and a server dropping off the roster disappears without
    needing a deletion verb.

    Refuses rather than truncates when there are too many servers. Truncating would
    make the surface quietly wrong about the one thing it is for, and "too many" here
    means something is broken rather than something is busy."""
    servers = payload.get("servers")
    if not isinstance(servers, dict):
        raise InventoryError("inventory payload has no 'servers' object")
    if len(servers) > MAX_SERVERS:
        raise InventoryError(
            f"{len(servers)} servers reported, over the {MAX_SERVERS} cap — refusing "
            f"the whole payload rather than storing a partial picture")

    now = time.time()
    # ONE hold of the lock from reading the old map to replacing it. Read outside it,
    # two overlapping pushes would both diff against the same old map: each would
    # report the other's change again, and the slower one would overwrite the newer
    # picture. Holding it also means a reader (``snapshot`` takes it too) never
    # observes a half-built inventory.
    with _LOCK:
        # Shallow is enough: the bodies are replaced below, never mutated.
        previous = dict(_SEEN)
        fresh: dict[str, dict] = {}
        for name, body in servers.items():
            if (policy._server_name_error(str(name)) is not None
                    or not isinstance(body, dict)):
                continue
            name = str(name)
            if "tools" in body:
                tools, read_only, unnameable = _clean_tools(body.get("tools") or [])
            else:
                # ABSENT IS NOT EMPTY, and the gateway takes trouble to keep them
                # apart: it omits the list entirely when it could not ask. Reading
                # that as "this server exposes nothing" would turn every brief
                # credential problem into an audit row saying the server lost its
                # whole surface, and would empty the picker while the operator is
                # trying to work out why. So the last known surface stands, and
                # `status` is what says it may be stale.
                was = previous.get(name, {})
                tools = list(was.get("tools", ()))
                read_only = list(was.get("read_only", ()))
                unnameable = was.get("unnameable", 0)
            fresh[name] = {
                "tools": tools,
                "read_only": read_only,
                "unnameable": unnameable,
                # Whether the tool list above is an OBSERVATION or merely the absence
                # of one. A server registered with a bad credential has never been
                # enumerated, and its empty list means "we could not ask" rather than
                # "it offers nothing" — a distinction the audit and the picker both
                # need.
                "enumerated": "tools" in body or bool(was.get("enumerated")),
                # Carried verbatim from the gateway, capped: "secret missing" and
                # "unreachable" are the states an operator most needs, and they are
                # the ones that otherwise surface as a 401 that reads like a policy
                # problem.
                "status": str(body.get("status") or "")[:200],
                "seen_at": now,
            }
        moved = changes(previous, fresh)
        _SEEN.clear()
        _SEEN.update(fresh)
    return fresh, moved


def snapshot() -> dict:
    """The inventory as the management API serves it.

    A DEEP-ENOUGH copy, because the caller renders it while the gateway may be
    replacing it — and because a handle on the live map would let one reader mutate
    what every other reader sees. A shallow ``dict(body)`` is not enough: it shares
    the tool LISTS, which are the part a caller is most likely to sort or filter in
    place."""
    with _LOCK:
        return {server: {key: list(value) if isinstance(value, list) else value
                         for key, value in body.items()}
                for server, body in _SEEN.items()}
