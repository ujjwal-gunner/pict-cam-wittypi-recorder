#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$HOME/pict-cam-wittypi-recorder"
SERVICE_NAME="pict-recorder.service"

echo "[pict] Installing pict-cam-wittypi-recorder ..."

mkdir -p "$HOME/recordings"

sudo apt-get update
sudo apt-get install -y python3 python3-picamera ffmpeg

sudo install -Dm644 "$PROJECT_DIR/systemd/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "[pict] Installed."
