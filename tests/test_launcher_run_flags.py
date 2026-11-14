# SPDX-License-Identifier: Apache-2.0
"""Every launcher's ``docker run`` keeps the container's boundary flags.

Each ``run-*-sandbox.sh`` carries its own ``docker run`` block, and a new tier starts
as a copy of one. Nothing at runtime notices a copy that lost a flag: the entrypoint
and boundary-check.sh read the agent's capability sets after the privilege drop, which
clears them whatever Docker granted, so an extra ``--cap-add`` or ``--privileged`` is
invisible from inside. This reads the launchers' text instead, over the same glob as
the Makefile's ``LAUNCHERS``, so a new launcher is held without being registered.

The guard parses shell text, so flags carried in an array (``RUN_MODE_ARGS``, the
``SC_*_ARGS``) are invisible to it: a required flag moved into one reads as missing,
a loud failure, but a forbidden flag added through one passes. A reviewer of a
launcher diff reads its arrays.
"""
from __future__ import annotations

import re
import shlex
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# What the root entrypoint needs during setup, before gosu drops to the agent:
# NET_ADMIN for the firewall (sandbox-common/init-firewall.sh); CHOWN, DAC_OVERRIDE
# and FOWNER to own /config and materialize config, SETGID and SETUID for the drop
# (sandbox-common/entrypoint.sh). The launchers' comments above their `docker run`
# say the same. A cap a new setup step needs widens this set, in the same commit.
SETUP_CAPS = frozenset({"NET_ADMIN", "CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"})

# Deliberately strict: a later hardening option widens this set rather than passing
# unread, so `seccomp=unconfined` or `apparmor=unconfined` can never slip in beside it.
NO_NEW_PRIVILEGES = frozenset({"no-new-privileges", "no-new-privileges:true",
                               "no-new-privileges=true"})

# The options that take a value, as `--flag=X` or `--flag X`: the boundary flags read
# here, and every other the launchers pass that way. An option missing from this set
# is read as taking none, so its value reads as the image and every option after it
# goes unread: a required flag then reads as missing, a loud failure, never a pass.
_VALUED = frozenset({"--cap-drop", "--cap-add", "--security-opt", "--memory",
                     "--memory-swap", "--pids-limit", "--name", "--hostname", "--network",
                     "--user", "--cpus", "--dns", "--env", "--volume", "--mount",
                     "--workdir", "--entrypoint", "--label", "--publish", "--add-host",
                     "--tmpfs", "--ulimit", "--device", "--env-file",
                     "-e", "-v", "-u", "-w", "-p", "-h", "-l", "-m"})

_DOCKER_RUN = re.compile(r"(?<![\w-])docker\s+run(?=\s|$)")


def launchers(directory: Path) -> list[Path]:
    """The ``run-*-sandbox.sh`` launchers in ``directory``, sorted; none fails closed,
    since a guard over an empty glob would pass having checked nothing."""
    found = sorted(directory.glob("run-*-sandbox.sh"))
    if not found:
        raise AssertionError(f"no run-*-sandbox.sh launcher in {directory}")
    return found


