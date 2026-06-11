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


def test_update_script_runs_post_deploy_audit_env():
    """Step 6b.i — audit_env.py runs post-restart so .env inline-comment
    leaks (the 2026-06-10 incident) get caught the same minute they happen."""
    src = _read_update_script()
    assert "audit_env.py" in src, \
        "update.sh doesn't run tools/audit_env.py post-deploy"
    # The flag must skip the live Negev auth probe (we don't want every
    # deploy to ping Negev — the daemon ingests it anyway on the next tick).
    assert "--skip-auth" in src, \
        "update.sh must pass --skip-auth to audit_env so the .env scan stays free"


def test_update_script_runs_post_deploy_audit_negev_multipliers():
    """Step 6b.ii — Negev multiplier-drift watchdog. The grids never change
    mid-tournament, but an admin tweak would silently invalidate every
    EV-optimal pick we compute. Catching it the day-of beats discovering
    points dropping a week later."""
    src = _read_update_script()
    assert "audit_negev_multipliers.py" in src, \
        "update.sh doesn't run tools/audit_negev_multipliers.py post-deploy"


def test_update_script_surfaces_preflight_enabled_features():
    """Step 6b.iii — surface the daemon's own preflight 'enabled: ...' line
    so the operator finishes update.sh knowing exactly which features are
    active vs degraded, without grepping journalctl manually."""
    src = _read_update_script()
    assert "preflight — enabled" in src or "preflight \\\\u2014 enabled" in src, \
        "update.sh doesn't surface the daemon's preflight features line"


def test_update_script_summary_includes_drift_flags():
    """Final summary tells the operator if the systemd unit or crontab
    DRIFTED and had to be re-applied. Without this, a quiet sync is
    indistinguishable from no-sync-needed."""
    src = _read_update_script()
    assert "SYSTEMD_CHANGED" in src and "CRON_CHANGED" in src, \
        "post-deploy summary must report drift-and-resync of systemd unit + crontab"


def test_error_count_is_single_line_integer():
    """Day-9.25: the `errs=...||echo 0` pattern produced a "0\\n0" string
    when grep -c matched zero AND the journal slice was empty — the
    integer test then errored "line 110: [: 0 0: integer expression
    expected". Pin the fix: `tail -1` coerces to single line + `2>/dev/null`
    on the test swallows non-integer values gracefully."""
    src = _read_update_script()
    # The fixed pattern uses tail -1 to ensure single-line output
    assert "tail -1" in src, \
        "update.sh must coerce errs count to a single integer line via tail -1"
    # And the integer test is guarded against non-integer values
    assert '[ "${errs:-0}" -gt 0 ] 2>/dev/null' in src, \
        "the integer test must redirect stderr so a non-numeric errs " \
        "doesn't poison the deploy output"


def test_error_count_logic_handles_zero_matches_gracefully():
    """Behavioral test: run the exact bash idiom on a fixture string that
    contains zero ERROR markers. Must produce errs=0 and the integer test
    must NOT print to stderr."""
    import subprocess
    cmd = r"""
        errs="$(printf '0\n0\n' | grep -c xMARKER_NOT_THERE 2>/dev/null || true)"
        errs="${errs:-0}"
        errs="$(printf '%s' "$errs" | tail -1)"
        if [ "${errs:-0}" -gt 0 ] 2>/dev/null; then
            echo "would_alert"
        else
            echo "ok errs=$errs"
        fi
    """
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert r.returncode == 0, f"shell exited non-zero: {r.stderr!r}"
    assert r.stdout.strip() == "ok errs=0", \
        f"unexpected stdout: {r.stdout!r}"
    # Critical: no "integer expression expected" garbage on stderr
    assert "integer expression expected" not in r.stderr
