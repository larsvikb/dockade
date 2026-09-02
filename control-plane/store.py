# SPDX-License-Identifier: Apache-2.0
"""Control-plane storage — the SQLite store, and the writes every path shares.

This is the crown-jewel state: the policy rules that decide egress, the per-tool
rules that decide the MCP gateway's surface, the audit trail those decisions are
written to, the durable approvals rows that are the single source of truth for a
hold's outcome, and the ingest cursor. Everything else in this service reads and
writes through here.

Bottom of the dependency order: this module imports no other module of the
control plane, so the schema and its migration constraint (the NOTE below
``_init_db``) can be read without the HTTP surface around them.
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable

DB_PATH = os.environ.get("CONTROL_DB", "/var/lib/control-plane/control.db")
SEED_PATH = os.environ.get(
    "CONTROL_SEED", "/etc/control-plane/egress-allowlist.txt")

# Cap on any single ingested string. These fields are agent-INFLUENCED (a host or URL
# the sandbox asked for), and this is a trust boundary: the proxy writes them faithfully,
# including a megabyte-long URL if the agent sent one. Truncating here keeps one request
# from bloating the store or the glanceable UI list. Applied on BOTH write paths — the
# ingest (ingest.py) and this module's own ``_audit`` — which is why it lives here.
DRAIN_MAX_FIELD = 2048

# What every rule that predates the client_class column is scoped to, and what a
# rules row falls back to if an INSERT ever omits the column.
#
# It is 'sandbox' because that is the truth about those rows rather than a
# convenience: while the proxy had exactly one client population, every rule an
# operator approved and every entry in the seed allowlist was approved FOR THE AGENT.
# Backfilling them to a wildcard would have preserved today's behaviour by granting
# the whole accumulated allowlist to mcp-net, which is the thing the column exists to
# stop; backfilling to anything else would silently revoke policy a human decided.
#
# Held equal to a class name in ``policy.CLIENT_CLASSES_DEFAULT`` by a test — this
# module is the bottom of the dependency order and must not import ``policy``, so the
# two spellings are tied by the suite rather than by a shared constant.
LEGACY_CLIENT_CLASS = "sandbox"

# The schema this code expects. Every entry in ``_STEPS`` below adds exactly one,
# and a store records the version it is at (see ``_migrate``), so "what has already
# run here" is a number to compare rather than a schema to interrogate.
SCHEMA_VERSION = 1


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table``, or an empty set if it does not exist. The empty
    case is what tells ``_detect_version`` "fresh store" from "old store"."""
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _detect_version(conn: sqlite3.Connection) -> int:
    """The version of a store written BEFORE this code stamped one — the only case
    where the schema itself has to be interrogated.

    A one-time bridge, and it stays fixed-size: every store this code touches leaves
    with a stamp, so nothing beyond the two shapes below can ever arrive unstamped
    again. A new step extends ``_STEPS``, never this function.

    ``rules`` absent means no DDL has run at all — a fresh file. It is reported as
    CURRENT rather than 0 because ``_init_db``'s ``CREATE TABLE`` block writes today's
    schema directly; running the steps over it would be re-doing work the DDL already
    did."""
    rules = _columns(conn, "rules")
    if not rules:
        return SCHEMA_VERSION
    return 1 if "client_class" in rules else 0


def _step_1_client_class(conn: sqlite3.Connection) -> None:
    """v1 — policy becomes per-client-class: ``rules`` is scoped and constrained by
    (pattern, client_class), ``audit`` and ``approvals`` record the class a decision
    was made under.

    The rebuilt ``rules`` DDL here is a near-copy of the one in ``_init_db``, and the
    two have to stay identical or a migrated store and a fresh one diverge in ways
    nothing would notice until one of them hit an insert path the other had not. They
    are compared, statement to statement, by a test rather than shared as a constant —
    a shared one would have to be parameterized by table name, which is how the
    migration's temporary table would end up in the fresh store's schema.

    ``audit`` and ``approvals`` take the column NULLABLE and with no default. Those
    are records, not constraints, and a row written before classes existed genuinely
    has no class — NULL says that, where backfilling a name would put a claim in the
    audit trail that nothing observed."""
    # A REBUILD rather than an ADD COLUMN, because the constraint changes too:
    # uniqueness becomes (pattern, client_class). The old column-level
    # UNIQUE(pattern) would let one class's rule for a host block another's —
    # `INSERT OR IGNORE` in ``resolve`` would silently write nothing, report the
    # rule already present, and leave the second client held forever on a host the
    # operator believes they approved. SQLite cannot drop a column-level constraint
    # in place, so the table is rebuilt: the twelve-step procedure, minus the steps
    # that only apply to foreign keys, triggers and views (this schema has none).
    conn.execute("DROP TABLE IF EXISTS rules_migrating")
    conn.execute("""
        CREATE TABLE rules_migrating (
            id           INTEGER PRIMARY KEY,
            pattern      TEXT NOT NULL,
            action       TEXT NOT NULL,
            source       TEXT NOT NULL,
            created_at   REAL NOT NULL,
            client_class TEXT NOT NULL DEFAULT '%s',
            UNIQUE(pattern, client_class)
        )""" % LEGACY_CLIENT_CLASS)
    # `id` is carried over, not regenerated: the UI's revoke button keys on it, so
    # renumbering would aim a pending click at another rule.
    conn.execute(
        "INSERT INTO rules_migrating(id, pattern, action, source, "
        "created_at, client_class) SELECT id, pattern, action, source, "
        "created_at, ? FROM rules", (LEGACY_CLIENT_CLASS,))
    conn.execute("DROP TABLE rules")
    conn.execute("ALTER TABLE rules_migrating RENAME TO rules")
    print(f"control-plane: migrated the rules table to per-client-class policy; "
          f"existing rules are scoped to {LEGACY_CLIENT_CLASS!r}", flush=True)
    for table in ("audit", "approvals"):
        cols = _columns(conn, table)
        if cols and "client_class" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN client_class TEXT")
            print(f"control-plane: added client_class to {table} (existing rows "
                  f"keep NULL — they predate client classes)", flush=True)


