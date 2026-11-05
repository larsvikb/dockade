# SPDX-License-Identifier: Apache-2.0
"""Guards over the network topology in ``docker-compose.yml``.

The API-surface split is only half code. ``control-plane/app.py`` serves
``/authorize``, the MCP gateway's bridge and the management API on separate
sockets, and ``tests/test_control_plane_api.py`` asserts which handler lands on
which — but what makes that worth anything is that each enforcer has a route to its
own bridge and to nothing else, and that lives here, in compose.

Neither half is checkable from the other. The app cannot see the topology it runs
under, and compose cannot see which routes an app serves. So the coupling has no
compiler between its ends and a test stands in, the same arrangement as the
relay-allowlist guard in ``test_control_plane_ui_js.py``.

Text-parsed rather than YAML-parsed on purpose: the unit suite installs no
packages (see ``tests/_loader.py``), and PyYAML is not in the stdlib. The reader
below, ``_block``, understands only nesting by indentation, which is all these
assertions need — it never has to interpret a scalar, so the folded ``entrypoint``
blocks elsewhere in the file cannot confuse it. Of the readers that take a scalar as a
path, ``_host_sources`` refuses a value it cannot follow rather than passing it;
``_mount_spellings`` and ``_long_mount`` read a value as written, one layer of quotes off.
``ComposeFormTests`` holds both files to the subset of YAML where reading by
indentation and Compose's reading agree, and names the readers shown to take a key at
their own depth; the others are not yet anchored (task 240).
"""
from __future__ import annotations

import ast
import ipaddress
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
#: The files, in the Makefile's -f order. ComposeFormTests pins the Makefile to this
#: tuple, so the reader and what runs cannot name different files.
COMPOSE_FILES = ("docker-compose.yml", "mcp-servers.yml")
# BOTH compose files, concatenated, because the Makefile's COMPOSE merges them with
# -f and the merged model is what actually runs. docker-compose.yml owns the
# topology; mcp-servers.yml owns the MCP server catalogue. Reading only the first
# would leave every placement rule below asserted against the file where servers are
# NOT declared — blind in exactly the place new services get added.
COMPOSE = [line
           for name in COMPOSE_FILES
           for line in (ROOT / name).read_text().splitlines()]
#: The catalogue alone, so the MCP placement rules below can be asserted over every
#: server it declares rather than over a list someone has to remember to extend.
MCP_COMPOSE = (ROOT / "mcp-servers.yml").read_text().splitlines()
ADDON = (ROOT / "proxies" / "egress" / "addon.py").read_text()
HOLDS = (ROOT / "control-plane" / "holds.py").read_text()
BOUNDARY = (ROOT / "sandbox-common" / "boundary-check.sh").read_text()
APP = (ROOT / "control-plane" / "app.py").read_text()
POLICY = (ROOT / "control-plane" / "policy.py").read_text()

#: The control plane's three listeners. Duplicated from app.py's defaults rather
#: than imported, deliberately: this file is asserting that compose and the app
#: AGREE, and importing the value from one side would make half the comparison
#: vacuous.
AUTHORIZE_PORT = 8091
MANAGE_PORT = 8090
TOOL_PORT = 8092


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
        raise AssertionError(f"no {key!r} block at indent {indent} in the compose files")
    return out


def _all_services() -> list[str]:
    """Every service declaration across BOTH compose files, as one body.

    Needed because COMPOSE is a concatenation and each file has its own top-level
    ``services:`` key — ``_block`` would stop at the second one and silently return
    only the first file's services. That failure mode is the dangerous direction:
    the rules below would pass by not seeing the services they exist to constrain.
    """
    body = _top_level("services")
    if not body:
        raise AssertionError("no services: block in either compose file")
    return body


def _top_level(key: str) -> list[str]:
    """Every top-level ``key:`` block across both compose files, as one body; empty
    when neither file declares one."""
    return [inner
            for i, line in enumerate(COMPOSE)
            if line.rstrip() == f"{key}:" or line.startswith(f"{key}: ")
            for inner in _block(COMPOSE[i:], key, 0)]


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
    """A scalar key directly under a block, at its children's indent, or None if
    absent. Never one nested deeper: a label or a long-form mount can spell any key."""
    indents = [len(line) - len(line.lstrip(" ")) for line in body
               if line.strip() and not line.strip().startswith("#")]
    if not indents:
        return None
    child = " " * min(indents)
    for line in body:
        m = re.match(rf"{child}{re.escape(key)}:\s*(\S+)", line)
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
        stripped = _uncommented(line)
        if not stripped or ":" not in stripped:
            continue
        k, _, v = stripped.partition(":")
        env[k.strip()] = v.strip()
    return env


def _pin(service: str, network: str) -> str | None:
    """The address a service pins on one of its networks, or None."""
    return _scalar(_block(_block(_service(service), "networks", 4), network, 6),
                   "ipv4_address")


def _probe(service: str) -> str:
    """A service's healthcheck ``test``, its comments left out: a comment line, and a
    `` # ...`` after an item. A ``|``/``>`` item's lines are text, ``#`` and all."""
    test = _block(_block(_service(service), "healthcheck", 4), "test", 6)
    out, in_text = [], False
    for line in test:
        if in_text and line.startswith(" " * 10):
            out.append(line)
            continue
        in_text = re.match(r" {8}- [|>]", line) is not None
        if in_text:
            out.append(line)
        elif not line.strip().startswith("#"):
            out.append(_uncommented(line))
    return "".join(out)


def _volume_items(service: str) -> list[list[str]]:
    """Each item of a service's ``volumes:``, as its lines, comments left out."""
    body = _service(service)
    if not any(re.match(r"    volumes:", ln) for ln in body):
        return []
    items: list[list[str]] = []
    for line in _block(body, "volumes", 4):
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("      - "):
            items.append([line.replace("- ", "  ", 1)])
        elif items:
            items[-1].append(line)
    return items


def _unquoted(value: str) -> str:
    """One layer of matching quotes stripped, and nothing else: a ``${VAR:-d}`` stays
    as written, unlike ``_plain_value``."""
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _item_keys(item: list[str], indent: int) -> dict[str, str]:
    """The keys of a mapping's lines at exactly ``indent``, values uncommented and
    unquoted."""
    return {m.group(1): _unquoted(_uncommented(m.group(2) or "")) for line in item
            if (m := re.match(rf" {{{indent}}}([A-Za-z0-9_.-]+):(?:\s+(.*))?$", line))}


def _mount_spellings(service: str) -> list[str]:
    """Every volume a service mounts, in the short form's ``source:target[:ro]``: a
    short-form item as written, a long-form one from its own keys."""
    out = []
    for item in _volume_items(service):
        keys = _item_keys(item, 8)
        if not keys:
            out.append(_unquoted(_uncommented(item[0])))
        elif "source" in keys and "target" in keys:
            ro = ":ro" if keys.get("read_only") == "true" else ""
            out.append(f"{keys['source']}:{keys['target']}{ro}")
    return out


def _network(name: str) -> list[str]:
    return _block(_block(COMPOSE, "networks", 0), name, 2)


def _is_internal(network: str) -> bool:
    return _scalar(_network(network), "internal") == "true"


def _subnet_of(network: str) -> str:
    subnet = _ipam_of(network, "subnet")
    if subnet is None:
        raise AssertionError(f"network {network!r} declares no subnet")
    return subnet


def _ipam_of(network: str, key: str) -> str | None:
    """A key (``subnet``, ``ip_range``, ``gateway``) of a network's first IPAM config
    entry, or None."""
    config = _block(_block(_network(network), "ipam", 4), "config", 6)
    entries = 0
    for line in config:
        entries += line.startswith("        - ")
        if entries > 1:
            break
        m = re.match(rf"        (?:- |  ){re.escape(key)}:\s*(\S+)", line)
        if m:
            return m.group(1)
    return None


def _masquerade_off(network: str) -> bool:
    opts = _block(_network(network), "driver_opts", 4)
    return _scalar(opts, "com.docker.network.bridge.enable_ip_masquerade") == '"false"'


def _restart_of(service: str) -> str:
    restart = _scalar(_service(service), "restart")
    if restart is None:
        raise AssertionError(f"{service} declares no restart policy at all")
    return restart


