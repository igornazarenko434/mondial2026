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
    """Build the strategy context from the `standings` table + games-left.

    Returns None (→ strategy layer safely no-ops) when:
      • standings table empty (pre-tournament, nothing entered yet)
      • `me` is None (no participant identity supplied — we can't compute
        "your gap to the leader" without knowing who YOU are)
      • `me` doesn't appear in standings (typo in MY_PARTICIPANT env var,
        or your row hasn't been populated yet — safer to no-op than to
        silently use someone else's totals)
      • only one row exists (no one to compare against)

    `total` arithmetic mirrors core.scoring.standings_writer: the writer
    applies the §14 -15 % group-stage reset to `group_points` itself once
    any KO match has been scored — so this reader just sums the columns
    straight (NO additional 0.85 multiplier — that was a bug that
    double-applied the reset post-knockouts).
    """
    # Day-9.27: include side_points so the gap-to-leader math matches what
    # the Negev app shows. Pre-Day-9.27 this column was always 0 (we couldn't
    # read side bets), so summing 3 columns was correct; now that
    # tournamentStats gives us the authoritative side_points, the strategy
    # tilt would miscalibrate without it.
    # COALESCE protects older DB rows that pre-date the side_points migration.
    rows = conn.execute(
        "SELECT participant, "
        "(group_points + knockout_points "
        " + COALESCE(side_points, 0) + futures_points) AS total "
        "FROM standings ORDER BY total DESC").fetchall()
    if not rows or len(rows) < 2:                  # need ≥ 2 to define "leader vs me"
        return None
    if me is None:
        return None                                 # can't compute gap without an identity
    totals = {r[0]: r[1] for r in rows}
    if me not in totals:
        return None                                 # ME not in standings yet → no-op
    leader_points = rows[0][1]
    second_points = rows[1][1]
    your_points = totals[me]
    return {"your_points": your_points, "leader_points": leader_points,
            "second_points": second_points, "games_left": games_left(conn)}


def recent_finished(conn: sqlite3.Connection, hours: int = 12) -> list[dict]:
    """Matches that finished recently — for scoring + 'who advanced' awareness."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    cur = conn.execute(
        "SELECT match_id, home, away, stage, grp, home_goals, away_goals "
        "FROM matches WHERE status='FINISHED' AND utc_kickoff >= ?", (since,))
    return _rows(cur)
