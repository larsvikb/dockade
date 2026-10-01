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
import socket
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
        env = {k: v for k, v in os.environ.items()
               if k not in ("ALLOW_UNSAFE_WORKSPACE", "XDG_RUNTIME_DIR")}
        env.update(HOME=str(self.home))
        env.update(env_over)
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

    def test_the_runtime_directories_are_refused(self):
        # A host socket there is as reachable read-write as it is read-only.
        runtime = self.root / "run-user"
        (runtime / "bus-dir").mkdir(parents=True)
        # This fixture's home lives under /tmp, which would refuse /tmp and the
        # fixture root for a different reason; a home elsewhere isolates the rule.
        elsewhere = {"HOME": "/nonexistent-home"}
        cases = [(Path("/tmp"), "that is /tmp", elsewhere),  # noqa: S108 (/tmp itself is the rule under test)
                 (runtime, f"that is {runtime}", {}),
                 (runtime / "bus-dir", f"that is {runtime}", {}),
                 (self.root, f"{runtime}, where", elsewhere)]
        for fixed in (Path("/run"), Path("/var/run")):
            if fixed.is_dir():
                cases.append((fixed, f"that is {fixed.resolve()}", {}))
        for workspace, why, env in cases:
            with self.subTest(workspace=workspace):
                self.assertRefused(self.guard(workspace, XDG_RUNTIME_DIR=str(runtime), **env), why)
        # Unlike /tmp, whose children are ordinary scratch: one is allowed as usual.
        scratch = Path("/tmp") / f"dockade-workspace-test-{os.getpid()}"  # noqa: S108 (a child of /tmp is the point)
        scratch.mkdir()
        self.addCleanup(scratch.rmdir)
        proc = self.guard(scratch)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(scratch.resolve()))

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


_REFS_HARNESS = ('set -euo pipefail; source "$1"; sc_refs; '
                 '[ ${#SC_REFS_ARGS[@]} -eq 0 ] || printf "%s\\n" "${SC_REFS_ARGS[@]}"')