def _sandbox_pool() -> str:
    """sandbox-net's dynamic pool: where every launched sandbox, and only a sandbox,
    gets its address, so the range the agent's class and lifeline are scoped to."""
    pool = _ipam_of("sandbox-net", "ip_range")
    if pool is None:
        raise AssertionError("sandbox-net declares no ip_range, so the agent's class "
                             "would have to be the whole subnet, infra included")
    return pool


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
        pinned = _pin("egress-proxy", "mcp-net")
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
                pinned = _pin(svc, "mcp-net")
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
        claimed = [_pin(svc, "mcp-net")
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
    Docker hands a dynamic member the LOWEST free address in its range, which is
    the same ``.2`` a pinned member is most likely to have asked for; whoever the
    daemon starts first takes it and the other dies with "Address already in use".
    `depends_on` does not help — it orders `compose up`, not the daemon bringing
    `restart: always` containers back after a host reboot, which is the only time
    the order differs from the one that was tested.

    Scoped to attachments DECLARED IN COMPOSE. The sandboxes are dynamic on
    sandbox-net by design, and drawn from its pool (``ip_range``), which every pin
    there sits below: ``test_no_pin_on_the_agent_network_is_in_the_sandbox_pool``."""

    @staticmethod
    def _legs() -> dict[str, dict[str, str | None]]:
        """network -> {service: pinned address or None} over both compose files."""
        legs: dict[str, dict[str, str | None]] = {}
        for svc in _service_names():
            for net in _raw_networks(svc):
                legs.setdefault(net, {})[svc] = _pin(svc, net)
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

    def test_the_control_plane_spans_every_control_network(self):
        # It is the one service that does, and that is the whole design: one
        # process, one surface per network — the shared management path plus one
        # single-conversation bridge per enforcer.
        self.assertEqual(_networks_of("control-plane"),
                         {"control-net", "authorize-net", "tool-authorize-net"})

    def test_the_tool_listener_binds_its_own_bridge_address(self):
        # The narrower of the two bind rules, and the reason it is not the authorize
        # listener's wildcard: this surface can release an approved tool call, so
        # serving it on every interface would put it within the egress proxy's reach.
        bind = _environment_of("control-plane")["CONTROL_TOOL_BIND"]
        self.assertNotIn(bind, ("0.0.0.0", "::", "*", ""))  # noqa: S104
        net = _subnet_of("tool-authorize-net").split("/")[0].rsplit(".", 1)[0]
        self.assertTrue(bind.startswith(net + "."),
                        f"CONTROL_TOOL_BIND {bind} is not on tool-authorize-net")

    def test_the_bound_tool_address_is_the_leg_compose_pins(self):
        # A bind that names an address this container does not hold is a listener
        # that refuses to start — the fail-closed direction, but at boot on the host
        # rather than here. The two are set in different blocks of the same file,
        # which is exactly the pair nothing else compares.
        leg = _pin("control-plane", "tool-authorize-net")
        self.assertEqual(_environment_of("control-plane")["CONTROL_TOOL_BIND"], leg)

    def test_every_probed_control_plane_address_is_one_compose_pins(self):
        # boundary-check.sh proves the sandbox has no route to the control plane by
        # dialing LITERAL addresses, one per surface. Each probe means something only
        # because a listener is really there: a pin that moves, or a port that
        # changes, leaves the probe passing against nothing — the same drift
        # ``test_the_probed_mcp_address_is_the_one_compose_pins`` exists for, and the
        # two files still cannot see each other. All three surfaces, because the
        # newest is the one a reader is most likely to leave unasserted.
        for net, port in (("control-net", MANAGE_PORT),
                          ("authorize-net", AUTHORIZE_PORT),
                          ("tool-authorize-net", TOOL_PORT)):
            with self.subTest(network=net):
                pinned = _pin("control-plane", net)
                self.assertIsNotNone(
                    pinned, f"the control plane's {net} leg has no fixed address")
                self.assertIn(f"{pinned}:{port}", BOUNDARY,
                              f"boundary-check.sh does not probe {net} at the "
                              f"address and port compose deploys")

    def test_every_relay_probe_has_an_ipv4_mapped_twin(self):
        # The relay guard is probed twice per address, and the mapped spelling is not
        # decoration: address-family containment once defeated the guard entirely,
        # and the dotted-quad probes passed throughout. So the mapped list is the only
        # one that can catch a regression of it, and a new control subnet added to
        # the dotted list alone leaves the bypass unprobed for exactly that subnet.
        for net in ("control-net", "authorize-net", "tool-authorize-net"):
            with self.subTest(network=net):
                pinned = _pin("control-plane", net)
                self.assertIn(f"[::ffff:{pinned}]", BOUNDARY,
                              f"boundary-check.sh probes {pinned} as a dotted quad "
                              f"but not as IPv4-mapped IPv6, which is the only "
                              f"spelling that catches an address-family bypass")

    def test_nothing_else_joins_the_tool_bridge(self):
        # Both members now, and exactly these two ever: the control plane and the MCP
        # gateway. The roster is asserted rather than the absence of a particular
        # service, because the point of a SECOND bridge is that no third component —
        # least of all the egress proxy — reaches the claim endpoint on it.
        #
        # The set is now CLOSED rather than pending, which is the state that makes
        # this guard bite: with one member it could only catch an arrival, and the
        # arrival it was waiting for was legitimate. From here every addition is one.
        members = {name.strip().rstrip(":")
                   for name in _block(COMPOSE, "services", 0)
                   if len(name) - len(name.lstrip(" ")) == 2
                   and name.strip().endswith(":")
                   and "tool-authorize-net" in _raw_networks(
                       name.strip().rstrip(":"))}
        self.assertEqual(members, {"control-plane", "tool-gateway"})
        self.assertNotIn("tool-authorize-net", _networks_of("egress-proxy"))

    def test_the_ui_talks_to_the_management_port_over_control_net(self):
        # Its two legs and no third: the task registry's page is another service
        # (tasks-ui), so nothing that reads the registry is on control-net.
        self.assertEqual(_networks_of("control-plane-ui"), {"control-net", "control-ui-net"})
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


UI_APP = (ROOT / "control-plane-ui" / "app.py").read_text()
TASKS_UI_APP = (ROOT / "tasks-ui" / "app.py").read_text()


def _ui_default(variable: str, app: str = UI_APP) -> str:
    """A default a UI's ``app.py`` sets, read as text for the reason
    ``_gateway_forbidden_default`` gives."""
    match = re.search(rf'"{variable}",\s*"([^"]+)"', app)
    if not match:
        raise AssertionError(f"no {variable} default this can read")
    return match.group(1)


class _UiServiceChecks:
    """A UI page is served on one address, the one on its own loopback-published
    network: ``main`` in its ``app.py`` says why, and ``check_bind`` in
    ``ui-common/guards.py`` refuses any other. These hold compose to it, for each UI."""

    SERVICE: ClassVar[str]
    NET: ClassVar[str]
    ENV: ClassVar[str]          # the prefix of its variables
    PUBLISH: ClassVar[str]      # the host side of its port mapping

    @property
    def app(self) -> str:
        return (ROOT / self.SERVICE / "app.py").read_text()

    def test_the_bind_is_the_leg_compose_pins(self):
        pinned = _pin(self.SERVICE, self.NET)
        self.assertIsNotNone(pinned, f"the UI's {self.NET} leg is not pinned")
        self.assertEqual(_environment_of(self.SERVICE)[f"{self.ENV}_BIND"], pinned)

    def test_nothing_else_joins_its_network(self):
        # OnlyFrom admits this whole network, so a member reaches the page and its
        # relay. The UI alone, as DESIGN.md's Networks says.
        members = {svc for svc in _service_names() if self.NET in _raw_networks(svc)}
        self.assertEqual(members, {self.SERVICE})

    def test_the_app_reads_the_variables_compose_sets(self):
        # A renamed variable would leave the bind empty, and check_bind refuses that,
        # but at the host's `make up` rather than here.
        for var in ("BIND", "PORT", "NET"):
            with self.subTest(variable=var):
                self.assertRegex(self.app, rf'os\.environ\.get\("{self.ENV}_{var}",')
        for var in ("BIND", "PORT"):
            with self.subTest(variable=var):
                self.assertIn(f"{self.ENV}_{var}", _environment_of(self.SERVICE))

    def test_the_bind_check_allows_exactly_its_networks_subnet(self):
        # A default that drifted from compose would refuse the real address, loud, or
        # allow another network's, silent.
        self.assertEqual(_ui_default(f"{self.ENV}_NET", self.app), _subnet_of(self.NET))
        env = _environment_of(self.SERVICE)
        if f"{self.ENV}_NET" in env:
            self.assertEqual(env[f"{self.ENV}_NET"].strip('"\''), _subnet_of(self.NET))

    def test_its_network_is_apart_from_every_other(self):
        subnet = ipaddress.ip_network(_subnet_of(self.NET))
        # Inside the range the relay guard hard-blocks, so the proxy refuses it as a
        # CONNECT target too.
        self.assertTrue(subnet.subnet_of(ipaddress.ip_network("172.16.0.0/12")))
        networks = [m.group(1) for ln in _block(COMPOSE, "networks", 0)
                    if (m := re.match(r"  ([a-z0-9-]+):\s*$", ln))]
        self.assertIn(self.NET, networks)
        for net in networks:
            if net == self.NET or not any(
                    "subnet:" in ln for ln in _block(_block(COMPOSE, "networks", 0), net, 2)):
                continue
            with self.subTest(network=net):
                self.assertFalse(subnet.overlaps(ipaddress.ip_network(_subnet_of(net))))

    def test_its_network_is_a_publish_and_no_egress_path(self):
        # Non-internal, since Docker publishes from no internal network alone, and so
        # masquerade off: nothing on it converses with the outside.
        self.assertFalse(_is_internal(self.NET))
        self.assertTrue(_masquerade_off(self.NET))

    def test_the_publish_and_the_healthcheck_reach_the_port_it_serves(self):
        body = _service(self.SERVICE)
        port = _environment_of(self.SERVICE)[f"{self.ENV}_PORT"].strip('"\'')
        self.assertEqual(_ui_default(f"{self.ENV}_PORT", self.app), port)
        publish = [ln.strip() for ln in _block(body, "ports", 4)
                   if ln.strip() and not ln.strip().startswith("#")]
        self.assertEqual(publish, [f'- "{self.PUBLISH}:{port}"'])
        self.assertIn(f'EXPOSE {port}\n',
                      (ROOT / self.SERVICE / "Dockerfile").read_text())
        # The page binds no loopback, so the probe dials the bound address, and names a
        # Host the allowlist takes, since the address is not one.
        probe = _probe(self.SERVICE)
        for var in ("BIND", "PORT"):
            self.assertIn(f"os.environ['{self.ENV}_{var}']", probe)
        self.assertIn("headers={'Host': '127.0.0.1'}", probe)
        self.assertIn("/healthz", probe)

    def test_it_forwards_nothing_between_its_legs(self):
        sysctls = [ln.strip() for ln in _block(_service(self.SERVICE), "sysctls", 4)
                   if ln.strip() and not ln.strip().startswith("#")]
        self.assertEqual(sysctls, ['net.ipv4.ip_forward: "0"'])

    def test_the_image_starts_the_entry_that_checks_the_bind(self):
        # uvicorn's own command line would take any --host and check nothing.
        dockerfile = (ROOT / self.SERVICE / "Dockerfile").read_text()
        entry = [ln for ln in dockerfile.splitlines() if ln.startswith("ENTRYPOINT")]
        self.assertEqual(entry, ['ENTRYPOINT ["python", "app.py"]'])

    def test_the_image_carries_each_shared_module_the_app_imports(self):
        # By plain name, from beside it: a Dockerfile without the line still builds,
        # and the container dies at its first import.
        dockerfile = (ROOT / self.SERVICE / "Dockerfile").read_text()
        shared = [name for name in re.findall(r"(?m)^import (\w+)$", self.app)
                  if (ROOT / "ui-common" / f"{name}.py").is_file()]
        self.assertIn("guards", shared)
        for name in shared:
            with self.subTest(module=name):
                self.assertRegex(dockerfile,
                                 rf"(?m)^COPY ui-common/{name}\.py \./{name}\.py$")

    def test_the_image_is_built_from_the_root_to_reach_ui_common(self):
        build = _block(_service(self.SERVICE), "build", 4)
        self.assertEqual(_scalar(build, "context"), ".")
        self.assertEqual(_scalar(build, "dockerfile"), f"{self.SERVICE}/Dockerfile")
        ignored = (ROOT / ".dockerignore").read_text().splitlines()
        for folder in (self.SERVICE, "ui-common"):
            self.assertNotIn(folder, ignored)


class ControlPlaneUiBindTests(_UiServiceChecks, unittest.TestCase):
    SERVICE, NET, ENV = "control-plane-ui", "control-ui-net", "CONTROL_UI"
    PUBLISH = "127.0.0.1:${DOCKADE_UI_PORT:-28090}"

    def test_the_link_to_the_tasks_ui_is_its_publish(self):
        # An address and nothing else; the default and compose's follow the tasks UI's
        # published port.
        link = _environment_of("control-plane-ui")["CONTROL_UI_TASKS_LINK"]
        self.assertEqual(link, f"http://{TasksUiBindTests.PUBLISH}/")
        self.assertEqual(_ui_default("CONTROL_UI_TASKS_LINK"), "http://127.0.0.1:28091/")


class TasksUiBindTests(_UiServiceChecks, unittest.TestCase):
    SERVICE, NET, ENV = "tasks-ui", "tasks-ui-net", "TASKS_UI"
    PUBLISH = "127.0.0.1:${DOCKADE_TASKS_UI_PORT:-28091}"

    def test_it_is_on_exactly_its_two_legs(self):
        # The registry's operator network and its own publish: never control-net, the
        # sandbox's network or an egress leg. It reads what agents wrote.
        self.assertEqual(_networks_of("tasks-ui"), {"tasks-ui-net", "tasks-operator-net"})

    def test_it_runs_with_the_registry_and_waits_for_it(self):
        body = _service("tasks-ui")
        self.assertEqual([ln.strip() for ln in _block(body, "profiles", 4)], ["- tasks"])
        self.assertEqual(_scalar(_block(_block(body, "depends_on", 4), "tasks", 6),
                                 "condition"), "service_healthy")

    def test_make_starts_and_stops_it_with_the_registry(self):
        # The page is the registry's: up together, down together, and back together
        # after a restore, which stops the registry under it.
        makefile = (ROOT / "Makefile").read_text()

        def recipe(target):
            return re.search(rf"^{target}:.*?(?=\n\S)", makefile, re.M | re.S).group(0)
        self.assertRegex(recipe("tasks-up"), r"up -d [^\n]*\btasks tasks-ui\n")
        self.assertIn("stop tasks-ui tasks\n", recipe("tasks-down"))
        self.assertIn("rm -f tasks-ui tasks\n", recipe("tasks-down"))
        self.assertRegex(recipe("restore-tasks"), r"trap '[^']*up -d [^']*\btasks tasks-ui' EXIT")

    def test_its_registry_leg_is_not_the_approval_uis_old_address(self):
        # 172.26.0.3 was the approval UI's leg here until this page existed. A host still
        # running that UI, or rolled back to it, would have the two racing for one pinned
        # address, and whichever starts second does not start.
        mine = _pin("tasks-ui", "tasks-operator-net")
        self.assertNotEqual(mine, "172.26.0.3")

    def test_it_writes_nothing_but_a_tmpfs(self):
        body = _service("tasks-ui")
        self.assertEqual(_scalar(body, "read_only"), "true")
        self.assertEqual([ln.strip() for ln in _block(body, "tmpfs", 4)], ["- /tmp"])

    def test_its_pins_are_the_approval_uis(self):
        # Both run ui-common's guards, which were tested against these.
        def pins(path):
            return sorted(ln for ln in path.read_text().splitlines()
                          if ln and not ln.startswith("#"))
        self.assertEqual(pins(ROOT / "tasks-ui" / "requirements.txt"),
                         pins(ROOT / "control-plane-ui" / "requirements.txt"))


class RootContextIgnoreTests(unittest.TestCase):
    """Every root-context build, not only the UIs', reads ``.dockerignore``."""

    IGNORED = tuple(ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines()
                    if not ln.startswith("#"))

    def test_bytecode_is_ignored_at_any_depth(self):
        self.assertIn("**/__pycache__", self.IGNORED)
        self.assertIn("**/*.pyc", self.IGNORED)

    def test_no_bytecode_pattern_is_anchored_to_the_root(self):
        for line in self.IGNORED:
            if "__pycache__" in line or line.endswith(".pyc"):
                with self.subTest(line=line):
                    self.assertTrue(line.startswith("**/"))


def _raw_networks(service: str) -> set[str]:
    """``_networks_of`` for a service that may declare no networks at all (the
    profile-gated llm-* variants do declare some; a future one might not)."""
    try:
        return _networks_of(service)
    except AssertionError:
        return set()


GATEWAY_SRC = (ROOT / "tool-gateway" / "app.py").read_text()


def _gateway_port() -> str:
    """The agent-facing port as compose sets it, unquoted.

    Compose quotes it — a bare 8100 would be a YAML int, and compose rejects a
    non-string environment value — so the quotes are part of the scalar this file's
    reader returns. Stripped here rather than at each use, because a probe compared
    against `172.30.0.11:"8100"` fails for a reason that has nothing to do with the
    property under test."""
    return _environment_of("tool-gateway")["GATEWAY_AGENT_PORT"].strip('"\'')


def _gateway_forbidden_default() -> set[str]:
    """The CIDRs tool-gateway/app.py refuses to serve the agent surface on.

    Read out of the source rather than imported, for the reason the relay-guard
    tests read the addon that way: importing would run the module's env-var
    resolution under the TEST's environment, which is not the one the container
    starts with."""
    match = re.search(r'"GATEWAY_BIND_FORBIDDEN",\s*\n?\s*"([^"]+)"', GATEWAY_SRC)
    if not match:
        raise AssertionError(
            "tool-gateway/app.py no longer has a GATEWAY_BIND_FORBIDDEN default this "
            "can read, so the guard is no longer held equal to the compose subnets")
    return {part.strip() for part in match.group(1).split(",") if part.strip()}


def _gateway_mcp_net_default() -> str:
    """The network tool-gateway/discovery.py lets a credential be sent to. Read out of
    the source for the reason ``_gateway_forbidden_default`` is."""
    source = (ROOT / "tool-gateway" / "discovery.py").read_text()
    match = re.search(r'"GATEWAY_MCP_NET",\s*"([^"]+)"', source)
    if not match:
        raise AssertionError(
            "tool-gateway/discovery.py no longer has a GATEWAY_MCP_NET default this can "
            "read, so where credentials go is no longer held equal to mcp-net")
    return match.group(1)


class GatewayPlacementTests(unittest.TestCase):
    """The gateway is triple-homed, and only ONE of the three legs is served on.

    Everything here is a placement rather than a setting, which is why it is
    asserted here rather than described in DESIGN.md: nothing inside the gateway
    process can observe which networks it was attached to, so compose is the only
    place these properties exist and this is the only place they are checked."""

    def test_the_gateway_is_on_exactly_its_three_legs(self):
        # Named exhaustively rather than checked one at a time. A fourth leg is the
        # failure this catches, and it would not announce itself: an added network
        # breaks nothing at runtime and every probe below would keep passing.
        self.assertEqual(_networks_of("tool-gateway"),
                         {"sandbox-net", "mcp-net", "tool-authorize-net"})

    def test_credentials_go_only_to_the_mcp_net_compose_deploys(self):
        # The gateway checks a server's resolved address against this before sending
        # its bearer token. A default that drifted from the real subnet would refuse
        # every server — loud — or, worse, admit a network the agent is on.
        self.assertEqual(_gateway_mcp_net_default(), _subnet_of("mcp-net"))
        override = _environment_of("tool-gateway").get("GATEWAY_MCP_NET")
        if override is not None:
            self.assertEqual(override.strip('"\''), _subnet_of("mcp-net"))

    def test_the_gateway_has_no_egress_of_its_own(self):
        # The whole point of the servers' egress going through the proxy is that a
        # container holding a write-capable credential has no unaudited path out. A
        # leg here would put exactly that next to the credentials being brokered.
        self.assertNotIn("egress-net", _networks_of("tool-gateway"))

    def test_the_agent_listener_binds_the_sandbox_net_address_and_only_that(self):
        # The bind and the leg are written in two places and must agree: a bind that
        # names an address compose does not deploy is a listener that refuses to
        # start, and one that names a DIFFERENT deployed address is the lateral edge
        # the guard in app.py exists to refuse.
        env = _environment_of("tool-gateway")
        pinned = _pin("tool-gateway", "sandbox-net")
        self.assertIsNotNone(pinned, "the gateway's sandbox-net leg has no fixed "
                                     "address, so GATEWAY_AGENT_BIND cannot name it")
        self.assertEqual(env["GATEWAY_AGENT_BIND"], pinned)

    def test_the_bind_guard_names_every_network_the_agent_surface_must_not_reach(self):
        # Same arrangement as the relay guard's FORBIDDEN_CIDRS: the default in the
        # source is held equal to the real subnets here, so the two cannot drift. The
        # entry that matters is mcp-net — a server container reaching the agent-facing
        # endpoint is the lateral move this whole placement exists to prevent.
        forbidden = _gateway_forbidden_default()
        for net in ("mcp-net", "tool-authorize-net", "authorize-net", "control-net"):
            with self.subTest(network=net):
                self.assertIn(_subnet_of(net), forbidden,
                              f"{net} is absent from GATEWAY_BIND_FORBIDDEN, so a "
                              f"bind on it would be accepted")

    def test_the_guard_does_not_forbid_the_network_it_must_bind(self):
        # The other direction, and a real way to write this wrong: listing sandbox-net
        # would make the gateway refuse its own correct configuration, and it would
        # fail closed at boot rather than here — a restart loop on the host, which is
        # a slower and less legible way to learn it.
        self.assertNotIn(_subnet_of("sandbox-net"), _gateway_forbidden_default())

    def test_the_gateway_does_not_take_an_address_from_the_catalogue_range(self):
        # mcp-servers.yml allocates .11+ to servers and grows; the gateway is not a
        # tenant of that catalogue. Overlap would not fail at boot — it would fail the
        # NEXT time a server was added and took an address already held.
        addr = ipaddress.ip_address(
            _pin("tool-gateway", "mcp-net"))
        for svc in _mcp_service_names():
            with self.subTest(server=svc):
                self.assertNotEqual(
                    addr,
                    ipaddress.ip_address(
                        _pin(svc, "mcp-net")),
                    f"{svc} and tool-gateway both claim {addr} on mcp-net")

    def test_the_hand_probe_reaches_the_gateway_where_compose_deploys_it(self):
        # `make gateway-tools` dials a literal address from a throwaway container on
        # sandbox-net. The name would resolve to that same leg — embedded DNS answers
        # only with the legs the querier shares — but a literal is what this test can
        # hold against the deployed address. A drift here does not fail loudly: the
        # probe would report an
        # unreachable gateway on a perfectly healthy deployment, which reads as a
        # broken surface rather than as a stale constant.
        makefile = (ROOT / "Makefile").read_text()
        env = _environment_of("tool-gateway")
        for name, deployed in (("GATEWAY_ADDR", env["GATEWAY_AGENT_BIND"]),
                               ("GATEWAY_PORT", _gateway_port())):
            with self.subTest(variable=name):
                match = re.search(rf"^{name}\s*\?=\s*(\S+)", makefile, re.M)
                self.assertIsNotNone(match, f"no {name} in the Makefile")
                self.assertEqual(match.group(1), deployed)

    def test_the_hand_probe_dials_from_the_agents_own_network(self):
        # Probing from anywhere else would either fail — which is the whole point of
        # the single-address bind — or need a published port, which would put the
        # agent's tool surface on the host.
        makefile = (ROOT / "Makefile").read_text()
        target = re.search(r"^gateway-tools:.*?(?=\n\S)", makefile, re.M | re.S)
        self.assertIsNotNone(target, "no gateway-tools target in the Makefile")
        self.assertIn("--network sandbox-net", target.group(0))

    def test_boundary_check_probes_the_gateway_where_compose_deploys_it(self):
        # The positive control. If this address drifts from compose, the probe fails
        # on a correct deployment and the two bind probes below it go vacuous — they
        # are gated on it, so a stale address here silently disables them rather than
        # failing loudly, which is the shape of bug this whole file exists to catch.
        pinned = _pin("tool-gateway", "sandbox-net")
        self.assertIn(f"{pinned}:{_gateway_port()}", BOUNDARY,
                      "boundary-check.sh does not probe the gateway at the address "
                      "and port compose deploys")

    def test_boundary_check_probes_every_leg_the_agent_surface_must_not_be_on(self):
        # The other two legs, by the same argument the mapped-IPv6 twins are held in
        # step with their dotted quads: a leg added to compose and not to the probe
        # list leaves exactly that leg unasserted, and it is the newest leg — the one
        # least likely to have been thought about — that goes unchecked.
        port = _gateway_port()
        for net in ("mcp-net", "tool-authorize-net"):
            with self.subTest(network=net):
                pinned = _pin("tool-gateway", net)
                self.assertIn(f"{pinned}:{port}", BOUNDARY,
                              f"boundary-check.sh does not probe the gateway's {net} "
                              f"leg, so a wildcard bind there would go undetected")

    def test_the_gateway_dials_the_control_planes_tool_leg_and_not_another(self):
        # The gateway reaches the control plane by ADDRESS while it reaches servers by
        # NAME, and the asymmetry is the property under test. A server is single-homed,
        # so its name resolves to its one leg; the control plane has three, two of them
        # networks this container must never speak to. An address names the LEG rather
        # than the container, so it stays correct if a second shared network is ever
        # added — and it is what this test can hold against the bind.
        #
        # Held against CONTROL_TOOL_BIND rather than against a literal: that variable
        # is what the control plane actually binds, so this fails if either side moves
        # instead of only when both are edited to disagree with this file.
        url = _environment_of("tool-gateway")["GATEWAY_CONTROL_URL"]
        bind = _environment_of("control-plane")["CONTROL_TOOL_BIND"]
        self.assertIn(f"//{bind}:", url,
                      f"GATEWAY_CONTROL_URL {url} does not dial the address the "
                      f"control plane binds its tool listener to ({bind})")
        leg = _pin("tool-gateway", "tool-authorize-net")
        self.assertNotIn(f"//{leg}:", url,
                         "GATEWAY_CONTROL_URL points at the gateway's own leg")

    def test_the_gateway_reaches_the_control_plane_over_no_other_network(self):
        # The reason the address above is safe to hardcode: it is on the one network
        # the two share. If the gateway ever gained a leg on authorize-net or
        # control-net, dialling "the control plane" would stop being unambiguous and
        # this file's whole single-leg argument would need revisiting.
        legs = _networks_of("tool-gateway")
        self.assertEqual(legs & {"authorize-net", "control-net", "control-ui-net"},
                         set(),
                         "the gateway has a leg on a control network, so it no longer "
                         "reaches the control plane over tool-authorize-net alone")

    def test_the_secrets_mount_is_read_only_and_never_made_by_docker(self):
        # The gateway only ever READS a token. Write access would let a compromised
        # gateway rewrite the credential every future call is made with, and would put
        # the agent one process-compromise away from editing what the broker spends —
        # which is the whole thing this container is between the agent and.
        mount = _long_mount("tool-gateway", "/run/dockade/secrets")
        self.assertEqual(mount["source"], "${MCP_SECRETS:-./secrets}")
        self.assertEqual(mount["read_only"], "true")
        # A missing directory fails the start: made by dockerd it would be root's, and
        # every later write there would need sudo. `make` makes it (SecretsDirTests).
        self.assertEqual(mount["create_host_path"], "false")

    def test_no_service_mounts_the_secrets_in_the_short_form(self):
        # The short form makes a missing source, as root, whatever the long form beside
        # it says.
        for svc in _service_names():
            with self.subTest(service=svc):
                self.assertEqual([ln for ln in _service(svc)
                                  if ln.strip().startswith("- ") and "MCP_SECRETS" in ln],
                                 [])

    def test_the_gateway_runs_as_a_user_that_can_read_the_secrets_mount(self):
        # These two lines are one fact split across a file boundary, and getting it
        # wrong is SILENT until enumeration: the container starts, the healthcheck
        # passes, and the only symptom is a permission error inside a report an
        # operator may not be reading. The host files are mode 0600 (secrets-perm-check
        # keeps them that way), so the mount is useless to any uid but the owner's.
        user = _scalar(_service("tool-gateway"), "user")
        self.assertTrue(user, "the gateway has a secrets mount but no user override, "
                              "so it runs as the image uid and can read none of it")
        self.assertIn("DOCKADE_UID", user, f"{user} does not take the invoking user's id")
        # Not root, which would read the mount by ignoring its mode — the one way to
        # make this test pass while throwing away the reason it exists.
        self.assertNotIn('"0:', user)
        self.assertNotIn("root", user)

    def test_the_gateway_image_uid_collides_with_no_other_infra_image(self):
        # It is only a fallback for a hand-run `docker run`, and it still has to be its
        # own: two images sharing a uid is harmless right up until they share a mount,
        # and then each can read what the other was given.
        uids = {}
        for path in sorted(ROOT.glob("*/Dockerfile")):
            for match in re.finditer(r"--uid (\d+)", path.read_text()):
                uids.setdefault(match.group(1), []).append(path.parent.name)
        shared = {uid: who for uid, who in uids.items() if len(set(who)) > 1}
        self.assertEqual(shared, {}, f"uid claimed by more than one image: {shared}")

    def test_no_other_service_is_given_the_secrets(self):
        # One holder of the directory, deliberately. Every additional container with it
        # is another process whose compromise spends the same token, and the servers in
        # particular must NOT have it — in http mode they ignore an env credential
        # anyway (NOTES.md), so a copy there would be exposure buying nothing. The task
        # registry is the one other service given anything from it: its own file, alone
        # (``TaskRegistryPlacementTests``).
        holders = {svc for svc in _service_names()
                   if any("MCP_SECRETS" in ln or "/run/dockade/secrets" in ln
                          for ln in _service(svc) if not ln.strip().startswith("#"))}
        self.assertEqual(holders, {"tool-gateway", "tasks"},
                         f"more than the gateway is given the MCP credentials: {holders}")

    def test_the_gateway_runs_the_same_stack_as_the_control_plane(self):
        # One definition of "the Python service shape we ship". Two services on two
        # FastAPI versions is a drift nobody decides — it happens when one gets bumped
        # and the other is forgotten, and the symptom surfaces in whichever is touched
        # next rather than at the bump.
        gw = (ROOT / "tool-gateway" / "requirements.txt").read_text()
        cp = (ROOT / "control-plane" / "requirements.txt").read_text()
        pins = lambda text: {  # noqa: E731
            line.strip() for line in text.splitlines()
            if line.strip() and not line.startswith("#")}
        self.assertEqual(pins(gw), pins(cp))


class OutcomeStreamWiringTests(unittest.TestCase):
    """The volume the gateway writes and the control plane drains.

    The one property that cannot be seen from either process. The gateway opens a path
    and the control plane opens a path, and nothing in either checks that they are the
    same volume, or that only one of them can write to it. A mismatch produces no error
    anywhere: the gateway records happily, the ingest finds no file, and the outcome
    column is simply always empty — the exact failure the stream exists to remove,
    wearing a working system as a disguise."""

    MOUNT = "/var/log/tool-gateway"

    def _mounts(self, service: str) -> list[str]:
        return [m for m in _mount_spellings(service) if m.startswith("tool-audit:")]

    def test_the_gateway_mounts_it_writable(self):
        # It is the writer. A `:ro` here would be a gateway that starts, serves tool
        # calls and fails on the first record — after the call has already happened.
        mounts = self._mounts("tool-gateway")
        self.assertEqual(mounts, [f"tool-audit:{self.MOUNT}"])

    def test_the_control_plane_mounts_it_read_only(self):
        # The reader must not be able to rewrite the evidence it is reading — the same
        # arrangement, and the same reason, as the egress proxy's audit volume. `:ro`
        # is the whole assertion: the control plane drains this file into the crown
        # jewel, and a writable mount would let the store's own reader edit its source.
        self.assertEqual(self._mounts("control-plane"), [f"tool-audit:{self.MOUNT}:ro"])

    def test_nothing_else_touches_it(self):
        # ONE writer, one reader. A second writer would make the ingest's
        # inode-following cursor ambiguous about whose records it is draining.
        for service in _service_names():
            if service in ("tool-gateway", "control-plane"):
                continue
            with self.subTest(service=service):
                self.assertEqual(self._mounts(service), [])

    def test_both_ends_name_the_same_file(self):
        # THE ONE THAT FAILS SILENTLY. Three files, no compiler: the gateway's default,
        # the control plane's, and the two mounts. A writer pointing outside the mount
        # still WORKS — it lands on the container filesystem — so the gateway looks
        # healthy while every record dies with it; a reader pointing elsewhere simply
        # finds no file, and the `status` column is empty forever. Neither errors.
        paths = {}
        for module, variable in (("tool-gateway/outcomes.py", "AUDIT_PATH"),
                                 ("control-plane/ingest.py", "TOOL_AUDIT_LOG")):
            source = (ROOT / module).read_text()
            found = re.search(
                rf'^{variable} = os\.environ\.get\(\s*"[A-Z_]+",\s*\n?\s*"([^"]+)"',
                source, re.M)
            self.assertIsNotNone(found, f"{module} no longer names {variable}")
            paths[module] = found.group(1)
        self.assertEqual(len(set(paths.values())), 1,
                         f"the writer and the reader disagree on the path: {paths}")
        written = next(iter(paths.values()))
        self.assertTrue(written.startswith(self.MOUNT + "/"),
                        f"{written} is outside the mounted volume, so records would "
                        f"not survive the container")

    def test_the_gateways_image_creates_the_directory(self):
        # A fresh named volume inherits ownership and mode from the image path it
        # covers. Without this the volume is root-owned, and compose runs this service
        # as the INVOKING USER — so the first record fails on a permission error that
        # names a log file rather than the mount that caused it.
        dockerfile = (ROOT / "tool-gateway" / "Dockerfile").read_text()
        self.assertRegex(dockerfile, rf"mkdir -p {self.MOUNT}")
        self.assertRegex(dockerfile, rf"chmod 1777 {self.MOUNT}")


class RestartPolicyTests(unittest.TestCase):
    """`always` on the substrate, `unless-stopped` on the optional tier-2 model
    server. The line is not stylistic: the two policies differ in exactly one case
    — whether a container that was down when the daemon stopped comes back when it
    starts — and that case cost this stack its governance authority across one
    ordinary power cycle, silently, with the proxy still up and reporting healthy.

    Asserted rather than left to review because the failure is invisible: a service
    demoted to `unless-stopped` behaves identically until the one reboot where it
    does not come back."""

    def test_the_infra_services_always_come_back(self):
        for service in ("egress-proxy", "control-plane", "control-plane-ui"):
            self.assertEqual(_restart_of(service), "always", service)

    def test_the_model_server_honours_a_deliberate_stop(self):
        # Stopping llm-intel to reclaim the shared memory pool is an ordinary
        # operator action, so it must NOT be resurrected by a reboot. This is the
        # other half of the line above — without it, "always everywhere" would
        # satisfy the test above and quietly break a real workflow.
        self.assertEqual(_restart_of("llm-intel"), "unless-stopped")


class RelayGuardAgreesWithComposeTests(unittest.TestCase):
    """The proxy's relay guard hard-blocks the control subnets by CIDR, and those
    CIDRs are written twice — once as a compose subnet, once as a default in
    ``addon.py``. Renaming or renumbering a network on one side would leave the
    guard pointed at an address range nothing uses, which fails open silently:
    the proxy starts, ``_assert_guard_configured`` passes on a non-empty list, and
    the blocked range is simply the wrong one."""

    def test_the_guard_default_covers_every_control_subnet(self):
        default = re.search(
            r'FORBIDDEN_CIDRS_DEFAULT = "([^"]+)"', ADDON)
        self.assertIsNotNone(default, "could not find the FORBIDDEN_CIDRS default")
        listed = {c.strip() for c in default.group(1).split(",")}
        for network in ("control-net", "authorize-net", "tool-authorize-net"):
            self.assertIn(_subnet_of(network), listed,
                          f"{network}'s subnet is not in the relay guard's default")

    def test_the_tool_bind_guard_names_every_other_enforcers_network(self):
        # The app-side half of the same two-ends problem, and it has to be checked
        # from compose for the same reason: a default pointed at a range nothing
        # uses passes every wildcard test while leaving the claim endpoint bindable
        # on authorize-net. The tool bridge's OWN subnet must not be listed — that
        # is the address the listener is required to bind.
        default = re.search(
            r'"CONTROL_TOOL_BIND_FORBIDDEN", "([^"]+)"', APP)
        self.assertIsNotNone(default, "could not find the tool bind guard's default")
        listed = {c.strip() for c in default.group(1).split(",")}
        for network in ("control-net", "authorize-net"):
            self.assertIn(_subnet_of(network), listed,
                          f"{network}'s subnet is not in the tool bind guard")
        self.assertNotIn(_subnet_of("tool-authorize-net"), listed,
                         "the tool bridge's own subnet is forbidden to the listener "
                         "that is required to bind it")

    def test_the_management_bind_guard_names_every_enforcers_network(self):
        # Same shape for the listener that grants. Both enforcers' networks must be
        # listed — the proxy's authorize-net and the gateway's tool-authorize-net —
        # and control-net must not be, because that is the address the management
        # listener is required to bind.
        default = re.search(
            r'"CONTROL_MANAGE_BIND_FORBIDDEN", "([^"]+)"', APP)
        self.assertIsNotNone(default, "could not find the management bind guard's default")
        listed = {c.strip() for c in default.group(1).split(",")}
        for network in ("authorize-net", "tool-authorize-net"):
            self.assertIn(_subnet_of(network), listed,
                          f"{network}'s subnet is not in the management bind guard")
        self.assertNotIn(_subnet_of("control-net"), listed,
                         "control-net is forbidden to the listener that is required "
                         "to bind it")

    def test_the_agent_network_declares_a_sandbox_pool_inside_its_subnet(self):
        # The pool is what makes "a sandbox" a range: Docker draws dynamic addresses
        # from it alone. The gateway is pinned outside it because an unset one may be
        # taken from the pool, putting the host-side bridge in the agent's class.
        subnet = ipaddress.ip_network(_subnet_of("sandbox-net"))
        pool = ipaddress.ip_network(_sandbox_pool())
        self.assertTrue(pool.subnet_of(subnet), f"{pool} is not inside {subnet}")
        self.assertNotEqual(pool, subnet, "the pool is the whole subnet, infra and all")
        gateway = _ipam_of("sandbox-net", "gateway")
        self.assertIsNotNone(gateway, "sandbox-net leaves its gateway to Docker")
        self.assertIn(ipaddress.ip_address(gateway), subnet)
        self.assertNotIn(ipaddress.ip_address(gateway), pool)

    def test_no_pin_on_the_agent_network_is_in_the_sandbox_pool(self):
        # The guard the class and the lifeline rest on: a pinned address is infra (the
        # proxy, the tool gateway, the llm), and one pinned into the pool would be
        # decided as the agent and take its lifeline.
        pool = ipaddress.ip_network(_sandbox_pool())
        pinned = AddressAllocationTests._legs()["sandbox-net"]
        self.assertTrue(any(pinned.values()), "found no pin on sandbox-net to check")
        for svc, addr in pinned.items():
            with self.subTest(service=svc):
                self.assertIsNotNone(addr, f"{svc} floats on sandbox-net")
                self.assertNotIn(ipaddress.ip_address(addr), pool,
                                 f"{svc} pins {addr}, inside the sandbox pool {pool}")

    def test_the_lifeline_range_is_the_sandbox_pool(self):
        # Same two-ends-no-compiler problem as the guard above, opposite sign: this
        # CIDR decides who KEEPS a grant rather than who is refused one. Pointed at
        # a subnet nothing uses it fails safe but silently — the lifeline stops
        # working for the agent and every Anthropic call starts depending on the
        # control plane being up, which looks like an outage, not a config error.
        # Pointed at too WIDE a range it hands the local allow to mcp-net, which is
        # the thing _is_lifeline exists to prevent, and at sandbox-net's whole subnet it
        # hands it to the llm and the tool gateway.
        default = re.search(r'"EGRESS_LIFELINE_CIDRS",\s*\n?\s*"([^"]+)"', ADDON)
        self.assertIsNotNone(default, "could not find the LIFELINE_CIDRS default")
        listed = {c.strip() for c in default.group(1).split(",")}
        self.assertEqual(listed, {_sandbox_pool()})

    def test_no_other_network_is_inside_the_lifeline_range(self):
        # The assertion the one above cannot make on its own: equality with
        # sandbox-net's pool is only meaningful while no other network is a
        # subnet of it. mcp-net is the one this matters for today.
        default = re.search(r'"EGRESS_LIFELINE_CIDRS",\s*\n?\s*"([^"]+)"', ADDON)
        lifeline = [ipaddress.ip_network(c.strip())
                    for c in default.group(1).split(",")]
        for network in ("mcp-net", "control-net", "authorize-net",
                        "tool-authorize-net"):
            subnet = ipaddress.ip_network(_subnet_of(network))
            for granted in lifeline:
                self.assertFalse(
                    subnet.subnet_of(granted),
                    f"{network} sits inside the permanent lifeline's client range")

    def test_every_client_class_range_is_a_real_compose_range(self):
        # Third instance of the same two-ends-no-compiler problem, and the one with
        # the quietest failure. A class range that matches no network places nobody:
        # its clients fall through to UNCLASSIFIED, match no rule, and are HELD — so
        # a renumbered subnet does not break governance, it makes every request from
        # that population wait for a human who has no idea why they are being asked.
        default = re.search(
            r'CLIENT_CLASSES_DEFAULT = "([^"]+)"', POLICY)
        self.assertIsNotNone(default, "could not find the CLIENT_CLASSES default")
        subnets = {_subnet_of(n) for n in ("sandbox-net", "mcp-net", "control-net",
                                           "authorize-net", "tool-authorize-net")}
        subnets.add(_sandbox_pool())
        mapped = {}
        for entry in default.group(1).split(","):
            name, _, cidr = entry.partition("=")
            self.assertIn(cidr.strip(), subnets,
                          f"client class {name!r} maps to {cidr!r}, which is not a "
                          f"subnet or ip_range any network in compose declares")
            mapped[name.strip()] = cidr.strip()
        # The two populations the egress proxy actually serves, each named. The proxy
        # has a leg on both, so both reach /authorize and both must be placeable —
        # an unmapped one is the union-of-needs problem returning by omission.
        # The agent's is the pool, not the subnet, so the infra pinned below it is
        # in no class: held, never decided as the agent.
        self.assertEqual(mapped, {"sandbox": _sandbox_pool(),
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
        for network in ("control-net", "authorize-net", "tool-authorize-net",
                        "sandbox-net", "mcp-net"):
            self.assertTrue(_is_internal(network), f"network {network} is not internal: true")


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
        self.assertEqual(_scalar(_service("control-plane"), "image"),
                         self._make_var("CONTROL_IMAGE"))

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
        mounts = [m for m in _mount_spellings("control-plane")
                  if m.startswith("control-state:")]
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

    def test_tool_port_default_matches_compose(self):
        # Compose sets no port for this listener either, so the app's default is the
        # deployed one — and it is what boundary-check.sh probes by literal.
        self.assertEqual(self._default("CONTROL_TOOL_PORT"), TOOL_PORT)

    def test_the_three_listeners_have_three_distinct_ports(self):
        # ``_assert_listeners_separated`` refuses a shared port at startup; this is
        # the same property read off the shipped defaults, so a collision is a failed
        # test rather than a container that will not boot.
        ports = {self._default(f"CONTROL_{name}_PORT")
                 for name in ("AUTHORIZE", "MANAGE", "TOOL")}
        self.assertEqual(len(ports), 3, ports)


class NoAppServesFastApiDocsTests(unittest.TestCase):
    """FastAPI serves ``/openapi.json``, ``/docs`` and ``/redoc`` on every app unless
    each is turned off, and no roster in this suite can see them: the stubs record
    only the routes our code declares. Measured under the pinned FastAPI before this
    guard existed, every app served the schema — both enforcer bridges, the
    browser-facing relay, and the agent-facing gateway, which had turned off the two
    pages but not the schema behind them.

    Read from the source for the reason the module docstring gives, and over every
    ``FastAPI(...)`` call rather than a list, so a new app is held to it too."""

    OFF = ("openapi_url", "docs_url", "redoc_url")

    def _apps(self) -> dict[str, list[str]]:
        """``path:line`` of each ``FastAPI(...)`` call → the routes it leaves on."""
        found = {}
        tops = [d for d in sorted(ROOT.iterdir())
                if d.is_dir() and not d.name.startswith(".") and d.name != "tests"]
        for path in (p for d in tops for p in sorted(d.rglob("*.py"))):
            rel = path.relative_to(ROOT)
            for node in ast.walk(ast.parse(path.read_text())):
                func = getattr(node, "func", None)
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if isinstance(node, ast.Call) and name == "FastAPI":
                    given = {k.arg: k.value for k in node.keywords}
                    found[f"{rel}:{node.lineno}"] = [
                        n for n in self.OFF
                        if not (isinstance(given.get(n), ast.Constant)
                                and given[n].value is None)]
        return found

    def test_every_service_is_searched(self):
        # A guard that found no apps would pass; name the services it must find.
        files = {site.rsplit(":", 1)[0] for site in self._apps()}
        self.assertLessEqual({"control-plane/app.py", "control-plane-ui/app.py",
                              "tool-gateway/app.py"}, files)

    def test_no_app_serves_the_schema_or_the_docs_pages(self):
        left_on = {site: routes for site, routes in self._apps().items() if routes}
        self.assertEqual(left_on, {}, "pass openapi_url=None, docs_url=None and "
                                      "redoc_url=None, each as a literal")


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
    their values stay env vars" in control-plane/DESIGN.md). The override is read
    anyway, so setting one later does not silently retire this guard.
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


REGISTRY = ROOT / "task-registry"


def _registry_default(module: str, variable: str) -> str:
    """A default the registry's source sets, read as text for the reason
    ``_gateway_forbidden_default`` gives."""
    match = re.search(rf'"{variable}",\s*"([^"]+)"', (REGISTRY / module).read_text())
    if not match:
        raise AssertionError(f"task-registry/{module} no longer has a {variable} default "
                             f"this can read")
    return match.group(1)


def _long_mount(service: str, target: str) -> dict[str, str]:
    """The keys of a long-form bind entry, by its target: its own, and ``bind:``'s."""
    for item in _volume_items(service):
        keys = _item_keys(item, 8)
        if keys.get("type") == "bind" and keys.get("target") == target:
            bind = _block(item, "bind", 8) if "bind" in keys else []
            return {**_item_keys(bind, 10), **keys}
    raise AssertionError(f"{service} mounts nothing at {target} in the long form")


class TaskRegistryPlacementTests(unittest.TestCase):
    """The task registry: an MCP server on ``mcp-net`` with a second leg for the
    operator, whose listener takes no token. So which networks it joins, and where each
    listener binds, decide who can write as the operator. ``serve.py`` refuses a bind
    off its own network; these hold compose to the same networks."""

    LEGS: ClassVar[dict[str, str]] = {"TASKS_AGENT_BIND": "mcp-net",
                                      "TASKS_OPERATOR_BIND": "tasks-operator-net"}

    def test_it_is_on_exactly_its_two_legs(self):
        # Exhaustive, as the gateway's legs are: a third breaks nothing at runtime. Never
        # sandbox-net or a control network, since it parses agent text.
        self.assertEqual(_networks_of("tasks"), {"mcp-net", "tasks-operator-net"})

    def test_nothing_else_joins_the_operator_network(self):
        # Being on it is the permission to write as the operator. The tasks UI is on it
        # for the registry's page, and serves nothing there (TasksUiBindTests): the
        # registry parses agent text, and a compromised one must not find the page's
        # relay. Anything else is an agent, a server or the gateway acting as the
        # operator, or a container on control-net within the registry's reach.
        members = {svc for svc in _service_names()
                   if "tasks-operator-net" in _raw_networks(svc)}
        self.assertEqual(members, {"tasks", "tasks-ui"})

    def test_the_ui_dials_the_operator_listener_where_it_binds(self):
        # By address: the UI's resolver would ask the host's DNS for a stopped
        # registry's name. The port is the listener's, and the UI's own address on the
        # network is one the listener's peer check admits.
        env = _environment_of("tasks")
        url = _environment_of("tasks-ui")["TASKS_UI_REGISTRY_URL"]
        self.assertEqual(url, f"http://{env['TASKS_OPERATOR_BIND']}:"
                              f"{env['TASKS_OPERATOR_PORT'].strip(chr(34))}")
        self.assertEqual(_ui_default("TASKS_UI_REGISTRY_URL", TASKS_UI_APP), url)
        mine = _pin("tasks-ui", "tasks-operator-net")
        self.assertIn(ipaddress.ip_address(mine),
                      ipaddress.ip_network(_registry_default("serve.py",
                                                             "TASKS_OPERATOR_NET")))

    def test_the_operator_network_is_internal_and_apart(self):
        self.assertTrue(_is_internal("tasks-operator-net"))
        subnet = ipaddress.ip_network(_subnet_of("tasks-operator-net"))
        # Inside the range the relay guard hard-blocks, so the proxy refuses it as a
        # CONNECT target too.
        self.assertTrue(subnet.subnet_of(ipaddress.ip_network("172.16.0.0/12")))
        for net in ("sandbox-net", "control-net", "authorize-net", "tool-authorize-net",
                    "mcp-net"):
            with self.subTest(network=net):
                self.assertFalse(subnet.overlaps(ipaddress.ip_network(_subnet_of(net))))

    def test_serve_reads_the_variables_compose_sets(self):
        # The binds default to "", which check_binds refuses, so a renamed one would
        # fail only at the host's start.
        source = (REGISTRY / "serve.py").read_text()
        for var in (*self.LEGS, "TASKS_AGENT_PORT", "TASKS_OPERATOR_PORT"):
            with self.subTest(variable=var):
                self.assertRegex(source, rf'os\.environ\.get\("{var}",')
                self.assertIn(var, _environment_of("tasks"))

    def test_each_listener_binds_the_leg_compose_pins(self):
        env = _environment_of("tasks")
        for var, net in self.LEGS.items():
            with self.subTest(variable=var):
                pinned = _pin("tasks", net)
                self.assertIsNotNone(pinned, f"the registry's {net} leg is not pinned")
                self.assertEqual(env[var], pinned)

    def test_the_bind_check_allows_exactly_the_compose_subnets(self):
        # serve.py refuses a bind outside these. A default that drifted from compose
        # would refuse the real address, loud, or allow another network's, silent.
        env = _environment_of("tasks")
        for var, net in (("TASKS_AGENT_NET", "mcp-net"),
                         ("TASKS_OPERATOR_NET", "tasks-operator-net")):
            with self.subTest(variable=var):
                self.assertEqual(_registry_default("serve.py", var), _subnet_of(net))
                if var in env:
                    self.assertEqual(env[var].strip('"\''), _subnet_of(net))

    def test_its_mcp_net_address_is_its_own(self):
        mine = _pin("tasks", "mcp-net")
        for svc in _service_names():
            if svc == "tasks" or "mcp-net" not in _raw_networks(svc):
                continue
            with self.subTest(service=svc):
                self.assertNotEqual(
                    _pin(svc, "mcp-net"), mine)

    def test_it_publishes_no_port_and_nothing_waits_on_it(self):
        # Optional everywhere: behind its own profile, and nothing outside that profile
        # depends on it, so `make up` never starts it. Its page waits on it, inside.
        body = _service("tasks")
        self.assertFalse([ln for ln in body if ln.strip().startswith("ports:")])
        self.assertEqual(_block(body, "profiles", 4), ["      - tasks"])

        def profiles(svc):
            try:
                return {ln.strip().lstrip("- ") for ln in _block(_service(svc), "profiles", 4)}
            except AssertionError:
                return set()
        for svc in sorted(s for s in _service_names() if "tasks" not in profiles(s)):
            # The block form alone: ComposeFormTests refuses the flow one.
            try:
                waits_on = _block(_service(svc), "depends_on", 4)
            except AssertionError:
                waits_on = []
            for needed in ("tasks", "tasks-ui"):
                with self.subTest(service=svc, needs=needed):
                    self.assertNotIn(needed, {ln.strip().lstrip("- ").rstrip(":")
                                              for ln in waits_on})

    def test_the_gateway_dials_it_by_the_name_it_serves_under(self):
        # The gateway dials a server by its registered name, and keys its tools on it.
        name = re.search(r'^SERVER_NAME = "([^"]+)"',
                         (REGISTRY / "agent_api.py").read_text(), re.M).group(1)
        self.assertIn("tasks", _service_names())
        self.assertEqual(name, "tasks")
        for key in ("container_name", "hostname"):
            with self.subTest(key=key):
                self.assertEqual(_scalar(_service("tasks"), key), name)

    def test_it_is_given_its_own_secret_file_alone_read_only(self):
        # Never the directory, which holds the other servers' tokens; and a missing file
        # must fail its start rather than become a directory beside them.
        target = _registry_default("agent_api.py", "TASKS_SECRET_FILE")
        mount = _long_mount("tasks", target)
        self.assertEqual(mount["source"], "${MCP_SECRETS:-./secrets}/tasks.json")
        self.assertEqual(mount["read_only"], "true")
        self.assertEqual(mount["create_host_path"], "false")
        named = [ln.strip() for ln in _service("tasks") if not ln.strip().startswith("#")
                 and ("MCP_SECRETS" in ln or "/run/dockade/secrets" in ln)]
        self.assertEqual(named, [f"source: {mount['source']}", f"target: {target}"])

    def test_tasks_up_writes_the_file_compose_mounts(self):
        makefile = (ROOT / "Makefile").read_text()
        recipe = re.search(r"^tasks-up:.*?(?=\n\S)", makefile, re.M | re.S)
        self.assertIsNotNone(recipe, "no tasks-up target in the Makefile")
        self.assertIn('secret="$(MCP_SECRETS)/tasks.json"', recipe.group(0))
        self.assertIn("--profile tasks", recipe.group(0))

    def test_it_forwards_nothing_between_its_legs(self):
        # Its peer check guards its own listeners; this keeps it from carrying a
        # neighbour's packet across to the other network's members.
        sysctls = [ln.strip() for ln in _block(_service("tasks"), "sysctls", 4)
                   if ln.strip() and not ln.strip().startswith("#")]
        self.assertEqual(sysctls, ['net.ipv4.ip_forward: "0"'])

    def test_it_runs_as_a_user_that_can_read_its_secret(self):
        # For the gateway's reason (test_the_gateway_runs_as_a_user_that_can_read_...).
        user = _scalar(_service("tasks"), "user")
        self.assertIsNotNone(user)
        self.assertIn("DOCKADE_UID", user)
        self.assertNotIn('"0:', user)
        self.assertNotIn("root", user)

    def test_its_store_is_a_volume_of_its_own_where_the_store_looks(self):
        where = _registry_default("taskdb.py", "TASKS_DB").rsplit("/", 1)[0]
        for svc in _service_names():
            mounts = [m for m in _mount_spellings(svc) if m.startswith("tasks-state:")]
            with self.subTest(service=svc):
                self.assertEqual(mounts, [f"tasks-state:{where}"] if svc == "tasks" else [])

    def test_the_healthcheck_dials_the_agent_listener_where_it_binds(self):
        # The listener binds no loopback, so a probe of 127.0.0.1 would never pass.
        probe = _probe("tasks")
        for var in ("TASKS_AGENT_BIND", "TASKS_AGENT_PORT"):
            self.assertIn(f"os.environ['{var}']", probe)
        self.assertIn("/healthz", probe)

    def test_it_runs_the_control_planes_stack_and_pins_pydantic(self):
        pins = lambda path: {  # noqa: E731
            line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#")}
        mine = pins(REGISTRY / "requirements.txt")
        self.assertLessEqual(pins(ROOT / "control-plane" / "requirements.txt"), mine)
        self.assertEqual({p.split("==")[0] for p in mine}, {"fastapi", "uvicorn", "pydantic"})
        self.assertTrue(all("==" in p for p in mine), mine)

    def test_every_module_is_shipped_and_the_entry_is_serve(self):
        # As ControlPlaneModulesAreShippedTests: a module with no COPY line passes every
        # test here and fails the container's start.
        dockerfile = (REGISTRY / "Dockerfile").read_text()
        for path in sorted(REGISTRY.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertIn(f"COPY {path.name} ./{path.name}", dockerfile)
        self.assertIn('ENTRYPOINT ["python", "serve.py"]', dockerfile)


class NoServiceSharesANetworkNamespaceTests(unittest.TestCase):
    """``network_mode: service:<name>`` gives a service another's legs with no
    ``networks:`` block, so every roster here would miss it, and its connections to a
    listener in that namespace come from the listener's own address, which a peer check
    on the network admits; ``host`` reaches every bridge through the host's address.
    A sidecar of the task registry would write as the operator."""

    def test_no_service_sets_a_network_mode(self):
        for svc in _service_names():
            with self.subTest(service=svc):
                self.assertFalse([ln for ln in _service(svc)
                                  if re.match(r"\s+network_mode:", ln)])


class NoServiceSharesAnotherNamespaceTests(unittest.TestCase):
    """The other namespaces, and another container's mounts. Each key is refused
    outright, as ``network_mode`` and ``cap_add`` are: the values that matter join the
    host's or another container's, and a harmless one (``ipc: private``) is a test edit
    with its reason.

    ``pid: host`` shows a service the host's processes, and tool-gateway and tasks run
    as the operator's uid and gid. With those matching, a service can read
    ``/proc/<pid>/environ`` and ``/proc/<pid>/root`` of the operator's processes: the
    host filesystem as the operator, with every capability dropped. Docker's AppArmor
    profile blocks that where AppArmor runs, which WSL2 does not by default. Reasoned,
    not run; and it assumes an engine sharing the operator's PID namespace, where
    Docker Desktop's would show its VM's processes instead. ``volumes_from`` would hand
    an MCP server the gateway's secrets mount, or a socket bound in a container outside
    compose."""

    KEYS = ("pid", "ipc", "userns_mode", "uts", "cgroup", "volumes_from")

    def test_no_service_sets_a_namespace_or_takes_anothers_mounts(self):
        keys = "|".join(self.KEYS)
        for svc in _service_names():
            with self.subTest(service=svc):
                self.assertFalse([ln for ln in _service(svc)
                                  if re.match(rf"    ({keys}):", ln)])


def _host_path_refusal(source: str) -> str | None:
    """Why a host path a container is given is refused, or None.

    ``source`` is as ``_host_sources`` reads it, a variable as its default, so one
    without a default cannot be judged, nor can a leading ``~``, which Compose reads
    as the home directory."""
    if not source or source[0] in ">|":
        return "continues on a later line, which this reader does not follow"
    if "$" in source or source.startswith("~"):
        return "cannot be read without the environment"
    path = posixpath.normpath(re.sub(r"/+", "/", source))
    if path == ".." or path.startswith("../"):
        return "climbs out of the repo"
    if path in ("/", "/var") or any(path == top or path.startswith(top + "/")
                                    for top in ("/run", "/var/run")):
        return "is where a container runtime keeps its socket, or holds it"
    return None


def _uncommented(line: str) -> str:
    return re.sub(r"\s+#.*$", "", line).strip()


def _plain_value(value: str) -> str:
    """One layer of matching quotes stripped, and ``${VAR:-d}`` or ``${VAR-d}`` read as
    ``d``, which a short form must be before it is split, since ``:-`` is itself a
    colon."""
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?-([^}]*)\}", r"\1", value)


def _host_sources() -> list[tuple[str, str]]:
    """Every host path a container is given, with where it is given: a service
    volume's short-form source (a one-field item is an anonymous volume's target) or
    its long-form ``source:``; a top-level volume's ``device:``; a top-level secret's
    or config's ``file:``, which Compose bind-mounts. A named volume reads as a
    relative path inside the repo, which passes."""
    found = []
    for svc in _service_names():
        body = _service(svc)
        if not any(re.match(r"    volumes:", ln) for ln in body):
            continue
        for line in _block(body, "volumes", 4):
            item = _uncommented(line)
            short = re.match(r"-(\s|$)", item) is not None
            item = re.sub(r"^-(\s+|$)", "", item)
            if m := re.match(r"source:\s*(.*)", item):
                found.append((svc, _plain_value(m.group(1))))
            elif not short or re.match(r"[A-Za-z_]+:(\s|$)", item):
                continue  # a long-form item's other keys
            elif not item or item[0] in ">|":
                found.append((svc, item))
            elif len(fields := _plain_value(item).split(":")) > 1:
                found.append((svc, fields[0]))
    for key, field in (("volumes", "device"), ("secrets", "file"), ("configs", "file")):
        for line in _top_level(key):
            if m := re.match(rf"{field}:\s*(.*)", _uncommented(line)):
                found.append((f"top-level {key}", _plain_value(m.group(1))))
    return found


class NoServiceHoldsTheRuntimeSocketTests(unittest.TestCase):
    """The container runtime's socket is root on the host, and DESIGN.md's "MCP gateway
    — governed tool capability" makes it what nothing in this design may hold. The
    likeliest way it arrives is the one that section names: a stdio-only MCP server,
    which would need it to be spawned.

    Two rules, because neither covers the other. The paths are where Linux runtimes
    keep their sockets, rootless ones included; Docker Desktop keeps its socket under
    the home directory, which only the name scan sees, and only by name."""

    SOCKETS = ("docker.sock", "containerd.sock", "podman.sock")

    def test_no_line_names_a_runtime_socket(self):
        self.assertFalse([line.strip() for line in COMPOSE
                          if not line.strip().startswith("#")
                          and any(s in line for s in self.SOCKETS)])

    def test_no_host_path_is_a_runtime_socket_or_holds_one(self):
        sources = _host_sources()
        self.assertTrue(sources, "no host path read: the reader no longer parses")
        for where, source in sources:
            with self.subTest(given_to=where, source=source):
                self.assertIsNone(_host_path_refusal(source))


class EachServiceIsDeclaredOnceTests(unittest.TestCase):
    """Compose merges every declaration of a name across the ``-f`` files, but every
    per-service rule here reads its first, so a catalogue server also declared in
    docker-compose.yml is read only from that copy, and its declaration in
    mcp-servers.yml could weaken it unseen. Whole-file scans read every copy."""

    def test_no_service_name_repeats_across_the_compose_files(self):
        names = _service_names()
        self.assertEqual(sorted({n for n in names if names.count(n) > 1}), [])


class PublishedPortTests(unittest.TestCase):
    """DESIGN.md says the two UIs' pages are the only ports the stack publishes, and only
    on host loopback: every other listener is on a network the host does not join."""

    def test_only_the_two_uis_publish_and_only_on_loopback(self):
        published = {}
        for svc in _service_names():
            body = _service(svc)
            if any(re.match(r"    ports:", ln) for ln in body):
                published[svc] = [ln.strip() for ln in _block(body, "ports", 4)
                                  if ln.strip() and not ln.strip().startswith("#")]
        self.assertEqual(set(published), {"control-plane-ui", "tasks-ui"})
        for svc, lines in published.items():
            with self.subTest(service=svc):
                self.assertTrue(lines)
                for ln in lines:
                    self.assertTrue(ln.startswith('- "127.0.0.1:'), ln)


class EveryServiceDropsEveryCapabilityTests(unittest.TestCase):
    """What keeps a single-address bind on its own leg. Linux takes a packet for any of
    its addresses on any interface, so a neighbour that could route or forge one would
    reach a listener bound to another network's address through the leg they share: an
    MCP server reaching the task registry's operator listener or the gateway's agent
    listener, the egress proxy the control plane's management listener, the control
    plane the UI's page. Routing takes
    NET_ADMIN and forging takes NET_RAW, which Docker grants by default. So every
    service drops them all, and none adds one back or runs privileged."""

    def test_every_service_drops_all_and_adds_none(self):
        for svc in _service_names():
            body = _service(svc)
            with self.subTest(service=svc):
                self.assertEqual([ln.strip() for ln in _block(body, "cap_drop", 4)
                                  if ln.strip() and not ln.strip().startswith("#")],
                                 ["- ALL"])
                self.assertFalse([ln for ln in body if re.match(r"    cap_add:", ln)])
                # The key, as cap_add's: a quoted "true" is refused without deciding
                # how Compose reads it.
                self.assertFalse([ln for ln in body if re.match(r"    privileged:", ln)])

    def test_every_service_sets_no_new_privileges(self):
        # Dropping every capability empties the bounding set, but a setuid or setgid
        # binary still changes uid or gid on exec: a setuid-root one runs as euid 0,
        # the owner of root's files. `no-new-privileges` stops that, and keeps an LSM
        # from relaxing confinement on exec. Exact, as `cap_drop` is: a
        # `no-new-privileges:false` beside it fails, and so does a new option until
        # this list names it.
        for svc in _service_names():
            body = _service(svc)
            with self.subTest(service=svc):
                self.assertEqual([ln.strip() for ln in _block(body, "security_opt", 4)
                                  if ln.strip() and not ln.strip().startswith("#")],
                                 ["- no-new-privileges:true"])


class EveryImageIsBuiltTests(unittest.TestCase):
    """`make verify-build` is the one check that a Dockerfile still builds, and compose
    builds only the services its active profiles enable. So a profile-gated service
    built from a Dockerfile is built there only if its profile is named."""

    def test_verify_build_names_every_profile_with_a_build(self):
        makefile = (ROOT / "Makefile").read_text()
        recipe = re.search(r"^verify-build:.*?(?=\n\S)", makefile, re.M | re.S).group(0)
        gated = {}
        for svc in _service_names():
            body = _service(svc)
            if any(re.match(r"    build:", ln) for ln in body) and \
                    any(re.match(r"    profiles:", ln) for ln in body):
                gated[svc] = [ln.strip().lstrip("- ") for ln in _block(body, "profiles", 4)
                              if ln.strip().startswith("- ")]
        self.assertEqual(set(gated), {"tasks", "tasks-ui"})
        for svc, profiles in gated.items():
            for profile in profiles:
                with self.subTest(service=svc, profile=profile):
                    self.assertIn(f"--profile {profile} build", recipe)

    def test_the_sandbox_builds_are_the_default_images(self):
        # Neither target builds the JDK variant, so a SANDBOX_JDK exported for a Java
        # project must not reach a launcher there and build it in the default's place.
        makefile = (ROOT / "Makefile").read_text()
        for target in ("verify-build", "rebuild"):
            recipe = re.search(rf"^{target}:.*?(?=\n\S)", makefile, re.M | re.S).group(0)
            runs = [line for line in recipe.splitlines()
                    if '"./$$launcher"' in line and not line.lstrip().startswith("#")]
            with self.subTest(target=target):
                self.assertTrue(runs, f"{target} no longer runs the launchers")
                for line in runs:
                    self.assertIn('SANDBOX_JDK= "./$$launcher" --build-only', line)


class _MakeRun(unittest.TestCase):
    """Runs a target of the Makefile, rather than parsing it, under a temp
    ``DOCKADE_CONFIG_HOME`` and a cleared environment, so neither a caller's
    ``MCP_SECRETS``, ``XDG_CONFIG_HOME`` or ``BACKUP_DIR`` nor the ``make test`` it runs
    under (``-n``, ``-j``) decides what this one does. Stdin is closed."""

    CLEARED = ("MCP_SECRETS", "XDG_CONFIG_HOME", "DOCKADE_CONFIG_HOME", "BACKUP_DIR",
               "MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "MAKELEVEL", "MAKEOVERRIDES",
               "MAKEFILES")
    DEFAULT_TARGET = "secrets-dir"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.use(self.tmp / "absent" / "dockade")

    def use(self, config: Path) -> None:
        self.config, self.secrets = config, config / "secrets"

    def make(self, *args: str, path: str | None = None,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        # Absolute, so not resolved through PATH; a failure without make, not a skip,
        # since this suite runs under it.
        make = shutil.which("make")
        self.assertIsNotNone(make, "make is not installed")
        run_env = {k: v for k, v in os.environ.items() if k not in self.CLEARED}
        run_env["DOCKADE_CONFIG_HOME"] = str(self.config)
        if path is not None:
            run_env["PATH"] = path
        run_env.update(env or {})
        return subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [make, "-s", "-C", str(ROOT), *(args or (self.DEFAULT_TARGET,))], env=run_env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
            check=False)


class SecretsDirTests(_MakeRun):
    """``make secrets-dir`` makes ``MCP_SECRETS`` as the invoking user, since the
    gateway's mount will not have dockerd make it (``create_host_path: false``).
    What matters is who owns what after it, so it is run."""

    def unwritable(self, path: Path) -> None:
        if os.geteuid() == 0:
            self.skipTest("root writes past any mode")
        path.chmod(0o500)
        self.addCleanup(path.chmod, 0o700)

    def test_a_fresh_host_gets_both_directories_as_its_user(self):
        run = self.make()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for path in (self.config, self.secrets):
            with self.subTest(path=path.name):
                self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(self.secrets.stat().st_mode), 0o700)
        again = self.make()
        self.assertEqual((again.returncode, again.stdout + again.stderr), (0, ""))

    def test_an_existing_directory_it_cannot_write_is_a_warning(self):
        # The gateway only reads it. The advice is for its owner, here the user.
        self.secrets.mkdir(parents=True)
        self.unwritable(self.secrets)
        run = self.make()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(f"WARNING: {self.secrets} is not writable by you", run.stdout)
        self.assertIn(f'chmod u+w "{self.secrets}"', run.stdout)

    def test_an_ancestor_it_cannot_write_fails_with_the_advice(self):
        # Compose would fail next on the missing directory, so this fails first. Named
        # from levels up, as the nearest directory that exists.
        locked = self.tmp / "locked"
        locked.mkdir()
        self.use(locked / "absent" / "dockade")
        self.unwritable(locked)
        run = self.make()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(f"cannot make {self.secrets}: {locked} is not writable", run.stdout)
        self.assertIn(f'chmod u+w "{locked}"', run.stdout)
        self.assertFalse((locked / "absent").exists())

    def test_another_owners_ancestor_is_advice_to_chown(self):
        # chmod cannot help with a directory someone else owns.
        usr = Path("/usr")
        if os.geteuid() == 0 or usr.stat().st_uid == os.getuid() or os.access(usr, os.W_OK):
            self.skipTest("needs a directory owned by another user that this one cannot write")
        self.use(usr / f"dockade-test-{os.getpid()}" / "dockade")
        run = self.make()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(f"cannot make {self.secrets}: {usr} is not writable", run.stdout)
        self.assertIn(f'sudo chown {os.getuid()}:{os.getgid()} "{usr}"', run.stdout)
        self.assertNotIn("chmod", run.stdout)

    def test_a_file_in_its_place_is_named_as_one(self):
        self.config.mkdir(parents=True)
        self.secrets.write_text("")
        run = self.make()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(f"cannot make {self.secrets}: {self.secrets} is not a directory",
                      run.stdout)

    def test_a_dry_run_of_up_runs_nothing(self):
        # `make -n` must stay a dry run. A recursive `$(MAKE)` line in a recipe makes
        # GNU make run that whole recipe under -n, compose included, under .ONESHELL.
        stub = self.tmp / "bin"
        stub.mkdir()
        calls = self.tmp / "docker-calls"
        (stub / "docker").write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n')
        (stub / "docker").chmod(0o755)
        run = self.make("-n", "up", path=f"{stub}{os.pathsep}{os.environ['PATH']}")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(calls.exists(), calls.read_text() if calls.exists() else "")
        self.assertFalse(self.config.exists())

    def test_every_target_that_can_start_the_gateway_makes_it_first(self):
        # A bare `$(COMPOSE) up` (re)creates the gateway, whose mount needs the
        # directory; `tasks-up` writes its secret there. In the graph, not in a recipe
        # (the dry-run test above says why), with the mode check after it.
        makefile = (ROOT / "Makefile").read_text()

        def prereqs(target: str) -> list[str]:
            rule = re.search(rf"^{re.escape(target)}:(?!=)([^#\n]*)", makefile, re.M)
            return rule.group(1).split() if rule else []

        def reaches(target: str, seen: frozenset[str] = frozenset()) -> bool:
            return any(dep == "secrets-dir" or (dep not in seen and
                                                reaches(dep, seen | {target}))
                       for dep in prereqs(target))
        self.assertIn("secrets-dir", prereqs("secrets-perm-check"))
        self.assertIn("secrets-perm-check", prereqs("up"))
        for target in ("up", "rebuild", "restore", "tasks-up"):
            with self.subTest(target=target):
                self.assertTrue(reaches(target))


#: The top-level keys each compose file may declare, each once. A ``networks:`` in
#: mcp-servers.yml would be merged over docker-compose.yml's and read by no rule; an
#: ``x-`` key exists to hold an anchor.
_TOP_LEVEL_KEYS = {"docker-compose.yml": ("name", "networks", "volumes", "services"),
                   "mcp-servers.yml": ("services",)}
_KEY_LINE = re.compile(r"(?P<key>[A-Za-z0-9_.-]+):(?: +(?P<value>.*))?")
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"' r"|'(?:[^']|'')*'")
_BLOCK_HEADER = re.compile(r"[|>]-?")
_QUOTED_LIST = re.compile(r'\[ *"[^"\\]*"(?: *, *"[^"\\]*")* *\]')
_TAG_ANCHOR_ALIAS = "a tag, anchor or alias"
_NOT_A_KEY_LINE = "not a plain `key:` line"


