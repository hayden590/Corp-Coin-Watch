#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 server. Run as root:
#   sudo bash setup-server.sh <git repo url> [branch]
# Installs the bot to /opt/corp-coin-watch, runs it as its own user, and
# registers a service that starts on boot and restarts itself if it crashes.
set -euo pipefail

REPO_URL="${1:?usage: sudo bash setup-server.sh <git repo url> [branch]}"
BRANCH="${2:-main}"
APP=/opt/corp-coin-watch

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo."; exit 1; fi

echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git

PYV=$(python3 -c 'import sys; print(sys.version_info >= (3, 11))')
if [ "$PYV" != "True" ]; then
  echo "Python 3.11+ is required (use Ubuntu 24.04)."; exit 1
fi

MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 2000 ] && ! swapon --show | grep -q .; then
  echo "==> Small server (${MEM_MB} MB RAM): adding a 2 GB swap file as a safety buffer"
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Creating the 'ccw' service user"
id ccw >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/ccw --shell /usr/sbin/nologin ccw

echo "==> Getting the code"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch origin "$BRANCH" && git -C "$APP" checkout "$BRANCH" && git -C "$APP" pull --ff-only origin "$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP"
fi

echo "==> Installing Python packages (takes 3-6 minutes on a small server - don't press anything)"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --progress-bar on --upgrade pip
"$APP/.venv/bin/pip" install --progress-bar on -r "$APP/requirements.txt"

echo "==> Server settings"
[ -f "$APP/.env" ] || cp "$APP/.env.example" "$APP/.env"
chmod 600 "$APP/.env"
if [ ! -f "$APP/config.local.yaml" ]; then
  cat > "$APP/config.local.yaml" <<'YAML'
# This machine only (not in git). A server has no screen, so no desktop pop-ups:
# alerts go to Telegram (phone + Telegram Desktop on your laptop) and/or Discord.
alerts:
  desktop: false
YAML
fi
mkdir -p "$APP/data" "$APP/logs"
chown -R ccw:ccw "$APP"

echo "==> Registering the service"
cp "$APP/deploy/corp-coin-watch.service" /etc/systemd/system/corp-coin-watch.service
systemctl daemon-reload
systemctl enable corp-coin-watch >/dev/null

cat <<EOF

Done. Next:
  1. Add your keys:          sudo nano $APP/.env
  2. Check it works:         cd $APP && sudo -u ccw .venv/bin/python main.py --dry-run
  3. (Optional) logins:      cd $APP && sudo -u ccw .venv/bin/python main.py telegram-login
                             cd $APP && sudo -u ccw .venv/bin/python main.py x-login
  4. Start it:               sudo systemctl start corp-coin-watch
  5. Watch it:               sudo journalctl -u corp-coin-watch -f
     Status:                 cd $APP && sudo -u ccw .venv/bin/python main.py health
EOF
