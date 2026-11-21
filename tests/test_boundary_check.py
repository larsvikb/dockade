# SPDX-License-Identifier: Apache-2.0
"""Checks of ``boundary-check.sh`` that must not pass on an input they cannot read.

Each block runs alone, lifted out of the script with the script's own ``ok``/``bad``
helpers and counters: the rest probes egress, DNS and mounts of a live sandbox. bash,
grep and awk run for real. The privilege-caps probe reads a fixture file in place of
the process's own status, the embedded-resolver check meets stubbed ``getent`` and
``dig`` on a PATH of grep alone, and the model-host probe, the SNI-fronting branch
and the port-gate probe meet a stubbed ``curl``, and the proxy-close probe meets a
loopback server the test scripts, so no verdict depends on the machine running the test.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
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
_NOT_DECIDED = ("503", "000", 56)

_HELD_MSG = ("held for approval (no CONNECT reply): approve it with a persisted rule,"
             " since once or a lease passes only for now, and rerun")
_REFUSED_MSG = ("refused by the egress proxy (CONNECT 403): a deny rule, a human rejection,"
                " or a destination the relay guard forbids")
_NOT_DECIDED_MSG = ("not decided by the egress proxy (CONNECT 503): a hold cap reached, the"
                    " control plane unreachable, or an approval nobody answered; rerun later")


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

    def test_a_connect_403_is_a_policy_refusal(self):
        run = self._run("api.widgets.example", {"api.widgets.example": _REFUSED})
        self.assertEqual(run.verdicts, [f"FAIL model host api.widgets.example {_REFUSED_MSG}"])
        self.assertEqual(run.counters, "pass=0 fail=1")

    def test_a_connect_503_is_not_decided_and_says_rerun(self):
        # The proxy's refusal for a cap, an outage or an expired card: still a
        # failure, but one a rerun can clear, unlike the 403.
        run = self._run("api.widgets.example", {"api.widgets.example": _NOT_DECIDED})
        self.assertEqual(run.verdicts,
                         [f"FAIL model host api.widgets.example {_NOT_DECIDED_MSG}"])
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


def _sni_branch() -> str:
    return _lift(r'^    front="\$\(curl .*?domain-fronting leak\)"\n    fi\n',
                 "SNI-fronting branch")


@unittest.skipUnless((_BASH and _GREP) or _STRICT, "bash or grep is not installed")
class SniFrontingBranchTests(unittest.TestCase):
    """The fronting probe runs curl with -k and no -f, so a refusal at the re-gate is
    curl exit 0 and only the body tells it from a leak. A cap or an outage there is
    the proxy's 503, which must read as refused, and a widened match must not let a
    passthrough body through."""

    def _run(self, body: str, rc: int) -> tuple[list[str], str]:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            os.symlink(_GREP, bin_dir / "grep")
            stub = f"curl() {{ printf '%s' {shlex.quote(body)}; return {rc}; }}\n"
            script = (_prelude() + _helpers() + stub + _sni_branch()
                      + 'echo "pass=$pass fail=$fail"\n')
            proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
                [_BASH, "-c", script], cwd=tmp, env={"PATH": str(bin_dir)},
                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        verdicts = [re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in lines[:-1]]
        return verdicts, lines[-1] if lines else ""

    def test_the_policy_body_passes(self):
        self.assertEqual(self._run("egress denied by policy\n", 0),
                         (["PASS SNI fronting refused by proxy (deny-sni)"], "pass=1 fail=0"))

    def test_the_not_decided_body_passes(self):
        body = "egress not decided: no one answered the approval in time - retry later\n"
        self.assertEqual(self._run(body, 0), (
            ["PASS SNI fronting refused by proxy (re-gate not decided)"], "pass=1 fail=0"))

    def test_any_other_body_is_a_leak(self):
        for body in ("<html>example</html>", "egress", "not decided", ""):
            with self.subTest(body=body):
                self.assertEqual(self._run(body, 0), (
                    ["FAIL SNI fronting NOT refused — got a passthrough response"
                     " (domain-fronting leak)"], "pass=0 fail=1"))

    def test_a_curl_failure_passes(self):
        self.assertEqual(self._run("curl: (35) TLS connect error", 35),
                         (["PASS SNI fronting blocked (no passthrough)"], "pass=1 fail=0"))


def _port_gate_block() -> str:
    return _lift(r'^    portresp="\$\(curl .*?^    fi\n', "port-gate probe")


# curl -v's report of a refused CONNECT, as a governed sandbox prints it (CRs kept):
# `>` lines are curl's request, `<` lines the proxy's response.
_VERBOSE_HEAD = (
    "* Uses proxy env variable https_proxy == 'http://egress-proxy:8080'\n"
    "*   Trying 172.30.0.10:8080...\n"
    "* Establish HTTP proxy tunnel to api.anthropic.com:8443\n"
    "> CONNECT api.anthropic.com:8443 HTTP/1.1\r\n"
    "> Host: api.anthropic.com:8443\r\n"
    "> User-Agent: curl/8.14.1\r\n"
    "> Proxy-Connection: Keep-Alive\r\n"
    ">\r\n")
_VERBOSE_TAIL = (
    "<\r\n"
    "* CONNECT tunnel failed, response 403\n"
    "* closing connection #0\n"
    "curl: (56) CONNECT tunnel failed, response 403\n")


def _refused(*headers: str, status: str = "403 Forbidden") -> str:
    lines = [f"HTTP/1.1 {status}", "Content-Type: text/plain",
             "x-proxy-error: blocked-by-allowlist", *headers, "content-length: 24"]
    return _VERBOSE_HEAD + "".join(f"< {line}\r\n" for line in lines) + _VERBOSE_TAIL


_PORT_REFUSED_OK = ("PASS egress proxy refuses non-443 CONNECT (api.anthropic.com:8443 -> 403,"
              " Connection: close)")
_PORT_NO_403 = "FAIL egress proxy did NOT 403 a non-443 CONNECT to api.anthropic.com:8443"
_PORT_NO_CLOSE = "FAIL egress proxy 403'd a non-443 CONNECT without Connection: close"


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class PortGateProbeTests(unittest.TestCase):
    """The port gate's 403 must carry ``Connection: close``: it is the refusal a
    running proxy shows without a rule or a card, and without the close a Node
    script whose https.get the proxy refused never exits. Read from curl -v, whose
    request lines can say ``Connection`` too."""

    @staticmethod
    def _curl(output: str, rc: int) -> str:
        """A ``curl`` that prints ``output`` only when asked for the verbose report of
        the port-gate target, and otherwise what ``curl -sS`` prints of a refused
        CONNECT, which has no response lines: a probe that dropped ``-v`` or asked
        another port would then read no 403."""
        return (
            'curl() {\n'
            '    local arg verbose="" target=""\n'
            '    for arg in "$@"; do\n'
            '        case "$arg" in\n'
            '            -v) verbose=1 ;;\n'
            '            https://api.anthropic.com:8443/) target=1 ;;\n'
            '        esac\n'
            '    done\n'
            '    if [ -n "$verbose" ] && [ -n "$target" ]; then\n'
            f'        printf %s {shlex.quote(output)} >&2\n'
            '    else\n'
            "        echo 'curl: (56) CONNECT tunnel failed, response 403' >&2\n"
            '    fi\n'
            f'    return {rc}\n'
            '}\n')

    def _run(self, output: str, rc: int = 56) -> tuple[list[str], str]:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            for tool in ("grep", "head", "tail", "tr"):
                found = shutil.which(tool)
                if found is None:
                    if _STRICT:
                        self.fail(f"{tool} is not installed")
                    self.skipTest(f"{tool} is not installed")
                os.symlink(found, bin_dir / tool)
            script = (_prelude() + _helpers() + self._curl(output, rc) + _port_gate_block()
                      + 'echo "pass=$pass fail=$fail"\n')
            proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
                [_BASH, "-c", script], cwd=tmp, env={"PATH": str(bin_dir)},
                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        verdicts = [re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in lines[:-1]]
        return verdicts, lines[-1] if lines else ""

    def _fails(self, output: str, verdict: str, rc: int = 56) -> str:
        verdicts, counts = self._run(output, rc)
        self.assertEqual(counts, "pass=0 fail=1")
        self.assertEqual(len(verdicts), 1, verdicts)
        self.assertTrue(verdicts[0].startswith(verdict), verdicts)
        return verdicts[0]

    def test_a_403_with_connection_close_passes(self):
        for header in ("Connection: close", "connection: close", "CONNECTION:close"):
            with self.subTest(header=header):
                self.assertEqual(self._run(_refused(header)),
                                 ([_PORT_REFUSED_OK], "pass=1 fail=0"))

    def test_a_403_without_connection_close_fails_and_shows_the_response(self):
        for headers in ((), ("Connection: keep-alive",), ("X-Connection: close",),
                        ("Connection: closed",)):
            with self.subTest(headers=headers):
                verdict = self._fails(_refused(*headers), _PORT_NO_CLOSE)
                self.assertIn("< HTTP/1.1 403 Forbidden", verdict)
                self.assertNotIn("\r", verdict)
                self.assertNotIn("> CONNECT", verdict)

    def test_the_request_s_own_connection_header_does_not_count(self):
        # curl's request lines can name Connection: close; only the response's count.
        output = _refused().replace("> Proxy-Connection: Keep-Alive\r\n",
                                    "> Connection: close\r\n")
        self._fails(output, _PORT_NO_CLOSE)

    def test_no_403_status_fails_whatever_the_headers(self):
        cases = {
            "another status": _refused("Connection: close", status="502 Bad Gateway"),
            "403 only in curl's own lines": _VERBOSE_HEAD.replace(
                "HTTP/1.1\r\n", "HTTP/1.1 403\r\n")
            + "* CONNECT tunnel failed, response 403\n",
            "403 in a response header": _refused("Connection: close", status="200 OK")
            .replace("content-length: 24", "x-note: HTTP/1.1 403"),
            "no output": "",
        }
        for name, output in cases.items():
            with self.subTest(case=name):
                verdict = self._fails(output, _PORT_NO_403, rc=7)
                if output:
                    self.assertIn("got: ", verdict)


_WAIT = "timeout 5 "
_SHORT_WAIT = "timeout 1 "


def _close_block() -> str:
    block = _lift(r"^    # The close itself:.*?^    fi\n", "proxy-close probe")
    swapped = block.replace(_WAIT, _SHORT_WAIT)
    # Each held-open case costs the probe's whole wait, and no verdict depends on
    # its length.
    if block.count(_WAIT) != 1 or _SHORT_WAIT not in swapped:
        raise AssertionError(f"the proxy-close probe no longer waits with `{_WAIT.strip()}` once")
    return swapped


# What the probe must send, and nothing more: a `Connection` header or HTTP/1.0 would
# have mitmproxy close for the request's sake, whatever the refusal said.
_BARE_CONNECT = b"CONNECT api.anthropic.com:8443 HTTP/1.1\r\nHost: api.anthropic.com:8443\r\n\r\n"
_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\ncontent-type: text/plain\r\ncontent-length: 23\r\n"
_BODY = b"\r\negress denied by policy"

_CLOSES = "PASS egress proxy closes the connection after refusing a CONNECT"
_NO_ADDRESS = ("FAIL egress proxy close not probed: HTTPS_PROXY is set but EGRESS_PROXY_IP"
               " is not (a broken launch)")


def _kept_open(addr: str) -> str:
    return (f"FAIL egress proxy at {addr} 403'd a CONNECT but kept the connection open"
            " (a Node https.get it refuses would hang)")


def _no_403(addr: str) -> str:
    return f"FAIL egress proxy at {addr} did NOT 403 a raw CONNECT to api.anthropic.com:8443 (got: "


class _Proxy:
    """A loopback server that takes one connection, reads the request to its blank
    line, answers ``response``, then closes, resets or holds the connection until
    ``stop``."""

    def __init__(self, response: bytes, then: str, at: tuple[str, int] = ("127.0.0.1", 0)):
        self.response, self.then = response, then
        self.request = b""
        self._release = threading.Event()
        self._sock = socket.socket()
        try:
            self._sock.bind(at)
        except OSError:
            self._sock.close()
            raise
        self._sock.listen(1)
        self.addr = "%s:%d" % self._sock.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        # Polled, as closing the socket from another thread does not wake accept.
        self._sock.settimeout(0.1)
        while True:
            if self._release.is_set():  # stopped before the probe connected
                return
            try:
                conn, _ = self._sock.accept()
                break
            except TimeoutError:
                continue
        with conn:
            conn.settimeout(10)
            # Read it all: closing with the request unread would reset, not close.
            while b"\r\n\r\n" not in self.request:
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:  # a probe that connects and never sends
                    return
                if not chunk:
                    break
                self.request += chunk
            conn.sendall(self.response)
            if self.then == "reset":
                time.sleep(0.2)  # the 403 is read before the reset arrives
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            elif self.then == "hold":
                self._release.wait(30)

    def stop(self) -> None:
        self._release.set()
        self._sock.close()
        self._thread.join(30)


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class ProxyCloseProbeTests(unittest.TestCase):
    """The refusal's ``Connection: close`` matters only if the proxy then closes: a
    Node https.get the proxy refused never exits otherwise. curl drops a failed
    CONNECT's connection either way, so this probe reads the close over a raw
    socket, and must fail on a proxy that sends the header and keeps the connection."""

    def _run(self, env: dict[str, str]) -> tuple[list[str], str]:
        """The block's lines and counters under real bash, on a PATH of only the
        tools it calls."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            for tool in ("timeout", "cat", "head", "tr"):
                found = shutil.which(tool)
                if found is None:
                    if _STRICT:
                        self.fail(f"{tool} is not installed")
                    self.skipTest(f"{tool} is not installed")
                os.symlink(found, bin_dir / tool)
            script = (_prelude() + _helpers() + _close_block()
                      + 'echo "pass=$pass fail=$fail"\n')
            proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
                [_BASH, "-c", script], cwd=tmp, env={"PATH": str(bin_dir), **env},
                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        lines = proc.stdout.splitlines()
        verdicts = [re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in lines[:-1]]
        return verdicts, lines[-1] if lines else ""

    def _against(self, response: bytes, then: str) -> tuple[list[str], str, _Proxy]:
        proxy = _Proxy(response, then)
        self.addCleanup(proxy.stop)
        ip, port = proxy.addr.split(":")
        verdicts, counts = self._run({"EGRESS_PROXY_IP": ip, "EGRESS_PROXY_PORT": port})
        proxy.stop()
        return verdicts, counts, proxy

    def test_a_403_then_close_passes_and_the_request_is_bare(self):
        verdicts, counts, proxy = self._against(_FORBIDDEN + _BODY, "close")
        self.assertEqual((verdicts, counts), ([_CLOSES], "pass=1 fail=0"))
        self.assertEqual(proxy.request, _BARE_CONNECT)

    def test_a_403_then_reset_passes(self):
        # cat fails on the reset, neither 0 nor the timeout's 124: still closed.
        verdicts, counts, _ = self._against(_FORBIDDEN + _BODY, "reset")
        self.assertEqual((verdicts, counts), ([_CLOSES], "pass=1 fail=0"))

    def test_a_403_held_open_fails_as_kept_open_with_or_without_the_header(self):
        for name, head in (("Connection: close", _FORBIDDEN + b"Connection: close\r\n"),
                           ("no Connection header", _FORBIDDEN)):
            with self.subTest(case=name):
                verdicts, counts, proxy = self._against(head + _BODY, "hold")
                self.assertEqual((verdicts, counts),
                                 ([_kept_open(proxy.addr)], "pass=0 fail=1"))

    def test_any_other_status_line_fails_as_no_403_showing_it(self):
        cases = {
            "200": b"HTTP/1.1 200 Connection established\r\n\r\n",
            "502": b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\n\r\n",
            "401": b"HTTP/1.1 401 Unauthorized\r\n\r\n",
            "403 of no HTTP/1 version": b"HTTP/1.2 403 Forbidden\r\n\r\n",
            "403 below the status line": b"HTTP/1.1 200 OK\r\n\r\nHTTP/1.1 403 Forbidden\r\n",
            "403 after other text": b"x HTTP/1.1 403 Forbidden\r\n\r\n",
            "403 of another protocol": b"HTTP/2 403\r\n\r\n",
            "4030": b"HTTP/1.1 4030 Odd\r\n\r\n",
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                verdicts, counts, proxy = self._against(response, "close")
                first = response.split(b"\r\n", 1)[0].decode()
                self.assertEqual(counts, "pass=0 fail=1")
                self.assertEqual(len(verdicts), 1, verdicts)
                self.assertTrue(verdicts[0].startswith(_no_403(proxy.addr) + first), verdicts)

    def test_a_bare_403_status_line_passes(self):
        # No reason phrase: the status line ends at the code.
        verdicts, counts, _ = self._against(b"HTTP/1.0 403\r\n\r\n", "close")
        self.assertEqual((verdicts, counts), ([_CLOSES], "pass=1 fail=0"))

    def test_a_server_that_never_answers_fails_as_no_403_not_kept_open(self):
        verdicts, counts, proxy = self._against(b"", "hold")
        self.assertEqual((verdicts, counts),
                         ([_no_403(proxy.addr) + "nothing)"], "pass=0 fail=1"))

    def test_nothing_listening_fails_as_no_403_showing_the_connect_error(self):
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        started = time.monotonic()
        verdicts, counts = self._run({"EGRESS_PROXY_IP": "127.0.0.1",
                                      "EGRESS_PROXY_PORT": str(port)})
        self.assertLess(time.monotonic() - started, 1, "refused, not waited out")
        self.assertEqual(counts, "pass=0 fail=1")
        self.assertEqual(len(verdicts), 1, verdicts)
        self.assertTrue(verdicts[0].startswith(_no_403(f"127.0.0.1:{port}")), verdicts)
        self.assertIn("Connection refused", verdicts[0])

    def test_a_long_reply_shows_its_first_three_lines(self):
        response = (b"HTTP/1.1 502 Bad Gateway\r\ncontent-type: text/plain\r\n"
                    b"content-length: 7\r\n\r\nupstream\r\n")
        verdicts, counts, proxy = self._against(response, "close")
        self.assertEqual((verdicts, counts), ([
            _no_403(proxy.addr) + "HTTP/1.1 502 Bad Gateway content-type: text/plain"
            " content-length: 7)"], "pass=0 fail=1"))

    def test_the_port_defaults_to_the_proxy_s_own(self):
        # 8080, as init-firewall.sh opens it; unset, `set -u` must not abort the script.
        # A fresh loopback address each run: the server closes first, so it leaves
        # its address and port in TIME_WAIT for a minute, and a listener bound to
        # 127.0.0.1:8080 alone holds only that address. One bound to 0.0.0.0:8080
        # holds them all: the test then skips, or fails under DOCKADE_REQUIRE_TOOLS.
        ip = "127.%d.%d.%d" % (1 + secrets.randbelow(254), secrets.randbelow(256),
                               1 + secrets.randbelow(254))
        try:
            proxy = _Proxy(_FORBIDDEN + _BODY, "close", at=(ip, 8080))
        except OSError:
            if _STRICT:
                raise
            self.skipTest(f"{ip}:8080 is in use on this machine")
        self.addCleanup(proxy.stop)
        verdicts, counts = self._run({"EGRESS_PROXY_IP": ip})
        proxy.stop()
        self.assertEqual((verdicts, counts), ([_CLOSES], "pass=1 fail=0"))
        self.assertEqual(proxy.request, _BARE_CONNECT)

    def test_no_proxy_address_fails_naming_the_variable(self):
        for env in ({}, {"EGRESS_PROXY_IP": ""}):
            with self.subTest(env=env):
                self.assertEqual(self._run(env), ([_NO_ADDRESS], "pass=0 fail=1"))


if __name__ == "__main__":
    unittest.main()
