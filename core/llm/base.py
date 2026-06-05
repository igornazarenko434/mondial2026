"""Provider-agnostic LLM interface.

Every provider implements `complete()`. The news agent and card writer talk to
this interface only, so the underlying model (Claude / Gemini / OpenAI) is
swappable and never leaks into business logic.
"""
from __future__ import annotations
import abc
import json


class LLMUnavailable(Exception):
    """Raised when a provider can't run (missing key/lib) so the router falls back."""


class LLMProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def available(self) -> bool:
        """True if this provider has its key + library and can be called."""

    @abc.abstractmethod
    def complete(self, system: str, prompt: str,
                 json_mode: bool = False, max_tokens: int = 1024) -> str:
        """Return the model's text response (JSON string if json_mode)."""

    def complete_json(self, system: str, prompt: str, **kw) -> dict:
        """Convenience: parse a JSON response, tolerating ```json fences."""
        raw = self.complete(system, prompt, json_mode=True, **kw)
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(raw)
