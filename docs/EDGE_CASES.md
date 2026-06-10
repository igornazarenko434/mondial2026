# Edge cases — what's tested, what isn't, and how to close the gaps

Living audit of the failure modes the system is exposed to vs the test
coverage we have. Authored 2026-06-09 ahead of the WC2026 opener
(2026-06-11 22:00 IDT) so we have one source of truth when something
weird shows up in a real card.

## Test coverage at a glance

- **593 tests** green as of `bbcfb91` (`pytest tests/ -q`)
- **0 live end-to-end runs** against real APIs in CI; one manual smoke
  on 2026-06-06 produced a clean Honeycomb trace (`4664ef0e…`).
- **The first 4 cards on 2026-06-10/11** (T-24h / -60m / -15m / -7m for
  the Mexico v South Africa opener) are the first time the news agent
  will run on real, live WC2026 data.

## Sources we hit (11 endpoints)

See [docs/SYSTEM_ARCHITECTURE.html](./SYSTEM_ARCHITECTURE.html) for the
visual map. Tabular form below shows test coverage per source.

| # | Source | Auth | Quota | Calling code | Tests | Live-validated? |
|---|---|---|---|---|---|---|
| 1 | football-data.org | API key | 10/min | `core/data/football_data.py` | ✅ unit | ✅ during ingest cron |
| 2 | the-odds-api.com | API key | 500/mo | `core/data/oddsapi.py` | ✅ extensive | ✅ Day-4 audit (1 credit, 72/72 events matched) |
| 3 | api-football.com | API key | 100/day | `core/data/api_football.py` | ✅ + Day-9.20 cache tests | ⚠ only via `populate_api_football_team_ids.py` — fixtures empty pre-tourney |
| 4 | brave search | API key | 1000/mo + 80/day soft cap | `core/data/web_search.py` | ✅ cache + budget guard | ⚠ only via Day-8 smoke probe |
| 5 | Negev Firestore | refresh token | unlimited (we throttle 5/sec) | `integrations/negev_toto_mcp.py` | ✅ 30+ unit | ✅ live (standings, broad bets, picks all read live) |
| 6 | Gemini | API key | 1500/day free | `core/llm/router.py::_provider_gemini` | ✅ router fallback + observability tests | ⚠ once on Day-9.11 smoke |
| 7 | Claude | API key / OAuth | PAYG | Same router | ✅ unit | ✅ Day-9.18 cross-provider scenarios |
| 8 | OpenAI | API key | PAYG | Same router | ✅ unit | ⚠ scenarios harness only |
| 9 | eloratings.net | scrape | none | `core/data/soccerdata_io.py::_fetch_eloratings` | ⚠ logic tested with fake input | ❌ scraper never live-asserted |
| 10 | martj42 GitHub CSV | none | GitHub | `core/data/results_io.py::historical_results` | ⚠ logic tested | ❌ CSV freshness never asserted |
| 11 | Telegram Bot API | bot token | 1 msg/sec/chat | `core/delivery/channels.py` | ✅ unit | ✅ every card lands |

## Edge cases — confident we handle

| Edge case | Test | Behavior |
|---|---|---|
| API-Football quota exhausted | `tests/test_api_football_caches_day920.py` | Stale-on-budget for injuries; team_id from disk cache (Day-9.20) |
| Team name variant mismatch (Czechia vs Czech Republic) | `tests/test_api_football_caches_day920.py` | 5-tier variant search |
| Brave returns 0 results | `tests/test_brave_quota_guard.py` | `gather_context` returns context without web block; LLM gets just lineups |
| Brave over monthly budget | `tests/test_brave_quota_guard.py`, `tests/test_brave_search_cache_day921.py` | Stale-on-budget; specific `brave_gate` reason stamped on card |
| LLM returns malformed JSON | `tests/test_news_observability_day911.py` | 3-tier parse: strict → regex_repair → empty; `parse_tier` field on card |
| LLM returns leading `+0.15` (Claude bug) | `tests/test_news_playbook.py` (Day-9.18) | Parser strips invalid `+`; `raw_excerpt` logged if all 3 LLMs fail |
| LLM emits markdown fences | Same | Strip ```json ``` fences anywhere |
| All 3 LLMs fail | `tests/test_news_observability_day911.py` | `AllProvidersFailed` → `analyze_safe` → NEUTRAL deltas `(0,0)` |
| LLM delta exceeds ±0.6 | `tests/test_news_playbook.py` | Clamped; `home_delta_clamped` flag stamped |
| api-football fixture endpoint empty (pre-tournament) | `tests/test_news_agent_day8.py` | Graceful None; pipeline degrades to Brave-only |
| Lineups only one team posted | `tests/test_news_agent_day8.py` | Both teams handled independently |
| Two simultaneous kickoffs | `tests/test_kickoff_cards.py::test_two_simultaneous_kickoffs_get_distinct_messages` | Each gets own message + ledger row; shared standings snapshot |
| Runs-ledger pollution across tests | `tests/conftest.py` autouse fixture (Day-9.22) | Isolated `:memory:` ledger per test |

## Edge cases — NOT confident we handle

Each row below has tooling next to it that I've shipped (or will ship in
this commit set) to close the gap.

