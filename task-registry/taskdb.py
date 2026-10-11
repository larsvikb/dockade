# SPDX-License-Identifier: Apache-2.0
"""The task registry's SQLite store: the schema, the transaction every write runs in,
the history row every write leaves, and who may be named as a write's author.

Bottom of the dependency order: imports no other module of the registry.

Named ``taskdb`` rather than ``store`` because the test suite loads every component
into one process, and siblings import each other by plain name: a second ``store``
would resolve to the control plane's."""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator

DB_PATH = os.environ.get("TASKS_DB", "/var/lib/task-registry/tasks.db")

# _SCHEMA creates what is missing and alters nothing: `IF NOT EXISTS` adds no column
# to a table that exists. Changing an existing table takes a version bump and a
# migration.
SCHEMA_VERSION = 1

#: The author of every write the operator makes, and of the registry's own (an expired
#: claim swept away). No agent id can equal either: an agent id is a kind, a hyphen and
#: an id, and `check_agent` refuses these words as a kind.
OPERATOR = "operator"
REGISTRY = "registry"

# `claude-<session uuid>`: the kind says which agent's id follows, so the registry
# needs no knowledge of any one agent.
_AGENT_RE = re.compile(r"(?P<kind>[a-z][a-z0-9]{1,15})-(?P<id>[0-9A-Za-z][0-9A-Za-z_-]{5,63})")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    remote      TEXT,
    retired_at  REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    -- AUTOINCREMENT: an id is never reused, so a stale reference cannot land on a
    -- newer task.
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER NOT NULL REFERENCES repos(id),
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    state       TEXT NOT NULL,
    severity    TEXT,
    kind        TEXT,
    parent_id   INTEGER REFERENCES tasks(id),
    revision    INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_by_repo_state ON tasks(repo_id, state);
CREATE INDEX IF NOT EXISTS tasks_by_parent ON tasks(parent_id);
CREATE TABLE IF NOT EXISTS claims (
    -- The primary key is the task: one holder at a time.
    task_id     INTEGER PRIMARY KEY REFERENCES tasks(id),
    holder      TEXT NOT NULL,
    branch      TEXT NOT NULL,
    -- sha256 of the token, which only the claim's reply ever carries.
    token_hash  TEXT NOT NULL,
    footprint   TEXT NOT NULL,
    progress    TEXT,
    claimed_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blockers (
    task_id     INTEGER NOT NULL REFERENCES tasks(id),
    blocker_id  INTEGER NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, blocker_id)
);
CREATE TABLE IF NOT EXISTS notes (
    -- AUTOINCREMENT, as for tasks: hiding names a note by id.
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL REFERENCES tasks(id),
    author       TEXT NOT NULL,
    text         TEXT NOT NULL,
    for_operator INTEGER NOT NULL,
    -- The claim move the note came with (`submit`, `send_back`, `release`), if any.
    move         TEXT,
    -- The flagged note this one answers: the operator's reply when marking it handled.
    reply_to     INTEGER REFERENCES notes(id),
    created_at   REAL NOT NULL,
    -- Set by the operator: the note stays in the record and leaves agents' reads.
    hidden_at    REAL,
    -- Set by the operator on a note marked for it: dealt with, and off its list.
    handled_at   REAL
);
CREATE INDEX IF NOT EXISTS notes_by_task ON notes(task_id, id);
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    author      TEXT NOT NULL,
    action      TEXT NOT NULL,
    task_id     INTEGER REFERENCES tasks(id),
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_by_task ON history(task_id);
"""


class Refused(Exception):
    """A request the registry will not carry out. ``current`` is what is there now,
    so the caller can correct itself without a second read."""

    def __init__(self, reason: str, current: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.current = current


def init() -> None:
    """Create the store, or open one this code's schema version wrote."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise RuntimeError(
                f"{DB_PATH} is at schema v{version}; this code knows v{SCHEMA_VERSION}")
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextlib.contextmanager
def reading() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def writing() -> Iterator[sqlite3.Connection]:
    """One transaction holding the write lock from its first statement, so a check
    and the write it allows cannot be split by another writer."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def record(conn: sqlite3.Connection, author: str, action: str, task_id: int | None,
           **detail: object) -> None:
    """The history row a write leaves, in the write's own transaction."""
    conn.execute(
        "INSERT INTO history(ts, author, action, task_id, detail) VALUES (?, ?, ?, ?, ?)",
        (time.time(), author, action, task_id, json.dumps(detail, sort_keys=True)))


def storable(text: str) -> bool:
    """Whether SQLite can store ``text``. A lone surrogate, which JSON can carry as
    ``\\ud800``, has no UTF-8 form, and binding it raises instead of refusing."""
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


def check_agent(agent: str) -> str:
    """``agent``, if it has the shape of an agent id: a shape, not a proof
    (``task-registry/DESIGN.md``, "Who an agent is")."""
    match = _AGENT_RE.fullmatch(agent or "")
    if match is None or match["kind"] in (OPERATOR, REGISTRY):
        raise Refused(
            f"{agent!r} is not an agent id: the agent's kind, a hyphen and its own "
            f"session id, as in claude-<session id>")
    return agent
