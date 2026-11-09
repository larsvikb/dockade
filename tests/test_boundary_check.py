# SPDX-License-Identifier: Apache-2.0
"""Checks of ``boundary-check.sh`` that must not pass on an input they cannot read.

Each block runs alone, lifted out of the script with the script's own ``ok``/``bad``
helpers and counters: the rest probes egress, DNS and mounts of a live sandbox. bash,
grep and awk run for real. The privilege-caps probe reads a fixture file in place of
the process's own status, and the embedded-resolver check meets stubbed ``getent`` and
``dig`` on a PATH of grep alone, so no verdict depends on the machine running the test.
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
SCRIPT = (ROOT / "sandbox-common" / "boundary-check.sh").read_text()

_BASH = shutil.which("bash")
# Missing tools SKIP on a dev machine and FAIL under DOCKADE_REQUIRE_TOOLS, which CI
# sets — as in tests/test_launcher_lib.py.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))

_STATUS = "/proc/self/status"
_ZERO = "0000000000000000"
_THREE_ZERO = f"CapInh:\t{_ZERO}\nCapPrm:\t{_ZERO}\nCapEff:\t{_ZERO}\nCapBnd:\t000001ffffffffff\nCapAmb:\t{_ZERO}\n"

_FAILED = "agent capabilities unreadable (the probe of its status file failed or found no Cap line)"
_EMPTY = "agent capabilities unreadable (no Cap sets read)"


def _lift(pattern: str, what: str) -> str:
    match = re.search(pattern, SCRIPT, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"boundary-check.sh no longer has the {what}")
    return match.group(0)


def _prelude() -> str:
    # The script's own options, lifted rather than restated: without pipefail a
    # probe that fails after printing reads as a success, and the block's verdict
    # rests on the script having no -e.
    return _lift(r"^set -uo pipefail\n", "`set -uo pipefail` line")


def _helpers() -> str:
    return _lift(r"^pass=0; fail=0\n.*?^info\(\) [^\n]*\n", "ok/bad helpers")


def _caps_block(status: Path) -> str:
    block = _lift(r"^# THE load-bearing property.*?(?=^# no-new-privileges)", "privilege-caps block")
    swapped = block.replace(_STATUS, str(status))
    # Without the swap the result would depend on the machine running the test.
    if block.count(_STATUS) != 1 or str(status) not in swapped:
        raise AssertionError(f"the privilege-caps block no longer reads {_STATUS} once")
    return swapped


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class CapsProbeTests(unittest.TestCase):

    def _run(self, status: str | None, stub: str = "") -> tuple[list[str], str]:
        """The block's PASS/FAIL lines and the counters after it, for a status file
        holding ``status`` (None: no file at all)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status"
            if status is not None:
                path.write_text(status)
            script = (_prelude() + _helpers() + stub + _caps_block(path)
                      + 'echo "pass=$pass fail=$fail"\n')
            proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
                [_BASH, "-c", script], cwd=tmp, env=dict(os.environ),
                capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        verdicts = [re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in lines[:-1]]
        return verdicts, lines[-1] if lines else ""

    def test_a_missing_status_file_fails_as_a_failed_probe(self):
        self.assertEqual(self._run(None), ([f"FAIL {_FAILED}"], "pass=0 fail=1"))

    def test_no_cap_lines_fails_as_a_failed_probe(self):
        self.assertEqual(self._run("Name:\tbash\nNoNewPrivs:\t1\n"),
                         ([f"FAIL {_FAILED}"], "pass=0 fail=1"))

    def test_cap_lines_with_no_value_fail_as_no_sets_read(self):
        self.assertEqual(self._run("CapPrm:\nCapEff:\nCapAmb:\n"),
                         ([f"FAIL {_EMPTY}"], "pass=0 fail=1"))

    def test_a_probe_that_fails_after_its_output_fails(self):
        # The real grep prints three zero sets, then the probe reports failure: what
        # it printed must not count as a reading.
        stub = 'grep() { command grep "$@"; return 3; }\n'
        self.assertEqual(self._run(_THREE_ZERO, stub), ([f"FAIL {_FAILED}"], "pass=0 fail=1"))

    def test_three_zero_sets_pass(self):
        self.assertEqual(self._run(_THREE_ZERO),
                         (["PASS agent holds no capabilities (Eff/Prm/Amb all zero)"],
                          "pass=1 fail=0"))

    def test_a_nonzero_set_fails_naming_the_sets(self):
        status = _THREE_ZERO.replace(f"CapEff:\t{_ZERO}", "CapEff:\t0000000000001000")
        self.assertEqual(self._run(status),
                         ([f"FAIL agent holds capabilities (Eff/Prm/Amb: {_ZERO} 0000000000001000 {_ZERO} )"],
                          "pass=0 fail=1"))


