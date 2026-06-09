"""Day-9.10 LLM observability — error classification, parse tiers,
over-budget short-circuit, raw-output capture."""
from __future__ import annotations

import sqlite3

import pytest

from core.obs.cost import CostLedger
from core.llm.router import LLMRouter, AllProvidersFailed


# ──────────────────────────── cost-ledger schema ────────────────────────────

def test_ledger_records_error_class_on_failure():
    L = CostLedger(":memory:")
    L.record("gemini", "complete", ok=False,
              error_class="RateLimitError",
              error_message="429 Quota exceeded for gemini-2.5-flash")
    row = L.conn.execute(
        "SELECT ok, error_class, error_message FROM api_calls "
        "WHERE provider='gemini'").fetchone()
    assert row[0] == 0
    assert row[1] == "RateLimitError"
    assert "Quota exceeded" in row[2]


def test_ledger_truncates_long_error_messages():
    L = CostLedger(":memory:")
    L.record("claude", "complete", ok=False,
              error_class="APIError",
              error_message="x" * 1000)
    row = L.conn.execute(
        "SELECT error_message FROM api_calls WHERE provider='claude'").fetchone()
    assert len(row[0]) == 200          # capped


def test_ledger_success_row_has_no_error_columns_set():
    L = CostLedger(":memory:")
    L.record("openai", "complete", ok=True, tokens=50)
    row = L.conn.execute(
        "SELECT ok, error_class, error_message FROM api_calls "
        "WHERE provider='openai'").fetchone()
    assert row[0] == 1
    assert row[1] is None
    assert row[2] is None


def test_ledger_migration_idempotent():
    """Re-opening a ledger over the same DB must not blow up — _migrate
    detects existing columns via PRAGMA."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    L1 = CostLedger(conn)
    L1.record("gemini", "complete", ok=True)
    L2 = CostLedger(conn)                  # second instance, same conn
    L2.record("gemini", "complete", ok=True)
    assert L2.usage("gemini")["calls"] == 2


# ──────────────────────── obs.external_call wiring ───────────────────────────

def test_external_call_stamps_error_class_on_exception(monkeypatch):
    from core import obs
    L = CostLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", L)
    with pytest.raises(ValueError):
        with obs.external_call("gemini", "complete"):
            raise ValueError("bad prompt")
    row = L.conn.execute(
        "SELECT ok, error_class, error_message FROM api_calls "
        "WHERE provider='gemini'").fetchone()
    assert row[0] == 0
    assert row[1] == "ValueError"
    assert "bad prompt" in row[2]


def test_external_call_success_path_no_error_class(monkeypatch):
    from core import obs
    L = CostLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", L)
    with obs.external_call("claude", "complete"):
        pass
    row = L.conn.execute(
        "SELECT ok, error_class FROM api_calls WHERE provider='claude'").fetchone()
    assert row[0] == 1 and row[1] is None


# ──────────────────────── over-budget short-circuit ─────────────────────────

class _FakeProvider:
    def __init__(self, name, available=True):
        self.name = name
        self._avail = available
        self.calls = 0
    def available(self):
        return self._avail
    def complete(self, system, prompt, **kw):
        self.calls += 1
        return '{"home_goal_delta": 0, "away_goal_delta": 0, "confidence": "low"}'


def test_router_skips_over_budget_provider(monkeypatch):
    """When the cost ledger says gemini is over budget, router should NOT
    call gemini — should fall straight through to claude."""
    L = CostLedger(":memory:")
    monkeypatch.setattr("core.obs.cost._LEDGER", L)
    # Manually stuff gemini's daily budget (1500 RPD) full
    for _ in range(1500):
        L.record("gemini", "complete", ok=True)
    assert L.over_budget("gemini")

    g = _FakeProvider("gemini")
    c = _FakeProvider("claude")
    r = LLMRouter(chain=["gemini", "claude"], registry={"gemini": g, "claude": c})
    r.complete("sys", "p")
    assert g.calls == 0                # bypassed
    assert c.calls == 1
    assert r.last_provider == "claude"


# ──────────────────────── per-provider error capture ────────────────────────

class _FailingProvider:
    def __init__(self, name, exc):
        self.name = name
        self.exc = exc
    def available(self):
        return True
    def complete(self, *_a, **_kw):
        raise self.exc


def test_router_records_fallback_errors_per_provider(monkeypatch):
    """When gemini → 429 and we fall through to claude → success, the router's
    last_fallback_errors must show gemini's error_class + message."""
    monkeypatch.setattr("core.obs.cost._LEDGER", CostLedger(":memory:"))

    class _RateLimitError(Exception):                  # mimic provider-SDK class
        pass

    g = _FailingProvider("gemini", _RateLimitError("429 Quota exceeded"))
    c = _FakeProvider("claude")
    r = LLMRouter(chain=["gemini", "claude"], registry={"gemini": g, "claude": c})
    r.complete("sys", "p")
    assert r.last_fallbacks == ["gemini"]
    assert "gemini" in r.last_fallback_errors
    assert r.last_fallback_errors["gemini"]["error_class"] == "_RateLimitError"
    assert "Quota exceeded" in r.last_fallback_errors["gemini"]["error_message"]


