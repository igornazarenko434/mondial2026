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
        # Day-9.10: per-provider failure detail. Maps each tried provider name
        # → {"error_class": "RateLimitError", "error_message": "...truncated"}
        # so the news_agent can stamp the *specific* upstream reason on the
        # card, not just "fallback happened".
        self.last_fallback_errors: dict[str, dict] = {}
        # Day-9.11: skip-list audit — names suffixed with :no_key /
        # :over_budget / :over_budget_check_failed that were bypassed
        # BEFORE the chain even tried them. Merged into last_fallbacks
        # by complete()/complete_json() so the card's news_fallbacks_used
        # field shows both bypassed-up-front and tried-and-failed.
        self._last_skips: list[str] = []

    def _ordered_available(self) -> list[LLMProvider]:
        """Providers in chain order, filtered by (a) key configured and
        (b) cost-ledger budget NOT exhausted. Records each skip reason on
        `last_skips` (suffixed `:no_key` or `:over_budget`) so the
        audit trail explains WHY a provider was bypassed.

        Day-9.11: skip-list audit AND fail-closed over_budget check (if the
        ledger raises, conservatively skip the provider rather than burning
        a possibly-over-budget call)."""
        out = []
        skips: list[str] = []
        for name in self.chain:
            p = self.registry.get(name)
            if not p:
                continue
            if not p.available():
                log.info("LLM provider '%s' configured but not available (no key)", name)
                skips.append(f"{name}:no_key")
                continue
            # Day-9.10/9.11: pre-flight over-budget check, fail CLOSED on error.
            try:
                from core.obs.cost import ledger
                if ledger().over_budget(name):
                    log.warning("LLM provider '%s' over budget; skipping", name)
                    skips.append(f"{name}:over_budget")
                    continue
            except Exception as e:                         # noqa: BLE001
                log.warning("over_budget check failed for %s (%s); "
                            "skipping provider conservatively", name, e)
                skips.append(f"{name}:over_budget_check_failed")
                continue
            out.append(p)
        # Stash on the instance so complete()/complete_json() can merge into
        # last_fallbacks. Tests use this to assert skip attribution.
        self._last_skips = skips
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
        # Day-9.11: pass units=0 explicitly — the first ledger.record call
        # (inside external_call) already counted units=1, so without this
        # the Gemini daily-1500 budget would tick TWICE per real call.
        try:
            from core.obs.cost import ledger
            from core.obs.logging import correlation_id
            txt = out_holder["text"]
            est = (len(system) + len(prompt) + len(str(txt))) // 4
            ledger().record(provider.name, "complete:tokens", units=0,
                              tokens=est,
                              correlation_id=correlation_id.get())
        except Exception as e:                              # noqa: BLE001
            log.warning("ledger token-update failed for %s: %s", provider.name, e)
        return out_holder["text"]

    def complete(self, system: str, prompt: str, **kw) -> str:
        last = None
        tried: list[str] = []
        errors: dict[str, dict] = {}
        available = self._ordered_available()
        for p in available:
            try:
                result = self._instrument(p, p.complete, system, prompt, **kw)
                # Success — stamp which model answered (for card audit trail).
                self.last_provider = p.name
                # Day-9.11: include skipped-up-front providers in the audit
                # trail so a card showing news_fallbacks_used=['gemini:no_key',
                # 'claude'] explains both bypass reasons end-to-end.
                self.last_fallbacks = list(self._last_skips) + tried
                self.last_fallback_errors = errors
                return result
            except Exception as e:               # noqa: BLE001 - fall back on any error
                tried.append(p.name)
                errors[p.name] = {"error_class": type(e).__name__,
                                   "error_message": str(e)[:200]}
                log.warning("provider '%s' failed (%s): %s; falling back",
                            p.name, type(e).__name__, e)
                last = e
        self.last_provider = None
        self.last_fallbacks = list(self._last_skips) + tried
        self.last_fallback_errors = errors
        # Day-9.11: preserve the SDK exception chain via `from last` so
        # tracebacks show the upstream provider error, not just the wrapper.
        raise AllProvidersFailed(
            f"no usable LLM in chain {self.chain}; last error: {last}") from last

    def complete_json(self, system: str, prompt: str, **kw) -> dict:
        last = None
        tried: list[str] = []
        errors: dict[str, dict] = {}
        available = self._ordered_available()
        for p in available:
            try:
                result = self._instrument(p, p.complete_json, system, prompt, **kw)
                self.last_provider = p.name
                self.last_fallbacks = list(self._last_skips) + tried
                self.last_fallback_errors = errors
                return result
            except Exception as e:               # noqa: BLE001
                tried.append(p.name)
                errors[p.name] = {"error_class": type(e).__name__,
                                   "error_message": str(e)[:200]}
                log.warning("provider '%s' json failed (%s): %s; falling back",
                            p.name, type(e).__name__, e)
                last = e
        self.last_provider = None
        self.last_fallbacks = list(self._last_skips) + tried
        self.last_fallback_errors = errors
        raise AllProvidersFailed(
            f"no usable LLM in chain {self.chain}; last error: {last}") from last
