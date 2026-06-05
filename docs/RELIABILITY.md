# Reliability — retry, fallback, failure visibility, delivery

Principle (verified best practice): **retry transient errors, fall back on
sustained ones, and never fail silently** — a circuit breaker or a dead scheduler
is dangerous only if nobody notices, so every outcome is recorded and alerted.

## What happens on a normal run (T-7m, per match)
1. `obs.run(label)` opens a correlation id + trace for the whole job.
2. The run ledger writes a `started` row.
3. Card build is wrapped in `retry(max_attempts=3)`:
   - **transient** errors (network blip, timeout, HTTP 429/5xx) → exponential
     backoff + jitter, retried up to 3×;
   - **permanent** errors (bad input, auth, `PermanentError`) → fail fast, no retry.
4. Data sources use `with_fallback(primary, backup)` (e.g. football-data →
   API-Football), and the **LLM router** falls back Claude → Gemini → OpenAI.
5. On success → card **delivered** to channels; ledger row set to `ok`
   (with which source served it and whether delivery succeeded).
6. On terminal failure → ledger row set to `failed` (+ reason) **and an alert is
   pushed**. The exception never escapes to crash the scheduler; the next match's
   job is unaffected.

## How you detect each situation
| Situation | How you know |
|---|---|
| Run succeeded | card arrives; `runs` row `ok`, `card_delivered=1` |
| Used a fallback (e.g. backup odds, Gemini) | `runs` row `fell_back=1`; log warning; counted in daily summary |
| Run failed | ⚠️ alert pushed immediately; `runs` row `failed` + `detail` |
| Stopped mid-execution | `runs` row stays `started` (never finished) → counted as **stuck** in `summary()` and the dashboard |
| Scheduler itself died | the **daily health summary** stops arriving / shows 0 runs — your heartbeat |
| Nearing a free quota | cost ledger logs an 80% warning; dashboard shows ⚠️ |

```python
from core.obs.runs import runs
runs().summary(24)     # {'total','ok','failed','stuck','fallbacks','cards_delivered','failures':[...]}
```

## Where final outputs go
`core/delivery/` fans out each card to the channels in `DELIVERY_CHANNELS`:
- **file** (always on) → `reports/<ts>.md` + appended `reports/feed.md`;
- **telegram** (optional) → your phone;
- **console** → stdout (local runs / tests).
If a channel errors, the others still get the card; if *all* fail, an alert is
raised so a computed-but-undelivered card can't vanish silently.

## Tuning
- Retry counts/backoff: args to `retry()` in `core/reliability.py`.
- Provider rate limits / budgets: `config/observability.py`.
- For heavier resilience you can drop in `tenacity` (retries) and `pybreaker`
  (circuit breaker) without changing call sites — but for this single-user,
  low-volume system, the built-in retry + fallback + loud alerting is sufficient.
  Add a circuit breaker only if a source starts failing for long stretches.
```
