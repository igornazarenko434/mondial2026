#!/usr/bin/env bash
# Safe-update a running Mondial 2026 daemon on the Hetzner VM.
#
# Usage (as root on the VM):
#   /home/mondial/mondial2026/infra/update.sh             # pull, restart, verify
#   /home/mondial/mondial2026/infra/update.sh --dry-run   # show what would change
#   /home/mondial/mondial2026/infra/update.sh --rollback  # go back to prev version
#   /home/mondial/mondial2026/infra/update.sh --force     # update even if a
#                                                          # match window is active
#                                                          # (USE WITH CARE)
#
# What it does, in order:
#   1. Refuse if the working tree has uncommitted changes (someone hand-edited
#      a file on the VM — investigate, don't blindly overwrite).
#   2. Record the current commit SHA in /home/mondial/mondial2026/.last_good_sha
#      (so --rollback works).
#   3. git fetch + show what's about to change.
#   4. git pull --ff-only (no merge commits, no surprises).
#   5. If requirements.txt changed → re-run pip install inside the venv.
#   6. systemctl restart mondial2026.
#   7. Wait 10 s, check `systemctl is-active`. If not running → AUTO-ROLLBACK
#      to the previous SHA, restart, exit non-zero so journal alerts.
#   8. Tail the last 30 journal lines so you can eyeball that it came up clean.
#
# State preservation (verified safe across `git pull`):
#   .env, store/*.db, store/*.json, store/heartbeat, store/backup/ are ALL
#   gitignored. git pull only touches tracked code/config; runtime state is
#   never overwritten.
#
# Idempotency: the daemon's runs ledger persists across restarts. A mid-tick
# restart never re-sends a card (was_handled() check + runs.start dedupe).
#
# Rollback strategy: --rollback flips HEAD back to .last_good_sha and restarts.
# Run it if a deployed version misbehaves.

set -euo pipefail

INSTALL_USER="mondial"
INSTALL_DIR="/home/${INSTALL_USER}/mondial2026"
SERVICE="mondial2026.service"
LAST_GOOD_FILE="${INSTALL_DIR}/.last_good_sha"
OBS_DB="${INSTALL_DIR}/store/obs.db"
# "Active worker" = any run with status='started' in the last 5 min.
# If we restart while one is running we might kill it between card-delivery
# and ledger.finish → was_handled() will mark it done but no card landed.
ACTIVE_WORKER_AGE_SECONDS=300

bold()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
warn()  { printf '\033[1;33m   warning: %s\033[0m\n' "$*"; }
fail()  { printf '\n\033[1;31m== FAILED: %s ==\033[0m\n' "$*" >&2; exit 1; }
ok()    { printf '\033[1;32m   ✓ %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || fail "run as root (try: sudo -i)"
[ -d "${INSTALL_DIR}/.git" ] || fail "${INSTALL_DIR} is not a git repo — was bootstrap completed?"

git_as_mondial() {
    sudo -u "$INSTALL_USER" git -C "$INSTALL_DIR" "$@"
}

active_worker_check() {
    # Returns 0 (truthy) if a worker is currently in flight; 1 (falsy) if quiet.
    # Detects via the runs ledger: any row with status='started' AND younger
    # than ACTIVE_WORKER_AGE_SECONDS.
    [ -f "$OBS_DB" ] || return 1                      # no DB yet → can't be busy
    local count
    count="$(sudo -u "$INSTALL_USER" sqlite3 "$OBS_DB" "
        SELECT COUNT(*) FROM runs
         WHERE status='started'
           AND started_at > datetime('now', '-${ACTIVE_WORKER_AGE_SECONDS} seconds')
    " 2>/dev/null || echo 0)"
    [ "$count" -gt 0 ]
}

list_recent_started_runs() {
    [ -f "$OBS_DB" ] || return
    sudo -u "$INSTALL_USER" sqlite3 -column -header "$OBS_DB" "
        SELECT match_id, window, started_at FROM runs
         WHERE status='started'
           AND started_at > datetime('now', '-${ACTIVE_WORKER_AGE_SECONDS} seconds')
         ORDER BY started_at DESC
    " 2>/dev/null
}

restart_and_verify() {
    bold "restart $SERVICE + health-check"
    systemctl restart "$SERVICE"
    sleep 10
    if ! systemctl is-active --quiet "$SERVICE"; then
        return 1
    fi
    ok "systemctl is-active: yes"

    # Confirm the new process actually got past startup. We look for
    # "scheduler started" in the last 30 lines of the journal since the
    # restart. Missing = systemd thinks it's alive but the Python process
    # never got past obs.setup() or preflight.check().
    if journalctl -u "$SERVICE" --since "30 seconds ago" --no-pager \
       2>/dev/null | grep -q "scheduler started"; then
        ok "found 'scheduler started' in fresh journal — boot completed"
    else
        warn "no 'scheduler started' in last 30 s of journal — process may be stuck"
        return 1
    fi

    # Count ERROR lines in the last 60 seconds (post-restart). Non-zero =
    # something broke loudly even though the process is technically running.
    # Day-9.25: `grep -c` exits 1 (with stdout "0") when there are zero
    # matches. The `|| echo 0` was meant to handle that, but bash's
    # `||` chains the second command's output ON TOP of the first, producing
    # a two-line "0\n0" string that fails the integer test below with
    # "line 110: [: 0 0: integer expression expected". Use `grep -c ... ||
    # true` + `tail -1` to coerce to a single integer line.
    local errs
    errs="$(journalctl -u "$SERVICE" --since "60 seconds ago" --no-pager \
            2>/dev/null | grep -c '"level": "ERROR"' 2>/dev/null || true)"
    errs="${errs:-0}"
    errs="$(printf '%s' "$errs" | tail -1)"
    if [ "${errs:-0}" -gt 0 ] 2>/dev/null; then
        warn "$errs ERROR line(s) in journal since restart — inspect below"
        return 1
    fi
    ok "no ERROR lines in journal since restart"
    return 0
}

# ─────────────────────────── --rollback ───────────────────────────
if [ "${1:-}" = "--rollback" ]; then
    [ -f "$LAST_GOOD_FILE" ] || fail "no $LAST_GOOD_FILE — nothing to roll back to"
    PREV="$(cat "$LAST_GOOD_FILE")"
    bold "rolling back to $PREV"
    git_as_mondial reset --hard "$PREV"
    restart_and_verify || fail "daemon failed to start even on rollback — SSH in and investigate"
    bold "last 30 journal lines"
    journalctl -u "$SERVICE" -n 30 --no-pager
    exit 0
fi

# ─────────────────────────── normal update ───────────────────────────
FORCE=0
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --force)   FORCE=1 ;;
        --dry-run) DRY_RUN=1 ;;
    esac
