# SPDX-License-Identifier: Apache-2.0
"""Each Python service installs exactly its ``requirements.txt``, and the files agree.

Each service's ``requirements.txt`` is a lock: its direct pins, then everything they
resolve to, so a version change of anything installed is a diff here. Its Dockerfile
installs it with ``--no-deps``, so nothing outside the file arrives, and runs ``pip
check``, so a file that is not a complete, consistent closure fails the build. This
module holds the half of that ``make test`` can see: every line is ``name==version``;
a file pinning fastapi pins starlette and anyio, whose behaviour the services rely on
(control-plane/requirements.txt says how); a package two files name has one version in
both, names compared PEP 503-normalised, so ``pydantic-core`` and ``pydantic_core`` are
one package; and each Dockerfile copies in its own ``requirements.txt`` once and has
one ``pip install`` line, exactly ``INSTALL``. Whether the closure is complete is ``pip check``'s, at
the image build. DESIGN.md's CI section gives the rule.

The files are read as text: a ``requirements.txt`` by line shape, a Dockerfile's
instructions through ``test_topology.py``'s reader, joined as BuildKit joins them. A
line out of the shape read here fails, naming its file, rather than matching nothing
and passing.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

# The services that ship, one list with test_python_pin.py's. Each must be among the
# files the glob finds, so one whose requirements.txt moves fails rather than dropping out.
from test_python_pin import ROOT, SERVICES
from test_topology import _dockerfile_instructions

# A package a file pinning fastapi must pin too: fastapi only bounds them, and the
# services rely on their behaviour.
WITH_FASTAPI = ("starlette", "anyio")

# Matched with re.match, which anchors at the start.
_PIN = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==([A-Za-z0-9.+!-]+)$")
# Any line that runs pip's install, however it is spelled (`python3 -m pip -q install`).
_PIP_INSTALL = re.compile(r"\bpip3?\b.*\binstall\b")
# The one install line each service's Dockerfile has, read with whitespace collapsed.
INSTALL = "RUN pip install --no-cache-dir --no-deps -r requirements.txt && pip check"


def normalise(name: str) -> str:
    """PEP 503's normalised form of a package name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path: Path, label: str) -> dict[str, str]:
    """The file's pins, each normalised name to its version. A line that is neither
    blank, a comment nor ``name==version`` fails, as does a package pinned twice."""
    found: dict[str, str] = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        if match is None:
            raise AssertionError(f"{label}:{number}: {line!r} is not name==version")
        name = normalise(match.group(1))
        if name in found:
            raise AssertionError(f"{label}:{number}: {match.group(1)} is pinned twice")
        found[name] = match.group(2)
    return found


def check_dockerfile(path: Path, service: str) -> None:
    """The service's own requirements.txt is copied in once, and installed by one line
    of exactly ``INSTALL``'s shape: the file and nothing else, then ``pip check``."""
    label = f"{service}/Dockerfile"
    lines = [text for _, text in _dockerfile_instructions(path.read_text())]
    copies = [line.split() for line in lines
              if re.match(r"\s*COPY\s", line, re.IGNORECASE) and "requirements" in line]
    if len(copies) != 1:
        raise AssertionError(f"{label}: expected one COPY of requirements.txt, "
                             f"found {len(copies)}")
    args = [w for w in copies[0][1:] if not w.startswith("--")]
    # Its own file, by the path a repo-root context gives it or the one its own
    # directory's context does.
    if args not in ([f"{service}/requirements.txt", "./requirements.txt"],
                    ["requirements.txt", "./requirements.txt"]):
        raise AssertionError(f"{label}: COPY {' '.join(args)} does not copy "
                             f"{service}/requirements.txt to ./requirements.txt")
    installs = [line for line in lines if _PIP_INSTALL.search(line)]
    if len(installs) != 1:
        raise AssertionError(f"{label}: expected one pip install line, "
                             f"found {len(installs)}")
    if " ".join(installs[0].split()) != INSTALL:
        raise AssertionError(f"{label}: the pip install line is not {INSTALL!r}, which "
                             f"installs exactly the file and fails the build if it is "
                             f"not a complete closure")


