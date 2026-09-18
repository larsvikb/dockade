# SPDX-License-Identifier: Apache-2.0
"""Guards over what the agent is shown, and over the wire that shows it.

Two modules, one file, because the question they answer together is one question: an
agent asks `tools/list` and gets back names it will later call. Splitting them would
let each pass while the join between them — a name that curation produced and the call
path cannot resolve — went unasserted.

WHAT IS NOT UNDER TEST HERE is worth naming, because this file would otherwise read as
if it were about security. Curation is PRESENTATION. Hiding a tool grants nothing and
showing one grants nothing; `policy._decide_tool` in the control plane decides every
call, whether or not the tool was ever listed, because a tool name can reach an agent
from a transcript or from text an earlier result injected. So the bar here is that the
list is RIGHT — an operator who wrote a rule sees its effect, and a name means exactly
one (server, tool) pair — rather than that it is a boundary.
"""
from __future__ import annotations

import unittest

from _loader import load_control_plane, load_protocol, load_surface

#: A roster as ``/tool/roster`` serves one. Written out rather than imported from the
#: control plane for the reason test_tool_discovery.py writes its own: the two are
#: separate processes that agree by contract, and a shared constant would let a test
#: pass on a contract neither side implements.
ROSTER = [{"server": "mcp-github",
           "auth": {"type": "none"},
           "tools": [{"tool": "get_issue", "action": "allow"},
                     {"tool": "create_pr", "action": "ask"},
                     {"tool": "delete_repo", "action": "deny"},
                     {"tool": "typoed_name", "action": "allow"}]}]

#: What that server said when it was dialled. `push_files` is real and unruled;
#: `typoed_name` above is ruled and absent. Both are ordinary steady states.
EXPOSED = [{"name": "get_issue", "description": "Read an issue.",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "number"}}},
            "annotations": {"readOnlyHint": True}},
           {"name": "create_pr", "description": "Open a pull request.",
            "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": False}},
           {"name": "delete_repo", "description": "Delete a repository.",
            "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": False}},
           {"name": "push_files", "description": "Push files.",
            "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": False}}]


def result(server: str = "mcp-github", tools: list[dict] | None = None) -> dict:
    """One ``discovery.reconcile`` result for a server that answered."""
    return {"server": server, "status": "ok", "rules": 0,
            "exposed": len(tools if tools is not None else EXPOSED),
            "tools": EXPOSED if tools is None else tools,
            "ruled_but_absent": [], "exposed_but_unruled": []}


def unreachable(server: str = "mcp-github") -> dict:
    """One result for a server that could not be dialled.

    The `tools` key is ABSENT rather than empty, which is the distinction ``reconcile``
    draws and the one ``publish`` reads: an empty list is a claim that the server
    exposes nothing, and a failed dial is not a claim at all."""
    return {"server": server, "status": "unreachable: connection refused", "rules": 0}


