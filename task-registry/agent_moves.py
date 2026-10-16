# SPDX-License-Identifier: Apache-2.0
"""What an agent may do to a task. Each move names the agent and the repo it works
in, and every write is recorded under that agent's id.

An agent shapes a task only while it is ``proposed``: once the operator moves it to
``open``, its spec and blockers are the ones the operator approved. It refines a
``proposed`` task into a plan by claiming it with ``claim_plan``, or does an ``open``
one by claiming it with ``claim_work``, and every move on the claim carries the
claim's token."""
from __future__ import annotations

import json
import sqlite3
from typing import NamedTuple

import claims
import notes
import plans
import taskdb
import tasks
from taskdb import Refused


def propose(agent: str, repo: str, title: str, body: str, severity: str | None = None,
            kind: str | None = None, parent: int | None = None,
            blocked_by: list[int] | tuple[int, ...] = ()) -> dict:
    """File a task for the operator to triage."""
    taskdb.check_agent(agent)
    tasks.check_new(title, body, severity, kind)
    with taskdb.writing() as conn:
        repo_id = tasks.repo_id(conn, repo, for_new_task=True)
        tasks.check_parent(conn, repo_id, parent)
        tasks.check_blockers(conn, repo_id, blocked_by)
        task = tasks.insert(conn, repo_id, title, body, "proposed", severity, kind, parent)
        tasks.set_blockers(conn, task, list(blocked_by))
        taskdb.record(conn, agent, "propose", task, title=title, body=body,
                      severity=severity, kind=kind, parent=parent,
                      blocked_by=list(blocked_by))
    return {"task": task, "revision": 1}


def edit_proposal(agent: str, repo: str, task: int, revision: int,
                  title: str | None = None, body: str | None = None,
                  severity: str | None = None, kind: str | None = None,
                  blocked_by: list[int] | None = None, token: str | None = None) -> dict:
    """Change a ``proposed`` task's spec, as of the revision the agent read. While the
    task is claimed, only its holder may, with the claim's token: an agent id is
    self-declared, so the id alone would let any session overwrite the refiner."""
    taskdb.check_agent(agent)
    tasks.check_fields(title, body, severity, kind)
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_in_repo(row, repo)
        if row["state"] != "proposed":
            raise Refused(f"task {task} is {row['state']}; an agent edits a spec only "
                          f"while it is proposed", {"state": row["state"]})
        if token is not None or claims.live(conn, task) is not None:
            claims.check_holder(conn, row, agent, token)
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


def claim_plan(agent: str, repo: str, task: int,
               footprint: list[str] | tuple[str, ...]) -> dict:
    """Take a ``proposed`` task to refine it, or a ``refined`` one back to ``proposed``
    to rework its plan. A plan lands nothing, so the claim names no branch."""
    taskdb.check_agent(agent)
    return _claim(agent, repo, task, "claim_plan", None, claims.footprint(footprint))


def claim_work(agent: str, repo: str, task: int, branch: str,
               footprint: list[str] | tuple[str, ...] | None = None) -> dict:
    """Take an ``open`` task to do it, or a ``landing`` one back to ``open`` for
    rework, naming the branch the work lands on. The footprint may be left out on a
    task whose current plan names one, and is the plan's then."""
    taskdb.check_agent(agent)
    claims.check_branch(branch)
    paths = None if footprint is None else claims.footprint(footprint)
    return _claim(agent, repo, task, "claim_work", branch, paths)


