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

def _seed_matches(state, tid="tid-x", monkeypatch=None):
    """Seed mock matches AND wire toto_query to filter by tournamentId."""
    rows = [
        {"_path": "matches/855734",
         "apiFixtureId": 855734, "tournamentId": tid,
         "homeTeam": "Korea Republic", "awayTeam": "Cape Verde Islands",
         "date": "2022-11-21T16:00:00+00:00", "stage": "Group Stage - 1",
         "status": "FT", "scoreFullTimeHome": 0, "scoreFullTimeAway": 2,
         "oddsHome": None, "oddsDraw": None, "oddsAway": None,
         "oddsSource": "api", "isDetonator": False, "exactScoreMultiplier": 1},
        {"_path": "matches/999999",
         "apiFixtureId": 999999, "tournamentId": tid,
         "homeTeam": "Mexico", "awayTeam": "South Africa",
         "date": "2026-06-11T19:00:00+00:00", "stage": "Round of 16",
         "status": "NS", "scoreFullTimeHome": None, "scoreFullTimeAway": None,
         "oddsHome": 1.85, "oddsDraw": 3.6, "oddsAway": 4.2,
         "isDetonator": True, "exactScoreMultiplier": 2},
        # Different tournament — must be filtered OUT by toto_get_matches
        {"_path": "matches/777", "apiFixtureId": 777, "tournamentId": "other-tid",
         "homeTeam": "X", "awayTeam": "Y", "date": "2026-06-11T20:00:00+00:00",
         "stage": "Group Stage", "status": "NS"},
    ]
    state["_match_rows"] = rows
    if monkeypatch:
        def fake_query(c, field, op, value, limit=200):
            assert c == "matches" and field == "tournamentId" and op == "EQUAL"
            return {"results": [r for r in rows if r.get("tournamentId") == value]}
        monkeypatch.setattr(ntm, "toto_query", fake_query)


def test_get_matches_normalizes_team_names(fake_firestore, monkeypatch):
    _seed_matches(fake_firestore, monkeypatch=monkeypatch)
    out = ntm.toto_get_matches(tournament_id="tid-x")
    by_apifid = {m["apiFixtureId"]: m for m in out}
    assert by_apifid[855734]["home"] == "South Korea"             # Korea Republic → canonical
    assert by_apifid[855734]["away"] == "Cape Verde"              # Cape Verde Islands → canonical


def test_get_matches_maps_stage_labels(fake_firestore, monkeypatch):
    _seed_matches(fake_firestore, monkeypatch=monkeypatch)
    out = ntm.toto_get_matches(tournament_id="tid-x")
    by_apifid = {m["apiFixtureId"]: m for m in out}
    assert by_apifid[855734]["stage"] == "Group"                  # "Group Stage - 1" → Group
    assert by_apifid[999999]["stage"] == "R16"                    # "Round of 16" → R16


def test_get_matches_date_after_filter(fake_firestore, monkeypatch):
    _seed_matches(fake_firestore, monkeypatch=monkeypatch)
    out = ntm.toto_get_matches(tournament_id="tid-x", date_after="2025-01-01")
    apifids = {m["apiFixtureId"] for m in out}
    assert apifids == {999999}                                    # 2022 row excluded


def test_get_matches_status_filter(fake_firestore, monkeypatch):
    _seed_matches(fake_firestore, monkeypatch=monkeypatch)
    out = ntm.toto_get_matches(tournament_id="tid-x", status="NS")
    assert len(out) == 1 and out[0]["status"] == "NS"


def test_get_matches_stage_filter_uses_mapped_label(fake_firestore, monkeypatch):
    _seed_matches(fake_firestore, monkeypatch=monkeypatch)
    out = ntm.toto_get_matches(tournament_id="tid-x", stage="R16")
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


# ─────────────────────────── toto_get_matches scopes to one tournament ───────────────────────────

