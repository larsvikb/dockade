# SPDX-License-Identifier: Apache-2.0
"""Guards over the gateway's discovery and reconciliation.

The point of this module is a REPORT, so the tests are mostly about the report being
right rather than about a refusal. That is a different bar from
``tests/test_tool_gateway.py``, and the reason is worth stating: nothing here grants.
A wrong answer from a server can only make the report wrong, because execution policy
is decided per call against rules an operator wrote, and a tool missing from every
list below is still denied by default if it is ever called.

What that buys is that these tests can use fixtures freely — a fake server's reply is
just a string — and what it costs is that a bug here is silent. Hence the coverage:
every failure direction is asserted to produce a LINE, because a diagnostic that
vanishes on error is worse than one that was never written.
"""
from __future__ import annotations

import contextlib
import http.client
import http.server
import io
import json
import re
import socket
import sys
import threading
import time
import unittest
from unittest import mock

from _loader import load_control_plane, load_discovery, load_inventory

#: A roster entry as ``/tool/roster`` serves one, trimmed to what this module reads.
#: Written out rather than imported from the control plane: these two are separate
#: processes that agree by contract, and a shared constant would make a test pass on
#: a contract neither side actually implements.
ENTRY = {"server": "mcp-github",
         "auth": {"type": "header", "header": "Authorization",
                  "template": "Bearer {secret}"},
         "endpoint": {"port": 8082, "path": "/mcp"},
         "tools": [{"tool": "get_issue", "action": "allow"},
                   {"tool": "create_pr", "action": "ask"}]}


def sse(result: dict, request_id: int = 1) -> str:
    """An MCP reply in the shape the server actually sends — SSE, not a JSON body."""
    return (f"data: "
            f"{json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result})}\n\n")


def response(result: dict) -> dict:
    """What ``post`` returns for a reply carrying ``result``."""
    return {"jsonrpc": "2.0", "id": 1, "result": result}


class Reply(io.BytesIO):
    """A server's reply as ``_OPENER.open`` hands one to ``post``: a binary stream
    with a Content-Type."""

    def __init__(self, body, content_type: str = "text/event-stream"):
        super().__init__(body.encode() if isinstance(body, str) else body)
        self.headers = {"Content-Type": content_type}


def read(discovery, raw: str, request_id: int = 1, on_request=None) -> dict:
    """``raw``, a reply's text, through the real reader: as `text/event-stream` when it
    has a data line, as `application/json` otherwise, as a server labels each."""
    content_type = ("text/event-stream" if re.search(r"(?m)^data:", raw)
                    else "application/json")
    return discovery.read_reply(Reply(raw, content_type), content_type, request_id,
                                on_request or (lambda request: None))


def echoing(result: dict, dialled: list | None = None):
    """A stand-in for ``_OPENER.open`` answering each request with ``result`` under the
    id the request carried, recording each request in ``dialled``."""
    @contextlib.contextmanager
    def urlopen(request, *_args, **_kwargs):
        if dialled is not None:
            dialled.append(request)
        yield Reply(sse(result, json.loads(request.data)["id"]))
    return urlopen


#: A reply body the swapped-in ``json.loads`` refuses as too deep: a marker rather than
#: a real nesting, so the proof does not depend on the depth at which this interpreter
#: gives up.
TOO_DEEP = b'"too-deep"'


def _loads_recursing_on_marker(real=json.loads):
    def stand_in(value, *args, **kwargs):
        if value == TOO_DEEP:
            raise RecursionError("too deep")
        return real(value, *args, **kwargs)
    return stand_in


def _parse_error(body: bytes) -> str:
    try:
        json.loads(body)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"{body!r} parsed")


#: Each way the control plane can answer badly, by where real code raises it: urlopen
#: for a status line that is not HTTP, the response's read for a connection reset or
#: a body cut short, and the parse of the reply for one that is not JSON or is nested
#: too deeply. Each with the text the reason quotes.
CONTROL_FAULTS = {
    "BadStatusLine": (dict(on_open=http.client.BadStatusLine("garbage")), "garbage"),
    "ConnectionResetError": (dict(on_read=ConnectionResetError("reset")), "reset"),
    "IncompleteRead": (dict(on_read=http.client.IncompleteRead(b"par", 10)),
                       "IncompleteRead(3 bytes read, 10 more expected)"),
    "ValueError": (dict(reply=b"{not json"), _parse_error(b"{not json")),
    "RecursionError": (dict(reply=TOO_DEEP), "too deep"),
}


@contextlib.contextmanager
def control_plane_answering(discovery, *, reply=b"{}", on_open=None, on_read=None):
    """``urllib.request.urlopen`` stood in for, answering ``reply`` or raising
    ``on_open`` or ``on_read`` where real code would. It parses nothing itself, so a
    RecursionError can only come from the code under test."""
    @contextlib.contextmanager
    def urlopen(_request, *_args, **_kwargs):
        if on_open is not None:
            raise on_open

        def read():
            if on_read is not None:
                raise on_read
            return reply
        yield mock.Mock(read=read)

    with mock.patch("urllib.request.urlopen", urlopen), \
            mock.patch.object(discovery.json, "loads", _loads_recursing_on_marker()):
        yield


class ParseTests(unittest.TestCase):

    def setUp(self):
        self.discovery = load_discovery()

    def parse(self, raw: str) -> list[dict]:
        return self.discovery.parse_tools(read(self.discovery, raw))

    def test_the_sse_shape_is_parsed(self):
        # The measured shape (NOTES.md). If this breaks, enumeration is broken.
        tools = self.parse(sse({"tools": [{"name": "get_issue"}]}))
        self.assertEqual([t["name"] for t in tools], ["get_issue"])

    def test_a_plain_json_body_is_also_parsed(self):
        # Legal for this transport and not what the server sends today. Accepted so a
        # version bump cannot silently empty the report — the failure would look like
        # "the server exposes no tools", which is a sentence this report is supposed
        # to be trusted about.
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "result": {"tools": [{"name": "get_issue"}]}})
        self.assertEqual([t["name"] for t in self.parse(body)],
                         ["get_issue"])

    def test_prose_is_reported_as_prose(self):
        # The bad-bearer failure mode: a bare line of text, no JSON. The parse error
        # is the symptom and the text is the diagnosis, so the text has to survive
        # into the message or an operator is left debugging a JSONDecodeError.
        with self.assertRaises(self.discovery.DiscoveryError) as caught:
            self.parse("Missing Authorization header")
        self.assertIn("Missing Authorization header", str(caught.exception))

    def test_a_jsonrpc_error_is_not_an_empty_tool_list(self):
        # The one that matters most. `result` is absent on an error reply, so a naive
        # reader returns [] and the report says the server exposes nothing — which
        # would read as "every rule is dead policy" and invite someone to delete them.
        with self.assertRaises(self.discovery.DiscoveryError) as caught:
            self.parse(
                'data: {"jsonrpc":"2.0","id":1,"error":{"code":-32601}}')
        self.assertIn("-32601", str(caught.exception))

    def test_an_empty_reply_is_an_error(self):
        with self.assertRaises(self.discovery.DiscoveryError):
            self.parse("")

    def test_valid_json_that_is_not_an_object_is_an_error_not_a_crash(self):
        # `message.get(...)` on a list or a string raised AttributeError, which
        # `reconcile` does not catch — so one server answering oddly killed the
        # reconcile thread for the life of the process, healthcheck still green.
        for raw in ("[]", '"x"', "null", "7", "data: []\n\n"):
            with self.subTest(raw=raw), \
                    self.assertRaises(self.discovery.DiscoveryError):
                self.parse(raw)

    def test_a_result_without_a_tool_list_is_an_error_not_an_empty_list(self):
        # `{"result": null}` and `{"result": []}` are not "this server exposes
        # nothing", which is the claim an empty list makes and which the inventory
        # push would then carry to the operator's picker.
        for result in (None, [], "tools", {"tools": None}, {"tools": {"a": 1}}):
            with self.subTest(result=result):
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})
                with self.assertRaises(self.discovery.DiscoveryError) as caught:
                    self.parse(body)
                self.assertIn("no tool list", str(caught.exception))

    def test_a_notification_ahead_of_the_response_is_skipped(self):
        # Q4. A stream may carry progress or log notifications before the response;
        # taking the first event read one of those as the reply.
        raw = ('data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}\n\n'
               + sse({"tools": [{"name": "get_issue"}]}))
        self.assertEqual([t["name"] for t in self.parse(raw)],
                         ["get_issue"])

    def test_a_request_from_the_server_is_not_taken_for_the_reply(self):
        # It has an id — possibly OUR id — and a `method`. The method is what marks it
        # as a request rather than the response to ours.
        raw = ('data: {"jsonrpc":"2.0","id":1,"method":"sampling/createMessage"}\n\n'
               + sse({"tools": [{"name": "get_issue"}]}))
        self.assertEqual([t["name"] for t in self.parse(raw)],
                         ["get_issue"])

    def test_sse_is_read_by_the_spec_not_by_the_line(self):
        # The optional space, a message split over several data lines, CRLF endings,
        # and the fields that are not data. Each is legal SSE a server may send.
        body = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "x"}]}}
        text = json.dumps(body, indent=1)
        split = "".join(f"data:{line}\r\n" for line in text.splitlines())
        raw = (": a comment\r\nevent: message\r\nid: 7\r\n" + split + "\r\n")
        self.assertEqual([t["name"] for t in self.parse(raw)], ["x"])

    def test_a_stream_that_never_answers_our_request_is_an_error(self):
        for raw in ('data: {"jsonrpc":"2.0","method":"notifications/progress",'
                    '"params":{"progress":1}}\n\n',
                    sse({"tools": []}).replace('"id": 1', '"id": 2')):
            with self.subTest(raw=raw), \
                    self.assertRaises(self.discovery.DiscoveryError) as caught:
                self.parse(raw)
            self.assertIn("no response", str(caught.exception))

    def test_a_malformed_reply_is_a_line_in_the_report_not_a_dead_thread(self):
        # Through `reconcile`, which is what the loop calls: the shape error has to
        # arrive as the DiscoveryError it catches, and read as NOT ENUMERATED.
        self.discovery.post = lambda *_a, **_k: read(self.discovery, "[]")
        report = "\n".join(self.discovery.format_report([self.discovery.reconcile(ENTRY)]))
        self.assertIn("NOT ENUMERATED", report)
        self.assertIn("expected a JSON object", report)


