"""Resilience of source-facing code: dynamic odds key, consensus, news
degradation, and delivery when every channel fails."""
import core.data.oddsapi as oddsapi
from core.data.oddsapi import resolve_wc_key, consensus_probs, devig
from orchestrator.agents.news_agent import analyze_safe, NEUTRAL
import core.delivery as delivery


# --- dynamic World Cup sport-key resolution (no network) ---
def test_resolve_wc_key_finds_world_cup(monkeypatch):
    monkeypatch.setattr(oddsapi, "list_sports", lambda: [
        {"key": "soccer_epl", "title": "EPL", "group": "Soccer"},
        {"key": "soccer_fifa_world_cup_2026", "title": "FIFA World Cup", "group": "Soccer"},
    ])
    assert resolve_wc_key() == "soccer_fifa_world_cup_2026"


def test_resolve_wc_key_skips_womens(monkeypatch):
    monkeypatch.setattr(oddsapi, "list_sports", lambda: [
        {"key": "soccer_fifa_world_cup_womens", "title": "FIFA Women's World Cup", "group": "Soccer"},
        {"key": "soccer_fifa_world_cup", "title": "FIFA World Cup", "group": "Soccer"},
    ])
    assert resolve_wc_key() == "soccer_fifa_world_cup"


def test_resolve_wc_key_falls_back_on_error(monkeypatch):
    def boom(): raise ConnectionError("down")
    monkeypatch.setattr(oddsapi, "list_sports", boom)
    assert resolve_wc_key() == "soccer_fifa_world_cup"   # default fallback


def test_consensus_probs_averages_books():
    p = consensus_probs([{"H": 2.0, "D": 3.5, "A": 4.0},
                         {"H": 2.1, "D": 3.4, "A": 3.9}])
    assert abs(sum(p.values()) - 1.0) < 1e-9


# --- news/LLM degradation: never blocks a pick ---
class _BoomRouter:
    def complete(self, *a, **k): raise RuntimeError("all providers down")
    def complete_json(self, *a, **k): raise RuntimeError("all providers down")


def test_news_analyze_safe_returns_neutral_on_failure():
    out = analyze_safe("Norway", "France", "Mbappé injured?", router=_BoomRouter())
    # Deltas/notes preserved as NEUTRAL (the pick stays unbiased on failure).
    assert out["home_goal_delta"] == 0.0 and out["away_goal_delta"] == 0.0
    for k in NEUTRAL:
        assert out[k] == NEUTRAL[k]
    # Plus the failure-mode + provider audit fields so render_card can show
    # ⚠news and Honeycomb cross-references the attempted model.
    assert "failure" in out and "all providers down" in out["failure"]
    assert "provider" in out
    assert "fallbacks_used" in out


# --- delivery: all channels fail -> returns False, never raises ---
def test_delivery_all_channels_fail(monkeypatch):
    class _BadChannel:
        name = "bad"
        def send(self, t, b): raise IOError("disk full / telegram down")
    monkeypatch.setattr(delivery, "_channels", lambda: [_BadChannel()])
    ok = delivery.deliver_card({"home": "A", "away": "B", "stage": "Group",
        "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
        "expected_points": 1.0, "model_prob": {"H": .5, "D": .3, "A": .2},
        "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}})
    assert ok is False        # no exception, signals undelivered so pipeline alerts
