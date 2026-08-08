# SPDX-License-Identifier: Apache-2.0
"""
Egress proxy policy + audit addon.

Governed egress proxy for the Claude sandbox(es): a CONNECT-level forward proxy
that permits only what policy allows and records every decision. It does NOT
decrypt TLS — HTTPS is tunnelled untouched (``data.ignore_connection = True`` in
``tls_clienthello``), so the sandbox needs no custom CA. We govern by name
because the CONNECT authority and the TLS SNI both name the destination, which
is all domain-level control needs — and, unlike the v1 IP firewall, naming the
host is what closes the shared-CDN / domain-fronting gap.

Step 2a — this proxy is now a CONTROL-PLANE CLIENT. Instead of reading a static
allowlist file, it asks the control plane per connection:

    POST {EGRESS_CONTROL_PLANE_URL}/authorize {host, ...} -> {decision, reason}

That one call is both the policy decision AND the audit record (the control
plane writes the row as it decides), so policy and audit share the round-trip
the proxy already makes — and no client-side cache, so an operator edit applies
to the very next connection.

But NOT every decision is made by that call. The relay guard, the port gate, the
SNI anti-fronting check and the permanent-lifeline allow are all decided HERE,
locally, precisely so they cannot be widened (or stalled) by the control plane —
and a control-plane outage produces local fail-closed denials by definition. Those
never reach the central store on the authorize path, so each audit line carries
``central``: true when the control plane already wrote the row itself, false when
this local stream is the only record of it. The control plane tails this file
(read-only) and ingests the ``central: false`` lines, which is what keeps its
"recent decisions" view a record of *decisions* rather than of round-trips. The
flag is the join key for that: it is what stops the ingest double-counting every
governed request. See ``_drain_egress_audit`` in control-plane/app.py.

Fail-closed, with one deliberate exception:
  - The **permanent lifeline** hosts (``EGRESS_PERMANENT_HOSTS`` — the Anthropic
    API/auth endpoints) are allowed by a LOCAL check *before* the control plane
    is consulted, so a control-plane outage never bricks the agent's own API.
  - Every other host depends on the control plane. If it is unreachable or slow
    (timeout), the request is DENIED and audited locally — governed egress fails
    closed when the policy authority is down, which is the intended posture.

Control-plane isolation: this proxy is the only component on BOTH sandbox-net and
a control network, so it — not network segmentation — is what keeps the agent off
the control plane. Before any policy/permanent/port check it hard-refuses any
destination that names a control-plane host or resolves into either control subnet
(``_forbidden``), a guard that no rule, approval, or port change can widen.

The control network this proxy joins is authorize-net, which carries ONLY
``/authorize``. That is deliberate and it is what bounds the damage here: this
guard is best-effort against DNS rebinding (the resolve branch re-resolves, so a
name can change answers between the check and the dial), so the design assumes it
can be beaten and arranges for the far side to be worth little. Even a total
bypass reaches a listener that can answer a policy question and nothing else — it
cannot approve a held request, read the audit store, or see a pending approval,
because the management API is served on a different port bound to a different
address on a network this container is not attached to. See DESIGN.md.
The same guard hard-blocks the private / special-use ranges (cloud metadata /
link-local, loopback, RFC1918 and friends — see ``PRIVATE_CIDRS``), so the proxy
can never be turned into an SSRF pivot to the instance-metadata service or the
internal net. Because that promise is about a DESTINATION and not about a string,
the guard normalizes before matching (case, trailing FQDN dot, IPv6 brackets) and
folds the IPv6 forms that carry an embedded IPv4 address down to the address they
actually dial — otherwise ``[::ffff:169.254.169.254]`` is the same destination
under a spelling no v4 blocklist recognizes (see ``_embedded_ipv4``).

An unknown host is HELD (2b): the control plane blocks the /authorize response
until a human approves/rejects it or its hold window elapses. To the proxy this
is just a slow allow/deny — the connection waits, then proceeds or gets a 403.
That is why CONTROL_TIMEOUT is long (see below); it does not weaken fail-closed.

The call runs in a worker thread (``asyncio.to_thread`` + stdlib ``urllib``), so
it never blocks mitmproxy's event loop and the egress image needs no extra
dependency. Port gating stays local (this is an HTTP/S proxy; a raw CONNECT to a
non-443 port would carry arbitrary TCP regardless of host policy).

Later: MITM / body-level audit is a per-domain option — enabled by NOT setting
``ignore_connection`` for a chosen domain (needs our CA on the box).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import time
import urllib.request
from typing import NamedTuple

from mitmproxy import http, tls

AUDIT_PATH = os.environ.get("EGRESS_AUDIT_LOG", "/var/log/egress/audit.jsonl")

# Control plane: where policy decisions + audit go. One call per connection.
# Default port is 8091 — the AUTHORIZE listener on authorize-net (compose sets this
# explicitly). 8090 is the management surface, on a network this proxy has no route
# to; pointing here would fail closed, but the default should name the right port.
CONTROL_PLANE_URL = os.environ.get(
    "EGRESS_CONTROL_PLANE_URL", "http://control-plane:8091").rstrip("/")
AUTHORIZE_URL = CONTROL_PLANE_URL + "/authorize"
# The control plane may HOLD an unknown host while a human decides (2b), so a
# single /authorize call can legitimately block for up to the control plane's
# hold window. This timeout must therefore EXCEED that window (default there is
# 120s). It is a read timeout, so the fail-closed cases are unaffected: an
# unreachable control plane fails immediately (connection refused / DNS), not by
# waiting this out — only a genuine hold (or a hung control plane) waits long.
CONTROL_TIMEOUT = float(os.environ.get("EGRESS_CONTROL_TIMEOUT", "130"))

# Permanent lifeline — allowed locally, BEFORE the control plane is consulted,
# so an outage of the control plane can never sever the agent's own API/auth.
# Mirrors the PERMANENT block of policies/egress-allowlist.txt. Same match
# semantics as the control plane: a leading dot matches subdomains.
def _hosts(env: str, default: str) -> tuple[str, ...]:
    return tuple(h.strip().lower() for h in os.environ.get(env, default).split(",")
                 if h.strip())

PERMANENT_HOSTS = _hosts("EGRESS_PERMANENT_HOSTS",
                         "api.anthropic.com,claude.ai,platform.claude.com")

# This is an HTTP(S) egress proxy, so the policy has a port intent even though
# host policy names only hosts: CONNECT tunnels are for HTTPS, plain forward
# requests are for HTTP. Gate on it — otherwise an allowed host is reachable on
# ANY port over a raw TCP tunnel (SSH, arbitrary TLS services, ...), widening
# the channel well past what "allow example.com" is meant to grant. Override for
# the rare host that legitimately serves HTTP(S) on a non-standard port.
def _ports(env: str, default: str) -> frozenset[int]:
    return frozenset(int(p) for p in os.environ.get(env, default).split(",") if p.strip())

ALLOWED_CONNECT_PORTS = _ports("EGRESS_CONNECT_PORTS", "443")  # HTTPS tunnels
ALLOWED_HTTP_PORTS = _ports("EGRESS_HTTP_PORTS", "80")         # plain HTTP

logger = logging.getLogger("egress")

# ── Forbidden destinations (control plane / control networks) ─────────────────
# This proxy is the ONE component attached to BOTH sandbox-net and a control
# network, so it — not network segmentation — is what actually keeps the agent off
# the control plane: were the proxy to relay a connection onto authorize-net, the
# agent would be talking to the control plane directly. We therefore refuse,
# BEFORE any policy / permanent-lifeline / port check, any destination that names
# a control-plane host or resolves into either control subnet. This guard is
# checked first and is never consulted against policy, so it cannot be widened by
# a rule, a human approval, a change to the port allowlist, or a public name whose
# DNS is pointed at a control subnet.
#
# What a bypass here now costs is bounded by the API-surface split (see the module
# docstring): the reachable listener serves /authorize alone, so the worst outcome
# is an unauthorized policy QUERY rather than an approval of the agent's own held
# request. That is the point of the split — this guard is racy by nature, so the
# far side is arranged to be worth little rather than the guard being trusted.
def _parse_cidrs(env: str, default: str) -> tuple:
    nets = []
    for c in os.environ.get(env, default).split(","):
        c = c.strip()
        if not c:
            continue
        try:
            nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            logger.warning("ignoring invalid forbidden CIDR %r", c)
    return tuple(nets)

# Defaults mirror docker-compose.yml. BOTH control networks are listed, and the
# second one is the one that matters operationally: this proxy is attached to
# authorize-net (172.29.0.0/24) and NOT to control-net (172.31.0.0/24), so
# authorize-net is where a relayed connection could actually land. control-net
# stays listed because a guard that only blocks what is currently routable would
# quietly stop covering the control plane the moment the topology changes, and
# because being unroutable is a property of the compose file rather than of this
# process — this file cannot verify it and should not assume it.
FORBIDDEN_CIDRS = _parse_cidrs(
    "EGRESS_FORBIDDEN_CIDRS", "172.31.0.0/24,172.29.0.0/24")
FORBIDDEN_HOSTS = _hosts("EGRESS_FORBIDDEN_HOSTS", "control-plane,control-plane-ui")

# Special-use / private ranges that are NEVER a legitimate egress target for the
# sandbox, hard-blocked exactly like control-net. This proxy's default route is
# egress-net — a masquerading bridge with a path to the cloud instance-metadata
# service (169.254.169.254 -> cloud credentials), the Docker host, and the host's
# internal network — so without this an allowlist rule or a single human approval
# could turn the proxy into an SSRF pivot to those. The proxy's only legitimate
# upstreams are PUBLIC hosts (reached via egress-net); sibling data-plane services
# on sandbox-net are reached by the agent DIRECTLY, never through this proxy, so
# blocking every private range here breaks nothing. Kept SEPARATE from
# EGRESS_FORBIDDEN_CIDRS so the control-net guard's fail-closed startup assertion
# stays about control-net specifically. RFC1918 172.16.0.0/12 already covers both
# control-net (172.31/24) and sandbox-net (172.30/24), but control-net is also
# listed there so that guard is independently configurable. Override only for a
# deployment that legitimately proxies to a private target (e.g. an internal
# package mirror) — narrow it, don't blanket-empty it.
#
# Beyond the obvious RFC1918/loopback/link-local set, this covers the special-use
# v4 ranges that are ALSO local-or-undialable and so have no business being an
# egress target: 0.0.0.0/8 (connect(0.0.0.0) reaches localhost on Linux — the same
# loopback pivot as 127/8, by another spelling), 100.64.0.0/10 (CGNAT, which is
# where a host's VPN/overlay peers live), 192.0.0.0/24 and 198.18.0.0/15 (IETF
# protocol assignments / benchmarking), and 224.0.0.0/4 + 240.0.0.0/4 (multicast,
# reserved, and the 255.255.255.255 broadcast address).
PRIVATE_CIDRS = _parse_cidrs(
    "EGRESS_PRIVATE_CIDRS",
    "0.0.0.0/8,10.0.0.0/8,100.64.0.0/10,127.0.0.0/8,169.254.0.0/16,"
    "172.16.0.0/12,192.0.0.0/24,192.168.0.0/16,198.18.0.0/15,"
    "224.0.0.0/4,240.0.0.0/4,"
    "::/128,::1/128,fe80::/10,fc00::/7")

# One list for the literal-IP and resolve checks, so both branches cover every
# hard-blocked range (control-net + the private/special-use ranges).
_BLOCKED_CIDRS = FORBIDDEN_CIDRS + PRIVATE_CIDRS

# IPv6 forms that CARRY an embedded IPv4 address, and are therefore a way to write
# a blocked v4 destination that a v4-only blocklist does not recognize.
#
# This is the classic SSRF-filter bypass, and it defeated BOTH checks that
# ``_forbidden_reason`` documents as deterministic: stdlib containment never
# crosses address families, so ``::ffff:169.254.169.254`` is in none of the v4
# networks above, and ``getaddrinfo`` hands the same mapped form straight back, so
# the resolve branch re-tests the identical unrecognized address. Meanwhile
# ``connect()`` on a v4-mapped address delivers to the v4 host. So fold every such
# form down to the v4 address it actually dials and test THAT as well.
#
# NAT64 has no stdlib accessor (unlike ``ipv4_mapped`` / ``sixtofour``), so its
# well-known prefixes are matched explicitly and the address read out of the low
# 32 bits — the same read that handles the deprecated IPv4-compatible ``::a.b.c.d``.
#
# Teredo (2001::/32) is deliberately NOT folded: its embedded v4 is the client's
# own NAT address, not a destination anything delivers to, so folding it would
# block on an address the packet never reaches. It is also not a plausible egress
# target, so nothing is lost by leaving it to host policy.
_NAT64_PREFIXES = (ipaddress.ip_network("64:ff9b::/96"),
                   ipaddress.ip_network("64:ff9b:1::/48"))
_V4_COMPAT_PREFIX = ipaddress.ip_network("::/96")


def _unbracket(host: str) -> str:
    """Strip the brackets an IPv6 literal wears inside an authority or Host header
    (``[::1]``). mitmproxy splits the port off but leaves the brackets on, and a
    bracketed address defeats EVERY check here: ``ip_address()`` rejects the form,
    and ``getaddrinfo()`` cannot resolve it either (verified) — so without this a
    bracketed control-net address passes the guard entirely."""
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _embedded_ipv4(ip):
    """Return the IPv4 address an IPv6 literal would actually dial, or None.

    Covers v4-mapped (``::ffff:a.b.c.d``), 6to4 (``2002::/16``), NAT64
    (``64:ff9b::/96``) and the deprecated v4-compatible (``::a.b.c.d``) forms.
    Returns None for a plain v6 address, for a v4 address, and for the
    unspecified address (whose low 32 bits are zero and mean no v4 host)."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    for embedded in (ip.ipv4_mapped, ip.sixtofour):
        if embedded is not None:
            return embedded
    if ip in _V4_COMPAT_PREFIX or any(ip in n for n in _NAT64_PREFIXES):
        low = int(ip) & 0xFFFFFFFF
        if low:
            return ipaddress.IPv4Address(low)
    return None


