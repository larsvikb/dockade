# SPDX-License-Identifier: Apache-2.0
"""Guards over the executing half of the gateway.

Unlike ``tests/test_tool_surface.py``, this file IS about the boundary. Everything
here decides whether a side effect happens, so the bar is not "the answer is right"
but "nothing runs that was not authorised, and nothing that was authorised runs
twice".

The shape of most tests below follows from that: they assert what did NOT happen. A
deny that returns the right sentence while also dialling the server is a deny in text
only, and a test that checked the sentence alone would pass on it — so the server stub
counts its calls and the count is the assertion.

Both peers are stubbed, and neither stub is a fixture of convenience. ``_ask_control``
stands in for the control plane because reaching it needs a network this process does
not have; ``discovery.post`` stands in for a server for the same reason. What is real
is every decision between them, which is the part that can be wrong.
"""
from __future__ import annotations

import json
import unittest

from _loader import load_execute

#: One enabled server with one rule per action. Auth is `none`, so no secret file is
#: read — the descriptor path is discovery's business and is tested there.
ROSTER = [{"server": "mcp-github", "auth": {"type": "none"},
           "tools": [{"tool": "get_issue", "action": "allow"},
                     {"tool": "create_pr", "action": "ask"},
                     {"tool": "delete_repo", "action": "deny"}]}]

RESULT = {"server": "mcp-github", "status": "ok", "rules": 3, "exposed": 1,
          "tools": [{"name": "get_issue", "description": "Read an issue.",
                     "inputSchema": {"type": "object"},
                     "annotations": {"readOnlyHint": True}}],
          "ruled_but_absent": [], "exposed_but_unruled": []}

APPROVAL = "0" * 32


def sse(result: dict) -> str:
    """A server's reply in the shape it actually sends."""
    return f"data: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result})}\n\n"


class ExecutionTestCase(unittest.TestCase):
    """A gateway with both its peers replaced by recorders."""

    def setUp(self):
        self.execute = load_execute()
        # The roster has to be published before anything can be dialled: the auth
        # descriptor lives there and nowhere else.
        self.execute.surface.publish(ROSTER, [RESULT])
        self.asked: list[tuple[str, dict]] = []
        self.called: list[dict] = []
        self.answer: dict = {}
        self.reply = sse({"content": [{"type": "text", "text": "the issue"}]})

        def ask_control(path, payload):
            self.asked.append((path, payload))
            return self.answer

        def post(server, message, auth, timeout=None):
            self.called.append({"server": server, "message": message, "auth": auth,
                                "timeout": timeout})
            return self.reply

        self.execute._ask_control = ask_control
        self._real_post = self.execute.discovery.post
        self.execute.discovery.post = post

    def tearDown(self):
        # `discovery` is imported by plain name and therefore shared. Restored so a
        # patched module cannot leak into another file's tests.
        self.execute.discovery.post = self._real_post

    def text(self, result: dict) -> str:
        return "".join(block.get("text", "") for block in result.get("content") or [])


