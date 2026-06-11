"""Day-9.24: post-fire card audit — verify EVERYTHING worked.

After a window fires for a match, runs the full backend verification:

  1. Runs ledger      — did the job fire? status? duration? card_delivered?
  2. Predictions table — was the card persisted? show the full payload
  3. Re-rendered card  — exactly what got rendered into Telegram
  4. API call ledger   — every provider called in the correlation_id window
                         (helps see what odds_api/api-football/Brave/LLM did)
  5. Signal audit      — signals_used vs signals_failed + failure_reasons
  6. News audit        — provider, fallbacks, parse_tier, ctx_failures, brave_gate
  7. Feature flags     — friend_picks_section present? per_person_section present?
  8. Honeycomb hint    — the exact query to copy into Honeycomb for a trace view
  9. Journal hint      — the journalctl filter to read for that dispatch
 10. Anomaly flags     — auto-flag any concerning row (zero deltas, parse fail, etc.)

Pure local DB read. 0 API credits.

  PYTHONPATH=. .venv/bin/python tools/audit_fired_card.py 537327 T-24h
  PYTHONPATH=. .venv/bin/python tools/audit_fired_card.py --match Mexico T-24h
  PYTHONPATH=. .venv/bin/python tools/audit_fired_card.py --latest
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _banner(title: str):
    print(f"\n  ── {title} ──")


def _ok(b: bool) -> str:
    return "✓" if b else "✗"


def _resolve_match_id(conn, match_id: str | None, match_name: str | None
                       ) -> tuple[int | None, str, str]:
    """Return (match_id, home, away) or (None, '', '') if not found."""
    if match_id:
        try:
            mid = int(match_id)
        except ValueError:
            return None, "", ""
        row = conn.execute(
            "SELECT match_id, home, away FROM matches WHERE match_id=?",
            (mid,)).fetchone()
    elif match_name:
        q = match_name.lower()
        row = conn.execute(
            "SELECT match_id, home, away FROM matches "
            "WHERE LOWER(home) LIKE ? OR LOWER(away) LIKE ? "
            "ORDER BY utc_kickoff DESC LIMIT 1",
            (f"%{q}%", f"%{q}%")).fetchone()
    else:
        return None, "", ""
    return (row[0], row[1], row[2]) if row else (None, "", "")


def _find_latest_fire(conn_obs):
    """Find the most recent fired card across all matches × windows."""
    try:
        row = conn_obs.execute(
            "SELECT match_id, window FROM runs "
            "WHERE match_id > 0 AND status IN ('ok', 'failed') "
            "ORDER BY started_at DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None, None
    return (row[0], row[1]) if row else (None, None)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="audit_fired_card")
    p.add_argument("match_id", nargs="?",
                   help="Local match_id (integer). Use with window.")
    p.add_argument("window", nargs="?",
                   help="Window label (T-24h / T-60m / T-15m / T-7m)")
    p.add_argument("--match", default=None,
                   help="Find match by team-name substring (instead of match_id)")
    p.add_argument("--latest", action="store_true",
                   help="Auto-find the latest fire across all matches")
    args = p.parse_args(argv)

    from store.db import connect
    conn = connect()
    from core.obs.runs import runs
    led = runs()

    if args.latest:
        mid, window = _find_latest_fire(led.conn)
        if not mid:
            print("\n  No fired cards found in runs ledger yet.")
            return 1
        row = conn.execute(
            "SELECT home, away FROM matches WHERE match_id=?", (mid,)).fetchone()
        home, away = (row[0], row[1]) if row else ("?", "?")
    else:
        if not args.window:
            print("Specify <match_id> <window>, OR --match X <window>, OR --latest")
            return 2
        mid, home, away = _resolve_match_id(conn, args.match_id, args.match)
        if not mid:
            print(f"\n  ✗ Match not found.")
            return 1
        window = args.window

    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  Post-fire audit: {home} vs {away}  •  window={window}")
    print(f"  ║  match_id={mid}  •  now={datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  ╚════════════════════════════════════════════════════════════╝")

    cid = f"match-{mid}-{window}"

    # ─── 1. Runs ledger ───
    _banner("1. Runs ledger")
    try:
        r = led.conn.execute(
            "SELECT status, started_at, finished_at, card_delivered, detail "
            "FROM runs WHERE match_id=? AND window=? "
            "ORDER BY started_at DESC LIMIT 1", (mid, window)).fetchone()
    except sqlite3.OperationalError as e:
        print(f"  ✗ runs table read failed: {e}")
        return 2
    if not r:
        print(f"  ⏳ NOT YET FIRED for match {mid} window {window}.")
        print(f"     The daemon hasn't dispatched this job yet — check "
              f"`tools/show_schedule.py --match {home}` to see when it's due.")
        return 0
    status, started_at, finished_at, card_delivered, detail = r
    dur = "-"
    if started_at and finished_at:
        try:
            d = (datetime.fromisoformat(finished_at)
                 - datetime.fromisoformat(started_at)).total_seconds()
            dur = f"{d:.1f}s"
        except (ValueError, TypeError):
            pass
    print(f"  status:          {_ok(status == 'ok')} {status}")
    print(f"  started_at:      {started_at}")
    print(f"  finished_at:     {finished_at or '(in-progress?)'}")
    print(f"  duration:        {dur}")
    print(f"  card_delivered:  {_ok(bool(card_delivered))} {card_delivered}")
    if detail:
        print(f"  detail:          {detail[:140]}")

    # ─── 2. Predictions table ───
    _banner("2. Predictions table")
    pred = conn.execute(
        "SELECT created_at, pick_dir, pick_h, pick_a, modal_h, modal_a, "
        "expected_points, payload_json FROM predictions "
        "WHERE match_id=? AND window=? ORDER BY created_at DESC LIMIT 1",
        (mid, window)).fetchone()
    if not pred:
        print(f"  ✗ No card persisted for (match {mid}, window {window}).")
        print(f"     Did build_card succeed? Check journalctl below.")
        return 1
    created, pdir, ph, pa, mh, ma, ep, payload = pred
    print(f"  created_at:      {created}")
    print(f"  pick_direction:  {pdir}")
    print(f"  pick_exact:      {ph}-{pa}    modal: {mh}-{ma}")
    print(f"  expected_points: {ep}")
    try:
        card = json.loads(payload)
    except (TypeError, ValueError):
        card = {}
        print(f"  ⚠ payload_json invalid")

    # ─── 3. Rendered card body ───
    _banner("3. Rendered card body (what landed in Telegram)")
    if card:
        from core.delivery.base import render_card
        body = render_card(card)
        for ln in body.splitlines():
            print(f"    {ln}")
        print(f"\n    [chars: {len(body)} / 4096 cap]")
        if len(body) > 4096:
            print(f"    🛑 OVERFLOWED Telegram cap")

    # ─── 4. Cost-ledger calls under this correlation_id ───
    _banner("4. API calls (correlation_id) — what providers were hit")
    try:
        rows = led.conn.execute(
            "SELECT provider, endpoint, ts, units, status_code, "
            "error_class FROM api_calls WHERE correlation_id=? "
            "ORDER BY ts LIMIT 20", (cid,)).fetchall()
    except sqlite3.OperationalError as e:
        rows = []
        print(f"  (api_calls read failed: {e})")
    if not rows:
        print(f"  (no api_calls rows tagged with correlation_id={cid!r})")
    else:
        print(f"  {len(rows)} call(s):")
        for prov, ep_, ts, units, status_code, err in rows:
            err_s = f"  ✗ {err}" if err else ""
            print(f"    {ts[:19]}  {prov:<14} {ep_:<22} units={units}"
                  f"  HTTP={status_code or '-'}{err_s}")

    # ─── 4b. Scoring-table audit (Day-9.25) ───
    _banner("4b. Scoring-table audit (correct grid selected?)")
    stage = (card.get("stage") or "?")
    stable = card.get("scoring_table")
    xm = card.get("exact_multiplier_used")
    print(f"  match.stage:               {stage!r}")
    print(f"  scoring_table chosen:      {stable!r}  "
          f"(expected: 'group'|'ko'|'final' via STAGE_TYPE[stage])")
    print(f"  exact_multiplier for pick: {xm}  "
          f"(table cell for pick_exact_score)")
    # Cross-check: re-derive from config + show whether it matches.
    try:
        from config.rules import STAGE_TYPE, SCORE_TABLE, TABLE_CAP
        expected_stable = STAGE_TYPE.get(stage)
        if stable != expected_stable:
            print(f"  ⚠ DRIFT: card has scoring_table={stable!r} but "
                  f"STAGE_TYPE[{stage!r}]={expected_stable!r}")
        # Also show what the FULL row looks like for human verification
        if expected_stable and ph is not None and pa is not None:
            w, l = max(int(ph), int(pa)), min(int(ph), int(pa))
            from core.scoring.engine import exact_multiplier as _xm
            recomputed = _xm(expected_stable, w, l)
            cap = TABLE_CAP.get(expected_stable)
            print(f"  recomputed multiplier:     {recomputed}  "
                  f"(cap for {expected_stable!r} = {cap})")
            if xm is not None and abs(float(xm) - float(recomputed)) > 1e-9:
                print(f"  ⚠ STAMPED vs RECOMPUTED differ — audit chain broken")
    except Exception as e:                                  # noqa: BLE001
        print(f"  (cross-check failed: {type(e).__name__}: {e})")

    # ─── 5. Signal audit ───
    _banner("5. Signal audit (the auditability rule)")
    sig_used = card.get("signals_used") or []
    sig_failed = card.get("signals_failed") or []
    reasons = card.get("failure_reasons") or {}
    print(f"  signals_used:   {sig_used}")
    print(f"  signals_failed: {sig_failed}")
    for s in sig_failed:
        print(f"    ✗ {s}: {(reasons.get(s) or '?')[:80]}")
    covered = set(sig_used) | set(sig_failed)
    expected = {"dixon_coles", "elo", "market", "news"}
    missing = expected - covered
    if missing:
        print(f"  ⚠ AUDITABILITY VIOLATION: {missing} not in used∪failed")

    # ─── 6. News audit ───
    _banner("6. News audit")
    print(f"  provider:        {card.get('news_provider')!r}")
    print(f"  fallbacks_used:  {card.get('news_fallbacks_used') or []}")
    print(f"  parse_tier:      {card.get('news_parse_tier')!r}")
    print(f"  brave_gate:      {card.get('news_brave_gate')!r}")
    print(f"  ctx_failures:    {card.get('news_ctx_failures') or []}")
    print(f"  home_delta:      {card.get('news_home_delta')}"
          + (" (clamped)" if card.get('news_home_delta_clamped') else ""))
    print(f"  away_delta:      {card.get('news_away_delta')}"
          + (" (clamped)" if card.get('news_away_delta_clamped') else ""))
    print(f"  confidence:      {card.get('news_confidence')!r}"
          + (" (defaulted)" if card.get('news_confidence_was_defaulted') else ""))
    print(f"  failure:         {card.get('news_failure')!r}")
    if card.get('news_raw_excerpt'):
        print(f"  raw_excerpt:     {card['news_raw_excerpt'][:120]!r}")

    # ─── 7. Day-9.22 + 9.24 feature flags ───
    _banner("7. Day-9.22/9.24 features present?")
    fps = card.get("friend_picks_section")
    pps = card.get("per_person_section")
    pps_list = card.get("per_person_suggestions")
    print(f"  friend_picks_section: {_ok(bool(fps))} "
          f"{'(rendered)' if fps else '(none — no FRIEND_PARTICIPANTS or Negev failed)'}")
    print(f"  per_person_section:   {_ok(bool(pps))} "
          f"{'(rendered)' if pps else '(none — no STRATEGY_OVERRIDES or modal-fallback)'}")
    if pps_list:
        print(f"  per_person_suggestions ({len(pps_list)} row(s)):")
        for s in pps_list:
            print(f"    👤 {s['name']:<14} tilt={s['tilt']}  rank={s.get('rank')}  "
                  f"pick={s['pick_exact_score']}  EV={s['expected_points']:.2f}")

    # ─── 8. Honeycomb hint ───
    _banner("8. Honeycomb deep-dive")
    print(f"  Filter:   WHERE correlation_id=\"{cid}\"")
    print(f"  See:      run → stage:news → api-football / brave / gemini calls")

    # ─── 9. Journalctl hint ───
    _banner("9. Journalctl hint")
    print(f"  sudo journalctl -u mondial2026 --since '{started_at}' "
          f"--until '{finished_at or 'now'}' | grep -E 'build_card|news_agent|{mid}'")

    # ─── 10. Anomaly flags ───
    _banner("10. Anomaly flags (worth investigating)")
    flags = []
    if card.get("ev_pathway") == "modal_fallback":
        flags.append("ev_pathway=modal_fallback (no live odds)")
    if not card.get("locked_odds"):
        flags.append("locked_odds is empty")
    if card.get("news_failure"):
        flags.append(f"news_failure={card['news_failure']!r}")
    if card.get("news_parse_tier") not in (None, "strict"):
        flags.append(f"parse_tier degraded: {card['news_parse_tier']}")
    if (card.get("news_home_delta") == 0 and card.get("news_away_delta") == 0
        and card.get("news_provider")):
        flags.append("news_deltas are 0/0 (NEUTRAL — silent news failure?)")
    if sig_failed:
        flags.append(f"signals_failed={sig_failed}")
    if not card_delivered:
        flags.append("card_delivered=False (Telegram down?)")
    if status != "ok":
        flags.append(f"runs status={status!r}")
    if flags:
        for f in flags:
            print(f"  ⚠ {f}")
    else:
        print(f"  ✓ Nothing anomalous. Card fired cleanly.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
