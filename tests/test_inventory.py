# SPDX-License-Identifier: Apache-2.0
"""Guards over the control plane's in-memory tool inventory.

Everything else in this process holds what an OPERATOR decided and must be right.
This holds what a SERVER claimed and need only be BOUNDED — so the tests run the
other way round from the policy ones: less about the answer being correct, more
about a hostile or broken answer not costing anything.

The invariant underneath all of it, and the reason a write from the gateway is
acceptable on that bridge at all: nothing here reaches ``policy._decide_tool``. A
tool in this map is denied exactly as it was before it appeared.
"""
from __future__ import annotations

import unittest

from _loader import load_inventory

GITHUB = {"servers": {"mcp-github": {
    "status": "ok",
    "tools": [{"name": "get_issue", "annotations": {"readOnlyHint": True}},
              {"name": "create_pr", "annotations": {"readOnlyHint": False}}]}}}


class RecordTests(unittest.TestCase):

    def setUp(self):
        self.inv = load_inventory()

    def test_a_push_is_readable_back(self):
        # The positive control: every other test here asserts something is refused or
        # dropped, so without this one a module that stored NOTHING would pass them all.
        self.inv.record(GITHUB)
        seen = self.inv.snapshot()["mcp-github"]
        self.assertEqual(seen["tools"], ["create_pr", "get_issue"])
        self.assertEqual(seen["read_only"], ["get_issue"])
        self.assertEqual(seen["status"], "ok")
        self.assertGreater(seen["seen_at"], 0)

    def test_a_push_replaces_rather_than_merges(self):
        # Snapshot semantics. A server that drops off the roster has to DISAPPEAR, or
        # the picker would keep offering tools from a server the gateway no longer
        # dials — and there is no deletion verb to forget to send.
        self.inv.record(GITHUB)
        self.inv.record({"servers": {"mcp-other": {"status": "ok", "tools": []}}})
        self.assertEqual(set(self.inv.snapshot()), {"mcp-other"})

    def test_too_many_servers_is_refused_whole_rather_than_truncated(self):
        # Truncating would make the surface quietly wrong about the one thing it is
        # for. At this size something is broken, not busy, and a refusal the gateway
        # prints is more use than a partial picture nobody can tell is partial.
        too_many = {"servers": {f"s{n}": {"status": "ok", "tools": []}
                                for n in range(self.inv.MAX_SERVERS + 1)}}
        with self.assertRaises(self.inv.InventoryError) as caught:
            self.inv.record(too_many)
        self.assertIn("cap", str(caught.exception))

    def test_tools_past_the_cap_are_dropped_and_counted(self):
        # Per-server the trade flips: one chatty server must not cost the whole push,
        # so the list is bounded and the remainder is REPORTED rather than hidden.
        over = self.inv.MAX_TOOLS_PER_SERVER + 5
        self.inv.record({"servers": {"s": {"status": "ok", "tools": [
            {"name": f"tool_{n}"} for n in range(over)]}}})
        seen = self.inv.snapshot()["s"]
        self.assertEqual(len(seen["tools"]), self.inv.MAX_TOOLS_PER_SERVER)
        self.assertEqual(seen["unnameable"], 5)

    def test_a_name_no_rule_could_be_written_for_is_dropped_and_counted(self):
        # `policy._TOOL_RE` bounds what `tool_rules.tool` can hold, so a tool outside
        # it is unreachable rather than ungoverned — offering it in a picker would be
        # offering a choice that cannot be made. The count is kept so the surface can
        # say so instead of appearing to show fewer tools than the server has.
        self.inv.record({"servers": {"s": {"status": "ok", "tools": [
            {"name": "fine"}, {"name": "not a tool name"}, {"name": ""}]}}})
        seen = self.inv.snapshot()["s"]
        self.assertEqual(seen["tools"], ["fine"])
        self.assertEqual(seen["unnameable"], 2)

    def test_a_server_name_that_is_not_a_dns_label_is_skipped(self):
        # The key becomes a lookup against `mcp_servers`, whose names are held to this
        # shape at registration. A push naming something else describes nothing.
        self.inv.record({"servers": {"../etc": {"status": "ok", "tools": []},
                                     "ok-one": {"status": "ok", "tools": []}}})
        self.assertEqual(set(self.inv.snapshot()), {"ok-one"})

    def test_a_status_carrying_a_novel_is_capped(self):
        # Server-adjacent text on a surface an operator reads. Bounded here rather
        # than at the renderer, because the cap belongs where the trust boundary is.
        self.inv.record({"servers": {"s": {"status": "x" * 5000, "tools": []}}})
        self.assertLessEqual(len(self.inv.snapshot()["s"]["status"]), 200)

    def test_a_malformed_payload_is_refused_rather_than_ignored(self):
        for bad in ({}, {"servers": []}, {"servers": "nope"}):
            with self.subTest(payload=bad), self.assertRaises(self.inv.InventoryError):
                self.inv.record(bad)

    def test_a_snapshot_is_a_copy(self):
        # Callers render it while the gateway may be replacing it, and a handle on the
        # live map would let one reader mutate what every other reader sees.
        self.inv.record(GITHUB)
        self.inv.snapshot()["mcp-github"]["tools"].append("injected")
        self.assertNotIn("injected", self.inv.snapshot()["mcp-github"]["tools"])


