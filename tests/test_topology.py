# SPDX-License-Identifier: Apache-2.0
"""Guards over the network topology in ``docker-compose.yml``.

The API-surface split is only half code. ``control-plane/app.py`` serves
``/authorize`` and the management API on two sockets, and
``tests/test_control_plane_api.py`` asserts which handler lands on which — but
what makes that worth anything is that the egress proxy has a route to one and
not the other, and that lives here, in compose.

Neither half is checkable from the other. The app cannot see the topology it runs
under, and compose cannot see which routes an app serves. So the coupling has no
compiler between its ends and a test stands in, the same arrangement as the
relay-allowlist guard in ``test_control_plane_ui_js.py``.

Text-parsed rather than YAML-parsed on purpose: the unit suite installs no
packages (see ``tests/_loader.py``), and PyYAML is not in the stdlib. The reader
below understands only nesting by indentation, which is all these assertions
need — it never has to interpret a scalar, so the folded ``entrypoint`` blocks
elsewhere in the file cannot confuse it.
"""
from __future__ import annotations

import ipaddress
import re
import unittest
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
# BOTH compose files, concatenated, because the Makefile's COMPOSE merges them with
# -f and the merged model is what actually runs. docker-compose.yml owns the
# topology; mcp-servers.yml owns the MCP server catalogue. Reading only the first
# would leave every placement rule below asserted against the file where servers are
# NOT declared — blind in exactly the place new services get added.
COMPOSE = [line
           for name in ("docker-compose.yml", "mcp-servers.yml")
           for line in (ROOT / name).read_text().splitlines()]
#: The catalogue alone, so the MCP placement rules below can be asserted over every
#: server it declares rather than over a list someone has to remember to extend.
MCP_COMPOSE = (ROOT / "mcp-servers.yml").read_text().splitlines()
ADDON = (ROOT / "proxies" / "egress" / "addon.py").read_text()
HOLDS = (ROOT / "control-plane" / "holds.py").read_text()
BOUNDARY = (ROOT / "sandbox-common" / "boundary-check.sh").read_text()
APP = (ROOT / "control-plane" / "app.py").read_text()
POLICY = (ROOT / "control-plane" / "policy.py").read_text()

#: The control plane's two listeners. Duplicated from app.py's defaults rather
#: than imported, deliberately: this file is asserting that compose and the app
#: AGREE, and importing the value from one side would make half the comparison
#: vacuous.
AUTHORIZE_PORT = 8091
MANAGE_PORT = 8090


def _block(lines: list[str], key: str, indent: int) -> list[str]:
    """The lines nested under ``key`` at ``indent`` spaces, exclusive of the key.

    Ends at the next line indented at or below ``indent`` that is not blank and
    not a comment — so a commented-out sibling cannot truncate a block early.
    """
    want = " " * indent + key + ":"
    out: list[str] = []
    inside = False
    for line in lines:
        if not inside:
            if line.rstrip() == want or line.startswith(want + " "):
                inside = True
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        if len(line) - len(line.lstrip(" ")) <= indent:
            break
        out.append(line)
    if not inside:
        raise AssertionError(f"no {key!r} block at indent {indent} in docker-compose.yml")
    return out


def _all_services() -> list[str]:
    """Every service declaration across BOTH compose files, as one body.

    Needed because COMPOSE is a concatenation and each file has its own top-level
    ``services:`` key — ``_block`` would stop at the second one and silently return
    only the first file's services. That failure mode is the dangerous direction:
    the rules below would pass by not seeing the services they exist to constrain.
    """
    bodies = [_block(COMPOSE[i:], "services", 0)
              for i, line in enumerate(COMPOSE) if line.rstrip() == "services:"]
    if not bodies:
        raise AssertionError("no services: block in either compose file")
    return [line for body in bodies for line in body]


def _service(name: str) -> list[str]:
    return _block(_all_services(), name, 2)


def _service_names() -> list[str]:
    """Every service key, so a rule can be asserted across the whole file rather
    than against a list that a new service silently escapes."""
    return [m.group(1)
            for line in _all_services()
            if (m := re.match(r"  ([A-Za-z0-9._-]+):\s*$", line))]


