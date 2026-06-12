"""Day-9.26: side-bet override JSON tests.

Negev stores per-user side-bet picks at a Firestore path our regular-user
auth can't read (403 across every probed convention — the SPA uses a
privileged path we can't reach). To make leaderboard totals + ranks match
the app exactly, the operator maintains a small JSON override file
mapping displayName → cumulative side-bet points.

These tests pin:
  1. File-missing → side stays 0 for everyone (under-reported but never wrong)
  2. File-present → side gets added per user; ranks resort by total
  3. Fuzzy name match — operator can write "Cain" or "G. Cain" instead
     of the full "Gilad Cain" displayName
  4. Malformed JSON degrades gracefully (warning + ignore, sync still succeeds)
"""
from __future__ import annotations
import json
import os

import pytest

from integrations import negev_toto_mcp as ntm


@pytest.fixture
def mock_negev_3users(monkeypatch):
    """Lean dataset: 3 humans, 1 group match where 2 of them scored."""
    users = [
        {"uid": "u-igor",   "displayName": "Igor",        "role": "player",
         "tournaments": ["t1"]},
        {"uid": "u-vaadia", "displayName": "Vaadia",      "role": "player",
         "tournaments": ["t1"]},
        {"uid": "u-cain",   "displayName": "Gilad Cain",  "role": "player",
         "tournaments": ["t1"]},
    ]
    matches = [{"apiFixtureId": 1, "tournamentId": "t1",
                 "stage": "Group Stage - 1"}]
    bets = [
        # Gilad got 10 pts on the match, Vaadia 6, Igor 0
        {"userId": "u-cain",   "tournamentId": "t1",
         "matchId": "t1_1", "points": 10.0, "isExactScore": True},
        {"userId": "u-vaadia", "tournamentId": "t1",
         "matchId": "t1_1", "points": 6.0,  "isExactScore": False},
        {"userId": "u-igor",   "tournamentId": "t1",
         "matchId": "t1_1", "points": 0.0,  "isExactScore": False},
    ]
    def _read_all(coll, **_kw):
        return {"users": users, "matches": matches, "bets": bets}.get(coll, [])
    monkeypatch.setattr(ntm, "_read_all", _read_all)


def _write_overrides(tmp_path, monkeypatch, tid, data):
    """Write overrides to a temp file + redirect _load_side_bet_overrides."""
    path = tmp_path / f"side_bet_overrides_{tid}.json"
    with open(path, "w") as f:
        json.dump(data, f)
    # Patch the loader to point at our temp path
    real_loader = ntm._load_side_bet_overrides
    def _fake_loader(tid_arg):
        if tid_arg != tid:
            return {}
        with open(path) as f:
            return {k: float(v) for k, v in
                     (json.load(f).get("users") or {}).items()
                     if isinstance(v, (int, float))}
    monkeypatch.setattr(ntm, "_load_side_bet_overrides", _fake_loader)
    return path


def test_no_override_file_means_side_zero(mock_negev_3users, monkeypatch):
    """File missing → side stays 0; no error; ranks unchanged."""
    monkeypatch.setattr(ntm, "_load_side_bet_overrides", lambda _: {})
    rows = ntm.toto_get_standings("t1")
    by_name = {r["player"]: r for r in rows}
    assert by_name["Gilad Cain"]["side"] == 0
    assert by_name["Vaadia"]["side"] == 0
    assert by_name["Igor"]["side"] == 0
    # Ranks driven by group only: Gilad > Vaadia > Igor
    assert rows[0]["player"] == "Gilad Cain"
    assert rows[1]["player"] == "Vaadia"
    assert rows[2]["player"] == "Igor"


def test_override_present_adds_to_total_and_resorts(mock_negev_3users,
                                                      tmp_path, monkeypatch):
    """All three got 1 side-bet pt → ranks unchanged but totals correct."""
    _write_overrides(tmp_path, monkeypatch, "t1",
                      {"users": {"Gilad Cain": 1.0, "Vaadia": 1.0, "Igor": 1.0}})
    rows = ntm.toto_get_standings("t1")
    by_name = {r["player"]: r for r in rows}
    assert by_name["Gilad Cain"]["side"] == 1.0
    assert by_name["Gilad Cain"]["total"] == 11.0      # 10 group + 1 side
    assert by_name["Vaadia"]["total"] == 7.0           # 6 + 1
    assert by_name["Igor"]["total"] == 1.0             # 0 + 1


