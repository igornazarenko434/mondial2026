# Full-scale cost & rate-limit analysis

Tournament: **104 matches**, 11 Jun – 19 Jul 2026 (spans June + July → free
monthly quotas reset once mid-tournament). Each match runs 4 windows
(T-24h / T-60m / T-15m / T-7m).

## Per-provider budget (free tiers)

| Provider | Free limit | What we call | Per-match | Whole tournament | Headroom |
|---|---|---|---|---|---|
| **football-data.org** | 10 req/min (no monthly cap) | fixtures/results — **1 call returns all matches** | shared (daily) | ~150 calls total | ✅ huge |
| **The Odds API** | **500 credits/month** | 1 `/odds` call returns **all events**; cost = markets×regions | 0.3–3 credits | ~300 credits naive / **<100 batched** | ✅ under, even naive (×2 months) |
| **API-Football** | **100 req/day** | confirmed XI + injuries at T-60m/-15m | 2–4 calls | ~10–20/day peak | ✅ ≪100 |
| **LLM (Claude)** | subscription Agent SDK credit ($20/mo Pro) | news→deltas + card | ~2 calls, ~1.5k tok | ~300–400k tokens | ✅ < $5 of credit |
| **LLM (Gemini)** | 1,500 req/day (free) | fallback | ~2 calls | ~208 total | ✅ ≪ limit |

**Expected out-of-pocket: $0.** Only OpenAI pay-as-you-go would cost anything
(~$0.50 for the whole tournament if used for the 2 LLM calls/match).

## The one real constraint, and how the design removes it
**The Odds API monthly credits** is the tightest budget. Two design choices keep
it free:
1. **Batch, don't per-match.** One `/odds` call returns every upcoming event, so
   when several matches kick off together you serve them all from a single credit
   instead of one call each.
2. **Pull only near kickoff** (T-60m/-15m/-7m), never continuously.

The **cost ledger** (`core/obs/cost.py`) tracks credits used per provider per
period and **warns at 80%** (`OBS_QUOTA_WARN`), so you'll see it coming. Live
check any time:
```python
from core.obs.cost import ledger
print(ledger().quota_status("odds_api"))   # {'used':..,'budget':500,'fraction':..,'warn':..}
```

## Rate (requests/second) at scale & parallelism
The binding risk isn't volume, it's **bursts** — e.g. 4 matches all hitting T-7m
at 22:00:00. The **central token-bucket limiter** (`core/obs/ratelimit.py`),
shared across all parallel job chains, smooths these to each provider's allowed
rate (e.g. football-data 10/min). Because one bucket per provider is shared
process-wide, concurrent match jobs can't collectively breach a free-tier rate.

## Per-game cost summary
~6–12 external calls per match (1–2 batched odds, 2–4 lineup/injury, 2 LLM,
fixtures shared) → **≈ $0** on your Claude subscription + free data tiers.