def _mcp_service_names() -> list[str]:
    """Every service the MCP catalogue declares.

    Derived, for the reason ``_service_names`` gives and one sharper: the placement
    rules below are the ENTIRE boundary around a container holding a credential the
    sandbox must not have, and mcp-servers.yml tells its reader that a new server
    "cannot escape them by being added quietly". A hardcoded tuple made that false —
    a second server added to the catalogue and not to the tuple was asserted about by
    nothing at all, and nothing would have said so.

    Raises on an empty result rather than returning one, the same fail-closed shape as
    the LAUNCHERS and SPDX globs in the Makefile: a catalogue that stops parsing must
    not turn every test below into a silent pass.
    """
    names = [m.group(1)
             for line in _block(MCP_COMPOSE, "services", 0)
             if (m := re.match(r"  ([A-Za-z0-9._-]+):\s*$", line))]
    if not names:
        raise AssertionError(
            "no services parsed out of mcp-servers.yml — the MCP placement rules "
            "would check nothing. Did the file move, or its indentation change?")
    return names


def _scalar(body: list[str], key: str) -> str | None:
    """A scalar key directly under a service block, or None if absent."""
    for line in body:
        m = re.match(rf"\s*{re.escape(key)}:\s*(\S+)", line)
        if m:
            return m.group(1)
    return None


def _networks_of(service: str) -> set[str]:
    """Network names a service attaches to — the keys directly under its
    ``networks:``, ignoring per-network settings like ``ipv4_address``."""
    body = _block(_service(service), "networks", 4)
    return {line.strip().rstrip(":") for line in body
            if line.startswith(" " * 6) and len(line) - len(line.lstrip(" ")) == 6
            and line.strip() and not line.strip().startswith("#")}


def _environment_of(service: str) -> dict[str, str]:
    body = _block(_service(service), "environment", 4)
    env = {}
    for line in body:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        k, _, v = stripped.partition(":")
        env[k.strip()] = v.strip()
    return env


def _subnet_of(network: str) -> str:
    body = _block(_block(COMPOSE, "networks", 0), network, 2)
    for line in body:
        m = re.search(r"subnet:\s*(\S+)", line)
        if m:
            return m.group(1)
    raise AssertionError(f"network {network!r} declares no subnet")


class ProxyReachabilityTests(unittest.TestCase):
    """What the egress proxy can and cannot dial.

    The proxy is the component whose compromise this whole split is designed
    around: its relay guard is best-effort against DNS rebinding, so the design
    assumes the guard can be beaten and arranges for the far side to be worth
    little. That only holds while the proxy's route list stays this short."""

    def test_the_proxy_has_no_route_to_the_management_network(self):
        # THE assertion. Re-adding control-net here would restore the
        # self-approval path in full, and nothing else in the repo would notice:
        # every service keeps working, every health check stays green.
        nets = _networks_of("egress-proxy")
        self.assertNotIn("control-net", nets)
        self.assertNotIn("control-ui-net", nets)

    def test_the_proxy_reaches_the_control_plane_only_over_authorize_net(self):
        self.assertIn("authorize-net", _networks_of("egress-proxy"))

    def test_the_proxy_asks_the_authorize_port_not_the_management_port(self):
        url = _environment_of("egress-proxy")["EGRESS_CONTROL_PLANE_URL"]
        self.assertTrue(url.endswith(f":{AUTHORIZE_PORT}"), url)
        self.assertNotIn(str(MANAGE_PORT), url)

    def test_the_proxy_is_still_the_only_bridge_off_the_sandbox_net(self):
        # Context for the above: the proxy keeps its dual-homing (sandbox-net +
        # egress-net). Losing that would not be a security regression, but it
        # would mean these tests are guarding a component that no longer does
        # the job they assume, so assert the premise rather than imply it.
        self.assertLessEqual({"sandbox-net", "egress-net"},
                             _networks_of("egress-proxy"))


