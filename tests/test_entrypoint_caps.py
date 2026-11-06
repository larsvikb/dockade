# SPDX-License-Identifier: Apache-2.0
"""The entrypoint's capability assertion, run with the probe's output and status faked.

Only the block is run, lifted out with ``gosu`` stubbed to print fixture Cap lines
and return a fixture status: the rest arms a firewall as root. awk runs for real.
The check is the containment assertion, so every way the probe can come back
without a readable all-zero answer must end in a FATAL and exit 1.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = (ROOT / "sandbox-common" / "entrypoint.sh").read_text()

_BASH = shutil.which("bash")
# Missing tools SKIP on a dev machine and FAIL under DOCKADE_REQUIRE_TOOLS, which CI
# sets — as in tests/test_launcher_lib.py.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))

_ZERO = "0000000000000000"
_PROBE_FAILED = "FATAL: could not read the sandbox user's Linux capabilities (the probe exited non-zero)."
_NO_CAP_SET = "FATAL: could not read the sandbox user's Linux capabilities (the probe read no Cap set)."
_HOLDS_CAPS = "FATAL: sandbox user would start with Linux capabilities"
# Reached only when the block lets the boot go on: the script after it still runs
# under set -e and pipefail.
_STRICT_AFTER = ('[[ $- == *e* && -o pipefail ]] || '
                 '{ echo "strict mode is off past the block" >&2; exit 97; }\n')


def _caps_block() -> str:
    match = re.search(r"^# Load-bearing invariant: .*?^done\n",
                      ENTRYPOINT, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError("entrypoint.sh no longer has the capability assertion")
    return match.group(0)


def _cap_lines(*values: str) -> str:
    return "".join(f"Cap{name}:\t{value}\n"
                   for name, value in zip(("Prm", "Eff", "Amb"), values))


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class CapabilityAssertionTests(unittest.TestCase):

    def _run(self, out: str, rc: int) -> subprocess.CompletedProcess:
        env = dict(os.environ, USERNAME="sandbox", PROBE_OUT=out, PROBE_RC=str(rc))
        script = ('set -euo pipefail\n'
                  'gosu() { printf "%s" "$PROBE_OUT"; return "$PROBE_RC"; }\n'
                  + _caps_block() + _STRICT_AFTER)
        return subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_BASH, "-c", script], env=env, capture_output=True, text=True, timeout=60)

    def _fatals(self, proc: subprocess.CompletedProcess) -> list[str]:
        return [line for line in proc.stderr.splitlines() if line.startswith("FATAL:")]

    def test_a_probe_failing_as_grep_on_no_match_is_fatal(self):
        proc = self._run("", 1)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self._fatals(proc), [_PROBE_FAILED])

    def test_a_failing_probe_is_fatal_whatever_it_printed(self):
        proc = self._run(_cap_lines(_ZERO), 3)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self._fatals(proc), [_PROBE_FAILED])

    def test_a_probe_reading_no_cap_set_is_fatal(self):
        proc = self._run("", 0)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self._fatals(proc), [_NO_CAP_SET])

    def test_three_zero_sets_pass_quietly(self):
        proc = self._run(_cap_lines(_ZERO, _ZERO, _ZERO), 0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")

    def test_a_nonzero_set_is_fatal(self):
        proc = self._run(_cap_lines(_ZERO, "0000000000001000", _ZERO), 0)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        fatals = self._fatals(proc)
        self.assertEqual(len(fatals), 1, proc.stderr)
        self.assertTrue(fatals[0].startswith(_HOLDS_CAPS), proc.stderr)


if __name__ == "__main__":
    unittest.main()
