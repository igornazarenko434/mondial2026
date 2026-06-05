"""Preflight config check — run at startup so misconfiguration surfaces loudly
*before* the first match, not silently at T-7m.

Reports which features are enabled given the current env, and which are degraded
because a key/credential is missing. Never raises — it informs.
"""
from __future__ import annotations
import os
from core.obs.logging import get_logger

log = get_logger("preflight")


def check() -> dict:
    status = {
        "fixtures (football-data)": bool(os.environ.get("FOOTBALL_DATA_API_KEY")),
        "odds (the-odds-api)": bool(os.environ.get("ODDS_API_KEY")),
        "lineups/injuries (api-football)": bool(os.environ.get("API_FOOTBALL_KEY")),
        "llm: claude": bool(os.environ.get("ANTHROPIC_API_KEY")
                            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
        "llm: gemini": bool(os.environ.get("GEMINI_API_KEY")),
        "llm: openai": bool(os.environ.get("OPENAI_API_KEY")),
        "delivery: telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN")
                                   and os.environ.get("TELEGRAM_CHAT_ID")),
    }
    enabled = [k for k, v in status.items() if v]
    missing = [k for k, v in status.items() if not v]
    log.info("preflight — enabled: %s", ", ".join(enabled) or "none")
    if missing:
        log.warning("preflight — degraded/disabled (missing creds): %s", ", ".join(missing))
    # Hard requirements for the system to do anything useful:
    if not status["fixtures (football-data)"]:
        log.error("FOOTBALL_DATA_API_KEY missing — no fixtures, system cannot run")
    if not any(status[k] for k in ("llm: claude", "llm: gemini", "llm: openai")):
        log.warning("no LLM configured — news agent disabled; picks still work (model-only)")
    if not status["odds (the-odds-api)"]:
        log.warning("no odds key — picks will be model-only (no odds multiplier from market)")
    return status


if __name__ == "__main__":
    check()
