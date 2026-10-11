# SPDX-License-Identifier: Apache-2.0
"""The operator API: ``operator_moves`` and ``reads`` as plain JSON over HTTP, for the
control-plane UI to relay to. No HTML and no MCP: the UI renders, and agents reach the
registry through the gateway alone.

The app object only. What binds it, and where, is the service's entry
(``task-registry/DESIGN.md``, "The operator API").

Reads answer with what the read returns. Writes answer ``{"ok": true, ...}`` with the
move's result. A refusal answers ``{"ok": false, "detail": <reason>, "current": <what
is there now>}``: 404 when the task in the path, or a note or repo the request names,
does not exist; 409 when the revision named is stale (``current`` is then the task as
it is now); 400 for every other refusal, a missing parent or blocker among them. A
request not shaped as its route takes it answers 422 in the same shape (``invalid``),
and one Starlette turns away first, an unknown path, a wrong method or a body it cannot
parse, answers Starlette's status in the same shape too (``failed``), both before the
registry sees anything. So every answer carries ``ok``.
"""
from __future__ import annotations

from collections.abc import Callable

import operator_moves
import reads
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt
from starlette.exceptions import HTTPException
from taskdb import Missing, Refused, Stale

app = FastAPI(title="dockade task registry (operator)",
              openapi_url=None, docs_url=None, redoc_url=None)

#: What ``GET /api/tasks`` takes for "every state": the query string has no None.
EVERY_STATE = "all"

#: How many of a request's faults a 422 names; the rest are counted.
FAULTS_SHOWN = 10


# --- what a route answers: plain functions, so tests need no server ---------------

def read(fn: Callable, *args: object, **kwargs: object) -> tuple[int, object]:
    try:
        return 200, fn(*args, **kwargs)
    except Refused as refusal:
        return refused(refusal)


def write(fn: Callable, *args: object, **kwargs: object) -> tuple[int, dict]:
    try:
        result = fn(*args, **kwargs)
    except Refused as refusal:
        return refused(refusal)
    return 200, {"ok": True, **(result or {})}


def refused(refusal: Refused) -> tuple[int, dict]:
    status = (409 if isinstance(refusal, Stale)
              else 404 if isinstance(refusal, Missing) else 400)
    return status, {"ok": False, "detail": refusal.reason, "current": refusal.current}


def invalid(errors: list[dict]) -> tuple[int, dict]:
    """A request not shaped as its route takes it: each field and what is wrong with
    it, never the value. FastAPI's own answer echoes every rejected input, and one a
    JSON string can carry but UTF-8 cannot, a lone surrogate, then fails to encode and
    answers 500."""
    faults = [f"{_field(e.get('loc', ()))}: {e.get('msg', 'invalid')}"
              for e in errors[:FAULTS_SHOWN]]
    if len(errors) > FAULTS_SHOWN:
        faults.append(f"and {len(errors) - FAULTS_SHOWN} more")
    return 422, {"ok": False, "detail": _encodable("; ".join(faults)), "current": None}


def _field(loc: object) -> str:
    # An unknown field is named by its key, which the request chose: cut it short.
    return ".".join(str(part)[:100] for part in loc)


def _encodable(text: str) -> str:
    return text.encode("utf-8", "backslashreplace").decode()


def failed(status: int, detail: object) -> tuple[int, dict]:
    """What Starlette turns away before a route runs, with its status and its own fixed
    text ("Not Found", "There was an error parsing the body"), never the request's."""
    return status, {"ok": False, "detail": _encodable(str(detail)), "current": None}


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return _reply(invalid(exc.errors()))


@app.exception_handler(HTTPException)
async def turned_away(_request: Request, exc: HTTPException) -> JSONResponse:
    status, body = failed(exc.status_code, exc.detail)
    # A 405 carries the methods the path takes in its Allow header: keep it.
    return JSONResponse(body, status_code=status, headers=exc.headers)


def _reply(answer: tuple[int, object]) -> JSONResponse:
    status, body = answer
    return JSONResponse(body, status_code=status)


# --- request bodies -----------------------------------------------------------------
# Lists arrive as tuples: an immutable default, and the moves take either.

class Body(BaseModel):
    """A request body. An unknown field is refused, not ignored, so a misspelt one is
    not a change silently left unmade. Ids, revisions and flags are strict, so JSON's
    ``true`` and ``"1"`` are not numbers here, nor ``1`` a flag; field by field, since a
    model-wide ``strict`` would refuse a JSON array for a tuple."""
    model_config = ConfigDict(extra="forbid")


class CreateTask(Body):
    repo: str
    title: str
    body: str
    state: str
    severity: str | None = None
    kind: str | None = None
    parent: StrictInt | None = None
    blocked_by: tuple[StrictInt, ...] = ()


class EditTask(Body):
    revision: StrictInt
    title: str | None = None
    body: str | None = None
    severity: str | None = None
    kind: str | None = None
    blocked_by: tuple[StrictInt, ...] | None = None
    clear: tuple[str, ...] = ()


class MoveTask(Body):
    revision: StrictInt
    state: str