def test_get_matches_excludes_rows_from_other_tournaments(fake_firestore, monkeypatch):
    """Bug fix regression: USED to read the global matches collection and
    return ALL tournaments mixed (J-League / Allsvenskan etc.). Now structured-
    queries by tournamentId so only the requested pool's matches come back."""
    _seed_matches(fake_firestore, monkeypatch=monkeypatch, tid="tid-x")
    out = ntm.toto_get_matches(tournament_id="tid-x")
    tids = {m["tournamentId"] for m in out}
    assert tids == {"tid-x"}                           # NO 'other-tid' leak


# ─────────────────────────── toto_get_match_details ───────────────────────────

def test_get_match_details_combines_match_my_pred_friends_pts_grid(fake_firestore, monkeypatch):
    tid = "tid-x"
    _seed_matches(fake_firestore, monkeypatch=monkeypatch, tid=tid)
    monkeypatch.setitem(ntm._token, "uid", "uid-igor")
    rows = fake_firestore["_match_rows"]
    def fake_query(c, field, op, value, limit=200):
        if c == "matches" and field == "tournamentId":
            return {"results": [r for r in rows if r.get("tournamentId") == value]}
        if c == "bets":
            return {"results": [
                {"userId": "uid-igor", "matchId": "999999", "tournamentId": tid,
                 "homeScore": 3, "awayScore": 1, "points": 0, "_path": "bets/x"},
                {"userId": "uid-friend", "matchId": "999999", "tournamentId": tid,
                 "homeScore": 2, "awayScore": 0, "points": 0, "_path": "bets/y"},
            ]}
        return {"results": []}
    monkeypatch.setattr(ntm, "toto_query", fake_query)
    fake_firestore["docs"][f"tournaments/{tid}/settings/managerTables"] = {
        "grids": {"round16AndQuarter": {"3-1": 3.25}, "groupStage": {}, "semiAndFinal": {}}}
    fake_firestore["collections"]["users"] = [
        {"path": "users/uid-igor", "fields": {"uid": "uid-igor", "displayName": "Igor"}},
        {"path": "users/uid-friend", "fields": {"uid": "uid-friend", "displayName": "Alice"}},
    ]
    out = ntm.toto_get_match_details(home="Mexico", away="South Africa",
                                       tournament_id=tid)
    assert out["match"]["home"] == "Mexico"
    assert out["myPrediction"] == {"home": 3, "away": 1}
    assert out["exactPtsGridName"] == "round16AndQuarter"
    assert out["bingoMultiplier"] == 3.25
    assert any(f["displayName"] == "Igor" for f in out["friendsPicks"])


def test_get_match_details_returns_error_when_not_found(fake_firestore, monkeypatch):
    _seed_matches(fake_firestore, monkeypatch=monkeypatch, tid="tid-x")
    out = ntm.toto_get_match_details(home="Ghost", away="Nobody",
                                       tournament_id="tid-x")
    assert "error" in out


# ─────────────────────────── toto_update_match_result (gated) ───────────────────────────

def test_update_match_result_blocked_without_writes_flag(fake_firestore, monkeypatch):
    monkeypatch.delenv("NEGEV_ALLOW_WRITES", raising=False)
    out = ntm.toto_update_match_result("tid-x_999999", 2, 1, tournament_id="tid-x")
    assert "writes disabled" in (out.get("error") or "")


def test_update_match_result_when_writes_enabled_patches_correct_fields(fake_firestore, monkeypatch):
    monkeypatch.setenv("NEGEV_ALLOW_WRITES", "1")
    captured = {}
    def fake_patch(path, fields_json):
        captured["path"] = path
        captured["fields"] = json.loads(fields_json)
        return {"updated": path}
    monkeypatch.setattr(ntm, "toto_patch_document", fake_patch)
    ntm.toto_update_match_result(
        "999999", 2, 1, tournament_id="tid-x", status="FT")
    assert captured["path"] == "matches/tid-x_999999"
    assert captured["fields"] == {
        "scoreFullTimeHome": 2, "scoreFullTimeAway": 1,
        "goalsHome": 2, "goalsAway": 1, "status": "FT"}


