# SPDX-License-Identifier: Apache-2.0
"""Control-plane storage — the SQLite store, and the writes every path shares.

This is the crown-jewel state: the policy rules that decide egress, the timed
leases that decide it for a while, the per-tool rules that decide the MCP
gateway's surface, the audit trail those decisions are written to, the durable
approvals rows that are the single source of truth for a hold's outcome, and the
ingest cursor. Everything else in this service reads and writes through here.

Bottom of the dependency order: this module imports no other module of the
control plane, so the schema and its migration constraint (the NOTE below
``_init_db``) can be read without the HTTP surface around them.
"""
from __future__ import annotations

import os
import re
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
SCHEMA_VERSION = 6


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


# The TIMED GRANTS: an `allow_lease` decision, which allows one host for one client
# class until it expires. A third grant duration between `*_once` (this request) and
# `*_persist` (standing policy) — see DESIGN.md, "A lease is the third grant duration".
#
# ONE definition, shared by the v2 step and the fresh-store DDL, where the v1 rules
# rebuild deliberately kept two near-copies. The difference is that this is a NEW
# table: there is no constraint to change, so the step has no temporary table to
# parameterize a shared string by, which was the whole reason `_step_1_client_class`
# could not share one. Sharing here makes the divergence a test had to catch there
# impossible instead.
_LEASES_DDL = """
    CREATE TABLE IF NOT EXISTS leases (
        id           INTEGER PRIMARY KEY,
        -- ONE exact host, in `policy._normalize_host` form, matched by EQUALITY.
        -- Deliberately not a `pattern`: the breadth ladder on `rules` exists because
        -- a standing rule needs an operator choice about how wide it is, and a lease
        -- answers that question by expiring instead. So there is no leading-dot
        -- wildcard here and `policy._match` is not used on this column.
        host         TEXT NOT NULL,
        -- WHICH client population this grant covers, exactly as on `rules` and for
        -- the same reason. NOT NULL with no default: `resolve` refuses to lease for an
        -- unclassified client, so a row with no class cannot be written, and the
        -- constraint says so rather than leaving a fallback nobody meant.
        client_class TEXT NOT NULL,
        -- The card this was granted from. Provenance a rule has no equivalent of, and
        -- it is what makes the trail joinable end to end: the approvals row says what
        -- was asked and who answered, this says what the answer granted, and the audit
        -- rows say what rode it.
        approval_id  TEXT NOT NULL,
        created_at   REAL NOT NULL,
        expires_at   REAL NOT NULL,
        granted_by   TEXT NOT NULL   -- provenance of the resolver (app._actor)
        -- NO UNIQUE constraint, and that is a consequence rather than an omission. A
        -- second lease for a host already covered by a live one is UNREACHABLE: the
        -- live lease would have decided the request in `policy._decide`, so no card
        -- would have been raised to grant from. Every row that can be written is
        -- therefore a genuinely new grant, and a constraint would only force a choice
        -- between ignoring and extending that nothing can ever make.
    )"""


def _step_2_leases(conn: sqlite3.Connection) -> None:
    """v2 — timed grants get their own table (``_LEASES_DDL``).

    The cheapest shape a step can have: a new table, so nothing existing is read,
    rewritten or constrained differently, and a store that has never leased anything
    is fully migrated by creating it empty. Contrast ``_step_1_client_class``, which
    had to rebuild the crown-jewel rules table to change a constraint.

    A separate table rather than an ``expires_at`` column on ``rules``, because the
    rows are a different KIND — the same test that gave tool policy its own table
    (DESIGN.md). The consequence that decided it: every existing reader of ``rules``
    is a reader of standing policy, and a nullable expiry on it would mean the rules
    view, the conflict check in ``resolve``, ``revoke_rule``, the edit path and
    ``policy._decide`` each had to learn "except the expired ones" — five places to
    drift instead of one new pass."""
    conn.execute(_LEASES_DDL)


