# News / Injury agent — decision playbook

The rulebook the news agent follows so its contribution is **consistent, bounded,
and auditable** — not vibes. Critically: **the agent never picks a score.** It
reads the news and outputs two small numbers — `home_goal_delta` and
`away_goal_delta` — that nudge each team's expected goals *before* the Dixon-Coles
matrix is built. The model + EV optimizer then turn the adjusted goals into the
pick. Its only job: *"given what's confirmed about this match, how much should
each team's expected goals move, and how sure am I?"*

## When it runs and for how long (the time window)
- **T-24h** — light scan: suspensions, long-term injuries, likely rotation. Low confidence.
- **T-60m (primary run)** — official line-ups publish ~1h before kickoff, so this is
  when confirmed XI / late fitness / weather are known.
- **T-15m** — quick re-confirm only (changed XI, late withdrawal).
- **T-7m (LOCK)** — **no new searching.** Use the latest result; the card is emitted.
- **Recency filter:** only consider items from the last **48 h** (`NEWS_RECENCY_HOURS`).
  Older "news" is already baked into season stats / Elo.
- **Search budget:** up to **8 results per query × 4 queries** = 32 raw per T-60m
  (Brave bills per query, not per result; more raw → better signal density after
  ranking). Per-window query counts in `config/news.py::QUERIES_PER_WINDOW`.

