# SPDX-License-Identifier: Apache-2.0
"""Tier 1's per-container plugin settings: rendered, installed, noted, and passed on.

Each launch has its own marketplaces and plugins, so they live in the container's own
``/etc/claude-code/launch-settings.json`` rather than in the config volume every
sibling shares, and the root-owned wrapper hands the file to every ``claude`` call.

What runs here is lifted out of the scripts and pointed at temp directories: the
render, the note and the advisory as functions, the root phase's install block with
``gosu`` and ``install`` stubbed, and the wrapper with a stub for the real binary.
The rest of the hook writes /etc as root, which needs the container.
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

ROOT = Path(__file__).resolve().parents[1]
TIER_SETUP = (ROOT / "claude-sandbox" / "tier-setup.sh").read_text()
WRAPPER = (ROOT / "claude-sandbox" / "claude-wrapper.sh").read_text()
REEXEC = 'exec gosu "$USERNAME" "$0" "$@"'

_BASH = shutil.which("bash")
_SH = shutil.which("sh")
_JQ = shutil.which("jq")
# Missing bash or jq SKIPS on a dev machine and FAILS under DOCKADE_REQUIRE_TOOLS,
# which CI sets — as in tests/test_refs_note.py.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))
_TOOLS = bool(_BASH and _JQ)


def _function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", TIER_SETUP, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"tier-setup.sh no longer defines {name}()")
    return match.group(0)


def _shape() -> str:
    match = re.search(r"^LAUNCH_SHAPE='.*?'\n", TIER_SETUP, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError("tier-setup.sh no longer defines LAUNCH_SHAPE")
    return match.group(0)


def _assignment(src: str, name: str) -> str:
    match = re.search(rf'^\s*{name}="?([^"\n]+)"?$', src, re.MULTILINE)
    if match is None:
        raise AssertionError(f"no {name}= assignment")
    return match.group(1)


def _bash(script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
        [_BASH, "-c", "set -euo pipefail; " + script, "harness", *args],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {})})


def _marketplace(where: Path, name: object, plugins: tuple[str, ...] = ("hello",)) -> Path:
    (where / ".claude-plugin").mkdir(parents=True)
    manifest = {"owner": {"name": "x"}, "plugins": [{"name": p} for p in plugins]}
    if name is not None:
        manifest["name"] = name
    (where / ".claude-plugin" / "marketplace.json").write_text(json.dumps(manifest))
    return where


@unittest.skipUnless(_TOOLS or _STRICT, "bash or jq is not installed")
class RenderTests(unittest.TestCase):
    """``render_launch_settings``: what is mounted and allowlisted, as JSON."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mounted = Path(tmp.name) / "marketplaces"
        self.mounted.mkdir()

    def render(self, plugins: str = "", mounted: Path | None = None):
        proc = _bash(_function("render_launch_settings") + 'render_launch_settings "$1"',
                     str(mounted or self.mounted), env={"SANDBOX_PLUGINS": plugins})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("\n"), 1, "one line of JSON")
        return json.loads(proc.stdout), proc.stderr

    def test_a_directory_holding_marketplaces_registers_each_by_manifest_name(self):
        _marketplace(self.mounted / "dir-a", "alpha")
        _marketplace(self.mounted / "dir-b", "beta")
        (self.mounted / "not-a-marketplace").mkdir()
        settings, _ = self.render()
        self.assertEqual(settings, {"extraKnownMarketplaces": {
            "alpha": {"source": {"source": "directory", "path": f"{self.mounted}/dir-a"}},
            "beta": {"source": {"source": "directory", "path": f"{self.mounted}/dir-b"}},
        }})

    def test_a_directory_that_is_one_marketplace_registers_it(self):
        _marketplace(self.mounted, "solo")
        _marketplace(self.mounted / "nested", "ignored")
        settings, _ = self.render()
        self.assertEqual(list(settings["extraKnownMarketplaces"]), ["solo"])
        self.assertEqual(settings["extraKnownMarketplaces"]["solo"]["source"]["path"],
                         str(self.mounted))

    def test_a_nameless_manifest_is_skipped_with_a_warning(self):
        _marketplace(self.mounted / "nameless", None)
        _marketplace(self.mounted / "empty", "")
        (self.mounted / "broken" / ".claude-plugin").mkdir(parents=True)
        (self.mounted / "broken" / ".claude-plugin" / "marketplace.json").write_text("{")
        settings, err = self.render()
        self.assertEqual(settings, {})
        self.assertEqual(err.count("has no usable 'name' — skipping"), 3)

    def test_a_name_outside_the_alphabet_is_skipped_with_a_warning(self):
        for i, name in enumerate(("a b", "a`b", "a\nb", "a/b", "ü", "x*")):
            _marketplace(self.mounted / f"bad{i}", name)
        _marketplace(self.mounted / "good", "Good.name_1-2")
        settings, err = self.render()
        self.assertEqual(list(settings["extraKnownMarketplaces"]), ["Good.name_1-2"])
        self.assertEqual(err.count("outside [A-Za-z0-9._-] — skipping"), 6)

    def test_a_duplicate_name_warns_and_the_last_wins(self):
        _marketplace(self.mounted / "a", "same")
        _marketplace(self.mounted / "b", "same")
        settings, err = self.render()
        self.assertIn("two mounted marketplaces both call themselves 'same'", err)
        self.assertEqual(settings["extraKnownMarketplaces"]["same"]["source"]["path"],
                         f"{self.mounted}/b")

    def test_an_id_a_mounted_manifest_lacks_is_skipped(self):
        _marketplace(self.mounted / "m", "mkt", plugins=("hello",))
        settings, err = self.render("hello@mkt typo@mkt")
        self.assertEqual(settings["enabledPlugins"], {"hello@mkt": True})
        self.assertIn("marketplace 'mkt' declares no plugin 'typo' — skipping 'typo@mkt'", err)

    def test_an_id_naming_an_unmounted_marketplace_passes_through_silently(self):
        # The advisory, run by the user phase, is what says so; the render does not.
        settings, err = self.render("p@official")
        self.assertEqual(settings, {"enabledPlugins": {"p@official": True}})
        self.assertEqual(err, "")

    def test_an_id_without_a_marketplace_is_skipped(self):
        settings, err = self.render("bare")
        self.assertEqual(settings, {})
        self.assertIn("'bare' is not in plugin@marketplace form", err)

    def test_commas_separate_ids_like_whitespace(self):
        settings, _ = self.render("a@m,b@m c@m")
        self.assertEqual(sorted(settings["enabledPlugins"]), ["a@m", "b@m", "c@m"])

    def test_nothing_mounted_or_allowed_renders_an_empty_object(self):
        for mounted in (self.mounted, self.mounted / "absent"):
            with self.subTest(mounted=mounted):
                settings, err = self.render(mounted=mounted)
                self.assertEqual(settings, {})
                self.assertEqual(err, "")

    def test_the_render_passes_the_shape_root_checks(self):
        _marketplace(self.mounted / "m", "mkt")
        proc = _bash(_function("render_launch_settings")
                     + _shape() + 'jq -e "$LAUNCH_SHAPE" <<<"$(render_launch_settings "$1")"',
                     str(self.mounted), env={"SANDBOX_PLUGINS": "hello@mkt x@elsewhere"})
        self.assertEqual(proc.returncode, 0, proc.stderr)