def _blocked_cidr(ip_str: str):
    """Return the first hard-blocked network this IP falls in, or None.

    Tests the address AS WRITTEN and, for the IPv6 forms that carry an embedded
    IPv4 address, the v4 address it actually dials (see ``_embedded_ipv4``) —
    without which ``::ffff:172.31.0.2`` reaches control-net past a v4-only
    blocklist. A v4 address tested against a v6 network (or vice-versa) is simply
    not contained (stdlib returns False across versions), so mixing families in
    ``_BLOCKED_CIDRS`` is safe."""
    try:
        ip = ipaddress.ip_address(_unbracket(ip_str))
    except ValueError:
        return None
    candidates = [ip]
    embedded = _embedded_ipv4(ip)
    if embedded is not None:
        candidates.append(embedded)
    for candidate in candidates:
        for net in _BLOCKED_CIDRS:
            if candidate in net:
                return net
    return None


def _cidr_label(net) -> str:
    """Human label for a matched forbidden network, for the audit reason. Both
    control subnets share one label — the reason quotes the address anyway, so
    naming the specific network would add nothing a reader cannot already see."""
    return "control network" if net in FORBIDDEN_CIDRS else "private/special-use"


def _forbidden_reason(host: str) -> str | None:
    """Return a deny reason if this destination must never be dialed, else None.
    Covers the control plane's networks AND the private/special-use ranges
    (cloud metadata / link-local, loopback, RFC1918 — see ``PRIVATE_CIDRS``): a
    forbidden target is never weighed against policy, so no rule or human approval
    can turn the proxy into an SSRF pivot to the instance-metadata service, the
    Docker host, or the internal network. Does DNS, so it runs in a worker thread
    (see ``_forbidden``). Three checks, cheapest first: a forbidden hostname, a
    literal forbidden IP, then a name that RESOLVES into a forbidden range.

    Every check runs against the NORMALIZED destination (lowercased, trailing FQDN
    dot and IPv6 brackets stripped) and, in ``_blocked_cidr``, against the v4
    address that an IPv4-embedding IPv6 literal actually dials — so the guard is
    not spellable-around with ``[::ffff:172.31.0.2]``.

    Two of these are DETERMINISTIC guarantees — the exact-hostname match and the
    literal-IP-in-CIDR match decide from the request alone. The third (resolve
    step) is BEST-EFFORT defense-in-depth: it depends on a DNS lookup, so it is
    subject to a TOCTOU/rebind gap (mitmproxy re-resolves when it dials) and to
    resolution failure. That is acceptable here ONLY because it is not the
    load-bearing control. Reaching the control plane is prevented first by network
    topology (the sandbox has no route to either control network) and by the local
    port gate (the control plane listens on :8090 and :8091; CONNECT/HTTP are
    gated to :443/:80), so a rebound name is dialed on a port nothing serves — and
    if all of that failed, the API-surface split leaves this container able to
    reach an /authorize listener and nothing else. See DESIGN.md "Control-plane
    relay guard". The startup ``_assert_guard_configured`` refuses to run with the
    CIDR check disabled, so this is never silently off."""
    h = _unbracket((host or "").lower()).rstrip(".")
    if h in FORBIDDEN_HOSTS:
        return f"forbidden destination host {host} (control plane)"
    net = _blocked_cidr(h)
    if net is not None:
        return f"forbidden destination IP {host} ({_cidr_label(net)})"
    if not _BLOCKED_CIDRS:
        return None
    try:
        # Resolve the NORMALIZED name: a bracketed or trailing-dot form that the
        # raw `host` cannot resolve would otherwise skip this branch entirely.
        infos = socket.getaddrinfo(h, None)
    except OSError as e:
        # Cannot run the resolve-based check. Returning None is SAFE (not a silent
        # fail-open): a name that will not resolve here also will not resolve when
        # mitmproxy dials it, so no reach is granted — and the deterministic
        # hostname/literal-IP checks above already ran. Log it so a resolver
        # outage is observable rather than invisible.
        logger.warning("relay-guard resolve check skipped for %r (%s)", host, e)
        return None
    for info in infos:
        net = _blocked_cidr(info[4][0])
        if net is not None:
            return (f"destination {host} resolves to forbidden {info[4][0]} "
                    f"({_cidr_label(net)})")
    return None


