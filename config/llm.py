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

DEFAULT_CHAIN = ["claude", "gemini", "openai"]

# Cheap/fast models are plenty for this job (parse news -> structured deltas,
# write the recommendation card). Override per provider via env *_MODEL.
MODELS = {
    "claude": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
    "gemini": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
    "openai": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
}


def provider_chain() -> list[str]:
    raw = os.environ.get("LLM_PROVIDER_CHAIN")
    return [p.strip() for p in raw.split(",")] if raw else list(DEFAULT_CHAIN)
