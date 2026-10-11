# SPDX-License-Identifier: Apache-2.0
"""Claims: one agent's hold on an ``open`` task, the token that proves it, its expiry,
and the footprint it declares. The checks and plain writes here; the moves that take
and end a claim are in ``agent_moves`` and ``operator_moves``."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import posixpath
import re
import secrets
import sqlite3
import time

import taskdb
import tasks
from taskdb import Refused

_CLAIM_SECONDS_MAX = 30 * 24 * 3600


def _claim_seconds(raw: str) -> int:
    try:
        seconds = int(raw)
    except ValueError:
        seconds = 0
    if not 0 < seconds <= _CLAIM_SECONDS_MAX:
        raise RuntimeError(f"TASKS_CLAIM_SECONDS={raw!r} is not a whole number of "
                           f"seconds from 1 to {_CLAIM_SECONDS_MAX}")
    return seconds


#: How long a claim lives without a write from its holder.
CLAIM_SECONDS = _claim_seconds(os.environ.get("TASKS_CLAIM_SECONDS", "14400"))

BRANCH_MAX = 200
PROGRESS_MAX = 500
FOOTPRINT_MAX = 50
PATH_MAX = 300

_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")


def now() -> float:
    return time.time()


# --- what a claim carries --------------------------------------------------------

def check_branch(branch: object) -> str:
    """A branch name git would take, near enough: no spaces, no ``..``, no leading
    ``-`` that would read as an option."""
    if (not isinstance(branch, str) or not 0 < len(branch) <= BRANCH_MAX
            or not _BRANCH_RE.fullmatch(branch) or branch.startswith(("-", "/", "."))
            or branch.endswith(("/", ".", ".lock")) or ".." in branch or "//" in branch):
        raise Refused(f"{branch!r} is not a branch name")
    return branch


def footprint(paths: object) -> list[str]:
    """``paths`` as repo-relative paths, normalized: ``.`` is the whole repo. At least
    one, since a claim with none would overlap nothing: ``.`` says "not known yet"."""
    if not isinstance(paths, (list, tuple)) or not 0 < len(paths) <= FOOTPRINT_MAX:
        raise Refused(f"a footprint is a list of 1 to {FOOTPRINT_MAX} paths in the "
                      f"repo; ['.'] while they are not known")
    out = []
    for path in paths:
        if (not isinstance(path, str) or not 0 < len(path) <= PATH_MAX
                or any(ord(c) < 32 for c in path) or not taskdb.storable(path)):
            raise Refused(f"{path!r} is not a path in the repo")
        norm = posixpath.normpath(path)
        if norm == ".." or norm.startswith(("/", "../")):
            raise Refused(f"{path!r} is not a path in the repo")
        out.append(norm)
    return list(dict.fromkeys(out))


def _contains(outer: str, inner: str) -> bool:
    return outer in (".", inner) or inner.startswith(outer + "/")


# --- the live claim --------------------------------------------------------------

def live(conn: sqlite3.Connection, task: int) -> sqlite3.Row | None:
    """The claim on ``task`` if it has not expired. An expired row stays until the
    next claim sweeps it, but nothing treats it as held."""
    return conn.execute("SELECT * FROM claims WHERE task_id = ? AND expires_at > ?",
                        (task, now())).fetchone()


def check_holder(conn: sqlite3.Connection, row: sqlite3.Row, agent: str,
                 token: object) -> sqlite3.Row:
    """The live claim on the task, if ``agent`` holds it and ``token`` is its token.
    A refusal says who holds the task now, or that it is free."""
    claim = conn.execute("SELECT * FROM claims WHERE task_id = ?", (row["id"],)).fetchone()
    if claim is None or claim["expires_at"] <= now():
        mine = claim is not None and claim["holder"] == agent
        now_ = ("it is free to claim" if row["state"] in ("open", "landing")
                else f"it is {row['state']}")
        raise Refused(
            f"your claim on task {row['id']} lapsed; {now_}" if mine
            else f"you hold no claim on task {row['id']}; {now_}",
            {"holder": None, "state": row["state"]})
    if claim["holder"] != agent:
        raise Refused(f"task {row['id']} is held by {claim['holder']}",
                      {"holder": claim["holder"], "expires_at": claim["expires_at"]})
    if (not isinstance(token, str) or not taskdb.storable(token)
            or not hmac.compare_digest(_hash(token), claim["token_hash"])):
        raise Refused(f"that is not the token of the claim on task {row['id']}")
    return claim


def overlaps(conn: sqlite3.Connection, repo_id: int, task: int,
             paths: list[str]) -> list[dict]:
    """The other live claims in the repo whose footprint overlaps ``paths``: one
    path containing the other. Advisory: nothing is refused for an overlap."""
    found = []
    for other in conn.execute(
            "SELECT c.*, t.title FROM claims c JOIN tasks t ON t.id = c.task_id "
            "WHERE t.repo_id = ? AND c.task_id != ? AND c.expires_at > ? "
            "ORDER BY c.task_id", (repo_id, task, now())):
        shared = [p for p in json.loads(other["footprint"])
                  if any(_contains(p, mine) or _contains(mine, p) for mine in paths)]
        if shared:
            found.append({"task": other["task_id"], "title": other["title"],
                          "holder": other["holder"], "branch": other["branch"],
                          "paths": shared})
    return found


def open_blockers(conn: sqlite3.Connection, task: int) -> list[dict]:
    return [{"id": r["id"], "title": r["title"], "state": r["state"]}
            for r in conn.execute(
                "SELECT t.id, t.title, t.state FROM blockers b JOIN tasks t "
                "ON t.id = b.blocker_id WHERE b.task_id = ? AND t.state != 'closed' "
                "ORDER BY t.id", (task,))]


# --- plain writes: checked by the caller -----------------------------------------

def take(conn: sqlite3.Connection, task: int, agent: str, branch: str,
         paths: list[str]) -> tuple[str, float]:
    """Insert the claim and return its token, which is kept nowhere, and expiry."""
    sweep(conn)
    token = secrets.token_urlsafe(32)
    claimed_at = now()
    conn.execute(
        "INSERT INTO claims(task_id, holder, branch, token_hash, footprint, progress, "
        "claimed_at, expires_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
        (task, agent, branch, _hash(token), json.dumps(paths), claimed_at,
         claimed_at + CLAIM_SECONDS))
    return token, claimed_at + CLAIM_SECONDS


def renew(conn: sqlite3.Connection, task: int, progress: str | None,
          branch: str | None, paths: list[str] | None) -> float:
    """Write what is given and push the expiry out; the task's revision stays
    (``task-registry/DESIGN.md``, "States")."""
    expires_at = now() + CLAIM_SECONDS
    conn.execute(
        "UPDATE claims SET progress = COALESCE(?, progress), "
        "branch = COALESCE(?, branch), footprint = COALESCE(?, footprint), "
        "expires_at = ? WHERE task_id = ?",
        (progress, branch, None if paths is None else json.dumps(paths), expires_at,
         task))
    return expires_at


def end(conn: sqlite3.Connection, task: int) -> None:
    conn.execute("DELETE FROM claims WHERE task_id = ?", (task,))


def ended(claim: sqlite3.Row) -> dict:
    """What a history row keeps of a claim that ends, so the branch to land survives
    the claim."""
    return {"holder": claim["holder"], "branch": claim["branch"],
            "footprint": json.loads(claim["footprint"])}


def sweep(conn: sqlite3.Connection) -> None:
    """End every expired claim, under the registry's name, bumping the task's revision
    as any ending does."""
    for row in conn.execute("SELECT * FROM claims WHERE expires_at <= ?",
                            (now(),)).fetchall():
        end(conn, row["task_id"])
        revision = tasks.update(conn, row["task_id"], {})
        taskdb.record(conn, taskdb.REGISTRY, "lapse", row["task_id"], revision=revision,
                      expired_at=row["expires_at"], **ended(row))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
