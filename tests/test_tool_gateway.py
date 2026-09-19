# SPDX-License-Identifier: Apache-2.0
"""Guards over the MCP gateway's bind refusal.

``tests/test_topology.py`` asserts that compose gives the gateway the right legs
and that its forbidden-CIDR default matches the real subnets. That is the
configuration side. This file asserts the other half: that the process actually
REFUSES the configurations that list describes, rather than merely carrying a
list which describes them.

The distinction is the one #37 hit on the control plane. A guard that refused only
wildcards was satisfied by `CONTROL_TOOL_BIND=172.29.0.2` — a concrete address on
the other enforcer's network — and every healthcheck stayed green. Both spellings
of the mistake are tested here, because one check does not imply the other.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import unittest

from _loader import load_tool_gateway

#: The configuration compose actually deploys. Written out rather than read from
#: compose: test_topology.py already holds those two equal, and re-deriving it here
#: would make this file pass for whatever compose happens to say.
GOOD = {"GATEWAY_AGENT_BIND": "172.30.0.11"}

#: Two rosters that differ, for the pacing tests. Content is irrelevant —
#: what is under test is whether the loop notices they are not the same.
ROSTER_A = [{"server": "a"}]
ROSTER_B = [{"server": "b"}]


class BindGuardTests(unittest.TestCase):

    def _guard(self, env: dict[str, str]) -> None:
        load_tool_gateway(env)._assert_bind_is_agent_facing_only()

    def test_the_deployed_configuration_is_accepted(self):
        # The positive control, and it earns its place: every other test here asserts
        # a refusal, so without this one a guard that refused EVERYTHING would pass
        # the whole file while making the gateway unstartable.
        self._guard(GOOD)

    def test_a_wildcard_bind_is_refused(self):
        # A wildcard serves the agent-facing MCP endpoint on mcp-net too, where the
        # server containers live. Each of those holds a write-capable credential.
        for wildcard in ("0.0.0.0", "::", "*", ""):  # noqa: S104
            with self.subTest(bind=wildcard):
                with self.assertRaises(SystemExit) as caught:
                    self._guard({"GATEWAY_AGENT_BIND": wildcard})
                self.assertIn("wildcard", str(caught.exception))

    def test_an_address_on_mcp_net_is_refused(self):
        # The OTHER spelling, and the one a wildcard test misses. This is the exact
        # shape of the control plane's escaped case: a concrete, plausible-looking
        # address that happens to be on the network the surface must not reach.
        with self.assertRaises(SystemExit) as caught:
            self._guard({"GATEWAY_AGENT_BIND": "172.28.0.2"})
        self.assertIn("172.28.0.0/24", str(caught.exception))

    def test_an_address_on_a_control_network_is_refused(self):
        # The gateway has a leg on tool-authorize-net and none on the other two, so
        # only the first of these is reachable-in-principle today. All are refused
        # anyway: the rule is that the agent surface lives on the agent's network,
        # and a guard that depended on today's topology would need revisiting every
        # time the topology changed — which is when it is least likely to happen.
        for addr in ("172.27.0.3", "172.29.0.2", "172.31.0.2"):
            with self.subTest(bind=addr), self.assertRaises(SystemExit):
                self._guard({"GATEWAY_AGENT_BIND": addr})

    def test_a_hostname_is_not_inside_anything_and_is_not_resolved(self):
        # A guard that resolved names would depend on DNS, which is the class of
        # check the relay guard exists because it cannot trust. A hostname answers
        # False to every containment test and falls through — it will then fail at
        # bind time, loudly, which is the right place for a name that does not
        # resolve to a local address.
        self._guard({"GATEWAY_AGENT_BIND": "tool-gateway"})

    def test_an_unparseable_forbidden_entry_is_fatal(self):
        # Fail closed. This list has few members and every one is load-bearing, so a
        # typo must not read as "nothing is forbidden" — the failure mode would be a
        # process that starts, serves, and has no guard.
        with self.assertRaises(SystemExit) as caught:
            self._guard({**GOOD, "GATEWAY_BIND_FORBIDDEN": "172.28.0.0/24,not-a-cidr"})
        self.assertIn("not a CIDR", str(caught.exception))

    def test_the_guard_is_not_disabled_by_an_empty_forbidden_list(self):
        # An empty list legitimately parses to "nothing forbidden", so the wildcard
        # refusal has to stand on its own rather than being a special case of the
        # CIDR check. Asserted because the two refusals read as interchangeable and
        # are not: this is the configuration where only one of them is left.
        with self.assertRaises(SystemExit) as caught:
            self._guard({"GATEWAY_AGENT_BIND": "0.0.0.0",  # noqa: S104
                         "GATEWAY_BIND_FORBIDDEN": ""})
        self.assertIn("wildcard", str(caught.exception))


class ReconcilePacingTests(unittest.TestCase):
    """Polling and SPEAKING happen at different rates, and the gap is the design.

    The roster is cheap — one request to a sibling — so it is re-read often enough
    that a registration shows up while the operator is still looking. Dialling the
    servers is not cheap: every one is a container holding a write-capable credential.
    So a report follows a CHANGE, with a slow tick as the backstop for what changes
    without an operator."""

    def _loop(self, polls, interval=None):
        """Drive the loop once per poll result, capturing what it printed.

        ``polls`` are ``(roster_or_None, failure_text)`` pairs, exactly what
        ``discovery.poll`` returns. The stop Event is faked rather than timed: a test
        that slept would be pacing itself on the thing under test."""
        env = dict(GOOD)
        if interval is not None:
            env["GATEWAY_DISCOVERY_INTERVAL"] = interval
        gateway = load_tool_gateway(env)
        replies = iter(polls)
        pushed = []

        class FakeDiscovery:
            @staticmethod
            def poll():
                return next(replies)

            @staticmethod
            def roster_digest(roster):
                return json.dumps(roster, sort_keys=True)

            @staticmethod
            def reconcile_all(roster):
                return list(roster)

            @staticmethod
            def format_report(results):
                return [f"report:{len(results)}"]

            @staticmethod
            def push_inventory(_results):
                pushed.append(1)
                return ""

        class FakeStop:
            def __init__(self):
                self.waits = []

            def wait(self, delay):
                self.waits.append(delay)
                return len(self.waits) >= len(polls)

        gateway.discovery = FakeDiscovery
        stop, out = FakeStop(), io.StringIO()
        self.pushed = pushed
        with contextlib.redirect_stdout(out):
            gateway._reconcile_forever(stop)
        return stop.waits, out.getvalue()

    def test_the_loop_waits_the_roster_interval_not_the_report_interval(self):
        waits, _ = self._loop([(ROSTER_A, "")] * 3)
        gateway = load_tool_gateway(GOOD)
        self.assertEqual(waits, [gateway.ROSTER_INTERVAL] * 3)
        self.assertLess(gateway.ROSTER_INTERVAL, gateway.DISCOVERY_INTERVAL)

    def test_an_unchanged_roster_is_polled_but_not_re_reported(self):
        # The whole point of the split. Three polls, one report — otherwise a ten
        # second cadence would dial every credential-holding container six times a
        # minute to say nothing new.
        _, printed = self._loop([(ROSTER_A, "")] * 3)
        self.assertEqual(printed.count("report:"), 1)

    def test_a_changed_roster_reports_again(self):
        # Registering or enabling a server has to show up at the poll cadence, not at
        # the backstop — which is the papercut that prompted the split.
        _, printed = self._loop([(ROSTER_A, ""), (ROSTER_A, ""), (ROSTER_B, "")])
        self.assertEqual(printed.count("report:"), 2)

    def test_the_cold_start_race_is_reported_once_and_then_recovers(self):
        # No `depends_on`, and the control plane's healthcheck probes a different
        # listener than the tool bridge, so losing the first poll is expected. The
        # retry is the poll interval, so there is no third number for it.
        _, printed = self._loop([(None, "down")] * 3 + [(ROSTER_A, "")])
        self.assertEqual(printed.count("down"), 1)
        self.assertEqual(printed.count("report:"), 1)

    def test_losing_the_control_plane_is_announced_when_it_happens(self):
        # Flipping INTO failure is news. Reporting it only at the backstop would leave
        # up to five minutes in which the last thing said was a healthy report.
        _, printed = self._loop([(ROSTER_A, ""), (None, "down"), (None, "down")])
        self.assertEqual(printed.count("down"), 1)

    def test_a_persistent_outage_is_repeated_on_the_backstop_tick(self):
        # The quiet has to be BOUNDED. `GATEWAY_DISCOVERY_INTERVAL=0` makes every pass
        # due, standing in for a tick that has elapsed — a diagnostic that goes silent
        # exactly while something is broken is the failure this module exists to avoid.
        _, printed = self._loop([(None, "down")] * 3, interval="0")
        self.assertEqual(printed.count("down"), 3)

    def test_an_unchanged_roster_is_still_re_reported_on_the_backstop_tick(self):
        # What changes without an operator — a server restarting, an image bump adding
        # tools — moves nothing in the roster, so the digest alone would never notice.
        _, printed = self._loop([(ROSTER_A, "")] * 3, interval="0")
        self.assertEqual(printed.count("report:"), 3)

    def test_recovery_reports_even_when_the_roster_never_changed(self):
        # `reachable` flipping back has to trigger a report on its own: the digest is
        # unchanged across the outage, so without that check the first thing said after
        # a recovery would wait for the backstop.
        _, printed = self._loop([(ROSTER_A, ""), (None, "down"), (ROSTER_A, "")])
        self.assertEqual(printed.count("report:"), 2)


if __name__ == "__main__":
    unittest.main()


class ReconcileSurvivalTests(unittest.TestCase):
    """An exception inside one iteration costs one tick, not the thread.

    The loop is the only thing that refreshes the agent-facing listing and the control
    plane's inventory. Before this guard, anything `reconcile` did not catch — a
    server answering with a JSON list, say — propagated out of `_reconcile_forever`
    and ended the daemon thread while the process and `/healthz` stayed green, so a
    newly enabled server was never dialable and a disabled one kept its descriptor
    until a restart."""

    def test_a_raising_iteration_is_reported_and_the_loop_goes_on(self):
        gateway = load_tool_gateway(GOOD)
        calls = []

        class FakeDiscovery:
            @staticmethod
            def poll():
                return ([{"server": "s", "auth": {}, "tools": []}], "")

            @staticmethod
            def roster_digest(roster):
                return json.dumps(roster, sort_keys=True)

            @staticmethod
            def reconcile_all(roster):
                calls.append(1)
                if len(calls) == 1:
                    raise AttributeError("'list' object has no attribute 'get'")
                return list(roster)

            @staticmethod
            def format_report(results):
                return [f"report:{len(results)}"]

            @staticmethod
            def push_inventory(_results):
                return ""

        class FakeStop:
            waits = 0

            def wait(self, _delay):
                self.waits += 1
                return self.waits >= 2

        gateway.discovery = FakeDiscovery
        gateway.DISCOVERY_INTERVAL = 0  # the retry falls due at once
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gateway._reconcile_forever(FakeStop())
        printed = out.getvalue()
        self.assertEqual(len(calls), 2, "the loop did not come back for a second try")
        self.assertIn("reconcile failed (AttributeError", printed)
        self.assertIn("report:1", printed)


class BodyCapTests(unittest.TestCase):
    """The agent's body is read up to a cap and no further.

    Everything downstream of this listener buffers the whole message — `json.loads`,
    then the complete arguments POSTed to the control plane, then its request model —
    so an uncapped body was a sandbox-reachable OOM kill of the governance authority.
    Streamed, because Content-Length is the sender's claim: a chunked request has none
    and a lying one is caught only after the buffering it was meant to prevent."""

    class _Request:
        def __init__(self, chunks):
            self._chunks = chunks

        async def stream(self):
            for chunk in self._chunks:
                yield chunk

    def _read(self, chunks, cap):
        gateway = load_tool_gateway(GOOD)
        return asyncio.run(gateway.read_body(self._Request(chunks), cap=cap))

    def test_a_body_within_the_cap_is_read_whole(self):
        self.assertEqual(self._read([b"abc", b"def"], cap=6), b"abcdef")

    def test_a_body_over_the_cap_is_refused_at_the_chunk_that_crosses_it(self):
        gateway = load_tool_gateway(GOOD)
        seen = []

        class Counting(self._Request):
            async def stream(self):
                for chunk in self._chunks:
                    seen.append(chunk)
                    yield chunk

        with self.assertRaises(gateway.BodyTooLarge):
            asyncio.run(gateway.read_body(Counting([b"a" * 4, b"b" * 4, b"c" * 4]),
                                          cap=6))
        # The third chunk was never asked for: the read stops where the cap does.
        self.assertEqual(seen, [b"a" * 4, b"b" * 4])

    def test_the_default_cap_is_a_megabyte_and_comes_from_the_environment(self):
        self.assertEqual(load_tool_gateway(GOOD).BODY_MAX, 1024 * 1024)
        gateway = load_tool_gateway({**GOOD, "GATEWAY_BODY_MAX": "10"})
        self.assertEqual(gateway.BODY_MAX, 10)
        with self.assertRaises(gateway.BodyTooLarge):
            asyncio.run(gateway.read_body(self._Request([b"x" * 11])))

