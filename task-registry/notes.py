# SPDX-License-Identifier: Apache-2.0
"""Notes: what sessions and the operator say about a task, appended and never
changed. The checks, the plain write and the shape a note is read in; who may write
one is in ``agent_moves`` and ``operator_moves``."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time

import tasks
from taskdb import Missing

NOTE_MAX = 4000

#: How many of a task's latest notes a read of the task carries, beside the total, and
#: how many new ones an unchanged read carries before it is a full read instead.
IN_READ = 20

#: A notes mark, as ``mark`` makes one: the task's latest visible note id, a dot, and a
#: digest of every visible note up to it. The pattern caps the id at the 19 digits of
#: ``tasks.ID_MAX``, so ``int`` never meets one too long to convert; ``reads._known``
#: bounds it by ``tasks.ID_MAX`` itself.
MARK_RE = re.compile(r"(?P<latest>0|[1-9][0-9]{0,18})\.(?P<digest>[0-9a-f]{16})")


def check_note(text: object, *, required: bool = True) -> None:
    tasks.check_text(text, NOTE_MAX, "a note", required=required)


def post(conn: sqlite3.Connection, task: int, author: str, text: str,
         for_operator: bool = False, move: str | None = None,
         reply_to: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO notes(task_id, author, text, for_operator, move, reply_to, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task, author, text, int(for_operator), move, reply_to, time.time()))
    return cur.lastrowid


def note_row(conn: sqlite3.Connection, note: int) -> sqlite3.Row:
    tasks.check_id(note, "note")
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note,)).fetchone()
    if row is None:
        raise Missing(f"no note {note}")
    return row


def shown(row: sqlite3.Row, *, with_hidden: bool = False) -> dict:
    """A note as a read returns it: data, with its author and time, never text
    spliced into anything else."""
    note = {"id": row["id"], "task": row["task_id"], "author": row["author"],
            "text": row["text"], "for_operator": bool(row["for_operator"]),
            "move": row["move"], "reply_to": row["reply_to"], "at": row["created_at"],
            "handled": row["handled_at"] is not None}
    if with_hidden:
        note["hidden"] = row["hidden_at"] is not None
    return note


def visible(conn: sqlite3.Connection, task: int, before: int | None = None,
            limit: int = IN_READ) -> list[dict]:
    """Up to ``limit`` of the task's notes agents may read, the latest first chosen,
    returned oldest first; with ``before``, those older than that note."""
    rows = conn.execute(
        "SELECT * FROM notes WHERE task_id = ? AND hidden_at IS NULL "
        "AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
        (task, before, before, limit)).fetchall()
    return [shown(r) for r in reversed(rows)]


def count(conn: sqlite3.Connection, task: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM notes WHERE task_id = ? AND "
                        "hidden_at IS NULL", (task,)).fetchone()[0]


def mark(conn: sqlite3.Connection, task: int) -> str:
    """What an agent's copy of the task's notes is, so a later read can tell it is
    still current: hiding, showing and the handled mark move no revision. A note's text
    never changes, so a note's id and handled flag stand for all of it."""
    latest = conn.execute("SELECT MAX(id) FROM notes WHERE task_id = ? AND "
                          "hidden_at IS NULL", (task,)).fetchone()[0] or 0
    return f"{latest}.{digest(conn, task, latest)}"


def digest(conn: sqlite3.Connection, task: int, latest: int) -> str:
    """A digest of the task's visible notes up to note ``latest``; the task's id is in
    it, so no other task's mark can match."""
    found = hashlib.sha256(str(task).encode())
    for row in conn.execute(
            "SELECT id, handled_at IS NOT NULL AS handled FROM notes WHERE task_id = ? "
            "AND hidden_at IS NULL AND id <= ? ORDER BY id", (task, latest)):
        found.update(f",{row['id']}:{row['handled']}".encode())
    return found.hexdigest()[:16]
