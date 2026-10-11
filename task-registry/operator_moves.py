# SPDX-License-Identifier: Apache-2.0
"""What only the operator may do or see: create and edit any task, move any task to
any state, re-parent, break a claim, note and hide notes, keep the repo list; read the
history, every note, and the notes marked for the operator. Every write is recorded
under ``taskdb.OPERATOR``, and every write to a task's spec, state or claim names the
revision the operator read.

An agent-facing surface must not call anything here (``task-registry/DESIGN.md``,
"Two writers, two modules")."""
from __future__ import annotations

import json
import time

import claims
import notes
import taskdb
import tasks
from taskdb import OPERATOR, Refused

# --- tasks -----------------------------------------------------------------------


def create(repo: str, title: str, body: str, state: str, severity: str | None = None,
           kind: str | None = None, parent: int | None = None,
           blocked_by: list[int] | tuple[int, ...] = ()) -> dict:
    tasks.check_fields(title, body, severity, kind)
    tasks.check_state(state)
    with taskdb.writing() as conn:
        repo_id = tasks.repo_id(conn, repo, for_new_task=True)
        tasks.check_parent(conn, repo_id, parent)
        tasks.check_blockers(conn, repo_id, list(blocked_by))
        task = tasks.insert(conn, repo_id, title, body, state, severity, kind, parent)
        tasks.set_blockers(conn, task, list(blocked_by))
        taskdb.record(conn, OPERATOR, "create", task, title=title, body=body, state=state,
                      severity=severity, kind=kind, parent=parent,
                      blocked_by=list(blocked_by))
    return {"task": task, "revision": 1}


def edit(task: int, revision: int, title: str | None = None, body: str | None = None,
         severity: str | None = None, kind: str | None = None,
         blocked_by: list[int] | None = None) -> dict:
    """Change any task's spec, in any state."""
    tasks.check_fields(title, body, severity, kind)
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_revision(conn, row, revision)
        if blocked_by is not None:
            tasks.check_blockers(conn, row["repo_id"], blocked_by, task)
            tasks.set_blockers(conn, task, blocked_by)
        changes = tasks.given(title=title, body=body, severity=severity, kind=kind)
        if not changes and blocked_by is None:
            raise Refused(f"nothing to change on task {task}")
        new_revision = tasks.update(conn, task, changes)
        taskdb.record(conn, OPERATOR, "edit", task, revision=new_revision,
                      blocked_by=blocked_by, **changes)
    return {"task": task, "revision": new_revision}


def move(task: int, revision: int, state: str) -> dict:
    """Any state to any state: triage, deferring, reopening, closing. A move to any
    state but ``open`` ends the task's claim."""
    tasks.check_state(state)
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_revision(conn, row, revision)
        claims.sweep(conn)
        held = claims.live(conn, task)
        ending = held is not None and state != "open"
        if ending:
            claims.end(conn, task)
        new_revision = tasks.update(conn, task, {"state": state})
        taskdb.record(conn, OPERATOR, "move", task, revision=new_revision,
                      before=row["state"], state=state,
                      ended_claim=claims.ended(held) if ending else None)
    return {"task": task, "revision": new_revision}


def break_claim(task: int, revision: int) -> dict:
    """End any agent's claim."""
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_revision(conn, row, revision)
        held = claims.live(conn, task)
        if held is None:
            raise Refused(f"task {task} has no live claim to break")
        claims.end(conn, task)
        new_revision = tasks.update(conn, task, {})
        taskdb.record(conn, OPERATOR, "break_claim", task, revision=new_revision,
                      **claims.ended(held))
    return {"task": task, "revision": new_revision}


def reparent(task: int, revision: int, parent: int | None) -> dict:
    """Give ``task`` a new parent in its repo, or none."""
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_revision(conn, row, revision)
        tasks.check_parent(conn, row["repo_id"], parent, task)
        new_revision = tasks.update(conn, task, {"parent_id": parent})
        taskdb.record(conn, OPERATOR, "reparent", task, revision=new_revision,
                      before=row["parent_id"], parent=parent)
    return {"task": task, "revision": new_revision}


# --- notes -----------------------------------------------------------------------


def add_note(task: int, text: str) -> dict:
    notes.check_note(text)
    with taskdb.writing() as conn:
        tasks.task_row(conn, task)
        note = notes.post(conn, task, OPERATOR, text)
        taskdb.record(conn, OPERATOR, "note", task, note_id=note, note=text)
    return {"task": task, "note": note}


