# SPDX-License-Identifier: Apache-2.0
"""The service images and CI's tests run one Python minor.

The services are built ``FROM python:<X.Y>-slim``; CI's ``checks`` job sets
``PYTHON_VERSION`` and installs that minor with setup-python before anything runs
``python3``, then fails its toolchain step if ``python3`` is another. The two values are
restated, not derived, so that a bump which moves one and misses the other fails here,
in ``make test``, before the push. DESIGN.md's CI section gives the rule.

Both files are read as text, by line shape. A line reformatted out of the shape read
here fails closed, naming what was looked for, rather than matching nothing and passing.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"

# The images that ship. Each must be found among the python-based Dockerfiles, so a FROM
# that changes form (an ARG, a digest) fails rather than dropping out of the comparison.
SERVICES = ("control-plane", "control-plane-ui", "task-registry", "tasks-ui",
            "tool-gateway")

_FROM = re.compile(r"^\s*FROM\s+(.*)$", re.MULTILINE)
_PYTHON_BASE = re.compile(r"^python:(\d+\.\d+)-slim$")


def dockerfile_minors(root: Path) -> dict[str, str]:
    """Each ``*/Dockerfile`` under ``root`` whose base is Python, by directory, to the
    ``X.Y`` of its ``python:X.Y-slim`` base. A FROM that names python in any other form
    fails, as does a file whose FROM lines disagree."""
    minors: dict[str, str] = {}
    for path in sorted(root.glob("*/Dockerfile")):
        label = f"{path.parent.name}/Dockerfile"
        found = set()
        for line in _FROM.findall(path.read_text()):
            words = line.split()
            image = next((w for w in words if not w.startswith("--")), "")
            if "python" not in image.lower():
                continue
            match = _PYTHON_BASE.match(image)
            if match is None:
                raise AssertionError(
                    f"{label}: FROM {line.strip()!r} is not python:<X.Y>-slim, the one "
                    f"form this guard reads")
            found.add(match.group(1))
        if len(found) > 1:
            raise AssertionError(f"{label}: FROM names several Pythons: {sorted(found)}")
        if found:
            minors[path.parent.name] = found.pop()
    return minors


def image_minor(root: Path, services: tuple[str, ...] = SERVICES) -> str:
    """The one Python minor every python-based Dockerfile under ``root`` is built on."""
    minors = dockerfile_minors(root)
    missing = [s for s in services if s not in minors]
    if missing:
        raise AssertionError(
            f"no FROM python:<X.Y>-slim in {', '.join(f'{s}/Dockerfile' for s in missing)}")
    if len(set(minors.values())) != 1:
        raise AssertionError(f"the Dockerfiles disagree on the Python minor: {minors}")
    return next(iter(minors.values()))


def _jobs(text: str, label: str) -> dict[str, str]:
    """The workflow's jobs, by id, each to its text."""
    lines = text.splitlines()
    try:
        start = lines.index("jobs:")
    except ValueError:
        raise AssertionError(f"{label}: no top-level 'jobs:' line") from None
    jobs: dict[str, list[str]] = {}
    current = None
    for line in lines[start + 1:]:
        if line and not line.startswith(" ") and not line.startswith("#"):
            break  # the next top-level key
        header = re.match(r"^  ([\w-]+):\s*$", line)
        if header:
            current = header.group(1)
            jobs[current] = []
        elif current is not None:
            jobs[current].append(line)
    return {job: "\n".join(body) for job, body in jobs.items()}


def _code(text: str) -> str:
    """``text`` without its comment lines."""
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))


def _steps(job: str) -> list[str]:
    """A job's steps, in order, each to its uncommented text."""
    return [_code(step) for step in re.split(r"^      - ", job, flags=re.MULTILINE)[1:]]


