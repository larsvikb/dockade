# SPDX-License-Identifier: Apache-2.0
"""Two blocks of ``init-firewall.sh``: the :53 allow rules and the GitHub ranges.

Only the blocks run, lifted out of the script: the rest arms a firewall as root.
bash and jq run for real (the GitHub block's jq filter is under test); iptables,
ipset, curl and aggregate are stubbed as shell functions that log what they are
given. The CI image has no ``aggregate``, so it is stubbed both as a passthrough and
as failing, which covers both arms of the script's ``||`` fallback.
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


if __name__ == "__main__":
    unittest.main()
