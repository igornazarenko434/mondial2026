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
