# SPDX-License-Identifier: Apache-2.0
"""agy's boot hook and its wrapper, run.

``antigravity-sandbox/tier-setup.sh`` needs no root: as root it only re-execs as the
sandbox user, so its whole effect is the user phase, which runs here as the test's own
user with HOME and the config volume in a temp dir and the image's /etc/antigravity
pointed at a copy of the repo's templates. ``agy-wrapper.sh`` runs with its real binary
replaced by a stub that prints what it was given. The text both are wired by, in the
Dockerfile, is held in tests/test_sandbox_wiring.py.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from _loader import load_task_registry

ROOT = Path(__file__).resolve().parents[1]
TIER = ROOT / "antigravity-sandbox"
HOOK = (TIER / "tier-setup.sh").read_text()
WRAPPER = (TIER / "agy-wrapper.sh").read_text()
IMAGE_DIR = "IMAGE_DIR=/etc/antigravity\n"
_HELPER_PATH = "/usr/local/lib/dockade/operator-instructions.sh"
_OPERATOR_PATH = "/operator/AGENTS.md"

_BASH = shutil.which("bash")
_SH = shutil.which("sh")
_JQ = shutil.which("jq")
# Missing tools SKIP on a dev machine and FAIL under DOCKADE_REQUIRE_TOOLS, which CI
# sets — as in tests/test_launcher_lib.py.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))
_ROOT_USER = os.geteuid() == 0


class _Boot(unittest.TestCase):
    """A temp HOME, config volume and image dir, and a way to boot the hook in them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.config = self.tmp / "config"
        self.etc = self.tmp / "etc"
        for d in (self.home, self.config, self.etc):
            d.mkdir()
        shutil.copy(TIER / "settings.json", self.etc / "settings.json")
        shutil.copy(TIER / "user-AGENTS.md", self.etc / "AGENTS.md.template")
        self.assertEqual(HOOK.count(IMAGE_DIR), 1, "the hook no longer names its image dir")
        # The operator-instructions helper from the repo, where the image has it in
        # /usr/local/lib, and the operator's file at a temp path for /operator.
        self.operator = self.tmp / "operator" / "AGENTS.md"
        self.operator.parent.mkdir()
        hook = HOOK.replace(IMAGE_DIR, f"IMAGE_DIR={self.etc}\n")
        for baked, here in ((_HELPER_PATH, str(ROOT / "sandbox-common" / "operator-instructions.sh")),
                            (_OPERATOR_PATH, str(self.operator))):
            self.assertEqual(hook.count(baked), 1, f"the hook no longer names {baked} once")
            hook = hook.replace(baked, here)
        self.hook = self.tmp / "tier-setup.sh"
        self.hook.write_text(hook)

    def boot(self, **env_over: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("TOOL_GATEWAY_")}
        env.update(HOME=str(self.home), SANDBOX_CONFIG_DIR=str(self.config), **env_over)
        proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_BASH, str(self.hook)], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    @property
    def mcp_config(self) -> Path:
        return self.home / ".gemini" / "config" / "mcp_config.json"

    def entry(self) -> dict:
        return json.loads(self.mcp_config.read_text())["mcpServers"]["gateway"]


