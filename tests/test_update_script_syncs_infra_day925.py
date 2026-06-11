"""Day-9.25: pin that update.sh syncs the systemd unit + crontab into the
system paths after a git pull.

Live gap discovered: I bumped `infra/mondial2026.service` to add
`Environment="MPLCONFIGDIR=/tmp/matplotlib"`. The git pull on the VM updated
the repo file, but the RUNNING daemon kept using the stale
`/etc/systemd/system/mondial2026.service` — so my MPLCONFIGDIR fix wasn't
actually active. Same shape of gap for the crontab.

These tests grep the update.sh script for the sync logic so a future
refactor can't silently drop it.
"""
from __future__ import annotations
import os
import re


def _read_update_script() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "infra", "update.sh")
    with open(path) as f:
        return f.read()


def test_update_script_syncs_systemd_unit():
    """A path-sync step for the systemd unit must exist, and must include
    `systemctl daemon-reload` after copying so systemd picks up the change."""
    src = _read_update_script()
    assert "infra/mondial2026.service" in src, \
        "update.sh doesn't reference infra/mondial2026.service"
    assert "/etc/systemd/system/mondial2026.service" in src, \
        "update.sh doesn't write to /etc/systemd/system/"
    assert "daemon-reload" in src, \
        "update.sh must run `systemctl daemon-reload` after syncing the unit"


def test_update_script_syncs_crontab():
    """The crontab file must be installed via `crontab` (not just copied)."""
    src = _read_update_script()
    assert "infra/mondial2026.crontab" in src, \
        "update.sh doesn't reference infra/mondial2026.crontab"
    # crontab is installed via `crontab <file>` command. The file path may
    # be passed via a variable, so match a few common shapes.
    assert re.search(r'crontab\s+(\S*CRON_REPO_FILE\S*|.*mondial2026\.crontab)', src), \
        "update.sh doesn't install the crontab via `crontab <file>`"


def test_update_script_runs_pip_install_when_requirements_changes():
    """Regression: existing behavior — requirements.txt diff → pip install."""
    src = _read_update_script()
    assert "pip install" in src
    assert "requirements.txt" in src


def test_update_script_restarts_daemon_via_systemctl():
    """The actual `systemctl restart mondial2026.service` happens after both
    code AND infra sync — otherwise the restart picks up code but uses the
    old systemd Environment."""
    src = _read_update_script()
    assert "systemctl restart" in src or "restart_and_verify" in src, \
        "update.sh must restart the daemon"


def test_update_script_has_auto_rollback():
    """Day-9.5 design: if restart fails, revert to the saved SHA."""
    src = _read_update_script()
    assert "AUTO-ROLLBACK" in src or "rollback" in src.lower()
    assert ".last_good_sha" in src, \
        "update.sh must use .last_good_sha to know what to roll back to"


def test_systemd_unit_in_repo_has_mplconfigdir():
    """Pin the Day-9.25 fix — make sure the unit on disk has MPLCONFIGDIR
    so a future copy-paste edit doesn't silently drop it."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "infra", "mondial2026.service")
    with open(path) as f:
        unit = f.read()
    assert "MPLCONFIGDIR" in unit, \
        "infra/mondial2026.service missing MPLCONFIGDIR — Bug F would reappear"
