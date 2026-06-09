"""Unified CLI over every Negev Toto question worth asking.

WHY
===

Eight previously-scattered tools collapsed into one consistent surface so
a human (or the future Telegram bot) has ONE place to discover what the
Negev MCP can answer + ONE renderer per question.

Each subcommand calls the most-suitable `integrations.negev_toto_mcp`
function(s), aggregates if needed (id→name joins, popularity counts,
match labels), and prints a Telegram-safe plain-text block. Future bot
just imports the `handle_*` functions and routes /commands to them.

Subcommands:

  standings  [--n 10]           leaderboard + tracked block
  broad                         everyone's futures (Winner / Cinderella /
                                  GoldenBoot / BestPlayer) with names + popularity
  match      <home> <away>      one match's picks + popularity + my pick
  player     <displayName>      one player's picks across every match
  sidebets   [--include-empty]  side-bet questions + answers (where visible)
  suggest    <home> <away>      our pipeline's locked card for that match
  upcoming   [--n 5]            next N matches + per-match pick counts
  help                          this list

Usage:
    PYTHONPATH=. .venv/bin/python tools/toto.py <subcommand> [args]

Future Telegram bot can do:
    from tools.toto import handle_standings, handle_match, ...
    reply = handle_match(home="Mexico", away="South Africa")
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ───────────────────────── shared helpers ─────────────────────────

def _ntm():
    try:
        from integrations import negev_toto_mcp as m
        return m
    except Exception as e:                                # noqa: BLE001
        raise RuntimeError(f"Negev MCP import failed: {e}")


def _tracked() -> tuple[str, list[str]]:
    me = (os.environ.get("MY_PARTICIPANT") or "Igor").strip()
    friends = [s.strip() for s in
                os.environ.get("FRIEND_PARTICIPANTS", "").split(",")
                if s.strip()]
    return me, friends


def _tag(name: str, me: str, friends: list[str]) -> str:
    if name == me:
        return "  ← you"
    if name in friends:
        return "  ← tracked"
    return ""


def _load_name_maps(ntm) -> dict[str, dict[str, str]]:
    """One call → id→name dicts for all 4 broad-bet categories.
    bestPlayer dual-registered (roster_<uid> + bare <uid>)."""
    maps = {"winner": {}, "cinderella": {}, "goldenBoot": {}, "bestPlayer": {}}
    try:
        cats = ntm.toto_get_broad_bet_categories()
        for c in (cats.get("categories") or []):
            cid = c.get("id")
            if cid not in maps:
                continue
            for opt in (c.get("options") or []):
                oid = opt.get("id")
                if not oid:
                    continue
                maps[cid][oid] = opt.get("name") or oid
                if cid == "bestPlayer" and oid.startswith("roster_"):
                    maps[cid][oid[len("roster_"):]] = opt.get("name") or oid
    except Exception:                                     # noqa: BLE001
        pass
    return maps


def _translate(maps: dict, cat: str, raw: str | None) -> str:
    if not raw:
        return "-"
    return maps.get(cat, {}).get(raw) or f"{raw}?"


def _uid_for_name(ntm, name: str) -> str | None:
    """Look up a player's uid by displayName via the standings view (which
    is keyed on displayName + carries uid)."""
    try:
        for r in ntm.toto_get_standings(include_bots=True):
            if (r.get("player") or "").lower() == name.lower():
                return r.get("uid")
    except Exception:                                     # noqa: BLE001
        pass
    return None


# ───────────────────────── handlers ─────────────────────────

def handle_standings(*, n: int = 10) -> str:
    """Top-N + the full tracked block via core.reporting.people."""
    ntm = _ntm()
    me, friends = _tracked()
    rows = ntm.toto_get_standings(include_bots=True)
    if not rows:
        return "Standings: (empty)"
    from core.reporting import people
    lines = [f"📊 Negev standings — top {n}"]
    humans = [r for r in rows if r.get("role") != "bot"]
    for r in humans[:n]:
        tag = _tag(r.get("player", ""), me, friends)
        lines.append(f"  {r['rank']:>2}. {r['player']:<20} {r['total']:>6.1f}{tag}")
    lines.append("")
    lines.append("─── Tracked 👥 ───")
    for name in [me, *friends]:
        lines.append(people.render_compact(rows, name, self_name=me))
    return "\n".join(lines)


def handle_broad() -> str:
    """Everyone's futures with names + popularity footer."""
    ntm = _ntm()
    me, friends = _tracked()
    maps = _load_name_maps(ntm)
    rows = ntm.toto_get_broad_bets()
    submitted = [r for r in rows
                  if r.get("winner") or r.get("cinderella")
                     or r.get("goldenBoot") or r.get("bestPlayer")]
    lines = [f"🎯 Broad bets — {len(submitted)} submitted"]
    lines.append(f"{'':<22} {'Winner':<12} {'Cinderella':<14} "
                  f"{'GoldenBoot':<18} {'BestPlayer':<18}")
    for r in submitted:
        name = r.get("displayName") or "?"
        tag = _tag(name, me, friends)
        w = _translate(maps, "winner", r.get("winner"))[:12]
        ci = _translate(maps, "cinderella", r.get("cinderella"))[:14]
        gb = _translate(maps, "goldenBoot", r.get("goldenBoot"))[:18]
        bp = _translate(maps, "bestPlayer", r.get("bestPlayer"))[:18]
        lines.append(f"  {name:<20} {w:<12} {ci:<14} {gb:<18} {bp:<18}{tag}")
    if maps and submitted:
        lines.append("")
        lines.append("── Pool popularity ──")
        for cat in ("winner", "cinderella", "goldenBoot", "bestPlayer"):
            c = Counter(_translate(maps, cat, r.get(cat))
                          for r in submitted if r.get(cat))
            top = c.most_common(5)
            if top:
                parts = ", ".join(f"{n} ({k})" for n, k in top)
                lines.append(f"  {cat:<12} {parts}")
    return "\n".join(lines)


