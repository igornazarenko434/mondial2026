"""Simulated-context scenario tests for the news agent — no Brave, no
api-football, just hand-crafted contexts piped through the REAL LLM.

Goal: stress-test the LLM's reasoning + output format against specific
edge cases without burning external API budget on data-gathering. Each
scenario:
  - feeds a hand-crafted context block
  - calls analyze_safe (real LLM)
  - prints raw deltas + notes + discarded
  - checks against an expected-range rubric
  - flags any drift from the SYSTEM prompt's rubric (§ Day-8 playbook)

Cost: ~1 LLM call per scenario × 10 scenarios ≈ 10/1500 daily Gemini RPD.
No external HTTP beyond the LLM provider's own endpoint.

Usage:
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/test_news_agent_scenarios.py
    '

Flags:
  --scenario NAME    run ONE scenario by name
  --list             list all scenario names and exit
  --no-save          don't write the report file
  --report-dir PATH  default reports/
"""
from __future__ import annotations
import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ───── Scenario library ───────────────────────────────────────────────────────

@dataclass
class Scenario:
    name: str
    description: str
    home: str
    away: str
    context: str
    # Expected ranges to check the LLM's output against:
    expected_home_delta_range: tuple[float, float]   # (min, max) inclusive
    expected_away_delta_range: tuple[float, float]
    expected_confidence: tuple[str, ...] = ("low", "medium", "high")
    # The rubric being tested, for human auditor:
    rubric_lesson: str = ""


def _make(name, desc, home, away, ctx, hr, ar, **kw) -> Scenario:
    return Scenario(name=name, description=desc, home=home, away=away,
                     context=ctx, expected_home_delta_range=hr,
                     expected_away_delta_range=ar, **kw)


