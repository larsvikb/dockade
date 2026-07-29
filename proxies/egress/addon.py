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
the proxy already makes — no client-side cache (an operator edit applies to the
very next connection) and no separate audit channel.

Fail-closed, with one deliberate exception:
  - The **permanent lifeline** hosts (``EGRESS_PERMANENT_HOSTS`` — the Anthropic
    API/auth endpoints) are allowed by a LOCAL check *before* the control plane
    is consulted, so a control-plane outage never bricks the agent's own API.
  - Every other host depends on the control plane. If it is unreachable or slow
    (timeout), the request is DENIED and audited locally — governed egress fails
    closed when the policy authority is down, which is the intended posture.

Control-plane isolation: this proxy is the only component on BOTH sandbox-net
and control-net, so it — not network segmentation — is what keeps the agent off
the control plane. Before any policy/permanent/port check it hard-refuses any
destination that names a control-plane host or resolves into the control-net
subnet (``_forbidden``), a guard that no rule, approval, or port change can widen.

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

from mitmproxy import http, tls

AUDIT_PATH = os.environ.get("EGRESS_AUDIT_LOG", "/var/log/egress/audit.jsonl")

# Control plane: where policy decisions + audit go. One call per connection.
CONTROL_PLANE_URL = os.environ.get(
    "EGRESS_CONTROL_PLANE_URL", "http://control-plane:8090").rstrip("/")
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

# ── Forbidden destinations (control plane / control-net) ──────────────────────
# This proxy is the ONE component attached to BOTH sandbox-net and control-net,
# so it — not network segmentation — is what actually keeps the agent off the
# control plane: were the proxy to relay a connection onto control-net, the agent
# could reach the control plane (and, e.g., approve its own held requests). We
# therefore refuse, BEFORE any policy / permanent-lifeline / port check, any
# destination that names a control-plane host or resolves into the control-net
# subnet. This guard is checked first and is never consulted against policy, so it
# cannot be widened by a rule, a human approval, a change to the port allowlist,
# or a public name whose DNS is pointed at control-net.
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

# Defaults mirror docker-compose.yml: control-net is 172.31.0.0/24 and the
# control-plane services are reachable there by name.
FORBIDDEN_CIDRS = _parse_cidrs("EGRESS_FORBIDDEN_CIDRS", "172.31.0.0/24")
FORBIDDEN_HOSTS = _hosts("EGRESS_FORBIDDEN_HOSTS", "control-plane,control-plane-ui")


def _ip_in_forbidden(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in FORBIDDEN_CIDRS)


def _forbidden_reason(host: str) -> str | None:
    """Return a deny reason if this destination must never be dialed (control
    plane / control-net), else None. Does DNS, so it runs in a worker thread
    (see ``_forbidden``). Three checks, cheapest first: a forbidden hostname, a
    literal forbidden IP, then a name that RESOLVES into a forbidden range.

    Two of these are DETERMINISTIC guarantees — the exact-hostname match and the
    literal-IP-in-CIDR match decide from the request alone. The third (resolve
    step) is BEST-EFFORT defense-in-depth: it depends on a DNS lookup, so it is
    subject to a TOCTOU/rebind gap (mitmproxy re-resolves when it dials) and to
    resolution failure. That is acceptable here ONLY because it is not the
    load-bearing control — reaching the control plane is prevented first by
    network topology (the sandbox has no control-net route) and by the local port
    gate (control plane listens on :8090; CONNECT/HTTP are gated to :443/:80), so
    a rebound name is dialed on a port nothing serves. See DESIGN.md
    "Control-plane relay guard" and the planned API-surface split (#4) that caps
    the blast radius of any guard bypass. The startup ``_assert_guard_configured``
    refuses to run with the CIDR check disabled, so this is never silently off."""
    h = (host or "").lower().rstrip(".")
    if h in FORBIDDEN_HOSTS:
        return f"forbidden destination host {host} (control plane)"
    if _ip_in_forbidden(h):
        return f"forbidden destination IP {host} (control-net)"
    if not FORBIDDEN_CIDRS:
        return None
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        # Cannot run the resolve-based check. Returning None is SAFE (not a silent
        # fail-open): a name that will not resolve here also will not resolve when
        # mitmproxy dials it, so no reach is granted — and the deterministic
        # hostname/literal-IP checks above already ran. Log it so a resolver
        # outage is observable rather than invisible.
        logger.warning("relay-guard resolve check skipped for %r (%s)", host, e)
        return None
    for info in infos:
        if _ip_in_forbidden(info[4][0]):
            return (f"destination {host} resolves to forbidden {info[4][0]} "
                    "(control-net)")
    return None


async def _forbidden(host: str) -> str | None:
    return await asyncio.to_thread(_forbidden_reason, host)


