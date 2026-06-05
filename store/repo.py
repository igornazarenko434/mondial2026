"""Read helpers over the matches table — the bridge between the live calendar
(refreshed by football_data.ingest) and the scheduler.

`upcoming_matches` is what the daemon polls each tick to know which games are
coming, when, and who's playing. `recent_finished` drives results/scoring and
tells you who won (and therefore who advances). Bracket opponents that were TBD
appear here automatically once a daily re-ingest pulls the resolved fixtures.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone, timedelta


def _rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def upcoming_matches(conn: sqlite3.Connection, within_hours: int = 26) -> list[dict]:
    """Scheduled/timed matches kicking off within the horizon (>= T-24h window).

    Returns the shape the scheduler expects: match_id, utc_kickoff, home, away,
    stage, detonator. Skips TBD knockout rows that don't yet have both teams.
    """
    now = datetime.now(timezone.utc)
    horizon = (now + timedelta(hours=within_hours)).isoformat()
    cur = conn.execute(
        "SELECT match_id, utc_kickoff, home, away, stage, grp, detonator "
        "FROM matches WHERE status IN ('SCHEDULED','TIMED') "
        "AND utc_kickoff IS NOT NULL AND utc_kickoff <= ? "
        "AND home IS NOT NULL AND away IS NOT NULL "
        "ORDER BY utc_kickoff", (horizon,))
    out = []
    for r in _rows(cur):
        r["detonator"] = bool(r.get("detonator"))
        out.append(r)
    return out


def games_left(conn: sqlite3.Connection) -> int:
    """Matches not yet finished — feeds the strategy layer's 'time remaining'."""
    return conn.execute(
        "SELECT COUNT(*) FROM matches WHERE status != 'FINISHED'").fetchone()[0]


def standings_context(conn: sqlite3.Connection, me: str | None = None) -> dict | None:
    """Build the strategy context from the `standings` table (which you populate
    with your group's points) + games-left from the calendar. Returns None if
    standings aren't populated → strategy layer safely no-ops.

    me: your participant name; defaults to the row with the highest futures+... —
    pass it explicitly for correctness.
    """
    rows = conn.execute(
        "SELECT participant, (group_points*0.85 + knockout_points + futures_points) AS total "
        "FROM standings ORDER BY total DESC").fetchall()
    if not rows:
        return None
    totals = {r[0]: r[1] for r in rows}
    leader_points = rows[0][1]
    second_points = rows[1][1] if len(rows) > 1 else rows[0][1]
    your_points = totals.get(me, rows[0][1]) if me else rows[0][1]
    return {"your_points": your_points, "leader_points": leader_points,
            "second_points": second_points, "games_left": games_left(conn)}


def recent_finished(conn: sqlite3.Connection, hours: int = 12) -> list[dict]:
    """Matches that finished recently — for scoring + 'who advanced' awareness."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    cur = conn.execute(
        "SELECT match_id, home, away, stage, grp, home_goals, away_goals "
        "FROM matches WHERE status='FINISHED' AND utc_kickoff >= ?", (since,))
    return _rows(cur)
