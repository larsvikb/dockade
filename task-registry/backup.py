# SPDX-License-Identifier: Apache-2.0
"""The task registry's store copied out and put back, for ``make backup`` and
``make restore-tasks``: run in a throwaway container from the registry's image with the
store's volume mounted (the Makefile's ``TASKS_TOOL``), never through the service, so
it works with the registry stopped.

The control plane's twins are ``BACKUP_PY`` and ``RESTORE_PY`` in the Makefile, and
the reasoning beside them holds here: ``VACUUM INTO`` for a consistent single file from
a live store, stdout and stdin as the transport, every check before the replace. What
differs is said where it differs.

    python3 backup.py snapshot  > tasks.db     # the store, on stdout
    python3 backup.py restore   < tasks.db     # replace the store with it
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from typing import BinaryIO, NoReturn

import taskdb

#: The registry's tables: a file without every one is not a registry store, but for
#: the ones a later version added, which a store from before it lacks.
TABLES = ("repos", "tasks", "claims", "blockers", "notes", "history", "plans",
          "plan_paths")
ADDED_IN = {"plans": 2, "plan_paths": 2}

#: What a snapshot and a restore report counting.
COUNTED = ("repos", "tasks", "notes")


def say(text: str) -> None:
    # stdout carries the store's bytes, so every line for a human goes to stderr.
    print(text, file=sys.stderr)


def snapshot(out: BinaryIO) -> bool:
    """Write a consistent copy of the store to ``out``; False, having written nothing,
    when there is no store yet. The volume can exist without one, made at the service's
    first start, which may have stopped before the store was opened."""
    src = taskdb.DB_PATH
    if not os.path.exists(src):
        say(f"backup: the task registry's volume holds no store at {src} yet; "
            f"nothing to back up")
        return False
    snap = src + ".backup-snapshot"
    if os.path.exists(snap):
        os.unlink(snap)
    conn = sqlite3.connect(src, timeout=10.0)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("VACUUM INTO ?", (snap,))
    finally:
        conn.close()
    try:
        version, counts = _describe(snap)
        with open(snap, "rb") as f:
            shutil.copyfileobj(f, out)
        out.flush()
    finally:
        os.unlink(snap)
    say(f"backup: task registry schema v{version}, {_counted(counts)}")
    return True


def restore(inp: BinaryIO) -> None:
    """Replace the store with the backup on ``inp``, refusing first anything the
    registry would not open. Run only with the registry stopped: nothing may hold the
    store, or its sidecars, while they are replaced."""
    dst = taskdb.DB_PATH
    tmp = dst + ".restore-incoming"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(tmp, "wb") as f:
        shutil.copyfileobj(inp, f)

    def reject(why: str) -> NoReturn:
        os.unlink(tmp)
        sys.exit(f"restore-tasks: REFUSED, store untouched — {why}")

    try:
        conn = sqlite3.connect(tmp)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            # A copy of a WAL store says so in its header, and every later open of it
            # would make sidecars beside it: one file, as a snapshot is.
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        reject(f"not a readable SQLite database ({e})")
    if integrity != "ok":
        reject(f"integrity_check says {integrity!r}")
    missing = {t for t in TABLES if t not in tables
               and not (0 < version < ADDED_IN.get(t, 0))}
    if missing:
        reject(f"not a task registry store — missing table(s): {', '.join(sorted(missing))}")
    # taskdb.init refuses a version outside `taskdb.OPENS`: a file it would refuse
    # leaves the registry down with its old store gone, so it is refused here first. An
    # older version it opens is taken forward at the registry's next start.
    if version > taskdb.SCHEMA_VERSION:
        reject(f"backup is schema v{version}, this registry understands "
               f"v{taskdb.SCHEMA_VERSION} — update the image first")
    if version not in taskdb.OPENS:
        reject(f"backup is schema v{version}, and this registry opens "
               f"{', '.join(f'v{v}' for v in taskdb.OPENS if v)} only")

    _keep_ids_moving(dst, tmp)
    _, counts = _describe(tmp)
    try:
        os.replace(tmp, dst)
    except OSError as e:
        # The store's directory is sticky, so a store another uid owns cannot be
        # replaced by this one.
        reject(f"could not replace {dst} ({e})")
    # As RESTORE_PY: a -wal left behind belongs to the store just replaced, and applied
    # over this one corrupts it.
    for side in ("-wal", "-shm"):
        try:
            os.unlink(dst + side)
        except FileNotFoundError:
            pass
        except OSError as e:
            sys.exit(f"restore-tasks: RESTORED but could not remove {dst + side} ({e}) "
                     f"— remove it before starting the registry")
    say(f"restore-tasks: store replaced from a schema-v{version} backup ({_counted(counts)})")


def _keep_ids_moving(dst: str, incoming: str) -> None:
    """Carry the replaced store's id counters into the incoming one where they are
    higher. Ids are never reused (``taskdb``'s AUTOINCREMENT), so a task id a session
    or the gateway's audit holds cannot come to name a newer task; an older backup
    would otherwise hand out again the ids of every task filed since it was taken. The
    counters only; no row of the replaced store survives."""
    if not os.path.exists(dst):
        return
    try:
        old = sqlite3.connect(dst)
        try:
            counters = old.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
        finally:
            old.close()
    except sqlite3.DatabaseError as e:
        # The store being replaced may be the broken one this restore is for.
        say(f"restore-tasks: could not read the current store's id counters ({e}); "
            f"ids continue from the backup's")
        return
    conn = sqlite3.connect(incoming)
    try:
        with conn:
            for name, seq in counters:
                updated = conn.execute(
                    "UPDATE sqlite_sequence SET seq = max(seq, ?) WHERE name = ?",
                    (seq, name)).rowcount
                if not updated:
                    conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                                 (name, seq))
    finally:
        conn.close()


def _describe(path: str) -> tuple[int, dict[str, int]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 (fixed names)
                  for t in COUNTED}
    finally:
        conn.close()
    return version, counts


def _counted(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {t}" for t, n in counts.items())


def main(argv: list[str]) -> int:
    if argv == ["snapshot"]:
        snapshot(sys.stdout.buffer)
        return 0
    if argv == ["restore"]:
        restore(sys.stdin.buffer)
        return 0
    say("usage: python3 backup.py snapshot > FILE | restore < FILE")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
