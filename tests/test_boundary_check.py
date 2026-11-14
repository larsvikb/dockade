# SPDX-License-Identifier: Apache-2.0
"""Checks of ``boundary-check.sh`` that must not pass on an input they cannot read.

Each block runs alone, lifted out of the script with the script's own ``ok``/``bad``
helpers and counters: the rest probes egress, DNS and mounts of a live sandbox. bash,
grep and awk run for real. The privilege-caps probe reads a fixture file in place of
the process's own status, the embedded-resolver check meets stubbed ``getent`` and
``dig`` on a PATH of grep alone, and the model-host probe meets a stubbed ``curl`` on
an empty PATH, so no verdict depends on the machine running the test.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple

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

# The two modes that assert, each set alone: a governed cloud tier and local mode.
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


_LIFELINE = "the lifeline every cloud tier reaches unheld"
_GOVERNED_IF = 'if [ -n "${HTTPS_PROXY:-}" ]; then\n    # Since 2b'


def _model_host_block() -> str:
    block = _lift(r"^# A cloud tier's OWN model hosts.*?(?=^# Governed mode, the cloud tiers)",
                  "model-host block")
    start = SCRIPT.index(block)
    lifeline = SCRIPT.find(_LIFELINE)
    if lifeline == -1 or lifeline > start:
        raise AssertionError("the model-host block no longer follows the lifeline probe")
    # Between the block and the governed section's `if` there is only its comment, so
    # no probe of that section runs first and holds a card the caps would count.
    between = SCRIPT[start + len(block):SCRIPT.index(_GOVERNED_IF)]
    if any(ln.strip() and not ln.lstrip().startswith("#") for ln in between.splitlines()):
        raise AssertionError("the model-host block no longer sits just before the governed section")
    return block


# The block's three modes, each built alone: the sandbox running the test has
# HTTPS_PROXY set, and a branch that FAILed outside governed mode without first
# checking the list was non-empty would break every standalone and local check.
_PROXY = "http://proxy.invalid:3128"
_HOST_MODES = {"governed": {"HTTPS_PROXY": _PROXY},
               "local": {"SANDBOX_MODE": "local"},
               "standalone": {}}

# What curl gives the block for each outcome, as measured from a governed sandbox:
# the CONNECT code, the HTTP code, and curl's exit status.
_REACHED = ("200", "404", 0)
_HELD = ("000", "000", 28)
_REFUSED = ("403", "000", 56)

_HELD_MSG = ("held for approval (no CONNECT reply): approve it with a persisted rule,"
             " since once or a lease passes only for now, and rerun")
_REFUSED_MSG = ("refused by the egress proxy (CONNECT 403): a deny rule, the control plane"
                " failing closed, a pending-hold cap reached (rerun once pending cards clear),"
                " or a destination the relay guard forbids")


def _curl_stub(answers: dict[str, tuple[str, str, int]]) -> str:
    """A ``curl`` answering each ``https://<host>/`` as ``answers`` says, logging its
    argv one call per line with the words joined by _SEP; a host it was not told
    about fails as unresolvable. It writes the codes where its ``-w`` names them, as
    curl does, so a block that swapped the two would read them swapped."""
    cases = "".join(f"        {shlex.quote(host)}) c={c} h={h} rc={rc} ;;\n"
                    for host, (c, h, rc) in answers.items())
    return ('curl() {\n'
            '    local IFS=$\'\\x1f\' url="${*: -1}" host fmt="" prev="" arg c h rc\n'
            '    printf \'%s\\n\' "$*" >> "$CURL_LOG"\n'
            '    for arg in "$@"; do [ "$prev" = "-w" ] && fmt="$arg"; prev="$arg"; done\n'
            '    host="${url#https://}"; host="${host%/}"\n'
            '    case "$host" in\n'
            f'{cases}'
            "        *) c=000 h=000 rc=6 ;;\n"
            '    esac\n'
            "    local pc='%{http_connect}' ph='%{http_code}'\n"
            '    fmt=${fmt//"$pc"/$c}\n'
            '    printf \'%s\' "${fmt//"$ph"/$h}"\n'
            '    return "$rc"\n'
            '}\n')


_SEP = "\x1f"


class _HostRun(NamedTuple):
    verdicts: list[str]
    counters: str
    curl_calls: list[str]


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class ModelHostProbeTests(unittest.TestCase):
    """Each cloud tier's own model hosts, asserted through the proxy, a held one failing."""

    def _run(self, hosts: str | None, answers: dict[str, tuple[str, str, int]],
             mode: str = "governed", files: tuple[str, ...] = ()) -> _HostRun:
        """The block's lines and counters with SANDBOX_MODEL_HOSTS as ``hosts`` (None:
        unset), in ``mode``, on an empty PATH so only the stub can answer for curl."""
        with tempfile.TemporaryDirectory() as tmp:
            for name in files:
                (Path(tmp) / name).touch()
            log = Path(tmp) / "curl.log"
            log.touch()
            env = {"PATH": str(Path(tmp) / "no-bin"), "CURL_LOG": str(log),
                   **_HOST_MODES[mode]}
            if hosts is not None:
                env["SANDBOX_MODEL_HOSTS"] = hosts
            script = (_prelude() + _helpers() + _curl_stub(answers) + _model_host_block()
                      + 'echo "pass=$pass fail=$fail"\n')
            proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
                [_BASH, "-c", script], cwd=tmp, env=env,
                capture_output=True, text=True, encoding="utf-8", timeout=60)
            calls = log.read_text().splitlines()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        lines = proc.stdout.splitlines()
        verdicts = [re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in lines[:-1]]
        return _HostRun(verdicts, lines[-1] if lines else "", calls)

    def test_an_http_response_from_the_host_passes(self):
        run = self._run("api.widgets.example", {"api.widgets.example": _REACHED})
        self.assertEqual(run.verdicts, [
            f"PASS model host api.widgets.example reachable via proxy {_PROXY} (HTTP 404)"])
        self.assertEqual(run.counters, "pass=1 fail=0")

    def test_the_probe_goes_through_the_ambient_proxy_bounded(self):
        # The exact call: the verdicts rest on the codes arriving in this order, and
        # on the body going nowhere so the codes are all that is read.
        run = self._run("api.widgets.example", {"api.widgets.example": _REACHED})
        self.assertEqual([c.split(_SEP) for c in run.curl_calls], [[
            "-sS", "-o", "/dev/null", "--connect-timeout", "5", "--max-time", "8",
            "-w", "%{http_connect} %{http_code}", "https://api.widgets.example/"]])

    def test_a_response_whose_body_then_stalls_passes(self):
        # Headers arrived, then --max-time fired: the host answered.
        run = self._run("api.widgets.example", {"api.widgets.example": ("200", "200", 28)})
        self.assertEqual(run.verdicts, [
            f"PASS model host api.widgets.example reachable via proxy {_PROXY} (HTTP 200)"])
        self.assertEqual(run.counters, "pass=1 fail=0")

    def test_a_timeout_with_no_connect_reply_is_held(self):
        run = self._run("api.widgets.example", {"api.widgets.example": _HELD})
        self.assertEqual(run.verdicts, [f"FAIL model host api.widgets.example {_HELD_MSG}"])
        self.assertEqual(run.counters, "pass=0 fail=1")

    def test_a_connect_403_is_refused_naming_the_caps(self):
        run = self._run("api.widgets.example", {"api.widgets.example": _REFUSED})
        self.assertEqual(run.verdicts, [f"FAIL model host api.widgets.example {_REFUSED_MSG}"])
        self.assertEqual(run.counters, "pass=0 fail=1")

    def test_failures_past_the_hold_decision_are_unreachable(self):
        # Neither held nor refused: the proxy answered the CONNECT, or curl never
        # got as far as asking.
        for answer in (("200", "000", 28), ("000", "000", 7), ("502", "000", 56)):
            connect, _, rc = answer
            with self.subTest(answer=answer):
                run = self._run("api.widgets.example", {"api.widgets.example": answer})
                self.assertEqual(run.verdicts, [
                    f"FAIL model host api.widgets.example unreachable through the egress proxy"
                    f" (curl exit {rc}, CONNECT {connect})"])
                self.assertEqual(run.counters, "pass=0 fail=1")

    def test_several_hosts_each_get_their_own_verdict_in_order(self):
        run = self._run("  a.widgets.example\tb.widgets.example  c.widgets.example ",
                        {"a.widgets.example": _HELD, "b.widgets.example": _REACHED,
                         "c.widgets.example": _REFUSED})
        self.assertEqual(run.verdicts, [
            f"FAIL model host a.widgets.example {_HELD_MSG}",
            f"PASS model host b.widgets.example reachable via proxy {_PROXY} (HTTP 404)",
            f"FAIL model host c.widgets.example {_REFUSED_MSG}"])
        self.assertEqual(run.counters, "pass=1 fail=2")

    def test_an_empty_or_unset_list_probes_and_prints_nothing_in_every_mode(self):
        for mode in _HOST_MODES:
            for hosts in (None, "", "  "):
                with self.subTest(mode=mode, hosts=hosts):
                    self.assertEqual(self._run(hosts, {}, mode),
                                     _HostRun([], "pass=0 fail=0", []))

    def test_a_list_outside_governed_mode_is_one_failure_and_no_probe(self):
        for mode in ("local", "standalone"):
            with self.subTest(mode=mode):
                run = self._run("a.widgets.example b.widgets.example", {}, mode)
                self.assertEqual(run.verdicts, [
                    "FAIL SANDBOX_MODEL_HOSTS names a.widgets.example b.widgets.example, but no"
                    " egress proxy is set to probe them through (governed mode only)"])
                self.assertEqual(run.counters, "pass=0 fail=1")
                self.assertEqual(run.curl_calls, [])

    def test_the_list_is_split_without_globbing(self):
        # Globbed, `*` would become the file's name and probe that instead.
        run = self._run("*", {"*": _REACHED}, files=("widgets.txt",))
        self.assertEqual(run.verdicts, [
            f"PASS model host * reachable via proxy {_PROXY} (HTTP 404)"])
        self.assertEqual(run.curl_calls[0].split(_SEP)[-1], "https://*/")


if __name__ == "__main__":
    unittest.main()
