"""Symmetric per-participant renderers.

WHAT
====

Two render functions used by every Telegram message that mentions a player:

  • `render_block(rows, name)` — a 5-7 line audit block. Used in the 📊
    standings summary header and once-per-day-at-09:00 ☀️ summary so each
    tracked person (you + every name in FRIEND_PARTICIPANTS) gets the EXACT
    same level of detail: rank, total, group/futures split, gap to leader,
    gap to second, gap to you.

  • `render_compact(rows, name)` — a one-line row for budget-tight messages
    (the ☀️ summary's "Tracked 👥" footer, the ⚽ kickoff card's standings
    line). ≤90 chars on mobile.

  • `render_match_picks_block(picks, my_pred, tracked, home, away)` — what
    each tracked person predicted for ONE match. Used in the T+1m kickoff
    card and as a footer on T-60m/-15m/-7m cards once picks are visible.

  • `tracked_participants()` — single source of truth for "who do we render
    in these blocks": `[MY_PARTICIPANT, *FRIEND_PARTICIPANTS]`, dedup
    preserving declaration order. Adding a friend is a one-line .env edit.

WHY
===

Without this module, six Telegram-rendering files would duplicate the same
rank/gap math. Drift inevitable. With it, "show futures-points split too"
is one edit in one place.

ROW SHAPE
=========

The Negev standings row shape produced by `toto_get_standings()`:
    {player: str, rank: int, total: float, direction: float, broad: float,
     exactCount: int, role: str, uid: str}

`direction` is Negev's combined group+KO direction points (their data
model collapses them); `broad` is futures.  When reading rows from our
LOCAL standings table instead (group_points, knockout_points,
futures_points), call `from_db_row(...)` first to adapt.
"""
from __future__ import annotations
import os


# ───────────────────────── env / identity ─────────────────────────

def my_participant() -> str:
    """The display name that represents YOU. Falls back to 'me'."""
    return (os.environ.get("MY_PARTICIPANT") or "me").strip() or "me"


