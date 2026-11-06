# SPDX-License-Identifier: Apache-2.0
"""Every test module ends with its ``__main__`` guard, and the suite's files are
where test, lint and compile all see them.

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

    # A hook's GIT_DIR/GIT_INDEX_FILE would stage the fixture into the real repo, and
    # the make variables of the ``make test`` this runs under would steer this make.
    CLEARED = ("MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "MAKELEVEL", "MAKEOVERRIDES",
               "MAKEFILES")

    def test_an_untracked_test_file_and_helper_are_listed(self):
        # Run make rather than parse it. An index-only TESTFILES lists only the tracked
        # file, and nothing without git, so the set tells the two apart either way.
        make = shutil.which("make")
        self.assertIsNotNone(make, "make is not installed")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GIT_") and k not in self.CLEARED}
        names = {"tests/test_tracked.py", "tests/test_new.py", "tests/_helper.py"}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "tests").mkdir()
            for name in names:
                (Path(tmp) / name).touch()
            git = shutil.which("git")
            if git:
                for args in (["init", "-q"], ["add", "-f", "--", "tests/test_tracked.py"]):
                    subprocess.run([git, "-C", tmp, *args], env=env, check=True,  # noqa: S603 (absolute path from shutil.which, fixed args)
                                   stdin=subprocess.DEVNULL, capture_output=True)
            run = subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
                [make, "-s", "--no-print-directory", "-f", str(ROOT / "Makefile"), "-C", tmp,
                 "--eval", "print-testfiles: ; @echo $(TESTFILES)", "print-testfiles"],
                env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=60, check=False)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(set(run.stdout.split()), names,
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
