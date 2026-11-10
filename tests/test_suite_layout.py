# SPDX-License-Identifier: Apache-2.0
"""Every test module ends with its ``__main__`` guard, and every Python file make
lint and make consistency read is where they see it.

``python3 tests/test_x.py`` runs ``unittest.main()`` when the guard is reached, so
anything defined after it does not exist yet: those tests are skipped, and the run
still reports OK. Discovery (``make test``) imports the whole module and never sees
the gap, so only this check does.

Files are found by globbing the directory beside this module rather than asking git,
so a worktree git cannot read and a new, still-untracked test file are covered too.
The glob is discovery's own default pattern, ``test*.py``, so every module ``make
test`` runs is checked.

The Makefile's ``TESTFILES``, which ``make lint`` and ``make consistency`` read, follows
the same rule: ``$(wildcard tests/*.py)``, tracked or not. That glob does not descend,
so a ``.py`` file below the top level of ``tests/`` would go unlinted; it fails here
instead.

``PYFILES`` applies the same rule to component code: a glob per component directory.
``PyfilesTests`` is the one check here that asks git rather than globbing, because it
holds that directory list complete: every tracked module outside ``tests/`` must be
in it. The index is the set of modules the repo ships; a disk walk would sweep in
gitignored directories such as ``models/`` and ``.claude/worktrees/``, whole copies
of the repo inside the checkout.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
PATTERN = "test*.py"  # unittest discovery's default, which make test uses
# A missing tool skips on a dev machine and fails in CI: test_docs.py's _NeedsGit.
_STRICT = bool(os.environ.get("DOCKADE_REQUIRE_TOOLS"))
COMPONENTS = ("proxies/egress", "control-plane", "control-plane-ui", "ui-common",
              "tasks-ui", "tool-gateway", "task-registry", "sandbox-common/instructions")

# A hook's GIT_DIR/GIT_INDEX_FILE would stage a fixture into the real repo or read
# another repo's index, and the make variables of the ``make test`` this runs under
# would steer the make below.
_CLEARED = ("MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "MAKELEVEL", "MAKEOVERRIDES",
            "MAKEFILES")


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if not k.startswith("GIT_") and k not in _CLEARED}


def _make_variable(test: unittest.TestCase, name: str, directory: Path | str) -> list[str]:
    """``$(name)`` as the repo's Makefile expands it, run in ``directory``."""
    make = shutil.which("make")
    test.assertIsNotNone(make, "make is not installed")
    run = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
        [make, "-s", "--no-print-directory", "-f", str(ROOT / "Makefile"), "-C", str(directory),
         "--eval", f"print-var: ; @echo $({name})", "print-var"],
        env=_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=60, check=False)
    test.assertEqual(run.returncode, 0, run.stdout + run.stderr)
    return run.stdout.split()


def _git(test: unittest.TestCase) -> str:
    git = shutil.which("git")
    if not git:
        if _STRICT:
            test.fail("git is not installed and DOCKADE_REQUIRE_TOOLS is set: refusing "
                      "to pass without reading the index")
        test.skipTest("git is not installed: cannot read the index")
    return git


def tracked_modules(git: str, root: Path) -> set[str]:
    """Every ``.py`` in ``root``'s index outside ``tests/`` and still on disk, so a
    deletion not yet staged is not reported. Fails, saying which, when git cannot
    list the index or lists no such module."""
    run = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
        [git, "-C", str(root), "ls-files", "-z", "--", "*.py"],
        env=_env(), stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
    if run.returncode != 0:
        raise AssertionError("git could not list the index: "
                             + run.stderr.decode("utf-8", errors="replace"))
    names = run.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    modules = {name for name in names
               if name and not name.startswith("tests/") and (root / name).is_file()}
    if not modules:
        raise AssertionError("git listed no module outside tests/")
    return modules


