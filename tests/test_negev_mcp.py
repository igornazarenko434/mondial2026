"""Offline tests for the Negev Toto MCP typed tools.

Every test mocks `requests.get/post/patch` (and `_id_token`) so no network
contact is made. Pattern mirrors tests/test_ingest.py's football-data mock.
"""
from __future__ import annotations
import json
import pytest

from integrations import negev_toto_mcp as ntm


# ─────────────────────────────── helpers ───────────────────────────────

class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = json.dumps(body) if isinstance(body, dict) else str(body)
    @property
    def ok(self):  return 200 <= self.status_code < 300
    def json(self):  return self._body
    def raise_for_status(self):
        if not self.ok:
            raise AssertionError(f"HTTP {self.status_code}")


def _encode_doc(path: str, fields: dict) -> dict:
    """Build a Firestore raw-document dict matching the wire format."""
    return {
        "name": f"projects/p/databases/(default)/documents/{path}",
        "fields": {k: ntm._encode(v) for k, v in fields.items()},
    }


@pytest.fixture
def fake_firestore(monkeypatch):
    """Mock everything network-side. Tests seed the `state` dict.

    state['docs']:        {path: fields_dict} — single docs by path
    state['collections']: {collection_path: [{path, fields}]} — collection rows
    """
    monkeypatch.setattr(ntm, "_id_token", lambda: "fake-id")
    monkeypatch.setitem(ntm._token, "uid", "uid-igor")

    state = {"docs": {}, "collections": {}}

    def _get(url, headers=None, params=None, timeout=None):
        # Extract path after "/documents/"
        if "/documents/" not in url:
            return _FakeResp({}, 404)
        path = url.split("/documents/", 1)[1]
        if path in state["collections"]:
            page_size = (params or {}).get("pageSize", 100)
            rows = state["collections"][path]
            docs = [_encode_doc(r["path"], r["fields"]) for r in rows[:page_size]]
            return _FakeResp({"documents": docs})
        if path in state["docs"]:
            return _FakeResp(_encode_doc(path, state["docs"][path]))
        return _FakeResp({"error": {"code": 404, "message": "not found"}}, 404)

    def _patch(url, headers=None, params=None, json=None, timeout=None):
        return _FakeResp({"updated": True})

    monkeypatch.setattr(ntm.requests, "get", _get)
    monkeypatch.setattr(ntm.requests, "patch", _patch)
    return state


# ─────────────────────────── toto_list_tournaments ───────────────────────────

def test_list_tournaments_unions_user_tournaments_and_filters_inaccessible(fake_firestore):
    fake_firestore["collections"]["users"] = [
        {"path": "users/uid-a", "fields": {"uid": "uid-a", "displayName": "Alice",
                                          "tournaments": ["tid-real", "tid-test"]}},
        {"path": "users/uid-b", "fields": {"uid": "uid-b", "displayName": "Bob",
                                          "tournaments": ["tid-real"]}},
    ]
    fake_firestore["docs"]["tournaments/tid-real"] = {
        "name": "Negev Toto 2026",
        "settings": {"totalPrizePool": 32426},
        "createdAt": "2026-06-05T20:19:00Z",
    }
    fake_firestore["docs"]["tournaments/tid-test"] = {
        "name": "Test Pool",
        "settings": {"totalPrizePool": 100},
    }
    out = ntm.toto_list_tournaments()
    ids = {t["id"] for t in out}
    assert ids == {"tid-real", "tid-test"}                 # union of users
    real = next(t for t in out if t["id"] == "tid-real")
    assert real["accessible"] is True
    assert real["name"] == "Negev Toto 2026"
    assert real["prize_pool"] == 32426
    # Sorted by descending prize pool
    assert out[0]["id"] == "tid-real"


def test_list_tournaments_handles_inaccessible_tournaments(fake_firestore):
    fake_firestore["collections"]["users"] = [
        {"path": "users/u", "fields": {"uid": "u", "tournaments": ["tid-real", "tid-hidden"]}},
    ]
    fake_firestore["docs"]["tournaments/tid-real"] = {"name": "Real", "settings": {"totalPrizePool": 100}}
    # tid-hidden intentionally NOT in docs — should return 404, marked inaccessible
    out = ntm.toto_list_tournaments()
    hidden = next(t for t in out if t["id"] == "tid-hidden")
    assert hidden["accessible"] is False
    assert "404" in (hidden.get("error") or "")


