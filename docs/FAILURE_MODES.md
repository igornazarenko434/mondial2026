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
4. **No LLM/news (any provider)** → neutral deltas (`news_agent.analyze_safe`),
   pick still computed.
5. **Nothing usable** → pipeline records `failed` and pushes an alert (loud, not silent).

## Day-9.25 reliability improvements

| Bug | Was | Now |
|---|---|---|
| **SQLite thread-safety** | Single conn opened in main thread, handed to worker → `objects created in a thread can only be used in that same thread` → persist_card silently failed → predictions table EMPTY for whole tournament | Per-worker conn via `with closing(connect()) as conn` in every callback (fixtures, ingest, build, standings, daily_summary, kickoff, strategy_context). SQLite serializes writes via OS-level journal; `ON CONFLICT(match_id, window)` upserts safely under concurrent dispatch. |
| **Strategy context fails same way** | `strategy_context_fn failed: ...; using pure-EV` on every dispatch | Fixed by the same per-worker conn pattern |
| **Gemini parse-fail returns NEUTRAL** | Truncated JSON → parser rejects → NEUTRAL → claude/openai never tried | `LLMRouter.complete_validated` cascades on **semantic** failures (parse-fail) too. Records `error_class='ValidationFailed'` for the failed provider, tries next. |
| **Detonator display double-counts** | Card showed "EV ≈ 3.37 → ×2 detonator ≈ 6.74" but 3.37 already included the ×2 | Now shows "EV ≈ 3.37 (×2 detonator already applied)". Math unchanged; display fixed. |
| **Negev MCP requests bypass instrumentation** | Raw `requests.*` in `negev_toto_mcp.py` not wrapped → new callers silently bypass rate-limit + ledger | New `_fs(method, url, endpoint=...)` helper wraps every Firestore call at the SOURCE. All `firestore:*` + `auth:*` endpoints record automatically. |
| **matplotlib R/O home warning** | `ProtectHome=read-only` blocks `$HOME/.config/matplotlib` → WARNING every dispatch | `Environment="MPLCONFIGDIR=/tmp/matplotlib"` in systemd unit |
| **Standings phantom rows + missed joiners** | Sync only UPSERTS → departed members persist as zero-pt phantoms; renames create duplicates | After upsert, computes set diff (DB) − (current Negev roster) → DELETE phantoms. **Safety**: deletion only runs when `n_upserted > 0` so a transient empty fetch doesn't wipe the table. MY_PARTICIPANT row never deleted. |
| **OTel exporter could silently no-op** | `OTEL_TRACES_EXPORTER=otlp` with missing endpoint/header → no spans, no error | New `_check_tracing()` in preflight verifies endpoint+headers+span open/close at startup |
| **update.sh missed infra drift** | Bumping `infra/mondial2026.service` updated repo but not `/etc/systemd/system/` → daemon kept stale unit | Step 5b runs on EVERY invocation; cmp + cp + daemon-reload; same for crontab |
| **update.sh bash bug** | `grep -c \|\| echo 0` produced `"0\n0"` → `[ "0 0" -gt 0 ]` error | `\|\| true` + `tail -1` + `2>/dev/null` guards |

## Stage-by-stage