def test_router_records_all_failed_when_no_provider_works(monkeypatch):
    monkeypatch.setattr("core.obs.cost._LEDGER", CostLedger(":memory:"))
    g = _FailingProvider("gemini", RuntimeError("timeout"))
    c = _FailingProvider("claude", RuntimeError("503"))
    r = LLMRouter(chain=["gemini", "claude"], registry={"gemini": g, "claude": c})
    with pytest.raises(AllProvidersFailed):
        r.complete("sys", "p")
    assert r.last_provider is None
    assert set(r.last_fallback_errors) == {"gemini", "claude"}


# ─────────────────────────── parse-tier capture ────────────────────────────

def test_parse_tier_strict():
    from orchestrator.agents.news_agent import _parse_json_lenient
    data, tier = _parse_json_lenient('{"home_goal_delta": 0.1, "away_goal_delta": 0}')
    assert data == {"home_goal_delta": 0.1, "away_goal_delta": 0}
    assert tier == "strict"


def test_parse_tier_regex_repair():
    from orchestrator.agents.news_agent import _parse_json_lenient
    junk = ('Sure, here is the JSON you asked for: '
            '{"home_goal_delta": 0.2, "away_goal_delta": -0.1} '
            '— hope that helps!')
    data, tier = _parse_json_lenient(junk)
    assert data["home_goal_delta"] == 0.2
    assert tier == "regex_repair"


def test_parse_tier_failed():
    from orchestrator.agents.news_agent import _parse_json_lenient
    data, tier = _parse_json_lenient("yeah I dunno about this match really")
    assert data is None
    assert tier == "failed"


def test_parse_tier_empty():
    from orchestrator.agents.news_agent import _parse_json_lenient
    data, tier = _parse_json_lenient("")
    assert data is None
    assert tier == "empty"


def test_parse_tier_strict_handles_claude_leading_plus_sign():
    """Day-9.18: Claude (and others) emit `+0.15` for positive numbers as
    a clarity hint, but JSON spec FORBIDS leading + on numbers. Verified
    against Claude Haiku 4.5 in the cross-provider scenario harness.
    The parser must strip the + defensively."""
    from orchestrator.agents.news_agent import _parse_json_lenient
    bad = ('{"home_goal_delta": -0.25, "away_goal_delta": +0.20, '
           '"confidence": "medium", "notes": [], "discarded_sources": []}')
    data, tier = _parse_json_lenient(bad)
    assert data is not None, "+0.20 must be tolerated"
    assert tier == "strict"
    assert data["away_goal_delta"] == 0.20


def test_parse_tier_strict_handles_claude_markdown_fences():
    """Day-9.18: Claude wraps responses in ```json...``` despite the system
    prompt saying not to. The parser must strip fences anywhere in the text."""
    from orchestrator.agents.news_agent import _parse_json_lenient
    wrapped = ('```json\n'
               '{"home_goal_delta": 0.15, "away_goal_delta": 0.0, '
               '"confidence": "high", "notes": [], "discarded_sources": []}'
               '\n```')
    data, tier = _parse_json_lenient(wrapped)
    assert data is not None
    assert tier == "strict"
    assert data["home_goal_delta"] == 0.15


def test_parse_tier_strict_handles_both_quirks_together():
    """Combined real-world Claude case: markdown fences + leading +."""
    from orchestrator.agents.news_agent import _parse_json_lenient
    real_claude = ('```json\n'
                    '{\n  "home_goal_delta": -0.25,\n  "away_goal_delta": +0.20,\n'
                    '  "confidence": "medium",\n  "notes": ["X", "Y"],\n'
                    '  "discarded_sources": []\n}\n```')
    data, tier = _parse_json_lenient(real_claude)
    assert data is not None
    assert tier == "strict"
    assert data["home_goal_delta"] == -0.25
    assert data["away_goal_delta"] == 0.20


def test_parse_tier_doesnt_corrupt_legitimate_negative_numbers():
    """Defensive: stripping + must not touch -0.30 or - in mid-text."""
    from orchestrator.agents.news_agent import _parse_json_lenient
    good = ('{"home_goal_delta": -0.30, "away_goal_delta": 0.0, '
            '"confidence": "high", '
            '"notes": ["Mexico already qualified -0.30 squad rotation"], '
            '"discarded_sources": []}')
    data, tier = _parse_json_lenient(good)
    assert data is not None
    assert tier == "strict"
    assert data["home_goal_delta"] == -0.30   # negative preserved
    assert "Mexico already qualified -0.30" in data["notes"][0]