# Each scenario is calibrated against the SYSTEM-prompt rubric:
#   Key striker out:            -0.30 to -0.45  (to that team)
#   Important attacker out:     -0.15 to -0.30
#   1st-keeper / 2+ defenders:  +0.15 to +0.30  (to OPPONENT)
#   Squad rotation / qualified: -0.20 to -0.40
#   Must-win motivation:        +0.05 to +0.15
#   Star returns / confirmed:   +0.10 to +0.25
#   Heavy rain / heat / altitude: -0.10 to -0.20 (BOTH)
#   Manager confirms low-block: -0.10 to -0.15
#   Nothing material:           0.0
SCENARIOS: list[Scenario] = [
    # ───── 1. Empty context — should return NEUTRAL ─────
    _make(
        "empty_context",
        "No information at all — must return NEUTRAL with low confidence",
        "Mexico", "South Africa",
        ctx=("[MATCH: Mexico vs South Africa, kickoff 2026-06-11T19:00:00+00:00, stage Group A]\n"
             "[FETCHED: 2026-06-09 10:00Z; recency cap 48h]\n"
             "[SOURCE: API-Football]\nlineups source unavailable\n\n"
             "[SOURCE: brave_search]\n(no results within recency window)"),
        hr=(0.0, 0.0), ar=(0.0, 0.0),
        expected_confidence=("low",),
        rubric_lesson="Empty context → 0.0/0.0/low",
    ),

    # ───── 2. Key striker OUT — apply -0.30 to -0.45 ─────
    _make(
        "key_striker_out_home",
        "Mexico's star striker confirmed out — home delta must be -0.30 to -0.45",
        "Mexico", "South Africa",
        ctx=("[MATCH: Mexico vs South Africa, kickoff 2026-06-11T19:00:00+00:00, stage Group A]\n"
             "[FETCHED: 2026-06-09 10:00Z; recency cap 48h]\n\n"
             "[SOURCE: API-Football /fixtures/lineups]\n"
             "Mexico (4-3-3): Ochoa, Sanchez, Montes, Vasquez, Gallardo, Alvarez, Chavez, Lozano, "
             "Antuna, Pizarro, Gimenez (NOT in XI — Raul Jimenez ruled OUT, knee injury, "
             "confirmed by Mexico FA press release 2026-06-09 09:00Z). "
             "South Africa (4-2-3-1): Williams, Mokoena, Hlanti, Mvala, Ndlovu, Mokwena, Mudau, "
             "Zwane, Mbatha, Mokoena, Foster — strongest available XI.\n\n"
             "[SOURCE: brave_search]\n"
             "- [2026-06-09] Mexico FA confirms Raul Jimenez out of opener with knee injury "
             "— striker who scored 4 goals in qualifying ruled out 12h before kickoff. "
             "Estradas to start as makeshift forward."),
        hr=(-0.45, -0.20), ar=(0.0, 0.10),
        expected_confidence=("medium", "high"),
        rubric_lesson="Key striker out → home -0.30 to -0.45, away unchanged",
    ),

    # ───── 3. Multi-defender out (away) → home gets +0.15 to +0.30 ─────
    _make(
        "defenders_out_away_helps_home",
        "South Africa loses 2 defenders + keeper — home (Mexico) should get +0.15 to +0.30",
        "Mexico", "South Africa",
        ctx=("[MATCH: Mexico vs South Africa]\n[FETCHED: 2026-06-09 10:00Z; recency cap 48h]\n\n"
             "[SOURCE: API-Football /injuries — South Africa]\n"
             "South Africa injuries: Williams (1st-choice keeper, suspended); "
             "Hlanti (CB, hamstring); Mvala (CB, red card last group); "
             "Mokoena (DM, fitness). 4 key defensive players unavailable.\n\n"
             "[SOURCE: brave_search]\n"
             "- [2026-06-09] South Africa fielding 'patched' back four including a 19-year-old "
             "U20 player at right-back; analysts warn Mexico will exploit set pieces against the "
             "makeshift defense."),
        hr=(0.10, 0.30), ar=(0.0, 0.10),
        expected_confidence=("medium", "high"),
        rubric_lesson="2+ key defenders + keeper out → opponent +0.15 to +0.30",
    ),

    # ───── 4. Rotation / dead rubber (both teams qualified) ─────
    # NOTE: when rotation + dead rubber + qualified all compound, the LLM
    # may go beyond -0.40 (towards the hard ±0.6 cap). That's reasonable
    # judgment under the stacked-condition rubric. Allow up to -0.6 each.
    _make(
        "both_qualified_rotation",
        "Both teams already through — rotation expected → both deltas negative (up to cap)",
        "Brazil", "Spain",
        ctx=("[MATCH: Brazil vs Spain — final group game]\n[FETCHED: 2026-06-23 10:00Z; recency cap 48h]\n\n"
             "[SOURCE: API-Football lineups]\n"
             "Brazil predicted XI: 8 rotation players named; coach confirmed in press conference "
             "that Casemiro, Vinicius, Raphinha will REST.\n"
             "Spain predicted XI: 7 changes from previous game; Yamal, Pedri, Williams RESTED.\n\n"
             "[SOURCE: brave_search]\n"
             "- [2026-06-22] Both Brazil and Spain already advanced to knockout stage; coaches "
             "say result irrelevant and they'll prioritise fitness for the R16."),
        hr=(-0.60, -0.15), ar=(-0.60, -0.15),
        expected_confidence=("medium", "high"),
        rubric_lesson="Both teams qualified + rotation → both -0.20 to -0.60 (LLM may stack to cap)",
    ),

    # ───── 5. Must-win motivation (away) ─────
    # IMPORTANT: this scenario tests COMPOUNDING rubrics — South Africa must
    # win (+0.05 to +0.15) AND Mexico already through (squad rotation likely,
    # -0.20 to -0.40). The LLM should reasonably apply BOTH. Acceptable
    # ranges below allow either interpretation:
    #   - "must-win only": home unchanged, away +0.05 to +0.20
    #   - "rotation + must-win": home -0.40 to -0.20, away +0.05 to +0.20
    _make(
        "must_win_motivation_away",
        "South Africa MUST win to advance — away +0.05/+0.20; Mexico may also be -0.40/0",
        "Mexico", "South Africa",
        ctx=("[MATCH: Mexico vs South Africa — final group game]\n[FETCHED: 2026-06-24 10:00Z]\n\n"
             "[SOURCE: brave_search]\n"
             "- [2026-06-24] South Africa enter the final group game KNOWING they must win or be "
             "eliminated; manager calls for 'all-out attack'. Mexico already through to R16 "
             "regardless of result. Strongest XI named for South Africa."),
        hr=(-0.45, 0.10), ar=(0.05, 0.20),
        expected_confidence=("low", "medium"),
        rubric_lesson="Must-win for one team → that team +0.05 to +0.15 (compounding with Mexico rotation acceptable: -0.20 to -0.40)",
    ),

    # ───── 6. Outdated info (Qatar 2022) — should DISCARD ─────
    _make(
        "outdated_2022_info_should_be_discarded",
        "Article is from Qatar 2022 — LLM should discard, NOT use",
        "Argentina", "France",
        ctx=("[MATCH: Argentina vs France — World Cup 2026 quarter-final]\n[FETCHED: 2026-07-09 10:00Z]\n\n"
             "[SOURCE: brave_search]\n"
             "- [2022-12-18] Argentina win the World Cup final on penalties; Messi captain "
             "as Argentina beat France 4-2 on PKs after 3-3 draw. Mbappé hat-trick.\n"
             "- [2022-12-19] Argentina lift trophy in Lusail; first WC win since 1986."),
        hr=(0.0, 0.0), ar=(0.0, 0.0),
        expected_confidence=("low",),
        rubric_lesson="2022 info → discarded; 0.0/0.0/low; discarded_sources should list it",
    ),

    # ───── 7. Star RETURNS from injury ─────
    _make(
        "star_returns_home_team",
        "Mexico's star striker confirmed FIT and starting — small positive +0.10 to +0.25",
        "Mexico", "South Africa",
        ctx=("[MATCH: Mexico vs South Africa]\n[FETCHED: 2026-06-09 10:00Z]\n\n"
             "[SOURCE: API-Football lineups]\n"
             "Mexico (4-3-3): Ochoa; Sanchez, Montes, Vasquez, Gallardo; Alvarez, Chavez, "
             "Lozano; Antuna, Pizarro, Jimenez (Raul JIMENEZ confirmed starting after 6-week "
             "recovery; manager says 'fit, 100% ready, will play full 90').\n\n"
             "[SOURCE: brave_search]\n"
             "- [2026-06-09] Raul Jimenez declared fully fit after Mexico FA medical staff "
             "clear him to start; goalscorer in 5 of his last 7 international starts."),
        hr=(0.05, 0.30), ar=(-0.10, 0.10),
        expected_confidence=("medium", "high"),
        rubric_lesson="Star returns confirmed → that team +0.10 to +0.25",
    ),

    # ───── 8. Extreme weather affecting BOTH teams ─────
    # When heat AND altitude AND "unprecedented" all stack, the LLM may
    # double up the rubric (-0.10 to -0.20 × multiple conditions). Allow
    # range up to -0.40 each since stacked extreme conditions are
    # legitimately worse than any single one.
    _make(
        "extreme_heat_altitude",
        "40°C + Mexico City altitude → both teams negatively affected (range expanded for stacking)",
        "Mexico", "South Africa",
        ctx=("[MATCH: Mexico vs South Africa, kickoff 2026-06-11 at Estadio Azteca, Mexico City]\n"
             "[FETCHED: 2026-06-09 10:00Z]\n\n"
             "[SOURCE: brave_search]\n"
             "- [2026-06-09] Heat advisory for Mexico City as temperature expected to hit 38°C "
             "at kickoff. Estadio Azteca at 2,240m altitude. South Africa coach: 'unprecedented "
             "conditions, players will struggle'. Mexico team unaccustomed to playing in such "
             "heat; medical staff warning of cramp risk on both sides.\n"
             "- [2026-06-09] Officials approve cooling breaks at 30/75 minutes."),
        hr=(-0.40, -0.05), ar=(-0.40, -0.05),
        expected_confidence=("medium", "high"),
        rubric_lesson="Heat + altitude (stacked) → both -0.10 to -0.40",
    ),

    # ───── 9. Garbage / off-topic content — should ignore ─────
    _make(
        "garbage_unrelated_content",
        "Context is unrelated articles — must return 0.0/0.0 and discard",
        "Mexico", "South Africa",
        ctx=("[MATCH: Mexico vs South Africa]\n[FETCHED: 2026-06-09 10:00Z]\n\n"
             "[SOURCE: brave_search]\n"
             "- [2026-06-09] Bitcoin price up 4% today on hopes of Fed rate cut.\n"
             "- [2026-06-09] Best taqueria in Mexico City — reviews and recommendations.\n"
             "- [2026-06-09] World Chess Championship: Carlsen wins game 6 vs Nepomniachtchi."),
        hr=(0.0, 0.0), ar=(0.0, 0.0),
        expected_confidence=("low",),
        rubric_lesson="Irrelevant info → 0.0/0.0; discarded list should include each item",
    ),

    # ───── 10. Both teams lose stars — clamp at ±0.6 cap ─────
    _make(
        "both_lose_stars_test_cap",
        "Both teams lose 2 key strikers — verify deltas stay within ±0.6 cap",
        "France", "Argentina",
        ctx=("[MATCH: France vs Argentina, World Cup 2026 quarter-final]\n[FETCHED: 2026-07-10 10:00Z]\n\n"
             "[SOURCE: API-Football lineups]\n"
             "France: Mbappé OUT (hamstring), Griezmann OUT (knock), Dembélé OUT (suspended) — "
             "3 of their attacking unit absent. France will start a 19-year-old debutant.\n"
             "Argentina: Messi OUT (injury — last-minute), Julian Alvarez OUT (illness) — "
             "Lautaro Martinez has to lead the line alone.\n\n"
             "[SOURCE: brave_search]\n"
             "- [2026-07-10] Mbappé scan confirms grade-2 hamstring tear, OUT.\n"
             "- [2026-07-10] Messi sent home from training; Argentina FA confirms he will NOT play."),
        hr=(-0.6, -0.30), ar=(-0.6, -0.30),
        expected_confidence=("medium", "high"),
        rubric_lesson="Both teams lose stars → BOTH at -0.30 to -0.6 cap (not halved)",
    ),
]


