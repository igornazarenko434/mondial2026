"""News / injury agent — the one LLM-driven worker (Day 8 wired).

Turns unstructured pre-match information (confirmed XI, injuries, rotation,
weather, motivation) into STRUCTURED expected-goal deltas the model applies.
The agent NEVER picks a score — it only nudges each team's expected goals,
bounded by `DELTA_CLAMP` and traceable via `notes[]`.

Day-8 pipeline per call:
  1. `gather_context(match, window)`:
     - API-Football: fixture lookup → confirmed lineups + per-team injuries
       (always fired when `should_search(window)` and the key is set)
     - Brave Search: per-window dated queries from `search_queries(...)`
       (skipped silently if no key — degrades to api-football-only)
     - Assemble into a single text block with explicit [SOURCE: …] headers
       and a [MATCH: …] header so the LLM can date-relevance-filter
     - Cap at `CONTEXT_MAX_CHARS` to keep LLM tokens bounded
  2. `analyze_safe(home, away, context_text)`:
     - LLM router (Gemini → Claude → OpenAI) with strict JSON mode
     - Three-tier JSON parse (strict → repair-mode regex → NEUTRAL)
     - Output validation: clamp deltas, downgrade-on-suspicion, default fields

Guardrails (5 layers):
  L1 query-level    : every query carries 'WC 2026' + date + stage
  L2 source-side    : API-Football is fixture-id-scoped (no cross-match);
                       Brave freshness='pw' (past week, matches recency cap)
  L3 context assembly: each block dated, snippets capped, total ≤ context cap
  L4 LLM prompt     : explicit "ignore non-2026 / different opponent" rules
  L5 output validate: clamp + anti-hallucination (both-deltas-at-cap → halve)
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.llm.router import LLMRouter
from core.obs.logging import get_logger
from config.news import (
    NEWS_MAX_QUERIES, NEWS_RECENCY_HOURS, DELTA_CLAMP, SEARCH_WINDOWS,
    QUERIES_PER_WINDOW, SNIPPET_LEN, CONTEXT_MAX_CHARS, PER_QUERY_RESULTS,
    should_search,
)

log = get_logger("news")


# ───────────────────────── System prompt (Layer 4) ─────────────────────────

SYSTEM = f"""You are a FIFA World Cup 2026 match analyst.

YOU NEVER PICK A SCORE. You read pre-match information for ONE specific fixture
and output two small numbers that nudge each team's expected goals. The model
and the bookmaker odds do the heavy lifting — your job is only to TILT.

THE FIXTURE will be supplied as the [MATCH: ...] header in the context block.
Use ONLY information that:
  - is dated within the last {NEWS_RECENCY_HOURS} hours, AND
  - refers to the 2026 World Cup (not Qatar 2022, not Euros, not friendlies), AND
  - refers to THESE specific teams in THIS specific match.

OUTPUT EXACTLY THIS JSON SCHEMA — NO OTHER TEXT, NO MARKDOWN:
{{"home_goal_delta":  <float in [-{DELTA_CLAMP}, +{DELTA_CLAMP}]>,
 "away_goal_delta":  <float in [-{DELTA_CLAMP}, +{DELTA_CLAMP}]>,
 "confidence":       "low" | "medium" | "high",
 "notes":            [<string>, ...],     // ≤5 entries, each ≤80 chars, every non-zero delta justified
 "discarded_sources":[<string>, ...]      // sources you found but ignored, with one-line reason
}}

RUBRIC (apply per team, sum per team, clamp each to ±{DELTA_CLAMP}):
  Key striker / top scorer OUT:               -0.30 to -0.45 to that team
  Important attacker out:                     -0.15 to -0.30 to that team
  1st-choice keeper / 2+ key defenders out:   +0.15 to +0.30 to the OPPONENT
  Squad rotation / qualified / dead rubber:   -0.20 to -0.40 to that team
  Must-win motivation:                        +0.05 to +0.15 to that team
  Star returns / confirmed fit:               +0.10 to +0.25 to that team
  Heavy rain / extreme heat / altitude:       -0.10 to -0.20 to BOTH
  Manager confirms low-block:                 -0.10 to -0.15 to that team
  Nothing material / normal strongest XI:     0.0

