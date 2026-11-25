# SPDX-License-Identifier: Apache-2.0
"""Guards over the three things that point a sandbox at the gateway.

The wiring is deliberately spread across three files that cannot see each other, so
this is where they are held together:

  init-firewall.sh       grants the route, governed mode only
  sandbox-lib.sh         discovers the gateway, exempts it from the proxy, passes it on
                         (sc_governed_egress, which run-claude-sandbox.sh calls)
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
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import ClassVar

from _loader import load_protocol, load_tool_gateway
from test_launcher_run_flags import launchers
from test_topology import _environment_of

ROOT = Path(__file__).resolve().parents[1]
FIREWALL = (ROOT / "sandbox-common" / "init-firewall.sh").read_text()
LAUNCHER = (ROOT / "run-claude-sandbox.sh").read_text()
LIB = (ROOT / "sandbox-lib.sh").read_text()
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
        # A tier with no governed egress (opencode's) has no governed tool path and
        # must not acquire one by sharing a network. Asserted on the guard rather than
        # on the launcher never regressing, which is the same belt-and-braces the
        # egress-proxy allow carries.
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
        self.assertIn('gateway_ip="$(sc_service_ip "$gateway_name" "$net")"', LIB)

    def test_the_gateway_is_exempt_from_the_proxy(self):
        # THE ONE THAT FAILS MOST CONFUSINGLY. Claude Code's MCP transport honours the
        # proxy environment, and the egress proxy hard-blocks private ranges — so
        # without this the agent gets "egress denied by policy" for its own tool
        # gateway, an error naming the wrong component and a refusal governance never
        # made. Measured from inside a sandbox before the line existed.
        self.assertIn(
            'no_proxy_list="${no_proxy_list},${gateway_name},${gateway_ip}"', LIB)

    def test_both_cases_of_no_proxy_carry_the_same_list(self):
        # curl reads `http_proxy` in lower case only and NO_PROXY in either; the
        # pairing exists for that asymmetry, and a new entry added to one spelling
        # would be absent from whichever tools read the other.
        self.assertIn('-e "NO_PROXY=$no_proxy_list"', LIB)
        self.assertIn('-e "no_proxy=$no_proxy_list"', LIB)

    def test_the_address_and_port_reach_the_container(self):
        for var, local in (("TOOL_GATEWAY_IP", "gateway_ip"),
                           ("TOOL_GATEWAY_PORT", "gateway_port")):
            with self.subTest(variable=var):
                self.assertIn(f'-e "{var}=${local}"', LIB)

    def test_the_default_port_is_the_one_compose_deploys(self):
        # Two ends, no compiler. A drift here points the sandbox at a port nothing
        # serves, and the symptom is an MCP server that fails to connect.
        deployed = _environment_of("tool-gateway")["GATEWAY_AGENT_PORT"].strip("\"'")
        self.assertIn(f'gateway_port="${{TOOL_GATEWAY_PORT:-{deployed}}}"', LIB)

    def test_the_default_name_is_the_container_compose_runs(self):
        self.assertIn('gateway_name="${TOOL_GATEWAY_NAME:-tool-gateway}"', LIB)
        self.assertIn("container_name: tool-gateway",
                      (ROOT / "docker-compose.yml").read_text())

    def test_a_missing_gateway_is_reported_rather_than_fatal(self):
        # The sandbox still launches: `--strict-mcp-config` goes on unconditionally, so
        # no gateway means a provably empty tool surface rather than a failed start.
        # That is Claude Code's flag, so Claude's launcher says it, not the library.
        self.assertIn("MCP surface is empty", LAUNCHER)
        self.assertNotIn("MCP surface is empty", LIB)

    def test_the_launcher_wires_governed_egress_after_choosing_its_network(self):
        # sc_ensure_network decides whether the network may be the standalone bridge;
        # sc_governed_egress then asks what is on it, so the order matters.
        ensure = LAUNCHER.index('sc_ensure_network "$SANDBOX_NET" true')
        self.assertLess(ensure, LAUNCHER.index('sc_governed_egress "$SANDBOX_NET"'))

    def test_the_launcher_passes_the_proxy_env_to_docker_run(self):
        # The silent failure: a launcher that computes the env and drops it launches
        # with no proxy variables and no EGRESS_PROXY_IP, which the firewall reads as
        # the standalone posture — direct egress, nothing audited.
        run = LAUNCHER[LAUNCHER.index('docker run "${RUN_MODE_ARGS[@]}"'):]
        self.assertIn('${SC_PROXY_ENV_ARGS[@]+"${SC_PROXY_ENV_ARGS[@]}"}', run)

    def test_the_launcher_prints_both_descriptions(self):
        self.assertIn('echo "Egress:    $SC_EGRESS_DESC"', LAUNCHER)
        self.assertIn('echo "Tools:     $SC_TOOLS_DESC; MCP surface is empty"', LAUNCHER)
        self.assertIn('echo "Tools:     $SC_TOOLS_DESC"', LAUNCHER)


class WorkspaceMountWiringTests(unittest.TestCase):
    """Each launcher mounts, starts in and prints the path ``sc_workspace_mount`` names.

    tests/test_launcher_lib.py runs the function and a launcher's refusal; a launcher
    that computed the path and still mounted at ``/workspace`` would pass both, and
    give every project the one memory folder again."""

    MOUNT = '    -v "$WORKSPACE":"$SC_WORKSPACE_MOUNT" \\\n    -w "$SC_WORKSPACE_MOUNT" \\\n'

    def test_each_launcher_mounts_and_starts_in_the_named_path(self):
        for path in launchers(ROOT):
            text = path.read_text()
            with self.subTest(launcher=path.name):
                guard = text.index('sc_guard_workspace "$WORKSPACE"\n'
                                   'WORKSPACE="$SC_WORKSPACE"\n'
                                   'sc_workspace_mount "$WORKSPACE"\n')
                run = text.index("\ndocker run ")
                self.assertLess(guard, run)
                self.assertEqual(text.count(self.MOUNT), 1)
                self.assertGreater(text.index(self.MOUNT), run)
                self.assertEqual(re.findall(r"^\s*-w\b.*", text, re.M),
                                 ['    -w "$SC_WORKSPACE_MOUNT" \\'])
                self.assertIn('echo "Workspace: $WORKSPACE -> $SC_WORKSPACE_MOUNT"', text)
                self.assertNotRegex(text, r":/workspace\b")


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
                    .replace("${TOOL_GATEWAY_PORT:-8100}", port)
                    .replace("${SANDBOX_ID}", self.SANDBOX_ID))
        return json.loads(rendered)

    SANDBOX_ID = "0123456789abcdef" * 2

    def mint(self) -> str:
        """The line that mints the sandbox id, run alone. Only the pipeline runs, not
        the boot script around it."""
        line = re.search(r'^\s*SANDBOX_ID="\$\((.*)\)"$', TIER_SETUP, re.M)
        self.assertIsNotNone(line, "tier-setup.sh no longer mints a sandbox id")
        bash = shutil.which("bash")
        self.assertIsNotNone(bash)
        return subprocess.run(  # noqa: S603 (absolute path from shutil.which; the line is this repo's)
            [bash, "-c", line.group(1)], capture_output=True, text=True,
            check=True).stdout

    def test_the_document_is_valid_json_with_one_server(self):
        self.assertEqual(list(self.config()["mcpServers"]), ["gateway"])

    def test_the_key_is_the_name_the_gateway_answers_to(self):
        # The agent sees `mcp__<key>__<server>__<tool>`, so a mismatch is invisible —
        # both sides keep working and only the name in a transcript is wrong.
        self.assertIn(load_protocol().SERVER_NAME, self.config()["mcpServers"])

    def test_the_url_is_the_path_the_gateway_serves(self):
        entry = self.config()["mcpServers"]["gateway"]
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["url"], "http://172.30.0.11:8100/mcp")

    def test_the_sandbox_id_is_sent_literally_in_the_header_the_gateway_reads(self):
        # Literal: the id is minted in the root phase, in no environment the agent's
        # Claude Code inherits, and expansion can blank a credential-looking value
        # (NOTES.md). Either would send every sandbox as one with no id.
        entry = self.config()["mcpServers"]["gateway"]
        self.assertEqual(entry["headers"], {"X-Dockade-Sandbox": self.SANDBOX_ID})
        self.assertEqual(load_tool_gateway({"GATEWAY_AGENT_BIND": "172.30.0.11"})
                         .SANDBOX_HEADER, "X-Dockade-Sandbox".lower())

    def test_the_id_is_minted_in_the_shape_the_gateway_forwards(self):
        gateway = load_tool_gateway({"GATEWAY_AGENT_BIND": "172.30.0.11"})
        first, second = self.mint(), self.mint()
        self.assertRegex(first, r"\A[0-9a-f]{32}\Z")
        self.assertTrue(gateway._SANDBOX_RE.fullmatch(first))
        self.assertNotEqual(first, second)

    def test_the_id_is_minted_on_every_boot_beside_the_file_it_is_written_to(self):
        # In the root phase, after the stale file is removed and before the new one is
        # written: a fresh id per boot, never one carried over in the config volume.
        mint = TIER_SETUP.index('SANDBOX_ID="$(')
        self.assertLess(TIER_SETUP.index('rm -f "$MCP_GATEWAY_CONFIG"'), mint)
        self.assertLess(mint, TIER_SETUP.index('"$MCP_GATEWAY_CONFIG" <<EOF'))
        self.assertLess(mint, TIER_SETUP.index('exec gosu "$USERNAME" "$0" "$@"'))
        self.assertEqual(TIER_SETUP.count("SANDBOX_ID="), 1)

    def test_the_boot_prints_the_prefix_a_card_shows(self):
        # The prefix and never more: the boot log is not where the whole id belongs.
        printed = [m for line in TIER_SETUP.splitlines()
                   if re.match(r"\s*(echo|printf)\b", line)
                   for m in re.findall(r"\$(?:\{SANDBOX_ID\b[^}]*\}|SANDBOX_ID\b)",
                                       line)]
        self.assertEqual(printed, ["${SANDBOX_ID:0:8}"])

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

    # The flags it puts on, and their order, are run in tests/test_launch_settings.py.

    def test_the_yolo_alias_resolves_through_the_wrapper(self):
        # An alias that named an absolute path would bypass the flags, which is the
        # whole reason the flags are not in the alias to begin with.
        bashrc = (ROOT / "claude-sandbox" / "dotfiles" / ".bashrc.tier").read_text()
        self.assertIn("alias claude-yolo='claude ", bashrc)


class AntigravityWiringTests(unittest.TestCase):
    """agy's tier, where the files cannot see each other: the Dockerfile, the hook and
    the wrapper. The hook and the wrapper are run in tests/test_antigravity_setup.py;
    these hold what only the text says."""

    DOCKERFILE = (ROOT / "antigravity-sandbox" / "Dockerfile").read_text()
    HOOK = (ROOT / "antigravity-sandbox" / "tier-setup.sh").read_text()
    WRAPPER = (ROOT / "antigravity-sandbox" / "agy-wrapper.sh").read_text()

    def test_the_header_is_the_one_the_gateway_reads(self):
        names = re.findall(r'"(X-[A-Za-z-]+)"', self.HOOK)
        self.assertEqual(names, ["X-Dockade-Sandbox"])
        self.assertEqual(load_tool_gateway({"GATEWAY_AGENT_BIND": "172.30.0.11"})
                         .SANDBOX_HEADER, names[0].lower())

    def test_the_entry_is_keyed_by_the_gateways_name(self):
        self.assertIn(f"{{mcpServers: {{{load_protocol().SERVER_NAME}: ", self.HOOK)

    def test_the_real_binary_is_off_path_where_the_wrapper_execs_it(self):
        real = re.search(r'^REAL="(/[^"]+)"$', self.WRAPPER, re.M)
        self.assertIsNotNone(real, "agy-wrapper.sh no longer names the real binary")
        self.assertIn(f"\"/tmp/agy/$AGY_TARBALL_BINARY\" {real.group(1)} ", self.DOCKERFILE)
        directory = real.group(1).rsplit("/", 1)[0]
        path = re.search(r'^ENV PATH="([^"]+)"$', self.DOCKERFILE, re.M)
        self.assertIsNotNone(path)
        self.assertNotIn(directory, path.group(1))
        self.assertNotIn(directory, ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin",
                                     "/usr/bin", "/sbin", "/bin"))
        self.assertIn(f"install -d -m 0755 {directory} ", self.DOCKERFILE)
        self.assertIn("COPY antigravity-sandbox/agy-wrapper.sh /usr/local/bin/agy\n",
                      self.DOCKERFILE)

    def test_the_state_links_lead_into_the_config_volume(self):
        # The login and settings dirs are links into /config; ~/.gemini/config, which
        # carries the boot's sandbox id, is not, so siblings never share it.
        links = re.findall(r"ln -s (\S+) (\S+)", self.DOCKERFILE)
        self.assertEqual(links, [
            ("/config/antigravity-cli", "/home/$USERNAME/.gemini/antigravity-cli"),
            ("/config/antigravity", "/home/$USERNAME/.config/antigravity")])
        self.assertIn("ENV SANDBOX_CONFIG_DIR=/config\n", self.DOCKERFILE)
        self.assertIn('MCP_CONFIG="$HOME/.gemini/config/mcp_config.json"', self.HOOK)

    def test_updates_are_off_by_the_value_measured_to_work(self):
        self.assertIn("ENV AGY_CLI_DISABLE_AUTO_UPDATE=true\n", self.DOCKERFILE)

    def test_the_build_runs_agy_so_a_failure_fails_it_with_updates_off(self):
        # Assigned, not substituted inside echo, whose status hides a failed binary;
        # the switch on the command, since the ENV comes after this layer.
        real = re.search(r'^REAL="(/[^"]+)"$', self.WRAPPER, re.M).group(1)
        runs = [line.strip() for line in self.DOCKERFILE.splitlines()
                if f"{real} --version" in line]
        self.assertEqual(runs, [
            f'&& v="$(AGY_CLI_DISABLE_AUTO_UPDATE=true {real} --version)" \\'])
        self.assertLess(self.DOCKERFILE.index(runs[0]),
                        self.DOCKERFILE.index("ENV AGY_CLI_DISABLE_AUTO_UPDATE=true"))
        self.assertIn('    && echo "agy $version: $v"\n', self.DOCKERFILE)

    def test_the_download_is_checked_against_the_manifest_before_it_is_unpacked(self):
        check = self.DOCKERFILE.index('&& echo "${sha}  /tmp/agy.tar.gz" | sha512sum -c - \\')
        self.assertLess(self.DOCKERFILE.index('sha="$(jq -er \'.sha512\''), check)
        self.assertLess(check, self.DOCKERFILE.index("&& tar -xzf /tmp/agy.tar.gz"))

    def test_the_binary_dir_stays_root_owned(self):
        # A sandbox-owned dir would let the agent, or agy's updater, replace the binary.
        directory = re.search(r'^REAL="(/[^"]+)/[^/"]+"$', self.WRAPPER, re.M).group(1)
        chowns = [line for line in self.DOCKERFILE.splitlines() if "chown" in line]
        self.assertTrue(chowns)
        for line in chowns:
            with self.subTest(line=line):
                self.assertNotIn(directory, line)
                self.assertNotIn("/opt", line)
        self.assertIn(f"install -d -m 0755 {directory} ", self.DOCKERFILE)
        # The chown that follows its creation names its targets, none of them it.
        chown = re.search(r"chown -R \$USERNAME:\$USERNAME ([^\n]+)", self.DOCKERFILE)
        self.assertEqual(chown.group(1), "/workspace /config /home/$USERNAME")

    def test_the_shared_settings_are_renamed_into_place(self):
        # The volume is every sibling's: a write in place could be read half-done.
        lines = [ln.strip() for ln in self.HOOK.splitlines()
                 if "settings" in ln and not ln.lstrip().startswith("#")]
        self.assertEqual(lines, [
            'settings="$CONFIG_DIR/antigravity-cli/settings.json"',
            'install -m 0600 "$IMAGE_DIR/settings.json" "$settings.tmp.$$"',
            'mv -f "$settings.tmp.$$" "$settings"'])


class JdkVariantWiringTests(unittest.TestCase):
    """The opt-in JDK across the Dockerfile, the boot hook and the launcher.

    No automated build runs the variant (neither `make verify-build` nor CI), so an
    edit that breaks only it shows at its next manual build; these hold its shape."""

    GUARD = 'RUN if [ -z "$JDK" ]; then exit 0; fi \\\n'
    VERSION_FILE = "/etc/claude-code/jdk-version"
    BUILD = ('sc_build_image "$IMAGE_NAME" "$IMAGE_DIR" "$CACHE_FLAG" '
             '${SC_BUILD_ARGS[@]+"${SC_BUILD_ARGS[@]}"}')

    def run_text(self) -> str:
        start = DOCKERFILE.index(self.GUARD)
        return DOCKERFILE[start:DOCKERFILE.index("\n\n", start)]

    def test_the_layer_sits_below_the_toolchain_and_above_the_copies(self):
        # Above the pipx RUN it would rebuild the toolchain per variant; below the
        # COPYs every script edit would refetch the JDK.
        order = [DOCKERFILE.index("RUN pipx install ruff"),
                 DOCKERFILE.index("\nUSER root\n"),
                 DOCKERFILE.index('\nARG JDK=""\n'),
                 DOCKERFILE.index(self.GUARD),
                 DOCKERFILE.index("COPY sandbox-common/dotfiles/")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(re.findall(r"^USER .*", DOCKERFILE, re.M)[-1], "USER root")

    def test_an_empty_arg_makes_the_layer_do_nothing(self):
        self.assertEqual(len(re.findall(r"^ARG JDK=", DOCKERFILE, re.M)), 1)
        self.assertEqual(DOCKERFILE.count('if [ -z "$JDK" ]'), 1)

    def test_the_jar_is_checked_before_it_is_installed(self):
        run = self.run_text()
        fetched = run.index("-o /tmp/junit.jar")
        # Adjacent lines, so nothing between them (an `|| true`) can make the digest
        # advisory.
        checked = run.index('echo "${JUNIT_CONSOLE_SHA256}  /tmp/junit.jar" | sha256sum -c - \\\n'
                            '    && install -D -m 0644 /tmp/junit.jar "$jar" \\\n')
        self.assertLess(fetched, checked)
        self.assertRegex(DOCKERFILE, r"(?m)^ARG JUNIT_CONSOLE_SHA256=[0-9a-f]{64}$")

    def test_the_layer_keeps_the_apt_hygiene_and_tests_itself(self):
        run = self.run_text()
        man = run.index("mkdir -p /usr/share/man/man1")
        update = run.index("apt-get update")
        install = run.index('apt-get install -y --no-install-recommends "openjdk-${JDK}-jdk-headless"')
        clean = run.index("apt-get clean && rm -rf /var/lib/apt/lists/*")
        self.assertEqual([man, update, install, clean], sorted([man, update, install, clean]))
        for step in ("java -version", "javac -d", "--fail-if-no-tests", "--scan-class-path",
                     "javac -version", '"$jar" --version | sed -n \'1{', "mkdir -p /etc/claude-code \\",
                     f"> {self.VERSION_FILE} \\", "rm /tmp/junit.jar \\", 'rm -rf "$t" \\'):
            with self.subTest(step=step):
                self.assertIn(step, run)

    def test_the_boot_note_reads_the_file_the_build_writes_as_root(self):
        root_phase, user_phase = TIER_SETUP.split(TierHookPrivilegeTests.REEXEC, 1)
        call = f"jdk_note {self.VERSION_FILE}"
        self.assertEqual(root_phase.count(call), 1)
        self.assertNotIn("jdk_note", user_phase)
        joined = root_phase.index(call)
        self.assertLess(root_phase.index('plugin_note="$(launch_note "$launch")"'), joined)
        self.assertLess(joined, root_phase.index('"$MANAGED_NOTE" <<<"$note"'))

    def test_the_launcher_uses_the_variant_tag_for_every_build_and_the_run(self):
        variant = LAUNCHER.index('sc_jdk_variant "${SANDBOX_IMAGE:-claude-sandbox}"')
        self.assertLess(variant, LAUNCHER.index('if [[ "$BUILD_ONLY" == "true" ]]'))
        self.assertEqual(re.findall(r"^IMAGE_NAME=.*", LAUNCHER, re.M),
                         ['IMAGE_NAME="$SC_IMAGE"'])
        builds = [line.strip() for line in LAUNCHER.splitlines()
                  if line.strip().startswith("sc_build_image")]
        self.assertEqual(builds, [self.BUILD, self.BUILD])
        self.assertIn('! docker image inspect "$IMAGE_NAME"', LAUNCHER)
        self.assertIn('"$IMAGE_NAME" ${RUN_CMD_ARGS[@]+"${RUN_CMD_ARGS[@]}"}', LAUNCHER)
        self.assertIn('echo "Image:     $IMAGE_NAME"', LAUNCHER)

    def test_opencode_never_takes_the_variant(self):
        opencode = (ROOT / "run-opencode-sandbox.sh").read_text()
        self.assertNotIn("sc_jdk_variant", opencode)
        self.assertNotIn("SC_BUILD_ARGS", opencode)


class TierHookPrivilegeTests(unittest.TestCase):
    """Root touches the image layer; the sandbox user touches the volume.

    The config volume is agent-owned and shared by every concurrent sandbox of a
    tier, so a sibling's agent can be rearranging it while a boot runs. A root
    process writing there follows whatever symlink it finds — `install -d` chowns the
    target, `>>` appends through the link, `jq file` reads through it — and a
    check-then-act guard only narrows the window. Each hook therefore re-execs itself
    as the sandbox user before its first volume write. Read from the source, the way
    the other tests in this file read the heredoc: running the hooks means a root
    container."""

    REEXEC = 'exec gosu "$USERNAME" "$0" "$@"'
    HOOKS: ClassVar = {
        "claude-sandbox": TIER_SETUP,
        "opencode-sandbox": (ROOT / "opencode-sandbox" / "tier-setup.sh").read_text(),
        "antigravity-sandbox": (ROOT / "antigravity-sandbox" / "tier-setup.sh").read_text(),
    }

    def test_every_hook_hands_itself_to_the_sandbox_user(self):
        for tier, src in self.HOOKS.items():
            with self.subTest(tier=tier):
                self.assertIn(self.REEXEC, src)

    def test_no_hook_writes_agent_owned_files_as_root(self):
        # `install -o sandbox` is the spelling of "root, writing a file for the
        # agent" — the exact act the re-exec exists to remove. Its absence is what
        # keeps the split honest when the next materialized file is added.
        for tier, src in self.HOOKS.items():
            with self.subTest(tier=tier):
                self.assertNotIn('install -o "$USERNAME"', src)
                self.assertNotIn('install -d -o "$USERNAME"', src)
                self.assertNotIn('chown -h "$USERNAME', src)

    def test_root_owned_artefacts_live_only_in_the_image_layer(self):
        # Claude's gateway pointer, launch settings and managed note are what root
        # writes, and they must be written BEFORE the re-exec and OUTSIDE the volume.
        root_phase, user_phase = TIER_SETUP.split(self.REEXEC, 1)
        self.assertIn("MCP_GATEWAY_CONFIG=/etc/claude-code/", root_phase)
        self.assertIn("LAUNCH_SETTINGS=/etc/claude-code/launch-settings.json", root_phase)
        self.assertIn("MANAGED_NOTE=/etc/claude-code/", root_phase)
        self.assertIn("install -o root -g root -m 0644", root_phase)
        self.assertNotIn("install -o root", user_phase)
        self.assertNotIn("$CONFIG_DIR/", root_phase.split("CONFIG_DIR=", 1)[1])

    @staticmethod
    def _code(src: str) -> list[str]:
        return [line for line in src.splitlines() if not line.lstrip().startswith("#")]

    def test_root_opens_no_marketplace_and_renders_only_as_the_user(self):
        # A sibling whose workspace holds this launch's marketplaces checkout can
        # write it, so root reading a manifest is a root read through the sibling.
        root_phase = TIER_SETUP.split(self.REEXEC, 1)[0]
        self.assertNotIn("marketplace.json", root_phase)
        self.assertNotIn("render_launch_settings", root_phase)
        calls = [line for line in self._code(root_phase) if "--render-launch-settings" in line]
        self.assertEqual(len(calls), 1, calls)
        self.assertIn('gosu "$USERNAME" "$0" --render-launch-settings', calls[0])

    def test_the_launch_settings_are_cleared_then_installed_root_owned(self):
        root_phase = TIER_SETUP.split(self.REEXEC, 1)[0]
        cleared = root_phase.index('rm -f "$LAUNCH_SETTINGS"')
        rendered = root_phase.index('gosu "$USERNAME" "$0" --render-launch-settings')
        written = root_phase.index(
            'install -o root -g root -m 0644 /dev/stdin "$LAUNCH_SETTINGS"')
        self.assertLess(cleared, rendered)
        self.assertLess(rendered, written)

    def test_the_user_phase_writes_no_launch_state_into_the_volume(self):
        # settings.json in the volume is the image's template and nothing more, and
        # CLAUDE.md the template with the operator's per-machine text after it; the
        # launch's plugins and their note are root's, in /etc.
        user_phase = TIER_SETUP.split(self.REEXEC, 1)[1]
        code = self._code(user_phase)
        self.assertEqual(
            [line for line in code if "settings.json" in line and "launch-settings" not in line],
            ['install -m 0644 /etc/claude-code/user-settings.json "$CONFIG_DIR/settings.json"'])
        self.assertEqual(
            [line for line in code if "CLAUDE.md" in line],
            ['install -m 0644 /etc/claude-code/CLAUDE.md.template "$CONFIG_DIR/CLAUDE.md"',
             'append_operator_instructions "/operator/CLAUDE.md" "$CONFIG_DIR/CLAUDE.md"'])
        self.assertEqual([line for line in code if "launch-settings.json" in line],
                         ["plugin_advisory /etc/claude-code/launch-settings.json \\"])
        self.assertNotIn(">>", "\n".join(code))


class OperatorInstructionsWiringTests(unittest.TestCase):
    """The operator's instructions file, from each launcher's mount to its tier's boot.

    tests/test_launcher_lib.py runs ``sc_operator_config`` and
    tests/test_operator_instructions.py the append; a launcher that dropped the flags,
    named a file its hook does not read, or a hook that wrote the file anywhere else,
    would pass both."""

    HELPER = "/usr/local/lib/dockade/operator-instructions.sh"
    GUARD = ('sc_guard_workspace "$WORKSPACE"\nWORKSPACE="$SC_WORKSPACE"\n'
             'sc_workspace_mount "$WORKSPACE"\n')
    CALL = re.compile(r'^sc_operator_config (\S+) "\$WORKSPACE" (\S+)$', re.M)

    def tiers(self) -> list[str]:
        found = re.findall(r"^_SC_TIERS=\((.*)\)$", LIB, re.M)
        self.assertEqual(len(found), 1, found)
        return found[0].split()

    def wiring(self):
        """(launcher name, its text, tier, file, image dir) for each launcher."""
        for path in launchers(ROOT):
            text = path.read_text()
            calls = self.CALL.findall(text)
            self.assertEqual(len(calls), 1, f"{path.name}: {calls}")
            image_dir = re.findall(r'^IMAGE_DIR="([^"]+)"$', text, re.M)
            self.assertEqual(len(image_dir), 1, path.name)
            yield path.name, text, calls[0][0], calls[0][1], image_dir[0]

    def test_each_launcher_names_a_known_tier_once_and_tiers_are_not_shared(self):
        seen = [tier for _, _, tier, _, _ in self.wiring()]
        self.assertTrue(seen)
        self.assertEqual(sorted(seen), sorted(set(seen)))
        for tier in seen:
            with self.subTest(tier=tier):
                self.assertIn(tier, self.tiers())

    def test_each_launcher_passes_its_workspace_after_the_guard_set_it(self):
        for name, text, _, _, _ in self.wiring():
            with self.subTest(launcher=name):
                guard = text.index(self.GUARD)
                call = self.CALL.search(text).start()
                self.assertLess(guard, call)
                # Nothing reassigns WORKSPACE between the guard and the call.
                self.assertNotRegex(text[guard + len(self.GUARD):call], r"(?m)^\s*WORKSPACE=")
                self.assertLess(call, text.index("\ndocker run "))

    def test_each_launcher_mounts_the_flags_and_prints_the_custom_line(self):
        for name, text, _, _, _ in self.wiring():
            with self.subTest(launcher=name):
                run = text[text.index("\ndocker run "):]
                self.assertEqual(text.count("SC_OPERATOR_ARGS"), 2)
                self.assertIn('    ${SC_OPERATOR_ARGS[@]+"${SC_OPERATOR_ARGS[@]}"} \\\n', run)
                self.assertIn('[[ -z "$SC_CUSTOM_DESC" ]] || echo "Custom:    $SC_CUSTOM_DESC"\n',
                              text)

    def test_the_mount_is_at_operator_never_where_a_cli_loads_on_its_own(self):
        # Claude Code loads a CLAUDE.md from /etc/claude-code and from its config
        # directory, opencode from its config directory: a file mounted there would
        # load once as mounted and again as appended.
        self.assertEqual(re.findall(r"SC_OPERATOR_ARGS\+=.*", LIB),
                         ['SC_OPERATOR_ARGS+=(-v "$real":/operator/"$name":ro)'])
        for path in [*launchers(ROOT), ROOT / "sandbox-lib.sh"]:
            for target in re.findall(r'-v "[^"]*":("?/[^: ]*)', path.read_text()):
                with self.subTest(file=path.name, target=target):
                    self.assertFalse(target.startswith(("/etc/", "/config/", "/home/")), target)

    def test_each_dockerfile_copies_the_helper_where_its_hook_sources_it(self):
        for _, _, _, _, image_dir in self.wiring():
            with self.subTest(image=image_dir):
                dockerfile = (ROOT / image_dir / "Dockerfile").read_text()
                self.assertEqual(dockerfile.count(
                    f"COPY sandbox-common/operator-instructions.sh {self.HELPER}\n"), 1)
                self.assertIn(f"               {self.HELPER} \\\n", dockerfile)

    def test_each_hook_appends_the_launchers_file_onto_its_install_and_nothing_else(self):
        for _, _, _, file, image_dir in self.wiring():
            with self.subTest(image=image_dir):
                hook = (ROOT / image_dir / "tier-setup.sh").read_text()
                root_phase, user_phase = hook.split(TierHookPrivilegeTests.REEXEC, 1)
                self.assertNotIn("/operator/", root_phase)
                self.assertNotIn("append_operator_instructions", root_phase)
                code = TierHookPrivilegeTests._code(user_phase)
                named = [line for line in code if file in line]
                self.assertEqual(len(named), 2, named)
                # From the image (/etc, or agy's $IMAGE_DIR, which is /etc/antigravity)
                # into the config volume or, for agy, the container's own home.
                install = re.fullmatch(
                    r'install -m 0644 ("?(?:/etc/|\$IMAGE_DIR/)\S+\.template"?) '
                    r'("\$(?:CONFIG_DIR|HOME)/\S+")', named[0])
                self.assertIsNotNone(install, named[0])
                self.assertTrue(install.group(2).endswith(f'/{file}"'), named[0])
                self.assertEqual(named[1],
                                 f"append_operator_instructions \"/operator/{file}\" {install.group(2)}")
                sources = [line for line in code if self.HELPER in line]
                self.assertEqual(sources, [f"source {self.HELPER}"])
                self.assertLess(code.index(sources[0]), code.index(named[1]))
                self.assertEqual([line for line in code if "/operator/" in line], [named[1]])
                self.assertNotIn(">>", "\n".join(code))


if __name__ == "__main__":
    unittest.main()
