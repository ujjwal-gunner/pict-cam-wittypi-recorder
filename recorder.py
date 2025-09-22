#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, signal, threading, http.server, socketserver, subprocess, urllib.parse, io
from datetime import datetime, timedelta
from pathlib import Path

# =========================
# CAMERA / RUNTIME CONFIG
# =========================
CAMERA_CONFIG = {
    "resolution": (1296, 972),     # width, height
    "framerate": 15,               # FPS
    "bitrate": 4_000_000,          # bits per second (use bitrate as the single quality knob)
    "annotation_label": "PICT WittyPi Recorder",  # shown in overlay
    "file_extension": ".h264",     # raw H.264 (mp4 will be produced after wrap)
}

# MODE SWITCH:
#   - Set DURATION_SECONDS = None --> Witty Pi mode (wait for next shutdown)
#   - Set DURATION_SECONDS = N (int, 1..18000) --> Fixed-duration mode (record for N seconds)
DURATION_SECONDS = None  # e.g., 120 for 2 minutes; must be <= 18000

RECORDINGS_DIR         = Path.home() / "recordings"
WITTYPI_DIR_CANDIDATES = [Path.home() / "wittypi", Path("/home/pi/wittypi"), Path("/home/pi/WittyPi")]
RUNSCRIPT_CANDIDATES   = [d / "runScript.sh" for d in WITTYPI_DIR_CANDIDATES]
SCHEDULE_FILE_CANDIDATES = [d / "schedule.wpi" for d in WITTYPI_DIR_CANDIDATES]

SAFETY_MARGIN_SECONDS  = 60         # stop this many seconds before next Witty Pi shutdown
CHECK_INTERVAL         = 60         # how often to poll Witty Pi for next shutdown
WEB_PORT               = 8123       # HTTP server port

# RPi Cam Web Interface stop options
RPICAM_STOP_CANDIDATES = [
    ["/home/pi/RPi_Cam_Web_Interface/stop.sh"],
    ["/var/www/html/RPi_Cam_Web_Interface/stop.sh"],
]
RPICAM_SERVICE_CANDIDATES = [
    "raspimjpeg",
    "rpicamweb",
    "rpi-cam-web-interface",
]

stop_requested = False
def _on_signal(signum, frame):
    global stop_requested
    stop_requested = True
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, _on_signal)

def _now_local():
    return datetime.now().astimezone()

def _seconds_until(ts: datetime) -> float:
    return (ts - _now_local()).total_seconds()

def _find_existing(paths):
    for p in paths:
        if Path(p).exists():
            return Path(p)
    return None

def get_next_shutdown_from_wittypi():
    """
    Call Witty Pi's runScript.sh and parse 'Next shutdown at:'.
    Returns aware datetime or None if not available.
    """
    runscript = _find_existing(RUNSCRIPT_CANDIDATES)
    if not runscript:
        return None
    try:
        out = subprocess.check_output(["bash", str(runscript)], stderr=subprocess.STDOUT).decode("utf-8", "ignore")
    except Exception as e:
        print("[pict] Failed to run runScript.sh:", e)
        return None

    # Example lines:
    # Next startup at:   2025-09-23 09:00:00
    # Next shutdown at:  2025-09-23 10:30:00
    m = re.search(r"Next shutdown at:\s*([0-9-]+\s+[0-9:]+)", out)
    if not m:
        return None
    try:
        # interpret as local time
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
    except Exception:
        return None