def friend_participants() -> list[str]:
    """Friends to also render. Order = declaration order in .env.
    Empty/whitespace items dropped."""
    raw = os.environ.get("FRIEND_PARTICIPANTS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def tracked_participants() -> list[str]:
    """[you, *friends] — what every message renders blocks for. Dedup
    preserves first-occurrence order so YOU always come first."""
    seen: set[str] = set()
    out: list[str] = []
    for name in [my_participant(), *friend_participants()]:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


# ───────────────────────── row lookup / leader math ─────────────────────────

def _find(rows: list[dict], name: str) -> dict | None:
    return next((r for r in rows if r.get("player") == name), None)


def _humans(rows: list[dict]) -> list[dict]:
    """Bot-filtered for honest leader/second math (Day-9.15 pattern)."""
    return [r for r in rows if r.get("role") != "bot"]


def from_db_row(row: dict | None) -> dict | None:
    """Adapt a LOCAL standings-table row (group_points/knockout_points/
    futures_points + participant) to the Negev row shape used by
    render_block. Returns None if row is None.

    Note: rank is NOT computed here — local DB rows don't carry it. The
    caller is responsible for passing rows = a list of ALL local rows
    pre-sorted with rank set if it needs render_block. Use Negev rows
    directly when possible (the rank there matches the app's number)."""
    if not row:
        return None
    return {
        "player": row.get("participant"),
        "rank": row.get("rank", 0),
        # Day-9.26: separate group + knockout + side + futures so render_block
        # can show the 4 categories distinctly (matching the Negev app's
        # Standings page columns).
        "total": float(row.get("group_points") or 0)
                  + float(row.get("knockout_points") or 0)
                  + float(row.get("side_points") or 0)
                  + float(row.get("futures_points") or 0),
        "direction": float(row.get("group_points") or 0),
        "knockout":  float(row.get("knockout_points") or 0),
        "side":      float(row.get("side_points") or 0),
        "broad":     float(row.get("futures_points") or 0),
        "exactCount": 0,
        "role": "player",
        "uid": None,
    }


# ───────────────────────── renderers ─────────────────────────

def render_block(rows: list[dict], name: str,
                 *, self_name: str | None = None,
                 broad_bets: dict | None = None) -> str:
    """Multi-line audit block for ONE person. Used in the header of 📊 and
    as the per-person block in ☀️.

    Layout (each line ≤72 chars on mobile, total 5-7 lines):
      👤 Vaadia
         Rank:        12 / 67    (app view)
         Total:       3.5 pts
         Split:       group 3.5  •  futures 0.0
         vs leader   (Gilad, 12.5):  -9.0
         vs second   (Sarah, 10.0):  -6.5
         vs you      (Igor,   0.0):  +3.5  ← Vaadia ahead
         Broad bets: Brazil • Iran • Lautaro • Patishi   [if broad_bets given]

    If the person IS `self_name`, the "vs you" line is skipped (it would
    show +0.0 vs yourself — noise).
    """
    self_name = self_name or my_participant()
    row = _find(rows, name)
    if not row:
        return f"👤 {name}\n   ✗ Not in standings"
    n = len(rows)
    humans = _humans(rows)
    leader = humans[0] if humans else row
    second = humans[1] if len(humans) > 1 else None
    me_row = _find(rows, self_name)
    is_me = (name == self_name)
    tag = "  ← you" if is_me else ""

    direction = float(row.get("direction") or 0)
    knockout = float(row.get("knockout") or 0)
    side = float(row.get("side") or 0)
    broad = float(row.get("broad") or 0)
    total = float(row.get("total") or 0)
    rank = row.get("rank") or "?"

    # Day-9.26: split line now mirrors Negev's 4-column standings page.
    # Show only the non-zero categories on mobile to keep the line tight;
    # always show 'group' since that's the dominant signal early-tournament.
    split_parts = [f"group {direction:.1f}"]
    if knockout > 0:
        split_parts.append(f"KO {knockout:.1f}")
    if side > 0:
        split_parts.append(f"side {side:.1f}")
    if broad > 0 or sum((knockout, side, broad)) == 0:
        # always include futures when we want to make explicit nothing else fired
        split_parts.append(f"futures {broad:.1f}")
    split_line = "  •  ".join(split_parts)

    lines = [
        f"👤 {name}{tag}",
        f"   Rank:       {rank} / {n}  (app view)",
        f"   Total:      {total:.1f} pts",
        f"   Split:      {split_line}",
    ]
    if leader.get("player") != name:
        gap = leader["total"] - total
        lines.append(
            f"   vs leader  ({leader['player']}, {leader['total']:.1f}): "
            f"{-gap:+.1f}")
    if second and second.get("player") != name:
        gap = second["total"] - total
        lines.append(
            f"   vs second  ({second['player']}, {second['total']:.1f}): "
            f"{-gap:+.1f}")
    if not is_me and me_row:
        gap_you = me_row["total"] - total
        relation = ("ahead of you" if gap_you < 0
                    else ("behind you" if gap_you > 0 else "tied with you"))
        lines.append(
            f"   vs you     ({self_name}, {me_row['total']:.1f}): "
            f"{-gap_you:+.1f}  ← {name} {relation}")
    if broad_bets:
        parts = [str(broad_bets.get(k)) for k in
                  ("winner", "cinderella", "goldenBoot", "bestPlayer")
                  if broad_bets.get(k)]
        if parts:
            lines.append("   Broad bets: " + " • ".join(parts))
    return "\n".join(lines)


def render_compact(rows: list[dict], name: str,
                   *, self_name: str | None = None) -> str:
    """One-line participant summary. Mobile-friendly (~90 chars max).

      Igor:    0.0 pts   (rank 26/67, -12.5 vs leader)   ← you
      Vaadia:  3.5 pts   (rank 12/67,  -9.0 vs leader)   [3.5 ahead of you]
    """
    self_name = self_name or my_participant()
    row = _find(rows, name)
    if not row:
        return f"  {name}: ✗ not in standings"
    n = len(rows)
    humans = _humans(rows)
    leader = humans[0] if humans else row
    me_row = _find(rows, self_name)
    gap_l = leader["total"] - row["total"]
    is_me = (name == self_name)
    tag = "  ← you" if is_me else ""
    line = (f"  {name}: {row['total']:.1f} pts   "
            f"(rank {row['rank']}/{n}, {-gap_l:+.1f} vs leader){tag}")
    if not is_me and me_row:
        gap_you = me_row["total"] - row["total"]
        if abs(gap_you) < 0.05:
            line += "   [tied with you]"
        elif gap_you < 0:
            line += f"   [{abs(gap_you):.1f} ahead of you]"
        else:
            line += f"   [{abs(gap_you):.1f} behind you]"
    return line


def render_match_picks_block(picks: list[dict] | None,
                              my_pred: dict | None,
                              tracked: list[str],
                              home: str, away: str,
                              *, self_name: str | None = None,
                              title: str = "👥 Picks") -> str:
    """Per-match picks block. Mine + every tracked friend, side by side.

    `picks` is the Negev `friendsPicks` list:
        [{displayName, homeScore, awayScore, points, breakdown}]
    `my_pred` is `myPrediction`: {home, away} or None.
    `tracked` is `tracked_participants()` (you first).

      👥 Picks
        Igor:    Mexico 2 — South Africa 1   ← you
        Vaadia:  Draw 1 — 1
        (no pick from friend X yet)

    Skips users with no pick recorded yet — the kickoff card uses this to
    show only who's actually engaged for THIS match. Each row is plain
    text (Telegram-safe).
    """
    self_name = self_name or my_participant()
    pick_by_name = {p.get("displayName"): p for p in (picks or [])}
    out = [title]
    for name in tracked:
        is_me = (name == self_name)
        tag = "   ← you" if is_me else ""
        if is_me and my_pred and my_pred.get("home") is not None:
            h, a = my_pred["home"], my_pred["away"]
            out.append(f"  {name}: {home} {h} — {away} {a}{tag}")
            continue
        p = pick_by_name.get(name)
        if p and p.get("homeScore") is not None:
            h, a = p["homeScore"], p["awayScore"]
            pts = p.get("points")
            pts_tag = f"  ({pts:.1f} pts)" if isinstance(pts, (int, float)) else ""
            out.append(f"  {name}: {home} {h} — {away} {a}{tag}{pts_tag}")
        else:
            out.append(f"  {name}: (no pick yet){tag}")
    return "\n".join(out)
