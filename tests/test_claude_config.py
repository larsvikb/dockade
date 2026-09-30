# SPDX-License-Identifier: Apache-2.0
"""No committed Claude Code config acts, or grants itself permission, on its own.

Skills and agents are committed and reviewed like ``CLAUDE.md``; what would take effect
in a session without anyone being asked is refused, and this file is the one place that
says what. Why the line sits there: DESIGN.md "Durable host config lives outside the
repo".

Every rule is an allowed list, because Claude Code keeps adding files and keys that act
on their own and a list of bad ones is out of date the day after:

- under any ``.claude/`` directory, only skills and agents may be committed;
- their frontmatter may hold only plain ``key:`` lines with listed keys, between lines
  that are exactly ``---``;
- no symlink or submodule may be committed anywhere, since what it points at is never
  read here.

Paths compare case-insensitively: the host checkout sits on a case-insensitive
filesystem, where ``.Claude/Settings.json`` is ``.claude/settings.json``.

Each file is checked as staged and, where that differs, as it is on disk. A file the
index has never seen is out of reach, and so is the host checkout's live working tree
between runs; SECURITY.md accepts that channel under the read-write workspace mount.
"""
from __future__ import annotations

import re
import subprocess
import unittest

from test_docs import _GIT, ROOT, _NeedsGit

#: Frontmatter keys that make a skill or agent act on its own. The first three are what
#: Claude Code itself ignores in plugin-shipped agents "for security reasons";
#: `initialPrompt` is ignored there too.
REFUSED_KEYS = {
    "hooks": "registers hooks, which run commands",
    "mcpServers": "starts MCP servers, which run commands",
    "permissionMode": "changes the permission mode",
    "allowed-tools": "grants tools without a prompt, and workspace trust never gates it",
    "initialPrompt": "sends a prompt by itself when the agent starts a session",
}

#: Keys a skill or agent may set. None of them runs or grants anything (checked against
#: Claude Code's docs on 2026-09-30); add one only after the same check.
ALLOWED_KEYS = frozenset({
    "name", "description", "when_to_use", "argument-hint", "arguments",
    "disable-model-invocation", "user-invocable", "disallowed-tools", "model",
    "effort", "context", "agent", "paths", "shell", "license", "compatibility",
    "metadata", "tools", "disallowedTools", "color", "maxTurns", "skills", "memory",
    "background", "isolation", "omitClaudeMd", "keep-coding-instructions",
})

_KEY_LINE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)[ \t]*:(?:[ \t]|$)")
_SYMLINK, _GITLINK = "120000", "160000"


def parse_ls_files(raw: bytes) -> list[tuple[str, str, str]]:
    """``(mode, blob id, path)`` from ``git ls-files -z -s``. NUL-separated, so a name
    with a space or a non-ASCII byte arrives whole instead of split or C-quoted."""
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode, blob, _stage = meta.decode().split()
        entries.append((mode, blob, path.decode("utf-8", "surrogateescape")))
    return entries


def _under_claude(path: str) -> list[list[str]]:
    """What follows each ``.claude`` directory in the path, casefolded."""
    parts = path.casefold().split("/")
    return [parts[i + 1:] for i, part in enumerate(parts[:-1]) if part == ".claude"]


def _skill_or_agent(tail: list[str]) -> bool:
    if tail[0] == "skills":
        return len(tail) >= 3 and ".claude-plugin" not in tail
    return tail[0] == "agents" and len(tail) == 2 and tail[1].endswith(".md")


def path_problem(mode: str, path: str) -> str | None:
    """Why this index entry must not be committed, or None."""
    if mode == _SYMLINK:
        return "a symlink, whose target this check never reads"
    if mode == _GITLINK:
        return "a submodule, whose content this check never reads"
    if path.casefold().rsplit("/", 1)[-1] == ".mcp.json":
        return "declares MCP servers, which run as commands"
    if not all(_skill_or_agent(tail) for tail in _under_claude(path)):
        return "under .claude/, only skills/<name>/… and agents/<name>.md are allowed"
    return None