_VERSION = re.compile(r"^\s*PYTHON_VERSION:\s*(.*?)\s*$", re.MULTILINE)
_SETUP = re.compile(r"^\s*uses:\s*actions/setup-python@\S+\s*$", re.MULTILINE)
_SETUP_INPUT = re.compile(r"^\s*python-version:\s*(.*?)\s*$", re.MULTILINE)
_TOOLCHAIN = re.compile(r"^\s*name:\s*Toolchain versions\s*$", re.MULTILINE)
_MAKE = re.compile(r"(^|[\s;&|(])make(\s|$)", re.MULTILINE)
_READ_MINOR = re.compile(r"^\s*(\w+)=\$\(python3\s[^)]*\)", re.MULTILINE)
# A step key that lets the job go on past the step: skipped, or failed and ignored.
_SOFTENED = re.compile(r"^( {8})?(if|continue-on-error)\s*:", re.MULTILINE)
# The same keys on the job itself: a skipped job or an ignored failure passes the run.
_JOB_SOFTENED = re.compile(r"^ {4}(if|continue-on-error)\s*:", re.MULTILINE)


def _one(found: list[int], what: str, label: str) -> int:
    if len(found) != 1:
        raise AssertionError(f"{label}: expected one {what} in the checks job, "
                             f"found {len(found)}")
    return found[0]


def _not_softened(step: str, what: str, label: str) -> None:
    key = _SOFTENED.search(step)
    if key:
        raise AssertionError(f"{label}: the {what} has '{key.group(2)}:', so the job "
                             f"can go on without it")


def _comparison_fails_the_job(run: str, label: str) -> None:
    """The toolchain step reads python3's minor, compares it with ``$PYTHON_VERSION``,
    and the branch taken on a mismatch reaches ``exit 1``."""
    if "|| true" in run:
        raise AssertionError(f"{label}: the toolchain step has '|| true', so its "
                             f"comparison could not fail the job")
    variables = _READ_MINOR.findall(run)
    for var in variables:
        pattern = (rf'^\s*if \[ "\${var}" != "\$PYTHON_VERSION" \]; then$'
                   rf'|^\s*if \[ "\$PYTHON_VERSION" != "\${var}" \]; then$')
        match = re.search(pattern, run, re.MULTILINE)
        if match is None:
            continue
        branch = run[match.end():].split("\n")
        body = []
        for line in branch:
            if re.match(r"^\s*(fi|else|elif)\b", line):
                break
            body.append(line.strip())
        if "exit 1" in body:
            return
        raise AssertionError(f"{label}: the toolchain step's comparison with "
                             f"$PYTHON_VERSION never reaches 'exit 1'")
    raise AssertionError(
        f"{label}: the toolchain step does not compare python3's minor with "
        f"$PYTHON_VERSION (looked for VAR=$(python3 ...) and "
        f'if [ "$VAR" != "$PYTHON_VERSION" ]; then)')


def workflow_minor(text: str, label: str) -> str:
    """The minor the workflow's ``checks`` job pins, once it is shown that setup-python
    installs it before the toolchain check and the tests, and the check can fail."""
    jobs = _jobs(text, label)
    if "checks" not in jobs:
        raise AssertionError(f"{label}: no 'checks' job")
    for job, body in jobs.items():
        if job != "checks" and _VERSION.search(_code(body)):
            raise AssertionError(f"{label}: PYTHON_VERSION is set in job {job!r}; "
                                 f"only 'checks' runs the tests")
    checks = jobs["checks"]
    key = _JOB_SOFTENED.search(_code(checks.split("\n    steps:")[0]))
    if key:
        raise AssertionError(f"{label}: the checks job has '{key.group(1)}:', so the "
                             f"run can pass without its tests")
    versions = _VERSION.findall(_code(checks))
    if len(versions) != 1:
        raise AssertionError(f"{label}: expected the checks job to set "
                             f"PYTHON_VERSION once, found {len(versions)}")
    if not _VERSION.search(_code(checks.split("\n    steps:")[0])):
        raise AssertionError(f"{label}: PYTHON_VERSION is set in a step, not in the "
                             f"checks job's env, where every step reads it")
    if "PYTHON_VERSION" in _code(text.split("\njobs:")[0]):
        raise AssertionError(f"{label}: PYTHON_VERSION is set outside the checks job")
    # Quoted: YAML reads an unquoted 3.10 as the number 3.1.
    quoted = re.fullmatch(r"\"(\d+\.\d+)\"|'(\d+\.\d+)'", versions[0])
    if quoted is None:
        raise AssertionError(f"{label}: PYTHON_VERSION {versions[0]} is not a quoted X.Y")
    version = quoted.group(1) or quoted.group(2)

    steps = _steps(checks)
    setup = _one([i for i, s in enumerate(steps) if _SETUP.search(s)],
                 "actions/setup-python step", label)
    toolchain = _one([i for i, s in enumerate(steps) if _TOOLCHAIN.search(s)],
                     "'Toolchain versions' step", label)
    inputs = _SETUP_INPUT.findall(steps[setup])
    if inputs != ["${{ env.PYTHON_VERSION }}"]:
        raise AssertionError(f"{label}: setup-python's python-version is {inputs}, not "
                             f"${{{{ env.PYTHON_VERSION }}}}")
    _not_softened(steps[setup], "setup-python step", label)
    _not_softened(steps[toolchain], "toolchain step", label)
    if setup > toolchain:
        raise AssertionError(f"{label}: setup-python comes after the toolchain step")
    makes = [i for i, s in enumerate(steps) if _MAKE.search(s)]
    if not makes:
        raise AssertionError(f"{label}: no step in the checks job runs make")
    if setup > makes[0]:
        raise AssertionError(f"{label}: setup-python comes after a step that runs make")
    _comparison_fails_the_job(steps[toolchain], label)
    return version


