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
