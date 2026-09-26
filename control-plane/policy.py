# SPDX-License-Identifier: Apache-2.0
"""Policy matching — what a stored rule means, and what a request decides to.

``_decide`` answers allow/deny/hold for a host AND the class of client asking; the
rest exist so that nothing else re-derives what a pattern matches. A leading dot is a
subdomain wildcard, and the two places that must agree about it (the matcher and the
patterns an operator may persist) sit side by side so they cannot drift.

``_decide`` reads two tables. The second, leases, holds allows that expire
(``_live_lease``), and is consulted last, so a lease loses to a block like any
other allow.

``_client_class`` maps a peer address to the class a rule is scoped to. It lives
beside the matcher that consumes it, not in the proxy that observed the address.

``_decide_tool`` at the bottom does the same job for the MCP gateway's table and
shares no code with the host matcher: it sits next to ``_decide`` because each way
the two differ is a decision. Where a tool's rule is `ask`, a pin may answer the call
in advance (``_answering_pin``).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import time

import store

# What a client is, for policy purposes: the ingress network it reached the proxy on,
# named. Why the network and not the address, and why the mapping lives here and not
# in the proxy: DESIGN.md, "Policy is scoped to a client class".
#
# Defaults mirror docker-compose.yml, and tests/test_topology.py holds them equal to
# the real subnets. An address in no listed range is UNCLASSIFIED, which matches no
# rule and is therefore held, like an unknown host.
CLIENT_CLASSES_DEFAULT = "sandbox=172.30.0.0/24,mcp=172.28.0.0/24"
# Not a valid rule scope: ``api_approvals.resolve`` refuses to persist one, because a
# rule for "whoever we could not identify" would grant every future unidentified
# client.
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

    The set a rule may be scoped to. UNCLASSIFIED is absent because
    ``_parse_client_classes`` refuses it as a name, so this validates an
    operator-supplied class without a second exclusion."""
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
        if addr.version == net.version and addr in net:
            return name
    return UNCLASSIFIED


def _normalize_host(host: str) -> str:
    """A hostname in the ONE form ``_decide`` compares and the lease table stores.

    The trailing FQDN dot goes: `evil.com.` and `evil.com` are the same destination,
    so a block of one must not miss the other. A lease is matched by EQUALITY against
    this form (``_live_lease``), so a host normalized differently on the two paths is
    a grant no request can ever equal.

    A LEADING dot survives, unlike in ``_persist_candidates``: this is whatever the
    agent asked for, and folding `.example.com` down to `example.com` would widen a
    grant to the parent name.

    Surrounding whitespace survives too, deliberately. A ``strip()`` would make a
    padded host match a rule it currently misses, which on an allow rule is a
    loosening; as it is, a padded host stays unknown and gets a card. The proxy's
    ``_forbidden_reason`` (proxies/egress/addon.py) does the same two things to a
    name."""
    return (host or "").lower().rstrip(".")


def _short_duration(seconds: float) -> str:
    """A duration for a human to read in an audit reason. Minutes and seconds only —
    nothing here is ever longer than a lease, and an hours field would be dead code."""
    whole = max(0, int(seconds))
    return f"{whole // 60}m{whole % 60:02d}s" if whole >= 60 else f"{whole}s"


def _match(host: str, pattern: str) -> bool:
    """Leading dot matches subdomains; bare entry is an exact host match."""
    if pattern.startswith("."):
        return host == pattern[1:] or host.endswith(pattern)
    return host == pattern


def _pattern_scope(pattern: str) -> str:
    """How broadly a stored pattern matches, in words, for the rules view.

    Beside the ``_match`` that implements it, so the two cannot disagree. A leading
    dot is a subdomain wildcard that looks like an ordinary hostname, so a rule for
    ``.example.com`` needs saying out loud."""
    return "host + subdomains" if pattern.startswith(".") else "exact host"