@unittest.skipUnless(_BASH or _STRICT, "bash is not installed")
class RefsTests(unittest.TestCase):
    """``sc_refs`` turning ``SANDBOX_REFS`` into read-only ``/refs/<name>`` mounts.

    Every entry is something the operator named, so each failure is asserted to be
    a refusal with the entry in it — a skipped entry would launch a sandbox that
    looks right and is missing what the agent was told to read."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home" / "alice"
        self.home.mkdir(parents=True)
        self.config = self.root / "config"
        (self.config / "secrets").mkdir(parents=True)
        self.src = self.root / "src"
        for d in ("tools/dockade", "games/platformer", "a/docs", "b/docs"):
            (self.src / d).mkdir(parents=True)

    def refs(self, value, **env_over):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        env.update(HOME=str(self.home), SANDBOX_REFS=value,
                   DOCKADE_CONFIG_HOME=str(self.config))
        env.update(env_over)
        return subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
            [_BASH, "-c", _REFS_HARNESS, "sc_refs", str(LIB)],
            capture_output=True, text=True, env=env, timeout=60)

    def mounts(self, value):
        """The ``-v`` operands, after asserting every other word is ``-v``."""
        proc = self.refs(value)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = proc.stdout.splitlines()
        self.assertEqual(args[::2], ["-v"] * (len(args) // 2), args)
        return args[1::2]

    def assertRefused(self, value, why):
        proc = self.refs(value)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("REFUSING to mount ref", proc.stderr)
        self.assertIn(why, proc.stderr)

    def test_unset_mounts_nothing(self):
        self.assertEqual(self.mounts(""), [])

    def test_each_path_mounts_read_only_under_its_basename(self):
        value = f"{self.src}/tools/dockade,{self.src}/games/platformer/"
        self.assertEqual(self.mounts(value), [
            f"{self.src}/tools/dockade:/refs/dockade:ro",
            f"{self.src}/games/platformer:/refs/platformer:ro",
        ])

    def test_an_explicit_name_wins(self):
        self.assertEqual(self.mounts(f"docs-b={self.src}/b/docs"),
                         [f"{self.src}/b/docs:/refs/docs-b:ro"])

    def test_two_entries_with_one_name_are_refused_not_suffixed(self):
        self.assertRefused(f"{self.src}/a/docs,{self.src}/b/docs",
                           "/refs/docs is already taken")
        self.assertEqual(len(self.mounts(f"{self.src}/a/docs,b={self.src}/b/docs")), 2)

    def test_blank_entries_and_padding_are_ignored(self):
        self.assertEqual(self.mounts(f" {self.src}/tools/dockade , ,"),
                         [f"{self.src}/tools/dockade:/refs/dockade:ro"])

    def test_a_quoted_tilde_means_home(self):
        (self.home / "notes").mkdir()
        self.assertEqual(self.mounts("~/notes"), [f"{self.home}/notes:/refs/notes:ro"])

    def test_a_symlink_mounts_its_target_under_the_target_name(self):
        link = self.root / "link"
        link.symlink_to(self.src / "tools" / "dockade")
        self.assertEqual(self.mounts(str(link)),
                         [f"{self.src}/tools/dockade:/refs/dockade:ro"])

    def test_a_missing_directory_is_refused(self):
        self.assertRefused(f"{self.src}/nope", "is not a directory")

    def test_the_read_only_mount_rules_hold(self):
        profile = self.root / "mnt" / "c" / "Users" / "Alice"
        profile.mkdir(parents=True)
        (profile / "NTUSER.DAT").touch()
        for value, why in ((str(self.home), "that is your home directory"),
                           (str(self.home.parent), "your home directory"),
                           ("/", "that is the filesystem root"),
                           (str(self.config), "the MCP secrets directory"),
                           (str(profile), "that is a Windows user profile")):
            with self.subTest(value=value):
                self.assertRefused(value, why)

    def test_a_name_docker_or_the_agent_cannot_use_is_refused(self):
        odd = self.src / "has space"
        odd.mkdir()
        self.assertRefused(str(odd), "not a usable name under /refs")
        self.assertEqual(self.mounts(f"spaced={odd}"), [f"{odd}:/refs/spaced:ro"])
        colon = self.src / "a:b"
        colon.mkdir()
        self.assertRefused(f"ab={colon}", "contains ':'")

    def test_credentials_inside_warn_without_refusing(self):
        (self.src / "tools" / "dockade" / ".ssh").mkdir()
        proc = self.refs(f"{self.src}/tools/dockade")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WARNING: ref 'dockade' contains '.ssh'", proc.stderr)

    def test_a_ref_that_is_credential_material_is_refused(self):
        (self.home / ".ssh").mkdir()
        (self.home / ".aws" / "sso").mkdir(parents=True)
        self.assertRefused("keys=~/.ssh", "is credential material ('.ssh')")
        self.assertRefused("sso=~/.aws/sso", "is credential material ('.aws')")

    def test_a_newline_separates_entries_like_a_comma(self):
        # `read` alone would stop at the newline and drop the second entry.
        self.assertEqual(self.mounts(f"{self.src}/tools/dockade,\n{self.src}/a/docs"), [
            f"{self.src}/tools/dockade:/refs/dockade:ro",
            f"{self.src}/a/docs:/refs/docs:ro",
        ])

    def test_a_name_with_a_leading_dot_is_refused(self):
        # Hidden from `ls /refs` and from boundary-check.sh's /refs/*/ probe.
        dotted = self.src / ".dotfiles"
        dotted.mkdir()
        for value in (str(dotted), f".x={self.src}/a/docs", f"..={self.src}/a/docs",
                      f".={self.src}/a/docs"):
            with self.subTest(value=value):
                self.assertRefused(value, "not a usable name under /refs")
        self.assertEqual(self.mounts(f"dotfiles={dotted}"),
                         [f"{dotted}:/refs/dotfiles:ro"])

    def test_the_runtime_directories_are_refused(self):
        runtime = self.root / "run-user"
        (runtime / "bus-dir").mkdir(parents=True)
        # This fixture's home and config live under /tmp, which would refuse /tmp
        # for a different reason; a home elsewhere isolates the rule under test.
        elsewhere = {"HOME": "/nonexistent-home", "DOCKADE_CONFIG_HOME": "/nonexistent-cfg"}
        for value, why, env in (("/tmp", "that is /tmp", elsewhere),  # noqa: S108 (/tmp itself is the rule under test)
                                (str(runtime), f"that is {runtime}", {}),
                                (str(runtime / "bus-dir"), f"that is {runtime}", {})):
            with self.subTest(value=value):
                proc = self.refs(value, XDG_RUNTIME_DIR=str(runtime), **env)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn(why, proc.stderr)
        # Unlike /tmp, whose children are ordinary scratch: one is mounted as usual.
        scratch = Path("/tmp") / f"dockade-refs-test-{os.getpid()}"  # noqa: S108 (a child of /tmp is the point)
        scratch.mkdir()
        self.addCleanup(scratch.rmdir)
        self.assertEqual(self.mounts(str(scratch)),
                         [f"{scratch.resolve()}:/refs/{scratch.name}:ro"])

    def test_a_socket_or_fifo_near_the_top_is_refused(self):
        # A :ro bind does not stop connect() on a socket or a write to a FIFO.
        sock_dir = self.src / "tools" / "dockade" / "tmux-1000"
        sock_dir.mkdir()
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(sock_dir / "default"))
        self.assertRefused(f"{self.src}/tools/dockade", "holds a socket or FIFO")
        os.mkfifo(self.src / "a" / "docs" / "pipe")
        self.assertRefused(f"{self.src}/a/docs", "holds a socket or FIFO")

    def test_the_socket_scan_stops_at_three_levels(self):
        # The bound, pinned: deep enough for tmux-<uid>/default and ssh-*/agent.*,
        # shallow enough not to walk a whole checkout on every launch.
        deep = self.src / "b" / "docs" / "one" / "two" / "three"
        deep.mkdir(parents=True)
        os.mkfifo(deep / "pipe")
        self.assertEqual(len(self.mounts(f"{self.src}/b/docs")), 1)


if __name__ == "__main__":
    unittest.main()
