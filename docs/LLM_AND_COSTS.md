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

## Recommended setup (cheapest path that still works)

This project barely touches the LLM — only the **news/injury agent**
(`orchestrator/agents/news_agent.py`) parses unstructured pre-match news into a
small JSON object `{home_goal_delta, away_goal_delta, confidence, notes}`. About
**3–5 calls per match × 104 matches ≈ ~400–500 calls total**, ~500–2000 input
tokens and ~50–200 output tokens each. That's structured extraction against a
fixed rubric, not reasoning, so the **small/cheap tier** of each provider is
correct — Sonnet/GPT-4o/Opus would be overkill and ~6–60× the cost.

The default chain is therefore **free-first, cheap-paid-second**:

1. **Primary: Gemini 2.5 Flash** (free tier, 1,500 req/day — we need ~5/day).
   Set `GEMINI_API_KEY` from Google AI Studio.
2. **Fallback: Claude Haiku 4.5** (pay-as-you-go, ~$0.001/1k tokens average).
   Used only when Gemini errors or rate-limits. Set `ANTHROPIC_API_KEY`
   (or `CLAUDE_CODE_OAUTH_TOKEN` if you have a Pro/Max Agent SDK credit).
3. **Last fallback: GPT-4o-mini** (pay-as-you-go, ~$0.0006/1k input). Optional.
   Set `OPENAI_API_KEY` if you want a third fallback.

```bash
# .env (default chain matches config/llm.py)
LLM_PROVIDER_CHAIN=gemini,claude,openai   # try free first, fall back to cheap paid
GEMINI_API_KEY=...                         # free, AI Studio
ANTHROPIC_API_KEY=...                      # pay-as-you-go (or CLAUDE_CODE_OAUTH_TOKEN)
# OPENAI_API_KEY=...                       # optional 3rd fallback
```

### Why these specific models (and not Sonnet/Opus/Pro)

| Picked model | Tier | What it gets us | Why not the bigger model |
|---|---|---|---|
| `gemini-2.5-flash` | small/free | Structured JSON, fast, free tier | Pro is paid and unneeded for rubric extraction |
| `claude-haiku-4-5-20251001` | small | Cheap, accurate JSON | Sonnet 4.6 is ~6× the price for the same output here; Opus is ~60× |
| `gpt-4o-mini` | small | Cheapest OpenAI with native JSON mode | GPT-4o / GPT-5 are ~30–50× the price; unnecessary |

The router (`core/llm/router.py`) is fully model-agnostic — set `CLAUDE_MODEL`,
`GEMINI_MODEL`, `OPENAI_MODEL` per provider to override defaults without code
changes. Missing keys/libraries are skipped, so an unconfigured provider is
simply skipped.

## Total running cost of the whole system
- **Data** (football-data.org, soccerdata, eloratings) — **free**.
- **Odds** (The Odds API free tier, 500 req/mo) — **free** if you only pull near
  kickoff.
- **Lineups/injuries** (API-Football free, 100 req/day) — **free**.
- **LLM** — **~$0** in the common case (Gemini covers it); **~$0.40 worst case**
  for the whole tournament if every call fell back to Haiku.

→ **Expected out-of-pocket: ~$0** for normal use. The only way you'd pay is if
you (a) blow past Gemini's 1,500 req/day (impossible at our ~5/day), (b) every
Gemini call fails so Haiku takes them all (~$0.40 tournament-wide), or (c) you
deliberately enable OpenAI as the active provider.

## How the agent layer plugs in (build order, day 9)
1. Install `claude-agent-sdk` (and optionally `google-genai`).
2. Define subagents (data/odds/news/model/scoring) — the news agent already calls
   `LLMRouter`; the others are deterministic Python tools the orchestrator invokes.
3. The orchestrator (Claude, via the SDK) reads the fixture calendar, spawns one
   job chain per match at T-24h/-60m/-15m/-7m, and writes the card. Async fan-out
   handles two simultaneous kickoffs in parallel.
4. Keep the deterministic pipeline runnable without the SDK (`orchestrator/run.py`)
   so you can always test the math independently of the agent wrapper.