class McpServerPlacementTests(unittest.TestCase):
    """Where MCP server containers sit, which is their entire boundary.

    Each one holds a write-capable credential the sandbox is not allowed to have.
    Nothing in the container stops the agent using it — only the fact that the
    agent has no route to the container does. A stray `sandbox-net:` here would
    hand the agent the credential directly and make the gateway's per-tool policy
    decorative, and every health check would stay green while it did."""

    #: Every server the catalogue declares — derived, never listed, so adding one to
    #: mcp-servers.yml is what subjects it to the rules below. See _mcp_service_names.
    MCP_SERVERS: ClassVar[tuple[str, ...]] = tuple(_mcp_service_names())

    def test_mcp_servers_never_join_the_agent_network(self):
        for svc in self.MCP_SERVERS:
            with self.subTest(service=svc):
                self.assertEqual(_networks_of(svc), {"mcp-net"})

    def test_mcp_servers_have_no_egress_of_their_own(self):
        # They reach the internet only by asking the proxy. Attaching egress-net
        # here would be a second way off-box, held by the one process in this
        # design carrying a credential worth stealing.
        for svc in self.MCP_SERVERS:
            with self.subTest(service=svc):
                self.assertNotIn("egress-net", _networks_of(svc))

    def test_mcp_servers_are_told_to_use_the_proxy(self):
        # The placement above makes the proxy the only reachable destination; this
        # is what makes the server actually use it rather than merely fail.
        for svc in self.MCP_SERVERS:
            with self.subTest(service=svc):
                env = _environment_of(svc)
                for var in ("HTTP_PROXY", "HTTPS_PROXY"):
                    self.assertIn("egress-proxy", env.get(var, ""))

    def test_the_proxy_can_be_reached_from_the_mcp_network(self):
        # The premise the three assertions above depend on: if the proxy ever
        # leaves mcp-net, the servers do not fall back to direct egress — they
        # stop working — but the tests would still pass while claiming otherwise.
        self.assertIn("mcp-net", _networks_of("egress-proxy"))

    def test_no_variable_is_required_at_interpolation_time(self):
        # Not an MCP-specific rule, and not a new one: the compose file has said so
        # in prose since the llm-* profiles landed (see the DOCKADE_LLM_MODEL and
        # DOCKADE_RENDER_GID notes), because interpolation runs over the whole file
        # before profiles select services. A `:?` in a profile nobody enabled takes
        # `build` and `up` down for the infra services. Prose did not stop it being
        # reintroduced, so this is the guard.
        offenders = [ln.strip() for ln in COMPOSE if re.search(r"\$\{[^}]+:\?", ln)]
        self.assertEqual(offenders, [], "use ${VAR:-}, not ${VAR:?}")

    def test_a_memory_cap_always_disables_swap(self):
        # Also not MCP-specific, and also learned by breaking it. Docker defaults
        # the swap ceiling to 2x memory, so `mem_limit` ALONE is half a cap — the
        # container gets its RAM again in swap, on a host where a ceiling above
        # what exists is no ceiling at all. Uncapped is a legitimate choice here
        # (the llm-* services argue for it); capped-but-swappable is the shape that
        # is never intended, and it is invisible in review because the line that
        # would say so is the one that is missing.
        for svc in _service_names():
            body = _service(svc)
            mem = _scalar(body, "mem_limit")
            if mem is None:
                continue            # uncapped on purpose — see the llm-* services
            with self.subTest(service=svc):
                self.assertEqual(_scalar(body, "memswap_limit"), mem,
                                 f"{svc}: mem_limit {mem} without a matching "
                                 "memswap_limit is really a 2x cap")

    def test_the_probed_mcp_address_is_the_one_compose_pins(self):
        # boundary-check.sh proves the sandbox has no route to mcp-net by dialing a
        # LITERAL address, and that probe means something only because the proxy is
        # really listening on it. If compose stops pinning the address, or pins a
        # different one, the probe keeps passing — against nothing. The two files
        # cannot see each other, so this is where they are held together.
        pinned = _scalar(_block(_service("egress-proxy"), "mcp-net", 6),
                         "ipv4_address")
        self.assertIsNotNone(pinned, "the proxy's mcp-net leg has no fixed address")
        self.assertIn(f"http://{pinned}:8080", BOUNDARY,
                      "boundary-check.sh probes an address compose does not pin")

    def test_every_mcp_server_pins_its_address(self):
        # The egress proxy identifies a caller by peer address, because that is all
        # a TCP connection carries. A dynamic address is therefore not an identity:
        # Docker reassigns in start order, so a restarted server can inherit another
        # server's address and have its egress decided under that server's rules.
        # An unpinned server does not fail — it just quietly stops being separable,
        # which is why the guard is here rather than left to review.
        for svc in self.MCP_SERVERS:
            with self.subTest(service=svc):
                pinned = _scalar(_block(_service(svc), "mcp-net", 6), "ipv4_address")
                self.assertIsNotNone(pinned, f"{svc} does not pin an mcp-net address")
                self.assertIn(
                    ipaddress.ip_address(pinned),
                    ipaddress.ip_network(_subnet_of("mcp-net")),
                    f"{svc} pins {pinned}, which is outside mcp-net")

    def test_no_two_mcp_containers_claim_the_same_address(self):
        # Including the proxy's own leg. Two containers pinned to one address is a
        # startup failure for the second — loud, and therefore not the danger. The
        # danger is a server pinned to the PROXY's address, which would collide with
        # the one endpoint every server depends on, and boundary-check.sh probes.
        claimed = [_scalar(_block(_service(svc), "mcp-net", 6), "ipv4_address")
                   for svc in (*self.MCP_SERVERS, "egress-proxy")]
        self.assertEqual(len(claimed), len(set(claimed)),
                         f"two containers pin the same mcp-net address: {claimed}")

    def test_the_mcp_network_is_inside_the_range_the_relay_guard_blocks(self):
        # Why the proxy's leg is inbound-only in effect: mcp-net sits in the
        # private range the guard hard-blocks, so the proxy refuses it as a
        # CONNECT target and cannot be asked to relay the agent into the servers.
        self.assertTrue(
            ipaddress.ip_network(_subnet_of("mcp-net")).subnet_of(
                ipaddress.ip_network("172.16.0.0/12")))


