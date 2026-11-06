# SPDX-License-Identifier: Apache-2.0
"""Four blocks of ``init-firewall.sh``: the :53 allow rules, the GitHub ranges, the
allowed-domains loop and the host gateway allow.

Only the blocks run, lifted out of the script: the rest arms a firewall as root.
bash and jq run for real (the GitHub block's jq filter is under test); iptables,
ipset, curl, aggregate, dig and ip are stubbed as shell functions that log what
they are given or answer per domain or per case. The CI image has no
``aggregate``, so it is stubbed both as a passthrough and as failing, which covers
both arms of the script's ``||`` fallback.
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
SCRIPT = (ROOT / "sandbox-common" / "init-firewall.sh").read_text()

_BASH = shutil.which("bash")
_JQ = shutil.which("jq")
# Missing tools SKIP on a dev machine and FAIL under DOCKADE_REQUIRE_TOOLS, which CI
# sets — as in tests/test_launcher_lib.py.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))

_PRELUDE = "set -euo pipefail\nIFS=$'\\n\\t'\n"


def _lift(pattern: str, what: str) -> str:
    match = re.search(pattern, SCRIPT, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"init-firewall.sh no longer has the {what}")
    return match.group(0)


def _dns_block(resolv_conf: Path) -> str:
    block = _lift(r"^mapfile -t DNS_SERVERS .*?^done\n", "DNS allow block")
    swapped = block.replace("/etc/resolv.conf", str(resolv_conf))
    # Without the swap the result would depend on the machine running the test.
    if block.count("/etc/resolv.conf") != 1 or str(resolv_conf) not in swapped:
        raise AssertionError("the DNS allow block no longer reads /etc/resolv.conf once")
    return swapped


def _github_block() -> str:
    return _lift(r"^    _sane_cidr\(\) \{.*?"
                 r'^        echo "WARNING: could not fetch GitHub IP ranges"\n    fi\n',
                 "GitHub ranges block")


def _domain_loop() -> str:
    # Ends at the outer loop's `done` (4 spaces), past the inner `done <<< "$ips"`.
    return _lift(r'^    for domain in "\$\{ALLOWED_DOMAINS\[@\]\}"; do\n.*?^    done\n',
                 "allowed-domains loop")


def _gateway_block() -> str:
    return _lift(r'^if \[ "\$DIRECT_EGRESS" -eq 1 \]; then\n.*?^fi\n',
                 "host gateway block")


def _run(script: str, env: dict[str, str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
        [_BASH, "-c", _PRELUDE + script], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=60)


@unittest.skipUnless((_BASH and _JQ) or _STRICT, "bash or jq is not installed")
class DnsAllowTests(unittest.TestCase):

    def _rules(self, direct_egress: int, nameservers: list[str], upstream: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            resolv = Path(tmp) / "resolv.conf"
            resolv.write_text("".join(f"nameserver {ns}\n" for ns in nameservers)
                              + "options ndots:0\n")
            log = Path(tmp) / "iptables.log"
            stub = f'iptables() {{ local IFS=" "; echo "$*" >> {log}; }}\n'
            env = dict(os.environ, DIRECT_EGRESS=str(direct_egress), UPSTREAM_DNS=upstream)
            script = stub + _dns_block(resolv)
            proc = _run(script, env, tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return log.read_text().splitlines() if log.exists() else []

    @staticmethod
    def _expected(*addresses: str) -> list[str]:
        return [f"-A OUTPUT -p {proto} --dport 53 -d {ns} -j ACCEPT"
                for ns in addresses for proto in ("udp", "tcp")]

    def test_standalone_allows_each_resolver_once_in_first_seen_order(self):
        # sandbox-net's resolv.conf names 127.0.0.11, which the script also appends,
        # and an UPSTREAM_DNS that repeats an address must not double it either.
        # Its IPv6 and junk entries get no rule at all.
        rules = self._rules(1, ["127.0.0.11"], "192.0.2.53 2001:db8::53 198.51.100.53 not-an-ip 192.0.2.53")
        self.assertEqual(rules, self._expected("127.0.0.11", "192.0.2.53", "198.51.100.53"))

    def test_governed_allows_only_the_embedded_resolver(self):
        rules = self._rules(0, ["127.0.0.11"], "192.0.2.53")
        self.assertEqual(rules, self._expected("127.0.0.11"))


_V4_SANE = ["140.82.112.0/20", "192.30.252.0/22", "185.199.108.0/22"]
_V4_BAD = "10.0.0.0/8"
_V6 = ["2a0a:a440::/29", "2606:50c0::/32"]


@unittest.skipUnless((_BASH and _JQ) or _STRICT, "bash or jq is not installed")
class GithubRangesTests(unittest.TestCase):

    def _run_block(self, aggregate: str) -> tuple[list[str], str]:
        meta = {"web": [_V4_SANE[0], _V6[0]], "api": [_V4_SANE[1], _V4_BAD],
                "git": [_V4_SANE[2], _V6[1]]}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "meta.json").write_text(json.dumps(meta))
            log = Path(tmp) / "ipset.log"
            stubs = (f"curl() {{ cat {Path(tmp) / 'meta.json'}; }}\n"
                     f'ipset() {{ local IFS=" "; echo "$*" >> {log}; }}\n'
                     f"aggregate() {{ {aggregate}; }}\n")
            proc = _run(stubs + _github_block(), dict(os.environ), tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            added = log.read_text().splitlines() if log.exists() else []
            return added, proc.stdout

    def _check(self, aggregate: str) -> None:
        added, out = self._run_block(aggregate)
        self.assertEqual(sorted(added),
                         sorted(f"add allowed-domains {c}" for c in _V4_SANE))
        self.assertIn(f"skipping suspicious GitHub CIDR: {_V4_BAD}", out)
        for prefix in _V6:
            self.assertNotIn(prefix, out)

    def test_aggregate_passthrough_gets_only_ipv4(self):
        self._check("cat")

    def test_failing_aggregate_falls_back_to_ipv4_only(self):
        self._check("cat >/dev/null; return 1")


# A dig stub answering per domain with these stdout lines and this exit status. The
# failing domains come first, so the loop going on past them shows in what reaches
# ipset; the rc-9 one with an A row on stdout is the partial answer to discard.
_DIG = {
    "dead.example": ([], 9),
    "partial.example": (["partial.example. 60 IN A 203.0.113.9"], 9),
    "empty.example": ([], 0),
    # The CNAME's target passes the script's IPv4 check, so only the `$4 == "A"`
    # filter keeps it out of ipset.
    "good.example": (["good.example. 60 IN CNAME 203.0.113.77.",
                      "edge.example. 60 IN A 198.51.100.1",
                      "edge.example. 60 IN A not-an-ip",
                      "edge.example. 60 IN A 198.51.100.2"], 0),
}


def _dig_stub() -> str:
    arms = []
    for name, (rows, rc) in _DIG.items():
        echoes = "".join(f'echo "{row}"; ' for row in rows)
        arms.append(f"    {name}) {echoes}return {rc};;\n")
    # The domain is dig's last argument.
    return 'dig() {\n  case "${!#}" in\n' + "".join(arms) + "  esac\n}\n"


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class AllowedDomainsTests(unittest.TestCase):

    def test_a_failed_lookup_warns_and_the_loop_goes_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "ipset.log"
            stubs = (_dig_stub()
                     + f'ipset() {{ local IFS=" "; echo "$*" >> {log}; }}\n'
                     + f"ALLOWED_DOMAINS=({' '.join(_DIG)})\n")
            proc = _run(stubs + _domain_loop(), dict(os.environ), tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            added = log.read_text().splitlines() if log.exists() else []
        self.assertEqual(added, ["add allowed-domains 198.51.100.1",
                                 "add allowed-domains 198.51.100.2"])
        failed = ("WARNING: failed to resolve {} "
                  "(the lookup exited non-zero, usually no resolver replied)")
        self.assertEqual(proc.stdout.splitlines(), [
            failed.format("dead.example"),
            failed.format("partial.example"),
            "WARNING: failed to resolve empty.example",
        ])


# Reached only when the block lets the boot go on: the script after it still runs
# under set -e and pipefail.
_STRICT_AFTER = ('[[ $- == *e* && -o pipefail ]] || '
                 '{ echo "strict mode is off past the block" >&2; exit 97; }\n')
_DEFAULT_ROW = "default via 192.0.2.1 dev eth0"
_LOOKUP_FAILED = "WARNING: host gateway not allowed (the route lookup exited non-zero)"
_NO_ROUTE = "WARNING: host gateway not allowed (no IPv4 default route)"


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class HostGatewayTests(unittest.TestCase):

    def _run_block(self, rows: list[str], rc: int, direct_egress: int = 1):
        """Run the block with ``ip`` printing ``rows`` and returning ``rc``.

        Returns the iptables calls, stdout, and whether ``ip`` ran at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "iptables.log"
            ran = Path(tmp) / "ip.ran"
            # A file, not an environment variable: one string in the environment
            # is capped at 128 KiB, below what the long-table case needs. A real
            # `ip` dies of a SIGPIPE; the stub passes on cat's, since set -e is off
            # inside the script's `if !`.
            routes = Path(tmp) / "routes"
            routes.write_text("".join(f"{row}\n" for row in rows))
            stubs = (f'ip() {{ : > {ran}; cat {routes} || return; return "$IP_RC"; }}\n'
                     f'iptables() {{ local IFS=" "; echo "$*" >> {log}; }}\n')
            env = dict(os.environ, DIRECT_EGRESS=str(direct_egress), IP_RC=str(rc))
            proc = _run(stubs + _gateway_block() + _STRICT_AFTER, env, tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            calls = log.read_text().splitlines() if log.exists() else []
            return calls, proc.stdout, ran.exists()

    def test_a_failed_lookup_warns_and_allows_nothing(self):
        for rows in ([], [_DEFAULT_ROW]):
            with self.subTest(rows=rows):
                calls, out, _ = self._run_block(rows, 2)
                self.assertEqual(calls, [])
                self.assertEqual(out.splitlines(), [_LOOKUP_FAILED])

    def test_no_ipv4_default_route_warns_and_allows_nothing(self):
        for rows in (["192.0.2.0/24 dev eth0 proto kernel scope link src 192.0.2.5"],
                     ["default dev wg0 scope link"]):
            with self.subTest(rows=rows):
                calls, out, _ = self._run_block(rows, 0)
                self.assertEqual(calls, [])
                self.assertEqual(out.splitlines(), [_NO_ROUTE])

    def test_the_first_default_route_is_allowed_both_ways(self):
        calls, out, _ = self._run_block(
            ["192.0.2.0/24 dev eth0 proto kernel scope link src 192.0.2.5",
             _DEFAULT_ROW, "default via 198.51.100.1 dev eth1"], 0)
        self.assertEqual(calls, ["-A INPUT -s 192.0.2.1 -j ACCEPT",
                                 "-A OUTPUT -d 192.0.2.1 -j ACCEPT"])
        self.assertEqual(out, "")

    def test_a_long_route_table_after_the_default_row_is_read_to_the_end(self):
        # About 490 KB, well past a pipe's buffer plus awk's read block: an awk that
        # stopped at the default row would leave `ip` writing into a closed pipe,
        # and pipefail would fail the lookup on its SIGPIPE.
        filler = [f"198.51.100.{i % 256} dev eth0 proto kernel scope link src 192.0.2.5"
                  for i in range(8000)]
        calls, out, _ = self._run_block([_DEFAULT_ROW, *filler], 0)
        self.assertEqual(calls, ["-A INPUT -s 192.0.2.1 -j ACCEPT",
                                 "-A OUTPUT -d 192.0.2.1 -j ACCEPT"])
        self.assertEqual(out, "")

    def test_without_direct_egress_the_route_is_never_looked_up(self):
        calls, out, ran = self._run_block([_DEFAULT_ROW], 0, direct_egress=0)
        self.assertEqual((calls, out, ran), ([], "", False))


if __name__ == "__main__":
    unittest.main()
