# SPDX-License-Identifier: Apache-2.0
"""Every ``sqlite3.connect`` in the control plane and the task registry passes
``autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL``.

Python's docs say ``autocommit``'s default, legacy today, will become ``False``. Under
``False`` a connection is always inside a transaction: an explicit ``BEGIN`` and
``VACUUM`` raise, a ``journal_mode`` change into or out of WAL raises, and ``PRAGMA
foreign_keys = ON`` silently does nothing. Each component's own tests run its factory
and the sites that break under the patched default ``under_the_announced_default``
gives; this guard holds the rule at every site, those and the rest.

Only the call's arguments are read, so a connection must be opened as a direct call
spelled ``sqlite3.connect(…)``, and so must stay in legacy mode once open. Refused, as
forms that would hide a connection or change its mode out of sight: ``sqlite3.connect``
named anywhere but as the function of a call (``open_db = sqlite3.connect``,
``functools.partial(sqlite3.connect, …)``), ``sqlite3.dbapi2`` named at all,
``getattr(sqlite3, …)`` and ``setattr(sqlite3, …)``, ``sqlite3.Connection`` called or
subclassed, ``import sqlite3 as …``, an import of ``connect``, ``Connection``, ``*`` or
``dbapi2`` from sqlite3, an import of any sqlite3 submodule or of ``_sqlite3``, and an
assignment to any ``.autocommit``, ``setattr``'s included. What stays open is reaching
``Connection`` some other way, by an alias of it, ``type(…)`` of a connection, or a
``factory=`` subclass built through either; ``factory=`` itself is allowed.

Each source is counted, and each component must hold a call: a walk that found
nothing, or a component directory renamed, checked nothing.
The tests' own connections are left on the default: on a Python with the new one they
fail loudly."""
from __future__ import annotations

import ast
import contextlib
import sqlite3
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ("control-plane", "task-registry")
KEYWORD = "autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL"

_CONNECT = sqlite3.connect


@contextlib.contextmanager
def under_the_announced_default() -> Iterator[None]:
    """``sqlite3.connect`` as it will behave once ``autocommit`` defaults to ``False``:
    a call that names no ``autocommit`` gets ``False``. Enter it after a test's fixtures
    are built, so they are built as today."""
    def connect(*args, **kwargs):
        kwargs.setdefault("autocommit", False)
        return _CONNECT(*args, **kwargs)
    with mock.patch.object(sqlite3, "connect", connect):
        yield


#: Each group ``_sources`` yields; every one must hold a ``sqlite3.connect`` call.
GROUPS = COMPONENTS
#: What ``from sqlite3 import`` may not bring in: each opens a connection, or a module
#: that does, under a name this check does not follow.
HIDING_IMPORTS = ("connect", "Connection", "dbapi2", "*")


def _is_sqlite3(node: ast.expr, attr: str) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == attr
            and isinstance(node.value, ast.Name) and node.value.id == "sqlite3")


def _is_autocommit(target: ast.expr) -> bool:
    return isinstance(target, ast.Attribute) and target.attr == "autocommit"


def _import_problem(node: ast.stmt) -> str | None:
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module == "_sqlite3" or module.startswith("sqlite3."):
            return f"imports from `{module}`"
        if module == "sqlite3":
            hidden = [a.name for a in node.names if a.name in HIDING_IMPORTS]
            if hidden:
                return f"imports `{hidden[0]}` from sqlite3"
    elif isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "_sqlite3" or alias.name.startswith("sqlite3."):
                return f"imports `{alias.name}`"
            if alias.name == "sqlite3" and alias.asname not in (None, "sqlite3"):
                return f"imports sqlite3 as `{alias.asname}`"
    return None


