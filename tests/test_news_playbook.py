"""News agent playbook wiring: query budget, search window, rubric clamping,
and that a parsed delta flows through (deterministic parts only — no live LLM)."""
from orchestrator.agents import news_agent as na
from config.news import should_search, NEWS_MAX_QUERIES, DELTA_CLAMP


def test_search_queries_bounded_and_relevant():
    qs = na.search_queries("Norway", "France")
    assert len(qs) <= NEWS_MAX_QUERIES
    assert any("lineup" in q.lower() for q in qs)
    assert any("Norway" in q for q in qs) and any("France" in q for q in qs)


def test_search_window_excludes_lock():
    assert should_search("T-60m") and should_search("T-24h") and should_search("T-15m")
    assert not should_search("T-7m")            # no new info at lock
    assert not should_search("post-match")


import json


class _FakeRouter:
    """Mock LLM router that returns a string from complete() (the new analyze
    contract — Day-8 uses llm.complete with json_mode, not complete_json)."""
    def __init__(self, payload):
        self.payload = payload
    def complete(self, system, prompt, *, json_mode=True, max_tokens=500):
        return json.dumps(self.payload)


def test_rubric_deltas_are_clamped():
    # model returns out-of-range deltas → clamped to ±DELTA_CLAMP. Read the
    # constant from config so this test stays correct as the clamp is tuned
    # (Day-9.26: tightened 0.6 → 0.15 for tournament risk control).
    out = na.analyze("Norway", "France", "Norway rest everyone",
                     router=_FakeRouter({"home_goal_delta": -2.0, "away_goal_delta": 1.5,
                                         "confidence": "high", "notes": ["x"]}))
    assert out["home_goal_delta"] == -DELTA_CLAMP and out["away_goal_delta"] == DELTA_CLAMP
    assert out["confidence"] == "high"


def test_normal_delta_passes_and_defaults_filled():
    # Day-9.26: clamp tightened to ±0.15 — pick a within-clamp delta so this
    # test stays narrowly about pass-through + default-filling.
    out = na.analyze("A", "B", "nothing notable",
                     router=_FakeRouter({"home_goal_delta": -0.10, "away_goal_delta": 0.08}))
    assert out["home_goal_delta"] == -0.10 and out["away_goal_delta"] == 0.08
    assert out["confidence"] == "low" and out["notes"] == []   # defaults filled
    assert "discarded_sources" in out                            # Day-8 added field
