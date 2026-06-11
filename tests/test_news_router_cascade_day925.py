"""Day-9.25: pin the router-level cascade on parse-fail.

Live incident (2026-06-10): Gemini returned HTTP 200 with valid-looking JSON
that was TRUNCATED mid-string in `discarded_sources` because Gemini's verbose
output exceeded max_tokens=2048. Parser correctly rejected → tier='failed' →
NEUTRAL deltas. But Claude and OpenAI were both available with budget → the
chain never tried them. News contributed zero data for the opener card.

These tests pin three properties of the new `complete_validated` API:

  1. SEMANTIC cascade — if provider's body fails validation, fall through.
  2. TRANSPORT cascade — provider raises → still cascades (parity with old).
  3. All providers fail validation → AllProvidersFailed propagates;
     analyze_safe converts that to NEUTRAL with attribution.
"""
from __future__ import annotations
from unittest.mock import MagicMock
import pytest

from core.llm.router import LLMRouter, AllProvidersFailed


def _provider(name: str, response: str | Exception):
    """Build a fake provider that yields `response` (a str or raises Exception)."""
    p = MagicMock()
    p.name = name
    p.available.return_value = True
    if isinstance(response, Exception):
        p.complete.side_effect = response
        p.complete_json.side_effect = response
    else:
        p.complete.return_value = response
        p.complete_json.return_value = {"raw": response}
    return p


def _no_budget_check(monkeypatch):
    """Bypass the over_budget pre-flight so registry providers are all
    'available'. The cost ledger is process-singleton and we don't seed it."""
    import core.llm.router as r
    real_ordered = r.LLMRouter._ordered_available

    def _filtered(self):
        out = []
        for name in self.chain:
            p = self.registry.get(name)
            if p and p.available():
                out.append(p)
        self._last_skips = []
        return out
    monkeypatch.setattr(r.LLMRouter, "_ordered_available", _filtered)


def test_complete_validated_returns_first_successful_validation(monkeypatch):
    """Happy path: first provider's body passes validation → no cascade."""
    _no_budget_check(monkeypatch)
    p1 = _provider("gemini", '{"home_goal_delta": 0.1}')
    p2 = _provider("claude", '{"never": "reached"}')
    router = LLMRouter(chain=["gemini", "claude"],
                        registry={"gemini": p1, "claude": p2})

    def validator(raw):
        return ("home_goal_delta" in raw), raw

    out = router.complete_validated("sys", "prompt", validator)
    assert "home_goal_delta" in out
    assert router.last_provider == "gemini"
    p2.complete.assert_not_called()                  # claude never tried


def test_complete_validated_cascades_when_validation_fails(monkeypatch):
    """Mexico-incident scenario: gemini returns 200 with unparseable body →
    router cascades to claude → claude's body validates → return claude's."""
    _no_budget_check(monkeypatch)
    p_gem = _provider("gemini", '{"home_goal_delta": 0.0, "discarded_so')  # truncated
    p_cla = _provider("claude", '{"home_goal_delta": 0.15, "away_goal_delta": -0.10}')
    router = LLMRouter(chain=["gemini", "claude"],
                        registry={"gemini": p_gem, "claude": p_cla})

    def validator(raw):
        import json
        try:
            return True, json.loads(raw)
        except json.JSONDecodeError as e:
            return False, f"parse_failed: {e}"

    out = router.complete_validated("sys", "prompt", validator)
    assert "0.15" in out                              # claude's body, not gemini's
    assert router.last_provider == "claude"
    assert "gemini" in router.last_fallbacks
    err = router.last_fallback_errors.get("gemini")
    assert err is not None
    assert err["error_class"] == "ValidationFailed"
    assert "parse_failed" in err["error_message"]