def problems(source: str, where: str) -> tuple[list[str], int]:
    """What in ``source`` breaks the rule, each as ``where:line: why``, and how many
    ``sqlite3.connect`` calls it holds."""
    tree = ast.parse(source)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    found, calls = [], 0

    def refuse(node: ast.AST, why: str) -> None:
        found.append(f"{where}:{node.lineno}: {why}")

    for node in ast.walk(tree):
        why = _import_problem(node) if isinstance(node, ast.stmt) else None
        if why:
            refuse(node, f"{why}; open connections with `sqlite3.connect` so this check "
                         f"sees them")
        elif _is_sqlite3(node, "dbapi2"):
            refuse(node, "names sqlite3.dbapi2, whose `connect` this check does not "
                         "follow; open connections with `sqlite3.connect`")
        elif _is_sqlite3(node, "connect") and id(node) not in called:
            refuse(node, "names sqlite3.connect outside a call of it, where this check "
                         "cannot read the call's arguments")
        elif isinstance(node, ast.Call) and _is_sqlite3(node.func, "connect"):
            calls += 1
            given = [k.value for k in node.keywords if k.arg == "autocommit"]
            if not (given and _is_sqlite3(given[0], "LEGACY_TRANSACTION_CONTROL")):
                refuse(node, f"sqlite3.connect without {KEYWORD}, whose default is "
                             f"announced to become False")
        elif isinstance(node, ast.Call) and _is_sqlite3(node.func, "Connection"):
            refuse(node, "calls sqlite3.Connection; open connections with "
                         "`sqlite3.connect`")
        elif isinstance(node, ast.ClassDef) and any(
                _is_sqlite3(base, "Connection") for base in node.bases):
            refuse(node, "subclasses sqlite3.Connection, whose calls this check "
                         "cannot follow")
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ("getattr", "setattr") and node.args):
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == "sqlite3":
                refuse(node, f"reaches into sqlite3 by `{node.func.id}`, which this "
                             f"check cannot follow")
            elif (node.func.id == "setattr" and len(node.args) > 1
                  and isinstance(node.args[1], ast.Constant)
                  and node.args[1].value == "autocommit"):
                refuse(node, "sets autocommit after connecting; keep connections in "
                             "legacy transaction control")
        elif ((isinstance(node, ast.Assign) and any(map(_is_autocommit, node.targets)))
              or (isinstance(node, (ast.AugAssign, ast.AnnAssign))
                  and _is_autocommit(node.target))):
            refuse(node, "assigns autocommit after connecting; keep connections in "
                         "legacy transaction control")
    return found, calls


def _sources() -> Iterator[tuple[str, str, str]]:
    """Each source as its group, where it is, and its text."""
    for component in COMPONENTS:
        for path in sorted((ROOT / component).rglob("*.py")):
            yield component, str(path.relative_to(ROOT)), path.read_text()


def empty_groups(calls: dict[str, int]) -> list[str]:
    """The groups of ``GROUPS`` that held no call. Fails closed per group: a
    component renamed, or a source dropped from ``_sources``, would otherwise be a
    group that checked nothing."""
    return [group for group in GROUPS if not calls.get(group)]


