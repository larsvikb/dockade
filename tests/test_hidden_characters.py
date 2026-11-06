# SPDX-License-Identifier: Apache-2.0
"""No tracked file holds a character a reader cannot see.

A bidi override, an isolate or a zero-width character in source is the Trojan Source
hazard: the text a reviewer reads is not the text the interpreter runs, and neither an
editor nor a diff shows it. The reviewer scans each change for one; this holds the ones
already tracked and the ones a review misses. It is easy to add one without meaning to:
an escape typed into a tool parameter arrives as the character (NOTES.md, "A backslash-u
escape typed into a tool parameter arrives as the character").

Hidden means what ``_hidden`` in ``task-registry/export.py`` says, the set the task
export already escapes: categories Cc, Cf, Zl and Zp, and Unicode's
Default_Ignorable_Code_Point ranges. Taken from there, not copied, so Python has one
definition. Tab and newline are the only exceptions, so a CRLF line ending fails too.

No allowlist. Every file type tracked here has an escape form, and a test that must
feed a raw character builds it at runtime from one, as this file does. A change that
must track one verbatim adds a named exemption with its reason, for its review to judge.
"""
from __future__ import annotations

import tempfile
import unicodedata
import unittest
from pathlib import Path

from _loader import load_task_registry_backup
from test_docs import ROOT, _NeedsGit, _tracked

_export = load_task_registry_backup().export
_hidden, _spelled = _export._hidden, _export.spelled

#: The two characters a text file may hold that ``_hidden`` counts.
_ALLOWED = frozenset({chr(9), chr(10)})


def hidden_characters(text: str) -> list[tuple[int, int, int]]:
    """(line, column, code point) of each hidden character in ``text``, both counted
    from 1. Lines end at a newline only: ``str.splitlines`` also splits on U+2028,
    U+0085 and a form feed and drops them, which would hide the very characters this
    looks for."""
    found = []
    for number, line in enumerate(text.split(chr(10)), 1):
        found += [(number, column, ord(c)) for column, c in enumerate(line, 1)
                  if c not in _ALLOWED and _hidden(c)]
    return found


def _describe(point: int) -> str:
    c = chr(point)
    name = unicodedata.name(c, "") or "control character"
    return f"U+{point:04X} {name}, written {_spelled(c)}"


def file_problems(path: Path, name: str) -> list[str]:
    """What is wrong with the file at ``path``, reported as ``name``: each hidden
    character it holds, or that it is not UTF-8. A symlink or anything else that is
    not a regular file is skipped: its target is not what is tracked, and may be
    outside the repo."""
    if path.is_symlink() or not path.is_file():
        return []
    # Bytes, not read_text(): universal newlines would turn a CR into a newline
    # before it could be seen.
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return [f"{name}: not UTF-8, so its characters are unread"]
    return [f"{name}:{line}:{column} {_describe(point)}"
            for line, column, point in hidden_characters(text)]


_B = chr(92)
MESSAGE = (
    "tracked files hold characters a reader cannot see. Write each as its escape: as "
    f"shown in Python; in JS and JSON as {_B}uXXXX up to U+FFFF, and above it "
    f"{_B}u{{XXXXX}} in JS and a surrogate pair in JSON; with printf in shell; or "
    "spell it U+XXXX in Markdown and YAML prose. A U+000D is a CRLF line ending: "
    "convert the file to LF. A file that must hold one verbatim needs a named "
    "exemption here, with its reason.")


class DetectorTests(unittest.TestCase):

    def test_each_kind_of_hidden_character_is_found_where_it_sits(self):
        for point in (0x202E, 0x2066, 0x200B, 0xFEFF, 0x2028, 0x0085, 0xFE0F,
                      0xE0100, 0x000D, 0x001B, 0x0000, 0x00AD, 0x200D):
            with self.subTest(f"U+{point:04X}"):
                text = "first\nab" + chr(point) + "c\n"
                self.assertEqual(hidden_characters(text), [(2, 3, point)])

    def test_a_hit_after_a_line_separator_or_form_feed_keeps_its_line(self):
        for sep in (0x2028, 0x000C):
            with self.subTest(f"U+{sep:04X}"):
                text = "one\nx" + chr(sep) + "y" + chr(0x202E) + "\n"
                self.assertEqual(hidden_characters(text),
                                 [(2, 2, sep), (2, 4, 0x202E)])

    def test_a_crlf_line_ending_is_found(self):
        self.assertEqual(hidden_characters("a\r\nb\n"), [(1, 2, 0x000D)])

    def test_tab_newline_and_ordinary_non_ascii_pass(self):
        # an em dash, a rightwards arrow and an a-ring, as the repo's prose uses them
        text = "\tindented " + chr(0x2014) + chr(0x2192) + chr(0x00E5) + "\n\n"
        self.assertEqual(hidden_characters(text), [])

    def test_a_hit_is_described_by_code_point_name_and_escape(self):
        self.assertEqual(_describe(0x202E),
                         "U+202E RIGHT-TO-LEFT OVERRIDE, written "
                         + chr(92) + "u202e")
        self.assertEqual(_describe(0x000D),
                         "U+000D control character, written " + chr(92) + "u000d")

    def test_an_astral_hit_gets_the_python_escape_and_the_message_the_js_and_json(self):
        self.assertEqual(_describe(0xE0100),
                         "U+E0100 VARIATION SELECTOR-17, written "
                         + chr(92) + "U000e0100")
        self.assertIn(chr(92) + "u{XXXXX} in JS", MESSAGE)
        self.assertIn("a surrogate pair in JSON", MESSAGE)


class FileReadingTests(unittest.TestCase):
    """``file_problems`` on real files, so how a file is read is tested, not only
    what is found in the text."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _file(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_a_crlf_line_ending_is_read_as_a_cr(self):
        path = self._file("crlf.txt", b"one\r\ntwo\n")
        self.assertEqual(file_problems(path, "crlf.txt"),
                         ["crlf.txt:1:4 U+000D control character, written "
                          + chr(92) + "u000d"])

    def test_a_file_that_is_not_utf8_fails_by_name(self):
        path = self._file("latin1.txt", b"caf" + bytes([0xE9]) + b"\n")
        self.assertEqual(file_problems(path, "latin1.txt"),
                         ["latin1.txt: not UTF-8, so its characters are unread"])

    def test_a_symlink_is_skipped_while_its_target_fails(self):
        target = self._file("target.txt", ("x" + chr(0x202E) + "y\n").encode())
        link = self.dir / "link.txt"
        link.symlink_to(target)
        self.assertEqual(file_problems(link, "link.txt"), [])
        self.assertEqual(len(file_problems(target, "target.txt")), 1)

    def test_a_clean_file_and_a_missing_one_pass(self):
        path = self._file("clean.txt", ("\tok " + chr(0x2014) + "\n").encode())
        self.assertEqual(file_problems(path, "clean.txt"), [])
        self.assertEqual(file_problems(self.dir / "gone.txt", "gone.txt"), [])


class TrackedFileTests(_NeedsGit):

    def test_no_tracked_file_holds_a_hidden_character(self):
        names = _tracked()
        self.assertLessEqual({"Makefile", "tests/test_hidden_characters.py"}, set(names),
                             "git could not read the index here, so this checked nothing")
        problems = [p for name in names for p in file_problems(ROOT / name, name)]
        self.assertEqual(problems, [], MESSAGE)


if __name__ == "__main__":
    unittest.main()
