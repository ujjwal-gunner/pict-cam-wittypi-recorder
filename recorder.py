#!/usr/bin/env python3
import os, time, signal, subprocess, threading, http.server, socketserver, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from picamera import PiCamera

# =========================
# CAMERA CONFIG (EDIT HERE)
# =========================
CAMERA_CONFIG = {
    "resolution": (1920, 1080),
    "framerate": 25,
    "bitrate": 8000000,
    "annotation_text": "PICT WittyPi Recorder",
    "file_extension": ".h264",
}

RECORDINGS_DIR = Path.home() / "recordings"
SAFETY_MARGIN_SECONDS = 60
CHECK_INTERVAL = 60   # how often to poll wittypi for next shutdown
WEB_PORT = 8123       # unique port for recordings HTTP server

stop_requested = False
def _on_signal(signum, frame):
    global stop_requested
    stop_requested = True
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, _on_signal)

def get_next_shutdown():
    """
    Call Witty Pi's runScript.sh and parse "Next shutdown" time.
    Returns a timezone-aware datetime, or None if not scheduled.
    """
    try:
        out = subprocess.check_output(
            ["bash", "/home/pi/wittypi/runScript.sh"],
            stderr=subprocess.STDOUT
        ).decode().strip()
        match = re.search(r"Next shutdown at:\s+([\d-]+\s+[\d:]+)", out)
        if match:
            # Assume system local time is correct
            return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
    except Exception as e:
        print("[pict] Failed to query Witty Pi:", e)
    return None

def stop_rpi_cam_interface():
    try:
        subprocess.run(
            ["/home/pi/RPi_Cam_Web_Interface/stop.sh"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print("[pict] Stopped RPi Cam Web Interface")
    except Exception:
        pass

def serve_recordings():
    os.chdir(RECORDINGS_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", WEB_PORT), handler) as httpd:
        print(f"[pict] Web server running at http://<pi-ip>:{WEB_PORT}/")
        httpd.serve_forever()

def wrap_to_mp4(h264_file):
    mp4_file = h264_file.with_suffix(".mp4")
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-framerate", str(CAMERA_CONFIG["framerate"]),
            "-i", str(h264_file),
            "-c:v", "copy", str(mp4_file)
        ], check=True)
        print(f"[pict] Wrapped {h264_file} -> {mp4_file}")
    except Exception as e:
        print(f"[pict] Failed to wrap {h264_file}: {e}")

def main():
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    # Start web server in background
    threading.Thread(target=serve_recordings, daemon=True).start()

    # Wait until WittyPi has a valid shutdown time
    next_shutdown = None
    while not stop_requested:
        next_shutdown = get_next_shutdown()
        if next_shutdown:
            remain = (next_shutdown - datetime.now().astimezone()).total_seconds()
            if remain > SAFETY_MARGIN_SECONDS + 5:
                break
        print("[pict] No WittyPi shutdown scheduled, retrying...")
        time.sleep(CHECK_INTERVAL)

    if not next_shutdown:
        print("[pict] No shutdown time found, exiting.")
        return

    stop_at = next_shutdown - timedelta(seconds=SAFETY_MARGIN_SECONDS)
    duration_s = int((stop_at - datetime.now().astimezone()).total_seconds())
    if duration_s <= 0:
        print("[pict] Shutdown too soon, skipping.")
        return

    # Stop RPi Cam Web Interface before recording
    stop_rpi_cam_interface()

    out_path = RECORDINGS_DIR / f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}{CAMERA_CONFIG['file_extension']}"
    print(f"[pict] Recording until {stop_at.isoformat()} -> {out_path}")

    with PiCamera(resolution=CAMERA_CONFIG["resolution"]) as camera:
        camera.framerate = CAMERA_CONFIG["framerate"]
        camera.annotate_text = CAMERA_CONFIG["annotation_text"]
        camera.start_recording(str(out_path), bitrate=CAMERA_CONFIG["bitrate"])
        while not stop_requested and datetime.now().astimezone() < stop_at:
            camera.wait_recording(1)
        camera.stop_recording()
    print("[pict] Recording finished.")

    # Convert to mp4
    wrap_to_mp4(out_path)

if __name__ == "__main__":
    main()
