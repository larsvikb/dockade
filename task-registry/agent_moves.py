# SPDX-License-Identifier: Apache-2.0
"""What an agent may do to a task. Each move names the agent and the repo it works
in, and every write is recorded under that agent's id.

An agent shapes a task only while it is ``proposed``: once the operator moves it to
``open``, its spec and blockers are the ones the operator approved. It works an
``open`` task by claiming it, and every move on the claim carries the claim's token."""
from __future__ import annotations

import json

import claims
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


# --- claims ----------------------------------------------------------------------


def claim(agent: str, repo: str, task: int, branch: str,
          footprint: list[str] | tuple[str, ...]) -> dict:
    """Take an ``open`` task, or a ``landing`` one back to ``open`` for rework. The
    reply carries the token, which nothing else ever returns, and what the claim runs
    into: other claims its footprint overlaps, and blockers not yet closed. Neither is
    refused."""
    taskdb.check_agent(agent)
    claims.check_branch(branch)
    paths = claims.footprint(footprint)
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_in_repo(row, repo)
        if row["state"] not in ("open", "landing"):
            raise Refused(f"task {task} is {row['state']}; only an open or landing task "
                          f"can be claimed", {"state": row["state"]})
        held = claims.live(conn, task)
        if held is not None:
            raise Refused(f"task {task} is held by {held['holder']}",
                          {"holder": held["holder"], "expires_at": held["expires_at"]})
        token, expires_at = claims.take(conn, task, agent, branch, paths)
        revision = tasks.update(conn, task, {"state": "open"})
        taskdb.record(conn, agent, "claim", task, revision=revision, before=row["state"],
                      branch=branch, footprint=paths)
        reply = {"task": task, "token": token, "expires_at": expires_at,
                 "revision": revision,
                 "overlaps": claims.overlaps(conn, row["repo_id"], task, paths),
                 "open_blockers": claims.open_blockers(conn, task)}
    return reply


def update(agent: str, repo: str, task: int, token: str, progress: str | None = None,
           branch: str | None = None,
           footprint: list[str] | tuple[str, ...] | None = None) -> dict:
    """Say how the work goes, correct the branch or footprint, and renew the claim;
    with nothing else given, only renew it."""
    taskdb.check_agent(agent)
    claims.check_text(progress, claims.PROGRESS_MAX, "a progress line")
    if branch is not None:
        claims.check_branch(branch)
    paths = None if footprint is None else claims.footprint(footprint)
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_in_repo(row, repo)
        held = claims.check_holder(conn, row, agent, token)
        expires_at = claims.renew(conn, task, progress, branch, paths)
        taskdb.record(conn, agent, "update", task, progress=progress, branch=branch,
                      footprint=paths)
        current = paths if paths is not None else json.loads(held["footprint"])
        reply = {"task": task, "expires_at": expires_at, "revision": row["revision"],
                 "overlaps": claims.overlaps(conn, row["repo_id"], task, current)}
    return reply


def submit(agent: str, repo: str, task: int, token: str, note: str | None = None) -> dict:
    """The work is done: the task waits in ``landing`` for the operator to land it."""
    return _end_claim(agent, repo, task, token, note, "submit", "landing")


def send_back(agent: str, repo: str, task: int, token: str, note: str) -> dict:
    """The spec is not clear enough to implement: back to ``proposed``, with a note on
    what is unclear."""
    return _end_claim(agent, repo, task, token, note, "send_back", "proposed")


def release(agent: str, repo: str, task: int, token: str, note: str | None = None) -> dict:
    """Give the task up; it stays ``open`` for another agent."""
    return _end_claim(agent, repo, task, token, note, "release", None)


def _end_claim(agent: str, repo: str, task: int, token: str, note: str | None,
               action: str, state: str | None) -> dict:
    taskdb.check_agent(agent)
    claims.check_text(note, claims.NOTE_MAX, "a note", required=action == "send_back")
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_in_repo(row, repo)
        held = claims.check_holder(conn, row, agent, token)
        claims.end(conn, task)
        revision = tasks.update(conn, task, {} if state is None else {"state": state})
        taskdb.record(conn, agent, action, task, revision=revision, note=note,
                      **claims.ended(held))
    return {"task": task, "revision": revision}
