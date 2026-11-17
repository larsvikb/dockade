# SPDX-License-Identifier: Apache-2.0
"""Retention for the store's audit table, for ``make audit-prune``: delete audit rows
and unspent call tokens older than ``AUDIT_RETENTION_DAYS``, then ``VACUUM``. Run by
``docker exec`` in the LIVE control-plane service, so it shares the app's view of the
store (``CONTROL_DB``, WAL mode). Not part of the process: nothing imports it, and the
image ships it only to be run.

    AUDIT_RETENTION_DAYS=30 python3 audit_prune.py

Only the audit table and ``tool_call_tokens`` are touched: rules, approvals and the
ingest cursor stay as they are. The window's default lives in the Makefile, which
always passes it; the one here only covers a run by hand.
"""
from __future__ import annotations

import os
import sqlite3
import time

# The app's own module, imported rather than restated: DB_PATH is defined once and an
# operator's CONTROL_DB override is honoured for free. WORKDIR is its directory.
import store


def prune(days: int) -> tuple[int, int]:
    """Delete what is older than ``days`` and return the space to the OS: how many
    audit rows, and how many unspent call tokens, went."""
    cutoff = time.time() - days * 86400
    conn = sqlite3.connect(store.DB_PATH, timeout=10.0,
                           autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL)
    try:
        # The app runs in WAL and the drain loop writes in short bursts; wait rather
        # than fail on a momentary lock. VACUUM below needs the write lock to itself.
        conn.execute("PRAGMA busy_timeout=10000")
        deleted = conn.execute("DELETE FROM audit WHERE ts < ?", (cutoff,)).rowcount
        # A token row is left when an outcome line never arrived to spend it. Absent on
        # a store a control plane older than the table last started.
        tokens = 0
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='tool_call_tokens'").fetchone():
            tokens = conn.execute("DELETE FROM tool_call_tokens WHERE created_at < ?",
                                  (cutoff,)).rowcount
        conn.commit()
        # DELETE frees pages inside the file but does not shrink it; VACUUM rebuilds
        # the file compactly and returns the space to the OS. Must run outside a
        # transaction, hence the commit above.
        conn.execute("VACUUM")
    finally:
        conn.close()
    return deleted, tokens


def main() -> None:
    days = int(os.environ.get("AUDIT_RETENTION_DAYS", "30"))
    deleted, tokens = prune(days)
    print(f"audit-prune: deleted {deleted} audit row(s) and {tokens} unspent call "
          f"token(s) older than {days}d, then VACUUM")


if __name__ == "__main__":
    main()