class RepoTests(unittest.TestCase):
    def test_the_images_and_ci_pin_one_minor(self):
        self.assertEqual(workflow_minor(WORKFLOW.read_text(), str(WORKFLOW.name)),
                         image_minor(ROOT),
                         "check.yml's PYTHON_VERSION is not the minor the service "
                         "Dockerfiles are built FROM; a bump moves both")


# A minimal checks job in the workflow's shape, edited per fixture below.
_GOOD = """\
name: check
on:
  pull_request:
jobs:
  checks:
    runs-on: ubuntu-latest
    env:
      DOCKADE_REQUIRE_TOOLS: 1
      PYTHON_VERSION: "3.14"
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: ${{ env.PYTHON_VERSION }}
      - name: Toolchain versions
        run: |
          python3 --version
          minor=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
          if [ "$minor" != "$PYTHON_VERSION" ]; then
            echo "python3 is $minor" >&2
            exit 1
          fi
      - name: make lint consistency test
        run: make lint consistency test
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - name: make verify-build
        run: make verify-build
"""

_SETUP_STEP = """\
      - uses: actions/setup-python@v7
        with:
          python-version: ${{ env.PYTHON_VERSION }}
"""


def _replace(old: str, new: str, text: str = _GOOD) -> str:
    if text.count(old) != 1:
        raise ValueError(f"the fixture edit {old!r} matches {text.count(old)} times")
    return text.replace(old, new)