class TransactionControlTests(unittest.TestCase):
    def test_every_connection_states_legacy_transaction_control(self):
        found, calls = [], dict.fromkeys(GROUPS, 0)
        for group, where, source in _sources():
            more, count = problems(source, where)
            found += more
            calls[group] += count
        self.assertEqual(found, [])
        self.assertEqual(empty_groups(calls), [],
                         "no sqlite3.connect call found to check in these")

    def test_a_group_with_no_call_is_named_though_others_have_some(self):
        calls = dict.fromkeys(GROUPS, 3)
        self.assertEqual(empty_groups(calls), [])
        calls["task-registry"] = 0
        self.assertEqual(empty_groups(calls), ["task-registry"])
        del calls["control-plane"]
        self.assertEqual(empty_groups(calls), ["control-plane", "task-registry"])

    def test_the_stated_keyword_passes(self):
        self.assertEqual(problems(
            f"import sqlite3\nsqlite3.connect('x', timeout=1.0, {KEYWORD})\n", "ok"),
            ([], 1))

    def test_a_connection_on_any_other_transaction_control_is_refused(self):
        for given in ("", ", autocommit=True", ", autocommit=False",
                      ", autocommit=LEGACY_TRANSACTION_CONTROL", ", **options"):
            with self.subTest(given=given):
                found, calls = problems(f"import sqlite3\nsqlite3.connect('x'{given})\n",
                                        "fixture.py")
                self.assertEqual(calls, 1)
                self.assertEqual(len(found), 1)
                self.assertTrue(found[0].startswith("fixture.py:2: sqlite3.connect without"))

    def test_an_import_that_would_hide_a_call_is_refused(self):
        for source in ("from sqlite3 import connect", "from sqlite3 import Row, connect",
                       "from sqlite3 import connect as open_db", "from sqlite3 import *",
                       "from sqlite3 import Connection", "from sqlite3 import dbapi2",
                       "from sqlite3.dbapi2 import connect", "import sqlite3.dbapi2",
                       "import sqlite3.dbapi2 as db", "import _sqlite3",
                       "from _sqlite3 import connect",
                       "import sqlite3 as db", "import os, sqlite3 as db"):
            with self.subTest(source=source):
                found, _ = problems(source + "\n", "fixture.py")
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].startswith("fixture.py:1: imports"), found)

    def test_a_connection_opened_out_of_sight_is_refused(self):
        for source, why in (
                ("open_db = sqlite3.connect", "names sqlite3.connect outside a call"),
                ("functools.partial(sqlite3.connect, 'x')",
                 "names sqlite3.connect outside a call"),
                ("sqlite3.dbapi2.connect('x')", "names sqlite3.dbapi2"),
                ("db = sqlite3.dbapi2", "names sqlite3.dbapi2"),
                ("getattr(sqlite3, 'connect')('x')", "reaches into sqlite3 by `getattr`"),
                ("setattr(sqlite3, 'connect', None)", "reaches into sqlite3 by `setattr`"),
                ("sqlite3.Connection('x')", "calls sqlite3.Connection"),
                ("class Mine(sqlite3.Connection):\n    pass", "subclasses sqlite3.Connection")):
            with self.subTest(source=source):
                found, _ = problems(f"import functools, sqlite3\n{source}\n", "fixture.py")
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].startswith(f"fixture.py:2: {why}"), found)

    def test_a_change_of_autocommit_after_connecting_is_refused(self):
        for source in ("conn.autocommit = False", "conn.autocommit: bool = True",
                       "a = conn.autocommit = False", "conn.autocommit |= True",
                       "setattr(conn, 'autocommit', False)"):
            with self.subTest(source=source):
                found, _ = problems(source + "\n", "fixture.py")
                self.assertEqual(len(found), 1, found)
                self.assertRegex(found[0], r"^fixture\.py:1: (assigns|sets) autocommit")

    def test_other_uses_of_sqlite3_pass(self):
        self.assertEqual(problems(
            "import sqlite3\nimport sqlite3 as sqlite3\nfrom sqlite3 import Row\n"
            "def f(conn: sqlite3.Connection) -> sqlite3.Connection:\n"
            "    conn.row_factory = sqlite3.Row\n"
            "    print(conn.autocommit, getattr(conn, 'autocommit'))\n"
            "    return conn\n", "ok"), ([], 0))


class AnnouncedDefaultTests(unittest.TestCase):
    """The stand-in the components' tests run under has the behaviour it stands for,
    and leaves a stated keyword alone."""

    def test_a_connection_naming_no_autocommit_is_always_in_a_transaction(self):
        with under_the_announced_default():
            conn = sqlite3.connect(":memory:")
        try:
            self.assertIs(conn.autocommit, False)
            with self.assertRaisesRegex(sqlite3.OperationalError, "within a transaction"):
                conn.execute("BEGIN")
        finally:
            conn.close()

    def test_a_stated_keyword_is_kept(self):
        with under_the_announced_default():
            conn = sqlite3.connect(":memory:", autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL)
        try:
            self.assertEqual(conn.autocommit, sqlite3.LEGACY_TRANSACTION_CONTROL)
            conn.execute("BEGIN")
            conn.execute("COMMIT")
        finally:
            conn.close()

    def test_it_is_undone_on_leaving(self):
        with under_the_announced_default():
            pass
        self.assertIs(sqlite3.connect, _CONNECT)


if __name__ == "__main__":
    unittest.main()
