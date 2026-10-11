# SPDX-License-Identifier: Apache-2.0
"""Tasks: their fields and states, how they relate, and the plain writes.

The checks here hold for every writer. The writes (``insert``, ``update``,
``set_blockers``) check nothing: ``agent_moves`` and ``operator_moves`` call them after
their checks, and a surface for agents calls ``agent_moves`` and ``reads``, never
these."""
from __future__ import annotations

import re
import sqlite3
import time

import taskdb
from taskdb import Refused

STATES = ("proposed", "open", "landing", "deferred", "closed")
SEVERITIES = ("high", "medium", "low")
KINDS = ("finding", "question", "improvement")

TITLE_MAX = 200
BODY_MAX = 32_000

REPO_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
# host/owner/repo, or host/group/subgroup/repo on a forge with groups: never an owner
# alone, which would scope anything keyed on it to every repo the owner has.
REMOTE_RE = re.compile(r"[A-Za-z0-9.-]+(/[A-Za-z0-9._-]+){2,4}")


# --- fields ----------------------------------------------------------------------

def check_fields(title: str | None = None, body: str | None = None,
                 severity: str | None = None, kind: str | None = None) -> None:
    """Refuse a field outside its bounds. ``None`` is "not given" and passes."""
    if title is not None and not (0 < len(title.strip()) <= TITLE_MAX
                                  and taskdb.storable(title)):
        raise Refused(f"a title is 1 to {TITLE_MAX} characters of text")
    if body is not None and not (len(body) <= BODY_MAX and taskdb.storable(body)):
        raise Refused(f"a body is at most {BODY_MAX} characters of text")
    if severity is not None and severity not in SEVERITIES:
        raise Refused(f"severity is one of {', '.join(SEVERITIES)}")
    if kind is not None and kind not in KINDS:
        raise Refused(f"kind is one of {', '.join(KINDS)}")


def check_state(state: str) -> str:
    if state not in STATES:
        raise Refused(f"state is one of {', '.join(STATES)}")
    return state


def check_id(value: object) -> int:
    """A task id, which must be an int: SQLite finds task 3 under ``"3"`` too, where
    the loop checks' ``==`` would not match it, and a loop would pass."""
    if type(value) is not int or value < 1:
        raise Refused(f"{value!r} is not a task id")
    return value


# --- repos -----------------------------------------------------------------------

