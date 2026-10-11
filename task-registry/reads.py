# SPDX-License-Identifier: Apache-2.0
"""What any surface may read: a repo's tasks, one task in full, a task's history, and
the repo list. Nothing here writes."""
from __future__ import annotations

import json
import sqlite3

import claims
import taskdb
import tasks


def list_tasks(repo: str, state: str | None = "open", holder: str | None = None,
               parent: int | None = None) -> list[dict]:
    """A repo's tasks in ``state`` (every state for ``None``), optionally only those
    ``holder`` has a live claim on, or the children of ``parent``. A task that is
    ``ready`` is in the ready queue: open, unclaimed, and blocked by nothing."""
    if state is not None:
        tasks.check_state(state)
    if parent is not None:
        tasks.check_id(parent)
    with taskdb.reading() as conn:
        query = ("SELECT t.*, r.name AS repo FROM tasks t JOIN repos r ON r.id = t.repo_id "
                 "WHERE t.repo_id = ?")
        args: list[object] = [tasks.repo_id(conn, repo)]
        if state is not None:
            query += " AND t.state = ?"
            args.append(state)
        if parent is not None:
            query += " AND t.parent_id = ?"
            args.append(parent)
        found = [_summary(conn, row) for row in conn.execute(query + " ORDER BY t.id", args)]
    if holder is not None:
        found = [t for t in found if t["claim"] and t["claim"]["holder"] == holder]
    return found


def get_task(task: int) -> dict:
    with taskdb.reading() as conn:
        return detail(conn, tasks.task_row(conn, task))


def history(task: int) -> list[dict]:
    """Every write a task has had, oldest first, with its author."""
    with taskdb.reading() as conn:
        tasks.task_row(conn, task)
        return [{"ts": r["ts"], "author": r["author"], "action": r["action"],
                 "detail": json.loads(r["detail"])}
                for r in conn.execute(
                    "SELECT * FROM history WHERE task_id = ? ORDER BY id", (task,))]


def repos() -> list[dict]:
    with taskdb.reading() as conn:
        return [{"name": r["name"], "remote": r["remote"],
                 "retired": r["retired_at"] is not None}
                for r in conn.execute("SELECT * FROM repos ORDER BY name")]


def _summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return _summarize(conn, row, claims.live(conn, row["id"]))


def _summarize(conn: sqlite3.Connection, row: sqlite3.Row,
               held: sqlite3.Row | None) -> dict:
    blocked = conn.execute(
        "SELECT 1 FROM blockers b JOIN tasks t ON t.id = b.blocker_id "
        "WHERE b.task_id = ? AND t.state != 'closed' LIMIT 1", (row["id"],)).fetchone()
    claim = None if held is None else {
        "holder": held["holder"], "branch": held["branch"],
        "progress": held["progress"], "expires_at": held["expires_at"]}
    return {"id": row["id"], "repo": row["repo"], "title": row["title"],
            "state": row["state"], "severity": row["severity"], "kind": row["kind"],
            "parent": row["parent_id"], "blocked": blocked is not None,
            "claim": claim,
            "ready": row["state"] == "open" and claim is None and blocked is None,
            "revision": row["revision"]}


def detail(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    def brief(query: str) -> list[dict]:
        return [{"id": r["id"], "title": r["title"], "state": r["state"]}
                for r in conn.execute(query, (row["id"],))]

    parent = brief("SELECT p.id, p.title, p.state FROM tasks t "
                   "JOIN tasks p ON p.id = t.parent_id WHERE t.id = ?")
    held = claims.live(conn, row["id"])
    summary = _summarize(conn, row, held)
    if held is not None:
        summary["claim"] |= {"footprint": json.loads(held["footprint"]),
                             "claimed_at": held["claimed_at"]}
    return {**summary, "body": row["body"],
            "parent": parent[0] if parent else None,
            "children": brief("SELECT id, title, state FROM tasks "
                              "WHERE parent_id = ? ORDER BY id"),
            "blockers": brief("SELECT t.id, t.title, t.state FROM blockers b "
                              "JOIN tasks t ON t.id = b.blocker_id "
                              "WHERE b.task_id = ? ORDER BY t.id"),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}
