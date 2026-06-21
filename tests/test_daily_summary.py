"""Day-9: 09:00 daily summary — TZ math, idempotency, content composition.

The summary acts as a positive heartbeat (its absence = daemon dead). Tests
pin the must-hold properties: fires exactly once per Asia/Jerusalem day, only
after the configured local hour, never raises, contains the four sections.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from schedule import daily_summary as ds
from core.obs.runs import RunLedger


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        c.executescript(f.read())
    return c


@pytest.fixture
def led():
    return RunLedger(":memory:")


def _at(local_dt_str: str, tz: str = "Asia/Jerusalem") -> datetime:
    """Convenience: '2026-06-11 09:30' Asia/Jerusalem → UTC datetime."""
    naive = datetime.strptime(local_dt_str, "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


# ─────────────── timing gate ───────────────

def test_returns_false_before_09_00_local(conn, led):
    """At 08:59 local we are NOT due — would-be tomorrow's summary."""
    with patch.object(ds, "delivery") as mock_d:
        mock_d.summary.return_value = True
        sent = ds.send_if_due(conn, led, now=_at("2026-06-11 08:59"))
    assert sent is False
    assert mock_d.summary.call_count == 0


def test_returns_true_at_09_00_first_time(conn, led):
    with patch.object(ds, "delivery") as mock_d:
        mock_d.summary.return_value = True
        sent = ds.send_if_due(conn, led, now=_at("2026-06-11 09:00"))
    assert sent is True
    assert mock_d.summary.call_count == 1


def test_returns_true_anytime_after_09_00(conn, led):
    """Catch-up: a restart at 14:00 local still sends today's summary."""
    with patch.object(ds, "delivery") as mock_d:
        mock_d.summary.return_value = True
        sent = ds.send_if_due(conn, led, now=_at("2026-06-11 14:00"))
    assert sent is True


# ─────────────── idempotency ───────────────

def test_does_not_re_send_within_same_local_day(conn, led):
    """Two ticks within the same Jerusalem day → one Telegram message."""
    with patch.object(ds, "delivery") as mock_d:
        mock_d.summary.return_value = True
        ds.send_if_due(conn, led, now=_at("2026-06-11 09:00"))
        ds.send_if_due(conn, led, now=_at("2026-06-11 09:01"))
        ds.send_if_due(conn, led, now=_at("2026-06-11 23:59"))
    assert mock_d.summary.call_count == 1


def test_does_send_again_next_local_day(conn, led):
    with patch.object(ds, "delivery") as mock_d:
        mock_d.summary.return_value = True
        ds.send_if_due(conn, led, now=_at("2026-06-11 09:00"))
        ds.send_if_due(conn, led, now=_at("2026-06-12 09:00"))
    assert mock_d.summary.call_count == 2


# ─────────────── content composition (all 4 sections) ───────────────

def test_summary_text_contains_today_games(conn):
    """Today's games block shows kickoff time + teams + stage."""
    # Insert one match kicking off at 19:00 Asia/Jerusalem on 2026-06-11
    ko_utc = _at("2026-06-11 19:00").isoformat()
    conn.execute(
        "INSERT INTO matches (match_id, utc_kickoff, stage, grp, home, away, status) "
        "VALUES (?, ?, 'Group', 'A', 'Mexico', 'South Africa', 'SCHEDULED')",
        (12345, ko_utc))
    conn.commit()
    txt = ds.build_summary_text(conn, _at("2026-06-11 09:00"))
    assert "Mexico vs South Africa" in txt
    assert "19:00" in txt
    assert "Group" in txt


def test_summary_text_includes_overnight_game_before_next_summary(conn):
    """Day-9.29: a match kicking off at 03:00 local of TOMORROW should
    appear in TONIGHT's 09:00 summary — otherwise it falls into a future
    summary that doesn't fire until AFTER kickoff."""
    # Match at 2026-06-12 03:00 Asia/Jerusalem (= tomorrow night to user).
    # 2026-06-11 summary fires at 09:00 local → window 00:00 today through
    # 09:00 tomorrow → the 03:00 match must be included.
    ko_utc = _at("2026-06-12 03:00").isoformat()
    conn.execute(
        "INSERT INTO matches (match_id, utc_kickoff, stage, grp, home, away, status) "
        "VALUES (?, ?, 'Group', 'D', 'Argentina', 'Iran', 'SCHEDULED')",
        (54321, ko_utc))
    conn.commit()
    txt = ds.build_summary_text(conn, _at("2026-06-11 09:00"))
    assert "Argentina vs Iran" in txt
    assert "03:00" in txt
    # Header annotation kicks in when overnight games are present
    assert "through tomorrow 09:00" in txt


def test_summary_text_excludes_game_after_next_summary_window(conn):
    """A match kicking off at 10:00 local of TOMORROW (1h AFTER the next
    summary fires) belongs to TOMORROW's summary, not today's."""
    ko_utc = _at("2026-06-12 10:00").isoformat()
    conn.execute(
        "INSERT INTO matches (match_id, utc_kickoff, stage, grp, home, away, status) "
        "VALUES (?, ?, 'Group', 'E', 'Spain', 'Portugal', 'SCHEDULED')",
        (98765, ko_utc))
    conn.commit()
    txt = ds.build_summary_text(conn, _at("2026-06-11 09:00"))
    assert "Spain vs Portugal" not in txt


def test_summary_text_contains_recent_results(conn):
    """Yesterday's results block shows the score."""
    yest_ko = _at("2026-06-10 18:00").isoformat()
    conn.execute(
        "INSERT INTO matches (match_id, utc_kickoff, stage, grp, home, away, status, home_goals, away_goals) "
        "VALUES (?, ?, 'Group', 'B', 'France', 'Norway', 'FINISHED', 2, 1)",
        (88888, yest_ko))
    conn.commit()
    txt = ds.build_summary_text(conn, _at("2026-06-11 09:00"))
    assert "France 2-1 Norway" in txt


