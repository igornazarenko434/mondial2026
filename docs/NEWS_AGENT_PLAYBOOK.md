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
- **Search budget:** at most **`NEWS_MAX_QUERIES` (6)** queries per match, time-boxed,
  to respect rate limits and avoid over-reading. Stop early once the XI is confirmed.

## What it searches (sources, priority order)
1. **Confirmed line-up** — API-Football lineups; FIFA official; club/federation channels; Sofascore / RotoWire predicted→confirmed XI.
2. **Injuries / suspensions** — Transfermarkt injury table; ESPN / BBC Sport team news.
3. **Context** — match importance (already qualified? must-win? dead rubber?), rest/travel days, weather/altitude/heat, manager quotes on rotation.

`news_agent.search_queries(home, away)` generates the exact query list.

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

## Guardrails (enforced in code, not just the prompt)
- Deltas hard-clamped to ±0.6 in `news_agent.analyze`.
- On any LLM failure / bad JSON → **neutral (0, 0)** via `analyze_safe`, so a pick
  always goes out on model + odds alone. News can never block or crash a card.
- The agent must justify each adjustment in `notes[]` (e.g. "Norway: 6 starters
  benched per manager presser") so every delta is traceable on the card.

## Worked example
Norway vs France, T-60m. Findings: "Norway already through, manager confirms heavy
rotation" → Norway -0.30; "Mbappé confirmed starts after a knock" → France +0.15.
Output `{home_goal_delta: -0.30, away_goal_delta: +0.15, confidence: "high",
notes: [...]}` → model lowers Norway's λ, raises France's → matrix shifts toward a
clearer France win → EV optimizer re-picks accordingly.
