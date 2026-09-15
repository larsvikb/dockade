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

from _loader import load_protocol, load_surface

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

    def test_nothing_is_listed_before_the_first_reconcile(self):
        # The correct cold answer, and a harmless one: an empty list is not a grant.
        self.assertEqual(self.surface.listing(), [])

    def test_publishing_makes_the_listing_readable(self):
        self.surface.publish(ROSTER, [result()])
        self.assertIn("mcp-github__get_issue",
                      [t["name"] for t in self.surface.listing()])

    def test_a_server_that_could_not_be_dialled_keeps_its_tools(self):
        # A restarting container or a briefly missing credential must not read to the
        # agent as a capability that was withdrawn — and keeping the entry buys no
        # risk, because execution is decided per call against the control plane.
        self.surface.publish(ROSTER, [result()])
        self.surface.publish(ROSTER, [unreachable()])
        self.assertIn("mcp-github__get_issue",
                      [t["name"] for t in self.surface.listing()])

    def test_a_server_removed_from_the_roster_loses_its_tools(self):
        # A different event from a failed dial: this one is an operator's answer
        # (disabled or revoked), not a failure to ask.
        self.surface.publish(ROSTER, [result()])
        self.surface.publish([], [])
        self.assertEqual(self.surface.listing(), [])

    def test_a_rule_flipped_to_deny_leaves_the_listing_on_the_next_reconcile(self):
        # The operator-visible loop this whole step exists to close. A rule edit changes
        # the roster digest, which is what triggers the reconcile that republishes.
        self.surface.publish(ROSTER, [result()])
        denied = [{**ROSTER[0],
                   "tools": [{"tool": "get_issue", "action": "deny"}]}]
        self.surface.publish(denied, [result()])
        self.assertEqual(self.surface.listing(), [])

    def test_a_server_that_stops_exposing_a_tool_stops_listing_it(self):
        # The supply-chain direction: an image bump that REMOVES a tool. The rule
        # survives and decides nothing, which is `ruled_but_absent` in the report.
        self.surface.publish(ROSTER, [result()])
        self.surface.publish(ROSTER, [result(tools=[EXPOSED[1]])])
        self.assertEqual([t["name"] for t in self.surface.listing()],
                         ["mcp-github__create_pr"])


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

    def test_a_call_is_refused_while_the_executing_half_is_unbuilt(self):
        # Fail-closed is the ordinary state of this surface, so an unbuilt executor
        # refusing every call is the same answer an unruled tool would get.
        answer = self.ask("tools/call", {"name": "mcp-github__get_issue"})
        self.assertIn("error", answer)
        self.assertNotIn("result", answer)

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