@unittest.skipUnless(_TOOLS or _STRICT, "bash or jq is not installed")
class ShapeTests(unittest.TestCase):
    """``LAUNCH_SHAPE``, root's check on what the sandbox user rendered."""

    def accepts(self, doc: str) -> bool:
        proc = _bash(_shape() + 'jq -e "$LAUNCH_SHAPE" >/dev/null 2>&1 <<<"$1"', doc)
        return proc.returncode == 0

    def test_the_two_keys_pass(self):
        self.assertTrue(self.accepts(json.dumps({
            "extraKnownMarketplaces": {"m": {"source": {}}},
            "enabledPlugins": {"p@m": True}})))
        self.assertTrue(self.accepts('{"enabledPlugins":{"p@m":true}}'))

    def test_anything_else_fails(self):
        for doc in ("{}", "[]", "not json", '"x"',
                    '{"enabledPlugins":{}}',
                    '{"enabledPlugins":{"p@m":false}}',
                    '{"enabledPlugins":{"p@m":true},"hooks":{"x":true}}',
                    '{"extraKnownMarketplaces":{"a b":{}}}',
                    '{"extraKnownMarketplaces":[]}',
                    '{"enabledPlugins":[true]}',
                    '{"extraKnownMarketplaces":{"abc\\n":{}}}'):
            with self.subTest(doc=doc):
                self.assertFalse(self.accepts(doc))