def _value_form(value: str, key: str | None) -> tuple[str | None, bool]:
    """What is wrong with a key's or an item's value, if anything, and whether it
    opens a block scalar."""
    v = value.strip()
    if v[0] in "!&*":
        return _TAG_ANCHOR_ALIAS, False
    if v[0] == "#":
        return "a comment where a value belongs", False
    if v[0] in "|>":
        header, _, rest = v.partition(" ")
        if not _BLOCK_HEADER.fullmatch(header) or rest.strip()[:1] not in ("", "#"):
            return "a block scalar header", False
        return None, True
    if v[0] in "[{":
        # mcp-github's `command:`. On one line, so it holds no line a reader could
        # take for a key.
        if key in ("command", "entrypoint") and _QUOTED_LIST.fullmatch(v):
            return None, False
        return "a flow collection", False
    if v[0] in "\"'":
        m = _QUOTED.match(v)
        if not m:
            return "a quoted scalar not closed on its line", False
        if "\\" in m.group(0) and v[0] == '"':
            return "a backslash escape in a double-quoted scalar", False
        rest = v[m.end():]
        if rest and not re.fullmatch(r" +#.*", rest):
            return "text after a quoted scalar", False
        return None, False
    if (v[0] in "-?:" and v[1:2] in ("", " ")) or v[0] in ",]}%@`":
        return "a value starting with an indicator", False
    plain = v.split(" #", 1)[0].rstrip()
    if ": " in plain or plain.endswith(":"):
        return "a mapping inside a plain scalar", False
    return None, False