def test_update_match_result_knockout_with_penalties(fake_firestore, monkeypatch):
    monkeypatch.setenv("NEGEV_ALLOW_WRITES", "1")
    captured = {}
    def fake_patch(path, fields_json):
        captured["fields"] = json.loads(fields_json)
        return {"updated": path}
    monkeypatch.setattr(ntm, "toto_patch_document", fake_patch)
    ntm.toto_update_match_result(
        "tid-x_999", 1, 1, tournament_id="tid-x", status="PEN",
        penalty_home=5, penalty_away=4, winner_team="Mexico")
    assert captured["fields"]["status"] == "PEN"
    assert captured["fields"]["scorePenaltyHome"] == 5
    assert captured["fields"]["scorePenaltyAway"] == 4
    assert captured["fields"]["winnerTeam"] == "Mexico"


# ─────────────────────────── toto_next_match ───────────────────────────

def test_next_match_returns_first_pending_with_correct_stage_type(fake_firestore, monkeypatch):
    _seed_matches(fake_firestore, monkeypatch=monkeypatch, tid="tid-x")
    out = ntm.toto_next_match(tournament_id="tid-x")
    assert out["match"]["home"] == "Mexico"
    assert out["stage_type"] == "knockout"
    assert out["requires_penalties"] is True
    assert "knockout" in out["instructions"].lower()


def test_next_match_for_group_only_asks_for_score(fake_firestore, monkeypatch):
    state = fake_firestore
    state["_match_rows"] = [
        {"_path": "matches/g1", "apiFixtureId": 1, "tournamentId": "tid-x",
         "homeTeam": "A", "awayTeam": "B", "date": "2026-06-11T10:00:00+00:00",
         "stage": "Group Stage", "status": "NS",
         "oddsHome": 1.5, "oddsDraw": 3, "oddsAway": 5},
    ]
    monkeypatch.setattr(ntm, "toto_query",
        lambda c, f, op, v, limit=200: {"results": state["_match_rows"]})
    out = ntm.toto_next_match(tournament_id="tid-x")
    assert out["stage_type"] == "group"
    assert out["requires_penalties"] is False


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

# ─────────────────────── _is_bot detection (triple-redundant) ───────────────────────

def test_is_bot_catches_role_field():
    assert ntm._is_bot({"uid": "abc", "role": "bot"}) is True


def test_is_bot_catches_isBot_field():
    assert ntm._is_bot({"uid": "abc", "isBot": True}) is True


def test_is_bot_catches_uid_prefix():
    """Even if a bot is missing the role/isBot fields, the uid prefix saves us."""
    assert ntm._is_bot({"uid": "bot_chinchilla"}) is True


def test_is_bot_returns_false_for_human():
    assert ntm._is_bot({"uid": "abc123", "role": "player", "isBot": False}) is False
    assert ntm._is_bot({"uid": "abc123"}) is False           # no fields at all
    assert ntm._is_bot({}) is False                          # totally empty


def test_is_bot_doesnt_match_human_uid_containing_bot():
    """Word 'bot' inside a uid (not as a prefix) is fine — only the 'bot_'
    prefix is the convention. 'cabotagecorp' → human."""
    assert ntm._is_bot({"uid": "cabotagecorp123", "role": "player"}) is False


