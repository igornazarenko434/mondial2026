#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 24.04 LTS VM (Hetzner CX22 / Oracle Always-Free /
# any Debian-family host) into a running Mondial 2026 scheduler daemon.
#
# Run as root on the VM, once:
#   wget https://raw.githubusercontent.com/igornazarenko434/mondial2026/main/infra/bootstrap.sh
#   bash bootstrap.sh
#
# What it does, in order (idempotent — safe to re-run):
#   1. apt-update + install python3.12 (Ubuntu 24.04 stock), git, sqlite3, curl, tzdata
#   2. create a non-root `mondial` user
#   3. git clone the repo into /home/mondial/mondial2026
#   4. create venv + pip install -r requirements.txt
#   5. WAIT for you to populate .env (cat the template, prompt to copy)
#   6. install + enable the systemd unit
#   7. install the nightly backup cron
#   8. tail the journal so you can confirm the daemon comes up clean
#
# After it finishes, you should see structured-JSON logs scrolling. Ctrl-C to
# exit the tail; the daemon keeps running.
#
# Why we use the stock Python: Ubuntu 24.04 LTS ships Python 3.12, which is
# more than modern enough for our code (we only need ZoneInfo + 3.10 union
# syntax, both of which work under `from __future__ import annotations`).
# Avoiding the deadsnakes PPA dependency keeps this bootstrap one apt-get
# call from the system repos — no third-party trust, no PPA-not-yet-for-this-
# release surprises.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/igornazarenko434/mondial2026.git}"
INSTALL_USER="${INSTALL_USER:-mondial}"
INSTALL_DIR="/home/${INSTALL_USER}/mondial2026"
TZ_NAME="${TZ_NAME:-Asia/Jerusalem}"

bold() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
warn() { printf '\033[1;33m   warning: %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m== FAILED: %s ==\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run as root (try: sudo -i)"

bold "1. system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# All from Ubuntu's main repos — no PPA needed. python3 on 24.04 = 3.12.
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    git sqlite3 curl tzdata ca-certificates
# Set the local TZ for log timestamps. timedatectl can fail if dbus isn't up
# yet on a brand-new VM — that's harmless, log timestamps still work.
timedatectl set-timezone "$TZ_NAME" || warn "could not set timezone to $TZ_NAME (continuing)"

# Sanity check: confirm Python is recent enough for our code (need 3.10+ for
# the union-type syntax we use under `from __future__ import annotations`).
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    fail "python3 is $PY_VER — need 3.10 or newer"
fi
echo "   python3 version: $PY_VER"

bold "2. service user '${INSTALL_USER}'"
id -u "$INSTALL_USER" > /dev/null 2>&1 || useradd -m -s /bin/bash "$INSTALL_USER"

bold "3. clone repo into ${INSTALL_DIR}"
if [ ! -d "$INSTALL_DIR/.git" ]; then
    sudo -u "$INSTALL_USER" git clone "$REPO_URL" "$INSTALL_DIR"
else
    sudo -u "$INSTALL_USER" git -C "$INSTALL_DIR" pull --ff-only
fi

bold "4. venv + dependencies"
sudo -u "$INSTALL_USER" bash -c "
    set -e
    cd '$INSTALL_DIR'
    if [ ! -d .venv ]; then
        python3 -m venv .venv
    fi
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
"

bold "5. .env (secrets — NEVER committed)"
# -s = file exists AND non-empty. If user only copied the template and didn't
# fill anything, the template is non-empty placeholder strings — we proceed
# (the preflight check at startup catches missing real values loudly).
if [ ! -s "$INSTALL_DIR/.env" ]; then
    if [ -f "$INSTALL_DIR/.env.example" ]; then
        cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
        chown "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR/.env"
        chmod 600 "$INSTALL_DIR/.env"
    fi
    VM_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    cat <<EOF

────────────────────────────────────────────────────────────────────────
ACTION REQUIRED — populate ${INSTALL_DIR}/.env with real values:

  FOOTBALL_DATA_API_KEY=
  ODDS_API_KEY=
  API_FOOTBALL_KEY=
  BRAVE_SEARCH_API_KEY=
  GEMINI_API_KEY=
  TELEGRAM_BOT_TOKEN=
  TELEGRAM_CHAT_ID=
  OTEL_TRACES_EXPORTER=otlp
  OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
  OTEL_EXPORTER_OTLP_HEADERS=x-honeycomb-team=YOUR_HONEYCOMB_KEY

Easiest path — from your Mac, in a second terminal:
  scp ~/private_Igor/Mondial_2026/mondial2026/.env root@${VM_IP:-<this-vm-ip>}:/tmp/mondial.env

Then back here on this VM:
  mv /tmp/mondial.env ${INSTALL_DIR}/.env
  chown ${INSTALL_USER}:${INSTALL_USER} ${INSTALL_DIR}/.env
  chmod 600 ${INSTALL_DIR}/.env

Then re-run THIS script — it will skip ahead to step 6 automatically.
────────────────────────────────────────────────────────────────────────
EOF
    exit 0
fi

bold "6. systemd unit + enable + start"
install -m 644 "$INSTALL_DIR/infra/mondial2026.service" \
    /etc/systemd/system/mondial2026.service
mkdir -p "$INSTALL_DIR/store" "$INSTALL_DIR/cache"
chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR/store" "$INSTALL_DIR/cache"
systemctl daemon-reload
systemctl enable --now mondial2026.service
sleep 2
systemctl status --no-pager mondial2026.service || true

bold "7. nightly backup cron + daily Negev standings sync"
TMP_CRON="$(mktemp)"
crontab -u "$INSTALL_USER" -l 2>/dev/null \
    | grep -v 'mondial2026/infra/backup.sh' \
    | grep -v 'mondial2026/tools/sync_negev_standings.py' \
    > "$TMP_CRON" || true
# Backup at 03:15 IDT, sync at 07:00 IDT (2h before the 09:00 daily summary)
echo "15 3 * * *  $INSTALL_DIR/infra/backup.sh" >> "$TMP_CRON"
echo "0 7 * * *  cd $INSTALL_DIR && set -a && . ./.env && set +a && PYTHONPATH=. .venv/bin/python tools/sync_negev_standings.py --quiet" >> "$TMP_CRON"
crontab -u "$INSTALL_USER" "$TMP_CRON"
rm -f "$TMP_CRON"

bold "8. live journal — Ctrl-C to exit (the daemon stays up)"
sleep 1
journalctl -u mondial2026.service -f -n 40