def check(root: Path, services: tuple[str, ...] = SERVICES) -> dict[str, dict[str, str]]:
    """Every ``*/requirements.txt`` under ``root``, each service's among them, held
    together; returns each directory's pins."""
    files = {path.parent.name: path for path in sorted(root.glob("*/requirements.txt"))}
    missing = [s for s in services if s not in files]
    if missing:
        raise AssertionError(
            f"no requirements.txt in {', '.join(f'{s}/' for s in missing)}")
    every: dict[str, dict[str, str]] = {}
    for service, path in files.items():
        label = f"{service}/requirements.txt"
        mine = every[service] = pins(path, label)
        if "fastapi" in mine:
            lacking = [p for p in WITH_FASTAPI if p not in mine]
            if lacking:
                raise AssertionError(f"{label}: pins fastapi but not "
                                     f"{', '.join(lacking)}")
        dockerfile = path.parent / "Dockerfile"
        if not dockerfile.is_file():
            raise AssertionError(f"{label}: no Dockerfile beside it to install it")
        check_dockerfile(dockerfile, service)
    versions: dict[str, dict[str, list[str]]] = {}
    for service, mine in every.items():
        for name, version in mine.items():
            versions.setdefault(name, {}).setdefault(version, []).append(service)
    for name, by_version in sorted(versions.items()):
        if len(by_version) > 1:
            where = "; ".join(
                f"{v} in {', '.join(f'{s}/requirements.txt' for s in services_)}"
                for v, services_ in sorted(by_version.items()))
            raise AssertionError(f"{name} has more than one version: {where}")
    return every


class RepoTests(unittest.TestCase):
    def test_the_services_install_one_consistent_lock_each(self):
        every = check(ROOT)
        for service in SERVICES:
            self.assertIn("fastapi", every[service], service)


# A file and a Dockerfile in the shapes the services use, edited per fixture below.
_REQUIREMENTS = """\
# a comment
fastapi==0.141.1
uvicorn==0.53.0
starlette==1.10.0

# the closure
anyio==4.16.0
pydantic==2.14.1
pydantic_core==2.50.1
"""

_DOCKERFILE = """\
FROM python:3.14-slim
COPY requirements.txt ./requirements.txt
# --no-deps installs exactly the file; pip install in a comment is not a line
RUN pip install --no-cache-dir --no-deps -r requirements.txt && pip check
COPY app.py ./app.py
"""


def _replace(old: str, new: str, text: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"the fixture edit {old!r} matches {text.count(old)} times")
    return text.replace(old, new)