class WorkflowFixtureTests(unittest.TestCase):
    def fails(self, text: str, why: str) -> None:
        with self.assertRaisesRegex(AssertionError, r"check\.yml") as raised:
            workflow_minor(text, "check.yml")
        self.assertRegex(str(raised.exception), why)

    def test_the_fixture_passes(self):
        self.assertEqual(workflow_minor(_GOOD, "check.yml"), "3.14")

    def test_no_python_version(self):
        self.fails(_replace('      PYTHON_VERSION: "3.14"\n', ""), "PYTHON_VERSION once")

    def test_two_python_versions(self):
        self.fails(_replace('      PYTHON_VERSION: "3.14"\n',
                            '      PYTHON_VERSION: "3.14"\n      PYTHON_VERSION: "3.13"\n'),
                   "PYTHON_VERSION once, found 2")

    def test_python_version_only_in_another_job(self):
        text = _replace('      PYTHON_VERSION: "3.14"\n', "")
        text = _replace("  build:\n    runs-on: ubuntu-latest\n",
                        '  build:\n    runs-on: ubuntu-latest\n    env:\n'
                        '      PYTHON_VERSION: "3.14"\n', text)
        self.fails(text, "in job 'build'")

    def test_python_version_at_the_workflow_level(self):
        self.fails(_replace("on:\n", 'env:\n  PYTHON_VERSION: "3.14"\non:\n'),
                   "outside the checks job")

    def test_an_unquoted_python_version(self):
        self.fails(_replace('PYTHON_VERSION: "3.14"', "PYTHON_VERSION: 3.14"),
                   "not a quoted X.Y")

    def test_python_version_only_in_a_step(self):
        text = _replace('      PYTHON_VERSION: "3.14"\n', "")
        text = _replace("        run: make lint consistency test\n",
                        "        run: make lint consistency test\n        env:\n"
                        '          PYTHON_VERSION: "3.14"\n', text)
        self.fails(text, "set in a step")

    def test_no_setup_python(self):
        self.fails(_replace(_SETUP_STEP, ""), "one actions/setup-python step")

    def test_setup_python_only_in_another_job(self):
        text = _replace(_SETUP_STEP, "")
        text = _replace("      - name: make verify-build\n",
                        _SETUP_STEP + "      - name: make verify-build\n", text)
        self.fails(text, "one actions/setup-python step")

    def test_two_setup_pythons(self):
        self.fails(_replace(_SETUP_STEP, _SETUP_STEP + _SETUP_STEP),
                   "found 2")

    def test_setup_python_with_a_literal_version(self):
        self.fails(_replace("python-version: ${{ env.PYTHON_VERSION }}",
                            'python-version: "3.14"'), "not \\$\\{\\{ env")

    def test_setup_python_after_the_toolchain_step(self):
        text = _replace(_SETUP_STEP, "")
        text = _replace("      - name: make lint consistency test\n",
                        _SETUP_STEP + "      - name: make lint consistency test\n", text)
        self.fails(text, "after the toolchain step")

    def test_setup_python_after_make(self):
        # The toolchain step's own comparison would pass here too, while make ran on
        # the runner's default.
        text = _replace(_SETUP_STEP, "")
        text = _replace("      - name: Toolchain versions\n",
                        "      - name: make lint consistency test\n"
                        "        run: make lint consistency test\n"
                        + _SETUP_STEP + "      - name: Toolchain versions\n", text)
        self.fails(text, "after a step that runs make")

    def test_no_make_step(self):
        start = _GOOD.index("      - name: make lint")
        end = _GOOD.index("  build:")
        self.fails(_GOOD[:start] + _GOOD[end:], "no step in the checks job runs make")

    def test_a_softened_step(self):
        # Each lets the job pass with python3 on another minor: a skipped or failed
        # setup-python leaves the runner's default, a skipped or ignored toolchain
        # step never fails the job.
        setup = "      - uses: actions/setup-python@v7\n"
        toolchain = "      - name: Toolchain versions\n"
        for anchor, key in ((setup, "if: false"), (setup, "continue-on-error: true"),
                            (toolchain, "if: false"),
                            (toolchain, "continue-on-error: true")):
            with self.subTest(step=anchor.strip(), key=key):
                self.fails(_replace(anchor, f"{anchor}        {key}\n"),
                           f"'{key.split(':')[0]}:'")

    def test_comments_are_not_read(self):
        # A comment that says make before setup-python, or `|| true` in the toolchain
        # step, is prose, not a step that runs make or a softened comparison.
        text = _replace("      - uses: actions/checkout@v7\n" + _SETUP_STEP,
                        "      - uses: actions/checkout@v7\n"
                        "      # nothing before setup-python may run make\n" + _SETUP_STEP)
        text = _replace("          python3 --version\n",
                        "          python3 --version\n"
                        "          # no `|| true` below: a mismatch fails the job\n", text)
        self.assertEqual(workflow_minor(text, "check.yml"), "3.14")

    def test_no_toolchain_step(self):
        self.fails(_replace("name: Toolchain versions", "name: Versions"),
                   "'Toolchain versions' step")

    def test_toolchain_step_only_in_another_job(self):
        text = _replace("name: Toolchain versions", "name: Versions")
        text = _replace("      - name: make verify-build\n",
                        "      - name: Toolchain versions\n        run: python3 --version\n"
                        "      - name: make verify-build\n", text)
        self.fails(text, "'Toolchain versions' step")

    def test_toolchain_step_without_the_comparison(self):
        start = _GOOD.index("          minor=")
        end = _GOOD.index("      - name: make lint")
        self.fails(_GOOD[:start] + _GOOD[end:], "does not compare")

    def test_toolchain_step_that_only_echoes(self):
        start = _GOOD.index("          minor=")
        end = _GOOD.index("      - name: make lint")
        self.fails(_GOOD[:start] + '          echo "want $PYTHON_VERSION"\n' + _GOOD[end:],
                   "does not compare")

    def test_comparison_ending_in_or_true(self):
        self.fails(_replace("            exit 1\n", "            exit 1 || true\n"),
                   "\\|\\| true")

    def test_comparison_that_never_exits(self):
        self.fails(_replace("            exit 1\n", ""), "never reaches 'exit 1'")

    def test_comparison_that_exits_zero(self):
        self.fails(_replace("            exit 1\n", "            exit 0\n"),
                   "never reaches 'exit 1'")

    def test_comparison_commented_out(self):
        self.fails(_replace("            exit 1\n", "            # exit 1\n"),
                   "never reaches 'exit 1'")

    def test_comparison_against_a_literal(self):
        self.fails(_replace('[ "$minor" != "$PYTHON_VERSION" ]', '[ "$minor" != "3.14" ]'),
                   "does not compare")

    def test_exit_1_only_in_the_else_branch(self):
        # Exits when the versions match, and passes when they differ.
        self.fails(_replace("            exit 1\n",
                            "            true\n          else\n            exit 1\n"),
                   "never reaches 'exit 1'")

    def test_a_softened_checks_job(self):
        for key in ("if: false", "continue-on-error: true"):
            with self.subTest(key=key):
                self.fails(_replace("  checks:\n    runs-on: ubuntu-latest\n",
                                    f"  checks:\n    runs-on: ubuntu-latest\n    {key}\n"),
                           f"checks job has '{key.split(':')[0]}:'")

    def test_no_checks_job(self):
        self.fails(_replace("  checks:\n", "  tests:\n"), "no 'checks' job")