async def _forbidden(host: str) -> str | None:
    return await asyncio.to_thread(_forbidden_reason, host)


def _assert_guard_configured() -> None:
    """Fail CLOSED at startup if the relay guard's CIDR check is disabled by
    configuration. This proxy is the ONE bridge between sandbox-net and a control
    network, so the CIDR check is not optional: an empty ``EGRESS_FORBIDDEN_CIDRS``
    would silently drop BOTH the literal-IP and the resolve-based branches, leaving
    only exact-hostname matching — bypassable by a literal IP or a name that
    resolves into a control subnet. There is no legitimate reason to run this proxy
    with that check off, so refuse to start rather than serve with the hole
    (matches the repo's "no silent unsafe defaults"). The hostname list is a cheap
    fast-path on top of this, not a substitute for it."""
    if not FORBIDDEN_CIDRS:
        raise RuntimeError(
            "EGRESS_FORBIDDEN_CIDRS is empty — the control-plane relay guard would "
            "not block the control networks by IP. Refusing to start (fail "
            "closed). Set it to the control subnet(s), e.g. "
            "172.31.0.0/24,172.29.0.0/24.")


# Per-connection record of the authorized CONNECT authority, keyed by the client
# connection id. The holding decision is made ONCE, at http_connect; the TLS SNI
# stage then only *compares* against it (no second control-plane call), so an
# unknown host that a human approves "once" is not re-held mid-connection. All
# access is on mitmproxy's single event loop (the hooks), so no lock is needed.
_conn_authority: dict[str, str] = {}