def _step_3_tool_correlation(conn: sqlite3.Connection) -> None:
    """v3 — tool decisions become joinable: ``audit`` gains ``server``, ``tool`` and
    ``approval_id`` (the DDL in ``_init_db`` carries the reasoning).

    Three ``ALTER TABLE ADD COLUMN``s and nothing else, which is the cheapest step
    shape after ``_step_2_leases``: no constraint changes, so none of the twelve-step
    rebuild ``_step_1_client_class`` needed. The loop and the message are modelled on
    that step's own tail, which added ``client_class`` to these same tables.

    Existing rows keep NULL and are NOT backfilled. The values are derivable from
    ``reason`` prose for some of them — but only some, and only by parsing a sentence
    that has been reworded before. A column filled by re-reading old prose would put a
    claim in the audit trail that nothing observed, which is the same objection that
    kept ``client_class`` from being backfilled to a wildcard."""
    added = [column for column in ("server", "tool", "approval_id")
             if column not in _columns(conn, "audit")]
    for column in added:
        conn.execute(f"ALTER TABLE audit ADD COLUMN {column} TEXT")
    if added:
        print(f"control-plane: added {', '.join(added)} to audit (existing rows keep "
              f"NULL — they predate tool correlation)", flush=True)


def _step_4_tool_status(conn: sqlite3.Connection) -> None:
    """v4 — ``audit.status``, for how a tool call ended (the DDL carries the reasoning).

    Split from v3 rather than added with the other three, and the split was deliberate:
    when the correlation columns landed nothing could write this one, and a column no
    writer fills is schema that documents an intention rather than a record. It gets a
    step of its own now that the gateway's stream exists to fill it."""
    if "status" not in _columns(conn, "audit"):
        conn.execute("ALTER TABLE audit ADD COLUMN status TEXT")
        print("control-plane: added status to audit (existing rows keep NULL — they "
              "predate the gateway's outcome stream)", flush=True)


def _step_5_decision_to_kind(conn: sqlite3.Connection) -> None:
    """v5 — ``audit.decision`` becomes ``audit.kind`` (the DDL carries the reasoning).

    A RENAME, not a new column and a copy. `ALTER TABLE ... RENAME COLUMN` has been in
    SQLite since 3.25 and keeps the data in place, so there is no window where the two
    disagree and nothing to backfill — which matters more here than usual, since the
    table being migrated is the audit trail itself and a copy that half-ran would leave
    rows whose kind was invented by a migration rather than recorded by a writer.

    Guarded on the column still being there so a store already migrated is left alone:
    the version stamp should make that unreachable, but a rename that runs twice raises
    rather than no-ops, and this table is the one worth being paranoid about."""
    columns = _columns(conn, "audit")
    if "decision" in columns and "kind" not in columns:
        conn.execute("ALTER TABLE audit RENAME COLUMN decision TO kind")
        print("control-plane: renamed audit.decision to audit.kind (several of its "
              "values were never decisions)", flush=True)


# Ordered, and the order is the only thing that decides what runs: a step is applied
# when its version exceeds the store's, so steps must be APPEND-ONLY and never
# renumbered, reordered or edited once shipped — a store in the field has already run
# the old body and will never run it again. Each entry is (version, label, function),
# the label being what the operator sees in the log.
def _step_6_persisted_pattern(conn: sqlite3.Connection) -> None:
    """v6 — ``approvals.pattern``, the rule a `persist` decision wrote (the DDL
    carries the reasoning). One nullable ``ALTER TABLE ADD COLUMN``, the cheapest
    step shape. Existing rows keep NULL: the pattern an old click stored is
    recoverable from ``rules`` only by guessing, and a guess is not a record."""
    if "pattern" not in _columns(conn, "approvals"):
        conn.execute("ALTER TABLE approvals ADD COLUMN pattern TEXT")
        print("control-plane: added pattern to approvals (existing rows keep NULL — "
              "they predate the column)", flush=True)


