# SPDX-License-Identifier: Apache-2.0
"""Policy matching — what a stored rule means, and what a request decides to.

Every function here is a pure reading of a policy table: ``_decide`` answers
allow/deny/hold for a host AND the class of client asking, and the rest exist so
that nothing else has to re-derive what a pattern matches. That is the point of
the module — a leading dot is a subdomain wildcard, and the two places that must
agree about it (the matcher and the patterns an operator may persist) are written
side by side so they cannot drift.

``_decide_tool`` at the bottom does the same job for the MCP gateway's own table,
and shares no code with the host matcher on purpose: it is here to be read next to
``_decide``, because the ways the two differ are each a decision.

``_client_class`` is the second half of that: a rule is scoped to a client class,
so the mapping from an observed peer address to a class name lives here, beside
the matcher that consumes it, rather than in the proxy that observed the address.
That placement is deliberate — see the comment above ``CLIENT_CLASSES``.
"""
from __future__ import annotations

import ipaddress
import os
import re

import store

# What a client is, for policy purposes: the INGRESS NETWORK it reached the proxy
# on, named. Everything below hangs off that one choice, so the reasoning for it:
#
# A single global allowlist is the union of every client's needs, and it stops being
# least privilege the moment there is a second consumer. Since mcp-net the egress
# proxy has one: MCP server containers, which hold a credential the sandbox must not
# have and want a different, usually narrower, set of hosts. Without a class on the
# rule, every host an operator ever approved for the agent is reachable by every
# container the proxy serves — attaching a network to that proxy adds a CLIENT
# POPULATION, not just a route.
#
# The identity is the NETWORK and not the address, because the network is provable
# from the topology while an address is not: sandboxes are ephemeral and Docker hands
# out `.2` to whichever container starts first, so a rule keyed to an address would
# silently transfer to the next tenant of it (the same caveat /api/audit states about
# reading a grouped `client`).
#
# Mapped HERE rather than in the proxy, even though the proxy is what observes the
# peer address and already CIDR-matches one for the permanent lifeline. The lifeline
# is the one allow made without asking this service, so its client check has to live
# where the decision does; every other decision is the policy authority's, and the
# proxy is deliberately client-agnostic about them (see ``_is_lifeline`` in
# proxies/egress/addon.py). Keeping the classification on this side also means one
# mapping serves however many governed proxies call /authorize, rather than each one
# carrying a copy to drift.
#
# Defaults mirror docker-compose.yml; tests/test_topology.py holds them equal to the
# real subnets, so renaming a network's subnet without this fails the suite. An
# address in no listed range is UNCLASSIFIED, which matches no rule and is therefore
# held — the fail-safe direction, and the same default-deny an unknown host gets.
CLIENT_CLASSES_DEFAULT = "sandbox=172.30.0.0/24,mcp=172.28.0.0/24"
# The class of a client whose address is in none of the ranges above. Not a valid
# rule scope: ``resolve`` refuses to persist one (a rule keyed to "whoever we could
# not identify" would grant to every future unidentified client, which is the
# union-of-needs erosion this whole dimension exists to stop).
UNCLASSIFIED = "unclassified"


def _parse_client_classes(spec: str) -> tuple[tuple[str, object], ...]:
    """``name=cidr`` pairs, comma-separated, into (name, network) in listed order.

    A name may repeat, so one class can span several ranges. First match wins, which
    is why order is preserved rather than collapsed into a dict. An unparseable entry
    is dropped rather than fatal: the consequence is that its clients fall through to
    UNCLASSIFIED and are held, so a typo costs approvals rather than granting any."""
    out = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, cidr = entry.partition("=")
        name, cidr = name.strip().lower(), cidr.strip()
        if not name or not cidr or name == UNCLASSIFIED:
            continue
        try:
            out.append((name, ipaddress.ip_network(cidr, strict=False)))
        except ValueError:
            continue
    return tuple(out)


CLIENT_CLASSES = _parse_client_classes(
    os.environ.get("CONTROL_CLIENT_CLASSES", CLIENT_CLASSES_DEFAULT))


