# SPDX-License-Identifier: Apache-2.0
"""The operator's own instructions, appended at boot after a tier's baked ones.

``sandbox-common/operator-instructions.sh`` is sourced by each tier's setup hook in
its sandbox-user phase, and runs under the hook's ``set -euo pipefail``. Run here
under real bash on temporary files: the hook itself is a root boot script, but this
function only reads one file and appends to another. The hooks' call sites are held in
tests/test_sandbox_wiring.py, the launcher's mount in tests/test_launcher_lib.py.

Each failure is run as the hook runs it, with a statement after the call and a
timeout, so a failure that killed the boot, or a read that blocked it, shows as one.

``boundary-check.sh``'s probe of the ``/operator`` mounts is lifted out and run on a
temporary directory, as tests/test_boundary_check.py runs its blocks.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_boundary_check import _helpers, _lift, _prelude

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "sandbox-common" / "operator-instructions.sh"

_BASH = shutil.which("bash")
# Missing bash SKIPS on a dev machine and FAILS under DOCKADE_REQUIRE_TOOLS, which CI
# sets, as in tests/test_launcher_lib.py.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))

# As a hook runs it: strict, with something after the call that must still run.
_HARNESS = ('set -euo pipefail; source "$1"; append_operator_instructions "$2" "$3"; '
            'echo "went on"')

_BAKED = "# You are in a sandbox\n\n- **No push.** Branch and commit here.\n"
_HEADING = "# The operator's own instructions"
_WHOSE = ("Everything above this heading is dockade's, the same in every sandbox of this "
          "tier. What follows is the operator's, added on this machine.")
_ROOT_SKIP = "root reads and writes a file whatever its mode"


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class AppendTests(unittest.TestCase):
    """``append_operator_instructions <operator_file> <dest>``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.dest = self.root / "CLAUDE.md"
        self.dest.write_text(_BAKED)
        self.src = self.root / "operator" / "CLAUDE.md"
        self.src.parent.mkdir()

    def append(self, src: Path | None = None, dest: Path | None = None):
        return subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_BASH, "-c", _HARNESS, "append", str(HELPER), str(src or self.src),
             str(dest or self.dest)],
            capture_output=True, text=True, encoding="utf-8", timeout=30)

    def assertWentOn(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "went on\n")

    def assertBaked(self, proc, warning: str | None = None):
        self.assertWentOn(proc)
        self.assertEqual(self.dest.read_bytes(), _BAKED.encode())
        if warning is None:
            self.assertEqual(proc.stderr, "")
        else:
            self.assertIn(f"WARNING: {warning}", proc.stderr)
            self.assertIn("the operator's instructions were not added", proc.stderr)

    @staticmethod
    def appended(content: str) -> str:
        return f"{_BAKED}\n{_HEADING}\n\n{_WHOSE}\n\n{content}"

    def test_present_appends_the_heading_then_the_content_after_the_baked_text(self):
        # Whole file: the baked text an unchanged prefix, then a heading line naming the
        # operator, so an empty or HTML-comment separator fails too.
        self.src.write_text("- Prefer `rg` to `grep`.\n- Answer in British English.\n")
        proc = self.append()
        self.assertWentOn(proc)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(self.dest.read_text(), self.appended(
            "- Prefer `rg` to `grep`.\n- Answer in British English.\n"))

    def test_the_heading_is_a_markdown_heading_naming_the_operator(self):
        self.assertRegex(_HEADING, r"^# .*\boperator\b")
        self.assertIn(f'OPERATOR_INSTRUCTIONS_HEADING="{_HEADING}"', HELPER.read_text())

    def test_content_without_a_trailing_newline_still_ends_with_one(self):
        self.src.write_text("No trailing newline")
        self.assertWentOn(self.append())
        self.assertEqual(self.dest.read_text(), self.appended("No trailing newline\n"))

    def test_trailing_blank_lines_are_not_carried_over(self):
        self.src.write_text("One line\n\n\n")
        self.assertWentOn(self.append())
        self.assertEqual(self.dest.read_text(), self.appended("One line\n"))

    def test_absent_empty_and_whitespace_only_leave_the_baked_file(self):
        self.assertBaked(self.append())
        for content in ("", "\n", "  \n\t\n \n"):
            with self.subTest(content=content):
                self.src.write_text(content)
                self.assertBaked(self.append())

    def test_a_directory_fifo_or_dangling_link_warns_and_leaves_the_baked_file(self):
        directory = self.root / "operator" / "dir"
        directory.mkdir()
        fifo = self.root / "operator" / "fifo"
        os.mkfifo(fifo)
        dangling = self.root / "operator" / "dangling"
        dangling.symlink_to(self.root / "nowhere")
        for src in (directory, fifo, dangling):
            with self.subTest(src=src.name):
                self.assertBaked(self.append(src), f"{src} is not a regular file")

    def test_a_link_to_a_regular_file_is_followed(self):
        target = self.root / "dotfiles" / "CLAUDE.md"
        target.parent.mkdir()
        target.write_text("From dotfiles\n")
        self.src.symlink_to(target)
        self.assertWentOn(self.append())
        self.assertEqual(self.dest.read_text(), self.appended("From dotfiles\n"))

    @unittest.skipIf(os.geteuid() == 0, _ROOT_SKIP)
    def test_an_unreadable_file_warns_and_leaves_the_baked_file(self):
        self.src.write_text("Secret\n")
        self.src.chmod(0)
        self.addCleanup(self.src.chmod, 0o644)
        self.assertBaked(self.append(), f"{self.src} could not be read")

    def test_a_destination_that_cannot_be_written_warns_and_goes_on(self):
        self.src.write_text("Content\n")
        directory = self.root / "a-directory"
        directory.mkdir()
        proc = self.append(dest=directory)
        self.assertWentOn(proc)
        self.assertIn(f"WARNING: {directory} could not be written", proc.stderr)
        self.assertEqual(list(directory.iterdir()), [])

    @unittest.skipIf(os.geteuid() == 0, _ROOT_SKIP)
    def test_a_read_only_destination_warns_and_stays_as_baked(self):
        self.src.write_text("Content\n")
        self.dest.chmod(0o444)
        self.addCleanup(self.dest.chmod, 0o644)
        self.assertBaked(self.append(), f"{self.dest} could not be written")