def _block_scalar_allowed(key: str | None, parent: str | None) -> bool:
    """Where a ``|``/``>`` block may stand: where the files have one today. Its lines
    are text that a reader searching by indent takes for keys (a ``command: |`` line
    at indent 6 is in reach of any indent-6 search), so it stands in as few places as
    the files need."""
    return key in ("command", "entrypoint") or (key, parent) == ("test", "healthcheck")


_BLOCK_SCALAR_ELSEWHERE = "a block scalar outside command, entrypoint or healthcheck test"
_BLANK_IN_BLOCK_SCALAR = "a blank line inside a block scalar"
_FOLDED_DEEPER = "a folded block scalar line deeper than its first"
_KEY_TWICE = "a key twice in one mapping"


class ComposeFormError(Exception):
    """A compose file outside the form ``_compose_tree`` reads. Not an AssertionError,
    so no handler that reads one as "the rule fails" or "absent" can swallow it."""


class _BlockScalar:
    """A ``|``/``>`` block being read: where it goes, and its lines so far."""

    def __init__(self, parent: int, header: str, put: Callable[[str], None]) -> None:
        self.parent = parent        # its key's or dash's column; its lines lie deeper
        self.folded = header.startswith(">")
        self.strip = header.endswith("-")
        self.put = put
        self.indent: int | None = None
        self.lines: list[str] = []
        self.last = 0                # the number of its last line
        self.blanks: list[int] = []  # blank lines since the last line, by number

    def close(self, broken: bool = True) -> None:
        """``broken``: whether its last line ends in a line break, which `|` and `>`
        keep."""
        value = (" " if self.folded else "\n").join(self.lines)
        self.put(value + ("\n" if self.lines and broken and not self.strip else ""))


