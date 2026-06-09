"""core/reporting/people — symmetric per-participant renderers."""
from __future__ import annotations

import pytest

from core.reporting import people


# ───────────────────────── tracked_participants / env ─────────────────────────

def test_tracked_participants_defaults_to_me_when_unset(monkeypatch):
    monkeypatch.delenv("MY_PARTICIPANT", raising=False)
    monkeypatch.delenv("FRIEND_PARTICIPANTS", raising=False)
    assert people.tracked_participants() == ["me"]


def test_tracked_participants_uses_my_participant(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.delenv("FRIEND_PARTICIPANTS", raising=False)
    assert people.tracked_participants() == ["Igor"]


def test_tracked_participants_includes_friends_in_order(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia,Alon, David ")
    assert people.tracked_participants() == ["Igor", "Vaadia", "Alon", "David"]


def test_tracked_participants_dedups_friend_who_is_also_me(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Igor,Vaadia,Igor")
    assert people.tracked_participants() == ["Igor", "Vaadia"]


def test_tracked_participants_ignores_empty_csv_entries(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", ",Vaadia,,, ")
    assert people.tracked_participants() == ["Igor", "Vaadia"]


# ───────────────────────── from_db_row ─────────────────────────

def test_from_db_row_collapses_three_columns_into_negev_shape():
    db = {"participant": "Igor", "group_points": 10.5,
          "knockout_points": 2.0, "futures_points": 4.3}
    r = people.from_db_row(db)
    assert r["player"] == "Igor"
    assert r["total"] == 10.5 + 2.0 + 4.3
    assert r["direction"] == 10.5 + 2.0
    assert r["broad"] == 4.3
    assert r["role"] == "player"


def test_from_db_row_returns_none_for_none():
    assert people.from_db_row(None) is None


def test_from_db_row_handles_missing_columns():
    r = people.from_db_row({"participant": "X"})
    assert r["total"] == 0
    assert r["direction"] == 0
    assert r["broad"] == 0


# ───────────────────────── render_block ─────────────────────────

def _make_rows(*tuples):
    """Helper: each tuple = (name, rank, total, direction, broad, role).
    Defaults role='player'."""
    rows = []
    for t in tuples:
        name, rank, total, direction, broad, *rest = t
        role = rest[0] if rest else "player"
        rows.append({"player": name, "rank": rank, "total": total,
                      "direction": direction, "broad": broad,
                      "exactCount": 0, "role": role})
    return rows


def test_render_block_for_self_marks_you_and_skips_vs_you(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = _make_rows(
        ("Gilad", 1, 12.5, 8.0, 4.5),
        ("Sarah", 2, 10.0, 8.0, 2.0),
        ("Igor", 26, 0.0, 0.0, 0.0))
    block = people.render_block(rows, "Igor")
    assert "👤 Igor" in block
    assert "← you" in block
    assert "vs leader" in block and "Gilad" in block
    assert "vs second" in block and "Sarah" in block
    assert "vs you" not in block          # skipped when self


def test_render_block_for_friend_shows_all_three_gaps(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = _make_rows(
        ("Gilad", 1, 12.5, 8.0, 4.5),
        ("Sarah", 2, 10.0, 8.0, 2.0),
        ("Vaadia", 12, 3.5, 3.5, 0.0),
        ("Igor", 26, 0.0, 0.0, 0.0))
    block = people.render_block(rows, "Vaadia")
    assert "👤 Vaadia" in block
    assert "← you" not in block
    assert "vs leader" in block
    assert "vs second" in block
    assert "vs you" in block
    assert "Vaadia ahead of you" in block         # 3.5 vs 0.0
    assert "+3.5" in block                          # gap_to_you sign


def test_render_block_for_leader_skips_vs_leader_line(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Vaadia")
    rows = _make_rows(("Vaadia", 1, 10.0, 10.0, 0.0),
                       ("Other", 2, 5.0, 5.0, 0.0))
    block = people.render_block(rows, "Vaadia")
    assert "vs leader" not in block               # self IS leader
    assert "vs second" in block


def test_render_block_friend_ahead_or_behind_or_tied(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Me")
    rows = _make_rows(("Leader", 1, 100, 100, 0),
                       ("Friend", 5, 10, 10, 0),
                       ("Me", 5, 10, 10, 0))
    block = people.render_block(rows, "Friend")
    assert "tied with you" in block


def test_render_block_missing_person_returns_placeholder(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = _make_rows(("Igor", 1, 0, 0, 0))
    block = people.render_block(rows, "Ghost")
    assert "✗ Not in standings" in block


def test_render_block_filters_bots_from_leader_math(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = _make_rows(
        ("Chinchilla", 1, 50, 50, 0, "bot"),
        ("Gilad", 2, 12.5, 8, 4.5, "player"),
        ("Igor", 26, 0, 0, 0, "player"))
    block = people.render_block(rows, "Igor")
    # leader math must skip the bot
    assert "Gilad" in block
    assert "Chinchilla" not in block


def test_render_block_with_broad_bets_inline(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = _make_rows(("Igor", 1, 0, 0, 0))
    bb = {"winner": "Portugal", "cinderella": "Uzbekistan",
          "goldenBoot": "Mbappé", "bestPlayer": "Arkadi"}
    block = people.render_block(rows, "Igor", broad_bets=bb)
    assert "Broad bets:" in block
    assert "Portugal" in block
    assert "Uzbekistan" in block


# ───────────────────────── render_compact ─────────────────────────

def test_render_compact_for_self_shows_you_tag(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = _make_rows(("Gilad", 1, 12.5, 8, 4.5),
                       ("Igor", 5, 3.0, 3.0, 0.0))
    line = people.render_compact(rows, "Igor")
    assert "Igor: 3.0 pts" in line
    assert "rank 5/2" in line                       # n=2 rows
    assert "← you" in line


def test_render_compact_for_friend_shows_relative_gap(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = _make_rows(("Vaadia", 12, 3.5, 3.5, 0),
                       ("Igor", 26, 0, 0, 0))
    line = people.render_compact(rows, "Vaadia")
    assert "Vaadia: 3.5 pts" in line
    assert "3.5 ahead of you" in line


def test_render_compact_tied(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Me")
    rows = _make_rows(("F", 1, 5, 5, 0), ("Me", 1, 5, 5, 0))
    line = people.render_compact(rows, "F")
    assert "tied with you" in line


def test_render_compact_missing_returns_placeholder(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = _make_rows(("Igor", 1, 0, 0, 0))
    line = people.render_compact(rows, "Missing")
    assert "✗ not in standings" in line


# ───────────────────────── render_match_picks_block ─────────────────────────

def test_match_picks_my_pick_and_friend_pick(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    picks = [{"displayName": "Vaadia", "homeScore": 1, "awayScore": 1, "points": 0}]
    my_pred = {"home": 2, "away": 1}
    out = people.render_match_picks_block(picks, my_pred, ["Igor", "Vaadia"],
                                           "Mexico", "South Africa")
    assert "👥 Picks" in out
    assert "Igor: Mexico 2 — South Africa 1" in out
    assert "← you" in out
    assert "Vaadia: Mexico 1 — South Africa 1" in out


def test_match_picks_no_pick_yet_shown_explicitly(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    out = people.render_match_picks_block([], None, ["Igor", "Vaadia"],
                                           "France", "Norway")
    assert "Igor: (no pick yet)" in out
    assert "Vaadia: (no pick yet)" in out


def test_match_picks_show_points_after_scoring(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    picks = [{"displayName": "Vaadia", "homeScore": 2, "awayScore": 1,
              "points": 5.625}]
    out = people.render_match_picks_block(picks, {"home": 2, "away": 1},
                                           ["Igor", "Vaadia"], "X", "Y")
    assert "5.6 pts" in out      # rendered to 1 decimal


def test_match_picks_skips_unknown_friends_gracefully(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    # 'Friend2' isn't in picks AND has no my_pred → (no pick yet)
    out = people.render_match_picks_block([], None,
                                           ["Igor", "Friend1", "Friend2"],
                                           "H", "A")
    for n in ("Igor", "Friend1", "Friend2"):
        assert n in out
    assert out.count("(no pick yet)") == 3


def test_match_picks_telegram_safe_no_markdown_chars(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    picks = [{"displayName": "Vaadia", "homeScore": 1, "awayScore": 0}]
    out = people.render_match_picks_block(picks, {"home": 2, "away": 1},
                                           ["Igor", "Vaadia"], "X", "Y")
    assert "*" not in out
    assert "_" not in out