@unittest.skipUnless(_TOOLS or _STRICT, "bash or jq is not installed")
class NoteTests(unittest.TestCase):
    """``launch_note``: the managed-note bullet, read from the settings."""

    def note(self, doc: str) -> str:
        proc = _bash(_function("launch_note") + 'launch_note "$1"', doc)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_it_names_exactly_the_registered_marketplaces(self):
        doc = {"extraKnownMarketplaces": {"beta": {}, "alpha": {}},
               "enabledPlugins": {"p@gamma": True}}
        first = self.note(json.dumps(doc)).splitlines()[0]
        names = re.findall(r"`([^`]+)`", first.split(":", 1)[1])
        self.assertEqual(sorted(names), sorted(doc["extraKnownMarketplaces"]))

    def test_it_states_what_no_longer_turns_a_plugin_off(self):
        for doc in ('{"extraKnownMarketplaces":{"m":{}}}', '{"enabledPlugins":{"p@m":true}}'):
            with self.subTest(doc=doc):
                note = " ".join(self.note(doc).split())
                self.assertIn("`/plugin disable` of a host-enabled plugin", note)
                self.assertIn("`enabledPlugins: false` in a project's or your own settings", note)
                self.assertIn("a `--settings` of its own", note)

    def test_plugins_alone_name_no_marketplace(self):
        note = self.note('{"enabledPlugins":{"p@m":true}}')
        self.assertNotIn("/marketplaces", note)
        self.assertTrue(note.startswith("- **Plugins are enabled host-side"))

    def test_nothing_is_said_for_empty_settings(self):
        for doc in ("{}", '{"extraKnownMarketplaces":{}}'):
            with self.subTest(doc=doc):
                self.assertEqual(self.note(doc), "")