class AddressAllocationTests(unittest.TestCase):
    """Per network: pin every member's address, or pin none of them.

    Mixing the two is a reboot-order race, and it does not look like one in review.
    Docker hands a dynamic member the LOWEST free address in the subnet, which is
    the same ``.2`` a pinned member is most likely to have asked for; whoever the
    daemon starts first takes it and the other dies with "Address already in use".
    `depends_on` does not help — it orders `compose up`, not the daemon bringing
    `restart: always` containers back after a host reboot, which is the only time
    the order differs from the one that was tested.

    Scoped to attachments DECLARED IN COMPOSE. The sandboxes are dynamic on
    sandbox-net by design: the launchers start them long after the substrate holds
    its pinned addresses, so they can only ever be handed what is left."""

    @staticmethod
    def _legs() -> dict[str, dict[str, str | None]]:
        """network -> {service: pinned address or None} over both compose files."""
        legs: dict[str, dict[str, str | None]] = {}
        for svc in _service_names():
            for net in _raw_networks(svc):
                legs.setdefault(net, {})[svc] = _scalar(
                    _block(_service(svc), net, 6), "ipv4_address")
        return legs

    def test_a_network_with_any_fixed_address_fixes_them_all(self):
        for net, members in self._legs().items():
            if not any(members.values()):
                continue        # all-dynamic is fine — nobody has a claim to lose
            with self.subTest(network=net):
                floating = sorted(s for s, addr in members.items() if addr is None)
                self.assertEqual(
                    floating, [],
                    f"{net} mixes fixed and dynamic addresses: {floating} float "
                    f"while {sorted(s for s, a in members.items() if a)} are pinned")

    def test_every_fixed_address_is_inside_its_network(self):
        # A pin outside the subnet is refused at start; a pin inside the WRONG
        # network's subnet is the typo this catches, and it fails the same way the
        # race above does — at boot, on the host, not here.
        for net, members in self._legs().items():
            subnet = ipaddress.ip_network(_subnet_of(net)) if any(
                members.values()) else None
            for svc, addr in members.items():
                if addr is None:
                    continue
                with self.subTest(network=net, service=svc):
                    self.assertIn(ipaddress.ip_address(addr), subnet,
                                  f"{svc} pins {addr}, outside {net} ({subnet})")


