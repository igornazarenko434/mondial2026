"""Day-9.27: tournamentStats direct-read tests.

Discovered by grepping the Negev SPA bundle (/assets/index-*.js): the
standings page reads `tournamentStats/{tid}_{uid}` docs that Negev's
Cloud Function pre-computes after every match/side-bet resolution.

Live verified 2026-06-13:
  - Igor:        pointsTotal=2.91  groupGames=1.91  side=1     ✓ app "2.9 Pts"
  - Gilad Cain:  pointsTotal=22.33 groupGames=20.33 side=2     ✓ app "22.3"
  - Hershko:     pointsTotal=17.78 groupGames=16.78 side=1     ✓ app "17.8"

These tests pin the contract so the rewrite can't regress to the
bet-aggregation + override-file path.
"""
from __future__ import annotations
from unittest.mock import patch

import pytest

from integrations import negev_toto_mcp as ntm


@pytest.fixture
def stats_dataset(monkeypatch):
    """Realistic 5-user fixture mirroring 2026-06-13 production state."""
    users = [
        {"uid": "u-cain",   "displayName": "Gilad Cain", "role": "player",
         "tournaments": ["t1"]},
        {"uid": "u-hersh",  "displayName": "Hershko",    "role": "player",
         "tournaments": ["t1"]},
        {"uid": "u-igor",   "displayName": "Igor",       "role": "player",
         "tournaments": ["t1"]},
        {"uid": "bot_chin", "displayName": "Chinchilla", "role": "bot",
         "isBot": True, "tournaments": ["t1"]},
        {"uid": "u-out",    "displayName": "Outside",    "role": "player",
         "tournaments": ["other-t"]},
    ]
    # tournamentStats docs — keyed by tid_uid in path
    stats = [
        {"_path": "tournamentStats/t1_u-cain",
         "userId": "u-cain", "tournamentId": "t1",
         "pointsTotal": 22.33, "groupGamesPoints": 20.33,
         "koutGamesPoints": 0, "sideBetPoints": 2,
         "broadBetPoints": 0, "exactScoreCount": 2,
         "previousRank": 1},
        {"_path": "tournamentStats/t1_u-hersh",
         "userId": "u-hersh", "tournamentId": "t1",
         "pointsTotal": 17.78, "groupGamesPoints": 16.78,
         "koutGamesPoints": 0, "sideBetPoints": 1,
         "broadBetPoints": 0, "exactScoreCount": 1,
         "previousRank": 21},
        {"_path": "tournamentStats/t1_u-igor",
         "userId": "u-igor", "tournamentId": "t1",
         "pointsTotal": 2.91, "groupGamesPoints": 1.91,
         "koutGamesPoints": 0, "sideBetPoints": 1,
         "broadBetPoints": 0, "exactScoreCount": 0,
         "previousRank": 66},
        # Chinchilla in our tournament too
        {"_path": "tournamentStats/t1_bot_chin",
         "userId": "bot_chin", "tournamentId": "t1",
         "pointsTotal": 1.0, "groupGamesPoints": 0,
         "koutGamesPoints": 0, "sideBetPoints": 1,
         "broadBetPoints": 0},
        # Doc for a DIFFERENT tournament — must not bleed in
        {"_path": "tournamentStats/other-t_u-out",
         "userId": "u-out", "tournamentId": "other-t",
         "pointsTotal": 999, "groupGamesPoints": 999},
    ]
    def _read_all(coll, **_kw):
        if coll == "users":
            return users
        if coll == "tournamentStats":
            return stats
        return []
    monkeypatch.setattr(ntm, "_read_all", _read_all)


def test_standings_reads_tournamentStats_directly(stats_dataset):
    """Day-9.27 primary path: rows come from tournamentStats verbatim."""
    rows = ntm.toto_get_standings("t1")
    by_name = {r["player"]: r for r in rows}
    # G. Cain matches app screenshot byte-for-byte
    cain = by_name["Gilad Cain"]
    assert cain["total"] == 22.33
    assert cain["direction"] == 20.33
    assert cain["side"] == 2.0
    assert cain["knockout"] == 0.0
    assert cain["broad"] == 0.0
    # Igor
    igor = by_name["Igor"]
    assert igor["total"] == 2.91
    assert igor["direction"] == 1.91
    assert igor["side"] == 1.0