def _resolves_to(*addresses):
    """A ``getaddrinfo`` that answers with ``addresses``, in order — Docker DNS for a
    name the gateway can see on more than one network."""
    def getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", (address, port)) for address in addresses]
    return getaddrinfo


class PlacementTests(unittest.TestCase):
    """Where a server's credential may go (S21). The gateway dials by name and Docker
    DNS answers from every network it shares, sandbox-net included, so the address is
    what decides — and it has to be on mcp-net."""

    def setUp(self):
        self.discovery = load_discovery()
        self.discovery.read_secret = lambda server: "the-token"
        self.dialled = []

        patcher = mock.patch.object(self.discovery._OPENER, "open",
                                    echoing({"tools": []}, self.dialled))
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, *addresses):
        with mock.patch("socket.getaddrinfo", _resolves_to(*addresses)):
            return self.discovery.post(ENTRY, {"id": 1})

    def test_the_server_is_dialled_at_its_mcp_net_address_under_its_own_name(self):
        self.post("172.28.0.11")
        [request] = self.dialled
        self.assertEqual(request.full_url, "http://172.28.0.11:8082/mcp")
        # What urllib sent when the name was in the URL, so the server sees no change.
        self.assertEqual(request.get_header("Host"), "mcp-github:8082")
        self.assertEqual(request.get_header("Authorization"), "Bearer the-token")

    def test_the_port_and_path_are_the_ones_the_roster_gives(self):
        entry = {**ENTRY, "endpoint": {"port": 3000, "path": "/v1/mcp"}}
        with mock.patch("socket.getaddrinfo", _resolves_to("172.28.0.11")):
            self.discovery.post(entry, {"id": 1})
        [request] = self.dialled
        self.assertEqual(request.full_url, "http://172.28.0.11:3000/v1/mcp")
        self.assertEqual(request.get_header("Host"), "mcp-github:3000")

    def test_an_endpoint_that_could_move_the_host_dials_nothing(self):
        # The path follows the checked address in the URL, so `@host` there would make
        # `host` the one dialled, with the credential. Refused before anything resolves
        # or any secret is read, as a bad name is.
        self.discovery.read_secret = mock.Mock(return_value="the-token")
        for endpoint in ({"port": 8082, "path": "@evil.example/mcp"},
                         {"port": 8082, "path": "//evil.example/mcp"},
                         {"port": 8082, "path": "/mcp?x=@evil.example"},
                         {"port": 8082, "path": "/%40evil.example"},
                         {"port": 8082, "path": "/../mcp"},
                         {"port": 8082, "path": "mcp"},
                         {"port": "8082@evil.example", "path": "/mcp"},
                         {"port": 0, "path": "/mcp"},
                         {"port": True, "path": "/mcp"},
                         {"path": "/mcp"},
                         None):
            with self.subTest(endpoint=endpoint), \
                    self.assertRaises(self.discovery.DiscoveryError):
                self.post_to({**ENTRY, "endpoint": endpoint}, "172.28.0.11")
        self.assertEqual(self.dialled, [])
        self.discovery.read_secret.assert_not_called()

    def post_to(self, entry, *addresses):
        with mock.patch("socket.getaddrinfo", _resolves_to(*addresses)):
            return self.discovery.post(entry, {"id": 1})

    def test_the_path_pattern_is_the_control_planes_own(self):
        # Two images, no shared module, as in test_tool_surface: this test is what
        # holds the gateway's check to the one registration is held to.
        policy = load_control_plane().policy
        self.assertEqual(self.discovery._PATH_RE.pattern, policy._PATH_RE.pattern)
        self.assertEqual(self.discovery._PATH_MAX_LEN, policy._PATH_MAX_LEN)
        # The pattern is half of each check; the segment rule and the length cap are
        # the other half, so the two checks are held to the same answers as well.
        for path in ("/", "/mcp", "/v1/mcp/", "/.well-known/mcp", "/../mcp", "/./x",
                     "/a//b", "//x", "@x/mcp", "/mcp\n", "/mcp?x", "/" + "a" * 127,
                     "/" + "a" * 128):
            with self.subTest(path=path):
                try:
                    self.discovery.check_endpoint({"port": 8082, "path": path})
                    gateway = True
                except self.discovery.DiscoveryError:
                    gateway = False
                self.assertEqual(gateway, policy._endpoint_error(8082, path) is None)

    def test_a_long_path_is_refused_at_once(self):
        # The first spelling of the pattern backtracked exponentially: this path took it
        # seconds, a 128-char one would never return. Kept short so a regression FAILS
        # here rather than hanging the run.
        started = time.monotonic()
        with self.assertRaises(self.discovery.DiscoveryError):
            self.discovery.check_endpoint({"port": 8082, "path": "/" + "a" * 28 + "!"})
        self.assertLess(time.monotonic() - started, 0.5)

    def test_a_name_with_a_trailing_newline_is_not_a_name(self):
        # `$` matches before a final newline; the check is a fullmatch so it cannot.
        with self.assertRaises(self.discovery.DiscoveryError):
            self.discovery.check_name("mcp-github\n")

    def test_a_name_that_leads_off_mcp_net_is_not_sent_the_credential(self):
        # The finding: a sandbox-net container answering to a server's name.
        with self.assertRaises(self.discovery.DiscoveryError) as caught:
            self.post("172.30.0.9")
        self.assertEqual(self.dialled, [])
        self.assertIn("172.30.0.9", str(caught.exception))
        self.assertIn("mcp-net", str(caught.exception))

    def test_the_mcp_net_answer_is_chosen_whatever_order_dns_gives(self):
        self.post("172.30.0.9", "172.28.0.11")
        self.assertEqual(self.dialled[0].full_url, "http://172.28.0.11:8082/mcp")

    def test_a_name_that_does_not_resolve_dials_nothing(self):
        def unresolvable(*args, **kwargs):
            raise OSError("Name or service not known")

        with mock.patch("socket.getaddrinfo", unresolvable), \
                self.assertRaises(self.discovery.DiscoveryError) as caught:
            self.discovery.post(ENTRY, {"id": 1})
        self.assertEqual(self.dialled, [])
        self.assertIn("does not resolve", str(caught.exception))

    def test_the_name_is_resolved_once_and_the_answer_is_what_is_dialled(self):
        # Checking one lookup and connecting on another would check one answer and
        # use another. The URL carrying the address is what makes the second lookup
        # impossible, so it is the thing asserted.
        calls = []

        def counting(host, port, *args, **kwargs):
            calls.append(host)
            return [(2, 1, 6, "", ("172.28.0.11", port))]

        with mock.patch("socket.getaddrinfo", counting):
            self.discovery.post(ENTRY, {"id": 1})
        self.assertEqual(calls, ["mcp-github"])
        self.assertTrue(self.dialled[0].host.startswith("172.28.0.11"))

    def test_a_network_that_cannot_be_parsed_refuses_to_start(self):
        with self.assertRaises(SystemExit) as caught:
            load_discovery({"GATEWAY_MCP_NET": "not-a-cidr"})
        self.assertIn("GATEWAY_MCP_NET", str(caught.exception))

    def test_a_network_with_host_bits_refuses_to_start(self):
        with self.assertRaises(SystemExit) as caught:
            load_discovery({"GATEWAY_MCP_NET": "172.28.0.0/2"})
        self.assertIn("host bits", str(caught.exception))


class PlacementOnTheWireTests(unittest.TestCase):
    """The same property through the real urllib: the connection goes to the checked
    address and the request line still names the server. Loopback stands in for
    mcp-net, so the check under test is the real one with a different network."""

    def test_the_request_on_the_wire_names_the_server_not_the_address(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        seen = []

        def serve():
            conn, _ = listener.accept()
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(65536)
                seen.append(data.decode("latin-1"))
                body = sse({"tools": []}).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Content-Length: %d\r\nConnection: close\r\n\r\n%s"
                             % (len(body), body))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        discovery = load_discovery({"GATEWAY_MCP_NET": "127.0.0.0/8"})
        with mock.patch("socket.getaddrinfo", _resolves_to("127.0.0.1")):
            discovery.post({"server": "mcp-github", "auth": {"type": "none"},
                            "endpoint": {"port": port, "path": "/mcp"}}, {"id": 1})
        head = seen[0].lower()
        self.assertIn(f"host: mcp-github:{port}\r\n", head)
        self.assertEqual(head.count("host:"), 1)


