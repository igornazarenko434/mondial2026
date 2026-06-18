"""SQLite helpers."""
from __future__ import annotations
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "mondial.db")
SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")

# How long a connection waits for a write lock before raising
# `sqlite3.OperationalError: database is locked`. The daemon dispatches up to
# `SCHED_MAX_WORKERS` (default 6) match jobs concurrently; without WAL +
# busy_timeout, two workers UPSERTing into `predictions` in the same tick can
# collide on the rollback-journal lock and one will fail. 10s is comfortably
# above any single write we do (most are <50 ms).
_BUSY_TIMEOUT_MS = 10_000


def _configure(conn: sqlite3.Connection) -> None:
    """Apply concurrency-safe PRAGMAs to a freshly opened connection.

    - journal_mode=WAL: writers don't block readers, readers don't block writers
    - synchronous=NORMAL: durable under WAL + safe across power loss for our
      use case (we never claim transactional durability beyond crash-recovery)
    - busy_timeout: per-connection wait window for the next write lock

    WAL is a database-level setting (persists once set on a file DB). The
    PRAGMA is idempotent on subsequent connects. `:memory:` databases ignore
    the journal_mode=WAL request gracefully — SQLite leaves them in MEMORY mode.
    """
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.DatabaseError:
        # Stay resilient: a sick file shouldn't prevent the daemon from
        # opening a connection (the caller will likely fail on the next
        # operation anyway with a clearer error).
        pass


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    # timeout= seeds the C-level busy handler so the first transaction also
    # benefits from the wait window (PRAGMA below covers all subsequent ones).
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    _configure(conn)
    return conn


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = connect(path)
    with open(SCHEMA) as f:
        conn.executescript(f.read())
    conn.commit()
    return conn


if __name__ == "__main__":
    init_db()
    print(f"initialised {DB_PATH}")
