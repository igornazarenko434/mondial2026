"""Run-status ledger — answers "did each match-window job succeed, fall back,
or fail, and why?".

Every pipeline run writes a row: started -> ok | failed (with detail, provider,
attempts, whether a card was delivered). This is how you know the system's state
without watching it live, and how the daily health summary is built.
"""
from __future__ import annotations
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from config import observability as cfg

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    match_id INTEGER, window TEXT,
    status TEXT,            -- started | ok | failed
    fell_back INTEGER DEFAULT 0,
    provider TEXT,          -- e.g. which odds/llm source actually served
    attempts INTEGER DEFAULT 1,
    card_delivered INTEGER DEFAULT 0,
    detail TEXT,            -- error / note
    correlation_id TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunLedger:
    def __init__(self, db: str | sqlite3.Connection | None = None):
        self.conn = (db if isinstance(db, sqlite3.Connection)
                     else sqlite3.connect(db or ":memory:", check_same_thread=False))
        self._lock = threading.Lock()
        self.conn.execute(_DDL); self.conn.commit()

    def start(self, match_id, window, correlation_id="-") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO runs (started_at,match_id,window,status,correlation_id)"
                " VALUES (?,?,?, 'started', ?)", (_now(), match_id, window, correlation_id))
            self.conn.commit()
            return cur.lastrowid

    def finish(self, run_id, status, *, provider=None, attempts=1,
               fell_back=False, card_delivered=False, detail=None):
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=?, provider=?, attempts=?,"
                " fell_back=?, card_delivered=?, detail=? WHERE id=?",
                (_now(), status, provider, attempts, int(fell_back),
                 int(card_delivered), detail, run_id))
            self.conn.commit()

    def was_handled(self, match_id, window) -> bool:
        """True if this (match, window) already ran (any status) — survives
        restarts, so the scheduler never re-sends a card after a crash."""
        row = self.conn.execute(
            "SELECT 1 FROM runs WHERE match_id=? AND window=? LIMIT 1",
            (match_id, window)).fetchone()
        return row is not None

    def stuck(self, older_than_min: int = 20) -> list[dict]:
        """Runs that started but never finished (crashed/hung mid-execution)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_min)).isoformat()
        cur = self.conn.execute(
            "SELECT id,match_id,window,started_at FROM runs"
            " WHERE status='started' AND started_at<?", (cutoff,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def recent(self, hours: int = 24) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cur = self.conn.execute(
            "SELECT match_id,window,status,fell_back,provider,card_delivered,detail"
            " FROM runs WHERE started_at>=? ORDER BY started_at", (since,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def summary(self, hours: int = 24) -> dict:
        rows = self.recent(hours)
        return {
            "total": len(rows),
            "ok": sum(r["status"] == "ok" for r in rows),
            "failed": sum(r["status"] == "failed" for r in rows),
            "stuck": sum(r["status"] == "started" for r in rows),   # never finished!
            "fallbacks": sum(bool(r["fell_back"]) for r in rows),
            "cards_delivered": sum(bool(r["card_delivered"]) for r in rows),
            "failures": [r for r in rows if r["status"] != "ok"],
        }


_LEDGER: RunLedger | None = None


def runs() -> RunLedger:
    global _LEDGER
    if _LEDGER is None:
        try:
            _LEDGER = RunLedger(cfg.OBS_DB)
        except Exception:
            _LEDGER = RunLedger(":memory:")
    return _LEDGER