class RedirectTests(unittest.TestCase):
    """What a server's reply can make the dial do, through the real urllib over
    loopback. A followed redirect would carry the credential to a host the gateway
    resolved without ``_placed`` (S21's property, from the server's side rather than
    DNS's); a reply that is not HTTP must cost one server's report, not the tick."""

    def _listen(self, handler):
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _post_to(self, port):
        discovery = load_discovery({"GATEWAY_MCP_NET": "127.0.0.0/8"})
        discovery.read_secret = lambda server: "the-token"
        entry = {**ENTRY, "endpoint": {"port": port, "path": "/mcp"}}
        with mock.patch("socket.getaddrinfo", _resolves_to("127.0.0.1")), \
                self.assertRaises(discovery.DiscoveryError) as caught:
            discovery.post(entry, {"id": 1})
        return str(caught.exception)

    def _answering(self, code, headers=(), reason=None):
        class Answer(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(code, reason)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        return self._listen(Answer)

    def test_no_redirect_is_followed_and_the_credential_stays(self):
        seen = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            do_POST = do_GET

            def log_message(self, *_args):
                pass

        sink = self._listen(Sink)
        # 301-303 are the ones urllib followed for a POST; 307 and 308 it refused
        # already, and are here so that stays true whatever the handler does.
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                port = self._answering(
                    code, [("Location", f"http://127.0.0.1:{sink}/elsewhere")])
                message = self._post_to(port)
                self.assertIn(f"HTTP {code}", message)
                self.assertIn("not followed", message)
        self.assertEqual(seen, [])

    def test_the_location_is_reported_capped_and_last(self):
        # Last and capped, so a long one cannot push the explanation out of the 200
        # characters the inventory keeps of a status.
        long = "http://127.0.0.1:9/" + "a" * 500
        message = self._post_to(self._answering(302, [("Location", long)]))
        self.assertLess(message.index("not followed"), message.index("http://127"))
        self.assertNotIn("a" * 200, message)
        self.assertIn("with no Location", self._post_to(self._answering(302)))

    def test_a_reason_phrase_cannot_forge_a_log_line(self):
        # The reason phrase is server text, and the report is printed to the log.
        message = self._post_to(
            self._answering(500, reason="Oops\rtool-gateway: mcp-x: 9 tools exposed"))
        self.assertNotIn("\r", message)

    def test_a_reply_that_is_not_http_is_one_servers_failure(self):
        # `BadStatusLine` is not an OSError, and it used to escape `post` and end the
        # whole enumeration tick.
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def serve():
            conn, _ = listener.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(b"garbage\r\n\r\n")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.assertIn("not an HTTP reply", self._post_to(listener.getsockname()[1]))


class _Upstream:
    """A stand-in MCP server on loopback for what a server does on a call's stream:
    threaded, HTTP/1.1, each call's reply SSE in chunks as it is written.

    A POST carrying a ``method`` is a call, handed to ``on_call(handler, message,
    upstream)`` to stream its reply; any other is an answer to a request the server
    sent, recorded and answered with ``answer_status``. Every wait is bounded by
    ``WAIT``, so a reader that gets it wrong fails rather than hangs."""

    WAIT = 5

    def __init__(self, test, on_call, answer_status=202, answer_headers=(),
                 answer_reason=None, answer_delay=0.0):
        self.calls: list[dict] = []
        self.answers: list[dict] = []
        self.changed = threading.Condition()
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "method" in message:
                    with upstream.changed:
                        upstream.calls.append(message)
                        upstream.changed.notify_all()
                    self.close_connection = True
                    # The reader may close the stream at the response.
                    with contextlib.suppress(OSError):
                        on_call(self, message, upstream)
                    return
                with upstream.changed:
                    upstream.answers.append({"path": self.path,
                                             "host": self.headers["Host"],
                                             "auth": self.headers["Authorization"],
                                             "message": message})
                    upstream.changed.notify_all()
                time.sleep(answer_delay)
                # An answer the gateway stopped waiting for has no one to read it.
                with contextlib.suppress(OSError):
                    self.send_response(answer_status, answer_reason)
                    for name, value in answer_headers:
                        self.send_header(name, value)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def stream(self):
                self.streaming = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

            def chunk(self, data: bytes):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

            def event(self, message: dict, end: bytes = b"\n"):
                self.chunk(b"data: " + json.dumps(message).encode() + end + end)

            def finish(self):
                if getattr(self, "streaming", False):
                    with contextlib.suppress(OSError):
                        self.wfile.write(b"0\r\n\r\n")
                super().finish()

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.block_on_close = False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        test.addCleanup(server.server_close)
        test.addCleanup(server.shutdown)
        self.server = server
        self.port = server.server_address[1]

    def wait_for(self, predicate) -> bool:
        with self.changed:
            return self.changed.wait_for(lambda: predicate(self), timeout=self.WAIT)


def _result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


class ServerRequestTests(unittest.TestCase):
    """What a server sends on a call's own stream, through the real ``post`` and
    urllib over loopback, getaddrinfo patched as in RedirectTests. A stateful server
    sends a request there and holds the call until it is answered, and keys each
    call's stream by its id (NOTES.md, "A stateful MCP server talks back on a call's
    stream")."""

    def setUp(self):
        self.call = {"jsonrpc": "2.0", "method": "tools/call",
                     "params": {"name": "delete_thing", "arguments": {}}}
        self.discovery = load_discovery({"GATEWAY_MCP_NET": "127.0.0.0/8"})
        self.secrets_read = 0

        def read_secret(server):
            # A new token per read, so an answer that read the credential again rather
            # than taking the call's headers carries the wrong one.
            self.secrets_read += 1
            return f"token-{self.secrets_read}"

        self.discovery.read_secret = read_secret
        self.lookups = []

        def getaddrinfo(host, port, *_args, **_kwargs):
            self.lookups.append(host)
            return [(2, 1, 6, "", ("127.0.0.1", port))]

        patcher = mock.patch("socket.getaddrinfo", getaddrinfo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, upstream, message=None):
        entry = {**ENTRY, "endpoint": {"port": upstream.port, "path": "/mcp"}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            response = self.discovery.post(entry, message or self.call, timeout=10)
        return response, out.getvalue().splitlines()

    def refusal(self, method: str) -> str:
        return (f"tool-gateway: mcp-github: refused its {method} request on a call's "
                f"stream; nothing a server asks reaches the agent")

    def test_each_request_is_answered_as_it_arrives_on_the_calls_own_terms(self):
        # The SDK's elicitation carries the call's own id; the result is withheld
        # until all three are answered, so a reader that answers only once it has
        # read the whole reply never gets it.
        def on_call(handler, message, upstream):
            handler.stream()
            handler.event({"jsonrpc": "2.0", "method": "notifications/message",
                           "params": {"level": "info", "data": "working"}})
            handler.event({"jsonrpc": "2.0", "id": message["id"],
                           "method": "elicitation/create",
                           "params": {"message": "Sure?", "requestedSchema": {}}})
            handler.event({"jsonrpc": "2.0", "id": "r-1", "method": "roots/list"})
            handler.event({"jsonrpc": "2.0", "id": 0, "method": "ping"})
            if upstream.wait_for(lambda u: len(u.answers) >= 3):
                handler.event({"jsonrpc": "2.0", "id": message["id"],
                               "result": _result("done")})

        upstream = _Upstream(self, on_call)
        response, lines = self.post(upstream)
        [call] = upstream.calls
        self.assertEqual(response, {"jsonrpc": "2.0", "id": call["id"],
                                    "result": _result("done")})
        refused = {"code": -32601,
                   "message": "the gateway answers no requests from a server"}
        self.assertEqual([answer["message"] for answer in upstream.answers],
                         [{"jsonrpc": "2.0", "id": call["id"], "error": refused},
                          {"jsonrpc": "2.0", "id": "r-1", "error": refused},
                          {"jsonrpc": "2.0", "id": 0, "result": {}}])
        for answer in upstream.answers:
            self.assertEqual((answer["path"], answer["host"], answer["auth"]),
                             ("/mcp", f"mcp-github:{upstream.port}", "Bearer token-1"))
        self.assertEqual(self.secrets_read, 1)
        # One lookup of the name across the call and every answer: an answer goes to
        # the address the call was placed at, not to the name resolved again. The
        # rest are socket.create_connection passing each dial's address literal
        # through getaddrinfo, which asks no resolver; there is one per dial.
        names = [host for host in self.lookups if host != "127.0.0.1"]
        self.assertEqual(names, ["mcp-github"])
        self.assertEqual(len(self.lookups), 1 + 1 + len(upstream.answers))
        self.assertEqual(lines, [self.refusal("elicitation/create"),
                                 self.refusal("roots/list")])

    def test_a_method_cannot_forge_a_log_line_and_is_capped(self):
        forged = "x\rtool-gateway: mcp-github: 9 tools exposed\x1b[2K"

        def on_call(handler, message, upstream):
            handler.stream()
            handler.event({"jsonrpc": "2.0", "id": 1, "method": forged})
            handler.event({"jsonrpc": "2.0", "id": 2, "method": "m" * 500})
            handler.event({"jsonrpc": "2.0", "id": message["id"], "result": {}})

        _response, lines = self.post(_Upstream(self, on_call))
        self.assertEqual(lines, [
            self.refusal("x\\rtool-gateway: mcp-github: 9 tools exposed\\x1b[2K"),
            self.refusal("m" * 120)])

    def test_overlapping_calls_each_get_their_own_result(self):
        # Held until both have arrived, so a lock around the whole call cannot pass;
        # an id seen before is rejected, as the SDK loses one of two calls sharing it.
        def on_call(handler, message, upstream):
            handler.stream()
            if [call["id"] for call in upstream.calls].count(message["id"]) > 1:
                handler.event({"jsonrpc": "2.0", "id": message["id"],
                               "error": {"code": -32600, "message": "id reused"}})
            elif upstream.wait_for(lambda u: len(u.calls) >= 2):
                who = message["params"]["arguments"]["who"]
                handler.event({"jsonrpc": "2.0", "id": message["id"],
                               "result": _result(who)})

        upstream = _Upstream(self, on_call)
        entry = {**ENTRY, "endpoint": {"port": upstream.port, "path": "/mcp"}}
        results = {}

        def call(who):
            message = {**self.call, "params": {"name": "t", "arguments": {"who": who}}}
            try:
                results[who] = self.discovery.post(entry, message, timeout=10)
            except self.discovery.DiscoveryError as exc:
                results[who] = str(exc)

        threads = [threading.Thread(target=call, args=(who,), daemon=True)
                   for who in "ab"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3 * _Upstream.WAIT)
        self.assertEqual({who: result.get("result") if isinstance(result, dict)
                          else result for who, result in results.items()},
                         {"a": _result("a"), "b": _result("b")})

    def test_the_reply_is_left_at_the_response_however_its_lines_end(self):
        # The SDK's stream outlives its response; pinging after it, it ends only when
        # this stand-in gives up, which a reader waiting for the end would wait for.
        for end in (b"\n", b"\r", b"\r\n"):
            with self.subTest(end=end):
                gave_up = threading.Event()

                def on_call(handler, message, upstream, end=end, gave_up=gave_up):
                    handler.stream()
                    handler.event({"jsonrpc": "2.0", "id": message["id"],
                                   "result": _result("done")}, end)
                    deadline = time.monotonic() + _Upstream.WAIT
                    while time.monotonic() < deadline:
                        handler.chunk(b": ping" + end + end)
                        time.sleep(0.05)
                    gave_up.set()

                response, _lines = self.post(_Upstream(self, on_call))
                self.assertFalse(gave_up.is_set())
                self.assertEqual(response["result"], _result("done"))

    def test_a_redirected_answer_is_not_followed_and_the_call_still_returns(self):
        seen = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            do_POST = do_GET

            def log_message(self, *_args):
                pass

        sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)
        self.addCleanup(sink.server_close)
        threading.Thread(target=sink.serve_forever, daemon=True).start()
        self.addCleanup(sink.shutdown)

        def on_call(handler, message, upstream):
            handler.stream()
            handler.event({"jsonrpc": "2.0", "id": 5, "method": "ping"})
            handler.event({"jsonrpc": "2.0", "id": message["id"],
                           "result": _result("done")})

        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                upstream = _Upstream(
                    self, on_call, answer_status=code,
                    answer_headers=[("Location", f"http://127.0.0.1:"
                                                 f"{sink.server_address[1]}/x")])
                response, lines = self.post(upstream)
                self.assertEqual(response["result"], _result("done"))
                [line] = lines
                self.assertTrue(line.startswith(
                    f"tool-gateway: mcp-github: the answer to its ping request was "
                    f"not delivered — HTTP {code} from mcp-github, a redirect, not "
                    f"followed"), line)
        self.assertEqual(seen, [])

    def test_a_json_body_is_read_whole(self):
        # How the task registry answers: one JSON body, no stream.
        def on_call(handler, message, upstream):
            body = json.dumps({"jsonrpc": "2.0", "id": message["id"],
                               "result": _result("whole")}).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        upstream = _Upstream(self, on_call)
        # Twice, so the second carries an id other than 1: a reader that matched the
        # whole body on a constant would still pass on the first.
        responses = [self.post(upstream)[0] for _ in range(2)]
        self.assertEqual(responses, [{"jsonrpc": "2.0", "id": call["id"],
                                      "result": _result("whole")}
                                     for call in upstream.calls])
        self.assertNotEqual(upstream.calls[1]["id"], 1)

    #: The start of the line a ping's undelivered answer logs.
    UNDELIVERED = ("tool-gateway: mcp-github: the answer to its ping request was not "
                   "delivered — ")

    @staticmethod
    def _ping_then_result(handler, message, upstream):
        handler.stream()
        handler.event({"jsonrpc": "2.0", "id": 5, "method": "ping"})
        handler.event({"jsonrpc": "2.0", "id": message["id"], "result": _result("done")})

    def test_a_refused_answer_is_logged_and_the_call_still_returns(self):
        def on_call(handler, message, upstream):
            # The port closes once the call is in, so the answer's connect is refused.
            upstream.server.shutdown()
            upstream.server.socket.close()
            self._ping_then_result(handler, message, upstream)

        response, lines = self.post(_Upstream(self, on_call))
        self.assertEqual(response["result"], _result("done"))
        [line] = lines
        self.assertTrue(line.startswith(self.UNDELIVERED + "unreachable: "), line)
        self.assertIn("refused", line)

    def test_an_answer_that_times_out_is_logged_and_the_call_still_returns(self):
        upstream = _Upstream(self, self._ping_then_result, answer_delay=1.5)
        with mock.patch.object(self.discovery, "TIMEOUT", 0.3):
            response, lines = self.post(upstream)
        self.assertEqual(response["result"], _result("done"))
        self.assertEqual(lines, [self.UNDELIVERED + "no answer from mcp-github within 0.3s"])

    def test_why_an_answer_failed_is_logged_capped(self):
        # The reason phrase is the server's, of any length, once per request it sends.
        upstream = _Upstream(self, self._ping_then_result, answer_status=500,
                             answer_reason="x" * 20000)
        response, lines = self.post(upstream)
        self.assertEqual(response["result"], _result("done"))
        [line] = lines
        self.assertEqual(line, self.UNDELIVERED + ("HTTP 500 from mcp-github — '"
                                                    + "x" * 200)[:200])

    def test_each_request_gets_an_id_of_its_own_whatever_the_caller_sent(self):
        dialled = []
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        with mock.patch.object(self.discovery._OPENER, "open",
                               echoing({"tools": []}, dialled)):
            for _ in range(3):
                self.discovery.post(ENTRY, message)
        ids = [json.loads(request.data)["id"] for request in dialled]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(type(ident) is int for ident in ids))
        self.assertEqual(message["id"], 1)