class NameTests(unittest.TestCase):
    """The encoding, and its inverse.

    This pair is the load-bearing part of the whole surface. The agent sees one MCP
    server, so several backing servers' tools share one namespace; a call arrives under
    a flattened name and has to resolve to exactly the (server, tool) the listing meant
    by it, or policy is read for one tool and run for another."""

    def setUp(self):
        self.surface = load_surface()

    def test_every_name_the_listing_produces_decodes_back_to_its_pair(self):
        # The property, asserted over the real curated output rather than over hand-
        # written strings: whatever curation can emit, the call path can resolve.
        listing = self.surface.curate(ROSTER, {"mcp-github": EXPOSED})
        self.assertTrue(listing)  # a vacuous pass here would hide everything below
        for tool in listing:
            server, name = self.surface.split_exposed(tool["name"])
            self.assertEqual(server, "mcp-github")
            self.assertEqual(tool["name"], self.surface.exposed_name(server, name))

    def test_a_tool_name_containing_the_separator_still_decodes(self):
        # The case a naive `split` gets wrong. It is safe only because a server name is
        # a DNS label and cannot contain `_` at all, so the FIRST separator is always
        # the join — which is a fact about `discovery.check_name`, not about this file.
        name = self.surface.exposed_name("mcp-github", "weird__tool__name")
        self.assertEqual(self.surface.split_exposed(name),
                         ("mcp-github", "weird__tool__name"))

    def test_the_other_legal_tool_punctuation_decodes_too(self):
        # `.`, `:` and `-` are all legal in `policy._TOOL_RE`, so any of them could have
        # been the separator and none of them may break it.
        for tool in ("a.b", "a:b", "a-b", "a_b"):
            with self.subTest(tool=tool):
                self.assertEqual(
                    self.surface.split_exposed(self.surface.exposed_name("s", tool)),
                    ("s", tool))

    def test_a_name_that_is_not_one_is_refused_rather_than_salvaged(self):
        # An agent can send any string. A decoder that guessed would be inventing the
        # identity policy is then keyed on.
        for bad in ("get_issue", "", "__get_issue", "mcp-github__", "__"):
            with self.subTest(name=bad):
                self.assertIsNone(self.surface.split_exposed(bad))

    def test_a_name_one_character_away_from_a_real_one_is_refused_not_normalised(self):
        # The control plane strips and lower-cases what it is asked about; the gateway
        # used to forward the raw halves. Trailing whitespace of any kind, a control
        # character, or an upper-case server would have been decided as one name and
        # dialled as another. Every one of these is refused before anything is asked.
        real = self.surface.exposed_name("mcp-github", "get_issue")
        for bad in (real + " ", real + "\t", real + "\u3000", real + "\x1f",
                    " " + real, "mcp-github__ get_issue", "MCP-GITHUB__get_issue",
                    "mcp_github__get_issue", "mcp-github__get issue",
                    "mcp-github__" + "x" * 129):
            with self.subTest(name=bad):
                self.assertIsNone(self.surface.split_exposed(bad))
        self.assertEqual(self.surface.split_exposed(real), ("mcp-github", "get_issue"))

    def test_the_name_patterns_are_the_control_planes_own(self):
        # Two images, no shared module: the only thing holding the gateway's idea of a
        # canonical name to the control plane's is this test. If they drift, the
        # gateway starts forwarding names the control plane normalises, which is the
        # exact gap the patterns exist to close.
        policy = load_control_plane().policy
        self.assertEqual(self.surface.SERVER_RE.pattern, policy._SERVER_RE.pattern)
        self.assertEqual(self.surface.TOOL_RE.pattern, policy._TOOL_RE.pattern)

    def test_two_servers_exposing_one_tool_name_stay_two_tools(self):
        # The collision the flattening exists to prevent. Without it, whichever server
        # was enumerated last would silently decide the other's policy.
        roster = [{"server": "mcp-github", "tools": [{"tool": "search", "action": "allow"}]},
                  {"server": "mcp-jira", "tools": [{"tool": "search", "action": "deny"}]}]
        listing = self.surface.curate(
            roster, {"mcp-github": [{"name": "search", "inputSchema": {}}],
                     "mcp-jira": [{"name": "search", "inputSchema": {}}]})
        self.assertEqual([t["name"] for t in listing], ["mcp-github__search"])


