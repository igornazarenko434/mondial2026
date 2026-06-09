"""Offline tests for tools/sync_negev_standings.py.

Mocks the Negev MCP module (no Firestore network calls). Pins the
mapping (Negev directionPoints → group_points, broadBetPoints → futures_points)
and the dry-run / include-bots / MY_PARTICIPANT behaviours.
"""
from __future__ import annotations
import sqlite3
from types import SimpleNamespace

import pytest

from tools import sync_negev_standings as sns


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        c.executescript(f.read())
    return c


@pytest.fixture
def fake_ntm():
    """Builds a fake `ntm` module exposing toto_get_standings()."""
    def make(rows):
        return SimpleNamespace(
            toto_get_standings=lambda *, tournament_id, include_bots=False:
                [r for r in rows if include_bots or r.get("role") != "bot"])
    return make


def test_sync_writes_one_row_per_player_with_correct_mapping(conn, fake_ntm, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = [
        {"player": "Alice", "rank": 1, "total": 28, "direction": 24, "broad": 4,
         "exactCount": 3, "role": "player"},
        {"player": "Igor", "rank": 2, "total": 18, "direction": 16, "broad": 2,
         "exactCount": 1, "role": "player"},
    ]
    out = sns.sync_standings(tournament_id="tid-x", conn=conn,
                              ntm=fake_ntm(rows))
    assert out["ok"]
    assert out["participants"] == 2
    assert out["upserted"] == 2
    assert out["my_rank"] == 2
    assert out["my_total"] == 18
    assert out["my_gap_to_leader"] == 10
    db = {r["participant"]: dict(r) for r in conn.execute(
        "SELECT participant, group_points, knockout_points, futures_points FROM standings"
    ).fetchall()}
    # Mapping: directionPoints → group_points, 0 → knockout, broadBetPoints → futures
    assert db["Alice"]["group_points"] == 24.0
    assert db["Alice"]["knockout_points"] == 0.0
    assert db["Alice"]["futures_points"] == 4.0
    assert db["Igor"]["futures_points"] == 2.0


def test_sync_is_idempotent_re_run_doesnt_duplicate(conn, fake_ntm, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = [{"player": "Igor", "rank": 1, "total": 5, "direction": 5, "broad": 0,
             "exactCount": 0, "role": "player"}]
    sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(rows))
    # Re-run with same data
    sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(rows))
    n = conn.execute("SELECT COUNT(*) FROM standings WHERE participant='Igor'").fetchone()[0]
    assert n == 1                                              # ON CONFLICT UPDATE


