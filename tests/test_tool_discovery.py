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
import json
import unittest
from unittest import mock

from _loader import load_discovery

#: A roster entry as ``/tool/roster`` serves one, trimmed to what this module reads.
#: Written out rather than imported from the control plane: these two are separate
#: processes that agree by contract, and a shared constant would make a test pass on
#: a contract neither side actually implements.
ENTRY = {"server": "mcp-github",
         "auth": {"type": "header", "header": "Authorization",
                  "template": "Bearer {secret}"},
         "tools": [{"tool": "get_issue", "action": "allow"},
                   {"tool": "create_pr", "action": "ask"}]}


def sse(result: dict) -> str:
    """An MCP reply in the shape the server actually sends — SSE, not a JSON body."""
    return f"data: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result})}\n\n"


class ParseTests(unittest.TestCase):

    def setUp(self):
        self.discovery = load_discovery()

    def test_the_sse_shape_is_parsed(self):
        # The measured shape (NOTES.md). If this breaks, enumeration is broken.
        tools = self.discovery.parse_tools(sse({"tools": [{"name": "get_issue"}]}))
        self.assertEqual([t["name"] for t in tools], ["get_issue"])

    def test_a_plain_json_body_is_also_parsed(self):
        # Legal for this transport and not what the server sends today. Accepted so a
        # version bump cannot silently empty the report — the failure would look like
        # "the server exposes no tools", which is a sentence this report is supposed
        # to be trusted about.
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "result": {"tools": [{"name": "get_issue"}]}})
        self.assertEqual([t["name"] for t in self.discovery.parse_tools(body)],
                         ["get_issue"])

    def test_prose_is_reported_as_prose(self):
        # The bad-bearer failure mode: a bare line of text, no JSON. The parse error
        # is the symptom and the text is the diagnosis, so the text has to survive
        # into the message or an operator is left debugging a JSONDecodeError.
        with self.assertRaises(self.discovery.DiscoveryError) as caught:
            self.discovery.parse_tools("Missing Authorization header")
        self.assertIn("Missing Authorization header", str(caught.exception))

    def test_a_jsonrpc_error_is_not_an_empty_tool_list(self):
        # The one that matters most. `result` is absent on an error reply, so a naive
        # reader returns [] and the report says the server exposes nothing — which
        # would read as "every rule is dead policy" and invite someone to delete them.
        with self.assertRaises(self.discovery.DiscoveryError) as caught:
            self.discovery.parse_tools(
                'data: {"jsonrpc":"2.0","id":1,"error":{"code":-32601}}')
        self.assertIn("-32601", str(caught.exception))

    def test_an_empty_reply_is_an_error(self):
        with self.assertRaises(self.discovery.DiscoveryError):
            self.discovery.parse_tools("")

    def test_valid_json_that_is_not_an_object_is_an_error_not_a_crash(self):
        # `message.get(...)` on a list or a string raised AttributeError, which
        # `reconcile` does not catch — so one server answering oddly killed the
        # reconcile thread for the life of the process, healthcheck still green.
        for raw in ("[]", '"x"', "null", "7", "data: []\n\n"):
            with self.subTest(raw=raw), \
                    self.assertRaises(self.discovery.DiscoveryError):
                self.discovery.parse_tools(raw)

    def test_a_result_without_a_tool_list_is_an_error_not_an_empty_list(self):
        # `{"result": null}` and `{"result": []}` are not "this server exposes
        # nothing", which is the claim an empty list makes and which the inventory
        # push would then carry to the operator's picker.
        for result in (None, [], "tools", {"tools": None}, {"tools": {"a": 1}}):
            with self.subTest(result=result):
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})
                with self.assertRaises(self.discovery.DiscoveryError) as caught:
                    self.discovery.parse_tools(body)
                self.assertIn("no tool list", str(caught.exception))

    def test_a_notification_ahead_of_the_response_is_skipped(self):
        # Q4. A stream may carry progress or log notifications before the response;
        # taking the first event read one of those as the reply.
        raw = ('data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}\n\n'
               + sse({"tools": [{"name": "get_issue"}]}))
        self.assertEqual([t["name"] for t in self.discovery.parse_tools(raw)],
                         ["get_issue"])

    def test_a_request_from_the_server_is_not_taken_for_the_reply(self):
        # It has an id — possibly OUR id — and a `method`. The method is what marks it
        # as a request rather than the response to ours.
        raw = ('data: {"jsonrpc":"2.0","id":1,"method":"sampling/createMessage"}\n\n'
               + sse({"tools": [{"name": "get_issue"}]}))
        self.assertEqual([t["name"] for t in self.discovery.parse_tools(raw)],
                         ["get_issue"])

    def test_sse_is_read_by_the_spec_not_by_the_line(self):
        # The optional space, a message split over several data lines, CRLF endings,
        # and the fields that are not data. Each is legal SSE a server may send.
        body = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "x"}]}}
        text = json.dumps(body, indent=1)
        split = "".join(f"data:{line}\r\n" for line in text.splitlines())
        raw = (": a comment\r\nevent: message\r\nid: 7\r\n" + split + "\r\n")
        self.assertEqual([t["name"] for t in self.discovery.parse_tools(raw)], ["x"])

    def test_a_stream_that_never_answers_our_request_is_an_error(self):
        for raw in ('data: {"jsonrpc":"2.0","method":"notifications/progress",'
                    '"params":{"progress":1}}\n\n',
                    sse({"tools": []}).replace('"id": 1', '"id": 2')):
            with self.subTest(raw=raw), \
                    self.assertRaises(self.discovery.DiscoveryError) as caught:
                self.discovery.parse_tools(raw)
            self.assertIn("no response", str(caught.exception))

    def test_a_malformed_reply_is_a_line_in_the_report_not_a_dead_thread(self):
        # Through `reconcile`, which is what the loop calls: the shape error has to
        # arrive as the DiscoveryError it catches, and read as NOT ENUMERATED.
        self.discovery.post = lambda *_a, **_k: "[]"
        report = "\n".join(self.discovery.format_report([self.discovery.reconcile(ENTRY)]))
        self.assertIn("NOT ENUMERATED", report)
        self.assertIn("expected a JSON object", report)


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
        @contextlib.contextmanager
        def urlopen(*_args, **_kwargs):
            yield mock.Mock(read=lambda: sse({"tools": [tool]}).encode())

        with mock.patch("urllib.request.urlopen", urlopen):
            tools = self.discovery.list_tools("mcp-github", {"type": "none"})
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

    def test_the_push_carries_the_name_and_the_claim_and_nothing_else(self):
        # What crosses to the control plane is what a person choosing a rule reads. A
        # description is third-party text, and the crown jewel then holds it.
        claim = self.discovery._as_claim([
            {"name": "get_issue", "description": "Read an issue.",
             "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}])
        self.assertEqual(claim, [{"name": "get_issue",
                                  "annotations": {"readOnlyHint": True}}])


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
        self.discovery.list_tools = lambda *_a, **_k: [
            {"name": n, "annotations": {"readOnlyHint": False}} for n in exposed]
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
        self.discovery.list_tools = lambda *_a, **_k: [
            {"name": "dangerous", "annotations": {}}]
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

    def test_an_empty_roster_says_so_rather_than_printing_nothing(self):
        # Silence is ambiguous: it reads the same as a broken reporter. The whole
        # value of this module is that a reader can tell the difference.
        self.assertIn("no enabled servers",
                      "\n".join(self.discovery.format_report([])))


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

    def test_key_order_is_not_a_change(self):
        # JSON object order is not meaningful and the control plane is free to change
        # it. Without sorting, an unrelated backend edit would look like a roster
        # change forever and the loop would re-dial every server on every poll.
        reordered = {"tools": ENTRY["tools"], "auth": ENTRY["auth"],
                     "server": ENTRY["server"]}
        self.assertEqual(self.discovery.roster_digest([ENTRY]),
                         self.discovery.roster_digest([reordered]))


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


if __name__ == "__main__":
    unittest.main()
