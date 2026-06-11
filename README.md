# Mondial 2026 — autonomous pick engine for the WC2026 Toto pool

> A self-hosted scheduling daemon that watches every World Cup 2026 match
> and, at strict per-match windows (T-24h → T-7m), assembles a
> degradation-safe probability model from four independent signals, runs
> EV-optimization against the pool's exact-score multipliers, and pushes
> the recommendation to Telegram. **731 tests. $0/month in operational
> cost. One human operator. Full LLM provider cascade with semantic
> failure detection. Per-card forensic-grade audit trail.**

[![tests](https://img.shields.io/badge/tests-731%20passing-brightgreen)]() [![python](https://img.shields.io/badge/python-3.12-blue)]() [![status](https://img.shields.io/badge/status-live%20in%20production-green)]() [![infra](https://img.shields.io/badge/infra-Hetzner%20CPX22%20%E2%82%AC5%2Fmo-orange)]() [![observability](https://img.shields.io/badge/tracing-Honeycomb-success)]() [![license](https://img.shields.io/badge/license-private-lightgrey)]()

---

## What this actually is

A **friends' Toto pool** for the FIFA World Cup 2026. **65 humans + 3 bots (68 total in app)** submit picks across:
- per-game exact scores (1X2 + score, scored against a fixed multiplier grid)
- futures (winner / cinderella / golden boot / "best player")
- daily side bets (yes/no, over/under)

This system **automates the per-game and futures decisions** end-to-end —
gathering live data, running a probabilistic model, picking the
expected-points-maximizing option, and delivering it as a Telegram card
~7 minutes before kickoff.

It does **not** play poker against the pool's social dynamics. Every
recommendation is the answer to one question:

> "Given everything I can know right now about this match, which exact
> score gives me the highest expected points under the pool's published
> scoring rules?"

Validated via Monte Carlo (50,000 tournaments × 68 players × 64 matches):
**P(YOU win the pool) = 61.4%** when friends play modal/safe picks.

## The pick pipeline in one picture

```
                                                  T-24h    T-60m    T-15m    T-7m  ← LOCK
                                                    │        │        │       │
            ┌────────────────┐                      └────────┴────────┴───────┘
            │ schedule/runner│  ← scheduler daemon (Python 3.12, systemd, Hetzner CPX22)
            └───────┬────────┘    per-worker SQLite conn (Day-9.25 thread-safety fix)
                    │  per-tick: ingest → standings → daily_summary → kickoff_cards
                    │  per-window: due_jobs → batch fetch_all_odds → dispatch
                    ▼
        ┌───────────────────────────────────────────────────────────────────────┐
        │                       core/decision/build_card.py                    │
        │                                                                       │
        │   ┌─────────────┐  ┌──────────┐  ┌─────────────┐  ┌─────────────────┐│
        │   │ Dixon-Coles │  │   Elo    │  │  Market     │  │  News pipeline  ││
        │   │             │  │          │  │             │  │  (Day-9.25)     ││
        │   │ martj42 CSV │  │eloratings│  │ the-odds-api│  │  brave → rank → ││
        │   │ → fit → λh,λa  │.net → P(H,│  │ → devig →   │  │  top-K context  ││
        │   │             │  │ D,A)     │  │ P(H,D,A)    │  │  → LLM cascade  ││
        │   │   30% blend │  │ 20% blend│  │  50% blend  │  │  → ±0.6 δh,δa  ││
        │   └──────┬──────┘  └─────┬────┘  └──────┬──────┘  └──────┬──────────┘│
        │          └────────────┬──┴────────────┬─┘                 │           │
        │                       ▼               ▼                   │           │
        │              blended_matrix(λ+δ, elo_p, market_p)         │           │
        │                       │                                  ▼           │
        │                       ▼                                  fold into λ │
        │            P(score) 0..6 × 0..6                                      │
        │                       │                                              │
        │                       ▼                                              │
        │        ev_optimizer.recommend(matrix, Negev_multipliers)             │
        │                       │  + scoring_table + exact_multiplier_used     │
        │                       ▼                                              │
        │   {pick_direction, pick_exact_score, expected_points, audit_trail}   │
        └───────────────────────────────┬──────────────────────────────────────┘
                                        ▼
                            core/delivery → 📲 Telegram channel
                            (📊 standings, ☀️ daily, ⚽ kickoff, 🃏 card)
```

## The 15 things that make this not toy software

| # | Engineering principle | Where it lives |
|---|---|---|
| 1 | **Never raises** — every signal loader wrapped in try/except; pipeline always produces a card | `core/decision/build_card.py` |
| 2 | **Audit trail mandatory** — `signals_used` ∪ `signals_failed` = `{dc, elo, market, news}`, pinned by parametrized tests | `test_build_card.py::test_auditability_golden_rule[...]` |
| 3 | **Graceful degradation ladder** — DC+Elo+Market+News → fewer → modal pick → alert | `docs/FAILURE_MODES.md` |
| 4 | **Single source of truth for scoring** — every multiplier in `config/rules.py`, pinned cell-by-cell to Negev's server-side grid; per-card multiplier stamp in `audit_fired_card.py` section 4b | Day-9.7 + `tools/audit_negev_multipliers.py` (runs every `update.sh`) |
| 5 | **Real probabilities, not rules of thumb** — Dixon-Coles fit on 4,068 real internationals, blended with Pinnacle's devigged odds | `core/models/predict.py::score_distribution` |
| 6 | **EV-optimization** — picks the score that maximizes `P(score) × points_multiplier`, not the modal score; Monte Carlo validated 61% P(win) | `core/decision/ev_optimizer.recommend` + `tools/pick_analyzer.py` |
| 7 | **News article relevance ranking** (Day-9.25) — scores every Brave article on team-name presence, injury/lineup keywords, source authority (ESPN/Sports Mole +3; Wikipedia -3), freshness. Top-5 get 1200-char snippets; lowest scores dropped first | `orchestrator/agents/news_ranker.py` |
| 8 | **LLM router with semantic-failure cascade** (Day-9.25) — `complete_validated` cascades on *transport* errors AND *unparseable bodies*. Live-verified: Gemini 503 → Claude succeeded with same ranked context. Every provider's error class + message recorded in `last_fallback_errors`. | `core/llm/router.py::complete_validated` |
| 9 | **Per-provider quota guards** — every external call wrapped in `obs.external_call`; token-bucket + monthly/daily ledger; over-budget = graceful degrade | `core/obs/__init__.py`, `core/obs/cost.py` |
| 10 | **Distributed tracing in production** — every card has a `correlation_id` traceable through Honeycomb (`WHERE correlation_id="match-537327-T-7m"`); preflight self-test verifies exporter at startup | `core/obs/tracing.py`, `config/preflight.py::_check_tracing()` |
| 11 | **Concurrent dispatch with per-worker SQLite connections** (Day-9.25) — up to 6 simultaneous group-stage kickoffs run in parallel via ThreadPoolExecutor with `with closing(connect()) as conn` per callback. Pinned by 8 thread-safety tests including 24-concurrent-dispatch stress + today-22:00 + tomorrow-22:00 scenario | `schedule/runner.py:__main__` |
| 12 | **Idempotent everywhere** — re-running ingest/scoring/standings sync is safe; runs-ledger prevents double-fire; ON CONFLICT upserts handle catchup | `core/obs/runs.py`, `schedule/runner.py` |
| 13 | **Negev standings reconciliation** (Day-9.25) — sync detects departed members + rename duplicates, DELETEs phantom rows. MY_PARTICIPANT row preserved. Empty-fetch safety prevents wiping the table | `tools/sync_negev_standings.py` |
| 14 | **731 tests, mostly offline** — every external dependency injectable; CI-friendly; one autouse fixture isolates singleton ledgers per test | `tests/conftest.py` |
| 15 | **Self-healing deploy script** (Day-9.25) — `update.sh` step 5b syncs `infra/*.service` + crontab to system paths on EVERY invocation (catches drift even on no-op deploys). Step 6b runs free smoke audits (.env hygiene + Negev grid alignment). Auto-rollback on any health-check failure | `infra/update.sh` |

## Tech stack

- **Python 3.12** — no async, just threads (work is I/O-bound; CPU work is microseconds)
- **SQLite** — single-file DB for matches, predictions, odds snapshots, standings, runs ledger, cost ledger. Per-worker connections under ThreadPoolExecutor.
- **Firebase Firestore** — read/write to the Negev Toto app (friends pool) via a hand-rolled MCP-style connector. Source-wrapped in `obs.external_call` (Day-9.25).
- **OpenTelemetry** → **Honeycomb** (live in production) — distributed tracing; preflight self-test catches misconfiguration
- **Long-polling Telegram Bot API** — message delivery
- **Hetzner CPX22 / Ubuntu 24.04** — €5/mo, systemd-managed daemon, `MPLCONFIGDIR=/tmp/matplotlib` for sandboxed deps
- **No ML framework** — Dixon-Coles is closed-form; LLM only for news synthesis
- **No web frontend** — UX is push notifications + browsable `reports/*.md`

## Architecture at a glance

| Layer | Modules | Purpose |
|---|---|---|
| **Scheduler** | `schedule/{runner,scheduler,watchdog,daily_summary,kickoff_cards}` | Tick loop, dispatch windows, watchdog, hooks |
| **Decision** | `core/decision/{build_card,ev_optimizer,strategy,sidebets,futures}` | Per-game pick, win-the-pool tilt, side bets, futures |
| **Models** | `core/models/{dixon_coles,elo,blend,fit,predict,montecarlo}` | Goal-rate fit, Elo, blend, score matrix, MC bracket sim |
| **Data** | `core/data/{football_data,oddsapi,api_football,web_search,results_io,soccerdata_io,teams}` | All 11 external endpoints + canonicalization |
| **Agents** | `orchestrator/agents/{news_agent,news_ranker}` | LLM-mediated context synthesis + Day-9.25 article ranking |
| **LLM** | `core/llm/{router,providers}` | Gemini → Claude → OpenAI cascade with semantic-failure detection (Day-9.25) |
| **Observability** | `core/obs/{tracing,logging,cost,runs,ratelimit}` | Rate limit, budget, ledger, tracing, runs |
| **Delivery** | `core/delivery/{base,channels}` | Telegram + file + console + render_card |
| **Storage** | `store/{db,repo,schema.sql}` | SQLite I/O (per-worker conns) |
| **Integrations** | `integrations/negev_toto_mcp.py` | Firestore connector + 30+ MCP tools, all source-wrapped (Day-9.25) |
| **Tools** | `tools/*.py` (40+) | Operator CLIs (audit, sync, inspect, analyze, …) |

## Data sources

| Source | Auth | Free quota | Used for |
|---|---|---|---|
| football-data.org | API key | 10 req/min | Match calendar, fixtures, results |
| the-odds-api.com | API key | 500 credits/mo | Decimal 1X2 + futures odds (Pinnacle preferred) |
| api-football.com | API key | 100 req/day | Confirmed XI, injuries, fixture IDs |
| brave search api | API key | 1000 req/mo | Web snippets for the news agent (ranked Day-9.25) |
| Negev Firestore | refresh token | unlimited | Friends pool — standings, picks, side bets |
| Google Gemini Flash 2.5 | API key | 1500 req/day | LLM primary (free tier) |
| Anthropic Claude Haiku 4.5 | API key | PAYG | LLM cascade fallback (active) |
| OpenAI gpt-4o-mini | API key | PAYG | LLM last-resort cascade |
| eloratings.net | scrape | none | Elo ratings per nation (daily cached) |
| martj42 GitHub CSV | none | none | Historical international results for DC fit |
| Telegram Bot API | bot token | 1 msg/sec/chat | Output delivery |

**Total OOP cost ≈ $0/month.** Cost ledger tracks burn rate; budget-guards short-circuit before fees apply.

## Quick start

### Local dev (no API keys needed for tests)

```bash
git clone <repo> mondial2026 && cd mondial2026
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests/ -q                   # → 731 passed in ~36s
```

### Run a card live (burns ~5 free units of various APIs)

```bash
cp .env.example .env  # then fill in keys (see "Configuration" below)
PYTHONPATH=. .venv/bin/python tools/run_one_card_live.py Mexico "South Africa" --window T-7m
```

### Full VM provision (Hetzner CPX22)

```bash
sudo bash infra/bootstrap.sh
# Installs systemd unit, crontab, venv, .env prompt; idempotent.
```

### Continuous deployment

```bash
ssh root@<vm-ip> '/home/mondial/mondial2026/infra/update.sh'
# git pull + pip install (if reqs changed) + infra sync + smoke audits
# + 3-level health check + auto-rollback on failure.
```

## Configuration — what you'd need to make this work for yourself

### `.env` — the only manual setup

The `.env` is **gitignored** (secrets). After `cp .env.example .env`:

#### Required (system can't run without these)

```
FOOTBALL_DATA_API_KEY=...      # free at football-data.org
ODDS_API_KEY=...               # free Starter tier at the-odds-api.com
```

#### Required for full feature set (else the related signal degrades silently)

```
GEMINI_API_KEY=...             # free at aistudio.google.com (recommended primary LLM)
ANTHROPIC_API_KEY=...          # optional cascade fallback
OPENAI_API_KEY=...             # optional last-resort
BRAVE_SEARCH_API_KEY=...       # free $5/mo at brave.com/search/api
API_FOOTBALL_KEY=...           # free at api-sports.io (used at T-60m for lineups)
TELEGRAM_BOT_TOKEN=...         # free at @BotFather on Telegram
TELEGRAM_CHAT_ID=...           # /start your bot, then call getUpdates to find chat_id
```

#### Negev Toto sync (friends pool — optional, fork-specific)

```
NEGEV_TOURNAMENT_ID=...        # your friends-pool's tournament document ID
NEGEV_REFRESH_TOKEN=...        # capture from negev-toto.web.app DevTools IndexedDB
NEGEV_ALLOW_WRITES=0           # set to 1 ONLY if you want to write picks
MY_PARTICIPANT=YourName        # display name in the Negev app
FRIEND_PARTICIPANTS=Vaadia,Tal # comma-separated tracked friends (optional)
```

#### Observability (Honeycomb — optional but recommended)

```
OTEL_SERVICE_NAME=mondial2026
OTEL_TRACES_EXPORTER=otlp                              # or 'console' or 'none'
OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
OTEL_EXPORTER_OTLP_HEADERS=x-honeycomb-team=<api-key>  # free at honeycomb.io
```

#### Strategy (default OFF — pure EV-MAX)

```
STRATEGY_TILT=0                # 0 = pure EV; 0.3-0.6 = position-aware variance
STRATEGY_TOP_K=5
STRATEGY_SWING=6.0
```

#### Scheduler (defaults are correct for the tournament)

```
SCHED_POLL_SECONDS=60
SCHED_MAX_WORKERS=6
INGEST_EVERY_MIN=30
```

### What's NOT in the config that you'd need to provide

| Item | Where | Why it's not configurable |
|---|---|---|
| `data/wc2026_groups.csv` | Canonical roster | Locked by FIFA at draw; updates would be a rule change |
| `data/wc2026_detonator_fixtures.csv` | Detonator games | Pool-specific; depends on your friends' rules |
| `config/rules.py::SCORE_TABLE` | Scoring multipliers | Pool-specific; cross-check with `tools/audit_negev_multipliers.py` if you fork |
| `config/rules.py::WINNER_PAYOUT`, etc. | Futures payouts | Pool-specific |
| `integrations/negev_toto_mcp.py` | Firebase project ID | Hard-coded to "negev-toto"; fork & rename for your pool |

### If you fork this for a different Toto pool

1. **Replace `config/rules.py`** with your pool's scoring grid and futures payouts.
   Run `tools/audit_negev_multipliers.py` (with your pool's Firestore endpoint) to
   verify your grids match the pool's published rules cell-by-cell.
2. **Replace `data/wc2026_groups.csv`** with your tournament's group draw.
3. **Replace `data/wc2026_detonator_fixtures.csv`** with whatever your pool calls
   "high-value games" (the ×2 detonator mechanic).
4. **Rewrite `integrations/negev_toto_mcp.py`** to point at your pool's Firebase
   project. Or if your pool isn't on Firebase, write a similar connector to whatever
   it uses. The `obs.external_call` wrapping at the source is the only requirement.
5. **Test it:** `pytest tests/ -q` should stay green. Add tests for any new
   integration code.

## Inspecting the system after kickoff

40+ CLI tools, all in `tools/`. The unified entry point is `tools/toto.py`:

```bash
PYTHONPATH=. python tools/toto.py help               # list every subcommand
PYTHONPATH=. python tools/toto.py standings --n 10
PYTHONPATH=. python tools/toto.py match Mexico "South Africa"
PYTHONPATH=. python tools/toto.py player Vaadia
PYTHONPATH=. python tools/toto.py suggest Mexico "South Africa"
```

Plus per-card and per-decision forensics:

```bash
# Day-9.25: full post-fire audit for one card (zero API)
python tools/audit_fired_card.py 537327 T-7m

# Day-9.25: LLM news-agent deep dive — Brave queries, ranked context,
# system prompt, provider chain, Gemini's notes + discarded reasoning
python tools/news_inspect.py Mexico "South Africa" --window T-24h

# Day-9.25: per-match EV vs MODAL vs LONGSHOT trade-off table
python tools/pick_analyzer.py Mexico "South Africa" --detonator \
    --xg-home 2.05 --xg-away 0.65 --odds-h 1.43 --odds-d 4.56 --odds-a 8.77

# 5-section LLM runbook (chain state, per-provider failures, parse tiers)
python tools/llm_audit.py --hours 24

# 14 live MCP checks against Negev's Firestore
python tools/verify_negev_live.py

# Negev grid drift watchdog (also runs on every update.sh)
python tools/audit_negev_multipliers.py

# End-to-end Negev↔us scoring sync verification
python tools/verify_scoring_sync.py
```

## Documentation map

| Doc | Audience | What's inside |
|---|---|---|
| [README.md](./README.md) | anyone | This file — recruiter / fork-curious overview |
| [CLAUDE.md](./CLAUDE.md) | dev (incl. AI sessions) | Build order, day-by-day changelog, golden rules, component matrix |
| [docs/SYSTEM_ARCHITECTURE.html](./docs/SYSTEM_ARCHITECTURE.html) | anyone (browser) | Visual walkthrough of every pipeline stage |
| [docs/SERVER.md](./docs/SERVER.md) | operator | Live VM ops: every .env var, SQL queries, Honeycomb queries, alert taxonomy |
| [docs/SCHEDULING.md](./docs/SCHEDULING.md) | operator | Daemon internals, hooks, safe-update procedure (Day-9.25 update.sh) |
| [docs/STRATEGY.md](./docs/STRATEGY.md) | operator | Win-the-pool tilt + pick_analyzer + Monte Carlo (Day-9.25) |
| [docs/OBSERVABILITY.md](./docs/OBSERVABILITY.md) | dev / ops | OTel→Honeycomb chain, complete_validated cascade, audit tools |
| [docs/NEWS_AGENT_PLAYBOOK.md](./docs/NEWS_AGENT_PLAYBOOK.md) | dev | News pipeline + ranker rubric + worked examples (Day-9.25 rewrite) |
| [docs/FAILURE_MODES.md](./docs/FAILURE_MODES.md) | dev | Degradation ladder per component + Day-9.25 improvements table |
| [docs/EDGE_CASES.md](./docs/EDGE_CASES.md) | dev / ops | What's tested vs not, with closing tools per gap |
| [docs/FUTURES_LOCK_2026.md](./docs/FUTURES_LOCK_2026.md) | operator | The 4 pre-tournament picks + analysis |
| [docs/COST_AND_LIMITS.md](./docs/COST_AND_LIMITS.md) | operator | Per-provider quotas + projected burn |
| [docs/BLUEPRINT.md](./docs/BLUEPRINT.md) | architect | Original system design |

## Why these design choices

### "Why not LangChain / Agent SDK?"

Because the flow is a scheduled pipeline, not a conversational graph. Adding
an agent runtime would buy us nothing (we already have ContextVars,
tracing, retries, rate limits, cascade with semantic-failure detection)
and cost us debuggability. The "agent" here is a structured LLM call
with a 4-tier defense: budget pre-check → per-provider cascade →
parse-tier classification → output clamp.

### "Why no vector DB / RAG?"

The data is live + structured. We need today's lineup, today's odds,
today's injuries. Retrieval-augmented anything against last-week's
documents would lose to a direct API call. The news agent calls Brave
fresh on every match window AND ranks the results by relevance to THIS
specific match (Day-9.25 ranker).

### "Why is the market signal weighted highest?"

Because Pinnacle is sharper than any model we can build with 4 years
of national-team data. Pinnacle aggregates the entire sharp-money pool;
Dixon-Coles aggregates a noisy historical signal. We weight the
information source by its accuracy, not its complexity.

### "Why no async?"

The work is I/O-bound but low-frequency (≤ 6 concurrent matches). Threads
+ a shared token-bucket rate limiter give us true parallelism (Python
releases the GIL during I/O) with simpler debuggability than asyncio.
Day-9.25 added per-worker SQLite connections to make this fully safe
under ThreadPoolExecutor.

### "Why SQLite?"

The whole tournament fits in ~10 MB. Postgres would add ops overhead for
zero benefit. SQLite's online `.backup` mode handles concurrent reads
during nightly snapshots. The Day-9.25 per-worker connection pattern
keeps it safe under ThreadPoolExecutor without sacrificing ACID.

### "Why €5/mo Hetzner instead of Lambda?"

Long-polling Telegram + a 24/7 watchdog need a persistent process; serverless
cold-starts would miss windows. Hetzner gives us 100% control, deterministic
latency, and is cheaper than a $0.001/request Lambda at our scale.

### "Why pure EV-MAX as default instead of variance tilting?"

Monte Carlo over 50,000 tournaments × 68 players × 64 matches: when
friends play modal/safe picks (the realistic case), **EV-MAX gives YOU
61% P(win)**. Higher variance picks are reserved for mid-tournament
catch-up scenarios via the opt-in strategy tilt. See `docs/STRATEGY.md`
for the full statistical analysis.

## Live status — 2026-06-11

- ✅ **731 tests green** in 36s
- ✅ Daemon deployed: Hetzner CPX22, Falkenstein, `167.233.66.192`, systemd-managed
- ✅ All 104 fixtures ingested + 6 detonators tagged
- ✅ Day-7 futures locked (Portugal / Uzbekistan / Mbappé / Arkadi — saved to Negev)
- ✅ Day-9.25 enhancements deployed: news ranker, complete_validated cascade, per-worker SQLite, scoring_table stamp, Negev reconciliation, update.sh self-heal
- ✅ Live Honeycomb tracing — preflight self-test passes
- ✅ Live LLM cascade — verified Gemini 503 → Claude succeeded
- ⚽ **First T-60m card: 2026-06-11 21:00 IDT** (Mexico vs South Africa — 6h 57m from now)
- 🃏 **First T-7m LOCK: 2026-06-11 21:53 IDT** (Mexico vs South Africa, ⚡ DETONATOR ×2)

## Test coverage

```
$ pytest tests/ -q
731 passed in 35.92s
```

Every external dependency is **injectable** (`fetch=`, `read=`,
`http_get=`) so the entire test suite runs offline with zero API credits.

Test categories:
- **Per-stage signal failure** — every fail-fast path through `build_card`
- **News ranker edge cases** — 36 tests covering empty results, 60+ articles,
  team aliases, source authority spectrum, URL/title dedup
- **LLM cascade** — 6 tests pinning semantic-failure cascade behavior
- **SQLite thread-safety** — 8 tests including 24 concurrent dispatches +
  1000 sequential persists + today-22:00 + tomorrow-22:00 simulation
- **Negev sync reconciliation** — 6 tests including phantom cleanup +
  rename duplicates + empty-fetch safety
- **Scoring multiplier per-card stamp** — 5 tests across all stages
- **update.sh contract** — 10 tests pinning infra sync + smoke audits +
  bash error-counter guards
- **Detonator display** — 5 tests pinning the ×2 once-applied invariant
- **Preflight tracing self-test** — 7 tests across exporter modes

## Contributing

Currently a single-operator project. If you're forking it for your own
pool: see "If you fork this for a different Toto pool" above. If you're a
future LLM session: read [CLAUDE.md](./CLAUDE.md) §"Onboarding a new LLM
session" first.

## License

Private. Not for redistribution.
