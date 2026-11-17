# SPDX-License-Identifier: Apache-2.0
"""The control-plane test modules loaded in one process each use their own store.

The process has one ``store``, whose paths are those of the module that loaded it
first, so each module binds its own in ``setUpModule`` (``use_control_store`` in
``tests/_loader.py``). ``make test``'s order is discovery's, so this does not rely
on it: a child process runs each module's ``OwnStoreTests`` together, importing the
modules in the order named, once forwards and once reversed. The first module
imported already has its paths, so only the two orders together cover all three.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent

_MODULES = ("test_control_plane", "test_control_plane_api", "test_control_plane_ingest")


class StoreIsolationTests(unittest.TestCase):
    def _run(self, modules):
        run = subprocess.run(  # noqa: S603 (sys.executable, fixed args)
            [sys.executable, "-B", "-W", "ignore::ResourceWarning", "-m", "unittest",
             *(f"{name}.OwnStoreTests" for name in modules)],
            cwd=TESTS, env=os.environ.copy(), capture_output=True, text=True,
            timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(f"Ran {len(modules)} tests", run.stderr)

    def test_each_module_has_its_own_store_in_import_order(self):
        self._run(_MODULES)

    def test_each_module_has_its_own_store_in_reverse_order(self):
        self._run(tuple(reversed(_MODULES)))


if __name__ == "__main__":
    unittest.main()
