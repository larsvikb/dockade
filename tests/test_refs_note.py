# SPDX-License-Identifier: Apache-2.0
"""The note tier 1's hook writes for the mounted refs, and where it writes it.

Only the ``refs_note`` function is run, lifted out of ``tier-setup.sh`` and pointed
at a temp directory: the rest of the hook writes /etc as root and the config volume
as the sandbox user, which needs the container.
"""
from __future__ import annotations

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
        root_phase = TIER_ONE.split(REEXEC, 1)[0]
        defined = root_phase.index("\nrefs_note() {\n")
        cleared = root_phase.index('rm -f "$REFS_NOTE"')
        rendered = root_phase.index('note="$(refs_note /refs)"')
        written = root_phase.index('install -o root -g root -m 0644 /dev/stdin "$REFS_NOTE"')
        self.assertIn("REFS_NOTE=/etc/claude-code/CLAUDE.md\n", root_phase)
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


if __name__ == "__main__":
    unittest.main()