def test_standings_ranks_match_app_ordering(stats_dataset):
    """G. Cain 22.33 > Hershko 17.78 > Igor 2.91 > Chinchilla 1.0."""
    rows = ntm.toto_get_standings("t1")
    names = [r["player"] for r in rows]
    assert names == ["Gilad Cain", "Hershko", "Igor", "Chinchilla"]
    assert rows[0]["rank"] == 1
    assert rows[2]["rank"] == 3


def test_other_tournament_stats_dont_leak(stats_dataset):
    """Outside has pointsTotal=999 for a DIFFERENT tournament. Must NOT
    appear in t1's standings."""
    rows = ntm.toto_get_standings("t1")
    assert all(r["player"] != "Outside" for r in rows)


def test_exclude_bots_when_asked(stats_dataset):
    """include_bots=False filters Chinchilla even if it has tournamentStats."""
    rows = ntm.toto_get_standings("t1", include_bots=False)
    assert all(r["role"] != "bot" for r in rows)


def test_previous_rank_surfaced_for_change_arrows(stats_dataset):
    """previousRank should bubble through so render can show ↑/↓ arrows."""
    rows = ntm.toto_get_standings("t1")
    cain = next(r for r in rows if r["player"] == "Gilad Cain")
    assert cain.get("previousRank") == 1


def test_fallback_to_bet_aggregation_when_stats_empty(monkeypatch):
    """If tournamentStats is empty (Negev outage), fall back to the Day-9.26
    bet aggregation path. Keeps the system live under partial degradation."""
    users = [{"uid": "u1", "displayName": "X", "role": "player",
              "tournaments": ["t1"]}]
    matches = [{"apiFixtureId": 1, "tournamentId": "t1",
                 "stage": "Group Stage - 1"}]
    bets = [{"userId": "u1", "tournamentId": "t1",
             "matchId": "t1_1", "points": 5.5, "isExactScore": False}]
    def _read_all(coll, **_kw):
        if coll == "tournamentStats":
            return []
        return {"users": users, "matches": matches, "bets": bets,
                 f"tournaments/t1/broadBets": []}.get(coll, [])
    monkeypatch.setattr(ntm, "_read_all", _read_all)
    monkeypatch.setattr(ntm, "_load_side_bet_overrides", lambda _: {})
    rows = ntm.toto_get_standings("t1")
    # Fallback path produces the bet-aggregated row
    assert rows[0]["player"] == "X"
    assert rows[0]["direction"] == 5.5


def test_toto_get_side_bet_voters_returns_yes_no_lists(monkeypatch):
    """Day-9.27: the new tool reads sideBetAnswers + the shell."""
    users = [
        {"uid": "u-igor",  "displayName": "Igor",   "role": "player"},
        {"uid": "u-vaad",  "displayName": "Vaadia", "role": "player"},
        {"uid": "u-cain",  "displayName": "Cain",   "role": "player"},
    ]
    shell = {"_path": "tournaments/t1/sideBets/sb_x",
             "question": "Q?", "correctAnswer": "No",
             "isResolved": True}
    answers = [
        {"_path": "tournaments/t1/sideBetAnswers/u-igor",
         "userId": "u-igor", "answers": {"sb_x": "No"}},
        {"_path": "tournaments/t1/sideBetAnswers/u-vaad",
         "userId": "u-vaad", "answers": {"sb_x": "Yes"}},
        {"_path": "tournaments/t1/sideBetAnswers/u-cain",
         "userId": "u-cain", "answers": {"sb_x": "No"}},
    ]
    def _read_all(coll, **_kw):
        if coll == "users":
            return users
        if "sideBetAnswers" in coll:
            return answers
        return []
    monkeypatch.setattr(ntm, "_read_all", _read_all)
    monkeypatch.setattr(ntm, "toto_get_document",
                         lambda path: shell if "sideBets/sb_x" in path else
                         {"error": "404"})
    out = ntm.toto_get_side_bet_voters("sb_x", tournament_id="t1")
    assert out["question"] == "Q?"
    assert out["correct_answer"] == "No"
    assert out["yes_count"] == 1
    assert out["no_count"] == 2
    yes_names = [v["player"] for v in out["yes_voters"]]
    no_names  = [v["player"] for v in out["no_voters"]]
    assert "Vaadia" in yes_names
    assert "Igor" in no_names
    assert "Cain" in no_names
    # 'winners' field = voters who picked the correctAnswer
    winners = [v["player"] for v in out["winners"]]
    assert "Igor" in winners and "Cain" in winners
    assert "Vaadia" not in winners
