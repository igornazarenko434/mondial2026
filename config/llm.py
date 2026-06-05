"""LLM provider configuration — model-agnostic, configurable, with fallbacks.

Choose providers and order via env, e.g.:
    LLM_PROVIDER_CHAIN="claude,gemini,openai"
The router (core/llm/router.py) tries them in order and falls back on failure.

Auth (set whichever you use in .env):
  claude  -> CLAUDE_CODE_OAUTH_TOKEN  (covered by your Pro/Max subscription's
             Agent SDK credit)  OR  ANTHROPIC_API_KEY (pay-as-you-go)
  gemini  -> GEMINI_API_KEY           (free tier in Google AI Studio)
  openai  -> OPENAI_API_KEY           (pay-as-you-go; ChatGPT Plus does NOT count)
"""
import os

# Free first, cheap-paid second: Gemini Flash is the project's only "free tier"
# (1500 req/day; we need ~5/day), so put it ahead of paid Claude. Haiku is the
# cheap-paid safety net when Gemini rate-limits or errors. OpenAI mini is the
# last fallback. Override via env: LLM_PROVIDER_CHAIN="gemini,claude,openai".
DEFAULT_CHAIN = ["gemini", "claude", "openai"]

# Small/cheap class models are correct for this job. The only LLM task is the
# news/injury agent (orchestrator/agents/news_agent.py): match a fixed rubric
# and emit a 4-key JSON object. That's structured extraction, not reasoning —
# Haiku/Flash/mini tier is purpose-built for it, ~6x cheaper than the mid-tier
# (Sonnet) with comparable accuracy on this shape of task. Override per
# provider via env *_MODEL if needed.
MODELS = {
    "claude": os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
    "gemini": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
    "openai": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
}


def provider_chain() -> list[str]:
    raw = os.environ.get("LLM_PROVIDER_CHAIN")
    return [p.strip() for p in raw.split(",")] if raw else list(DEFAULT_CHAIN)
