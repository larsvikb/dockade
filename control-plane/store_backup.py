# SPDX-License-Identifier: Apache-2.0
"""The control plane's store copied out and put back, for ``make backup`` and ``make
restore``: run in a throwaway container from the control plane's image with the
store's volume mounted (the Makefile's ``CONTROL_TOOL``, whose comment says why not
through the service), so it works with the stack down. Not part of the process:
nothing imports it, and the image ships it only to be run.

    python3 store_backup.py snapshot  > control.db   # the store, on stdout
    python3 store_backup.py restore   < control.db   # replace the store with it

STDOUT IS A SNAPSHOT'S TRANSPORT — the store's bytes and nothing else, so every line
for a human goes to stderr, or it would end up inside the .db file. That is why the
alternative (write into the container, ``docker cp`` it out) was not taken: it needs a
RUNNING container to copy from, and the moment you most want a backup is the moment
the stack is down. A restore reads the backup on stdin, the mirror of that, so the
file never has to be copied into a container that may not be running.

``task-registry/backup.py`` is the task registry's twin, and says where it differs.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from typing import BinaryIO, NoReturn

# The app's own module, imported rather than restated: DB_PATH is defined once and an
# operator's CONTROL_DB override is honoured for free. WORKDIR is its directory.
import store

#: A restore's exit status when the store was replaced and a sidecar of the old one
#: could not be removed: ``make restore`` does not start the stack on it. Its own
#: value, as a refusal and an uncaught error both exit 1. The Makefile's
#: ``RESTORE_SIDECAR_LEFT``, and ``backup.SIDECAR_LEFT`` in the task registry.
SIDECAR_LEFT = 3

#: The tables a file must hold to be a control-plane store.
TABLES = ("rules", "audit", "approvals", "audit_cursor")

#: What a snapshot and a restore report counting.
COUNTED = ("rules", "approvals", "audit")


def say(text: str) -> None:
    print(text, file=sys.stderr)


def snapshot(out: BinaryIO) -> None:
    """Write a consistent copy of the store to ``out``. Safe against a live stack."""
    src = store.DB_PATH
    if not os.path.exists(src):
        sys.exit(f"backup: no store at {src} — nothing to back up (has it ever run?)")

    # A sibling of the store, so it is on the same filesystem as the file being copied
    # and inside the volume the container can write.
    snap = src + ".backup-snapshot"
    if os.path.exists(snap):
        os.unlink(snap)

    conn = sqlite3.connect(src, timeout=10.0)
    try:
        # The app is (or may be) live and writing in short bursts; wait rather than fail.
        conn.execute("PRAGMA busy_timeout=10000")
        # VACUUM INTO, not a file copy: it takes a read lock and writes a standalone,
        # compacted database that already includes everything in the WAL. So it is
        # consistent against a live store, and the result has NO -wal/-shm sidecar —
        # which is what lets `restore` be a single file move rather than a three-file
        # dance.
        conn.execute("VACUUM INTO ?", (snap,))
    finally:
        conn.close()

    try:
        snapconn = sqlite3.connect(snap)
        counts = {t: snapconn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 (fixed names)
                  for t in COUNTED}
        version = snapconn.execute("PRAGMA user_version").fetchone()[0]
        snapconn.close()

        with open(snap, "rb") as f:
            shutil.copyfileobj(f, out)
        out.flush()
    finally:
        os.unlink(snap)

    say(f"backup: schema v{version}, {_counted(counts)}")


def restore(inp: BinaryIO) -> None:
    """Replace the store with the backup on ``inp``, refusing first anything that is not
    a control-plane store this build can run. Run only with the stack stopped, as
    ``make restore`` does: nothing may hold the store, or its sidecars, while they are
    replaced."""
    dst = store.DB_PATH
    tmp = dst + ".restore-incoming"

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(tmp, "wb") as f:
        shutil.copyfileobj(inp, f)

    def reject(why: str) -> NoReturn:
        """Refuse BEFORE the replace, and take the half-written file with us. Every
        check below is here because passing it is what makes the destructive step
        safe — after os.replace there is nothing left to compare against."""
        os.unlink(tmp)
        sys.exit(f"restore: REFUSED, store untouched — {why}")

    try:
        conn = sqlite3.connect(tmp)
        # Closed before any refusal unlinks the file: SQLite still holding a file with
        # a WAL-mode header open leaves its -wal and -shm behind the unlink.
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 (fixed names)
                      for t in COUNTED if t in tables}
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        reject(f"not a readable SQLite database ({e})")

    if integrity != "ok":
        reject(f"integrity_check says {integrity!r}")
    missing = set(TABLES) - tables
    if missing:
        reject(f"not a control-plane store — missing table(s): {', '.join(sorted(missing))}")
    # A backup from a NEWER build carries a schema this code has no steps for, and
    # migration only runs forwards: restoring it would leave the app reading columns it
    # does not understand, or writing rows the newer build would misread. The fix is to
    # update the image, not to force it, so this refuses rather than warns.
    if version > store.SCHEMA_VERSION:
        reject(f"backup is schema v{version}, this control plane understands "
               f"v{store.SCHEMA_VERSION} — update the image first")

    try:
        os.replace(tmp, dst)
    except OSError as e:
        reject(f"could not replace {dst} ({e})")
    # Stale sidecars from the store just replaced. A -wal left behind belongs to a
    # DIFFERENT database file and applying it over the restored one is how a good
    # backup becomes a corrupt store, so failing to remove it is fatal, not a warning.
    # Safe here because `restore` stops the stack first: nothing has the file open.
    for side in ("-wal", "-shm"):
        try:
            os.unlink(dst + side)
        except FileNotFoundError:
            pass
        except OSError as e:
            say(f"restore: RESTORED but could not remove {dst + side} ({e}) — "
                f"remove it before starting the control plane")
            sys.exit(SIDECAR_LEFT)

    print(f"restore: store replaced from a schema-v{version} backup ({_counted(counts)})")


def _counted(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {t}" for t, n in counts.items())


def main(argv: list[str]) -> int:
    if argv == ["snapshot"]:
        snapshot(sys.stdout.buffer)
        return 0
    if argv == ["restore"]:
        restore(sys.stdin.buffer)
        return 0
    say("usage: python3 store_backup.py snapshot > FILE | restore < FILE")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