def _scalar_value(value: str) -> str:
    """A one-line scalar as YAML reads it, every scalar a string: quotes off (``''`` is
    ``'`` in single quotes; the gate refuses escapes in double ones), and a plain
    one's trailing comment off."""
    v = value.strip()
    if v[0] == '"':
        return _QUOTED.match(v).group(0)[1:-1]
    if v[0] == "'":
        return _QUOTED.match(v).group(0)[1:-1].replace("''", "'")
    return v.split(" #", 1)[0].rstrip()


def _walk(name: str, text: str) -> tuple[list[str], dict]:
    """One compose file's lines outside the form read here, as ``file:line: form``,
    and the tree of nested dicts, lists and strings the lines inside it make. The tree
    is whole only when the list is empty. See ComposeFormTests."""
    out: list[str] = []
    root: dict = {}
    # Open blocks: (column, "map" or "seq", the key that opened it, its container).
    stack: list[tuple[int, str, str | None, dict | list]] = [(0, "map", None, root)]
    expect: int | None = None    # where the block under a key with no value opens
    expect_key: str | None = None
    expect_in: dict = root       # the mapping that key is in
    scalar: _BlockScalar | None = None
    top_seen: list[str] = []
    n = 0

    def key(col: int, k: str, value: str | None) -> None:
        nonlocal expect, expect_key, expect_in, scalar
        mapping = stack[-1][3]
        if k in ("extends", "include"):
            out.append(f"{name}:{n}: `{k}`")
        if col == 0:
            if k not in _TOP_LEVEL_KEYS[name]:
                out.append(f"{name}:{n}: top-level `{k}`")
            elif k in top_seen:
                out.append(f"{name}:{n}: top-level `{k}` again")
            top_seen.append(k)
        elif k in mapping:
            # PyYAML keeps the last, a reader the first: two readings of one file.
            out.append(f"{name}:{n}: {_KEY_TWICE}")
        if not value or not value.strip():
            expect, expect_key, expect_in = col + 2, k, mapping
            return
        form, block = _value_form(value, k)
        if form:
            out.append(f"{name}:{n}: {form}")
        elif block:
            if not _block_scalar_allowed(k, stack[-1][2]):
                out.append(f"{name}:{n}: {_BLOCK_SCALAR_ELSEWHERE}")
            scalar = _BlockScalar(col, value.split()[0], lambda v: mapping.__setitem__(k, v))
        elif value.strip()[0] == "[":
            mapping[k] = re.findall(r'"([^"]*)"', value)
        else:
            mapping[k] = _scalar_value(value)

    for n, line in enumerate(text.splitlines(), 1):
        indent = len(line) - len(line.lstrip(" "))
        body = line[indent:]
        if scalar is not None:
            # A line of spaces past the block's indent is a line of it, as YAML reads it.
            if not body.strip() and (scalar.indent is None or len(line) <= scalar.indent):
                scalar.blanks.append(n)
                continue
            if indent > scalar.parent:
                if scalar.indent is None:
                    scalar.indent = indent
                elif indent < scalar.indent:
                    out.append(f"{name}:{n}: a block scalar line shallower than its first")
                    continue
                # A blank line there is a line break YAML keeps, even in a `>` block.
                out.extend(f"{name}:{b}: {_BLANK_IN_BLOCK_SCALAR}" for b in scalar.blanks)
                scalar.blanks = []
                content = line[scalar.indent:]
                if scalar.folded and content[:1] in (" ", "\t"):
                    # Folding keeps a deeper line's breaks; the reader joins with spaces.
                    out.append(f"{name}:{n}: {_FOLDED_DEEPER}")
                scalar.lines.append(content)
                scalar.last = n
                continue
            scalar.close()
            scalar = None
        if line.startswith(("---", "...")):
            out.append(f"{name}:{n}: a document marker")
            continue
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        if "\t" in line:
            out.append(f"{name}:{n}: a tab")
            continue
        if indent % 2:
            out.append(f"{name}:{n}: an odd indent")
            continue
        item = body == "-" or body.startswith("- ")
        if expect is not None:
            if indent == expect:
                block: dict | list = [] if item else {}
                expect_in[expect_key] = block
                stack.append((indent, "seq" if item else "map", expect_key, block))
            else:
                out.append(f"{name}:{n}: not at indent {expect}, under a key with no value")
            expect = None
        while stack[-1][0] > indent:
            stack.pop()
        if stack[-1][0] != indent:
            out.append(f"{name}:{n}: an indent no open block has")
            continue
        if (stack[-1][1] == "seq") != item:
            out.append(f"{name}:{n}: an item in a mapping, or a key in a sequence")
            continue
        if not item:
            m = _KEY_LINE.fullmatch(body)
            if not m:
                out.append(f"{name}:{n}: {_NOT_A_KEY_LINE}")
                continue
            key(indent, m["key"], m["value"])
            continue
        rest = body[2:]
        if not rest.strip() or rest.startswith(" "):
            out.append(f"{name}:{n}: an item not one space after its dash")
            continue
        items = stack[-1][3]
        m = _KEY_LINE.fullmatch(rest)
        if m:
            mapping: dict = {}
            items.append(mapping)
            stack.append((indent + 2, "map", None, mapping))
            key(indent + 2, m["key"], m["value"])
            continue
        form, block_scalar = _value_form(rest, None)
        if form:
            out.append(f"{name}:{n}: {form}")
        elif block_scalar:
            if not _block_scalar_allowed(stack[-1][2], stack[-2][2]):
                out.append(f"{name}:{n}: {_BLOCK_SCALAR_ELSEWHERE}")
            items.append("")
            at = len(items) - 1
            scalar = _BlockScalar(indent, rest.split()[0],
                                  lambda v, items=items, at=at: items.__setitem__(at, v))
        else:
            items.append(_scalar_value(rest))
    if scalar is not None:
        # At the end of the text: its last line has a break if a line or one follows.
        scalar.close(scalar.last < n or text.endswith(("\n", "\r")))
    if expect is not None:
        out.append(f"{name}:{n}: a key with nothing under it")
    return out, root


