#!/usr/bin/env bash
# Nightly SQLite snapshot — runs via cron at 03:15 local time (see
# infra/bootstrap.sh which installs the entry).
#
# Why this script and not `cp store/mondial.db ...`:
#   sqlite3 .backup uses the online backup API → consistent even if the
#   daemon is mid-write. cp on a busy DB can corrupt the snapshot.
#
# Retention: 7 days. The whole tournament + 2-week reflection window fits in
# 7 daily snapshots, each ~1-2 MB. Older copies are deleted.
#
# Recovery: stop the daemon, gunzip + move the snapshot into place:
#   systemctl stop mondial2026
#   gunzip -c store/backup/mondial-2026-06-12.db.gz > store/mondial.db
#   systemctl start mondial2026
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DB="$HERE/store/mondial.db"
OBS_DB="$HERE/store/obs.db"
DEST="$HERE/store/backup"
TODAY="$(date +%F)"

mkdir -p "$DEST"

for src in "$DB" "$OBS_DB"; do
    [ -f "$src" ] || continue
    base="$(basename "$src" .db)"
    target="$DEST/${base}-${TODAY}.db"
    # Online backup — safe even if the daemon is writing concurrently.
    sqlite3 "$src" ".backup '$target'"
    gzip -f "$target"
done

# Rotate: keep the 7 most-recent per source.
for base in mondial obs; do
    ls -1t "$DEST"/${base}-*.db.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
done

# Print a single-line summary that cron will email (if MAILTO is set) or
# that you can grep from /var/log/syslog.
echo "[backup] $(date -Is) ok: $(ls -1 $DEST/*.db.gz | wc -l) snapshots in $DEST"