class ControlPlaneBindTests(unittest.TestCase):
    """The management listener is out of the proxy's reach because of WHERE it
    binds, not merely which port it uses. Both halves are asserted here because
    either one alone is insufficient: a wildcard bind serves the management API
    on authorize-net whatever the port, and a shared port serves it to whoever
    can reach the address."""

    def test_the_management_listener_binds_a_control_net_address(self):
        bind = _environment_of("control-plane")["CONTROL_MANAGE_BIND"]
        self.assertNotIn(bind, ("0.0.0.0", "::", "*", ""))  # noqa: S104
        net = _subnet_of("control-net").split("/")[0].rsplit(".", 1)[0]
        self.assertTrue(bind.startswith(net + "."),
                        f"CONTROL_MANAGE_BIND {bind} is not on control-net")

    def test_the_control_plane_spans_both_control_networks(self):
        # It is the one service that does, and that is the whole design: one
        # process, one surface per network.
        self.assertEqual(_networks_of("control-plane"),
                         {"control-net", "authorize-net"})

    def test_the_ui_talks_to_the_management_port_over_control_net(self):
        self.assertEqual(_networks_of("control-plane-ui"),
                         {"control-net", "control-ui-net"})
        url = _environment_of("control-plane-ui")["CONTROL_BACKEND_URL"]
        self.assertTrue(url.endswith(f":{MANAGE_PORT}"), url)

    def test_nothing_else_joins_the_authorize_network(self):
        # authorize-net carries exactly one conversation. A third member would
        # be a component that can reach /authorize without anyone deciding it
        # should, so the roster is asserted rather than the absence of any
        # particular service.
        members = {name.strip().rstrip(":")
                   for name in _block(COMPOSE, "services", 0)
                   if len(name) - len(name.lstrip(" ")) == 2
                   and name.strip().endswith(":")
                   and "authorize-net" in _raw_networks(name.strip().rstrip(":"))}
        self.assertEqual(members, {"egress-proxy", "control-plane"})


def _raw_networks(service: str) -> set[str]:
    """``_networks_of`` for a service that may declare no networks at all (the
    profile-gated llm-* variants do declare some; a future one might not)."""
    try:
        return _networks_of(service)
    except AssertionError:
        return set()


class RestartPolicyTests(unittest.TestCase):
    """`always` on the substrate, `unless-stopped` on the optional tier-2 model
    server. The line is not stylistic: the two policies differ in exactly one case
    — whether a container that was down when the daemon stopped comes back when it
    starts — and that case cost this stack its governance authority across one
    ordinary power cycle, silently, with the proxy still up and reporting healthy.

    Asserted rather than left to review because the failure is invisible: a service
    demoted to `unless-stopped` behaves identically until the one reboot where it
    does not come back."""

    def _restart_of(self, service: str) -> str:
        for line in _service(service):
            m = re.match(r"\s*restart:\s*(\S+)", line)
            if m:
                return m.group(1)
        raise AssertionError(f"{service} declares no restart policy at all")

    def test_the_infra_services_always_come_back(self):
        for service in ("egress-proxy", "control-plane", "control-plane-ui"):
            self.assertEqual(self._restart_of(service), "always", service)

    def test_the_model_server_honours_a_deliberate_stop(self):
        # Stopping llm-intel to reclaim the shared memory pool is an ordinary
        # operator action, so it must NOT be resurrected by a reboot. This is the
        # other half of the line above — without it, "always everywhere" would
        # satisfy the test above and quietly break a real workflow.
        self.assertEqual(self._restart_of("llm-intel"), "unless-stopped")


