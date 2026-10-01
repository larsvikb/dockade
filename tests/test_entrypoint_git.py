# SPDX-License-Identifier: Apache-2.0
"""The entrypoint's git identity block, run from a workspace like a host-made worktree.

Why it runs from / is the comment above the block in ``entrypoint.sh``. Only the
block is run, lifted out with ``gosu`` stubbed: the rest arms a firewall as root.
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
ENTRYPOINT = (ROOT / "sandbox-common" / "entrypoint.sh").read_text()

_BASH = shutil.which("bash")
_GIT = shutil.which("git")
# Missing tools SKIP on a dev machine and FAIL under DOCKADE_REQUIRE_TOOLS, which CI
# sets — as in tests/test_launcher_lib.py.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))


def _identity_block() -> str:
    match = re.search(r'^if \[ -n "\$\{GIT_USER_NAME:-\}" \].*?^fi\n',
                      ENTRYPOINT, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError("entrypoint.sh no longer has the git identity block")
    return match.group(0)


@unittest.skipUnless((_BASH and _GIT) or _STRICT, "bash or git is not installed")
class GitIdentityTests(unittest.TestCase):

    def test_a_host_worktree_as_the_workspace_does_not_stop_the_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / ".git").write_text(
                "gitdir: /mnt/c/Users/someone/src/app/.git/worktrees/tmp.x\n")
            config = Path(tmp) / "gitconfig"
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.update(GIT_CONFIG_GLOBAL=str(config), USERNAME="sandbox",
                       GIT_USER_NAME="Ada Example", GIT_USER_EMAIL="ada@example.invalid")
            script = 'set -euo pipefail; gosu() { shift; "$@"; }; ' + _identity_block()
            proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
                [_BASH, "-c", script], cwd=workspace, env=env,
                capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            written = config.read_text()
            self.assertIn("name = Ada Example", written)
            self.assertIn("email = ada@example.invalid", written)


if __name__ == "__main__":
    unittest.main()
