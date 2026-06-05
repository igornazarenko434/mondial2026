"""Observability + cost/rate-limit configuration.

Everything here is data so the obs layer is fully configurable via env:
  OBS_ENABLED=1                 master switch
  OBS_LOG_JSON=1                structured JSON logs (vs human)
  OBS_LOG_LEVEL=INFO
  OTEL_TRACES_EXPORTER=console  console | otlp | none
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   (Jaeger/Tempo/Honeycomb)
  OBS_DB=store/obs.db           SQLite cost/quota ledger
"""
import os

ENABLED = os.environ.get("OBS_ENABLED", "1") == "1"
LOG_JSON = os.environ.get("OBS_LOG_JSON", "1") == "1"
LOG_LEVEL = os.environ.get("OBS_LOG_LEVEL", "INFO")
SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "mondial2026")
TRACES_EXPORTER = os.environ.get("OTEL_TRACES_EXPORTER", "console")  # console|otlp|none
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OBS_DB = os.environ.get("OBS_DB", os.path.join(os.path.dirname(__file__), "..", "store", "obs.db"))

# Free-tier rate limits + budgets per external provider.
#   rate/per  -> token-bucket smoothing (requests per `per` seconds)
#   budget/budget_period -> hard monthly/daily quota tracked by the cost ledger
PROVIDER_LIMITS = {
    # ---- API-keyed providers (limits = published free-tier ceilings) ----
    "football_data": {"rate": 10, "per": 60,   "budget": None,  "budget_period": None},   # 10 req/min, no daily cap on WC
    "odds_api":      {"rate": 1,  "per": 2,    "budget": 500,   "budget_period": "month"},# 500 credits/mo (credits = markets x regions)
    "api_football":  {"rate": 5,  "per": 60,   "budget": 100,   "budget_period": "day"},  # 100 req/day (actual rate 30/min — kept conservative)
    "gemini":        {"rate": 15, "per": 60,   "budget": 1500,  "budget_period": "day"},  # 2.5 Flash free tier: 15 RPM, 1500 RPD
    "claude":        {"rate": 50, "per": 60,   "budget": None,  "budget_period": None},   # Haiku 4.5 PAYG: ~50 RPM tier
    "openai":        {"rate": 60, "per": 60,   "budget": None,  "budget_period": None},   # PAYG; tier-dependent
    # ---- Scrapers (no published limit; self-imposed polite ceilings) ----
    # Both are 24h disk-cached in the data layer, so effective rate is ~1/day.
    "eloratings":    {"rate": 6,  "per": 60,   "budget": None,  "budget_period": None},   # eloratings.net/World.tsv
    "martj42":       {"rate": 6,  "per": 60,   "budget": None,  "budget_period": None},   # GitHub raw CSV
}

# Rough $/unit for cost ESTIMATES (free providers = 0). Tune to your plan.
# Anthropic Haiku 4.5 pay-as-you-go: ~$1/Mtok input, ~$5/Mtok output (mixed avg
# ~0.001/1k). Set this so the cost ledger reflects real spend, not zero.
PRICING = {
    "football_data": {"per_call": 0.0},
    "odds_api":      {"per_call": 0.0},   # free tier; "cost" tracked as credits
    "api_football":  {"per_call": 0.0},
    "gemini":        {"per_1k_tokens": 0.0},    # gemini-2.5-flash free tier
    "claude":        {"per_1k_tokens": 0.001},  # claude-haiku-4-5 PAYG (avg in/out)
    "openai":        {"per_1k_tokens": 0.0006}, # gpt-4o-mini-ish input price
    "eloratings":    {"per_call": 0.0},   # free scrape
    "martj42":       {"per_call": 0.0},   # free GitHub raw
}

# Warn when a provider's budget usage crosses this fraction.
QUOTA_WARN_FRACTION = float(os.environ.get("OBS_QUOTA_WARN", "0.8"))