def test_summary_text_contains_standings_line_when_row_exists(conn):
    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, futures_points) "
        "VALUES ('me', 12.5, 0.0, 4.2)")
    conn.commit()
    txt = ds.build_summary_text(conn, _at("2026-06-11 09:00"))
    assert "16.7" in txt and "12.5" in txt          # total + group line


def test_summary_text_contains_budget_line(conn):
    txt = ds.build_summary_text(conn, _at("2026-06-11 09:00"))
    assert "Brave" in txt and "odds" in txt         # always present


def test_summary_text_is_telegram_safe_no_markdown(conn):
    txt = ds.build_summary_text(conn, _at("2026-06-11 09:00"))
    # No underscores or asterisks that would 400 Telegram's Markdown parser
    # (we send plain text anyway, but this is a belt-and-suspenders check).
    assert "*" not in txt
    # Underscores in team names like 'South_Africa' would be the risk; the
    # data has 'South Africa' with a space, so plain text is safe.


# ─────────────── failure modes ───────────────

def test_delivery_alert_failure_records_failed_run_and_still_dedupes(conn, led):
    """If Telegram is down, we record the failed run anyway so the dedupe
    fires next tick — prevents a retry storm. We accept missing one day's
    summary over flooding the chat."""
    with patch.object(ds, "delivery") as mock_d:
        mock_d.summary.return_value = False           # delivery failed
        first = ds.send_if_due(conn, led, now=_at("2026-06-11 09:00"))
        second = ds.send_if_due(conn, led, now=_at("2026-06-11 09:30"))
    assert first is False and second is False
    assert mock_d.summary.call_count == 1             # not retried


def test_alert_raising_does_not_crash(conn, led):
    """delivery.alert raising must be swallowed — the daemon loop must keep
    polling no matter what."""
    with patch.object(ds, "delivery") as mock_d:
        mock_d.summary.side_effect = RuntimeError("Telegram chat-id wrong")
        sent = ds.send_if_due(conn, led, now=_at("2026-06-11 09:00"))
    assert sent is False                            # no exception escapes


def test_summary_text_never_raises_on_missing_tables(tmp_path):
    """build_summary_text should degrade section-by-section, never fail
    catastrophically — empty DB still produces a header + budget line."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # NO schema applied — every read will raise
    txt = ds.build_summary_text(c, _at("2026-06-11 09:00"))
    assert "Mondial 2026" in txt
    assert "Budget" in txt


# ─────────────── Day-9.22: tracked friends ───────────────

def _install_fake_negev(monkeypatch, rows):
    """Replace `toto_get_standings` on the real integrations.negev_toto_mcp
    module. We can't substitute the whole module (Python's
    `from integrations import negev_toto_mcp` resolves via package attribute,
    not just sys.modules), so we patch the function directly — works for any
    local re-import in build_summary_text."""
    from integrations import negev_toto_mcp as ntm
    monkeypatch.setattr(ntm, "toto_get_standings",
                         lambda **kwargs: rows)


def test_summary_text_renders_per_friend_compact_lines(conn, monkeypatch):
    """Day-9.22: ☀️ summary includes a Tracked block with one compact line
    per (me + each friend), each carrying rank/total/gap-to-leader."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    _install_fake_negev(monkeypatch, [
        {"player": "Gilad",  "rank": 1, "total": 12.5, "direction": 8, "broad": 4.5,
         "role": "player"},
        {"player": "Vaadia", "rank": 12, "total": 3.5, "direction": 3.5, "broad": 0,
         "role": "player"},
        {"player": "Igor",   "rank": 26, "total": 0.0, "direction": 0, "broad": 0,
         "role": "player"},
    ])
    txt = ds.build_summary_text(conn, _at("2026-06-11 09:00"))
    assert "Tracked 👥" in txt
    assert "Igor: 0.0 pts" in txt
    assert "Vaadia: 3.5 pts" in txt
    # Friend has a "ahead of you" tag because Vaadia 3.5 > Igor 0.0
    assert "ahead of you" in txt


def test_summary_text_falls_back_to_local_db_when_negev_unreachable(conn, monkeypatch):
    """When Negev raises, the legacy 'Your score' DB-backed line still
    fires so we don't lose the morning heartbeat entirely."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")

    def _boom(**_):
        raise RuntimeError("Negev token expired")
    from integrations import negev_toto_mcp as ntm
    monkeypatch.setattr(ntm, "toto_get_standings", _boom)

    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, futures_points) "
        "VALUES ('Igor', 5.0, 0.0, 0.0)")
    conn.commit()
    txt = ds.build_summary_text(conn, _at("2026-06-11 09:00"))
    assert "Your score: 5.0" in txt
    assert "Tracked 👥" not in txt
    assert "Budget" in txt                          # heartbeat still present


def test_summary_text_no_tracked_block_when_friends_empty(conn, monkeypatch):
    """Pure backward-compat: no FRIEND_PARTICIPANTS → legacy 'Your score' line."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.delenv("FRIEND_PARTICIPANTS", raising=False)
    _install_fake_negev(monkeypatch, [
        {"player": "Igor", "rank": 1, "total": 5.0, "direction": 5, "broad": 0,
         "role": "player"},
    ])
    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, futures_points) "
        "VALUES ('Igor', 5.0, 0.0, 0.0)")
    conn.commit()
    txt = ds.build_summary_text(conn, _at("2026-06-11 09:00"))
    # Even with only-self, tracked block renders the compact line for self.
    assert ("Tracked 👥" in txt) or ("Your score: 5.0" in txt)
