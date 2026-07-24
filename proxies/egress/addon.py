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

The call runs in a worker thread (``asyncio.to_thread`` + stdlib ``urllib``), so
it never blocks mitmproxy's event loop and the egress image needs no extra
dependency. Port gating stays local (this is an HTTP/S proxy; a raw CONNECT to a
non-443 port would carry arbitrary TCP regardless of host policy).

Later: MITM / body-level audit is a per-domain option — enabled by NOT setting
``ignore_connection`` for a chosen domain (needs our CA on the box).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request

from mitmproxy import http, tls

AUDIT_PATH = os.environ.get("EGRESS_AUDIT_LOG", "/var/log/egress/audit.jsonl")

# Control plane: where policy decisions + audit go. One call per connection.
CONTROL_PLANE_URL = os.environ.get(
    "EGRESS_CONTROL_PLANE_URL", "http://control-plane:8090").rstrip("/")
AUTHORIZE_URL = CONTROL_PLANE_URL + "/authorize"
CONTROL_TIMEOUT = float(os.environ.get("EGRESS_CONTROL_TIMEOUT", "3.0"))

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


async def _authorize(host: str, *, stage: str, audit: bool = True,
                     **fields) -> tuple[bool, str]:
    """Return (allowed, reason). Permanent lifeline hosts are allowed locally
    without consulting the control plane (outage resilience). Everything else
    asks the control plane; any error there fails CLOSED (deny)."""
    if _is_permanent(host):
        return True, "permanent lifeline (local)"
    payload = {"host": host, "stage": stage, "audit": audit, **fields}
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
    _setup_audit_file()
    _audit("startup", control_plane=AUTHORIZE_URL, audit=AUDIT_PATH,
           permanent=list(PERMANENT_HOSTS))


async def http_connect(flow: http.HTTPFlow) -> None:
    """All HTTPS via a forward proxy arrives as CONNECT host:port. Decide here,
    before any TLS — rejecting with a 403 needs no CA. Port-gate LOCALLY first
    (the tunnel is opaque TCP via ``ignore_connection`` below, so an allowed host
    on an unrestricted port would carry any protocol), then ask the control plane
    about the host."""
    host = flow.request.host
    port = flow.request.port
    client = flow.client_conn.peername[0] if flow.client_conn.peername else None
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
        _audit("allow", proto="connect", host=host, port=port, client=client,
               reason=reason)
    else:
        _audit("deny", proto="connect", host=host, port=port, client=client,
               reason=reason)
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})


async def tls_clienthello(data: tls.ClientHelloData) -> None:
    """Decide whether to pass this HTTPS connection through undecrypted. The
    CONNECT authority was already authorized in ``http_connect``, but the TLS SNI
    can name a DIFFERENT host (domain-fronting behind an allowed CONNECT target),
    so authorize it too. ``audit=False``: the connection was already audited at
    the CONNECT stage, so this per-connection SNI recheck is policy-only to avoid
    doubling every HTTPS row; a fronting DENY is still recorded locally below.

      - SNI absent, or SNI authorized -> tunnel it (``ignore_connection``); no
        CA needed.
      - SNI present but NOT authorized -> refuse to tunnel. Interception stays
        ON, which fails CLOSED both ways: with no mitmproxy CA in the sandbox
        (the standing invariant) the client rejects the minted cert and the
        handshake dies; if a CA is ever added for per-domain MITM, the flow is
        instead decrypted and re-gated at the HTTP layer by ``request`` below."""
    sni = data.client_hello.sni
    if sni is None:
        data.ignore_connection = True
        return
    allowed, reason = await _authorize(sni, stage="sni", audit=False)
    if allowed:
        data.ignore_connection = True
        return
    _audit("deny-sni", sni=sni, reason=reason,
           note="SNI not authorized; refusing passthrough (possible domain-fronting)")


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
