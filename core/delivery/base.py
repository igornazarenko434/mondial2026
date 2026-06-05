"""Notifier interface + card rendering. Channels implement `send(title, body)`."""
from __future__ import annotations
import abc


class Notifier(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def available(self) -> bool: ...

    @abc.abstractmethod
    def send(self, title: str, body: str) -> None: ...


def _pct(v) -> str:
    return f"{v * 100:.0f}%" if isinstance(v, (int, float)) else "?"


def _ev_text(ev) -> str:
    """Format expected_points whether it's a plain float (current ev_optimizer
    output) or a {direction, exact, with_detonator} dict (Day 6 build_card)."""
    if isinstance(ev, (int, float)):
        return f"{ev:.2f}"
    if isinstance(ev, dict):
        parts = []
        if "direction" in ev: parts.append(f"direction {ev['direction']:.2f}")
        if "exact" in ev: parts.append(f"exact {ev['exact']:.2f}")
        if "with_detonator" in ev: parts.append(f"w/ detonator {ev['with_detonator']:.2f}")
        return ", ".join(parts) if parts else "?"
    return "?"


def render_card(card: dict) -> str:
    """Recommendation dict -> human-readable plain-text card the user reads
    before betting. Plain text (no Markdown) — Telegram-safe and identical in
    file/console output. Follows the blueprint §9 shape, degrades gracefully on
    missing fields so the degradation-ladder paths still produce a sendable card.
    """
    home = card.get("home", "Home")
    away = card.get("away", "Away")
    stage = card.get("stage", "?")
    group = card.get("group")
    when = card.get("kickoff_local")
    det = "  ⚡ DETONATOR x2" if card.get("detonator") else ""

    odds = card.get("locked_odds") or {}
    prob = card.get("model_prob") or {}
    pick = card.get("pick_exact_score") or {}
    modal = card.get("modal_score") or {}

    dir_code = card.get("pick_direction", "?")
    dir_label = {"H": f"{home} win", "D": "Draw", "A": f"{away} win"}.get(dir_code, str(dir_code))

    header = f"⚽ {home} vs {away}"
    if when:
        header += f" — {when}"
    header += f" ({stage}{(' ' + group) if group else ''}){det}"
    lines = [header]

    if odds:
        lines.append(
            f"Locked odds: {home} {odds.get('H','?')} / Draw {odds.get('D','?')} / {away} {odds.get('A','?')}"
        )
    if prob:
        lines.append(
            f"Model: {home} {_pct(prob.get('H'))} / Draw {_pct(prob.get('D'))} / {away} {_pct(prob.get('A'))}"
        )

    pick_line = f"► Direction: {dir_label}"
    if pick:
        pick_line += f"    ► Exact: {home} {pick.get('home','?')} — {away} {pick.get('away','?')}"
    lines.append(pick_line)

    if modal:
        lines.append(f"   (likeliest: {home} {modal.get('home','?')} — {away} {modal.get('away','?')})")

    lines.append(f"Expected points ≈ {_ev_text(card.get('expected_points'))}")

    for note in card.get("context") or []:
        lines.append(f"ℹ {note}")

    return "\n".join(lines)
