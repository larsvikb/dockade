# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the decision functions in ``sandbox-lib.sh``, run under bash.

``tests/test_sandbox_wiring.py`` text-parses the launchers, and says why: they arm a
firewall and start containers, so running them is what ``make check-boundary`` does on
a host. That reasoning is about the *scripts*. It does not reach the handful of
functions in ``sandbox-lib.sh`` that only decide — they take arguments, ask ``docker``
a question, and either return or refuse. Nothing is armed and nothing is launched, so
the decision can be exercised directly with a stub on ``PATH``, and a stub is the only
way to reach the branch that matters: the answer depends on the *state of the host's
networks*, which no amount of reading the source can vary.

Why that is worth the machinery. ``sc_ensure_network`` is where tier 2's "no egress"
stops being a claim and becomes a check, and both of its failure modes are silent —
adopting a network that should have been refused launches a sandbox that looks correct
and has an egress route, and a refusal that fires when it should not simply means the
launcher never runs. Reading the function tells you which branch was written; only
running it tells you which branch a given host takes.

The stub records every ``docker`` invocation, so "it refused" is asserted together with
"it created nothing" — the halves are separable, and a refusal that has already created
the bridge would be the worse bug.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "sandbox-lib.sh"

_BASH = shutil.which("bash")
# Missing bash SKIPS on a dev machine and FAILS under DOCKADE_REQUIRE_TOOLS, which CI
# sets — the same bargain tests/test_control_plane_ui_js.py strikes for node.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))

# A `docker` that answers from the environment instead of from the host, and logs what
# it was asked. Deliberately NOT `set -u`: it is standing in for a real binary, which
# tolerates being called with any argv, and a stub that dies on an unexpected call
# would report the launcher's bug as its own.
_DOCKER_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$STUB_LOG"
if [ "$1 $2" = "network inspect" ]; then
    [ "$STUB_NET_EXISTS" = "true" ] || exit 1
    for arg in "$@"; do
        if [ "$arg" = "{{.Internal}}" ]; then
            printf '%s\n' "$STUB_NET_INTERNAL"
            exit 0
        fi
    done
    printf '[]\n'
    exit 0