class DecisionTests(ExecutionTestCase):

    def test_an_allow_asks_first_and_then_runs(self):
        self.answer = {"decision": "allow", "reason": "'allow' by rule"}
        result = self.execute.call("mcp-github__get_issue", {"id": 1}, "172.30.0.5")
        self.assertEqual([path for path, _ in self.asked], ["/tool/authorize"])
        self.assertEqual(len(self.called), 1)
        self.assertEqual(self.text(result), "the issue")
        self.assertFalse(result.get("isError"))

    def test_the_pair_is_what_governance_is_asked_about(self):
        # Not the flattened string. The rule an operator wrote is keyed on
        # (server, tool), so asking under any other identity reads the wrong row — or
        # no row, which denies for the wrong reason.
        self.answer = {"decision": "allow", "reason": "ok"}
        self.execute.call("mcp-github__get_issue", {}, "172.30.0.5")
        _, payload = self.asked[0]
        self.assertEqual(payload["server"], "mcp-github")
        self.assertEqual(payload["tool"], "get_issue")

    def test_the_sandbox_address_is_relayed_not_the_gateways(self):
        # It is what the per-client ask cap counts. A gateway reporting itself would
        # make one shared bucket of every sandbox.
        self.answer = {"decision": "allow", "reason": "ok"}
        self.execute.call("mcp-github__get_issue", {}, "172.30.0.5")
        self.assertEqual(self.asked[0][1]["client"], "172.30.0.5")

    def test_the_complete_arguments_are_sent_for_the_decision(self):
        # On the ask path they become what a human reads and what the grant is bound
        # to, so a payload trimmed here would bind an approval to a different call.
        args = {"id": 1, "body": "a" * 100}
        self.answer = {"decision": "allow", "reason": "ok"}
        self.execute.call("mcp-github__get_issue", args, "172.30.0.5")
        self.assertEqual(self.asked[0][1]["args"], args)

    def test_a_deny_dials_nothing(self):
        # The assertion that matters is the empty call list, not the sentence.
        self.answer = {"decision": "deny", "reason": "no rule for 'delete_repo'"}
        result = self.execute.call("mcp-github__delete_repo", {}, "172.30.0.5")
        self.assertEqual(self.called, [])
        self.assertTrue(result["isError"])
        self.assertIn("no rule for 'delete_repo'", self.text(result))

    def test_a_deny_says_retrying_will_not_help(self):
        # An agent that cannot tell a refusal from a delay retries one forever.
        self.answer = {"decision": "deny", "reason": "disabled"}
        self.assertIn("not succeed on retry",
                      self.text(self.execute.call("mcp-github__x", {}, None)))

    def test_an_unrecognised_decision_is_a_deny(self):
        # The store is a file on a volume and the bridge is another process. Anything
        # that is not an explicit grant must fail closed here too.
        for decision in ("maybe", "", None, "ALLOW", "allow_once"):
            with self.subTest(decision=decision):
                self.called.clear()
                self.answer = {"decision": decision, "reason": "?"}
                result = self.execute.call("mcp-github__get_issue", {}, None)
                self.assertTrue(result["isError"])
                self.assertEqual(self.called, [])

    def test_an_unreachable_control_plane_refuses_rather_than_runs(self):
        # The one that would turn an outage into a default-allow.
        def boom(path, payload):
            raise self.execute.discovery.DiscoveryError("connection refused")

        self.execute._ask_control = boom
        result = self.execute.call("mcp-github__get_issue", {}, None)
        self.assertEqual(self.called, [])
        self.assertTrue(result["isError"])
        self.assertIn("Nothing ran", self.text(result))

    def test_a_name_that_does_not_resolve_asks_nothing_and_runs_nothing(self):
        # There is no (server, tool) pair to decide about, and inventing one would be
        # the gateway choosing which rule applies.
        for name in ("get_issue", "__x", "mcp-github__", ""):
            with self.subTest(name=name):
                self.asked.clear()
                self.called.clear()
                result = self.execute.call(name, {}, None)
                self.assertTrue(result["isError"])
                self.assertEqual(self.asked, [])
                self.assertEqual(self.called, [])

    def test_a_server_off_the_roster_is_not_dialled_even_on_an_allow(self):
        # Defence in depth behind the control plane's own check: without the roster
        # entry there is no auth descriptor, so dialling anyway would turn an
        # operator's disable into a 401 that reads like a broken tool.
        self.execute.surface.publish([], [])
        self.answer = {"decision": "allow", "reason": "ok"}
        result = self.execute.call("mcp-github__get_issue", {}, None)
        self.assertEqual(self.called, [])
        self.assertTrue(result["isError"])


class AskTests(ExecutionTestCase):

    def setUp(self):
        super().setUp()
        self.answer = {"decision": "ask", "reason": "'ask' by rule",
                       "approval_id": APPROVAL, "deadline": 1.0, "joined": False}

    def test_an_ask_runs_nothing_and_is_not_an_error(self):
        # A result rather than an error, because an agent can act on a result and
        # records an error as a failed call — which is the stranded caller arriving by
        # another route.
        result = self.execute.call("mcp-github__create_pr", {"title": "x"}, None)
        self.assertEqual(self.called, [])
        self.assertFalse(result.get("isError"))

    def test_the_pending_result_carries_the_id_and_the_way_back(self):
        # The instruction travels in the result, where it cannot be forgotten
        # mid-session and cannot drift from the gateway that emitted it.
        text = self.text(self.execute.call("mcp-github__create_pr", {}, None))
        self.assertIn(APPROVAL, text)
        self.assertIn(self.execute.surface.RESUME_TOOL, text)

    def test_the_pending_result_steers_away_from_a_reformulated_retry(self):
        # A retry with different arguments hashes differently and opens a SECOND ask,
        # so the flood the caps exist to bound arrives from ordinary model behaviour.
        text = self.text(self.execute.call("mcp-github__create_pr", {}, None))
        self.assertIn("second question", text)

    def test_joining_an_identical_ask_is_said_out_loud(self):
        self.answer = {**self.answer, "joined": True}
        self.assertIn("joined",
                      self.text(self.execute.call("mcp-github__create_pr", {}, None)))