CONFIDENCE:
  "high"   = confirmed XI from a primary source AND at least one explicit news item
  "medium" = predicted XI or strong news without confirmation
  "low"    = rumor, pre-T-60m scan, or no usable signals

DO NOT:
  - use any information from years other than 2026 — discard and list it
  - use any information about a different opponent — discard unless it's a
    team-level injury that obviously carries forward
  - invent injuries without a source — every note must point to something in the
    provided context
  - move deltas beyond ±{DELTA_CLAMP} (will be clamped anyway)
  - return any text outside the JSON object

EXAMPLE 1 — Norway vs France at T-60m, primary scan, both teams' XIs confirmed:
  Context excerpt:
    [SOURCE: API-Football lineups] Norway XI: 6 rotation players vs strongest…
    [SOURCE: brave_search "Mbappé confirmed start"] "Mbappé back in XI after knock"
  Output:
  {{"home_goal_delta": -0.30, "away_goal_delta": +0.15, "confidence": "high",
    "notes": ["Norway: 6 starters rotated per manager presser",
              "Mbappé: confirmed XI, France strongest"],
    "discarded_sources": []}}

EXAMPLE 2 — Mexico vs South Africa at T-24h, no real news yet:
  Output:
  {{"home_goal_delta": 0.0, "away_goal_delta": 0.0, "confidence": "low",
    "notes": ["no usable pre-match news within recency window"],
    "discarded_sources": ["2022 friendly article skipped (out of date)"]}}

