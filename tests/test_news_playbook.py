"""News agent playbook wiring: query budget, search window, rubric clamping,
and that a parsed delta flows through (deterministic parts only — no live LLM)."""
from orchestrator.agents import news_agent as na
from config.news import should_search, NEWS_MAX_QUERIES


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
    # model returns out-of-range deltas → clamped to ±DELTA_CLAMP (=0.6).
    # Day-8 also keeps confidence as-passed when valid (no anti-hallucination
    # halving — that was over-aggressive and removed).
    out = na.analyze("Norway", "France", "Norway rest everyone",
                     router=_FakeRouter({"home_goal_delta": -2.0, "away_goal_delta": 1.5,
                                         "confidence": "high", "notes": ["x"]}))
    assert out["home_goal_delta"] == -0.6 and out["away_goal_delta"] == 0.6
    assert out["confidence"] == "high"


def test_normal_delta_passes_and_defaults_filled():
    out = na.analyze("A", "B", "nothing notable",
                     router=_FakeRouter({"home_goal_delta": -0.3, "away_goal_delta": 0.15}))
    assert out["home_goal_delta"] == -0.3 and out["away_goal_delta"] == 0.15
    assert out["confidence"] == "low" and out["notes"] == []   # defaults filled
    assert "discarded_sources" in out                            # Day-8 added field