def repo_id(conn: sqlite3.Connection, name: str, *, for_new_task: bool = False) -> int:
    """The id of repo ``name``. A retired repo takes no new task."""
    row = None if not taskdb.storable(name) else conn.execute(
        "SELECT id, retired_at FROM repos WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise Refused(f"no repo {name!r} in the registry", {"repos": active_repos(conn)})
    if for_new_task and row["retired_at"] is not None:
        raise Refused(f"repo {name!r} is retired", {"repos": active_repos(conn)})
    return row["id"]


def active_repos(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT name FROM repos WHERE retired_at IS NULL ORDER BY name")]


# --- one task --------------------------------------------------------------------

def task_row(conn: sqlite3.Connection, task: int) -> sqlite3.Row:
    check_id(task)
    row = conn.execute(
        "SELECT t.*, r.name AS repo FROM tasks t JOIN repos r ON r.id = t.repo_id "
        "WHERE t.id = ?", (task,)).fetchone()
    if row is None:
        raise Refused(f"no task {task}")
    return row


def check_in_repo(row: sqlite3.Row, repo: str) -> None:
    """Refuse an agent's write on a task of a repo other than the one it works in."""
    if row["repo"] != repo:
        raise Refused(f"task {row['id']} belongs to repo {row['repo']!r}, not {repo!r}",
                      {"repo": row["repo"]})


def check_revision(conn: sqlite3.Connection, row: sqlite3.Row, revision: int) -> None:
    """Refuse a write based on a task as it no longer is, with the task as it is."""
    if type(revision) is not int:
        raise Refused(f"{revision!r} is not a revision")
    if row["revision"] != revision:
        import reads  # here, not at the top: reads imports this module
        raise Refused(
            f"task {row['id']} is at revision {row['revision']}, not {revision}: it "
            f"changed since it was read", reads.detail(conn, row))


_UPDATABLE = frozenset({"title", "body", "state", "severity", "kind", "parent_id"})


def given(**fields: object) -> dict:
    """The fields an edit names: ``None`` means "leave as it is"."""
    return {k: v for k, v in fields.items() if v is not None}


def update(conn: sqlite3.Connection, task: int, changes: dict) -> int:
    """Write ``changes`` to ``task`` and return its new revision.

    The revision counts every change to a task's content or state, so one number
    tells a writer whether the task is still the one it read."""
    if not changes.keys() <= _UPDATABLE:
        raise ValueError(f"not task columns: {sorted(changes.keys() - _UPDATABLE)}")
    sets = "".join(f"{column} = ?, " for column in changes)
    conn.execute(
        f"UPDATE tasks SET {sets}revision = revision + 1, updated_at = ? WHERE id = ?",  # noqa: S608 (column names come from _UPDATABLE, values are bound)
        (*changes.values(), time.time(), task))
    return conn.execute("SELECT revision FROM tasks WHERE id = ?", (task,)).fetchone()[0]


def insert(conn: sqlite3.Connection, repo_id: int, title: str, body: str, state: str,
           severity: str | None, kind: str | None, parent: int | None) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO tasks(repo_id, title, body, state, severity, kind, parent_id, "
        "revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (repo_id, title, body, state, severity, kind, parent, now, now))
    return cur.lastrowid


# --- how tasks relate ------------------------------------------------------------

def check_parent(conn: sqlite3.Connection, repo_id: int, parent: int | None,
                 task: int | None = None) -> None:
    """Refuse a parent outside the repo, or one that would make ``task`` its own
    ancestor."""
    if parent is None:
        return
    check_id(parent)
    row = conn.execute("SELECT repo_id FROM tasks WHERE id = ?", (parent,)).fetchone()
    if row is None or row["repo_id"] != repo_id:
        raise Refused(f"no task {parent} in this repo to be a parent")
    ancestor, seen = parent, set()
    while ancestor is not None:
        if ancestor == task:
            raise Refused(f"task {parent} descends from task {task}, so cannot be its parent")
        if ancestor in seen:
            raise Refused(f"the parents above task {parent} loop; the operator must "
                          f"re-parent one of them first")
        seen.add(ancestor)
        ancestor = conn.execute(
            "SELECT parent_id FROM tasks WHERE id = ?", (ancestor,)).fetchone()[0]


def check_blockers(conn: sqlite3.Connection, repo_id: int, blockers: list[int],
                   task: int | None = None) -> None:
    """Refuse a blocker outside the repo, or one that waits on ``task`` itself."""
    for blocker in blockers:
        check_id(blocker)
        if blocker == task:
            raise Refused(f"task {task} cannot block on itself")
        row = conn.execute("SELECT repo_id FROM tasks WHERE id = ?", (blocker,)).fetchone()
        if row is None or row["repo_id"] != repo_id:
            raise Refused(f"no task {blocker} in this repo to block on")
        if task is not None and _waits_on(conn, blocker, task):
            raise Refused(f"task {blocker} already waits on task {task}; blocking on it "
                          f"would make neither ever ready")


def _waits_on(conn: sqlite3.Connection, task: int, target: int) -> bool:
    """Whether ``task`` is ``target`` or waits on it through any chain of blockers."""
    seen, frontier = set(), [task]
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current not in seen:
            seen.add(current)
            frontier.extend(r[0] for r in conn.execute(
                "SELECT blocker_id FROM blockers WHERE task_id = ?", (current,)))
    return False


def set_blockers(conn: sqlite3.Connection, task: int, blockers: list[int]) -> None:
    conn.execute("DELETE FROM blockers WHERE task_id = ?", (task,))
    conn.executemany("INSERT INTO blockers(task_id, blocker_id) VALUES (?, ?)",
                     [(task, b) for b in dict.fromkeys(blockers)])