# A wildcard must keep at least this many labels. One label is either a public suffix
# or a bare name, and a standing allow rule for `.com` would end governance for that
# entire TLD in a single click.
_WILDCARD_MIN_LABELS = 2


def _persist_candidates(host: str) -> list[str]:
    """The patterns an operator may persist for a held host, NARROWEST FIRST.

    The host on an approval is chosen by the AGENT, and a leading dot is a subdomain
    wildcard (``_match``), so storing it verbatim would let a request for
    ``.example.com`` persist a rule for every subdomain. Deriving the candidates here
    makes exact-vs-wildcard an operator's choice from a bounded set, not a string the
    requester supplies.

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


# What a stored pattern may contain, per label: narrower than DNS permits, because a
# pattern outside a hostname's charset is a typo or an injection attempt, never a
# rule that decides anything. Underscore is in for service names (`_dns.example.com`).
_LABEL_RE = re.compile(r"^[a-z0-9_-]+$")
# The DNS name ceiling, as a size bound: patterns are stored, listed in full and
# compared on every decision.
_PATTERN_MAX_LEN = 253


def _normalize_pattern(pattern: str) -> str:
    """A pattern as the store holds it: trimmed, lowercased, trailing FQDN dot removed.

    One definition, because two paths write rules — a ``*_persist`` approval and
    direct operator creation — and a pattern normalized differently by one of them
    silently never matches, while reading in the rules view as policy in force.

    A LEADING dot survives: it is the subdomain-wildcard marker (``_match``), which is
    why this cannot be the ``.strip('.')`` used for a host."""
    p = (pattern or "").strip().lower()
    return "." + p[1:].rstrip(".") if p.startswith(".") else p.rstrip(".")


def _rule_error(pattern: str, action: str) -> str | None:
    """Why ``pattern`` cannot be stored as an ``action`` rule, or None if it can.
    Expects an already-``_normalize_pattern``'d pattern.

    Only an operator-supplied pattern needs this: ``_persist_candidates`` derives its
    patterns from an observed host, well-formed by construction. A malformed pattern
    is refused rather than stored inert, because an inert rule reads as policy in
    force and the operator stops asking why the host is still being held.

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


# ── leases: an allow that expires ────────────────────────────────────────────
# How long an `allow_lease` grant decides for. Not to be confused with
# ``holds.HOLD_TIMEOUT``: that bounds how long a human has to ANSWER, this how long
# the answer LASTS (so nothing here is called a "window", which is taken).
#
# An ergonomics number, not a safety floor, because `/api/egress/leases/{id}/revoke`
# closes a grant early: long enough that an agent finishes without a second card.
# Configurable, which is why the action is not named for its duration — an
# `allow_5m` button on a store set to 30 minutes would be a lie nothing catches.
LEASE_SECONDS = float(os.environ.get("CONTROL_LEASE_SECONDS", "1800"))


def _live_lease(conn, host: str, client_class: str, now: float):
    """The live lease covering ``host`` for ``client_class``, or None.

    Expiry is enforced HERE, in the read, so a row that outlives its deadline cannot
    grant whatever did or did not delete it. The sweep in ``api_approvals.resolve``'s
    lease branch only bounds the table's size.

    ``host`` is matched by equality on the ``_normalize_host`` form, never by
    ``_match``: a lease names one destination and has no wildcard to widen along.

    Longest-lived first, because that is the one whose remaining time the reason should
    name. Two live rows for one host is unreachable today (see the absent UNIQUE in
    ``store._LEASES_DDL``), so this is a defined answer to an undefined-by-construction
    case rather than a case being handled."""
    return conn.execute(
        "SELECT id, expires_at FROM leases "
        "WHERE host=? AND client_class=? AND expires_at > ? "
        "ORDER BY expires_at DESC LIMIT 1",
        (host, client_class, now)).fetchone()


