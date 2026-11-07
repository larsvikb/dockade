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

import json
import threading
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

    def test_a_server_name_with_a_trailing_newline_is_skipped(self):
        # Pushed keys reach the check unstripped, so `ok-one\n` would otherwise be
        # stored beside `ok-one` as a second entry naming no registered server.
        self.inv.record({"servers": {"ok-one\n": {"status": "ok", "tools": []},
                                     "ok-one": {"status": "ok", "tools": []}}})
        self.assertEqual(set(self.inv.snapshot()), {"ok-one"})

    def test_a_status_carrying_a_novel_is_capped(self):
        # Server-adjacent text on a surface an operator reads. Bounded here rather
        # than at the renderer, because the cap belongs where the trust boundary is.
        self.inv.record({"servers": {"s": {"status": "x" * 5000, "tools": []}}})
        self.assertLessEqual(len(self.inv.snapshot()["s"]["status"]), 200)

    def _served(self, status):
        # The route serves snapshot() through pydantic-core, which a stubbed FastAPI
        # does not run; json.dumps to UTF-8 rejects a lone surrogate and accepts its
        # escape the same way.
        self.inv.record({"servers": {"s": {"status": status, "tools": []}}})
        snap = self.inv.snapshot()
        json.dumps(snap, ensure_ascii=False).encode("utf-8")
        return snap["s"]["status"]

    def test_a_status_holding_a_lone_surrogate_is_stored_escaped(self):
        # A server's error text can carry one, and stored raw it would make every
        # GET of the inventory fail until the next push replaced it.
        for code in (0xD800, 0xDFFF):
            with self.subTest(code=hex(code)):
                self.assertEqual(self._served("boom " + chr(code)),
                                 "boom \\u%04x" % code)

    def test_a_status_of_surrogates_is_escaped_before_it_is_capped(self):
        # Capped first, 200 surrogates would escape to some 1200 characters.
        self.assertLessEqual(len(self._served(chr(0xDC80) * 250)), 200)

    def test_a_status_utf8_can_carry_is_stored_unchanged(self):
        for status in ("Grüße", "ok " + chr(0x1F600)):
            with self.subTest(status=status):
                self.assertEqual(self._served(status), status)

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

    def test_overlapping_pushes_diff_against_each_other_not_the_same_old_map(self):
        # Push A stalls while cleaning its tools; push B arrives meanwhile. Were the old
        # map read outside the lock, both would diff against the empty map — two
        # "first seen" rows — and A, finishing last, would overwrite B's newer picture.
        entered, release = threading.Event(), threading.Event()
        clean = self.inv._clean_tools

        def stalling(raw):
            if not entered.is_set():
                entered.set()
                release.wait(5)
            return clean(raw)

        self.inv._clean_tools = stalling
        self.addCleanup(setattr, self.inv, "_clean_tools", clean)
        grown = {"servers": {"mcp-github": {"status": "ok", "tools": [
            *GITHUB["servers"]["mcp-github"]["tools"], {"name": "merge_pr"}]}}}
        moved = []
        a = threading.Thread(target=lambda: moved.extend(self.inv.record(GITHUB)[1]))
        b = threading.Thread(target=lambda: moved.extend(self.inv.record(grown)[1]))
        a.start()
        self.assertTrue(entered.wait(5))
        b.start()
        b.join(0.2)             # B either finishes against the old map, or waits for A
        release.set()
        a.join(5)
        b.join(5)
        self.assertEqual(sorted(line for _, line in moved),
                         ["mcp-github first seen exposing 2 tool(s)",
                          "mcp-github now exposes merge_pr"])
        self.assertIn("merge_pr", self.inv.snapshot()["mcp-github"]["tools"])


D1, D2, D3 = "a" * 64, "b" * 64, "c" * 64


def _push(**digests):
    """mcp-github exposing one tool per keyword, each with that digest (None: none)."""
    return {"servers": {"mcp-github": {"status": "ok", "tools": [
        {"name": name, **({} if digest is None else {"digest": digest})}
        for name, digest in digests.items()]}}}