def _claim(agent: str, repo: str, task: int, action: str, branch: str | None,
           paths: list[str] | None) -> dict:
    """The step both claim moves share. The reply carries the token, which nothing else
    ever returns, and what the claim runs into: other claims its footprint overlaps,
    and blockers not yet closed. Neither is refused."""
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_in_repo(row, repo)
        into = claims.CLAIMED_INTO[action].get(row["state"])
        if into is None:
            raise Refused(_unclaimable(task, row["state"], action),
                          {"state": row["state"]})
        held = claims.live(conn, task)
        if held is not None:
            raise Refused(f"task {task} is held by {held['holder']}",
                          {"holder": held["holder"], "expires_at": held["expires_at"]})
        if paths is None:
            plan = plans.current(conn, task)
            if plan is None:
                raise Refused(f"task {task} has no plan whose footprint the claim could "
                              f"take: name one, ['.'] while the paths are not known")
            paths = plans.paths(conn, plan["id"])
        token, expires_at = claims.take(conn, task, agent, branch, paths)
        revision = tasks.update(conn, task, {"state": into})
        taskdb.record(conn, agent, action, task, revision=revision, before=row["state"],
                      footprint=paths, **tasks.given(branch=branch))
        reply = {"task": task, "token": token, "expires_at": expires_at,
                 "revision": revision,
                 "overlaps": claims.overlaps(conn, row["repo_id"], task, paths),
                 "open_blockers": claims.open_blockers(conn, task)}
    return reply


def _unclaimable(task: int, state: str, action: str) -> str:
    """Why ``action`` does not take a task in ``state``, naming the move that does."""
    def takes(move: str) -> str:
        return f"{move} takes a task that is {' or '.join(claims.CLAIMED_INTO[move])}"

    other = claims.claimed_with(state)
    if other is not None:
        return f"task {task} is {state}: {takes(action)}, and {other} one that is {state}"
    return (f"task {task} is {state}, which no claim takes: "
            f"{'; '.join(takes(move) for move in claims.CLAIMED_INTO)}")


def update(agent: str, repo: str, task: int, token: str, progress: str | None = None,
           branch: str | None = None,
           footprint: list[str] | tuple[str, ...] | None = None) -> dict:
    """Say how the work goes, correct the footprint or the work's branch, and renew the
    claim; with nothing else given, only renew it. The history row keeps only what
    changed."""
    taskdb.check_agent(agent)
    tasks.check_text(progress, claims.PROGRESS_MAX, "a progress line")
    if branch is not None:
        claims.check_branch(branch)
    paths = None if footprint is None else claims.footprint(footprint)
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_in_repo(row, repo)
        held = claims.check_holder(conn, row, agent, token)
        if branch is not None and row["state"] == "proposed":
            raise Refused(f"a claim to refine task {task} has no branch: the work's "
                          f"branch is named when it is claimed, with claim_work")
        expires_at = claims.renew(conn, task, progress, branch, paths)
        taskdb.record(conn, agent, "update", task,
                      **tasks.given(progress=progress, branch=branch, footprint=paths))
        current = paths if paths is not None else json.loads(held["footprint"])
        reply = {"task": task, "expires_at": expires_at, "revision": row["revision"],
                 "overlaps": claims.overlaps(conn, row["repo_id"], task, current)}
    return reply


def submit_plan(agent: str, repo: str, task: int, token: str, approach: str,
                footprint: list[str] | tuple[str, ...], tests: str, out_of_scope: str,
                risks: str, review_rounds: int, review_verdict: str,
                note: str | None = None) -> dict:
    """The proposal is refined: its plan is written, the task waits in ``refined`` for
    the operator to approve the two together, and the claim ends. The reply names the
    live claims and current plans the footprint overlaps; none is refused."""
    taskdb.check_agent(agent)
    paths = plans.check(approach, footprint, tests, out_of_scope, risks, review_rounds,
                        review_verdict)
    notes.check_note(note, required=False)
    fields = {"approach": approach, "tests": tests, "out_of_scope": out_of_scope,
              "risks": risks, "review_rounds": review_rounds,
              "review_verdict": review_verdict}
    with taskdb.writing() as conn:
        row, held, revision = _close(conn, agent, repo, task, token, "submit_plan")
        plan = plans.insert(conn, task, revision, agent, fields, paths)
        note_id = (None if note is None
                   else notes.post(conn, task, agent, note, move="submit_plan"))
        taskdb.record(conn, agent, "submit_plan", task, revision=revision, note=note,
                      note_id=note_id, plan=plan, plan_footprint=paths, **fields,
                      **claims.ended(held))
        reply = {"task": task, "revision": revision, "plan": plan,
                 "overlaps": claims.overlaps(conn, row["repo_id"], task, paths),
                 "plan_overlaps": plans.overlaps(conn, row["repo_id"], task, paths)}
    return reply