_OPERATOR = "/operator/*"


def _operator_block(root: Path) -> str:
    block = _lift(r"^# /operator/<file> are the operator's own files.*?^\[ \"\$operator_files\" [^\n]*\n",
                  "/operator probe")
    # Without the swap the verdict would depend on the machine having an /operator.
    if block.count(_OPERATOR) != 1:
        raise AssertionError(f"the /operator probe no longer globs {_OPERATOR} once")
    return block.replace(_OPERATOR, f"{root}/operator/*")


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class OperatorMountProbeTests(unittest.TestCase):
    """boundary-check.sh's probe: each mounted operator file is read-only, and probing
    it changes nothing. Read-only is a file mode here and a mount option in the
    sandbox; ``: >>`` is refused by either."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "operator").mkdir()

    def run_probe(self) -> tuple[list[str], str]:
        script = (_prelude() + _helpers() + _operator_block(self.root)
                  + 'echo "pass=$pass fail=$fail"\n')
        proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_BASH, "-c", script], cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        verdicts = [re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in lines[:-1]]
        return verdicts, lines[-1] if lines else ""

    def test_nothing_mounted_is_information_not_a_verdict(self):
        verdicts, counts = self.run_probe()
        self.assertEqual(counts, "pass=0 fail=0")
        self.assertEqual(len(verdicts), 1)
        self.assertIn("/operator not mounted", verdicts[0])

    def test_a_writable_file_fails_and_is_left_as_it_was(self):
        file = self.root / "operator" / "CLAUDE.md"
        file.write_text("Mine\n")
        before = file.stat().st_mtime_ns
        verdicts, counts = self.run_probe()
        self.assertEqual(counts, "pass=0 fail=1")
        self.assertEqual(verdicts, [f"FAIL {file} is WRITABLE — the operator's files must be read-only"])
        self.assertEqual(file.read_text(), "Mine\n")
        self.assertEqual(file.stat().st_mtime_ns, before)

    @unittest.skipIf(os.geteuid() == 0, _ROOT_SKIP)
    def test_each_read_only_file_passes(self):
        names = ("AGENTS.md", "CLAUDE.md")
        for name in names:
            file = self.root / "operator" / name
            file.write_text("Mine\n")
            file.chmod(0o444)
        verdicts, counts = self.run_probe()
        self.assertEqual(counts, "pass=2 fail=0")
        self.assertEqual(verdicts, [f"PASS {self.root}/operator/{name} is read-only" for name in names])


if __name__ == "__main__":
    unittest.main()
