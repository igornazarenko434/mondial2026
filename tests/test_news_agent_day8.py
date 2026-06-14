"""Day-8 news agent — comprehensive offline coverage of:
 - per-window query generation (date + WC 2026 + stage tagged)
 - gather_context assembly (API-Football + Brave Search graceful merge)
 - JSON parsing tiers (strict → repair → NEUTRAL)
 - analyze + analyze_safe degradation
 - clamp + default-fill of output
"""
from __future__ import annotations
import json
from unittest.mock import MagicMock
import pytest

from orchestrator.agents import news_agent as na
from config.news import (
    DELTA_CLAMP, NEWS_MAX_QUERIES, QUERIES_PER_WINDOW, should_search,
)


# ─────────────────── Layer 1: query generation ───────────────────

def test_search_queries_per_window_counts_match_config():
    qs_24 = na.search_queries("Mexico", "South Africa", window="T-24h")
    qs_60 = na.search_queries("Mexico", "South Africa", window="T-60m")
    qs_15 = na.search_queries("Mexico", "South Africa", window="T-15m")
    assert len(qs_24) == QUERIES_PER_WINDOW["T-24h"]
    assert len(qs_60) == QUERIES_PER_WINDOW["T-60m"]
    assert len(qs_15) == QUERIES_PER_WINDOW["T-15m"]
    # NEWS_MAX_QUERIES global cap respected
    assert all(len(qs) <= NEWS_MAX_QUERIES for qs in (qs_24, qs_60, qs_15))


def test_search_queries_include_team_names():
    qs = na.search_queries("Norway", "France", window="T-60m")
    assert all("Norway" in q or "France" in q for q in qs)


def test_search_queries_include_world_cup_2026_anchor():
    """L1 guardrail: every T-60m query must mention WC 2026 so old tournaments
    can't outrank the current match."""
    qs = na.search_queries("Mexico", "South Africa", window="T-60m",
                            kickoff_utc="2026-06-11T19:00:00Z")
    assert all("World Cup 2026" in q for q in qs)


def test_search_queries_include_kickoff_date():
    """L1 guardrail: queries date-stamped so wrong-day articles can't surface."""
    qs = na.search_queries("Mexico", "South Africa", window="T-60m",
                            kickoff_utc="2026-06-11T19:00:00Z")
    # At least the lineup queries carry the date
    assert any("2026-06-11" in q for q in qs)


def test_search_queries_include_stage_label_for_knockouts():
    qs = na.search_queries("France", "Argentina", window="T-24h",
                            stage="QF")
    # The T-24h preview query carries the stage label
    assert any("Quarter-finals" in q for q in qs)


def test_should_search_excludes_lock_window():
    assert should_search("T-60m") and should_search("T-24h") and should_search("T-15m")
    assert not should_search("T-7m")
    assert not should_search("post-match")


# ─────────────────── Layer 3: gather_context ───────────────────

def test_gather_context_includes_match_and_fetched_header():
    """Every context block must start with [MATCH: ...] and [FETCHED: ...]
    so the LLM has the date anchor for L4 filtering."""
    match = {"home": "Mexico", "away": "South Africa", "stage": "Group",
              "group": "A", "utc_kickoff": "2026-06-11T19:00:00+00:00"}

    fake_af = MagicMock()
    fake_af.find_fixture_id.return_value = None  # season not populated yet
    fake_af.find_team_id.return_value = None

    fake_ws = MagicMock()
    fake_ws.return_value = []                      # no key / no results

    txt = na.gather_context(match, window="T-60m",
                             api_football=fake_af,
                             web_search_many=fake_ws)
    assert txt.startswith("[MATCH: Mexico vs South Africa")
    assert "[FETCHED:" in txt
    assert "Group A" in txt


def test_gather_context_assembles_api_football_blocks():
    match = {"home": "Mexico", "away": "South Africa", "stage": "Group",
              "group": "A", "utc_kickoff": "2026-06-11T19:00:00+00:00"}

    fake_af = MagicMock()
    fake_af.find_fixture_id.return_value = 123456
    fake_af.fetch_lineups.return_value = [
        {"team": "Mexico", "formation": "4-3-3",
         "startXI": ["Ochoa (G)"], "substitutes": []}
    ]
    fake_af.find_team_id.side_effect = [771, 772]
    fake_af.fetch_injuries.side_effect = [
        [{"player": "Lozano", "reason": "Hamstring"}],
        [],   # no injuries for SA
    ]
    fake_ws = MagicMock(return_value=[])

    txt = na.gather_context(match, window="T-60m",
                             api_football=fake_af,
                             web_search_many=fake_ws)
    assert "API-Football /fixtures/lineups" in txt
    assert "Mexico (4-3-3)" in txt
    assert "Lozano (Hamstring)" in txt
    assert "South Africa injuries: none reported" in txt