class AtRevision(Body):
    revision: StrictInt


class Reparent(Body):
    revision: StrictInt
    # Required, and null for none: an absent parent must not read as "no parent".
    parent: StrictInt | None


class NoteText(Body):
    text: str


class HideNotes(Body):
    notes: tuple[StrictInt, ...]
    hidden: StrictBool = True


class HandleNote(Body):
    handled: StrictBool = True
    reply: str | None = None


class HandleNotes(Body):
    notes: tuple[StrictInt, ...]
    handled: StrictBool = True


class AddRepo(Body):
    name: str
    remote: str | None = None


class RenameRepo(Body):
    new_name: str


class SetRemote(Body):
    # Required, and null to remove: an absent remote must not read as "remove it".
    remote: str | None


class RetireRepo(Body):
    retired: StrictBool = True


# --- tasks ------------------------------------------------------------------------

@app.get("/api/tasks")
def list_tasks(repo: str, state: str = "open", holder: str | None = None,
               parent: int | None = None, before: int | None = None) -> JSONResponse:
    return _reply(read(reads.list_tasks, repo, None if state == EVERY_STATE else state,
                       holder, parent, before))


@app.get("/api/search")
def search_tasks(text: str, repo: str | None = None) -> JSONResponse:
    return _reply(read(reads.search_tasks, text, repo))


@app.get("/api/tasks/{task}")
def get_task(task: int) -> JSONResponse:
    return _reply(read(reads.get_task, task))


@app.post("/api/tasks")
def create_task(req: CreateTask) -> JSONResponse:
    return _reply(write(operator_moves.create, req.repo, req.title, req.body, req.state,
                        severity=req.severity, kind=req.kind, parent=req.parent,
                        blocked_by=req.blocked_by))


@app.post("/api/tasks/{task}/edit")
def edit_task(task: int, req: EditTask) -> JSONResponse:
    return _reply(write(operator_moves.edit, task, req.revision, title=req.title,
                        body=req.body, severity=req.severity, kind=req.kind,
                        blocked_by=req.blocked_by, clear=req.clear))


@app.post("/api/tasks/{task}/move")
def move_task(task: int, req: MoveTask) -> JSONResponse:
    return _reply(write(operator_moves.move, task, req.revision, req.state))


@app.post("/api/tasks/{task}/break-claim")
def break_claim(task: int, req: AtRevision) -> JSONResponse:
    return _reply(write(operator_moves.break_claim, task, req.revision))


@app.post("/api/tasks/{task}/reparent")
def reparent(task: int, req: Reparent) -> JSONResponse:
    return _reply(write(operator_moves.reparent, task, req.revision, req.parent))


@app.get("/api/tasks/{task}/history")
def history(task: int, before: int | None = None) -> JSONResponse:
    return _reply(read(operator_moves.history, task, before))


# --- notes ------------------------------------------------------------------------

@app.get("/api/tasks/{task}/notes")
def all_notes(task: int, before: int | None = None) -> JSONResponse:
    return _reply(read(operator_moves.all_notes, task, before))


@app.post("/api/tasks/{task}/notes")
def add_note(task: int, req: NoteText) -> JSONResponse:
    return _reply(write(operator_moves.add_note, task, req.text))


@app.get("/api/notes/flagged")
def notes_for_operator(repo: str | None = None, handled: bool = False,
                       before: int | None = None) -> JSONResponse:
    return _reply(read(operator_moves.notes_for_operator, repo, handled, before))


@app.post("/api/notes/hide")
def hide_notes(req: HideNotes) -> JSONResponse:
    """Hide or show the notes named; one note is a list of one."""
    return _reply(write(operator_moves.hide_notes, req.notes, req.hidden))


@app.post("/api/notes/{note}/handle")
def handle_note(note: int, req: HandleNote) -> JSONResponse:
    """Mark one note handled, with a reply if there is one."""
    return _reply(write(operator_moves.handle_note, note, req.handled, req.reply))


@app.post("/api/notes/handle")
def handle_notes(req: HandleNotes) -> JSONResponse:
    """Mark the notes named handled, or not, without a reply: for after a flood."""
    return _reply(write(operator_moves.handle_notes, req.notes, req.handled))


# --- repos ------------------------------------------------------------------------

@app.get("/api/repos")
def repos() -> JSONResponse:
    return _reply(read(reads.repos))


@app.post("/api/repos")
def add_repo(req: AddRepo) -> JSONResponse:
    return _reply(write(operator_moves.add_repo, req.name, req.remote))


@app.post("/api/repos/{name}/rename")
def rename_repo(name: str, req: RenameRepo) -> JSONResponse:
    return _reply(write(operator_moves.rename_repo, name, req.new_name))


@app.post("/api/repos/{name}/remote")
def set_remote(name: str, req: SetRemote) -> JSONResponse:
    return _reply(write(operator_moves.set_remote, name, req.remote))


@app.post("/api/repos/{name}/retire")
def retire_repo(name: str, req: RetireRepo) -> JSONResponse:
    return _reply(write(operator_moves.retire_repo, name, req.retired))