def _assert_guard_configured() -> None:
    """Fail CLOSED at startup if the relay guard's CIDR check is disabled by
    configuration. This proxy is the ONE bridge between sandbox-net and
    control-net, so the control-net CIDR check is not optional: an empty
    ``EGRESS_FORBIDDEN_CIDRS`` would silently drop BOTH the literal-IP and the
    resolve-based branches, leaving only exact-hostname matching — bypassable by a
    literal IP or a name that resolves into control-net. There is no legitimate
    reason to run this proxy with that check off, so refuse to start rather than
    serve with the hole (matches the repo's "no silent unsafe defaults"). The
    hostname list is a cheap fast-path on top of this, not a substitute for it."""
    if not FORBIDDEN_CIDRS:
        raise RuntimeError(
            "EGRESS_FORBIDDEN_CIDRS is empty — the control-plane relay guard would "
            "not block control-net by IP. Refusing to start (fail closed). Set it "
            "to the control-net subnet(s), e.g. 172.31.0.0/24.")


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
    req = urllib.request.Request(
        AUTHORIZE_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=CONTROL_TIMEOUT) as resp:  # noqa: S310 (fixed internal URL)
        return json.loads(resp.read().decode())


async def _authorize(host: str, *, stage: str, **fields) -> tuple[bool, str]:
    """Return (allowed, reason). Permanent lifeline hosts are allowed locally
    without consulting the control plane (outage resilience). Everything else
    asks the control plane; any error there fails CLOSED (deny). The control
    plane audits every decision it makes, so there is no per-call audit toggle."""
    if _is_permanent(host):
        return True, "permanent lifeline (local)"
    payload = {"host": host, "stage": stage, **fields}
    try:
        resp = await asyncio.to_thread(_post_authorize, payload)
    except Exception as e:  # noqa: BLE001 — any failure must fail closed
        return False, f"control-plane unreachable, fail-closed ({e})"
    return resp.get("decision") == "allow", resp.get("reason", "")


def _audit(decision: str, **fields) -> None:
    """Local audit stream (stdout + optional file). The control plane holds the
    authoritative central record; this mirrors it for live `logs` viewing and is
    the sole record when the control plane is unreachable."""
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
           forbidden_cidrs=[str(n) for n in FORBIDDEN_CIDRS])


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
        _audit("deny", proto="connect", host=host, port=port, client=client,
               reason=forbidden)
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})
        return
    if port not in ALLOWED_CONNECT_PORTS:
        _audit("deny", proto="connect", host=host, port=port, client=client,
               reason=f"port {port} not permitted for CONNECT "
                      f"({sorted(ALLOWED_CONNECT_PORTS)})")
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})
        return
    allowed, reason = await _authorize(
        host, stage="connect", proto="connect", port=port, client=client)
    if allowed:
        # Remember the authorized authority for this connection so the SNI stage
        # can verify against it without a second (possibly re-holding) decision.
        _conn_authority[flow.client_conn.id] = host.lower()
        _audit("allow", proto="connect", host=host, port=port, client=client,
               reason=reason)
    else:
        _audit("deny", proto="connect", host=host, port=port, client=client,
               reason=reason)
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
    _audit("deny-sni", sni=sni, authority=authority,
           note="SNI does not match the authorized CONNECT authority; refusing "
                "passthrough (possible domain-fronting)")


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
    # Forbid control-plane / control-net for EVERY name the client asserts
    # (transport host and Host/:authority), before policy or the port gate.
    for name in {flow.request.host, flow.request.pretty_host}:
        forbidden = await _forbidden(name)
        if forbidden:
            _audit("deny", proto=proto, method=flow.request.method,
                   url=flow.request.pretty_url, reason=forbidden)
            flow.response = http.Response.make(
                403, b"egress denied by policy\n", {"Content-Type": "text/plain"})
            return
    if port not in allowed_ports:
        _audit("deny", proto=proto, method=flow.request.method,
               url=flow.request.pretty_url,
               reason=f"port {port} not permitted for {proto} "
                      f"({sorted(allowed_ports)})")
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})
        return
    # sorted() only to make the "which name failed" report deterministic.
    names = sorted({flow.request.host, flow.request.pretty_host})
    bad_name, bad_reason = None, ""
    for name in names:
        allowed, reason = await _authorize(
            name, stage="http", proto=proto, port=port,
            method=flow.request.method, url=flow.request.pretty_url)
        if not allowed:
            bad_name, bad_reason = name, reason
            break
    if bad_name is None:
        _audit("allow", proto=proto, method=flow.request.method,
               url=flow.request.pretty_url)
    else:
        _audit("deny", proto=proto, method=flow.request.method,
               url=flow.request.pretty_url,
               reason=f"host not authorized ({bad_name}): {bad_reason}")
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})


def client_disconnected(client) -> None:  # mitmproxy lifecycle hook
    """Drop this connection's remembered authority so ``_conn_authority`` does
    not grow unbounded over the proxy's lifetime."""
    _conn_authority.pop(client.id, None)
