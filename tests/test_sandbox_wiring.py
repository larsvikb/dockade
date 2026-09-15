# SPDX-License-Identifier: Apache-2.0
"""Guards over the three things that point a sandbox at the gateway.

The wiring is deliberately spread across three files that cannot see each other, so
this is where they are held together:

  init-firewall.sh       grants the route, governed mode only
  run-claude-sandbox.sh  discovers the gateway, exempts it from the proxy, passes it on
  tier-setup.sh          writes the file the wrapper points `--mcp-config` at
  claude-wrapper.sh      puts the flags on every invocation

Every failure here is SILENT at runtime, which is why they are worth asserting rather
than leaving to review. A missing firewall grant looks like a gateway that is down. A
missing `NO_PROXY` entry looks like a governance refusal, because the egress proxy
answers "egress denied by policy" for a private address. A wrapper pointed at the wrong
binary looks like Claude Code is not installed. None of them fails loudly, and none is
visible from inside the file that caused it.

Text-parsed rather than executed, for the reason tests/test_topology.py is: these are
shell scripts that arm a firewall and launch containers, and running them is what
`make check-boundary` does on a host.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from _loader import load_protocol
from test_topology import _environment_of

ROOT = Path(__file__).resolve().parents[1]
FIREWALL = (ROOT / "sandbox-common" / "init-firewall.sh").read_text()
LAUNCHER = (ROOT / "run-claude-sandbox.sh").read_text()
TIER_SETUP = (ROOT / "claude-sandbox" / "tier-setup.sh").read_text()
WRAPPER = (ROOT / "claude-sandbox" / "claude-wrapper.sh").read_text()
DOCKERFILE = (ROOT / "claude-sandbox" / "Dockerfile").read_text()


class FirewallGrantTests(unittest.TestCase):
    """The route to the gateway, and the tier that must not have it."""

    def test_the_gateway_is_granted_as_a_slash_32_on_its_port(self):
        # Per service, never as a subnet allow — the rule init-firewall.sh states for
        # every data-plane service and the one this is the third instance of.
        self.assertRegex(
            FIREWALL,
            r'iptables -A OUTPUT -p tcp -d "\$TOOL_GATEWAY_IP" \\\n'
            r'\s*--dport "\$\{TOOL_GATEWAY_PORT:-8100\}" -j ACCEPT')

    def test_the_grant_is_governed_mode_only(self):
        # Tier 2 has no governed tool path and must not acquire one by sharing a
        # network. Asserted on the guard rather than on the launcher never regressing,
        # which is the same belt-and-braces the egress-proxy allow carries.
        guard = re.search(r'if \[\[ "\$MODE" = "governed" && "\$\{TOOL_GATEWAY_IP:-\}"',
                          FIREWALL)
        self.assertIsNotNone(
            guard, "the tool-gateway grant is not gated on governed mode")

    def test_the_grant_needs_an_address_to_have_been_passed(self):
        # An unset variable must not become an `iptables -d ""`, which would either
        # error or — worse — be interpreted as something wider.
        self.assertIn(r'"${TOOL_GATEWAY_IP:-}" =~ ^[0-9.]+$', FIREWALL)


class LauncherTests(unittest.TestCase):
    """Discovery, the proxy exemption, and what reaches the container."""

    def test_the_gateway_is_discovered_on_the_sandbox_network(self):
        self.assertIn('TOOL_GATEWAY_IP="$(sc_service_ip "$TOOL_GATEWAY_NAME" '
                      '"$SANDBOX_NET")"', LAUNCHER)

    def test_the_gateway_is_exempt_from_the_proxy(self):
        # THE ONE THAT FAILS MOST CONFUSINGLY. Claude Code's MCP transport honours the
        # proxy environment, and the egress proxy hard-blocks private ranges — so
        # without this the agent gets "egress denied by policy" for its own tool
        # gateway, an error naming the wrong component and a refusal governance never
        # made. Measured from inside a sandbox before the line existed.
        self.assertIn(
            'NO_PROXY_LIST="${NO_PROXY_LIST},${TOOL_GATEWAY_NAME},${TOOL_GATEWAY_IP}"',
            LAUNCHER)

    def test_both_cases_of_no_proxy_carry_the_same_list(self):
        # curl reads `http_proxy` in lower case only and NO_PROXY in either; the
        # pairing exists for that asymmetry, and a new entry added to one spelling
        # would be absent from whichever tools read the other.
        self.assertIn('-e "NO_PROXY=$NO_PROXY_LIST"', LAUNCHER)
        self.assertIn('-e "no_proxy=$NO_PROXY_LIST"', LAUNCHER)

    def test_the_address_and_port_reach_the_container(self):
        for var in ("TOOL_GATEWAY_IP", "TOOL_GATEWAY_PORT"):
            with self.subTest(variable=var):
                self.assertIn(f'-e "{var}=${var}"', LAUNCHER)

    def test_the_default_port_is_the_one_compose_deploys(self):
        # Two ends, no compiler. A drift here points the sandbox at a port nothing
        # serves, and the symptom is an MCP server that fails to connect.
        deployed = _environment_of("tool-gateway")["GATEWAY_AGENT_PORT"].strip("\"'")
        self.assertIn(f'TOOL_GATEWAY_PORT="${{TOOL_GATEWAY_PORT:-{deployed}}}"',
                      LAUNCHER)

    def test_the_default_name_is_the_container_compose_runs(self):
        self.assertIn('TOOL_GATEWAY_NAME="${TOOL_GATEWAY_NAME:-tool-gateway}"',
                      LAUNCHER)
        self.assertIn("container_name: tool-gateway",
                      (ROOT / "docker-compose.yml").read_text())

    def test_a_missing_gateway_is_reported_rather_than_fatal(self):
        # The sandbox still launches: `--strict-mcp-config` goes on unconditionally, so
        # no gateway means a provably empty tool surface rather than a failed start.
        self.assertIn("MCP surface is empty", LAUNCHER)


class GatewayConfigTests(unittest.TestCase):
    """The file the wrapper points `--mcp-config` at."""

    def config(self, ip: str = "172.30.0.11", port: str = "8100") -> dict:
        """The heredoc tier-setup.sh writes, rendered with its two variables.

        Extracted and substituted rather than executed: running tier-setup.sh means
        running a boot script as root. What is under test is the DOCUMENT, and it is
        parsed as JSON here so a trailing comma or an unquoted value fails loudly
        instead of at a session start on someone's machine."""
        body = re.search(r'install -o root -g root -m 0644 /dev/stdin '
                         r'"\$MCP_GATEWAY_CONFIG" <<EOF\n(.*?)\nEOF',
                         TIER_SETUP, re.S)
        self.assertIsNotNone(body, "tier-setup.sh no longer writes the gateway config")
        rendered = (body.group(1)
                    .replace("${TOOL_GATEWAY_IP}", ip)
                    .replace("${TOOL_GATEWAY_PORT:-8100}", port))
        return json.loads(rendered)

    def test_the_document_is_valid_json_with_one_server(self):
        self.assertEqual(list(self.config()["mcpServers"]), ["dockade"])

    def test_the_key_is_the_name_the_gateway_answers_to(self):
        # The agent sees `mcp__<key>__<server>__<tool>`, so a mismatch is invisible —
        # both sides keep working and only the name in a transcript is wrong.
        self.assertIn(load_protocol().SERVER_NAME, self.config()["mcpServers"])

    def test_the_url_is_the_path_the_gateway_serves(self):
        entry = self.config()["mcpServers"]["dockade"]
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["url"], "http://172.30.0.11:8100/mcp")

    def test_a_stale_entry_is_removed_before_it_can_be_reused(self):
        # Its ABSENCE is what tells the wrapper there is no gateway, so a file left by
        # an earlier run of the same container is a session pointed at an address that
        # may no longer answer.
        self.assertIn('rm -f "$MCP_GATEWAY_CONFIG"', TIER_SETUP)

    def test_it_is_written_outside_the_config_volume(self):
        # The volume persists across runs; /etc is image layer, so the file lives
        # exactly as long as the container that discovered the address.
        self.assertIn("MCP_GATEWAY_CONFIG=/etc/claude-code/", TIER_SETUP)

    def test_it_is_root_owned_so_the_agent_cannot_repoint_it(self):
        self.assertIn("install -o root -g root -m 0644", TIER_SETUP)