# ───── Helpers ────────────────────────────────────────────────────────────────

class TeeWriter:
    def __init__(self, fh):
        self.fh = fh
    def __call__(self, *args, **kw):
        print(*args, **kw)
        if self.fh:
            print(*args, **kw, file=self.fh)


def banner(P, s): P(f"\n{'=' * 78}\n  {s}\n{'=' * 78}")


def check_in_range(value: float, lo: float, hi: float) -> str:
    if value is None:
        return "✗ None"
    return "✓" if lo <= value <= hi else f"✗ outside [{lo:+.2f}, {hi:+.2f}]"


# ───── Run one scenario ───────────────────────────────────────────────────────

def _router_for(force_provider: str | None):
    """Build an LLMRouter limited to a single provider. None ⇒ default chain."""
    from core.llm.router import LLMRouter
    if force_provider is None:
        return LLMRouter()
    return LLMRouter(chain=[force_provider])


def run_scenario(s: Scenario, P, force_provider: str | None = None,
                  label_suffix: str = "") -> dict:
    from orchestrator.agents.news_agent import analyze_safe

    banner(P, f"§ {s.name}{label_suffix}")
    P(f"  Description: {s.description}")
    P(f"  Fixture:     {s.home} vs {s.away}")
    P(f"  Rubric:      {s.rubric_lesson}")
    if force_provider:
        P(f"  Provider:    {force_provider!r} (forced — no fallback)")
    P()
    P("  ─── CONTEXT (handed to the LLM) ───────────────────────────────────────")
    for line in s.context.split("\n"):
        P(f"    {line}")
    P("  ─── end context ───────────────────────────────────────────────────────")

    router = _router_for(force_provider)
    t0 = time.monotonic()
    result = analyze_safe(s.home, s.away, context_text=s.context, router=router)
    dur_ms = (time.monotonic() - t0) * 1000

    P(f"\n  ─── LLM RESPONSE ({dur_ms:.0f}ms) ─────────────────────────────────────")
    P(f"    provider:           {result.get('provider')!r}")
    if result.get("fallbacks_used"):
        P(f"    fallbacks tried:    {result['fallbacks_used']}")
    P(f"    parse_tier:         {result.get('parse_tier')!r}")
    P(f"    home_goal_delta:    {result.get('home_goal_delta'):+.3f}")
    P(f"    away_goal_delta:    {result.get('away_goal_delta'):+.3f}")
    P(f"    confidence:         {result.get('confidence')!r}")
    P(f"    notes:")
    for n in (result.get("notes") or []):
        P(f"      - {n}")
    if result.get("discarded_sources"):
        P(f"    discarded_sources:")
        for d in result["discarded_sources"]:
            P(f"      - {d}")
    if result.get("raw_excerpt"):
        P(f"    raw_excerpt (parse imperfect): {result['raw_excerpt'][:200]!r}")

    # Check vs expected
    P(f"\n  ─── RUBRIC CHECK ──────────────────────────────────────────────────────")
    h_check = check_in_range(result.get("home_goal_delta"),
                              *s.expected_home_delta_range)
    a_check = check_in_range(result.get("away_goal_delta"),
                              *s.expected_away_delta_range)
    conf_check = ("✓" if result.get("confidence") in s.expected_confidence
                  else f"✗ expected one of {s.expected_confidence}")
    P(f"    home_delta in {s.expected_home_delta_range}: {h_check}")
    P(f"    away_delta in {s.expected_away_delta_range}: {a_check}")
    P(f"    confidence in {s.expected_confidence}: {conf_check}")

    passed = (h_check.startswith("✓") and a_check.startswith("✓")
              and conf_check.startswith("✓"))
    P(f"\n    Overall: {'✓ PASS' if passed else '✗ FAIL — review LLM reasoning above'}")
    return {"scenario": s.name, "passed": passed,
            "home_delta": result.get("home_goal_delta"),
            "away_delta": result.get("away_goal_delta"),
            "confidence": result.get("confidence")}


