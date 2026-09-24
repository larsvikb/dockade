# SPDX-License-Identifier: Apache-2.0
"""Provenance — who performed a privileged act, for the durable record.

Whoever resolves an approval or changes policy is recorded as ``_actor(request)`` in
the audit row, and for a resolution also on the row it writes (``resolved_by``, a
lease's ``granted_by``). It is detection, not prevention: a process on the host can
forge every self-reported field, and only a human-presence gesture the host cannot
replay would close that (DESIGN.md, "Approval provenance — detection where prevention
is not available"). Until then, a forged act should at least read differently in the
record from an operator's click.
"""
from __future__ import annotations

# The header the control-plane-ui relay sets to the BROWSER's address. The relay
# strips any client-supplied copy first (control-plane-ui/app.py), so a caller cannot
# self-report it, but it is only as trustworthy as the relay: hence ``via-ui=``,
# asserted rather than observed. A test pins the name against the relay's.
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
        are usually what betrays a non-browser caller."""
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