IF UNSURE → 0.0, never guess."""


NEUTRAL = {"home_goal_delta": 0.0, "away_goal_delta": 0.0,
           "confidence": "low", "notes": [], "discarded_sources": []}


# ─────────────────────────── Query generation (L1) ─────────────────────────

def _date_str(kickoff_utc: str | None) -> str:
    """UTC ISO → YYYY-MM-DD for inclusion in queries. Empty string if unparseable."""
    if not kickoff_utc:
        return ""
    try:
        return datetime.fromisoformat(str(kickoff_utc).replace("Z", "+00:00")) \
                       .astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ""


_STAGE_LABEL = {
    "Group": "group stage", "R32": "Round of 32", "R16": "Round of 16",
    "QF": "Quarter-finals", "SF": "Semi-finals", "3rd": "Third place",
    "Final": "Final",
}


def search_queries(home: str, away: str, *,
                   kickoff_utc: str | None = None,
                   stage: str | None = None,
                   group: str | None = None,
                   window: str = "T-60m") -> list[str]:
    """Return per-window queries. Each is date-stamped + 'WC 2026' tagged so
    a stale Qatar-2022 article can't outrank the current one.

    T-24h: light scan (3 queries) — long-term injuries, qualification scenarios.
    T-60m: primary (6 queries) — lineups, late injuries, weather.
    T-15m: re-confirm (2 queries) — late team news.
    """
    d = _date_str(kickoff_utc)
    stage_lbl = _STAGE_LABEL.get(stage or "", stage or "")
    grp_lbl = (f" Group {group}" if (group and stage in (None, "Group")) else "")
    yyyymm = d[:7] if d else ""

    if window == "T-24h":
        qs = [
            f"{home} World Cup 2026 squad injuries suspensions {yyyymm}",
            f"{away} World Cup 2026 squad injuries suspensions {yyyymm}",
            f"{home} vs {away} World Cup 2026{grp_lbl} {stage_lbl} preview",
        ]
    elif window == "T-15m":
        qs = [
            f"{home} {away} World Cup 2026 late team news {d}",
            f"{home} {away} World Cup 2026 starting lineup {d}",
        ]
    else:                                   # T-60m default (the primary)
        qs = [
            f"{home} starting XI World Cup 2026 vs {away} {d}",
            f"{away} starting XI World Cup 2026 vs {home} {d}",
            f"{home} {away} World Cup 2026 lineup {d}",
            f"{home} injury news today World Cup 2026",
            f"{away} injury news today World Cup 2026",
            f"{home} {away} World Cup 2026 weather forecast {d}",
        ]

    # Honour the per-window override + the global NEWS_MAX_QUERIES safety cap.
    cap = min(NEWS_MAX_QUERIES, QUERIES_PER_WINDOW.get(window, len(qs)))
    return qs[:cap]


# ─────────────────────────── Context gathering (L3) ────────────────────────

def _fmt_lineups(lineups: list[dict] | None) -> str:
    if not lineups:
        return ""
    parts = []
    for L in lineups:
        team = L.get("team", "?")
        formation = L.get("formation", "?")
        xi = ", ".join((L.get("startXI") or [])[:11])
        parts.append(f"{team} ({formation}): {xi}")
    return " | ".join(parts)


def _fmt_injuries(team_name: str, injuries: list[dict] | None) -> str:
    if not injuries:
        return f"{team_name} injuries: none reported"
    items = []
    for inj in injuries[:6]:                       # cap per team
        name = inj.get("player", "?")
        reason = inj.get("reason") or inj.get("type") or "?"
        items.append(f"{name} ({reason})")
    return f"{team_name} injuries: " + "; ".join(items)


def _fmt_web_results(results: list[dict], snippet_len: int) -> str:
    if not results:
        return ""
    rows = []
    for r in results[:8]:                          # global cap on web snippets
        title = r.get("title") or ""
        snippet = (r.get("snippet") or "")[:snippet_len]
        date = r.get("date") or "?"
        rows.append(f"- [{date}] {title} | {snippet}")
    return "\n".join(rows)


def gather_context(match: dict, window: str = "T-60m",
                   *, api_football=None, web_search_many=None,
                   now_utc: datetime | None = None) -> str:
    """Assemble the pre-match context block fed to the LLM.

    Sources tried, in priority order (each independently graceful):
      1. API-Football  /fixtures (id) → /fixtures/lineups → /injuries × 2
      2. Brave Search  on the per-window query set (if BRAVE_SEARCH_API_KEY set)

    Returns the assembled context string (≤ CONTEXT_MAX_CHARS). Empty string
    if nothing usable was found (the LLM will then output NEUTRAL).
    """
    if api_football is None:
        from core.data import api_football as api_football  # noqa: F811
    if web_search_many is None:
        from core.data.web_search import web_search_many   # noqa: F811

    home = match.get("home") or "Home"
    away = match.get("away") or "Away"
    stage = match.get("stage") or "Group"
    group = match.get("group")
    kickoff_utc = match.get("utc_kickoff") or match.get("kickoff_utc") or ""
    local_iso = match.get("kickoff_local") or ""
    nowstr = (now_utc or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%MZ")

    # --- Header (Layer 3 dating)
    parts = [
        f"[MATCH: {home} vs {away}, kickoff {kickoff_utc or local_iso}, "
        f"stage {stage}{(' Group ' + group) if group and stage == 'Group' else ''}]",
        f"[FETCHED: {nowstr}; recency cap {NEWS_RECENCY_HOURS}h]"
    ]

    # --- API-Football block (T-60m and T-15m — lineups don't exist at T-24h)
    if window in ("T-60m", "T-15m"):
        try:
            fid = api_football.find_fixture_id(home, away, kickoff_utc)
            if fid:
                lineups = api_football.fetch_lineups(fid)
                lineup_txt = _fmt_lineups(lineups)
                if lineup_txt:
                    parts.append(f"[SOURCE: API-Football /fixtures/lineups]\n{lineup_txt}")
                else:
                    parts.append("[SOURCE: API-Football /fixtures/lineups]\n"
                                  "lineup not yet published")
            else:
                parts.append("[SOURCE: API-Football]\nfixture not found in api-football "
                              "(WC 2026 season may not be populated yet)")
        except Exception as e:                       # noqa: BLE001
            log.warning("gather_context lineups failed: %s", e)
            parts.append("[SOURCE: API-Football]\nlineups source unavailable")

        # Injuries per team — best-effort, never blocks
        for side, team_name in [("home", home), ("away", away)]:
            try:
                tid = api_football.find_team_id(team_name)
                if tid:
                    inj = api_football.fetch_injuries(tid)
                    parts.append(f"[SOURCE: API-Football /injuries — {team_name}]\n"
                                  f"{_fmt_injuries(team_name, inj)}")
            except Exception as e:                   # noqa: BLE001
                log.warning("gather_context injuries failed for %s: %s",
                            team_name, e)

    # --- Brave Search block (all windows that should_search)
    try:
        qs = search_queries(home, away, kickoff_utc=kickoff_utc,
                            stage=stage, group=group, window=window)
        if qs:
            results = web_search_many(qs, n=PER_QUERY_RESULTS,
                                       snippet_len=SNIPPET_LEN)
            web_txt = _fmt_web_results(results, SNIPPET_LEN)
            if web_txt:
                parts.append(f"[SOURCE: brave_search × {len(qs)} queries]\n{web_txt}")
            else:
                parts.append("[SOURCE: brave_search]\n"
                              "(no key configured or no results)")
    except Exception as e:                           # noqa: BLE001
        log.warning("gather_context web_search failed: %s", e)

    txt = "\n\n".join(parts)
    if len(txt) > CONTEXT_MAX_CHARS:
        # Trim from the end — header + API-Football data stays, web tail is cut
        txt = txt[:CONTEXT_MAX_CHARS - 30] + "\n…(truncated)"
    return txt


# ─────────────────────────── LLM analysis (L5 validate) ────────────────────

def _clamp(x) -> float:
    try:
        return max(-DELTA_CLAMP, min(DELTA_CLAMP, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _parse_json_lenient(raw: str) -> dict | None:
    """Three-tier JSON parser: strict → regex-repair → None."""
    if not raw:
        return None
    s = raw.strip()
    # Strip ```json fences (the router already does this once, defensive again)
    s = s.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Tier 2: find the largest {...} block in the text
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _validate_and_clamp(data: dict | None) -> dict:
    """Layer-5 output guard: clamp deltas, default missing fields.

    The clamp to ±DELTA_CLAMP is the hard ceiling. Both-at-cap legitimate
    scenarios exist (e.g. weather hits both teams + one team rotates), so
    we don't second-guess via halving — that was over-aggressive. The
    auditable notes[] field is the human check.
    """
    if not isinstance(data, dict):
        return dict(NEUTRAL)
    out = dict(NEUTRAL)
    hd = _clamp(data.get("home_goal_delta", 0.0))
    ad = _clamp(data.get("away_goal_delta", 0.0))
    conf = data.get("confidence", "low")
    if conf not in ("low", "medium", "high"):
        conf = "low"

    notes = data.get("notes") or []
    if not isinstance(notes, list):
        notes = []
    notes = [str(n)[:80] for n in notes[:5]]

    discarded = data.get("discarded_sources") or []
    if not isinstance(discarded, list):
        discarded = []
    discarded = [str(s)[:120] for s in discarded[:5]]

    out["home_goal_delta"] = round(hd, 3)
    out["away_goal_delta"] = round(ad, 3)
    out["confidence"] = conf
    out["notes"] = notes
    out["discarded_sources"] = discarded
    return out


def analyze(home: str, away: str, context_text: str,
            router: LLMRouter | None = None) -> dict:
    """LLM-driven analysis. May raise; use analyze_safe in production."""
    llm = router or LLMRouter()
    prompt = (f"Fixture: {home} (home) vs {away} (away).\n\n"
              f"Pre-match context:\n{context_text}\n\n"
              f"Return ONLY the JSON adjustment defined in the system prompt.")
    # Try strict JSON mode first; fall back to plain text + lenient parse
    try:
        raw = llm.complete(SYSTEM, prompt, json_mode=True, max_tokens=500)
    except Exception:
        raw = llm.complete(SYSTEM, prompt, json_mode=False, max_tokens=500)
    parsed = _parse_json_lenient(raw)
    return _validate_and_clamp(parsed)


def analyze_safe(home: str, away: str, context_text: str,
                 router: LLMRouter | None = None) -> dict:
    """Graceful-degradation wrapper: on ANY failure, return NEUTRAL deltas so
    the pick still goes out (model-only). The pipeline MUST call this.
    """
    try:
        return analyze(home, away, context_text, router)
    except Exception as e:                            # noqa: BLE001
        log.warning("news/LLM unavailable for %s vs %s (%s); neutral deltas",
                    home, away, e)
        return dict(NEUTRAL)