class RelayGuardAgreesWithComposeTests(unittest.TestCase):
    """The proxy's relay guard hard-blocks the control subnets by CIDR, and those
    CIDRs are written twice — once as a compose subnet, once as a default in
    ``addon.py``. Renaming or renumbering a network on one side would leave the
    guard pointed at an address range nothing uses, which fails open silently:
    the proxy starts, ``_assert_guard_configured`` passes on a non-empty list, and
    the blocked range is simply the wrong one."""

    def test_the_guard_default_covers_every_control_subnet(self):
        default = re.search(
            r'"EGRESS_FORBIDDEN_CIDRS",\s*\n?\s*"([^"]+)"', ADDON)
        self.assertIsNotNone(default, "could not find the FORBIDDEN_CIDRS default")
        listed = {c.strip() for c in default.group(1).split(",")}
        for network in ("control-net", "authorize-net"):
            self.assertIn(_subnet_of(network), listed,
                          f"{network}'s subnet is not in the relay guard's default")

    def test_the_lifeline_range_is_the_sandbox_network(self):
        # Same two-ends-no-compiler problem as the guard above, opposite sign: this
        # CIDR decides who KEEPS a grant rather than who is refused one. Pointed at
        # a subnet nothing uses it fails safe but silently — the lifeline stops
        # working for the agent and every Anthropic call starts depending on the
        # control plane being up, which looks like an outage, not a config error.
        # Pointed at too WIDE a range it hands the local allow to mcp-net, which is
        # the thing _is_lifeline exists to prevent.
        default = re.search(r'"EGRESS_LIFELINE_CIDRS",\s*\n?\s*"([^"]+)"', ADDON)
        self.assertIsNotNone(default, "could not find the LIFELINE_CIDRS default")
        listed = {c.strip() for c in default.group(1).split(",")}
        self.assertEqual(listed, {_subnet_of("sandbox-net")})

    def test_no_other_network_is_inside_the_lifeline_range(self):
        # The assertion the one above cannot make on its own: equality with
        # sandbox-net's subnet is only meaningful while no other network is a
        # subnet of it. mcp-net is the one this matters for today.
        default = re.search(r'"EGRESS_LIFELINE_CIDRS",\s*\n?\s*"([^"]+)"', ADDON)
        lifeline = [ipaddress.ip_network(c.strip())
                    for c in default.group(1).split(",")]
        for network in ("mcp-net", "control-net", "authorize-net"):
            subnet = ipaddress.ip_network(_subnet_of(network))
            for granted in lifeline:
                self.assertFalse(
                    subnet.subnet_of(granted),
                    f"{network} sits inside the permanent lifeline's client range")

    def test_every_client_class_range_is_a_real_compose_subnet(self):
        # Third instance of the same two-ends-no-compiler problem, and the one with
        # the quietest failure. A class range that matches no network places nobody:
        # its clients fall through to UNCLASSIFIED, match no rule, and are HELD — so
        # a renumbered subnet does not break governance, it makes every request from
        # that population wait for a human who has no idea why they are being asked.
        default = re.search(
            r'CLIENT_CLASSES_DEFAULT = "([^"]+)"', POLICY)
        self.assertIsNotNone(default, "could not find the CLIENT_CLASSES default")
        subnets = {_subnet_of(n) for n in ("sandbox-net", "mcp-net", "control-net",
                                           "authorize-net")}
        mapped = {}
        for entry in default.group(1).split(","):
            name, _, cidr = entry.partition("=")
            self.assertIn(cidr.strip(), subnets,
                          f"client class {name!r} maps to {cidr!r}, which is not a "
                          f"subnet any network in compose declares")
            mapped[name.strip()] = cidr.strip()
        # The two populations the egress proxy actually serves, each named. The proxy
        # has a leg on both, so both reach /authorize and both must be placeable —
        # an unmapped one is the union-of-needs problem returning by omission.
        self.assertEqual(mapped, {"sandbox": _subnet_of("sandbox-net"),
                                  "mcp": _subnet_of("mcp-net")})

    def test_the_class_ranges_do_not_overlap(self):
        # First match wins, so an overlap would silently place one network's clients
        # in another's class and decide their requests under rules written for
        # somebody else — least privilege inverted rather than merely weakened.
        default = re.search(r'CLIENT_CLASSES_DEFAULT = "([^"]+)"', POLICY)
        nets = [(e.split("=")[0], ipaddress.ip_network(e.split("=")[1]))
                for e in default.group(1).split(",")]
        for i, (name_a, a) in enumerate(nets):
            for name_b, b in nets[i + 1:]:
                self.assertFalse(a.overlaps(b),
                                 f"client classes {name_a} and {name_b} overlap")

    def test_the_agents_class_is_the_one_the_lifeline_is_scoped_to(self):
        # Two independent client checks — the proxy's local lifeline allow and the
        # control plane's rule scoping — and they have to be about the same network,
        # because both derive from the same sentence: only sandbox-net runs the agent.
        # Drifting apart would mean the population that keeps the Anthropic lifeline
        # is not the population the agent's own rules were written for.
        classes = re.search(r'CLIENT_CLASSES_DEFAULT = "([^"]+)"', POLICY).group(1)
        lifeline = re.search(
            r'"EGRESS_LIFELINE_CIDRS",\s*\n?\s*"([^"]+)"', ADDON).group(1)
        agent_range = dict(e.split("=") for e in classes.split(","))["sandbox"]
        self.assertEqual({agent_range}, {c.strip() for c in lifeline.split(",")})

    def test_the_networks_the_guard_blocks_are_internal(self):
        # An internal bridge has no route off-box. Both control networks must be one,
        # or the control plane itself would gain egress. mcp-net is here for the same
        # reason wearing a different hat: its internal-ness is what makes the egress
        # proxy the only thing an MCP server container can reach, so a server that
        # ignores HTTPS_PROXY fails loudly instead of quietly going direct — from a
        # container holding a write-capable credential. Placement is that boundary;
        # nothing inside those containers enforces it.
        for network in ("control-net", "authorize-net", "sandbox-net", "mcp-net"):
            body = _block(_block(COMPOSE, "networks", 0), network, 2)
            self.assertTrue(
                any(re.match(r"\s*internal:\s*true\s*$", line) for line in body),
                f"network {network} is not internal: true")