def _class_names() -> tuple[str, ...]:
    """Every configured class name, in listed order, deduplicated.

    A class may span several CIDRs, so the parsed pairs repeat names; this is the set
    a rule may legitimately be scoped TO. UNCLASSIFIED is absent by construction —
    ``_parse_client_classes`` refuses it as a name — which is what makes this usable
    as the validation list for an operator-supplied class without a second exclusion."""
    seen: list[str] = []
    for name, _ in CLIENT_CLASSES:
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def _client_class(client: str | None) -> str:
    """Which policy class the peer address ``client`` belongs to.

    Everything that cannot be placed — a missing address, a malformed one, an address
    in no configured range — is UNCLASSIFIED, and every one of those is a hold rather
    than an error. A caller that cannot be identified gets governed, not exempted."""
    if not client:
        return UNCLASSIFIED
    try:
        addr = ipaddress.ip_address(client.strip().strip("[]"))
    except ValueError:
        return UNCLASSIFIED
    for name, net in CLIENT_CLASSES:
        # Containment never crosses address families, so a v4 client simply misses a
        # v6 range and vice versa — no need to guard the version explicitly.
        if addr.version == net.version and addr in net:
            return name
    return UNCLASSIFIED


def _match(host: str, pattern: str) -> bool:
    """Leading dot matches subdomains; bare entry is an exact host match."""
    if pattern.startswith("."):
        return host == pattern[1:] or host.endswith(pattern)
    return host == pattern


def _pattern_scope(pattern: str) -> str:
    """How broadly a stored pattern matches, in words, for the rules view.

    Derived HERE, beside the ``_match`` that implements it, so the UI cannot drift
    from the real semantics. It matters because a leading dot is a SUBDOMAIN WILDCARD
    while looking like an ordinary hostname: a rule persisted for ``.example.com``
    also grants every subdomain, and nothing in the approval flow says so (the
    pattern comes verbatim from the requested host — see the rule-management item in
    DESIGN.md). Naming the scope is the cheap half of that fix."""
    return "host + subdomains" if pattern.startswith(".") else "exact host"


# A wildcard must keep at least this many labels. One label is either a public suffix
# or a bare name, and a standing allow rule for `.com` would end governance for that
# entire TLD in a single click.
_WILDCARD_MIN_LABELS = 2


def _persist_candidates(host: str) -> list[str]:
    """The patterns an operator may persist for a held host, NARROWEST FIRST.

    Exists because ``resolve`` used to store the requested host verbatim. Two facts
    made that sharper than it looks: a leading dot is a subdomain wildcard (``_match``),
    and the host on an approval is chosen by the AGENT — so a request for
    ``.example.com`` persisted a rule covering every subdomain of example.com, and
    nothing in this system revokes a rule. Deriving the candidate set here, in the
    module that defines matching, makes exact-vs-wildcard an *operator choice from a
    bounded set* instead of a string the requester supplies.

    Three at most, in increasing order of breadth — so the first is both the safest and
    the default:

      - the exact host;
      - ``.host`` — that host and its subdomains;
      - ``.<last two labels>`` — the registrable domain, which is what an operator
        usually wants when one service spreads over many hostnames.

    Anything broader is deliberately NOT offered: it needs direct policy editing, which
    is a decision rather than a click.

    Known limitation, stated rather than hidden: with no public-suffix list the
    two-label suffix of ``example.co.uk`` is ``.co.uk``, which grants far more than it
    appears to. That is why the UI shows the chosen pattern VERBATIM in a confirm step
    instead of describing it, and why a human picks."""
    exact = (host or "").strip().lower().strip(".")
    if not exact:
        return []
    out = [exact]
    try:
        ipaddress.ip_address(exact.strip("[]"))
    except ValueError:
        pass
    else:
        return out          # an IP literal has no subdomains to wildcard over
    labels = exact.split(".")
    if not all(labels):
        return out          # malformed (`a..b`): offer the exact string, invent nothing
    for depth in (len(labels), _WILDCARD_MIN_LABELS):
        # `depth > len(labels)` is the single-label case (`localhost`), where the
        # two-label suffix does not exist and taking it anyway would manufacture
        # `.localhost` — precisely the one-label wildcard the floor exists to forbid.
        if depth < _WILDCARD_MIN_LABELS or depth > len(labels):
            continue
        pattern = "." + ".".join(labels[len(labels) - depth:])
        if pattern not in out:
            out.append(pattern)
    return out