def test_complete_validated_stashes_parsed_sentinel(monkeypatch):
    """Validator can return a parsed object as the sentinel; the router
    stashes it on `last_validated_sentinel` so news_agent doesn't double-parse."""
    _no_budget_check(monkeypatch)
    p = _provider("gemini", '{"home_goal_delta": 0.2}')
    router = LLMRouter(chain=["gemini"], registry={"gemini": p})

    def validator(raw):
        import json
        return True, json.loads(raw)

    router.complete_validated("sys", "prompt", validator)
    assert router.last_validated_sentinel == {"home_goal_delta": 0.2}


def test_complete_validated_raises_when_all_providers_fail_validation(monkeypatch):
    """All providers in chain return unparseable bodies → AllProvidersFailed."""
    _no_budget_check(monkeypatch)
    p_gem = _provider("gemini", "garbage{")
    p_cla = _provider("claude", "also bad{")
    router = LLMRouter(chain=["gemini", "claude"],
                        registry={"gemini": p_gem, "claude": p_cla})

    def validator(raw):
        return False, "always_fails"

    with pytest.raises(AllProvidersFailed):
        router.complete_validated("sys", "prompt", validator)
    assert router.last_provider is None
    assert "gemini" in router.last_fallbacks
    assert "claude" in router.last_fallbacks


def test_complete_validated_handles_transport_exception_then_validation_fail(monkeypatch):
    """Mixed failure modes: gemini raises (network), claude returns garbage
    (validation), openai returns valid body → all three failure-classes
    surfaced in last_fallback_errors."""
    _no_budget_check(monkeypatch)
    p_gem = _provider("gemini", ConnectionError("timeout"))
    p_cla = _provider("claude", "not json {")
    p_oai = _provider("openai", '{"home_goal_delta": 0.0}')
    router = LLMRouter(chain=["gemini", "claude", "openai"],
                        registry={"gemini": p_gem, "claude": p_cla, "openai": p_oai})

    def validator(raw):
        import json
        try:
            return True, json.loads(raw)
        except json.JSONDecodeError:
            return False, "parse_fail"

    out = router.complete_validated("sys", "prompt", validator)
    assert router.last_provider == "openai"
    assert "gemini" in router.last_fallback_errors
    assert router.last_fallback_errors["gemini"]["error_class"] == "ConnectionError"
    assert "claude" in router.last_fallback_errors
    assert router.last_fallback_errors["claude"]["error_class"] == "ValidationFailed"


# ────────── News-agent integration ──────────

def test_news_analyze_cascades_to_claude_when_gemini_truncates(monkeypatch):
    """Re-creates the Mexico v SA scenario at the news_agent layer: gemini
    returns truncated JSON; news_agent's _parse_json_lenient returns
    tier='failed'; the new validator returns ok=False; router cascades to
    claude which returns valid JSON; the analyze() result reflects claude's
    deltas with provider='claude' and gemini surfaced in fallback_errors."""
    _no_budget_check(monkeypatch)
    from orchestrator.agents.news_agent import analyze
    truncated = ('{"home_goal_delta": 0.0,\n "away_goal_delta": 0.0,\n '
                 '"confidence": "low",\n "notes": ["no usable pre-match news"],\n '
                 '"discarded_sources": ["2026-06-05 World Cup 2')  # mid-string cut
    claude_valid = '{"home_goal_delta": 0.05, "away_goal_delta": -0.05, "confidence": "medium", "notes": ["mexico key player back"]}'
    p_gem = _provider("gemini", truncated)
    p_cla = _provider("claude", claude_valid)
    router = LLMRouter(chain=["gemini", "claude"],
                        registry={"gemini": p_gem, "claude": p_cla})

    out = analyze("Mexico", "South Africa", "context", router=router)

    assert out["provider"] == "claude"
    assert out["parse_tier"] == "strict"
    assert abs(out["home_goal_delta"] - 0.05) < 1e-6
    assert abs(out["away_goal_delta"] - (-0.05)) < 1e-6
    assert "gemini" in (out.get("fallbacks_used") or [])
    fb_errs = out.get("fallback_errors") or {}
    assert "gemini" in fb_errs
    assert fb_errs["gemini"]["error_class"] == "ValidationFailed"
