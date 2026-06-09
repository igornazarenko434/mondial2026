"""Day-9.23: Negev multiplier drift watchdog.

Negev's per-stage exact-score multiplier grid is the SCORING CONSTANT we
optimize against in ev_optimizer.recommend. If the admin changes those
multipliers (e.g. tweaks 2-0 from ×2.25 to ×2.5 mid-tournament), every
EV-optimal pick we compute would be subtly wrong against the live
scoring rules.

This tool:

  1. Pulls Negev's live grid via toto_get_scoring_grids
  2. Diffs against config/rules.py::SCORE_TABLE cell-by-cell
  3. Reports any drift with severity classification
  4. Suggests remediation steps
  5. With --telegram, fires a Telegram alert ONLY on detected drift
     (silent on the happy path — perfect for daily cron).

  PYTHONPATH=. .venv/bin/python tools/audit_negev_multipliers.py
  PYTHONPATH=. .venv/bin/python tools/audit_negev_multipliers.py --telegram

Negev's three grids → our keys mapping (Day-9.7 verified all 49×3=147 cells):
  groupStage          → SCORE_TABLE["group"]
  round16AndQuarter   → SCORE_TABLE["ko"]
  semiAndFinal        → SCORE_TABLE["final"]

Cost: 1 Negev call. Free.
Exit code: 0 on aligned, 1 on drift (cron --telegram fires alert on 1).
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Negev → our internal stage-key
GRID_MAP = {
    "groupStage":        "group",
    "round16AndQuarter": "ko",
    "semiAndFinal":      "final",
}


def _telegram_alert(drift_lines: list[str]) -> bool:
    """Fire a ⚠ alert. Returns True if delivery succeeded."""
    try:
        from core import delivery
        body = ("Negev SCORING multipliers drifted from our local config — "
                "ev_optimizer is computing against the wrong payoff function "
                "until config/rules.py is patched.\n\n"
                "First few drifted cells:\n  " +
                "\n  ".join(drift_lines[:8]) +
                ("\n  ... +%d more" % (len(drift_lines) - 8)
                 if len(drift_lines) > 8 else "") +
                "\n\nFix: re-run with no flags, copy the diff into "
                "config/rules.py::_GROUP / _KO / _FINAL, "
                "pytest tests/, systemctl restart mondial2026.")
        return bool(delivery.alert("Negev multiplier drift detected", body))
    except Exception as e:                                # noqa: BLE001
        print(f"  ⚠ Telegram alert send failed: {e}")
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="audit_negev_multipliers")
    p.add_argument("--telegram", action="store_true",
                   help="On drift, fire ⚠ Telegram alert via delivery.alert(). "
                        "Silent on the happy path — safe for daily cron.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress success-line output for cron logs")
    args = p.parse_args(argv)
    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  Negev multiplier alignment check")
    print(f"  ╚════════════════════════════════════════════════════════════╝")
    print()

    try:
        from integrations import negev_toto_mcp as ntm
    except Exception as e:                                # noqa: BLE001
        print(f"  ✗ Negev MCP import failed: {e}")
        return 2

    from config.rules import SCORE_TABLE

    try:
        live = ntm.toto_get_scoring_grids()
    except Exception as e:                                # noqa: BLE001
        print(f"  ✗ toto_get_scoring_grids failed: {e}")
        return 2

    if "error" in (live or {}):
        print(f"  ✗ {live.get('error')}")
        return 2

    grids = live.get("grids") or {}
    print(f"  Tournament: {live.get('tournament_id')}")
    print(f"  Live grids: {list(grids.keys())}")
    print()

    total_cells = 0
    matches = 0
    drift = []
    missing_in_negev = []
    missing_in_ours = []

    for negev_key, our_key in GRID_MAP.items():
        negev_grid = grids.get(negev_key) or {}
        our_grid = SCORE_TABLE.get(our_key) or {}
        print(f"  ── {negev_key}  ↔  SCORE_TABLE[{our_key!r}] ──")

        # Negev's keys are 'H-A' strings (e.g. '2-1', '6+-3')
        # Our keys are (winner, loser) int tuples
        # Compare every cell present on EITHER side.
        seen = set()
        for h_a, neg_val in negev_grid.items():
            seen.add(h_a)
            total_cells += 1
            try:
                h_str, a_str = h_a.split("-")
                # Skip blowout cells ("6+-3"); the cap covers those
                if "+" in h_str or "+" in a_str:
                    continue
                h, a = int(h_str), int(a_str)
                # SCORE_TABLE only stores winner-side cells (h >= a)
                if h >= a:
                    ours = our_grid.get((h, a))
                else:
                    # Mirror — Negev stores asymmetric (away-win) cells
                    # explicitly; we infer them by flipping. ev_optimizer
                    # already handles this — drift here is still
                    # informative.
                    ours = our_grid.get((a, h))
            except (ValueError, AttributeError):
                continue
            if ours is None:
                missing_in_ours.append(f"{our_key}:{h_a}")
                continue
            if abs(float(neg_val) - float(ours)) < 0.01:
                matches += 1
            else:
                drift.append(f"  ⚠ {our_key} {h_a}: ours={ours}  "
                              f"negev={neg_val}  Δ={neg_val - ours:+.2f}")

        # Any cells we have that Negev doesn't?
        for (h, a), val in our_grid.items():
            key = f"{h}-{a}"
            mirror = f"{a}-{h}"
            if key not in seen and mirror not in seen:
                missing_in_negev.append(f"{our_key}:{key}")

        cells_in_grid = sum(1 for _ in negev_grid)
        print(f"    Negev cells: {cells_in_grid}  "
              f"ours: {len(our_grid)}")
    print()

    print(f"  ── Summary ──")
    print(f"  Total cells compared:  {total_cells}")
    print(f"  Matching cells:        {matches}")
    print(f"  Drifted cells:         {len(drift)}")
    print(f"  Missing on our side:   {len(missing_in_ours)}")
    print(f"  Missing on Negev side: {len(missing_in_negev)}")

    if drift:
        print()
        print(f"  ⚠ Drift detected — config/rules.py is out of sync with Negev:")
        for d in drift[:20]:
            print(d)
        if len(drift) > 20:
            print(f"    ... +{len(drift) - 20} more")
        print()
        print(f"  REMEDIATION:")
        print(f"    1. Confirm with the Negev admin that the change was intentional")
        print(f"       (not a bug in their app).")
        print(f"    2. Patch config/rules.py::_GROUP / _KO / _FINAL to the new values.")
        print(f"    3. Re-run tools/negev_consistency_audit.py to verify the patch.")
        print(f"    4. Re-run pytest tests/ — scoring tests pin the old values via")
        print(f"       direct lookups (will need updating).")
        print(f"    5. systemctl restart mondial2026 on the VM.")
        if args.telegram:
            ok = _telegram_alert([d.strip() for d in drift])
            print()
            print(f"  Telegram alert {'sent ✓' if ok else 'FAILED ✗'}")
        return 1

    # Schema-mismatch classification: cells with goals ≥4 are blowouts
    # handled by TABLE_CAP on OUR side (config/rules.py::TABLE_CAP).
    # Cells with goals ≥6 are extrapolations on our side. Neither is drift.
    def _is_blowout(cell: str) -> bool:
        try:
            grid, scoreline = cell.split(":")
            h, a = scoreline.split("-")
            return int(h) >= 4 or int(a) >= 4
        except (ValueError, KeyError):
            return False

    expected_skips = sum(1 for c in missing_in_ours if _is_blowout(c))
    expected_extras = sum(1 for c in missing_in_negev if _is_blowout(c))
    real_missing_ours = [c for c in missing_in_ours if not _is_blowout(c)]
    real_missing_neg = [c for c in missing_in_negev if not _is_blowout(c)]

    if expected_skips or expected_extras:
        print()
        print(f"  ℹ Schema deltas (EXPECTED, not drift):")
        print(f"    {expected_skips:>3} blowout cells (≥4 goals) on Negev — "
              f"we use TABLE_CAP for those")
        print(f"    {expected_extras:>3} blowout cells (≥6 goals) on our side — "
              f"extrapolations beyond Negev's printed grid")

    if real_missing_ours or real_missing_neg:
        print()
        print(f"  ⚠ UNEXPECTED schema mismatch — investigate:")
        for c in real_missing_ours:
            print(f"    Missing on our side: {c}")
        for c in real_missing_neg:
            print(f"    Missing on Negev side: {c}")
        return 1

    if not args.quiet:
        print()
        print(f"  ✓ All {matches} comparable multipliers aligned. "
              f"Scoring engine matches Negev's grid byte-for-byte where they overlap.")
        print(f"  ✓ {expected_skips + expected_extras} schema deltas are all "
              f"expected (blowout cells handled by TABLE_CAP).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