# What a stored pattern may contain, per label. Deliberately narrower than DNS
# permits: this is the charset of a hostname an agent could actually ask for, and a
# pattern outside it can only ever be a typo or an injection attempt, never a rule
# that decides anything. Underscore is in because service names (`_dns.example.com`)
# use it and a resolver will happily be asked for one.
_LABEL_RE = re.compile(r"^[a-z0-9_-]+$")
# The DNS name ceiling. A bound rather than a semantic check: patterns are stored,
# listed in full by the rules view and compared on every decision, so an unbounded
# one is a way to bloat the crown-jewel store through a governance endpoint.
_PATTERN_MAX_LEN = 253


def _normalize_pattern(pattern: str) -> str:
    """A pattern as the store holds it: trimmed, lowercased, trailing FQDN dot removed.

    The single definition of that shape, because two paths now write rules — a
    ``*_persist`` approval (whose candidates ``_persist_candidates`` already produces
    in this form) and direct operator creation — and a pattern normalized differently
    by one of them is a rule that silently never matches. ``_decide`` lowercases the
    host and strips its trailing dot before comparing, so anything this does not
    remove here is a mismatch that reads, in the rules view, as policy in force.

    A LEADING dot survives: it is the subdomain-wildcard marker (``_match``), which is
    why this cannot be the ``.strip('.')`` used for a host."""
    p = (pattern or "").strip().lower()
    return "." + p[1:].rstrip(".") if p.startswith(".") else p.rstrip(".")


def _rule_error(pattern: str, action: str) -> str | None:
    """Why ``pattern`` cannot be stored as an ``action`` rule, or None if it can.
    Expects an already-``_normalize_pattern``'d pattern.

    The validation an operator-supplied pattern needs and a persisted one does not:
    ``_persist_candidates`` derives its patterns from a host the proxy observed, so
    they are well-formed and bounded by construction. Here the string comes from a
    caller, and a rule is standing policy — a malformed one is refused outright rather
    than stored inert, because an inert rule reads as policy in force in the rules view
    and the operator stops asking why the host is still being held.

    **The wildcard floor applies to ALLOW only, and the asymmetry is the point.** A
    one-label wildcard is ``.com``: as an allow it ends governance for an entire TLD in
    one call, silently — nothing afterwards raises a hold to notice it by. As a block
    it only ever tightens, it announces itself the first time anything is denied, and
    it is revocable. Refusing both would mean the broadest blocks — the ones most worth
    writing — are the ones this endpoint cannot express."""
    if action not in ("allow", "block"):
        return f"action must be 'allow' or 'block', not {action!r}"
    if not pattern:
        return "pattern is empty"
    if len(pattern) > _PATTERN_MAX_LEN:
        return (f"pattern is {len(pattern)} characters; the DNS ceiling is "
                f"{_PATTERN_MAX_LEN}")
    wildcard = pattern.startswith(".")
    body = pattern[1:] if wildcard else pattern
    try:
        ipaddress.ip_address(body.strip("[]"))
    except ValueError:
        pass
    else:
        # An address literal has no subdomains, so a leading dot on one cannot mean
        # what it means everywhere else. Refused rather than silently accepted as an
        # exact match: `._match` would compare it as a suffix and it would match
        # nothing, which is the inert-rule failure this function exists to prevent.
        return ("an IP address has no subdomains, so a leading dot cannot be a "
                "wildcard over one") if wildcard else None
    labels = body.split(".")
    if not all(_LABEL_RE.match(label) for label in labels):
        return (f"{pattern!r} is not a hostname pattern — expected labels of letters, "
                f"digits, '-' or '_' separated by dots, optionally led by one dot for "
                f"a subdomain wildcard")
    if wildcard and action == "allow" and len(labels) < _WILDCARD_MIN_LABELS:
        return (f"{pattern!r} is a wildcard over a single label, which as an allow "
                f"grants everything under it — that needs at least "
                f"{_WILDCARD_MIN_LABELS} labels (a block may be this broad)")
    return None


