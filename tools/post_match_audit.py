"""Post-match audit: cross-check our score_match() vs Negev's official points.

For each FINISHED match in our matches table:
  1. Read MY bet from Negev (via toto_query)
  2. Wait + retry if Negev's Cloud Function hasn't computed `points` yet
     (race: match goes FT before the scoring trigger runs; typically <30s
     but worth handling defensively up to ~5 min)
  3. Compute what `core.scoring.engine.score_match()` would award given the
     actual result + locked odds + detonator flag
  4. Compare to Negev's `points` field on the bet
  5. Print a per-match line + summary; alert via Telegram if any |delta| > 0.01

Cron-friendly. Read-only; no writes. Intended to run AFTER the daily Negev
sync at 07:00 IDT (and on-demand after each evening's matches).

Usage:
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/post_match_audit.py
    '

Flags:
  --telegram   send the summary to Telegram if any discrepancy > 0.01
  --retries N  max retries per bet (default 5, ~5 min total backoff)
"""
from __future__ import annotations
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.scoring.engine import score_match
from core.obs.logging import get_logger
from core import obs
from store.db import connect
from core.data.oddsapi import latest_snapshot

log = get_logger("post_match_audit")


def _fetch_my_bet_with_retry(ntm, tid: str, match_id_negev: str,
                              max_retries: int = 5,
                              backoff_seconds: float = 30.0) -> dict | None:
    """Read my bet for this match. Retry with backoff if `processedAt` is
    still missing (Negev's Cloud Function hasn't run yet). Returns the bet
    doc or None if no bet found / never processed within budget."""
    uid = ntm._token.get("uid") or ntm._id_token() and ntm._token.get("uid")
    for attempt in range(1, max_retries + 1):
        res = ntm.toto_query("bets", "userId", "EQUAL", uid, limit=200)
        for b in res.get("results", []):
            if (b.get("tournamentId") == tid
                and (b.get("matchId") == match_id_negev
                     or str(b.get("matchId")) == match_id_negev.split("_")[-1])):
                if b.get("processedAt"):                # Cloud Function ran ✓
                    return b
                if attempt == max_retries:
                    log.warning("bet for match %s found but not yet processed "
                                "after %d attempts; using current values",
                                match_id_negev, attempt)
                    return b                            # return unprocessed
                log.info("bet for %s pending Negev scoring (attempt %d/%d) — "
                         "waiting %.0fs", match_id_negev, attempt, max_retries,
                         backoff_seconds)
                time.sleep(backoff_seconds)
                break
        else:
            return None                                 # no bet found
    return None