class _Chunks:
    """A reply arriving in the given pieces, one per read, counting the reads."""

    def __init__(self, *pieces: bytes):
        self.pieces = list(pieces)
        self.reads = 0

    def read1(self, _size=-1) -> bytes:
        self.reads += 1
        return self.pieces.pop(0) if self.pieces else b""

    def read(self, _size=-1) -> bytes:
        raise AssertionError("an SSE reply was read whole")


class ReaderTests(unittest.TestCase):
    """``read_reply`` in memory: how a stream is cut into events, and what is a
    request, a response, or neither."""

    def setUp(self):
        self.discovery = load_discovery()
        self.handed = []

    def read(self, *pieces: bytes, content_type="text/event-stream", request_id=1):
        return self.discovery.read_reply(_Chunks(*pieces), content_type, request_id,
                                         self.handed.append)

    def test_an_event_split_inside_a_character_is_decoded_whole(self):
        text = '{"jsonrpc":"2.0","id":1,"result":{"t":"räk"}}'.encode()
        cut = text.index("ä".encode()) + 1
        response = self.read(b"data: " + text[:cut], text[cut:] + b"\n\n")
        self.assertEqual(response["result"], {"t": "räk"})

    def test_a_crlf_split_between_reads_is_one_line_end(self):
        response = self.read(b'data: {"jsonrpc":"2.0",\r',
                             b'\ndata: "id":1,"result":{}}\r\n\r\n')
        self.assertEqual(response, {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_a_request_is_handed_over_before_the_next_read(self):
        stream = _Chunks(b'data: {"jsonrpc":"2.0","id":"q","method":"ping"}\n\n',
                         b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')
        reads_when_handed = []
        self.discovery.read_reply(stream, "text/event-stream", 1,
                                  lambda request: reads_when_handed.append(stream.reads))
        self.assertEqual(reads_when_handed, [1])

    def test_only_a_string_or_an_integer_id_makes_a_request(self):
        events = [{"method": "a", "id": "s"}, {"method": "b", "id": 0},
                  {"method": "c", "id": True}, {"method": "d", "id": None},
                  {"method": "e", "id": 1.5}, {"method": "f"},
                  {"method": "g", "id": ["x"]}]
        self.read(*(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events),
                  b'data: {"id":1,"result":{}}\n\n')
        self.assertEqual([request["method"] for request in self.handed], ["a", "b"])

    def test_a_response_to_another_id_or_a_bool_one_is_not_ours(self):
        response = self.read(b'data: {"id":true,"result":"bool"}\n\n',
                             b'data: {"id":2,"result":"other"}\n\n',
                             b'data: {"id":1,"result":"ours"}\n\n')
        self.assertEqual(response["result"], "ours")
        self.assertEqual(self.read(b'data: {"id":null,"result":"null"}\n\n')["result"],
                         "null")

    def test_the_media_type_is_read_with_any_parameters_and_any_case(self):
        for content_type in ("text/event-stream; charset=utf-8", "Text/Event-Stream",
                             " text/event-stream"):
            with self.subTest(content_type=content_type):
                self.assertEqual(
                    self.read(b'data: {"id":1,"result":{}}\n\n',
                              content_type=content_type)["result"], {})

    def test_any_other_type_is_read_whole_and_its_requests_go_unanswered(self):
        body = (b'data: {"jsonrpc":"2.0","id":"q","method":"ping"}\n\n'
                b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')
        for content_type in ("application/json", None):
            with self.subTest(content_type=content_type):
                response = self.discovery.read_reply(io.BytesIO(body), content_type, 1,
                                                     self.handed.append)
                self.assertEqual(response["result"], {})
        self.assertEqual(self.handed, [])

    def test_an_event_with_no_data_is_passed_over(self):
        with self.assertRaises(self.discovery.DiscoveryError) as caught:
            self.read(b"data:\n\ndata: \n\n")
        self.assertEqual(str(caught.exception),
                         "the reply carried no response to the request")


def _list_tools_of(discovery, entries: list):
    """``list_tools`` against a server whose tool list is exactly ``entries``."""
    with mock.patch.object(discovery._OPENER, "open", echoing({"tools": entries})), \
            mock.patch("socket.getaddrinfo", _resolves_to("172.28.0.11")):
        return discovery.list_tools({**ENTRY, "auth": {"type": "none"}})


def _refusing_arguments(marker: object, real, error: type = RecursionError):
    """A stand-in for ``json.dumps`` that raises ``error`` on a message whose
    ``params.arguments`` is ``marker`` and is ``real`` on anything else, so a message
    too deep to encode is proved without depending on the depth at which this
    interpreter gives up. It matches on the arguments, not the whole message, so a
    change to the envelope cannot silently stop it matching."""
    def stand_in(value, *args, **kwargs):
        params = value.get("params") if isinstance(value, dict) else None
        if isinstance(params, dict) and params.get("arguments") == marker:
            raise error("refused")
        return real(value, *args, **kwargs)
    return stand_in


class UnencodableMessageTests(unittest.TestCase):
    """A message too deep to encode was never sent, and nothing was spent on it: the
    encode comes before the lookup, the credential read and the dial."""

    def test_a_message_too_deep_to_encode_is_never_sent(self):
        discovery = load_discovery()
        resolved, read, dialled = [], [], []
        marker = {"too-deep": True}
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "get_issue", "arguments": marker}}

        def getaddrinfo(host, *args, **kwargs):
            resolved.append(host)
            return [(2, 1, 6, "", ("172.28.0.11", 8082))]

        def read_secret(server):
            read.append(server)
            return "the-token"

        def urlopen(request, *args, **kwargs):
            dialled.append(request)
            raise AssertionError("the request was sent")

        discovery.read_secret = read_secret
        with mock.patch.object(discovery.json, "dumps",
                               _refusing_arguments(marker, json.dumps)), \
                mock.patch("socket.getaddrinfo", getaddrinfo), \
                mock.patch.object(discovery._OPENER, "open", urlopen), \
                self.assertRaises(discovery.DiscoveryError) as caught:
            discovery.post(ENTRY, message)
        self.assertEqual(caught.exception.kind, "transport-error")
        self.assertIn("so it was never sent", str(caught.exception))
        self.assertIn("will fail the same way", str(caught.exception))
        self.assertEqual((resolved, read, dialled), ([], [], []))

    def test_an_encoder_failure_other_than_depth_is_not_called_never_sent(self):
        # A TypeError or ValueError from the encoder is a gateway bug, not a property
        # of the agent's arguments, so it is not dressed up as one: it escapes `post`.
        discovery = load_discovery()
        marker = {"unencodable": True}
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "get_issue", "arguments": marker}}
        with mock.patch.object(discovery.json, "dumps",
                               _refusing_arguments(marker, json.dumps, TypeError)), \
                mock.patch.object(discovery._OPENER, "open",
                                  side_effect=AssertionError("the request was sent")), \
                self.assertRaises(TypeError) as caught:
            discovery.post(ENTRY, message)
        self.assertNotIsInstance(caught.exception, discovery.DiscoveryError)