def _is_main_guard(node: ast.stmt) -> bool:
    """``if __name__ == "__main__":``, matched by structure, not source text."""
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    return (isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")


def guard_problem(source: str) -> str | None:
    """Why ``source`` would not run all of itself as a script, or None."""
    body = ast.parse(source).body
    guards = [i for i, node in enumerate(body) if _is_main_guard(node)]
    if not guards:
        return "has no `if __name__ == \"__main__\":` guard, so running it runs no tests: end the file with one"
    if guards[-1] != len(body) - 1 or len(guards) > 1:
        after = body[guards[0] + 1]
        what = getattr(after, "name", type(after).__name__)
        return (f"has {what} (line {after.lineno}) after its `__main__` guard "
                f"(line {body[guards[0]].lineno}), which running it as a script skips: move the guard to the end")
    return None


class GuardIsLastTests(unittest.TestCase):

    def test_every_test_module_ends_with_its_main_guard(self):
        for path in sorted(TESTS.glob(PATTERN)):
            with self.subTest(path.name):
                problem = guard_problem(path.read_text(encoding="utf-8"))
                if problem:
                    self.fail(f"tests/{path.name} {problem}")

    def test_the_glob_finds_the_real_test_modules(self):
        # An empty or misdirected glob would pass the check above vacuously.
        found = {path.name for path in TESTS.glob(PATTERN)}
        self.assertIn(Path(__file__).name, found)
        self.assertLessEqual({"test_control_plane_api.py", "test_control_plane_ui_js.py",
                              "test_sandbox_wiring.py", "test_tool_execution.py",
                              "test_tool_gateway.py", "test_task_registry_backup.py"},
                             found)


class TestfilesTests(unittest.TestCase):
    """``TESTFILES`` lists every file in ``tests/`` that test runs, tracked or not."""

    def test_an_untracked_test_file_and_helper_are_listed(self):
        # Run make rather than parse it. An index-only TESTFILES lists only the tracked
        # file, and nothing without git, so the set tells the two apart either way.
        names = {"tests/test_tracked.py", "tests/test_new.py", "tests/_helper.py"}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "tests").mkdir()
            for name in names:
                (Path(tmp) / name).touch()
            git = shutil.which("git")
            if git:
                for args in (["init", "-q"], ["add", "-f", "--", "tests/test_tracked.py"]):
                    subprocess.run([git, "-C", tmp, *args], env=_env(), check=True,  # noqa: S603 (absolute path from shutil.which, fixed args)
                                   stdin=subprocess.DEVNULL, capture_output=True)
            listed = _make_variable(self, "TESTFILES", tmp)
        self.assertEqual(set(listed), names,
                         "TESTFILES must list every tests/*.py, untracked included: "
                         "see the comment above it in the Makefile")

    def test_no_python_file_sits_below_the_top_level(self):
        # Walked on disk rather than listed by git, so it cannot pass empty in a
        # worktree git cannot read.
        top, nested = set(), []
        for dirpath, dirnames, filenames in os.walk(TESTS):
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", "__pycache__")]
            here = Path(dirpath)
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                if here == TESTS:
                    top.add(name)
                else:
                    nested.append(str((here / name).relative_to(ROOT)))
        self.assertIn(Path(__file__).name, top)
        self.assertEqual(nested, [],
                         "TESTFILES globs tests/*.py only, so make lint and make "
                         "consistency skip these: move them up to tests/ (see the "
                         "comment above TESTFILES in the Makefile)")


