"""Delivery layer: render_card formatting + TelegramNotifier payload shape.

Why this test file exists: every Telegram send in the pipeline (cards, alerts,
health summaries) goes through TelegramNotifier.send. A prior bug used
parse_mode='Markdown' on bodies that contain underscores like 'with_detonator',
which Telegram rejected as 400. These tests pin the contract so it doesn't
regress.
"""
from __future__ import annotations
from unittest.mock import patch, MagicMock

from core.delivery.base import render_card
from core.delivery.channels import TelegramNotifier


# ---------- render_card ----------

def _full_card():
    return {
        "home": "Norway", "away": "France",
        "stage": "Group", "group": "I",
        "kickoff_local": "2026-06-26 22:00",
        "detonator": True,
        "locked_odds": {"H": 4.20, "D": 3.60, "A": 1.85},
        "model_prob":  {"H": 0.22, "D": 0.26, "A": 0.52},
        "pick_exact_score": {"home": 1, "away": 2},
        "pick_direction": "A",
        "modal_score": {"home": 0, "away": 1},
        "expected_points": 1.90,
        "context": ["Norway likely rotates", "Mbappé confirmed starts"],
    }


def test_render_card_uses_team_names_not_letters():
    out = render_card(_full_card())
    # team names appear; raw H/D/A codes from pick_direction don't leak.
    assert "Norway" in out and "France" in out
    # Day-6 label: "► Pick: <team> win" (was "► Direction:" pre-Day-6).
    assert "► Pick: France win" in out
    # kickoff included
    assert "2026-06-26 22:00" in out
    # detonator note shown
    assert "DETONATOR" in out
    # exact pick shown with team names
    assert "Norway 1" in out and "France 2" in out
    # likeliest (modal) shown
    assert "likeliest" in out and "Norway 0" in out and "France 1" in out
    # expected points formatted to 2dp, not dict repr
    assert "Expected points ≈ 1.90" in out
    # context bullets present
    assert "Mbappé confirmed starts" in out


def test_render_card_handles_dict_expected_points():
    """Day 6 build_card may emit a structured dict; render must format it cleanly."""
    card = _full_card()
    card["expected_points"] = {"direction": 0.96, "exact": 1.90, "with_detonator": 3.80}
    out = render_card(card)
    assert "direction 0.96" in out
    assert "exact 1.90" in out
    assert "w/ detonator 3.80" in out
    # never the raw Python dict repr (the prior bug)
    assert "{'direction'" not in out


def test_render_card_no_markdown_asterisks():
    """Plain text only — no **bold** or *italic*, since Telegram is sent without parse_mode."""
    out = render_card(_full_card())
    assert "**" not in out
    # single asterisks must not appear as Telegram-Markdown emphasis either
    assert "*Norway*" not in out and "*France*" not in out


def test_render_card_degrades_on_missing_fields():
    """Graceful-degradation ladder: render must not raise even on a minimal card."""
    minimal = {"home": "A", "away": "B", "stage": "Group",
               "pick_direction": "H",
               "pick_exact_score": {"home": 1, "away": 0},
               "expected_points": 1.0,
               "model_prob": {"H": 0.5, "D": 0.3, "A": 0.2},
               "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}
    out = render_card(minimal)
    assert "A vs B" in out and "A win" in out and "Expected points ≈ 1.00" in out


# ---------- TelegramNotifier ----------

def test_telegram_send_uses_plain_text_no_parse_mode(monkeypatch):
    """The bug we're pinning: payload must NOT include parse_mode and the
    title must NOT be wrapped in markdown emphasis."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    captured = {}
    fake_resp = MagicMock(); fake_resp.raise_for_status = MagicMock()
    def _post(url, json=None, timeout=None):
        captured["url"], captured["json"], captured["timeout"] = url, json, timeout
        return fake_resp

    with patch("requests.post", side_effect=_post):
        TelegramNotifier().send("Norway vs France — pick", "body with_detonator underscore")

    assert captured["url"].endswith("/bottok/sendMessage")
    payload = captured["json"]
    assert payload["chat_id"] == "42"
    assert "parse_mode" not in payload, "parse_mode must be unset (avoids 400 on underscores)"
    # title appears as-is, not wrapped in * or **
    assert payload["text"].startswith("Norway vs France — pick\n")
    assert "*Norway" not in payload["text"]


def test_telegram_not_available_when_creds_missing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramNotifier().available() is False
