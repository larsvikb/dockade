# SPDX-License-Identifier: Apache-2.0
"""Provenance — who performed a privileged act, for the durable record.

Whoever resolves an approval or changes policy is recorded as ``_actor(request)`` in
the audit row's ``actor``, and for a resolution also on the row it writes
(``resolved_by``, a lease's ``granted_by``). It is detection, not prevention: a process on the host can
forge every self-reported field, and only a human-presence gesture the host cannot
replay would close that (DESIGN.md, "Approval provenance — detection where prevention
is not available"). Until then, a forged act should at least read differently in the
record from an operator's click.
"""
from __future__ import annotations

# The header the control-plane-ui relay sets to the BROWSER's address. The relay
# strips any client-supplied copy first (control-plane-ui/app.py), so a caller through
# it cannot self-report it; a process on the host, which reaches the API directly, can
# (SECURITY.md, "Out of scope"). It is only as trustworthy as whoever set it: hence
# ``via-ui=``, asserted rather than observed. A test pins the name against the relay's.
ACTOR_HEADER = "x-dockade-actor"
# Bound each recorded self-reported header (User-Agent, Origin) so a hostile one
# cannot bloat the store.
_ACTOR_UA_MAX = 120


def _actor(request) -> str:
    """The actor string, in labelled fields because each is trusted differently:
      - ``peer``   — the socket address this process observed. The caller cannot forge
        it, but through the UI it is the relay's address, not the human's.
      - ``via-ui`` — the browser address the relay asserts (``ACTOR_HEADER``).
      - ``origin`` / ``ua`` — self-reported, so forgeable. Recorded anyway because they
        are usually what betrays a non-browser caller.

    The ORDER is a contract: ``shortActor`` (control-plane-ui/provenance.js) keeps only
    a leading ``peer``, then ``via-ui`` straight after it, because a caller can type
    either label into its Origin or User-Agent. A new trusted field goes before
    ``origin``, and both sides change together. Among the containers only the relay
    sets ``ACTOR_HEADER``: the management API is reached through the relay, which
    strips a caller's copy, and the tool gateway, which reaches the tool listener
    directly, sets none. Through either, neither value holds a space. A process on the
    host can set the header too (SECURITY.md, "Out of scope"), and its value is then
    free text, which the cell cuts at the first whitespace."""
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
