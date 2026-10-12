# SPDX-License-Identifier: Apache-2.0
"""The task registry's entry, ``task-registry/serve.py``: where each listener may bind,
which app each address serves, and that nothing opens before the checks pass. Where
compose puts the two legs is held in ``tests/test_topology.py``."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from _loader import load_task_registry_serve

serve = load_task_registry_serve()
agent_api, operator_api, taskdb = serve.agent_api, serve.operator_api, serve.taskdb

AGENT_NET, OPERATOR_NET = "172.28.0.0/24", "172.26.0.0/24"
AGENT, OPERATOR = "172.28.0.3", "172.26.0.2"
SECRET = "s3cret-" * 6


class BindTests(unittest.TestCase):
    def check(self, agent: str = AGENT, operator: str = OPERATOR,
              agent_net: str = AGENT_NET, operator_net: str = OPERATOR_NET) -> None:
        serve.check_binds(agent, operator, agent_net, operator_net)

    def refused(self, **kwargs: str) -> str:
        with self.assertRaises(SystemExit) as caught:
            self.check(**kwargs)
        return str(caught.exception)

    def test_each_listener_on_its_own_network_starts(self):
        self.check()

    def test_anything_but_an_address_on_its_own_network_is_refused(self):
        # A wildcard serves every leg; a name would make the check depend on DNS; the
        # mapped spelling is another family, which uvicorn would bind as such.
        for bind in ("", "0.0.0.0", "::", "*", "tasks", "localhost", "127.0.0.1",  # noqa: S104
                     "::ffff:172.26.0.2", "::ffff:172.28.0.3", "172.27.0.3"):
            for listener in ("agent", "operator"):
                with self.subTest(listener=listener, bind=bind):
                    message = self.refused(**{listener: bind})
                    self.assertIn(f"TASKS_{listener.upper()}_BIND={bind!r}", message)

    def test_the_operator_listener_is_never_served_on_the_agents_network(self):
        # The one this module exists for: no token, so every MCP server would write as
        # the operator.
        message = self.refused(operator=AGENT)
        self.assertIn("mcp-net", message)
        self.assertIn("no token", message)

    def test_the_agent_listener_is_not_served_on_the_operators_network(self):
        self.refused(agent=OPERATOR)

    def test_networks_that_overlap_are_refused_whatever_the_binds(self):
        # Overlapping, the operator bind could sit on mcp-net and still be "inside" its
        # own network.
        for agent_net, operator_net in (("172.28.0.0/24", "172.28.0.0/24"),
                                        ("172.28.0.0/24", "172.28.0.0/16"),
                                        ("172.0.0.0/8", "172.26.0.0/24"),
                                        ("172.28.0.0/24", "172.28.0.0/25")):
            with self.subTest(agent_net=agent_net, operator_net=operator_net):
                self.assertIn("overlap", self.refused(agent_net=agent_net,
                                                      operator_net=operator_net,
                                                      operator=AGENT))

    def test_a_network_that_does_not_parse_is_refused(self):
        # Host bits set is a typo too: 172.26.0.2/24 for 172.26.0.0/24.
        for net in ("", "172.26.0.2/24", "172.26.0.0/33", "tasks-operator-net"):
            for key, var in (("agent_net", "TASKS_AGENT_NET"),
                             ("operator_net", "TASKS_OPERATOR_NET")):
                with self.subTest(var=var, net=net):
                    self.assertIn(f"{var}={net!r}", self.refused(**{key: net}))


class OnlyFromTests(unittest.TestCase):
    """The wrapper each listener's app is served in: a peer outside its network is
    refused before the app sees the request."""

    def call(self, scope: dict) -> tuple[list[dict], list[dict]]:
        seen, sent = [], []

        async def app(scope, receive, send):
            seen.append(scope)

        async def send(message):
            sent.append(message)

        wrapped = serve.OnlyFrom(app, serve._network("TASKS_OPERATOR_NET", OPERATOR_NET))
        asyncio.run(wrapped(scope, None, send))
        return seen, sent

    def test_a_peer_on_its_network_reaches_the_app(self):
        scope = {"type": "http", "client": ("172.26.0.10", 40000)}
        self.assertEqual(self.call(scope), ([scope], []))

    def test_any_other_peer_is_refused_and_the_app_never_runs(self):
        for client in (("172.28.0.11", 40000), ("172.28.0.3", 40000), ("127.0.0.1", 1),
                       ("::ffff:172.26.0.10", 1), ("tasks", 1), None):
            with self.subTest(client=client):
                seen, sent = self.call({"type": "http", "client": client})
                self.assertEqual(seen, [])
                self.assertEqual(sent[0]["status"], 403)
                self.assertEqual(json.loads(sent[1]["body"]),
                                 {"ok": False, "current": None,
                                  "detail": "this listener answers its own network only"})

    def test_a_scope_with_no_peer_but_the_servers_own_passes(self):
        # The lifespan scope has no client; it is uvicorn's, not a request.
        scope = {"type": "lifespan"}
        self.assertEqual(self.call(scope), ([scope], []))

    def test_a_websocket_from_elsewhere_is_closed_before_its_handshake(self):
        # None is served, and no websocket library is installed; this is for the day
        # one is, so the refusal is a 403 rather than an app that returned unanswered.
        seen, sent = self.call({"type": "websocket", "client": ("172.28.0.11", 1)})
        self.assertEqual((seen, sent), ([], [{"type": "websocket.close"}]))


class _Uvicorn:
    """A uvicorn that records what each server would serve, and where, and whether the
    store was there when it was made."""

    def __init__(self) -> None:
        self.configs: list[types.SimpleNamespace] = []
        self.store_ready: list[bool] = []
        recorder = self

        class Config:
            def __init__(self, app, host, port, **kwargs):
                self.app, self.host, self.port, self.kwargs = app, host, port, kwargs

        class Server:
            def __init__(self, config):
                recorder.store_ready.append(os.path.exists(taskdb.DB_PATH))
                self.config = config

            async def serve(self):
                recorder.configs.append(self.config)

        self.module = types.SimpleNamespace(Config=Config, Server=Server)


class EntryTests(unittest.TestCase):
    """``main``: the binds, the secret and the store, then each app on its own address."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dockade-serve-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.secret = os.path.join(self.dir, "tasks.json")
        Path(self.secret).write_text(json.dumps({"token": SECRET}))
        self.store = os.path.join(self.dir, "store", "tasks.db")
        self.uvicorn = _Uvicorn()
        for target, name, value in (
                (serve, "AGENT_BIND", AGENT), (serve, "AGENT_PORT", 8110),
                (serve, "OPERATOR_BIND", OPERATOR), (serve, "OPERATOR_PORT", 8111),
                (serve, "AGENT_NET", AGENT_NET), (serve, "OPERATOR_NET", OPERATOR_NET),
                (agent_api, "SECRET_FILE", self.secret), (taskdb, "DB_PATH", self.store)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(sys.modules, {"uvicorn": self.uvicorn.module})
        patch.start()
        self.addCleanup(patch.stop)

    def run_main(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asyncio.run(serve.main())
        return out.getvalue()

    def test_each_app_is_served_on_its_own_address_and_nowhere_else(self):
        self.run_main()
        served = {(c.host, c.port): c.app for c in self.uvicorn.configs}
        self.assertEqual(set(served), {(AGENT, 8110), (OPERATOR, 8111)})
        # Each answers its own network's peers only, whichever leg a packet came in on,
        # and the peer is the socket's: no proxy header may name another.
        for config in self.uvicorn.configs:
            self.assertIs(config.kwargs.get("proxy_headers"), False)
        self.assertIsInstance(served[(AGENT, 8110)], serve.OnlyFrom)
        self.assertIsInstance(served[(OPERATOR, 8111)], serve.OnlyFrom)
        self.assertEqual(str(served[(AGENT, 8110)].net), AGENT_NET)
        self.assertEqual(str(served[(OPERATOR, 8111)].net), OPERATOR_NET)
        agent_app, operator_app = served[(AGENT, 8110)].app, served[(OPERATOR, 8111)].app
        # No operator route on the agent leg, and nothing but the operator API on its
        # own: the operator's writes reach mcp-net through neither.
        self.assertEqual(sorted(agent_app.routes), [("GET", "/healthz"), ("POST", "/mcp")])
        self.assertIs(operator_app, operator_api.app)
        self.assertTrue(operator_app.routes)
        self.assertTrue(all(path.startswith("/api/") for _, path in operator_app.routes))

    def test_the_agent_listener_checks_the_secret_in_its_file(self):
        with mock.patch.object(agent_api, "build", wraps=agent_api.build) as build:
            self.run_main()
        build.assert_called_once_with(SECRET)

    def test_the_store_is_made_before_either_listener(self):
        self.assertFalse(os.path.exists(self.store))
        self.run_main()
        self.assertEqual(self.uvicorn.store_ready, [True, True])

    def test_a_bad_bind_opens_no_listener_and_no_store(self):
        with mock.patch.object(serve, "OPERATOR_BIND", AGENT), \
                self.assertRaises(SystemExit):
            self.run_main()
        self.assertEqual(self.uvicorn.store_ready, [])
        self.assertFalse(os.path.exists(self.store))

    def test_no_secret_opens_no_listener_and_no_store(self):
        for content in (None, "", json.dumps({"token": "short"})):
            with self.subTest(content=content):
                if content is None:
                    os.remove(self.secret)
                else:
                    Path(self.secret).write_text(content)
                with self.assertRaises(RuntimeError):
                    self.run_main()
                self.assertEqual(self.uvicorn.store_ready, [])
                self.assertFalse(os.path.exists(self.store))

    def test_it_says_where_each_listener_is(self):
        said = self.run_main()
        self.assertIn(f"agent listener on {AGENT}:8110", said)
        self.assertIn(f"operator listener on {OPERATOR}:8111", said)


if __name__ == "__main__":
    unittest.main()