def test_get_standings_excludes_all_three_known_negev_bots(fake_firestore):
    """Live: Negev Toto 2026 has 3 known bots — Chinchilla, Monkey, Owl. Each
    carries all 3 bot signals. They MUST NOT appear in our standings when
    include_bots=False (the default). Pins the live-discovered behavior."""
    tid = "tid-x"
    fake_firestore["collections"]["users"] = [
        {"path": "users/bot_chinchilla", "fields": {
            "uid": "bot_chinchilla", "displayName": "The Chinchilla",
            "role": "bot", "isBot": True, "tournaments": [tid],
            "pointsTotal": 4.3, "directionPoints": 2, "broadBetPoints": 0,
            "exactScoreCount": 1}},
        {"path": "users/bot_monkey", "fields": {
            "uid": "bot_monkey", "displayName": "The Monkey",
            "role": "bot", "isBot": True, "tournaments": [tid],
            "pointsTotal": 0, "directionPoints": 0, "broadBetPoints": 0,
            "exactScoreCount": 0}},
        {"path": "users/bot_owl", "fields": {
            "uid": "bot_owl", "displayName": "The Owl",
            "role": "bot", "isBot": True, "tournaments": [tid],
            "pointsTotal": 0, "directionPoints": 0, "broadBetPoints": 0,
            "exactScoreCount": 0}},
        {"path": "users/u-igor", "fields": {
            "uid": "uid-igor", "displayName": "Igor",
            "role": "player", "tournaments": [tid],
            "pointsTotal": 0, "directionPoints": 0, "broadBetPoints": 0,
            "exactScoreCount": 0}},
    ]
    # Default: bots excluded
    rows = ntm.toto_get_standings(tid)
    names = {r["player"] for r in rows}
    assert names == {"Igor"}                                 # only the human
    # Critically: Chinchilla's 4.3 pts must NOT be reflected as a "leader" —
    # if it leaked through, Igor's gap would be 4.3 instead of 0, and the
    # strategy layer would tilt for variance when there's no actual leader
    assert rows[0]["total"] == 0
    # Explicit opt-in: bots included
    rows_with_bots = ntm.toto_get_standings(tid, include_bots=True)
    names_with_bots = {r["player"] for r in rows_with_bots}
    assert names_with_bots == {"Igor", "The Chinchilla", "The Monkey", "The Owl"}


# ─────────────────────── toto_get_scoring_grids ───────────────────────

def test_get_scoring_grids_returns_three_named_grids(fake_firestore):
    tid = "tid-x"
    fake_firestore["docs"][f"tournaments/{tid}/settings/managerTables"] = {
        "grids": {
            "groupStage": {"0-0": 2.75, "1-0": 1.5, "1-1": 2.25, "2-1": 1.5},
            "round16AndQuarter": {"0-0": 3.75, "1-0": 2.25, "1-1": 3.0},
            "semiAndFinal": {"0-0": 5.0, "1-0": 3.0, "1-1": 4.0},
        }
    }
    out = ntm.toto_get_scoring_grids(tid)
    assert out["tournament_id"] == tid
    assert set(out["grids"].keys()) == {"groupStage", "round16AndQuarter", "semiAndFinal"}
    # Spot-check one cell
    assert out["grids"]["groupStage"]["2-1"] == 1.5


# ─────────────────────── toto_get_broad_bet_categories ───────────────────────

def test_get_broad_bet_categories_returns_full_options(fake_firestore):
    tid = "tid-x"
    fake_firestore["docs"][f"tournaments/{tid}/settings/broadBets"] = {
        "isPublished": True,
        "isLocked": False,
        "categories": [
            {"id": "winner", "options": [
                {"id": "team_Portugal", "name": "Portugal", "points": 39, "isKilled": False},
                {"id": "team_Spain", "name": "Spain", "points": 20, "isKilled": False},
            ]},
            {"id": "goldenBoot", "options": [
                {"id": "1780696080628", "name": "Mbappe", "points": 20, "isKilled": False},
            ]},
        ]
    }
    fake_firestore["collections"]["users"] = []                # synth-bestPlayer with empty roster
    out = ntm.toto_get_broad_bet_categories(tid)
    assert out["isPublished"] is True
    assert out["isLocked"] is False
    cat_ids = {c["id"] for c in out["categories"]}
    # bestPlayer is auto-appended even if settings doc lacks it
    assert cat_ids == {"winner", "goldenBoot", "bestPlayer"}
    winner = next(c for c in out["categories"] if c["id"] == "winner")
    assert len(winner["options"]) == 2
    assert winner["options"][0]["name"] == "Portugal"