class SchemaDigestTests(unittest.TestCase):
    """A tool's arguments moving, which widens a plain `allow` on a dispatcher and is
    audited the way a new tool is."""

    def setUp(self):
        self.inv = load_inventory()

    def test_a_moved_digest_is_one_line_naming_the_tool(self):
        self.inv.record(_push(get_issue=D1, issue_read=D1))
        _, moved = self.inv.record(_push(get_issue=D1, issue_read=D2))
        self.assertEqual(moved,
                         [("mcp-github", "mcp-github changed the arguments of issue_read")])

    def test_a_first_sighting_says_nothing_about_arguments(self):
        _, moved = self.inv.record(_push(issue_read=D1))
        self.assertEqual(moved, [("mcp-github", "mcp-github first seen exposing 1 tool(s)")])
        self.assertEqual(self.inv.snapshot()["mcp-github"]["changed"], {})

    def test_a_first_digest_is_quiet(self):
        # A gateway older than digests, upgraded: nothing observed the arguments move.
        self.inv.record(_push(issue_read=None))
        _, moved = self.inv.record(_push(issue_read=D1))
        self.assertEqual(moved, [])
        self.assertEqual(self.inv.snapshot()["mcp-github"]["changed"], {})

    def test_a_new_tool_reads_only_as_new(self):
        self.inv.record(_push(get_issue=D1))
        _, moved = self.inv.record(_push(get_issue=D1, issue_write=D2))
        self.assertEqual(moved, [("mcp-github", "mcp-github now exposes issue_write")])

    def test_two_moves_are_named_in_order(self):
        self.inv.record(_push(pull_request_read=D1, issue_read=D1))
        _, moved = self.inv.record(_push(pull_request_read=D2, issue_read=D2))
        self.assertEqual(moved, [("mcp-github", "mcp-github changed the arguments of "
                                                "issue_read, pull_request_read")])

    def test_a_tool_that_leaves_and_returns_reads_only_as_new(self):
        # Its digest leaves with it, so the return is a new tool's, not a move: the
        # same gap a whole server leaving the roster has.
        self.inv.record(_push(get_issue=D1, issue_read=D1))
        self.inv.record(_push(get_issue=D1))
        self.assertEqual(self.inv.snapshot()["mcp-github"]["digests"], {"get_issue": D1})
        _, moved = self.inv.record(_push(get_issue=D1, issue_read=D2))
        self.assertEqual(moved, [("mcp-github", "mcp-github now exposes issue_read")])

    def test_a_move_across_a_push_that_could_not_enumerate_is_reported(self):
        self.inv.record(_push(issue_read=D1))
        self.inv.record({"servers": {"mcp-github": {"status": "unreachable"}}})
        self.assertEqual(self.inv.snapshot()["mcp-github"]["digests"], {"issue_read": D1})
        _, moved = self.inv.record(_push(issue_read=D2))
        self.assertEqual(moved,
                         [("mcp-github", "mcp-github changed the arguments of issue_read")])

    def test_a_move_across_a_push_with_no_digest_is_reported(self):
        self.inv.record(_push(issue_read=D1))
        _, moved = self.inv.record(_push(issue_read=None))
        self.assertEqual(moved, [])
        _, moved = self.inv.record(_push(issue_read=D2))
        self.assertEqual(moved,
                         [("mcp-github", "mcp-github changed the arguments of issue_read")])

    def test_a_malformed_digest_is_dropped(self):
        for bad in (D1.upper(), D1[:-1], D1 + "0", D1 + "\n", 7, None, ["x"]):
            with self.subTest(digest=bad):
                inv = load_inventory()
                inv.record({"servers": {"s": {"status": "ok", "tools": [
                    {"name": "t", "digest": bad}]}}})
                seen = inv.snapshot()["s"]
                self.assertEqual(seen["tools"], ["t"])
                self.assertEqual(seen["digests"], {})

    def test_when_the_arguments_changed_is_kept_while_the_tool_is_exposed(self):
        self.inv.record(_push(issue_read=D1))
        self.inv.record(_push(issue_read=D2))
        at = self.inv.snapshot()["mcp-github"]["changed"]["issue_read"]
        self.assertGreater(at, 0)
        self.inv.record(_push(issue_read=D2))
        self.inv.record({"servers": {"mcp-github": {"status": "unreachable"}}})
        self.assertEqual(self.inv.snapshot()["mcp-github"]["changed"], {"issue_read": at})
        self.inv.record(_push(issue_read=D3))
        self.assertGreaterEqual(
            self.inv.snapshot()["mcp-github"]["changed"]["issue_read"], at)
        self.inv.record(_push(get_issue=D1))
        self.assertEqual(self.inv.snapshot()["mcp-github"]["changed"], {})

    def test_a_snapshot_shares_no_per_tool_map(self):
        self.inv.record(_push(issue_read=D1))
        self.inv.record(_push(issue_read=D2))
        seen = self.inv.snapshot()["mcp-github"]
        seen["digests"]["issue_read"] = D3
        seen["changed"].clear()
        again = self.inv.snapshot()["mcp-github"]
        self.assertEqual(again["digests"], {"issue_read": D2})
        self.assertIn("issue_read", again["changed"])


if __name__ == "__main__":
    unittest.main()