class MalformedEntryTests(unittest.TestCase):
    """A tool entry of the wrong shape is dropped or read conservatively, and counted,
    at the read: on main a non-string name or non-object annotations raised past
    ``reconcile`` and stopped the round for every server."""

    def setUp(self):
        self.discovery = load_discovery()

    def test_an_entry_with_no_usable_name_is_dropped_and_counted(self):
        good = {"name": "get_issue"}
        for bad in ({"name": ["x"]}, {"name": 5}, "get_issue", {"description": "x"},
                    {"name": ""}):
            with self.subTest(entry=bad):
                tools, malformed = _list_tools_of(self.discovery, [good, bad, good])
                self.assertEqual([tool["name"] for tool in tools],
                                 ["get_issue", "get_issue"])
                self.assertEqual(malformed, {"entries": 1, "dropped": 1})

    def test_annotations_that_are_not_an_object_keep_the_tool_with_no_claim(self):
        # The operator's answer on the task: keep and count. Not read-only, so it is
        # denied unless a rule says otherwise.
        tools, malformed = _list_tools_of(
            self.discovery, [{"name": "get_issue", "annotations": "ro"},
                             {"name": "list_issues",
                              "annotations": {"readOnlyHint": True}}])
        self.assertEqual([(tool["name"], tool["annotations"]) for tool in tools],
                         [("get_issue", {"readOnlyHint": False}),
                          ("list_issues", {"readOnlyHint": True})])
        self.assertEqual(malformed, {"entries": 1, "dropped": 0})

    def test_absent_or_null_annotations_are_not_malformed(self):
        _tools, malformed = _list_tools_of(
            self.discovery, [{"name": "a"}, {"name": "b", "annotations": None}])
        self.assertEqual(malformed, {"entries": 0, "dropped": 0})

    def test_the_report_gives_both_counts_and_no_server_text(self):
        entries = [{"name": "get_issue"}, {"name": ["evil\nline"]},
                   {"name": "create_pr", "annotations": "evil-text"}]
        self.discovery.post = lambda *_a, **_k: response({"tools": entries})
        result = self.discovery.reconcile(ENTRY)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exposed"], 2)
        lines = [line for line in self.discovery.format_report([result])
                 if "malformed" in line]
        self.assertEqual(lines,
                         ["tool-gateway:   2 malformed tool entries in the reply, "
                          "1 of them dropped"])

    def test_a_well_formed_reply_prints_no_malformed_line(self):
        self.discovery.post = lambda *_a, **_k: response({"tools": [{"name": "get_issue"}]})
        report = "\n".join(self.discovery.format_report(
            [self.discovery.reconcile(ENTRY)]))
        self.assertNotIn("malformed", report)


class TrimTests(unittest.TestCase):
    """What survives a server's reply, and where each narrowing happens.

    There are TWO of them and they are different sizes, which is the thing worth
    guarding: the reply is trimmed once at the read, to the union of what both readers
    need, and again at the push, to the much smaller claim the control plane holds in
    memory. Collapsing them in either direction breaks something silently — trim only
    to the claim and the served tool list has no schema, so every tool is uncallable;
    push the read shape and third-party prose accumulates on the crown jewel."""

    def setUp(self):
        self.discovery = load_discovery()

    def _listed(self, tool: dict) -> dict:
        """``list_tools`` against a server that answers with exactly ``tool``."""
        tools, _malformed = _list_tools_of(self.discovery, [tool])
        return tools[0]

    def test_the_schema_survives_the_read(self):
        # The regression this pair was split for. An agent cannot call a tool whose
        # arguments it cannot see, so a trim that kept only the name and the read-only
        # claim produced a tool list that looked complete and was unusable.
        schema = {"type": "object", "properties": {"owner": {"type": "string"}}}
        listed = self._listed({"name": "get_issue", "description": "Read an issue.",
                               "inputSchema": schema})
        self.assertEqual(listed["inputSchema"], schema)
        self.assertEqual(listed["description"], "Read an issue.")

    def test_what_nobody_reads_is_dropped_at_the_read(self):
        # Inline base64 icons are most of a real reply by bytes (NOTES.md). Nothing
        # renders them here, and this process holds the result in memory between
        # reconciles.
        listed = self._listed({"name": "get_issue", "icons": ["data:image/png;base64,AA"],
                               "outputSchema": {"type": "object"}})
        self.assertNotIn("icons", listed)
        self.assertNotIn("outputSchema", listed)

    def test_a_tool_with_no_schema_is_served_rather_than_deleted(self):
        # Malformed by the spec, which makes it a judgement call: the honest reading is
        # "no arguments we know of", and dropping it silently would let a server remove
        # a tool from the operator's view by omitting a field.
        self.assertEqual(self._listed({"name": "ping"})["inputSchema"],
                         {"type": "object"})

    def test_the_push_carries_the_name_the_claim_and_a_digest_and_nothing_else(self):
        # What crosses to the control plane is what a person choosing a rule reads. A
        # description is third-party text, and the crown jewel then holds it; the
        # schema crosses only as its fixed-length digest.
        schema = {"type": "object", "properties": {"owner": {"type": "string"}}}
        claim = self.discovery._as_claim([
            {"name": "get_issue", "description": "Read an issue.",
             "inputSchema": schema, "annotations": {"readOnlyHint": True}}])
        self.assertEqual(claim, [{"name": "get_issue",
                                  "annotations": {"readOnlyHint": True},
                                  "digest": self.discovery.schema_digest(schema)}])


class AuthHeaderTests(unittest.TestCase):

    def setUp(self):
        self.discovery = load_discovery()

    def test_a_none_descriptor_sends_nothing(self):
        self.assertEqual(self.discovery.auth_header({"type": "none"}, None), {})

    def test_the_template_is_applied_with_no_per_server_branch(self):
        # DESIGN.md's claim is that header name and template cover every variation
        # that occurs with no GitHub-specific code. Asserted with a descriptor that is
        # deliberately not GitHub's, because a branch would still pass the other one.
        self.assertEqual(
            self.discovery.auth_header(
                {"type": "header", "header": "X-Api-Key", "template": "{secret}"},
                "abc123"),
            {"X-Api-Key": "abc123"})

    def test_a_header_descriptor_with_no_secret_is_an_error_not_an_empty_header(self):
        # Sending the header with an empty value would get a 401 back, which is
        # indistinguishable in a log from a revoked token. "Secret missing" is a
        # different operator action and has to stay a different message.
        with self.assertRaises(self.discovery.DiscoveryError) as caught:
            self.discovery.auth_header(ENTRY["auth"], None)
        said = str(caught.exception)
        self.assertIn("no file", said)
        # Names the header it wanted, so the message distinguishes "this server needs a
        # credential" from "this server's credential is wrong".
        self.assertIn("Authorization", said)


class SecretPathTests(unittest.TestCase):

    def test_the_filename_is_the_container_name_with_no_prefix_added(self):
        # The three-way agreement this had wrong: the server name IS the container
        # name (`mcp-github`), so prefixing `mcp-` again looks for
        # `mcp-mcp-github.json` — a file the Makefile's probe helper never writes.
        # Asserted by writing the file the Makefile documents and requiring it be the
        # one found.
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as secrets:
            with open(f"{secrets}/mcp-github.json", "w", encoding="utf-8") as handle:
                _json.dump({"token": "tok-abc"}, handle)
            discovery = load_discovery({"GATEWAY_SECRETS_DIR": secrets})
            self.assertEqual(discovery.read_secret("mcp-github"), "tok-abc")

    def test_a_file_too_deep_to_parse_is_unreadable_not_an_escape(self):
        # RecursionError is a RuntimeError, not a ValueError. On main it escaped
        # `read_secret`, and through `post` reached `_run`'s catch-all as a call that
        # may have run. Raised by a stand-in, so the proof does not depend on the
        # interpreter's depth.
        import tempfile
        with tempfile.TemporaryDirectory() as secrets:
            with open(f"{secrets}/mcp-github.json", "w", encoding="utf-8") as handle:
                json.dump({"token": "tok-abc"}, handle)
            discovery = load_discovery({"GATEWAY_SECRETS_DIR": secrets})
            with mock.patch.object(discovery.json, "load",
                                   side_effect=RecursionError("too deep")), \
                    self.assertRaises(discovery.DiscoveryError) as caught:
                discovery.read_secret("mcp-github")
        self.assertIn(f"secret file {secrets}/mcp-github.json is unreadable",
                      str(caught.exception))
        self.assertEqual(caught.exception.kind, "transport-error")

    def test_a_name_that_is_not_a_dns_label_is_refused_before_it_becomes_a_path(self):
        # Validated at the point of USE, not only at registration. The control plane
        # checks this too, and that is exactly why a second check is cheap insurance:
        # these are separate processes, and this one turns the string into a
        # filesystem path and a URL.
        discovery = load_discovery({"GATEWAY_SECRETS_DIR": "/nonexistent-for-tests"})
        for bad in ("../../etc/passwd", "a/b", "", "Mcp-GitHub", "-leading"):
            with self.subTest(server=bad), self.assertRaises(discovery.DiscoveryError):
                discovery.read_secret(bad)

    def test_the_path_is_derived_from_the_name_and_nothing_else(self):
        # The cross-wiring guard. Nothing in the roster contributes a path component,
        # so a forged config write cannot point one server at another's credential.
        # Asserted by reading a directory that does not exist: the name must appear in
        # the path the module builds, and no other input may.
        discovery = load_discovery({"GATEWAY_SECRETS_DIR": "/nonexistent-for-tests"})
        self.assertIsNone(discovery.read_secret("mcp-github"))

    def test_a_file_with_no_token_key_is_not_reported_as_a_missing_file(self):
        # The real one, found by shipping it. A secrets file written to the OLD shape —
        # a descriptor with the token inlined into the template — parses fine and has
        # no `token`, and reporting that as "no secret is present" sends its reader
        # looking for a file that is sitting right there.
        import json as _json
        import tempfile
        stale = {"auth": {"type": "header", "header": "Authorization",
                          "template": "Bearer github_pat_redacted"}}
        with tempfile.TemporaryDirectory() as secrets:
            with open(f"{secrets}/mcp-github.json", "w", encoding="utf-8") as handle:
                _json.dump(stale, handle)
            discovery = load_discovery({"GATEWAY_SECRETS_DIR": secrets})
            with self.assertRaises(discovery.DiscoveryError) as caught:
                discovery.read_secret("mcp-github")
        said = str(caught.exception)
        self.assertIn("has no 'token'", said)
        # Names the file, so the fix is obvious from the line alone.
        self.assertIn("mcp-github.json", said)

    def test_a_token_that_cannot_be_sent_is_refused_naming_the_file(self):
        # Each of these read as a token on main and then raised inside http.client
        # (or auth_header, for the non-string), outside DiscoveryError.
        import json as _json
        import tempfile
        # "tök" is latin-1, which http.client could send: pins the ASCII half.
        for token in ("tök", "tök-ābc", "tok\nabc", "tok abc", 12345, ["tok"]):
            with self.subTest(token=token), tempfile.TemporaryDirectory() as secrets:
                with open(f"{secrets}/mcp-github.json", "w", encoding="utf-8") as handle:
                    _json.dump({"token": token}, handle)
                discovery = load_discovery({"GATEWAY_SECRETS_DIR": secrets})
                with self.assertRaises(discovery.DiscoveryError) as caught:
                    discovery.read_secret("mcp-github")
                said = str(caught.exception)
                self.assertIn(f"{secrets}/mcp-github.json", said)
                self.assertIn("cannot be sent", said)
                self.assertNotIn(str(token), said)

    def test_a_printable_token_still_reads(self):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as secrets:
            with open(f"{secrets}/mcp-github.json", "w", encoding="utf-8") as handle:
                _json.dump({"token": "github_pat_A1b2~!$"}, handle)
            discovery = load_discovery({"GATEWAY_SECRETS_DIR": secrets})
            self.assertEqual(discovery.read_secret("mcp-github"), "github_pat_A1b2~!$")

    def test_a_missing_file_still_names_the_path_it_looked_at(self):
        # The placeholder this printed before — a literal "<server>.json" — was
        # unresolvable by the person reading it, which is the one thing an error must
        # not be.
        discovery = load_discovery({"GATEWAY_SECRETS_DIR": "/nonexistent-for-tests"})
        with self.assertRaises(discovery.DiscoveryError) as caught:
            discovery.auth_header({"type": "header", "header": "Authorization"}, None,
                                  discovery.secret_path("mcp-github"))
        said = str(caught.exception)
        self.assertIn("/nonexistent-for-tests/mcp-github.json", said)
        self.assertNotIn("<server>", said)

    def test_a_missing_secret_is_none_rather_than_an_exception(self):
        # "Configured, secret missing" is a state to REPORT, not a crash: it is the
        # one that otherwise surfaces as an upstream 401 and reads like a policy
        # problem (DESIGN.md, "What the UI can say without ever seeing a value").
        discovery = load_discovery({"GATEWAY_SECRETS_DIR": "/nonexistent-for-tests"})
        self.assertIsNone(discovery.read_secret("anything"))