class ResumeTests(ExecutionTestCase):

    #: A sentinel rather than None, because None is one of the argument values under
    #: test below and a default that swallowed it would make that case vacuous.
    DEFAULT = object()

    def resume(self, arguments=DEFAULT):
        return self.execute.call(
            self.execute.surface.RESUME_TOOL,
            {"approval_id": APPROVAL} if arguments is self.DEFAULT else arguments,
            "172.30.0.5")

    def test_resumption_is_routed_without_being_split(self):
        # A native tool has no server to decide against, so it must not reach the
        # decode at all — and its name has no separator, so it could not survive one.
        self.assertIsNone(
            self.execute.surface.split_exposed(self.execute.surface.RESUME_TOOL))

    def test_a_granted_claim_runs_the_call(self):
        self.answer = {"ok": True, "status": "allowed", "server": "mcp-github",
                       "tool": "create_pr", "args_json": '{"title":"approved"}'}
        result = self.resume()
        self.assertEqual([path for path, _ in self.asked],
                         [f"/tool/asks/{APPROVAL}/claim"])
        self.assertEqual(len(self.called), 1)
        self.assertFalse(result.get("isError"))

    def test_the_arguments_that_run_are_the_approved_ones(self):
        # The agent never re-sends the payload, so what executes is necessarily what
        # the human read. This is the test that would catch a "helpful" merge of the
        # agent's arguments into the approved ones.
        self.answer = {"ok": True, "status": "allowed", "server": "mcp-github",
                       "tool": "create_pr", "args_json": '{"title":"approved"}'}
        self.resume({"approval_id": APPROVAL, "title": "smuggled"})
        self.assertEqual(self.called[0]["message"]["params"]["arguments"],
                         {"title": "approved"})

    def test_a_pending_approval_is_not_an_error_and_runs_nothing(self):
        # The question the whole pending design turns on: still waiting is a delay,
        # not a refusal.
        self.answer = {"ok": False, "detail": "not claimable (pending)",
                       "status": "pending", "spent": False, "terminal": False}
        result = self.resume()
        self.assertEqual(self.called, [])
        self.assertFalse(result.get("isError"))
        self.assertIn("Still waiting", self.text(result))

    def test_a_pending_approval_keeps_the_agent_on_the_same_id(self):
        self.answer = {"ok": False, "detail": "not claimable (pending)",
                       "status": "pending", "spent": False, "terminal": False}
        text = self.text(self.resume())
        self.assertIn(APPROVAL, text)
        self.assertIn("same id", text)

    def test_every_terminal_refusal_is_final_and_says_so(self):
        # `denied`, `expired` and `spent` are three different states and one answer:
        # stop. A spent approval in particular must not be retried, because the call
        # it released already ran.
        for status, spent in (("denied", False), ("expired", False), ("allowed", True)):
            with self.subTest(status=status):
                self.called.clear()
                self.answer = {"ok": False, "detail": f"not claimable ({status})",
                               "status": status, "spent": spent, "terminal": True}
                result = self.resume()
                self.assertEqual(self.called, [])
                self.assertTrue(result["isError"])
                self.assertIn("final", self.text(result))

    def test_the_terminal_states_are_narrated_rather_than_echoed(self):
        # `not claimable (denied)` is the control plane's internal spelling for a state
        # it has no prose for. This is the surface an agent reads, so the rendering
        # belongs here — and the states say different things: refused by a person, or
        # never answered at all.
        for status, expected in (("denied", "refused"), ("expired", "expired before")):
            with self.subTest(status=status):
                self.answer = {"ok": False, "detail": f"not claimable ({status})",
                               "status": status, "spent": False, "terminal": True}
                text = self.text(self.resume())
                self.assertIn(expected, text)
                self.assertNotIn("not claimable", text)

    def test_a_state_with_no_prose_here_keeps_the_control_planes_own_words(self):
        # The already-claimed case arrives with a sentence written for a reader. Falling
        # through to it is what keeps this map from having to track every state the
        # control plane can report.
        self.answer = {"ok": False,
                       "detail": "this approval has already been claimed and its call "
                                 "has run",
                       "status": "allowed", "spent": True, "terminal": True}
        self.assertIn("already been claimed", self.text(self.resume()))

    def test_an_unknown_approval_claims_nothing_further(self):
        self.answer = {"ok": False, "detail": "unknown approval", "status": None,
                       "spent": False, "terminal": True}
        result = self.resume()
        self.assertTrue(result["isError"])
        self.assertEqual(self.called, [])

    def test_an_id_that_is_not_one_never_reaches_the_bridge(self):
        # It is the only thing on this path that comes from the agent, and it lands in
        # a request path on the crown-jewel bridge.
        for bad in (None, "", "../../tool/roster", "0" * 31, "g" * 32, 7,
                    {"approval_id": None}):
            with self.subTest(value=bad):
                self.asked.clear()
                result = self.resume({"approval_id": bad} if not isinstance(bad, dict)
                                     else bad)
                self.assertTrue(result["isError"])
                self.assertEqual(self.asked, [])

    def test_an_id_with_stray_whitespace_is_still_redeemable(self):
        # An id copied out of a pending result picks up a trailing space or newline
        # often enough that refusing it costs a human's approval rather than catching
        # anything: the charset is bounded hex, so trimming cannot turn one valid id
        # into another.
        self.answer = {"ok": True, "status": "allowed", "server": "mcp-github",
                       "tool": "get_issue", "args_json": "{}"}
        for spelling in (f"  {APPROVAL}  ", f"{APPROVAL}\n", f"\t{APPROVAL}"):
            with self.subTest(spelling=spelling):
                self.asked.clear()
                self.resume({"approval_id": spelling})
                self.assertEqual([path for path, _ in self.asked],
                                 [f"/tool/asks/{APPROVAL}/claim"])

    def test_whitespace_is_trimmed_rather_than_stripped_throughout(self):
        # Inner whitespace is not a formatting artifact, it is a different string —
        # and one that must never reach a URL path.
        self.asked.clear()
        result = self.resume({"approval_id": APPROVAL[:16] + " " + APPROVAL[17:]})
        self.assertTrue(result["isError"])
        self.assertEqual(self.asked, [])

    def test_arguments_that_are_not_an_object_are_refused(self):
        for arguments in (None, "abc", 7, []):
            with self.subTest(arguments=arguments):
                self.assertTrue(self.resume(arguments)["isError"])

    def test_an_unreachable_control_plane_leaves_the_approval_untouched(self):
        # Worth saying in the text: the agent should come back rather than raise a new
        # question, because nothing was spent.
        def boom(path, payload):
            raise self.execute.discovery.DiscoveryError("connection refused")

        self.execute._ask_control = boom
        result = self.resume()
        self.assertEqual(self.called, [])
        self.assertTrue(result["isError"])
        self.assertIn("untouched", self.text(result))