def _decide(host: str, client_class: str) -> tuple[str, str]:
    """(decision, reason). Block wins over allow; an unmatched host is HELD for
    human approval rather than denied outright.

    A rule decides only for the class it was written for. A host with no rule at all
    and a host allowed only for ANOTHER class look the same to an operator sure they
    already approved it, so the hold reason names the classes that do match — which
    answers "why am I being asked this again".

    Three passes, and their order is the decision. Blocks first, then standing
    allows, then live leases:

      - A lease is an allow that expires, so it LOSES TO A BLOCK exactly as any allow
        does. That falls out of the block pass running first, and it is the invariant
        the ordering exists to guarantee: a timed grant must never become a way around
        policy an operator wrote down.
      - Leases come last because when both a rule and a lease cover a host, the
        STANDING rule is the more informative thing to record. Which of two allows
        answers a request cannot change the answer — only the audit line."""
    host = _normalize_host(host)
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT pattern, action, client_class FROM rules").fetchall()
        # Filtered ONCE, before either pass, so block-wins-over-allow is decided
        # within the class and cannot be influenced by a rule written for a
        # different one.
        mine = [r for r in rows if r["client_class"] == client_class]
        for r in mine:
            if r["action"] == "block" and _match(host, r["pattern"]):
                return "deny", f"blocked by rule ({r['pattern']} for {client_class})"
        for r in mine:
            if r["action"] == "allow" and _match(host, r["pattern"]):
                return "allow", f"allowed by rule ({r['pattern']} for {client_class})"
        # Same connection as the rules read, and reached only when no rule decided, so
        # an already-allowed request never pays for it.
        now = time.time()
        lease = _live_lease(conn, host, client_class, now)
    if lease is not None:
        # The remaining time goes IN THE REASON, so the trail explains a burst of
        # allows after one human approval without anyone reconstructing it.
        return "allow", (f"allowed by lease ({host} for {client_class}, "
                         f"{_short_duration(lease['expires_at'] - now)} left)")
    elsewhere = sorted({r["client_class"] for r in rows
                        if r["client_class"] != client_class
                        and _match(host, r["pattern"])})
    scope = (f" (matched only for: {', '.join(elsewhere)})" if elsewhere else "")
    return "hold", (f"no matching rule for client class {client_class}{scope} "
                    f"— held for approval")


# ── tool policy: what a `tool_rules` row means ───────────────────────────────
# The gateway's surface, keyed on (server, tool). Written beside ``_decide`` and
# not merged into it: the two decide different KINDS of row (control-plane/DESIGN.md,
# "Tool policy gets its own table"), and reading them together is what shows the three
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

# What a server's auth descriptor may say. 'none' is the default and the preferred
# case; 'header' is what a server forces when it will not read an env credential.
AUTH_TYPES = ("none", "header")
# The placeholder the gateway substitutes the secret into. Required in a header
# template and required to appear EXACTLY once — see ``_auth_descriptor_error``.
SECRET_PLACEHOLDER = "{secret}"  # noqa: S105 (the hole a secret goes in, not one)

# A server name is a hostname the gateway dials, so it is held to a DNS label rather
# than to anything looser: whatever is stored here must be dialable, and the failure
# mode of a name that is not is a tool surface that never answers.
_SERVER_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# A tool name is compared byte-for-byte by ``_decide_tool``, so this is a storage
# bound: the characters MCP servers use in practice, plus ':' for namespaced names. A
# tool outside it cannot have a rule, so it is denied — unreachable, not ungoverned.
# Widen this if such a server appears.
_TOOL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# An HTTP field name, narrower than the RFC's token: the characters a header an
# operator would actually configure is spelled with.
_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_TEMPLATE_MAX_LEN = 200