# ─────────────────────────── toto_get_standings ───────────────────────────

def _seed_standings(state, tid):
    state["collections"]["users"] = [
        {"path": "users/u1", "fields": {"uid": "u1", "displayName": "Igor",
                                        "role": "player", "tournaments": [tid],
                                        "pointsTotal": 12, "directionPoints": 10,
                                        "broadBetPoints": 2, "exactScoreCount": 1}},
        {"path": "users/u2", "fields": {"uid": "u2", "displayName": "Alice",
                                        "role": "player", "tournaments": [tid],
                                        "pointsTotal": 20, "directionPoints": 18,
                                        "broadBetPoints": 2, "exactScoreCount": 3}},
        {"path": "users/u3", "fields": {"uid": "u3", "displayName": "Bob",
                                        "role": "player", "tournaments": [tid],
                                        "pointsTotal": 20, "directionPoints": 17,
                                        "broadBetPoints": 3, "exactScoreCount": 5}},
        {"path": "users/u4", "fields": {"uid": "u4", "displayName": "Chinchilla",
                                        "role": "bot", "isBot": True,
                                        "tournaments": [tid],
                                        "pointsTotal": 999, "exactScoreCount": 99}},
        {"path": "users/u5", "fields": {"uid": "u5", "displayName": "OutsideUser",
                                        "role": "player", "tournaments": ["other-tid"],
                                        "pointsTotal": 50}},
    ]


def test_get_standings_filters_by_tournament_and_excludes_bots_by_default(fake_firestore):
    _seed_standings(fake_firestore, "tid-x")
    rows = ntm.toto_get_standings("tid-x")
    names = [r["player"] for r in rows]
    assert names == ["Bob", "Alice", "Igor"]                 # tied 20 → exactCount tie-break
    assert "Chinchilla" not in names                          # bot excluded
    assert "OutsideUser" not in names                         # other tournament excluded
    # Ranks 1..3 assigned
    assert [r["rank"] for r in rows] == [1, 2, 3]


def test_get_standings_tie_break_by_exact_score_count(fake_firestore):
    _seed_standings(fake_firestore, "tid-x")
    rows = ntm.toto_get_standings("tid-x")
    # Alice and Bob both 20 pts; Bob has exactCount=5 > Alice 3 → Bob ranks first
    assert rows[0]["player"] == "Bob" and rows[0]["exactCount"] == 5
    assert rows[1]["player"] == "Alice"


def test_get_standings_include_bots_flag(fake_firestore):
    _seed_standings(fake_firestore, "tid-x")
    rows = ntm.toto_get_standings("tid-x", include_bots=True)
    assert rows[0]["player"] == "Chinchilla"                 # bot ranks #1 with 999 pts


def test_get_standings_extended_returns_full_user_doc(fake_firestore):
    _seed_standings(fake_firestore, "tid-x")
    rows = ntm.toto_get_standings("tid-x", extended=True)
    assert "_full" in rows[0]
    assert rows[0]["_full"]["uid"] in ("u2", "u3")            # one of the tied top


def test_get_standings_resolves_tid_from_env(fake_firestore, monkeypatch):
    _seed_standings(fake_firestore, "tid-from-env")
    monkeypatch.setenv("NEGEV_TOURNAMENT_ID", "tid-from-env")
    rows = ntm.toto_get_standings()                          # no tid arg
    assert len(rows) == 3