class UpstreamReplyTests(ExecutionTestCase):

    def test_the_sse_shape_is_parsed(self):
        self.assertEqual(
            self.execute.parse_call(sse({"content": [], "isError": False})),
            {"content": [], "isError": False})

    def test_a_plain_json_body_is_also_parsed(self):
        # Legal for this transport and not what the server sends today. Accepted so a
        # version bump cannot turn every tool call into a parse failure.
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": []}})
        self.assertEqual(self.execute.parse_call(body), {"content": []})

    def test_a_rejected_request_is_not_passed_off_as_a_failed_tool(self):
        # The protocol separates "the call ran and the tool reports a problem" from
        # "the request was rejected". Flattening them would tell an agent its arguments
        # were wrong when the server was unreachable, or the reverse.
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "error": {"code": -32602, "message": "bad params"}})
        result = self.execute.parse_call(body)
        self.assertTrue(result["isError"])
        self.assertIn("rejected the call", self.text(result))

    def test_a_reply_that_is_not_one_is_an_error_rather_than_an_empty_result(self):
        # An empty result would read to the agent as a tool that succeeded and said
        # nothing, which is the one thing a transport failure must not look like.
        for raw in ("", "not json at all", '{"jsonrpc":"2.0","id":1}'):
            with self.subTest(raw=raw), \
                 self.assertRaises(self.execute.discovery.DiscoveryError):
                self.execute.parse_call(raw)

    def test_an_unreachable_server_is_reported_as_a_failed_call(self):
        def boom(server, message, auth, timeout=None):
            raise self.execute.discovery.DiscoveryError("unreachable: refused")

        self.execute.discovery.post = boom
        self.answer = {"decision": "allow", "reason": "ok"}
        result = self.execute.call("mcp-github__get_issue", {}, None)
        self.assertTrue(result["isError"])
        self.assertIn("could not call", self.text(result))

    def test_a_call_gets_the_longer_timeout_not_the_discovery_one(self):
        # Enumeration waits for a sibling answering from memory; a call crosses the
        # internet through a proxy that may be holding it for a human.
        self.answer = {"decision": "allow", "reason": "ok"}
        self.execute.call("mcp-github__get_issue", {}, None)
        self.assertEqual(self.called[0]["timeout"], self.execute.CALL_TIMEOUT)
        self.assertGreater(self.execute.CALL_TIMEOUT,
                           self.execute.discovery.TIMEOUT)


if __name__ == "__main__":
    unittest.main()
