# SPDX-License-Identifier: Apache-2.0
"""The note tier 1's hook writes for the mounted refs, and where it writes it.

Only the ``refs_note`` function is run, lifted out of ``tier-setup.sh`` and pointed
at a temp directory: the rest of the hook writes /etc as root and the config volume
as the sandbox user, which needs the container.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = {tier: (ROOT / tier / "tier-setup.sh").read_text()
         for tier in ("claude-sandbox", "opencode-sandbox")}
TIER_ONE = HOOKS["claude-sandbox"]
REEXEC = 'exec gosu "$USERNAME" "$0" "$@"'

_BASH = shutil.which("bash")
# Missing bash SKIPS on a dev machine and FAILS under DOCKADE_REQUIRE_TOOLS, which CI
# sets — as in tests/test_launcher_lib.py.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))


def _function(src: str) -> str:
    match = re.search(r"^refs_note\(\) \{\n.*?^\}\n", src, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError("tier-setup.sh no longer defines refs_note()")
    return match.group(0)


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class RefsNoteTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.refs = Path(tmp.name)

    def note(self, refs: Path) -> str:
        proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_BASH, "-c", 'set -euo pipefail; ' + _function(TIER_ONE) + 'refs_note "$1"',
             "refs_note", str(refs)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_each_mounted_ref_is_named(self):
        for name in ("dockade", "platformer"):
            (self.refs / name).mkdir()
        (self.refs / "stray-file").touch()
        note = self.note(self.refs)
        self.assertIn(f"`{self.refs}/dockade`, `{self.refs}/platformer`.", note)
        self.assertNotIn("stray-file", note)
        self.assertIn("read-only", note)

    def test_nothing_is_said_when_nothing_is_mounted(self):
        # The launcher mounts no /refs at all then; an empty one says nothing either.
        for refs in (self.refs, self.refs / "absent"):
            with self.subTest(refs=refs):
                self.assertEqual(self.note(refs), "")


class RefsNotePlacementTests(unittest.TestCase):
    """The config volume is shared by every concurrent sandbox of a tier, and each
    launch has its own SANDBOX_REFS, so a note written there names whichever sibling
    booted last. /etc is the container's own layer."""

    def test_tier_one_writes_it_as_root_into_its_own_layer(self):
        # One managed note, shared with the plugins' bullet (tests/test_launch_settings.py).
        root_phase = TIER_ONE.split(REEXEC, 1)[0]
        defined = root_phase.index("\nrefs_note() {\n")
        cleared = root_phase.index('rm -f "$MANAGED_NOTE"')
        rendered = root_phase.index('note="$(refs_note /refs)"')
        written = root_phase.index('install -o root -g root -m 0644 /dev/stdin "$MANAGED_NOTE"')
        self.assertIn("MANAGED_NOTE=/etc/claude-code/CLAUDE.md\n", root_phase)
        # Cleared first, as the gateway pointer is: the file's absence is the
        # statement that nothing is mounted.
        self.assertLess(defined, cleared)
        self.assertLess(cleared, rendered)
        self.assertLess(rendered, written)

    def test_no_hook_writes_the_refs_into_the_shared_volume(self):
        for tier, src in HOOKS.items():
            with self.subTest(tier=tier):
                user_phase = src.split(REEXEC, 1)[1]
                self.assertNotIn("/refs", user_phase)


_GIT = shutil.which("git")


_DOCS = frozenset({"DESIGN.md", "NOTES.md", "README.md"})


def _is_doc(name: str) -> bool:
    *dirs, base = name.split("/")
    return base in _DOCS and not any(d.startswith(".") for d in dirs)


class RefsReadAllowTests(unittest.TestCase):
    """Both tiers may read /refs without asking, and nothing else about the refs
    loads. The read allow is measured to load nothing from a ref; the command-line
    flag loads a directory's skills, agents and commands (NOTES.md, "An allowed read
    directory loads nothing from it"); the environment variable exists to load the
    allowed directories' CLAUDE.md files. So each tier's allow is held to exactly
    /refs, in the one file that carries it, and the other two to nowhere. Every
    tracked file outside tests/ is scanned for them but a DESIGN.md, NOTES.md or
    README.md outside any dot directory."""

    def test_only_docs_outside_a_dot_directory_are_skipped(self):
        for name in (".claude/skills/x/NOTES.md", ".opencode/README.md",
                     "control-plane/.claude/skills/x/README.md", "SKILL.md",
                     "CONTRIBUTING.md", "notes.md"):
            with self.subTest(name=name):
                self.assertFalse(_is_doc(name))
        for name in ("DESIGN.md", "control-plane/DESIGN.md", "tasks-ui/NOTES.md",
                     "README.md"):
            with self.subTest(name=name):
                self.assertTrue(_is_doc(name))

    def test_each_tier_allows_reads_under_refs_and_nothing_more(self):
        user = json.loads((ROOT / "claude-sandbox" / "user-settings.json").read_text())
        self.assertEqual(user["permissions"], {"additionalDirectories": ["/refs"]})
        text = (ROOT / "opencode-sandbox" / "opencode.json").read_text()
        self.assertEqual(json.loads(text)["permission"],
                         {"external_directory": {"/refs/**": "allow"}})
        # Once: an agent's own `permission` block could grant a second, wider one.
        self.assertEqual(text.count("external_directory"), 1)

    @unittest.skipUnless(_GIT or _STRICT, "git is not installed")
    def test_no_other_way_to_load_a_ref_is_in_the_tree(self):
        # Tracked files outside tests/ and the docs, which name these to explain them
        # and which nothing loads as instructions. The docs are named, not the
        # instruction files, so a new .md of any other name is scanned until someone
        # exempts it. A dot directory holds tool config (.claude/, a future
        # .opencode/), where a NOTES.md or README.md can load beside a skill.
        # COMMENTS COUNT: telling one from code needs a parser per file type, so a
        # comment about any of these words its way around the literal string.
        listing = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_GIT, "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True, check=True).stdout.decode("utf-8", "surrogateescape")
        files = [f for f in listing.split("\0")
                 if f and not f.startswith("tests/") and not _is_doc(f)]
        self.assertIn("claude-sandbox/user-settings.json", files)
        instructions = {"CLAUDE.md", "control-plane/CLAUDE.md",
                        "claude-sandbox/user-CLAUDE.md", "opencode-sandbox/user-AGENTS.md",
                        ".claude/skills/review-before-push/SKILL.md",
                        ".claude/agents/commit-reviewer.md", ".claude/agents/planner.md",
                        ".claude/agents/plan-reviewer.md"}
        self.assertEqual(instructions - set(files), set(),
                         "instruction files the guard no longer scans")
        allowed = {"additionalDirectories": {"claude-sandbox/user-settings.json"},
                   "external_directory": {"opencode-sandbox/opencode.json"},
                   "--add-dir": set(),
                   "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": set()}
        found: dict[str, set[str]] = {needle: set() for needle in allowed}
        for name in files:
            path = ROOT / name
            if not path.is_file():
                continue  # deleted in the worktree, not yet in the index
            data = path.read_bytes()
            for needle, holders in found.items():
                if needle.encode() in data:
                    holders.add(name)
        for needle, holders in found.items():
            with self.subTest(needle=needle):
                self.assertEqual(
                    holders, allowed[needle],
                    f"{needle} would load a ref's skills or instructions, or allow a "
                    "second root; only a DESIGN.md, NOTES.md or README.md outside a dot "
                    "directory may name it, so explain it there or word a comment "
                    "around the literal string")


if __name__ == "__main__":
    unittest.main()