def test_gather_context_includes_brave_web_results_when_available():
    match = {"home": "Mexico", "away": "South Africa", "stage": "Group",
              "group": "A", "utc_kickoff": "2026-06-11T19:00:00+00:00"}

    fake_af = MagicMock()
    fake_af.find_fixture_id.return_value = None
    fake_af.find_team_id.return_value = None
    fake_ws = MagicMock(return_value=[
        {"title": "Mexico vs SA preview", "snippet": "Lozano starts",
         "url": "https://espn.com/...", "date": "2026-06-11"},
    ])

    txt = na.gather_context(match, window="T-60m",
                             api_football=fake_af, web_search_many=fake_ws)
    assert "brave_search" in txt
    assert "Mexico vs SA preview" in txt
    assert "[2026-06-11]" in txt


def test_gather_context_respects_max_chars_cap():
    """L3: context_text capped to keep LLM tokens bounded."""
    long_snippet = "x" * 10000
    match = {"home": "A", "away": "B", "stage": "Group", "group": "A",
              "utc_kickoff": "2026-06-11T19:00:00+00:00"}
    fake_af = MagicMock()
    fake_af.find_fixture_id.return_value = None
    fake_af.find_team_id.return_value = None
    fake_ws = MagicMock(return_value=[
        {"title": "T", "snippet": long_snippet, "url": "u",
         "date": "2026-06-11"} for _ in range(20)
    ])
    from config.news import CONTEXT_MAX_CHARS
    txt = na.gather_context(match, window="T-60m",
                             api_football=fake_af, web_search_many=fake_ws)
    assert len(txt) <= CONTEXT_MAX_CHARS


def test_gather_context_skips_api_football_at_T24h():
    """L2: lineups aren't published 24h out, so no point asking API-Football
    at T-24h. gather_context should only query Brave at that window."""
    match = {"home": "Mexico", "away": "South Africa", "stage": "Group",
              "utc_kickoff": "2026-06-11T19:00:00+00:00"}
    fake_af = MagicMock()
    fake_ws = MagicMock(return_value=[])
    na.gather_context(match, window="T-24h",
                       api_football=fake_af, web_search_many=fake_ws)
    fake_af.find_fixture_id.assert_not_called()
    fake_af.fetch_lineups.assert_not_called()


def test_gather_context_resilient_to_api_football_exception():
    """A raised exception from API-Football MUST NOT propagate; context
    proceeds with whatever else is available."""
    match = {"home": "Mexico", "away": "South Africa", "stage": "Group",
              "utc_kickoff": "2026-06-11T19:00:00+00:00"}
    fake_af = MagicMock()
    fake_af.find_fixture_id.side_effect = RuntimeError("boom")
    fake_ws = MagicMock(return_value=[])
    txt = na.gather_context(match, window="T-60m",
                             api_football=fake_af, web_search_many=fake_ws)
    assert "[MATCH:" in txt
    assert "lineups source unavailable" in txt


# ─────────────────── Layer 5: parse + clamp + analyze_safe ───────────────────

class _RawRouter:
    """Mock that returns whatever raw string we pass via complete()."""
    def __init__(self, raw):
        self.raw = raw
    def complete(self, system, prompt, *, json_mode=True, max_tokens=500):
        return self.raw


def test_strict_json_parse_succeeds():
    # Day-9.26: DELTA_CLAMP tightened from 0.6 → 0.15. Use values strictly
    # inside the clamp so this test asserts parse-success without colliding
    # with the validator clamp (clamping is covered by
    # test_clamp_enforces_delta_cap below).
    payload = {"home_goal_delta": -0.10, "away_goal_delta": 0.05,
               "confidence": "high", "notes": ["test"],
               "discarded_sources": []}
    out = na.analyze("A", "B", "ctx",
                      router=_RawRouter(json.dumps(payload)))
    assert out["home_goal_delta"] == -0.10 and out["confidence"] == "high"