class CurationTests(unittest.TestCase):

    def setUp(self):
        self.surface = load_surface()
        self.listing = self.surface.curate(ROSTER, {"mcp-github": EXPOSED})
        self.names = [tool["name"] for tool in self.listing]

    def test_allow_and_ask_are_both_shown(self):
        # `ask` is on the list deliberately: it is a GRANT path, and hiding it would
        # mean the approval queue could never be reached by the agent that needs it.
        self.assertIn("mcp-github__get_issue", self.names)
        self.assertIn("mcp-github__create_pr", self.names)

    def test_deny_and_unruled_are_the_same_absence(self):
        # Not two mechanisms. An unconfigured tool is denied rather than held, so
        # "ruled deny" and "never ruled" produce the identical surface.
        self.assertNotIn("mcp-github__delete_repo", self.names)
        self.assertNotIn("mcp-github__push_files", self.names)

    def test_a_rule_for_a_tool_the_server_does_not_expose_shows_nothing(self):
        # `ruled_but_absent` — dead policy. There is no schema to present, so there is
        # nothing that could be presented even if it were live.
        self.assertNotIn("mcp-github__typoed_name", self.names)

    def test_the_description_and_schema_are_carried(self):
        # Without these the list is decorative: an agent cannot call a tool whose
        # arguments it cannot see.
        shown = next(t for t in self.listing if t["name"] == "mcp-github__get_issue")
        self.assertEqual(shown["description"], "Read an issue.")
        self.assertIn("properties", shown["inputSchema"])

    def test_an_ask_tool_says_so_in_its_description(self):
        # Otherwise an `ask` tool is indistinguishable from an `allow` one until it is
        # called, so the agent cannot do the gated work first and the unblocked work
        # while it waits, and cannot warn a human that a task will need them.
        shown = next(t for t in self.listing if t["name"] == "mcp-github__create_pr")
        self.assertTrue(shown["description"].startswith(self.surface.ASK_NOTICE))
        self.assertIn("Open a pull request.", shown["description"])

    def test_an_allow_tool_carries_no_notice(self):
        # The notice has to MEAN something. Put on everything, it stops being a signal
        # and becomes noise the agent learns to skip.
        shown = next(t for t in self.listing if t["name"] == "mcp-github__get_issue")
        self.assertEqual(shown["description"], "Read an issue.")

    def test_the_notice_names_the_tool_that_finishes_the_call(self):
        # A notice that said "this may need approval" and stopped would leave the agent
        # knowing it is stuck and not how to get unstuck.
        self.assertIn(self.surface.RESUME_TOOL, self.surface.ASK_NOTICE)

    def test_the_servers_annotations_do_not_reach_the_agent(self):
        # `readOnlyHint` is server-supplied and therefore untrusted. Forwarding it hands
        # a third party a lever on the client's own permission behaviour; its legitimate
        # home is the operator's picker, where a human reads it as a claim.
        for tool in self.listing:
            self.assertNotIn("annotations", tool)

    def test_the_order_is_stable_rather_than_enumeration_order(self):
        # A list that churned with enumeration order would move a session's tool set
        # for no reason an operator changed.
        reversed_listing = self.surface.curate(ROSTER,
                                               {"mcp-github": list(reversed(EXPOSED))})
        self.assertEqual([t["name"] for t in reversed_listing], self.names)

    def test_a_server_absent_from_the_roster_contributes_nothing(self):
        # The roster is the authority on which servers are enabled. Tools remembered
        # for a server an operator switched off must not be shown.
        listing = self.surface.curate([], {"mcp-github": EXPOSED})
        self.assertEqual(listing, [])


class PublicationTests(unittest.TestCase):
    """What survives between reconciles, and what does not."""

    def setUp(self):
        self.surface = load_surface()

    def proxied(self) -> list[str]:
        """The listed names that came from a server.

        The gateway's own tools are filtered out HERE rather than asserted around,
        because every case in this class is about the policy join and a native tool is
        not in it — it is proxied from no server and governed by no rule."""
        native = {tool["name"] for tool in self.surface.NATIVE_TOOLS}
        return [tool["name"] for tool in self.surface.listing()
                if tool["name"] not in native]

    def test_nothing_is_proxied_before_the_first_reconcile(self):
        # The correct cold answer, and a harmless one: an empty list is not a grant.
        self.assertEqual(self.proxied(), [])

    def test_the_gateways_own_tools_are_listed_without_any_server(self):
        # Resumption has to be reachable before anything has been enumerated, because
        # an agent can hold an approval id across a gateway restart — and it depends on
        # no server, so there is nothing for a reconcile to contribute to it.
        self.assertIn(self.surface.RESUME_TOOL,
                      [tool["name"] for tool in self.surface.listing()])

    def test_publishing_makes_the_listing_readable(self):
        self.surface.publish(ROSTER, [result()])
        self.assertIn("mcp-github__get_issue", self.proxied())

    def test_a_server_that_could_not_be_dialled_keeps_its_tools(self):
        # A restarting container or a briefly missing credential must not read to the
        # agent as a capability that was withdrawn — and keeping the entry buys no
        # risk, because execution is decided per call against the control plane.
        self.surface.publish(ROSTER, [result()])
        self.surface.publish(ROSTER, [unreachable()])
        self.assertIn("mcp-github__get_issue", self.proxied())

    def test_a_server_removed_from_the_roster_loses_its_tools(self):
        # A different event from a failed dial: this one is an operator's answer
        # (disabled or revoked), not a failure to ask.
        self.surface.publish(ROSTER, [result()])
        self.surface.publish([], [])
        self.assertEqual(self.proxied(), [])

    def test_a_rule_flipped_to_deny_leaves_the_listing_on_the_next_reconcile(self):
        # The operator-visible loop this whole step exists to close. A rule edit changes
        # the roster digest, which is what triggers the reconcile that republishes.
        self.surface.publish(ROSTER, [result()])
        denied = [{**ROSTER[0],
                   "tools": [{"tool": "get_issue", "action": "deny"}]}]
        self.surface.publish(denied, [result()])
        self.assertEqual(self.proxied(), [])

    def test_a_server_that_stops_exposing_a_tool_stops_listing_it(self):
        # The supply-chain direction: an image bump that REMOVES a tool. The rule
        # survives and decides nothing, which is `ruled_but_absent` in the report.
        self.surface.publish(ROSTER, [result()])
        self.surface.publish(ROSTER, [result(tools=[EXPOSED[1]])])
        self.assertEqual(self.proxied(), ["mcp-github__create_pr"])


