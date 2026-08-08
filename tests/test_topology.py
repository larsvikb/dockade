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

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text().splitlines()
ADDON = (ROOT / "proxies" / "egress" / "addon.py").read_text()
APP = (ROOT / "control-plane" / "app.py").read_text()

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


def _service(name: str) -> list[str]:
    return _block(_block(COMPOSE, "services", 0), name, 2)


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

    def test_the_networks_the_guard_blocks_are_internal(self):
        # An internal bridge has no route off-box. Both control networks must be
        # one, or the control plane itself would gain egress.
        for network in ("control-net", "authorize-net", "sandbox-net"):
            body = _block(_block(COMPOSE, "networks", 0), network, 2)
            self.assertTrue(
                any(re.match(r"\s*internal:\s*true\s*$", line) for line in body),
                f"network {network} is not internal: true")


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


if __name__ == "__main__":
    unittest.main()