def _setup_audit_file() -> None:
    """Best-effort durable local audit sink. Never fatal: stdout is always the
    primary local audit stream (captured by ``docker compose logs egress-proxy``)
    and the control plane is the central, queryable store; this file is a
    convenience for persistence and grep on the mounted volume."""
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        handler = logging.FileHandler(AUDIT_PATH)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    except OSError as e:
        logger.warning("audit file unavailable (%s); logging to stdout only", e)


def _match(host: str, pattern: str) -> bool:
    """Leading dot matches subdomains; bare entry is an exact host match."""
    if pattern.startswith("."):
        return host == pattern[1:] or host.endswith(pattern)
    return host == pattern


def _is_permanent(host: str) -> bool:
    host = (host or "").lower()
    return any(_match(host, p) for p in PERMANENT_HOSTS)


def _post_authorize(payload: dict) -> dict:
    """Blocking POST to the control plane. Runs in a worker thread (see
    ``_authorize``), so blocking here is fine — it never touches the event
    loop."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(  # noqa: S310 (fixed internal URL)
        AUTHORIZE_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=CONTROL_TIMEOUT) as resp:  # noqa: S310 (fixed internal URL)
        return json.loads(resp.read().decode())


class Verdict(NamedTuple):
    allowed: bool
    reason: str
    # Did the CONTROL PLANE write this decision to the central audit store? True
    # only when the /authorize call actually returned a decision — the two local
    # paths below are false, and their audit lines are the sole record until the
    # control plane ingests this file. Never inferred at the call site: whether a
    # given host short-circuits as a lifeline is knowable only here.
    central: bool


async def _authorize(host: str, *, stage: str, **fields) -> Verdict:
    """Permanent lifeline hosts are allowed locally without consulting the control
    plane (outage resilience). Everything else asks the control plane; any error
    there fails CLOSED (deny). The control plane audits every decision it makes
    itself, which is what ``Verdict.central`` reports."""
    if _is_permanent(host):
        return Verdict(True, "permanent lifeline (local)", False)
    payload = {"host": host, "stage": stage, **fields}
    try:
        resp = await asyncio.to_thread(_post_authorize, payload)
    except Exception as e:  # noqa: BLE001 — any failure must fail closed
        return Verdict(False, f"control-plane unreachable, fail-closed ({e})", False)
    return Verdict(resp.get("decision") == "allow", resp.get("reason", ""), True)


def _audit(decision: str, **fields) -> None:
    """Local audit stream (stdout + the file at AUDIT_PATH). The control plane holds
    the authoritative central record; this mirrors it for live `logs` viewing and is
    the sole record of the locally-made decisions (and of everything, while the
    control plane is unreachable).

    Every DECISION line carries ``central`` — see the module docstring. It is the
    control plane's ingest filter, so a decision line without it is invisible to the
    ingest by default, which is the fail-safe direction: a missing flag under-reports
    rather than double-counts. Non-decision lines (``startup``) deliberately omit it.

    Field names on decision lines mirror the /authorize payload (stage/host/port/
    proto/client/method/url/reason) so the ingest is a straight column mapping with
    no per-decision translation; extra fields are for the local stream only."""
    logger.info(json.dumps({"ts": round(time.time(), 3),
                            "decision": decision, **fields}))


def load(loader) -> None:  # mitmproxy lifecycle hook
    # Refuse to start with the relay guard's CIDR check disabled (fail closed)
    # BEFORE serving any traffic or announcing readiness.
    _assert_guard_configured()
    _setup_audit_file()
    _audit("startup", control_plane=AUTHORIZE_URL, audit=AUDIT_PATH,
           permanent=list(PERMANENT_HOSTS),
           forbidden_hosts=list(FORBIDDEN_HOSTS),
           forbidden_cidrs=[str(n) for n in FORBIDDEN_CIDRS],
           private_cidrs=[str(n) for n in PRIVATE_CIDRS])


async def http_connect(flow: http.HTTPFlow) -> None:
    """All HTTPS via a forward proxy arrives as CONNECT host:port. Decide here,
    before any TLS — rejecting with a 403 needs no CA. Port-gate LOCALLY first
    (the tunnel is opaque TCP via ``ignore_connection`` below, so an allowed host
    on an unrestricted port would carry any protocol), then ask the control plane
    about the host."""
    host = flow.request.host
    port = flow.request.port
    client = flow.client_conn.peername[0] if flow.client_conn.peername else None
    # Refuse to relay onto the control plane / control-net BEFORE anything else
    # — this proxy is the only bridge between the two, so this guard is what keeps
    # the agent off the control plane, not (only) network segmentation.
    forbidden = await _forbidden(host)
    if forbidden:
        _audit("deny", stage="connect", proto="connect", host=host, port=port,
               client=client, reason=forbidden, central=False)
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})
        return
    if port not in ALLOWED_CONNECT_PORTS:
        _audit("deny", stage="connect", proto="connect", host=host, port=port,
               client=client, central=False,
               reason=f"port {port} not permitted for CONNECT "
                      f"({sorted(ALLOWED_CONNECT_PORTS)})")
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})
        return
    v = await _authorize(
        host, stage="connect", proto="connect", port=port, client=client)
    if v.allowed:
        # Remember the authorized authority for this connection so the SNI stage
        # can verify against it without a second (possibly re-holding) decision.
        _conn_authority[flow.client_conn.id] = host.lower()
        _audit("allow", stage="connect", proto="connect", host=host, port=port,
               client=client, reason=v.reason, central=v.central)
    else:
        _audit("deny", stage="connect", proto="connect", host=host, port=port,
               client=client, reason=v.reason, central=v.central)
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})


def tls_clienthello(data: tls.ClientHelloData) -> None:
    """Decide whether to pass this HTTPS connection through undecrypted. The
    destination decision was already made (and possibly held for approval) at
    ``http_connect``; here we only guard against the TLS SNI naming a DIFFERENT
    host than that authorized CONNECT authority (domain-fronting). This is a
    local string comparison — NO second control-plane call — so a host a human
    approved "once" (no persisted rule) is never re-held mid-connection.

      - SNI absent, or SNI == the authorized authority -> tunnel it
        (``ignore_connection``); no CA needed.
      - SNI names a different / unrecorded host -> refuse to tunnel. Interception
        stays ON, which fails CLOSED both ways: with no mitmproxy CA in the
        sandbox (the standing invariant) the client rejects the minted cert and
        the handshake dies; if a CA is ever added for per-domain MITM, the flow
        is instead decrypted and re-gated at the HTTP layer by ``request``."""
    sni = data.client_hello.sni
    authority = _conn_authority.get(data.context.client.id)
    if sni is None or (authority is not None and sni.lower() == authority):
        data.ignore_connection = True
        return
    # Recorded as a plain `deny` at stage `sni`, NOT as its own `deny-sni` decision
    # verb, so it maps onto the audit table's decision vocabulary (allow|deny|hold)
    # and renders in the UI with the stage as a qualifier — `deny  sni · fronted.host`
    # — instead of arriving as an unstyled tag the frontend has no rule for. `host` is
    # the SNI because that is the name the client actually asserted; the authority it
    # contradicts is in the reason. `sni`/`authority` stay as separate fields for the
    # local stream, where they are still worth grepping on their own.
    peer = data.context.client.peername
    _audit("deny", stage="sni", host=sni, client=peer[0] if peer else None,
           sni=sni, authority=authority, central=False,
           reason=f"SNI does not match the authorized CONNECT authority "
                  f"({authority or 'none recorded'}); refusing passthrough "
                  f"(possible domain-fronting)")


async def request(flow: http.HTTPFlow) -> None:
    """Gate a decrypted request. Today this only sees PLAIN HTTP (HTTPS is
    tunnelled opaque via ``ignore_connection``), but a denied-SNI flow — or any
    flow once a MITM CA is added — arrives here decrypted and MUST be re-gated.

    Gate EVERY name the client asserts, not just one:
      - ``request.host`` — the transport target we dial. For an intercepted HTTPS
        flow this is the CONNECT authority (already authorized at CONNECT).
      - ``request.pretty_host`` — the Host header / :authority, the name the
        client actually addresses.
    Domain-fronting hides a NON-authorized Host behind an authorized CONNECT
    authority, so checking only ``request.host`` would wave the fronted request
    through. Requiring BOTH closes it."""
    https = flow.request.scheme == "https"
    proto = "https" if https else "http"
    allowed_ports = ALLOWED_CONNECT_PORTS if https else ALLOWED_HTTP_PORTS
    port = flow.request.port
    # The same peer address ``http_connect`` reports. Omitting it left every plaintext
    # -HTTP decision recorded with no client at all — and this control plane is shared
    # across sandboxes, so those audit rows could not say whose request they were. It
    # went unnoticed while the decisions view had no client column to be blank; it
    # showed up in the first live test after the column was added. Read BEFORE the two
    # guards below, not just before /authorize: those denials are audited too, and are
    # now ingested centrally, so leaving them clientless reopened the same hole on the
    # paths that need attribution most.
    client = flow.client_conn.peername[0] if flow.client_conn.peername else None
    # Forbid control-plane / control-net for EVERY name the client asserts
    # (transport host and Host/:authority), before policy or the port gate.
    for name in {flow.request.host, flow.request.pretty_host}:
        forbidden = await _forbidden(name)
        if forbidden:
            _audit("deny", stage="http", proto=proto, host=name, port=port,
                   client=client, method=flow.request.method,
                   url=flow.request.pretty_url, reason=forbidden, central=False)
            flow.response = http.Response.make(
                403, b"egress denied by policy\n", {"Content-Type": "text/plain"})
            return
    if port not in allowed_ports:
        _audit("deny", stage="http", proto=proto, host=flow.request.pretty_host,
               port=port, client=client, method=flow.request.method,
               url=flow.request.pretty_url, central=False,
               reason=f"port {port} not permitted for {proto} "
                      f"({sorted(allowed_ports)})")
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})
        return
    # sorted() only to make the "which name failed" report deterministic.
    names = sorted({flow.request.host, flow.request.pretty_host})
    bad_name, bad_reason, central = None, "", True
    for name in names:
        v = await _authorize(
            name, stage="http", proto=proto, port=port, client=client,
            method=flow.request.method, url=flow.request.pretty_url)
        # Every name must clear the control plane, so the flow is centrally recorded
        # only if EVERY consulted name was — one lifeline name short-circuiting
        # locally leaves this decision partly unrecorded there, and `and` is the
        # fail-safe fold: it ingests a possible duplicate rather than dropping a row.
        central = central and v.central
        if not v.allowed:
            bad_name, bad_reason = name, v.reason
            break
    if bad_name is None:
        _audit("allow", stage="http", proto=proto, host=flow.request.pretty_host,
               port=port, method=flow.request.method, client=client,
               url=flow.request.pretty_url, central=central)
    else:
        _audit("deny", stage="http", proto=proto, host=bad_name, port=port,
               method=flow.request.method, client=client,
               url=flow.request.pretty_url, central=central,
               reason=f"host not authorized ({bad_name}): {bad_reason}")
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})


def client_disconnected(client) -> None:  # mitmproxy lifecycle hook
    """Drop this connection's remembered authority so ``_conn_authority`` does
    not grow unbounded over the proxy's lifetime."""
    _conn_authority.pop(client.id, None)