class ChangeTests(unittest.TestCase):
    """What becomes a durable audit row, which is the only part of this that outlives
    the process."""

    def setUp(self):
        self.inv = load_inventory()

    def test_an_unchanged_surface_produces_no_row(self):
        # A push arrives whenever the roster moves, which during an operator's session
        # is often. A row per push is not a record, it is a way to make one unreadable.
        self.inv.record(GITHUB)
        _, moved = self.inv.record(GITHUB)
        self.assertEqual(moved, [])

    def test_a_first_sighting_is_not_reported_as_a_surface_change(self):
        # This map empties on every restart of the control plane, so the FIRST push
        # after one would otherwise name every tool as newly appeared — a supply-chain
        # alarm for an event that did not happen. Counted, not enumerated: a delta is
        # what makes names worth reading, and the inventory holds the full list.
        _, moved = self.inv.record(GITHUB)
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0][0], "mcp-github")
        self.assertIn("first seen", moved[0][1])
        self.assertNotIn("get_issue", moved[0][1])

    def test_a_server_that_has_never_been_enumerated_is_not_reported_as_seen(self):
        # Enabling a server with a bad credential must not log "first seen exposing 0
        # tool(s)" — that claims the server offers nothing, when the truth is nobody
        # managed to ask. The gateway's own log says what went wrong; this log should
        # stay silent rather than assert something false.
        _, moved = self.inv.record({"servers": {"mcp-github": {
            "status": "auth descriptor wants 'Authorization' but there is no file"}}})
        self.assertEqual(moved, [])
        self.assertFalse(self.inv.snapshot()["mcp-github"]["enumerated"])

    def test_a_new_tool_is_named(self):
        # THE reason this is audited at all: an image bump that starts exposing a
        # destructive tool, with no human in the loop, is a supply-chain event. The
        # name is what makes the row worth keeping, and a tool name is charset-bounded
        # where a description is not.
        self.inv.record(GITHUB)
        grown = {"servers": {"mcp-github": {"status": "ok", "tools": [
            {"name": "get_issue"}, {"name": "create_pr"},
            {"name": "delete_repository"}]}}}
        _, moved = self.inv.record(grown)
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0][0], "mcp-github")
        self.assertIn("delete_repository", moved[0][1])
        self.assertIn("now exposes", moved[0][1])

    def test_a_server_leaving_the_roster_writes_no_row(self):
        # Disabling a server empties it out of the roster, so the next push omits it
        # entirely — which naively reads as "this server dropped all 25 of its tools".
        # It did not; an operator turned it off, and that is already audited on the
        # management listener with their identity attached. Found by disabling a server
        # and reading the log.
        self.inv.record(GITHUB)
        _, moved = self.inv.record({"servers": {}})
        self.assertEqual(moved, [])
        self.assertEqual(self.inv.snapshot(), {})

    def test_a_tool_that_disappears_is_named_too(self):
        # The other direction, and not merely for symmetry: a tool vanishing turns any
        # rule naming it into dead policy, which is what the gateway's own report
        # calls out. The row is where that becomes dated evidence.
        self.inv.record(GITHUB)
        _, moved = self.inv.record({"servers": {"mcp-github": {
            "status": "ok", "tools": [{"name": "get_issue"}]}}})
        self.assertIn("no longer exposes create_pr", moved[0][1])

    def test_a_server_that_could_not_be_enumerated_does_not_read_as_empty(self):
        # The distinction the gateway takes trouble to preserve: "exposes nothing" and
        # "we could not ask" are different, and collapsing them here would log every
        # brief credential problem as a server losing all of its tools.
        self.inv.record(GITHUB)
        _, moved = self.inv.record({"servers": {"mcp-github": {
            "status": "unreachable: connection refused"}}})
        self.assertEqual(moved, [])
        # The last known surface STANDS, with the status saying it may be stale. An
        # operator mid-debug should not also lose the tool list they were reading.
        seen = self.inv.snapshot()["mcp-github"]
        self.assertEqual(seen["tools"], ["create_pr", "get_issue"])
        self.assertIn("unreachable", seen["status"])


if __name__ == "__main__":
    unittest.main()