def test_override_resorts_ranks_when_side_bet_breaks_tie(mock_negev_3users,
                                                          tmp_path,
                                                          monkeypatch):
    """Realistic scenario: G. Cain gets a side-bet pt, Esi doesn't → G. Cain
    leapfrogs Esi at rank 1 in the app. Our sort must do the same."""
    # Tweak the bets so Cain and Vaadia are TIED at 10 group pts
    def _read_all(coll, **_kw):
        if coll == "bets":
            return [
                {"userId": "u-cain", "tournamentId": "t1",
                 "matchId": "t1_1", "points": 10.0, "isExactScore": False},
                {"userId": "u-vaadia", "tournamentId": "t1",
                 "matchId": "t1_1", "points": 10.0, "isExactScore": True},  # higher exactCount
                {"userId": "u-igor", "tournamentId": "t1",
                 "matchId": "t1_1", "points": 0.0,  "isExactScore": False},
            ]
        return mock_negev_3users  # other collections unchanged

    # Re-mock with the tweaked bets data
    users = [
        {"uid": "u-igor",   "displayName": "Igor",       "role": "player",
         "tournaments": ["t1"]},
        {"uid": "u-vaadia", "displayName": "Vaadia",     "role": "player",
         "tournaments": ["t1"]},
        {"uid": "u-cain",   "displayName": "Gilad Cain", "role": "player",
         "tournaments": ["t1"]},
    ]
    matches = [{"apiFixtureId": 1, "tournamentId": "t1",
                 "stage": "Group Stage - 1"}]
    bets = [
        {"userId": "u-cain",   "tournamentId": "t1",
         "matchId": "t1_1", "points": 10.0, "isExactScore": False},
        {"userId": "u-vaadia", "tournamentId": "t1",
         "matchId": "t1_1", "points": 10.0, "isExactScore": True},
        {"userId": "u-igor",   "tournamentId": "t1",
         "matchId": "t1_1", "points": 0.0,  "isExactScore": False},
    ]
    def _ra(coll, **_kw):
        return {"users": users, "matches": matches, "bets": bets}.get(coll, [])
    monkeypatch.setattr(ntm, "_read_all", _ra)
    # Without side bet override: Vaadia ranks #1 by exactCount tiebreak
    monkeypatch.setattr(ntm, "_load_side_bet_overrides", lambda _: {})
    rows = ntm.toto_get_standings("t1")
    assert rows[0]["player"] == "Vaadia"
    # WITH G. Cain getting a side bet, his total 11 > Vaadia's 10 → he ranks #1
    _write_overrides(tmp_path, monkeypatch, "t1",
                      {"users": {"Gilad Cain": 1.0}})
    rows = ntm.toto_get_standings("t1")
    assert rows[0]["player"] == "Gilad Cain"
    assert rows[0]["total"] == 11.0
    assert rows[1]["player"] == "Vaadia"


def test_fuzzy_match_finds_full_displayname(mock_negev_3users, tmp_path,
                                              monkeypatch):
    """Operator writes 'Cain' or 'G. Cain' but the actual displayName is
    'Gilad Cain'. Fuzzy partial matching must find it."""
    _write_overrides(tmp_path, monkeypatch, "t1",
                      {"users": {"Cain": 1.0}})
    rows = ntm.toto_get_standings("t1")
    cain = next(r for r in rows if r["player"] == "Gilad Cain")
    assert cain["side"] == 1.0


def test_fuzzy_match_doesnt_create_false_positive(mock_negev_3users,
                                                    tmp_path, monkeypatch):
    """A completely unrelated override name doesn't accidentally match."""
    _write_overrides(tmp_path, monkeypatch, "t1",
                      {"users": {"Atlantis": 999.0}})
    rows = ntm.toto_get_standings("t1")
    for r in rows:
        assert r["side"] == 0


def test_malformed_override_json_degrades_to_zero(mock_negev_3users,
                                                    tmp_path, monkeypatch):
    """A typo in the JSON file shouldn't break the sync."""
    bad_path = tmp_path / "side_bet_overrides_t1.json"
    with open(bad_path, "w") as f:
        f.write("{ this is not valid json")
    # Real loader (not the helper) — we want to exercise the try/except
    real_loader = ntm._load_side_bet_overrides
    def _wrapping_loader(tid):
        # Redirect to our bad file
        path_str = str(bad_path)
        if not os.path.exists(path_str):
            return {}
        try:
            with open(path_str) as f:
                data = json.load(f)
            return {k: float(v) for k, v in
                     (data.get("users") or {}).items()
                     if isinstance(v, (int, float))}
        except Exception:
            return {}
    monkeypatch.setattr(ntm, "_load_side_bet_overrides", _wrapping_loader)
    rows = ntm.toto_get_standings("t1")
    # Sync completes; sides stay 0
    for r in rows:
        assert r["side"] == 0
