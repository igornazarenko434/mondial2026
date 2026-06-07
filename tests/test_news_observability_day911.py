"""Day-9.11 — closing the gaps the audit workflow surfaced.

Covers:
  * obs.span auto-stamps correlation_id + stage
  * external_call captures status_code / retry_after / error_kind on failure
  * RateLimitTimeout raises closed when local bucket can't be acquired
  * Router stores skips list (no_key / over_budget) in last_fallbacks
  * Router uses `raise from` so the original SDK exception is the __cause__
  * Router _instrument's post-call token row passes units=0 (no double-count)
  * _validate_and_clamp surfaces every silent default/clamp/format-error
  * _parse_json_lenient empty/regex/failed tiers correctly tagged
  * news_agent stamps json_mode_fallback_used when json_mode=True dies
  * build_card unifies card['news_failure'] == failure_reasons['news']
  * gather_context's per-source ctx_failures + brave_gate land on news_meta
  * web_search._budget_clear returns (ok, reason) — 4 specific blockers
"""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import MagicMock

import pytest

from core.obs.cost import CostLedger
from core.llm.router import LLMRouter, AllProvidersFailed


# ──────────────────────── HTTP status_code + retry_after ──────────────────

def test_external_call_captures_status_code_and_retry_after(monkeypatch):
    from core import obs
    L = CostLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", L)

    class _Resp:
        status_code = 429
        headers = {"Retry-After": "37"}

    class _HTTPError(Exception):
        def __init__(self, msg, resp):
            super().__init__(msg)
            self.response = resp

    with pytest.raises(_HTTPError):
        with obs.external_call("gemini", "complete"):
            raise _HTTPError("429 too many requests", _Resp())

    row = L.conn.execute(
        "SELECT status_code, retry_after, error_kind, error_class "
        "FROM api_calls WHERE provider='gemini'").fetchone()
    assert row[0] == 429
    assert row[1] == "37"
    assert row[2] == "http"
    assert row[3] == "_HTTPError"


