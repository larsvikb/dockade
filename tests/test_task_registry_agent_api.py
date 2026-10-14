# SPDX-License-Identifier: Apache-2.0
"""The task registry's agent listener (``task-registry/agent_api.py``,
``agent_tools.py``): the MCP wire, the gateway's secret, the tools and the schema each
call is checked against, and that the surface calls only what an agent may. Each test
that touches the store gets one of its own."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

import _surface
from _loader import ROOT, load_task_registry_agent_api

reg = load_task_registry_agent_api()
Refused = reg.taskdb.Refused
api, tools, op, reads = reg.agent_api, reg.agent_tools, reg.operator_moves, reg.reads

ALICE = "claude-0b6f3a52-91d4-4c1e-a7e2-5d0c8e2f4b19"
BOB = "claude-7e21c9d0-3a5b-4f86-b0d4-c1e9a2f7d360"
SECRET = "s3cret-" * 6
EXPECTED = f"Bearer {SECRET}".encode()

TOOL_NAMES = ["list_tasks", "get_task", "task_notes", "search_tasks", "repos", "propose",
              "edit_proposal", "claim", "update", "submit_plan", "submit_work", "send_back",
              "release", "add_note"]

PLAN = {"approach": "A rotate step in the key store.", "footprint": ["src/keys.py"],
        "tests": "A rotation keeps old keys readable.", "out_of_scope": "None.",
        "risks": "A reader mid-rotation.", "review_rounds": 2,
        "review_verdict": "Passes."}


def rpc(method: str, params: dict | None = None, message_id: object = 7) -> dict:
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def through_the_wire(name: str, arguments: object) -> dict:
    """A tool call as the gateway sends it: JSON in, JSON out."""
    status, reply = api.answer(json.dumps(rpc("tools/call", {
        "name": name, "arguments": arguments})).encode())
    if status != 200:
        raise AssertionError((status, reply))
    # What Starlette's JSONResponse does with it: a lone surrogate in the reply raises
    # here and nowhere else.
    json.dumps(reply, ensure_ascii=False, allow_nan=False).encode()
    return reply["result"]


class _Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-agent-api-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        previous = reg.taskdb.DB_PATH
        self.addCleanup(setattr, reg.taskdb, "DB_PATH", previous)
        reg.taskdb.DB_PATH = os.path.join(self.dir, "tasks.db")
        reg.taskdb.init()
        op.add_repo("widgets")
        op.add_repo("gadgets")
        self.task = op.create("widgets", "A task", "What to do.", "open")["task"]

    def ok(self, name: str, **arguments) -> object:
        result = through_the_wire(name, arguments)
        self.assertFalse(result["isError"], result)
        return json.loads(result["content"][0]["text"])

    def refused(self, name: str, arguments: object) -> list[str]:
        result = through_the_wire(name, arguments)
        self.assertTrue(result["isError"], result)
        return [block["text"] for block in result["content"]]


class WireTests(unittest.TestCase):
    def test_initialize_names_the_server_and_settles_a_version(self):
        reply = api.handle(rpc("initialize", {"protocolVersion": "2025-03-26"}))["result"]
        self.assertEqual((reply["protocolVersion"], reply["serverInfo"]["name"],
                          reply["capabilities"]), ("2025-03-26", "tasks", {"tools": {}}))
        unknown = api.handle(rpc("initialize", {"protocolVersion": "1999-01-01"}))
        self.assertEqual(unknown["result"]["protocolVersion"], api.PROTOCOL_VERSIONS[0])

    def test_a_notification_gets_no_answer(self):
        self.assertIsNone(api.handle({"jsonrpc": "2.0", "method": "notifications/x"}))
        self.assertEqual(api.answer(b'{"jsonrpc": "2.0", "method": "notifications/x"}'),
                         (202, None))

    def test_a_malformed_request_is_an_error_with_its_id(self):
        cases = [([rpc("ping")], None, api.INVALID_REQUEST),
                 ("ping", None, api.INVALID_REQUEST),
                 ({"id": 3, "method": "ping"}, 3, api.INVALID_REQUEST),
                 (rpc("resources/list"), 7, api.METHOD_NOT_FOUND),
                 (rpc("tools/call", {"arguments": {}}), 7, api.INVALID_PARAMS)]
        for message, message_id, code in cases:
            with self.subTest(message=message):
                reply = api.handle(message)
                self.assertEqual((reply["id"], reply["error"]["code"]), (message_id, code))
        self.assertEqual(api.handle(rpc("ping"))["result"], {})

    def test_a_body_that_is_not_json_is_a_parse_error(self):
        for body in (b"not json", b"\xff\xfe", b""):
            with self.subTest(body=body):
                status, reply = api.answer(body)
                self.assertEqual((status, reply["id"], reply["error"]["code"]),
                                 (400, None, api.PARSE_ERROR))


class SecretTests(unittest.TestCase):
    def run_request(self, header, chunks=(b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}',)):
        read = []

        async def body():
            for chunk in chunks:
                read.append(chunk)
                yield chunk

        return asyncio.run(api.respond(header, body(), EXPECTED)), read

    def test_only_the_gateways_secret_is_let_in_and_nothing_is_read_before(self):
        for header in (None, "", SECRET, f"bearer {SECRET}", f"Bearer {SECRET} ",
                       f"Bearer {SECRET[:-1]}", "Bearer " + "x" * len(SECRET)):
            with self.subTest(header=header):
                (status, reply), read = self.run_request(header)
                self.assertEqual((status, reply, read), (401, {"error": api.UNAUTHORIZED}, []))
        (status, reply), _ = self.run_request(f"Bearer {SECRET}")
        self.assertEqual((status, reply["result"]), (200, {}))

    def test_a_body_past_the_cap_is_refused_without_reading_the_rest(self):
        chunks = [b"x" * 600_000, b"x" * 600_000, b"never read"]
        (status, _), read = self.run_request(f"Bearer {SECRET}", chunks)
        self.assertEqual((status, len(read)), (413, 2))

    def test_the_secret_comes_from_the_gateways_file_or_nothing_starts(self):
        directory = tempfile.mkdtemp(prefix="dockade-agent-secret-")
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "tasks.json")
        self.assertIn(path, str(self.raises(api.load_secret, path)))
        for token in (7, "x" * (api.SECRET_MIN - 1), SECRET + " ", " " + SECRET,
                      SECRET[:20] + " " + SECRET[20:], SECRET + "\n", "é" * 40,
                      "€" * 40):
            with self.subTest(token=token):
                Path(path).write_text(json.dumps({"token": token}))
                self.raises(api.load_secret, path)
                if isinstance(token, str):
                    self.raises(api.build, token)
        for content in ("not json", "[]", '{"note": "x"}'):
            with self.subTest(content=content):
                Path(path).write_text(content)
                self.raises(api.load_secret, path)
        Path(path).write_text(json.dumps({"token": SECRET, "note": "for the gateway"}))
        self.assertEqual(api.load_secret(path), SECRET)

    def test_the_app_needs_a_secret_and_serves_two_routes(self):
        for bad in (None, "", "x" * (api.SECRET_MIN - 1)):
            with self.subTest(secret=bad):
                self.raises(api.build, bad)
        self.assertEqual(sorted(api.build(SECRET).routes),
                         [("GET", "/healthz"), ("POST", "/mcp")])

    def raises(self, fn, *args) -> RuntimeError:
        with self.assertRaises(RuntimeError) as caught:
            fn(*args)
        return caught.exception


class ListingTests(unittest.TestCase):
    def test_the_tools_are_the_agents_moves_and_reads(self):
        listed = api.handle(rpc("tools/list"))["result"]["tools"]
        self.assertEqual([t["name"] for t in listed], TOOL_NAMES)
        for tool in listed:
            with self.subTest(tool=tool["name"]):
                run = tools.TOOLS[tool["name"]].run
                self.assertIn(run.__module__, ("agent_moves", "reads"))
                self.assertEqual(tool["annotations"]["readOnlyHint"],
                                 run.__module__ == "reads")
                schema = tool["inputSchema"]
                self.assertFalse(schema["additionalProperties"])
                self.assertTrue(tool["description"])
                self.assertTrue(all(p["description"] for p in schema["properties"].values()))

    def test_the_schema_is_the_functions_signature(self):
        """Every argument a move takes is in its served schema and nothing else is;
        required where the move has no default; and null allowed wherever the move's
        default is None, so an agent that sends null for "not given" is not refused."""
        for tool in tools.listing():
            signature = inspect.signature(tools.TOOLS[tool["name"]].run).parameters
            schema = tool["inputSchema"]
            with self.subTest(tool=tool["name"]):
                self.assertEqual(list(schema["properties"]), list(signature))
                self.assertEqual(schema["required"],
                                 [n for n, p in signature.items()
                                  if p.default is inspect.Parameter.empty])
                for name, prop in schema["properties"].items():
                    takes_null = "null" in prop["type"]
                    if signature[name].default is None:
                        self.assertTrue(takes_null, name)
                    elif takes_null:
                        # The one null that is not "not given": every state.
                        self.assertEqual((tool["name"], name), ("list_tasks", "state"))

    def test_the_schema_says_the_stores_choices_and_lengths(self):
        propose = next(t for t in tools.listing() if t["name"] == "propose")
        properties = propose["inputSchema"]["properties"]
        self.assertEqual(properties["severity"]["enum"], [*reg.tasks.SEVERITIES, None])
        self.assertEqual(properties["title"]["maxLength"], reg.tasks.TITLE_MAX)
        self.assertEqual(properties["blocked_by"]["items"], {"type": "integer"})


class SurfaceTests(unittest.TestCase):
    """The agent listener calls ``agent_moves`` and ``reads`` and nothing else of the
    store (``task-registry/DESIGN.md``, "Two writers, two modules"): from any other
    registry module it reads constants only."""

    CALLABLE = frozenset({"agent_moves", "reads"})

    def source(self, module: str) -> str:
        return (ROOT / "task-registry" / f"{module}.py").read_text()

    def test_the_tools_call_only_what_an_agent_may(self):
        imported, chains = _surface.reached(self.source("agent_tools"))
        self.assertNotIn("operator_moves", imported)
        self.assertEqual(_surface.overreach(chains, self.CALLABLE,
                                            frozenset({("taskdb", "Refused")})), [])

    def test_the_listener_reaches_the_store_only_through_the_tools(self):
        imported, _ = _surface.reached(self.source("agent_api"))
        self.assertEqual(imported & (_surface.STORE | {"operator_api"}), set())
        self.assertIn("agent_tools", imported)
        # Nor through them: ``agent_tools.reads`` is the store all the same.
        _, through = _surface.reached(self.source("agent_api"), frozenset({"agent_tools"}))
        self.assertEqual(_surface.overreach(through, frozenset({"agent_tools"})), [])


class InstructionTests(unittest.TestCase):
    """The descriptions name the variable an agent's id is made from and leave its
    repo's name to the repo's ``CLAUDE.md``; the instructions say where that variable is
    and give the name (``task-registry/DESIGN.md``, "Who an agent is" and "Repos are a
    list the operator keeps"), and name tools by name. Each must still match what the
    registry serves, or a session meets the mismatch at its first call."""

    IMAGE = ROOT / "claude-sandbox" / "user-CLAUDE.md"
    REPO = ROOT / "CLAUDE.md"
    # Any whitespace between the words, so a reflowed paragraph still matches.
    REPO_NAME = re.compile(r"name\s+in\s+the\s+task\s+registry\s+is\s+`([^`]*)`")

    def test_the_image_names_the_variable_the_id_description_names(self):
        variables = re.findall(r"\$([A-Z][A-Z0-9_]*)", tools.AGENT.description)
        self.assertEqual(len(variables), 1, variables)
        self.assertIn(f"`${variables[0]}`", self.IMAGE.read_text())
        # A session id is a UUID, and `claude-` and one is an id the store takes.
        agent = f"claude-{uuid.uuid4()}"
        self.assertEqual(reg.taskdb.check_agent(agent), agent)

    def test_this_repo_gives_one_name_the_store_would_take(self):
        self.assertIn("CLAUDE.md", tools.REPO.description)
        names = self.REPO_NAME.findall(self.REPO.read_text())
        self.assertEqual(len(names), 1, names)
        self.assertTrue(reg.tasks.REPO_NAME_RE.fullmatch(names[0]), names[0])

    def test_every_tool_the_instructions_name_is_served(self):
        named = {path.name: re.findall(r"tasks__(\w+)", path.read_text())
                 for path in (self.IMAGE, self.REPO)}
        self.assertTrue(named[self.REPO.name])
        for where, names in named.items():
            with self.subTest(file=where):
                self.assertEqual(sorted(set(names) - set(tools.TOOLS)), [])


class CallTests(_Store):
    def test_a_task_worked_from_claim_to_landing(self):
        claimed = self.ok("claim", agent=ALICE, repo="widgets", task=self.task,
                          branch="fix-widgets", footprint=["src"])
        token = claimed["token"]
        self.assertEqual(self.ok("list_tasks", repo="widgets", holder=ALICE)["total"], 1)
        updated = self.ok("update", agent=ALICE, repo="widgets", task=self.task,
                          token=token, progress="Halfway.")
        self.assertEqual(updated["task"], self.task)
        self.ok("add_note", agent=BOB, repo="widgets", task=self.task, text="Watch src/x.")
        self.ok("submit_work", agent=ALICE, repo="widgets", task=self.task, token=token,
                note="Ready.")
        got = self.ok("get_task", task=self.task)
        self.assertEqual((got["state"], [n["text"] for n in got["notes"]]),
                         ("landing", ["Watch src/x.", "Ready."]))
        self.assertNotIn(token, json.dumps(got))

    def test_a_proposal_is_filed_found_and_refined(self):
        task = self.ok("propose", agent=ALICE, repo="widgets", title="Rotate keys",
                       body="Every 90 days.", severity="low", blocked_by=[self.task])["task"]
        hits = self.ok("search_tasks", text="ROTATE")
        self.assertEqual([t["id"] for t in hits["tasks"]], [task])
        self.ok("edit_proposal", agent=ALICE, repo="widgets", task=task, revision=1,
                body="Every 30 days.", severity=None)
        self.assertEqual(self.ok("get_task", task=task)["body"], "Every 30 days.")
        self.assertEqual(self.ok("repos")[0]["name"], "gadgets")

    def test_a_proposal_planned_approved_and_done(self):
        task = self.ok("propose", agent=ALICE, repo="widgets", title="Rotate keys",
                       body="Every 90 days.")["task"]
        token = self.ok("claim", agent=ALICE, repo="widgets", task=task,
                        branch="rotate-keys", footprint=["src"])["token"]
        planned = self.ok("submit_plan", agent=ALICE, repo="widgets", task=task,
                          token=token, **PLAN)
        got = self.ok("get_task", task=task)
        self.assertEqual((got["state"], got["revision"]), ("refined", planned["revision"]))
        self.assertEqual({k: got["plan"][k] for k in PLAN}, PLAN)
        op.move(task, planned["revision"], "open")
        claimed = self.ok("claim", agent=BOB, repo="widgets", task=task,
                          branch="rotate-keys")
        self.assertEqual(reads.get_task(task)["claim"]["footprint"], ["src/keys.py"])
        self.ok("submit_work", agent=BOB, repo="widgets", task=task,
                token=claimed["token"])
        self.assertEqual(self.ok("get_task", task=task)["state"], "landing")

    def test_each_hand_in_names_the_other_on_the_wrong_state(self):
        token = self.ok("claim", agent=ALICE, repo="widgets", task=self.task,
                        branch="b", footprint=["."])["token"]
        reason, _ = self.refused("submit_plan", {"agent": ALICE, "repo": "widgets",
                                                 "task": self.task, "token": token, **PLAN})
        self.assertIn("submit_work", reason)

    def test_a_refusal_says_why_first_and_what_is_there_now_after(self):
        self.ok("claim", agent=ALICE, repo="widgets", task=self.task, branch="b",
                footprint=["."])
        reason, current = self.refused("claim", {"agent": BOB, "repo": "widgets",
                                                 "task": self.task, "branch": "c",
                                                 "footprint": ["."]})
        self.assertIn(f"held by {ALICE}", reason)
        self.assertEqual(json.loads(current)["current"]["holder"], ALICE)
        self.assertEqual(self.refused("get_task", {"task": 999}), ["no task 999"])

    def test_text_comes_back_as_written(self):
        task = self.ok("propose", agent=ALICE, repo="widgets", title="Åtgärda nycklar",
                       body="B")["task"]
        result = through_the_wire("get_task", {"task": task})
        self.assertIn("Åtgärda nycklar", result["content"][0]["text"])

    def test_a_call_with_no_arguments_takes_none(self):
        reply = api.handle(rpc("tools/call", {"name": "repos"}))["result"]
        self.assertEqual(len(json.loads(reply["content"][0]["text"])), 2)

    def test_null_is_not_given_where_the_move_has_no_value_of_its_own(self):
        self.assertEqual(self.ok("list_tasks", repo="widgets", state=None, holder=None,
                                 parent=None, before=None)["total"], 1)
        self.ok("propose", agent=ALICE, repo="widgets", title="T", body="B")
        self.assertEqual(self.ok("list_tasks", repo="widgets")["total"], 1)
        self.assertEqual(self.ok("list_tasks", repo="widgets", state=None)["total"], 2)
        self.refused("add_note", {"agent": ALICE, "repo": "widgets", "task": self.task,
                                  "text": "x", "for_operator": None})
        self.refused("propose", {"agent": ALICE, "repo": "widgets", "title": "T",
                                 "body": "B", "blocked_by": None})

    def test_arguments_are_checked_against_the_schema_before_anything_runs(self):
        cases = {
            "an unknown tool": ("delete_task", {"task": 1}, "no tool 'delete_task'"),
            "arguments not an object": ("get_task", [self.task], "an object"),
            "an unknown argument": ("get_task", {"task": self.task, "as": "operator"},
                                    "no argument 'as'"),
            "a missing argument": ("claim", {"agent": ALICE, "repo": "widgets"},
                                   "missing task, branch"),
            "true for an id": ("get_task", {"task": True}, "task is an integer"),
            "a string for an id": ("get_task", {"task": str(self.task)},
                                   "task is an integer"),
            "a float for an id": ("get_task", {"task": 1.0}, "task is an integer"),
            "a list of the wrong items": ("claim", {"agent": ALICE, "repo": "widgets",
                                                   "task": self.task, "branch": "b",
                                                   "footprint": [1]},
                                         "footprint is a list of strings"),
            "null where it means nothing": ("get_task", {"task": None}, "task is an integer"),
        }
        for case, (name, arguments, said) in cases.items():
            with self.subTest(case):
                self.assertIn(said, self.refused(name, arguments)[0])
        self.assertEqual(reads.get_task(self.task)["claim"], None)
        self.assertEqual(reads.list_tasks("widgets", None)["total"], 1)


class OddArgumentTests(_Store):
    """Whatever JSON an argument carries, a call answers: a result or a refusal, never
    an exception out of the store."""

    ODD = (None, True, False, 0, -1, 2**63, 2**64, 1.5, "", " ", "x", "\ud800",
           "a" * 50_000, [], [None], [0], [2**64], ["x"], ["\ud800"], ["../.."], {}, {"a": 1})

    def baseline(self, name: str) -> dict:
        token = agent_claim = None
        if name in ("update", "submit_plan", "submit_work", "send_back", "release"):
            state = "proposed" if name == "submit_plan" else "open"
            task = op.create("widgets", "T", "B", state)["task"]
            agent_claim = reg.agent_moves.claim(ALICE, "widgets", task, "b", ["."])
            token = agent_claim["token"]
        values = {"agent": ALICE, "repo": "widgets", "task": self.task, "token": token,
                  "title": "T", "body": "B", "text": "x", "branch": "b",
                  "revision": 1, "note": "n", "progress": "p", **PLAN}
        if agent_claim is not None:
            values["task"] = agent_claim["task"]
        params = tools.TOOLS[name].params
        return {p.name: values[p.name] for p in params if p.required}

    def test_no_argument_name_raises(self):
        for name in tools.TOOLS:
            for odd in ("\ud800", "", " ", "a" * 1000, "agent\n", "__class__"):
                with self.subTest(tool=name, argument=repr(odd)[:40]):
                    arguments = self.baseline(name) | {odd: 1}
                    self.assertTrue(through_the_wire(name, arguments)["isError"])

    def test_no_argument_value_raises(self):
        for name, tool in tools.TOOLS.items():
            for param in tool.params:
                for value in self.ODD:
                    with self.subTest(tool=name, param=param.name, value=repr(value)[:40]):
                        arguments = self.baseline(name) | {param.name: value}
                        result = through_the_wire(name, arguments)
                        self.assertIn(result["isError"], (True, False))


if __name__ == "__main__":
    unittest.main()