def handle_match(*, home: str, away: str) -> str:
    """One match: who picked what + popularity + my pick + tracked friends."""
    ntm = _ntm()
    me, friends = _tracked()
    tracked = (me, *friends)
    details = ntm.toto_get_match_details(home=home, away=away)
    if "error" in (details or {}):
        return f"⚠ {details['error']}"
    m = details.get("match") or {}
    my = details.get("myPrediction")
    mult = details.get("bingoMultiplier")
    picks = details.get("friendsPicks") or []
    lines = [f"⚽ {home} vs {away}  ({m.get('stage', '?')}, "
              f"status={m.get('status', '?')})"]
    if my:
        line = f"  YOUR pick: {home} {my.get('home')} — {away} {my.get('away')}"
        if mult:
            line += f"  (mult ×{mult})"
        lines.append(line)
    else:
        lines.append("  YOUR pick: (not submitted)")
    lines.append(f"  Picks recorded: {len(picks)} player(s)")
    by_name = {p.get("displayName"): p for p in picks}
    lines.append("")
    lines.append("── Tracked picks ──")
    for name in tracked:
        pi = by_name.get(name)
        tag = _tag(name, me, friends)
        if pi and pi.get("homeScore") is not None:
            pts = pi.get("points")
            pts_s = f"  ({pts:.1f} pts)" if isinstance(pts, (int, float)) else ""
            lines.append(f"  {name}: {home} {pi['homeScore']} — "
                          f"{away} {pi['awayScore']}{pts_s}{tag}")
        else:
            lines.append(f"  {name}: (no pick yet){tag}")
    # Popularity
    score_counts = Counter()
    dir_counts = Counter()
    for pi in picks:
        h, a = pi.get("homeScore"), pi.get("awayScore")
        if h is None:
            continue
        score_counts[(h, a)] += 1
        dir_counts["H" if h > a else ("D" if h == a else "A")] += 1
    if score_counts:
        lines.append("")
        lines.append("── Popular picks ──")
        for (h, a), c in score_counts.most_common(5):
            lines.append(f"  {home} {h} — {away} {a}   {c} pick(s)  "
                          f"({c / len(picks) * 100:.0f}%)")
        lines.append(f"  Direction: H={dir_counts['H']}  D={dir_counts['D']}  "
                      f"A={dir_counts['A']}")
    return "\n".join(lines)


