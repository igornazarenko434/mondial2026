# Mondial 2026 — autonomous pick engine for the WC2026 Toto pool

> A self-hosted scheduling daemon that watches every World Cup 2026 match
> and, at strict per-match windows (T-24h → T-7m), assembles a
> degradation-safe probability model from four independent signals, runs
> EV-optimization against the pool's exact-score multipliers, and pushes
> the recommendation to Telegram. **Built in 9 days. 600 tests. $0/month
> in operational cost. One human operator.**

[![tests](https://img.shields.io/badge/tests-600%20passing-brightgreen)]() [![python](https://img.shields.io/badge/python-3.12-blue)]() [![status](https://img.shields.io/badge/status-pre--tournament%20live-green)]() [![infra](https://img.shields.io/badge/infra-Hetzner%20CPX22%20%E2%82%AC5%2Fmo-orange)]() [![license](https://img.shields.io/badge/license-private-lightgrey)]()

---

## What this actually is

A **friends' Toto pool** for the FIFA World Cup 2026 pays ₪32,426 to the
top finisher. 67 players submit picks across:
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

## The pick pipeline in one picture

```
                                                  T-24h    T-60m    T-15m    T-7m  ← LOCK
                                                    │        │        │       │
            ┌────────────────┐                      └────────┴────────┴───────┘
            │ schedule/runner│  ← scheduler daemon (Python 3.12, systemd, Hetzner CPX22)
            └───────┬────────┘
                    │  per-tick: ingest → standings → daily_summary → kickoff_cards
                    │  per-window: due_jobs → batch fetch_all_odds → dispatch
                    ▼
        ┌───────────────────────────────────────────────────────────────────────┐
        │                       core/decision/build_card.py                    │
        │                                                                       │
        │   ┌─────────────┐  ┌──────────┐  ┌─────────────┐  ┌─────────────────┐│
        │   │ Dixon-Coles │  │   Elo    │  │  Market     │  │   News agent    ││
        │   │             │  │          │  │             │  │  (LLM router)   ││
        │   │ martj42 CSV │  │eloratings│  │ the-odds-api│  │  brave + api-   ││
        │   │ → fit → λh,λa  │.net → P(H,│  │ → devig →   │  │  football →     ││
        │   │             │  │ D,A)     │  │ P(H,D,A)    │  │  Gemini/Claude  ││
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
        │                       │                                              │
        │                       ▼                                              │
        │   {pick_direction, pick_exact_score, expected_points, audit_trail}   │
        └───────────────────────────────┬──────────────────────────────────────┘
                                        ▼
                            core/delivery → 📲 Telegram channel
                            (📊 standings, ☀️ daily, ⚽ kickoff, 🃏 card)
```

## The 13 things that make this not toy software

| # | Engineering principle | Where it lives |
|---|---|---|
| 1 | **Never raises** — every signal loader wrapped in try/except; pipeline always produces a card | `core/decision/build_card.py` |
| 2 | **Audit trail mandatory** — `signals_used` ∪ `signals_failed` = `{dc, elo, market, news}`, pinned by parametrized tests | `test_build_card.py::test_auditability_golden_rule[...]` |
| 3 | **Graceful degradation ladder** — DC+Elo+Market+News → fewer → modal pick → alert | `docs/FAILURE_MODES.md` |
| 4 | **Single source of truth for scoring** — every multiplier in `config/rules.py`, pinned cell-by-cell to Negev's server-side grid | Day-9.7 audit + `tools/audit_negev_multipliers.py` |
| 5 | **Real probabilities, not rules of thumb** — Dixon-Coles fit on 4,045 real internationals, blended with Pinnacle's devigged odds | `core/models/predict.py::score_distribution` |
| 6 | **EV-optimization** — picks the score that maximizes `P(score) × points_multiplier`, not the modal score | `core/decision/ev_optimizer.recommend` |
| 7 | **Per-provider quota guards** — every external call wrapped in `obs.external_call`; token-bucket + monthly/daily ledger; over-budget = graceful degrade | `core/obs/__init__.py`, `core/obs/cost.py` |
| 8 | **Distributed tracing in production** — every card has a `correlation_id` traceable through Honeycomb (`WHERE correlation_id="match-1489369-T-7m"`) | `core/obs/tracing.py` (Day-9.11) |
| 9 | **LLM-provider-agnostic** — router falls Gemini → Claude → OpenAI with 3-tier parse repair, fully observable | `core/llm/router.py` |
| 10 | **Concurrent dispatch** — up to 4 simultaneous group-stage kickoffs run in parallel via `ThreadPoolExecutor`, idempotency via runs ledger | `schedule/runner.py` |
| 11 | **Idempotent everywhere** — re-running ingest/scoring/standings sync is safe; runs-ledger prevents double-fire | `core/obs/runs.py`, `schedule/runner.py` |
| 12 | **600 tests, mostly offline** — every external dependency injectable; CI-friendly; one autouse fixture isolates singleton ledgers per test | `tests/conftest.py` (Day-9.22) |
| 13 | **One operator, zero ops cost** — €5/mo VM, $0/mo APIs (all free tiers), systemd auto-restart, watchdog alerts on stuck/silent | `docs/SERVER.md` |

## Tech stack

- **Python 3.12** — no async, just threads (work is I/O-bound; CPU work is microseconds)
- **SQLite** — single-file DB for matches, predictions, odds snapshots, standings, runs ledger, cost ledger
- **Firebase Firestore** — read/write to the Negev Toto app (friends pool) via a hand-rolled MCP-style connector
- **OpenTelemetry** → Honeycomb — distributed tracing
- **Long-polling Telegram Bot API** — message delivery
- **Hetzner CPX22 / Ubuntu 24.04** — €5/mo, systemd-managed daemon
- **No ML framework** — Dixon-Coles is closed-form; LLM only for news synthesis
- **No web frontend** — UX is push notifications + browsable `reports/*.md`

## Architecture at a glance

| Layer | Modules | Purpose |
|---|---|---|
| **Scheduler** | `schedule/{runner,scheduler,watchdog,daily_summary,kickoff_cards}` | Tick loop, dispatch windows, watchdog, hooks |
| **Decision** | `core/decision/{build_card,ev_optimizer,strategy,sidebets,futures}` | Per-game pick, win-the-pool tilt, side bets, futures |
| **Models** | `core/models/{dixon_coles,elo,blend,fit,predict,montecarlo}` | Goal-rate fit, Elo, blend, score matrix, MC bracket sim |
| **Data** | `core/data/{football_data,oddsapi,api_football,web_search,results_io,soccerdata_io,teams}` | All 11 external endpoints + canonicalization |
| **Agents** | `orchestrator/agents/news_agent.py` | LLM-mediated context synthesis |
| **LLM** | `core/llm/{router,providers}` | Gemini → Claude → OpenAI chain with parse repair |
| **Observability** | `core/obs/{tracing,logging,cost,runs}` | Rate limit, budget, ledger, tracing, runs |
| **Delivery** | `core/delivery/{base,channels}` | Telegram + file + console + render_card |
| **Storage** | `store/{db,repo,schema.sql}` | SQLite I/O |
| **Integrations** | `integrations/negev_toto_mcp.py` | Firestore connector + 30+ MCP tools |
| **Tools** | `tools/*.py` (40+) | Operator CLIs (audit, sync, suggest, smoke-test, …) |

## Data sources

| Source | Auth | Free quota | Used for |
|---|---|---|---|
| football-data.org | API key | 10 req/min | Match calendar, fixtures, results |
| the-odds-api.com | API key | 500 credits/mo | Decimal 1X2 + futures odds (Pinnacle preferred) |
| api-football.com | API key | 100 req/day | Confirmed XI, injuries, fixture IDs |
| brave search api | API key | 1000 req/mo | Web snippets for the news agent context |
| Negev Firestore | refresh token | unlimited | Friends pool — standings, picks, side bets |
| Google Gemini | API key | 1500 req/day | LLM primary (free tier) |
| Anthropic Claude | API key | PAYG | LLM fallback |
| OpenAI | API key | PAYG | LLM last-resort |
| eloratings.net | scrape | none | Elo ratings per nation |
| martj42 GitHub CSV | none | none | Historical international results for DC fit |
| Telegram Bot API | bot token | 1 msg/sec/chat | Output delivery |

**Total OOP cost ≈ $0/month.** Cost ledger tracks burn rate; budget-guards short-circuit before fees apply.

## Live status — 2026-06-09

- ✅ 600 tests green
- ✅ Daemon deployed: Hetzner CPX22, Falkenstein, `167.233.66.192`, systemd-managed
- ✅ Day-1 calendar live (104 fixtures ingested + detonator-tagged)
- ✅ Day-7 futures locked (Portugal / Uzbekistan / Mbappé / "Arkadi" — saved to Negev)
- ✅ Day-9.22: per-friend symmetric blocks + T+1m kickoff card + per-card picks footer
- ⏳ First T-24h card: **2026-06-10 22:00 IDT**
- ⏳ First T-7m LOCK: **2026-06-11 21:53 IDT** (Mexico v South Africa)

## Quick start

```bash
git clone <repo> mondial2026 && cd mondial2026
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in keys
PYTHONPATH=. .venv/bin/python -m schedule.runner       # local run
sudo bash infra/bootstrap.sh                           # full VM setup
```

## Inspecting the system after kickoff

40+ CLI tools, all in `tools/`. The unified entry point is `tools/toto.py`:

```bash
PYTHONPATH=. python tools/toto.py help              # list every subcommand
PYTHONPATH=. python tools/toto.py standings --n 10
PYTHONPATH=. python tools/toto.py match Mexico "South Africa"
PYTHONPATH=. python tools/toto.py player Vaadia
PYTHONPATH=. python tools/toto.py suggest Mexico "South Africa"
```

Plus the audit tools:

```bash
PYTHONPATH=. python tools/audit_martj42.py             # DC training data freshness
PYTHONPATH=. python tools/audit_negev_multipliers.py   # drift watchdog
PYTHONPATH=. python tools/audit_team_aliases.py        # cross-source name reconciliation
PYTHONPATH=. python tools/run_one_card_live.py "Mexico" "South Africa" --window T-7m
PYTHONPATH=. python tools/news_preview.py "Mexico" "South Africa"
PYTHONPATH=. python tools/llm_audit.py --hours 24      # 5-section runbook
```

## Documentation map

| Doc | Audience | What's inside |
|---|---|---|
| [CLAUDE.md](./CLAUDE.md) | dev (incl. AI sessions) | Build order, day-by-day changelog, golden rules, component matrix |
| [docs/SERVER.md](./docs/SERVER.md) | operator | Live VM ops: every .env var, SQL queries, Honeycomb queries, alert taxonomy |
| [docs/SCHEDULING.md](./docs/SCHEDULING.md) | operator | Daemon internals, hooks, safe-update procedure |
| [docs/STRATEGY.md](./docs/STRATEGY.md) | operator | Win-the-pool tilt, how to activate mid-tournament |
| [docs/BLUEPRINT.md](./docs/BLUEPRINT.md) | architect | Original system design |
| [docs/FAILURE_MODES.md](./docs/FAILURE_MODES.md) | dev | Degradation ladder per component |
| [docs/EDGE_CASES.md](./docs/EDGE_CASES.md) | dev / ops | What's tested vs not, with closing tools per gap |
| [docs/SYSTEM_ARCHITECTURE.html](./docs/SYSTEM_ARCHITECTURE.html) | anyone (browser) | Visual walkthrough of every pipeline stage |
| [docs/FUTURES_LOCK_2026.md](./docs/FUTURES_LOCK_2026.md) | operator | The 4 pre-tournament picks + analysis |
| [docs/COST_AND_LIMITS.md](./docs/COST_AND_LIMITS.md) | operator | Per-provider quotas + projected burn |

## Why these design choices

### "Why not LangChain / Agent SDK?"
Because the flow is a scheduled pipeline, not a conversational graph. Adding
an agent runtime would buy us nothing (we already have ContextVars,
tracing, retries, rate limits) and cost us debuggability. The "agent" here
is one LLM call inside a `try/except` with a 3-tier parse repair.

### "Why no vector DB / RAG?"
The data is live + structured. We need today's lineup, today's odds,
today's injuries. Retrieval-augmented anything against last-week's
documents would lose to a direct API call. The news agent calls Brave
fresh on every match window.

### "Why is the market signal weighted highest?"
Because Pinnacle is sharper than any model we can build with 4 years
of national-team data. Pinnacle aggregates the entire sharp-money pool;
Dixon-Coles aggregates a noisy historical signal. We weight the
information source by its accuracy, not its complexity.

### "Why no async?"
The work is I/O-bound but low-frequency (≤ 4 concurrent matches). Threads
+ a shared token-bucket rate limiter give us true parallelism (Python
releases the GIL during I/O) with simpler debuggability than asyncio.

### "Why SQLite?"
The whole tournament fits in ~10 MB. Postgres would add ops overhead for
zero benefit. SQLite's online `.backup` mode handles concurrent reads
during nightly snapshots.

### "Why €5/mo Hetzner instead of Lambda?"
Long-polling Telegram + a 24/7 watchdog need a persistent process; serverless
cold-starts would miss windows. Hetzner gives us 100% control, deterministic
latency, and is cheaper than a $0.001/request Lambda at our scale.

## Test coverage

```
$ pytest tests/ -q
600 passed in 21.44s
```

Every external dependency is **injectable** (`fetch=`, `read=`,
`http_get=`) so the entire test suite runs offline with zero API credits.

## Contributing

Currently a single-operator project. If you're a future LLM session: read
[CLAUDE.md](./CLAUDE.md) §"Onboarding a new LLM session" first.

## License

Private. Not for redistribution.
