"""Day-5: results → score_match → standings.

Reads finished matches from SQLite, looks up our prediction + the locked odds
snapshot, scores via core.scoring.engine.score_match, and aggregates into the
standings table. Handles the §14 -15% group-stage reset at the group→knockout
transition and §5 prize-ladder distribution at tournament end.

Public functions:
  update_standings(conn, participant=...)  — main entry point; idempotent
  compute_prize_distribution(conn, pot)    — at tournament end
  score_one_match(conn, match_row, ...)    — single-match helper, exposed
                                              for unit tests + manual scoring

Single-participant per call: the predictions table is scoped to one user
(us), so `participant` is just a label written to the standings row. Multi-
participant support would require extending the predictions schema with a
participant_id column — explicitly deferred.
"""
from __future__ import annotations
import sqlite3
from core.scoring.engine import score_match, apply_group_reset, prize_split
from core.data.oddsapi import latest_snapshot
from config.rules import STAGE_TYPE
from core.obs.logging import get_logger

log = get_logger("scoring.standings")


def score_one_match(conn: sqlite3.Connection, match_row,
                    participant: str, window: str = "T-7m") -> float | None:
    """Score one finished match for one participant. Returns points (>=0) or
    None if no prediction / no usable odds / unknown stage / non-finished
    status. Pure read over conn — no writes. NEVER raises (per CLAUDE.md
    golden rule #8 — the scheduler must keep running on a single bad row).
    """
    pred = conn.execute(
        "SELECT pick_h, pick_a FROM predictions "
        "WHERE match_id=? AND window=?",
        (match_row["match_id"], window),
    ).fetchone()
    if not pred:
        return None
    if pred["pick_h"] is None or pred["pick_a"] is None:
        return None
    snap = latest_snapshot(conn, match_row["match_id"])
    if not snap or not all(snap.get(k) for k in ("H", "D", "A")):
        return None    # no usable odds → can't score; skip
    odds = {"H": snap["H"], "D": snap["D"], "A": snap["A"]}
    try:
        return score_match(
            stage=match_row["stage"],
            pred_h=int(pred["pick_h"]), pred_a=int(pred["pick_a"]),
            act_h=int(match_row["home_goals"]), act_a=int(match_row["away_goals"]),
            odds=odds, detonator=bool(match_row["detonator"]),
        )
    except ValueError as e:
        # Unknown stage label (e.g. football-data introduces a new bracket
        # code we don't have in STAGE_TYPE yet). Skip + log — never crash.
        log.warning("score_match skipped match %s: %s",
                    match_row["match_id"], e)
        return None


