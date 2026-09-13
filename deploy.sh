#!/usr/bin/env bash
# Contabo VPS install for ns-sync.
#
#   sudo ./deploy.sh              # first run: creates .env template, then stops
#   sudo nano /opt/ns-sync/.env   # fill in tokens + SELF_JID
#   sudo ./deploy.sh              # second run: installs systemd + cron
set -euo pipefail

APP_DIR=/opt/ns-sync
SERVICE_USER=ns-sync
SERVICE_FILE=/etc/systemd/system/ns-sync.service
CRON_FILE=/etc/cron.d/ns-sync
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo ./deploy.sh)" >&2
  exit 1
fi

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "Creating system user $SERVICE_USER"
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$APP_DIR"
cp "$SRC_DIR"/ns_sync.py "$SRC_DIR"/ns_peri_bridge.py "$SRC_DIR"/requirements.txt "$APP_DIR"/
# contacts.json holds real phone numbers and is never in git, so seed it from
# the example on first install and leave an existing one alone.
if [[ ! -f "$APP_DIR/contacts.json" ]]; then
  if [[ -f "$SRC_DIR/contacts.json" ]]; then
    cp "$SRC_DIR/contacts.json" "$APP_DIR/contacts.json"
  else
    cp "$SRC_DIR/contacts.example.json" "$APP_DIR/contacts.json"
    echo "==> $APP_DIR/contacts.json example se banaya gaya - isme asli JIDs bharo"
  fi
fi

if [[ ! -d "$APP_DIR/venv" ]]; then
  echo "Creating venv"
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$SRC_DIR/env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
  echo
  echo "==> $APP_DIR/.env banaya gaya hai. Ab isme tokens aur SELF_JID bharo:"
  echo "    sudo nano $APP_DIR/.env"
  echo "    phir dobara chalao: sudo ./deploy.sh"
  exit 0
fi
chmod 600 "$APP_DIR/.env"
chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"

echo "Installing systemd service"
cp "$SRC_DIR/ns-sync.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable ns-sync.service
systemctl restart ns-sync.service

echo "Installing cron (push mode, times are UTC)"
cat > "$CRON_FILE" <<EOF
30 15 * * *   $SERVICE_USER  $APP_DIR/venv/bin/python $APP_DIR/ns_sync.py --push today   >> /var/log/ns-sync-cron.log 2>&1
30 3  * * 1   $SERVICE_USER  $APP_DIR/venv/bin/python $APP_DIR/ns_sync.py --push tasks   >> /var/log/ns-sync-cron.log 2>&1
30 14 * * 0   $SERVICE_USER  $APP_DIR/venv/bin/python $APP_DIR/ns_sync.py --push week    >> /var/log/ns-sync-cron.log 2>&1
EOF
chmod 644 "$CRON_FILE"

touch /var/log/ns-sync-cron.log
chown "$SERVICE_USER:$SERVICE_USER" /var/log/ns-sync-cron.log

echo
echo "Done."
echo "Status: systemctl status ns-sync.service"
echo "Logs:   journalctl -u ns-sync.service -f"
