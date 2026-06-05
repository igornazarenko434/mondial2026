"""Seed the matches table from the bundled CSV fixtures — lets you dry-run the
scheduler/pipeline on Day 1 BEFORE wiring the live football-data key.

Reads data/wc2026_detonator_fixtures.csv (date + Israel kickoff time), converts to
UTC, normalizes team names, and upserts. Replace with live `football_data.ingest`
once you have an API key.
"""
from __future__ import annotations
import csv
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from core.data.teams import normalize

CSV = os.path.join(os.path.dirname(__file__), "..", "data", "wc2026_detonator_fixtures.csv")
ISRAEL = ZoneInfo("Asia/Jerusalem")


def seed(conn, csv_path: str = CSV) -> int:
    rows = 0
    with open(csv_path) as f:
        for i, r in enumerate(csv.DictReader(f), start=9001):
            if r["date"] == "TBD" or not r["home"] or r["home"] == "TBD":
                continue
            local = datetime.fromisoformat(f"{r['date']}T{r['kickoff_israel_time']}:00").replace(tzinfo=ISRAEL)
            utc = local.astimezone(ZoneInfo("UTC"))
            conn.execute("""INSERT INTO matches
                (match_id, utc_kickoff, local_kickoff, stage, grp, home, away, status, detonator)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(match_id) DO UPDATE SET utc_kickoff=excluded.utc_kickoff""",
                (i, utc.isoformat(), local.isoformat(), "Group", r["group"],
                 normalize(r["home"]), normalize(r["away"]), "TIMED",
                 1 if r["detonator"] == "Y" else 0))
            rows += 1
    conn.commit()
    return rows


if __name__ == "__main__":
    from store.db import init_db
    conn = init_db()
    print(f"seeded {seed(conn)} fixtures into the matches table")