def handle_player(*, name: str) -> str:
    """One player's picks across every match. Maps matchId → home/away via
    toto_get_matches (1 call) so we don't print opaque ids."""
    ntm = _ntm()
    uid = _uid_for_name(ntm, name)
    if not uid:
        return f"⚠ Player {name!r} not found in standings."
    # Map matchId → match row (for home/away/status labels)
    matches = ntm.toto_get_matches(limit=300)
    by_mid = {m["match_id"]: m for m in matches}
    by_apid = {str(m.get("apiFixtureId")): m for m in matches
                if m.get("apiFixtureId")}
    res = ntm.toto_query("bets", "userId", "EQUAL", uid, limit=300)
    rows = [r for r in (res.get("results") or [])
             if r.get("tournamentId") == os.environ.get(
                 "NEGEV_TOURNAMENT_ID", "")]
    lines = [f"👤 {name} — {len(rows)} pick(s)"]
    if not rows:
        lines.append("  (none submitted yet)")
        return "\n".join(lines)
    for r in rows:
        mid_raw = r.get("matchId")
        # matchId can be e.g. "n40y..._1489369" — split on the LAST underscore
        m = by_mid.get(mid_raw)
        if not m and isinstance(mid_raw, str) and "_" in mid_raw:
            tail = mid_raw.split("_")[-1]
            m = by_apid.get(tail)
        h_name = m["home"] if m else "?"
        a_name = m["away"] if m else "?"
        ko = (m or {}).get("kickoff_utc") or (m or {}).get("date") or ""
        when = ko[:10] if ko else "?"
        hs, ascore = r.get("homeScore"), r.get("awayScore")
        pts = r.get("points")
        pts_s = f"  ({pts:.1f} pts)" if isinstance(pts, (int, float)) else ""
        lines.append(f"  {when} {h_name} {hs} — {a_name} {ascore}{pts_s}")
    return "\n".join(lines)


def handle_sidebets(*, include_empty: bool = False) -> str:
    """Side-bet questions (published + unresolved), plus everyone's answers
    when visible. Negev's answers sub-collection path is unverified until a
    real side bet ships — we gracefully report 'no answers visible' if so."""
    ntm = _ntm()
    bets = ntm.toto_get_side_bets()
    if not include_empty:
        bets = [b for b in bets if b.get("question")]
    if not bets:
        return ("🎲 Side bets: nothing published yet.\n"
                "  (Negev's founder hasn't posted any side-bet questions.)")
    lines = [f"🎲 Side bets — {len(bets)} doc(s)"]
    tid = os.environ.get("NEGEV_TOURNAMENT_ID", "")
    for b in bets:
        sid = b["id"]
        status = ("RESOLVED" if b.get("isResolved")
                  else ("LOCKED" if b.get("isLocked")
                        else "OPEN" if b.get("isActive") else "pending"))
        lines.append(f"  • {sid}  [{status}]")
        q = b.get("question")
        if q:
            lines.append(f"     Q: {q}")
        if b.get("correctAnswer") is not None:
            lines.append(f"     ✓ correct: {b['correctAnswer']}")
        # Try to fetch answers — Negev's answer-collection path was a best
        # guess (see toto_submit_side_bet_answer). Read silently and degrade.
        try:
            answers = ntm._read_all(f"tournaments/{tid}/sideBets/{sid}/answers")
            if answers:
                users = {u.get("uid"): u.get("displayName")
                          for u in ntm._read_all("users") if u.get("uid")}
                ans_lines = []
                for a in answers[:15]:
                    nm = users.get(a.get("userId"), a.get("userId", "?"))
                    val = a.get("answer")
                    ans_lines.append(f"       {nm}: "
                                      f"{'Yes' if val else 'No'}")
                if ans_lines:
                    lines.append(f"     Answers ({len(answers)}):")
                    lines.extend(ans_lines)
        except Exception:                                # noqa: BLE001
            pass     # silent: answer-collection schema unverified
    return "\n".join(lines)