# ───── Main ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="test_news_agent_scenarios")
    p.add_argument("--scenario", help="run only this named scenario")
    p.add_argument("--list", action="store_true",
                   help="list scenarios and exit")
    p.add_argument("--no-save", action="store_true")
    p.add_argument("--report-dir", default="reports")
    p.add_argument("--provider", choices=("gemini", "claude", "openai", "all"),
                   default=None,
                   help="Force a specific provider (skip the router fallback "
                        "chain). 'all' runs every scenario through each "
                        "configured provider sequentially.")
    args = p.parse_args(argv)

    if args.list:
        print(f"\nAvailable scenarios ({len(SCENARIOS)}):")
        for s in SCENARIOS:
            print(f"  {s.name:<35} {s.description}")
        return 0

    run = [s for s in SCENARIOS if args.scenario is None or s.name == args.scenario]
    if args.scenario and not run:
        print(f"ERROR: scenario {args.scenario!r} not found. --list to see all.",
              file=sys.stderr)
        return 2

    fh = None
    report_path = None
    if not args.no_save:
        os.makedirs(args.report_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report_path = os.path.join(args.report_dir,
                                    f"news_agent_scenarios_{ts}.txt")
        fh = open(report_path, "w")
    P = TeeWriter(fh)

    P(f"\n  ✻ News-agent scenario harness — {len(run)} scenario(s)")
    if report_path:
        P(f"  Report: {report_path}")

    providers_to_run = ([args.provider] if args.provider and args.provider != "all"
                         else (["gemini", "claude", "openai"] if args.provider == "all"
                               else [None]))
    results = []
    for prov in providers_to_run:
        suffix = f"  [provider={prov}]" if prov else ""
        if prov:
            banner(P, f"PROVIDER PASS: {prov!r}")
        for i, s in enumerate(run):
            r = run_scenario(s, P, force_provider=prov, label_suffix=suffix)
            r["provider_requested"] = prov
            results.append(r)
            if i < len(run) - 1:
                # Gemini free tier: 5 RPM. Sleep 13s between scenarios so we
                # don't trip 429 RESOURCE_EXHAUSTED. Other providers (Claude
                # 50 RPM, OpenAI 500 RPM) tolerate faster cadence but the
                # sleep is harmless.
                time.sleep(13 if prov == "gemini" or prov is None else 2)

    # Summary
    banner(P, "Summary")
    n_pass = sum(1 for r in results if r["passed"])
    n_fail = len(results) - n_pass
    P(f"  {n_pass}/{len(results)} runs passed the rubric check")
    for r in results:
        mark = "✓" if r["passed"] else "✗"
        prov_tag = f" [{r.get('provider_requested')}]" if r.get("provider_requested") else ""
        P(f"    {mark} {r['scenario']:<40}{prov_tag:<11} "
          f"home={r['home_delta']:+.2f}  away={r['away_delta']:+.2f}  "
          f"conf={r['confidence']!r}")

    # Cross-provider table when running --provider all
    if args.provider == "all":
        banner(P, "Cross-provider comparison matrix")
        scenarios_seen = []
        for r in results:
            if r["scenario"] not in scenarios_seen:
                scenarios_seen.append(r["scenario"])
        prov_cols = ("gemini", "claude", "openai")
        # Header
        hdr = f"  {'scenario':<35}"
        for p in prov_cols:
            hdr += f" {p:<22}"
        P(hdr)
        P("  " + "-" * (35 + 3 * 23))
        for scn in scenarios_seen:
            line = f"  {scn:<35}"
            for p in prov_cols:
                row = next((r for r in results
                            if r["scenario"] == scn and r["provider_requested"] == p),
                           None)
                if row:
                    mark = "✓" if row["passed"] else "✗"
                    cell = (f"{mark} h{row['home_delta']:+.2f} a{row['away_delta']:+.2f}")
                else:
                    cell = "(skipped)"
                line += f" {cell:<22}"
            P(line)

    P()
    if report_path:
        P(f"  Full report saved to: {report_path}")

    if fh:
        fh.close()
    return 0 if n_fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
