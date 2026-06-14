"""Day-6: build_card audit trail + degradation paths + persistence.

Every test runs offline by injecting all four loaders. The GOLDEN AUDITABILITY
RULE is pinned by test_auditability_golden_rule: every signal in
{dixon_coles, elo, market, news} must appear in signals_used OR signals_failed
on every emitted card, no exception.
"""
from __future__ import annotations
import json
import sqlite3
import pytest
from core.decision.build_card import build_card, persist_card, ALL_SIGNALS


# ---------- helpers / fakes ----------

def _match(stage="Group", detonator=False, mid=1,
           home="Mexico", away="South Africa"):
    return {"match_id": mid, "home": home, "away": away,
            "stage": stage, "detonator": detonator,
            "utc_kickoff": "2026-06-11T19:00:00+00:00",
            "group": "A"}


def _good_strengths(home="Mexico", away="South Africa"):
    return {"teams": {home: {"attack": 0.30, "defence": -0.20},
                       away: {"attack": -0.30, "defence": 0.20}},
            "home_adv": 0.20, "rho": -0.05}


def _good_odds():
    """Reasonable Pinnacle-style odds for a heavy favorite."""
    return {"H": 1.85, "D": 3.60, "A": 4.20, "book": "pinnacle"}


def _good_elo(home="Mexico", away="South Africa"):
    return {home: 1875.0, away: 1518.0}


def _good_news(*a, **kw):
    return {"home_goal_delta": 0.0, "away_goal_delta": 0.0,
            "confidence": "low", "notes": []}


def _schema_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        conn.executescript(f.read())
    return conn


# ---------- 1. GOLDEN AUDITABILITY RULE ----------

@pytest.mark.parametrize("scenario", [
    "all_good",
    "dc_fails",
    "elo_fails",
    "market_fails",
    "news_fails",
    "everything_fails",
])
def test_auditability_golden_rule(scenario):
    """Every signal in ALL_SIGNALS must appear in signals_used OR signals_failed
    on EVERY emitted card. Silent bypass = bug."""
    def raises(*a, **kw): raise RuntimeError("boom")

    loaders = {
        "strengths_loader": lambda _r: _good_strengths(),
        "elo_loader":       lambda: _good_elo(),
        "odds_fetcher":     lambda h, a, **k: _good_odds(),
        "news_analyzer":    _good_news,
        "results_loader":   lambda: [{"home": "X", "away": "Y",
                                       "home_goals": 1, "away_goals": 0,
                                       "days_ago": 30}],
    }
    if scenario == "dc_fails":     loaders["strengths_loader"] = raises
    if scenario == "elo_fails":    loaders["elo_loader"]       = raises
    if scenario == "market_fails": loaders["odds_fetcher"]     = raises
    if scenario == "news_fails":   loaders["news_analyzer"]    = raises
    if scenario == "everything_fails":
        loaders = {k: raises for k in loaders}

    card = build_card(_match(), **loaders)
    covered = set(card["signals_used"]) | set(card["signals_failed"])
    assert set(ALL_SIGNALS) <= covered, (
        f"signal(s) missing from audit trail: "
        f"{set(ALL_SIGNALS) - covered}  (scenario={scenario})")


# ---------- 2. Happy path: all four signals + EV-optimized ----------

def test_happy_path_all_signals_used():
    card = build_card(_match(),
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=_good_news,
                      results_loader=lambda: [])
    assert set(card["signals_used"]) == {"dixon_coles", "elo", "market", "news"}
    assert card["signals_failed"] == []
    assert card["failure_reasons"] == {}
    assert card["ev_pathway"] == "ev_optimized"
    assert card["pick_direction"] in ("H", "D", "A")


# ---------- 3. Per-signal failure → correct audit + degradation ----------