class NativeToolTests(unittest.TestCase):
    """The gateway's own tools — a category with its own rule.

    A native tool is outside the per-tool policy governing everything else on the
    surface, which is exactly why it needs one: a native tool must not cause an
    ungoverned side effect. Resumption sits on that line and is admissible only because
    its effect is bound to an id a human approved, with arguments they read."""

    def setUp(self):
        self.surface = load_surface()

    def test_a_native_name_cannot_be_confused_with_a_proxied_one(self):
        # Structural rather than a convention that has to be remembered: every proxied
        # name is built by ``exposed_name``, which always inserts the separator, so the
        # decode built for safety is also what tells the two apart.
        for tool in self.surface.NATIVE_TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertIsNone(self.surface.split_exposed(tool["name"]))
                self.assertNotIn(self.surface.SEP, tool["name"])

    def test_the_resume_tool_is_shaped_like_an_mcp_tool(self):
        resume = next(t for t in self.surface.NATIVE_TOOLS
                      if t["name"] == self.surface.RESUME_TOOL)
        self.assertEqual(resume["inputSchema"]["required"], ["approval_id"])
        self.assertIn("approval_id", resume["inputSchema"]["properties"])

    def test_the_description_says_it_runs_the_call_and_runs_it_once(self):
        # The two things a model would otherwise get wrong, and the reason the tool is
        # not called `get_result`: this is the trigger for the side effect, not a
        # collection of one that already happened.
        resume = next(t for t in self.surface.NATIVE_TOOLS
                      if t["name"] == self.surface.RESUME_TOOL)
        self.assertIn("RUNS the call", resume["description"])
        self.assertIn("once", resume["description"])

    def test_a_native_tool_is_not_produced_by_the_policy_join(self):
        # It is proxied from no server, so no roster and no enumeration can contribute
        # it — and no rule can withdraw it either.
        listed = {tool["name"] for tool in self.surface.curate(ROSTER,
                                                              {"mcp-github": EXPOSED})}
        for tool in self.surface.NATIVE_TOOLS:
            self.assertNotIn(tool["name"], listed)


