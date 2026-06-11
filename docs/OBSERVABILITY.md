# Observability — tracing, logging, metrics, cost/quota

Modern, vendor-neutral stack: **OpenTelemetry** for traces (live on Honeycomb in
production), **structured JSON logs** with correlation IDs, and a **SQLite
cost/quota ledger** that's always-on and free. Everything is modular and degrades
to safe no-ops if a dependency or backend is absent — the pipeline runs
identically without it.

## The five pillars

| Pillar | Module | What you get |
|---|---|---|
| **Tracing** | `core/obs/tracing.py` | OTel spans per agent/stage; one trace per match-window job; exporter = console / OTLP (Jaeger·Tempo·Honeycomb) / none. Auto-stamps `correlation_id` + `stage` on EVERY span (Day-9.11). |
| **Logging** | `core/obs/logging.py` | JSON logs with `correlation_id` + `trace_id` on every line, tying logs to the exact job. |
| **Metrics** | `core/obs/metrics.py` | counters (api_calls, llm_tokens, errors) + latency histograms |
| **Cost/quota** | `core/obs/cost.py` | every external call persisted; per-period usage; 80% budget warnings; `error_class` + `error_message` columns on failure (Day-9.10) |
| **Rate limit** | `core/obs/ratelimit.py` | shared token-bucket per provider (parallel-safe). `n=1` always; credit accounting separate via `units=` (Day-9.13 fix decoupled rate-limit from credit cost) |

## One-line instrumentation for any outbound call

```python
from core import obs
obs.setup()                                   # once at startup

with obs.run("match-537327-T-7m"):            # correlation id for the whole job
    with obs.external_call("odds_api", "h2h", units=2):
        ... # rate-limited, traced, timed, and cost-recorded automatically
```

The five wrapped clients (`oddsapi.py`, `football_data.py`, `api_football.py`,
`web_search.py`, `negev_toto_mcp.py`) and the LLM router are all instrumented
this way. The Day-9.25 fix moved Negev's instrumentation to the SOURCE (the `_fs`
helper inside `integrations/negev_toto_mcp.py`), so any new caller of
`toto_*` tools is automatically rate-limited + ledger-recorded — no risk of
silent bypass.

New external calls: just wrap them in `obs.external_call(...)`. The audit
golden rule (CLAUDE.md §3): "Every `requests.get/post` must be inside
`obs.external_call(...)`".

## Live tracing chain → Honeycomb (production state)

```
.env on VM:
  OTEL_SERVICE_NAME=mondial2026
  OTEL_TRACES_EXPORTER=otlp
  OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
  OTEL_EXPORTER_OTLP_HEADERS=x-honeycomb-team=<API_KEY>

Daemon startup self-test (Day-9.25):
  preflight tracing — OTLP exporter to https://api.honeycomb.io OK
```

**Preflight self-test** (`config/preflight.py::_check_tracing`) verifies the SDK
is importable, endpoint+headers are present, and a no-op span open/close cycle
completes. Loud ERROR on any failure; informational only — daemon still runs.
Test pinned by `test_preflight_tracing_day925.py`.

In Honeycomb, a single query returns the full span tree for one card:
```
WHERE correlation_id = "match-537327-T-7m"

run (parent)
├── stage:news
│   ├── gather_context.api_football.lineups
│   ├── gather_context.api_football.injuries.home
│   ├── gather_context.api_football.injuries.away
│   ├── gather_context.brave_search
│   │   └── brave_search.web × 3
│   └── news_agent.parse_validate × N (one per provider tried by cascade)
├── stage:odds
│   └── odds_api.odds
├── stage:negev
│   ├── firestore:get_document
│   └── firestore:read_all_paged
└── telegram_bot.sendMessage
```

## Configuration (all env, see `config/observability.py`)

```
OBS_ENABLED=1                      OBS_LOG_JSON=1            OBS_LOG_LEVEL=INFO
OTEL_TRACES_EXPORTER=console|otlp|none
OTEL_EXPORTER_OTLP_ENDPOINT=...    OTEL_EXPORTER_OTLP_HEADERS=...
OBS_DB=store/obs.db                OBS_QUOTA_WARN=0.8
```

Provider rate limits, budgets, and price estimates live in
`config/observability.py` (`PROVIDER_LIMITS`, `PRICING`) — the single place to
tune them.

## Where you actually SEE the metrics (no Grafana needed)

The SQLite ledgers (`api_calls`, `runs`) **are** the metric store — every call and
run is persisted with a `correlation_id` (e.g. `match-537327-T-7m`), latency,
tokens, cost, AND (Day-9.10) `error_class` + `error_message`. So you can see
metrics per game / provider / window with zero extra infrastructure.