class ReconcileTests(unittest.TestCase):

    def setUp(self):
        self.discovery = load_discovery()

    def _reconcile(self, exposed):
        # Shaped like what list_tools now returns — trimmed tool objects, not bare
        # names — because the inventory push carries the server's read-only claim
        # alongside each name.
        self.discovery.list_tools = lambda *_a, **_k: (
            [{"name": n, "annotations": {"readOnlyHint": False}} for n in exposed],
            {"entries": 0, "dropped": 0})
        return self.discovery.reconcile(ENTRY)

    def test_the_two_directions_are_separate_findings(self):
        # Each names a different operator action — delete a dead rule, or write a
        # missing one — so they are never summed into a "mismatch" count.
        result = self._reconcile(["get_issue", "merge_pull_request"])
        self.assertEqual(result["ruled_but_absent"], ["create_pr"])
        self.assertEqual(result["exposed_but_unruled"], ["merge_pull_request"])

    def test_agreement_reports_neither(self):
        result = self._reconcile(["create_pr", "get_issue"])
        self.assertEqual(result["ruled_but_absent"], [])
        self.assertEqual(result["exposed_but_unruled"], [])
        self.assertIn("agree", "\n".join(self.discovery.format_report([result])))

    def test_a_deny_rule_still_counts_as_ruled(self):
        # Presentation and enforcement are separate axes: a `deny` row is a decision
        # an operator made, so the tool is not "unruled" and must not be reported as
        # needing a rule. Every rule ships on the roster, deny rows included.
        self.discovery.list_tools = lambda *_a, **_k: (
            [{"name": "dangerous", "annotations": {}}], {"entries": 0, "dropped": 0})
        result = self.discovery.reconcile(
            {"server": "s", "auth": {}, "tools": [{"tool": "dangerous",
                                                   "action": "deny"}]})
        self.assertEqual(result["exposed_but_unruled"], [])

    def test_an_unreachable_server_is_a_line_not_a_crash(self):
        # One bad server must not stop the others being reported, and "unreachable"
        # is itself a finding worth seeing beside the rest.
        def boom(*_a, **_k):
            raise self.discovery.DiscoveryError("unreachable: connection refused")
        self.discovery.list_tools = boom
        report = "\n".join(self.discovery.format_report([self.discovery.reconcile(ENTRY)]))
        self.assertIn("NOT ENUMERATED", report)
        self.assertIn("connection refused", report)

    def test_one_server_raising_leaves_the_others_enumerated(self):
        # On main reconcile_all was a list comprehension with no per-server catch, so
        # either of these ended the round and nothing was published or pushed.
        good = {**ENTRY, "server": "mcp-good"}
        raising = {**ENTRY, "server": "mcp-raises"}
        deep = {**ENTRY, "server": "mcp-deep"}
        def post(entry, *_a, **_k):
            if entry["server"] == "mcp-raises":
                raise RuntimeError("something nobody listed")
            if entry["server"] == "mcp-deep":
                # What json.loads raises on a reply nested too deep for it. Raised
                # here rather than provoked, because the depth at which json.loads
                # raises depends on the Python build, and the guard's job is to catch
                # it whatever raises it.
                raise RecursionError("maximum recursion depth exceeded")
            return response({"tools": [{"name": "get_issue"}]})
        self.discovery.post = post
        results = self.discovery.reconcile_all([raising, good, deep])
        by_server = {result["server"]: result for result in results}
        self.assertEqual([r["server"] for r in results],
                         ["mcp-raises", "mcp-good", "mcp-deep"])
        self.assertEqual(by_server["mcp-good"]["status"], "ok")
        self.assertEqual([t["name"] for t in by_server["mcp-good"]["tools"]],
                         ["get_issue"])
        for server, kind in (("mcp-raises", "RuntimeError"),
                             ("mcp-deep", "RecursionError")):
            with self.subTest(server=server):
                self.assertNotIn("tools", by_server[server])
                self.assertIn(kind, by_server[server]["status"])
        report = "\n".join(self.discovery.format_report(results))
        self.assertIn("mcp-raises: NOT ENUMERATED", report)
        self.assertIn("mcp-deep: NOT ENUMERATED", report)
        self.assertIn("mcp-good: 1 tool exposed", report)

    def test_a_template_that_cannot_be_sent_is_refused_without_the_secret(self):
        # http.client's own refusal quotes the whole header value, token included, and
        # the status is pushed to the control plane and served from it.
        import tempfile
        secret = "ghp_" + "S" * 36
        opened = []
        for template in ("Bearer\n{secret}", "Bearer\r{secret}", "Bearer ✓{secret}"):
            with self.subTest(template=template), \
                    tempfile.TemporaryDirectory() as secrets:
                with open(f"{secrets}/mcp-github.json", "w", encoding="utf-8") as handle:
                    json.dump({"token": secret}, handle)
                discovery = load_discovery({"GATEWAY_SECRETS_DIR": secrets})
                entry = {**ENTRY, "auth": {"type": "header", "header": "Authorization",
                                           "template": template}}
                with mock.patch.object(discovery._OPENER, "open",
                                       lambda *a, **k: opened.append(a)), \
                        mock.patch("socket.getaddrinfo", _resolves_to("172.28.0.11")):
                    [result] = discovery.reconcile_all([entry])
                self.assertNotIn("tools", result)
                self.assertNotIn(secret, result["status"])
                self.assertNotIn("Bearer", result["status"])
                self.assertIn("cannot be sent", result["status"])
                self.assertIn(f"{secrets}/mcp-github.json", result["status"])
        self.assertEqual(opened, [])

    def test_an_empty_roster_says_so_rather_than_printing_nothing(self):
        # Silence is ambiguous: it reads the same as a broken reporter. The whole
        # value of this module is that a reader can tell the difference.
        self.assertIn("no enabled servers",
                      "\n".join(self.discovery.format_report([])))


#: A backslash, written so that no expected line below relies on a Python escape
#: meaning what the printer's does.
BS = chr(92)


class ReportPrintTests(unittest.TestCase):
    """The report on the gateway's stdout, where a server's own text lands: its error
    as a status, its tool names. Every assertion is a whole line, so a different
    escaping, or a cap, fails it rather than slipping past a substring."""

    def setUp(self):
        self.discovery = load_discovery()

    def _print(self, results: list[dict]) -> str:
        # Strict UTF-8, which is stricter than the container's surrogateescape stdout:
        # that one writes U+DC80 to U+DCFF as raw bytes and raises on the rest, so
        # anything this harness accepts, the gateway's stdout accepts too.
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
        with mock.patch("sys.stdout", out):
            for line in self.discovery.format_report(results):
                print(line)
        out.flush()
        return raw.getvalue().decode()

    def test_plain_text_prints_as_it_is_and_uncapped(self):
        # The cap is the outcome stream's, sized for its file; a status on the report
        # is shown whole. Fixed well above that cap's default of 512, so the pin holds
        # on code that never imported outcomes, as well as on code that does.
        status = "unreachable: " + "x" * 4096
        results = [{"server": "mcp-github", "status": "ok", "rules": 2, "exposed": 2,
                    "ruled_but_absent": ["create_pr"],
                    "exposed_but_unruled": ["merge_pull_request", "push"]},
                   {"server": "mcp-widgets", "status": status, "rules": 1}]
        self.assertEqual(self.discovery.format_report(results), [
            "tool-gateway: mcp-github: 2 tools exposed, 2 rules",
            "tool-gateway:   rules for tools the server does not expose (dead policy): "
            "create_pr",
            "tool-gateway:   tools with no rule (denied by default): "
            "merge_pull_request, push",
            f"tool-gateway: mcp-widgets: NOT ENUMERATED — {status}"])

    def test_a_servers_error_text_prints_escaped_and_cannot_fail_the_print(self):
        # A high surrogate, a low one outside surrogateescape's range, an ESC and a
        # newline: the first two would raise on the container's stdout, the last two
        # would rewrite the operator's terminal. The name is the roster's raw value,
        # which check_name's refusal keeps on a NOT ENUMERATED result.
        result = {"server": "bad" + chr(0xDC00),
                  "status": ("server returned an error: " + chr(0xD800) + chr(0xDD00)
                             + chr(0x1B) + "[2J" + "\nforged"),
                  "rules": 0}
        self.assertEqual(self._print([result]),
                         f"tool-gateway: bad{BS}udc00: NOT ENUMERATED — server returned "
                         f"an error: {BS}ud800{BS}udd00{BS}x1b[2J{BS}nforged\n")

    def test_a_servers_tool_names_print_escaped_on_both_lists(self):
        result = {"server": "mcp-github", "status": "ok", "rules": 2, "exposed": 2,
                  "ruled_but_absent": ["dead" + chr(0xD801), "gone\r"],
                  "exposed_but_unruled": ["new" + chr(0xDE00), "x" + chr(0x9B) + "y"]}
        self.assertEqual(self._print([result]),
                         "tool-gateway: mcp-github: 2 tools exposed, 2 rules\n"
                         "tool-gateway:   rules for tools the server does not expose "
                         f"(dead policy): dead{BS}ud801, gone{BS}r\n"
                         "tool-gateway:   tools with no rule (denied by default): "
                         f"new{BS}ude00, x{BS}x9by\n")

    def test_each_name_is_printed_on_its_own_so_a_non_string_one_still_prints(self):
        # Escaped per name, before the join: a join over a name that is not a string
        # raises, and the push after the report never happens.
        result = {"server": "mcp-github", "status": "ok", "rules": 0, "exposed": 1,
                  "ruled_but_absent": [], "exposed_but_unruled": [5]}
        self.assertEqual(self.discovery.format_report([result])[1],
                         "tool-gateway:   tools with no rule (denied by default): 5")

    def test_a_result_with_no_server_name_prints_none(self):
        result = {"server": None, "status": "a roster entry with no name", "rules": 0}
        self.assertEqual(self.discovery.format_report([result]),
                         ["tool-gateway: None: NOT ENUMERATED — a roster entry with "
                          "no name"])


