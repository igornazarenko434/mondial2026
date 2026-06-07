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
        # Trimmed to 4 queries to fit Brave's $5/mo = 1,000-call free credit.
        # The joint "<home> <away> lineup <date>" query already returns both
        # teams' lineup articles, so per-team duplicates were dropped. Weather
        # query removed — the impact on goal expectations is small (-0.10 to
        # -0.20 for both teams in the rubric, almost a wash) and rarely fires
        # because outdoor stadium-level forecasts aren't usually that extreme.
        qs = [
            f"{home} {away} World Cup 2026 lineup {d}",
            f"{home} {away} World Cup 2026 preview {d}",
            f"{home} injury news today World Cup 2026",
            f"{away} injury news today World Cup 2026",
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


def _parse_json_lenient(raw: str) -> tuple[dict | None, str]:
    """Three-tier JSON parser. Returns (data, tier).

    tier is one of:
      "strict"        — raw output parsed as valid JSON directly
      "regex_repair"  — first/largest {...} block parsed (LLM added prose)
      "empty"         — empty input (provider returned nothing)
      "failed"        — both tiers failed (output was non-JSON garbage)

    The tier is stamped on the card as `news_parse_tier` so the user can
    tell whether neutral deltas came from "everything ran fine, just no
    news" vs "LLM responded but the output was unparseable".
    """
    if not raw:
        return None, "empty"
    s = raw.strip()
    # Strip ```json fences (the router already does this once, defensive again)
    s = s.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(s), "strict"
    except json.JSONDecodeError:
        pass
    # Tier 2: find the largest {...} block in the text
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        try:
            return json.loads(m.group(0)), "regex_repair"
        except json.JSONDecodeError:
            pass
    return None, "failed"


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
    """LLM-driven analysis. May raise; use analyze_safe in production.

    The returned dict has a new field `provider` set to the LLM name that
    actually answered (e.g. "gemini" / "claude" / "openai") so the card
    audit trail can show which model produced the news output.

    Day-9.10 stamps additional fields for full observability:
      - parse_tier:        which of strict/regex_repair/empty/failed succeeded
      - raw_excerpt:       first 200 chars of the unparseable output (only
                            set when parse_tier is 'failed', so the user can
                            see what the LLM actually returned)
      - fallback_errors:   {provider_name: {error_class, error_message}} for
                            each upstream provider that failed BEFORE the
                            successful one — tells you WHY Gemini was
                            bypassed (RateLimitError? AuthError? Timeout?)
    """
    llm = router or LLMRouter()
    prompt = (f"Fixture: {home} (home) vs {away} (away).\n\n"
              f"Pre-match context:\n{context_text}\n\n"
              f"Return ONLY the JSON adjustment defined in the system prompt.")
    # Try strict JSON mode first; fall back to plain text + lenient parse
    try:
        raw = llm.complete(SYSTEM, prompt, json_mode=True, max_tokens=500)
    except Exception:
        raw = llm.complete(SYSTEM, prompt, json_mode=False, max_tokens=500)
    parsed, tier = _parse_json_lenient(raw)
    out = _validate_and_clamp(parsed)
    out["provider"] = getattr(llm, "last_provider", None)
    out["fallbacks_used"] = list(getattr(llm, "last_fallbacks", []) or [])
    out["fallback_errors"] = dict(getattr(llm, "last_fallback_errors", {}) or {})
    out["parse_tier"] = tier
    if tier == "failed":
        # Capture the head of the bad output so we can see why the LLM's
        # response wasn't parseable. Logged AND stamped on the card.
        excerpt = str(raw)[:200] if raw else ""
        out["raw_excerpt"] = excerpt
        log.warning("LLM output for %s vs %s could not be parsed (provider=%s); "
                    "first 200 chars: %r",
                    home, away, out["provider"], excerpt)
    return out


def analyze_safe(home: str, away: str, context_text: str,
                 router: LLMRouter | None = None) -> dict:
    """Graceful-degradation wrapper: on ANY failure, return NEUTRAL deltas so
    the pick still goes out (model-only). The pipeline MUST call this.

    On failure, the returned dict's `provider` field is None and `failure`
    holds a short reason — both surface on the card so the user knows why
    news contributed zero (vs. legitimately neutral input data).

    Day-9.10 also stamps:
      - failure_class:   exception type name (e.g. 'AllProvidersFailed')
      - fallback_errors: per-provider error_class+message map (which provider
                          died with which error before we gave up)
    """
    try:
        return analyze(home, away, context_text, router)
    except Exception as e:                            # noqa: BLE001
        log.warning("news/LLM unavailable for %s vs %s (%s: %s); neutral deltas",
                    home, away, type(e).__name__, e)
        out = dict(NEUTRAL)
        out["provider"] = None
        out["fallbacks_used"] = list(getattr(router, "last_fallbacks", []) or []) \
                                if router else []
        out["fallback_errors"] = dict(getattr(router, "last_fallback_errors", {}) or {}) \
                                  if router else {}
        out["failure"] = str(e)[:120]
        out["failure_class"] = type(e).__name__
        out["parse_tier"] = "never_called"             # didn't get past LLM call
        return out


# ─────────────────── T-15m cache reuse (cost cut) ───────────────────

_CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


def read_prior_deltas(conn, match_id: int, max_age_min: int,
                       min_confidence: str = "medium") -> dict | None:
    """Look at the most recent prior news_deltas stored on this match's card
    (predictions.payload_json from T-60m) and reuse them if recent enough +
    confident enough. Returns the deltas dict in the standard shape, or None
    if no acceptable prior exists (caller should run a fresh search).

    Saves 1 Brave query × 2 + 1 LLM call per match at T-15m when conditions
    are met (~70% of matches in a typical tournament).
    """
    if not conn or not match_id or max_age_min <= 0:
        return None
    try:
        import json
        from datetime import datetime, timezone, timedelta
        row = conn.execute(
            "SELECT created_at, payload_json FROM predictions "
            "WHERE match_id=? AND window='T-60m' "
            "ORDER BY created_at DESC LIMIT 1",
            (match_id,)).fetchone()
        if not row:
            return None
        created_at, payload = row["created_at"], row["payload_json"]
        if not payload:
            return None
        # Age check
        try:
            t = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return None
        if (datetime.now(timezone.utc) - t) > timedelta(minutes=max_age_min):
            return None
        card = json.loads(payload)
        prior = {
            "home_goal_delta": card.get("news_home_delta"),
            "away_goal_delta": card.get("news_away_delta"),
            "confidence": card.get("news_confidence", "low"),
            "notes": card.get("news_notes") or [],
            "discarded_sources": card.get("news_discarded") or [],
        }
        # Older cards may not have news_* fields broken out — fall back to
        # the nested news block if present.
        if prior["home_goal_delta"] is None:
            nb = card.get("news") or {}
            prior["home_goal_delta"] = nb.get("home_goal_delta", 0.0)
            prior["away_goal_delta"] = nb.get("away_goal_delta", 0.0)
            prior["confidence"] = nb.get("confidence", "low")
        # Confidence floor
        if _CONF_ORDER.get(prior["confidence"], 0) < _CONF_ORDER.get(min_confidence, 1):
            return None
        return _validate_and_clamp(prior)
    except Exception as e:                            # noqa: BLE001
        log.debug("read_prior_deltas failed for match %s: %s", match_id, e)
        return None