def _decide(host: str, client_class: str) -> tuple[str, str]:
    """(decision, reason). Block wins over allow; an unmatched host is HELD for
    human approval (2b) rather than denied outright.

    A rule decides only for the class it was written for. Both halves of the key
    matter and they fail differently: a host with no rule at all is unknown, while a
    host allowed for ANOTHER class is a least-privilege boundary doing its job — and
    those are indistinguishable to an operator who is looking at a rule they are sure
    they already approved. So the hold reason names the classes that DO match, which
    is the whole of what "why am I being asked this again" needs answering."""
    # Strip a trailing FQDN dot, matching the proxy's relay guard: `evil.com.` and
    # `evil.com` are the same destination, so without this an explicit operator BLOCK
    # of `evil.com` misses `evil.com.` — it lands in a hold and can be re-prompted
    # indefinitely. (Stored patterns are already dot-normalized on the persist path.)
    host = (host or "").lower().rstrip(".")
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT pattern, action, client_class FROM rules").fetchall()
    # Filtered ONCE, before either pass, so block-wins-over-allow is decided within
    # the class and cannot be influenced by a rule written for a different one.
    mine = [r for r in rows if r["client_class"] == client_class]
    for r in mine:
        if r["action"] == "block" and _match(host, r["pattern"]):
            return "deny", f"blocked by rule ({r['pattern']} for {client_class})"
    for r in mine:
        if r["action"] == "allow" and _match(host, r["pattern"]):
            return "allow", f"allowed by rule ({r['pattern']} for {client_class})"
    elsewhere = sorted({r["client_class"] for r in rows
                        if r["client_class"] != client_class
                        and _match(host, r["pattern"])})
    scope = (f" (matched only for: {', '.join(elsewhere)})" if elsewhere else "")
    return "hold", (f"no matching rule for client class {client_class}{scope} "
                    f"— held for approval")


# ── tool policy: what a `tool_rules` row means ───────────────────────────────
# The gateway's surface, keyed on (server, tool). Written beside ``_decide`` and
# not merged into it: the two decide different KINDS of row (DESIGN.md, "Tool
# policy gets its own table"), and reading them together is what shows the three
# divergences below are deliberate rather than an omission.
#
#   - No wildcards. An exact (server, tool) match or no match at all.
#   - CASE-SENSITIVE, where ``_decide`` lowercases the host. A hostname is
#     case-insensitive by DNS; a tool name is an identifier its server chose, so
#     folding case here would make two distinct tools one rule.
#   - An unmatched tool is DENIED, where an unmatched host is held. A server's
#     tool set is finite and enumerable at connect time, so refusing the unknown
#     costs a configuration step rather than making the surface unusable — and it
#     makes the exposed tool list a configuration artifact rather than a mirror of
#     whatever the upstream image last added.
_TOOL_ACTIONS = ("allow", "deny", "ask")


def _decide_tool(server: str, tool: str) -> tuple[str, str]:
    """(decision, reason) for calling ``tool`` on ``server``: allow, deny or ask.

    ``ask`` is not this path's ``hold``. An egress hold blocks the request inside the
    control plane until a human answers; a tool ask is registered and answered
    immediately, with the agent given a way back to it (DESIGN.md, "An ``ask``
    answers immediately"). This function only says which of the three a call is.

    Every failure direction is a deny, including a stored action this code does not
    recognize. That last case is not reachable through the API — it validates on
    write — which is exactly why it is handled here: the store is a file on a volume,
    and the one thing a hand-edited or corrupted row must never do is grant."""
    server, tool = (server or "").strip(), (tool or "").strip()
    if not server or not tool:
        return "deny", "a tool call needs both a server and a tool name"
    with store._connect() as conn:
        row = conn.execute(
            "SELECT action FROM tool_rules WHERE server = ? AND tool = ?",
            (server, tool)).fetchone()
    if row is None:
        return "deny", (f"no rule for {tool!r} on {server!r} — an unconfigured tool "
                        f"is denied, not held")
    action = row["action"]
    if action not in _TOOL_ACTIONS:
        return "deny", (f"the rule for {tool!r} on {server!r} carries an unknown "
                        f"action {action!r}")
    return action, f"{action!r} by rule ({tool} on {server})"