def has_frontmatter_to_check(path: str) -> bool:
    """A skill's or agent's markdown, whatever the case of its name."""
    return path.casefold().endswith(".md") and bool(_under_claude(path))


def frontmatter_problems(text: str) -> list[str]:
    """Why this markdown file's frontmatter must not be committed; empty if it may."""
    if text.startswith(chr(0xFEFF)):
        return ["a byte-order mark, which Claude Code reads past"]
    lines = text.splitlines()
    if not lines or not lines[0].startswith("---"):
        return []
    if lines[0] != "---":
        return ["line 1: frontmatter must open with a line that is exactly ---"]
    problems: list[str] = []
    seen_key = False
    for n, line in enumerate(lines[1:], start=2):
        if line == "---":
            return problems
        if line.startswith("---"):
            return [*problems, f"line {n}: frontmatter must close with exactly ---"]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            # A continuation of the key above. Before any key it is an indented
            # mapping, which YAML reads as the top level.
            if not seen_key:
                problems.append(f"line {n}: indented before any key")
            continue
        match = _KEY_LINE.match(line)
        if not match:
            problems.append(f"line {n}: not a plain `key:` line")
            continue
        seen_key = True
        key = match.group(1)
        if key in REFUSED_KEYS:
            problems.append(f"line {n}: `{key}` {REFUSED_KEYS[key]}")
        elif key not in ALLOWED_KEYS:
            problems.append(f"line {n}: `{key}` is not on the allowed list")
    return [*problems, "the frontmatter never closes"]


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 (absolute path from shutil.which, fixed args)
        [_GIT, "-C", str(ROOT), *args], capture_output=True, check=False)


def _versions(blob: str, path: str) -> list[bytes]:
    """The staged content, and the file on disk when that differs: a commit carries the
    first, a session on this checkout loads the second. (`git add -N` stages an empty
    blob, so the staged copy alone would miss a file that exists only on disk.)"""
    staged = _git("cat-file", "blob", blob).stdout
    local = ROOT / path
    if local.is_file():
        on_disk = local.read_bytes()
        if on_disk != staged:
            return [staged, on_disk]
    return [staged]


class CommittedConfigTests(_NeedsGit):

    def test_no_committed_claude_code_config_acts_by_itself(self):
        listing = _git("ls-files", "-z", "-s")
        entries = parse_ls_files(listing.stdout) if listing.returncode == 0 else []
        self.assertIn("Makefile", {path for _, _, path in entries},
                      "git could not read the index here, so this checked nothing")
        problems = []
        for mode, blob, path in entries:
            reason = path_problem(mode, path)
            if reason:
                problems.append(f"{path}: {reason}")
                continue
            if not has_frontmatter_to_check(path):
                continue
            for raw in _versions(blob, path):
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    problems.append(f"{path}: not UTF-8, so its frontmatter is unread")
                    continue
                problems += [f"{path}: {p}" for p in frontmatter_problems(text)]
        self.assertEqual(problems, [],
                         "committed Claude Code config that would act by itself")