### `api_calls` schema (Day-9.10)

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
ts TEXT, provider TEXT, endpoint TEXT,
units REAL DEFAULT 1, tokens INTEGER DEFAULT 0,
duration_ms REAL DEFAULT 0,
est_cost REAL DEFAULT 0, ok INTEGER DEFAULT 1,
correlation_id TEXT,
error_class TEXT, error_message TEXT     -- Day-9.10
status_code INTEGER, retry_after TEXT,    -- Day-9.11
error_kind TEXT                            -- Day-9.11
```

### Runbook tools

| Tool | What | Cost |
|---|---|---|
| `tools/audit_fired_card.py <match_id> <window>` | Full post-fire audit: runs ledger, predictions payload, rendered card body, api_calls trail, signal audit (Day-9.25 section 4b: scoring_table + exact_multiplier), news audit, Honeycomb hint, journalctl hint, anomaly flags | 0 |
| `tools/news_inspect.py <home> <away> --window X` | Forensic LLM deep-dive: queries → ranked context → system prompt → provider chain → notes/discarded reasoning | 1 LLM + 3 Brave |
| `tools/pick_analyzer.py <home> <away> --xg-home ... ` | EV vs MODAL vs SAFEST vs LONGSHOT trade-off table per match | 0 |
| `tools/llm_audit.py --hours 24` | 5-section LLM runbook: chain state, per-provider ledger, quota, per-card parse_tier+raw_excerpt, recent failures with correlation_id | 0 |
| `tools/obs_audit.py` | End-to-end probe of every provider (rate-limit + ledger + span) | ~5 across all providers |
| `tools/metrics.py` | CLI metrics view (overall or per-correlation_id) | 0 |
| `tools/dashboard.py` | Static HTML dashboard at `reports/dashboard.html` | 0 |
| `tools/verify_negev_live.py` | 14 live MCP checks against Negev's Firestore | ~14 free Negev calls |
| `tools/audit_negev_multipliers.py` | Diff Negev's grids vs our `config/rules.py` cell-by-cell | 1 free Negev call |
| `tools/audit_env.py` | .env inline-comment trap scanner + optional Negev auth probe | 0 (with `--skip-auth`) |
| `tools/verify_scoring_sync.py` | End-to-end Negev↔us scoring loop audit | 3-4 free Negev calls |
| `tools/show_schedule.py` | Live schedule state inspector (next windows, fire times, countdowns) | 0 |
| `tools/show_my_rank.py`, `show_pool_picks.py` | Quick standings + friends' picks views | 1 free Negev call |

### Day-9.25 enhancements

**Per-card scoring-table stamp** — `build_card` writes `scoring_table` +
`exact_multiplier_used` on every card. `audit_fired_card.py` section 4b
cross-checks the stamped value vs `STAGE_TYPE[stage]` and the engine's
recomputation. If a future football-data stage code doesn't map cleanly via
`RULES_STAGE`, the audit immediately shows `⚠ DRIFT`.

**News ranking diagnostics** — `gather_context` stamps `context_meta` with:
- `brave_top3_titles` — first 60 chars of the 3 highest-scoring articles
- `brave_lowest_included_score` — the cutoff line
- `brave_n_raw` — pre-dedup count
- `brave_n_after_dedup` — post-title-dedup count
- `brave_n_dropped_low_score` — how many low-score articles got cut

**LLM router cascade attribution** — `complete_validated` records per-provider
`error_class` (transport: `ConnectionError`/`RateLimitError`/`AuthError`;
semantic: `ValidationFailed`) + `error_message` in `last_fallback_errors`. The
card's `news_fallbacks_used` and `news_fallback_errors` fields show the chain
visit + reasons for every bypassed provider.

**Smoke audits in `update.sh` (Day-9.25 step 6b)** — runs `audit_env.py
--skip-auth --quiet` + `audit_negev_multipliers.py --quiet` on EVERY invocation
(including no-op runs) so .env drift / Negev grid drift gets caught the same
day it happens.

## Install (optional deps)

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp
```

Without these, tracing/metrics are no-ops; logging and the cost ledger still
work (no external deps).

## What to watch during the tournament

- **Quota warnings** in logs (80% of any provider's monthly credits is the one
  to watch). Check `ledger().quota_status("<provider>")`.
- **`tools/llm_audit.py --hours 24`** every match-day morning — surfaces any
  silent NEUTRAL deltas, cascade falls, parse failures.
- **`tools/show_schedule.py --match <name>`** before kickoff — confirms the
  next windows are pending + countdown to fire.
- **Honeycomb** dashboard during a match-window cluster — see latency
  distribution across the 4 simultaneous match dispatches.
- **`update.sh` step 6b output** every deploy — `.env` hygiene + Negev grid
  alignment both must show ✓.

## Bottom-line architecture

Every external call records: provider, endpoint, units, tokens, latency, HTTP
status, error class, correlation_id. Every span gets the correlation_id +
stage auto-stamped. Logs carry the same correlation_id. Honeycomb ties it all
together via `WHERE correlation_id = "match-X-window"`. The audit tools read
from local SQLite so they work offline without burning API quota.

**This means: any production incident is debuggable in ≤ 60 seconds via
`tools/audit_fired_card.py <match_id> <window>` + one Honeycomb query.** No
journalctl grep required.
