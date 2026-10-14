# SPDX-License-Identifier: Apache-2.0
"""Plans: how a task will be done, written by the agent that refined it and approved
with the task. Each ``submit_plan`` adds a row and none is ever changed; a task's
current plan is its latest one not superseded. The checks, the plain writes and the
shape a plan is read in; who may write one is in ``agent_moves``."""
from __future__ import annotations

import sqlite3
import time

import claims
import tasks
from taskdb import Refused

# The caps are the plan's shape as much as storage: a plan that does not fit is over-
# detailed, or more than one task (`task-registry/DESIGN.md`, "Plans").
TEXTS = {"approach": 4000, "tests": 2000, "out_of_scope": 1500, "risks": 1500,
         "review_verdict": 1000}
REVIEW_ROUNDS_MAX = 20


def check(approach: object, footprint: object, tests: object, out_of_scope: object,
          risks: object, review_rounds: object, review_verdict: object) -> list[str]:
    """Refuse a plan missing a part or past its caps; return its footprint, normalized."""
    given = {"approach": approach, "tests": tests, "out_of_scope": out_of_scope,
             "risks": risks, "review_verdict": review_verdict}
    for name, limit in TEXTS.items():
        tasks.check_text(given[name], limit, f"a plan's {name}", required=True)
    if type(review_rounds) is not int or not 0 < review_rounds <= REVIEW_ROUNDS_MAX:
        raise Refused(f"review_rounds is how many times a fresh-context reviewer read "
                      f"the plan, 1 to {REVIEW_ROUNDS_MAX}")
    paths = claims.footprint(footprint)
    if "." in paths:
        raise Refused("a plan's footprint names where the change goes; '.' is the whole "
                      "repo: name its top-level directories if it truly spans them")
    return paths


def insert(conn: sqlite3.Connection, task: int, revision: int, author: str,
           fields: dict, paths: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO plans(task_id, task_revision, author, created_at, approach, tests, "
        "out_of_scope, risks, review_rounds, review_verdict) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task, revision, author, time.time(), fields["approach"], fields["tests"],
         fields["out_of_scope"], fields["risks"], fields["review_rounds"],
         fields["review_verdict"]))
    conn.executemany("INSERT INTO plan_paths(plan_id, path) VALUES (?, ?)",
                     [(cur.lastrowid, p) for p in paths])
    return cur.lastrowid


def current(conn: sqlite3.Connection, task: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM plans WHERE task_id = ? AND superseded_at IS NULL "
                        "ORDER BY id DESC LIMIT 1", (task,)).fetchone()


def paths(conn: sqlite3.Connection, plan: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT path FROM plan_paths WHERE plan_id = ? ORDER BY rowid", (plan,))]


def shown(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """A plan as a read returns it: data, each part apart, never spliced into the
    task's own text."""
    return {"id": row["id"], "task": row["task_id"], "task_revision": row["task_revision"],
            "author": row["author"], "at": row["created_at"],
            "approach": row["approach"], "footprint": paths(conn, row["id"]),
            "tests": row["tests"], "out_of_scope": row["out_of_scope"],
            "risks": row["risks"], "review_rounds": row["review_rounds"],
            "review_verdict": row["review_verdict"],
            "superseded_at": row["superseded_at"]}


def latest(conn: sqlite3.Connection, task: int) -> dict | None:
    """The current plan, or with none the latest superseded one, which is what the next
    refiner works from; ``None`` for a task never planned. The latest row is the current
    one whenever there is one: superseding marks every plan a task has so far."""
    row = conn.execute("SELECT * FROM plans WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                       (task,)).fetchone()
    return None if row is None else shown(conn, row)


def every(conn: sqlite3.Connection, task: int) -> list[dict]:
    return [shown(conn, r) for r in conn.execute(
        "SELECT * FROM plans WHERE task_id = ? ORDER BY id DESC", (task,))]


def overlaps(conn: sqlite3.Connection, repo_id: int, task: int,
             mine: list[str]) -> list[dict]:
    """The current plans of the repo's other refined and open tasks whose footprint
    overlaps ``mine``. Advisory, as a claim's overlaps are."""
    found = []
    for other in conn.execute(
            "SELECT p.id, p.task_id, t.title, t.state FROM plans p "
            "JOIN tasks t ON t.id = p.task_id WHERE t.repo_id = ? AND p.task_id != ? "
            "AND p.superseded_at IS NULL AND t.state IN ('refined', 'open') "
            "ORDER BY p.task_id", (repo_id, task)):
        shared = claims.shared(paths(conn, other["id"]), mine)
        if shared:
            found.append({"task": other["task_id"], "title": other["title"],
                          "state": other["state"], "plan": other["id"], "paths": shared})
    return found
