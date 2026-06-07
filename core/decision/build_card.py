"""Day-6 build_card — the central model→decision→audit assembler.

This is the single function the scheduler dispatches at each window. It loads
every signal, runs the model, stamps the AUDIT TRAIL on the card so a reader
can see exactly what fed the pick (and what didn't, with one-line reasons),
optionally predicts a penalty-shootout winner on knockouts with non-trivial
draw probability, and persists to the `predictions` table.

GOLDEN AUDITABILITY RULE: every signal in {dixon_coles, elo, market, news}
must appear in EITHER signals_used OR signals_failed+failure_reasons —
silent bypass is a bug. Pinned by test_build_card.

NEVER RAISES (CLAUDE.md golden rule #10). Loaders are wrapped in try/except;
on total failure we still return a (degraded) renderable card so the pipeline
can deliver it with the right alert annotations.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo
import os

from config.rules import DRAW_PEN_THRESHOLD
from core.data.teams import normalize
from core.data.results_io import historical_results
from core.data.soccerdata_io import national_team_elo, elo_of
from core.data.oddsapi import fetch_match_odds
from core.models.fit import cached_strengths, expected_goals_fn
from core.models.predict import match_card
from core.scoring.penalties import predict_shootout
from orchestrator.agents.news_agent import analyze_safe
from core.obs.logging import get_logger

log = get_logger("decision.build_card")

# Stages eligible for a penalty-winner pick — group games go to draw, not pens.
_KO_STAGES = {"R32", "R16", "QF", "SF", "3rd", "Final"}

ALL_SIGNALS = ("dixon_coles", "elo", "market", "news")


def _trim(s: str, n: int = 80) -> str:
    """Compact one-line failure reason; never blow up a card with long stacks."""
    out = " ".join(str(s).split())[:n]
    return out


def _utc_to_local(utc_iso: str | None, tz: str | None = None) -> str | None:
    """UTC ISO → local-time display string (Israel by default)."""
    if not utc_iso:
        return None
    tz_name = tz or os.environ.get("LOCAL_TZ", "Asia/Jerusalem")
    try:
        dt = datetime.fromisoformat(str(utc_iso).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def build_card(match: dict, conn=None, *,
               strengths_loader: Callable | None = None,
               elo_loader: Callable | None = None,
               odds_fetcher: Callable | None = None,
               news_analyzer: Callable | None = None,
               results_loader: Callable | None = None,
               events_cache: list | None = None,
               draw_pen_threshold: float | None = None,
               local_tz: str | None = None,
               window: str = "T-7m") -> dict:
    """Build a fully-audited card for one match. Never raises.

    Loaders are injectable for offline testing. In production each defaults
    to the real wiring (martj42 results → DC fit → strengths cache;
    eloratings.net daily cache; the-odds-api batched fetch with budget guard;
    news_agent.analyze_safe).

    events_cache lets the scheduler share ONE fetch_all_odds call across many
    matches in the same window (saves credits).

    Persists to `predictions` if conn given (upsert on (match_id, window)).
    """
    strengths_loader = strengths_loader or cached_strengths
    elo_loader       = elo_loader       or national_team_elo
    odds_fetcher     = odds_fetcher     or fetch_match_odds
    news_analyzer    = news_analyzer    or analyze_safe
    results_loader   = results_loader   or historical_results
    threshold        = draw_pen_threshold if draw_pen_threshold is not None else DRAW_PEN_THRESHOLD

    home = normalize(match.get("home")) or "Home"
    away = normalize(match.get("away")) or "Away"
    stage = match.get("stage") or "Group"
    detonator = bool(match.get("detonator", False))

    signals_used: list[str] = []
    signals_failed: list[str] = []
    failure_reasons: dict[str, str] = {}

    # ───── 1. Dixon-Coles fit → expected_goals_fn ─────
    try:
        results = results_loader()
        strengths = strengths_loader(results)
        if not strengths or not strengths.get("teams"):
            raise ValueError("empty strengths dict")
        eg_fn = expected_goals_fn(strengths)
        signals_used.append("dixon_coles")
    except Exception as e:                       # noqa: BLE001
        signals_failed.append("dixon_coles")
        failure_reasons["dixon_coles"] = _trim(e)
        eg_fn = lambda h, a: (1.3, 1.1)          # neutral fallback

    # ───── 2. Elo ratings ─────
    try:
        elo = elo_loader()
        if not elo:
            raise ValueError("empty elo dict")
        signals_used.append("elo")
    except Exception as e:                       # noqa: BLE001
        signals_failed.append("elo")
        failure_reasons["elo"] = _trim(e)
        elo = {}

    # ───── 3. Market odds (the SCORING multiplier) ─────
    try:
        scoring_odds = odds_fetcher(home, away,
                                    kickoff_utc=match.get("utc_kickoff"),
                                    events=events_cache)
        if scoring_odds and all(isinstance(scoring_odds.get(k), (int, float))
                                and scoring_odds[k] > 1.0 for k in ("H", "D", "A")):
            signals_used.append("market")
        else:
            signals_failed.append("market")
            failure_reasons["market"] = ("odds_api over budget or no event"
                                         if scoring_odds is None
                                         else "incomplete or invalid odds")
            scoring_odds = None
    except Exception as e:                       # noqa: BLE001
        signals_failed.append("market")
        failure_reasons["market"] = _trim(e)
        scoring_odds = None

    # ───── 4. News-injury deltas (Day 8 — context-gathering wired) ─────
    # At T-24h / T-60m we gather real context from API-Football + Brave Search
    # before calling analyze_safe. At T-15m we FIRST try to reuse T-60m's
    # high-confidence result from the predictions table (saves 1 LLM call + 6
    # Brave calls per match when the XI is already confirmed). At T-7m (lock)
    # and any other window we pass empty context → LLM returns NEUTRAL → news
    # still counted as "used" but with zero shift. analyze_safe NEVER raises;
    # on total LLM failure it returns NEUTRAL.
    # Day-9.11: wrap the entire news section in obs.staged('news') so api_football
    # + brave_search + LLM spans become children of `stage:news` in Honeycomb
    # under the run's correlation_id. Also captures `stage` on any escaping
    # exception so build_card can stamp news_failure_stage on the card.
    try:
        from core import obs as _obs
        _news_stage = _obs.staged("news", match_id=match.get("match_id"),
                                   window=window)
    except Exception:                                # noqa: BLE001
        from contextlib import nullcontext
        _news_stage = nullcontext()
    with _news_stage:
      try:
        from config.news import (should_search, T15M_REUSE_AGE_MIN,
                                   T15M_REUSE_MIN_CONFIDENCE)
        from orchestrator.agents.news_agent import (
            gather_context, read_prior_deltas, context_meta
        )

        # T-15m cache: reuse if T-60m was recent and confident enough
        deltas = None
        if window == "T-15m" and conn is not None:
            prior = read_prior_deltas(conn, match.get("match_id"),
                                       max_age_min=T15M_REUSE_AGE_MIN,
                                       min_confidence=T15M_REUSE_MIN_CONFIDENCE)
            if prior is not None:
                deltas = prior
                log.info("news/T-15m reused T-60m deltas for match %s "
                          "(saved 1 LLM + Brave calls)", match.get("match_id"))

        ctx_meta_snapshot = {}
        if deltas is None:                            # cache miss → run fresh
            if should_search(window):
                try:
                    context_text = gather_context(
                        {**match, "home": home, "away": away, "stage": stage},
                        window=window)
                    ctx_meta_snapshot = context_meta()   # Day-9.11
                except Exception as e:               # noqa: BLE001
                    log.warning("gather_context failed for %s vs %s (%s): %s; "
                                "using empty", home, away, type(e).__name__, e)
                    context_text = ""
            else:
                context_text = ""                    # T-7m / unknown window
            deltas = news_analyzer(home, away, context_text=context_text)

        news_deltas = (float(deltas.get("home_goal_delta", 0.0)),
                       float(deltas.get("away_goal_delta", 0.0)))
        # Stash for audit / future reuse by next-window's read_prior_deltas
        news_meta = {
            "home": news_deltas[0], "away": news_deltas[1],
            "confidence": deltas.get("confidence", "low"),
            "notes": deltas.get("notes") or [],
            "provider": deltas.get("provider"),          # which LLM answered
            "fallbacks_used": deltas.get("fallbacks_used") or [],
            "fallback_errors": deltas.get("fallback_errors") or {},  # Day-9.10
            "parse_tier": deltas.get("parse_tier"),       # Day-9.10
            "raw_excerpt": deltas.get("raw_excerpt"),     # Day-9.10
            "failure": deltas.get("failure"),
            "failure_class": deltas.get("failure_class"), # Day-9.10
            # Day-9.11: per-source context-gathering diagnostics
            "ctx_failures": ctx_meta_snapshot.get("ctx_failures") or [],
            "context_sources_ok": ctx_meta_snapshot.get("sources_ok") or [],
            "context_truncated_chars": ctx_meta_snapshot.get("context_truncated_chars") or 0,
            "context_chars": ctx_meta_snapshot.get("context_chars") or 0,
            "brave_gate": ctx_meta_snapshot.get("brave_gate"),    # Day-9.11
        }
        # Day-9.11: pass through parse+validate provenance fields. These are
        # only set when there's something interesting to report (clamp,
        # default, schema error) — None values mean "no anomaly".
        for k in ("home_delta_raw", "away_delta_raw",
                  "home_delta_clamped", "away_delta_clamped", "delta_parse_error",
                  "confidence_was_defaulted", "confidence_raw",
                  "notes_truncated", "notes_original_count", "notes_format_error",
                  "schema_error",
                  "json_mode_fallback_used", "json_mode_error_class"):
            news_meta[k] = deltas.get(k)
        # If the LLM legitimately ran but ALL providers failed, count news as
        # signals_failed so the audit trail is honest (not silently "used").
        # Day-9.11: news_failure_canonical is the SINGLE source of truth — both
        # card['news_failure'] and failure_reasons['news'] reference it, so the
        # two fields are byte-identical across success / partial / exception.
        news_failure_canonical = _trim(news_meta.get("failure") or "", 80) or None
        if news_failure_canonical:
            signals_failed.append("news")
            failure_reasons["news"] = news_failure_canonical
        else:
            signals_used.append("news")
      except Exception as e:                          # noqa: BLE001
        signals_failed.append("news")
        # Day-9.11: same canonical form in the exception branch — derive from
        # `e`, then both card['news_failure'] and failure_reasons['news']
        # share that one value.
        news_failure_canonical = _trim(e, 80) or None
        failure_reasons["news"] = news_failure_canonical
        news_deltas = (0.0, 0.0)
        # Day-9.11: stamp the stage tag captured by obs.staged on the
        # exception so we can attribute "this card failed in news stage"
        # even when the exception type alone wouldn't tell us.
        try:
            from core import obs as _obs_err
            _failure_stage = _obs_err.stage_of(e)
        except Exception:                              # noqa: BLE001
            _failure_stage = "-"
        news_meta = {"home": 0.0, "away": 0.0, "confidence": "low",
                       "notes": [], "provider": None, "fallbacks_used": [],
                       "fallback_errors": {}, "parse_tier": "never_called",
                       "raw_excerpt": None,
                       "failure": news_failure_canonical,   # canonical, Day-9.11
                       "failure_class": type(e).__name__,
                       "failure_stage": _failure_stage,
                       "ctx_failures": [], "context_sources_ok": [],
                       "context_truncated_chars": 0, "context_chars": 0}

    # ───── 5. Run the model assembler ─────
    try:
        card = match_card(home=home, away=away, stage=stage,
                          detonator=detonator,
                          expected_goals_fn=eg_fn, elo=elo,
                          scoring_odds=scoring_odds,
                          news_deltas=news_deltas)
    except Exception as e:                       # noqa: BLE001 - defensive only
        log.exception("match_card raised in build_card; returning alert card")
        card = {"home": home, "away": away, "stage": stage,
                "model_prob": {"H": 1/3, "D": 1/3, "A": 1/3},
                "pick_exact_score": {"home": 0, "away": 0},
                "pick_direction": "?",
                "modal_score": {"home": 0, "away": 0},
                "expected_points": None, "detonator": detonator,
                "locked_odds": scoring_odds, "ranked_alternatives": [],
                "note": _trim(f"match_card failed: {e}", 120)}
        if "model" not in signals_failed:
            signals_failed.append("model")
            failure_reasons["model"] = _trim(e)

    # ───── 6. Decision-branch label ─────
    card["ev_pathway"] = ("modal_fallback"
                          if card.get("expected_points") is None
                             or card.get("note")
                          else "ev_optimized")

    # ───── 7. Penalty-winner pick (KO + draw_prob >= threshold) ─────
    card["penalty_winner"] = None
    if stage in _KO_STAGES:
        draw_p = float(card.get("model_prob", {}).get("D", 0.0) or 0.0)
        if draw_p >= threshold:
            elo_h = elo_of(elo, home) if elo else 1500.0
            elo_a = elo_of(elo, away) if elo else 1500.0
            card["penalty_winner"] = predict_shootout(elo_h, elo_a)

    # ───── 8. AUDIT TRAIL — pin to the card BEFORE the golden-rule check ─────
    card["signals_used"]    = signals_used
    card["signals_failed"]  = signals_failed
    card["failure_reasons"] = failure_reasons
    # News-deltas detail (flat fields = direct queryable from SQL; also picked
    # up by next-window's read_prior_deltas for the T-15m reuse).
    card["news_home_delta"]     = news_meta.get("home", 0.0)
    card["news_away_delta"]     = news_meta.get("away", 0.0)
    card["news_confidence"]     = news_meta.get("confidence", "low")
    card["news_notes"]          = news_meta.get("notes", [])
    card["news_provider"]       = news_meta.get("provider")
    card["news_fallbacks_used"] = news_meta.get("fallbacks_used", [])
    card["news_fallback_errors"] = news_meta.get("fallback_errors", {})  # Day-9.10
    card["news_parse_tier"]     = news_meta.get("parse_tier")             # Day-9.10
    card["news_raw_excerpt"]    = news_meta.get("raw_excerpt")            # Day-9.10
    card["news_failure"]        = news_meta.get("failure")
    card["news_failure_class"]  = news_meta.get("failure_class")          # Day-9.10
    # Day-9.11: per-source context diagnostics + stage tag on exception
    card["news_ctx_failures"]   = news_meta.get("ctx_failures", [])
    card["news_context_sources_ok"]      = news_meta.get("context_sources_ok", [])
    card["news_context_truncated_chars"] = news_meta.get("context_truncated_chars", 0)
    card["news_context_chars"]  = news_meta.get("context_chars", 0)
    card["news_failure_stage"]  = news_meta.get("failure_stage")
    card["news_brave_gate"]     = news_meta.get("brave_gate")   # Day-9.11
    # Day-9.11: parse+validate provenance — every silent default / clamp /
    # truncation now visible. The full set of optional flags from
    # _validate_and_clamp passes through `deltas` into news_meta below.
    for k in ("home_delta_raw", "away_delta_raw",
              "home_delta_clamped", "away_delta_clamped", "delta_parse_error",
              "confidence_was_defaulted", "confidence_raw",
              "notes_truncated", "notes_original_count", "notes_format_error",
              "schema_error",
              "json_mode_fallback_used", "json_mode_error_class"):
        card[f"news_{k}"] = news_meta.get(k)

    # Golden auditability rule: every signal must appear somewhere. The
    # production path enforces this by construction (we visit every signal
    # exactly once above), but log loudly if some future refactor breaks it.
    covered = set(signals_used) | set(signals_failed)
    missing = [s for s in ALL_SIGNALS if s not in covered]
    if missing:
        log.error("auditability violation in build_card: missing %s", missing)

    # ───── 9. Match metadata ─────
    card["match_id"]    = match.get("match_id")
    card["window"]      = window
    card["kickoff_utc"] = match.get("utc_kickoff")
    # Always re-derive kickoff_local for a clean display format — don't trust
    # the raw ISO string football-data stored in the matches.local_kickoff col.
    card["kickoff_local"] = _utc_to_local(match.get("utc_kickoff"), local_tz)
    # Strip football-data's "GROUP_" prefix for a tighter card header.
    raw_group = match.get("group") or match.get("grp")
    if isinstance(raw_group, str) and raw_group.upper().startswith("GROUP_"):
        raw_group = raw_group[len("GROUP_"):]
    card["group"] = raw_group

    # ───── 10. Persistence ─────
    if conn is not None:
        try:
            persist_card(conn, card)
        except Exception as e:                   # noqa: BLE001
            log.error("persist_card failed for match %s: %s",
                      card.get("match_id"), e)

    return card


def persist_card(conn, card: dict) -> None:
    """Upsert one card into the predictions table. payload_json holds the
    full card so we can reconstruct everything (audit, penalty, context)
    after a restart without re-running the model."""
    pick  = card.get("pick_exact_score") or {}
    modal = card.get("modal_score")      or {}
    ev    = card.get("expected_points")
    ev_num = ev if isinstance(ev, (int, float)) else None
    conn.execute(
        "INSERT INTO predictions (match_id, created_at, window, pick_dir, "
        "pick_h, pick_a, modal_h, modal_a, expected_points, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(match_id, window) DO UPDATE SET "
        "created_at=excluded.created_at, "
        "pick_dir=excluded.pick_dir, "
        "pick_h=excluded.pick_h, pick_a=excluded.pick_a, "
        "modal_h=excluded.modal_h, modal_a=excluded.modal_a, "
        "expected_points=excluded.expected_points, "
        "payload_json=excluded.payload_json",
        (card.get("match_id"),
         datetime.now(timezone.utc).isoformat(),
         card.get("window", "T-7m"),
         card.get("pick_direction"),
         pick.get("home"), pick.get("away"),
         modal.get("home"), modal.get("away"),
         ev_num,
         json.dumps(card, default=str, ensure_ascii=False)))
    conn.commit()
