"""Fixture ingestion from football-data.org (World Cup is in the free tier).

This is how the system KNOWS when each game is and who plays. Run it on Day 1
and then daily; it upserts the full calendar into SQLite. Knockout opponents
(currently TBD) fill in automatically as the bracket resolves.

SOURCE AUDIT (Jun 2026, see docs/SOURCES.md): football-data.org is reliable and
free, but its WC data is an "older format" that may NOT expose Round-of-32
placeholders for the 48-team bracket. If Day-1 ingest shows R32 missing, switch
the PRIMARY fixtures source to API-Football (core/data/api_football.fixtures_backup)
via `reliability.with_fallback(api_football..., fetch_wc_matches)` — same row shape.
"""
from __future__ import annotations
import csv
import os
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from core.data.teams import normalize
from core.obs.logging import get_logger

log = get_logger("ingest")
FD_BASE = "https://api.football-data.org/v4"
LOCAL_TZ = os.environ.get("LOCAL_TZ", "Asia/Jerusalem")
DETONATOR_CSV = os.path.join(os.path.dirname(__file__), "..", "..", "data",
                             "wc2026_detonator_fixtures.csv")


def _local_iso(utc: datetime) -> str:
    """UTC → local ISO, falling back to UTC if tzdata is unavailable."""
    try:
        return utc.astimezone(ZoneInfo(LOCAL_TZ)).isoformat()
    except Exception:                       # noqa: BLE001 - missing tzdata on minimal hosts
        return utc.isoformat()

# football-data stage code -> the rules stage used by config.rules.STAGE_TYPE.
# Verify LAST_32 against the live 48-team API on Day 1 and extend if needed.
RULES_STAGE = {
    "GROUP_STAGE": "Group", "LAST_32": "R32", "LAST_16": "R16",
    "QUARTER_FINALS": "QF", "SEMI_FINALS": "SF",
    "THIRD_PLACE": "3rd", "FINAL": "Final",
}


def to_rules_stage(fd_stage: str | None) -> str | None:
    return RULES_STAGE.get(fd_stage, fd_stage)


def fetch_wc_matches() -> list[dict]:
    """All World Cup matches with kickoff, stage, group, teams, status, score."""
    key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not key:
        raise RuntimeError("Set FOOTBALL_DATA_API_KEY in .env")
    from core import obs
    with obs.external_call("football_data", "wc_matches"):
        resp = requests.get(f"{FD_BASE}/competitions/WC/matches",
                            headers={"X-Auth-Token": key}, timeout=30)
        resp.raise_for_status()
    out = []
    for m in resp.json().get("matches", []):
        utc_raw = m.get("utcDate")
        if not utc_raw:                     # skip rows without a kickoff time
            log.warning("match %s has no utcDate; skipped", m.get("id"))
            continue
        try:
            utc = datetime.fromisoformat(utc_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            log.warning("match %s bad utcDate %r; skipped", m.get("id"), utc_raw)
            continue
        # Football-data.org returns group as "GROUP_A" — strip the prefix so
        # what we store matches the canonical roster in data/wc2026_groups.csv
        # (single letters A–L). The card-render also strips defensively in
        # build_card, but normalising at the data layer makes the DB self-
        # consistent for audit tools, SQL joins, and any downstream consumer.
        raw_group = m.get("group")
        if isinstance(raw_group, str) and raw_group.upper().startswith("GROUP_"):
            raw_group = raw_group[len("GROUP_"):]
        out.append({
            "match_id": m["id"],
            "utc_kickoff": utc.isoformat(),
            "local_kickoff": _local_iso(utc),
            "stage": to_rules_stage(m.get("stage")),   # store rules stage for scoring
            "group": raw_group,
            "home": normalize((m.get("homeTeam") or {}).get("name")),
            "away": normalize((m.get("awayTeam") or {}).get("name")),
            "status": m.get("status"),
            "home_goals": (m.get("score", {}).get("fullTime") or {}).get("home"),
            "away_goals": (m.get("score", {}).get("fullTime") or {}).get("away"),
        })
    return out


def ingest(db_conn):
    """Upsert all matches into the `matches` table. Returns count."""
    rows = fetch_wc_matches()
    cur = db_conn.cursor()
    for r in rows:
        cur.execute("""
            INSERT INTO matches (match_id, utc_kickoff, local_kickoff, stage,
                                 grp, home, away, status, home_goals, away_goals)
            VALUES (:match_id,:utc_kickoff,:local_kickoff,:stage,:group,:home,
                    :away,:status,:home_goals,:away_goals)
            ON CONFLICT(match_id) DO UPDATE SET
                utc_kickoff=excluded.utc_kickoff, local_kickoff=excluded.local_kickoff,
                stage=excluded.stage, grp=excluded.grp, home=excluded.home,
                away=excluded.away, status=excluded.status,
                home_goals=excluded.home_goals, away_goals=excluded.away_goals
        """, r)
        # NOTE: the UPDATE branch intentionally does NOT touch `detonator`, so a
        # re-ingest preserves tags set by tag_detonators().
    db_conn.commit()
    return len(rows)


def _detonator_pairs(csv_path: str = DETONATOR_CSV) -> set[frozenset]:
    """Unordered {home, away} pairs flagged as detonators in the bundled CSV
    (group-stage games with known teams; knockout detonators are TBD)."""
    pairs = set()
    if not os.path.exists(csv_path):
        return pairs
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            if r.get("detonator") == "Y" and r.get("home") and r["home"] != "TBD":
                pairs.add(frozenset({normalize(r["home"]), normalize(r["away"])}))
    return pairs


def tag_detonators(db_conn, csv_path: str = DETONATOR_CSV) -> int:
    """Set detonator=1 on matches whose team pair matches a known detonator game.
    Order-independent (sources may swap home/away). Returns count tagged."""
    pairs = _detonator_pairs(csv_path)
    if not pairs:
        return 0
    tagged = 0
    cur = db_conn.cursor()
    for mid, home, away in cur.execute(
            "SELECT match_id, home, away FROM matches WHERE home IS NOT NULL "
            "AND away IS NOT NULL").fetchall():
        if frozenset({home, away}) in pairs:
            cur.execute("UPDATE matches SET detonator=1 WHERE match_id=?", (mid,))
            tagged += 1
    db_conn.commit()
    return tagged


def refresh(db_conn) -> dict:
    """Day-1 / daily entrypoint: ingest the calendar AND tag detonators.
    The daemon calls this so detonators are always flagged. Idempotent."""
    n = ingest(db_conn)
    d = tag_detonators(db_conn)
    log.info("calendar refresh: %d matches, %d detonators tagged", n, d)
    return {"matches": n, "detonators": d}