def _server_name_error(server: str) -> str | None:
    """Why ``server`` cannot be a server name, or None if it can.

    One place, because the name is load-bearing three times over: the gateway dials
    it, the secret path is derived from it, and ``tool_rules.server`` points at it.
    Validating it once at registration is what lets the rule endpoints check only
    that the server EXISTS."""
    if not server:
        return "server name is empty"
    if not _SERVER_RE.match(server):
        return (f"{server!r} is not a server name — expected a DNS label: lowercase "
                f"letters, digits and '-', not starting or ending with '-', at most "
                f"63 characters")
    return None


def _auth_descriptor_error(auth_type: str, header: str, template: str) -> str | None:
    """Why an auth descriptor cannot be stored, or None if it can.

    Two refusals carry real weight. A ``header`` descriptor with no
    ``{secret}`` placeholder builds a header with no credential in it, and the
    upstream answer to that is a 401 — indistinguishable, from the UI, from a policy
    problem or an expired token. And a ``none`` descriptor carrying header fields is
    configuration that says two things at once, so neither is the source of truth."""
    if auth_type not in AUTH_TYPES:
        return (f"auth type must be one of {', '.join(AUTH_TYPES)}, "
                f"not {auth_type!r}")
    if auth_type == "none":
        if header or template:
            return ("an auth type of 'none' takes no header and no template — the "
                    "server holds its own credential and the gateway injects nothing")
        return None
    if not header:
        return "a 'header' auth type needs the header name to set"
    if not _HEADER_RE.match(header):
        return (f"{header!r} is not a header name — expected letters, digits and '-', "
                f"at most 64 characters")
    if not template:
        return (f"a 'header' auth type needs a template containing "
                f"{SECRET_PLACEHOLDER}")
    if len(template) > _TEMPLATE_MAX_LEN:
        return (f"the template is {len(template)} characters; the ceiling is "
                f"{_TEMPLATE_MAX_LEN}")
    if template.count(SECRET_PLACEHOLDER) != 1:
        return (f"the template must contain {SECRET_PLACEHOLDER} exactly once, so the "
                f"gateway has one place to put the secret — {template!r} has "
                f"{template.count(SECRET_PLACEHOLDER)}")
    return None


def _tool_rule_error(tool: str, action: str) -> str | None:
    """Why ``tool`` cannot be stored as an ``action`` rule, or None if it can.

    The server half is deliberately not checked here: it is validated once at
    registration by ``_server_name_error``, and the rule endpoints check that the
    named server exists rather than re-deriving whether it could.

    There is no wildcard floor to enforce, which is the whole difference from
    ``_rule_error``. A host pattern can be broadened until it grants a TLD; a tool
    name names one tool, so breadth is not expressible and an ``allow`` here is
    exactly as wide as it reads."""
    if action not in _TOOL_ACTIONS:
        return (f"action must be one of {', '.join(_TOOL_ACTIONS)}, not {action!r}")
    if not tool:
        return "tool name is empty"
    if not _TOOL_RE.match(tool):
        return (f"{tool!r} is not a tool name — expected letters, digits, '_', '-', "
                f"'.' or ':', at most 128 characters")
    return None


# ── pinned allows: an `ask` answered in advance ──────────────────────────────
# A `tool_pins` row allows the calls to one tool whose arguments carry exact values in
# the fields it names; every other field is free. ``_decide_tool`` reads one only
# while that tool's rule is `ask`, so a pin can release only what a human could have
# released from a card (DESIGN.md, "A pinned allow is an ask answered in advance").
#
# There is no pinned DENY, and that is what makes exact equality safe. How a server
# compares values is unknown here (GitHub reads `Dockade` as `dockade`): a value that
# misses an allow falls back to a card, where one that missed a deny would run.

#: A field a pin may name, and the shape of EVERY key in a call a pin answers: an
#: ASCII identifier. Go's encoding/json matches object keys to struct fields
#: case-insensitively, folding U+017F (long s) to `s` and the Kelvin sign to `k`, so
#: `{"repo": "dockade", "Repo": "other"}` could meet a pin on `repo` while the server
#: reads the other key. Keys that are ASCII and distinct once lower-cased leave any
#: parser one key per field.
_PIN_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
#: The longest string a pin holds. A pin names a target — a repository, a method, a
#: channel — and a longer string is content, which a pin is not for.
_PIN_VALUE_MAX = 200