def handle_suggest(*, home: str, away: str) -> str:
    """System's locked card for this match — reads predictions table directly
    (no recomputation, no LLM call). Shows the most-recent window we have."""
    from store.db import connect
    try:
        conn = connect()
        row = conn.execute(
            "SELECT match_id FROM matches WHERE home=? AND away=?",
            (home, away)).fetchone()
    except sqlite3.Error as e:
        return f"⚠ DB read failed: {e}"
    if not row:
        return f"⚠ Match {home} vs {away} not in our matches table."
    mid = row[0]
    pred = conn.execute(
        "SELECT created_at, window, pick_dir, pick_h, pick_a, "
        "expected_points, payload_json FROM predictions WHERE match_id=? "
        "ORDER BY created_at DESC LIMIT 1", (mid,)).fetchone()
    if not pred:
        return (f"⚠ No card has been built yet for {home} vs {away}.\n"
                f"  First window (T-24h) will fire 24h before kickoff.\n"
                f"  To compute on demand: rerun daemon or call build_card "
                f"manually via tools/run_one_match.py.")
    created, w, pdir, ph, pa, ep, payload = pred
    import json
    try:
        card = json.loads(payload)
    except (TypeError, ValueError):
        card = {}
    from core.delivery.base import render_card
    out = render_card(card) if card else (
        f"Match: {home} vs {away}\nWindow: {w}\n"
        f"Pick: dir={pdir} exact={home} {ph} — {away} {pa}\nEV={ep}")
    return f"🃏 Cached suggestion ({w}, built {created}):\n\n{out}"


def handle_upcoming(*, n: int = 5) -> str:
    """Next N scheduled matches + how many picks each has so far."""
    ntm = _ntm()
    matches = ntm.toto_get_matches(date_after=None, status="NS", limit=200)
    matches = sorted(matches, key=lambda m: m.get("date") or "")
    if not matches:
        return "Upcoming: (none)"
    matches = sorted(matches, key=lambda m: m.get("kickoff_utc") or "")
    lines = [f"📅 Next {min(n, len(matches))} match(es):"]
    for m in matches[:n]:
        ko = m.get("kickoff_utc") or m.get("date") or "?"
        lines.append(f"  {ko[:16]:<16}  {m['home']:<14} vs "
                      f"{m['away']:<22}  ({m.get('stage', '?')})")
    return "\n".join(lines)


def handle_help() -> str:
    return ("📖 Available questions:\n"
            "  /standings  [--n N]            top-N leaderboard + tracked block\n"
            "  /broad                          everyone's futures + popularity\n"
            "  /match <home> <away>            picks + popularity for one match\n"
            "  /player <name>                  all picks of one player\n"
            "  /sidebets                       side-bet questions + answers\n"
            "  /suggest <home> <away>          our pipeline's cached pick\n"
            "  /upcoming [--n N]               next N matches\n"
            "  /help                           this list\n"
            "All read-only. Telegram bot will eventually route to the same "
            "handlers.")


# ───────────────────────── CLI dispatch ─────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toto",
                                 description="Unified Negev Toto inspector.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("standings")
    s.add_argument("--n", type=int, default=10)

    sub.add_parser("broad")

    s = sub.add_parser("match")
    s.add_argument("home")
    s.add_argument("away")

    s = sub.add_parser("player")
    s.add_argument("name")

    s = sub.add_parser("sidebets")
    s.add_argument("--include-empty", action="store_true",
                   help="Include unpublished shells (default: only Q's with text)")

    s = sub.add_parser("suggest")
    s.add_argument("home")
    s.add_argument("away")

    s = sub.add_parser("upcoming")
    s.add_argument("--n", type=int, default=5)

    sub.add_parser("help")

    args = p.parse_args(argv)
    try:
        if args.cmd == "standings":
            text = handle_standings(n=args.n)
        elif args.cmd == "broad":
            text = handle_broad()
        elif args.cmd == "match":
            text = handle_match(home=args.home, away=args.away)
        elif args.cmd == "player":
            text = handle_player(name=args.name)
        elif args.cmd == "sidebets":
            text = handle_sidebets(include_empty=args.include_empty)
        elif args.cmd == "suggest":
            text = handle_suggest(home=args.home, away=args.away)
        elif args.cmd == "upcoming":
            text = handle_upcoming(n=args.n)
        else:
            text = handle_help()
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    except Exception as e:                                # noqa: BLE001
        print(f"✗ unexpected: {e}", file=sys.stderr)
        return 1
    print()
    print(text)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