def test_repair_mode_json_parse_handles_text_around_json():
    """L5: LLM emits 'Here's the JSON: {...}' — extract via regex.

    Day-9.26: deltas chosen to lie inside the tightened ±0.15 clamp so this
    test stays narrowly about regex-extraction; clamping is tested separately."""
    raw = "Here is the JSON adjustment you asked for:\n```json\n{\"home_goal_delta\":0.1,\"away_goal_delta\":-0.12,\"confidence\":\"medium\",\"notes\":[],\"discarded_sources\":[]}\n```\nLet me know if you need more."
    out = na.analyze("A", "B", "ctx", router=_RawRouter(raw))
    assert out["home_goal_delta"] == 0.1
    assert out["away_goal_delta"] == -0.12


def test_malformed_json_returns_neutral_via_safe():
    """L5: unparseable JSON → analyze_safe returns NEUTRAL."""
    out = na.analyze_safe("A", "B", "ctx",
                            router=_RawRouter("not json at all{{{"))
    # analyze_safe catches; deltas are 0.0
    assert out["home_goal_delta"] == 0.0
    assert out["away_goal_delta"] == 0.0


def test_clamp_enforces_delta_cap():
    raw = json.dumps({"home_goal_delta": -2.0, "away_goal_delta": 1.5,
                       "confidence": "high", "notes": ["x"]})
    out = na.analyze("A", "B", "ctx", router=_RawRouter(raw))
    assert out["home_goal_delta"] == -DELTA_CLAMP
    assert out["away_goal_delta"] == DELTA_CLAMP


def test_defaults_filled_when_missing_fields():
    raw = json.dumps({"home_goal_delta": 0.1, "away_goal_delta": -0.05})
    out = na.analyze("A", "B", "ctx", router=_RawRouter(raw))
    assert out["confidence"] == "low"          # default
    assert out["notes"] == []
    assert out["discarded_sources"] == []


def test_notes_capped_at_5_entries_and_80_chars():
    long_note = "x" * 200
    raw = json.dumps({"home_goal_delta": 0.0, "away_goal_delta": 0.0,
                       "confidence": "low",
                       "notes": [long_note] * 10})
    out = na.analyze("A", "B", "ctx", router=_RawRouter(raw))
    assert len(out["notes"]) == 5
    assert all(len(n) <= 80 for n in out["notes"])


def test_invalid_confidence_falls_back_to_low():
    raw = json.dumps({"home_goal_delta": 0.0, "away_goal_delta": 0.0,
                       "confidence": "EXTREMELY_HIGH",
                       "notes": []})
    out = na.analyze("A", "B", "ctx", router=_RawRouter(raw))
    assert out["confidence"] == "low"


def test_analyze_safe_returns_neutral_on_router_exception():
    class Boom:
        def complete(self, *a, **k): raise RuntimeError("LLM down")
    out = na.analyze_safe("A", "B", "ctx", router=Boom())
    # Neutral deltas/notes preserved.
    for k in na.NEUTRAL:
        assert out[k] == na.NEUTRAL[k]
    # Failure + provider audit fields stamped so render_card can show
    # ⚠news and we know which model was attempted (for Honeycomb cross-ref).
    assert "failure" in out and "LLM down" in out["failure"]
    assert "provider" in out          # may be None when router fully crashed
    assert "fallbacks_used" in out


# ─────────────────── End-to-end via build_card ───────────────────

def test_build_card_uses_gather_context_at_search_windows():
    """Final integration: build_card calls gather_context for T-60m (a search
    window) but passes empty context for T-7m (lock window)."""
    from core.decision.build_card import build_card
    seen = {"contexts": []}

    def fake_news_analyzer(home, away, *, context_text=""):
        seen["contexts"].append(context_text)
        return {"home_goal_delta": 0.0, "away_goal_delta": 0.0,
                 "confidence": "low", "notes": [], "discarded_sources": []}

    match = {"match_id": 999, "home": "Mexico", "away": "South Africa",
              "stage": "Group", "group": "A",
              "utc_kickoff": "2026-06-11T19:00:00+00:00", "detonator": True}

    # T-7m — should pass empty context (no search at lock)
    build_card(match, window="T-7m",
                strengths_loader=lambda _r: {"teams": {"Mexico": {"attack": 0.3, "defence": -0.2},
                                                        "South Africa": {"attack": -0.3, "defence": 0.2}},
                                              "home_adv": 0.2, "rho": -0.05},
                elo_loader=lambda: {"Mexico": 1875.0, "South Africa": 1518.0},
                odds_fetcher=lambda h, a, **k: None,
                news_analyzer=fake_news_analyzer,
                results_loader=lambda: [])

    # Day-9.28: T-7m now short-circuits to NEUTRAL without calling news_analyzer
    # at all (no LLM call with empty context). Previously it called the analyzer
    # with context_text="" — now it returns NEUTRAL directly, saving 1 LLM call.
    assert seen["contexts"] == [], \
        "T-7m must NOT call news_analyzer — returns NEUTRAL directly (no LLM call)"


