"""Notifier interface + card rendering (Day 6: audit-trail + penalty line).

Rendering rules per docs/BLUEPRINT.md §9 + CLAUDE.md Day-6 spec:
  * Plain text — no Markdown (Telegram-safe).
  * ≤8 lines normal, ≤9 on knockout+penalty branch.
  * Signals line is mandatory: shows what fed the pick + inline failures.
  * If pens line ONLY on knockouts with draw_prob >= DRAW_PEN_THRESHOLD.
  * Context truncated to 2 bullets, joined on one line.
"""
from __future__ import annotations
import abc


# Display caps (a strict assertion in tests; logged warning at runtime).
MAX_LINES_NORMAL = 8
MAX_LINES_KO_PEN = 9


class Notifier(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def available(self) -> bool: ...

    @abc.abstractmethod
    def send(self, title: str, body: str) -> None: ...


def _pct(v) -> str:
    return f"{v * 100:.0f}%" if isinstance(v, (int, float)) else "?"


def _ev_text(ev, detonator: bool) -> str:
    """Format expected_points (float or dict).

    Day-9.25: previously this multiplied `ev` by 2 again when detonator was
    True, producing a misleading "EV ≈ 3.37 → ×2 detonator ≈ 6.74" line.
    But `recommend()` (core/decision/ev_optimizer.py:36) ALREADY multiplies
    by DETONATOR_FACTOR=2 inside the EV formula, so the stored
    expected_points value IS the detonator-included expectation. The
    arrow-line was applying ×2 a SECOND time and presenting 4× the no-
    detonator EV as if it were the upside. `test_detonator_doubles_ev`
    pins this: `expected_points(det=True) == 2 * expected_points(det=False)`.

    Fix: present the value AS-IS plus an honest annotation that the ×2 is
    already included. The user still sees the detonator badge in line 1's
    "⚡ DETONATOR x2" suffix.
    """
    if isinstance(ev, (int, float)):
        s = f"{ev:.2f}"
        if detonator:
            s += "  (×2 detonator already applied)"
        return s
    if isinstance(ev, dict):
        parts = []
        if "direction" in ev: parts.append(f"direction {ev['direction']:.2f}")
        if "exact" in ev:     parts.append(f"exact {ev['exact']:.2f}")
        if "with_detonator" in ev:
            parts.append(f"w/ detonator {ev['with_detonator']:.2f}")
        return ", ".join(parts) if parts else "?"
    return "?"


_SIGNAL_LABEL = {"dixon_coles": "DC", "elo": "Elo",
                 "market": "Market", "news": "News", "model": "Model"}


def _signals_line(card: dict) -> str:
    """Audit-trail line. Compact: 'Signals: DC+Elo+Market+News(gemini)' on the
    happy path; with failures: 'Signals: DC+Elo  ⚠market: budget   ⚠news: 429'.

    The (provider) suffix appears next to News when known (gemini / claude /
    openai) so the user sees WHICH model produced the news output. On news
    failure, the ⚠ annotation also shows the failure reason.
    """
    used = card.get("signals_used") or []
    failed = card.get("signals_failed") or []
    reasons = card.get("failure_reasons") or {}
    news_provider = card.get("news_provider")
    parts = []
    if used:
        labels = []
        for s in used:
            label = _SIGNAL_LABEL.get(s, s)
            if s == "news" and news_provider:
                label = f"{label}({news_provider})"
            labels.append(label)
        parts.append("Signals: " + "+".join(labels))
    else:
        parts.append("Signals: (none)")
    for s in failed:
        # trim each reason short on render so the line stays Telegram-friendly
        r = (reasons.get(s) or "")[:50]
        label = _SIGNAL_LABEL.get(s, s).lower()
        parts.append(f"  ⚠{label}: {r}")
    return "".join(parts)


def _penalty_line(card: dict) -> str | None:
    """► If pens: <team> (XX%) — only when penalty_winner is set on the card."""
    pen = card.get("penalty_winner")
    if not pen:
        return None
    home, away = card.get("home", "Home"), card.get("away", "Away")
    winner_team = home if pen.get("winner") == "H" else away
    p = pen.get("p_winner", 0.5)
    return f"► If pens: {winner_team} ({p * 100:.0f}%)"


def _top5_candidates_section(card: dict) -> str | None:
    """Day-9.26: render the top-5 EV alternatives so the operator can see WHY
    a pick was chosen and what the next-best options were. Marks the cell that
    was actually picked (which may differ from #1 when the direction gate is
    active). Also surfaces the gate decision in plain English.

    Format (appended after the existing ≤8/≤9-line cap, like friend_picks):

        ─────────────────  📊 Top 5 candidates  ──────────────────
        1. 2-0 (H)  P=15.6%  ×2.25  EV=2.56  ← picked
        2. 0-0 (D)  P= 9.6%  ×2.75  EV=3.51
        3. 1-1 (D)  P=10.0%  ×2.25  EV=3.12
        4. 3-0 (H)  P= 8.4%  ×3.25  EV=2.54
        5. 2-1 (H)  P=10.1%  ×1.50  EV=2.21
        Gate: dominant H @ 68% (≥55%) → restricted to H cells.

    The block is suppressed when ranked_alternatives is empty (the modal-
    fallback path with no usable odds).
    """
    ranked = card.get("ranked_alternatives") or []
    if not ranked:
        return None
    pick = card.get("pick_exact_score") or {}
    ph, pa = pick.get("home"), pick.get("away")

    rows = ["─────────────────  📊 Top 5 candidates  ─────────────────"]
    for n, r in enumerate(ranked[:5], 1):
        h, a, d = r.get("home"), r.get("away"), r.get("direction", "?")
        p = r.get("p_score")
        mult = r.get("exact_multiplier")
        ev = r.get("expected_points")
        marker = "  ← picked" if (h == ph and a == pa) else ""
        p_str  = f"P={p*100:5.1f}%" if isinstance(p, (int, float)) else "P=  ?"
        m_str  = f"×{mult:.2f}"     if isinstance(mult, (int, float)) else "× ?"
        ev_str = f"EV={ev:.2f}"     if isinstance(ev, (int, float)) else "EV= ?"
        rows.append(f"  {n}. {h}-{a} ({d})  {p_str}  {m_str}  {ev_str}{marker}")

    # If the picked cell is not in top-5 by raw EV (i.e. the gate moved it
    # outside the EV top-5), append it explicitly so the reader knows where
    # it came from. Should be rare but possible when the mild-favorite gate's
    # half-EV-half-P score lands on a mid-table cell.
    in_top5 = any(r.get("home") == ph and r.get("away") == pa for r in ranked[:5])
    if not in_top5 and ph is not None and pa is not None:
        rows.append(f"  ← picked: {ph}-{pa} (outside top-5 by raw EV — gate selected)")

    note = card.get("gate_note")
    if note:
        rows.append(f"Gate: {note}")
    return "\n".join(rows)


def render_card(card: dict) -> str:
    """Recommendation dict → compact, plain-text human card.

    Layout (line count bound by MAX_LINES_NORMAL = 8, or MAX_LINES_KO_PEN = 9
    when penalty_winner is set):
      1. Header        : ⚽ <h> vs <a> — <when> (<stage>[ <group>])  [⚡DETONATOR]
      2. Locked odds   : <h> 1.85 / Draw 3.60 / <a> 4.20
      3. Model         : <h> 22% / Draw 26% / <a> 52%
      4. Pick + Exact  : ► Pick: <a> win    Exact: <h> 1 — <a> 2
      5. (likeliest)   : (likeliest: <h> 0 — <a> 1)   [omitted if modal==pick]
      6. If pens line  : ► If pens: <a> (51%)         [only KO+draw branch]
      7. Expected pts  : Expected points ≈ 1.90  → ×2 detonator ≈ 3.80
      8. Signals       : Signals: DC+Elo+Market+News [+ inline ⚠ failures]
      9. Context       : ℹ <bullet 1>    ℹ <bullet 2>  [≤2 bullets, joined]
    """
    home = card.get("home", "Home")
    away = card.get("away", "Away")
    stage = card.get("stage", "?")
    group = card.get("group")
    when = card.get("kickoff_local")
    det = "  ⚡ DETONATOR x2" if card.get("detonator") else ""

    odds  = card.get("locked_odds") or {}
    prob  = card.get("model_prob")  or {}
    pick  = card.get("pick_exact_score") or {}
    modal = card.get("modal_score") or {}

    dir_code = card.get("pick_direction", "?")
    dir_label = {"H": f"{home} win", "D": "Draw",
                 "A": f"{away} win"}.get(dir_code, str(dir_code))

    # 1. Header — Day-9.12: surface the window label so the user can tell at
    # a glance whether this card is the LOCK (T-7m, scoring-decisive) or one
    # of the earlier previews. Only set when card.window is present (existing
    # tests / older callers without `window` render the legacy header).
    window = card.get("window")
    win_tag = {"T-24h": "[T-24h]", "T-60m": "[T-60m]",
                "T-15m": "[T-15m]", "T-7m":  "[T-7m LOCK]"}.get(window, "")
    header = f"⚽ {home} vs {away}"
    if when:
        header += f" — {when}"
    if win_tag:
        header += f" {win_tag}"
    header += f" ({stage}{(' ' + group) if group else ''}){det}"
    lines = [header]

    # 2. Locked odds
    if odds:
        lines.append(
            f"Locked odds: {home} {odds.get('H','?')} / Draw {odds.get('D','?')} / "
            f"{away} {odds.get('A','?')}")

    # 3. Model probs
    if prob:
        lines.append(
            f"Model: {home} {_pct(prob.get('H'))} / Draw {_pct(prob.get('D'))} / "
            f"{away} {_pct(prob.get('A'))}")

    # 4. Pick + Exact (combined onto one line)
    # Day-9.12: when build_card had to fall back to modal-pick because live
    # odds weren't available (ev_pathway == "modal_fallback"), append a tiny
    # tag so the reader knows this pick is NOT EV-optimal — it's the most-
    # likely score the model saw. Distinguishes a fully-fed card from one
    # that ran on a degraded signal mix.
    pick_line = f"► Pick: {dir_label}"
    if pick:
        pick_line += f"    Exact: {home} {pick.get('home','?')} — {away} {pick.get('away','?')}"
    if card.get("ev_pathway") == "modal_fallback":
        pick_line += "  [no live odds]"
    lines.append(pick_line)

    # 5. Modal — show only if it differs from the pick (avoid noise)
    if modal and (modal.get("home") != pick.get("home")
                   or modal.get("away") != pick.get("away")):
        lines.append(
            f"   (likeliest: {home} {modal.get('home','?')} — {away} {modal.get('away','?')})")

    # 6. Penalty winner line — KO + draw threshold reached
    pen = _penalty_line(card)
    if pen:
        lines.append(pen)

    # 7. Expected points
    lines.append(f"Expected points ≈ {_ev_text(card.get('expected_points'), card.get('detonator', False))}")

    # 8. Signals (audit trail)
    lines.append(_signals_line(card))

    # 9. Context — at most 2 bullets, joined on one line, each <= 60 chars
    ctx = (card.get("context") or [])[:2]
    if ctx:
        bullets = "    ".join(f"ℹ {str(c)[:60]}" for c in ctx)
        lines.append(bullets)

    # Enforce the line-count cap. Runtime: log warning + truncate so the card
    # still gets delivered. Tests: see test_render_card_day6 — they assert it.
    cap = MAX_LINES_KO_PEN if pen else MAX_LINES_NORMAL
    if len(lines) > cap:
        import logging
        logging.getLogger("delivery").warning(
            "render_card overflowed cap (%d > %d); truncating", len(lines), cap)
        lines = lines[:cap]

    # Day-9.26: append the top-5 candidates considered (with the picked one
    # marked + the direction-gate decision in plain English). Like the
    # friend-picks footer, this is supplementary and bypasses the line cap
    # because it's analytic context the operator should always see.
    top5 = _top5_candidates_section(card)
    if top5:
        lines.append(top5)

    # Day-9.22: append tracked-friends' picks footer (when configured) AFTER
    # the model cap. The cap exists to keep the model output compact; the
    # picks footer is supplementary social context and shouldn't be truncated.
    section = card.get("friend_picks_section")
    if section:
        lines.append(section)

    # Day-9.24: per-person strategy suggestions — one row per tracked
    # participant with their OWN EV-optimal pick based on their tilt +
    # standings context. Appended after friend_picks_section so the social
    # layer (who picked what) and the analytic layer (what the model
    # recommends per person) stack cleanly.
    pp_section = card.get("per_person_section")
    if pp_section:
        lines.append(pp_section)

    return "\n".join(lines)
