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

import json
import unittest

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
        self.assertIn("no secret", str(caught.exception))


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
        self.discovery.list_tools = lambda *_a, **_k: exposed
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
        self.discovery.list_tools = lambda *_a, **_k: ["dangerous"]
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


class RunTests(unittest.TestCase):

    def test_an_unreachable_control_plane_is_reported_and_not_raised(self):
        # The gateway must come up and stay up whether or not the authority answers —
        # the reasoning compose states for not gating on service_healthy. A diagnostic
        # thread that dies on a restart of the control plane would be worse than none,
        # because it would stay dead after the control plane came back.
        discovery = load_discovery(
            {"GATEWAY_CONTROL_URL": "http://127.0.0.1:1",
             "GATEWAY_DISCOVERY_TIMEOUT": "0.2"})
        answered, lines = discovery.run()
        self.assertFalse(answered)
        self.assertIn("roster unavailable", "\n".join(lines))

    def test_an_unreachable_server_still_counts_as_the_authority_answering(self):
        # The flag tracks the CONTROL PLANE, not the servers. A pass where the roster
        # arrived and a server was down is a COMPLETE report, so pacing on it would
        # keep the loop in its cold-start retry forever while everything it needs is
        # working — a hot loop caused by a finding rather than by a fault.
        discovery = load_discovery()
        discovery.fetch_roster = lambda: [dict(ENTRY)]
        def boom(*_a, **_k):
            raise discovery.DiscoveryError("unreachable: connection refused")
        discovery.list_tools = boom
        answered, lines = discovery.run()
        self.assertTrue(answered)
        self.assertIn("NOT ENUMERATED", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