@unittest.skipIf(_ROOT_USER, "as root the hook hands itself to the sandbox user")
@unittest.skipUnless((_BASH and _JQ) or _STRICT, "bash or jq is not installed")
class HookTests(_Boot):

    def test_the_volume_halves_are_made_as_private_directories(self):
        self.boot()
        for name in ("antigravity-cli", "antigravity"):
            with self.subTest(dir=name):
                path = self.config / name
                self.assertTrue(path.is_dir() and not path.is_symlink())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_a_link_or_a_file_in_their_place_is_replaced_and_a_login_is_kept(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.config / "antigravity-cli").symlink_to(elsewhere)
        (self.config / "antigravity").write_text("stray")
        self.boot()
        for name in ("antigravity-cli", "antigravity"):
            with self.subTest(dir=name):
                self.assertTrue((self.config / name).is_dir())
                self.assertFalse((self.config / name).is_symlink())
        self.assertEqual(list(elsewhere.iterdir()), [])
        token = self.config / "antigravity-cli" / "antigravity-oauth-token"
        token.write_text("{}")
        self.boot()
        self.assertEqual(token.read_text(), "{}")

    def test_the_settings_and_the_instructions_are_the_images_every_boot(self):
        self.boot()
        settings = self.config / "antigravity-cli" / "settings.json"
        rules = self.home / ".gemini" / "AGENTS.md"
        settings.write_text("{}")
        rules.write_text("edited in a session")
        self.boot()
        self.assertEqual(settings.read_bytes(), (TIER / "settings.json").read_bytes())
        self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in settings.parent.iterdir()),
                         ["settings.json"])
        self.assertEqual(rules.read_bytes(), (TIER / "user-AGENTS.md").read_bytes())
        self.assertEqual(stat.S_IMODE(rules.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(rules.parent.stat().st_mode), 0o700)

    def test_the_operators_instructions_follow_the_images_every_boot(self):
        # Once per boot, onto the freshly installed template, never onto the last
        # boot's file: the helper's own output is tests/test_operator_instructions.py's.
        self.operator.write_text("- Answer in British English.\n")
        rules = self.home / ".gemini" / "AGENTS.md"
        baked = (TIER / "user-AGENTS.md").read_text()
        for _ in range(2):
            proc = self.boot()
            self.assertNotIn("WARNING", proc.stderr)
            text = rules.read_text()
            self.assertTrue(text.startswith(baked), text[:200])
            self.assertEqual(text.count("# The operator's own instructions"), 1)
            self.assertTrue(text.endswith("\n\n- Answer in British English.\n"), text[-200:])

    def test_the_gateway_entry_is_written_only_when_a_gateway_was_found(self):
        proc = self.boot()
        self.assertFalse(self.mcp_config.exists())
        self.assertEqual(proc.stdout, "")
        for bad in ("", "gateway", "gw-172.30.0.11", "172.30.0.11; x", "::1"):
            with self.subTest(ip=bad):
                self.boot(TOOL_GATEWAY_IP=bad)
                self.assertFalse(self.mcp_config.exists())

    def test_the_entry_names_the_gateway_by_address_and_port(self):
        self.boot(TOOL_GATEWAY_IP="172.30.0.11")
        self.assertEqual(self.entry()["url"], "http://172.30.0.11:8100/mcp")
        self.assertEqual(stat.S_IMODE(self.mcp_config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.mcp_config.parent.stat().st_mode), 0o700)
        self.boot(TOOL_GATEWAY_IP="172.30.0.11", TOOL_GATEWAY_PORT="9100")
        self.assertEqual(json.loads(self.mcp_config.read_text()),
                         {"mcpServers": {"gateway": {
                             "url": "http://172.30.0.11:9100/mcp",
                             "headers": {"X-Dockade-Sandbox": self.entry()["headers"][
                                 "X-Dockade-Sandbox"]}}}})

    def test_a_later_boot_without_a_gateway_removes_the_entry(self):
        self.boot(TOOL_GATEWAY_IP="172.30.0.11")
        self.assertTrue(self.mcp_config.exists())
        self.boot()
        self.assertFalse(self.mcp_config.exists())

    def test_each_boot_mints_a_new_id_and_prints_only_its_prefix(self):
        ids = []
        for _ in range(2):
            proc = self.boot(TOOL_GATEWAY_IP="172.30.0.11")
            sandbox_id = self.entry()["headers"]["X-Dockade-Sandbox"]
            self.assertRegex(sandbox_id, r"\A[0-9a-f]{32}\Z")
            self.assertEqual(proc.stdout.splitlines(), [
                "  MCP gateway -> http://172.30.0.11:8100/mcp",
                f"  Sandbox id  -> {sandbox_id[:8]} (the prefix this sandbox's approval "
                "cards show)"])
            ids.append(sandbox_id)
        self.assertNotEqual(ids[0], ids[1])


class SettingsTests(unittest.TestCase):
    """What the image's agy settings hold, which tier-setup.sh's comment says why."""

    SETTINGS = json.loads((TIER / "settings.json").read_text())

    def test_agys_own_sandbox_is_off_and_the_mode_is_the_default(self):
        self.assertIs(self.SETTINGS["enableTerminalSandbox"], False)
        self.assertEqual(self.SETTINGS["agentMode"], "")

    def test_the_only_rule_is_the_refs_read_allow(self):
        self.assertEqual(self.SETTINGS["permissions"], {"allow": ["read_file(/refs)"]})
        self.assertEqual(set(self.SETTINGS),
                         {"enableTerminalSandbox", "agentMode", "permissions"})


@unittest.skipUnless(_SH or _STRICT, "sh is not installed")
class WrapperTests(unittest.TestCase):
    """The wrapper passes everything through and gives each start its own id."""

    REAL = 'REAL="/opt/antigravity-cli/agy"\n'
    _STUB = ('#!/bin/sh\nprintf "ID %s\\n" "$DOCKADE_SESSION_ID"\n'
             'for a in "$@"; do printf "ARG %s\\n" "$a"; done\n')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.real = self.tmp / "agy-real"
        self.assertEqual(WRAPPER.count(self.REAL), 1, "the wrapper no longer names agy")
        self.wrapper = self.tmp / "agy"
        self.wrapper.write_text(WRAPPER.replace(self.REAL, f'REAL="{self.real}"\n'))

    def run_agy(self, *args: str, stub: bool = True) -> subprocess.CompletedProcess:
        if stub:
            self.real.write_text(self._STUB)
            self.real.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "DOCKADE_SESSION_ID"}
        return subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_SH, str(self.wrapper), *args], capture_output=True, text=True, env=env,
            timeout=60)

    @staticmethod
    def _lines(out: str, tag: str) -> list[str]:
        return [ln[len(tag) + 1:] for ln in out.splitlines() if ln.startswith(tag + " ")]

    def test_every_argument_reaches_agy_unchanged(self):
        args = ("--remote-agent", "--remote-control", "remote-control", "start",
                "--settings", '{"a": 1}', "-p", "two words", "--mode=auto", "")
        proc = self.run_agy(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._lines(proc.stdout, "ARG"), list(args))

    def test_each_start_gets_its_own_id_in_the_registrys_shape(self):
        agent_re = load_task_registry().taskdb._AGENT_RE
        ids = []
        for _ in range(2):
            proc = self.run_agy()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            (session_id,) = self._lines(proc.stdout, "ID")
            self.assertRegex(session_id, r"\A[0-9a-f-]{36}\Z")
            self.assertTrue(agent_re.fullmatch(f"agy-{session_id}"))
            ids.append(session_id)
        self.assertNotEqual(ids[0], ids[1])

    def test_a_missing_or_unrunnable_binary_says_so(self):
        for case in ("missing", "not executable"):
            with self.subTest(case=case):
                if case == "not executable":
                    self.real.write_text(self._STUB)
                    self.real.chmod(0o644)
                proc = self.run_agy("-p", "hi", stub=False)
                self.assertEqual(proc.returncode, 127)
                self.assertIn(f"missing from {self.real}", proc.stderr)
                self.assertEqual(proc.stdout, "")

    def test_the_yolo_alias_resolves_through_the_wrapper(self):
        # An alias naming the real binary's path would skip the id.
        bashrc = (TIER / "dotfiles" / ".bashrc.tier").read_text()
        aliases = re.findall(r"^alias ([\w-]+)='([^']*)'$", bashrc, re.M)
        self.assertEqual(aliases, [("agy-yolo", "agy --dangerously-skip-permissions")])


if __name__ == "__main__":
    unittest.main()