def test_get_broad_bet_categories_synthesizes_bestPlayer_from_users(fake_firestore):
    """The bestPlayer category is a META-BET: which PARTICIPANT (friend) will
    finish highest in the pool — NOT a football player. The Negev app dynamically
    builds this dropdown from the users collection client-side, so our MCP must
    do the same. Without this synthesis, the settings doc only contains 1
    placeholder option, but the UI shows ~50."""
    tid = "tid-x"
    fake_firestore["docs"][f"tournaments/{tid}/settings/broadBets"] = {
        "isPublished": True, "isLocked": False,
        "categories": [
            {"id": "winner", "options": []},
            {"id": "bestPlayer", "options": [
                {"id": "placeholder", "name": "placeholder", "points": 5, "isKilled": False}
            ]},
        ]
    }
    fake_firestore["collections"]["users"] = [
        # 3 approved humans in this tournament
        {"path": "users/u1", "fields": {"uid": "u1", "displayName": "Aharony",
                                        "role": "player", "status": "approved",
                                        "tournaments": [tid]}},
        {"path": "users/u2", "fields": {"uid": "u2", "displayName": "Alfi",
                                        "role": "player", "status": "approved",
                                        "tournaments": [tid]}},
        {"path": "users/u3", "fields": {"uid": "u3", "displayName": "Igor",
                                        "role": "player", "status": "approved",
                                        "tournaments": [tid]}},
        # Bot — excluded
        {"path": "users/bot1", "fields": {"uid": "bot_chinchilla",
                                          "displayName": "The Chinchilla",
                                          "role": "bot", "isBot": True,
                                          "status": "approved",
                                          "tournaments": [tid]}},
        # Pending — excluded (status != approved)
        {"path": "users/u4", "fields": {"uid": "u4", "displayName": "Pending",
                                        "role": "player", "status": "pending",
                                        "tournaments": [tid]}},
        # Different tournament — excluded
        {"path": "users/u5", "fields": {"uid": "u5", "displayName": "Outsider",
                                        "role": "player", "status": "approved",
                                        "tournaments": ["other-tid"]}},
    ]
    out = ntm.toto_get_broad_bet_categories(tid)
    bp = next(c for c in out["categories"] if c["id"] == "bestPlayer")
    names = [o["name"] for o in bp["options"]]
    # Only approved humans in this tournament; alphabetical; placeholder gone
    assert names == ["Aharony", "Alfi", "Igor"]
    assert bp["_synthesized"] is True                          # audit trail
    # All synthesized options have the default Kod-bonus value of 5
    assert all(o["points"] == 5 for o in bp["options"])
    assert all(o["isKilled"] is False for o in bp["options"])


def test_get_broad_bet_categories_appends_bestPlayer_if_missing(fake_firestore):
    """If the settings doc doesn't list bestPlayer at all, we still synthesize
    it from users so the caller gets a complete picture."""
    tid = "tid-x"
    fake_firestore["docs"][f"tournaments/{tid}/settings/broadBets"] = {
        "isPublished": True, "isLocked": False,
        "categories": [{"id": "winner", "options": []}],          # no bestPlayer entry
    }
    fake_firestore["collections"]["users"] = [
        {"path": "users/u1", "fields": {"uid": "u1", "displayName": "Igor",
                                        "role": "player", "status": "approved",
                                        "tournaments": [tid]}},
    ]
    out = ntm.toto_get_broad_bet_categories(tid)
    bp = next(c for c in out["categories"] if c["id"] == "bestPlayer")
    assert [o["name"] for o in bp["options"]] == ["Igor"]
    assert bp["_synthesized"] is True