done

bold "1. safety check — clean working tree?"
if ! git_as_mondial diff --quiet || ! git_as_mondial diff --cached --quiet; then
    git_as_mondial status -s
    fail "uncommitted changes on the VM — investigate before updating. \
If you intentionally edited files here, commit or stash them first; \
if not, run \`git checkout .\` to discard (DANGEROUS — uses git, not file deletion)."
fi
ok "working tree clean"

bold "1b. safety check — any worker in flight?"
if active_worker_check; then
    list_recent_started_runs
    if [ "$FORCE" -eq 1 ]; then
        warn "worker(s) active but --force given; proceeding (you may miss a card)"
    else
        fail "a match-window job is currently in flight. Restarting now could \
kill it between Telegram delivery and ledger.finish, leaving was_handled=True \
without a card sent. Wait a few minutes for it to finish, then re-run. If you \
MUST deploy now, add --force (you accept the missed-card risk)."
    fi
else
    ok "no active workers — safe to restart"
fi

bold "2. record current commit (for --rollback)"
CURRENT="$(git_as_mondial rev-parse HEAD)"
echo "$CURRENT" | sudo -u "$INSTALL_USER" tee "$LAST_GOOD_FILE" > /dev/null
ok "saved current SHA: $CURRENT"

bold "3. fetch + show incoming changes"
git_as_mondial fetch --quiet origin main
INCOMING="$(git_as_mondial rev-parse origin/main)"
NO_CODE_CHANGE=0
if [ "$CURRENT" = "$INCOMING" ]; then
    ok "already at latest ($CURRENT) — no code to pull"
    NO_CODE_CHANGE=1
    # Day-9.25: do NOT early-exit here. Even when there's no code change to
    # pull, the systemd unit + crontab MAY have drifted from a previous
    # deploy that failed mid-sync, a hand-edit on the VM, or a bug in an
    # older update.sh. Step 5b below is cheap (`cmp` two files) and self-
    # healing — running it on every invocation ensures the VM converges
    # to the repo's truth on the next admin touch.
fi
if [ "$NO_CODE_CHANGE" -eq 0 ]; then
    echo "   current  : $CURRENT"
    echo "   incoming : $INCOMING"
    echo "   diff stat:"
    git_as_mondial --no-pager log --oneline "$CURRENT..$INCOMING" | sed 's/^/     /'
    git_as_mondial --no-pager diff --stat "$CURRENT..$INCOMING" | sed 's/^/     /'
fi

if [ "$DRY_RUN" -eq 1 ]; then
    bold "DRY-RUN — would pull (if any) + sync infra + restart. Re-run without --dry-run to apply."
    exit 0
fi