class ProtocolTests(unittest.TestCase):

    def setUp(self):
        self.protocol = load_protocol()

    def ask(self, method: str, params: dict | None = None, listing=()):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        return self.protocol.handle(message, list(listing))

    def test_a_known_protocol_version_is_echoed(self):
        for version in self.protocol.PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                answer = self.ask("initialize", {"protocolVersion": version})
                self.assertEqual(answer["result"]["protocolVersion"], version)

    def test_an_unknown_protocol_version_is_answered_with_one_we_speak(self):
        # The spec's own shape: the server names a version it supports and the client
        # decides whether it can live with it. It fails in the safe direction — a
        # client that cannot is expected to disconnect rather than assume.
        answer = self.ask("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(answer["result"]["protocolVersion"],
                         self.protocol.PROTOCOL_VERSIONS[0])

    def test_no_capability_is_advertised_that_is_not_served(self):
        # `listChanged` in particular. The transport here is stateless, so there is no
        # server-to-client stream to send the notification on, and a client told
        # otherwise would wait for an update that cannot arrive.
        capabilities = self.ask("initialize")["result"]["capabilities"]
        self.assertEqual(capabilities, {"tools": {}})

    def test_tools_list_answers_with_the_listing_it_was_given(self):
        listing = [{"name": "mcp-github__get_issue", "description": "",
                    "inputSchema": {"type": "object"}}]
        self.assertEqual(self.ask("tools/list", listing=listing)["result"],
                         {"tools": listing})

    def test_the_list_is_complete_rather_than_paginated(self):
        # No `nextCursor` is how this transport says "that was all of them". A client
        # that saw one would page forever against a list held in memory.
        self.assertNotIn("nextCursor", self.ask("tools/list")["result"])

    def test_a_call_is_refused_when_no_executor_is_wired_in(self):
        # Fail-closed is the ordinary state of this surface, so a gateway with no
        # executing half refuses every call rather than half-answering them.
        answer = self.ask("tools/call", {"name": "mcp-github__get_issue"})
        self.assertIn("error", answer)
        self.assertNotIn("result", answer)

    def test_a_call_is_handed_to_the_executor_verbatim(self):
        # The name is not decoded here and the arguments are not inspected. This module
        # has no policy in it, and a protocol layer that pre-judged either would be a
        # second place where a call could be decided.
        seen = []

        def execute(name, arguments):
            seen.append((name, arguments))
            return {"content": [{"type": "text", "text": "ran"}], "isError": False}

        answer = self.protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "mcp-github__get_issue", "arguments": {"id": 7}}},
            [], execute)
        self.assertEqual(seen, [("mcp-github__get_issue", {"id": 7})])
        self.assertEqual(answer["result"]["content"][0]["text"], "ran")

    def test_an_executor_result_is_a_result_rather_than_an_error(self):
        # Including a refusal. Everything the executor decides carries a reason the
        # agent can act on; a JSON-RPC error carries none it can use.
        answer = self.protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "x__y"}}, [],
            lambda name, arguments: {"content": [], "isError": True})
        self.assertNotIn("error", answer)
        self.assertTrue(answer["result"]["isError"])

    def test_a_call_with_no_tool_name_never_reaches_the_executor(self):
        # A malformed REQUEST is the one thing on this path that is a protocol error
        # rather than a decision, so it is refused before anything can decide it.
        for params in ({}, {"name": ""}, {"name": 7}, {"name": None}):
            with self.subTest(params=params):
                def execute(name, arguments):  # pragma: no cover - must not run
                    raise AssertionError("the executor was reached")

                answer = self.protocol.handle(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": params}, [], execute)
                self.assertEqual(answer["error"]["code"], self.protocol.INVALID_PARAMS)

    def test_a_notification_is_never_answered(self):
        # Including one we do not recognize: replying to a notification is a protocol
        # violation, and an error carrying an id it does not have is malformed twice.
        for method in ("notifications/initialized", "notifications/cancelled",
                       "notifications/something-new"):
            with self.subTest(method=method):
                self.assertIsNone(self.protocol.handle(
                    {"jsonrpc": "2.0", "method": method}, []))

    def test_the_surfaces_that_are_not_served_say_so(self):
        # Each is a way in rather than a missing feature — a resource read would be a
        # second, unruled path to a server's data.
        for method in ("resources/list", "resources/read", "prompts/list",
                       "completion/complete", "logging/setLevel"):
            with self.subTest(method=method):
                answer = self.ask(method)
                self.assertEqual(answer["error"]["code"],
                                 self.protocol.METHOD_NOT_FOUND)

    def test_a_batch_is_refused_rather_than_unpacked(self):
        # Removed from the protocol in 2025-06-18. Supporting it would mean deciding
        # what a partial failure means for a surface where one member can be a governed
        # side effect.
        answer = self.protocol.handle([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], [])
        self.assertEqual(answer["error"]["code"], self.protocol.INVALID_REQUEST)

    def test_a_message_that_is_not_one_is_refused(self):
        for message in ("ping", 7, None, {}, {"jsonrpc": "1.0", "id": 1, "method": "ping"},
                        {"jsonrpc": "2.0", "id": 1}):
            with self.subTest(message=message):
                answer = self.protocol.handle(message, [])
                self.assertEqual(answer["error"]["code"], self.protocol.INVALID_REQUEST)

    def test_a_refusal_keeps_the_id_it_was_asked_under(self):
        # A client matches a response to its request by id. An error that dropped it is
        # an error the client cannot attribute, which reads as a hung call.
        answer = self.protocol.handle(
            {"jsonrpc": "2.0", "id": "abc", "method": "nope"}, [])
        self.assertEqual(answer["id"], "abc")

    def test_an_unparseable_body_answers_under_a_null_id(self):
        # The one response whose id is null by necessity: the id lives inside the
        # message that could not be parsed.
        self.assertEqual(self.protocol.parse_error()["id"], None)
        self.assertEqual(self.protocol.parse_error()["error"]["code"],
                         self.protocol.PARSE_ERROR)

    def test_every_answer_is_a_jsonrpc_message(self):
        # Cheap, and it catches the hand-built dict that forgot the envelope.
        for answer in (self.ask("initialize"), self.ask("ping"), self.ask("tools/list"),
                       self.ask("nope"), self.protocol.parse_error()):
            self.assertEqual(answer["jsonrpc"], "2.0")
            self.assertEqual("result" in answer, "error" not in answer)


if __name__ == "__main__":
    unittest.main()