# ─────────────────────── toto_get_match_bets ───────────────────────

def test_get_match_bets_filters_by_tournament_and_joins_displayName(fake_firestore, monkeypatch):
    tid = "tid-x"
    other_tid = "tid-other"
    # Mock toto_query result
    def fake_query(*a, **k):
        return {"results": [
            {"userId": "u1", "matchId": "m1", "tournamentId": tid, "homeScore": 2,
             "awayScore": 1, "points": 3.0, "isCorrectDir": True, "isExactScore": False,
             "breakdown": {"basePoints": 1.0, "totalPoints": 3.0, "odds": 2.0,
                            "multiplier": 1.5, "detonatorMultiplier": 1,
                            "penaltiesBonus": 0, "isCorrectDir": True,
                            "isExactScore": False, "points": 3.0},
             "processedAt": "2026-06-11T22:00:00Z", "updatedAt": "...",
             "isBot": False, "_path": "bets/x"},
            {"userId": "u1", "matchId": "m1", "tournamentId": other_tid, "homeScore": 0,
             "awayScore": 0, "points": 99.0, "_path": "bets/y"},  # wrong tournament
        ]}
    monkeypatch.setattr(ntm, "toto_query", fake_query)
    fake_firestore["collections"]["users"] = [
        {"path": "users/u1", "fields": {"uid": "u1", "displayName": "Igor"}}
    ]
    rows = ntm.toto_get_match_bets("m1", tid)
    assert len(rows) == 1                                  # other tournament filtered out
    assert rows[0]["userId"] == "u1"
    assert rows[0]["displayName"] == "Igor"
    assert rows[0]["breakdown"]["odds"] == 2.0
    assert rows[0]["points"] == 3.0


# ─────────────────────── toto_get_my_bets ───────────────────────

def test_get_my_bets_filters_by_my_uid_and_tournament(fake_firestore, monkeypatch):
    tid = "tid-x"
    def fake_query(c, field, op, value, limit=200):
        # Simulate Firestore query for userId = my uid
        assert field == "userId" and value == "uid-igor"
        return {"results": [
            {"userId": "uid-igor", "matchId": "m1", "tournamentId": tid,
             "homeScore": 2, "awayScore": 1, "points": 3.0,
             "updatedAt": "2026-06-11T22:00:00Z", "_path": "bets/x"},
            {"userId": "uid-igor", "matchId": "m2", "tournamentId": "tid-other",
             "homeScore": 1, "awayScore": 0, "_path": "bets/y"},
        ]}
    monkeypatch.setattr(ntm, "toto_query", fake_query)
    rows = ntm.toto_get_my_bets(tid)
    assert len(rows) == 1                                  # filtered to tid
    assert rows[0]["matchId"] == "m1"


# ─────────────────────── _tid resolution edges ───────────────────────

def test_tid_resolves_from_arg_first(monkeypatch):
    monkeypatch.setenv("NEGEV_TOURNAMENT_ID", "tid-env")
    assert ntm._tid("tid-arg") == "tid-arg"


def test_tid_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("NEGEV_TOURNAMENT_ID", "tid-env")
    assert ntm._tid(None) == "tid-env"


def test_tid_raises_clear_error_when_neither_set(monkeypatch):
    monkeypatch.delenv("NEGEV_TOURNAMENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="tournament_id required"):
        ntm._tid(None)


# ─────────────────────── mcp import is optional ───────────────────────

def test_module_works_without_mcp_package_installed():
    """Regression: the `mcp` import must be optional so tools that use this
    module as a library (e.g. tools/sync_negev_standings.py) work on the VM
    without the `mcp[cli]` package installed."""
    # The module imported fine — that's the assertion. If `mcp` were a hard
    # dependency the test file wouldn't have loaded.
    assert hasattr(ntm, "toto_get_standings")
    assert hasattr(ntm, "_tid")


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
