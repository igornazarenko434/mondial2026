"""Retry/backoff, fallback, run-status ledger, and pipeline failure handling."""
import pytest
from core.reliability import retry, with_fallback, PermanentError
from core.obs.runs import RunLedger
from orchestrator.pipeline import process_match


# --- retry ---
def test_retry_succeeds_after_transient():
    calls = {"n": 0}
    @retry(max_attempts=3, sleep=lambda _: None)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("blip")
        return "ok"
    assert flaky() == "ok" and calls["n"] == 3


def test_retry_fails_fast_on_permanent():
    calls = {"n": 0}
    @retry(max_attempts=3, sleep=lambda _: None)
    def bad():
        calls["n"] += 1
        raise PermanentError("bad input")
    with pytest.raises(PermanentError):
        bad()
    assert calls["n"] == 1            # not retried


def test_retry_exhausts():
    @retry(max_attempts=2, sleep=lambda _: None)
    def always():
        raise TimeoutError("down")
    with pytest.raises(TimeoutError):
        always()


# --- fallback ---
def test_fallback_uses_backup():
    def primary(): raise ConnectionError("primary down")
    def backup(): return "from-backup"
    assert with_fallback(primary, backup, label="odds") == "from-backup"


def test_fallback_all_fail():
    with pytest.raises(RuntimeError):
        with_fallback(lambda: (_ for _ in ()).throw(OSError("x")),
                      lambda: (_ for _ in ()).throw(OSError("y")))


# --- run ledger ---
def test_run_ledger_tracks_status():
    led = RunLedger(":memory:")
    rid = led.start(401, "T-7m")
    led.finish(rid, "ok", card_delivered=True)
    s = led.summary()
    assert s["total"] == 1 and s["ok"] == 1 and s["cards_delivered"] == 1


# --- pipeline: failure is recorded + stays loud (no exception escapes) ---
def test_pipeline_records_failure(monkeypatch):
    import core.delivery as d
    sent = []
    monkeypatch.setattr(d, "deliver_card", lambda c: True)
    monkeypatch.setattr(d, "alert", lambda t, b: sent.append((t, b)) or True)
    def boom(_m): raise ConnectionError("odds source down")
    res = process_match({"match_id": 9, "home": "A", "away": "B"}, "T-7m",
                        build_card=boom, max_attempts=2)
    assert res["status"] == "failed"
    assert sent and "FAILED" in sent[0][0]      # an alert was sent


def test_pipeline_delivers_on_success(monkeypatch):
    import core.delivery as d
    delivered = []
    monkeypatch.setattr(d, "deliver_card", lambda c: delivered.append(c) or True)
    card = {"home": "A", "away": "B", "stage": "Group",
            "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
            "expected_points": 1.2, "model_prob": {"H": .5, "D": .3, "A": .2},
            "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}
    res = process_match({"match_id": 1, "home": "A", "away": "B"}, "T-7m",
                        build_card=lambda m: card)
    assert res["status"] == "ok" and res["delivered"] and delivered