# Ordered, and the order is the only thing that decides what runs: a step is applied
# when its version exceeds the store's, so steps must be APPEND-ONLY and never
# renumbered, reordered or edited once shipped — a store in the field has already run
# the old body and will never run it again. Each entry is (version, label, function),
# the label being what the operator sees in the log.
_STEPS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "per-client-class policy", _step_1_client_class),
)


def _migrate() -> None:
    """Bring an EXISTING store up to ``SCHEMA_VERSION``. Runs before the
    ``CREATE TABLE IF NOT EXISTS`` block, so on a fresh store it applies nothing and
    the DDL below is the whole definition.

    It exists because the store is a long-lived named volume that outlives container
    and image churn, and ``CREATE TABLE IF NOT EXISTS`` is a NO-OP on an existing
    table — so without this, a column added to the DDL would be missing on every
    store created before it and every statement naming it would fail at runtime.

    The version lives in SQLite's own ``user_version`` header field rather than in a
    table of ours. It needs no DDL to exist (so there is no bootstrap step that
    itself needs migrating), it is written inside the same transaction as the step it
    records, and it costs no query on the hot path — nothing reads it but this
    function. Stores that predate the stamp are placed once by ``_detect_version``.

    Its own connection in AUTOCOMMIT mode with an explicit ``BEGIN``/``COMMIT``,
    which is load-bearing rather than stylistic: Python's sqlite3 opens an implicit
    transaction for DML only, so DDL issued on a default connection runs outside one.
    The v1 rules rebuild drops a table, and a crash between the copy and the drop
    with no transaction around them loses the crown-jewel policy rules. SQLite itself
    has transactional DDL; this is what lets us use it — and it covers the stamp too,
    so a failed step leaves the version where it was and the retry is the same run."""
    with _connect() as conn:
        conn.isolation_level = None                   # explicit transaction control
        conn.execute("BEGIN IMMEDIATE")
        try:
            at = conn.execute("PRAGMA user_version").fetchone()[0]
            if at == 0:                  # unstamped: placed by shape, exactly once
                at = _detect_version(conn)
            for version, label, step in _STEPS:
                if version <= at:
                    continue
                step(conn)
                print(f"control-plane: schema v{version} applied ({label})",
                      flush=True)
                at = version
            # Not parameterizable — PRAGMA takes no placeholders — so the value is
            # forced to int rather than interpolated as it arrives.
            conn.execute(f"PRAGMA user_version = {int(at)}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    # BEFORE the DDL below, so a half-applied rebuild can never meet a
    # `CREATE TABLE IF NOT EXISTS rules` that would recreate the table empty.
    _migrate()
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                id         INTEGER PRIMARY KEY,
                pattern    TEXT NOT NULL,          -- host or .suffix (see _match)
                action     TEXT NOT NULL,          -- 'allow' | 'block'
                source     TEXT NOT NULL,          -- 'seed' | 'operator'
                created_at REAL NOT NULL,
                -- WHICH client population this rule decides for (policy._client_class).
                -- A rule is scoped, so the same pattern can be allowed for one class
                -- and unknown to another — which is the point, and why uniqueness is
                -- the PAIR. The default is mirrored from the migration deliberately,
                -- so a fresh store and a migrated one have identical schemas and an
                -- insert path cannot behave differently between them.
                client_class TEXT NOT NULL DEFAULT '%s',
                UNIQUE(pattern, client_class)
            )""" % LEGACY_CLIENT_CLASS)
        # The OTHER policy table, for the MCP gateway's surface. It is a separate
        # table rather than a scope on `rules` because the rows are a different kind,
        # not a differently keyed one — the reasoning is in DESIGN.md, "Tool policy
        # gets its own table". `action` is the only column the two share.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_rules (
                id         INTEGER PRIMARY KEY,
                -- The server the gateway DIALLED, by name. Not an address and not a
                -- client_class: those name a network and are derived from a peer
                -- address, which is the OTHER of the two identities (DESIGN.md,
                -- "Per-server identity has two different answers"). Nothing here is
                -- derived — the gateway knows the name because it used it.
                server     TEXT NOT NULL,
                -- One exact tool name. There is no wildcard and no breadth ladder:
                -- `policy._match`'s leading dot describes a host namespace, and a
                -- tool name has no hierarchy to widen along.
                tool       TEXT NOT NULL,
                action     TEXT NOT NULL,          -- 'allow' | 'deny' | 'ask'
                -- 'operator' is the only value today, and the column is here anyway:
                -- provenance on a policy row is what the rules view labels, and a
                -- COLUMN is the expensive kind to add to a long-lived store (the NOTE
                -- below `_init_db`) where this whole table was free.
                source     TEXT NOT NULL,
                created_at REAL NOT NULL,
                -- Uniqueness is the PAIR, because tool names are not namespaced
                -- across servers: two servers can each expose an `issue_read`, and
                -- the server half is what stops one server's policy deciding for
                -- the other's identically named tool.
                UNIQUE(server, tool)
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
                -- The class ``client`` was placed in when the decision was made, kept
                -- rather than re-derived: the CIDR map is configuration and can change,
                -- so re-deriving would relabel history under today's topology. NULL is
                -- reachable on a migrated store (rows that predate classes) and on an
                -- unclassified client, and means exactly that.
                client_class TEXT,
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
                -- Settled when the hold is raised and read back by ``resolve``, so a
                -- persisted rule is scoped to the class the request was DECIDED under
                -- rather than to a second derivation made at the click.
                client_class TEXT,
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


# NOTE for whoever adds the next column — THREE edits, and all three are required:
#   1. the DDL above, which is what a fresh store gets;
#   2. a new `_step_N` above, which is what every store already in the field gets;
#   3. `SCHEMA_VERSION` and `_STEPS`, bumped and appended by one.
# `CREATE TABLE IF NOT EXISTS` is a NO-OP on an existing table — it silently does not
# add columns — and this store is a long-lived named volume that deliberately outlives
# container and image churn, so a column added at (1) alone is MISSING on any store
# created before it and every statement naming it then fails at runtime. The only
# alternative is `make destroy`, which discards the policy rules and the audit history.
#
# The step's shape is the only real question: an additive nullable column is an
# `ALTER TABLE ADD COLUMN`; anything that changes a CONSTRAINT is a table rebuild,
# because SQLite cannot alter one in place (see `_step_1_client_class`).
#
# Take a `make backup` before deploying a step that rebuilds a table. The transaction
# makes a FAILED migration safe; it does nothing about one that succeeds and turns out
# to be wrong.


def _seed_if_empty() -> int:
    """Load the seed file into an empty rules table. Idempotent: once any rule
    exists the store is authoritative and the file is never re-read.

    Every seeded rule is scoped to ``LEGACY_CLIENT_CLASS``, written EXPLICITLY rather
    than left to the column default. The seed file is the agent's transitional
    allowlist — package registries and GitHub, pending the cache and git paths (see
    DESIGN.md) — so scoping it to the agent is what it means, and stating it here is
    what keeps the file from quietly becoming policy for every future client
    population. A seed entry for another class would need a syntax the file does not
    have; that is a decision for whoever first needs one."""
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
            "INSERT OR IGNORE INTO rules(pattern, action, source, created_at, "
            "client_class) VALUES (?, 'allow', 'seed', ?, ?)",
            [(p, now, LEGACY_CLIENT_CLASS) for p in patterns])
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
            "client_class, method, url, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), decision, cap(fields.get("stage")), cap(fields.get("host")),
             fields.get("port"), cap(fields.get("proto")), cap(fields.get("client")),
             cap(fields.get("client_class")),
             cap(fields.get("method")), cap(fields.get("url")), cap(fields.get("reason"))))
        conn.commit()
    # Mirror every decision to stdout so `docker compose logs -f control-plane`
    # (make logs-cp) is a live decision feed — the same role the egress proxy's
    # stdout audit plays. The SQLite table above stays the durable, queryable
    # record (served at /api/audit); this line is for live viewing only. Compact
    # and greppable: one line, empty fields omitted, reason after a ' :: '.
    shown = " ".join(
        f"{k}={fields[k]}" for k in
        ("stage", "host", "port", "proto", "client", "client_class", "method", "url")
        if fields.get(k) is not None)
    reason = fields.get("reason")
    print(f"AUDIT {decision} {shown}" + (f" :: {reason}" if reason else ""),
          flush=True)