_STEPS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "per-client-class policy", _step_1_client_class),
    (2, "timed grants (leases)", _step_2_leases),
    (3, "tool correlation columns", _step_3_tool_correlation),
    (4, "tool outcome status", _step_4_tool_status),
    (5, "audit.decision becomes audit.kind", _step_5_decision_to_kind),
    (6, "persisted pattern on approvals", _step_6_persisted_pattern),
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
        # The servers whose tools `tool_rules` decides for. Compose DECLARES a server
        # and a human brings the container up; this table is the other half of that
        # split — which of the running servers is enabled, and how the gateway
        # authenticates to it (DESIGN.md, "The control plane configures servers; it
        # never starts them"). Nothing here starts anything, and nothing here grants:
        # the row is configuration, and the credential it describes lives outside this
        # store entirely.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mcp_servers (
                -- The name the gateway dials, which is also the container's name. The
                -- natural key: the gateway has no address for a server and needs none,
                -- so there is no id for this to be looked up by instead.
                server        TEXT PRIMARY KEY,
                -- 0 until an operator says otherwise. Registering a server is not
                -- enabling it, for the reason every default here points the same way.
                enabled       INTEGER NOT NULL DEFAULT 0,
                -- The AUTH DESCRIPTOR: enough to build a request, useless to steal.
                -- 'none' is the default and the preferred case — a server holding its
                -- own env credential, which is FARTHER from the agent than one the
                -- agent-facing gateway holds. 'header' is for a server that forces
                -- per-request injection, as GitHub's does in http mode.
                auth_type     TEXT NOT NULL DEFAULT 'none',   -- 'none' | 'header'
                auth_header   TEXT,             -- e.g. 'Authorization'
                auth_template TEXT,             -- e.g. 'Bearer {secret}'
                created_at    REAL NOT NULL
                -- NOTE what is absent: any reference to the secret. The gateway reads
                -- exactly `/run/dockade/secrets/<server>.json`, DERIVED from the
                -- name above, because a stored free-text path would let a forged
                -- config write point one server at another server's credential. Making
                -- that impossible beats validating against it — the move
                -- `_persist_candidates` already makes for egress patterns.
            )""")
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
                -- WHAT KIND of thing this row records, not what was decided: four
                -- of its values are not decisions anyone made (`observe` is a
                -- server's claim about itself, `outcome` is how a call ended, and
                -- create/edit/revoke are configuration changes). The vocabulary lives
                -- in `audit.KINDS`.
                --
                -- NOT the same word as the `decision` in AuthorizeResponse and on the
                -- tool bridge. That one really is a decision — the answer to a policy
                -- question — and keeps its name.
                kind     TEXT NOT NULL,
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
                reason   TEXT,
                -- The TOOL columns. Everything above is egress vocabulary, and tool
                -- decisions used to borrow it by writing "issue_read on mcp-github:
                -- ..." into `reason` — which reads fine and joins to nothing. These
                -- three exist so the trail can be QUERIED rather than grepped: which
                -- rows concern one server, and which rows belong to one approval.
                --
                -- `approval_id` is the one that earns the set. A tool ask writes rows
                -- at four separate moments (the hold, the human's click, the claim,
                -- and — once the gateway's stream lands — how the call ended), and
                -- without a column they are joinable only by parsing prose, so the UI
                -- cannot put an outcome on the card that produced it.
                --
                -- NULL on every egress row and on every tool row written before v3,
                -- for the reason `client_class` is nullable: these are records, not
                -- constraints, and a row that predates the column genuinely has no
                -- value for it.
                server       TEXT,
                tool         TEXT,
                approval_id  TEXT,
                -- How the call ENDED, from the gateway's own stream (ingest.py). Its
                -- vocabulary is `ingest.TOOL_STATUSES`, not `decision`'s: an outcome
                -- is not a decision anyone made, and `ok` / `tool-error` /
                -- `transport-error` / `timeout` answer a different question than
                -- allow/deny/hold. NULL on every row that is not a tool outcome.
                status       TEXT
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
                -- How far the decision REACHED: this request, a timed lease, or
                -- standing policy. A plain TEXT column, which is why the lease needed
                -- no step of its own here — a third value costs nothing where a third
                -- column would have cost a migration.
                mode        TEXT,             -- once | lease | persist
                resolved_at REAL,
                resolved_by TEXT,             -- provenance of the resolver (_actor)
                -- WHAT a `persist` decision wrote: the rule pattern the operator chose
                -- from the breadth ladder. Without it the record of a click that
                -- changed standing policy said only which HOST was allowed, and a
                -- `.co.uk` chosen from the ladder left a trail reading
                -- "allow api.example.co.uk". NULL for once/lease and for every row
                -- that predates the column.
                pattern     TEXT
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status)")
        # Timed grants. No index: `policy._decide` reads this table only when no rule
        # decided the request, and it holds at most a handful of rows — one per live
        # lease, swept on the grant path (see the lease branch in `resolve`). An index
        # on a table that small buys nothing and would be one more thing to keep true.
        conn.execute(_LEASES_DDL)
        # The tool surface's approvals, which split from the table above for the
        # reason the rules did: the rows are egress-shaped there — host, port, proto,
        # method, url — against a server, a tool and a payload here (DESIGN.md,
        # "``approvals`` splits the same way").
        #
        # What does NOT split is the operator's pending queue; these rows and those
        # are two builders behind one list.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_approvals (
                id          TEXT PRIMARY KEY,
                ts          REAL NOT NULL,
                server      TEXT NOT NULL,
                tool        TEXT NOT NULL,
                -- The complete arguments, in the ONE canonical form that is also what
                -- `args_digest` covers and what the human is shown
                -- (holds._canonical_args). Nothing is dropped, summarized or
                -- truncated: this is what someone reads to decide, so a payload
                -- trimmed on the way in would hide exactly the part worth hiding. An
                -- oversized one is refused instead (holds.TOOL_ARGS_MAX).
                args_json   TEXT NOT NULL,
                -- What the grant is BOUND to, and the key an identical retry joins on.
                -- Over the canonical form of the arguments, so a payload that differs
                -- only in key order is the same ask, while one that differs in any
                -- VALUE is a different ask and cannot ride this approval.
                args_digest TEXT NOT NULL,
                client      TEXT,
                status      TEXT NOT NULL,   -- pending | allowed | denied | expired
                -- Durable, where the egress deadline lives only in `holds._PENDING_
                -- DEADLINE`. Nothing is blocked on a tool ask, so there is no worker
                -- whose timeout would enforce a window and no in-process state to
                -- lose: expiry is decided by reading this column (holds.
                -- ``_expire_tool_asks``), which also means a restart cannot resurrect
                -- an ask as pending forever.
                deadline    REAL NOT NULL,
                resolved_at REAL,
                resolved_by TEXT,            -- provenance of the resolver (_actor)
                -- Set when the gateway EXECUTES this ask, which happens on resumption
                -- rather than at the human's click. It is what makes an approval
                -- single-use: the claim is a conditional UPDATE, so two resumptions
                -- of one approved ask cannot both run the side effect.
                claimed_at  REAL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS tool_approvals_status "
                     "ON tool_approvals(status)")
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


#: What ends or rewrites a terminal line: C0 and C1 controls, DEL, and the Unicode
#: line and paragraph separators. The gateway's outcome stream has the same guard.
_UNPRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _printable(value: object) -> str:
    """One field of a live-feed line, capped and on one line. The feed carries text
    from the far side of every trust boundary this store has — a host the agent
    named, a third-party server's error — and a newline in it would let that text
    append a forged `AUDIT …` line to `make logs-cp`. Escaped rather than stripped,
    so what arrived stays visible."""
    text = str(value)[:DRAIN_MAX_FIELD]
    return _UNPRINTABLE.sub(lambda m: m.group().encode("unicode_escape").decode("ascii"),
                            text)


def _audit(kind: str, **fields) -> None:
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
            "INSERT INTO audit(ts, kind, stage, host, port, proto, client, "
            "client_class, method, url, reason, server, tool, approval_id, "
            "status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), kind, cap(fields.get("stage")), cap(fields.get("host")),
             fields.get("port"), cap(fields.get("proto")), cap(fields.get("client")),
             cap(fields.get("client_class")),
             cap(fields.get("method")), cap(fields.get("url")), cap(fields.get("reason")),
             cap(fields.get("server")), cap(fields.get("tool")),
             cap(fields.get("approval_id")), cap(fields.get("status"))))
        conn.commit()
    # Mirror every decision to stdout so `docker compose logs -f control-plane`
    # (make logs-cp) is a live decision feed — the same role the egress proxy's
    # stdout audit plays. The SQLite table above stays the durable, queryable
    # record (served at /api/audit); this line is for live viewing only. Compact
    # and greppable: one line, empty fields omitted, reason after a ' :: '.
    # The tool columns are mirrored too, so a grep for one approval id finds every row
    # that belongs to it in the live feed — which is the whole point of the columns and
    # would be lost if the stdout stream still only carried the id inside prose on the
    # one row whose sentence happens to name it.
    shown = " ".join(
        f"{k}={_printable(fields[k])}" for k in
        ("stage", "host", "port", "proto", "client", "client_class", "method", "url",
         "server", "tool", "approval_id")
        if fields.get(k) is not None)
    reason = fields.get("reason")
    print(f"AUDIT {kind} {shown}" + (f" :: {_printable(reason)}" if reason else ""),
          flush=True)