def _form_violations(name: str, text: str) -> list[str]:
    """Every line of one compose file outside the form the readers here read, as
    ``file:line: form``. See ComposeFormTests."""
    return _walk(name, text)[0]


def _compose_tree(name: str, text: str) -> dict:
    """One compose file as PyYAML's ``BaseLoader`` reads it (nested dicts and lists,
    every scalar a string), or ComposeFormError naming every line outside the form."""
    violations, tree = _walk(name, text)
    if violations:
        raise ComposeFormError("\n".join(violations))
    return tree


#: Any Makefile line that gives COMPOSE a value: every assignment operator, with
#: `override`/`export`, as a target-specific variable, in `$(eval ...)`, or `define`.
_MAKE_SETS_COMPOSE = re.compile(r"\bCOMPOSE\s*(?::{1,3}|[?+!])?=|\bdefine\s+COMPOSE\b")
#: An included makefile could carry its own `COMPOSE +=`.
_MAKE_INCLUDES = re.compile(r"\s*[-s]?include\s")


def _makefile_compose_violations(makefile: str) -> list[str]:
    """Why the Makefile's COMPOSE might not merge exactly COMPOSE_FILES, in order."""
    includes = [f"Makefile:{n}: an include" for n, line in enumerate(makefile.splitlines(), 1)
                if _MAKE_INCLUDES.match(line)]
    if includes:
        return includes
    sets = [(n, line) for n, line in enumerate(makefile.splitlines(), 1)
            if not line.lstrip().startswith("#") and _MAKE_SETS_COMPOSE.search(line)]
    if len(sets) != 1:
        return [f"Makefile:{n}: one of {len(sets)} COMPOSE assignments"
                for n, _ in sets] or ["Makefile: no COMPOSE assignment"]
    n, line = sets[0]
    want = "COMPOSE := docker compose" + "".join(f" -f {f}" for f in COMPOSE_FILES)
    if line != want:
        return [f"Makefile:{n}: {line!r} is not exactly {want!r}"]
    return []


def _swap(old: str, new: str) -> Callable[[str], str]:
    """An edit replacing the first ``old``, which must be there: a mutant that no
    longer applies would pass by testing the real file."""
    def edit(text: str) -> str:
        if old not in text:
            raise AssertionError(f"the mutant no longer applies: {old!r} is gone")
        return text.replace(old, new, 1)
    return edit


def _edits(*edits: Callable[[str], str]) -> Callable[[str], str]:
    def edit(text: str) -> str:
        for one in edits:
            text = one(text)
        return text
    return edit


def _rule(cls: type[unittest.TestCase], method: str) -> Callable[[], bool]:
    """Whether a rule's own test passes, run outside the runner (``subTest`` then
    just yields), so a mutant is judged by the rule rather than a copy of it."""
    def holds() -> bool:
        try:
            getattr(cls(method), method)()
        except AssertionError:
            return False
        return True
    return holds


_TASKS_PROBE = ("        - python3\n        - -c\n        - >-\n"
                "          import os, urllib.request as u;\n"
                "          u.urlopen('http://%s:%s/healthz' % (os.environ['TASKS_AGENT_BIND'],\n"
                "          os.environ['TASKS_AGENT_PORT']), timeout=3)\n")


def _probe_in_a_comment_after(item: str) -> Callable[[str], str]:
    """The tasks probe dialling nothing, the real one kept in a comment after an item."""
    was = (" # u.urlopen('http://%s:%s/healthz' % (os.environ['TASKS_AGENT_BIND'], "
           "os.environ['TASKS_AGENT_PORT']))")
    broken = ("        - python3\n        - -c\n        - >-\n"
              "          import socket; socket.create_connection(('127.0.0.1', 1), 3)\n")
    return _swap(_TASKS_PROBE, broken.replace(f"{item}\n", f"{item}{was}\n", 1))


#: The tasks probe's folded block, its header and first two lines.
_TASKS_PROBE_HEAD = ("        - >-\n          import os, urllib.request as u;\n"
                     "          u.urlopen('http://%s:%s/healthz' % (os.environ['TASKS_AGENT_BIND'],\n")


def _in_tasks_probe(new_head: str) -> Callable[[str], str]:
    return _swap(_TASKS_PROBE_HEAD, new_head)


_LABEL_BLOCK_SCALAR = _swap("    name: sandbox-net\n    driver: bridge\n    internal: true\n",
                            "    name: sandbox-net\n    driver: bridge\n    labels:\n"
                            "      note: |\n        internal: true\n")