class DigestTests(unittest.TestCase):
    """What counts as "the roster changed", which is what paces the whole loop."""

    def setUp(self):
        self.discovery = load_discovery()

    def test_a_flipped_rule_action_changes_the_digest(self):
        # The case a server-name digest would miss. `ask` becoming `deny` changes the
        # report without changing which servers are dialled — and if it did not
        # re-trigger, the operator's edit would appear to have done nothing until the
        # backstop tick.
        other = {**ENTRY, "tools": [{"tool": "get_issue", "action": "allow"},
                                    {"tool": "create_pr", "action": "deny"}]}
        self.assertNotEqual(self.discovery.roster_digest([ENTRY]),
                            self.discovery.roster_digest([other]))

    def test_an_auth_descriptor_edit_changes_the_digest(self):
        # It changes HOW a server is dialled, so the next report may differ even though
        # nothing about the tool rules moved.
        other = {**ENTRY, "auth": {"type": "none", "header": None, "template": None}}
        self.assertNotEqual(self.discovery.roster_digest([ENTRY]),
                            self.discovery.roster_digest([other]))

    def test_an_endpoint_edit_changes_the_digest(self):
        # Where a server is dialled, so a server re-pointed at its right path is asked
        # again at once rather than at the backstop.
        other = {**ENTRY, "endpoint": {"port": 8082, "path": "/v1/mcp"}}
        self.assertNotEqual(self.discovery.roster_digest([ENTRY]),
                            self.discovery.roster_digest([other]))

    def test_key_order_is_not_a_change(self):
        # JSON object order is not meaningful and the control plane is free to change
        # it. Without sorting, an unrelated backend edit would look like a roster
        # change forever and the loop would re-dial every server on every poll.
        reordered = {"tools": ENTRY["tools"], "auth": ENTRY["auth"],
                     "endpoint": ENTRY["endpoint"], "server": ENTRY["server"]}
        self.assertEqual(self.discovery.roster_digest([ENTRY]),
                         self.discovery.roster_digest([reordered]))


#: A dispatcher in miniature: one tool, a required `method` enum, nested schemas
#: under each keyword the digest recurses through.
_DISPATCHER = {
    "type": "object",
    "description": "Read a pull request.",
    "title": "pull_request_read",
    "properties": {
        "method": {"type": "string", "enum": ["get", "get_diff"],
                   "description": "Which read."},
        "owner": {"type": "string", "description": "Repository owner.",
                  "examples": ["octo"]},
        "labels": {"type": "array",
                   "items": {"type": "string", "enum": ["bug", "docs"],
                             "description": "A label."}},
        "target": {"anyOf": [{"type": "string", "enum": ["head", "base"],
                              "description": "A ref."},
                             {"$ref": "#/$defs/sha"}]},
    },
    "required": ["method", "owner"],
    "$defs": {"sha": {"type": "string", "enum": ["HEAD"], "$comment": "short"}},
}


class SchemaDigestTests(unittest.TestCase):
    """What moves a tool's schema digest, and what must not.

    Two failure directions. A digest that misses a structural change lets a plain
    `allow` on a dispatcher widen with nothing reported; one that moves on a rewording
    raises a change row for every upstream copy edit, and a row nobody believes is a
    row nobody reads."""

    def setUp(self):
        self.discovery = load_discovery()

    def _digest(self, schema) -> str:
        return self.discovery.schema_digest(schema)

    def _with(self, edit) -> dict:
        schema = json.loads(json.dumps(_DISPATCHER))
        edit(schema)
        return schema

    def assertMoves(self, edit):
        self.assertNotEqual(self._digest(_DISPATCHER), self._digest(self._with(edit)))

    def assertStays(self, edit):
        self.assertEqual(self._digest(_DISPATCHER), self._digest(self._with(edit)))

    def test_it_is_a_sha256_in_hex(self):
        # The shape `inventory._DIGEST_RE` keeps; anything else is dropped there.
        self.assertRegex(self._digest(_DISPATCHER), r"\A[0-9a-f]{64}\Z")

    def test_an_enum_gaining_a_value_moves_it(self):
        # THE case: a dispatcher's new method, which a plain `allow` takes on silently.
        self.assertMoves(lambda s: s["properties"]["method"]["enum"].append("merge"))

    def test_a_changed_type_moves_it(self):
        self.assertMoves(lambda s: s["properties"]["owner"].update(type="integer"))

    def test_a_field_becoming_required_moves_it(self):
        self.assertMoves(lambda s: s["required"].append("labels"))

    def test_an_enum_value_added_in_a_nested_schema_moves_it(self):
        for where, edit in (
                ("items", lambda s: s["properties"]["labels"]["items"]["enum"]
                 .append("security")),
                ("anyOf", lambda s: s["properties"]["target"]["anyOf"][0]["enum"]
                 .append("merge")),
                ("$defs", lambda s: s["$defs"]["sha"]["enum"].append("ORIG_HEAD"))):
            with self.subTest(where=where):
                self.assertMoves(edit)

    def test_an_x_keyword_is_structure(self):
        # `x-mcp-header` routes an argument somewhere else entirely. An unknown keyword
        # is kept whole, prose-named contents included, since nothing here knows what
        # it means: erring toward drift, never toward silence.
        self.assertMoves(lambda s: s["properties"]["owner"].update(
            {"x-mcp-header": "X-Owner"}))
        with_x = self._with(lambda s: s.update({"x-meta": {"description": "a"}}))
        reworded = self._with(lambda s: s.update({"x-meta": {"description": "b"}}))
        self.assertNotEqual(self._digest(with_x), self._digest(reworded))

    def test_a_default_change_moves_it(self):
        # A default is what the server does when the agent says nothing, so it is
        # what the call does.
        self.assertMoves(lambda s: s["properties"]["method"].update(default="get_diff"))
        before = self._with(lambda s: s["properties"]["method"].update(default="get"))
        after = self._with(lambda s: s["properties"]["method"].update(default="get_diff"))
        self.assertNotEqual(self._digest(before), self._digest(after))

    def test_a_description_key_inside_a_const_or_default_is_data(self):
        # Object-valued data that happens to hold a `description` key. Stripping it
        # there would hide a change to what the call sends.
        for keyword in ("const", "default"):
            with self.subTest(keyword=keyword):
                before = self._with(lambda s, k=keyword: s["properties"].update(
                    body={"type": "object", k: {"description": "draft"}}))
                after = self._with(lambda s, k=keyword: s["properties"].update(
                    body={"type": "object", k: {"description": "ready"}}))
                self.assertNotEqual(self._digest(before), self._digest(after))

    def test_the_dependency_maps_keep_their_names(self):
        # `dependentRequired` and draft-07 `dependencies` key on a property NAME, which
        # may well be `description`; the subschema form still loses its prose.
        for keyword in ("dependentRequired", "dependencies"):
            with self.subTest(keyword=keyword):
                empty = self._with(lambda s, k=keyword: s.update({k: {}}))
                named = self._with(lambda s, k=keyword: s.update(
                    {k: {"description": ["title"]}}))
                self.assertNotEqual(self._digest(empty), self._digest(named))
        with_schema = self._with(lambda s: s.update(
            {"dependencies": {"owner": {"description": "a", "required": ["labels"]}}}))
        reworded = self._with(lambda s: s.update(
            {"dependencies": {"owner": {"description": "b", "required": ["labels"]}}}))
        self.assertEqual(self._digest(with_schema), self._digest(reworded))

    def test_rewording_any_prose_does_not_move_it(self):
        for where, edit in (
                ("top", lambda s: s.update(description="Fetch a PR.", title="read")),
                ("property", lambda s: s["properties"]["method"].update(
                    description="The operation.")),
                ("examples", lambda s: s["properties"]["owner"].update(
                    examples=["someone"])),
                ("items", lambda s: s["properties"]["labels"]["items"].update(
                    description="Label name.")),
                ("anyOf", lambda s: s["properties"]["target"]["anyOf"][0].update(
                    description="Ref name.")),
                ("$defs", lambda s: s["$defs"]["sha"].update({"$comment": "long"}))):
            with self.subTest(where=where):
                self.assertStays(edit)

    def test_order_does_not_move_it(self):
        reordered = {key: _DISPATCHER[key] for key in reversed(list(_DISPATCHER))}
        self.assertEqual(self._digest(_DISPATCHER), self._digest(reordered))
        self.assertStays(lambda s: s["required"].reverse())
        self.assertStays(lambda s: s["properties"]["method"]["enum"].reverse())
        typed = self._with(lambda s: s["properties"]["owner"].update(
            type=["string", "null"]))
        retyped = self._with(lambda s: s["properties"]["owner"].update(
            type=["null", "string"]))
        self.assertEqual(self._digest(typed), self._digest(retyped))

    def test_prose_names_in_name_and_data_positions_are_kept(self):
        # `description` and `title` are prose only as KEYWORDS. As a property, a
        # `$defs` or a `patternProperties` name they are arguments, and as an enum or
        # `const` value they are what the call sends.
        for where, edit in (
                ("properties", lambda s: s["properties"].update(
                    description={"type": "string"})),
                ("$defs", lambda s: s["$defs"].update(title={"type": "string"})),
                ("patternProperties", lambda s: s.update(
                    patternProperties={"description": {"type": "string"}})),
                ("enum", lambda s: s["properties"]["method"]["enum"].append("title")),
                ("const", lambda s: s["properties"]["owner"].update(
                    const="description"))):
            with self.subTest(where=where):
                self.assertMoves(edit)
        named = self._with(lambda s: s["properties"].update(
            description={"type": "string", "description": "a"}))
        reworded = self._with(lambda s: s["properties"].update(
            description={"type": "string", "description": "b"}))
        self.assertEqual(self._digest(named), self._digest(reworded))