# ─── Day-9.19: web-search pipeline trim audit ─────────────────────────────────

def test_web_search_trim_strips_html_tags():
    """Day-9.19: Brave returns descriptions with HTML markup
    (<strong>Mexico</strong>); strip at source so the LLM doesn't see noise."""
    from core.data.web_search import _trim
    s = _trim("<strong>Mexico (co-host)</strong>, South Africa, "
              "<em>South Korea</em>, and Czechia.", 200)
    assert "<strong>" not in s
    assert "</strong>" not in s
    assert "<em>" not in s
    assert "Mexico (co-host), South Africa, South Korea, and Czechia." in s


def test_web_search_trim_decodes_html_entities():
    """Day-9.19: also decode common HTML entities Brave includes."""
    from core.data.web_search import _trim
    s = _trim("Mexico &amp; South Africa won&#39;t face &lt;3 goals", 200)
    assert "&amp;" not in s
    assert "&#39;" not in s
    assert "&lt;" not in s
    assert "Mexico & South Africa won't face <3 goals" in s


def test_web_search_trim_preserves_inner_text_with_nested_tags():
    from core.data.web_search import _trim
    s = _trim("<p>Mbappé <strong>OUT</strong> with <em>knee</em> injury</p>", 200)
    assert "<p>" not in s
    assert "<strong>" not in s
    assert "Mbappé OUT with knee injury" in s


def test_web_search_trim_caps_after_html_strip():
    """Cap applies to the CLEANED text, so we don't waste budget on tags."""
    from core.data.web_search import _trim
    s = _trim("<strong>" + "X" * 300 + "</strong>", 50)
    assert len(s) == 50
    assert "<strong>" not in s


def test_fmt_web_results_respects_explicit_cap():
    from orchestrator.agents.news_agent import _fmt_web_results
    results = [{"title": f"Article {i}", "snippet": "x", "date": "2026-06-09"}
               for i in range(20)]
    out = _fmt_web_results(results, snippet_len=250, cap=5)
    assert out.count("- [") == 5
    out = _fmt_web_results(results, snippet_len=250, cap=15)
    assert out.count("- [") == 15


# ─────────────────────── news_agent stamping ──────────────────────

def test_analyze_stamps_parse_tier_and_excerpt_on_unparseable_output(monkeypatch):
    from orchestrator.agents import news_agent

    class _BadRouter:
        last_provider = "gemini"
        last_fallbacks: list = []
        last_fallback_errors: dict = {}
        def complete(self, system, prompt, json_mode=True, max_tokens=500):
            return "this is not json at all sorry"

    out = news_agent.analyze("Mexico", "South Africa", "context", router=_BadRouter())
    assert out["home_goal_delta"] == 0.0
    assert out["away_goal_delta"] == 0.0
    assert out["parse_tier"] == "failed"
    assert "raw_excerpt" in out
    assert "this is not json" in out["raw_excerpt"]
    assert out["provider"] == "gemini"


def test_analyze_strict_path_no_excerpt(monkeypatch):
    """When the LLM returned valid JSON, raw_excerpt should NOT be set
    (no privacy leak / no clutter)."""
    from orchestrator.agents import news_agent

    class _GoodRouter:
        last_provider = "claude"
        last_fallbacks: list = []
        last_fallback_errors: dict = {}
        def complete(self, system, prompt, json_mode=True, max_tokens=500):
            return '{"home_goal_delta": 0.15, "away_goal_delta": -0.10, "confidence": "high", "notes": ["XI confirmed"], "discarded_sources": []}'

    out = news_agent.analyze("Mexico", "South Africa", "context", router=_GoodRouter())
    assert out["parse_tier"] == "strict"
    assert "raw_excerpt" not in out          # only stamped on failure
    assert out["provider"] == "claude"


def test_analyze_safe_stamps_failure_class_on_total_failure(monkeypatch):
    from orchestrator.agents import news_agent

    class _DyingRouter:
        last_provider = None
        last_fallbacks = ["gemini", "claude"]
        last_fallback_errors = {
            "gemini": {"error_class": "RateLimitError", "error_message": "429"},
            "claude": {"error_class": "APITimeoutError", "error_message": "no response"},
        }
        def complete(self, *_a, **_kw):
            raise AllProvidersFailed("all dead")

    out = news_agent.analyze_safe("Mexico", "South Africa", "ctx", router=_DyingRouter())
    assert out["home_goal_delta"] == 0.0     # NEUTRAL preserved
    assert out["failure_class"] == "AllProvidersFailed"
    assert out["fallback_errors"]["gemini"]["error_class"] == "RateLimitError"
    assert out["fallback_errors"]["claude"]["error_class"] == "APITimeoutError"
    assert out["parse_tier"] == "never_called"