_GREP = shutil.which("grep")

_LEAK = ("external name resolved via the default resolver — embedded DNS is forwarding"
         " off an internal network (exfil channel)")
_NO_DIG = ("external names unresolvable, but dig is not installed to show the embedded"
           " resolver answering — inconclusive, not asserted")
_NO_ANSWER = ("external names unresolvable, but the embedded resolver did not answer"
              " either — inconclusive, not asserted")
_REFUSES = "embedded resolver refuses external names (no DNS forwarding off an internal network)"

_GETENT_FAILS = "getent() { return 2; }\n"
_GETENT_RESOLVES = 'getent() { echo "192.0.2.1      example.com"; }\n'
_DIG_SERVFAIL = 'dig() { echo ";; ->>HEADER<<- opcode: QUERY, status: SERVFAIL, id: 1"; }\n'
# What dig prints, and its exit status, when no server answers: no `status:` line.
_DIG_SILENT = 'dig() { echo ";; connection timed out; no servers could be reached"; return 9; }\n'

# The two modes that assert, each set alone: governed tier 1 and local tier 2.
_MODES = {"governed": {"HTTPS_PROXY": "http://proxy.invalid:3128"},
          "local": {"SANDBOX_MODE": "local"}}


def _resolver_block() -> str:
    return _lift(r"^# The probe above tests a resolver.*?(?=^echo$)", "embedded-resolver block")


@unittest.skipUnless((_BASH and _GREP) or _STRICT, "bash or grep is not installed")
class EmbeddedResolverTests(unittest.TestCase):
    """dig is the liveness half of the check: without it a failed lookup proves nothing."""

    def _run(self, mode: str, stubs: str) -> tuple[list[str], str]:
        """Every line the block prints and the counters after it, in ``mode``, with
        ``getent`` and ``dig`` only as ``stubs`` define them: the PATH holds grep alone,
        so the machine's own dig cannot answer for an absent one."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            os.symlink(_GREP, bin_dir / "grep")
            # Built, not copied: the sandbox running the test has HTTPS_PROXY set.
            env = {"PATH": str(bin_dir), **_MODES[mode]}
            script = (_prelude() + _helpers() + stubs + _resolver_block()
                      + 'echo "pass=$pass fail=$fail"\n')
            proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
                [_BASH, "-c", script], cwd=tmp, env=env,
                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        # Colour and the indent go, nothing else: a message's spacing, and the padding
        # that aligns info's marker with PASS, are part of what is asserted.
        verdicts = [re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in lines[:-1]]
        return verdicts, lines[-1] if lines else ""

    def _each_mode(self, stubs: str, expected: tuple[list[str], str]) -> None:
        for mode in _MODES:
            with self.subTest(mode=mode):
                self.assertEqual(self._run(mode, stubs), expected)

    def test_no_resolution_without_dig_is_inconclusive_not_a_pass(self):
        self._each_mode(_GETENT_FAILS, ([f"·    {_NO_DIG}"], "pass=0 fail=0"))

    def test_no_resolution_with_the_resolver_answering_passes(self):
        self._each_mode(_GETENT_FAILS + _DIG_SERVFAIL, ([f"PASS {_REFUSES}"], "pass=1 fail=0"))

    def test_no_resolution_with_the_resolver_silent_is_inconclusive(self):
        self._each_mode(_GETENT_FAILS + _DIG_SILENT, ([f"·    {_NO_ANSWER}"], "pass=0 fail=0"))

    def test_a_resolving_name_fails_without_dig(self):
        # The leak check comes first: an absent dig must not hide a leak.
        self._each_mode(_GETENT_RESOLVES, ([f"FAIL {_LEAK}"], "pass=0 fail=1"))


if __name__ == "__main__":
    unittest.main()
