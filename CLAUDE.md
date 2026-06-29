# Guidance for Claude Code — Mondial 2026

You are helping build a World Cup prediction system. Read this before working.

## Golden rules
1. **Never do points-affecting arithmetic in prose or the LLM.** All scoring and
   EV math lives in `core/scoring` and `core/decision` and is unit-tested. If you
   change scoring, update `tests/test_scoring.py` and keep the PDF examples green
   (France 2-1 → 3.000, draw 1-1 → 5.625, final 2-2 → 12.5).
2. **`config/rules.py` is the single source of truth** for tables/payouts. Don't
   hard-code rule numbers anywhere else.
3. **Keep agents stateless and idempotent** — read from the store, write back,
   safe to re-run. This is what makes parallel runs and retries safe.
4. **Respect free-tier quotas** — cache static data; only pull odds in the active
   match window (T-60m/-15m/-7m). Add a shared throttle before each external API.
5. **No RAG / vector DB** (data is live + structured). Don't add LangGraph yet —
   the flow is a scheduled pipeline. Add the Claude Agent SDK orchestrator last.
6. **Wrap every outbound API/LLM call in `obs.external_call(provider, endpoint)`**
   (see `core/obs/__init__.py`). It rate-limits (shared token bucket), traces,
   times, and records cost/quota automatically. Wrap each match-window job in
   `with obs.run(label):` for a correlation id. Never call an external service
   without going through the limiter — that's what keeps free tiers safe.
7. **LLM is model-agnostic** via `core/llm/router.py` (chain in `config/llm.py` /
   `LLM_PROVIDER_CHAIN`). Default to Claude (subscription credit) → Gemini (free).
   Never import a provider SDK directly in business logic.
8. **Run every match through `pipeline.process_match`** — it wraps build in
   retry+fallback, records run status, delivers the card, and **stays loud on
   failure** (alerts). Never deliver a card outside it; never let a job crash the
   scheduler. Outputs go through `core/delivery` only.
9. **No web frontend.** UX = push notifications + `reports/` files + the optional
   static `tools/dashboard.py`. Don't build a server UI unless explicitly asked.
10. **Build `build_card` to the graceful-degradation ladder** (docs/FAILURE_MODES.md):
    model+odds+news → model-only → Elo+market → neutral-news → (last resort) alert.
    It must NEVER raise; call `news_agent.analyze_safe`, check `ledger.over_budget`
    before odds pulls, normalize teams via `teams.normalize`, and devig defensively.
11. Run `pytest tests/ -q` after every change (482 tests should stay green as of 0075e7d).

## Current state — infrastructure already built (don't rebuild)
These layers exist, are tested, and run today with no API keys. Your job is to
feed them LIVE DATA, not to re-architect them:
- scoring engine, EV optimizer, Dixon-Coles/Elo/blend, de-vig (guarded)
- LLM router (`core/llm`), observability (`core/obs`: tracing/logs/cost/ratelimit/runs)
- reliability (`core/reliability`), delivery (`core/delivery`), pipeline
  (`orchestrator/pipeline`), scheduler+watchdog with catch-up & idempotency
  (`schedule/scheduler`,`runner`,`watchdog`), tools (`tools/dashboard`,`tools/metrics`)
- data: `football_data` (live, normalized, stage-mapped), `store/repo` (upcoming/
  finished), `teams` (name normalization), `config/preflight` (startup check)
- client stubs to fill: `oddsapi`, `api_football`, `soccerdata_io`, `montecarlo`
- win-strategy layer (`core/decision/strategy`): opt-in variance/position tilt on
  top of EV (default off). See docs/STRATEGY.md — max-EV ≠ max-P(win).
- side-bet recommender (`core/decision/sidebets`): daily over/under + yes/no from
  the per-match models.

## What the system produces — 3 outputs (all pure best-practice, NO standings logic by default)
1. **Per-game pick** — 1X2 + exact score that maximize expected points
   (`ev_optimizer.recommend`). This is the default and runs today.
2. **Overall / futures bets** — EV-ranked tables for winner / top scorer /
   Cinderella / fighter (`montecarlo` Day 7 + `config.rules` payouts); lock before
   11.06 21:59.
3. **Daily side bets** — over/under & yes/no recommended from the day's match
   models (`sidebets.recommend_total_goals` / `recommend_yes_no`).

## Position / standings strategy — OFF for now (enable later, mid-tournament)
The system does **NOT** consider your league position by default
(`STRATEGY_TILT=0` → pure expected-points per game). To turn it on later (e.g. if
you fall behind near the knockouts), set `STRATEGY_TILT=0.3–0.6` and pass a
standings context through `strategy.recommend_to_win(...)`. Until then, every pick
is the straight best-practice EV pick. Do not wire standings into `build_card` for
the MVP — keep it a clean, optional post-step.

