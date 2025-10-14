#!/usr/bin/env bash
set -euo pipefail

# --- Config ---
USER_NAME="$(whoami)"
PROJECT_DIR="$HOME/pict-cam-wittypi-recorder"
SERVICE_NAME="pict-recorder.service"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"

echo "[pict] Installing pict-cam-wittypi-recorder (Bookworm)..."

mkdir -p "$PROJECT_DIR" "$HOME/recordings"
sudo chown -R "$USER_NAME:$USER_NAME" "$PROJECT_DIR" "$HOME/recordings"

# Clean legacy conflict
if dpkg -l | awk '{print $2}' | grep -qx "libraspberrypi0"; then
  echo "[pict] Removing legacy libraspberrypi0..."
  sudo apt-get -y remove --purge libraspberrypi0 || true
fi
sudo apt-mark unhold libraspberrypi0 raspberrypi-bootloader raspberrypi-kernel 2>/dev/null || true

sudo apt-get update
sudo apt-get -y full-upgrade

# Runtime deps (Bookworm-safe)
sudo apt-get install -y --no-install-recommends \
  python3 python3-picamera2 python3-psutil ffmpeg libcamera-apps rfkill \
  raspi-utils libdtovl0

# Ensure camera access
sudo usermod -aG video "$USER_NAME" || true

# Systemd unit
sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=PICT WittyPi Camera Recorder
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
PermissionsStartOnly=true

# Optional pre-steps; ignore errors (Bookworm: tvservice often absent)
ExecStartPre=/bin/sh -c 'echo 1 > /sys/class/graphics/fb0/blank || true'
ExecStartPre=-/usr/bin/vcgencmd display_power 0
ExecStartPre=-/usr/bin/tvservice -o
ExecStartPre=-/usr/sbin/rfkill block bluetooth

ExecStart=/usr/bin/python3 $PROJECT_DIR/recorder.py

Restart=on-failure
RestartSec=3s
StartLimitIntervalSec=60
StartLimitBurst=10

# Hardening
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=false
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "[pict] Install complete."
echo "[pict] Start now:  sudo systemctl start $SERVICE_NAME"
echo "[pict] Logs live:  journalctl -u $SERVICE_NAME -n 200 -f"
