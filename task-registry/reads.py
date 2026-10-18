# SPDX-License-Identifier: Apache-2.0
"""What any surface may read, agents' included: a repo's tasks, one task in full with its
latest notes, its plan and the operator's answers to its plans' decisions, older notes
page by page, every plan a task had, a search, and the repo list. Nothing here
writes, and nothing here returns what the operator alone may see: a hidden note, or the
history, which keeps every text a task ever had."""
from __future__ import annotations

import json
import sqlite3

import claims
import notes
import plans
import taskdb
import tasks

#: How many tasks a listing returns, beside the total.
LIST_MAX = 50

#: How many tasks a search returns, beside the total.
SEARCH_MAX = 50
SEARCH_TEXT_MAX = 200

# A note matches a search only if agents may read it: a hit alone would say what a
# hidden note holds.
_NOTE_HIT = "n.hidden_at IS NULL AND instr(casefold(n.text), :text)"


def list_tasks(repo: str, state: str | None = "open", holder: str | None = None,
               parent: int | None = None, before: int | None = None) -> dict:
    """A repo's tasks in ``state`` (every state for ``None``), optionally only those
    ``holder`` has a live claim on, or the children of ``parent``; newest first, up to
    ``LIST_MAX`` with the total, and ``before`` pages back past a task. A task that is
    ``ready`` is in the ready queue: open, unclaimed, and blocked by nothing."""
    if state is not None:
        tasks.check_state(state)
    if holder is not None:
        taskdb.check_agent(holder)
    for value in (parent, before):
        if value is not None:
            tasks.check_id(value)
    with taskdb.reading() as conn:
        where = " FROM tasks t JOIN repos r ON r.id = t.repo_id WHERE t.repo_id = ?"
        args: list[object] = [tasks.repo_id(conn, repo)]
        if state is not None:
            where += " AND t.state = ?"
            args.append(state)
        if holder is not None:
            where += (" AND t.id IN (SELECT task_id FROM claims WHERE holder = ? "
                      "AND expires_at > ?)")
            args += [holder, claims.now()]
        if parent is not None:
            where += " AND t.parent_id = ?"
            args.append(parent)
        total = conn.execute("SELECT COUNT(*)" + where, args).fetchone()[0]
        page = conn.execute(
            "SELECT t.*, r.name AS repo" + where + " AND (? IS NULL OR t.id < ?) "
            "ORDER BY t.id DESC LIMIT ?", [*args, before, before, LIST_MAX])
        return {"tasks": [_summary(conn, row) for row in page], "total": total}


def get_task(task: int) -> dict:
    with taskdb.reading() as conn:
        return detail(conn, tasks.task_row(conn, task))


def search_tasks(text: str, repo: str | None = None) -> dict:
    """Tasks whose title, body or a visible note holds ``text``, as written but for
    case, in one repo or all; newest first, up to ``SEARCH_MAX``, with the total. Each
    names the latest of its notes that matched, as many as a read of a task carries,
    with their total."""
    tasks.check_text(text, SEARCH_TEXT_MAX, "a search", required=True)
    with taskdb.reading() as conn:
        conn.create_function("casefold", 1, str.casefold)
        query = ("SELECT t.*, r.name AS repo FROM tasks t JOIN repos r ON r.id = t.repo_id "  # noqa: S608
                 "WHERE (instr(casefold(t.title), :text) OR instr(casefold(t.body), :text) "
                 f"OR EXISTS (SELECT 1 FROM notes n WHERE n.task_id = t.id AND {_NOTE_HIT}))")
        args: dict[str, object] = {"text": text.casefold()}
        if repo is not None:
            query += " AND t.repo_id = :repo"
            args["repo"] = tasks.repo_id(conn, repo)
        found = conn.execute(query + " ORDER BY t.id DESC", args).fetchall()
        return {"tasks": [_hit(conn, row, args["text"]) for row in found[:SEARCH_MAX]],
                "total": len(found)}


def _hit(conn: sqlite3.Connection, row: sqlite3.Row, text: str) -> dict:
    matched = [n["id"] for n in conn.execute(
        f"SELECT n.id FROM notes n WHERE n.task_id = :task AND {_NOTE_HIT} ORDER BY n.id",  # noqa: S608
        {"task": row["id"], "text": text})]
    return _summary(conn, row) | {"matched_notes": matched[-notes.IN_READ:],
                                  "matched_notes_total": len(matched)}


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
            "notes_total": notes.count(conn, row["id"]),
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
    return {**summary, "body": row["body"], "plan": plans.latest(conn, row["id"]),
            "answers": plans.answers(conn, row["id"]),
            "notes": notes.visible(conn, row["id"]),
            "parent": parent[0] if parent else None,
            "children": brief("SELECT id, title, state FROM tasks "
                              "WHERE parent_id = ? ORDER BY id"),
            "blockers": brief("SELECT t.id, t.title, t.state FROM blockers b "
                              "JOIN tasks t ON t.id = b.blocker_id "
                              "WHERE b.task_id = ? ORDER BY t.id"),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def task_plans(task: int) -> list[dict]:
    """Every plan the task had, newest first, superseded ones marked: what the operator
    compares a plan with the one before."""
    with taskdb.reading() as conn:
        tasks.task_row(conn, task)
        return plans.every(conn, task)


def task_notes(task: int, before: int | None = None) -> list[dict]:
    """The task's notes older than note ``before``, as many as a read of the task
    carries: how an agent pages back past the latest."""
    if before is not None:
        tasks.check_id(before, "note")
    with taskdb.reading() as conn:
        tasks.task_row(conn, task)
        return notes.visible(conn, task, before)