## Component status matrix — exactly what exists vs what to implement
✅ built & tested (don't rebuild) · 🟡 stub to fill with live data · 🔌 wire-up only

| Component | Status | What YOU still do |
|---|---|---|
| `config/rules.py`, `config/llm.py`, `config/observability.py`, `config/strategy.py`, `config/preflight.py` | ✅ | nothing (tune values if desired) |
| `core/scoring/engine.py` (rules, EV helpers) | ✅ | nothing |
| `core/decision/ev_optimizer.py` (per-game pick) | ✅ | nothing |
| `core/decision/strategy.py` (win tilt) | ✅ **wired into pipeline, default-off** | enable later via `strategy_tilt` + context |
| `core/decision/sidebets.py` (daily over/under, yes/no) | ✅ | call it when the daily bet is published |
| `core/decision/futures.py` (pre-tournament EV ranker §7–10) | ✅ | feed it probabilities (market odds via `implied_probs`, or montecarlo) |
| `core/models/dixon_coles.py` `score_matrix` | ✅ | — ; `fit_strengths` 🟡 feed real results (Day 3) |
| `core/models/elo.py`, `blend.py` | ✅ | recalibrate `draw_base`/weights (Day 3) |
| `core/models/montecarlo.py` (futures prob source) | 🟡 stub | optional: bracket sim → probs (or just use market futures odds) (Day 7) |
| `core/data/football_data.py` (ingest + `refresh`/`tag_detonators`) | ✅ tested offline | add real API key (Day 1) |
| `core/data/cache.py` (daily disk cache) | ✅ | nothing |
| `core/data/soccerdata_io.py` (Elo/FBref loaders: cache+normalize+lookup) | ✅ logic; live `_fetch_eloratings`/`_read_fbref` 🟡 | wire the scrapes (Day 2) |
| `core/data/oddsapi.py` (devig, resolve_wc_key) | ✅ ; `fetch_match_odds` 🟡 | finish event→fixture matching (Day 4) |
| `core/data/api_football.py` (lineups/injuries/backup) | ✅ Day-8 | live-verify lineup payload + aliases once `/fixtures` populates (~24h pre-kickoff) |
| `core/data/web_search.py` (Brave Search, Day-8 news) | ✅ Day-8 | nothing; 3-layer budget brake keeps us at $0 |
| `core/data/teams.py` (normalize) | ✅ | extend aliases if a name slips through |
| `core/obs/*`, `core/reliability.py`, `core/delivery/*` | ✅ | set Telegram creds (optional) |
| `orchestrator/pipeline.py` (run+retry+deliver+strategy) | ✅ | nothing |
| **`build_card(match)` — the real model→card function** | ❌ **MISSING (only `demo_card`)** | **Day 6: implement to the degradation ladder; this is the central glue** |
| `schedule/scheduler.py`, `runner.py`, `watchdog.py` | ✅ wired to store+ingest | add API key, run under systemd (Day 9) |
| `store/db.py`, `store/repo.py` | ✅ | nothing |
| `tools/dashboard.py`, `tools/metrics.py` | ✅ | nothing |
| scoring → `standings` table wiring | ❌ to build | **Day 5** |

**Do FIRST / for now (MVP, no standings logic):** Day 1 calendar → Day 3 model →
Day 4 odds → **Day 6 `build_card`** (this turns the model into the per-game card)
→ deliver. That alone gives live per-game picks. Futures (Day 7) before 11.06 21:59.
Side bets work today via `sidebets`. Everything else is enhancement.

## Build order (each step is a good, self-contained task)
- [x] All infrastructure above (built + tested).
- [x] **Day 1 — calendar (code DONE, tested offline).** `football_data.refresh(conn)`
      = ingest + stage-map + name-normalize + **detonator tagging** (order-independent,
      survives re-ingest) + utcDate/tz guards. Daemon calls `refresh`. ON YOUR
      MACHINE: add `FOOTBALL_DATA_API_KEY`, run `store.db.init_db()` then
      `football_data.refresh(connect())`; verify ~104 matches + R32 stage code (extend
      `RULES_STAGE` if the live code differs) + `repo.upcoming_matches` returns today's.
- [x] **Day 2 — data agent (logic DONE, tested offline).** `soccerdata_io`
      Elo + FBref loaders with daily cache (`cache.py`), name normalization, and
      `elo_of`/`match_elos` lookup (proven to feed the model). ON YOUR MACHINE: wire
      `_fetch_eloratings` (eloratings.net/2026) and `_read_fbref` (soccerdata.FBref),
      or pass `fetch=`/`read=`; everything around them is built & tested.
- [x] **Day 3 — model (DONE incl. assembler + calibrate; needs live data).**
      `fit.py` (results→fit→expected_goals), `backtest.py` (log-loss/Brier/tune),
      `predict.py` (the model→decision ASSEMBLER: fit+elo+market+news → card,
      degradation-safe), `tools/calibrate.py` (load→fit→tune→recommend weights).
      ON YOUR MACHINE: wire `results_io._fetch_live`, run `tools.calibrate.run(...)`,
      paste the recommended weights into `config.rules.BLEND_WEIGHTS`.
- [x] **Day 4 — odds (DONE incl. live audit).** `oddsapi.fetch_match_odds`
      with budget-guarded fetch (`ledger.over_budget('odds_api')`), `fetch_all_odds`
      batch path (1 credit returns all events), `match_event_to_fixture` (canonical
      names + ±36h date window — prevents same-team friendlies false-matching a
      WC fixture), `pick_book` (Pinnacle → Betfair Exchange → Betfair) with
      `consensus_book` synthetic fallback, `snapshot_odds` upserting into
      `odds_snapshots`, `latest_snapshot` reading the sharpest available.
      LIVE-AUDIT JUN 2026: 1 credit fetched 72 events, 72/72 WC fixtures matched
      (100% incl. Bosnia after alias fix), 70 Pinnacle + 2 Betfair, Mexico v
      South Africa devig = 67/21/12 (textbook heavy-favorite), round-trip OK.
      Re-run the audit script if `the-odds-api` adds new team-name spellings.
- [x] **Day 5 — scoring at scale (DONE).** `core/scoring/standings_writer.py`
      with `update_standings()` (idempotent; -15% group reset on first KO
      match scored; preserves futures_points for Day 7), `score_one_match()`
      (returns None on missing data; catches unknown stage labels), and
      `compute_prize_distribution()` (§5 prize ladder).
      `core/scoring/penalties.py::predict_shootout(elo_h, elo_a)` — bounded
      ±0.05 from 50/50 per literature (tanh edge).
      LIVE-VERIFIED Jun 2026: Mexico 2-0 exact + detonator → 12.025 pts
      (matches PDF §12). R16 finish triggers -15% reset → 10.221 / 4.5 / 14.721.
      Penalty predictions: Spain v Brazil 52.0%, Argentina v Curacao 54.7%,
      equal Elo exact 50.0%.
      BUG FIXED in `latest_snapshot`: was using `ORDER BY captured_at DESC`
      on string labels (`"T-pre-tourney" > "T-7m"` in ASCII), picked stale
      pre-tournament snapshot over the T-7m lock. Now walks `WINDOW_PREFERENCE`
      in order. 3 regression tests pin the fix.
- [x] **Day 6 — card + delivery with full audit trail (DONE).**
      `core/decision/build_card.py` — central glue: loads four signals
      (`dixon_coles`/`elo`/`market`/`news`) each wrapped in try/except, stamps
      audit fields (`signals_used`, `signals_failed`, `failure_reasons`,
      `ev_pathway`), predicts `penalty_winner` on KO+draw, persists to
      `predictions` via upsert. NEVER RAISES (golden rule #10).
      `core/delivery/base.render_card` — compact ≤8 lines normal / ≤9 KO+pen;
      `Signals:` line with inline `⚠signal: reason`; modal-line collapse when
      modal==pick; strict cap enforced (log warning runtime, asserted in tests).
      `schedule/runner.py.__main__` now uses real `build_card` (was `demo_card`).
      `config/rules.DRAW_PEN_THRESHOLD = 0.15`.
      LIVE-VERIFIED Jun 2026 across 5 scenarios:
        - All 4 signals (Mexico v South Africa): pick = Draw 0-0 EV 3.42 →
          6.84 with detonator (EV ≠ modal — exactly the system's edge).
        - KO synthetic (R16): penalty_winner = Mexico (54%), render shows
          "► If pens: Mexico (54%)", 8 lines total.
        - Market-failed: ev_pathway=modal_fallback, render shows
          "Signals: DC+Elo+News  ⚠market: odds_api over budget or no event".
        - All 4 signals fail: render still works with all 4 ⚠ markers.
        - Real Telegram delivery: card landed on phone (✓).
      AUDITABILITY GOLDEN RULE pinned via parametrized test across 6 scenarios
      (`test_auditability_golden_rule[...]`) — every signal must appear in
      `signals_used` OR `signals_failed`; silent bypass is impossible by
      construction.
      CARRIED OVER TO DAY 9: `events_cache` parameter is BUILT into
      build_card but the SchedulerDaemon doesn't batch yet — each match
      currently re-fetches odds. Wire daemon to fetch ONE per window and
      pass `events_cache=` to every dispatched match in that window (cuts
      tournament-wide credits from ~300 → ~12).
      CARRIED OVER TO DAY 8: when news_agent.search_queries is wired to
      web-search/API-Football, build_card will start counting "news" as a
      meaningful contributor (currently analyze_safe returns NEUTRAL → news
      is in signals_used but contributes 0 delta).
- [x] **Day 7 — futures lock (DONE — pre-deadline).** Three EV-optimal picks
      computed via live data + Monte Carlo (fighter is intentionally manual).
      Deliverables:
      - `core/models/montecarlo.py` — 20k tournament-bracket simulator
        (Poisson goals from DC fit + Elo-edge penalty shootouts; snake-seeded
        R32 with intra-group rematch avoidance).
      - `core/data/futures_odds.py` — outright market fetchers (winner +
        topscorer with auto-detect; budget-guarded; canonical name normalize).
      - `tools/futures_lock.py` — orchestrator: load data → MC → market → EV
        tables → JSON + Telegram-able pretty-print. Uses **market-prior ×
        sqrt(MC team factor) hybrid** for scorer fallback when no live market.
      - `docs/FUTURES_LOCK_2026.md` — picks + reasoning + pool-win analysis.
      Live picks (cross-checked against ESPN/SI/FOX/RotoWire/Goal.com Jun 2026):
        - WINNER: **Portugal** (EV 3.04, +0.14 margin, market-driven)
        - CINDERELLA: **Uzbekistan** (EV 0.99, +0.62 margin, MC-driven)
        - SCORER: **Mbappé** (EV 3.39, +0.42 margin); contrarian alt **Bellingham** (2.88)
        - FIGHTER: manual per user
      Bug fix bundled: Qatar missing `cinderella_listed` flag in groups CSV.
      Tests: +26 (12 MC + 7 odds + 7 audit). User must enter picks in the
      Toto app before **11.06 21:59 Israel**.
- [x] **Day 8 — news agent (DONE — full wire + observability).**
      Live-verified Jun 2026: end-to-end build_card with real Gemini call
      returned `Signals: DC+Elo+Market+News(gemini)`; trace
      `4664ef0e…`/`7fc52e5d…` visible in Honeycomb with all child spans
      (gemini.complete, brave_search.web, api_football.fixtures/teams,
      football_data.wc_matches).
      Deliverables:
      - `core/data/web_search.py` — Brave Search adapter with 3-layer
        budget brake (key check → 90% monthly brake → 60/day cap).
      - `core/data/api_football.py` — real `find_fixture_id` / `fetch_lineups`
        / `fetch_injuries` / `find_team_id` (with global fallback).
      - `orchestrator/agents/news_agent.py` — 5-layer guardrails (L1 dated
        WC2026-anchored queries → L2 source-side freshness filter → L3 capped
        context with dated headers → L4 SYSTEM prompt with 2 worked examples
        → L5 strict→regex→NEUTRAL parse + ±0.6 clamp). `analyze()` returns
        provider/fallbacks_used; `analyze_safe()` adds failure on degraded
        path. `read_prior_deltas()` reuses T-60m's stored deltas at T-15m
        (saves ~70% of T-15m LLM+Brave calls when XI is confirmed).
      - `core/decision/build_card.py` — gather_context wired at T-24h/T-60m/
        T-15m; news_deltas flow to `match_card` → shift DC expected goals
        (`lh + news_deltas[0]`, `la + news_deltas[1]`). Card carries flat
        `news_provider` / `news_fallbacks_used` / `news_failure` fields
        (queryable from `predictions.payload_json`).
      - `core/delivery/base.py::render_card` — Signals line shows
        `News(gemini)` on success, `⚠news: <reason>` on failure.
      - `core/llm/router.py` — every LLM call wrapped in
        `obs.external_call(provider, "complete")`; `last_provider` +
        `last_fallbacks` stamped on success so callers know which model
        answered.
      - `tools/obs_audit.py` — end-to-end CLI that fires one live probe per
        provider, prints config matrix vs. each provider's published free-
        tier ceiling, verifies spans + ledger rows. Run with
        `OTEL_TRACES_EXPORTER=console` to inspect spans locally.
      Budget math (live, Jun 2026): Brave free credit = 1000 req/mo;
      104 matches × (3 T-24h + 4 T-60m + 2 T-15m worst case) = 936 reqs →
      fits in 1000 with 6% headroom. T-15m cache reuse drops it further.
      Current usage: 4/1000 ($0.02 cost, $0.00 OOP).
      Telegram bot + odds_api `/sports` (free) gaps closed in commit
      `07e9ee7` — every `requests.get/post` in non-test code is now
      inside `obs.external_call(...)`. PROVIDER_LIMITS + PRICING in
      `config/observability.py` cover all 10 providers.
      Tests: +28 over Day-7 baseline (313 total green): per-window query
      counts, gather_context assembly, JSON parse tiers, ±0.6 clamp,
      T-15m cache reuse, router provider stamping, render audit visibility.
      CARRIED OVER (cannot verify pre-tournament, by design): API-Football
      lineup payloads and team-name aliases will only be live-testable when
      `/fixtures` returns rows for WC2026 (`league=1&season=2026` was empty
      as of 2026-06-06; expected to populate ~24h before kickoff). Code
      degrades gracefully to Brave-only context if API-Football is empty.
- [x] **Day 9 — orchestrate (DONE — 24-Day-9 tests, 337 total green).**
      The daemon now runs four hooks per tick, all idempotent + fail-safe:
      `_maybe_ingest` (30 min cadence) → `_maybe_update_standings` (each
      tick, idempotent) → `_maybe_daily_summary` (09:00 Asia/Jerusalem) →
      `_fetch_events_cache_if_needed` (one fetch_all_odds shared across all
      matches in odds-windows) → dispatch → watchdog beat.
      Deliverables:
      - `schedule/runner.py` — added 3 hook callbacks (events_cache_fn,
        standings_update_fn, daily_summary_fn). `_run_job` stamps both
        `_window` and `_events_cache` onto the match dict; `__main__` wires
        all four hooks against the live SQLite+APIs. Default workers
        bumped 4→6 to cover up to 4 simultaneous group-stage kickoffs.
      - `schedule/daily_summary.py` — NEW module: 09:00 Asia/Jerusalem
        Telegram summary (today's games + recent results + standings +
        Brave/odds budget). Idempotent via synthetic match_id=-1 and a
        day-stamped window in the runs ledger. Degrades section-by-section
        on partial failures; never raises.
      - `infra/mondial2026.service` — systemd unit. `Restart=always`,
        `RestartSec=10`, security hardening, `MemoryMax=256M`.
      - `infra/bootstrap.sh` — one-line Ubuntu 24.04 provision: apt + venv
        + clone + .env prompt + systemd enable + cron. Idempotent.
      - `infra/backup.sh` — nightly `sqlite3 .backup` (online, safe under
        concurrent writes) + 7-day rotation. Cron-scheduled by bootstrap.
      - `docs/SCHEDULING.md` — Hetzner CX22 §, day-to-day ops table, full
        Telegram alert taxonomy (cards / pipeline / delivery / scheduler /
        stuck / daily summary), "if you stop receiving messages" runbook.
      Host: **Hetzner CX22, Falkenstein, Ubuntu 24.04**. Mac-can-be-closed
      risk eliminated. Public-repo design:
      secrets stay in `/home/mondial/.env` (chmod 600), NEVER committed.
      Edge cases tested (24 new tests, all offline — zero API calls):
        - events_cache fired ONCE per tick (not per match)
        - events_cache skipped at T-24h (saves a credit)
        - events_cache_fn raising → per-match fallback (build_card unaffected)
        - events_cache_fn None → backwards-compat (existing behaviour)
        - standings_update_fn raising → tick still dispatches
        - standings_update_fn None → no-op (backwards-compat)
        - daily_summary timing gate (before/at/after 09:00 local)
        - daily_summary idempotency within day, sends again next day
        - daily_summary delivery failure recorded once, no retry storm
        - daily_summary alert raising → swallowed, daemon loop survives
        - daily_summary on empty DB → header + budget line, no crash
        - workers default = 6 (regression pin)
        - ODDS_WINDOWS regression pin (T-24h excluded)
      Not done (explicitly optional per spec): Claude Agent SDK subagent
      wrap — adds complexity for no functional gain.
- [x] **Day 9.22 — symmetric tracked-participants + T+1m kickoff card (DONE — 593 total).**
      Adds first-class "track this friend" support across every Telegram
      message + a brand new T+1m kickoff dispatch window. Each tracked
      person (you + every name in `FRIEND_PARTICIPANTS`) appears with the
      SAME rank/total/split/gap-to-leader audit YOU get, in:
      📊 Negev standings sync (top-of-message blocks + Top-5 ← tracked marker)
      ☀️ 09:00 daily summary (compact per-person line under Tracked 👥)
      ⚽ T+1m kickoff card (NEW — per-match picks + standings + lineups)
      🃏 T-60m/-15m/-7m match cards (NEW footer: "👥 Picks" block under EV pick)
      Deliverables:
      - `core/reporting/people.py` — `tracked_participants()` (env →
        ordered list), `render_block` (multi-line audit), `render_compact`
        (one-line), `render_match_picks_block` (per-match picks).  Single
        source of truth reused by all four message sites.
      - `tools/find_member.py` — Negev member lookup by substring (find
        the exact `displayName` BEFORE pasting into `FRIEND_PARTICIPANTS`).
      - `tools/smoke_test_messages.py` — fires ONE of every message type
        at the channel so the operator can visually verify after a config
        change.
      - `schedule/kickoff_cards.py` — fires ~1 min after each kickoff in
        the [T+1m, T+15m] catchup window. Idempotent via runs ledger
        ('kickoff' window). Shared standings snapshot saves N-1 Negev
        credits when N simultaneous kickoffs fire (group stage edge case).
        Per-match exception isolation prevents one Negev/Telegram failure
        from blocking sibling matches.
      - `schedule/runner.py` — new `kickoff_card_fn` hook (None by default,
        existing tests untouched), new `_maybe_kickoff_cards` tick step.
      - `tools/sync_negev_standings.py::_format_telegram_summary` — new
        "TRACKED 👥" header section; friends in Top-5 / Around-You get
        ← tracked marker.
      - `schedule/daily_summary.py::build_summary_text` — new "Tracked 👥:"
        block, pulled fresh from Negev so ranks match the app exactly. Falls
        back to the legacy local-DB "Your score" line when Negev unreachable.
      - `core/decision/build_card.py::_build_friend_picks_section` — fetches
        + renders the per-match picks footer. `core/delivery/base.py::
        render_card` appends it AFTER the MAX_LINES cap (footer is
        supplementary; never truncated).
      - **Root-cause fix** for pre-existing test-ordering fragility: NEW
        `tests/conftest.py` autouse fixture isolates `core.obs.runs._LEDGER`
        and `core.obs.cost._LEDGER` to fresh `:memory:` per test. The
        production singleton path is unchanged; only pytest sees the
        isolated state. Fixes 7 previously-flaky tests in
        test_runner_day9 + test_scheduler.
      - `.env.example` — `FRIEND_PARTICIPANTS=` comma-separated.
      Cost (per tournament): Negev ~520 calls (no budget), api-football
      unchanged (kickoff lineups cached via Day-9.20). Tests: +111 (24
      reporting + 18 kickoff + 9 card-footer + 7 daily/sync extensions).
- [x] **Day 9.11 — LLM news-agent observability — structural attribution (DONE — 482 total).**
      Closes 10 patches from a fan-out workflow audit. Three structural
      blockers + seven important quality fixes:
      - `core/obs/tracing.py::span()` auto-stamps `correlation_id` + `stage`
        on EVERY span. `schedule/runner.py` snapshots ContextVars at
        ThreadPoolExecutor submit time and opens `obs.run("match-<id>-<win>")`
        INSIDE the worker — so a single Honeycomb query
        `WHERE correlation_id="match-X-T-7m"` now returns the full tree
        (run → stage:news → gather_context.api_football.lineups → … →
        gemini.complete → news_agent.parse_validate) instead of just the root.
      - `core/decision/build_card.py` wraps the news block in
        `obs.staged("news", match_id=..., window=...)`. Every api_football
        + brave_search + LLM span becomes a descendant of `stage:news`.
      - `orchestrator/agents/news_agent.py::gather_context()` wraps each
        sub-source in its own `obs.span("gather_context.<source>")` and
        captures structured per-source `ctx_failures` (source, error_class,
        truncated message) on a ContextVar. `context_meta()` exposes the
        last call's diagnostics + `brave_gate` reason; build_card stamps
        them on the card as `news_ctx_failures`, `news_context_sources_ok`,
        `news_context_truncated_chars`, `news_brave_gate`.
      - HTTP status_code + retry_after + error_kind columns added to the
        cost ledger (idempotent ALTER). `obs.external_call` extracts via
        `getattr(e.response, "status_code")` / `getattr(e, "status_code")`
        / `e.code()` — distinguishes Gemini 401 vs 429 vs 503 vs
        Cloudflare-HTML vs requests.Timeout vs ConnectionError.
      - `RateLimitTimeout` raised by `obs.external_call` when the local
        token bucket can't be acquired (was: log warning + PROCEED, which
        produced a downstream 429 indistinguishable from a real one).
        `LLMRouter` catches it via its existing `except Exception`, records
        `error_class='RateLimitTimeout'` in `last_fallback_errors`, falls
        through to the next provider.
      - `LLMRouter` correctness: `_instrument`'s post-call token row now
        passes `units=0` (was double-counting Gemini's 1500/day budget).
        `_ordered_available()` fails CLOSED on over_budget check exception
        (was silent skip-burn). Skips list (`<name>:no_key` /
        `:over_budget` / `:over_budget_check_failed`) merged into
        `last_fallbacks` so the card audit explains BOTH bypassed-up-front
        and tried-and-failed. `AllProvidersFailed` raised with `from last`
        to preserve the SDK exception chain.
      - `_validate_and_clamp` surfaces every silent degradation:
        `home_delta_clamped`, `home_delta_raw`, `delta_parse_error`,
        `confidence_was_defaulted`, `confidence_raw`, `notes_truncated`,
        `notes_original_count`, `notes_format_error`, `schema_error`.
        `analyze()` stamps `json_mode_fallback_used` +
        `json_mode_error_class` when the json_mode=True path dies and we
        retry without it.
      - `web_search._budget_clear()` now returns `(ok, reason)` —
        `no_key` / `monthly_brake` / `daily_cap` / `monthly_check_failed`.
        News-agent picks a SPECIFIC context placeholder and stamps
        `news_brave_gate` on the card. A sick ledger now fails CLOSED.
      - `build_card` unifies `card['news_failure']` and
        `failure_reasons['news']` to a single `news_failure_canonical` so
        the two fields are byte-identical across success/partial/exception.
      - `cost.CostLedger.record()` wraps its INSERT in try/except sqlite3
        (a sick ledger must not replace the real upstream exception) and
        UTF-8-safe truncates `error_message` (chopped 4-byte codepoint
        can't poison downstream readers).
      Tests: 16 new in `tests/test_news_observability_day911.py`.
      After this commit, the runbook "which step in the news pipeline
      failed, why, and where" is answerable in under 60 seconds via
      `tools/llm_audit.py` + one Honeycomb correlation_id query — no
      journalctl required.
- [x] **Day 9.10 — LLM news-agent observability hardening (DONE — 466 total).**
      Four targeted fixes so a missed-news incident is debuggable without
      grepping journalctl. Closes the LLM-side gaps audit identified:
      - `core/obs/cost.py` — `api_calls` table gains `error_class` +
        `error_message` columns via idempotent migration. `obs.external_call`
        captures `type(e).__name__` + first 200 chars of `str(e)` on any
        exception (Gemini's `RateLimitError` is now visually distinct from
        `APITimeoutError` / `AuthenticationError` / `APIConnectionError`).
      - `core/llm/router.py::_ordered_available()` now pre-flight-skips a
        provider when `ledger().over_budget(name)` is True — no more
        wasted 429 + noisy fallback log. Plus new
        `router.last_fallback_errors: {name: {error_class, error_message}}`
        for per-provider chain attribution.
      - `orchestrator/agents/news_agent.py::_parse_json_lenient(raw)` now
        returns `(data, tier)` where tier ∈ {strict, regex_repair, empty,
        failed}. `analyze()` stamps `parse_tier` on the result and (only
        when `tier == "failed"`) captures the first 200 chars of the raw
        LLM output as `raw_excerpt`. `analyze_safe()` adds `failure_class`
        + `fallback_errors` even when the router itself dies.
      - `core/decision/build_card.py` persists 5 new card fields:
        `news_fallback_errors`, `news_parse_tier`, `news_raw_excerpt`,
        `news_failure_class` (+ the pre-existing `news_provider` /
        `news_fallbacks_used` / `news_failure`).
      - `tools/llm_audit.py` — five-section runbook tool: live chain state
        (which provider would run NOW + why others are bypassed),
        per-provider ledger broken down by error class, quota state with
        `🛑 OVER` flag, per-card audit with parse_tier + raw_excerpt,
        recent raw failures with correlation_id (jump to Honeycomb).
      Tests: 16 new in `tests/test_llm_observability_day910.py`. After a
      missed-news card, the runbook is now: `tools/llm_audit.py --hours 24`
      → pinpoint which provider, which error class, which parse tier failed
      → grab correlation_id → Honeycomb shows the spans. No journalctl.
- [x] **Day 9.9 — ⚠ Telegram alert on Negev MCP failure (DONE — 450 total).**
      Standings + audit jobs were silently exiting 1 on Negev connect
      errors. Added `integrations/negev_alerts.py::alert_failure(source,
      reason)` shared helper used by both `tools/sync_negev_standings.py`
      and `tools/post_match_audit.py`. `classify(reason)` heuristically
      tags errors as `config` / `auth` / `rules` / `network` / `import` /
      `unknown` and attaches a concrete remediation hint (e.g. auth →
      "re-capture refreshToken from DevTools"). Fires regardless of
      `--telegram` flag so the 6 silent (`--quiet`) cron runs also warn.
      Both scripts gained `--test-alert` (sends a synthetic message + exit
      0 if Telegram round-trip works) for verification after env changes.
      12 tests in `tests/test_negev_alerts.py`.
- [x] **Day 9.8 — full post-match sync workflow (DONE — 438 total).**
      Closes every gap between "match finishes" and "our DB knows + Negev's
      official scoring is reflected". Deliverables:
      - `toto_get_matches` is now TOURNAMENT-SCOPED (was reading global
        matches across all tournaments — Negev's catalog mixes J-League /
        Allsvenskan / side pools).
      - `toto_get_match_details(home, away)` — combines match + my pick +
        friends' picks + applicable exact-PTS grid + bingoMultiplier.
      - `toto_next_match()` — workflow helper: tells which match comes
        next, what stage_type it is, whether penalty info is needed.
      - `toto_submit_match_prediction(home, away, h, a, advances_team?)` —
        save my per-match pick. `advances_team` for KO + predicted-draw
        cases (you pick which team wins on pens).
      - `toto_update_match_result(...)` — admin path for actual final
        score; supports KO + penalty_home/penalty_away + winner_team.
      - `tools/sync_negev_standings.py::sync_match_results()` — pulls
        Negev's FT/PEN matches → UPDATEs local matches table.
      - `tools/post_match_audit.py` — for each FINISHED match in our DB,
        compares our `score_match()` calc vs Negev's awarded points;
        retries 5×30s if Negev's `processedAt` not yet set; Telegram 🔍
        alert ONLY if Δ > 0.01.
      - `tools/verify_negev_live.py` — one-command 14-check smoke test
        for the MCP.
      - Cron expanded: 03:15 backup, 07:00 main sync + 📊, 08:00 audit,
        16/18/20/22/00/02 silent syncs (≤2h freshness on match-day evenings).
      - `infra/mondial2026.crontab` — single source of truth for the cron
        lines; bootstrap.sh installs from this file (idempotent).
      Live: opener prediction Mexico 2-1 saved successfully to
      `bets/n40ykJlOIA9Mg839hz91_1489369_nsauuOzpJXdVP93djMjIjCeUeMJ3`.
- [x] **Day 9.7 — scoring grid alignment to Negev (DONE — 422 total).**
      Negev consistency audit caught 3 group-stage cells (1-0, 2-0, 3-0)
      that disagreed between our `config/rules.py::_GROUP[0]` and Negev's
      authoritative server-side `managerTables.grids.groupStage`.
      Pattern Negev uses (now ours too): 1-0 ↔ 2-1 = 1.5 (same difficulty);
      2-0 ↔ 3-1 = 2.25; 3-0 ↔ 4-1 = 3.25. Updated `_GROUP[0]` to
      `[2.75, 1.5, 2.25, 3.25, 4.5, 4.5, 4.5, 4.5]` + 3 regression tests
      pinning the new values. All 147 of 147 cells now match Negev across
      groupStage, round16AndQuarter, semiAndFinal.
- [x] **Day 9.6 — Negev Toto MCP typed tools + daily standings sync (DONE — +30 tests, 400 total).**
      Closes the manual-standings-entry gap: the friends' Negev Toto app is
      now the live source of truth for participant points; the local
      standings table sync'd from it daily at 07:00 IDT.
      Deliverables:
      - `integrations/negev_toto_mcp.py` — added 7 typed `@mcp.tool()`
        functions on top of the existing 5 generic ones:
          * `toto_list_tournaments` — every accessible tournament + prize pool
          * `toto_get_standings(tid, extended, include_bots)` — ranked roster
            scoped to one tournament, tie-break by `exactScoreCount`
          * `toto_get_matches(date_after, status, stage, limit)` — Negev's
            match catalog with team names passed through `teams.normalize`
            and stages mapped to our `RULES_STAGE`
          * `toto_get_broad_bets(tid)` — per-user futures picks joined with
            `displayName` from the `users` collection
          * `toto_get_side_bets(tid, active_only)` — daily yes/no shells
          * `toto_get_my_preferences()` — read my `pref_*` flags
          * `toto_update_preferences(...)` — patch my prefs (gated by
            `NEGEV_ALLOW_WRITES=1`)
        Plus `_read_all` helper that paginates Firestore via
        `nextPageToken`. Verified live: 63 players found in the production
        Negev Toto 2026 tournament (`n40ykJlOIA9Mg839hz91`); Igor at rank
        26 with all-zeros (tournament hasn't started).
      - `tools/sync_negev_standings.py` — daily sync script:
          * Calls `toto_get_standings` → upserts to our `standings` table
          * Mapping (Option A from handoff): `directionPoints` →
            `group_points`, `0` → `knockout_points`, `broadBetPoints` →
            `futures_points`. Strategy layer only uses (you, leader,
            second) gaps so the group/KO split doesn't matter.
          * Wrapped in `obs.external_call("negev_toto", "get_standings")`
            so the call traces to Honeycomb + costs to the ledger.
          * Cron: `0 7 * * * /home/mondial/mondial2026/.venv/bin/python
            tools/sync_negev_standings.py` runs daily 2h before the 09:00
            daily summary so the summary reflects fresh Negev points.
          * `--dry-run` / `--include-bots` / `--tournament-id` flags.
      - `.env.example` — added `NEGEV_TOURNAMENT_ID=n40ykJlOIA9Mg839hz91`
        (the verified live id) + `NEGEV_ALLOW_WRITES=0`.
      - `config/observability.py` — added `negev_toto` provider (5 req/s
        polite throttle, no budget, $0 cost) to PROVIDER_LIMITS + PRICING.
      Tests: +30 (20 typed-tools + 10 sync). All offline (`requests.get`
      mocked exactly like `test_ingest.py` mocks football-data). Live
      smoke-test confirmed end-to-end: 63 players, correct mapping,
      Honeycomb trace.
- [x] **Day 9.5 — win-the-pool layer wired end-to-end (DONE — 32 tests; 370 total inc. GROUP_-strip regression).**
      Closes the gap between "pure-EV picks" and "position-aware picks":
      - `store/repo.py::standings_context` — fixed TWO bugs: (1) removed
        unconditional `group_points * 0.85` multiplier (was double-applying
        the §14 reset post-knockouts), (2) defaults to `None` (no-op) when
        `me=None` or `me` not in standings instead of silently using the
        leader's total as yours.
      - `tools/standings_set.py` — NEW CLI: `list / set / remove / import`
        for entering friends' Negev Toto standings (no auto-scrape — needs
        their Firebase auth which we deliberately don't depend on). Marks
        YOUR row via `MY_PARTICIPANT` env var.
      - `schedule/runner.py` — daemon now accepts `strategy_context_fn` +
        `strategy_tilt` hooks. `_run_job` loads context AT DISPATCH time
        (so a fresh `standings_set` update fires on the next match-window
        without a daemon restart). `__main__` wires the live versions
        backed by `MY_PARTICIPANT` env var + `config.strategy.DEFAULT_TILT`.
      - `.env.example` — added `MY_PARTICIPANT`, `STRATEGY_TILT`,
        `STRATEGY_TOP_K`, `STRATEGY_SWING` with on/off recommendations.
      - `docs/STRATEGY.md` — appended "How to actually turn it on" section
        with .env values, CLI usage, group-reset rules, "how to verify"
        SQL query.
      Default: OFF (`STRATEGY_TILT=0` → pure-EV unchanged). Flip later in
      tournament if behind: `echo STRATEGY_TILT=0.4 >> .env && systemctl
      restart mondial2026`.
- [x] **Operational infrastructure done (Day-9.5 era).**
      - Hetzner CPX22, Falkenstein, Ubuntu 24.04. Live IP in operator
        notes (NOT committed), hostname mondial2026.
      - `infra/bootstrap.sh` — idempotent Ubuntu provisioner. Uses stock
        python3 (3.12); no PPA dependencies.
      - `infra/update.sh` — safe code-update with active-worker guard
        (refuses to restart mid-window unless `--force`), 3-level health
        check post-restart (is-active + "scheduler started" in journal +
        zero ERROR lines in 60s), AUTO-ROLLBACK to previous SHA on any
        failure, `--dry-run` / `--rollback` flags.
      - `infra/backup.sh` — nightly `sqlite3 .backup` + 7-day rotation.
      - `infra/mondial2026.service` — `Restart=always`, MemoryMax=512M,
        ProtectSystem=strict, ReadWritePaths covers store/cache/reports.
      - `docs/SERVER.md` — canonical operational reference (~400 lines).
        Written so a fresh LLM session reading only this file can fully
        operate the daemon: server identity, every env var with
        defaults, rate limits verified against actual dashboards, ops
        cheat-sheet, SQL queries, Honeycomb queries, alert taxonomy,
        common problems + fixes, tournament timeline, "if you're a new
        LLM session" onboarding section.
      - `docs/SYSTEM_ARCHITECTURE.html` — single-file presentation
        explaining every stage, data flow, calculation, fallback
        (printable / shareable).
      - Honeycomb live: trace `604ae03d…` from VM has the full
        ~8-span audit run (gemini, brave, api-football, odds, football-
        data, telegram, run parent).
      - Provider rate limits verified against actual dashboards Jun 2026:
        api_football 10/min 100/day (bumped from 5/min), football_data
        10/min, odds_api 500/mo, brave 1/sec 1000/mo, gemini 15/min
        1500/day. All in `config/observability.py::PROVIDER_LIMITS`.
- [ ] **Day 10 — harden.** **Full BLEND_WEIGHTS calibration** — two paths:
      (a) PRE-TOURNAMENT WARM-START (~50 credits, optional). the-odds-api
          DOES expose a historical-odds endpoint on the free tier (corrected
          Jun 2026 — earlier docs claimed otherwise). It costs **10× the
          standard rate** (`GET /historical/sports/<key>/odds`: 1 market ×
          1 region × 10 = 10 credits/call), so the 500/mo budget allows
          ~50 historical calls. Pull odds for the last ~50 played
          internationals + match to results we already have in martj42 →
          full 3-source samples → real tune NOW.
      (b) POST-TOURNAMENT (preferred, free). After ≥20 played WC matches
          with locked T-7m odds + actual scores, re-run
          `tools.calibrate.run(...)` with the full 3-source grid (don't
          hold market fixed) and paste the winning triple.
      Fallback historical archives if (a) is exhausted: football-data.co.uk
      (European leagues, club only), Kaggle `mexwell/historical-football-
      resultsbetting-odds-data`, github.com/iredchuk/soccer-bookmaker-odds,
      footballcsv.github.io (results only, no odds). Most are club-focused —
      international-team odds archives are sparse, so (a) is the cleanest
      pre-tournament option.
      Partial Step-B DC-vs-Elo tune on real data (Jun 2026) showed DC/Elo
      ratio 0.75/0.25 → rescales to (0.375, 0.125, 0.50) with market=0.50
      fixed; current (0.30, 0.20, 0.50) defaults are within 0.4% of optimum
      so no urgent change.
      Plus: retries, throttle, finalize futures. Confirm
      `ledger().quota_status(...)` under all free quotas; optionally start
      Jaeger for a full match-window trace (see `docs/OBSERVABILITY.md`).

## Cross-day audit checklist (run after EVERY day's wire-up)

1. **Edge-case team-name aliases per data source.** The 5 sites use 5 different
   spellings; `core/data/teams.py` must canonicalize them all. Known per source
   (regression-tested in `tests/test_data_wiring.py`):
   - football-data.org: "Korea Republic", "Cabo Verde", "Cape Verde Islands"
   - the-odds-api: "Bosnia & Herzegovina" (with `&`), "Czech Republic"
   - eloratings.net: 2-letter codes via `eloratings_codes.py`
   - martj42 CSV: "Korea Republic", "Cabo Verde", "Cote d'Ivoire"
   - api-football: empty until tournament starts — re-audit ~24h pre-kickoff
     when `/fixtures?league=1&season=2026` first returns rows. Day-8 wired
     the client + graceful degradation; live payload-level alias check is
     the only remaining piece.
   After any new source addition, run the audit:
   `for n in {api_names}: assert normalize(n) in groups_truth`.
2. **Cost-ledger reconciliation.** `obs.cost.ledger().quota_status(provider)`
   for every provider after a window. Flag >50% used as warning, >80% as alert.
3. **Robustness pass on every external call.** Each `requests.get/post` must be
   inside `obs.external_call(...)` (rate limit + cost recorded). Run
   `grep -rn 'requests.\(get\|post\)' core/data/ orchestrator/` to verify zero
   ungated calls land in main.

> Observability is already wired (logging + cost ledger always on; OTel tracing
> optional). As you build each step, keep external calls inside
> `obs.external_call(...)` and jobs inside `obs.run(...)`. See
> `docs/COST_AND_LIMITS.md` for the full-scale budget and `docs/OBSERVABILITY.md`
> for how to trace live.

## Backlog — rule cases intentionally NOT yet automated (do after Day 10 if wanted)
These are manual in the spreadsheet today; port to `core/scoring` only if you want
full automation. None block the MVP.
- Penalty-shootout partial credit (§15c-e, §16c-d) — knockout games decided on pens.
- The "fighter" (§10) full ranking math — the futures pick is captured; ranking isn't.
- Extra-time→120' result mapping for knockout scoring.

## Stage mapping note
`football_data.RULES_STAGE` already maps football-data codes (GROUP_STAGE→Group,
LAST_32→R32, LAST_16→R16, QUARTER_FINALS→QF, SEMI_FINALS→SF, THIRD_PLACE→3rd,
FINAL→Final) and `ingest()` stores the rules stage directly, so `score_match`
gets the right stage. Verify the round-of-32 code string against the live API on
Day 1 (48-team format) and extend the map if needed.

## Testing the edge
`orchestrator/run.py` is the integration smoke test. Keep it runnable with no
keys (placeholder inputs) so you can always eyeball the pipeline.

## What's tested in production vs. what only the first whistle can prove

The system was deployed to the Hetzner VM on 2026-06-07. Between deploy and
the first match (2026-06-11 22:00 Israel) the daemon idle-ticks — no jobs
fire, no cards emit. This is by design. The following are pinned by 438
unit/integration tests but will only be confirmed in production from the
opener onwards:

| Path | Tests that pin it | First live fire |
|---|---|---|
| T-24h / T-60m / T-15m / T-7m dispatch | scheduler concurrency + idempotency tests | 2026-06-10 22:00 Israel |
| News agent's `gather_context` with REAL API-Football data | 8 tests under `test_news_agent_day8.py` | T-60m of opener |
| LLM router gemini→claude→openai fallback in PROD | `test_llm.py`'s 4-provider fallback tests | first time gemini returns malformed/empty |
| `events_cache` batched odds | `test_events_cache_fetched_once_per_tick_when_t7m_due` | T-60m of opener |
| T-15m cache reuse from T-60m predictions | `test_read_prior_deltas_*` | 21:45 Israel on opener day |
| `news_provider` stamped on a real card | `test_news_provider_stamped_on_card_when_analyzer_returns_one` | T-24h+ |
| Detonator ×2 EV multiplier on real card | `test_scoring_*` + `test_ev_optimizer_*` | opener IS detonator |
| Concurrent multi-match dispatch | `test_two_simultaneous_matches_run_concurrently` | Group day-2 (2026-06-12+) |
| Standings update after FINISHED | `test_standings_after_first_ko_applies_reset` | ~2026-06-12 morning |
| Strategy tilt active picks | 32 win-the-pool tests | When STRATEGY_TILT>0 |
| `process_match` retry on transient | `test_reliability_*` | first 429/503 from a provider |
| Watchdog stuck-job alert | `test_watchdog_detects_stuck_run` | first hung worker (hopefully never) |

## Known unhandled-but-low-risk edges (port if they trigger)

These are real edges that could appear during the tournament. Each has a
graceful default — none breaks the pick — but if you see them in logs you
can fix and `update.sh`.

- **Match status = `POSTPONED` / `CANCELLED`** — `upcoming_matches` filter
  is `status IN ('SCHEDULED','TIMED')` so postponed/cancelled rows drop out.
  When football-data re-publishes a new `utc_kickoff`, status flips back to
  `SCHEDULED` and the new windows compute correctly. **But**: if a card was
  already DELIVERED at T-7m for the old kickoff and the match then gets
  postponed (rare), the runs ledger sees `was_handled=True` for the old
  `(match_id, T-7m)` and won't re-fire when the new kickoff arrives. Fix
  if it triggers: `DELETE FROM runs WHERE match_id=<mid> AND window='T-7m'`.

- **Extra-time + penalty result mapping** — knockouts. Current
  `score_match` uses the FINAL score regardless of how it got there
  (regulation, ET, pens). For a match that finishes 1-1 in regulation and
  goes to pens, football-data returns `score.fullTime` as the regulation
  result and the pen score in a separate field. Our ingest takes
  `score.fullTime` so we score 1-1 (draw direction) which under the §15c-e
  rules is partially correct but doesn't award the pen-winner bonus.
  Backlog item — port to `score_match` if it materially affects standings.

- **`events_cache` staleness within a tick** — we fetch at start of tick,
  dispatch jobs that consume it ~milliseconds later. Within one tick the
  cache is fresh; if a tick takes 2+ minutes to dispatch (it doesn't — pool
  submission is instant), it could be stale. No real risk today.

- **Honeycomb OTLP export 500-ing** — silently degrades; the SDK keeps
  trying. No card-delivery impact. Daemon continues normally without traces
  until Honeycomb recovers.

- **Telegram bot rate-limit (30 msg/sec global)** — worst case 4 simultaneous
  group matches × 4 windows = 16 messages within 6 min. 4 msg/sec peak. Way
  under limit. Not a concern.

- **`detonator` flag if a match the CSV doesn't cover gets played** — the
  flag defaults to 0 (no detonator). Scoring computes correctly (no ×2);
  pick remains EV-optimal for non-detonator EV math. No edge.

- **Time changes BACKWARDS** (UEFA reschedules to earlier) — `due_jobs`
  recomputes from the new (earlier) `utc_kickoff` so new windows fire
  correctly. The OLD windows are then "in the past" and the catchup-cap
  (120 min) excludes them. No double-fire. No miss if the new kickoff is at
  least 7 minutes ahead.

## Onboarding a new LLM session

If you (a future LLM session) are looking at this repo for the first time,
read in this order:

1. **CLAUDE.md** (you're here) — build order + open items + golden rules
2. **docs/SERVER.md** — operational reference for the running daemon on
   Hetzner. Includes every .env var, every SQL/Honeycomb query you need,
   and onboarding instructions in §11.
3. **docs/SYSTEM_ARCHITECTURE.html** — single-file visual presentation of
   every pipeline stage, data flow, failure mode, and calculation. Open
   in any browser. Print-friendly.
4. **docs/SCHEDULING.md** — daemon internals + safe-update procedure
5. **docs/STRATEGY.md** — win-the-pool tilt + how to activate
6. **docs/BLUEPRINT.md** — original system design (some details now stale;
   trust the code over the blueprint when they conflict)
