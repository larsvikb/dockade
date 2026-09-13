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
            with self.subTest(bind=addr):
                with self.assertRaises(SystemExit):
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


if __name__ == "__main__":
    unittest.main()