class DeepSchemaPushTests(unittest.TestCase):
    """One server's schema must not cost every other server its audit rows. A schema
    `json` parses can be nested past what a recursive walk or `json.dumps` will go;
    raised, that ended the push, and with it the record of every other server's move.
    Nested relative to the recursion limit, so no particular threshold is assumed."""

    def setUp(self):
        self.discovery = load_discovery()

    @staticmethod
    def _deep(levels: int = 0) -> dict:
        schema: dict = {"type": "string"}
        for _ in range(levels or sys.getrecursionlimit() * 2):
            schema = {"type": "object", "properties": {"x": schema}}
        return schema

    def _pushed(self, results) -> dict:
        sent = []

        @contextlib.contextmanager
        def urlopen(request, *_args, **_kwargs):
            sent.append(json.loads(request.data))
            yield mock.Mock(read=lambda: b'{"ok": true}')

        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(self.discovery.push_inventory(results), "")
        return sent[0]

    @staticmethod
    def _result(server, schema) -> dict:
        return {"server": server, "status": "ok",
                "tools": [{"name": "pull_request_read", "annotations": {},
                           "inputSchema": schema}]}

    def test_a_schema_too_deep_to_encode_gets_the_fixed_digest(self):
        # The depth `json.dumps` gives up at is the interpreter's, so it is found
        # rather than assumed.
        levels = sys.getrecursionlimit()
        while True:
            schema = self._deep(levels)
            try:
                json.dumps(schema)
            except RecursionError:
                break
            if levels >= 1 << 17:
                self.skipTest(f"json.dumps encoded {levels} levels; nothing to test")
            levels *= 2
        self.assertEqual(self.discovery.schema_digest(schema),
                         self.discovery.UNDIGESTIBLE)
        self.assertRegex(self.discovery.UNDIGESTIBLE, r"\A[0-9a-f]{64}\Z")

    def test_a_schema_too_deep_to_walk_is_still_hashed_whole(self):
        # Between what `_structure` can walk and what `json.dumps` can encode, a deep
        # schema is hashed with its prose, so a widening inside it still moves the
        # digest. The recursion limit is lowered to just above this frame, which
        # bounds the Python walk and not the C encoder, so the band exists whatever
        # the interpreter's defaults.
        depth, here = 200, 0
        frame = sys._getframe()
        while frame is not None:
            here, frame = here + 1, frame.f_back

        def deep(values):
            schema: dict = {"type": "string", "enum": values}
            for _ in range(depth):
                schema = {"type": "object", "properties": {"x": schema}}
            return schema

        before, after = deep(["get"]), deep(["get", "merge"])
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(here + depth // 2)
        try:
            with self.assertRaises(RecursionError):
                self.discovery._structure(before)
            json.dumps(before, sort_keys=True)
            digests = [self.discovery.schema_digest(s) for s in (before, after)]
        finally:
            sys.setrecursionlimit(limit)
        self.assertNotIn(self.discovery.UNDIGESTIBLE, digests)
        self.assertNotEqual(digests[0], digests[1])

    def test_a_deep_schema_on_one_server_does_not_silence_another(self):
        normal = {"type": "object", "properties": {"method": {"enum": ["get"]}}}
        grown = {"type": "object", "properties": {"method": {"enum": ["get", "merge"]}}}
        inv = load_inventory()
        inv.record(self._pushed([self._result("mcp-hostile", {"type": "object"}),
                                 self._result("mcp-github", normal)]))
        _, moved = inv.record(self._pushed([self._result("mcp-hostile", self._deep()),
                                            self._result("mcp-github", grown)]))
        # Both moves are reported: the normal server's, and the deep one going into the
        # undigestible state, which errs toward drift rather than silence.
        self.assertEqual(sorted(moved), [
            ("mcp-github", "mcp-github changed the arguments of pull_request_read"),
            ("mcp-hostile", "mcp-hostile changed the arguments of pull_request_read")])
        _, moved = inv.record(self._pushed([self._result("mcp-hostile", self._deep()),
                                            self._result("mcp-github", grown)]))
        self.assertEqual(moved, [])


class PollTests(unittest.TestCase):

    def test_an_unreachable_control_plane_is_reported_and_not_raised(self):
        # The gateway must come up and stay up whether or not the authority answers —
        # the reasoning compose states for not gating on service_healthy. A diagnostic
        # thread that dies on a restart of the control plane would be worse than none,
        # because it would stay dead after the control plane came back.
        discovery = load_discovery(
            {"GATEWAY_CONTROL_URL": "http://127.0.0.1:1",
             "GATEWAY_DISCOVERY_TIMEOUT": "0.2"})
        roster, failure = discovery.poll()
        self.assertIsNone(roster)
        self.assertIn("roster unavailable", failure)

    def test_polling_the_roster_dials_no_server(self):
        # The split only pays if the cheap call stays cheap. If poll() ever enumerated,
        # the ten-second cadence would land on every container holding a credential,
        # which is the cost the two rates exist to avoid.
        discovery = load_discovery()
        def boom(*_a, **_k):
            raise AssertionError("poll() dialled a server")
        discovery.list_tools = boom
        discovery.fetch_roster = lambda: [dict(ENTRY)]
        roster, failure = discovery.poll()
        self.assertEqual(failure, "")
        self.assertEqual(len(roster), 1)

    def test_a_control_plane_answering_badly_is_reported_and_not_raised(self):
        # Escaped, these reached the reconcile loop's guard: "reconcile failed" every
        # tick, and the flip into failure never announced as one.
        for name, (fault, detail) in CONTROL_FAULTS.items():
            with self.subTest(name):
                discovery = load_discovery()
                with control_plane_answering(discovery, **fault):
                    roster, failure = discovery.poll()
                self.assertIsNone(roster)
                self.assertEqual(failure, f"tool-gateway: roster unavailable from "
                                          f"{discovery.CONTROL_URL} — {detail}")

    def test_a_roster_that_is_not_a_list_is_reported_and_not_raised(self):
        # A parsed body that is not a list is an outage with a reason, never a
        # roster. The float is there so a check listing bad types cannot pass.
        for reply, kind in ((b"null", "NoneType"), (b"{}", "dict"),
                            (b'"x"', "str"), (b"1", "int"), (b"1.5", "float")):
            with self.subTest(reply):
                discovery = load_discovery()
                with control_plane_answering(discovery, reply=reply):
                    roster, failure = discovery.poll()
                self.assertIsNone(roster)
                self.assertEqual(failure, f"tool-gateway: roster unavailable from "
                                          f"{discovery.CONTROL_URL} — a {kind} where a "
                                          f"list was expected")

    def test_an_empty_roster_is_an_answer_and_not_an_outage(self):
        # No servers enabled is a real roster: the check refuses a shape, not a size.
        discovery = load_discovery()
        with control_plane_answering(discovery, reply=b"[]"):
            self.assertEqual(discovery.poll(), ([], ""))

    def test_a_bug_in_fetching_the_roster_still_raises(self):
        # The catch names what the control plane can do wrong, so a bug here reaches
        # the loop guard under its own class rather than reading as an outage.
        discovery = load_discovery()

        def fetch_roster():
            raise TypeError("a bug")
        discovery.fetch_roster = fetch_roster
        with self.assertRaises(TypeError):
            discovery.poll()


#: One server's round, as ``push_inventory`` is handed it.
RESULTS = [{"server": "mcp-github", "status": "ok", "tools": []}]


class PushInventoryReplyTests(unittest.TestCase):
    """``push_inventory`` returns "" or a reason, whatever the control plane answers."""

    def test_a_control_plane_answering_badly_is_a_reason(self):
        for name, (fault, detail) in CONTROL_FAULTS.items():
            with self.subTest(name):
                discovery = load_discovery()
                with control_plane_answering(discovery, **fault):
                    failure = discovery.push_inventory(RESULTS)
                self.assertEqual(failure, f"tool-gateway: inventory not accepted by "
                                          f"{discovery.CONTROL_URL} — {detail}")

    def test_a_reply_that_is_not_an_object_is_a_reason(self):
        for reply, kind in ((b"[]", "list"), (b'"ok"', "str"), (b"null", "NoneType")):
            with self.subTest(reply):
                discovery = load_discovery()
                with control_plane_answering(discovery, reply=reply):
                    failure = discovery.push_inventory(RESULTS)
                self.assertEqual(failure, f"tool-gateway: inventory not accepted by "
                                          f"{discovery.CONTROL_URL} — a {kind} where an "
                                          f"object was expected")

    def test_an_accepted_push_is_no_reason(self):
        discovery = load_discovery()
        with control_plane_answering(discovery, reply=b'{"ok": true}'):
            self.assertEqual(discovery.push_inventory(RESULTS), "")

    def test_a_refused_push_is_the_refusal(self):
        discovery = load_discovery()
        with control_plane_answering(discovery, reply=b'{"ok": false, "detail": "x"}'):
            self.assertEqual(discovery.push_inventory(RESULTS),
                             "tool-gateway: inventory refused — x")

    def test_an_empty_reply_is_a_refusal_with_no_reason(self):
        # An empty body reads as `{}`, not as a body that will not parse.
        discovery = load_discovery()
        with control_plane_answering(discovery, reply=b""):
            self.assertEqual(discovery.push_inventory(RESULTS),
                             "tool-gateway: inventory refused — no reason given")

    def test_a_bug_in_pushing_still_raises(self):
        discovery = load_discovery()
        with control_plane_answering(discovery, on_open=TypeError("a bug")), \
                self.assertRaises(TypeError):
            discovery.push_inventory(RESULTS)


if __name__ == "__main__":
    unittest.main()