class PyfilesTests(unittest.TestCase):
    """``PYFILES`` lists every module outside ``tests/``, tracked or not."""

    def test_every_tracked_module_outside_tests_is_listed(self):
        pyfiles = set(_make_variable(self, "PYFILES", ROOT))  # before git: no make fails, not skips
        self.assertTrue(pyfiles, "PYFILES expands to nothing in the repo: see the "
                                 "comment above it in the Makefile")
        tracked = tracked_modules(_git(self), ROOT)
        self.assertEqual(sorted(tracked - pyfiles), [],
                         "tracked but not in PYFILES, so make lint and make consistency "
                         "skip them: add their directory to the glob (see the comment "
                         "above PYFILES in the Makefile)")

    def test_an_untracked_module_in_each_component_is_listed(self):
        names = {f"{component}/scratch.py" for component in COMPONENTS}
        with tempfile.TemporaryDirectory() as tmp:
            for name in names:
                (Path(tmp) / name).parent.mkdir(parents=True)
                (Path(tmp) / name).touch()
            listed = _make_variable(self, "PYFILES", tmp)
        self.assertEqual(set(listed), names,
                         "PYFILES must glob every component directory, untracked files "
                         "included: see the comment above it in the Makefile")


class TrackedModulesTests(unittest.TestCase):
    """``tracked_modules`` itself: it cannot hand PyfilesTests an empty set."""

    def _repo(self, tmp: str, git: str, names: list[str]) -> None:
        subprocess.run([git, "-C", tmp, "init", "-q"], env=_env(), check=True,  # noqa: S603 (absolute path from shutil.which, fixed args)
                       stdin=subprocess.DEVNULL, capture_output=True)
        for name in names:
            (Path(tmp) / name).parent.mkdir(parents=True, exist_ok=True)
            (Path(tmp) / name).touch()
        subprocess.run([git, "-C", tmp, "add", "-f", "--", *names], env=_env(), check=True,  # noqa: S603 (absolute path from shutil.which, fixed args)
                       stdin=subprocess.DEVNULL, capture_output=True)

    def test_a_directory_git_cannot_read_fails(self):
        git = _git(self)
        with tempfile.TemporaryDirectory() as tmp, \
                self.assertRaisesRegex(AssertionError, "git could not list the index"):
            tracked_modules(git, Path(tmp))

    def test_an_index_with_no_module_outside_tests_fails(self):
        git = _git(self)
        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp, git, ["tests/test_x.py"])
            with self.assertRaisesRegex(AssertionError, "git listed no module outside tests/"):
                tracked_modules(git, Path(tmp))

    def test_tests_and_a_module_deleted_but_not_staged_are_left_out(self):
        git = _git(self)
        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp, git, ["comp/kept.py", "comp/gone.py", "tests/test_x.py"])
            (Path(tmp) / "comp/gone.py").unlink()
            self.assertEqual(tracked_modules(git, Path(tmp)), {"comp/kept.py"})


GUARD = 'if __name__ == "__main__":\n    unittest.main()\n'
CLASS = "class T(unittest.TestCase):\n    def test_x(self):\n        pass\n"


class GuardProblemTests(unittest.TestCase):

    def test_a_guard_last_passes(self):
        self.assertIsNone(guard_problem("import unittest\n" + CLASS + GUARD))

    def test_a_class_after_the_guard_is_named(self):
        problem = guard_problem("import unittest\n" + GUARD + CLASS)
        self.assertIn("has T (line 4) after its `__main__` guard (line 2)", problem)

    def test_any_statement_after_the_guard_is_refused(self):
        self.assertIn("Assign", guard_problem("import unittest\n" + CLASS + GUARD + "X = 1\n"))

    def test_a_missing_guard_is_refused(self):
        self.assertIn("no `if __name__", guard_problem("import unittest\n" + CLASS))

    def test_a_second_guard_mid_file_is_refused(self):
        self.assertIsNotNone(guard_problem("import unittest\n" + GUARD + CLASS + GUARD))

    def test_another_if_is_not_a_guard(self):
        self.assertIsNotNone(guard_problem('if __name__ != "__main__":\n    pass\n'))

    def test_a_guard_on_another_name_or_value_is_not_a_guard(self):
        for test in ('__name__ == "main"', 'name == "__main__"', '__name__ == __main__',
                     '__name__ == "__main__" == x', 'x.__name__ == "__main__"'):
            with self.subTest(test):
                self.assertIsNotNone(guard_problem(f"if {test}:\n    pass\n"))


if __name__ == "__main__":
    unittest.main()
