#!/usr/bin/env bash
# Pull the latest code and restart. Run as root:  sudo bash /opt/corp-coin-watch/deploy/update.sh
set -euo pipefail
APP=/opt/corp-coin-watch
BRANCH=$(git -C "$APP" rev-parse --abbrev-ref HEAD)
git -C "$APP" pull --ff-only origin "$BRANCH"
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
chown -R ccw:ccw "$APP"
systemctl restart corp-coin-watch
echo "Updated to $(git -C "$APP" log --oneline -1) and restarted."
