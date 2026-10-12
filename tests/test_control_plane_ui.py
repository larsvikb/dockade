# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the control-plane-ui frontend (``control-plane-ui/app.py``).

Two groups, both regression suites for real defects:

**Relay host pinning.** The frontend reverse-proxies to the backend. A request like
``//evil.com/x`` is captured by ``{path:path}`` as ``/evil.com/x``; the old ``"/" +
path`` turned that back into the network-path reference ``//evil.com/x``, which
httpx resolves against the base URL as a DIFFERENT authority (``http://evil.com/x``)
— a caller-controlled host override / SSRF. The fix collapses leading slashes so the
upstream host always stays the backend.

**Browser-facing guards.** This app publishes the only API that can grant egress
(``POST /approvals/{id}/resolve`` is self-approval if reached), on host loopback,
with no auth. The Host allowlist (against DNS rebinding), the refusals of a
cross-origin state change and of being embedded, and the security headers are
``ui-common/guards.py``'s, tested in ``tests/test_ui_guards.py``. Here: that this app
installs them under its own name and allowlist, relays only an allowlist of backend
paths (so ``POST /authorize`` is unreachable from a page), and replaces
client-supplied provenance headers with the peer address it actually observed.

Dependency-free: like ``tests/_loader.py`` we stub the third-party imports
(``httpx``, the ``fastapi``/``starlette`` bits) before loading the module by path.
The httpx ``build_request`` stub resolves the passed path against the base URL with
stdlib ``urllib.parse.urljoin`` — which follows the SAME RFC-3986 rules httpx does
for network-path references — and records the resulting authority plus headers, so
the tests assert real end-to-end outcomes rather than string shapes."""
from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import os
import re
import sys
import types
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]

_BASE = "http://control-plane:8090"
_sent: dict = {}


def _install_stubs() -> None:
    # httpx: an AsyncClient whose build_request resolves the path against base_url
    # (RFC-3986, via urljoin) so we can assert the effective upstream authority, and
    # records the outgoing headers so we can assert provenance handling.
    if "httpx" not in sys.modules:
        httpx = types.ModuleType("httpx")

        class _Resp:
            status_code = 200
            headers: ClassVar[dict] = {}

            def aiter_raw(self):
                async def _gen():
                    if False:
                        yield b""
                return _gen()

            async def aclose(self):
                pass

        class AsyncClient:
            def __init__(self, *args, base_url="", **kwargs):
                self._base = str(base_url)

            def build_request(self, method, url, **kwargs):
                _sent["url"] = urljoin(self._base, url)
                _sent["method"] = method
                _sent["headers"] = list(kwargs.get("headers") or [])
                return object()

            async def send(self, req, stream=False):
                return _Resp()

        class RequestError(Exception):
            """Stand-in for the base of httpx's transport errors — what the relay
            catches to answer 502 when the backend is unreachable."""

        httpx.AsyncClient = AsyncClient
        httpx.RequestError = RequestError
        sys.modules["httpx"] = httpx

    # Augment rather than assume ownership: discovery order is arbitrary, so another
    # module may have installed an httpx stub of its own first.
    if not hasattr(sys.modules["httpx"], "RequestError"):
        sys.modules["httpx"].RequestError = type("RequestError", (Exception,), {})
    if not hasattr(sys.modules["httpx"], "Timeout"):
        sys.modules["httpx"].Timeout = lambda *a, **k: (a, k)

    # Another test module may have installed the shared fastapi stub first (test
    # discovery order is arbitrary), so augment whatever exists rather than assume
    # a clean slate: ensure FastAPI.api_route/.middleware and the responses the UI
    # imports. Response stubs expose .status_code so guard refusals are assertable.
    def _decorator(*_a, **_k):
        return lambda fn: fn

    class _R:
        def __init__(self, *a, **k):
            self.args, self.kwargs = a, k
            self.status_code = k.get("status_code", 200)
            self.body = a[0] if a else None
            # A dict stands in for Starlette's MutableHeaders — `security_headers`
            # writes onto every response it wraps, refusals included.
            self.headers = dict(k.get("headers") or {})

    if "fastapi" not in sys.modules:
        fa = types.ModuleType("fastapi")

        class FastAPI:
            def __init__(self, *a, **k):
                pass

            get = staticmethod(_decorator)
            post = staticmethod(_decorator)
            api_route = staticmethod(_decorator)
            middleware = staticmethod(_decorator)

        class Request:
            pass

        fa.FastAPI = FastAPI
        fa.Request = Request
        responses = types.ModuleType("fastapi.responses")
        responses.StreamingResponse = _R
        fa.responses = responses
        sys.modules["fastapi"] = fa
        sys.modules["fastapi.responses"] = responses

    fa = sys.modules["fastapi"]
    for meth in ("get", "post", "api_route", "middleware"):
        if not hasattr(fa.FastAPI, meth):
            setattr(fa.FastAPI, meth, staticmethod(_decorator))
    responses = sys.modules["fastapi.responses"]
    # `Response` is the BASE class the relay handler is annotated with (a union
    # annotation makes real FastAPI raise at import — see the note on `proxy`), so the
    # stub must expose it too or the module under test cannot be imported at all.
    for name in ("FileResponse", "StreamingResponse", "PlainTextResponse",
                 "JSONResponse", "Response"):
        if not hasattr(responses, name):
            setattr(responses, name, _R)

    if "starlette.background" not in sys.modules:
        starlette = sys.modules.setdefault("starlette", types.ModuleType("starlette"))
        bg = types.ModuleType("starlette.background")
        bg.BackgroundTask = lambda *a, **k: None
        starlette.background = bg
        sys.modules["starlette.background"] = bg


def _load_ui(name: str = "dockade_control_plane_ui") -> types.ModuleType:
    _install_stubs()
    # The image copies ui-common/'s modules beside app.py, which imports them by plain
    # name; here they are found on the path instead.
    if str(ROOT / "ui-common") not in sys.path:
        sys.path.insert(0, str(ROOT / "ui-common"))
    path = ROOT / "control-plane-ui" / "app.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ui = _load_ui()

# Permissive stand-in for the relay allowlist, used only by the host-pinning tests
# below: they must keep asserting that the UPSTREAM PINNING holds on its own, not
# because the allowlist happens to reject the hostile paths first.
_ALLOW_ANYTHING = (("GET", re.compile(r"^/.*$")), ("POST", re.compile(r"^/.*$")))


class _FakeRequest:
    """Minimal stand-in for a Starlette Request. Headers are lowercased on the way
    in, matching Starlette's case-insensitive mapping, since the app reads them in
    lowercase."""

    def __init__(self, method="GET", headers=None, peer="127.0.0.1"):
        self.method = method
        self.query_params = {}
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.headers = types.SimpleNamespace(
            get=self._headers.get, items=lambda: list(self._headers.items()))
        self.client = types.SimpleNamespace(host=peer) if peer else None

    async def body(self) -> bytes:
        return b""


def _ok_host(**kw):
    """A request that already satisfies the Host guard, for tests about other guards."""
    headers = {"host": "127.0.0.1:28090", **kw.pop("headers", {})}
    return _FakeRequest(headers=headers, **kw)


async def _passthrough(_request):
    return "REACHED-THE-APP"


class RelayAllowlistTests(unittest.TestCase):
    """Only the paths this UI actually uses are relayed — above all NOT
    ``POST /authorize``, reaching which means forged audit rows and consumed hold
    slots on the governance authority."""

    def _proxy(self, captured_path, method="GET"):
        _sent.clear()
        return asyncio.run(ui.proxy(captured_path, _ok_host(method=method)))

    def test_ui_paths_are_relayed(self):
        for path, method in (("approvals/stream", "GET"),
                             ("api/audit", "GET"), ("api/audit/events", "GET"),
                             ("api/egress/rules", "GET"),
                             ("api/config", "GET"),
                             ("approvals/0123abcd/resolve", "POST"),
                             # The two writes to standing policy. Both grant-adjacent:
                             # this one GRANTS outright (a standing allow decides future
                             # requests with no hold and no click) and the other takes a
                             # grant back.
                             ("api/egress/rules", "POST"),
                             ("api/egress/rules/12/revoke", "POST"),
                             # The timed grants. Read, and taken back — see below for
                             # the collection POST that is deliberately absent.
                             ("api/egress/leases", "GET"),
                             ("api/egress/leases/12/revoke", "POST"),
                             # MCP servers. None of these grants — a registration lands
                             # disabled and an enabled server with no rules denies every
                             # call — which is what separates them from the egress
                             # writes above.
                             ("api/mcp/servers", "GET"),
                             ("api/mcp/servers", "POST"),
                             ("api/mcp/servers/mcp-github/edit", "POST"),
                             ("api/mcp/servers/mcp-github/revoke", "POST"),
                             # Tool policy. The inventory is read-only and decides
                             # nothing; the rule writes do decide, and `POST
                             # api/mcp/rules` with an `allow` is the widest grant on
                             # this list by consequence — the agent calls that tool
                             # with no hold and no click.
                             ("api/mcp/inventory", "GET"),
                             ("api/mcp/rules", "GET"),
                             ("api/mcp/rules", "POST"),
                             ("api/mcp/rules/12/edit", "POST"),
                             ("api/mcp/rules/12/revoke", "POST"),
                             # Pins: read, and taken back, which only narrows.
                             ("api/mcp/pins", "GET"),
                             ("api/mcp/pins/12/revoke", "POST")):
            self._proxy(path, method)
            self.assertEqual(urlsplit(_sent["url"]).netloc, "control-plane:8090",
                             f"{method} {path} must relay")

    def test_authorize_is_not_relayed(self):
        # The proxy's decision endpoint: no browser has any business reaching it.
        resp = self._proxy("authorize", "POST")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.body, "refused by control-plane-ui: POST /authorize is not a "
                                    "relayed path\n")
        self.assertEqual(_sent, {}, "must not reach the backend at all")

    def test_unknown_and_wrong_method_paths_are_not_relayed(self):
        for path, method in (("healthz", "GET"), ("api/secrets", "GET"),
                             ("approvals", "POST"), ("status", "POST"),
                             # Dropped from the allowlist once nothing called them.
                             # `GET /approvals` matters most: it carries the pending
                             # hosts, clients and URLs, so an uncalled route was
                             # relaying real data. Both still serve on control-net.
                             ("approvals", "GET"), ("status", "GET"),
                             ("approvals/x/resolve", "GET"),
                             ("approvals/a/b/resolve", "POST"),
                             # Config is read-only — the UI reads settings, it does
                             # not set them.
                             ("api/config", "POST"),
                             # The rules COLLECTION now takes a POST, and these are the
                             # neighbouring shapes that must not have come with it: the
                             # entry is anchored, so nothing under the collection is
                             # relayed except the one revoke path spelled out below.
                             ("api/egress/rules/", "POST"),
                             ("api/egress/rules/12", "POST"),
                             # The leases COLLECTION takes no POST, and that absence is
                             # a decision rather than an oversight: a lease is only ever
                             # created by resolving a card, because a timed grant with no
                             # request behind it is just a standing rule somebody will
                             # forget they wrote. The same anchoring and the same
                             # id bound as the rules paths, asserted the same way.
                             ("api/egress/leases", "POST"),
                             ("api/egress/leases/", "POST"),
                             ("api/egress/leases/12", "POST"),
                             ("api/egress/leases/../revoke", "POST"),
                             ("api/egress/leases/1.2/revoke", "POST"),
                             ("api/egress/leases/abc/revoke", "POST"),
                             ("api/egress/leases//revoke", "POST"),
                             ("api/egress/leases/12/revoke", "GET"),
                             # The audit views are reads, and each is its OWN entry:
                             # anchoring `^/api/audit` without the `$` would relay
                             # every future path under that prefix sight unseen,
                             # which is how a default-deny allowlist stops being one.
                             ("api/audit", "POST"), ("api/audit/events", "POST"),
                             ("api/audit/events/x", "GET"),
                             ("api/audit/anything", "GET"),
                             # The revoke route's id segment is bounded to DIGITS,
                             # and that bound is load-bearing rather than tidy: the
                             # segment lands in a URL path, so a looser class admits
                             # dot-segments, which httpx resolves upstream into a
                             # different path than the one the allowlist approved.
                             # Widening it to `[^/]+` passes every other test here.
                             ("api/egress/rules/../revoke", "POST"),
                             ("api/egress/rules/1.2/revoke", "POST"),
                             ("api/egress/rules/abc/revoke", "POST"),
                             ("api/egress/rules//revoke", "POST"),
                             ("api/egress/rules/12/revoke", "GET"),
                             # The PRE-RENAME paths, asserted gone rather than left
                             # to rot. A rename that adds the new entry and forgets
                             # to drop the old one relays both, and every other test
                             # here passes: the UI calls the new path and never
                             # notices the old one still works. See "URLs carry the
                             # surface" in control-plane/DESIGN.md.
                             ("api/rules", "GET"),
                             ("api/rules/12/revoke", "POST"),
                             # The tool-rule paths carry the same digit bound as the
                             # egress ones, for the same traversal reason, and it is
                             # asserted separately because the two entries are separate
                             # regexes — a loosened copy of a bounded pattern is exactly
                             # the drift a shared assertion would hide.
                             ("api/mcp/rules/../revoke", "POST"),
                             ("api/mcp/rules/1.2/edit", "POST"),
                             ("api/mcp/rules/abc/edit", "POST"),
                             ("api/mcp/rules//revoke", "POST"),
                             ("api/mcp/rules/12/revoke", "GET"),
                             ("api/mcp/rules/12", "POST"),
                             # The inventory is a READ of third-party text. A write to
                             # it would be a browser pushing a server's surface into the
                             # control plane, which only the gateway may do.
                             ("api/mcp/inventory", "POST"),
                             # No collection POST: a pin is written by resolving a
                             # card, never from a form. The id is digit-bounded as the
                             # rule ids are.
                             ("api/mcp/pins", "POST"),
                             ("api/mcp/pins/../revoke", "POST"),
                             ("api/mcp/pins/abc/revoke", "POST"),
                             ("api/mcp/pins/12/revoke", "GET"),
                             ("api/mcp/pins/12", "POST")):
            resp = self._proxy(path, method)
            self.assertEqual(getattr(resp, "status_code", None), 403,
                             f"{method} {path} must be refused")


class ProvenanceHeaderTests(unittest.TestCase):
    """The backend records ``ACTOR_HEADER`` as this relay's assertion about the
    browser, so a client must not be able to set it — otherwise the audit record
    would faithfully repeat a forged actor."""

    def _relay_headers(self, request):
        _sent.clear()
        asyncio.run(ui.proxy("api/egress/rules", request))
        return _sent["headers"]

    def test_actor_header_is_set_from_the_observed_peer(self):
        headers = self._relay_headers(_ok_host(peer="10.9.9.9"))
        self.assertIn((ui.ACTOR_HEADER, "10.9.9.9"), headers)

    def test_client_supplied_actor_and_forwarding_headers_are_stripped(self):
        headers = self._relay_headers(_ok_host(peer="10.9.9.9", headers={
            ui.ACTOR_HEADER: "the-operator-honest-really",
            "x-forwarded-for": "1.2.3.4",
            "x-real-ip": "1.2.3.4",
            "forwarded": "for=1.2.3.4",
        }))
        names = [k.lower() for k, _ in headers]
        for spoofable in ("x-forwarded-for", "x-real-ip", "forwarded"):
            self.assertNotIn(spoofable, names)
        # Exactly one actor header, carrying the peer rather than the claim.
        actors = [v for k, v in headers if k.lower() == ui.ACTOR_HEADER]
        self.assertEqual(actors, ["10.9.9.9"])

    def test_host_header_is_not_forwarded(self):
        # httpx must set Host for the backend, or the upstream sees the UI's.
        names = [k.lower() for k, _ in self._relay_headers(_ok_host())]
        self.assertNotIn("host", names)

    def test_framing_headers_are_not_forwarded(self):
        """This relay READS the body and re-sends it, so it owns the framing: httpx
        sets `content-length` from the content it is handed. Forwarding the caller's
        value, or a `transfer-encoding: chunked` alongside a fixed-length re-send,
        is how a request desyncs from its length — request smuggling rather than an
        untidy header.

        `expect: 100-continue` is stripped for a different reason: the backend would
        wait for an interim response this relay never sends."""
        headers = self._relay_headers(_ok_host(headers={
            "content-length": "9999",
            "transfer-encoding": "chunked",
            "expect": "100-continue",
        }))
        names = [k.lower() for k, _ in headers]
        for framing in ("content-length", "transfer-encoding", "expect"):
            self.assertNotIn(framing, names, framing)

    def test_hop_by_hop_headers_are_not_forwarded(self):
        # They describe the connection they arrived on, not the message, so copying
        # them onto a different connection is a protocol error. Applied to requests
        # as well as responses — it used to be responses only.
        headers = self._relay_headers(_ok_host(headers={
            "connection": "keep-alive", "keep-alive": "timeout=5",
            "upgrade": "websocket", "te": "trailers",
            "proxy-authorization": "Basic Zm9v",
        }))
        names = [k.lower() for k, _ in headers]
        for hop in ("connection", "keep-alive", "upgrade", "te",
                    "proxy-authorization"):
            self.assertNotIn(hop, names, hop)

    def test_the_two_directions_strip_the_same_hop_by_hop_set(self):
        # One list, applied both ways. They were separate literals, and the request
        # side simply did not have this class of header in it at all.
        self.assertLessEqual(ui._HOP_BY_HOP, ui._STRIP_REQ)
        self.assertLessEqual(ui._HOP_BY_HOP, ui._STRIP_RESP)

    def test_ordinary_headers_still_reach_the_backend(self):
        # The stripping must be a list, not a filter that eats everything: a relay
        # that forwarded no headers would pass every test above.
        headers = self._relay_headers(_ok_host(headers={
            "accept": "application/json", "if-none-match": '"abc"',
        }))
        names = [k.lower() for k, _ in headers]
        self.assertIn("accept", names)
        self.assertIn("if-none-match", names)


class StaticRevalidationTests(unittest.TestCase):
    """The page must not run a version of its own control logic older than the one
    the container is serving.

    `FileResponse` sends validators and no `Cache-Control`, which lets a browser apply
    HEURISTIC freshness and skip the revalidation entirely. The failure that follows is
    not an error but a MISRENDER, and it was observed rather than imagined: a page
    cached from before the tool surface drew every tool ask through the egress card
    builder — a literal "undefined" where the host would be, under the four egress
    action buttons, which the backend refuses with a 400 because the action sets are
    per-surface. A real pending decision, on screen, that cannot be actioned."""

    def test_the_page_script_is_revalidated_on_every_load(self):
        for name in sorted(ui.UI_MODULES):
            with self.subTest(module=name):
                self.assertEqual(ui.script(name.removesuffix(".js"))
                                 .headers["cache-control"], "no-cache")

    def test_a_module_nobody_listed_is_not_served(self):
        # The name off the URL only picks an entry in `UI_MODULES`, so the route
        # cannot be used to read anything else in the directory — `..` included, which
        # the route pattern lets through as a name.
        for name in ("evil", "..", "app.js", "requirements"):
            with self.subTest(name=name):
                self.assertEqual(ui.script(name).status_code, 404)

    def test_a_served_module_is_read_from_the_path_behind_its_name(self):
        # The path is built from the list at import, never from the request: nothing
        # a caller sent is joined into it, which is what makes the point above true
        # by construction rather than by a check a later edit could move.
        for name in sorted(ui.UI_MODULES):
            with self.subTest(module=name):
                self.assertEqual(ui.script(name.removesuffix(".js")).args[0],
                                 f"{ui.UI_DIR}/{name}")

    def test_the_document_is_revalidated_too(self):
        # The markup carries the stylesheet and the element ids the script binds to,
        # so a stale document against a fresh script is the same failure mirrored.
        self.assertEqual(ui.index().headers["cache-control"], "no-cache")

    def test_it_revalidates_rather_than_refusing_to_store(self):
        # `no-store` would refetch both files in full on every load and every
        # reconnect. The validators are already being sent, so `no-cache` gets the same
        # guarantee for one conditional request and a 304.
        for headers in (ui.script("app").headers, ui.index().headers):
            self.assertNotIn("no-store", headers["cache-control"])

    def test_the_security_middleware_does_not_drop_it(self):
        # `security_headers` wraps every response and assigns its own names onto it.
        # If it ever assigned a whole header mapping instead, this would go silently —
        # and silence is exactly the failure mode being closed here.
        async def _next(_request):
            return ui.script("app")

        resp = asyncio.run(ui.guards.security_headers(_ok_host(), _next))
        self.assertEqual(resp.headers["cache-control"], "no-cache")
        self.assertIn("content-security-policy", resp.headers)

    def test_relayed_responses_are_left_alone(self):
        # Not a security header and deliberately not in `SECURITY_HEADERS`: it answers
        # a staleness problem the two STATIC files have, because they are the only
        # responses here carrying validators with no policy beside them. Putting it on
        # every relayed response would change caching for the API and the SSE stream to
        # fix something neither of them has.
        self.assertNotIn("cache-control", ui.guards.SECURITY_HEADERS)


class BackendUnreachableTests(unittest.TestCase):
    """A backend that is down/restarting must produce a legible 502, not a 500.

    Load-bearing for the SSE feed specifically: an EventSource treats ANY non-200 as a
    PERMANENT close (readyState CLOSED, no built-in retry), so this path is what the
    page's hand-rolled reconnect reconnects from. Before it existed the exception
    became a 500 and the page sat blind on "reconnecting…" until a manual reload."""

    def _proxy_with_dead_backend(self, path="api/egress/rules", method="GET"):
        async def _boom(_req, stream=False):
            raise sys.modules["httpx"].RequestError("connection refused")

        _sent.clear()
        with mock.patch.object(ui._client, "send", _boom):
            return asyncio.run(ui.proxy(path, _ok_host(method=method)))

    def test_transport_error_becomes_502(self):
        resp = self._proxy_with_dead_backend()
        self.assertEqual(resp.status_code, 502)

    def test_stream_path_also_becomes_502(self):
        # The case that matters: this is the endpoint EventSource is attached to.
        resp = self._proxy_with_dead_backend("approvals/stream")
        self.assertEqual(resp.status_code, 502)

    def test_the_guards_still_run_first(self):
        # A dead backend must not turn a refusal into a 502 — the refusal happens
        # before any upstream call is attempted.
        resp = self._proxy_with_dead_backend("authorize", "POST")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(_sent, {}, "must not reach the backend at all")


class RelayHostPinningTests(unittest.TestCase):
    """The upstream authority must stay the backend. Kept separate from the relay
    allowlist ON PURPOSE: the allowlist would reject these paths first today, so
    these tests neutralise it to prove the PINNING holds on its own — otherwise a
    later widening of the allowlist would silently re-expose the SSRF."""

    def _upstream_host(self, captured_path: str) -> str:
        _sent.clear()
        with mock.patch.object(ui, "_RELAY_ROUTES", _ALLOW_ANYTHING):
            asyncio.run(ui.proxy(captured_path, _ok_host()))
        return urlsplit(_sent["url"]).netloc

    def test_normal_paths_reach_the_backend(self):
        # {path:path} captures these WITHOUT a leading slash.
        for p in ("api/egress/rules", "approvals/stream", "api/audit"):
            self.assertEqual(self._upstream_host(p), "control-plane:8090",
                             f"normal path {p!r} must relay to the backend")

    def test_network_path_reference_cannot_override_the_host(self):
        # {path:path} RETAINS one leading slash for a '//host' request, which is the
        # SSRF vector. The upstream authority must stay the backend regardless.
        for p in ("/evil.com/x", "/evil.com", "/attacker:1234/steal"):
            self.assertEqual(
                self._upstream_host(p), "control-plane:8090",
                f"captured path {p!r} must NOT override the upstream host")

    def test_network_path_reference_is_also_refused_by_the_allowlist(self):
        # Belt and braces: with the real allowlist in place it never gets that far.
        _sent.clear()
        resp = asyncio.run(ui.proxy("/evil.com/x", _ok_host()))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(_sent, {})


class TasksRelayTests(unittest.TestCase):
    """The Tasks tab's relay: ``/tasks/api/…`` goes to the task registry's operator
    listener as ``/api/…``, exactly the routes the page calls, and nothing else does.
    That listener takes no token, so this table is the whole of what a browser on the
    loopback port can do there."""

    def _proxy(self, captured_path, method="GET", **kw):
        _sent.clear()
        return asyncio.run(ui.proxy(captured_path, _ok_host(method=method, **kw)))

    def test_the_reads_reach_the_registry_with_the_prefix_taken_off(self):
        for path, upstream in (("tasks/api/repos", "/api/repos"),
                               ("tasks/api/tasks", "/api/tasks"),
                               ("tasks/api/tasks/12", "/api/tasks/12"),
                               ("tasks/api/tasks/12/notes", "/api/tasks/12/notes"),
                               ("tasks/api/tasks/12/history", "/api/tasks/12/history"),
                               ("tasks/api/notes/flagged", "/api/notes/flagged"),
                               ("tasks/api/search", "/api/search")):
            with self.subTest(path=path):
                resp = self._proxy(path)
                self.assertNotEqual(resp.status_code, 403)
                self.assertEqual(_sent["url"], ui.TASKS_URL + upstream)

    def test_nothing_else_reaches_it(self):
        # No write yet; an id that is not digits; a control-plane path under the
        # prefix, or a registry path without it; the agent listener's paths, and the
        # pages FastAPI would add.
        for path, method in (("tasks/api/tasks", "POST"),
                             ("tasks/api/tasks/12/move", "POST"),
                             ("tasks/api/tasks/12/break-claim", "POST"),
                             ("tasks/api/tasks/12/notes", "POST"),
                             ("tasks/api/notes/hide", "POST"),
                             ("tasks/api/repos", "POST"),
                             ("tasks/api/tasks/abc", "GET"),
                             ("tasks/api/tasks/1/../../../api/audit", "GET"),
                             ("tasks/api/tasks/12345678901234567890", "GET"),
                             ("tasks/api/audit", "GET"),
                             ("tasks/approvals/stream", "GET"),
                             ("tasks/api/config", "GET"),
                             ("api/repos", "GET"), ("api/tasks", "GET"),
                             ("api/notes/flagged", "GET"),
                             ("tasks/mcp", "POST"), ("tasks/healthz", "GET"),
                             ("tasks/openapi.json", "GET"), ("tasks/docs", "GET"),
                             ("tasks/running", "POST")):
            with self.subTest(path=path, method=method):
                resp = self._proxy(path, method)
                self.assertEqual(resp.status_code, 403)
                self.assertEqual(_sent, {})

    def test_no_path_belongs_to_both_tables(self):
        # The prefix is what keeps each path to one upstream.
        for _method, rx in ui._TASKS_RELAY_ROUTES:
            with self.subTest(pattern=rx.pattern):
                self.assertTrue(rx.pattern.startswith("^/tasks/api/"))
        for _method, rx in ui._RELAY_ROUTES:
            with self.subTest(pattern=rx.pattern):
                self.assertFalse(rx.pattern.startswith("^/tasks"))

    def test_the_upstream_stays_the_registry_whatever_the_table_lets_through(self):
        # As RelayHostPinningTests, with this table opened up: the prefix comes off and
        # what is left is still an absolute path on the registry.
        with mock.patch.object(ui, "_TASKS_RELAY_ROUTES",
                               (("GET", re.compile(r"^/tasks/.*$")),)):
            for path in ("tasks//evil.com/x", "tasks///evil.com", "/tasks//x:1/y"):
                with self.subTest(path=path):
                    self._proxy(path)
                    self.assertEqual(urlsplit(_sent["url"]).netloc,
                                     urlsplit(ui.TASKS_URL).netloc)

    def test_client_provenance_headers_are_stripped_here_too(self):
        self._proxy("tasks/api/repos", headers={"x-forwarded-for": "10.9.9.9",
                                                ui.ACTOR_HEADER: "forged"})
        sent = dict(_sent["headers"])
        self.assertNotIn("x-forwarded-for", sent)
        self.assertEqual(sent[ui.ACTOR_HEADER], "127.0.0.1")

    def test_the_registrys_answer_carries_none_of_its_headers(self):
        # The lower-trust upstream: its JSON body, and no header of its choosing.
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

        with mock.patch.object(ui._tasks_client, "send", send), \
                mock.patch.object(ui._client, "send", send):
            registry = self._proxy("tasks/api/repos")
            backend = self._proxy("api/config")
        self.assertEqual(registry.kwargs["headers"], {"content-type": "application/json"})
        # The control plane's are relayed as before.
        self.assertEqual(backend.kwargs["headers"]["location"], "http://x/")

    def test_a_registry_that_does_not_answer_is_a_502_in_its_own_shape(self):
        # The status the page reads as "not running", which the registry never sends:
        # its refusals are 400, 404, 409 and 422 (task-registry/operator_api.py).
        async def _boom(_req, stream=False):
            raise sys.modules["httpx"].RequestError("connection refused")

        with mock.patch.object(ui._tasks_client, "send", _boom), \
                mock.patch.object(ui._client, "send", _boom):
            registry = self._proxy("tasks/api/repos")
            backend = self._proxy("api/config")
        self.assertEqual(registry.status_code, 502)
        self.assertEqual(registry.args[0]["ok"], False)
        self.assertIsNone(registry.args[0]["current"])
        self.assertIn("task registry did not answer", registry.args[0]["detail"])
        # The control plane's own stays as it was.
        self.assertEqual(backend.status_code, 502)
        self.assertIn("control-plane backend unreachable", backend.args[0])

    def test_the_registry_is_dialled_by_address_and_with_a_bounded_wait(self):
        # By name, a stopped registry's `tasks` would be asked of the host's DNS.
        host = urlsplit(ui.TASKS_URL).hostname
        self.assertEqual(str(ipaddress.ip_address(host)), host)
        src = (ROOT / "control-plane-ui" / "app.py").read_text()
        self.assertRegex(src, r"_tasks_client = httpx\.AsyncClient\(base_url=TASKS_URL,"
                              r"\s+timeout=httpx\.Timeout\(10\.0, connect=2\.0\)\)")


class TasksRunningTests(unittest.TestCase):
    """``GET /tasks/running``, which this container answers itself: whether the task
    registry answers, so the page asks it before calling the registry at all."""

    def _ask(self, get):
        with mock.patch.object(ui._tasks_client, "get", get, create=True):
            return asyncio.run(ui.tasks_running())

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
        # A 403 is its peer check refusing this UI's address: there is no tab to show.
        for status in (403, 404, 500):
            async def get(path, status=status):
                return types.SimpleNamespace(status_code=status)

            with self.subTest(status=status):
                self.assertEqual(self._ask(get), {"running": False})

    def test_it_is_served_here_before_the_relay_route(self):
        # Declared above the catch-all, which would otherwise take it and refuse it.
        src = (ROOT / "control-plane-ui" / "app.py").read_text()
        self.assertLess(src.index('@app.get("/tasks/running")'),
                        src.index('@app.api_route("/{path:path}"'))


class GuardWiringTests(unittest.TestCase):
    """The shared guards, put on this app at import under its name and allowlist."""

    def test_they_are_installed_on_this_app_once(self):
        calls = []
        with mock.patch.object(ui.guards, "install", lambda *a: calls.append(a)):
            fresh = _load_ui("dockade_control_plane_ui_wiring")
        self.assertEqual(len(calls), 1)
        app, name, allowed = calls[0]
        self.assertIs(app, fresh.app)
        self.assertEqual(name, "control-plane-ui")
        self.assertIs(allowed, fresh.ALLOWED_HOSTNAMES)

    def test_the_allowlist_is_loopback_unless_its_variable_says_otherwise(self):
        # Compared without the port, so it holds whatever port the host publishes.
        self.assertEqual(ui.ALLOWED_HOSTNAMES, {"127.0.0.1", "localhost", "::1"})
        with mock.patch.dict(os.environ, {"CONTROL_UI_ALLOWED_HOSTS": "ui.example"}):
            fresh = _load_ui("dockade_control_plane_ui_hosts")
        self.assertEqual(fresh.ALLOWED_HOSTNAMES, {"ui.example"})

    def test_an_empty_allowlist_stops_the_import(self):
        with mock.patch.dict(os.environ, {"CONTROL_UI_ALLOWED_HOSTS": " , "}), \
                self.assertRaises(RuntimeError) as caught:
            _load_ui("dockade_control_plane_ui_empty")
        self.assertIn("CONTROL_UI_ALLOWED_HOSTS is empty", str(caught.exception))


UI_NET, UI_BIND = "172.25.0.0/24", "172.25.0.2"


class EntryTests(unittest.TestCase):
    """``main``: the bind is checked before uvicorn runs, and the app it runs is this
    one, on that address alone, with the peer taken from the socket."""

    def setUp(self):
        self.calls: list[tuple[tuple, dict]] = []

    def run_main(self, bind: str) -> None:
        uvicorn = types.ModuleType("uvicorn")
        uvicorn.run = lambda *a, **k: self.calls.append((a, k))
        with mock.patch.dict(sys.modules, {"uvicorn": uvicorn}), \
                mock.patch.object(ui, "BIND", bind), mock.patch.object(ui, "PORT", 18090):
            ui.main()

    def test_it_serves_this_app_on_the_bound_address_alone(self):
        self.run_main(UI_BIND)
        self.assertEqual(len(self.calls), 1)
        (served,), kwargs = self.calls[0]
        self.assertEqual(kwargs, {"host": UI_BIND, "port": 18090,
                                  "proxy_headers": False, "log_level": "info"})
        # Inside the peer check, for its own network, refusing in its own name.
        self.assertIsInstance(served, ui.guards.OnlyFrom)
        self.assertIs(served.app, ui.app)
        self.assertEqual(str(served.net), UI_NET)
        self.assertIn(b"refused by control-plane-ui: ", served._refusal)

    def test_anything_but_an_address_on_control_ui_net_starts_nothing(self):
        # A wildcard serves every leg; a name would make the check depend on DNS; the
        # mapped spelling is another family, which uvicorn would bind as such. Then the
        # UI's control-net leg, and the task registry's operator network, which the UI
        # joins for the registry's view.
        for bind in ("", "0.0.0.0", "::", "*", "control-plane-ui", "localhost",  # noqa: S104
                     "127.0.0.1", "::1", "::ffff:172.25.0.2", "172.31.0.10",
                     "172.26.0.3"):
            with self.subTest(bind=bind), self.assertRaises(SystemExit) as caught:
                self.run_main(bind)
            message = str(caught.exception)
            self.assertTrue(
                message.startswith(f"control-plane-ui: CONTROL_UI_BIND={bind!r} "), message)
            self.assertIn("The relay reaches the management API", message)
        self.assertEqual(self.calls, [])

    def test_a_network_that_does_not_parse_starts_nothing(self):
        with mock.patch.object(ui, "UI_NET", "control-ui-net"), \
                self.assertRaises(SystemExit) as caught:
            self.run_main(UI_BIND)
        self.assertIn("control-plane-ui: CONTROL_UI_NET='control-ui-net'",
                      str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_the_network_checked_is_control_ui_nets(self):
        self.assertEqual(ui.UI_NET, UI_NET)

    def test_the_port_defaults_to_the_one_the_image_exposes(self):
        self.assertEqual(ui.PORT, 8090)


if __name__ == "__main__":
    unittest.main()
