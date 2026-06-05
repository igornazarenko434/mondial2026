# Failure modes & edge cases — what can break, and how we handle it

Production-ready but not over-built: the goal is **never silently miss a card,
never crash the loop, never send a wrong/duplicate card, and degrade gracefully
when a data source is unavailable.** Below is each stage, its real failure modes,
and where the mitigation lives.

## Graceful-degradation ladder (the core policy)
A pick should still go out even if pieces are missing. `build_card` (Day 6) must
follow this order and never raise:
1. **Best:** model (Dixon-Coles+Elo) blended with de-vigged live odds + news deltas.
2. **No odds / over budget / devig fails** → model-only pick (Elo+Dixon-Coles);
   note "no market odds" on the card. (Odds are the scoring multiplier, so flag it.)
3. **No model fit (convergence/unknown team)** → Elo + market only.
4. **No LLM/news** → neutral deltas (`news_agent.analyze_safe`), pick still computed.
5. **Nothing usable** → pipeline records `failed` and pushes an alert (loud, not silent).

## Stage-by-stage

| Stage | Failure / edge case | Handling | Where |
|---|---|---|---|
| **Fixture ingest** | API down/5xx/timeout | retry+backoff; on total failure keep last-known calendar in SQLite (persisted) + alert | `reliability.retry`, `football_data` |
| | Malformed/partial JSON | `.get()` guards; bad rows skipped | `football_data.fetch_wc_matches` |
| | Team-name mismatch across sources | **`teams.normalize`** canonical names; applied at ingest | `core/data/teams.py` |
| | Naive/odd timezone string | coerced to aware UTC | `scheduler._parse_utc` |
| | 0 upcoming matches | system idle; daily summary shows it | `repo.upcoming_matches`, summary |
| **Scheduler/timing** | **daemon restarts near kickoff → missed window** | **catch-up**: fire windows up to `catchup_min` late, before kickoff | `scheduler.due_jobs` |
| | **restart re-sends an already-sent card** | **persistent idempotency** via runs ledger `was_handled` | `runs.was_handled`, `runner.tick` |
| | tick throws | caught; loop continues next cycle | `runner.run_forever` |
| | more simultaneous matches than workers | jobs queue; set `SCHED_MAX_WORKERS≥` peak (WC peak ~4) | config |
| **Odds** | event/team not matched | `fetch_match_odds` returns None → model-only pick | degradation ladder |
| | quota exhausted (monthly credits) | **pre-check `ledger.over_budget('odds_api')`** → skip + degrade | `cost.over_budget` |
| | missing/zero/negative odds | `devig` validates, raises `ValueError` → caller degrades | `oddsapi.devig` |
| **Model** | fit doesn't converge / unknown team | catch → Elo+market only | degradation ladder (Day 3) |
| | NaN/negative expected goals | clamp to small positive before `score_matrix` | `dixon_coles` (Day 3) |
| **News/LLM** | all providers fail / no key / bad JSON | **`analyze_safe` → neutral deltas**; pick unaffected | `news_agent.analyze_safe` |
| | hallucinated huge delta | clamped to ±0.6 | `news_agent` |
| **Scoring** | postponed/abandoned/penalties (§20, ET) | only score `FINISHED` with both scores; ET/pens are backlog (manual) | `repo.recent_finished`, CLAUDE backlog |
| **Delivery** | a channel errors (Telegram down) | other channels still send; file channel always on | `delivery._fanout` |
| | all channels fail | pipeline still alerts + logs (stderr) | `pipeline`, logging |
| | duplicate card on retry | retry wraps **build only**, deliver runs once | `pipeline.process_match` |
| **Process** | daemon crash | run under systemd/launchd (auto-restart) + catch-up + idempotency | `docs/SCHEDULING.md` |
| | scheduler hung / dead | heartbeat staleness + missing daily summary alert | `watchdog` |
| | job hung | network timeouts on all HTTP calls; stuck-run detection | `requests timeout`, `runs.stuck` |
| **Config** | missing API keys | **preflight** reports enabled/degraded features at startup | `config/preflight.py` |
| | SQLite on network FS | use local disk for the DB (documented) | `docs/SCHEDULING.md` |
| | host clock skew | rely on NTP (documented); all logic in UTC | — |

## What we deliberately did NOT add (avoid over-engineering)
- No circuit-breaker library — retry + fallback + loud alerting suffices at this
  volume; add `pybreaker` only if a source fails for long stretches.
- No message queue / Celery / Redis — a thread pool + SQLite is right for one user.
- No exactly-once distributed semantics — the runs ledger gives at-most-once per
  (match, window), which is what matters here.