def stop_rpi_cam_interface():
    # Try systemd services first
    for svc in RPICAM_SERVICE_CANDIDATES:
        try:
            subprocess.run(["/bin/systemctl", "stop", svc], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    # Try stop.sh scripts
    for cmd in RPICAM_STOP_CANDIDATES:
        try:
            if Path(cmd[0]).exists():
                subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    # Kill raspimjpeg process if any
    try:
        subprocess.run(["/usr/bin/pkill", "-f", "raspimjpeg"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    print("[pict] RPi Cam Web Interface stop attempted (service/stop.sh/kill).")

def build_output_path():
    ts = _now_local().strftime("%Y%m%d_%H%M%S")
    return RECORDINGS_DIR / f"rec_{ts}{CAMERA_CONFIG['file_extension']}"

def wrap_to_mp4(h264_file: Path):
    mp4_file = h264_file.with_suffix(".mp4")
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-framerate", str(CAMERA_CONFIG["framerate"]),
            "-i", str(h264_file),
            "-c:v", "copy", str(mp4_file)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[pict] Wrapped {h264_file.name} -> {mp4_file.name}")
    except Exception as e:
        print(f"[pict] MP4 wrap failed: {e}")

# --------------------------
# HTTP server (index, download+delete, delete, schedule editor)
# --------------------------
def _safe_child_of(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False

def _list_files_html():
    rows = []
    for p in sorted(RECORDINGS_DIR.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
        if not p.is_file():
            continue
        name = p.name
        size_mb = (p.stat().st_size / (1024*1024)) if p.exists() else 0
        rows.append(f"<tr><td>{name}</td><td>{size_mb:.1f} MB</td>"
                    f"<td><a href='/download?name={urllib.parse.quote(name)}'>download & delete</a></td>"
                    f"<td><a href='/delete?name={urllib.parse.quote(name)}' onclick=\"return confirm('Delete {name}?')\">delete</a></td></tr>")
    return "\n".join(rows)

def _schedule_path():
    return _find_existing(SCHEDULE_FILE_CANDIDATES)

def _schedule_html():
    sched = _schedule_path()
    body = ""
    if sched and sched.exists():
        try:
            body = sched.read_text()
        except Exception as e:
            body = f"# error reading schedule.wpi: {e}\n"
    else:
        body = "# schedule.wpi not found; create and Save to write one.\n"
    esc = body.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    return f"""
<html><head><meta charset="utf-8"><title>schedule.wpi</title></head>
<body>
<h2>schedule.wpi editor</h2>
<form method="POST" action="/schedule">
<textarea name="content" rows="28" cols="100" style="font-family:monospace;">{esc}</textarea><br/>
<button type="submit">Save</button>
</form>
<p><a href="/">← Back</a></p>
</body></html>
"""

class PictHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            # index
            html = f"""
<html><head><meta charset="utf-8"><title>PICT Recorder</title></head>
<body>
<h2>Recordings</h2>
<table border="1" cellspacing="0" cellpadding="6">
<tr><th>File</th><th>Size</th><th>Download+Delete</th><th>Delete</th></tr>
{_list_files_html()}
</table>
<p><a href="/schedule">View/Edit schedule.wpi</a></p>
</body></html>
"""
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/download":
            qs = urllib.parse.parse_qs(parsed.query)
            name = qs.get("name", [""])[0]
            target = (RECORDINGS_DIR / name).resolve()
            if not name or not target.exists() or not _safe_child_of(target, RECORDINGS_DIR):
                self.send_error(404, "File not found")
                return
            try:
                fs = target.stat()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
                self.send_header("Content-Length", str(fs.st_size))
                self.end_headers()
                with target.open("rb") as f:
                    while True:
                        chunk = f.read(64*1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                # ✅ File is NOT deleted after download
            except BrokenPipeError:
                # client aborted; nothing to do
                pass
            except Exception as e:
                self.send_error(500, f"Error sending file: {e}")
            return

        if parsed.path == "/delete":
            qs = urllib.parse.parse_qs(parsed.query)
            name = qs.get("name", [""])[0]
            target = (RECORDINGS_DIR / name).resolve()
            if not name or not target.exists() or not _safe_child_of(target, RECORDINGS_DIR):
                self.send_error(404, "File not found")
                return
            try:
                target.unlink()
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
            except Exception as e:
                self.send_error(500, f"Delete failed: {e}")
            return

        if parsed.path == "/schedule":
            html = _schedule_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        # Fallback to static listing of recordings dir
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/schedule":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", "ignore")
            fields = urllib.parse.parse_qs(body)
            content = fields.get("content", [""])[0]
            sched = _schedule_path() or SCHEDULE_FILE_CANDIDATES[0]
            try:
                sched.parent.mkdir(parents=True, exist_ok=True)
                sched.write_text(content)
                # Optional: you may want to call wittypi/apply script here if you use one.
                # For safety, we only save file; user/WittyPi can apply it.
                self.send_response(302)
                self.send_header("Location", "/schedule")
                self.end_headers()
            except Exception as e:
                self.send_error(500, f"Saving schedule failed: {e}")
            return
        self.send_error(404, "Unknown POST")

def serve_http():
    try:
        os.chdir(RECORDINGS_DIR)
    except Exception:
        return
    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True
    with ReusableTCPServer(("", WEB_PORT), PictHTTPRequestHandler) as httpd:
        print(f"[pict] HTTP server at http://0.0.0.0:{WEB_PORT}/ (serving {RECORDINGS_DIR})")
        try:
            httpd.serve_forever()
        except Exception:
            pass

def record_for_duration(duration_seconds: int):
    """
    Record for a fixed duration (seconds).
    """
    from picamera import PiCamera
    stop_rpi_cam_interface()
    out_path = build_output_path()
    print(f"[pict] Fixed-duration mode: {duration_seconds}s -> {out_path.name}")
    cam = PiCamera(resolution=CAMERA_CONFIG["resolution"])
    try:
        cam.framerate = CAMERA_CONFIG["framerate"]
        # live annotation loop (updates every second)
        start = _now_local()
        end   = start + timedelta(seconds=duration_seconds)
        cam.start_recording(str(out_path), bitrate=CAMERA_CONFIG["bitrate"])
        while not stop_requested and _now_local() < end:
            remaining = int((_seconds_until(end)))
            overlay = (
                f"{CAMERA_CONFIG['annotation_label']}\n"
                f"{_now_local().strftime('%Y-%m-%d %H:%M:%S')} | "
                f"{CAMERA_CONFIG['resolution'][0]}x{CAMERA_CONFIG['resolution'][1]} @ {CAMERA_CONFIG['framerate']}fps | "
                f"{CAMERA_CONFIG['bitrate']//1_000_000} Mb/s | "
                f"rem {max(0, remaining)}s"
            )
            cam.annotate_text = overlay
            cam.wait_recording(1)
        cam.stop_recording()
    finally:
        cam.close()
        print("[pict] Recording stopped; camera closed.")
    wrap_to_mp4(out_path)

def record_until_wittypi_shutdown():
    """
    Wait for Witty Pi's next shutdown and record until 1 minute before that time.
    """
    from picamera import PiCamera
    # Wait loop
    next_shutdown = None
    while not stop_requested:
        next_shutdown = get_next_shutdown_from_wittypi()
        if next_shutdown:
            remain = _seconds_until(next_shutdown)
            if remain > SAFETY_MARGIN_SECONDS + 5:
                break
        print("[pict] No Witty Pi 'next shutdown' yet; rechecking...")
        for _ in range(CHECK_INTERVAL):
            if stop_requested:
                print("[pict] Stop requested while waiting; exiting wait loop.")
                return
            time.sleep(1)

    if not next_shutdown:
        print("[pict] Witty Pi schedule unavailable; exiting.")
        return

    stop_at = next_shutdown - timedelta(seconds=SAFETY_MARGIN_SECONDS)
    if _seconds_until(stop_at) <= 0:
        print("[pict] Shutdown too soon to record; exiting.")
        return

    # Start recording
    stop_rpi_cam_interface()
    out_path = build_output_path()
    print(f"[pict] Next shutdown: {next_shutdown.isoformat()}")
    print(f"[pict] Will stop at:  {stop_at.isoformat()} (safety {SAFETY_MARGIN_SECONDS}s)")
    print(f"[pict] Output file:   {out_path.name}")

    cam = PiCamera(resolution=CAMERA_CONFIG["resolution"])
    try:
        cam.framerate = CAMERA_CONFIG["framerate"]
        cam.start_recording(str(out_path), bitrate=CAMERA_CONFIG["bitrate"])
        # live annotation loop (updates every second)
        while not stop_requested and _seconds_until(stop_at) > 0:
            remaining = int(max(0, _seconds_until(stop_at)))
            overlay = (
                f"{CAMERA_CONFIG['annotation_label']}\n"
                f"{_now_local().strftime('%Y-%m-%d %H:%M:%S')} | "
                f"{CAMERA_CONFIG['resolution'][0]}x{CAMERA_CONFIG['resolution'][1]} @ {CAMERA_CONFIG['framerate']}fps | "
                f"{CAMERA_CONFIG['bitrate']//1_000_000} Mb/s | "
                f"rem {remaining}s"
            )
            cam.annotate_text = overlay
            cam.wait_recording(1.0)
        cam.stop_recording()
    finally:
        cam.close()
        print("[pict] Recording stopped; camera closed.")
    wrap_to_mp4(out_path)

def main():
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    # Start HTTP server in background
    threading.Thread(target=serve_http, daemon=True).start()

    # Decide mode
    if isinstance(DURATION_SECONDS, int) and 1 <= DURATION_SECONDS <= 18000:
        record_for_duration(DURATION_SECONDS)
    else:
        record_until_wittypi_shutdown()

if __name__ == "__main__":
    main()
