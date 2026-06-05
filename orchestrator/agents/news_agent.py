"""News/Injury agent — the one LLM-driven worker.

Turns unstructured pre-match news (confirmed XI, injuries, rotation, weather,
motivation) into STRUCTURED expected-goal deltas the model can apply. It NEVER
picks a score — it only nudges each team's expected goals, bounded and audited.
Follows docs/NEWS_AGENT_PLAYBOOK.md (the rubric is embedded in SYSTEM below so the
model actually applies it). Runs in the windows in config/news.py (primary T-60m).
"""
from __future__ import annotations
from core.llm.router import LLMRouter
from config.news import NEWS_MAX_QUERIES, NEWS_RECENCY_HOURS, DELTA_CLAMP, should_search

# The rubric the model must follow — mirrors docs/NEWS_AGENT_PLAYBOOK.md.
SYSTEM = f"""You are a World Cup match analyst. You do NOT pick a score. You read
ONLY confirmed pre-match news (last {NEWS_RECENCY_HOURS} hours) for ONE fixture and
output how much each team's EXPECTED GOALS should move, with justification.

Output ONLY this JSON:
{{"home_goal_delta": float, "away_goal_delta": float,
  "confidence": "low|medium|high", "notes": [string]}}

Apply this rubric per team, then sum, then keep each delta within
[-{DELTA_CLAMP}, +{DELTA_CLAMP}] (deltas are modest — the model/market do the heavy lifting):
- key striker/top scorer OUT: -0.30..-0.45 to that team
- important attacker/playmaker OUT: -0.15..-0.30
- first-choice keeper or 2+ key defenders OUT: +0.15..+0.30 to the OPPONENT
- squad rotation / already-qualified / dead rubber: -0.20..-0.40 to that team
- must-win / win-and-through motivation: +0.05..+0.15
- star attacker returns / confirmed fit: +0.10..+0.25
- heavy rain / extreme heat / high altitude: -0.10..-0.20 to BOTH
- manager confirms defensive low block: -0.10..-0.15 to that team
- nothing material / normal strongest XI: 0.0

confidence: "high" only if the XI is confirmed by a primary source; "medium" for a
strong predicted XI; "low" for rumor or an early scan. Justify every non-zero delta
in notes[] (cite what you saw). If unsure, return 0.0 — never guess."""


NEUTRAL = {"home_goal_delta": 0.0, "away_goal_delta": 0.0,
           "confidence": "low", "notes": []}


def search_queries(home: str, away: str) -> list[str]:
    """The queries the agent runs (bounded by NEWS_MAX_QUERIES). Day-8: feed these
    to the web-search / API-Football lineup+injury tools."""
    qs = [
        f"{home} vs {away} confirmed lineup today",
        f"{home} team news injuries suspensions",
        f"{away} team news injuries suspensions",
        f"{home} {away} World Cup 2026 preview rotation motivation",
        f"{home} vs {away} weather forecast kickoff",
        f"{home} {away} predicted XI sofascore",
    ]
    return qs[:NEWS_MAX_QUERIES]


def _clamp(x) -> float:
    return max(-DELTA_CLAMP, min(DELTA_CLAMP, float(x)))


def analyze(home: str, away: str, context_text: str,
            router: LLMRouter | None = None) -> dict:
    llm = router or LLMRouter()
    prompt = (f"Fixture: {home} (home) vs {away} (away).\n"
              f"Confirmed pre-match news:\n{context_text}\n\nReturn the JSON adjustment.")
    data = llm.complete_json(SYSTEM, prompt, max_tokens=500)
    for k in ("home_goal_delta", "away_goal_delta"):
        data[k] = _clamp(data.get(k, 0.0))
    data.setdefault("confidence", "low")
    data.setdefault("notes", [])
    return data


def analyze_safe(home: str, away: str, context_text: str,
                 router: LLMRouter | None = None) -> dict:
    """Graceful-degradation wrapper: if the LLM is unavailable or returns garbage,
    return NEUTRAL deltas so the pick still goes out (model-only). The pipeline
    must always call THIS — news can never block or crash a card."""
    from core.obs.logging import get_logger
    log = get_logger("news")
    try:
        return analyze(home, away, context_text, router)
    except Exception as e:  # noqa: BLE001
        log.warning("news/LLM unavailable for %s vs %s (%s); neutral deltas", home, away, e)
        return dict(NEUTRAL)
