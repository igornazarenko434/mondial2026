"""Preflight config check — run at startup so misconfiguration surfaces loudly
*before* the first match, not silently at T-7m.

Reports which features are enabled given the current env, and which are degraded
because a key/credential is missing. Never raises — it informs.

Day-9.23: ALSO checks the running env vars for the "inline-comment trap" —
values that contain ` # ...` because systemd's EnvironmentFile parser doesn't
strip inline comments. This bit us on 2026-06-10: NEGEV_EMAIL=<your-email>
with an inline comment made Firebase reject as INVALID_EMAIL.
"""
from __future__ import annotations
import os
import re
from core.obs.logging import get_logger

log = get_logger("preflight")


# Vars where an inline-comment leak would silently break the daemon.
# Auth-affecting + delivery-affecting only — generic optional keys can leak
# without functional impact and we don't want to be too chatty.
INLINE_HAZARD_KEYS = (
    "NEGEV_EMAIL", "NEGEV_PASSWORD", "NEGEV_REFRESH_TOKEN",
    "NEGEV_TOURNAMENT_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "MY_PARTICIPANT", "FRIEND_PARTICIPANTS",
    "FOOTBALL_DATA_API_KEY", "ODDS_API_KEY", "API_FOOTBALL_KEY",
    "BRAVE_SEARCH_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
)
# `<whitespace>#<anything>` at end of value = systemd has leaked an inline comment
_INLINE_COMMENT_RE = re.compile(r"\s+#")


def _detect_inline_comment_leaks() -> list[tuple[str, str]]:
    """Scan os.environ for vars whose value contains an inline-comment leak.
    Returns [(key, snippet), ...] for any detected hazard.

    Detection: whitespace + '#' anywhere AFTER a non-space character.
    A leading '#' at column 0 would be a comment line (never reaches env).
    """
    out = []
    for key in INLINE_HAZARD_KEYS:
        val = os.environ.get(key)
        if not val:
            continue
        m = _INLINE_COMMENT_RE.search(val)
        if m:
            snippet = val[max(0, m.start() - 8):m.end() + 12]
            out.append((key, snippet))
    return out


def _check_tracing() -> bool:
    """Day-9.25 OTel exporter self-test. Returns True iff:
      - TRACES_EXPORTER is "none" → tracing intentionally off; return True
      - TRACES_EXPORTER is "console" → no remote endpoint needed; return True
      - TRACES_EXPORTER is "otlp" → endpoint+headers configured, the OTel SDK
        importable, and a no-op span open/close cycle survives end-to-end

    A False return shows up in the preflight log as a degraded feature so
    the operator notices BEFORE missing traces in Honeycomb. We never raise
    — preflight is informational, not a hard gate (the daemon still runs)."""
    try:
        from config import observability as cfg
    except Exception as e:                              # noqa: BLE001
        log.warning("preflight tracing — observability config import failed: %s", e)
        return False
    if not cfg.ENABLED or cfg.TRACES_EXPORTER in (None, "", "none"):
        log.info("preflight tracing — disabled (TRACES_EXPORTER=%r)",
                  cfg.TRACES_EXPORTER)
        return True                                       # intentional off
    if cfg.TRACES_EXPORTER == "console":
        log.info("preflight tracing — console exporter (local stderr only)")
        return True
    if cfg.TRACES_EXPORTER == "otlp":
        if not cfg.OTLP_ENDPOINT:
            log.error("preflight tracing — OTEL_TRACES_EXPORTER=otlp but "
                      "OTEL_EXPORTER_OTLP_ENDPOINT empty; traces will NO-OP")
            return False
        # Honeycomb requires an auth header. Don't fail on absent (could be a
        # local Jaeger), just note it.
        if "honeycomb" in cfg.OTLP_ENDPOINT.lower() and \
                not os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"):
            log.error("preflight tracing — Honeycomb endpoint configured but "
                      "OTEL_EXPORTER_OTLP_HEADERS empty (need x-honeycomb-team "
                      "API key); spans will be rejected")
            return False
        # Live-fire a no-op span. Failure = the SDK isn't installed or the
        # exporter config is malformed at construction time.
        try:
            from core.obs.tracing import span as _span
            with _span("preflight.healthcheck", source="preflight"):
                pass
            log.info("preflight tracing — OTLP exporter to %s OK",
                      cfg.OTLP_ENDPOINT)
            return True
        except Exception as e:                          # noqa: BLE001
            log.error("preflight tracing — span open/close failed (%s: %s); "
                      "traces will be lost", type(e).__name__, e)
            return False
    log.warning("preflight tracing — unknown TRACES_EXPORTER=%r; treating as off",
                cfg.TRACES_EXPORTER)
    return False


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
    # Day-9.23 — inline-comment hygiene check. Loud at startup so the operator
    # fixes the .env BEFORE the daemon spends 15 hours producing degraded cards.
    leaks = _detect_inline_comment_leaks()
    if leaks:
        log.error("preflight — INLINE COMMENT LEAK in .env (systemd doesn't "
                  "strip inline #). Fix by moving the comment to its own line "
                  "ABOVE the var. Affected:")
        for key, snippet in leaks:
            log.error("  %s contains an inline-comment leak near '%s'",
                      key, snippet.strip())
    status["env_hygiene_ok"] = not leaks

    # Day-9.24 — STRATEGY_OVERRIDES validation. If set, MUST parse as JSON
    # dict {name: float}; otherwise the per-person section silently no-ops
    # and the operator wouldn't know why. Loud ERROR at startup is honest.
    raw = (os.environ.get("STRATEGY_OVERRIDES") or "").strip()
    overrides_ok = True
    if raw:
        try:
            import json as _json
            d = _json.loads(raw)
            if not isinstance(d, dict):
                log.error("STRATEGY_OVERRIDES parses to %s, expected dict; "
                          "per-person section will be ignored", type(d).__name__)
                overrides_ok = False
            else:
                for k, v in d.items():
                    if not isinstance(v, (int, float)):
                        log.error("STRATEGY_OVERRIDES['%s']=%r is not numeric; "
                                  "per-person section will be ignored", k, v)
                        overrides_ok = False
                        break
                if overrides_ok:
                    log.info("preflight — STRATEGY_OVERRIDES active: %s", d)
        except Exception as e:                          # noqa: BLE001
            log.error("STRATEGY_OVERRIDES not valid JSON: %s — per-person "
                      "section will be ignored", e)
            overrides_ok = False
    status["strategy_overrides_ok"] = overrides_ok

    # Day-9.25 — OTel exporter self-test. If TRACES_EXPORTER=otlp but the
    # endpoint or auth header is missing/garbled, spans silently no-op and
    # Honeycomb shows nothing. The right time to find out is at startup, not
    # 3 days into the tournament when debugging a card failure.
    status["tracing"] = _check_tracing()

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