class ControlPlaneModulesAreShippedTests(unittest.TestCase):
    """Every module ``app.py`` sits beside is COPYed into the image.

    Same shape as the rest of this file — two ends with no compiler between them.
    ``app.py`` imports its siblings by plain name, which the unit suite satisfies
    from the source tree, so a module that no ``COPY`` line mentions passes every
    test here and then raises ``ImportError`` the moment the container starts. The
    build cannot catch it either: a missing COPY is a file that is simply absent,
    not an error.

    Asserted over the DIRECTORY rather than a list, so adding the sixth module is
    what trips it — a list would have to be remembered in the same breath as the
    COPY line it exists to compensate for."""

    CP_DIR: ClassVar[Path] = ROOT / "control-plane"
    DOCKERFILE: ClassVar[str] = (ROOT / "control-plane" / "Dockerfile").read_text()

    def test_every_sibling_module_has_a_copy_line(self):
        for path in sorted(self.CP_DIR.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertIn(f"COPY control-plane/{path.name} ", self.DOCKERFILE,
                              f"{path.name} sits next to app.py but no COPY line "
                              f"puts it in the image — it would ImportError at "
                              f"container start")


class BackupToolTargetsAgreeTests(unittest.TestCase):
    """`make backup` / `make restore` reach the store through `docker run`, so the
    image, the volume and the mount path are ARGUMENTS rather than things compose
    hands them — three names restated in the Makefile that compose owns.

    They are the only place in the repo where compose's own identifiers are spelled
    out somewhere compose never reads, which is what makes this the file's usual
    shape: two ends, no compiler between them. Renaming the volume in compose leaves
    both targets pointed at a name docker will happily CREATE as an empty volume, so
    the drift does not fail loudly — `backup` reports there is nothing to back up,
    and a `restore` into it succeeds against a store nothing serves."""

    MAKEFILE: ClassVar[str] = (ROOT / "Makefile").read_text()

    def _make_var(self, name: str) -> str:
        m = re.search(rf"^{name}\s*:?=\s*(\S+)", self.MAKEFILE, re.M)
        self.assertIsNotNone(m, f"no {name} in the Makefile — renamed or removed")
        return m.group(1)

    def test_the_image_is_the_one_compose_builds(self):
        declared = [line.split(":", 1)[1].strip()
                    for line in _service("control-plane")
                    if line.strip().startswith("image:")]
        self.assertEqual(declared, [self._make_var("CONTROL_IMAGE")])

    def test_the_volume_is_the_one_compose_names(self):
        # The top-level volume's `name:`, not its key: `name:` is what docker sees,
        # and it is what `docker run -v` has to be given.
        names = [line.split(":", 1)[1].strip()
                 for line in _block(_block(COMPOSE, "volumes", 0), "control-state", 2)
                 if line.strip().startswith("name:")]
        self.assertEqual(names, [self._make_var("CONTROL_VOLUME")])

    def test_the_mount_path_is_where_the_service_mounts_it(self):
        # Where the app looks for the store (store.DB_PATH's default is under it), so
        # a `docker run` that mounts the volume anywhere else backs up an empty
        # directory and reports success.
        mounts = [line.strip().lstrip("- ") for line in _service("control-plane")
                  if line.strip().startswith("- control-state:")]
        self.assertEqual(len(mounts), 1, "control-plane's control-state mount moved")
        path = mounts[0].split(":", 1)[1]
        self.assertIn(f"-v $(CONTROL_VOLUME):{path}", self.MAKEFILE,
                      f"compose mounts the store at {path}; the Makefile's "
                      f"`docker run` mounts it somewhere else")

    def test_the_run_container_joins_no_network(self):
        # Not a preference. control-plane pins its addresses on both its networks, so
        # a second container that joins them collides with the running one — which is
        # the bug these targets shipped with. The store is reached through the volume;
        # nothing about that needs a route.
        m = re.search(r"^CONTROL_TOOL\s*=\s*(.*(?:\\\n.*)*)", self.MAKEFILE, re.M)
        self.assertIsNotNone(m, "no CONTROL_TOOL in the Makefile — renamed or removed")
        self.assertIn("--network none", m.group(1))


class AppPortDefaultsAgreeTests(unittest.TestCase):
    """Close the third edge of the port triangle. The constants above are checked
    against compose, and compose sets NEITHER ``CONTROL_AUTHORIZE_PORT`` nor
    ``CONTROL_MANAGE_PORT`` — so the APP's own defaults are the deployed ports, yet
    nothing asserted they still match. A drift there would pass every
    compose-vs-literal check here while the healthcheck (which hardcodes 8091) and the
    proxy failed at deploy. Text-parsed, to keep this file free of the app import (see
    the module docstring)."""

    def _default(self, name: str) -> int:
        m = re.search(rf'{name}",\s*"(\d+)"', APP)
        self.assertIsNotNone(m, f"no default for {name} in control-plane/app.py")
        return int(m.group(1))

    def test_authorize_port_default_matches_compose(self):
        self.assertEqual(self._default("CONTROL_AUTHORIZE_PORT"), AUTHORIZE_PORT)

    def test_manage_port_default_matches_compose(self):
        self.assertEqual(self._default("CONTROL_MANAGE_PORT"), MANAGE_PORT)


class ProxyOutlastsTheHoldWindowTests(unittest.TestCase):
    """The proxy must still be waiting when the hold window closes.

    ``/authorize`` blocks for up to ``CONTROL_HOLD_TIMEOUT`` while a human decides, and
    the proxy abandons that call after ``EGRESS_CONTROL_TIMEOUT``. Order them the wrong
    way and the proxy gives up on a request whose card is still on the operator's screen:
    the agent is denied by a race rather than by a decision, and the click that follows
    resolves an approval with nobody left to serve. The gap between the two covers the
    round trip.

    The constants live in DIFFERENT IMAGES and neither process can read the other's — the
    same no-compiler-between-the-ends situation as the rest of this file. Compose
    overrides neither today, so the source defaults are what deploys (the reasoning for
    leaving them in the environment at all is under "Hold bounds are fail-closed, so
    their values stay env vars" in DESIGN.md). The override is read anyway, so setting
    one later does not silently retire this guard.
    """

    @staticmethod
    def _default(text: str, name: str, where: str) -> float:
        m = re.search(rf'{re.escape(name)}",\s*"([0-9.]+)"', text)
        if m is None:
            raise AssertionError(f"no default for {name} in {where}")
        return float(m.group(1))

    def _effective(self, service: str, name: str, text: str, where: str) -> float:
        override = _environment_of(service).get(name)
        if override is None:
            return self._default(text, name, where)
        try:
            return float(override)
        except ValueError:
            self.fail(f"{service} sets {name}={override!r} in compose, which this guard "
                      f"cannot compare as a number — so it can no longer tell whether "
                      f"the proxy outlasts the hold window. Inline the value or teach "
                      f"this test the form.")

    def test_the_proxy_waits_longer_than_the_hold_window(self):
        proxy = self._effective("egress-proxy", "EGRESS_CONTROL_TIMEOUT",
                                ADDON, "proxies/egress/addon.py")
        hold = self._effective("control-plane", "CONTROL_HOLD_TIMEOUT",
                               HOLDS, "control-plane/holds.py")
        self.assertGreater(
            proxy, hold,
            f"the proxy gives up on /authorize after {proxy}s but a hold can run "
            f"{hold}s, so a held request dies on the proxy side while its card is "
            f"still pending — raise EGRESS_CONTROL_TIMEOUT or lower "
            f"CONTROL_HOLD_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