@unittest.skipUnless(_TOOLS or _STRICT, "bash or jq is not installed")
class AdvisoryTests(unittest.TestCase):
    """``plugin_advisory``: an enabled id whose marketplace nothing knows."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.settings = self.dir / "launch-settings.json"
        self.known = self.dir / "known_marketplaces.json"

    def advise(self) -> str:
        proc = _bash(_function("plugin_advisory") + 'plugin_advisory "$1" "$2"',
                     str(self.settings), str(self.known))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        return proc.stderr

    def test_an_id_neither_mounted_nor_known_is_noted(self):
        self.settings.write_text('{"enabledPlugins":{"p@nowhere":true}}')
        self.assertIn("'p@nowhere' names marketplace 'nowhere', which is neither mounted",
                      self.advise())

    def test_an_id_naming_a_mounted_marketplace_is_silent(self):
        self.settings.write_text('{"extraKnownMarketplaces":{"m":{}},'
                                 '"enabledPlugins":{"p@m":true}}')
        self.assertEqual(self.advise(), "")

    def test_an_id_the_volume_knows_is_silent(self):
        self.settings.write_text('{"enabledPlugins":{"p@cached":true,"q@nowhere":true}}')
        self.known.write_text('{"cached":{}}')
        err = self.advise()
        self.assertNotIn("p@cached", err)
        self.assertIn("q@nowhere", err)

    def test_no_launch_settings_is_silent(self):
        self.assertEqual(self.advise(), "")


@unittest.skipUnless(_TOOLS or _STRICT, "bash or jq is not installed")
class RootInstallTests(unittest.TestCase):
    """The root phase's block from the render to the managed note, run as written
    with ``gosu`` answering for the render and ``install`` copying stdin."""

    GOSU = '#!/bin/sh\n[ -z "$STUB_FAIL" ] || exit 1\nprintf "%s\\n" "$STUB_RENDER"\n'
    INSTALL = '#!/bin/sh\nfor last; do :; done\ncat > "$last"\n'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.etc = self.dir / "etc"
        self.etc.mkdir()
        self.refs = self.dir / "refs"
        self.refs.mkdir()
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        for name, body in (("gosu", self.GOSU), ("install", self.INSTALL)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.path = f"{bin_dir}:{os.environ['PATH']}"
        root_phase = TIER_SETUP.split(REEXEC, 1)[0]
        start = root_phase.index("    LAUNCH_SETTINGS=/etc/claude-code/")
        end = root_phase.index("    # Root is done.")
        self.block = (root_phase[start:end]
                      .replace("/etc/claude-code/", f"{self.etc}/")
                      .replace("refs_note /refs", f"refs_note {self.refs}"))

    def run_block(self, render: str = "{}", fail: bool = False):
        script = (_function("refs_note") + _shape() + _function("launch_note")
                  + 'USERNAME=sandbox\n' + self.block)
        proc = _bash(script, env={"PATH": self.path, "STUB_RENDER": render,
                                  "STUB_FAIL": "1" if fail else ""})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stderr

    @property
    def installed(self) -> Path:
        return self.etc / "launch-settings.json"

    @property
    def note(self) -> Path:
        return self.etc / "CLAUDE.md"

    def test_an_empty_render_installs_nothing(self):
        self.installed.write_text("stale")
        self.note.write_text("stale")
        self.assertEqual(self.run_block("{}"), "")
        self.assertFalse(self.installed.exists())
        self.assertFalse(self.note.exists())

    def test_a_failing_render_warns_and_boots_without_plugins(self):
        err = self.run_block(fail=True)
        self.assertIn("rendering the plugin settings failed — launching without plugins", err)
        self.assertFalse(self.installed.exists())

    def test_a_malformed_render_warns_and_boots_without_plugins(self):
        for render in ("not json", '{"hooks":{}}', '{"extraKnownMarketplaces":{"a`b":{}}}',
                       # jq -e alone judges only the last document of a stream.
                       '{"hooks":{"x":1}} {"enabledPlugins":{"p@m":true}}',
                       # Oniguruma's $ also matches before a trailing newline.
                       '{"extraKnownMarketplaces":{"abc\\n":{}}}'):
            with self.subTest(render=render):
                err = self.run_block(render)
                self.assertIn("malformed — launching without plugins", err)
                self.assertFalse(self.installed.exists())
                self.assertFalse(self.note.exists())

    def test_a_render_is_installed_and_noted(self):
        render = '{"extraKnownMarketplaces":{"mkt":{}},"enabledPlugins":{"p@mkt":true}}'
        self.assertEqual(self.run_block(render), "")
        self.assertEqual(json.loads(self.installed.read_text()), json.loads(render))
        self.assertIn("mounted read-only at `/marketplaces`**: `mkt`.", self.note.read_text())

    def test_the_refs_and_the_plugins_share_the_one_note(self):
        (self.refs / "docs").mkdir()
        self.run_block('{"extraKnownMarketplaces":{"mkt":{}}}')
        note = self.note.read_text()
        self.assertIn("Reference material is mounted read-only", note)
        self.assertIn("Plugin marketplaces are mounted read-only", note)
        self.assertLess(note.index("Reference material"), note.index("Plugin marketplaces"))
        self.run_block("{}")
        self.assertNotIn("Plugin marketplaces", self.note.read_text())


@unittest.skipUnless(_SH or _STRICT, "sh is not installed")
class WrapperTests(unittest.TestCase):
    """The wrapper, run with its paths pointed at temp files and the real binary a
    stub that prints the argv it was given, one argument per line."""

    STUB = '#!/bin/sh\nfor a; do printf "%s\\n" "$a"; done\n'
    CALLERS = ((), ("--settings", "mine.json"), ("--settings=mine.json",),
               ("--settings", "a.json", "--settings=b.json"),
               ("-p", "hi there"), ("mcp", "list"))

    def test_the_paths_are_the_ones_tier_setup_writes(self):
        for name in ("MCP_CONFIG", "LAUNCH_SETTINGS"):
            with self.subTest(name=name):
                setup_name = "MCP_GATEWAY_CONFIG" if name == "MCP_CONFIG" else name
                self.assertEqual(_assignment(WRAPPER, name),
                                 _assignment(TIER_SETUP, setup_name))

    def wrapper(self, tmp: Path, *, gateway: bool, launch: bool) -> Path:
        real = tmp / "real-claude"
        real.write_text(self.STUB)
        real.chmod(real.stat().st_mode | stat.S_IXUSR)
        mcp, settings = tmp / "mcp.json", tmp / "launch.json"
        for path, present in ((mcp, gateway), (settings, launch)):
            if present:
                path.write_text("{}")
        src = (WRAPPER
               .replace(f'REAL="{_assignment(WRAPPER, "REAL")}"', f'REAL="{real}"')
               .replace(f'MCP_CONFIG="{_assignment(WRAPPER, "MCP_CONFIG")}"',
                        f'MCP_CONFIG="{mcp}"')
               .replace(f'LAUNCH_SETTINGS="{_assignment(WRAPPER, "LAUNCH_SETTINGS")}"',
                        f'LAUNCH_SETTINGS="{settings}"'))
        self.assertNotIn("/etc/claude-code/", src)
        wrapper = tmp / "claude"
        wrapper.write_text(src)
        return wrapper

    def test_every_combination(self):
        for gateway in (False, True):
            for launch in (False, True):
                for caller in self.CALLERS:
                    with self.subTest(gateway=gateway, launch=launch, caller=caller), \
                            tempfile.TemporaryDirectory() as tmp:
                        self.check(Path(tmp), gateway, launch, list(caller))

    def check(self, tmp: Path, gateway: bool, launch: bool, caller: list[str]):
        wrapper = self.wrapper(tmp, gateway=gateway, launch=launch)
        proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_SH, str(wrapper), *caller], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = proc.stdout.splitlines()
        # Strict mode sits right before the caller's argv, which arrives intact.
        n = len(caller)
        self.assertEqual(argv[len(argv) - n:], caller)
        self.assertEqual(argv[len(argv) - n - 1], "--strict-mcp-config")
        prepended = argv[:len(argv) - n - 1]
        expected = ((["--settings", str(tmp / "launch.json")] if launch else [])
                    + (["--mcp-config", str(tmp / "mcp.json")] if gateway else []))
        # The launch --settings comes first, so a caller's replaces it.
        self.assertEqual(prepended, expected)
        # Each variadic flag is followed by an option.
        if gateway:
            self.assertTrue(argv[argv.index("--mcp-config") + 2].startswith("-"))
        caller_settings = any(a == "--settings" or a.startswith("--settings=") for a in caller)
        if launch and caller_settings:
            self.assertEqual(proc.stderr.count("\n"), 1, proc.stderr)
            self.assertIn("host's plugins and marketplaces are not loaded", proc.stderr)
        else:
            self.assertEqual(proc.stderr, "")

    def test_a_missing_binary_exits_127(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = self.wrapper(Path(tmp), gateway=True, launch=True)
            (Path(tmp) / "real-claude").unlink()
            proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
                [_SH, str(wrapper)], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 127)


if __name__ == "__main__":
    unittest.main()
