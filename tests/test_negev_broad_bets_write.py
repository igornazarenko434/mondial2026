"""Negev broad-bets write tool (Day-9.11) — toto_save_broad_bets.

Pure-offline tests with mocked Firestore — never touch the network.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from integrations import negev_toto_mcp as ntm


TID = "n40ykJlOIA9Mg839hz91"


CATEGORIES_FIXTURE = {
    "tournament_id": TID,
    "isPublished": True,
    "isLocked": False,
    "categories": [
        {"id": "winner", "title": "Tournament Winner", "options": [
            {"id": "team_Portugal", "name": "Portugal",  "points": 39, "isKilled": False},
            {"id": "team_France",   "name": "France",    "points": 20, "isKilled": False},
            {"id": "team_England",  "name": "England",   "points": 26, "isKilled": False},
        ]},
        {"id": "cinderella", "title": "Cinderella Team", "options": [
            {"id": "team_Uzbekistan", "name": "Uzbekistan", "points": 23, "isKilled": False},
            {"id": "team_CongoDR",    "name": "Congo DR",   "points": 15, "isKilled": False},
        ]},
        {"id": "goldenBoot", "title": "Golden Boot", "options": [
            {"id": "1780580161396", "name": "Mbappe",     "points": 20, "isKilled": False},
            {"id": "1780580161397", "name": "Harry Kane", "points": 21, "isKilled": False},
        ]},
        {"id": "bestPlayer", "title": "Best Placed Player", "options": [
            {"id": "uid_aharony", "name": "Aharony", "points": 5, "isKilled": False},
            {"id": "uid_alfi",    "name": "Alfi",    "points": 5, "isKilled": False},
            {"id": "uid_igor",    "name": "Igor",    "points": 5, "isKilled": False},
        ], "_synthesized": True},
    ],
}


@pytest.fixture
def categories(monkeypatch):
    """Stub toto_get_broad_bet_categories with the fixture above."""
    monkeypatch.setattr(ntm, "toto_get_broad_bet_categories",
                        lambda tournament_id=None: dict(CATEGORIES_FIXTURE))


@pytest.fixture
def fake_uid(monkeypatch):
    monkeypatch.setattr(ntm, "_token", {"uid": "uid_igor", "id": "x", "refresh": "x"})


# ──────────────────────── ID resolution ───────────────────────────────────

def test_resolve_option_id_exact_id_match():
    rid = ntm._resolve_option_id("winner", "team_Portugal", CATEGORIES_FIXTURE)
    assert rid == "team_Portugal"


def test_resolve_option_id_by_display_name():
    rid = ntm._resolve_option_id("winner", "Portugal", CATEGORIES_FIXTURE)
    assert rid == "team_Portugal"


def test_resolve_option_id_case_insensitive():
    rid = ntm._resolve_option_id("cinderella", "uzbekistan", CATEGORIES_FIXTURE)
    assert rid == "team_Uzbekistan"


def test_resolve_option_id_relaxed_punctuation():
    rid = ntm._resolve_option_id("cinderella", "Congo  DR", CATEGORIES_FIXTURE)
    assert rid == "team_CongoDR"


def test_resolve_option_id_unknown_returns_none():
    rid = ntm._resolve_option_id("winner", "Atlantis", CATEGORIES_FIXTURE)
    assert rid is None


def test_resolve_option_id_best_player_by_displayname():
    rid = ntm._resolve_option_id("bestPlayer", "Aharony", CATEGORIES_FIXTURE)
    assert rid == "uid_aharony"


# ──────────────── toto_save_broad_bets — dry-run path ──────────────────────

def test_save_broad_bets_dry_run_resolves_all_four(categories, fake_uid):
    out = ntm.toto_save_broad_bets(
        winner="Portugal", cinderella="Uzbekistan",
        golden_boot="Mbappe", best_player="Igor",
        tournament_id=TID, dry_run=True)
    assert out["dry_run"] is True
    assert out["resolved"] == {
        "winner":     "team_Portugal",
        "cinderella": "team_Uzbekistan",
        "goldenBoot": "1780580161396",
        "bestPlayer": "uid_igor",
    }
    assert out["would_patch"] == f"tournaments/{TID}/broadBets/uid_igor"
    # updatedAt is current; userId + tid present
    assert out["fields"]["userId"] == "uid_igor"
    assert out["fields"]["tournamentId"] == TID
    assert "updatedAt" in out["fields"]


def test_save_broad_bets_dry_run_partial_update(categories, fake_uid):
    """User wants to update ONLY the winner — other 3 must NOT appear in selections."""
    out = ntm.toto_save_broad_bets(winner="Portugal",
                                     tournament_id=TID, dry_run=True)
    assert out["resolved"] == {"winner": "team_Portugal"}
    assert "cinderella" not in out["fields"]["selections"]


def test_save_broad_bets_empty_call_errors(categories, fake_uid):
    out = ntm.toto_save_broad_bets(tournament_id=TID, dry_run=True)
    assert "error" in out
    assert "nothing to save" in out["error"]


def test_save_broad_bets_unresolved_choice_returns_error(categories, fake_uid):
    out = ntm.toto_save_broad_bets(winner="Atlantis",
                                     tournament_id=TID, dry_run=True)
    assert "error" in out
    assert out["unresolved"][0]["category"] == "winner"
    assert out["unresolved"][0]["choice"] == "Atlantis"


# ──────────────── write-gating semantics ────────────────────────────────

def test_save_broad_bets_writes_disabled_returns_error(categories, fake_uid, monkeypatch):
    monkeypatch.delenv("NEGEV_ALLOW_WRITES", raising=False)
    out = ntm.toto_save_broad_bets(winner="Portugal", tournament_id=TID)
    assert "error" in out
    assert "writes disabled" in out["error"]
    assert out["resolved"] == {"winner": "team_Portugal"}    # still shows the plan


def test_save_broad_bets_writes_enabled_calls_patch(categories, fake_uid, monkeypatch):
    monkeypatch.setenv("NEGEV_ALLOW_WRITES", "1")
    calls = {}

    def fake_patch(path, fields_json):
        calls["path"] = path
        calls["fields"] = json.loads(fields_json)
        return {"updateTime": "2026-06-07T18:00:00Z"}

    monkeypatch.setattr(ntm, "toto_patch_document", fake_patch)

    out = ntm.toto_save_broad_bets(winner="Portugal", cinderella="Uzbekistan",
                                     golden_boot="Mbappe", best_player="Igor",
                                     tournament_id=TID)
    assert out["ok"] is True
    assert calls["path"] == f"tournaments/{TID}/broadBets/uid_igor"
    assert calls["fields"]["selections"] == {
        "winner":     "team_Portugal",
        "cinderella": "team_Uzbekistan",
        "goldenBoot": "1780580161396",
        "bestPlayer": "uid_igor",
    }
    assert calls["fields"]["userId"] == "uid_igor"
    assert calls["fields"]["tournamentId"] == TID