if [ "$NO_CODE_CHANGE" -eq 0 ]; then
    bold "4. git pull --ff-only"
    REQ_BEFORE="$(sha256sum "${INSTALL_DIR}/requirements.txt" | awk '{print $1}')"
    git_as_mondial pull --ff-only --quiet origin main
    ok "pulled to $(git_as_mondial rev-parse HEAD)"
    REQ_AFTER="$(sha256sum "${INSTALL_DIR}/requirements.txt" | awk '{print $1}')"

    bold "5. requirements.txt changed?"
    if [ "$REQ_BEFORE" != "$REQ_AFTER" ]; then
        warn "requirements.txt changed — reinstalling deps inside venv"
        sudo -u "$INSTALL_USER" bash -c "
            set -e
            cd '$INSTALL_DIR'
            .venv/bin/pip install --quiet --upgrade pip
            .venv/bin/pip install --quiet -r requirements.txt
        "
        ok "pip install completed"
    else
        ok "no dep changes — skipping pip install"
    fi
else
    ok "skipping steps 4-5 (no code change to pull/install)"
fi

# ─── Day-9.25: sync infra/* into the system paths ────────────────────────
# git pull only updates the repo; the systemd unit lives in
# /etc/systemd/system/ and the crontab lives in cron-spool — both need an
# explicit copy + reload after a repo-side change. Before this step,
# bumping infra/mondial2026.service (e.g. adding MPLCONFIGDIR=/tmp/...) on
# Mac would update the repo file on the VM but the running daemon would
# STILL use the stale unit until someone manually copied + daemon-reload'd.
# Same gap for the crontab.
bold "5b. sync infra/* to system paths (runs even when no code changed)"
SYSTEMD_REPO_UNIT="${INSTALL_DIR}/infra/mondial2026.service"
SYSTEMD_LIVE_UNIT="/etc/systemd/system/mondial2026.service"
SYSTEMD_CHANGED=0
CRON_CHANGED=0
if [ -f "$SYSTEMD_REPO_UNIT" ]; then
    if ! cmp -s "$SYSTEMD_REPO_UNIT" "$SYSTEMD_LIVE_UNIT"; then
        warn "systemd unit drifted — syncing infra/mondial2026.service → $SYSTEMD_LIVE_UNIT"
        cp "$SYSTEMD_REPO_UNIT" "$SYSTEMD_LIVE_UNIT"
        systemctl daemon-reload
        SYSTEMD_CHANGED=1
        ok "systemd unit synced + daemon-reload done"
    else
        ok "systemd unit up-to-date"
    fi
else
    warn "no $SYSTEMD_REPO_UNIT in repo — skipping unit sync"
fi

CRON_REPO_FILE="${INSTALL_DIR}/infra/mondial2026.crontab"
if [ -f "$CRON_REPO_FILE" ]; then
    CRON_INSTALLED="$(sudo -u "$INSTALL_USER" crontab -l 2>/dev/null || true)"
    CRON_REPO_CONTENT="$(cat "$CRON_REPO_FILE")"
    if [ "$CRON_INSTALLED" != "$CRON_REPO_CONTENT" ]; then
        warn "crontab drifted — installing from $CRON_REPO_FILE"
        sudo -u "$INSTALL_USER" crontab "$CRON_REPO_FILE"
        CRON_CHANGED=1
        ok "crontab synced ($(grep -c '^[^#]' "$CRON_REPO_FILE" 2>/dev/null || echo '?') active lines)"
    else
        ok "crontab up-to-date"
    fi
else
    warn "no $CRON_REPO_FILE in repo — skipping crontab sync"
fi