## The pipeline (Day-9.25 rewrite — full architecture)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  1. search_queries(home, away, window)                                    │
│     T-24h: 3 queries (squad/injuries per team + preview)                  │
│     T-60m: 4 queries (joint lineup + preview + 2 injury)                  │
│     T-15m: 2 queries (late team news + starting XI)                       │
│                                                                           │
│  2. web_search_many(queries, n=8, freshness='pw')                         │
│     • Brave HTTP call per query (5-min cache; budget gate: 90% monthly    │
│       brake + 60 req/day cap)                                             │
│     • Sleep 1.1s between queries (Brave free-tier 1 req/sec)              │
│     • Returns: list of {title, snippet[:600], url, date}                  │
│     • Initial URL-exact dedup                                             │
│                                                                           │
│  3. news_ranker.dedup_by_url_or_title  (Day-9.25)                          │
│     • Normalize: lowercase, strip query strings, strip fragments          │
│     • Title-similarity dedup on first 60 chars                            │
│     • Catches /amp/, trailing slashes, mixed-case host variants          │
│                                                                           │
│  4. news_ranker.rank_articles(deduped, home, away)  (Day-9.25)            │
│     Each article scored on:                                              │
│       +8 BOTH teams in title  /  +5 ONE team in title                    │
│       +3 BOTH teams in snippet / +2 ONE team in snippet                  │
│       +3 injury/suspension keyword (injury, ruled out, doubt, …)         │
│       +3 lineup/XI keyword (starting xi, predicted, confirmed, …)        │
│       +2 tactical keyword (must-win, low-block, rotation, …)             │
│       +3 trusted source (ESPN, Goal, Sky, Sports Mole, BBC, …)           │
│       -3 generic source (Wikipedia, Reddit)                              │
│       +2 preview-pattern title (preview, vs, prediction, team news)      │
│       +2 freshness ≤24h  /  +1 freshness ≤48h                            │
│       -2 tournament-overview title pattern                               │
│     Sort descending; stable sort preserves Brave order on ties.          │
│                                                                           │
│  5. _fmt_web_results(ranked_top_20, home, away)  (Day-9.25)              │
│     • Top-5 get LONG_SNIPPET_LEN=1200 chars (so injury detail isn't cut) │
│     • Rest get SNIPPET_LEN=600 chars                                     │
│     • Each row embeds [rank N, score K] so the LLM sees signal strength  │
│                                                                           │
│  6. gather_context — assemble full LLM input                              │
│     [MATCH: home vs away, kickoff, stage]                                │
│     [FETCHED: now; recency cap 48h]                                     │
│     [SOURCE: API-Football lineups] ...           T-60m / T-15m only      │
│     [SOURCE: API-Football injuries — home] ...   T-60m / T-15m only      │
│     [SOURCE: brave_search × N queries; M of N included after relevance   │
│      ranking; lowest included score=K]                                  │
│       - [date] [rank 1, score 20] title | snippet                        │
│       - [date] [rank 2, score 17] title | snippet                        │
│       ...                                                                │
│     Final truncation at CONTEXT_MAX_CHARS=12000 (drops LOWEST-scored     │
│     articles first, not last-in-Brave-order)                             │
│                                                                           │
│  7. LLMRouter.complete_validated(SYSTEM, prompt, _validator,             │
│                                   json_mode=True, max_tokens=4096)        │
│     (Day-9.25) Semantic-failure cascade:                                 │
│       • Try gemini → if parses strict/regex_repair → return             │
│       • If gemini returns 200 but parse fails → cascade to claude         │
│       • If claude fails too → cascade to openai                         │
│       • All providers exhaust → raise AllProvidersFailed                 │
│     Each provider records error_class + error_message in                │
│     last_fallback_errors for full audit visibility.                     │
│                                                                           │
│  8. _validate_and_clamp(parsed)                                          │
│     Deltas clamped to ±0.6 (DELTA_CLAMP).                                │
│     Surface every silent degradation:                                    │
│       home_delta_raw, home_delta_clamped, confidence_was_defaulted,     │
│       notes_truncated, schema_error                                      │
└──────────────────────────────────────────────────────────────────────────┘
```

## What it searches (sources, priority order)

1. **API-Football** — `/fixtures/lineups`, `/injuries` per team (T-60m + T-15m
   only; lineups don't exist at T-24h)
2. **Brave Search** — date-stamped queries with `freshness='pw'` (past week).
   Results are ranked by `news_ranker.rank_articles`, NOT taken in raw Brave order.

`news_agent.search_queries(home, away, window)` generates the exact query list.

### Trusted sources (auto-boosted +3)

ESPN, Goal, Sky Sports, BBC, Guardian, Fox Sports, Sports Mole, AS, Marca,
Sofascore, Transfermarkt, RotoWire, FourFourTwo, FIFA, UEFA, Athletic. Full
list in `news_ranker.TRUSTED_SOURCES`.

### Lightly-downweighted sources (-3)

Wikipedia overviews, Reddit threads. These tend to be reference articles
rather than actionable team news. They aren't excluded — they're just
demoted so they get dropped first when the context cap bites.

### Team aliases (so short forms match canonical names)

```
South Korea       ↔ Korea / KOR / Republic of Korea
United States     ↔ USA / US / United States of America
Czechia           ↔ Czech Republic / Czech
South Africa      ↔ Bafana
Cape Verde        ↔ Cape Verde Islands / Cabo Verde
Saudi Arabia      ↔ Saudi
New Zealand       ↔ NZ / All Whites
Bosnia & Herzegovina ↔ Bosnia / Herzegovina / BIH
```

Full list in `news_ranker._team_aliases`.

## How findings map to goal-deltas (the rubric)

Apply per team, then **sum**, then **clamp to [-0.6, +0.6]**. Deliberately modest —
the model and market do the heavy lifting; news only tilts.

| Finding | Delta |
|---|---|
| Key striker / top scorer **out** (injury/susp.) | **-0.30 to -0.45** to that team |
| Important attacker / playmaker out | -0.15 to -0.30 to that team |
| First-choice keeper or 2+ key defenders out | **+0.15 to +0.30 to the OPPONENT** |
| Squad rotation — already qualified / dead rubber | **-0.20 to -0.40** to that team |
| Must-win / win-and-through motivation | +0.05 to +0.15 to that team |
| Star attacker returns / confirmed fit | +0.10 to +0.25 to that team |
| Heavy rain / extreme heat / high altitude | -0.10 to -0.20 to **both** |
| Manager confirms defensive / low-block setup | -0.10 to -0.15 to that team |
| Nothing material / normal strongest XI | **0.0** |

**Confidence** (`low|medium|high`): `high` only when the XI is confirmed from a
primary source; `medium` for a strong predicted XI; `low` for rumor or pre-T-60m
scans. Low-confidence findings sit at the small end of a range.

## Token-budget chain (Day-9.25 tuning)

| Limit | Value | Notes |
|---|---|---|
| Brave Search free monthly | 1000 reqs | ~4-7 reqs/match × 104 matches = ~600-700 |
| Brave daily cap | 60 reqs | soft brake |
| Brave per query | 8 results | Brave allows up to 20; bills per query not result |
| Top-K long snippets | 5 × 1200 chars = 6000 | most relevant articles keep detail |
| Mid-pack snippets | 15 × 600 chars = 9000 | bounded |
| Total context cap | **12000 chars (≈ 3000 tokens)** | Gemini Flash supports 1M tokens — we use **0.3%** |
| LLM output cap | `max_tokens = 4096` | typical 500-1500 tokens; never truncates verbose `discarded_sources` |
| Gemini daily | 1500 calls/day | typical 4-7 calls/match-day |
| Claude / OpenAI | PAYG | cascade fallback only — fires when Gemini parse fails |

## Guardrails (enforced in code, not just the prompt)

- Deltas hard-clamped to ±0.6 in `news_agent._validate_and_clamp`.
- On any LLM failure / bad JSON across the whole chain → **neutral (0, 0)** via
  `analyze_safe`, so a pick always goes out on model + odds alone. News can never
  block or crash a card.
- The agent must justify each adjustment in `notes[]` (e.g. "Norway: 6 starters
  benched per manager presser") so every delta is traceable on the card.
- Every silent degradation (clamp, default, schema error) surfaces a flag on
  the output: `home_delta_clamped`, `confidence_was_defaulted`, `delta_parse_error`,
  `notes_format_error`, `schema_error`.

## Worked examples (production data, 2026-06-11)

### Mexico v South Africa T-24h (rubric trigger fires)

```
brave_search × 3 queries → 20 of 20 included after relevance ranking;
lowest included score=5

rank 1, score 20  Preview: Mexico vs South Africa — Sports Mole
rank 2, score 20  Mexico vs. South Africa — World Cup Preview
rank 3, score 17  FIFA Statistical Preview "No missing players"
rank 4, score 17  FIFA World Cup 2026 official
rank 5, score 17  Yahoo Sports lineups
rank 6-8, score 13-14  Predictions/Bets
rank 9-17, score 5-13  Generic tournament info
```

LLM output:
```
provider:        gemini
parse_tier:      strict
home_goal_delta: +0.10  (Mexico — host opener motivation)
away_goal_delta:  0.00
confidence:      medium
notes: ["Mexico: host nation playing opening match, strong motivation",
        "No material player injuries or absences reported"]
discarded: ["rank 3: Malagon/Ruiz already missed squad, not playing today",
            "rank 9-14: general tournament info, not match-specific"]
```

Rubric line triggered: **"Must-win motivation: +0.05 to +0.15"**. Mexico is the
host nation playing the opener — textbook must-win. Gemini chose +0.10
(middle of the range).

### South Korea v Czechia T-24h (cascade kicks in)

Gemini was overloaded (HTTP 503 "high demand"). The router's `complete_validated`
caught the transport error and **automatically cascaded to Claude**, which received
the SAME ranked context and returned cleanly:

```
WARNING llm.router: provider 'gemini' raised (ServerError): 503 UNAVAILABLE;
        falling back

provider:        claude   ← cascade succeeded
fallbacks_used:  ['gemini']
fallback_errors: {'gemini': {error_class: 'ServerError', message: '503 ...'}}
parse_tier:      strict
home_goal_delta: 0.0
away_goal_delta: 0.0
confidence:      low
notes: ["no confirmed XIs or specific injury/suspension details provided",
        "sources mention team news exists but no explicit player availability stated",
        "Opta odds and general previews insufficient for delta adjustment"]
```

This is a clean NEUTRAL — no rubric line fired. Claude correctly applied the
**"IF UNSURE → 0.0"** rule from the system prompt. The fact that gemini's outage
didn't break the card is the **Day-9.25 cascade in action**.

## Inspection tools (live, on the VM)

### `tools/news_inspect.py` — per-card forensic deep dive

```bash
sudo -u mondial bash -c '
  cd /home/mondial/mondial2026
  set -a && source .env && set +a
  PYTHONPATH=. .venv/bin/python tools/news_inspect.py Mexico "South Africa" --window T-24h
'
```

Shows for the live match (burns ~1 LLM call + 3 Brave queries):
1. The exact Brave queries generated for the window
2. Each Brave query's results (titles + snippets + dates)
3. The ranked + scored article order
4. The full assembled context block sent to the LLM
5. The system prompt (rubric + JSON schema + examples)
6. Provider chain visited (which LLM answered + fallback errors)
7. Gemini's `notes[]` — the WHY behind each delta
8. Gemini's `discarded_sources[]` — what was seen but ignored, by rank
9. Final clamped output + how it plugs into DC's expected goals

### `tools/llm_audit.py` — chain state + per-class failures + parse tiers

```bash
sudo -u mondial bash -c 'cd /home/mondial/mondial2026 && set -a && source .env && set +a && PYTHONPATH=. .venv/bin/python tools/llm_audit.py --hours 24'
```

Five sections:
1. LLM chain state — what would run RIGHT NOW + bypass reasons
2. Per-provider ledger broken down by error class
3. Quota state with 🛑 OVER flag
4. Per-card audit with parse_tier + raw_excerpt
5. Recent raw failures with correlation_id (Honeycomb jump-off)

### `tools/audit_fired_card.py` — post-fire complete audit

After ANY card fires, the full audit trail with section 4b showing the
scoring-table choice + multiplier used.
