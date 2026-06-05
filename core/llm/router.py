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
        """Rate-limit + cost-track one provider call (best-effort; never blocks
        the pipeline if the obs layer is absent)."""
        try:
            from core.obs import ratelimit
            from core.obs.cost import ledger
            from core.obs.logging import correlation_id
            ratelimit.acquire(provider.name)
            out = fn(system, prompt, **kw)
            est_tokens = (len(system) + len(prompt) + len(str(out))) // 4
            ledger().record(provider.name, "complete", tokens=est_tokens,
                            correlation_id=correlation_id.get())
            return out
        except ImportError:
            return fn(system, prompt, **kw)

    def complete(self, system: str, prompt: str, **kw) -> str:
        last = None
        for p in self._ordered_available():
            try:
                return self._instrument(p, p.complete, system, prompt, **kw)
            except Exception as e:               # noqa: BLE001 - fall back on any error
                log.warning("provider '%s' failed: %s; falling back", p.name, e)
                last = e
        raise AllProvidersFailed(
            f"no usable LLM in chain {self.chain}; last error: {last}")

    def complete_json(self, system: str, prompt: str, **kw) -> dict:
        last = None
        for p in self._ordered_available():
            try:
                return self._instrument(p, p.complete_json, system, prompt, **kw)
            except Exception as e:               # noqa: BLE001
                log.warning("provider '%s' json failed: %s; falling back", p.name, e)
                last = e
        raise AllProvidersFailed(
            f"no usable LLM in chain {self.chain}; last error: {last}")
