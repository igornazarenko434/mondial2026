# Connecting an LLM — what works, and what it costs (verified June 2026)

## The key fact: a chat subscription ≠ programmatic access
Building software that calls a model needs **API/SDK access**, which is billed
separately from the consumer chat apps.

| You have | Can your code use it? | Cost to run the agent layer |
|---|---|---|
| **Claude Pro / Max** | **Yes — via the Claude Agent SDK.** As of **15 Jun 2026**, Pro/Max/Team/Enterprise plans include a **monthly Agent SDK credit** (Pro ≈ $20/mo, Max 20x ≈ $200/mo) that funds the Agent SDK and `claude -p`. | **$0 extra** while you stay within the monthly credit |
| **ChatGPT Plus** | **No.** Plus only unlocks chatgpt.com. The OpenAI API is a separate, pay-as-you-go product. | requires OpenAI API credits |
| **Gemini (consumer) subscription** | **No** for that sub, **but** the **Gemini API has a free tier** (Google AI Studio key) | **$0** within free limits |
| **Anthropic API key** | Yes (pay-as-you-go) | per-token, no monthly credit |
| **OpenAI API key** | Yes (pay-as-you-go) | per-token |

Important compliance note: the OAuth credential tied to Free/Pro/Max is intended
**only** for Claude Code / claude.ai / the Agent SDK. Use it **through the Agent
SDK**, not by hand-rolling it into an unrelated service.

## Recommended setup for you (you have a Claude subscription)
This project barely touches the LLM — only the **news/injury agent** (parse text
→ structured deltas) and the **card writer**. That's a handful of small calls per
match, so cost is tiny.

1. **Primary: Claude via the Agent SDK on your Pro/Max subscription.** Authenticate
   Claude Code / the Agent SDK with your plan; the monthly Agent SDK credit covers
   this hobby usage with near-certainty. Set `CLAUDE_CODE_OAUTH_TOKEN`.
2. **Fallback: Gemini free tier.** Create a free Google AI Studio key; set
   `GEMINI_API_KEY`. Free within ~1,500 req/day (Flash) — far more than you need.
3. **Optional: OpenAI** only if you want a third fallback and don't mind a few
   cents of pay-as-you-go.

```bash
# .env
LLM_PROVIDER_CHAIN=claude,gemini      # try Claude first, fall back to free Gemini
CLAUDE_CODE_OAUTH_TOKEN=...           # from your Claude subscription
GEMINI_API_KEY=...                    # free, AI Studio
```

The router (`core/llm/router.py`) is fully model-agnostic: change one env var to
reorder providers or drop one. Missing keys/libraries are skipped automatically,
so a provider you haven't configured simply isn't used.

## Total running cost of the whole system
- **Data** (football-data.org, soccerdata, eloratings) — **free**.
- **Odds** (The Odds API free tier, 500 req/mo) — **free** if you only pull near
  kickoff.
- **Lineups/injuries** (API-Football free, 100 req/day) — **free**.
- **LLM** — **$0** on your Claude subscription credit + Gemini free tier.

→ **Expected out-of-pocket: $0** for normal use. The only way you'd pay is if you
choose OpenAI pay-as-you-go, or blow past the free quotas (the throttling and
near-kickoff scheduling in the design prevent that).

## How the agent layer plugs in (build order, day 9)
1. Install `claude-agent-sdk` (and optionally `google-genai`).
2. Define subagents (data/odds/news/model/scoring) — the news agent already calls
   `LLMRouter`; the others are deterministic Python tools the orchestrator invokes.
3. The orchestrator (Claude, via the SDK) reads the fixture calendar, spawns one
   job chain per match at T-24h/-60m/-15m/-7m, and writes the card. Async fan-out
   handles two simultaneous kickoffs in parallel.
4. Keep the deterministic pipeline runnable without the SDK (`orchestrator/run.py`)
   so you can always test the math independently of the agent wrapper.