def test_get_standings_raises_when_no_tid(fake_firestore, monkeypatch):
    monkeypatch.delenv("NEGEV_TOURNAMENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="tournament_id required"):
        ntm.toto_get_standings()


# ─────────────────────────── toto_get_matches ───────────────────────────

def _seed_matches(state):
    state["collections"]["matches"] = [
        {"path": "matches/855734", "fields": {
            "apiFixtureId": 855734, "homeTeam": "Korea Republic", "awayTeam": "Cape Verde Islands",
            "date": "2022-11-21T16:00:00+00:00", "stage": "Group Stage - 1",
            "status": "FT", "scoreFullTimeHome": 0, "scoreFullTimeAway": 2,
            "oddsHome": None, "oddsDraw": None, "oddsAway": None,
            "oddsSource": "api", "isDetonator": False, "exactScoreMultiplier": 1,
        }},
        {"path": "matches/999999", "fields": {
            "apiFixtureId": 999999, "homeTeam": "Mexico", "awayTeam": "South Africa",
            "date": "2026-06-11T19:00:00+00:00", "stage": "Round of 16",
            "status": "NS", "scoreFullTimeHome": None, "scoreFullTimeAway": None,
            "oddsHome": 1.85, "oddsDraw": 3.6, "oddsAway": 4.2,
            "isDetonator": True, "exactScoreMultiplier": 2,
        }},
    ]


def test_get_matches_normalizes_team_names(fake_firestore):
    _seed_matches(fake_firestore)
    out = ntm.toto_get_matches()
    by_apifid = {m["apiFixtureId"]: m for m in out}
    assert by_apifid[855734]["home"] == "South Korea"             # Korea Republic → canonical
    assert by_apifid[855734]["away"] == "Cape Verde"              # Cape Verde Islands → canonical


def test_get_matches_maps_stage_labels(fake_firestore):
    _seed_matches(fake_firestore)
    out = ntm.toto_get_matches()
    by_apifid = {m["apiFixtureId"]: m for m in out}
    assert by_apifid[855734]["stage"] == "Group"                  # "Group Stage - 1" → Group
    assert by_apifid[999999]["stage"] == "R16"                    # "Round of 16" → R16


def test_get_matches_date_after_filter(fake_firestore):
    _seed_matches(fake_firestore)
    out = ntm.toto_get_matches(date_after="2025-01-01")
    apifids = {m["apiFixtureId"] for m in out}
    assert apifids == {999999}                                    # 2022 row excluded


def test_get_matches_status_filter(fake_firestore):
    _seed_matches(fake_firestore)
    out = ntm.toto_get_matches(status="NS")
    assert len(out) == 1 and out[0]["status"] == "NS"


def test_get_matches_stage_filter_uses_mapped_label(fake_firestore):
    _seed_matches(fake_firestore)
    out = ntm.toto_get_matches(stage="R16")
    assert len(out) == 1 and out[0]["stage"] == "R16"


# ─────────────────────────── toto_get_broad_bets ───────────────────────────

def test_get_broad_bets_joins_displayName_from_users(fake_firestore):
    tid = "tid-x"
    fake_firestore["collections"]["users"] = [
        {"path": "users/u1", "fields": {"uid": "u1", "displayName": "Igor"}},
        {"path": "users/u2", "fields": {"uid": "u2", "displayName": "Alice"}},
    ]
    fake_firestore["collections"][f"tournaments/{tid}/broadBets"] = [
        {"path": f"tournaments/{tid}/broadBets/u1", "fields": {
            "userId": "u1", "updatedAt": "2026-06-07T10:00:00Z",
            "selections": {"winner": "team_Portugal", "goldenBoot": "1780696080628",
                            "cinderella": "team_Uzbekistan", "bestPlayer": "roster_X"}}},
        {"path": f"tournaments/{tid}/broadBets/u2", "fields": {
            "userId": "u2", "updatedAt": "2026-06-06T20:00:00Z",
            "selections": {"winner": "team_Spain", "goldenBoot": "...",
                            "cinderella": "team_CapeVerde", "bestPlayer": "roster_Y"}}},
    ]
    out = ntm.toto_get_broad_bets(tid)
    by_name = {r["displayName"]: r for r in out}
    assert by_name["Igor"]["winner"] == "team_Portugal"
    assert by_name["Alice"]["cinderella"] == "team_CapeVerde"
    # Sorted alphabetical by displayName
    assert [r["displayName"] for r in out] == ["Alice", "Igor"]


# ─────────────────────────── toto_get_side_bets ───────────────────────────

def test_get_side_bets_active_only_filter(fake_firestore):
    tid = "tid-x"
    fake_firestore["collections"][f"tournaments/{tid}/sideBets"] = [
        {"path": f"tournaments/{tid}/sideBets/sb_2026-06-11", "fields": {
            "question": "Will there be a red card?", "stage": "Group Stage - 1",
            "startTime": "2026-06-11T19:00:00+00:00",
            "isActive": True, "isLocked": True, "isResolved": False}},
        {"path": f"tournaments/{tid}/sideBets/sb_2022-11-21", "fields": {
            "question": "Old", "stage": "Group Stage",
            "startTime": "2022-11-21T16:00:00+00:00",
            "isActive": False, "isResolved": True, "correctAnswer": True}},
    ]
    all_rows = ntm.toto_get_side_bets(tid)
    assert len(all_rows) == 2
    active = ntm.toto_get_side_bets(tid, active_only=True)
    assert len(active) == 1 and active[0]["id"] == "sb_2026-06-11"
    assert active[0]["stage"] == "Group"                          # mapped from "Group Stage - 1"


# ─────────────────────────── toto_get_my_preferences ───────────────────────────

def test_get_my_preferences_extracts_pref_fields(fake_firestore):
    fake_firestore["docs"]["users/uid-igor"] = {
        "uid": "uid-igor", "displayName": "Igor", "role": "player", "status": "approved",
        "pref_results": True, "pref_reminders": True, "pref_announcements": False,
        "pref_broadBets": True, "pref_sideBets": True,
        "pointsTotal": 0,                                          # NOT included in output
    }
    p = ntm.toto_get_my_preferences()
    assert p["displayName"] == "Igor"
    assert p["pref_announcements"] is False                       # the only False flag
    assert "pointsTotal" not in p                                  # preferences-only view


# ─────────────────────────── toto_update_preferences ───────────────────────────

def test_update_preferences_gated_by_env(fake_firestore, monkeypatch):
    monkeypatch.delenv("NEGEV_ALLOW_WRITES", raising=False)
    r = ntm.toto_update_preferences(pref_reminders=False)
    assert "writes disabled" in (r.get("error") or "")


def test_update_preferences_no_fields_passed_is_error(fake_firestore, monkeypatch):
    monkeypatch.setenv("NEGEV_ALLOW_WRITES", "1")
    r = ntm.toto_update_preferences()
    assert "nothing to update" in (r.get("error") or "")


def test_update_preferences_passes_only_explicit_fields(fake_firestore, monkeypatch):
    """When NEGEV_ALLOW_WRITES=1 and one pref is set, the PATCH body only has
    that one field — not all 5 prefs."""
    monkeypatch.setenv("NEGEV_ALLOW_WRITES", "1")
    captured = {}
    def _patch(url, headers=None, params=None, json=None, timeout=None):
        captured["url"] = url
        captured["fields"] = json.get("fields", {}) if json else {}
        captured["params"] = params
        return _FakeResp({"updated": True})
    monkeypatch.setattr(ntm.requests, "patch", _patch)
    ntm.toto_update_preferences(pref_reminders=False)
    assert list(captured["fields"]) == ["pref_reminders"]
    assert captured["fields"]["pref_reminders"] == {"booleanValue": False}


# ─────────────────────────── _read_all pagination ───────────────────────────

def test_read_all_paginates_via_nextPageToken(monkeypatch):
    monkeypatch.setattr(ntm, "_id_token", lambda: "fake-id")
    pages = [
        {"documents": [_encode_doc("c/1", {"i": 1}), _encode_doc("c/2", {"i": 2})],
         "nextPageToken": "page2"},
        {"documents": [_encode_doc("c/3", {"i": 3})]},  # last page, no token
    ]
    call_count = {"n": 0}
    def _get(url, headers=None, params=None, timeout=None):
        n = call_count["n"]; call_count["n"] += 1
        return _FakeResp(pages[n])
    monkeypatch.setattr(ntm.requests, "get", _get)
    docs = ntm._read_all("c")
    assert [d["i"] for d in docs] == [1, 2, 3]
    assert call_count["n"] == 2                                   # two pages fetched
