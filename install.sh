#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$HOME/pict-cam-wittypi-recorder"
SERVICE_NAME="pict-recorder.service"

echo "[pict] Installing pict-cam-wittypi-recorder ..."

mkdir -p "$HOME/recordings"

sudo apt-get update
sudo apt-get install -y python3 python3-picamera ffmpeg

SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=PICT WittyPi Camera Recorder
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/python3 $PROJECT_DIR/recorder.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "[pict] Installed. Service will run at boot."