# ──── Day-9.25: post-deploy smoke audits — extracted into a function so
# they run on EVERY invocation (even no-op "already at latest" runs).
# Heartbeat behavior: every time the operator runs update.sh, we re-verify
# the .env hygiene + Negev grid alignment. A drift in either is caught
# the moment it happens, not weeks later when a card scores wrong.
run_smoke_audits() {
    bold "6b. smoke audits (free / read-only — run on every invocation)"

    # 6b.i — .env inline-comment trap detector (zero API cost, reads .env)
    # Catches the 2026-06-10 incident where NEGEV_EMAIL had an inline
    # comment that systemd's parser didn't strip → INVALID_EMAIL silently.
    if [ -f "${INSTALL_DIR}/tools/audit_env.py" ]; then
        if sudo -u "$INSTALL_USER" bash -c "
            cd '$INSTALL_DIR'
            set -a; source .env; set +a
            PYTHONPATH=. .venv/bin/python tools/audit_env.py --skip-auth --quiet
        " > /tmp/audit_env_out 2>&1; then
            ok "audit_env: .env hygiene OK"
        else
            warn "audit_env reported issues — review:"
            head -20 /tmp/audit_env_out | sed 's/^/     /'
        fi
        rm -f /tmp/audit_env_out
    fi

    # 6b.ii — Negev scoring-grid drift (1 free Negev call)
    # The grids never change mid-tournament, but a Negev admin tweak
    # would silently invalidate every EV-optimal pick. Canary check.
    if [ -f "${INSTALL_DIR}/tools/audit_negev_multipliers.py" ]; then
        if sudo -u "$INSTALL_USER" bash -c "
            cd '$INSTALL_DIR'
            set -a; source .env; set +a
            PYTHONPATH=. .venv/bin/python tools/audit_negev_multipliers.py --quiet
        " > /tmp/audit_neg_out 2>&1; then
            ok "audit_negev_multipliers: grids match config/rules.py"
        else
            warn "audit_negev_multipliers reported drift — review:"
            tail -15 /tmp/audit_neg_out | sed 's/^/     /'
        fi
        rm -f /tmp/audit_neg_out
    fi

    # 6b.iii — preflight summary from the running daemon's recent log
    # Surface the daemon's own preflight 'enabled: ...' line so the operator
    # sees which features are active without grepping journalctl manually.
    PREFLIGHT_LINE="$(journalctl -u "$SERVICE" --since '120 seconds ago' \
                       --no-pager 2>/dev/null | grep -m1 'preflight — enabled' \
                       || true)"
    if [ -n "$PREFLIGHT_LINE" ]; then
        ENABLED="$(printf '%s' "$PREFLIGHT_LINE" | sed -E 's/.*"msg": "preflight \\\\u2014 (enabled: [^"]*).*/\1/')"
        ok "$ENABLED"
    fi
}

print_summary() {
    bold "7. post-deploy summary"
    NEW_SHA="$(git_as_mondial rev-parse HEAD)"
    UPTIME="$(systemctl show -p ActiveEnterTimestamp --value "$SERVICE")"
    LAST_CARD="$(sudo -u "$INSTALL_USER" sqlite3 "$OBS_DB" \
        "SELECT match_id, window, started_at FROM runs WHERE card_delivered=1 ORDER BY started_at DESC LIMIT 1" \
        2>/dev/null || echo 'none yet')"
    LAST_HEARTBEAT="$(stat -c %y "${INSTALL_DIR}/store/heartbeat" 2>/dev/null | cut -d. -f1 || echo 'no heartbeat yet')"
    echo "   deployed SHA       : $NEW_SHA"
    echo "   daemon started     : $UPTIME"
    echo "   last heartbeat     : $LAST_HEARTBEAT"
    echo "   last card sent     : $LAST_CARD"
    echo "   systemd unit synced: $( [ "$SYSTEMD_CHANGED" -eq 1 ] && echo 'YES (drifted, re-applied)' || echo 'already matched' )"
    echo "   crontab synced     : $( [ "$CRON_CHANGED" -eq 1 ] && echo 'YES (drifted, re-applied)' || echo 'already matched' )"
}

# ─────────────────────────── restart + auto-rollback ───────────────────────────
# Day-9.25: if there's no code change AND no infra drift, the daemon doesn't
# need restarting. But we STILL run the smoke audits + summary so the
# operator gets the same heartbeat info as on a real deploy.
if [ "$NO_CODE_CHANGE" -eq 1 ] && [ "$SYSTEMD_CHANGED" -eq 0 ]; then
    ok "no code change, no infra drift — daemon already running latest"
    run_smoke_audits
    print_summary
    bold "✓ UPDATE OK — daemon was already up-to-date; audits ran clean"
    exit 0
fi

if restart_and_verify; then
    bold "6. last 30 journal lines"
    journalctl -u "$SERVICE" -n 30 --no-pager

    run_smoke_audits
    print_summary

    bold "✓ UPDATE OK — daemon running latest"
    echo
    echo "Watch the next ticks land here:    journalctl -u $SERVICE -f"
    echo "Or wait for the next ☀️ Daily summary on Telegram to confirm end-to-end."
    exit 0
fi

# ─────────────── auto-rollback path ───────────────
warn "daemon failed to come up cleanly — AUTO-ROLLBACK to $CURRENT"
git_as_mondial reset --hard "$CURRENT"
if restart_and_verify; then
    bold "last 30 journal lines (post-rollback)"
    journalctl -u "$SERVICE" -n 30 --no-pager
    fail "update FAILED and was rolled back to $CURRENT. Investigate the new \
commit on your Mac, fix, push, then re-run /home/mondial/mondial2026/infra/update.sh."
fi
fail "daemon failed to start even on rollback — manually intervene. \
Last good SHA: $CURRENT. Check: journalctl -u $SERVICE -n 100"