def hide_note(note: int, hidden: bool = True) -> None:
    """Take a note out of agents' reads, or put it back. The note itself stays."""
    tasks.check_flag(hidden, "hidden")
    with taskdb.writing() as conn:
        row = notes.note_row(conn, note)
        conn.execute("UPDATE notes SET hidden_at = ? WHERE id = ?",
                     (time.time() if hidden else None, note))
        taskdb.record(conn, OPERATOR, "hide_note", row["task_id"], note_id=note,
                      hidden=hidden)


def all_notes(task: int) -> list[dict]:
    """Every note on ``task``, hidden ones marked: the operator's read, which no
    agent-facing surface may offer."""
    with taskdb.reading() as conn:
        tasks.task_row(conn, task)
        return [notes.shown(r, with_hidden=True) for r in conn.execute(
            "SELECT * FROM notes WHERE task_id = ? ORDER BY id", (task,))]


def notes_for_operator(repo: str | None = None) -> list[dict]:
    """Visible notes an agent marked for the operator, newest first, in one repo or
    all."""
    with taskdb.reading() as conn:
        query = ("SELECT n.* FROM notes n JOIN tasks t ON t.id = n.task_id "
                 "WHERE n.for_operator = 1 AND n.hidden_at IS NULL")
        args: list[object] = []
        if repo is not None:
            query += " AND t.repo_id = ?"
            args.append(tasks.repo_id(conn, repo))
        return [notes.shown(r) for r in conn.execute(query + " ORDER BY n.id DESC", args)]


def history(task: int) -> list[dict]:
    """Every write a task has had, oldest first, with its author and the values
    written: every body an agent proposed and every note, hidden ones too. The
    operator's read, which no agent-facing surface may offer."""
    with taskdb.reading() as conn:
        tasks.task_row(conn, task)
        return [{"ts": r["ts"], "author": r["author"], "action": r["action"],
                 "detail": json.loads(r["detail"])}
                for r in conn.execute(
                    "SELECT * FROM history WHERE task_id = ? ORDER BY id", (task,))]


# --- repos -----------------------------------------------------------------------


def add_repo(name: str, remote: str | None = None) -> None:
    _check_repo_name(name)
    _check_remote(remote)
    with taskdb.writing() as conn:
        if conn.execute("SELECT 1 FROM repos WHERE name = ?", (name,)).fetchone():
            raise Refused(f"repo {name!r} already exists")
        conn.execute("INSERT INTO repos(name, remote) VALUES (?, ?)", (name, remote))
        taskdb.record(conn, OPERATOR, "add_repo", None, name=name, remote=remote)


def rename_repo(name: str, new_name: str) -> None:
    """Rename a repo; its tasks follow. The repo's instructions file must say the new
    name before its agents can write again."""
    _check_repo_name(new_name)
    with taskdb.writing() as conn:
        tasks.repo_id(conn, name)
        if conn.execute("SELECT 1 FROM repos WHERE name = ?", (new_name,)).fetchone():
            raise Refused(f"repo {new_name!r} already exists")
        conn.execute("UPDATE repos SET name = ? WHERE name = ?", (new_name, name))
        taskdb.record(conn, OPERATOR, "rename_repo", None, name=name, new_name=new_name)


def set_remote(name: str, remote: str | None) -> None:
    _check_remote(remote)
    with taskdb.writing() as conn:
        tasks.repo_id(conn, name)
        conn.execute("UPDATE repos SET remote = ? WHERE name = ?", (remote, name))
        taskdb.record(conn, OPERATOR, "set_remote", None, name=name, remote=remote)


def retire_repo(name: str, retired: bool = True) -> None:
    """A retired repo keeps its tasks and takes no new ones."""
    tasks.check_flag(retired, "retired")
    with taskdb.writing() as conn:
        tasks.repo_id(conn, name)
        conn.execute("UPDATE repos SET retired_at = ? WHERE name = ?",
                     (time.time() if retired else None, name))
        taskdb.record(conn, OPERATOR, "retire_repo", None, name=name, retired=retired)


def _check_repo_name(name: str) -> None:
    if not tasks.REPO_NAME_RE.fullmatch(name or ""):
        raise Refused(f"{name!r} is not a repo name: lowercase letters, digits and "
                      f"hyphens, starting with a letter or digit")


def _check_remote(remote: str | None) -> None:
    if remote is None:
        return
    if (not tasks.REMOTE_RE.fullmatch(remote)
            or any(part in (".", "..") for part in remote.split("/"))):
        raise Refused(f"{remote!r} is not a remote: host/owner/repo, as in "
                      f"github.com/acme/widgets")