fi
exit 0
"""

# `set -euo pipefail` is what both launchers set before sourcing the library, and it is
# load-bearing for this function: under `set -e` a bare `[[ cond ]] && return 0` that
# takes the false branch fails the whole list and kills the launcher outright. Running
# the tests under anything laxer would pass while the real thing exits 1.
_HARNESS = 'set -euo pipefail; source "$1"; sc_ensure_network "$2" "$3"'


class _Run(NamedTuple):
    status: int
    stderr: str
    docker_calls: list[str]


def _ensure_network(*, exists: bool, internal: bool, allow_fallback: bool) -> _Run:
    """Run ``sc_ensure_network`` against a host whose networks are what we say."""
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "docker"
        stub.write_text(_DOCKER_STUB)
        stub.chmod(0o755)
        log = Path(tmp) / "calls.log"
        log.touch()

        env = dict(os.environ)
        env.update(
            PATH=f"{tmp}{os.pathsep}{env['PATH']}",
            STUB_LOG=str(log),
            STUB_NET_EXISTS="true" if exists else "false",
            STUB_NET_INTERNAL="true" if internal else "false",
        )
        proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_BASH, "-c", _HARNESS, "sc_ensure_network", str(LIB), "sandbox-net",
             "true" if allow_fallback else "false"],
            capture_output=True, text=True, env=env, timeout=60)
        return _Run(proc.returncode, proc.stderr, log.read_text().splitlines())


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class EnsureNetworkTests(unittest.TestCase):
    """What tier 2 accepts, and what tier 1 is still allowed to do."""

    def test_a_non_internal_network_is_refused_for_a_tier_with_no_fallback(self):
        # The finding this exists for. A tier-1 standalone run leaves `sandbox-net`
        # behind as a plain bridge with real egress; it is indistinguishable by name
        # from the compose-owned internal one, so a tier-2 launch that checked only
        # existence would adopt it and quietly gain the egress its design forbids.
        run = _ensure_network(exists=True, internal=False, allow_fallback=False)
        self.assertNotEqual(run.status, 0, f"adopted a non-internal network\n{run.stderr}")
        self.assertIn("NOT internal", run.stderr)

    def test_the_refusal_says_how_to_recover(self):
        # The state is invisible from the error the human would otherwise get (none —
        # the launcher would succeed), so the message carries the remedy.
        run = _ensure_network(exists=True, internal=False, allow_fallback=False)
        self.assertIn("docker network rm sandbox-net", run.stderr)

    def test_an_internal_network_is_accepted_for_a_tier_with_no_fallback(self):
        run = _ensure_network(exists=True, internal=True, allow_fallback=False)
        self.assertEqual(run.status, 0, run.stderr)

    def test_a_missing_network_is_still_refused_for_a_tier_with_no_fallback(self):
        # The original refusal, kept under test because the internal check was added
        # ahead of it and an early `return 0` would swallow this case.
        run = _ensure_network(exists=False, internal=False, allow_fallback=False)
        self.assertNotEqual(run.status, 0)
        self.assertIn("not found", run.stderr)

    def test_no_tier_without_fallback_ever_creates_a_network(self):
        # Both refusals, asserted on the side effect rather than on the exit code:
        # refusing after creating the bridge would leave the next launch to adopt it.
        for exists, internal in ((False, False), (True, False)):
            with self.subTest(exists=exists):
                run = _ensure_network(
                    exists=exists, internal=internal, allow_fallback=False)
                self.assertFalse(
                    [c for c in run.docker_calls if c.startswith("network create")],
                    "refused, but created the network anyway")

    def test_tier_one_still_creates_the_bridge_when_there_is_none(self):
        # The standalone mode the fallback exists for: degraded, direct, firewall-only
        # egress is a supported tier-1 configuration and must keep working.
        run = _ensure_network(exists=False, internal=False, allow_fallback=True)
        self.assertEqual(run.status, 0, run.stderr)
        self.assertIn("network create sandbox-net", run.docker_calls)

    def test_tier_one_accepts_a_non_internal_network_it_left_behind(self):
        # The same leftover bridge tier 2 refuses is tier 1 reattaching to its own
        # standalone mode. The check is asymmetric on purpose, so assert the half that
        # would be easy to over-tighten.
        run = _ensure_network(exists=True, internal=False, allow_fallback=True)
        self.assertEqual(run.status, 0, run.stderr)
        self.assertFalse(
            [c for c in run.docker_calls if c.startswith("network create")])

    def test_tier_one_accepts_the_compose_owned_internal_network(self):
        run = _ensure_network(exists=True, internal=True, allow_fallback=True)
        self.assertEqual(run.status, 0, run.stderr)



_GUARD_HARNESS = ('set -euo pipefail; source "$1"; sc_guard_workspace "$2"; '
                  'printf "%s\\n" "$SC_WORKSPACE"')
_MARKETPLACE_HARNESS = 'set -euo pipefail; source "$1"; sc_marketplaces'


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class WorkspaceGuardTests(unittest.TestCase):
    """``sc_guard_workspace`` on a host laid out as we say.

    The Windows half is S30: on WSL the user's Windows profile is mounted at
    ``/mnt/c/Users/<name>``, far from ``$HOME``, so the home-directory checks never
    saw it. The profile is recognised by its registry hive, so the tree below is a
    fake one in a temp directory — which is also why the rule cannot depend on
    WSL being the host."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home" / "alice"
        self.home.mkdir(parents=True)
        self.drive = self.root / "mnt" / "c"
        self.profile = self.drive / "Users" / "Alice"
        self.project = self.profile / "source" / "app"
        self.project.mkdir(parents=True)
        (self.profile / "NTUSER.DAT").touch()

    def guard(self, workspace, **env_over):
        env = {k: v for k, v in os.environ.items() if k != "ALLOW_UNSAFE_WORKSPACE"}
        env.update(HOME=str(self.home), **env_over)
        return subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_BASH, "-c", _GUARD_HARNESS, "sc_guard_workspace", str(LIB),
             str(workspace)], capture_output=True, text=True, env=env, timeout=60)

    def assertRefused(self, proc, why):
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("REFUSING to mount workspace", proc.stderr)
        self.assertIn(why, proc.stderr)

    def test_a_windows_profile_is_refused_like_a_home_directory(self):
        self.assertRefused(self.guard(self.profile), "that is a Windows user profile")

    def test_every_directory_holding_a_profile_is_refused_and_names_it(self):
        # The Users folder, the drive root, and the mount root holding the drives —
        # the three places a WSL user might reasonably `cd` to and launch from.
        for workspace in (self.drive / "Users", self.drive, self.drive.parent):
            with self.subTest(workspace=workspace):
                self.assertRefused(self.guard(workspace), f"({self.profile})")

    def test_a_project_inside_a_profile_is_allowed(self):
        # The ordinary WSL layout, like a project under $HOME: the guard refuses the
        # profile, not everything in it.
        proc = self.guard(self.project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(self.project))

    def test_the_override_still_overrides(self):
        proc = self.guard(self.profile, ALLOW_UNSAFE_WORKSPACE="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_the_home_directory_rules_still_hold(self):
        # Untested until now, and the Windows rule was added beside them.
        self.assertRefused(self.guard(self.home), "that is your home directory")
        self.assertRefused(self.guard(self.home.parent), "your home directory")
        self.assertRefused(self.guard(Path("/")), "that is the filesystem root")

    def test_an_ordinary_directory_is_allowed_and_resolved(self):
        plain = self.root / "work" / "app"
        plain.mkdir(parents=True)
        link = self.root / "link"
        link.symlink_to(plain)
        proc = self.guard(link)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(plain))

    def test_the_marketplaces_mount_is_held_to_the_same_rule(self):
        # Its guard says it refuses what this one refuses; read-only stops the agent
        # writing a profile, not reading one.
        env = dict(os.environ, HOME=str(self.home),
                   SANDBOX_MARKETPLACES_DIR=str(self.profile))
        proc = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_BASH, "-c", _MARKETPLACE_HARNESS, "sc_marketplaces", str(LIB)],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("REFUSING to mount marketplaces", proc.stderr)
        self.assertIn("that is a Windows user profile", proc.stderr)


if __name__ == "__main__":
    unittest.main()