def submit_work(agent: str, repo: str, task: int, token: str,
                note: str | None = None) -> dict:
    """The work is done: the task waits in ``landing`` for the operator to land it."""
    return _end_claim(agent, repo, task, token, note, "submit_work")


def send_back(agent: str, repo: str, task: int, token: str, note: str) -> dict:
    """The approved spec is not clear enough to implement: back to ``proposed``, with a
    note on what is unclear."""
    return _end_claim(agent, repo, task, token, note, "send_back")


def release(agent: str, repo: str, task: int, token: str, note: str | None = None) -> dict:
    """Give the task up; it stays where it is for another agent."""
    return _end_claim(agent, repo, task, token, note, "release")


class _Ending(NamedTuple):
    """A move that ends a claim. ``None`` for ``needs``: any state; for ``into``: the
    task stays where it is."""
    needs: str | None
    into: str | None
    refusal: str = ""


_ENDINGS = {
    "submit_plan": _Ending("proposed", "refined",
                           "a plan is for a proposed task; the work on an open one is "
                           "handed in with submit_work"),
    "submit_work": _Ending("open", "landing",
                           "a proposed task's claim refines it, and ends with "
                           "submit_plan"),
    "send_back": _Ending("open", "proposed",
                         "it is proposed already: release it, after a note "
                         "for_operator (add_note) if the proposal is not worth doing"),
    "release": _Ending(needs=None, into=None),
}


def _end_claim(agent: str, repo: str, task: int, token: str, note: str | None,
               action: str) -> dict:
    taskdb.check_agent(agent)
    notes.check_note(note, required=action == "send_back")
    with taskdb.writing() as conn:
        _, held, revision = _close(conn, agent, repo, task, token, action)
        note_id = None if note is None else notes.post(conn, task, agent, note, move=action)
        taskdb.record(conn, agent, action, task, revision=revision, note=note,
                      note_id=note_id, **claims.ended(held))
    return {"task": task, "revision": revision}


def _close(conn: sqlite3.Connection, agent: str, repo: str, task: int, token: str,
           action: str) -> tuple[sqlite3.Row, sqlite3.Row, int]:
    """End the holder's claim on ``task`` with ``action``, moving the task as the move
    does; the task as it was, the claim, and the new revision."""
    ending = _ENDINGS[action]
    row = tasks.task_row(conn, task)
    tasks.check_in_repo(row, repo)
    held = claims.check_holder(conn, row, agent, token)
    if ending.needs is not None and row["state"] != ending.needs:
        raise Refused(f"task {task} is {row['state']}: {ending.refusal}",
                      {"state": row["state"]})
    claims.end(conn, task)
    moved = {} if ending.into is None else {"state": ending.into}
    revision = tasks.update(conn, task, moved)
    return row, held, revision


# --- notes -----------------------------------------------------------------------


def add_note(agent: str, repo: str, task: int, text: str,
             for_operator: bool = False) -> dict:
    """Say something about a task in the agent's repo; no claim needed. A note
    informs and never instructs: the approved spec outranks it. ``for_operator``
    marks one that needs the operator."""
    taskdb.check_agent(agent)
    notes.check_note(text)
    tasks.check_flag(for_operator, "for_operator")
    with taskdb.writing() as conn:
        row = tasks.task_row(conn, task)
        tasks.check_in_repo(row, repo)
        note = notes.post(conn, task, agent, text, for_operator)
        taskdb.record(conn, agent, "note", task, note_id=note, note=text,
                      for_operator=for_operator)
    return {"task": task, "note": note}
