#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 24.04 LTS VM (Hetzner CX22 / Oracle Always-Free /
# any Debian-family host) into a running Mondial 2026 scheduler daemon.
#
# Run as root on the VM, once:
#   curl -fsSL https://raw.githubusercontent.com/<YOUR_USER>/mondial2026/main/infra/bootstrap.sh | bash
#
# Or interactively (recommended on first run):
#   ssh root@<vm-ip>
#   wget https://raw.githubusercontent.com/<YOUR_USER>/mondial2026/main/infra/bootstrap.sh
#   bash bootstrap.sh
#
# What it does, in order (idempotent — safe to re-run):
#   1. apt-update + install python 3.13, git, sqlite3, curl, tzdata
#   2. create a non-root `mondial` user
#   3. git clone the repo into /home/mondial/mondial2026
#   4. create venv + pip install -r requirements.txt
#   5. WAIT for you to populate .env (cat the template, prompt to edit)
#   6. install + enable the systemd unit
#   7. install the nightly backup cron
#   8. tail the journal so you can confirm the daemon comes up clean
#
# After it finishes, you should see structured-JSON logs scrolling. Ctrl-C to
# exit the tail; the daemon keeps running.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/igornazarenko434/mondial2026.git}"
INSTALL_USER="${INSTALL_USER:-mondial}"
INSTALL_DIR="/home/${INSTALL_USER}/mondial2026"
TZ_NAME="${TZ_NAME:-Asia/Jerusalem}"

bold() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

bold "1. system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3.13 python3.13-venv python3-pip \
    git sqlite3 curl tzdata ca-certificates
# Default Python on 24.04 is 3.12; we install 3.13 alongside via deadsnakes if missing.
if ! command -v python3.13 > /dev/null; then
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq python3.13 python3.13-venv
fi
timedatectl set-timezone "$TZ_NAME"

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
    cd '$INSTALL_DIR'
    python3.13 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
"

bold "5. .env (secrets — NEVER committed)"
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chown "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    cat <<'EOF'

────────────────────────────────────────────────────────────────────────
ACTION REQUIRED — edit /home/mondial/mondial2026/.env with these keys:
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

Save, then re-run this script — it will skip to step 6.
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

bold "7. nightly backup cron"
crontab -u "$INSTALL_USER" -l 2>/dev/null | grep -v 'mondial2026/infra/backup.sh' > /tmp/_mc || true
echo "15 3 * * *  $INSTALL_DIR/infra/backup.sh" >> /tmp/_mc
crontab -u "$INSTALL_USER" /tmp/_mc
rm -f /tmp/_mc

bold "8. live journal — Ctrl-C to exit (the daemon stays up)"
sleep 1
journalctl -u mondial2026.service -f -n 40