| Risk | What's at stake | Closing tool |
|---|---|---|
| **Brave returns IRRELEVANT results** ("Brazil" → movie reviews) | LLM gets noise → bad deltas | `tools/news_preview.py` (A) |
| **api-football returns lineups but team names mismatch** | Wrong team labels on the lineup | `tools/audit_team_aliases.py` (D) |
| **Two simultaneous matches share api-football quota** | Match 2 may starve match 1's injury fetch | `tools/run_one_card_live.py` (C) + monitoring (F) |
| **Foreign-language news in Brave response** | LLM may produce non-English notes | `tools/news_preview.py` (A) — visual inspection |
| **News agent runs at T-60m + T-15m + T-7m for same match — cache reuse?** | Wasted credits if T-15m doesn't reuse T-60m's deltas | `tools/run_one_card_live.py` (C) |
| **LLM JSON missing required fields** | Defaults to 0/low silently | Existing `confidence_was_defaulted` flag — verified by news_preview |
| **Telegram message body > 4096 chars** | Telegram 400s → card never delivered | `tests/test_telegram_4096_cap.py` (B) |
| **render_card overflow + friend_picks_section** | Day-9.22 footer adds ~5-8 lines AFTER the cap | Same test (B) |
| **football-data publishes a stage code we don't map** | `RULES_STAGE` may emit None → standings_writer skip | Logged at ingest; manual mitigation |
| **Match status='POSTPONED' MID-window** | Card may lock but match doesn't play that day | Known unhandled (CLAUDE.md); manual fix: `DELETE FROM runs WHERE match_id=X` |
| **Negev side-bet schema** | `toto_submit_side_bet_answer` path is a best guess | Founder hasn't published — schema unverifiable until then |
| **martj42 CSV freshness** | If stale by months, DC fit uses out-of-date strengths | `tools/audit_martj42.py` |
| **Negev admin changes the multiplier grid mid-tournament** | Our EV optimizer would compute against the wrong payoff function | `tools/audit_negev_multipliers.py` |

## Real incident closed in Day-9.23 (2026-06-10)

**Symptom:** ☀️ daily summary at 09:00 IDT showed legacy `Your score: 0.0` line
instead of the new `Tracked 👥:` block with Vaadia's row. The 📊 standings sync
at 07:00 (via cron) worked perfectly.

**Diagnostic chain:**
1. `/proc/$PID/environ` showed `FRIEND_PARTICIPANTS=Vaadia` was present
2. `journalctl` showed `Negev fetch for tracked blocks failed: Firebase
   sign-in failed (400): INVALID_EMAIL`
3. `.env` had `NEGEV_EMAIL=igor434@gmail.com   # your Negev Toto login email`
4. **systemd's EnvironmentFile parser doesn't strip inline `#` comments** —
   so the daemon's `NEGEV_EMAIL` actually contained the comment as part
   of the value. bash's `source .env` (used by cron) does strip it, which
   is why cron worked and daemon didn't.
5. The connector's auth fall-through (refresh-token → email/password)
   masked the issue — the email auth got the malformed value.

**Permanent fixes shipped:**
| Fix | File | What |
|---|---|---|
| Strip ALL inline `#` from `.env.example` | `.env.example` | Operators copying the example never reproduce the trap |
| Loud refresh-token failure | `integrations/negev_toto_mcp.py::_id_token` | No silent fallback by default; explicit opt-in via `NEGEV_ALLOW_PASSWORD_FALLBACK=1` |
| Preflight inline-comment detector | `config/preflight.py::_detect_inline_comment_leaks` | Daemon startup logs ERROR for every leaked var |
| Daily env hygiene cron | `tools/audit_env.py` + crontab 06:50 IDT | Fires ⚠ Telegram on any leak detected OR Negev auth probe failure |
| Once-per-day Negev failure alert | `integrations/negev_alerts.alert_failure_once_per_day` | All 3 daemon call sites (daily_summary, kickoff_cards, build_card friend_picks) fire ⚠ within 24h of any silent degradation, suppressing repeat alerts same day |

## Closing actions taken in this commit set (Day-9.23)

A. **`tools/news_preview.py`** — live news_agent inspection
B. **`tests/test_telegram_4096_cap.py`** — 4096-char regression test
C. **`tools/run_one_card_live.py`** — full pipeline live exerciser
D. **`tools/audit_team_aliases.py`** — cross-source name reconciliation
E. **Worst-case render assertions** in `test_card_friend_footer.py`
F. **News-agent panel** in `tools/llm_audit.py`
G. **`tools/audit_martj42.py`** — CSV freshness + WC2026 coverage check
H. **`tools/audit_negev_multipliers.py`** — multiplier drift watchdog

## Quota awareness — what each tool costs

| Tool | api-football | the-odds-api | Brave | LLM | Negev |
|---|---|---|---|---|---|
| `news_preview.py` | 0 (cached) | 0 | 4-7 credits | 1 call (≤2k tokens) | 0 |
| `run_one_card_live.py` | 1-3 | 1 credit (batch) | 4-7 | 1 | 0 |
| `audit_team_aliases.py` | 1 per team if cold cache (48 max) | 0 (uses existing cache) | 0 | 0 | 0 |
| `audit_martj42.py` | 0 | 0 | 0 | 0 | 0 (file read) |
| `audit_negev_multipliers.py` | 0 | 0 | 0 | 0 | 1 |

Run all five for a full pre-tournament audit ≈ 50 api-football credits +
1 odds_api credit + 8 Brave credits + 1 LLM call + 1 Negev call. Comfortable
within all free tiers if api-football quota has reset.

## If something looks weird in a real card after kickoff

1. **Check `tools/llm_audit.py --hours 24`** — surfaces which provider answered, parse_tier, raw_excerpt on failures
2. **Re-run `tools/news_preview.py "<home>" "<away>"`** — shows EXACTLY what the LLM saw
3. **Check `predictions.payload_json`** for that match × window — the full audit trail (provider, fallbacks, ctx_failures, brave_gate, error_class) is persisted
4. **Check Honeycomb** with `WHERE correlation_id="match-<id>-<window>"` — full trace tree