def test_news_failure_marks_signal_failed_with_reason():
    # Day-9.28: T-7m no longer calls news_analyzer (short-circuits to NEUTRAL).
    # Use T-60m so the analyzer is invoked and can fail.
    def boom(*a, **k): raise RuntimeError("gemini 429; claude empty")
    card = build_card(_match(), window="T-60m",
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=boom,
                      results_loader=lambda: [])
    assert "news" in card["signals_failed"]
    assert "gemini 429" in card["failure_reasons"]["news"]
    assert card["ev_pathway"] == "ev_optimized"  # other signals still good


def test_news_provider_stamped_on_card_when_analyzer_returns_one():
    """Day-8 audit visibility: when the LLM router answered (with a provider
    name), build_card propagates it to the flat news_provider field on the
    card so render_card can show 'News(gemini)' and the persisted prediction
    row carries the model identity for later cross-reference."""
    def good_with_provider(*a, **k):
        return {"home_goal_delta": -0.20, "away_goal_delta": +0.05,
                "confidence": "high", "notes": ["Mbappé starts"],
                "discarded_sources": [], "provider": "gemini",
                "fallbacks_used": []}
    # Day-9.28: T-7m short-circuits to NEUTRAL — use T-60m so the analyzer runs.
    card = build_card(_match(), window="T-60m",
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=good_with_provider,
                      results_loader=lambda: [])
    assert card["news_provider"] == "gemini"
    assert card["news_fallbacks_used"] == []
    assert card["news_failure"] is None
    assert "news" in card["signals_used"]


def test_news_provider_records_fallback_chain():
    """When gemini fails and claude takes over, fallbacks_used must show the
    chain walk so the audit trail is complete."""
    def good_with_fallback(*a, **k):
        return {"home_goal_delta": 0.10, "away_goal_delta": 0.0,
                "confidence": "medium", "notes": [],
                "discarded_sources": [], "provider": "claude",
                "fallbacks_used": ["gemini"]}
    # Day-9.28: T-7m short-circuits to NEUTRAL — use T-60m so the analyzer runs.
    card = build_card(_match(), window="T-60m",
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=good_with_fallback,
                      results_loader=lambda: [])
    assert card["news_provider"] == "claude"
    assert card["news_fallbacks_used"] == ["gemini"]


def test_market_failure_falls_back_to_modal():
    """No usable odds → modal_fallback branch in predict.match_card."""
    card = build_card(_match(),
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: None,   # budget over / no event
                      news_analyzer=_good_news,
                      results_loader=lambda: [])
    assert "market" in card["signals_failed"]
    assert card["ev_pathway"] == "modal_fallback"


def test_odds_returns_invalid_partial_dict_marks_market_failed():
    """A dict missing D or with bad values must not be treated as success."""
    card = build_card(_match(),
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: {"H": 1.5, "A": 3.0},  # no D
                      news_analyzer=_good_news,
                      results_loader=lambda: [])
    assert "market" in card["signals_failed"]


def test_dc_fit_failure_falls_back_to_neutral_eg_but_other_signals_intact():
    def boom(_r): raise RuntimeError("martj42 unreachable")
    card = build_card(_match(),
                      strengths_loader=boom,
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=_good_news,
                      results_loader=lambda: [])
    assert "dixon_coles" in card["signals_failed"]
    assert "elo" in card["signals_used"]
    assert "market" in card["signals_used"]
    assert "news" in card["signals_used"]
    # Even with neutral DC we still EV-optimize (market is fine)
    assert card["ev_pathway"] == "ev_optimized"


def test_elo_failure_marked_and_card_still_built():
    card = build_card(_match(),
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: {},   # empty → counted as failed
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=_good_news,
                      results_loader=lambda: [])
    assert "elo" in card["signals_failed"]
    assert "empty" in card["failure_reasons"]["elo"]


# ---------- 4. Penalty winner trigger conditions ----------

def test_group_stage_never_gets_penalty_winner():
    """Even when draw probability is huge, group games don't go to pens."""
    card = build_card(_match(stage="Group"),
                      strengths_loader=lambda _r: {
                          "teams": {"Mexico": {"attack": 0, "defence": 0},
                                    "South Africa": {"attack": 0, "defence": 0}},
                          "home_adv": 0.0, "rho": -0.05},
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: {"H": 3.0, "D": 2.5, "A": 3.0},
                      news_analyzer=_good_news,
                      results_loader=lambda: [])
    assert card["penalty_winner"] is None


