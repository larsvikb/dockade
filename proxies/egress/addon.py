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
    before any TLS — rejecting with a 403 needs no CA."""
    host = flow.request.host
    client = flow.client_conn.peername[0] if flow.client_conn.peername else None
    if _allowed(host):
        _audit("allow", proto="connect", host=host, port=flow.request.port,
               client=client)
    else:
        _audit("deny", proto="connect", host=host, port=flow.request.port,
               client=client)
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})


def tls_clienthello(data: tls.ClientHelloData) -> None:
    """Pass HTTPS through without decrypting (no CA in the sandbox). We still see
    the SNI: if it names a host the allowlist doesn't (e.g. domain-fronting
    behind an allowed CONNECT target), flag it. Enforcing on that mismatch is a
    step-1 refinement; step 0 observes and audits it."""
    data.ignore_connection = True
    sni = data.client_hello.sni
    if sni and not _allowed(sni):
        _audit("warn-sni", sni=sni,
               note="SNI not in allowlist (possible domain-fronting)")


def request(flow: http.HTTPFlow) -> None:
    """Plain HTTP (uncommon here — most traffic is HTTPS/CONNECT). Gate on the
    Host and log the full URL."""
    host = flow.request.host
    if _allowed(host):
        _audit("allow", proto="http", method=flow.request.method,
               url=flow.request.pretty_url)
    else:
        _audit("deny", proto="http", method=flow.request.method,
               url=flow.request.pretty_url)
        flow.response = http.Response.make(
            403, b"egress denied by policy\n", {"Content-Type": "text/plain"})
