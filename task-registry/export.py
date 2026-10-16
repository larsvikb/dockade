# SPDX-License-Identifier: Apache-2.0
"""A backup of the task registry as markdown, for reading without the server: per repo
and state, each task's fields, body, claim, plans and notes, the superseded plans and
hidden notes marked. One-way:
nothing reads it back, so the ``.db`` beside it stays the only backup format.

    python3 export.py < tasks.db > tasks.md

``make backup`` runs it on the snapshot it has just written, in a container from the
registry's image with no network and no volume (the Makefile's ``TASKS_EXPORT``).

Agents wrote most of what it shows, so none of that is markdown here: a value on one
line is a code span, a longer text a fenced block whose fence outruns every backtick
run inside it, and control, format and separator characters are spelled out. No text
can render as a link, an image or HTML, pass for one of this file's own headings, or
reorder what the reader sees.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import UTC, datetime

import taskdb
import tasks

#: Characters a reader cannot see, or that move what is around them: controls, format
#: characters (bidi overrides, zero-widths) and the line and paragraph separators.
_HIDDEN = frozenset({"Cc", "Cf", "Zl", "Zp"})

#: And whatever Unicode says to draw as nothing, its Default_Ignorable_Code_Point
#: ranges (DerivedCoreProperties.txt, 15.0). Variation selectors above all: a run of
#: them after one letter carries text a reader never sees and an agent still reads.
_IGNORABLE = frozenset(
    cp for lo, hi in ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C),
                      (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
                      (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F),
                      (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
                      (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
                      (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
    for cp in range(lo, hi + 1))

_TICKS = re.compile(r"`+")


def _hidden(c: str) -> bool:
    return ord(c) in _IGNORABLE or unicodedata.category(c) in _HIDDEN


def spelled(text: str, keep: str = "") -> str:
    """``text`` with every hidden character but those in ``keep`` written as its
    escape, ``\\u202e``."""
    return "".join(
        c if c in keep or not _hidden(c)
        else (f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
        for c in text)


def _ticks(text: str) -> int:
    return max((len(run) for run in _TICKS.findall(text)), default=0)


def inline(text: str) -> str:
    """One line of stored text as a code span, which renders nothing inside it."""
    text = spelled(str(text))
    if not text:
        return "(empty)"
    fence = "`" * (_ticks(text) + 1)
    # CommonMark takes one space off each end when both ends have one: pad, so the span
    # shows the text exactly, and a backtick at an end does not join the fence.
    pad = " " if (text[0] == "`" or text[-1] == "`"
                  or (text[0] == " " and text[-1] == " ")) else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def block(text: str) -> str:
    """Stored text of any length as a fenced block, which renders nothing inside it
    and which no line of it can close."""
    text = spelled(str(text), keep="\n\t")
    fence = "`" * max(3, _ticks(text) + 1)
    return f"{fence}text\n{text}\n{fence}"


def when(ts: object) -> str:
    return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _word(value: object, known: tuple[str, ...]) -> str:
    # A field the store checks against a fixed list; anything else is shown as text.
    return value if value in known else inline(str(value))


def render(conn: sqlite3.Connection, now: float) -> str:
    """The store on ``conn``, a registry store at a schema version this code reads, as
    markdown."""
    conn.row_factory = sqlite3.Row
    repos = conn.execute("SELECT * FROM repos ORDER BY name, id").fetchall()
    all_tasks = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    states = {int(t["id"]): t["state"] for t in all_tasks}
    claims = {int(c["task_id"]): c for c in conn.execute("SELECT * FROM claims")}
    blockers: dict[int, list[int]] = {}
    for b in conn.execute("SELECT task_id, blocker_id FROM blockers "
                          "ORDER BY task_id, blocker_id"):
        blockers.setdefault(int(b["task_id"]), []).append(int(b["blocker_id"]))
    notes: dict[int, list[sqlite3.Row]] = {}
    for n in conn.execute("SELECT * FROM notes ORDER BY id"):
        notes.setdefault(int(n["task_id"]), []).append(n)
    plans = _plans(conn)
    hidden = sum(n["hidden_at"] is not None for ns in notes.values() for n in ns)

    out = [
        "# Task registry export",
        "",
        f"Rendered {when(now)} from a schema-v{_version(conn)} store: "
        f"{len(repos)} repos, {len(all_tasks)} tasks, "
        f"{sum(map(len, notes.values()))} notes ({hidden} hidden).",
        "",
        "For reading without the server, and never read back: the `.db` beside this "
        "file is the backup. Stored text is shown as stored, one line in a code span "
        "and more in a fenced block, with control, format and separator characters "
        "spelled out as `\\uXXXX`, so none of it renders as markdown.",
    ]
    for repo in repos:
        facts = []
        if repo["remote"] is not None:
            facts.append(f"- remote {inline(repo['remote'])}")
        if repo["retired_at"] is not None:
            facts.append(f"- retired {when(repo['retired_at'])}, taking no new task")
        out += ["", f"## Repo {inline(repo['name'])}", *(["", *facts] if facts else [])]
        mine = [t for t in all_tasks if t["repo_id"] == repo["id"]]
        if not mine:
            out += ["", "No tasks."]
            continue
        order = list(tasks.STATES) + sorted({t["state"] for t in mine} - set(tasks.STATES),
                                            key=str)
        for state in order:
            group = [t for t in mine if t["state"] == state]
            if group:
                out += ["", f"### {_word(state, tasks.STATES)} ({len(group)})"]
                for task in group:
                    out += _task(task, states, claims, blockers, notes, plans)
    return "\n".join(out) + "\n"


def _task(task: sqlite3.Row, states: dict[int, str], claims: dict[int, sqlite3.Row],
          blockers: dict[int, list[int]], notes: dict[int, list[sqlite3.Row]],
          plans: dict[int, list[tuple[sqlite3.Row, list[str]]]]) -> list[str]:
    tid = int(task["id"])
    facts = [f"state {_word(task['state'], tasks.STATES)}"]
    if task["severity"] is not None:
        facts.append(f"severity {_word(task['severity'], tasks.SEVERITIES)}")
    if task["kind"] is not None:
        facts.append(f"kind {_word(task['kind'], tasks.KINDS)}")
    facts.append(f"revision {int(task['revision'])}")
    lines = ["", f"#### #{tid} {inline(task['title'])}", "",
             f"- {', '.join(facts)}",
             f"- created {when(task['created_at'])}, updated {when(task['updated_at'])}"]
    if task["parent_id"] is not None:
        lines.append(f"- parent #{int(task['parent_id'])}")
    if tid in blockers:
        lines.append("- blocked by " + ", ".join(
            f"#{b} ({_word(states.get(b, 'missing'), tasks.STATES)})"
            for b in blockers[tid]))
    claim = claims.get(tid)
    if claim is not None:
        # A claim to refine a task has no branch.
        branch = ("" if claim["branch"] is None
                  else f" on branch {inline(claim['branch'])}")
        lines.append(f"- claimed by {inline(claim['holder'])}{branch}, "
                     f"{when(claim['claimed_at'])}, until {when(claim['expires_at'])}; "
                     f"footprint {_footprint(claim['footprint'])}")
        if claim["progress"] is not None:
            lines += ["", "Progress:", "", block(claim["progress"])]
    lines += ["", "Body:", "", block(task["body"])]
    for plan, paths in plans.get(tid, []):
        lines += ["", _plan_line(plan) + ":", "",
                  "- footprint " + (", ".join(inline(p) for p in paths) or "(none)")]
        for part in ("approach", "tests", "out_of_scope", "risks", "review_verdict"):
            lines += ["", f"{part.replace('_', ' ').capitalize()}:", "", block(plan[part])]
    mine = notes.get(tid, [])
    if mine:
        hidden = sum(n["hidden_at"] is not None for n in mine)
        lines += ["", f"Notes, oldest first ({len(mine)}, {hidden} hidden):"]
        for note in mine:
            lines += ["", _note_line(note) + ":", "", block(note["text"])]
    return lines


def _footprint(stored: str) -> str:
    # Stored as a JSON list of paths; shown as the text it is if it is anything else.
    try:
        paths = json.loads(stored)
    except ValueError:
        paths = None
    if isinstance(paths, list) and paths and all(isinstance(p, str) for p in paths):
        return ", ".join(inline(p) for p in paths)
    return inline(stored)


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _plans(conn: sqlite3.Connection) -> dict[int, list[tuple[sqlite3.Row, list[str]]]]:
    """Every plan by task, oldest first, with its paths; none from a v1 store, which
    has no plans table."""
    if _version(conn) < 2:
        return {}
    paths: dict[int, list[str]] = {}
    for p in conn.execute("SELECT plan_id, path FROM plan_paths ORDER BY rowid"):
        paths.setdefault(int(p["plan_id"]), []).append(p["path"])
    plans: dict[int, list[tuple[sqlite3.Row, list[str]]]] = {}
    for plan in conn.execute("SELECT * FROM plans ORDER BY id"):
        plans.setdefault(int(plan["task_id"]), []).append(
            (plan, paths.get(int(plan["id"]), [])))
    return plans


def _plan_line(plan: sqlite3.Row) -> str:
    parts = [f"Plan {int(plan['id'])}", f"by {inline(plan['author'])}",
             when(plan["created_at"]), f"at task revision {int(plan['task_revision'])}",
             f"{int(plan['review_rounds'])} review round(s)"]
    if plan["superseded_at"] is not None:
        parts.append(f"**SUPERSEDED** {when(plan['superseded_at'])}")
    return ", ".join(parts)


def _note_line(note: sqlite3.Row) -> str:
    parts = [f"Note {int(note['id'])}", f"by {inline(note['author'])}",
             when(note["created_at"])]
    if note["for_operator"]:
        parts.append("for the operator")
    if note["handled_at"] is not None:
        parts.append(f"handled {when(note['handled_at'])}")
    if note["reply_to"] is not None:
        parts.append(f"replying to note {int(note['reply_to'])}")
    if note["move"] is not None:
        parts.append(f"with {inline(note['move'])}")
    if note["hidden_at"] is not None:
        parts.append(f"**HIDDEN** {when(note['hidden_at'])}, out of every agent's read")
    return ", ".join(parts)


def main(data: bytes) -> str:
    """The backup ``data`` as markdown; a SystemExit naming why for anything that is
    not a registry store this code reads."""
    if not data.startswith(b"SQLite format 3\x00"):
        sys.exit("export: the input is not a SQLite database")
    # Bytes 18 and 19 say rollback (1) or WAL (2), and a database in memory cannot be
    # WAL: read as rollback whatever the file says, so no SQLite's VACUUM INTO header
    # decides whether the export runs.
    data = data[:18] + b"\x01\x01" + data[20:]
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(data)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        readable = [v for v in taskdb.OPENS if v]
        if version not in readable:
            sys.exit(f"export: the backup is schema v{version}, this export reads "
                     f"{', '.join(f'v{v}' for v in readable)}")
        return render(conn, time.time())
    finally:
        conn.close()


if __name__ == "__main__":
    sys.stdout.buffer.write(main(sys.stdin.buffer.read()).encode())
