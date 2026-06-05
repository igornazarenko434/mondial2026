# Observability — tracing, logging, metrics, cost/quota

Modern, vendor-neutral stack: **OpenTelemetry** for traces/metrics, **structured
JSON logs** with correlation IDs, and a **SQLite cost/quota ledger** that's
always-on and free. Everything is modular and degrades to safe no-ops if a
dependency or backend is absent — the pipeline runs identically without it.

## The four pillars

| Pillar | Module | What you get |
|---|---|---|
| **Tracing** | `core/obs/tracing.py` | OTel spans per agent/stage; one trace per match-window job; exporter = console / OTLP (Jaeger·Tempo·Honeycomb) / none |
| **Logging** | `core/obs/logging.py` | JSON logs with `correlation_id` + `trace_id` on every line, tying logs to the exact job |
| **Metrics** | `core/obs/metrics.py` | counters (api_calls, llm_tokens, errors) + latency histograms |
| **Cost/quota** | `core/obs/cost.py` | every external call persisted; per-period usage; 80% budget warnings |
| **Rate limit** | `core/obs/ratelimit.py` | shared token-bucket per provider (parallel-safe) |

## One-line instrumentation for any outbound call
```python
from core import obs
obs.setup()                                   # once at startup

with obs.run("match-401-T7m"):                # correlation id for the whole job
    with obs.external_call("odds_api", "h2h", units=1):
        ... # rate-limited, traced, timed, and cost-recorded automatically
```
The two live API clients (`oddsapi.py`, `football_data.py`) and the LLM router
are already wrapped this way, so calls are throttled and accounted for out of the
box. New external calls: just wrap them in `obs.external_call(...)`.

## Tracing during a live run (how to actually watch it)
- **Default (zero setup):** `OTEL_TRACES_EXPORTER=console` prints spans to stdout.
- **Local UI (recommended):** run Jaeger in Docker and point OTLP at it:
  ```bash
  docker run -d --name jaeger -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one
  export OTEL_TRACES_EXPORTER=otlp
  export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
  ```
  Open http://localhost:16686 to see each match-window job as a trace with spans
  for data → odds → news → model → scoring, including latency and errors.
- **Hosted free tier:** Honeycomb / Grafana Cloud Tempo also speak OTLP — set the
  endpoint + headers and you get the same traces in the cloud.

## Configuration (all env, see config/observability.py)
```
OBS_ENABLED=1            OBS_LOG_JSON=1         OBS_LOG_LEVEL=INFO
OTEL_TRACES_EXPORTER=console|otlp|none         OTEL_EXPORTER_OTLP_ENDPOINT=...
OBS_DB=store/obs.db      OBS_QUOTA_WARN=0.8
```
Provider rate limits, budgets, and price estimates live in
`config/observability.py` (`PROVIDER_LIMITS`, `PRICING`) — the single place to
tune them.

## Install (optional deps)
```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp
```
Without these, tracing/metrics are no-ops; logging and the cost ledger still work
(no external deps).

## Where you actually SEE the metrics (no Grafana needed)
The SQLite ledgers (`api_calls`, `runs`) **are** the metric store — every call and
run is persisted with a `correlation_id` (e.g. `match-401-T-7m`), latency, tokens
and cost. So you can see metrics per game / provider / window with zero extra
infrastructure:
- **CLI:** `python -m tools.metrics` (overall + per provider) or
  `python -m tools.metrics match-401-T-7m` (one game: calls, tokens, avg latency,
  errors, cost).
- **Dashboard:** `python -m tools.dashboard` → `reports/dashboard.html` shows run
  health, per-provider metrics, and quota usage on one page.
- **Daily summary** pushed to Telegram: runs / ok / failed / fallbacks / cards.

**Do you need a metrics UI (Prometheus/Grafana)?** Not for a single user — the CLI
+ dashboard + push summary cover it. *Optional upgrade:* point the OTel metrics
exporter at Prometheus and view live charts in Grafana (free, self-hosted) if you
later want real-time time-series. The instrumentation is already there; it's just
a different exporter — no code changes to business logic.

## What to watch during the tournament
- **Quota warnings** in logs (80% of The Odds API monthly credits is the one to
  watch). Check `ledger().quota_status("odds_api")`.
- **Per-match trace** completes through all stages with no error spans before
  T-7m, so the recommendation card is always emitted on time.
- **external_call_ms** histogram — if odds/lineup latency creeps up, pull earlier.
