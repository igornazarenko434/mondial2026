# Data sources — audit (verified June 2026)

All free, all checked live this month. Each has a fallback so no single source is
a hard dependency.

| Source | Use | Status (Jun 2026) | Free tier | Reliability / notes | Fallback |
|---|---|---|---|---|---|
| **football-data.org** | fixtures, results, status | ✅ active (updated 2026-05-23); code `WC`; "free forever" pledge | 10 req/min, WC included | Reliable spine. **Caveat: older WC format may lack Round-of-32 placeholders + venue.** Verify R32 on Day 1. | API-Football |
| **API-Football** (api-sports) | fixtures (full 2026, R32), lineups, injuries | ✅ active; has a dedicated "FIFA World Cup 2026" guide | 100 req/day | **Recommended PRIMARY for 2026 fixtures** (complete 104-match + R32 + lineups). | football-data |
| **The Odds API** | 1X2/odds (scoring multiplier) | ✅ active | **500 credits/mo** (credits = markets×regions; ~85 multi-market calls) | WC **sport key resolved dynamically** via `/sports` (free call) — not hard-coded. Prefer Pinnacle/Betfair. | model-only pick |
| **soccerdata** (FBref/Understat) | team/player stats, xG | ✅ maintained (v1.9.0, 2026-04-12) | free (scrape+cache) | Scraping → can break if site changes; cache aggressively. | skip enrichment |
| **eloratings.net** | national-team Elo | ✅ active (eloratings.net/2026; updated after each fixture) | free (scrape) | "Highest predictive capability" for football; ESPN-referenced. | international-football.net elo table |
| **News / lineups** | confirmed XI, injuries, weather | ✅ | free (web search + API-Football) | LLM → neutral deltas if unavailable (`analyze_safe`). | neutral deltas |

## Modeling — still current (June 2026)
- **Dixon-Coles bivariate Poisson** remains the standard interpretable baseline;
  the ρ correction still fixes the 0-0/1-0/1-1 cells where our scoring concentrates
  value. 2026 research (Bundesliga study) shows **xG-based models (Skellam +
  isotonic calibration) are competitive and even profitable**, but also reaffirms
  that **bookmaker odds are the best-calibrated signal** — which is exactly why our
  blend leans 0.50 on the de-vigged market. So our approach is current and
  defensible.
- **Future enhancement (optional):** add an xG/Skellam signal (we already pull
  FBref xG) as a 4th input to the blend. Not required for the MVP.

## Action items reflected in code
- `oddsapi.resolve_wc_key()` discovers the live WC sport key dynamically.
- Day 1 task: confirm whether football-data exposes R32 for the 48-team bracket;
  if not, use API-Football as the primary fixtures source (the `with_fallback`
  wiring already supports swapping primary/backup).
- `requirements`/clients unchanged otherwise — all sources verified working.