def docker_runs(text: str) -> list[list[str]]:
    """The words of every uncommented ``docker run`` invocation in ``text``, from
    ``docker`` through its last backslash-continued line."""
    runs: list[list[str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        match = None if line.lstrip().startswith("#") else _DOCKER_RUN.search(line)
        if match is None:
            i += 1
            continue
        parts = [line[match.start():]]
        while parts[-1].rstrip().endswith("\\") and i + 1 < len(lines):
            parts[-1] = parts[-1].rstrip()[:-1]
            i += 1
            parts.append(lines[i])
        runs.append(shlex.split(" ".join(parts), comments=True))
        i += 1
    return runs


def _options(words: list[str]) -> list[tuple[str, str | None]]:
    """``(flag, value)`` for every option before the image, reading ``--flag=X`` and
    ``--flag X`` alike; a flag outside _VALUED keeps only what follows its own ``=``.

    Words after the image are the container's command, which Docker never reads as
    its own options, so a boundary flag moved there must not count. The image is the
    first word that is neither an option, an option's value, nor an array expansion
    (``"${ARGS[@]}"``, opaque here as the module docstring says)."""
    options: list[tuple[str, str | None]] = []
    i = 2  # past `docker run`
    while i < len(words):
        word = words[i]
        if word.startswith("-"):
            flag, eq, value = word.partition("=")
            if not eq and flag in _VALUED and i + 1 < len(words):
                i += 1
                value = words[i]
            options.append((flag, value if (eq or flag in _VALUED) else None))
        elif "[@]" not in word:
            break
        i += 1
    return options


def run_problems(words: list[str]) -> list[str]:
    """What one ``docker run`` invocation lacks or adds against the boundary."""
    options = _options(words)

    def values(flag: str) -> list[str]:
        return [v or "" for f, v in options if f == flag]

    problems = []
    if not any(v.upper() == "ALL" for v in values("--cap-drop")):
        problems.append("no --cap-drop=ALL")
    for cap in values("--cap-add"):
        if cap.upper().removeprefix("CAP_") not in SETUP_CAPS:
            problems.append(f"--cap-add={cap} is not a setup capability")
    opts = values("--security-opt")
    if not any(v in NO_NEW_PRIVILEGES for v in opts):
        problems.append("no --security-opt no-new-privileges")
    for opt in opts:
        if opt not in NO_NEW_PRIVILEGES:
            problems.append(f"--security-opt {opt} is not no-new-privileges")
    memory, swap = values("--memory"), values("--memory-swap")
    if not swap:
        problems.append("no --memory-swap")
    elif len(memory) != 1 or swap != memory:
        problems.append(f"--memory-swap {swap} is not the same argument as --memory {memory}")
    pids = values("--pids-limit")
    if not pids:
        problems.append("no --pids-limit")
    for limit in pids:
        # Docker reads 0 and -1 alike as unlimited.
        if not re.fullmatch(r"[1-9][0-9]*", limit):
            problems.append(f"--pids-limit={limit} is not a positive number")
    if any(f == "--privileged" for f, _ in options):
        problems.append("--privileged")
    return problems


def launcher_problems(text: str) -> list[str]:
    """Every problem in a launcher's text, each ``docker run`` numbered from 1."""
    runs = docker_runs(text)
    if not runs:
        return ["no uncommented docker run"]
    return [f"docker run #{n}: {p}" for n, words in enumerate(runs, 1)
            for p in run_problems(words)]


def every_launcher(directory: Path) -> dict[str, list[str]]:
    """Each launcher's problems, by name, for every launcher in ``directory``."""
    return {path.name: launcher_problems(path.read_text()) for path in launchers(directory)}


class LauncherRunFlagTests(unittest.TestCase):

    def test_every_launcher_keeps_its_boundary_flags(self):
        for name, problems in every_launcher(ROOT).items():
            with self.subTest(launcher=name):
                self.assertEqual(problems, [])


_CLAUDE = (ROOT / "run-claude-sandbox.sh").read_text()
_CAP_DROP = "    --cap-drop=ALL \\\n"


def _mutant(old: str, new: str) -> str:
    if _CLAUDE.count(old) != 1:
        raise AssertionError(f"run-claude-sandbox.sh no longer has one {old!r}")
    return _CLAUDE.replace(old, new)


def _with(line: str) -> str:
    """run-claude-sandbox.sh with ``line`` added to its docker run."""
    return _mutant(_CAP_DROP, f"{_CAP_DROP}    {line} \\\n")


class RunFlagCheckerTests(unittest.TestCase):
    """Each one-change mutant of run-claude-sandbox.sh is reported."""

    def assertReports(self, text: str, problem: str):
        problems = launcher_problems(text)
        self.assertIn(problem, problems)

    def test_the_unchanged_launcher_passes(self):
        self.assertEqual(launcher_problems(_CLAUDE), [])

    def test_each_required_flag_dropped_is_reported(self):
        for line, problem in (
                (_CAP_DROP, "no --cap-drop=ALL"),
                ("    --security-opt no-new-privileges \\\n",
                 "no --security-opt no-new-privileges"),
                ('    --memory-swap="$SANDBOX_MEMORY" \\\n', "no --memory-swap"),
                ("    --pids-limit=512 \\\n", "no --pids-limit")):
            with self.subTest(line=line):
                self.assertReports(_mutant(line, ""), f"docker run #1: {problem}")

    def test_memory_dropped_leaves_swap_unmatched(self):
        self.assertReports(
            _mutant('    --memory="$SANDBOX_MEMORY" \\\n', ""),
            "docker run #1: --memory-swap ['$SANDBOX_MEMORY'] is not the same argument as"
            " --memory []")

    def test_a_cap_drop_of_less_than_all_is_reported(self):
        self.assertReports(_mutant(_CAP_DROP, "    --cap-drop=NET_RAW \\\n"),
                           "docker run #1: no --cap-drop=ALL")

    def test_a_cap_add_outside_the_setup_set_is_reported_in_either_spelling(self):
        for line in ("--cap-add=SYS_ADMIN", "--cap-add SYS_ADMIN"):
            with self.subTest(line=line):
                self.assertReports(_with(line),
                                   "docker run #1: --cap-add=SYS_ADMIN is not a setup capability")

    def test_other_security_opts_are_reported(self):
        self.assertReports(
            _mutant("--security-opt no-new-privileges \\",
                    "--security-opt no-new-privileges:false \\"),
            "docker run #1: no --security-opt no-new-privileges")
        self.assertReports(
            _with("--security-opt seccomp=unconfined"),
            "docker run #1: --security-opt seccomp=unconfined is not no-new-privileges")

    def test_swap_differing_from_memory_is_reported(self):
        self.assertReports(
            _mutant('--memory-swap="$SANDBOX_MEMORY"', '--memory-swap="$SANDBOX_SWAP"'),
            "docker run #1: --memory-swap ['$SANDBOX_SWAP'] is not the same argument as"
            " --memory ['$SANDBOX_MEMORY']")

    def test_an_unlimited_pids_limit_is_reported(self):
        for limit in ("-1", "0"):
            with self.subTest(limit=limit):
                self.assertReports(
                    _mutant("--pids-limit=512", f"--pids-limit={limit}"),
                    f"docker run #1: --pids-limit={limit} is not a positive number")

    def test_privileged_is_reported(self):
        self.assertReports(_with("--privileged"), "docker run #1: --privileged")

    def test_a_second_docker_run_is_held_to_the_same_flags(self):
        text = _CLAUDE + '\ndocker run --rm --cap-drop=ALL \\\n    "$IMAGE_NAME" true\n'
        problems = launcher_problems(text)
        self.assertNotIn("docker run #1", " ".join(problems))
        self.assertIn("docker run #2: no --pids-limit", problems)
        self.assertNotIn("docker run #2: no --cap-drop=ALL", problems)

    def test_a_flag_after_the_image_does_not_count(self):
        # Docker hands it to the container's command, on the same line or a later one.
        image, rest = '    "$IMAGE_NAME" ', "${RUN_CMD_ARGS"
        for flag, problem in (("--cap-drop=ALL", "no --cap-drop=ALL"),
                              ("--pids-limit=512", "no --pids-limit")):
            moved = _mutant(f"    {flag} \\\n", "")
            self.assertEqual(moved.count(image + rest), 1)
            for after in (f"{image}{flag} {rest}", f"{image}\\\n    {flag} {rest}"):
                with self.subTest(flag=flag, after=after):
                    self.assertReports(moved.replace(image + rest, after),
                                       f"docker run #1: {problem}")

    def test_an_option_unknown_to_the_reader_fails_loudly(self):
        # Its value reads as the image, so every flag after it reads as missing.
        self.assertReports(_mutant("    --user root \\\n", "    --widgets root \\\n"),
                           "docker run #1: no --cap-drop=ALL")

    def test_a_flag_only_in_a_comment_does_not_count(self):
        text = _mutant("    --pids-limit=512 \\\n", "")
        text = text.replace("docker run ", "# --pids-limit=512\ndocker run ", 1)
        self.assertReports(text, "docker run #1: no --pids-limit")

    def test_no_docker_run_is_reported(self):
        self.assertEqual(launcher_problems(_mutant('docker run "${RUN_MODE_ARGS[@]}"',
                                                   'docker create "${RUN_MODE_ARGS[@]}"')),
                         ["no uncommented docker run"])

    def test_a_commented_out_docker_run_is_not_one(self):
        text = "\n".join("# " + ln for ln in _CLAUDE.splitlines())
        self.assertEqual(launcher_problems(text), ["no uncommented docker run"])


class LauncherGlobTests(unittest.TestCase):
    """The glob is what holds a new launcher, so it is run on a scratch directory."""

    def test_a_scratch_launcher_missing_a_flag_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "run-widgets-sandbox.sh"
            scratch.write_text(_mutant("    --pids-limit=512 \\\n", ""))
            (Path(tmp) / "widgets-helper.sh").write_text("docker run --privileged x\n")
            self.assertEqual(every_launcher(Path(tmp)),
                             {"run-widgets-sandbox.sh": ["docker run #1: no --pids-limit"]})

    def test_an_empty_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(AssertionError):
            every_launcher(Path(tmp))


if __name__ == "__main__":
    unittest.main()