def test_knockout_with_draw_over_threshold_sets_penalty_winner():
    """KO stage + market-implied high draw → penalty pick attached."""
    # Use balanced strengths + high draw odds → draw_prob > 15%
    card = build_card(_match(stage="R16", home="France", away="Germany"),
                      strengths_loader=lambda _r: {
                          "teams": {"France":  {"attack": 0, "defence": 0},
                                    "Germany": {"attack": 0, "defence": 0}},
                          "home_adv": 0.0, "rho": -0.05},
                      elo_loader=lambda: {"France": 2062.0, "Germany": 1925.0},
                      odds_fetcher=lambda h, a, **k: {"H": 2.5, "D": 2.5, "A": 3.0},
                      news_analyzer=_good_news,
                      results_loader=lambda: [])
    assert card["penalty_winner"] is not None
    assert card["penalty_winner"]["winner"] in ("H", "A")
    assert 0.5 <= card["penalty_winner"]["p_winner"] <= 0.55


def test_knockout_with_low_draw_prob_does_not_set_penalty_winner():
    """KO with crushing favorite → draw_prob < 15% → no penalty line."""
    card = build_card(_match(stage="R16"),
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: {"H": 1.1, "D": 12.0, "A": 25.0},
                      news_analyzer=_good_news,
                      results_loader=lambda: [],
                      draw_pen_threshold=0.15)
    assert card["penalty_winner"] is None


# ---------- 5. Persistence (predictions table) ----------

def test_persist_writes_payload_json_and_typed_columns():
    conn = _schema_conn()
    card = build_card(_match(),
                      conn=conn,
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=_good_news,
                      results_loader=lambda: [],
                      window="T-7m")
    row = conn.execute(
        "SELECT match_id, window, pick_dir, pick_h, pick_a, modal_h, modal_a, "
        "expected_points, payload_json FROM predictions WHERE match_id=?",
        (1,)).fetchone()
    assert row is not None
    assert row["window"] == "T-7m"
    assert row["pick_dir"] in ("H", "D", "A")
    assert row["pick_h"] == card["pick_exact_score"]["home"]
    assert row["pick_a"] == card["pick_exact_score"]["away"]
    payload = json.loads(row["payload_json"])
    assert set(payload["signals_used"]) >= {"dixon_coles", "elo", "market", "news"}
    assert "ev_pathway" in payload


def test_persist_is_idempotent_on_same_match_window():
    conn = _schema_conn()
    common = dict(
        strengths_loader=lambda _r: _good_strengths(),
        elo_loader=lambda: _good_elo(),
        odds_fetcher=lambda h, a, **k: _good_odds(),
        news_analyzer=_good_news,
        results_loader=lambda: [],
        window="T-7m")
    build_card(_match(), conn=conn, **common)
    build_card(_match(), conn=conn, **common)
    n = conn.execute("SELECT COUNT(*) FROM predictions WHERE match_id=1"
                      ).fetchone()[0]
    assert n == 1   # upsert, not duplicate


# ---------- 6. Never raises (safety net) ----------

def test_never_raises_even_when_everything_fails():
    def boom(*a, **k): raise RuntimeError("boom")
    # Day-9.28: T-7m short-circuits news to NEUTRAL (no LLM call), so use T-60m
    # to exercise the news failure path (boom raises after gather_context fails).
    card = build_card(_match(), window="T-60m",
                      strengths_loader=boom,
                      elo_loader=boom,
                      odds_fetcher=boom,
                      news_analyzer=boom,
                      results_loader=boom)
    # Card is degraded but rendered-able and complete
    assert isinstance(card, dict)
    assert set(card["signals_failed"]) >= {"dixon_coles", "elo", "market", "news"}
    assert card.get("pick_direction") is not None