class WrapperTests(unittest.TestCase):
    """`claude`, with the flags attached."""

    def test_the_wrapper_shadows_the_real_binary_on_path(self):
        # It works only because the agent-writable dir is LAST on PATH. If that ever
        # flips, the wrapper stops being reached and nothing announces it.
        self.assertIn("COPY claude-sandbox/claude-wrapper.sh /usr/local/bin/claude",
                      DOCKERFILE)
        self.assertIn('ENV PATH="$PATH:/home/$USERNAME/.local/bin"', DOCKERFILE)

    def test_the_wrapper_execs_where_the_image_installs_claude(self):
        # Hard-coded in the wrapper, because a PATH search would find the wrapper
        # itself. Held equal to the image's user here.
        user = re.search(r"^ENV USERNAME=(\S+)", DOCKERFILE, re.M) or \
            re.search(r"^ARG USERNAME=(\S+)", DOCKERFILE, re.M)
        self.assertIsNotNone(user, "the Dockerfile no longer names its sandbox user")
        self.assertIn(f'REAL="/home/{user.group(1)}/.local/bin/claude"', WRAPPER)

    def test_strict_mode_is_unconditional(self):
        # Both branches. With nothing supplied it yields zero MCP servers, so the
        # gateway-absent case is a provably empty surface rather than whatever a
        # workspace .mcp.json or the config volume has accumulated.
        execs = re.findall(r"^\s*exec .*$", WRAPPER, re.M)
        self.assertEqual(len(execs), 2, "the wrapper's two paths changed shape")
        for line in execs:
            with self.subTest(line=line):
                self.assertIn("--strict-mcp-config", line)

    def test_the_config_flag_is_followed_by_an_option_not_by_the_arguments(self):
        # `--mcp-config` is variadic and greedy: `claude --mcp-config X mcp list`
        # consumes `mcp` and `list` as further config paths (NOTES.md). The option
        # immediately after it is what stops the greed before "$@" is swallowed.
        line = next(line for line in re.findall(r"^\s*exec .*$", WRAPPER, re.M)
                    if "--mcp-config" in line)
        self.assertRegex(line, r'--mcp-config "\$MCP_CONFIG" --strict-mcp-config "\$@"')

    def test_the_yolo_alias_resolves_through_the_wrapper(self):
        # An alias that named an absolute path would bypass the flags, which is the
        # whole reason the flags are not in the alias to begin with.
        bashrc = (ROOT / "claude-sandbox" / "dotfiles" / ".bashrc.tier").read_text()
        self.assertIn("alias claude-yolo='claude ", bashrc)


if __name__ == "__main__":
    unittest.main()