class ComposeFormTests(unittest.TestCase):
    """The readers above take nesting from indentation and a key from ``key:`` at
    the start of a line; Compose reads YAML. Where those differ a rule reads one
    service and Compose runs another: ``networks: !reset`` makes Compose drop the
    block every placement rule still reads, and ``"privileged": true`` is a key no
    rule sees. So both files are held to the subset where the two agree, and a form
    outside it fails here rather than every rule having to consider it:

    - one document, its top-level keys from ``_TOP_LEVEL_KEYS``, each once;
    - every key plain and unquoted, ``key:`` with no space before the colon, never
      ``extends`` or ``include``; no tags, anchors, aliases or merge keys;
    - indentation in steps of two, a key's block opening exactly two deeper;
    - block style, the exception a one-line list of double-quoted strings under
      ``command``/``entrypoint``; every scalar on one line, with no backslash escape
      in a double-quoted one (``"/\\x72un"`` is ``/run``);
    - a ``|``/``>`` block only under ``command``, ``entrypoint`` or a healthcheck's
      ``test``, where the files have them today; its header with no indentation
      indicator or ``+``, no blank line before or between its lines, and in a ``>``
      one no line deeper than its first, since YAML keeps those line breaks;
    - no key twice in one mapping, which PyYAML reads as the last and a line reader
      as the first;
    - a key with no value has nothing after its colon, not even a comment
      (``_service_names`` matches the bare line);
    - the Makefile's COMPOSE merges exactly ``COMPOSE_FILES``, in order, is assigned
      once, and no makefile is included. A computed variable name (``$(X)SE +=``)
      is not looked for.

    The gate fixes the form; it does not make a reader read at the right depth. A
    label, a long-form mount or a block scalar's text can spell any key at a depth a
    reader searching a block takes for its own. These readers take a key at its own
    depth, each shown by a ``READER_MUTANTS`` decoy that the searching version reads
    wrongly: ``_scalar`` (and ``_is_internal``, ``_masquerade_off``, ``_restart_of``
    and the ``user`` and ``image`` checks, which go through it), ``_ipam_of`` and
    ``_subnet_of``, ``_pin``, ``_probe``, ``_mount_spellings``, ``_long_mount``, and
    ``_environment_of``'s cut of a trailing comment. The other readers are not yet
    anchored and may still search a block at any depth; task 240 replaces the line
    readers with one tree reader.

    A rule that needs a form outside this subset widens it here first, with a
    mutant showing the readers follow it.
    """

    #: (what, file, edit of the real text, the form the gate must name)
    MUTANTS: ClassVar[list[tuple[str, str, Callable[[str], str], str]]] = [
        ("networks reset", "mcp-servers.yml",
         _swap("    networks:\n      mcp-net:", "    networks: !reset\n      mcp-net:"),
         _TAG_ANCHOR_ALIAS),
        ("networks reset, two spaces", "mcp-servers.yml",
         _swap("    networks:\n      mcp-net:", "    networks:  !reset\n      mcp-net:"),
         _TAG_ANCHOR_ALIAS),
        ("cap_drop reset", "mcp-servers.yml",
         _swap("    cap_drop:\n", "    cap_drop: !reset\n"), _TAG_ANCHOR_ALIAS),
        ("security_opt reset", "mcp-servers.yml",
         _swap("    security_opt:\n", "    security_opt: !reset\n"), _TAG_ANCHOR_ALIAS),
        ("quoted cap_add", "mcp-servers.yml",
         _swap("    read_only: true\n", '    "cap_add": [NET_ADMIN]\n    read_only: true\n'),
         _NOT_A_KEY_LINE),
        ("quoted network_mode", "mcp-servers.yml",
         _swap("    read_only: true\n", '    "network_mode": host\n    read_only: true\n'),
         _NOT_A_KEY_LINE),
        ("quoted privileged", "mcp-servers.yml",
         _swap("    read_only: true\n", '    "privileged": true\n    read_only: true\n'),
         _NOT_A_KEY_LINE),
        ("? key", "mcp-servers.yml",
         _swap("    read_only: true\n", "    ? privileged\n    : true\n    read_only: true\n"),
         _NOT_A_KEY_LINE),
        ("space before the colon", "mcp-servers.yml",
         _swap("    read_only: true\n", "    pid : host\n    read_only: true\n"),
         _NOT_A_KEY_LINE),
        ("networks redeclared", "mcp-servers.yml",
         _swap("services:\n", "networks:\n  mcp-net:\n    internal: false\nservices:\n"),
         "top-level `networks`"),
        ("service key comment", "mcp-servers.yml",
         _swap("  mcp-github:\n", "  mcp-github: # the first server\n"),
         "a comment where a value belongs"),
        ("second document", "mcp-servers.yml",
         lambda t: t + "---\nservices:\n  mcp-x:\n    privileged: true\n",
         "a document marker"),
        ("anchor", "mcp-servers.yml",
         _swap("    cap_drop:\n", "    cap_drop: &drops\n"), _TAG_ANCHOR_ALIAS),
        ("alias", "mcp-servers.yml",
         _swap("    read_only: true\n", "    cap_add: *drops\n    read_only: true\n"),
         _TAG_ANCHOR_ALIAS),
        ("x- key", "mcp-servers.yml",
         _swap("services:\n", "x-base: &base\n  privileged: true\nservices:\n"),
         "top-level `x-base`"),
        ("merge key", "mcp-servers.yml",
         _swap("    read_only: true\n", "    <<: *base\n    read_only: true\n"),
         _NOT_A_KEY_LINE),
        ("extends", "mcp-servers.yml",
         _swap("    read_only: true\n",
               "    extends:\n      file: other.yml\n      service: base\n"
               "    read_only: true\n"),
         "`extends`"),
        ("include", "docker-compose.yml",
         _swap("\nservices:\n", "\ninclude:\n  - other.yml\nservices:\n"), "`include`"),
        ("flow volumes", "mcp-servers.yml",
         _swap("    read_only: true\n", '    volumes: ["/run:/x"]\n    read_only: true\n'),
         "a flow collection"),
        ("flow volume item", "mcp-servers.yml",
         _swap("    read_only: true\n",
               "    volumes:\n      - {type: bind, source: /run, target: /x}\n"
               "    read_only: true\n"),
         "a flow collection"),
        ("compact volumes", "mcp-servers.yml",
         _swap("    read_only: true\n", "    volumes:\n    - /run:/x\n    read_only: true\n"),
         "under a key with no value"),
        ("nested sequence item", "mcp-servers.yml",
         _swap("      - ALL\n", "      - - ALL\n"), "a value starting with an indicator"),
        ("? item", "mcp-servers.yml",
         _swap("      - ALL\n", "      - ? ALL\n"), "a value starting with an indicator"),
        ("service at indent 4", "mcp-servers.yml",
         lambda t: t.replace("\n  ", "\n    "), "under a key with no value"),
        # A block scalar must not swallow the key after it, whether its key is a
        # mapping's or an item's (its lines lie deeper than the key, not the dash).
        ("tag after a folded entrypoint", "docker-compose.yml",
         _swap("        --reasoning ${DOCKADE_LLM_REASONING:-off}\n",
               "        --reasoning ${DOCKADE_LLM_REASONING:-off}\n    cap_drop: !reset\n"),
         _TAG_ANCHOR_ALIAS),
        ("tag after a block scalar in an item", "docker-compose.yml",
         _swap("      - type: bind\n        source:",
               "      - type: >-\n          bind\n        read_only: !reset\n        source:"),
         _TAG_ANCHOR_ALIAS),
        ("tagged item after an item's block scalar", "docker-compose.yml",
         _swap("        --reasoning ${DOCKADE_LLM_REASONING:-off}\n",
               "        --reasoning ${DOCKADE_LLM_REASONING:-off}\n      - !reset x\n"),
         _TAG_ANCHOR_ALIAS),
        ("block scalar in a label", "docker-compose.yml", _LABEL_BLOCK_SCALAR,
         _BLOCK_SCALAR_ELSEWHERE),
        ("escape in a short volume", "mcp-servers.yml",
         _swap("    read_only: true\n",
               '    volumes:\n      - "/\\x72un:/host-run"\n    read_only: true\n'),
         "a backslash escape"),
        ("escape in a long volume", "mcp-servers.yml",
         _swap("    read_only: true\n",
               "    volumes:\n      - type: bind\n"
               '        source: "/var/\\x72un"\n        target: /x\n    read_only: true\n'),
         "a backslash escape"),
        ("tab", "mcp-servers.yml",
         _swap("    read_only: true\n", "  \tprivileged: true\n    read_only: true\n"),
         "a tab"),
        ("plain scalar continued", "mcp-servers.yml",
         _swap("    hostname: mcp-github\n", "    hostname: mcp-github\n      privileged\n"),
         "an indent no open block has"),
        ("odd indent", "mcp-servers.yml",
         _swap("    read_only: true\n", "     privileged: true\n    read_only: true\n"),
         "an odd indent"),
        ("two spaces after a dash", "mcp-servers.yml",
         _swap("      - ALL\n", "      -   ALL\n"), "not one space after its dash"),
        ("quoted scalar on two lines", "mcp-servers.yml",
         _swap("      - ALL\n", '      - "AL\n        L"\n'), "not closed on its line"),
        ("quoted key in an item", "mcp-servers.yml",
         _swap("      - ALL\n", '      - "cap_add": x\n'), "text after a quoted scalar"),
        ("spaced key in an item", "mcp-servers.yml",
         _swap("      - ALL\n", "      - cap add: x\n"), "a mapping inside a plain scalar"),
        ("top-level key twice", "docker-compose.yml",
         lambda t: t + "volumes:\n  x:\n    name: y\n", "top-level `volumes` again"),
        ("key with nothing under it", "mcp-servers.yml",
         lambda t: t + "    privileged:\n", "a key with nothing under it"),
        ("keep chomping", "docker-compose.yml",
         _swap("      - >\n        export LD", "      - >+\n        export LD"),
         "a block scalar header"),
        ("indentation indicator", "docker-compose.yml",
         _swap("      - >\n        export LD", "      - >2\n        export LD"),
         "a block scalar header"),
        ("blank line between a block's lines", "docker-compose.yml",
         _in_tasks_probe(_TASKS_PROBE_HEAD.replace(";\n", ";\n\n", 1)),
         _BLANK_IN_BLOCK_SCALAR),
        ("blank line before a block's first", "docker-compose.yml",
         _in_tasks_probe(_TASKS_PROBE_HEAD.replace(">-\n", ">-\n\n", 1)),
         _BLANK_IN_BLOCK_SCALAR),
        ("folded line deeper than its first", "docker-compose.yml",
         _in_tasks_probe(_TASKS_PROBE_HEAD.replace(";\n", ";\n            pass;\n", 1)),
         _FOLDED_DEEPER),
        ("key twice in a service", "mcp-servers.yml",
         _swap("    read_only: true\n", "    read_only: true\n    read_only: false\n"),
         _KEY_TWICE),
        ("key twice in an environment", "mcp-servers.yml",
         _swap("      HTTPS_PROXY: http://egress-proxy:8080\n",
               "      HTTPS_PROXY: http://egress-proxy:8080\n"
               "      HTTPS_PROXY: http://203.0.113.9:3128\n"),
         _KEY_TWICE),
    ]

    #: (what, file, edit of the real text) the gate accepts, each a form at the edge
    #: of the subset, so the parity tests read them too.
    ACCEPTED: ClassVar[list[tuple[str, str, Callable[[str], str]]]] = [
        ("blank line after a block's last", "docker-compose.yml",
         _swap("          os.environ['TASKS_AGENT_PORT']), timeout=3)\n",
               "          os.environ['TASKS_AGENT_PORT']), timeout=3)\n\n")),
        ("blank line after a literal block's last, at the end", "mcp-servers.yml",
         _swap('    command: ["http", "--listen-host", "0.0.0.0", "--port", "8082"]\n',
               "    command: |\n      http\n        --port 8082\n\n")),
    ]

    #: (what, file, edit of the real text, a rule's reading that holds on the real
    #: files and must not on the edit). Each edit hides the real key and leaves a
    #: decoy deeper down, where a reader that searches a block would find it.
    READER_MUTANTS: ClassVar[list[tuple[str, str, Callable[[str], str],
                                         Callable[[], bool]]]] = [
        ("block scalar label", "docker-compose.yml", _LABEL_BLOCK_SCALAR,
         lambda: _is_internal("sandbox-net")),
        ("boolean label", "docker-compose.yml",
         _swap("    driver: bridge\n    internal: true\n",
               "    driver: bridge\n    labels:\n      internal: true\n"),
         lambda: _is_internal("sandbox-net")),
        ("read_only in a long tmpfs mount", "docker-compose.yml",
         _swap("    pids_limit: 128\n    read_only: true\n    tmpfs:\n      - /tmp\n"
               "    cap_drop:",
               "    pids_limit: 128\n    volumes:\n      - type: tmpfs\n        target: /tmp\n"
               "        read_only: true\n    read_only: false\n    tmpfs:\n      - /tmp\n"
               "    cap_drop:"),
         lambda: _scalar(_service("tasks-ui"), "read_only") == "true"),
        ("ipam in a label", "docker-compose.yml",
         _swap("    internal: true\n    ipam:\n      config:\n"
               "        - subnet: 172.30.0.0/24\n"
               "          ip_range: 172.30.0.128/25\n          gateway: 172.30.0.1\n",
               "    internal: true\n    labels:\n      subnet: 172.30.0.0/24\n"
               "      ip_range: 172.30.0.128/25\n      gateway: 172.30.0.1\n"
               "    ipam:\n      config:\n        - subnet: 10.30.0.0/24\n"
               "          ip_range: 10.30.0.0/24\n          gateway: 10.30.0.1\n"),
         lambda: (_subnet_of("sandbox-net"), _ipam_of("sandbox-net", "ip_range"),
                  _ipam_of("sandbox-net", "gateway"))
         == ("172.30.0.0/24", "172.30.0.128/25", "172.30.0.1")),
        ("restart in a label", "docker-compose.yml",
         _swap("    restart: always\n",
               '    labels:\n      restart: always\n    restart: "no"\n'),
         lambda: _restart_of("egress-proxy") == "always"),
        ("masquerade in a label", "docker-compose.yml",
         _swap("    driver_opts:\n      com.docker.network.bridge.enable_ip_masquerade",
               "    labels:\n      com.docker.network.bridge.enable_ip_masquerade"),
         lambda: _masquerade_off("control-ui-net")),
        ("user in a label", "docker-compose.yml",
         _swap('    user: "${DOCKADE_UID:-1000}:${DOCKADE_GID:-1000}"\n',
               '    labels:\n      user: "${DOCKADE_UID:-1000}:${DOCKADE_GID:-1000}"\n'
               '    user: "0"\n'),
         _rule(GatewayPlacementTests,
               "test_the_gateway_runs_as_a_user_that_can_read_the_secrets_mount")),
        ("image in a label", "docker-compose.yml",
         _swap("    image: dockade-control-plane\n",
               "    labels:\n      image: dockade-control-plane\n    image: other\n"),
         _rule(BackupToolTargetsAgreeTests, "test_the_image_is_the_one_compose_builds")),
        ("read_only under bind", "docker-compose.yml",
         _swap("        read_only: true\n        bind:\n          create_host_path: false\n",
               "        read_only: false\n        bind:\n          create_host_path: false\n"
               "          x-was:\n            read_only: true\n"),
         _rule(GatewayPlacementTests,
               "test_the_secrets_mount_is_read_only_and_never_made_by_docker")),
        ("writable long-form tool-audit, :ro in a label", "docker-compose.yml",
         _edits(_swap("      - tool-audit:/var/log/tool-gateway:ro\n",
                      "      - type: volume\n        source: tool-audit\n"
                      "        target: /var/log/tool-gateway\n"),
                _swap("    volumes:\n      - control-state:",
                      "    labels:\n      - tool-audit:/var/log/tool-gateway:ro\n"
                      "    volumes:\n      - control-state:")),
         _rule(OutcomeStreamWiringTests, "test_the_control_plane_mounts_it_read_only")),
        ("long-form tool-audit on a server", "mcp-servers.yml",
         _swap("    read_only: true\n",
               "    volumes:\n      - type: volume\n        source: tool-audit\n"
               "        target: /x\n    read_only: true\n"),
         _rule(OutcomeStreamWiringTests, "test_nothing_else_touches_it")),
        ("tasks-state in a label, mounted elsewhere", "docker-compose.yml",
         _edits(_swap("      - tasks-state:/var/lib/task-registry\n",
                      "      - type: volume\n        source: tasks-state\n"
                      "        target: /elsewhere\n"),
                _swap("    container_name: tasks\n",
                      "    container_name: tasks\n    labels:\n"
                      "      - tasks-state:/var/lib/task-registry\n")),
         _rule(TaskRegistryPlacementTests,
               "test_its_store_is_a_volume_of_its_own_where_the_store_looks")),
        ("pin in an x- key", "mcp-servers.yml",
         _edits(_swap("    networks:\n      mcp-net:\n",
                      "    x-plan:\n      mcp-net:\n        ipv4_address: 172.28.0.11\n"
                      "    networks:\n      mcp-net:\n"),
                _swap("        ipv4_address: 172.28.0.11\n    environment:",
                      "        priority: 1\n    environment:")),
         _rule(McpServerPlacementTests, "test_every_mcp_server_pins_its_address")),
        ("pin in a command block", "mcp-servers.yml",
         _edits(_swap('    command: ["http", "--listen-host", "0.0.0.0", "--port", "8082"]\n',
                      "    command: |\n      http\n      mcp-net:\n"
                      "        ipv4_address: 172.28.0.11\n"),
                _swap("        ipv4_address: 172.28.0.11\n    environment:",
                      "        priority: 1\n    environment:")),
         _rule(McpServerPlacementTests, "test_every_mcp_server_pins_its_address")),
        ("old probe in a comment", "docker-compose.yml",
         _swap("          import os, urllib.request as u;\n"
               "          u.urlopen('http://%s:%s/healthz' % (os.environ['TASKS_AGENT_BIND'],\n"
               "          os.environ['TASKS_AGENT_PORT']), timeout=3)\n",
               "          import socket; socket.create_connection(('127.0.0.1', 1), 3)\n"
               "        # was: u.urlopen('http://%s:%s/healthz' % (os.environ['TASKS_AGENT_BIND'],"
               " os.environ['TASKS_AGENT_PORT']))\n"),
         _rule(TaskRegistryPlacementTests,
               "test_the_healthcheck_dials_the_agent_listener_where_it_binds")),
        ("quoted tool-audit on a server", "mcp-servers.yml",
         _swap("    read_only: true\n",
               '    volumes:\n      - type: volume\n        source: "tool-audit"\n'
               "        target: /x\n    read_only: true\n"),
         _rule(OutcomeStreamWiringTests, "test_nothing_else_touches_it")),
        ("quoted tasks-state on a server", "mcp-servers.yml",
         _swap("    read_only: true\n",
               "    volumes:\n      - type: volume\n        source: 'tasks-state'\n"
               "        target: /var/lib/task-registry\n    read_only: true\n"),
         _rule(TaskRegistryPlacementTests,
               "test_its_store_is_a_volume_of_its_own_where_the_store_looks")),
        ("read_only under bind over the item's", "docker-compose.yml",
         _swap("        read_only: true\n        bind:\n          create_host_path: false\n",
               "        read_only: false\n        bind:\n          create_host_path: false\n"
               "          read_only: true\n"),
         _rule(GatewayPlacementTests,
               "test_the_secrets_mount_is_read_only_and_never_made_by_docker")),
        ("old probe after an item", "docker-compose.yml",
         _probe_in_a_comment_after("        - python3"),
         _rule(TaskRegistryPlacementTests,
               "test_the_healthcheck_dials_the_agent_listener_where_it_binds")),
        ("old probe after the -c item", "docker-compose.yml",
         _probe_in_a_comment_after("        - -c"),
         _rule(TaskRegistryPlacementTests,
               "test_the_healthcheck_dials_the_agent_listener_where_it_binds")),
        ("proxy in a comment", "mcp-servers.yml",
         _swap("      HTTPS_PROXY: http://egress-proxy:8080\n",
               "      HTTPS_PROXY: http://203.0.113.9:3128 # was egress-proxy\n"),
         _rule(McpServerPlacementTests, "test_mcp_servers_are_told_to_use_the_proxy")),
    ]

    #: (what, edit of the real Makefile)
    MAKEFILE_MUTANTS: ClassVar[list[tuple[str, Callable[[str], str]]]] = [
        ("a second assignment", lambda t: t + "COMPOSE += -f x.yml\n"),
        ("target-specific", lambda t: t + "up: COMPOSE += -f x.yml\n"),
        ("in a recipe's eval", lambda t: t + "x:\n\t$(eval COMPOSE += -f x.yml)\n"),
        ("define", lambda t: t + "define COMPOSE\ndocker compose\nendef\n"),
        ("override", _swap("COMPOSE :=", "override COMPOSE :=")),
        ("reordered", _swap("-f docker-compose.yml -f mcp-servers.yml",
                            "-f mcp-servers.yml -f docker-compose.yml")),
        ("an extra file", _swap("-f mcp-servers.yml", "-f mcp-servers.yml -f x.yml")),
        ("an include", lambda t: t + "-include local.mk\n"),
    ]

    def test_each_compose_file_is_in_the_form_the_readers_read(self):
        for name in COMPOSE_FILES:
            with self.subTest(file=name):
                self.assertEqual(_form_violations(name, (ROOT / name).read_text()), [])

    def test_every_form_outside_it_is_named(self):
        for what, name, edit, form in self.MUTANTS:
            with self.subTest(mutant=what):
                found = _form_violations(name, edit((ROOT / name).read_text()))
                self.assertTrue([v for v in found if form in v], found)

    def test_a_refused_file_is_never_read_as_a_tree(self):
        for what, name, edit, form in self.MUTANTS:
            with self.subTest(mutant=what):
                with self.assertRaises(ComposeFormError) as refused:
                    _compose_tree(name, edit((ROOT / name).read_text()))
                self.assertIn(form, str(refused.exception))

    def test_each_accepted_edit_is_accepted(self):
        for what, name, edit in self.ACCEPTED:
            with self.subTest(edit=what):
                self.assertEqual(_form_violations(name, edit((ROOT / name).read_text())), [])

    def test_each_anchored_reader_reads_a_key_at_its_own_depth(self):
        module = sys.modules[__name__]
        for what, name, edit, reading in self.READER_MUTANTS:
            with self.subTest(mutant=what):
                self.assertTrue(reading())
                mutated = edit((ROOT / name).read_text()).splitlines()
                lines = [line for f in COMPOSE_FILES
                         for line in (mutated if f == name
                                      else (ROOT / f).read_text().splitlines())]
                with mock.patch.object(module, "COMPOSE", lines):
                    try:
                        held = reading()
                    except AssertionError:  # the reader found no key: also a failing rule
                        held = False
                self.assertFalse(held)

    def test_the_makefile_merges_exactly_the_files_read_here(self):
        self.assertEqual(_makefile_compose_violations((ROOT / "Makefile").read_text()), [])

    def test_every_other_compose_assignment_fails_the_pin(self):
        makefile = (ROOT / "Makefile").read_text()
        for what, edit in self.MAKEFILE_MUTANTS:
            with self.subTest(mutant=what):
                self.assertNotEqual(_makefile_compose_violations(edit(makefile)), [])


