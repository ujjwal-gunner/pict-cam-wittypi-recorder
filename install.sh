#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$HOME/pict-cam-wittypi-recorder"
SERVICE_NAME="pict-recorder.service"

echo "[pict] Installing pict-cam-wittypi-recorder ..."

# Ensure recordings directory exists
mkdir -p "$HOME/recordings"

# Install dependencies
sudo apt-get update
sudo apt-get install -y python3 python3-picamera ffmpeg

# Write systemd unit file directly
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=PICT WittyPi Camera Recorder

[Service]
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/python3 $PROJECT_DIR/recorder.py
User=pi

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd, enable service, and start it
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "[pict] Installed. Service will run at boot."
