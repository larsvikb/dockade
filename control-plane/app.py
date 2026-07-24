"""
Control plane — governance authority for the governed data-plane proxies.

Step 2a of the target architecture (see DESIGN.md). The management app the
**agent can never reach**: it lives only on `control-net`, and the sandbox has
no route there. Governed proxies (today just the egress proxy) call it on the
control path to make policy decisions and to record audit.

Responsibilities implemented here (2a):
  - **Policy store** — allow/block rules in SQLite (the "crown-jewel" state:
    durable, queryable, exportable; DESIGN.md "The policy + audit store").
  - **Authorize + audit in one call** — `POST /authorize` returns the decision
    AND records the audit row as it decides. Policy and audit share the one
    round-trip the proxy already makes; no separate audit channel, no client
    cache, so an operator edit takes effect on the very next connection.

Deliberately NOT here yet:
  - **Hold-for-approval** (2b) — `POST /authorize` returns only allow/deny for
    now; an unknown host is denied, not queued. The `hold` decision value and
    the approval queue/UI arrive with 2b.
  - **Audit browser + per-proxy config UI** (2c). Rows accumulate from 2a so
    there is history to browse once the UI lands.

No egress: this service is on `control-net` only (plus a host-loopback publish
for the human UI later). It must never be given an internet route — it is pure
management state.
"""
from __future__ import annotations

import os
import sqlite3
import time

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("CONTROL_DB", "/var/lib/control-plane/control.db")
SEED_PATH = os.environ.get(
    "CONTROL_SEED", "/etc/control-plane/egress-allowlist.txt")

app = FastAPI(title="dockade control plane", version="2a")


# ── storage ─────────────────────────────────────────────────────────────────
# One writer (this service), low volume, so SQLite is ample. WAL keeps the
# short-lived per-request connections from blocking each other. Endpoints are
# plain `def`, so FastAPI runs them in a threadpool — the blocking sqlite calls
# never stall the event loop, and we need no manual offloading.

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                id         INTEGER PRIMARY KEY,
                pattern    TEXT NOT NULL UNIQUE,   -- host or .suffix (see _match)
                action     TEXT NOT NULL,          -- 'allow' | 'block'
                source     TEXT NOT NULL,          -- 'seed' | 'operator' | ...
                created_at REAL NOT NULL
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit (
                id       INTEGER PRIMARY KEY,
                ts       REAL NOT NULL,
                decision TEXT NOT NULL,
                stage    TEXT,       -- connect | sni | http
                host     TEXT,
                port     INTEGER,
                proto    TEXT,
                client   TEXT,
                method   TEXT,
                url      TEXT,
                reason   TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)")
        conn.commit()


def _seed_if_empty() -> int:
    """Load the seed file into an empty rules table. Idempotent: once any rule
    exists (seeded or operator-added) the store is authoritative and the file is
    never re-read, so operator edits are never clobbered by a restart."""
    with _connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] > 0:
            return 0
        try:
            with open(SEED_PATH) as f:
                patterns = [ln.strip().lower() for ln in f
                            if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError:
            # No seed is a valid (fully default-deny) starting state.
            return 0
        now = time.time()
        conn.executemany(
            "INSERT OR IGNORE INTO rules(pattern, action, source, created_at) "
            "VALUES (?, 'allow', 'seed', ?)",
            [(p, now) for p in patterns])
        conn.commit()
        return len(patterns)


def _match(host: str, pattern: str) -> bool:
    """Same semantics the egress proxy used for its flat allowlist: a leading
    dot (".example.com") matches example.com and any subdomain; a bare entry is
    an exact host match."""
    if pattern.startswith("."):
        return host == pattern[1:] or host.endswith(pattern)
    return host == pattern


def _decide(host: str) -> tuple[str, str]:
    """Return (decision, reason). Block rules win over allow; an unmatched host
    is denied (default-deny). 2b will return 'hold' here for the unknown case
    instead of an outright 'deny'."""
    host = (host or "").lower()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT pattern, action FROM rules").fetchall()
    for r in rows:
        if r["action"] == "block" and _match(host, r["pattern"]):
            return "deny", f"blocked by rule ({r['pattern']})"
    for r in rows:
        if r["action"] == "allow" and _match(host, r["pattern"]):
            return "allow", f"allowed by rule ({r['pattern']})"
    return "deny", "no matching allow rule (default-deny)"


# ── API ─────────────────────────────────────────────────────────────────────

class AuthorizeRequest(BaseModel):
    host: str
    port: int | None = None
    proto: str | None = None
    client: str | None = None
    method: str | None = None
    url: str | None = None
    stage: str | None = None       # connect | sni | http
    audit: bool = True             # sni pre-checks pass False to avoid noise


class AuthorizeResponse(BaseModel):
    decision: str                  # allow | deny  (hold arrives in 2b)
    reason: str


@app.on_event("startup")
def _startup() -> None:
    _init_db()
    seeded = _seed_if_empty()
    if seeded:
        print(f"control-plane: seeded {seeded} allow rules from {SEED_PATH}",
              flush=True)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/authorize", response_model=AuthorizeResponse)
def authorize(req: AuthorizeRequest) -> AuthorizeResponse:
    decision, reason = _decide(req.host)
    if req.audit:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO audit(ts, decision, stage, host, port, proto, "
                "client, method, url, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), decision, req.stage, req.host, req.port,
                 req.proto, req.client, req.method, req.url, reason))
            conn.commit()
    return AuthorizeResponse(decision=decision, reason=reason)


@app.get("/", response_class=PlainTextResponse)
def status() -> str:
    """Minimal operator status until the 2c UI lands."""
    with _connect() as conn:
        rules = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        recent = conn.execute(
            "SELECT ts, decision, stage, host, reason FROM audit "
            "ORDER BY ts DESC LIMIT 10").fetchall()
    lines = [f"dockade control plane (2a) — {rules} rules, {audits} audit rows",
             "recent decisions:"]
    lines += [f"  {r['decision']:5} {r['stage'] or '-':7} {r['host'] or '-'} "
              f"({r['reason']})" for r in recent] or ["  (none yet)"]
    return "\n".join(lines) + "\n"
