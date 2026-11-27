# SPDX-License-Identifier: Apache-2.0
"""agy's boot hook, its wrapper and its status line, run.

``antigravity-sandbox/tier-setup.sh`` needs no root: as root it only re-execs as the
sandbox user, so its whole effect is the user phase, which runs here as the test's own
user with HOME and the config volume in a temp dir and the image's /etc/antigravity
pointed at a copy of the repo's templates. ``agy-wrapper.sh`` runs with its real binary
replaced by a stub that prints what it was given. ``sandbox-common/statusline.sh``,
which agy shares with Claude's tier, runs on the JSON a CLI hands it. The text all
three are wired by, in the Dockerfile, is held in tests/test_sandbox_wiring.py.
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
_GIT = shutil.which("git")
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
        self.assertIn("statusLine", json.loads(settings.read_text()))
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

    def test_the_status_line_is_the_baked_script(self):
        # Where the Dockerfile bakes it is held in tests/test_sandbox_wiring.py.
        self.assertEqual(self.SETTINGS["statusLine"],
                         {"type": "command", "command": "/etc/antigravity/statusline.sh"})

    def test_no_other_key_is_set(self):
        self.assertEqual(set(self.SETTINGS),
                         {"enableTerminalSandbox", "agentMode", "permissions", "statusLine"})


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


@unittest.skipUnless((_BASH and _JQ and _GIT) or _STRICT, "bash, jq or git is not installed")
class StatusLineTests(unittest.TestCase):
    """The shared status line, on the payload agy was measured sending before its first
    turn and after one (NOTES.md, "agy 1.5.17's status line, before and after a turn"),
    and on Claude Code's."""

    SCRIPT = ROOT / "sandbox-common" / "statusline.sh"
    _SGR = re.compile(r"\x1b\[\d+m")
    LAUNCH_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plain = Path(tmp.name) / "widgets"
        self.plain.mkdir()

    def line(self, payload: dict, launch_id: str | None = None) -> str:
        """What the script prints, with its own colours taken out."""
        env = {k: v for k, v in os.environ.items() if k != "DOCKADE_SESSION_ID"}
        if launch_id is not None:
            env["DOCKADE_SESSION_ID"] = launch_id
        proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_BASH, str(self.SCRIPT)], input=json.dumps(payload), capture_output=True,
            text=True, env=env, timeout=60)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        return self._SGR.sub("", proc.stdout)

    def agy(self, **over) -> dict:
        """agy 1.5.17's payload before the first turn, as measured, with the cwd."""
        return {"session_id": "", "cwd": str(self.plain),
                "workspace": {"current_dir": str(self.plain)},
                "context_window": {"total_input_tokens": 0, "context_window_size": 0,
                                   "current_usage": None}, **over}

    def claude(self, **over) -> dict:
        return {"session_id": "abcdef0123456789", "cwd": str(self.plain),
                "workspace": {"current_dir": str(self.plain)},
                "context_window": {"total_input_tokens": 142000,
                                   "context_window_size": 1000000,
                                   "current_usage": {"input_tokens": 142000}},
                "rate_limits": {"five_hour": {"used_percentage": 30.5},
                                "seven_day": {"used_percentage": 60}}, **over}

    def test_agys_payload_shows_the_launchs_id_and_nothing_it_lacks(self):
        self.assertEqual(self.line(self.agy(), self.LAUNCH_ID),
                         "🔒 sandbox · widgets · id 0f1e2d3c")

    def test_agys_payload_after_a_turn_shows_the_launchs_id_and_agys_context(self):
        # The figures as measured; the conversation id invented. quota is unread.
        payload = self.agy(
            session_id="cd8a5f9c-1111-2222-3333-444455556666",
            context_window={"total_input_tokens": 26741, "total_output_tokens": 2427,
                            "context_window_size": 1048576, "used_percentage": 2.55,
                            "current_usage": {"input_tokens": 4968, "output_tokens": 1524,
                                              "cache_creation_input_tokens": 0,
                                              "cache_read_input_tokens": 16853}},
            quota={"3p-5h": {"remaining_fraction": 0.9, "reset_in_seconds": 600}})
        self.assertEqual(self.line(payload, self.LAUNCH_ID),
                         "🔒 sandbox · widgets · id 0f1e2d3c · ctx 26k/1M 2%")

    def test_claudes_payload_shows_its_own_id_context_and_limits(self):
        self.assertEqual(
            self.line(self.claude()),
            "🔒 sandbox · widgets · id abcdef01 · ctx 142k/1M 14% · 5h 30% · 7d 60%")

    def test_the_launchs_id_wins_and_the_payloads_stands_in_without_one(self):
        # The launch's is the id agy's claims are made under; a payload id agy may
        # send after its first turn is its own conversation's, which is not.
        no_id = self.agy()
        del no_id["session_id"]
        for name, payload, launch_id, shown in (
                ("empty, no launch id", self.agy(), None, None),
                ("absent, a launch id", no_id, self.LAUNCH_ID, "0f1e2d3c"),
                ("null, a launch id", self.agy(session_id=None), self.LAUNCH_ID, "0f1e2d3c"),
                ("its own, a launch id", self.agy(session_id="abcdef0123"), self.LAUNCH_ID,
                 "0f1e2d3c"),
                ("its own, no launch id", self.agy(session_id="abcdef0123"), None,
                 "abcdef01"),
                ("its own, an empty launch id", self.agy(session_id="abcdef0123"), "",
                 "abcdef01")):
            with self.subTest(case=name):
                text = self.line(payload, launch_id)
                if shown is None:
                    self.assertEqual(text, "🔒 sandbox · widgets")
                else:
                    self.assertEqual(text, f"🔒 sandbox · widgets · id {shown}")

    def test_an_id_keeps_only_the_characters_an_agent_id_may_hold(self):
        bad = "a\x1b]0;b\x07c/d e-_f.g"
        for name, payload, launch_id in (("its own", self.agy(session_id=bad), None),
                                         ("the launch's", self.agy(), bad)):
            with self.subTest(id=name):
                self.assertEqual(self.line(payload, launch_id),
                                 "🔒 sandbox · widgets · id a0bcde-_")

    def test_no_control_character_in_the_directory_reaches_the_terminal(self):
        evil = str(self.plain / "x\x1b]0;title\x07y\x7fz")
        text = self.line(self.agy(cwd=evil, workspace={"current_dir": evil}))
        self.assertEqual(text, "🔒 sandbox · x]0;titleyz")

    def test_the_directory_is_the_workspaces_then_the_cwds_then_a_placeholder(self):
        other = self.plain.parent / "gadgets"
        other.mkdir()
        for payload, shown in (
                ({"workspace": {"current_dir": str(self.plain)}, "cwd": str(other)},
                 "widgets"),
                ({"cwd": str(other)}, "gadgets"),
                ({}, "workspace")):
            with self.subTest(shown=shown):
                self.assertEqual(self.line(payload), f"🔒 sandbox · {shown}")

    def test_the_branch_shows_inside_a_repo_only(self):
        self.assertEqual(self.line(self.agy()), "🔒 sandbox · widgets")
        subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_GIT, "init", "-q", "-b", "gizmo", str(self.plain)], check=True,
            capture_output=True, timeout=60)
        self.assertEqual(self.line(self.agy()), "🔒 sandbox · widgets · ⎇ gizmo")

    def test_the_context_segment_hides_without_a_current_figure(self):
        def window(**over):
            return {"total_input_tokens": 142000, "context_window_size": 1000000,
                    "current_usage": {"input_tokens": 142000}, **over}
        for name, cw in (("no current usage", window(current_usage=None)),
                         ("a zero total", window(total_input_tokens=0)),
                         ("a zero size", window(context_window_size=0)),
                         ("a string total", window(total_input_tokens="142000")),
                         ("a string size", window(context_window_size="1000000")),
                         ("no size", {"total_input_tokens": 142000,
                                      "current_usage": {"input_tokens": 1}})):
            with self.subTest(case=name):
                self.assertEqual(self.line(self.claude(context_window=cw)),
                                 "🔒 sandbox · widgets · id abcdef01 · 5h 30% · 7d 60%")


if __name__ == "__main__":
    unittest.main()
