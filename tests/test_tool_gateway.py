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

import contextlib
import io
import unittest

from _loader import load_tool_gateway

#: The configuration compose actually deploys. Written out rather than read from
#: compose: test_topology.py already holds those two equal, and re-deriving it here
#: would make this file pass for whatever compose happens to say.
GOOD = {"GATEWAY_AGENT_BIND": "172.30.0.11"}


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
    """Two paces, because the gateway deliberately starts before its authority.

    There is no `depends_on` on tool-gateway and the control plane's healthcheck
    probes a different listener than the tool bridge, so nothing in compose orders
    these two. The first pass losing that race is EXPECTED; waiting the steady
    interval afterwards is what made a normal cold start read as broken."""

    def _loop(self, answers):
        """Drive the loop once per answer, capturing what it printed and how it slept.

        The stop Event is faked rather than timed: asserting on the delay the loop
        ASKS for is the property, and a test that actually slept would be pacing
        itself on the thing under test."""
        gateway = load_tool_gateway(GOOD)
        replies = iter(answers)

        class FakeDiscovery:
            @staticmethod
            def run():
                return next(replies)

        class FakeStop:
            def __init__(self):
                self.waits = []

            def wait(self, delay):
                self.waits.append(delay)
                return len(self.waits) >= len(answers)

        gateway.discovery = FakeDiscovery
        stop, out = FakeStop(), io.StringIO()
        with contextlib.redirect_stdout(out):
            gateway._reconcile_forever(stop)
        return stop.waits, out.getvalue()

    def test_a_cold_start_retries_fast_rather_than_waiting_the_full_interval(self):
        # The bug this fixes: one refused connection at boot, then five minutes of a
        # log whose only line says the roster is unreachable.
        waits, _ = self._loop([(False, ["down"]), (False, ["down"])])
        gateway = load_tool_gateway(GOOD)
        self.assertEqual(waits, [gateway.STARTUP_RETRY] * 2)
        self.assertLess(gateway.STARTUP_RETRY, gateway.DISCOVERY_INTERVAL)

    def test_the_slow_interval_takes_over_once_the_authority_answers(self):
        # And STAYS taken over. Pacing on the latest result instead would drop back to
        # the fast retry whenever the control plane restarted, turning a brief outage
        # into a hot loop against the crown jewel.
        waits, _ = self._loop([(False, ["down"]), (True, ["ok"]), (False, ["down"])])
        gateway = load_tool_gateway(GOOD)
        self.assertEqual(waits, [gateway.STARTUP_RETRY,
                                 gateway.DISCOVERY_INTERVAL,
                                 gateway.DISCOVERY_INTERVAL])

    def test_the_cold_start_failure_is_said_once_not_every_retry(self):
        # At a 5s retry a slow control plane would otherwise print the same line
        # dozens of times and bury the success when it finally arrives.
        _, printed = self._loop([(False, ["down"])] * 4 + [(True, ["ok"])])
        self.assertEqual(printed.count("down"), 1)
        self.assertIn("ok", printed)

    def test_a_failure_after_the_authority_has_answered_is_always_printed(self):
        # The other direction, and the one that must not be optimised away: once the
        # control plane has worked, losing it is news. A diagnostic that goes quiet
        # exactly when something breaks is the failure this module exists to avoid.
        _, printed = self._loop([(True, ["ok"]), (False, ["down"]), (False, ["down"])])
        self.assertEqual(printed.count("down"), 2)


if __name__ == "__main__":
    unittest.main()
