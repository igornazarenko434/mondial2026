"""Day-9.23: pins the three permanent fixes from the 2026-06-10 incident.

  1. Negev refresh-token failure RAISES (no silent fallback) by default
  2. Negev refresh-token failure falls back IFF NEGEV_ALLOW_PASSWORD_FALLBACK=1
  3. preflight detects inline-comment leaks in env vars
  4. alert_failure_once_per_day suppresses repeat alerts within the same day
"""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest

from integrations import negev_toto_mcp as ntm
from integrations import negev_alerts
from config import preflight


# ───────────────────── 1. Refresh failure raises loudly ─────────────────────

def _reset_token():
    ntm._token.update(id=None, refresh=None, uid=None, exp=0)


def test_refresh_failure_raises_loudly_by_default(monkeypatch):
    """Day-9.23: refresh token set + refresh fails + no fallback opt-in =
    LOUD RuntimeError with remediation hint. Does NOT silently try email."""
    _reset_token()
    monkeypatch.setenv("NEGEV_REFRESH_TOKEN", "fake_refresh_xyz")
    monkeypatch.setenv("NEGEV_EMAIL", "user@x.com")
    monkeypatch.setenv("NEGEV_PASSWORD", "secret")
    monkeypatch.delenv("NEGEV_ALLOW_PASSWORD_FALLBACK", raising=False)

    fake_resp = MagicMock()
    fake_resp.ok = False
    fake_resp.status_code = 400
    fake_resp.text = "INVALID_REFRESH_TOKEN"
    with patch.object(ntm.requests, "post", return_value=fake_resp) as mock_post:
        with pytest.raises(RuntimeError) as ei:
            ntm._id_token()
    # Critical assertion: ONE call only (refresh), no fall-through to email
    assert mock_post.call_count == 1
    assert "refresh failed" in str(ei.value).lower()
    assert "re-capture it from negev-toto.web.app" in str(ei.value)


def test_refresh_failure_falls_through_when_explicitly_opted_in(monkeypatch):
    """Day-9.23: setting NEGEV_ALLOW_PASSWORD_FALLBACK=1 restores the old
    dual-mode behavior. Two requests fire: refresh (fails), then email (used)."""
    _reset_token()
    monkeypatch.setenv("NEGEV_REFRESH_TOKEN", "fake_refresh_xyz")
    monkeypatch.setenv("NEGEV_EMAIL", "user@x.com")
    monkeypatch.setenv("NEGEV_PASSWORD", "secret")
    monkeypatch.setenv("NEGEV_ALLOW_PASSWORD_FALLBACK", "1")

    bad = MagicMock(); bad.ok = False; bad.status_code = 400; bad.text = "x"
    good = MagicMock()
    good.ok = True
    good.json.return_value = {"idToken": "id_z", "refreshToken": "rt_z",
                                "localId": "uid_z", "expiresIn": "3600"}
    with patch.object(ntm.requests, "post", side_effect=[bad, good]) as mock_post:
        token = ntm._id_token()
    assert token == "id_z"
    assert mock_post.call_count == 2


def test_refresh_failure_with_no_password_configured_still_raises(monkeypatch):
    """Even with NEGEV_ALLOW_PASSWORD_FALLBACK=1, an empty password setup
    still raises (the fallback can't succeed without creds)."""
    _reset_token()
    monkeypatch.setenv("NEGEV_REFRESH_TOKEN", "fake")
    monkeypatch.setenv("NEGEV_ALLOW_PASSWORD_FALLBACK", "1")
    monkeypatch.delenv("NEGEV_EMAIL", raising=False)
    monkeypatch.delenv("NEGEV_PASSWORD", raising=False)
    bad = MagicMock(); bad.ok = False; bad.status_code = 400; bad.text = "x"
    with patch.object(ntm.requests, "post", return_value=bad):
        with pytest.raises(RuntimeError):
            ntm._id_token()


# ───────────────────── 2. Preflight inline-comment leak detection ─────────────────────

def test_preflight_detects_inline_comment_in_NEGEV_EMAIL(monkeypatch):
    """The smoking-gun shape that caused the 2026-06-10 incident:
    NEGEV_EMAIL value contains '  # <comment>' due to systemd's parser."""
    monkeypatch.setenv("NEGEV_EMAIL", "igor434@gmail.com   # your login email")
    leaks = preflight._detect_inline_comment_leaks()
    assert any(k == "NEGEV_EMAIL" for k, _ in leaks)


def test_preflight_no_leak_when_value_clean(monkeypatch):
    monkeypatch.setenv("NEGEV_EMAIL", "igor434@gmail.com")
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    leaks = preflight._detect_inline_comment_leaks()
    assert leaks == []


def test_preflight_status_carries_env_hygiene_flag(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "x")
    monkeypatch.setenv("NEGEV_EMAIL", "ok@x.com")
    status = preflight.check()
    assert status["env_hygiene_ok"] is True


def test_preflight_status_carries_env_hygiene_failure(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "x")
    monkeypatch.setenv("NEGEV_EMAIL", "user@x.com   # comment")
    status = preflight.check()
    assert status["env_hygiene_ok"] is False


# ───────────────────── 3. once-per-day suppression ─────────────────────

def test_alert_failure_once_per_day_first_call_fires(monkeypatch):
    """First call of the day → alert sent. Second call same day → suppressed."""
    # Reset the module-level tracker
    monkeypatch.setattr(negev_alerts, "_LAST_ALERT_DATE", None)
    sent = {"n": 0}
    monkeypatch.setattr(negev_alerts, "alert_failure",
                         lambda **_: (sent.__setitem__("n", sent["n"] + 1) or True))
    a = negev_alerts.alert_failure_once_per_day(source="x", reason="auth fail")
    b = negev_alerts.alert_failure_once_per_day(source="x", reason="auth fail")
    c = negev_alerts.alert_failure_once_per_day(source="y", reason="other")
    assert sent["n"] == 1
    assert a is True
    assert b is False
    assert c is False


def test_alert_failure_once_per_day_does_not_set_tracker_on_delivery_fail(monkeypatch):
    """If Telegram is down, don't mark today as alerted — let the next
    failure retry. Prevents losing the FIRST alert to a transient telegram outage."""
    monkeypatch.setattr(negev_alerts, "_LAST_ALERT_DATE", None)
    monkeypatch.setattr(negev_alerts, "alert_failure", lambda **_: False)
    a = negev_alerts.alert_failure_once_per_day(source="x", reason="boom")
    assert a is False
    assert negev_alerts._LAST_ALERT_DATE is None
    # Next attempt should ALSO try (not be suppressed)
    sent = {"n": 0}
    monkeypatch.setattr(negev_alerts, "alert_failure",
                         lambda **_: (sent.__setitem__("n", sent["n"] + 1) or True))
    b = negev_alerts.alert_failure_once_per_day(source="x", reason="boom")
    assert b is True
    assert sent["n"] == 1