class FrontmatterTests(unittest.TestCase):

    def test_what_may_be_committed(self):
        cases = {
            "plain skill": "---\nname: e\ndescription: x\n---\nbody\n",
            "hooks only in the body": "---\nname: b\n---\nhooks: here\n",
            "no frontmatter": "hooks: on line 1, but this is body text\n",
            "folded value mentioning hooks":
                "---\ndescription: >\n  hooks: is prose here\n  more\nname: f\n---\n",
            "comments and blank lines": "---\n# a comment\n\nname: c\n---\n",
            "CRLF": "---\r\nname: r\r\ndescription: x\r\n---\r\nbody\r\n",
            "nested key under an allowed one": "---\nmetadata:\n  hooks: no\n---\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertEqual(frontmatter_problems(text), [])

    def test_what_is_refused(self):
        bom = chr(0xFEFF)
        cases = {
            "hooks": "---\nname: a\nhooks:\n  Stop: []\n---\n",
            "mcpServers": "---\nmcpServers:\n  x: {command: sh}\n---\n",
            "permissionMode": "---\npermissionMode: bypassPermissions\n---\n",
            "allowed-tools": "---\nallowed-tools: Bash\n---\n",
            "initialPrompt": "---\ninitialPrompt: go\n---\n",
            "spaced key": "---\nhooks :\n---\n",
            "CRLF with hooks": "---\r\nname: f\r\nhooks:\r\n---\r\n",
            "double-quoted key": '---\n"hooks": {}\n---\n',
            "single-quoted key": "---\n'hooks' : {}\n---\n",
            "escaped quoted key": '---\n"hook\\x73": {}\n---\n',
            "explicit key": "---\n? hooks\n: {}\n---\n",
            "flow mapping": "---\n{name: t, hooks: {}}\n---\n",
            "indented mapping": "---\n  name: t\n  hooks: {}\n---\n",
            "merge key": "---\nmetadata: &m {x: 1}\n<<: *m\n---\n",
            "byte-order mark first": bom + "---\nhooks: {}\n---\n",
            "U+FEFF after the opener": "---" + bom + "\nallowed-tools: Bash\n---\n",
            "trailing space after the opener": "--- \nallowed-tools: Bash\n---\n",
            "a closer that is not exactly ---": "---\nname: x\n--- \nhooks: {}\n---\n",
            "unknown key": "---\nversion: 1\n---\n",
            "never closes": "---\nname: n\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertNotEqual(frontmatter_problems(text), [])


class PathTests(unittest.TestCase):

    def test_what_is_refused(self):
        for mode, path in [
            ("100644", ".claude/settings.json"),
            ("100644", ".claude/settings.local.json"),
            ("100644", "control-plane/.claude/settings.json"),
            ("100644", ".Claude/Settings.json"),
            ("100644", ".claude/scheduled_tasks.json"),
            ("100644", ".claude/workflows/w.yaml"),
            ("100644", ".claude/launch.json"),
            ("100644", ".claude/routines/r.md"),
            ("100644", ".claude/commands/c.md"),
            ("100644", ".claude/skills/SKILL.md"),
            ("100644", ".claude/agents/team/a.md"),
            ("100644", ".claude/skills/d/.claude-plugin/plugin.json"),
            ("100644", ".claude/skills/y/.claude/settings.json"),
            ("100644", ".mcp.json"),
            ("100644", ".MCP.json"),
            ("100644", "control-plane/.mcp.json"),
            (_SYMLINK, ".claude"),
            (_SYMLINK, "docs/link"),
            (_GITLINK, ".claude/skills/sub"),
            (_GITLINK, "vendor/lib"),
        ]:
            with self.subTest(path=path, mode=mode):
                self.assertIsNotNone(path_problem(mode, path))

    def test_what_may_be_committed(self):
        for path in [
            ".claude/skills/review/SKILL.md",
            ".claude/skills/review/scripts/probe.sh",
            ".claude/agents/reviewer.md",
            "control-plane/.claude/skills/x/SKILL.md",
            "docs/settings.json",
            "tests/.claude-plugin/x.json",
        ]:
            with self.subTest(path=path):
                self.assertIsNone(path_problem("100644", path))

    def test_which_files_have_their_frontmatter_checked(self):
        for path, checked in [
            (".claude/skills/c/SKILL.md", True),
            (".claude/skills/c/SKILL.MD", True),
            (".Claude/agents/a.md", True),
            (".claude/skills/c/notes.txt", False),
            ("docs/x.md", False),
        ]:
            with self.subTest(path=path):
                self.assertEqual(has_frontmatter_to_check(path), checked)

    def test_names_with_spaces_and_non_ascii_bytes_arrive_whole(self):
        raw = ("100644 " + "a" * 40 + " 0\t.claude/skills/a b/SKILL.md\0"
               "100644 " + "b" * 40 + " 0\tdé/.claude/settings.json\0").encode()
        paths = [path for _, _, path in parse_ls_files(raw)]
        self.assertEqual(paths, [".claude/skills/a b/SKILL.md",
                                 "dé/.claude/settings.json"])
        self.assertIsNotNone(path_problem("100644", paths[1]))


if __name__ == "__main__":
    unittest.main()