def audit(*, tournament_id: str | None = None,
          ntm=None,
          conn=None,
          max_retries: int = 5,
          backoff: float = 30.0) -> dict:
    """Per-match cross-check. Returns aggregate report."""
    if ntm is None:
        from integrations import negev_toto_mcp as ntm
    if conn is None:
        conn = connect()
    tid = tournament_id or os.environ.get("NEGEV_TOURNAMENT_ID", "").strip()
    if not tid:
        return {"ok": False, "error": "NEGEV_TOURNAMENT_ID not set"}

    # All finished matches in our DB
    finished = conn.execute(
        "SELECT match_id, home, away, stage, detonator, home_goals, away_goals "
        "FROM matches WHERE status='FINISHED' "
        "AND home_goals IS NOT NULL AND away_goals IS NOT NULL "
        "ORDER BY utc_kickoff DESC").fetchall()
    rows: list[dict] = []
    total_ours = total_negev = 0.0
    discrepancies = 0

    for m in finished:
        # Find the matching Negev match (by team-pair) to get its apiFixtureId
        try:
            with obs.external_call("negev_toto", "get_matches"):
                negev_ms = ntm.toto_get_matches(tournament_id=tid, limit=300)
        except Exception as e:                         # noqa: BLE001
            return {"ok": False, "error": f"Negev fetch failed: {e!s}"}
        negev_match = next((x for x in negev_ms
                             if x["home"] == m["home"] and x["away"] == m["away"]),
                            None)
        if not negev_match:
            log.warning("match %s vs %s not in Negev — skipping",
                         m["home"], m["away"])
            continue
        nmid = negev_match["match_id"]                 # '<tid>_<apifid>'

        # Read MY bet for this match
        bet = _fetch_my_bet_with_retry(ntm, tid, nmid,
                                         max_retries=max_retries,
                                         backoff_seconds=backoff)
        if not bet:
            log.info("no bet for match %s — skipping", nmid)
            continue

        # Compute what WE think the points should be
        snap = latest_snapshot(conn, m["match_id"])
        odds = {"H": (snap or {}).get("H"), "D": (snap or {}).get("D"),
                "A": (snap or {}).get("A")}
        if not all(odds.get(k) for k in ("H", "D", "A")):
            log.info("no locked odds for match %s — can't compute ours; "
                     "Negev's points stand as authoritative", m["match_id"])
            continue
        ours_pts = score_match(
            stage=m["stage"],
            pred_h=int(bet.get("homeScore") or 0),
            pred_a=int(bet.get("awayScore") or 0),
            act_h=int(m["home_goals"]),
            act_a=int(m["away_goals"]),
            odds=odds,
            detonator=bool(m["detonator"]),
        )
        negev_pts = float(bet.get("points") or 0.0)
        total_ours += ours_pts
        total_negev += negev_pts
        delta = ours_pts - negev_pts
        ok_match = abs(delta) < 0.01
        if not ok_match:
            discrepancies += 1
        rows.append({
            "match": f"{m['home']} {m['home_goals']}-{m['away_goals']} {m['away']}",
            "stage": m["stage"],
            "my_pick": f"{bet.get('homeScore')}-{bet.get('awayScore')}",
            "ours": ours_pts,
            "negev": negev_pts,
            "delta": delta,
            "ok": ok_match,
        })

    result = {
        "ok": True,
        "tournament_id": tid,
        "n_matches_audited": len(rows),
        "n_discrepancies": discrepancies,
        "total_ours": round(total_ours, 3),
        "total_negev": round(total_negev, 3),
        "total_delta": round(total_ours - total_negev, 3),
        "rows": rows,
    }
    return result


def _format_telegram(report: dict) -> tuple[str, str]:
    title = "🔍 Post-match audit"
    lines = [
        f"Matches audited: {report['n_matches_audited']}",
        f"Discrepancies (|Δ| > 0.01): {report['n_discrepancies']}",
        f"Total — ours: {report['total_ours']:.2f}  •  Negev: {report['total_negev']:.2f}  •  Δ: {report['total_delta']:+.2f}",
    ]
    if report["n_discrepancies"]:
        lines.append("")
        lines.append("Discrepant matches:")
        for r in report["rows"]:
            if not r["ok"]:
                lines.append(f"  • {r['match']}  pick {r['my_pick']}  ours={r['ours']:.2f} negev={r['negev']:.2f}  Δ={r['delta']:+.2f}")
    return title, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="post_match_audit")
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--retries", type=int, default=5)
    p.add_argument("--backoff", type=float, default=30.0)
    args = p.parse_args(argv)

    with obs.run("post_match_audit"):
        rep = audit(max_retries=args.retries, backoff=args.backoff)
    if not rep.get("ok"):
        print(f"FAILED: {rep.get('error')}", file=sys.stderr)
        return 1

    # Pretty print
    print(f"\n📋 Audited {rep['n_matches_audited']} match(es); "
          f"{rep['n_discrepancies']} discrepancy(ies); "
          f"total ours={rep['total_ours']:.2f}, negev={rep['total_negev']:.2f}, "
          f"Δ={rep['total_delta']:+.2f}\n")
    if rep["rows"]:
        print(f"  {'match':<38} {'stage':<7} {'pick':<6} {'ours':>7} {'negev':>7} {'Δ':>7}")
        for r in rep["rows"]:
            mark = "✓" if r["ok"] else "✗"
            print(f"  {mark} {r['match']:<36} {r['stage']:<7} {r['my_pick']:<6} "
                  f"{r['ours']:>7.2f} {r['negev']:>7.2f} {r['delta']:>+7.2f}")

    # Telegram only if requested AND there's a discrepancy
    if args.telegram and rep["n_discrepancies"]:
        try:
            from core import delivery
            title, body = _format_telegram(rep)
            delivery.summary(title, body)
        except Exception as e:                         # noqa: BLE001
            log.warning("Telegram send failed: %s", e)

    return 0 if rep["n_discrepancies"] == 0 else 0  # don't fail cron on Δ


if __name__ == "__main__":
    sys.exit(main())