#: Same arrangement as ``test_control_plane_ui_js.py``: without PyYAML the parity
#: tests skip, and DOCKADE_REQUIRE_TOOLS turns the skip into a failure.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))
#: Reads a JSON list of texts on stdin, writes their trees.
_PYYAML_TREES = ("import json, sys, yaml\n"
                 "json.dump([yaml.load(t, Loader=yaml.BaseLoader)"
                 " for t in json.load(sys.stdin)], sys.stdout)\n")


def _shebang_python(script: str) -> list[str] | None:
    """The command a script's ``#!`` line names, if it runs a Python: an interpreter
    named ``python...``, given or found by ``env``. Anything else, a ``/bin/sh``
    wrapper say, would be run with Python's arguments."""
    with open(script, encoding="utf-8", errors="replace") as text:
        first = text.readline()
    command = shlex.split(first[2:]) if first.startswith("#!") else []
    if command and os.path.basename(command[0]) == "env":
        interpreter = command[1] if len(command) > 1 else ""
    else:
        interpreter = command[0] if command else ""
    return command if os.path.basename(interpreter).startswith("python") else None


def _pyyaml_command() -> list[str] | None:
    """A Python that imports PyYAML: this one, else the one yamllint's shebang names,
    since yamllint needs PyYAML; or None. Run, not imported, so the unit suite itself
    still needs no package."""
    commands = [[sys.executable]]
    yamllint = shutil.which("yamllint")
    if yamllint is not None and (command := _shebang_python(yamllint)):
        commands.append(command)
    for command in commands:
        try:
            found = subprocess.run(  # noqa: S603 (an interpreter path, fixed arguments)
                [*command, "-I", "-c", "import yaml"], stdin=subprocess.DEVNULL,
                capture_output=True, timeout=60, check=False)
        except OSError:
            continue
        if found.returncode == 0:
            return command
    return None


class PyyamlLookupTests(unittest.TestCase):
    """Which ``#!`` lines ``_pyyaml_command`` will run as a Python."""

    CASES: ClassVar[list[tuple[str, list[str] | None]]] = [
        ("#!/home/u/.local/share/pipx/venvs/yamllint/bin/python\n",
         ["/home/u/.local/share/pipx/venvs/yamllint/bin/python"]),
        ("#!/usr/bin/env python3\n", ["/usr/bin/env", "python3"]),
        ("#!/bin/sh\n", None),
        ("#!/usr/bin/env sh\n", None),
        ("#!/usr/bin/env\n", None),
        ("import yamllint\n", None),
    ]

    def test_only_a_python_shebang_is_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            for first, want in self.CASES:
                with self.subTest(shebang=first):
                    script = Path(tmp) / "yamllint"
                    script.write_text(first + "rest\n")
                    self.assertEqual(_shebang_python(str(script)), want)


class ComposeTreeParityTests(unittest.TestCase):
    """``_compose_tree`` reads what PyYAML's ``BaseLoader`` reads, on every file the
    gate accepts that this suite has: the real ones, each table's edits, and the forms
    the subset allows that the real files lack."""

    #: (what, text), each read as mcp-servers.yml.
    SYNTHETIC: ClassVar[list[tuple[str, str]]] = [
        ("quoted scalars",
         "services:\n  x:\n    image: 'it''s'\n    hostname: \"a 'b' # c\"\n"
         "    environment:\n      A: 'x # not a comment'\n      B: plain # a comment\n"
         "      C: \"1\"  # after a quote\n      D: a#b\n"),
        ("literal and folded blocks",
         "services:\n  x:\n    command: |\n      run  \n      # a line of text\n"
         "        deeper\n    entrypoint: |-\n      one\n      two\n"
         "    healthcheck:\n      test:\n        - CMD\n        - >\n"
         "          folded  \n          text\n\n    restart: always\n"),
        ("a line of spaces past a literal block's indent",
         "services:\n  x:\n    command: |\n      a\n" + " " * 9 + "\n    restart: x\n"),
        ("a block with no lines, and one at the end of the text",
         "services:\n  x:\n    command: |\n    entrypoint: >\n      a\n      b"),
        ("a block, then a line of spaces, at the end of the text",
         "services:\n  x:\n    command: |\n      a\n  "),
        ("a quoted list and a sequence of mappings",
         "services:\n  x:\n    command: [\"a b\", \"c\"]\n    volumes:\n"
         "      - type: bind\n        bind:\n          create_host_path: false\n"
         "      - a:/b\n"),
    ]

    command: ClassVar[list[str]]

    @classmethod
    def setUpClass(cls) -> None:
        command = _pyyaml_command()
        if command is not None:
            cls.command = command
            return
        if _STRICT:
            raise AssertionError(
                "no Python here imports PyYAML and DOCKADE_REQUIRE_TOOLS is set — "
                "refusing to report success for the compose tree's parity tests. "
                "Install yamllint, or drop strict mode to skip them knowingly.")
        raise unittest.SkipTest("no Python here imports PyYAML")

    def assert_parity(self, docs: list[tuple[str, str, str]]) -> None:
        """Each (what, file, text) read alike by the tree and by PyYAML."""
        self.assertTrue(docs)
        run = subprocess.run(  # noqa: S603 (an interpreter path, fixed arguments)
            [*self.command, "-I", "-c", _PYYAML_TREES],
            input=json.dumps([text for _, _, text in docs]),
            capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        for (what, name, text), want in zip(docs, json.loads(run.stdout), strict=True):
            with self.subTest(what=what):
                self.assertEqual(_compose_tree(name, text), want)

    def test_the_real_files(self):
        self.assert_parity([(name, name, (ROOT / name).read_text())
                            for name in COMPOSE_FILES])

    def test_every_edit_the_gate_accepts(self):
        tables = ComposeFormTests
        edits = [(what, name, edit) for what, name, edit, *_ in
                 (*tables.MUTANTS, *tables.READER_MUTANTS, *tables.ACCEPTED)]
        docs = [(what, name, text) for what, name, edit in edits
                if not _form_violations(name, text := edit((ROOT / name).read_text()))]
        # Every MUTANTS edit is refused, and so is the one READER_MUTANTS row that is
        # also a MUTANTS row.
        self.assertEqual(len(docs), len(edits) - len(tables.MUTANTS) - 1)
        self.assert_parity(docs)

    def test_the_forms_the_files_lack(self):
        docs = [(what, "mcp-servers.yml", text) for what, text in self.SYNTHETIC]
        for what, name, text in docs:
            with self.subTest(what=what):
                self.assertEqual(_form_violations(name, text), [])
        self.assert_parity(docs)


class BackupRestoreRecipeTests(_MakeRun):
    """``backup``, ``restore`` and ``restore-tasks`` run against a stub ``docker`` that
    logs each call, answers every inspect, says the registry is running when asked, and
    has the store's tool (the ``python3`` entrypoint, not the preflight's ``test``)
    exit with the status a test sets. What is checked is which calls the recipe makes
    after the tool: a restart on every status but the sidecar one."""

    SIDECAR_LEFT = "3"

    def setUp(self):
        super().setUp()
        self.backups = self.tmp / "backups"
        self.calls = self.tmp / "docker-calls"
        stub = self.tmp / "bin"
        stub.mkdir()
        (stub / "docker").write_text(f"""#!/bin/sh
# One line a call: the inline programs are arguments, and span lines.
printf '%s\\n' "$*" | tr '\\n' ' ' >> "{self.calls}"; echo >> "{self.calls}"
case " $* " in
  *" --entrypoint python3 "*) cat > /dev/null; exit "$TOOL_STATUS";;
  " compose "*" ps "*) if [ -z "$STOPPED" ]; then echo 0123456789ab; fi;;
esac
""")
        (stub / "docker").chmod(0o755)
        self.path = f"{stub}{os.pathsep}{os.environ['PATH']}"

    def run_target(self, *args: str, tool_status: str, stopped: bool = False,
                   ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        run = self.make(*args, path=self.path,
                        env={"TOOL_STATUS": tool_status, "BACKUP_DIR": str(self.backups),
                             "STOPPED": "1" if stopped else ""})
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return run, calls

    def after_tool(self, calls: list[str]) -> list[str]:
        tool = [i for i, c in enumerate(calls) if "--entrypoint python3" in c]
        self.assertEqual(len(tool), 1, calls)
        return calls[tool[0] + 1:]

    def backup_file(self, name: str) -> str:
        path = self.tmp / name
        path.write_bytes(b"SQLite format 3\0" + b"\0" * 100)
        return str(path)

    def restore(self, target: str, prefix: str, tool_status: str, stopped: bool = False):
        return self.run_target(target, f"FILE={self.backup_file(prefix + '-x.db')}",
                               "FORCE=1", tool_status=tool_status, stopped=stopped)

    def test_a_restore_that_left_a_sidecar_does_not_start_the_stack(self):
        for target, prefix in (("restore", "dockade-control"),
                               ("restore-tasks", "dockade-tasks")):
            with self.subTest(target=target):
                self.calls.unlink(missing_ok=True)
                run, calls = self.restore(target, prefix, self.SIDECAR_LEFT)
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertIn(f"{target}: ", run.stdout)
                self.assertIn("stopped while that file is there", run.stdout)
                self.assertEqual(self.after_tool(calls), [])

    def test_a_stopped_registry_left_with_a_sidecar_is_told_so(self):
        run, calls = self.restore("restore-tasks", "dockade-tasks", self.SIDECAR_LEFT,
                                  stopped=True)
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("stays stopped while that file is there", run.stdout)
        self.assertFalse([c for c in calls if " up " in f"{c} "], calls)

    def test_a_refused_restore_starts_the_stack_again(self):
        for target, prefix, started in (("restore", "dockade-control", " up "),
                                        ("restore-tasks", "dockade-tasks",
                                         " up -d --wait --wait-timeout 120 tasks tasks-ui")):
            with self.subTest(target=target):
                self.calls.unlink(missing_ok=True)
                run, calls = self.restore(target, prefix, "1")
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertNotIn("stays stopped", run.stdout)
                [after] = self.after_tool(calls)
                self.assertTrue(after.startswith("compose "), after)
                self.assertIn(started, f"{after} ")

    def test_a_restore_that_ran_starts_the_stack_again(self):
        run, calls = self.restore("restore", "dockade-control", "0")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        [after] = self.after_tool(calls)
        self.assertIn(" up ", after)

    def test_a_failed_control_plane_snapshot_says_so_and_leaves_no_partial(self):
        run, calls = self.run_target("backup", tool_status="1")
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertIn("backup: FAILED — the control plane's snapshot did not run", run.stdout)
        self.assertNotIn("Left at", run.stdout)
        self.assertEqual(list(self.backups.iterdir()), [])
        # It stops there: the task registry's half is not tried.
        self.assertEqual(self.after_tool(calls), [])


if __name__ == "__main__":
    unittest.main()