# ---------- 7. Failure reason length cap ----------

def test_failure_reasons_are_compact_le_80_chars():
    def boom(*a, **k): raise RuntimeError("X" * 500)
    card = build_card(_match(),
                      strengths_loader=boom,
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=_good_news,
                      results_loader=lambda: [])
    assert len(card["failure_reasons"]["dixon_coles"]) <= 80


# ---------- 8. Team name normalization across sources ----------

def test_team_names_canonicalize_before_lookup():
    """build_card must run home/away through normalize() so loaders see
    canonical names regardless of which source the match dict came from."""
    seen_args = {}
    def remember_eg(_r):
        # Strengths dict keyed by canonical names
        return {"teams": {"South Korea": {"attack": 0.0, "defence": 0.0},
                          "Czechia":     {"attack": 0.0, "defence": 0.0}},
                "home_adv": 0.0, "rho": -0.05}
    def remember_odds(h, a, **k):
        seen_args["home"] = h
        seen_args["away"] = a
        return _good_odds()
    card = build_card({"match_id": 9, "home": "Korea Republic",
                        "away": "Czech Republic", "stage": "Group",
                        "utc_kickoff": "2026-06-12T19:00:00+00:00"},
                      strengths_loader=remember_eg,
                      elo_loader=lambda: {"South Korea": 1758, "Czechia": 1740},
                      odds_fetcher=remember_odds,
                      news_analyzer=_good_news,
                      results_loader=lambda: [])
    assert seen_args["home"] == "South Korea"
    assert seen_args["away"] == "Czechia"
    assert card["home"] == "South Korea"
    assert card["away"] == "Czechia"


# ---------- 9. Kickoff local time conversion ----------

def test_kickoff_local_derived_from_utc_when_absent():
    card = build_card(_match(),
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=_good_news,
                      results_loader=lambda: [],
                      local_tz="Asia/Jerusalem")
    # 2026-06-11 19:00 UTC = 22:00 Israel
    assert card["kickoff_local"] == "2026-06-11 22:00"


def test_group_label_strips_football_data_prefix():
    """football-data uses 'GROUP_A'; the card header should show 'A'."""
    m = _match()
    m["group"] = "GROUP_A"
    card = build_card(m,
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=_good_news,
                      results_loader=lambda: [])
    assert card["group"] == "A"


def test_kickoff_local_is_never_the_raw_iso_string():
    """Guard against accidental fallback to a DB-stored '2026-06-11T22:00:00+03:00'
    instead of the pretty '2026-06-11 22:00'."""
    m = _match()
    m["local_kickoff"] = "2026-06-11T22:00:00+03:00"   # raw ISO from football-data
    card = build_card(m,
                      strengths_loader=lambda _r: _good_strengths(),
                      elo_loader=lambda: _good_elo(),
                      odds_fetcher=lambda h, a, **k: _good_odds(),
                      news_analyzer=_good_news,
                      results_loader=lambda: [],
                      local_tz="Asia/Jerusalem")
    assert "T" not in card["kickoff_local"]
    assert "+" not in card["kickoff_local"]
    assert card["kickoff_local"] == "2026-06-11 22:00"


# ---------- 10. Events cache batching (Day-4 batched fetch) ----------

def test_events_cache_passed_through_to_odds_fetcher():
    """Scheduler shares ONE fetch_all_odds across many matches via events="""
    seen = {}
    def remember(h, a, *, events=None, **k):
        seen["events"] = events
        return _good_odds()
    sentinel = [{"home_team": "Mexico", "away_team": "South Africa",
                  "commence_time": "2026-06-11T19:00:00Z", "bookmakers": []}]
    build_card(_match(),
                strengths_loader=lambda _r: _good_strengths(),
                elo_loader=lambda: _good_elo(),
                odds_fetcher=remember,
                news_analyzer=_good_news,
                results_loader=lambda: [],
                events_cache=sentinel)
    assert seen["events"] is sentinel    # not refetched per match