class DockerfileFixtureTests(unittest.TestCase):
    def build(self, tags: dict[str, str]) -> Path:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for service, base in tags.items():
            (tmp / service).mkdir()
            (tmp / service / "Dockerfile").write_text(
                f"# a comment\nFROM {base}\nRUN true\n")
        return tmp

    def fails(self, tags: dict[str, str], why: str) -> None:
        with self.assertRaisesRegex(AssertionError, why):
            image_minor(self.build(tags), ("acme", "widgets"))

    def test_one_minor_passes_and_other_bases_are_ignored(self):
        root = self.build({"acme": "python:3.14-slim", "widgets": "python:3.14-slim",
                           "sandbox": "debian:13-slim"})
        self.assertEqual(image_minor(root, ("acme", "widgets")), "3.14")

    def test_another_minor(self):
        self.fails({"acme": "python:3.14-slim", "widgets": "python:3.13-slim"},
                   "disagree")

    def test_a_python_dockerfile_outside_the_services_is_held_too(self):
        self.fails({"acme": "python:3.14-slim", "widgets": "python:3.14-slim",
                    "gadgets": "python:3.12-slim"}, "disagree")

    def test_another_variant(self):
        for base in ("python:3.14", "python:3.14-alpine", "python:3.14-slim-trixie"):
            with self.subTest(base=base):
                self.fails({"acme": "python:3.14-slim", "widgets": base},
                           "widgets/Dockerfile.*not python:<X.Y>-slim")

    def test_a_form_the_reader_cannot_parse(self):
        for base in ("python:${PY}-slim", "python@sha256:" + "0" * 64,
                     "docker.io/library/python:3.14-slim"):
            with self.subTest(base=base):
                self.fails({"acme": "python:3.14-slim", "widgets": base},
                           "widgets/Dockerfile.*not python:<X.Y>-slim")

    def test_a_multi_stage_dockerfile_on_two_pythons(self):
        root = self.build({"acme": "python:3.14-slim", "widgets": "python:3.14-slim"})
        (root / "widgets" / "Dockerfile").write_text(
            "FROM python:3.13-slim AS build\nRUN true\n"
            "FROM python:3.14-slim\nCOPY --from=build /x /x\n")
        with self.assertRaisesRegex(AssertionError,
                                    "widgets/Dockerfile: FROM names several Pythons"):
            image_minor(root, ("acme", "widgets"))

    def test_a_service_off_python_entirely(self):
        self.fails({"acme": "python:3.14-slim", "widgets": "$BASE"},
                   "no FROM python:<X.Y>-slim in widgets/Dockerfile")

    def test_a_platform_flag_is_read_past(self):
        root = self.build({"acme": "--platform=linux/amd64 python:3.14-slim",
                           "widgets": "python:3.14-slim AS run"})
        self.assertEqual(image_minor(root, ("acme", "widgets")), "3.14")


if __name__ == "__main__":
    unittest.main()
