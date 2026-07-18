"""
Egress proxy policy + audit addon.

Step 0 of the governed egress proxy (see DESIGN.md). A CONNECT-level forward
proxy for the Claude sandbox(es): it enforces a domain allowlist (default-deny)
and writes a structured audit line for every decision. It does NOT decrypt TLS
— HTTPS is tunnelled untouched (``data.ignore_connection = True`` in
``tls_clienthello``), so the sandbox needs no custom CA. We still govern by name
because the CONNECT authority and the TLS SNI both name the destination, which
is all domain-level control needs — and, unlike the v1 IP firewall, naming the
host is what closes the shared-CDN / domain-fronting gap.

This is deliberately the skeleton, not the finished proxy:
  - step 1 flips the sandbox firewall so THIS is the only egress path;
  - step 2 replaces the static allowlist + stdout audit with the control plane
    (dynamic policy, hold-for-approval, durable/queryable audit store);
  - MITM / body-level audit is a later per-domain option — enabled by NOT
    setting ``ignore_connection`` for a chosen domain (needs our CA on the box).

The allowlist file is re-read on every request, so edits take effect on the
next connection — a cheap taste of "dynamic" before the control plane exists
(``docker exec egress-proxy vi /etc/egress/allowlist.txt`` or a bind mount).
"""
from __future__ import annotations

import json
import logging
import os
import time

from mitmproxy import http, tls

ALLOWLIST_PATH = os.environ.get("EGRESS_ALLOWLIST", "/etc/egress/allowlist.txt")
AUDIT_PATH = os.environ.get("EGRESS_AUDIT_LOG", "/var/log/egress/audit.jsonl")

# This is an HTTP(S) egress proxy, so the policy has a port intent even though
# the allowlist only names hosts: CONNECT tunnels are for HTTPS, plain forward
# requests are for HTTP. Gate on it — otherwise an allowlisted host is reachable
# on ANY port over a raw TCP tunnel (SSH, arbitrary TLS services, ...), widening
# the channel well past what "allow example.com" is meant to grant. Override for
# the rare host that legitimately serves HTTP(S) on a non-standard port.
def _ports(env: str, default: str) -> frozenset[int]:
    return frozenset(int(p) for p in os.environ.get(env, default).split(",") if p.strip())

ALLOWED_CONNECT_PORTS = _ports("EGRESS_CONNECT_PORTS", "443")  # HTTPS tunnels
ALLOWED_HTTP_PORTS = _ports("EGRESS_HTTP_PORTS", "80")         # plain HTTP

logger = logging.getLogger("egress")


def _setup_audit_file() -> None:
    """Best-effort durable audit sink. Never fatal: stdout is always the primary
    audit stream (captured by ``docker compose logs egress-proxy``); the file is
    a convenience for persistence and grep on the mounted volume."""
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        handler = logging.FileHandler(AUDIT_PATH)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    except OSError as e:
        logger.warning("audit file unavailable (%s); logging to stdout only", e)


def _load_allowlist() -> list[str]:
    """Read the allowlist fresh each call so live edits apply on the next
    connection. Fail CLOSED: if the policy file can't be read, allow nothing."""
    try:
        with open(ALLOWLIST_PATH) as f:
            return [ln.strip().lower() for ln in f
                    if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        logger.error("allowlist unreadable at %s — denying all", ALLOWLIST_PATH)
        return []


def _allowed(host: str) -> bool:
    host = (host or "").lower()
    for entry in _load_allowlist():
        # A leading dot (".example.com") matches example.com and any subdomain;
        # a bare entry is an exact host match.
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def _audit(decision: str, **fields) -> None:
    logger.info(json.dumps({"ts": round(time.time(), 3),
                            "decision": decision, **fields}))


def load(loader) -> None:  # mitmproxy lifecycle hook
    _setup_audit_file()
    _audit("startup", allowlist=ALLOWLIST_PATH, audit=AUDIT_PATH)


def http_connect(flow: http.HTTPFlow) -> None:
    """All HTTPS via a forward proxy arrives as CONNECT host:port. Decide here,
    before any TLS — rejecting with a 403 needs no CA. Gate on BOTH host and
    port: the tunnel is opaque TCP (``ignore_connection`` below), so an allowed
    host on an unrestricted port would carry any protocol, not just HTTPS."""
    host = flow.request.host
    port = flow.request.port
    client = flow.client_conn.peername[0] if flow.client_conn.peername else None
    allowed_host = _allowed(host)
    if allowed_host and port in ALLOWED_CONNECT_PORTS:
        _audit("allow", proto="connect", host=host, port=port, client=client)
    else:
        reason = ("host not allowlisted" if not allowed_host
                  else f"port {port} not permitted for CONNECT "
                       f"({sorted(ALLOWED_CONNECT_PORTS)})")
        _audit("deny", proto="connect", host=host, port=port, client=client,
               reason=reason)
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})


def tls_clienthello(data: tls.ClientHelloData) -> None:
    """Decide whether to pass this HTTPS connection through undecrypted. The
    CONNECT authority was already allowlisted in ``http_connect``, but the TLS
    SNI can name a DIFFERENT host (domain-fronting behind an allowed CONNECT
    target), so gate on it too.

      - SNI absent, or SNI allowlisted -> tunnel it (``ignore_connection``); no
        CA needed, as before.
      - SNI present but NOT allowlisted -> refuse to tunnel. We leave
        interception ON rather than passing the bytes through, which fails
        CLOSED both ways: with no mitmproxy CA in the sandbox (the standing
        invariant) the client rejects the minted cert and the handshake dies;
        if a CA is ever added for per-domain MITM, the flow is instead decrypted
        and re-gated at the HTTP layer by ``request`` below. Either way a
        non-allowlisted SNI cannot pass unexamined."""
    sni = data.client_hello.sni
    if sni is None or _allowed(sni):
        data.ignore_connection = True
        return
    _audit("deny-sni", sni=sni,
           note="SNI not allowlisted; refusing passthrough (possible domain-fronting)")


def request(flow: http.HTTPFlow) -> None:
    """Gate a decrypted request. Today this only sees PLAIN HTTP (HTTPS is
    tunnelled opaque via ``ignore_connection``), but a denied-SNI flow — or any
    flow once a MITM CA is added — arrives here decrypted and MUST be re-gated.

    Gate EVERY name the client asserts, not just one:
      - ``request.host`` — the transport target we dial. For an intercepted HTTPS
        flow this is the CONNECT authority (already allowlisted at CONNECT).
      - ``request.pretty_host`` — the Host header / :authority, the name the
        client actually addresses.
    Domain-fronting hides a NON-allowlisted Host behind an allowlisted CONNECT
    authority, so checking only ``request.host`` would wave the fronted request
    through (it did — see the DESIGN/audit trail). Requiring BOTH closes it."""
    https = flow.request.scheme == "https"
    proto = "https" if https else "http"
    allowed_ports = ALLOWED_CONNECT_PORTS if https else ALLOWED_HTTP_PORTS
    port = flow.request.port
    # sorted() only to make the "which name failed" report deterministic.
    names = sorted({flow.request.host, flow.request.pretty_host})
    bad_name = next((n for n in names if not _allowed(n)), None)
    if bad_name is None and port in allowed_ports:
        _audit("allow", proto=proto, method=flow.request.method,
               url=flow.request.pretty_url)
    else:
        reason = (f"host not allowlisted ({bad_name})" if bad_name is not None
                  else f"port {port} not permitted for {proto} "
                       f"({sorted(allowed_ports)})")
        _audit("deny", proto=proto, method=flow.request.method,
               url=flow.request.pretty_url, reason=reason)
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})
