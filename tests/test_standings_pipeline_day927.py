"""Day-9.27: pipeline-level pins protecting the Negev-sourced standings
from being clobbered by the local writer, and ensuring the strategy
tilt's standings_context reads ALL four point categories.

Two bugs the audit found:
  1. store/repo.py::standings_context summed only group+knockout+futures
     → missed side_points → strategy tilt's gap-to-leader math was off
     by 1pt per resolved side bet a tracked friend won.
  2. core/scoring/standings_writer.update_standings ran on EVERY 60-second
     daemon tick AND overwrote Igor's row with score_match-computed values
     from the LOCAL predictions table (=0 when predictions empty, or based
     on EV-optimal pick which can DIFFER from Igor's real Negev pick).
     Result: Igor's group_points was reset to 0 every minute, undoing the
     07:00 Negev sync within seconds.
"""
from __future__ import annotations
import sqlite3

import pytest


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        c.executescript(f.read())
    return c


def test_standings_context_includes_side_points(conn):
    """The gap-to-leader math must reflect ALL four categories. Pre-Day-9.27
    side_points was 0 for everyone (we couldn't read it), so the omission
    didn't matter. After Day-9.27 side_points carries real values."""
    from store.repo import standings_context
    # Leader: 10 group + 2 side = 12 total
    conn.execute("INSERT INTO standings VALUES ('Leader', 10, 0, 0, 2)")
    # Igor: 3 group + 1 side = 4 total  (gap to leader = 8)
    conn.execute("INSERT INTO standings VALUES ('Igor', 3, 0, 0, 1)")
    # Second: 5 group + 0 side = 5 total
    conn.execute("INSERT INTO standings VALUES ('Second', 5, 0, 0, 0)")
    conn.commit()
    ctx = standings_context(conn, me="Igor")
    assert ctx is not None
    assert ctx["your_points"] == 4       # 3 + 1
    assert ctx["leader_points"] == 12    # 10 + 2
    assert ctx["second_points"] == 5     # 5 + 0


def test_local_writer_does_not_overwrite_negev_row(conn):
    """The PRIMARY new safety: once Negev has written non-zero values for a
    participant, the local writer's tick MUST NOT clobber them with its
    score_match-computed totals (which can be 0 if no local predictions).

    Pre-fix scenario:
      07:00 IDT — Negev sync writes Igor: group=20.33 side=2 → 22.33
      07:01:00  — daemon tick fires local writer with empty predictions
                  → upsert sets group=0 knockout=0 (PRESERVES side via SELECT)
      07:01:00+ — Igor row now shows 0 + 0 + 0 + 2 = 2 (lost the 20.33!)

    After fix: tick 1 detects has_negev_row=True → skips upsert → values stay."""
    from core.scoring.standings_writer import update_standings
    # Seed Igor with a Negev-sourced row (non-zero group + side)
    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, "
        " futures_points, side_points) VALUES (?, ?, ?, ?, ?)",
        ("Igor", 20.33, 0, 0, 2.0))
    conn.commit()
    # Now run the local writer (no predictions / no matches → scored=0,
    # but also no matches to score anyway)
    out = update_standings(conn, participant="Igor")
    # The function MUST detect the Negev row and skip the write
    assert out["written_to_db"] is False
    # The DB values are untouched
    row = conn.execute(
        "SELECT group_points, side_points FROM standings "
        "WHERE participant='Igor'").fetchone()
    assert row["group_points"] == 20.33
    assert row["side_points"] == 2.0


def test_local_writer_does_write_for_fresh_participant_with_local_score(conn):
    """The Negev-row guard MUST allow legitimate writes for participants
    that haven't been Negev-synced yet AND have a real score to record.
    Otherwise the function becomes inert."""
    from core.scoring.standings_writer import update_standings
    # Seed a finished match + a prediction + odds → local score will be > 0
    conn.execute(
        "INSERT INTO matches (match_id, stage, home_goals, away_goals, "
        " status, detonator) VALUES (?, ?, ?, ?, ?, ?)",
        (1, "Group", 1, 0, "FINISHED", 0))
    conn.execute(
        "INSERT INTO predictions (match_id, created_at, window, "
        " pick_h, pick_a, pick_dir, expected_points, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "2026-06-11T22:00", "T-7m", 1, 0, "H", 1.5, "{}"))
    conn.execute(
        "INSERT INTO odds_snapshots VALUES (?, ?, ?, ?, ?, ?)",
        (1, "T-7m", "pinnacle", 2.0, 3.0, 4.0))
    conn.commit()

    out = update_standings(conn, participant="NewUser")
    # NewUser has no prior row → no Negev-row guard → write succeeds
    assert out["written_to_db"] is True
    assert out["group_points"] > 0


def test_standings_context_returns_none_when_only_me(conn):
    """Regression: when only ONE row exists, ctx is None — that contract
    is unaffected by Day-9.27."""
    from store.repo import standings_context
    conn.execute("INSERT INTO standings VALUES ('Igor', 5, 0, 0, 1)")
    conn.commit()
    assert standings_context(conn, me="Igor") is None


def test_standings_context_handles_legacy_null_side_points(conn):
    """A DB that pre-dates the Day-9.26 ALTER TABLE has side_points NULL
    on existing rows until the next sync rewrites them. COALESCE makes
    that safe — no SQL exception, just treats NULL as 0."""
    from store.repo import standings_context
    # Force side_points NULL
    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, "
        "futures_points, side_points) VALUES (?, ?, ?, ?, NULL)",
        ("Leader", 10, 0, 0))
    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, "
        "futures_points, side_points) VALUES (?, ?, ?, ?, NULL)",
        ("Igor", 3, 0, 0))
    conn.commit()
    ctx = standings_context(conn, me="Igor")
    assert ctx is not None
    assert ctx["your_points"] == 3
    assert ctx["leader_points"] == 10
