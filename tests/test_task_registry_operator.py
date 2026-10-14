# SPDX-License-Identifier: Apache-2.0
"""The operator's side of the task registry: the paged reads, the bulk hide and handle
and the clear in ``task-registry/operator_moves.py``, and the HTTP API over them in
``task-registry/operator_api.py``: the routes it serves, what each answers, and how a
refusal becomes a status. Each test gets a store of its own."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _surface
from _loader import load_operator_api, load_task_registry

reg = load_task_registry()
api = load_operator_api()
Refused, Missing, Stale = reg.taskdb.Refused, reg.taskdb.Missing, reg.taskdb.Stale
agent, op, reads = reg.agent_moves, reg.operator_moves, reg.reads

ALICE = "claude-0b6f3a52-91d4-4c1e-a7e2-5d0c8e2f4b19"


class _Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-operator-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        previous = reg.taskdb.DB_PATH
        self.addCleanup(setattr, reg.taskdb, "DB_PATH", previous)
        reg.taskdb.DB_PATH = os.path.join(self.dir, "tasks.db")
        reg.taskdb.init()
        op.add_repo("widgets")
        self.task = op.create("widgets", "A task", "What to do.", "open")["task"]

    def note(self, text="Seen this.", **kw):
        return agent.add_note(ALICE, "widgets", self.task, text, **kw)["note"]

    def revision(self, task=None):
        return reads.get_task(task or self.task)["revision"]

    def refused(self, fn, *args, **kw) -> Refused:
        with self.assertRaises(Refused) as caught:
            fn(*args, **kw)
        return caught.exception


class PagingTests(_Store):
    def test_a_tasks_notes_come_a_page_at_a_time_hidden_ones_too(self):
        made = [self.note(f"Note {i}") for i in range(op.PAGE + 3)]
        op.hide_note(made[-1])
        first = op.all_notes(self.task)
        self.assertEqual(first["total"], op.PAGE + 3)
        self.assertEqual([n["id"] for n in first["notes"]], made[3:])
        self.assertTrue(first["notes"][-1]["hidden"])
        seen, page = [], first["notes"]
        for _ in range(10):
            if not page:
                break
            seen = [n["id"] for n in page] + seen
            page = op.all_notes(self.task, before=page[0]["id"])["notes"]
        self.assertEqual(seen, made)
        for bad in ("1", 0, True):
            with self.subTest(before=bad):
                self.refused(op.all_notes, self.task, before=bad)

    def test_a_tasks_history_comes_a_page_at_a_time(self):
        for i in range(op.PAGE + 3):
            self.note(f"Note {i}")
        first = op.history(self.task)
        self.assertEqual(first["total"], op.PAGE + 4)
        self.assertEqual([h["detail"]["note"] for h in first["history"]],
                         [f"Note {i}" for i in range(3, op.PAGE + 3)])
        seen, page = [], first["history"]
        for _ in range(10):
            if not page:
                break
            seen = page + seen
            page = op.history(self.task, before=page[0]["id"])["history"]
        self.assertEqual([h["action"] for h in seen], ["create"] + ["note"] * (op.PAGE + 3))
        self.assertEqual([h["id"] for h in seen], sorted(h["id"] for h in seen))
        for bad in ("1", 0, True):
            with self.subTest(before=bad):
                self.refused(op.history, self.task, before=bad)


class BulkTests(_Store):
    def test_a_bulk_hide_takes_every_note_or_none(self):
        first, second = self.note("One."), self.note("Two.")
        self.assertIsInstance(self.refused(op.hide_notes, [first, 999]), Missing)
        self.assertEqual(reads.get_task(self.task)["notes_total"], 2)
        self.assertEqual(op.hide_notes([first, second, first]), {"notes": [first, second]})
        self.assertEqual(reads.get_task(self.task)["notes_total"], 0)
        hides = [h for h in op.history(self.task)["history"] if h["action"] == "hide_note"]
        self.assertEqual([(h["author"], h["detail"]["note_id"]) for h in hides],
                         [(reg.taskdb.OPERATOR, first), (reg.taskdb.OPERATOR, second)])
        op.hide_notes([first, second], hidden=False)
        self.assertEqual(reads.get_task(self.task)["notes_total"], 2)

    def test_a_bulk_handle_takes_flagged_notes_only_and_every_one_or_none(self):
        flagged, plain = self.note("Q1", for_operator=True), self.note("FYI")
        other = self.note("Q2", for_operator=True)
        refusal = self.refused(op.handle_notes, [flagged, plain])
        self.assertIn("not marked for the operator", refusal.reason)
        self.assertEqual(op.notes_for_operator()["total"], 2)
        self.assertEqual(op.handle_notes([flagged, other]), {"notes": [flagged, other]})
        self.assertEqual(op.notes_for_operator()["total"], 0)
        last = op.history(self.task)["history"][-1]
        self.assertEqual((last["action"], last["detail"]),
                         ("handle_note", {"note_id": other, "handled": True,
                                          "reply": None, "reply_id": None}))
        op.handle_notes([flagged], handled=False)
        self.assertEqual(op.notes_for_operator()["total"], 1)

    def test_a_bulk_names_1_to_bulk_max_note_ids(self):
        note = self.note(for_operator=True)
        for fn in (op.hide_notes, op.handle_notes):
            for bad in ([], [note] * (op.BULK_MAX + 1), ["1"], [True], [[note]], note,
                        "1,2", None):
                with self.subTest(fn=fn.__name__, notes=bad):
                    self.refused(fn, bad)
        self.refused(op.hide_notes, [note], hidden="yes")
        self.refused(op.handle_notes, [note], handled=1)
        self.assertEqual(op.notes_for_operator()["total"], 1)
        self.assertEqual(op.hide_notes([note] * op.BULK_MAX), {"notes": [note]})


class ClearTests(_Store):
    def test_the_operator_empties_a_severity_or_a_kind(self):
        task = op.create("widgets", "T", "B", "open", severity="high",
                         kind="finding")["task"]
        revision = op.edit(task, 1, clear=["severity"])["revision"]
        got = reads.get_task(task)
        self.assertEqual((got["severity"], got["kind"], got["revision"]),
                         (None, "finding", 2))
        self.assertEqual(op.history(task)["history"][-1]["detail"],
                         {"revision": 2, "blocked_by": None, "severity": None})
        op.edit(task, revision, title="T2", clear=("kind", "severity"))
        got = reads.get_task(task)
        self.assertEqual((got["title"], got["severity"], got["kind"]), ("T2", None, None))

    def test_clear_names_only_those_fields_and_none_being_set(self):
        for bad in (["title"], ["parent_id"], "severity", None, [1], [["severity"]]):
            with self.subTest(clear=bad):
                self.refused(op.edit, self.task, 1, clear=bad)
        refusal = self.refused(op.edit, self.task, 1, severity="low", clear=["severity"])
        self.assertIn("both set and cleared", refusal.reason)
        self.assertEqual(self.revision(), 1)


class RefusalKindTests(_Store):
    def test_a_missing_task_or_note_and_a_stale_revision_are_told_apart(self):
        self.assertIsInstance(self.refused(op.edit, 999, 1, title="T"), Missing)
        self.assertIsInstance(self.refused(reads.get_task, 999), Missing)
        self.assertIsInstance(self.refused(op.handle_note, 999), Missing)
        stale = self.refused(op.move, self.task, 7, "deferred")
        self.assertIsInstance(stale, Stale)
        self.assertEqual(stale.current["revision"], 1)
        plain = self.refused(op.move, self.task, 1, "nowhere")
        self.assertNotIsInstance(plain, (Missing, Stale))

    def test_an_unknown_repo_is_missing_and_a_retired_one_refused(self):
        unknown = self.refused(op.create, "gizmos", "T", "B", "open")
        self.assertIsInstance(unknown, Missing)
        self.assertEqual(unknown.current, {"repos": ["widgets"]})
        self.assertIsInstance(self.refused(op.rename_repo, "gizmos", "gadgets"), Missing)
        op.retire_repo("widgets")
        retired = self.refused(op.create, "widgets", "T", "B", "open")
        self.assertNotIsInstance(retired, (Missing, Stale))

    def test_a_value_of_the_wrong_type_is_refused_not_raised(self):
        calls = {
            "blocked_by=7": lambda: op.create("widgets", "T", "B", "open", blocked_by=7),
            "create title=None": lambda: op.create("widgets", None, "B", "open"),
            "create body=None": lambda: op.create("widgets", "T", None, "open"),
            "propose title=None": lambda: agent.propose(ALICE, "widgets", None, "B"),
            "propose body=None": lambda: agent.propose(ALICE, "widgets", "T", None),
            "repo name 7": lambda: op.add_repo(7),
            "remote 7": lambda: op.add_repo("gadgets", remote=7),
            "new name 7": lambda: op.rename_repo("widgets", 7),
            "remote a list": lambda: op.set_remote("widgets", ["github.com/a/w"]),
        }
        for label, call in calls.items():
            with self.subTest(call=label):
                self.refused(call)
        self.assertEqual([r["name"] for r in reads.repos()], ["widgets"])
        self.assertEqual(reads.list_tasks("widgets", None)["total"], 1)


class RouteTests(_Store):
    ROUTES = frozenset({
        ("GET", "/api/repos"), ("POST", "/api/repos"),
        ("POST", "/api/repos/{name}/rename"), ("POST", "/api/repos/{name}/remote"),
        ("POST", "/api/repos/{name}/retire"),
        ("GET", "/api/tasks"), ("POST", "/api/tasks"), ("GET", "/api/search"),
        ("GET", "/api/tasks/{task}"), ("POST", "/api/tasks/{task}/edit"),
        ("POST", "/api/tasks/{task}/move"), ("POST", "/api/tasks/{task}/break-claim"),
        ("POST", "/api/tasks/{task}/reparent"), ("GET", "/api/tasks/{task}/history"),
        ("GET", "/api/tasks/{task}/plans"),
        ("GET", "/api/tasks/{task}/notes"), ("POST", "/api/tasks/{task}/notes"),
        ("GET", "/api/notes/flagged"), ("POST", "/api/notes/hide"),
        ("POST", "/api/notes/handle"), ("POST", "/api/notes/{note}/handle"),
        ("GET", "/api/inbox"),
    })

    def call(self, route, *args):
        response = route(*args)
        json.dumps(response.body)
        return response.status_code, response.body

    def test_the_app_serves_exactly_these_routes(self):
        self.assertEqual(len(api.app.routes), len(set(api.app.routes)))
        self.assertEqual(set(api.app.routes), self.ROUTES)

    def test_every_operator_move_and_read_is_served(self):
        served = set(re.findall(r"operator_moves\.(\w+)", inspect.getsource(api)))
        public = {name for name, fn in inspect.getmembers(op, inspect.isfunction)
                  if not name.startswith("_") and fn.__module__ == op.__name__}
        # One note is hidden through the bulk route, as a list of one.
        self.assertEqual(public - served, {"hide_note"},
                         "a new operator move must be served here, or named here")

    def test_a_write_answers_ok_with_what_the_move_returned(self):
        status, body = self.call(api.create_task, api.CreateTask(
            repo="widgets", title="T", body="B", state="proposed", severity="low"))
        self.assertEqual((status, body), (200, {"ok": True, "task": body["task"],
                                                "revision": 1}))
        self.assertEqual(reads.get_task(body["task"])["severity"], "low")
        self.assertEqual(self.call(api.retire_repo, "widgets", api.RetireRepo()),
                         (200, {"ok": True}))

    def test_each_task_route_reaches_its_move(self):
        other = op.create("widgets", "Parent", "B", "open")["task"]
        _, body = self.call(api.edit_task, self.task, api.EditTask(
            revision=1, title="Renamed", blocked_by=(other,)))
        self.assertEqual(body, {"ok": True, "task": self.task, "revision": 2})
        self.assertEqual(reads.get_task(self.task)["blockers"][0]["id"], other)
        self.call(api.edit_task, self.task, api.EditTask(revision=2, blocked_by=()))
        token = agent.claim(ALICE, "widgets", self.task, "fix", ["."])["token"]
        self.assertTrue(token)
        _, body = self.call(api.break_claim, self.task, api.AtRevision(revision=4))
        self.assertEqual((body["revision"], reads.get_task(self.task)["claim"]), (5, None))
        self.call(api.reparent, self.task, api.Reparent(revision=5, parent=other))
        self.assertEqual(reads.get_task(self.task)["parent"]["id"], other)
        self.call(api.reparent, self.task, api.Reparent(revision=6, parent=None))
        self.call(api.move_task, self.task, api.MoveTask(revision=7, state="deferred"))
        got = reads.get_task(self.task)
        self.assertEqual((got["title"], got["parent"], got["state"]),
                         ("Renamed", None, "deferred"))
        op.edit(self.task, 8, severity="high")
        self.call(api.edit_task, self.task, api.EditTask(revision=9, clear=("severity",)))
        self.assertIsNone(reads.get_task(self.task)["severity"])

    def test_each_note_route_reaches_its_move(self):
        _, body = self.call(api.add_note, self.task, api.NoteText(text="From the operator."))
        mine = body["note"]
        question = self.note("Which one?", for_operator=True)
        another = self.note("And this?", for_operator=True)
        _, body = self.call(api.handle_note, question, api.HandleNote(reply="The first."))
        self.assertEqual((body["ok"], body["note"]), (True, question))
        self.assertEqual(reads.get_task(self.task)["notes"][-1]["reply_to"], question)
        self.assertEqual(self.call(api.handle_notes, api.HandleNotes(notes=(another,))),
                         (200, {"ok": True, "notes": [another]}))
        self.assertEqual(op.notes_for_operator()["total"], 0)
        self.call(api.handle_notes, api.HandleNotes(notes=(another,), handled=False))
        self.assertEqual(op.notes_for_operator()["total"], 1)
        self.assertEqual(self.call(api.hide_notes, api.HideNotes(notes=(mine,))),
                         (200, {"ok": True, "notes": [mine]}))
        self.call(api.hide_notes, api.HideNotes(notes=(question,), hidden=True))
        self.call(api.hide_notes, api.HideNotes(notes=(mine,), hidden=False))
        _, shown = self.call(api.all_notes, self.task)
        self.assertEqual([(n["id"], n["hidden"]) for n in shown["notes"]][:3],
                         [(mine, False), (question, True), (another, False)])

    def test_each_repo_route_reaches_its_move(self):
        self.call(api.add_repo, api.AddRepo(name="gadgets", remote="github.com/a/g"))
        self.assertEqual(reads.repos()[0]["remote"], "github.com/a/g")
        self.call(api.rename_repo, "gadgets", api.RenameRepo(new_name="gizmos"))
        self.call(api.set_remote, "gizmos", api.SetRemote(remote=None))
        self.call(api.retire_repo, "gizmos", api.RetireRepo(retired=True))
        _, got = self.call(api.repos)
        self.assertEqual(got, [{"name": "gizmos", "remote": None, "retired": True},
                               {"name": "widgets", "remote": None, "retired": False}])
        self.call(api.set_remote, "widgets", api.SetRemote(remote="github.com/a/w"))
        self.call(api.retire_repo, "gizmos", api.RetireRepo(retired=False))
        self.assertEqual([(r["remote"], r["retired"]) for r in reads.repos()],
                         [(None, False), ("github.com/a/w", False)])

    def test_reads_pass_through_with_their_arguments(self):
        op.create("widgets", "Later", "B", "deferred")
        _, open_only = self.call(api.list_tasks, "widgets")
        _, every = self.call(api.list_tasks, "widgets", api.EVERY_STATE)
        self.assertEqual((open_only, every), (reads.list_tasks("widgets"),
                                              reads.list_tasks("widgets", None)))
        self.assertNotEqual(open_only, every)
        newest = every["tasks"][0]["id"]
        _, older = self.call(api.list_tasks, "widgets", api.EVERY_STATE, None, None, newest)
        self.assertEqual(older, reads.list_tasks("widgets", None, before=newest))
        self.assertEqual(older["total"], every["total"])
        self.assertNotIn(newest, [t["id"] for t in older["tasks"]])
        self.assertEqual(self.call(api.list_tasks, "widgets", "deferred", None, None)[1],
                         reads.list_tasks("widgets", "deferred"))
        child = op.create("widgets", "Child", "B", "open", parent=self.task)["task"]
        _, children = self.call(api.list_tasks, "widgets", "open", None, self.task)
        self.assertEqual(children, reads.list_tasks("widgets", parent=self.task))
        self.assertIn(f"'id': {child}", repr(children))
        self.assertNotIn(f"'id': {self.task},", repr(children))
        op.add_repo("gadgets")
        op.create("gadgets", "Later too", "B", "open")
        _, found = self.call(api.search_tasks, "Later", "widgets")
        self.assertEqual(found, reads.search_tasks("Later", "widgets"))
        self.assertEqual(found["total"], 1)
        self.assertEqual(self.call(api.get_task, self.task)[1], reads.get_task(self.task))
        notes = [self.note(f"N{i}", for_operator=True) for i in range(3)]
        op.handle_note(notes[0])
        self.assertEqual(self.call(api.all_notes, self.task, notes[2])[1],
                         op.all_notes(self.task, before=notes[2]))
        self.assertEqual(self.call(api.history, self.task, 3)[1],
                         op.history(self.task, before=3))
        self.assertEqual(self.call(api.notes_for_operator, "widgets", True, None)[1],
                         op.notes_for_operator("widgets", handled=True))
        self.assertEqual(self.call(api.notes_for_operator, None, False, notes[2])[1],
                         op.notes_for_operator(before=notes[2]))
        self.assertEqual(self.call(api.inbox)[1], op.inbox())
        self.assertEqual(op.inbox()["waiting"]["total"], 1)

    def test_a_refusal_answers_its_reason_and_what_is_there_now(self):
        cases = [
            (404, api.get_task, (999,)),
            (404, api.history, (999,)),
            (404, api.all_notes, (999,)),
            (404, api.edit_task, (999, api.EditTask(revision=1, title="T"))),
            (404, api.add_note, (999, api.NoteText(text="Hi."))),
            (404, api.handle_note, (999, api.HandleNote())),
            (404, api.hide_notes, (api.HideNotes(notes=(999,)),)),
            (409, api.move_task, (self.task, api.MoveTask(revision=7, state="closed"))),
            (409, api.break_claim, (self.task, api.AtRevision(revision=7))),
            (400, api.move_task, (self.task, api.MoveTask(revision=1, state="nowhere"))),
            (400, api.break_claim, (self.task, api.AtRevision(revision=1))),
            (400, api.handle_notes, (api.HandleNotes(notes=()),)),
            (400, api.notes_for_operator, (None, False, "1")),
            (404, api.list_tasks, ("gizmos",)),
            (404, api.create_task, (api.CreateTask(repo="gizmos", title="T", body="B",
                                                   state="open"),)),
            (404, api.rename_repo, ("gizmos", api.RenameRepo(new_name="gadgets"))),
            (400, api.create_task, (api.CreateTask(repo="widgets", title="T", body="B",
                                                   state="open", parent=999),)),
        ]
        for status, route, args in cases:
            with self.subTest(route=route.__name__, status=status):
                got, body = self.call(route, *args)
                self.assertEqual((got, body["ok"]), (status, False))
                self.assertTrue(body["detail"])
        _, stale = self.call(api.move_task, self.task,
                             api.MoveTask(revision=7, state="closed"))
        self.assertEqual(stale["current"], reads.get_task(self.task))
        self.assertIn("revision 1", stale["detail"])
        self.assertEqual(self.revision(), 1)


# Each read route, and the read it serves.
READS = {
    "list_tasks": (reads, "list_tasks"), "search_tasks": (reads, "search_tasks"),
    "get_task": (reads, "get_task"), "repos": (reads, "repos"),
    "task_plans": (reads, "task_plans"),
    "history": (op, "history"), "all_notes": (op, "all_notes"),
    "notes_for_operator": (op, "notes_for_operator"), "inbox": (op, "inbox"),
}
# (route, parameter): what the route turns a value into, where it does not pass it on
# as it came.
MAPPED = {("list_tasks", "state"): {api.EVERY_STATE: None}}


class ReadRouteTests(unittest.TestCase):
    """Each read route takes every parameter of the read it serves, by the same name and
    in the same order, and passes it on: a parameter the read gains, a page cursor
    say, fails here until the route takes it too."""

    def parameters(self, route):
        module, name = READS[route]
        return list(inspect.signature(getattr(module, name)).parameters)

    def served(self, route, args):
        module, name = READS[route]
        signature = inspect.signature(getattr(module, name))
        calls = []

        def record(*a, **k):
            calls.append(signature.bind(*a, **k).arguments)
            return {}
        with mock.patch.object(module, name, record):
            getattr(api, route)(**args)
        [call] = calls
        return call

    def test_every_read_route_is_checked_here(self):
        gets = [path for method, path in api.app.routes if method == "GET"]
        self.assertEqual(len(gets), len(READS))

    def test_each_read_route_passes_every_parameter_of_its_read(self):
        def declared(fn):
            return [(name, p.default) for name, p in inspect.signature(fn).parameters.items()]
        for route, (module, read) in READS.items():
            with self.subTest(route=route):
                # Defaults too: a route defaulting otherwise would answer a request that
                # leaves the parameter out unlike the read it serves.
                self.assertEqual(declared(getattr(api, route)), declared(getattr(module, read)))
                values = {name: object() for name in self.parameters(route)}
                self.assertEqual(self.served(route, values), values)

    def test_a_mapped_parameter_arrives_mapped(self):
        for (route, parameter), mapping in MAPPED.items():
            for given, passed in mapping.items():
                with self.subTest(route=route, given=given):
                    args = {name: object() for name in self.parameters(route)}
                    args[parameter] = given
                    self.assertEqual(self.served(route, args)[parameter], passed)


class InvalidRequestTests(unittest.TestCase):
    def test_a_422_names_each_field_and_never_its_value(self):
        status, body = api.invalid([
            {"loc": ("body", "revision"), "msg": "Input should be a valid integer",
             "input": "SECRET-INPUT", "type": "int_type"},
            {"loc": ("body", "notes", 0), "msg": "Input should be a valid integer",
             "input": "\ud800"},
        ])
        self.assertEqual((status, body["ok"], body["current"]), (422, False, None))
        self.assertEqual(body["detail"], "body.revision: Input should be a valid integer; "
                                         "body.notes.0: Input should be a valid integer")
        self.assertNotIn("SECRET", json.dumps(body))

    def test_a_field_the_request_named_is_escaped_and_cut_short(self):
        _, body = api.invalid([{"loc": ("body", "\ud800" + "x" * 300),
                                "msg": "Extra inputs are not permitted"}])
        body["detail"].encode()
        self.assertTrue(body["detail"].startswith("body.\\ud800xx"))
        self.assertLess(len(body["detail"]), 150)

    def test_only_the_first_faults_are_named_and_the_rest_counted(self):
        errors = [{"loc": ("body", "notes", i), "msg": "bad"}
                  for i in range(api.FAULTS_SHOWN + 5)]
        _, body = api.invalid(errors)
        self.assertEqual(body["detail"].count("bad"), api.FAULTS_SHOWN)
        self.assertTrue(body["detail"].endswith("; and 5 more"))
        _, body = api.invalid(errors[:api.FAULTS_SHOWN])
        self.assertNotIn("more", body["detail"])

    def test_the_app_answers_a_malformed_request_itself(self):
        rejected = sys.modules["fastapi.exceptions"].RequestValidationError
        self.assertIs(api.app.exception_handlers[rejected], api.invalid_request)
        exc = rejected([{"loc": ("body", "revision"), "msg": "bad", "input": "SECRET"}])
        response = asyncio.run(api.invalid_request(None, exc))
        self.assertEqual((response.status_code, response.body), api.invalid(exc.errors()))

    def test_what_starlette_turns_away_is_answered_in_the_apis_shape(self):
        turned = sys.modules["starlette.exceptions"].HTTPException
        self.assertIs(api.app.exception_handlers[turned], api.turned_away)
        for status, detail in ((400, "There was an error parsing the body"),
                               (404, "Not Found"), (405, "Method Not Allowed")):
            with self.subTest(status=status):
                exc = turned(status, detail, {"Allow": "GET"} if status == 405 else None)
                response = asyncio.run(api.turned_away(None, exc))
                self.assertEqual((response.status_code, response.body),
                                 (status, {"ok": False, "detail": detail, "current": None}))
                self.assertEqual(response.headers, exc.headers or {})
        self.assertEqual(api.failed(400, "half \ud800")[1]["detail"], "half \\ud800")


class SurfaceTests(unittest.TestCase):
    """The API calls ``operator_moves`` and ``reads`` and nothing else of the store:
    from any other registry module it takes constants and the refusal classes only."""

    CALLABLE = frozenset({"operator_moves", "reads"})
    REFUSALS = frozenset({("taskdb", "Refused"), ("taskdb", "Missing"), ("taskdb", "Stale")})

    def test_the_api_calls_only_the_operators_moves_and_the_reads(self):
        imported, chains = _surface.reached(Path(api.__file__).read_text())
        self.assertNotIn("agent_moves", imported)
        self.assertLessEqual(self.CALLABLE, imported)
        self.assertEqual(_surface.overreach(chains, self.CALLABLE, self.REFUSALS), [])

    def test_the_guard_sees_through_an_alias_and_a_chain(self):
        cases = {
            "import tasks as _t\n_t.update(conn, 1, {})": ("tasks", "update"),
            "from tasks import update as u\nu(conn, 1, {})": ("tasks", "update"),
            "import reads\nreads.tasks.update(conn, 1, {})": ("reads", "tasks", "update"),
            "import operator_moves as om\nom.claims.end(conn, 1)":
                ("operator_moves", "claims", "end"),
            "import tasks\nhand_over(tasks)": ("tasks",),
        }
        for source, chain in cases.items():
            with self.subTest(source=source):
                _, chains = _surface.reached(source)
                self.assertIn(chain, chains)
                self.assertEqual(_surface.overreach(chains, self.CALLABLE), [chain])
        _, fine = _surface.reached("import reads\nimport tasks\nreads.get_task(tasks.ID_MAX)")
        self.assertEqual(_surface.overreach(fine, self.CALLABLE), [])

    def test_every_body_refuses_unknown_fields_and_takes_ids_and_flags_strictly(self):
        bodies = [model for _, model in inspect.getmembers(api, inspect.isclass)
                  if issubclass(model, api.BaseModel) and model is not api.BaseModel]
        self.assertGreater(len(bodies), 10)
        seen = []
        for model in bodies:
            with self.subTest(model=model.__name__):
                self.assertTrue(issubclass(model, api.Body))
                self.assertEqual(model.model_config.get("extra"), "forbid")
                for annotation in vars(model).get("__annotations__", {}).values():
                    self.assertNotRegex(annotation, r"\b(int|bool)\b")
                    seen.append(annotation)
        self.assertIn("tuple[StrictInt, ...]", seen)
        self.assertIn("StrictBool", seen)


if __name__ == "__main__":
    unittest.main()
