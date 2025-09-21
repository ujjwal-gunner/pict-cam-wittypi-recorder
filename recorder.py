#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pict-cam-wittypi-recorder: records with legacy picamera until 1 minute before Witty Pi's next shutdown.
Stops RPi Cam Web Interface first if running. Wraps .h264 files into .mp4 after recording. 
Also can run a simple web server to download recordings.
"""

import os, re, json, time, signal, subprocess, http.server, socketserver, threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# =========================
# CAMERA CONFIG (EDIT HERE)
# =========================
CAMERA_CONFIG = {
    "resolution": (1920, 1080),
    "framerate": 25,
    "bitrate": 8000000,
    "exposure_mode": "auto",
    "awb_mode": "auto",
    "iso": 0,
    "shutter_speed": 0,
    "hflip": False,
    "vflip": False,
    "rotation": 0,
    "annotation_text": "PICT WittyPi Recorder",
    "file_extension": ".h264",
}

RECORDINGS_DIR = Path.home() / "recordings"
SAFETY_MARGIN_SECONDS = 60

# Web server port
WEB_PORT = 8080

stop_requested = False

def _on_signal(signum, frame):
    global stop_requested
    stop_requested = True

for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, _on_signal)

def stop_rpi_cam_interface():
    try:
        subprocess.run(["/home/pi/RPi_Cam_Web_Interface/stop.sh"], check=True)
        print("[pict] Stopped RPi Cam Web Interface")
    except Exception:
        pass

def wrap_to_mp4(h264_file):
    mp4_file = h264_file.with_suffix(".mp4")
    try:
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(CAMERA_CONFIG["framerate"]),
            "-i", str(h264_file), "-c:v", "copy", str(mp4_file)
        ], check=True)
        print(f"[pict] Wrapped {h264_file} -> {mp4_file}")
    except Exception as e:
        print(f"[pict] Failed to wrap {h264_file}: {e}")

def serve_recordings():
    os.chdir(RECORDINGS_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", WEB_PORT), handler) as httpd:
        print(f"[pict] Web server running at http://0.0.0.0:{WEB_PORT}/")
        httpd.serve_forever()

def main():
    from picamera import PiCamera

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    # Stop RPi Cam Web Interface if running
    stop_rpi_cam_interface()

    # Simulated wittypi shutdown detection: record 1 minute for demo
    stop_at = datetime.now(timezone.utc) + timedelta(seconds=120)
    duration_s = int((stop_at - datetime.now(timezone.utc)).total_seconds())

    out_path = RECORDINGS_DIR / f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}{CAMERA_CONFIG['file_extension']}"

    # Start web server in background
    threading.Thread(target=serve_recordings, daemon=True).start()

    with PiCamera(resolution=CAMERA_CONFIG["resolution"]) as camera:
        camera.framerate = CAMERA_CONFIG["framerate"]
        camera.start_recording(str(out_path), bitrate=CAMERA_CONFIG["bitrate"])
        while not stop_requested and datetime.now(timezone.utc) < stop_at:
            camera.wait_recording(1)
        camera.stop_recording()
        print("[pict] Recording stopped")

    wrap_to_mp4(out_path)

if __name__ == "__main__":
    main()
