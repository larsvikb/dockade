# SPDX-License-Identifier: Apache-2.0
"""What an agent may do to a task. Each move names the agent and the repo it works
in, and every write is recorded under that agent's id.

An agent shapes a task only while it is ``proposed``: once the operator moves it to
``open``, its spec and blockers are the ones the operator approved."""
from __future__ import annotations

import taskdb
import tasks
from taskdb import Refused


def propose(agent: str, repo: str, title: str, body: str, severity: str | None = None,
            kind: str | None = None, parent: int | None = None,
            blocked_by: list[int] | tuple[int, ...] = ()) -> dict:
    """File a task for the operator to triage."""
    taskdb.check_agent(agent)
    tasks.check_fields(title, body, severity, kind)
    with taskdb.writing() as conn:
        repo_id = tasks.repo_id(conn, repo, for_new_task=True)
        tasks.check_parent(conn, repo_id, parent)
        tasks.check_blockers(conn, repo_id, list(blocked_by))
        task = tasks.insert(conn, repo_id, title, body, "proposed", severity, kind, parent)
        tasks.set_blockers(conn, task, list(blocked_by))
        taskdb.record(conn, agent, "propose", task, title=title, body=body,
                      severity=severity, kind=kind, parent=parent,
                      blocked_by=list(blocked_by))
    return {"task": task, "revision": 1}


def edit_proposal(agent: str, repo: str, task: int, revision: int,
                  title: str | None = None, body: str | None = None,
                  severity: str | None = None, kind: str | None = None,
                  blocked_by: list[int] | None = None) -> dict:
    """Change a ``proposed`` task's spec, as of the revision the agent read."""
    taskdb.check_agent(agent)
    tasks.check_fields(title, body, severity, kind)
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_in_repo(row, repo)
        if row["state"] != "proposed":
            raise Refused(f"task {task} is {row['state']}; an agent edits a spec only "
                          f"while it is proposed", {"state": row["state"]})
        tasks.check_revision(conn, row, revision)
        if blocked_by is not None:
            tasks.check_blockers(conn, row["repo_id"], blocked_by, task)
            tasks.set_blockers(conn, task, blocked_by)
        changes = tasks.given(title=title, body=body, severity=severity, kind=kind)
        if not changes and blocked_by is None:
            raise Refused(f"nothing to change on task {task}")
        new_revision = tasks.update(conn, task, changes)
        taskdb.record(conn, agent, "edit_proposal", task, revision=new_revision,
                      blocked_by=blocked_by, **changes)
    return {"task": task, "revision": new_revision}