class FixtureTests(unittest.TestCase):
    def build(self, requirements: dict[str, str] | None = None,
              dockerfiles: dict[str, str | None] | None = None) -> Path:
        """``acme`` and ``widgets``, each the fixture unless given."""
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        requirements = {"acme": _REQUIREMENTS, "widgets": _REQUIREMENTS,
                        **(requirements or {})}
        dockerfiles = {"acme": _DOCKERFILE, "widgets": _DOCKERFILE, **(dockerfiles or {})}
        for service, text in requirements.items():
            (tmp / service).mkdir()
            (tmp / service / "requirements.txt").write_text(text)
            if dockerfiles.get(service) is not None:
                (tmp / service / "Dockerfile").write_text(dockerfiles[service])
        return tmp

    def fails(self, root: Path, why: str) -> None:
        with self.assertRaisesRegex(AssertionError, why):
            check(root, ("acme", "widgets"))

    def test_the_fixture_passes(self):
        every = check(self.build(), ("acme", "widgets"))
        self.assertEqual(every["acme"]["pydantic-core"], "2.50.1")

    def test_a_loose_line(self):
        for line in ("starlette>=1.10.0", "starlette", "starlette==1.10.0 # why",
                     "starlette == 1.10.0", "-r other.txt", "starlette==1.10.*"):
            with self.subTest(line=line):
                text = _replace("starlette==1.10.0", line, _REQUIREMENTS)
                self.fails(self.build({"widgets": text}),
                           r"widgets/requirements\.txt:4: .* is not name==version")

    def test_a_package_pinned_twice(self):
        # Twice under two spellings is the same package.
        text = _REQUIREMENTS + "pydantic-core==2.50.1\n"
        self.fails(self.build({"widgets": text}),
                   r"widgets/requirements\.txt:10: pydantic-core is pinned twice")

    def test_fastapi_without_starlette_or_anyio(self):
        for pin in ("starlette==1.10.0\n", "anyio==4.16.0\n"):
            with self.subTest(pin=pin):
                text = _replace(pin, "", _REQUIREMENTS)
                self.fails(self.build({"widgets": text}),
                           rf"widgets/requirements\.txt: pins fastapi but not "
                           rf"{pin.split('==')[0]}$")

    def test_a_file_without_fastapi_need_not_pin_starlette(self):
        text = "# no web stack\nclick==8.5.1\n"
        check(self.build({"gadgets": text}, {"gadgets": _DOCKERFILE}),
              ("acme", "widgets"))

    def test_another_version_in_one_file(self):
        text = _replace("pydantic==2.14.1", "pydantic==2.14.3", _REQUIREMENTS)
        self.fails(self.build({"widgets": text}),
                   r"pydantic has more than one version: 2\.14\.1 in "
                   r"acme/requirements\.txt; 2\.14\.3 in widgets/requirements\.txt")

    def test_names_are_compared_normalised(self):
        text = _replace("pydantic_core==2.50.1", "pydantic-core==2.50.4", _REQUIREMENTS)
        self.fails(self.build({"widgets": text}),
                   r"pydantic-core has more than one version: 2\.50\.1 in acme/"
                   r"requirements\.txt; 2\.50\.4 in widgets/requirements\.txt")
        text = _replace("pydantic_core==2.50.1", "Pydantic.Core==2.50.4", _REQUIREMENTS)
        self.fails(self.build({"widgets": text}),
                   r"pydantic-core has more than one version")
        text = _replace("pydantic_core==2.50.1", "Pydantic.Core==2.50.1", _REQUIREMENTS)
        check(self.build({"widgets": text}), ("acme", "widgets"))

    def test_a_file_outside_the_services_is_held_too(self):
        text = "click==8.5.1\nanyio==4.15.0\n"
        self.fails(self.build({"gadgets": text}, {"gadgets": _DOCKERFILE}),
                   r"anyio has more than one version: .*4\.15\.0 in "
                   r"gadgets/requirements\.txt")

    def test_a_service_without_its_requirements(self):
        root = self.build()
        (root / "widgets" / "requirements.txt").unlink()
        self.fails(root, r"no requirements\.txt in widgets/$")

    def test_no_dockerfile_beside_it(self):
        self.fails(self.build(dockerfiles={"widgets": None}),
                   r"widgets/requirements\.txt: no Dockerfile")

    def test_an_install_line_of_another_shape(self):
        # Each installs something besides the file, resolves deps, or lets the build
        # pass whatever pip check says.
        line = "RUN pip install --no-cache-dir --no-deps -r requirements.txt && pip check"
        for other in (
                "RUN pip install --no-cache-dir -r requirements.txt && pip check",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt && pip list",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt ; pip check",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt "
                "&& echo pip check",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt "
                "&& pip check || true",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt "
                "&& pip check ; true",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt "
                "&& pip check && pip install starlette",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt "
                "fastapi==0.163.2 && pip check",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt "
                "-r extra.txt && pip check",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt.old "
                "&& pip check",
                "RUN pip install --no-cache-dir --no-deps -rrequirements.txt && pip check",
                "RUN pip install --no-cache-dir --no-deps fastapi && pip check",
                "RUN pip install --no-cache-dir --no-deps -r requirements.txt "
                "&& pip check extra"):
            with self.subTest(line=other):
                text = _replace(line, other, _DOCKERFILE)
                self.fails(self.build(dockerfiles={"widgets": text}),
                           r"widgets/Dockerfile: the pip install line is not ")

    def test_a_second_pip_install(self):
        for extra in ("RUN pip install foo\n", "RUN python -m pip install foo\n",
                      "RUN pip3 install foo\n", "RUN pip --no-cache-dir install foo\n",
                      "RUN python3 -m pip -q install foo\n",
                      "RUN true && \\\n    pip install foo\n"):
            with self.subTest(extra=extra):
                text = _DOCKERFILE + extra
                self.fails(self.build(dockerfiles={"widgets": text}),
                           r"widgets/Dockerfile: expected one pip install line, found 2")

    def test_no_pip_install(self):
        text = _replace("RUN pip install --no-cache-dir --no-deps -r requirements.txt "
                        "&& pip check\n", "", _DOCKERFILE)
        self.fails(self.build(dockerfiles={"widgets": text}),
                   r"widgets/Dockerfile: expected one pip install line, found 0")

    def test_a_continued_install_line_is_read_whole(self):
        text = _replace(" && pip check", " \\\n    && pip check", _DOCKERFILE)
        check(self.build(dockerfiles={"widgets": text}), ("acme", "widgets"))

    def test_the_dockerfile_is_read_as_the_builder_joins_it(self):
        copy = "COPY requirements.txt ./requirements.txt\n"
        install = " && pip check"
        for name, old, new, why in (
                ("an unindented continuation runs on with no separator",
                 copy, "COPY requirements\\\n.txt ./requirements.txt\n", None),
                ("a backslash followed by a space and a tab continues",
                 install, " \\ \t\n    && pip check", None),
                ("an escaped backslash ends the instruction",
                 copy, "RUN echo a\\\\\n" + copy, None),
                ("a copy after an escaped backslash is seen",
                 copy, copy + "RUN echo a\\\\\nCOPY other/requirements.txt ./r.txt\n",
                 r"expected one COPY of requirements\.txt, found 2"),
                ("a blank line inside a continuation is skipped",
                 install, " \\\n\n    && pip check", None)):
            with self.subTest(name):
                text = _replace(old, new, _DOCKERFILE)
                root = self.build(dockerfiles={"widgets": text})
                if why is None:
                    check(root, ("acme", "widgets"))
                else:
                    self.fails(root, rf"widgets/Dockerfile: {why}")

    def test_the_copy_from_a_repo_root_context_passes(self):
        text = _replace("COPY requirements.txt ", "COPY --chown=0:0 widgets/requirements.txt ",
                        _DOCKERFILE)
        check(self.build(dockerfiles={"widgets": text}), ("acme", "widgets"))

    def test_a_copy_of_another_file(self):
        for copy in ("COPY acme/requirements.txt ./requirements.txt",
                     "COPY other/requirements.txt ./requirements.txt",
                     "COPY widgets/requirements.txt.old ./requirements.txt",
                     "COPY requirements.txt ./other.txt",
                     "COPY requirements.txt extra.txt ./requirements.txt",
                     "COPY --from=requirements /x ./requirements.txt",
                     "COPY --from=requirements"):
            with self.subTest(copy=copy):
                text = _replace("COPY requirements.txt ./requirements.txt", copy,
                                _DOCKERFILE)
                self.fails(self.build(dockerfiles={"widgets": text}),
                           r"widgets/Dockerfile: COPY .* does not copy "
                           r"widgets/requirements\.txt to \./requirements\.txt")

    def test_requirements_copied_twice_or_not_at_all(self):
        line = "COPY requirements.txt ./requirements.txt\n"
        for copies, found in (("", 0), (line + "copy x/requirements.txt ./r.txt\n", 2)):
            with self.subTest(found=found):
                text = _replace(line, copies, _DOCKERFILE)
                self.fails(self.build(dockerfiles={"widgets": text}),
                           rf"widgets/Dockerfile: expected one COPY of requirements\.txt, "
                           rf"found {found}")


if __name__ == "__main__":
    unittest.main()
