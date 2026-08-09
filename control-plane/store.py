# SPDX-License-Identifier: Apache-2.0
"""Control-plane storage — the SQLite store, and the writes every path shares.

This is the crown-jewel state: the policy rules that decide egress, the audit
trail those decisions are written to, the durable approvals rows that are the
single source of truth for a hold's outcome, and the ingest cursor. Everything
else in this service reads and writes through here.

Bottom of the dependency order: this module imports no other module of the
control plane, so the schema and its migration constraint (the NOTE below
``_init_db``) can be read without the HTTP surface around them.
"""
from __future__ import annotations

import os
import sqlite3
import time

DB_PATH = os.environ.get("CONTROL_DB", "/var/lib/control-plane/control.db")
SEED_PATH = os.environ.get(
    "CONTROL_SEED", "/etc/control-plane/egress-allowlist.txt")

# Cap on any single ingested string. These fields are agent-INFLUENCED (a host or URL
# the sandbox asked for), and this is a trust boundary: the proxy writes them faithfully,
# including a megabyte-long URL if the agent sent one. Truncating here keeps one request
# from bloating the store or the glanceable UI list. Applied on BOTH write paths — the
# ingest (ingest.py) and this module's own ``_audit`` — which is why it lives here.
DRAIN_MAX_FIELD = 2048


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
        # This table grows without bound — every decision is kept, and only
        # /api/audit's VIEW is windowed (see api_audit), never the record itself.
        # That is deliberate: the trail is the artifact. There is no automatic
        # rotation; an operator thins it on their own schedule with `make
        # audit-prune`, which deletes rows past a retention window and VACUUMs to
        # return the disk. The `audit_ts` index above is what keeps that DELETE cheap
        # (and `make destroy` remains the separate, whole-store reset).
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
                resolved_at REAL,
                resolved_by TEXT              -- provenance of the resolver (_actor)
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status)")
        # Ingest cursor for the egress proxy's audit file (see _drain_egress_audit).
        # A NEW TABLE, deliberately — not a column on an existing one — so it needs
        # no migration on the long-lived store (read the note below this function).
        # `inode` is what distinguishes a rotated/replaced file from an appended one;
        # without it a fresh file inherits the old offset and its first N bytes are
        # never ingested.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_cursor (
                path   TEXT PRIMARY KEY,
                inode  INTEGER NOT NULL,
                offset INTEGER NOT NULL
            )""")
        conn.commit()


# NOTE for whoever adds the next column: there is NO migration step here, and that
# is only safe because of a one-time circumstance. `CREATE TABLE IF NOT EXISTS` is a
# NO-OP on an existing table — it silently does not add columns — and this store is a
# long-lived named volume that deliberately outlives container and image churn, so a
# new column is MISSING on any store created before it and every statement naming it
# then fails at runtime. `resolved_by` got away without one because the single store
# that predated it was migrated in place (ALTER TABLE ADD COLUMN) before this was
# removed, and every store created since gets the column from the DDL above. So a
# future additive column needs its own explicit ALTER for existing volumes; the only
# alternative is `make destroy`, which discards the policy rules and the audit
# history. (Restoring a pre-`resolved_by` backup of the volume would likewise need
# that ALTER run by hand.)


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


def _audit(decision: str, **fields) -> None:
    # Agent-INFLUENCED fields (host/url/... arrive on /authorize from the proxy, which
    # relays whatever the sandbox asked for) are truncated on write — the same
    # trust-boundary cap the ingest path applies (DRAIN_MAX_FIELD). Without it a
    # megabyte-long URL on a single request would bloat the crown-jewel store and the
    # glanceable /api/audit list. Non-string fields (port) and the server-set decision
    # pass through untouched.
    def cap(v):
        return v[:DRAIN_MAX_FIELD] if isinstance(v, str) else v
    with _connect() as conn:
        conn.execute(
            "INSERT INTO audit(ts, decision, stage, host, port, proto, client, "
            "method, url, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), decision, cap(fields.get("stage")), cap(fields.get("host")),
             fields.get("port"), cap(fields.get("proto")), cap(fields.get("client")),
             cap(fields.get("method")), cap(fields.get("url")), cap(fields.get("reason"))))
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
