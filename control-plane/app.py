"""
Control plane — governance authority for the governed data-plane proxies.

Step 2b: policy + audit + **hold-for-approval**. The management app the agent
can never reach (control-net only; the sandbox has no route there). Governed
proxies call it on the control path to authorize connections; unknown requests
are held for a human, who approves/rejects them in a live UI.

The authorize flow (one call from the proxy, `POST /authorize`):
  - host matches a BLOCK rule            -> deny   (audited)
  - host matches an ALLOW rule           -> allow  (audited)
  - no matching rule                     -> HOLD: record a pending approval and
    BLOCK the request until a human resolves it or CONTROL_HOLD_TIMEOUT elapses
    (-> default-deny). The proxy only ever sees allow/deny; the hold is internal.

A human resolves holds over the approvals API (GET /approvals, the SSE stream at
/approvals/stream, and POST /approvals/{id}/resolve), surfaced by the separate
control-plane-ui frontend; the backend serves no HTML itself:
  - allow-once / deny-once     — decide just this request
  - allow-persist / deny-persist — also write a rule so future connections skip
    the hold (progressive trust; DESIGN.md "auto-approve progressively more").

Concurrency model: run under a SINGLE uvicorn worker. `/authorize` and the
resolve endpoint are sync (FastAPI runs them in a threadpool); a held request
blocks its worker on a threading.Event that the resolve endpoint sets. Concurrent
holds are bounded (CONTROL_MAX_PENDING / CONTROL_MAX_PENDING_PER_CLIENT) so a
sandbox cannot pin every worker and stall governance for all sandboxes — over the
cap /authorize fails closed. The SQLite store is the source of truth for the UI
(the SSE stream polls it). Do NOT run multiple workers — the pending-event
registry is in-process.

No egress: this service is on control-net only. It must never be given an
internet route — it is pure management state (the crown jewel).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("CONTROL_DB", "/var/lib/control-plane/control.db")
SEED_PATH = os.environ.get(
    "CONTROL_SEED", "/etc/control-plane/egress-allowlist.txt")
# How long a held request waits for a human before defaulting to deny.
HOLD_TIMEOUT = float(os.environ.get("CONTROL_HOLD_TIMEOUT", "120"))
# Bound concurrent holds. A held request blocks a FastAPI threadpool worker
# until it is resolved or times out, so an unbounded number of holds would pin
# every worker and stall ALL /authorize decisions — and this control plane is
# shared across every sandbox, so one agent could starve governance for all. Cap
# in-flight holds globally and per client; over either cap /authorize fails CLOSED
# immediately (deny) instead of registering another blocking hold. Keep MAX_PENDING
# well under the threadpool size (anyio default ~40) so fast allow/deny decisions
# always have free workers. Set MAX_PENDING_PER_CLIENT to 0 to disable that cap.
MAX_PENDING = int(os.environ.get("CONTROL_MAX_PENDING", "16"))
MAX_PENDING_PER_CLIENT = int(os.environ.get("CONTROL_MAX_PENDING_PER_CLIENT", "4"))

app = FastAPI(title="dockade control plane", version="2b")

# In-memory registry of held requests, keyed by approval id. Single-process only
# (see module docstring). The Event wakes the blocked /authorize worker; the
# outcome carries the human's decision back to it. SQLite holds the durable state.
_LOCK = threading.Lock()
_PENDING_EVENTS: dict[str, threading.Event] = {}
_PENDING_OUTCOME: dict[str, str] = {}
# approval_id -> client, so the per-client hold cap can be counted under _LOCK.
_PENDING_CLIENT: dict[str, str | None] = {}


# ── storage ─────────────────────────────────────────────────────────────────

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
                source     TEXT NOT NULL,          -- 'seed' | 'operator'
                created_at REAL NOT NULL
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit (
                id       INTEGER PRIMARY KEY,
                ts       REAL NOT NULL,
                decision TEXT NOT NULL,       -- allow | deny | hold
                stage    TEXT,
                host     TEXT,
                port     INTEGER,
                proto    TEXT,
                client   TEXT,
                method   TEXT,
                url      TEXT,
                reason   TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS approvals (
                id          TEXT PRIMARY KEY,
                ts          REAL NOT NULL,
                host        TEXT NOT NULL,
                port        INTEGER,
                proto       TEXT,
                client      TEXT,
                method      TEXT,
                url         TEXT,
                status      TEXT NOT NULL,    -- pending | allowed | denied | expired
                mode        TEXT,             -- once | persist
                resolved_at REAL
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status)")
        conn.commit()


def _seed_if_empty() -> int:
    """Load the seed file into an empty rules table. Idempotent: once any rule
    exists the store is authoritative and the file is never re-read."""
    with _connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] > 0:
            return 0
        try:
            with open(SEED_PATH) as f:
                patterns = [ln.strip().lower() for ln in f
                            if ln.strip() and not ln.lstrip().startswith("#")]
        except OSError:
            return 0
        now = time.time()
        conn.executemany(
            "INSERT OR IGNORE INTO rules(pattern, action, source, created_at) "
            "VALUES (?, 'allow', 'seed', ?)",
            [(p, now) for p in patterns])
        conn.commit()
        return len(patterns)


def _match(host: str, pattern: str) -> bool:
    """Leading dot matches subdomains; bare entry is an exact host match."""
    if pattern.startswith("."):
        return host == pattern[1:] or host.endswith(pattern)
    return host == pattern


def _decide(host: str) -> tuple[str, str]:
    """(decision, reason). Block wins over allow; an unmatched host is HELD for
    human approval (2b) rather than denied outright."""
    host = (host or "").lower()
    with _connect() as conn:
        rows = conn.execute("SELECT pattern, action FROM rules").fetchall()
    for r in rows:
        if r["action"] == "block" and _match(host, r["pattern"]):
            return "deny", f"blocked by rule ({r['pattern']})"
    for r in rows:
        if r["action"] == "allow" and _match(host, r["pattern"]):
            return "allow", f"allowed by rule ({r['pattern']})"
    return "hold", "no matching rule — held for approval"


def _audit(decision: str, **fields) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO audit(ts, decision, stage, host, port, proto, client, "
            "method, url, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), decision, fields.get("stage"), fields.get("host"),
             fields.get("port"), fields.get("proto"), fields.get("client"),
             fields.get("method"), fields.get("url"), fields.get("reason")))
        conn.commit()
    # Mirror every decision to stdout so `docker compose logs -f control-plane`
    # (make logs-cp) is a live decision feed — the same role the egress proxy's
    # stdout audit plays. The SQLite table above stays the durable, queryable
    # record (served at /api/audit); this line is for live viewing only. Compact
    # and greppable: one line, empty fields omitted, reason after a ' :: '.
    shown = " ".join(
        f"{k}={fields[k]}" for k in
        ("stage", "host", "port", "proto", "client", "method", "url")
        if fields.get(k) is not None)
    reason = fields.get("reason")
    print(f"AUDIT {decision} {shown}" + (f" :: {reason}" if reason else ""),
          flush=True)


def _reserve_hold(approval_id: str, event: threading.Event,
                  client: str | None) -> str | None:
    """Atomically check the hold caps and, if under them, reserve a slot for this
    approval. Returns None on success (slot reserved), or a deny reason string if
    over the global or per-client cap (the caller must then fail CLOSED).

    Extracted from ``authorize`` so the cap logic is unit-testable without the
    FastAPI handler (see DESIGN.md). The whole check+reserve runs under ``_LOCK``
    so concurrent holds cannot race past the cap."""
    with _LOCK:
        over_global = len(_PENDING_EVENTS) >= MAX_PENDING
        over_client = (
            client is not None and MAX_PENDING_PER_CLIENT > 0
            and sum(1 for c in _PENDING_CLIENT.values() if c == client)
            >= MAX_PENDING_PER_CLIENT)
        if over_global or over_client:
            scope = "global" if over_global else f"client {client}"
            return f"hold capacity exceeded ({scope}) — fail-closed"
        _PENDING_EVENTS[approval_id] = event
        _PENDING_CLIENT[approval_id] = client
        return None


def _release_hold(approval_id: str) -> str | None:
    """Symmetric to ``_reserve_hold``: drop this approval's slot and return the
    human's outcome if one was recorded (else None). Under ``_LOCK`` so it is
    consistent with reservation."""
    with _LOCK:
        _PENDING_EVENTS.pop(approval_id, None)
        _PENDING_CLIENT.pop(approval_id, None)
        return _PENDING_OUTCOME.pop(approval_id, None)


def _list_pending() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, host, port, proto, client, method, url FROM approvals "
            "WHERE status='pending' ORDER BY ts").fetchall()
    return [dict(r) for r in rows]


# ── API models ──────────────────────────────────────────────────────────────

class AuthorizeRequest(BaseModel):
    host: str
    port: int | None = None
    proto: str | None = None
    client: str | None = None
    method: str | None = None
    url: str | None = None
    stage: str | None = None
    audit: bool = True


class AuthorizeResponse(BaseModel):
    decision: str                  # allow | deny  (hold is resolved internally)
    reason: str


class ResolveRequest(BaseModel):
    action: str                    # allow_once | allow_persist | deny_once | deny_persist


# ── lifecycle ───────────────────────────────────────────────────────────────

@app.on_event("startup")
def _startup() -> None:
    _init_db()
    # A held request cannot survive a restart (its blocked connection is gone),
    # so any 'pending' rows from a previous process are stale — expire them.
    with _connect() as conn:
        conn.execute(
            "UPDATE approvals SET status='expired', resolved_at=? "
            "WHERE status='pending'", (time.time(),))
        conn.commit()
    seeded = _seed_if_empty()
    if seeded:
        print(f"control-plane: seeded {seeded} allow rules from {SEED_PATH}",
              flush=True)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# ── authorize (proxy-facing) ────────────────────────────────────────────────

@app.post("/authorize", response_model=AuthorizeResponse)
def authorize(req: AuthorizeRequest) -> AuthorizeResponse:
    decision, reason = _decide(req.host)

    if decision in ("allow", "deny"):
        if req.audit:
            _audit(decision, stage=req.stage, host=req.host, port=req.port,
                   proto=req.proto, client=req.client, method=req.method,
                   url=req.url, reason=reason)
        return AuthorizeResponse(decision=decision, reason=reason)

    # HOLD (bounded): reserve a hold slot atomically with the cap check, so
    # concurrent holds can't race past the cap. Over the global or per-client cap,
    # fail CLOSED immediately rather than registering another worker-blocking hold.
    approval_id = uuid.uuid4().hex
    event = threading.Event()
    why = _reserve_hold(approval_id, event, req.client)
    if why is not None:
        _audit("deny", stage=req.stage, host=req.host, port=req.port,
               proto=req.proto, client=req.client, method=req.method,
               url=req.url, reason=why)
        return AuthorizeResponse(decision="deny", reason=why)

    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO approvals(id, ts, host, port, proto, client, method, "
            "url, status) VALUES (?,?,?,?,?,?,?,?, 'pending')",
            (approval_id, now, req.host, req.port, req.proto, req.client,
             req.method, req.url))
        conn.commit()
    _audit("hold", stage=req.stage, host=req.host, port=req.port,
           proto=req.proto, client=req.client, method=req.method, url=req.url,
           reason="held for approval")

    resolved = event.wait(HOLD_TIMEOUT)
    outcome = _release_hold(approval_id)

    if resolved and outcome:
        final, why = outcome, "human approval" if outcome == "allow" else "human rejection"
    else:
        final, why = "deny", "no decision within hold timeout — default-deny"
        # Mark expired only if still pending (resolve may have raced in).
        with _connect() as conn:
            conn.execute(
                "UPDATE approvals SET status='expired', resolved_at=? "
                "WHERE id=? AND status='pending'", (time.time(), approval_id))
            conn.commit()
    _audit(final, stage=req.stage, host=req.host, port=req.port, proto=req.proto,
           client=req.client, method=req.method, url=req.url, reason=why)
    return AuthorizeResponse(decision=final, reason=why)


# ── approvals (human-facing) ────────────────────────────────────────────────

@app.get("/approvals")
def approvals() -> list[dict]:
    return _list_pending()


@app.post("/approvals/{approval_id}/resolve")
def resolve(approval_id: str, req: ResolveRequest) -> JSONResponse:
    if req.action not in ("allow_once", "allow_persist", "deny_once", "deny_persist"):
        return JSONResponse({"ok": False, "detail": "bad action"}, status_code=400)
    outcome = "allow" if req.action.startswith("allow") else "deny"
    persist = req.action.endswith("persist")

    with _LOCK:
        event = _PENDING_EVENTS.get(approval_id)
    if event is None:
        # Already resolved, expired, or unknown — nothing to wake.
        return JSONResponse(
            {"ok": False, "detail": "not pending (expired or already resolved)"},
            status_code=409)

    with _connect() as conn:
        row = conn.execute(
            "SELECT host FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "detail": "unknown"}, status_code=404)
        updated = conn.execute(
            "UPDATE approvals SET status=?, mode=?, resolved_at=? "
            "WHERE id=? AND status='pending'",
            ("allowed" if outcome == "allow" else "denied",
             "persist" if persist else "once", time.time(), approval_id)).rowcount
        if updated and persist:
            conn.execute(
                "INSERT OR IGNORE INTO rules(pattern, action, source, created_at) "
                "VALUES (?,?, 'operator', ?)",
                (row["host"].lower(), "allow" if outcome == "allow" else "block",
                 time.time()))
        conn.commit()

    if not updated:
        return JSONResponse(
            {"ok": False, "detail": "raced — no longer pending"}, status_code=409)

    with _LOCK:
        _PENDING_OUTCOME[approval_id] = outcome
        event.set()
    return JSONResponse({"ok": True, "outcome": outcome, "persisted": persist})


@app.get("/approvals/stream")
async def approvals_stream(request: Request) -> StreamingResponse:
    """Server-sent events: push the pending-approval list whenever it changes.
    Polls SQLite once a second (a fast indexed query; the brief sync read is
    negligible on the event loop) and emits on change, plus a periodic heartbeat
    so proxies/clients can detect a dead stream."""
    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            pending = _list_pending()
            payload = json.dumps(pending)
            if payload != last:
                last = payload
                yield f"event: pending\ndata: {payload}\n\n"
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1.0)
    return StreamingResponse(gen(), media_type="text/event-stream")


# ── UI + status ─────────────────────────────────────────────────────────────

@app.get("/api/audit")
def api_audit(limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 500))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, decision, stage, host, reason FROM audit "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/status", response_class=PlainTextResponse)
def status() -> str:
    with _connect() as conn:
        rules = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    return (f"dockade control plane (2b) — {rules} rules, {audits} audit rows, "
            f"{pending} pending approvals\n")
