"""CLI wrapper around toto_save_broad_bets — shell-friendly Day-7 futures lock.

Use on the VM to commit (or preview) the 4 broad bets without ever pasting
Python into bash:

    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/save_broad_bets.py \
        --winner "Portugal" \
        --cinderella "Uzbekistan" \
        --golden-boot "Mbappe" \
        --best-player "Igor" \
        --dry-run
    '

Removing `--dry-run` performs the real Firestore PATCH (only if
`NEGEV_ALLOW_WRITES=1` in `.env`). Choices accept EITHER the display name
shown in the app ("Portugal") OR the full Firestore id ("team_Portugal").
Pass only the categories you want to set — partial updates are supported.

Always runs the dry-run resolution first and prints the planned PATCH so
you can verify name→id mapping before flipping the write gate.

Day-7 lock deadline (per project memory): **2026-06-11 21:59 IDT**.
"""
from __future__ import annotations
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _print(d: dict) -> None:
    print(json.dumps(d, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="save_broad_bets",
                                description="Submit Day-7 futures to Negev Toto.")
    p.add_argument("--winner",      default=None,
                   help="Tournament Winner (team name or team_<Name> id)")
    p.add_argument("--cinderella",  default=None,
                   help="Cinderella Team (team name or team_<Name> id)")
    p.add_argument("--golden-boot", default=None,
                   help="Golden Boot player (display name or numeric id)")
    p.add_argument("--best-player", default=None,
                   help="Best Placed Player — META-BET on a participant "
                        "(displayName of a friend; not a footballer)")
    p.add_argument("--tournament-id", default=None,
                   help="Override NEGEV_TOURNAMENT_ID env var")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve names → ids, show planned PATCH, do NOT call "
                        "Firestore. Use first to verify resolution.")
    p.add_argument("--force", action="store_true",
                   help="Skip the dry-run preview confirmation step")
    args = p.parse_args(argv)

    if not any([args.winner, args.cinderella, args.golden_boot, args.best_player]):
        print("ERROR: pass at least one of --winner / --cinderella / "
              "--golden-boot / --best-player", file=sys.stderr)
        return 2

    from integrations import negev_toto_mcp as ntm

    # ── Always do a dry-run first to surface name-resolution errors. ──
    plan = ntm.toto_save_broad_bets(
        winner=args.winner, cinderella=args.cinderella,
        golden_boot=args.golden_boot, best_player=args.best_player,
        tournament_id=args.tournament_id, dry_run=True)
    print("\n=== PLANNED PATCH (dry-run) ===")
    _print(plan)

    if "error" in plan:
        print("\n✗ Resolution failed — fix the choice(s) above and retry.",
              file=sys.stderr)
        return 2

    if args.dry_run:
        print("\nℹ Dry-run only — no Firestore write performed.")
        return 0

    if os.environ.get("NEGEV_ALLOW_WRITES") != "1":
        print("\n✗ NEGEV_ALLOW_WRITES != 1 — the env gate is closed.",
              file=sys.stderr)
        print("  Flip in .env (chmod 600), restart, re-run WITHOUT --dry-run.",
              file=sys.stderr)
        return 2

    # ── Confirm + commit unless --force was passed. ──
    if not args.force:
        print("\nAbout to PATCH the live Negev document at:")
        print(f"  {plan['would_patch']}")
        print("Resolved selections:")
        for k, v in plan["resolved"].items():
            print(f"  {k:<12} → {v}")
        try:
            ans = input("\nType YES (uppercase) to commit, anything else aborts: ")
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans.strip() != "YES":
            print("✗ Aborted — no write performed.", file=sys.stderr)
            return 1

    result = ntm.toto_save_broad_bets(
        winner=args.winner, cinderella=args.cinderella,
        golden_boot=args.golden_boot, best_player=args.best_player,
        tournament_id=args.tournament_id, dry_run=False)
    print("\n=== RESULT ===")
    _print(result)
    if not result.get("ok"):
        return 2
    print("\n✓ Saved to Negev. Verify by reading back:")
    print("  toto_get_broad_bets() — your row should now show the new selections")
    return 0


if __name__ == "__main__":
    sys.exit(main())