# ─────────────────── T-15m cache reuse (Brave cost cut) ───────────────────

def test_read_prior_deltas_reuses_recent_high_confidence(tmp_path):
    """T-15m must skip the LLM + Brave call when T-60m's stored deltas are
    fresh enough and confident enough — saves 1 LLM call + 4 Brave queries
    per match."""
    import sqlite3
    import json
    from datetime import datetime, timezone
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        conn.executescript(f.read())
    # Day-9.26: deltas chosen inside the tightened ±0.15 clamp so the read-back
    # preserves the stored values exactly. The cache-reuse mechanism (what this
    # test exercises) is independent of the clamp; clamping on read is a
    # safety belt that this test isn't trying to exercise.
    payload = json.dumps({
        "news_home_delta": -0.12, "news_away_delta": +0.10,
        "news_confidence": "high",
        "news_notes": ["Norway rotates", "Mbappé starts"]})
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO predictions (match_id, created_at, window, pick_dir, "
                  "pick_h, pick_a, modal_h, modal_a, expected_points, payload_json) "
                  "VALUES (1, ?, 'T-60m', 'A', 1, 2, 1, 2, 1.9, ?)",
                  (now, payload))
    conn.commit()

    prior = na.read_prior_deltas(conn, match_id=1,
                                   max_age_min=75, min_confidence="medium")
    assert prior is not None
    assert prior["home_goal_delta"] == -0.12
    assert prior["away_goal_delta"] == 0.10
    assert prior["confidence"] == "high"


def test_read_prior_deltas_rejects_stale(tmp_path):
    """A T-60m card older than max_age_min must not be reused."""
    import sqlite3, json
    from datetime import datetime, timezone, timedelta
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        conn.executescript(f.read())
    old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    payload = json.dumps({"news_home_delta": -0.3, "news_away_delta": 0.15,
                           "news_confidence": "high", "news_notes": []})
    conn.execute("INSERT INTO predictions (match_id, created_at, window, pick_dir, "
                  "pick_h, pick_a, modal_h, modal_a, expected_points, payload_json) "
                  "VALUES (1, ?, 'T-60m', 'A', 1, 2, 1, 2, 1.9, ?)",
                  (old, payload))
    conn.commit()
    assert na.read_prior_deltas(conn, match_id=1, max_age_min=75) is None


def test_read_prior_deltas_rejects_low_confidence(tmp_path):
    """Even if recent, a low-confidence T-60m deserves a fresh T-15m scan."""
    import sqlite3, json
    from datetime import datetime, timezone
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        conn.executescript(f.read())
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps({"news_home_delta": 0.0, "news_away_delta": 0.0,
                           "news_confidence": "low", "news_notes": []})
    conn.execute("INSERT INTO predictions (match_id, created_at, window, pick_dir, "
                  "pick_h, pick_a, modal_h, modal_a, expected_points, payload_json) "
                  "VALUES (1, ?, 'T-60m', 'A', 0, 0, 0, 0, 0.0, ?)",
                  (now, payload))
    conn.commit()
    assert na.read_prior_deltas(conn, match_id=1, max_age_min=75,
                                  min_confidence="medium") is None


def test_t60m_queries_trimmed_to_four_to_fit_brave_credit():
    """Regression: the T-60m query count MUST stay at 4. Higher = over Brave's
    1,000-call/month free credit (4 × 104 matches = 416). Lower = lose info."""
    qs = na.search_queries("Mexico", "South Africa", window="T-60m",
                             kickoff_utc="2026-06-11T19:00:00Z")
    assert len(qs) == 4
