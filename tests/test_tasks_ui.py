# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the tasks UI's server (``tasks-ui/app.py``): its relay to the task
registry's operator listener, its own ``/running``, the files it serves, and that it
starts behind the shared guards (``ui-common/guards.py``, tested in
``tests/test_ui_guards.py``) under its own name, allowlist, address and network.

The registry's operator listener takes no token, so the relay's table is the whole of
what a page can do there; most of what follows holds that table to exactly the reads
the page makes. Stubs and fakes are ``tests/_ui.py``'s, as for the approval UI."""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import sys
import types
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock
from urllib.parse import urlsplit

from _ui import load_ui, ok_host, sent
from test_control_plane_ui import ui as approval_ui

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str = "dockade_tasks_ui") -> types.ModuleType:
    return load_ui("tasks-ui", name)


tui = _load()

#: The writes the relay passes, each as the page posts it.
WRITES = ("api/tasks/12/move", "api/tasks/12/break-claim", "api/tasks/12/notes")


class RelayTests(unittest.TestCase):
    """``/api/…`` goes to the registry's operator listener as it came, exactly the
    routes the page calls, and nothing else does."""

    def _proxy(self, captured_path, method="GET", **kw):
        sent.clear()
        return asyncio.run(tui.proxy(captured_path, ok_host(method=method, **kw)))

    def test_the_reads_reach_the_registry(self):
        for path in ("api/repos", "api/tasks", "api/tasks/12", "api/tasks/12/notes",
                     "api/tasks/12/history", "api/tasks/12/plans", "api/inbox",
                     "api/search"):
            with self.subTest(path=path):
                resp = self._proxy(path)
                self.assertNotEqual(resp.status_code, 403)
                self.assertEqual((sent["method"], sent["url"]),
                                 ("GET", f"{tui.REGISTRY_URL}/{path}"))

    def test_the_three_writes_reach_it_as_posts(self):
        for path in WRITES:
            with self.subTest(path=path):
                resp = self._proxy(path, "POST")
                self.assertNotEqual(resp.status_code, 403)
                self.assertEqual((sent["method"], sent["url"]),
                                 ("POST", f"{tui.REGISTRY_URL}/{path}"))

    def test_nothing_else_reaches_it(self):
        # Every other write, and the three by another method or with another id; an id
        # that is not digits, or too long; the old prefix; the control plane's paths;
        # the agent listener's; the pages FastAPI would add.
        for path, method in (("api/tasks", "POST"), ("api/tasks/12/edit", "POST"),
                             ("api/tasks/12/reparent", "POST"),
                             ("api/tasks/12/move", "GET"),
                             ("api/tasks/12/break-claim", "GET"),
                             ("api/tasks/abc/move", "POST"),
                             ("api/tasks/12/move/x", "POST"),
                             ("api/tasks/12345678901234567890/notes", "POST"),
                             ("api/notes/hide", "POST"), ("api/notes/handle", "POST"),
                             ("api/notes/3/handle", "POST"),
                             ("api/tasks/12/plans", "POST"), ("api/inbox", "POST"),
                             # The flagged list the inbox replaced.
                             ("api/notes/flagged", "GET"),
                             ("api/repos", "POST"), ("api/tasks/abc", "GET"),
                             ("api/tasks/1/../../../api/audit", "GET"),
                             ("api/tasks/12345678901234567890", "GET"),
                             ("tasks/api/repos", "GET"), ("api/audit", "GET"),
                             ("approvals/stream", "GET"), ("api/config", "GET"),
                             ("approvals/x/resolve", "POST"), ("mcp", "POST"),
                             ("openapi.json", "GET"), ("docs", "GET"),
                             ("running", "POST"), ("healthz", "POST")):
            with self.subTest(path=path, method=method):
                resp = self._proxy(path, method)
                self.assertEqual(resp.status_code, 403)
                self.assertTrue(resp.body.startswith("refused by tasks-ui: "))
                self.assertEqual(sent, {})

    def test_the_upstream_stays_the_registry_whatever_the_table_lets_through(self):
        # As the approval UI's RelayHostPinningTests: with the table opened up, a
        # network-path reference is still an absolute path on the registry.
        with mock.patch.object(tui, "_RELAY_ROUTES", (("GET", re.compile(r"^/.*$")),)):
            for path in ("/evil.com/x", "//evil.com", "/x:1/y"):
                with self.subTest(path=path):
                    self._proxy(path)
                    self.assertEqual(urlsplit(sent["url"]).netloc,
                                     urlsplit(tui.REGISTRY_URL).netloc)

    def test_the_only_claim_about_who_is_the_relays_own(self):
        # A client's actor is dropped and the peer this relay saw put in its place,
        # once, which the registry records as `via-ui=` (operator_api.actor).
        for path, method in (("api/repos", "GET"), ("api/tasks/12/move", "POST")):
            with self.subTest(method=method):
                self._proxy(path, method, peer="172.24.0.1",
                            headers={"x-forwarded-for": "10.9.9.9",
                                     "x-dockade-actor": "forged", "forwarded": "x",
                                     "connection": "close", "accept": "*/*"})
                headers = sent["headers"]
                self.assertEqual([v for k, v in headers if k == tui.ACTOR_HEADER],
                                 ["172.24.0.1"])
                names = {k for k, _ in headers}
                for name in ("x-forwarded-for", "forwarded", "connection", "host"):
                    self.assertNotIn(name, names)
                self.assertEqual(dict(headers)["accept"], "*/*")

    def test_a_cross_origin_write_is_refused_before_the_relay(self):
        # The shared guard (tests/test_ui_guards.py), in front of each write.
        guard = tui.guards.request_guard(tui.NAME, tui.ALLOWED_HOSTNAMES)

        async def relay(request, path):
            return await tui.proxy(path, request)

        for path in WRITES:
            for headers in ({"sec-fetch-site": "cross-site"},
                            {"sec-fetch-site": "same-site"},
                            {"origin": "http://127.0.0.1:3000"}):
                with self.subTest(path=path, headers=headers):
                    sent.clear()
                    request = ok_host(method="POST", headers=headers)
                    resp = asyncio.run(guard(request, lambda r, p=path: relay(r, p)))
                    self.assertEqual(resp.status_code, 403)
                    self.assertEqual(sent, {})
            with self.subTest(path=path, headers="same-origin"):
                request = ok_host(method="POST", headers={"sec-fetch-site": "same-origin"})
                resp = asyncio.run(guard(request, lambda r, p=path: relay(r, p)))
                self.assertNotEqual(resp.status_code, 403)
                self.assertEqual(sent["method"], "POST")

    def test_it_strips_what_the_approval_ui_strips(self):
        # One reason per header, given once in control-plane-ui/app.py.
        self.assertEqual(tui._STRIP_REQ, approval_ui._STRIP_REQ)

    def test_the_registrys_answer_carries_none_of_its_headers(self):
        class _Hostile:
            status_code = 200
            headers: ClassVar[dict] = {"set-cookie": "s=1", "location": "http://x/",
                                       "content-type": "text/html",
                                       "access-control-allow-origin": "*"}

            def aiter_raw(self):
                async def _gen():
                    if False:
                        yield b""
                return _gen()

            async def aclose(self):
                pass

        async def send(_req, stream=False):
            return _Hostile()

        with mock.patch.object(tui._client, "send", send):
            resp = self._proxy("api/repos")
        self.assertEqual(resp.kwargs["headers"], {"content-type": "application/json"})

    def _failing(self, path, method, error):
        async def _boom(_req, stream=False):
            raise getattr(sys.modules["httpx"], error)("boom")

        with mock.patch.object(tui._client, "send", _boom):
            return self._proxy(path, method)

    def test_a_registry_that_does_not_answer_is_a_502_in_its_own_shape(self):
        # The status the page reads as "not running", which the registry never sends:
        # its refusals are 400, 404, 409 and 422 (task-registry/operator_api.py).
        resp = self._failing("api/repos", "GET", "ConnectError")
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.args[0]["ok"], False)
        self.assertIsNone(resp.args[0]["current"])
        self.assertIn("task registry did not answer", resp.args[0]["detail"])

    def test_a_write_that_was_never_sent_is_a_502_too(self):
        # Nothing reached the registry, so the page may say it is not running.
        for error in ("ConnectError", "ConnectTimeout", "PoolTimeout"):
            for path in WRITES:
                with self.subTest(error=error, path=path):
                    resp = self._failing(path, "POST", error)
                    self.assertEqual(resp.status_code, 502)
                    self.assertIn(f"({error})", resp.args[0]["detail"])

    def test_a_write_sent_and_not_answered_is_a_504_in_its_own_shape(self):
        # It may have landed: a status nothing else sends, so the page reads the task
        # again rather than offer to send it again. The base class too, and any other
        # this relay does not name, is taken as sent.
        for error in ("ReadTimeout", "WriteTimeout", "ReadError", "WriteError",
                      "RemoteProtocolError", "RequestError"):
            for path in WRITES:
                with self.subTest(error=error, path=path):
                    resp = self._failing(path, "POST", error)
                    self.assertEqual(resp.status_code, 504)
                    body = resp.args[0]
                    self.assertEqual((body["ok"], body["current"]), (False, None))
                    self.assertIn("may have landed", body["detail"])
                    self.assertIn(f"({error})", body["detail"])

    def test_a_read_with_no_answer_is_not_running_whatever_the_error(self):
        # A read changes nothing, so asking again is always safe.
        for error in ("ConnectError", "ReadTimeout", "RemoteProtocolError",
                      "RequestError"):
            with self.subTest(error=error):
                self.assertEqual(self._failing("api/tasks/12", "GET", error).status_code,
                                 502)

    def test_the_not_sent_list_is_these_three_and_each_is_a_request_error(self):
        httpx = sys.modules["httpx"]
        self.assertEqual(tui._NOT_SENT,
                         (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        for error in tui._NOT_SENT:
            self.assertTrue(issubclass(error, httpx.RequestError))
        # Neither status is one the registry sends (task-registry/operator_api.py).
        api = (ROOT / "task-registry" / "operator_api.py").read_text()
        for status in ("502", "504"):
            self.assertNotIn(status, api)

    def test_the_registry_is_dialled_by_address_and_with_a_bounded_wait(self):
        # By name, a stopped registry's `tasks` would be asked of the host's DNS.
        host = urlsplit(tui.REGISTRY_URL).hostname
        self.assertEqual(str(ipaddress.ip_address(host)), host)
        src = (ROOT / "tasks-ui" / "app.py").read_text()
        self.assertRegex(src, r"_client = httpx\.AsyncClient\(base_url=REGISTRY_URL,"
                              r"\s+timeout=httpx\.Timeout\(10\.0, connect=2\.0\)\)")


class RunningTests(unittest.TestCase):
    """``GET /running``, which this container answers itself: whether the registry
    answers, so the page asks it before calling the registry at all."""

    def _ask(self, get):
        with mock.patch.object(tui._client, "get", get, create=True):
            return asyncio.run(tui.running())

    def test_running_when_its_repo_list_answers(self):
        asked = []

        async def get(path):
            asked.append(path)
            return types.SimpleNamespace(status_code=200)

        self.assertEqual(self._ask(get), {"running": True})
        self.assertEqual(asked, ["/api/repos"])

    def test_not_running_when_nothing_answers(self):
        async def get(path):
            raise sys.modules["httpx"].RequestError("timed out")

        self.assertEqual(self._ask(get), {"running": False})

    def test_not_running_when_it_answers_anything_but_the_list(self):
        # A 403 is the registry's peer check refusing this UI's address.
        for status in (403, 404, 500):
            async def get(path, status=status):
                return types.SimpleNamespace(status_code=status)

            with self.subTest(status=status):
                self.assertEqual(self._ask(get), {"running": False})

    def test_it_is_served_here_before_the_relay_route(self):
        # Declared above the catch-all, which would otherwise take it and refuse it.
        src = (ROOT / "tasks-ui" / "app.py").read_text()
        self.assertLess(src.index('@app.get("/running")'),
                        src.index('@app.api_route("/{path:path}"'))


class StaticFileTests(unittest.TestCase):
    """The page and its modules: only the listed ones, revalidated on every load, for
    the reasons the approval UI's StaticRevalidationTests give."""

    def test_every_module_is_revalidated_and_read_from_its_listed_path(self):
        for name in sorted(tui.UI_MODULES):
            with self.subTest(module=name):
                resp = tui.script(name.removesuffix(".js"))
                self.assertEqual(resp.headers["cache-control"], "no-cache")
                self.assertEqual(resp.args[0], f"{tui.UI_DIR}/{name}")
        self.assertEqual(tui.index().headers["cache-control"], "no-cache")

    def test_a_module_nobody_listed_is_not_served(self):
        for name in ("evil", "..", "app.js", "requirements", "guards", "payload"):
            with self.subTest(name=name):
                self.assertEqual(tui.script(name).status_code, 404)


class GuardWiringTests(unittest.TestCase):
    """The shared guards, put on this app at import under its name and allowlist."""

    def test_they_are_installed_on_this_app_once(self):
        calls = []
        with mock.patch.object(tui.guards, "install", lambda *a: calls.append(a)):
            fresh = _load("dockade_tasks_ui_wiring")
        self.assertEqual(len(calls), 1)
        app, name, allowed = calls[0]
        self.assertIs(app, fresh.app)
        self.assertEqual(name, "tasks-ui")
        self.assertIs(allowed, fresh.ALLOWED_HOSTNAMES)

    def test_the_allowlist_is_loopback_unless_its_variable_says_otherwise(self):
        self.assertEqual(tui.ALLOWED_HOSTNAMES, {"127.0.0.1", "localhost", "::1"})
        with mock.patch.dict(os.environ, {"TASKS_UI_ALLOWED_HOSTS": "tasks.example"}):
            fresh = _load("dockade_tasks_ui_hosts")
        self.assertEqual(fresh.ALLOWED_HOSTNAMES, {"tasks.example"})

    def test_an_empty_allowlist_stops_the_import(self):
        with mock.patch.dict(os.environ, {"TASKS_UI_ALLOWED_HOSTS": " , "}), \
                self.assertRaises(RuntimeError) as caught:
            _load("dockade_tasks_ui_empty")
        self.assertIn("TASKS_UI_ALLOWED_HOSTS is empty", str(caught.exception))


UI_NET, UI_BIND = "172.24.0.0/24", "172.24.0.2"


class EntryTests(unittest.TestCase):
    """``main``: the bind is checked before uvicorn runs, and the app it runs is this
    one, on its tasks-ui-net address alone, inside the peer check."""

    def setUp(self):
        self.calls: list[tuple[tuple, dict]] = []

    def run_main(self, bind: str) -> None:
        uvicorn = types.ModuleType("uvicorn")
        uvicorn.run = lambda *a, **k: self.calls.append((a, k))
        with mock.patch.dict(sys.modules, {"uvicorn": uvicorn}), \
                mock.patch.object(tui, "BIND", bind), mock.patch.object(tui, "PORT", 18091):
            tui.main()

    def test_it_serves_this_app_on_the_bound_address_alone(self):
        self.run_main(UI_BIND)
        self.assertEqual(len(self.calls), 1)
        (served,), kwargs = self.calls[0]
        self.assertEqual(kwargs, {"host": UI_BIND, "port": 18091,
                                  "proxy_headers": False, "log_level": "info"})
        self.assertIsInstance(served, tui.guards.OnlyFrom)
        self.assertIs(served.app, tui.app)
        self.assertEqual(str(served.net), UI_NET)
        self.assertIn(b"refused by tasks-ui: ", served._refusal)

    def test_anything_but_an_address_on_tasks_ui_net_starts_nothing(self):
        # As the approval UI's list, then this UI's operator leg, where the registry is
        # its neighbour, and the approval UI's addresses.
        for bind in ("", "0.0.0.0", "::", "*", "tasks-ui", "localhost",  # noqa: S104
                     "127.0.0.1", "::1", "::ffff:172.24.0.2", "172.26.0.4",
                     "172.25.0.2", "172.31.0.10"):
            with self.subTest(bind=bind), self.assertRaises(SystemExit) as caught:
                self.run_main(bind)
            message = str(caught.exception)
            self.assertTrue(message.startswith(f"tasks-ui: TASKS_UI_BIND={bind!r} "),
                            message)
            self.assertIn("parses agent text", message)
        self.assertEqual(self.calls, [])

    def test_a_network_that_does_not_parse_starts_nothing(self):
        with mock.patch.object(tui, "UI_NET", "tasks-ui-net"), \
                self.assertRaises(SystemExit) as caught:
            self.run_main(UI_BIND)
        self.assertIn("tasks-ui: TASKS_UI_NET='tasks-ui-net'", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_the_defaults_are_its_network_and_the_port_the_image_exposes(self):
        self.assertEqual(tui.UI_NET, UI_NET)
        self.assertEqual(tui.PORT, 8091)


if __name__ == "__main__":
    unittest.main()
