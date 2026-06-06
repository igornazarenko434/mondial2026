"""Router fallback logic, tested with fake providers (no network)."""
import pytest
from core.llm.base import LLMProvider
from core.llm.router import LLMRouter, AllProvidersFailed


class Fake(LLMProvider):
    def __init__(self, name, ok=True, avail=True):
        self.name, self._ok, self._avail = name, ok, avail
    def available(self): return self._avail
    def complete(self, system, prompt, json_mode=False, max_tokens=1024):
        if not self._ok:
            raise RuntimeError(f"{self.name} boom")
        return f"{self.name}:{prompt}"


def make(reg_specs):
    reg = {n: Fake(n, ok, av) for (n, ok, av) in reg_specs}
    chain = [n for (n, _, _) in reg_specs]
    return LLMRouter(chain=chain, registry=reg)


def test_uses_first_available():
    r = make([("claude", True, True), ("gemini", True, True)])
    assert r.complete("s", "hi") == "claude:hi"


def test_skips_unavailable_then_succeeds():
    r = make([("claude", True, False), ("gemini", True, True)])
    assert r.complete("s", "hi") == "gemini:hi"


def test_falls_back_on_error():
    r = make([("claude", False, True), ("gemini", True, True)])
    assert r.complete("s", "hi") == "gemini:hi"


def test_raises_when_all_fail():
    r = make([("claude", False, True), ("gemini", False, True)])
    with pytest.raises(AllProvidersFailed):
        r.complete("s", "hi")


# ─────────────── Day-8 audit-trail: which model answered? ───────────────

def test_last_provider_stamped_after_successful_call():
    """After complete() succeeds, last_provider names the model that answered
    and last_fallbacks lists any providers attempted before it. This is what
    feeds the 'Signals: …+News(gemini)' annotation on the card."""
    r = make([("claude", True, True), ("gemini", True, True)])
    r.complete("s", "hi")
    assert r.last_provider == "claude"
    assert r.last_fallbacks == []


def test_last_provider_records_fallbacks_after_chain_walk():
    r = make([("claude", False, True), ("gemini", True, True)])
    r.complete("s", "hi")
    assert r.last_provider == "gemini"
    assert r.last_fallbacks == ["claude"]


def test_last_provider_cleared_when_all_fail():
    r = make([("claude", False, True), ("gemini", False, True)])
    with pytest.raises(AllProvidersFailed):
        r.complete("s", "hi")
    assert r.last_provider is None
    assert r.last_fallbacks == ["claude", "gemini"]


def test_llm_calls_wrapped_in_obs_external_call(monkeypatch):
    """Every router call MUST go through obs.external_call so Honeycomb
    receives the span + the cost-ledger gets the rate-limit token. If this
    wrap regresses, LLM calls vanish from traces."""
    from core import obs
    calls: list[tuple[str, str]] = []
    real_ec = obs.external_call

    def spy_ec(provider, endpoint, *args, **kw):
        calls.append((provider, endpoint))
        return real_ec(provider, endpoint, *args, **kw)
    monkeypatch.setattr(obs, "external_call", spy_ec)

    r = make([("claude", True, True), ("gemini", True, True)])
    r.complete("s", "hi")
    assert ("claude", "complete") in calls