def test_sync_updates_existing_row_with_new_values(conn, fake_ntm, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    sns.sync_standings(tournament_id="tid", conn=conn,
                        ntm=fake_ntm([{"player": "Igor", "rank": 1, "total": 5,
                                        "direction": 5, "broad": 0,
                                        "exactCount": 0, "role": "player"}]))
    # Second sync with different points
    sns.sync_standings(tournament_id="tid", conn=conn,
                        ntm=fake_ntm([{"player": "Igor", "rank": 1, "total": 12,
                                        "direction": 10, "broad": 2,
                                        "exactCount": 1, "role": "player"}]))
    row = dict(conn.execute("SELECT group_points, futures_points FROM standings "
                            "WHERE participant='Igor'").fetchone())
    assert row["group_points"] == 10.0 and row["futures_points"] == 2.0


def test_sync_dry_run_doesnt_write(conn, fake_ntm, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = [{"player": "Igor", "rank": 1, "total": 5, "direction": 5, "broad": 0,
             "exactCount": 0, "role": "player"}]
    out = sns.sync_standings(tournament_id="tid", conn=conn,
                              ntm=fake_ntm(rows), dry=True)
    assert out["ok"]
    n = conn.execute("SELECT COUNT(*) FROM standings").fetchone()[0]
    assert n == 0                                              # nothing written


def test_sync_excludes_bots_by_default(conn, fake_ntm, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    rows = [
        {"player": "Igor", "rank": 1, "total": 5, "direction": 5, "broad": 0,
         "exactCount": 0, "role": "player"},
        {"player": "Chinchilla", "rank": 2, "total": 4, "direction": 4, "broad": 0,
         "exactCount": 0, "role": "bot"},
    ]
    # Our fake_ntm fixture filters bots out at the MCP layer by default
    sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(rows))
    players = {r[0] for r in conn.execute("SELECT participant FROM standings").fetchall()}
    assert players == {"Igor"}                                  # bot excluded


def test_sync_detects_new_members_and_returns_them(conn, fake_ntm, monkeypatch):
    """Day-9.15: when a NEW user appears in Negev (joined since last sync),
    we must add them to the standings DB AND surface their name in the
    result so the operator + Telegram message can flag the addition."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")

    # First sync — Igor + Aharony exist
    rows_v1 = [
        {"player": "Igor",    "rank": 1, "total": 0, "direction": 0, "broad": 0,
         "exactCount": 0, "role": "player"},
        {"player": "Aharony", "rank": 2, "total": 0, "direction": 0, "broad": 0,
         "exactCount": 0, "role": "player"},
    ]
    r1 = sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(rows_v1))
    assert r1.get("ok")
    # First sync: BOTH names are "new" because the DB was empty
    assert sorted(r1.get("new_members", [])) == ["Aharony", "Igor"]

    # Second sync — same two PLUS a new member YahavHaMeleh
    rows_v2 = rows_v1 + [
        {"player": "YahavHaMeleh", "rank": 3, "total": 0, "direction": 0,
         "broad": 0, "exactCount": 0, "role": "player"},
    ]
    r2 = sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(rows_v2))
    assert r2.get("ok")
    assert r2.get("new_members") == ["YahavHaMeleh"], \
        f"only YahavHaMeleh should be flagged as new; got {r2.get('new_members')}"

    # Third sync — no new members → empty/absent
    r3 = sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(rows_v2))
    assert not r3.get("new_members")


def test_sync_warns_when_my_participant_missing(conn, fake_ntm, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "GhostName")
    out = sns.sync_standings(tournament_id="tid", conn=conn,
                              ntm=fake_ntm([{"player": "Alice", "rank": 1,
                                              "total": 0, "direction": 0,
                                              "broad": 0, "exactCount": 0,
                                              "role": "player"}]))
    assert out["ok"]
    assert "warning" in out
    assert "GhostName" in out["warning"]


def test_sync_returns_error_when_no_tid(conn, fake_ntm, monkeypatch):
    monkeypatch.delenv("NEGEV_TOURNAMENT_ID", raising=False)
    out = sns.sync_standings(tournament_id=None, conn=conn,
                              ntm=fake_ntm([]))
    assert not out["ok"]
    assert "NEGEV_TOURNAMENT_ID" in out["error"]


def test_sync_returns_error_when_ntm_raises(conn, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    class _Boom:
        def toto_get_standings(self, **kw):
            raise RuntimeError("token expired")
    out = sns.sync_standings(tournament_id="tid", conn=conn, ntm=_Boom())
    assert not out["ok"]
    assert "token expired" in out["error"]


def test_sync_returns_error_when_zero_rows(conn, fake_ntm, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    out = sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm([]))
    assert not out["ok"]
    assert "0 rows" in out["error"]


def test_telegram_summary_format_contains_top5_and_me(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.delenv("FRIEND_PARTICIPANTS", raising=False)
    rows = [
        {"player": f"P{i}", "rank": i, "total": 100 - i, "direction": 50,
         "broad": 50, "exactCount": 0, "role": "player"}
        for i in range(1, 11)
    ]
    # Put me at rank 8 (out of top 5) — should trigger the "AROUND YOU" block
    rows[7]["player"] = "Igor"
    title, body = sns._format_telegram_summary(rows, me="Igor", tid="tid-x")
    assert "Negev standings" in title
    assert "P1" in body and "P5" in body                 # top 5
    assert "Igor" in body and "← you" in body
    assert "AROUND YOU" in body                           # Day-9.22: new section title
    assert "vs leader" in body                            # tracked block contains gap line


def test_telegram_summary_no_around_when_in_top_5(monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.delenv("FRIEND_PARTICIPANTS", raising=False)
    rows = [{"player": "Igor", "rank": 1, "total": 100, "direction": 50,
             "broad": 50, "exactCount": 0, "role": "player"}] + [
        {"player": f"P{i}", "rank": i, "total": 100 - i, "direction": 50,
         "broad": 50, "exactCount": 0, "role": "player"}
        for i in range(2, 8)
    ]
    title, body = sns._format_telegram_summary(rows, me="Igor", tid="tid-x")
    assert "← you" in body
    assert "AROUND YOU" not in body                       # I'm in top 5; no extra block


def test_telegram_summary_includes_tracked_block_for_each_friend(monkeypatch):
    """Day-9.22: every friend in FRIEND_PARTICIPANTS gets a full audit block."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    rows = [
        {"player": "Gilad",  "rank": 1, "total": 12.5, "direction": 8, "broad": 4.5,
         "exactCount": 0, "role": "player"},
        {"player": "Sarah",  "rank": 2, "total": 10.0, "direction": 8, "broad": 2,
         "exactCount": 0, "role": "player"},
        {"player": "Vaadia", "rank": 12, "total": 3.5, "direction": 3.5, "broad": 0,
         "exactCount": 0, "role": "player"},
        {"player": "Igor",   "rank": 26, "total": 0.0, "direction": 0, "broad": 0,
         "exactCount": 0, "role": "player"},
    ]
    title, body = sns._format_telegram_summary(rows, me="Igor", tid="tid")
    assert "TRACKED" in body
    # Both Igor's and Vaadia's blocks present
    assert "👤 Igor" in body
    assert "👤 Vaadia" in body
    # Friend block carries vs-you line; my block does not
    assert "vs you" in body
    # Vaadia ahead of me (3.5 vs 0)
    assert "Vaadia ahead of you" in body


def test_telegram_summary_marks_tracked_friend_in_top5_list(monkeypatch):
    """Friends who land in the Top-5 scoreboard get a ← tracked marker."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("FRIEND_PARTICIPANTS", "Vaadia")
    rows = [
        {"player": "Vaadia", "rank": 1, "total": 50, "direction": 50, "broad": 0,
         "exactCount": 0, "role": "player"},
        {"player": "Igor",   "rank": 26, "total": 0, "direction": 0, "broad": 0,
         "exactCount": 0, "role": "player"},
    ]
    _title, body = sns._format_telegram_summary(rows, me="Igor", tid="tid")
    assert "← tracked" in body


def test_sync_with_send_telegram_calls_delivery_summary_not_alert(conn, fake_ntm, monkeypatch):
    """Regression: sync must call delivery.summary (no ⚠️ prefix) — NOT
    delivery.alert. An earlier version used alert(), which prepended ⚠️
    to the 📊 title and made the Telegram message look like a failure."""
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    sent = {}
    import core.delivery as d
    def fake_summary(title, body):
        sent["title"] = title
        sent["body"] = body
        return True
    def fake_alert(*a, **k):
        sent["alerted"] = True
        return True
    monkeypatch.setattr(d, "summary", fake_summary)
    monkeypatch.setattr(d, "alert", fake_alert)
    rows = [{"player": "Igor", "rank": 1, "total": 10, "direction": 8, "broad": 2,
             "exactCount": 1, "role": "player"}]
    out = sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(rows),
                              send_telegram=True)
    assert out["ok"] is True
    assert out["telegram_delivered"] is True
    assert "Igor" in sent["body"]
    assert sent["title"].startswith("📊")                  # clean prefix
    assert "⚠" not in sent["title"]                        # no failure marker
    assert sent.get("alerted") is None                     # delivery.alert NOT called


def test_sync_with_send_telegram_skipped_on_dry_run(conn, fake_ntm, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    import core.delivery as d
    fired = {"n": 0}
    monkeypatch.setattr(d, "summary", lambda t, b: fired.update(n=fired["n"]+1) or True)
    rows = [{"player": "Igor", "rank": 1, "total": 10, "direction": 8, "broad": 2,
             "exactCount": 1, "role": "player"}]
    out = sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(rows),
                              send_telegram=True, dry=True)
    assert out["ok"] is True
    assert "telegram_delivered" not in out                # never tried
    assert fired["n"] == 0


def test_sync_telegram_delivery_failure_doesnt_break_sync(conn, fake_ntm, monkeypatch):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    import core.delivery as d
    def boom(t, b):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(d, "summary", boom)
    rows = [{"player": "Igor", "rank": 1, "total": 10, "direction": 8, "broad": 2,
             "exactCount": 1, "role": "player"}]
    out = sns.sync_standings(tournament_id="tid", conn=conn, ntm=fake_ntm(rows),
                              send_telegram=True)
    assert out["ok"] is True                              # the standings still wrote
    assert out["telegram_delivered"] is False
    assert "telegram down" in out["telegram_error"]


def test_main_cli_dry_run_returns_zero(monkeypatch, capsys):
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    monkeypatch.setenv("NEGEV_TOURNAMENT_ID", "tid")
    class _NTM:
        def toto_get_standings(self, **kw):
            return [{"player": "Igor", "rank": 1, "total": 5, "direction": 5,
                      "broad": 0, "exactCount": 0, "role": "player"}]
    monkeypatch.setattr(sns, "_import_or_fail", lambda: _NTM())
    rc = sns.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "✓" in out and "Igor" in out
