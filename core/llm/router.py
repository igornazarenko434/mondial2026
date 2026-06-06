"""LLMRouter — tries providers in the configured order, falls back on failure.

Usage:
    from core.llm.router import LLMRouter
    llm = LLMRouter()                       # chain from config/env
    text = llm.complete(system, prompt)
    data = llm.complete_json(system, prompt)

The chain is fully configurable (config.llm.provider_chain / LLM_PROVIDER_CHAIN),
so you pick which model leads and which back it up — e.g. Claude first (covered
by your subscription credit), Gemini free tier as fallback.
"""
from __future__ import annotations
import logging
from config.llm import provider_chain
from core.llm.base import LLMProvider
from core.llm.providers import REGISTRY

log = logging.getLogger("llm.router")


class AllProvidersFailed(Exception):
    pass


class LLMRouter:
    def __init__(self, chain: list[str] | None = None,
                 registry: dict[str, LLMProvider] | None = None):
        self.chain = chain or provider_chain()
        self.registry = registry or REGISTRY
        # The provider that produced the most-recent successful response.
        # Read it after .complete() / .complete_json() to know which model
        # actually answered (useful for audit-stamping cards: gemini vs claude
        # vs openai). Stays None until at least one call succeeds.
        self.last_provider: str | None = None
        # Providers we attempted before the successful one — chain visibility
        # for fallback audit (e.g. ["gemini"] when we fell back to claude).
        self.last_fallbacks: list[str] = []

    def _ordered_available(self) -> list[LLMProvider]:
        out = []
        for name in self.chain:
            p = self.registry.get(name)
            if p and p.available():
                out.append(p)
            elif p:
                log.info("LLM provider '%s' configured but not available (no key)", name)
        return out

    def _instrument(self, provider, fn, system, prompt, **kw):
        """Run one provider call inside obs.external_call so it (a) goes to
        Honeycomb / OTLP as a span, (b) records in the cost ledger with token
        count + duration, (c) acquires the shared rate-limit token. Degrades
        safely when the obs layer isn't installed (ImportError → direct call)."""
        try:
            from core import obs
        except ImportError:
            return fn(system, prompt, **kw)
        # Token estimate — refined post-call once we know the output length.
        # Note: each provider has its own real token count; this is a rough
        # 4-chars-per-token heuristic used for budget tracking only.
        out_holder: dict = {}
        with obs.external_call(provider.name, "complete", units=1, tokens=0):
            out_holder["text"] = fn(system, prompt, **kw)
        # Post-call: re-record an estimated token count via the cost ledger
        # so quota tracking reflects real usage (external_call records on
        # exit but defaults to 0 tokens since we didn't know yet).
        try:
            from core.obs.cost import ledger
            from core.obs.logging import correlation_id
            txt = out_holder["text"]
            est = (len(system) + len(prompt) + len(str(txt))) // 4
            ledger().record(provider.name, "complete:tokens", tokens=est,
                              correlation_id=correlation_id.get())
        except Exception:                                 # noqa: BLE001
            pass
        return out_holder["text"]

    def complete(self, system: str, prompt: str, **kw) -> str:
        last = None
        tried: list[str] = []
        for p in self._ordered_available():
            try:
                result = self._instrument(p, p.complete, system, prompt, **kw)
                # Success — stamp which model answered (for card audit trail).
                self.last_provider = p.name
                self.last_fallbacks = tried
                return result
            except Exception as e:               # noqa: BLE001 - fall back on any error
                tried.append(p.name)
                log.warning("provider '%s' failed: %s; falling back", p.name, e)
                last = e
        self.last_provider = None
        self.last_fallbacks = tried
        raise AllProvidersFailed(
            f"no usable LLM in chain {self.chain}; last error: {last}")

    def complete_json(self, system: str, prompt: str, **kw) -> dict:
        last = None
        tried: list[str] = []
        for p in self._ordered_available():
            try:
                result = self._instrument(p, p.complete_json, system, prompt, **kw)
                self.last_provider = p.name
                self.last_fallbacks = tried
                return result
            except Exception as e:               # noqa: BLE001
                tried.append(p.name)
                log.warning("provider '%s' json failed: %s; falling back", p.name, e)
                last = e
        self.last_provider = None
        self.last_fallbacks = tried
        raise AllProvidersFailed(
            f"no usable LLM in chain {self.chain}; last error: {last}")