def update_standings(conn: sqlite3.Connection, participant: str = "me",
                     window: str = "T-7m",
                     apply_reset_after_groups: bool = True) -> dict:
    """Recompute standings for `participant` from the current `matches`,
    `predictions`, and `odds_snapshots` tables. Idempotent — runnable as
    often as you like; same finished matches always produce the same totals.

    Behavior:
      - Sums group-stage points and knockout-stage points separately
      - If any KO match has been scored, applies the §14 -15% reset to the
        group-stage total (turn-off via apply_reset_after_groups=False)
      - Preserves futures_points (Day-7 will populate)
      - Upserts the per-participant row into `standings`

    Returns: {participant, scored_matches, group_points, knockout_points,
              futures_points, total}
    """
    rows = conn.execute(
        "SELECT match_id, stage, home_goals, away_goals, detonator "
        "FROM matches WHERE status='FINISHED' "
        "AND home_goals IS NOT NULL AND away_goals IS NOT NULL"
    ).fetchall()

    group_pts = 0.0
    ko_pts = 0.0
    scored = 0
    for m in rows:
        pts = score_one_match(conn, m, participant=participant, window=window)
        if pts is None:
            continue
        scored += 1
        stype = STAGE_TYPE.get(m["stage"], "group")
        if stype == "group":
            group_pts += pts
        else:
            ko_pts += pts

    if apply_reset_after_groups and ko_pts > 0:
        group_pts = apply_group_reset(group_pts)

    # Day-9.27: NEVER clobber the Negev-sourced row.
    # `sync_negev_standings` writes the authoritative app-exact totals to
    # `participant=<MY_PARTICIPANT>` (e.g. "Igor"). The local writer here
    # (which runs on EVERY 60-second daemon tick) was overwriting those
    # values with score_match-computed numbers from the LOCAL predictions
    # table — which is either empty (=> 0/0) or based on the EV-optimal
    # pick (which can differ from the user's REAL Negev pick). The result
    # was Igor's group_points being reset to 0 every minute, undoing the
    # 07:00 Negev sync within seconds.
    #
    # Fix: skip the upsert when EITHER
    #   (a) scored_matches == 0 (nothing to write), OR
    #   (b) a Negev-sourced row already exists for this participant
    #       (detect via the side_points column being non-NULL OR the row
    #        having any non-zero category — both signal the Negev sync ran).
    # The local writer's data is still computed + returned in the dict so
    # post-match audits + observability still see the value, just NOT
    # written to the standings table.
    existing = conn.execute(
        "SELECT futures_points, side_points, group_points, knockout_points "
        "FROM standings WHERE participant=?", (participant,)
    ).fetchone()
    futures = float(existing["futures_points"]) if existing else 0.0
    side    = float(existing["side_points"] or 0) if existing else 0.0
    total = round(group_pts + ko_pts + futures + side, 3)

    has_negev_row = bool(existing) and (
        (existing["side_points"] is not None and existing["side_points"] > 0)
        or (existing["futures_points"] or 0) > 0
        or (existing["group_points"] or 0) > 0
        or (existing["knockout_points"] or 0) > 0)

    if scored == 0 or has_negev_row:
        log.info("standings (local) computed for %s but NOT written "
                  "(scored=%d, has_negev_row=%s) — Negev sync is source of truth",
                  participant, scored, has_negev_row)
    else:
        conn.execute(
            "INSERT INTO standings (participant, group_points, knockout_points, "
            "futures_points, side_points) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(participant) DO UPDATE SET "
            "group_points = excluded.group_points, "
            "knockout_points = excluded.knockout_points",
            (participant, round(group_pts, 3), round(ko_pts, 3), futures, side))
        conn.commit()
        log.info("standings updated (local writer): %s",
                  {"participant": participant, "group_points": round(group_pts, 3),
                   "knockout_points": round(ko_pts, 3)})

    out = {"participant": participant, "scored_matches": scored,
           "group_points": round(group_pts, 3),
           "knockout_points": round(ko_pts, 3),
           "futures_points": round(futures, 3),
           "side_points": round(side, 3), "total": total,
           "written_to_db": not (scored == 0 or has_negev_row)}
    return out


def compute_prize_distribution(conn: sqlite3.Connection,
                                total_pot: float,
                                n_ranked: int = 10) -> list[dict]:
    """At tournament end: rank participants by total points (futures included),
    apply the §5 prize ladder. Returns ranked list with per-place prize amounts.
    """
    rows = conn.execute(
        "SELECT participant, group_points, knockout_points, futures_points, "
        "COALESCE(side_points, 0) AS side_points, "
        "(group_points + knockout_points + futures_points "
        " + COALESCE(side_points, 0)) AS total "
        "FROM standings "
        "ORDER BY total DESC, participant ASC"      # stable order on ties
    ).fetchall()
    ladder = prize_split(total_pot, n_ranked=n_ranked)
    out = []
    for i, r in enumerate(rows, start=1):
        out.append({
            "rank": i, "participant": r["participant"],
            "group_points": r["group_points"],
            "knockout_points": r["knockout_points"],
            "futures_points": r["futures_points"],
            "total": round(r["total"], 3),
            "prize": ladder.get(i, 0.0),
        })
    return out