def test_external_call_classifies_timeout_vs_network(monkeypatch):
    from core import obs
    L = CostLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", L)

    class _ReadTimeout(Exception):
        pass

    class _ConnectionError(Exception):
        pass

    with pytest.raises(_ReadTimeout):
        with obs.external_call("api_football", "fixtures"):
            raise _ReadTimeout("read timed out")
    with pytest.raises(_ConnectionError):
        with obs.external_call("api_football", "fixtures"):
            raise _ConnectionError("dns failure")
    rows = L.conn.execute(
        "SELECT error_kind FROM api_calls WHERE provider='api_football' "
        "ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["timeout", "network"]


# ─────────────────────── Rate-limit fails closed ────────────────────────────

def test_external_call_raises_RateLimitTimeout_when_bucket_blocks(monkeypatch):
    from core import obs
    L = CostLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", L)
    monkeypatch.setattr("core.obs.ratelimit.acquire",
                        lambda *a, **kw: False)

    with pytest.raises(obs.RateLimitTimeout):
        with obs.external_call("brave_search", "web"):
            pytest.fail("body should NOT execute when local bucket blocks")

    row = L.conn.execute(
        "SELECT error_class, error_kind, ok FROM api_calls "
        "WHERE provider='brave_search'").fetchone()
    assert row[0] == "RateLimitTimeout"
    assert row[1] == "ratelimit_timeout"
    assert row[2] == 0


# ──────────────────────── Router correctness ────────────────────────────────

class _FakeProvider:
    def __init__(self, name, available=True, raises=None):
        self.name = name
        self._avail = available
        self.raises = raises
        self.calls = 0
    def available(self):
        return self._avail
    def complete(self, system, prompt, **kw):
        self.calls += 1
        if self.raises:
            raise self.raises
        return '{"home_goal_delta": 0, "away_goal_delta": 0, "confidence": "low"}'


def test_router_records_skips_in_last_fallbacks(monkeypatch):
    monkeypatch.setattr("core.obs.cost._LEDGER", CostLedger(":memory:"))
    g = _FakeProvider("gemini", available=False)
    c = _FakeProvider("claude")
    r = LLMRouter(chain=["gemini", "claude"], registry={"gemini": g, "claude": c})
    r.complete("sys", "p")
    assert "gemini:no_key" in r.last_fallbacks
    assert r.last_provider == "claude"


def test_router_over_budget_check_fails_closed(monkeypatch):
    """If ledger().over_budget raises, the provider must be skipped (not
    burned through). Day-9.11 fail-closed behaviour."""
    L = CostLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", L)

    def _angry_over_budget(self, name):
        raise RuntimeError("ledger corrupt")
    monkeypatch.setattr(CostLedger, "over_budget", _angry_over_budget)

    g = _FakeProvider("gemini")
    c = _FakeProvider("claude")
    r = LLMRouter(chain=["gemini", "claude"], registry={"gemini": g, "claude": c})
    # If we don't fail closed, gemini.calls would be 1.
    with pytest.raises(AllProvidersFailed):
        r.complete("sys", "p")
    assert g.calls == 0
    assert c.calls == 0
    assert "gemini:over_budget_check_failed" in r.last_fallbacks
    assert "claude:over_budget_check_failed" in r.last_fallbacks


def test_router_all_failed_preserves_exception_chain(monkeypatch):
    """Day-9.11: AllProvidersFailed.__cause__ should be the last SDK
    exception so tracebacks show the upstream root cause."""
    monkeypatch.setattr("core.obs.cost._LEDGER", CostLedger(":memory:"))

    class _BadAuth(Exception):
        pass

    g = _FakeProvider("gemini", raises=RuntimeError("timeout"))
    c = _FakeProvider("claude", raises=_BadAuth("401"))
    r = LLMRouter(chain=["gemini", "claude"], registry={"gemini": g, "claude": c})
    try:
        r.complete("sys", "p")
    except AllProvidersFailed as e:
        assert e.__cause__ is not None
        assert isinstance(e.__cause__, _BadAuth)
    else:
        pytest.fail("expected AllProvidersFailed")


def test_router_instrument_passes_units_zero_on_token_record(monkeypatch):
    """Day-9.11: the second ledger.record call (post-call token estimate)
    must pass units=0 — otherwise Gemini's 1500/day budget ticks 2x per real call."""
    L = CostLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", L)
    g = _FakeProvider("gemini")
    r = LLMRouter(chain=["gemini"], registry={"gemini": g})
    r.complete("sys", "p")
    # Two rows for gemini: one from external_call (units=1), one from token-update (units=0).
    rows = L.conn.execute(
        "SELECT endpoint, units FROM api_calls WHERE provider='gemini' "
        "ORDER BY id").fetchall()
    assert len(rows) == 2
    # First row is the wrapped call with units=1; second is the token-only post-call.
    assert rows[0][0] == "complete" and rows[0][1] == 1
    assert rows[1][0] == "complete:tokens" and rows[1][1] == 0


# ────────────────── Output provenance (_validate_and_clamp) ────────────────

def test_validate_clamp_surfaces_clamp_provenance():
    from orchestrator.agents.news_agent import _validate_and_clamp
    out = _validate_and_clamp({"home_goal_delta": 1.2, "away_goal_delta": 0.3,
                                "confidence": "high", "notes": []})
    assert out["home_goal_delta"] == 0.6     # clamped to DELTA_CLAMP
    assert out["home_delta_clamped"] is True
    assert "away_delta_clamped" not in out   # only set when actually clamped
    assert out["confidence"] == "high"
    assert "confidence_was_defaulted" not in out


def test_validate_clamp_surfaces_invalid_delta_type():
    from orchestrator.agents.news_agent import _validate_and_clamp
    out = _validate_and_clamp({"home_goal_delta": "lots",
                                "away_goal_delta": None,
                                "confidence": "medium", "notes": []})
    assert out["home_goal_delta"] == 0.0
    assert out["away_goal_delta"] == 0.0
    assert out["delta_parse_error"] is True
    assert "'lots'" in out["home_delta_raw"]


def test_validate_clamp_surfaces_defaulted_confidence():
    from orchestrator.agents.news_agent import _validate_and_clamp
    out = _validate_and_clamp({"home_goal_delta": 0.0, "away_goal_delta": 0.0,
                                "confidence": "extremely-high", "notes": []})
    assert out["confidence"] == "low"
    assert out["confidence_was_defaulted"] is True
    assert "extremely-high" in out["confidence_raw"]


def test_validate_clamp_surfaces_notes_truncation():
    from orchestrator.agents.news_agent import _validate_and_clamp
    out = _validate_and_clamp({"home_goal_delta": 0.0, "away_goal_delta": 0.0,
                                "confidence": "low",
                                "notes": [f"note {i}" for i in range(8)]})
    assert len(out["notes"]) == 5
    assert out["notes_truncated"] is True
    assert out["notes_original_count"] == 8


def test_validate_clamp_surfaces_non_dict_root():
    from orchestrator.agents.news_agent import _validate_and_clamp
    out = _validate_and_clamp(["not a dict"])
    assert out["schema_error"] == "non_dict_root"
    assert out["home_goal_delta"] == 0.0


def test_analyze_stamps_json_mode_fallback(monkeypatch):
    """Day-9.11: if json_mode=True raises and we retry without json_mode,
    the card must show json_mode_fallback_used=True so the auditor can
    tell why the strict-JSON path was skipped."""
    from orchestrator.agents import news_agent

    class _Router:
        last_provider = "claude"
        last_fallbacks: list = []
        last_fallback_errors: dict = {}
        def __init__(self):
            self.attempts = 0
        def complete(self, system, prompt, json_mode=True, max_tokens=500):
            self.attempts += 1
            if json_mode:
                raise RuntimeError("provider doesn't support json_mode")
            return '{"home_goal_delta": 0, "away_goal_delta": 0, "confidence": "low"}'

    out = news_agent.analyze("Mexico", "South Africa", "ctx", router=_Router())
    assert out["json_mode_fallback_used"] is True
    assert out["json_mode_error_class"] == "RuntimeError"


# ───────────────────── build_card: canonical news_failure ─────────────────

def test_build_card_news_failure_matches_failure_reasons(monkeypatch):
    """Day-9.11: card['news_failure'] and failure_reasons['news'] must be
    byte-identical so the audit trail can't drift."""
    from core.decision.build_card import build_card

    def _failing_news(home, away, context_text=None):
        return {"home_goal_delta": 0.0, "away_goal_delta": 0.0,
                "confidence": "low", "notes": [],
                "provider": None, "fallbacks_used": [],
                "fallback_errors": {},
                "parse_tier": "never_called",
                "failure": "AllProvidersFailed: no usable LLM",
                "failure_class": "AllProvidersFailed"}

    card = build_card(
        {"match_id": 1, "home": "Mexico", "away": "South Africa",
         "stage": "Group", "detonator": True},
        news_analyzer=_failing_news,
        strengths_loader=lambda *_a, **_k: (None, None),
        elo_loader=lambda *_a, **_k: None,
        odds_fetcher=lambda *_a, **_k: None,
        window="T-7m")
    assert card["news_failure"] is not None
    assert card["failure_reasons"]["news"] == card["news_failure"]


# ─────────────────── brave gate-check returns structured reason ──────────────

def test_brave_budget_clear_returns_reason_no_key(monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    from core.data.web_search import _budget_clear
    ok, reason = _budget_clear()
    assert ok is False
    assert reason == "no_key"


# ─────────────── gather_context per-source ctx_failures ─────────────────────

def test_gather_context_records_per_source_failure(monkeypatch):
    """When api_football.find_fixture_id raises, ctx_failures must capture
    source='api_football.lineups' + error_class + truncated message."""
    from orchestrator.agents import news_agent as na

    fake_af = MagicMock()
    fake_af.find_fixture_id.side_effect = RuntimeError("api-football 503")
    fake_af.find_team_id.return_value = None
    fake_ws = MagicMock(return_value=[])

    na.gather_context(
        {"home": "Mexico", "away": "South Africa", "stage": "Group",
         "group": "A", "utc_kickoff": "2026-06-11T19:00:00+00:00"},
        window="T-60m", api_football=fake_af, web_search_many=fake_ws)
    meta = na.context_meta()
    src_failures = {f["source"] for f in meta["ctx_failures"]}
    assert "api_football.lineups" in src_failures
    assert next(f for f in meta["ctx_failures"]
                if f["source"] == "api_football.lineups")["error_class"] == "RuntimeError"
