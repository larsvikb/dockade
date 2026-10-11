# SPDX-License-Identifier: Apache-2.0
"""Notes: what sessions and the operator say about a task, appended and never
changed. The checks, the plain write and the shape a note is read in; who may write
one is in ``agent_moves`` and ``operator_moves``."""
from __future__ import annotations

import sqlite3
import time

import tasks
from taskdb import Missing

NOTE_MAX = 4000

#: How many of a task's latest notes a read of the task carries, beside the total.
IN_READ = 20


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