| Stage | Failure / edge case | Handling | Where |
|---|---|---|---|
| **Fixture ingest** | API down/5xx/timeout | retry+backoff; on total failure keep last-known calendar in SQLite (persisted) + alert | `reliability.retry`, `football_data` |
| | Malformed/partial JSON | `.get()` guards; bad rows skipped | `football_data.fetch_wc_matches` |
| | Team-name mismatch across sources | **`teams.normalize`** canonical names; applied at ingest | `core/data/teams.py` |
| | Naive/odd timezone string | coerced to aware UTC | `scheduler._parse_utc` |
| | 0 upcoming matches | system idle; daily summary shows it | `repo.upcoming_matches`, summary |
| **Scheduler/timing** | **daemon restarts near kickoff → missed window** | **catch-up**: fire windows up to `catchup_min=120` late, before kickoff | `scheduler.due_jobs` |
| | **restart re-sends an already-sent card** | **persistent idempotency** via runs ledger `was_handled` | `runs.was_handled`, `runner.tick` |
| | tick throws | caught; loop continues next cycle | `runner.run_forever` |
| | more simultaneous matches than workers | jobs queue; `SCHED_MAX_WORKERS=6` covers up to 4-match clusters | config |
| | **per-worker SQLite conn (Day-9.25)** | every callback opens `with closing(connect()) as conn`; SQLite serializes via journal; ON CONFLICT upserts safely | `schedule/runner.py:__main__` |
| **Odds** | event/team not matched | `fetch_match_odds` returns None → model-only pick | degradation ladder |
| | quota exhausted (monthly credits) | **pre-check `ledger.over_budget('odds_api')`** → skip + degrade | `cost.over_budget` |
| | missing/zero/negative odds | `devig` validates, raises `ValueError` → caller degrades | `oddsapi.devig` |
| **Model** | fit doesn't converge / unknown team | catch → Elo+market only | degradation ladder (Day 3) |
| | NaN/negative expected goals | clamp to small positive before `score_matrix` | `dixon_coles` (Day 3) |
| **News/LLM** | **Gemini transport error (5xx/timeout/auth)** | `complete_validated` cascades to claude → openai; each provider's error_class + message recorded | `core/llm/router.py` |
| | **Gemini parses but body is unusable (truncated JSON, prose-only, garbage)** | (Day-9.25) `complete_validated` treats `parse_tier='failed'` as `ValidationFailed`; cascades to next provider | `news_agent.analyze` |
| | All providers cascade-fail | `analyze_safe` catches `AllProvidersFailed` → NEUTRAL (0, 0) with `failure_class` stamped | `news_agent.analyze_safe` |
| | Hallucinated huge delta | clamped to ±0.6 + `home_delta_clamped` flag stamped | `_validate_and_clamp` |
| | Brave context overflow | Day-9.25 ranker drops LOWEST-scored articles first (not last-in-Brave-order); top-K keep 1200-char snippets | `news_ranker`, `_fmt_web_results` |
| | Brave budget brake | serves STALE cached results (Day-9.21) when budget exhausted; `news_brave_gate` flag stamped on card | `web_search._budget_clear` |
| | API-Football fixture not found | placeholder text added; brave_search still runs; sources_ok stamped per source | `gather_context.api_football.lineups` |
| **Scoring** | postponed/abandoned/penalties (§20, ET) | only score `FINISHED` with both scores; ET/pens are backlog (manual) | `repo.recent_finished`, CLAUDE backlog |
| | **per-card multiplier stamp (Day-9.25)** | `build_card` writes `scoring_table` + `exact_multiplier_used`; `audit_fired_card.py` cross-checks vs `STAGE_TYPE[stage]` | `core/decision/build_card.py:309-339` |
| | unknown stage label | stamp degrades to None + warning log; card still produced | same |
| **Delivery** | a channel errors (Telegram down) | other channels still send; file channel always on | `delivery._fanout` |
| | all channels fail | pipeline still alerts + logs (stderr) | `pipeline`, logging |
| | duplicate card on retry | retry wraps **build only**, deliver runs once | `pipeline.process_match` |
| **Process** | daemon crash | run under systemd (`Restart=always`, `RestartSec=10`) + catch-up + idempotency | `infra/mondial2026.service` |
| | scheduler hung / dead | heartbeat staleness + missing daily summary alert | `watchdog` |
| | job hung | network timeouts on all HTTP calls; stuck-run detection | `requests timeout`, `runs.stuck` |
| **Config** | missing API keys | **preflight** reports enabled/degraded features at startup | `config/preflight.py` |
| | OTLP exporter misconfigured (no spans land) | **(Day-9.25) `_check_tracing()` opens a no-op span at startup**; logs ERROR if endpoint missing or Honeycomb headers absent | `config/preflight.py` |
| | systemd `Environment=` env-var leak (inline comments) | `audit_env.py` scans `.env` for the systemd inline-comment trap | `tools/audit_env.py` |
| | Negev grid drift mid-tournament | `audit_negev_multipliers.py` diffs Negev's live grids vs our `config/rules.py`; **runs on EVERY update.sh** | `update.sh` step 6b |
| **Negev sync** | empty fetch (auth fail / Firestore 503) | sync returns `ok=False`; DB **NOT** wiped (deletion gated on `n_upserted > 0`) | `tools/sync_negev_standings.py` |
| | departed members | (Day-9.25) reconciliation deletes rows not in current Negev roster (except MY_PARTICIPANT) | same |
| | renamed displayName (creates dup) | both old form(s) deleted by reconciliation; new canonical form remains | same |
| **VM deployment** | (Day-9.25) **infra/* drift between repo and system paths** | `update.sh` step 5b: cmp systemd unit + crontab; sync + daemon-reload when drifted | `infra/update.sh` |
| | (Day-9.25) drift in env / Negev grid silently | `update.sh` step 6b smoke audits on EVERY invocation | same |

## What we deliberately did NOT add (avoid over-engineering)

- No circuit-breaker library — retry + fallback + loud alerting suffices at this
  volume; add `pybreaker` only if a source fails for long stretches.
- No message queue / Celery / Redis — a thread pool + SQLite is right for one user.
- No exactly-once distributed semantics — the runs ledger gives at-most-once per
  (match, window), which is what matters here.
- No UID-keyed standings table — name-keyed is sufficient given Day-9.25's
  reconciliation; UID-keyed would let rename ALSO preserve the historic
  group_points snapshot in our DB across renames, but Negev's UID-backed totals
  are pulled fresh on every sync so no points are lost.
