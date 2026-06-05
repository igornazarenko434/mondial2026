"""SQLite helpers."""
from __future__ import annotations
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "mondial.db")
SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