def _pin_value(value: object) -> str | None:
    """``value`` in the form a pin holds and compares, or None if no pin can hold it.

    Canonical JSON rather than ``==``, because Python's equality is wider than the
    wire's: ``True == 1`` and ``147 == 147.0``, while a server receives `true`, `1`
    and `147.0` as three different values. Strings, integers and booleans only: a
    float, a null, a list or an object is not an identifier."""
    if isinstance(value, (bool, int)) or (
            isinstance(value, str) and len(value) <= _PIN_VALUE_MAX):
        return json.dumps(value, ensure_ascii=False)
    return None


def _canonical_pins(pins: dict) -> str:
    """The stored form of a pin set, one canonical JSON object, so that two equal pin
    sets are one string and ``UNIQUE(server, tool, pins_json)`` means what it says."""
    return json.dumps(pins, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_pins(pins_json: str) -> dict | None:
    """A stored pin set, or None if the row does not hold one.

    Nothing writes an invalid row through the API, so every None here is a store
    edited by hand or corrupted, and such a row must never grant. An EMPTY set above
    all: it would answer every call to the tool, which is promoting the tool to
    `allow` without its rule saying so."""
    try:
        pins = json.loads(pins_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(pins, dict) or not pins:
        return None
    if not all(_PIN_FIELD_RE.fullmatch(field) and _pin_value(value) is not None
               for field, value in pins.items()):
        return None
    return pins


def _unambiguous(args: dict) -> bool:
    """Whether every parser reads the same fields out of ``args``: each key an ASCII
    identifier (``_PIN_FIELD_RE``), and no two keys equal once lower-cased."""
    keys = list(args)
    return (all(isinstance(key, str) and _PIN_FIELD_RE.fullmatch(key) for key in keys)
            and len({key.lower() for key in keys}) == len(keys))


def _pin_matches(pins: dict, args: object) -> bool:
    """Whether a call with ``args`` carries every pinned field with its exact value.

    Every key of the call is checked, not only the pinned ones: an ambiguous call
    (``_unambiguous``) gets a card."""
    if not isinstance(args, dict) or not _unambiguous(args):
        return False
    return all(field in args and _pin_value(args[field]) == _pin_value(value)
               for field, value in pins.items())


def _why_unpinnable(value: object) -> str:
    """Why ``_pin_value`` refuses ``value``, in words for the card."""
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    if value is None:
        return "null"
    if isinstance(value, float):
        return "a number with a fraction or an exponent"
    return (f"longer than {_PIN_VALUE_MAX} characters, which is content rather than "
            f"a name")


def _pin_candidates(args_json: str) -> dict:
    """What a pin may be made of for one held call, derived from the call as stored.

    ``fields`` are the ones an operator may tick, each with the value a pin would hold
    (``_pin_value``); ``unpinnable`` are the rest, each with why not. Any non-empty
    subset of ``fields`` makes a pin that answers this very call, which is what keeps
    the choice bounded: nothing is offered that the call on the card does not carry.

    ``refused`` says why no pin can be made at all. An ambiguous call is refused whole
    rather than offered the fields that do parse, because a pin made from it would
    never answer a call shaped like it."""
    try:
        args = json.loads(args_json)
    except (TypeError, ValueError):
        args = None
    if not isinstance(args, dict):
        return {"fields": [], "unpinnable": [],
                "refused": "the arguments are not an object, so there is no field to pin"}
    if not _unambiguous(args):
        return {"fields": [], "unpinnable": [],
                "refused": "a key is not a plain identifier, or two keys differ only in "
                           "case, so no pin could answer a call like this one"}
    fields, unpinnable = [], []
    for field in sorted(args):
        value = _pin_value(args[field])
        if value is None:
            unpinnable.append({"field": field, "why": _why_unpinnable(args[field])})
        else:
            fields.append({"field": field, "value": value})
    return {"fields": fields, "unpinnable": unpinnable,
            "refused": None if fields else "none of this call's arguments can be pinned"}


def _answering_pin(conn, server: str, tool: str,
                   args: object) -> tuple[int, dict] | None:
    """The oldest pin on (``server``, ``tool``) that ``args`` meets, as (id, pins), or
    None. The caller has found the tool's rule to be `ask`."""
    if not isinstance(args, dict):
        return None
    for row in conn.execute(
            "SELECT id, pins_json FROM tool_pins WHERE server = ? AND tool = ? "
            "ORDER BY id", (server, tool)):
        pins = _parse_pins(row["pins_json"])
        if pins is not None and _pin_matches(pins, args):
            return row["id"], pins
    return None


def _decide_tool(server: str, tool: str, args: object = None) -> tuple[str, str]:
    """(decision, reason) for calling ``tool`` on ``server``: allow, deny or ask.

    ``ask`` is not this path's ``hold``. An egress hold blocks the request inside the
    control plane until a human answers; a tool ask is registered and answered
    immediately, with the agent given a way back to it (DESIGN.md, "An ``ask``
    answers immediately"). This function only says which of the three a call is.

    The SERVER's state is part of the decision, not a separate gate the caller
    applies afterwards. A rule on a server nobody registered, or on one an operator
    switched off, decides nothing here — it denies. The disable switch means "the
    gateway will no longer dial this server" (``api_mcp.edit_mcp_server``), and a
    gateway that asks anyway must get a refusal from the authority rather than a
    grant it is trusted not to act on. Two lookups rather than one JOIN, because the
    REASONS differ and each names a different operator action: an unregistered server
    needs registering, a disabled one enabling, an unconfigured tool a rule.

    Every failure direction is a deny, including a stored action this code does not
    recognize. That last case is not reachable through the API — it validates on
    write — which is exactly why it is handled here: the store is a file on a volume,
    and the one thing a hand-edited or corrupted row must never do is grant.

    Under an `ask` rule, a pin that ``args`` meets answers the call, and the decision
    is `allow`. Under any other rule no pin is read: a pin cannot soften a `deny`, and
    an `allow` has nothing to answer. The claim passes no ``args``, because a human
    has already answered its call."""
    server, tool = (server or "").strip(), (tool or "").strip()
    if not server or not tool:
        return "deny", "a tool call needs both a server and a tool name"
    with store._connect() as conn:
        registered = conn.execute(
            "SELECT enabled FROM mcp_servers WHERE server = ?", (server,)).fetchone()
        if registered is None:
            return "deny", (f"no MCP server named {server!r} is registered, so nothing "
                            f"about its tools has been decided")
        if not registered["enabled"]:
            return "deny", (f"MCP server {server!r} is registered but disabled — its "
                            f"tool rules do not decide while it is switched off")
        row = conn.execute(
            "SELECT action FROM tool_rules WHERE server = ? AND tool = ?",
            (server, tool)).fetchone()
        if row is None:
            return "deny", (f"no rule for {tool!r} on {server!r} — an unconfigured "
                            f"tool is denied, not held")
        action = row["action"]
        if action not in _TOOL_ACTIONS:
            return "deny", (f"the rule for {tool!r} on {server!r} carries an unknown "
                            f"action {action!r}")
        pin = _answering_pin(conn, server, tool, args) if action == "ask" else None
    if pin is not None:
        pin_id, pins = pin
        return "allow", (f"'allow' by pin {pin_id} ({tool} on {server}, pinned: "
                         f"{', '.join(sorted(pins))})")
    return action, f"{action!r} by rule ({tool} on {server})"
