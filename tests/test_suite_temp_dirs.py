# SPDX-License-Identifier: Apache-2.0
"""Importing the test modules leaves nothing behind in the temp directory.

A module that needs a temp dir before its first test, as the control-plane modules do
for ``CONTROL_DB``, makes it at import, where no ``setUp`` or ``addCleanup`` reaches,
so it must remove it at exit. A child process imports every test module beside this
one with ``TMPDIR`` pointed at an empty directory, exits, and the directory must be
empty again.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent

_CHILD = """
import importlib, pathlib, tempfile
print(tempfile.gettempdir())
names = sorted(p.stem for p in pathlib.Path.cwd().glob("test_*.py"))
for name in names:
    importlib.import_module(name)
print("\\n".join(names))
"""

# The modules whose import-time temp dirs this test was written for; the child
# importing them is what makes an empty directory mean something.
_KNOWN = {"test_control_plane", "test_control_plane_api", "test_control_plane_ingest"}


class ImportLeavesNoTempDirsTests(unittest.TestCase):
    def test_every_test_module_cleans_up_what_it_makes_at_import(self):
        with tempfile.TemporaryDirectory(prefix="dockade-suite-tmp-") as tmp:
            run = subprocess.run(  # noqa: S603 (sys.executable, fixed args)
                [sys.executable, "-B", "-c", _CHILD],
                cwd=TESTS, env={**os.environ, "TMPDIR": tmp},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            tempdir, *names = run.stdout.split("\n")
            self.assertEqual(tempdir, tmp)
            imported = set(filter(None, names))
            self.assertGreater(len(imported), 1, run.stdout)
            self.assertLessEqual(_KNOWN, imported)
            self.assertEqual(sorted(os.listdir(tmp)), [],
                             "left behind in TMPDIR by importing the test modules")


if __name__ == "__main__":
    unittest.main()
