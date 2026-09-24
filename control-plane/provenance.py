# SPDX-License-Identifier: Apache-2.0
"""Provenance — who performed a privileged act, for the durable record.

Granting egress is the privileged act here, and there are two ways to perform it:
resolving a hold, and writing a standing rule outright (``create_rule``, the config-
first half of policy — the other three rule paths are all downstream of a request the
agent already made). Both record the PROVENANCE of whoever did it (``_actor``) — on
the durable approvals row (``resolved_by``) and in the audit reason for a resolution,
in the audit reason for a rule written or revoked. That is detection, not prevention:
the self-reported fields are forgeable by a host-local caller. It exists so a forged approval is at least visible in the
record afterwards, which it previously was not — an operator's click and a
scripted POST were indistinguishable once written.
"""
from __future__ import annotations

# Header the control-plane-ui relay uses to assert the BROWSER's address. The relay
# strips any client-supplied copy before setting it (see control-plane-ui/app.py),
# so a caller cannot self-report this value — but it is only as trustworthy as the
# relay, which is why _actor labels it as asserted rather than observed.
ACTOR_HEADER = "x-dockade-actor"
# Bound each recorded self-reported header (User-Agent, Origin) so a hostile one
# cannot bloat the store.
_ACTOR_UA_MAX = 120


def _actor(request) -> str:
    """Compact provenance for whoever resolved an approval, for the durable record.

    The trust level differs per field, so the labels distinguish them:
      - ``peer``   — the socket address this process itself observed. Unforgeable by
        the caller, but for anything arriving through the UI it is the
        control-plane-ui container, so it identifies the RELAY, not the human.
      - ``via-ui`` — the browser address the relay asserts (``ACTOR_HEADER``).
      - ``origin`` / ``ua`` — self-reported by the client, therefore forgeable.
        Recorded anyway because they are usually what betrays a non-browser caller.

    DETECTION, not prevention. A process running on the host can forge every
    self-reported field, and origin/Host checks in the relay are browser-enforced so
    they do not constrain it either. Preventing host-local forgery needs a human
    presence gesture the host cannot replay (WebAuthn user-presence or an
    out-of-band confirm) — see DESIGN.md. Until then the goal is that a forged
    approval leaves a trace that reads differently from an operator's click."""
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
