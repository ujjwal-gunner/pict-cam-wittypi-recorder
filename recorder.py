#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
pict-cam-wittypi-recorder (full-feature build)

Web UI @ http://<pi>:8123
- Status (Recording WittyPi / Recording Duration / Waiting / Idle) with remaining time
- Controls: Start duration, Re-run WittyPi mode, Stop recording
- Config table (resolution, fps, quality, mode, dirs, ports, etc.)
- List / Download / Delete recordings
- View/Edit schedule.wpi
- Logs viewer (scrollable, multiline)
- MJPEG Preview stream (/preview) when NOT recording (no storage)

Camera handling:
- Legacy picamera (PiCamera) with retry/open/close management
- Annotation overlay updated every second (label | time | res | fps | quality | remaining)
- Stops RPi Cam Web Interface (no sudo): tries stop.sh and pkill raspimjpeg
- Keeps service alive after each recording (never exits on its own)
"""

import os, re, time, signal, threading, http.server, socketserver, subprocess, urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from collections import deque
import shutil, psutil
import socket
HOSTNAME = socket.gethostname()

# =========================
# CAMERA / RUNTIME CONFIG
# =========================
CAMERA_CONFIG = {
    "resolution": (1296, 972),     # width, height
    "framerate": 15,               # FPS
    "bitrate": 4_000_000,          # bits per second (single quality knob), currently not used in code.
    "quality": 22,
    "annotation_label": "PICT WittyPi Recorder",
    "file_extension": ".h264",     # raw H.264 (mp4 wrap optional via ffmpeg)
}

# MODE SWITCH:
#   - Set DURATION_SECONDS = None --> Witty Pi mode (wait/record until 1 min before next shutdown)
#   - Set DURATION_SECONDS = N (int, 1..18000) --> Fixed-duration mode (record for N seconds)
DURATION_SECONDS = None  # e.g., 600 for 10 minutes; must be <= 18000 to be used

RECORDINGS_DIR = Path.home() / "recordings"
WITTYPI_DIR_CANDIDATES = [Path.home() / "wittypi", Path("/home/pi/wittypi"), Path("/home/pi/WittyPi")]
RUNSCRIPT_CANDIDATES   = [d / "runScript.sh" for d in WITTYPI_DIR_CANDIDATES]
SCHEDULE_FILE_CANDIDATES = [d / "schedule.wpi" for d in WITTYPI_DIR_CANDIDATES]

SAFETY_MARGIN_SECONDS  = 60         # stop 1 minute before Witty Pi shutdown
CHECK_INTERVAL         = 30         # poll Witty Pi every N seconds
WEB_PORT               = 8123       # HTTP server port

# RPi Cam Web Interface stop options (no sudo usage)
RPICAM_STOP_CANDIDATES = [
    ["/home/pi/RPi_Cam_Web_Interface/stop.sh"],
    ["/var/www/html/RPi_Cam_Web_Interface/stop.sh"],
]
RPICAM_SERVICE_NAMES = ["raspimjpeg"]  # we'll pkill instead of systemctl to avoid sudo

# ------------- Logging -------------
LOG_PATH = RECORDINGS_DIR / "recorder.log"
LOG_MAX = 1000   # keep last 1000 lines
LOG = deque(maxlen=LOG_MAX)
def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG.append(line)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
        # truncate file to last LOG_MAX lines
        with LOG_PATH.open("r+") as f:
            lines = f.readlines()
            if len(lines) > LOG_MAX:
                f.seek(0)
                f.writelines(lines[-LOG_MAX:])
                f.truncate()
    except Exception:
        pass


# ------------- Global State -------------
state_lock = threading.Lock()
state = {
    "mode": "idle",                 # idle | waiting | recording_duration | recording_wittypi
    "recording": False,
    "record_start": None,           # datetime
    "record_stop_target": None,     # datetime
    "last_error": "",
    "preview_on": False,
}

stop_requested = threading.Event()
record_stop_event = threading.Event()

# ------------- Utilities -------------
def _now_local():
    return datetime.now().astimezone()

def _seconds_until(ts: datetime) -> float:
    return (ts - _now_local()).total_seconds()

def _find_existing(paths):
    for p in paths:
        if Path(p).exists():
            return Path(p)
    return None

def _safe_child_of(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False

def html_escape(s: str) -> str:
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def build_output_path():
    ts = _now_local().strftime("%Y%m%d_%H%M%S")
    return RECORDINGS_DIR / f"{HOSTNAME}_{ts}{CAMERA_CONFIG['file_extension']}"

def wrap_to_mp4(h264_file: Path):
    # Optional: wrap to mp4 if ffmpeg is installed
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception:
        log("ffmpeg not found; skipping mp4 wrap")
        return
    mp4_file = h264_file.with_suffix(".mp4")
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-framerate", str(CAMERA_CONFIG["framerate"]),
            "-i", str(h264_file),
            "-c:v", "copy",
            str(mp4_file)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"Wrapped {h264_file.name} -> {mp4_file.name}")
        # ✅ Delete the .h264 after success
        h264_file.unlink(missing_ok=True)
    except Exception as e:
        log(f"MP4 wrap failed: {e}")


# ------------- Witty Pi -------------
def get_next_shutdown_from_wittypi():
    """
    Use Witty Pi's runScript.sh output to obtain 'Next shutdown at:'.
    Returns tz-aware datetime or None.
    """
    runscript = _find_existing(RUNSCRIPT_CANDIDATES)
    if not runscript:
        return None
    try:
        out = subprocess.check_output(["bash", str(runscript)], stderr=subprocess.STDOUT).decode("utf-8", "ignore")
    except Exception as e:
        log(f"runScript.sh error: {e}")
        return None
    m = re.search(r"Next shutdown at:\s*([0-9-]+\s+[0-9:]+)", out)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
    except Exception:
        return None

def schedule_path():
    return _find_existing(SCHEDULE_FILE_CANDIDATES) or SCHEDULE_FILE_CANDIDATES[0]

# ------------- System Info Panel -------------
def system_info_html():
    try:
        du = shutil.disk_usage("/")
        disk_str = f"{du.used/1e9:.1f} GB used / {du.total/1e9:.1f} GB total ({100*du.used/du.total:.1f}%)"
    except Exception:
        disk_str = "N/A"
    try:
        vm = psutil.virtual_memory()
        ram_str = f"{vm.available/1e6:.1f} MB free / {vm.total/1e6:.1f} MB total ({vm.percent}% used)"
    except Exception:
        ram_str = "N/A"
    try:
        cpu_str = f"{psutil.cpu_percent(interval=0.5)} %"
    except Exception:
        cpu_str = "N/A"
    return f"""
<h3>System Info</h3>
<table border='1' cellspacing='0' cellpadding='6'>
<tr><td>Disk</td><td>{disk_str}</td></tr>
<tr><td>RAM</td><td>{ram_str}</td></tr>
<tr><td>CPU</td><td>{cpu_str}</td></tr>
</table>
"""


# ------------- RPi Cam Web Interface -------------
def stop_rpi_cam_interface():
    # Try stop.sh scripts (many installs work without sudo)
    for cmd in RPICAM_STOP_CANDIDATES:
        try:
            if Path(cmd[0]).exists():
                subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    # Kill raspimjpeg if still running (no sudo)
    for name in RPICAM_SERVICE_NAMES:
        try:
            subprocess.run(["/usr/bin/pkill", "-f", name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    log("RPi Cam Web Interface stop attempted (stop.sh/pkill).")

# ------------- Camera Manager -------------
class CameraManager:
    """
    Manages a single PiCamera instance with retry, overlay and preview helpers.
    """
    def __init__(self):
        self.cam = None
        self.lock = threading.Lock()
        self.preview_thread = None
        self.preview_stop = threading.Event()

    def open_with_retry(self, attempts=4, delay=2.0):
        with self.lock:
            if self.cam is not None:
                return True
            for i in range(1, attempts+1):
                try:
                    from picamera import PiCamera
                    self.cam = PiCamera(resolution=CAMERA_CONFIG["resolution"])
                    self.cam.framerate = CAMERA_CONFIG["framerate"]
                    log("Camera opened")
                    return True
                except Exception as e:
                    log(f"Camera open failed (attempt {i}/{attempts}): {e}")
                    time.sleep(delay)
            with state_lock:
                state["last_error"] = "Cannot open camera (MMAL/ENOMEM or busy)."
            return False

    def close(self):
        with self.lock:
            try:
                if self.cam:
                    self.cam.close()
                    log("Camera closed")
            except Exception:
                pass
            self.cam = None

    def set_overlay(self, text: str):
        with self.lock:
            if self.cam:
                try:
                    self.cam.annotate_text = text
                except Exception:
                    pass

    def start_recording(self, path: Path):
        with self.lock:
            if not self.cam:
                return False
            try:
                self.cam.start_recording(str(path), quality=CAMERA_CONFIG["quality"])
                return True
            except Exception as e:
                log(f"start_recording error: {e}")
                return False

    def wait_recording(self, sec=1.0):
        with self.lock:
            if self.cam:
                try:
                    self.cam.wait_recording(sec)
                except Exception as e:
                    log(f"wait_recording error: {e}")

    def stop_recording(self):
        with self.lock:
            if self.cam:
                try:
                    self.cam.stop_recording()
                except Exception:
                    pass

    # ---- MJPEG Preview (no storage) ----
    def _mjpeg_streamer(self, wfile):
        """
        Start a MJPEG stream and write frames to wfile as multipart.
        Only called when NOT recording.
        """
        class _FrameWriter:
            boundary = b"--FRAME\r\nContent-Type: image/jpeg\r\n\r\n"
            def __init__(self, out): self.out = out
            def write(self, b):
                try:
                    self.out.write(self.boundary)
                    self.out.write(b)
                    self.out.write(b"\r\n")
                except Exception:
                    pass
            def flush(self): pass

        if not self.open_with_retry():
            try: wfile.write(b"")
            except Exception: pass
            return

        writer = _FrameWriter(wfile)

        with self.lock:
            try:
                self.cam.start_recording(writer, format='mjpeg')
            except Exception as e:
                log(f"Preview start error: {e}")
                return

        with state_lock:
            state["preview_on"] = True
        self.preview_stop.clear()
        log("Preview started")

        try:
            while not self.preview_stop.is_set():
                self.wait_recording(0.2)
        finally:
            with self.lock:
                try:
                    if self.cam:
                        self.cam.stop_recording()
                except Exception:
                    pass
            with state_lock:
                state["preview_on"] = False
            log("Preview stopped")

    def start_preview(self, wfile):
        t = threading.Thread(target=self._mjpeg_streamer, args=(wfile,), daemon=True)
        self.preview_thread = t
        t.start()

    def stop_preview(self):
        self.preview_stop.set()
        if self.preview_thread and self.preview_thread.is_alive():
            self.preview_thread.join(timeout=2.0)

CAM = CameraManager()

# ------------- Recording -------------
def annotate(rem_s: int):
    return (
        f"{CAMERA_CONFIG['annotation_label']} ({HOSTNAME})\n"
        f"{_now_local().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"{CAMERA_CONFIG['resolution'][0]}x{CAMERA_CONFIG['resolution'][1]} @ {CAMERA_CONFIG['framerate']}fps | "
        f"Q={CAMERA_CONFIG['quality']} | rem {max(0, rem_s)}s"
    )

def do_record_until(stop_at: datetime):
    out_path = build_output_path()
    log(f"Recording -> {out_path.name} (stop at {stop_at.isoformat()})")
    stop_rpi_cam_interface()
    if not CAM.open_with_retry():
        log("Cannot open camera; aborting recording.")
        return

    if not CAM.start_recording(out_path):
        log("start_recording failed; aborting.")
        return

    with state_lock:
        state["recording"] = True
        state["record_start"] = _now_local()
        state["record_stop_target"] = stop_at

    try:
        while not record_stop_event.is_set() and _seconds_until(stop_at) > 0:
            rem = int(max(0, _seconds_until(stop_at)))
            CAM.set_overlay(annotate(rem))
            CAM.wait_recording(1.0)
    finally:
        CAM.stop_recording()
        with state_lock:
            state["recording"] = False
            state["record_stop_target"] = None
            state["mode"] = "idle"   # ensure UI resets to idle
        log("Recording stopped; closing camera.")
        CAM.close()

    wrap_to_mp4(out_path)

def worker_wittypi_loop():
    while not stop_requested.is_set():
        with state_lock:
            state["mode"] = "waiting"
        next_shutdown = get_next_shutdown_from_wittypi()
        if next_shutdown:
            remain = _seconds_until(next_shutdown)
            if remain > SAFETY_MARGIN_SECONDS + 5:
                stop_at = next_shutdown - timedelta(seconds=SAFETY_MARGIN_SECONDS)
                with state_lock:
                    state["mode"] = "recording_wittypi"
                record_stop_event.clear()
                do_record_until(stop_at)
                # loop to wait for next schedule again
                continue
        time.sleep(CHECK_INTERVAL)

def worker_duration_once(seconds: int):
    with state_lock:
        state["mode"] = "recording_duration"
    stop_at = _now_local() + timedelta(seconds=seconds)
    record_stop_event.clear()
    do_record_until(stop_at)
    # mode is reset to idle in do_record_until()

# ------------- Web UI helpers -------------
def format_bytes(n):
    return f"{n/1048576:.1f} MB"

def list_files_rows():
    rows = []
    for p in sorted(RECORDINGS_DIR.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
        if not p.is_file():
            continue
        name = p.name
        size = format_bytes(p.stat().st_size)
        rows.append(
            f"<tr><td>{name}</td><td>{size}</td>"
            f"<td><a href='/download?name={urllib.parse.quote(name)}'>download</a></td>"
            f"<td><a href='/delete?name={urllib.parse.quote(name)}' onclick=\"return confirm('Delete {name}?')\">delete</a></td></tr>"
        )
    return "\n".join(rows)

def config_table():
    kv = {
        "resolution": f"{CAMERA_CONFIG['resolution'][0]}x{CAMERA_CONFIG['resolution'][1]}",
        "framerate": CAMERA_CONFIG["framerate"],
        "quality": f"{CAMERA_CONFIG['quality']}",
        "label": CAMERA_CONFIG["annotation_label"],
        "file_extension": CAMERA_CONFIG["file_extension"],
        "mode": ("WittyPi (DURATION_SECONDS=None)" if (DURATION_SECONDS is None) else f"Fixed duration {DURATION_SECONDS}s"),
        "web_port": WEB_PORT,
        "recordings_dir": str(RECORDINGS_DIR),
        "wittypi_dir": str(_find_existing(WITTYPI_DIR_CANDIDATES) or "(not found)"),
        "schedule_file": str(schedule_path()),
    }
    rows = "\n".join([f"<tr><td>{k}</td><td>{html_escape(str(v))}</td></tr>" for k,v in kv.items()])
    return f"<table border='1' cellspacing='0' cellpadding='6'>{rows}</table>"

def status_panel():
    with state_lock:
        m = state["mode"]
        rec = state["recording"]
        preview = state["preview_on"]
        stop_target = state["record_stop_target"]
        last_err = state["last_error"]
    rem = ""
    if stop_target:
        s = int(max(0, _seconds_until(stop_target)))
        rem = f" | remaining: {s}s"
    extras = []
    if rec: extras.append("recording")
    if preview: extras.append("preview ON")
    extras_str = f" ({', '.join(extras)})" if extras else ""
    err_str = f"<br/><span style='color:#a00;'>Last error: {html_escape(last_err)}</span>" if last_err else ""
    return f"<p><b>Status:</b> {html_escape(m)}{extras_str}{rem}{err_str}</p>"

def schedule_editor_html():
    sp = schedule_path()
    try:
        body = sp.read_text() if sp.exists() else "# schedule.wpi (create/save)\n"
    except Exception as e:
        body = f"# error reading schedule.wpi: {e}\n"
    esc = html_escape(body)
    return f"""
<h3>schedule.wpi</h3>
<form method="POST" action="/schedule">
<textarea name="content" rows="20" cols="100" style="font-family:monospace;">{esc}</textarea><br/>
<button type="submit">Save</button>
</form>
"""

# ------------- HTTP Server -------------
class UIHandler(http.server.BaseHTTPRequestHandler):
    # suppress default request logging
    def log_message(self, format, *args):
        return

    def _send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            logs_text = html_escape("\n".join(LOG))
            page = f"""
<html>
<head>
<meta charset="utf-8">
<title>PICT Recorder</title>
<meta http-equiv="refresh" content="10">
</head>
<body>
<h2>PICT Recorder</h2>
{status_panel()}

<h3>Controls</h3>
<form method="POST" action="/start_duration" style="margin-bottom:8px;">
Start duration (1..18000 s): <input name="seconds" size="8"/>
<button type="submit">Start</button>
</form>
<form method="POST" action="/start_wittypi" style="display:inline;">
<button type="submit">Re-run WittyPi mode</button>
</form>
<form method="POST" action="/stop" style="display:inline;margin-left:8px;">
<button type="submit">Stop recording</button>
</form>
<p>Preview (new tab, only when not recording): <a href="/preview" target="_blank">/preview</a></p>

<h3>Configuration</h3>
{config_table()}
{system_info_html()}

<h3>Recordings</h3>
<table border="1" cellspacing="0" cellpadding="6">
<tr><th>File</th><th>Size</th><th>Download</th><th>Delete</th></tr>
{list_files_rows()}
</table>

{schedule_editor_html()}

<h3>Logs (latest)</h3>
<pre style="background:#111;color:#0f0;padding:8px;max-height:360px;overflow-y:auto;white-space:pre;">{logs_text}</pre>
</body></html>
"""
            self._send_html(page)
            return

        if path == "/download":
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
            except Exception as e:
                self.send_error(500, f"Error sending file: {e}")
            return

        if path == "/delete":
            qs = urllib.parse.parse_qs(parsed.query)
            name = qs.get("name", [""])[0]
            target = (RECORDINGS_DIR / name).resolve()
            if not name or not target.exists() or not _safe_child_of(target, RECORDINGS_DIR):
                self.send_error(404, "File not found")
                return
            try:
                target.unlink()
                self.send_response(302); self.send_header("Location", "/"); self.end_headers()
            except Exception as e:
                self.send_error(500, f"Delete failed: {e}")
            return

        if path == "/preview":
            # Only when not recording
            with state_lock:
                if state["recording"]:
                    self.send_error(409, "Preview unavailable while recording")
                    return
            # Headers for multipart MJPEG
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            try:
                CAM.start_preview(self.wfile)
                while True:
                    time.sleep(0.25)
            except BrokenPipeError:
                pass
            except Exception:
                pass
            finally:
                CAM.stop_preview()
            return

        if path == "/schedule":
            html = f"<html><body>{schedule_editor_html()}<p><a href='/'>← Back</a></p></body></html>"
            self._send_html(html)
            return

        self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/schedule":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", "ignore")
            fields = urllib.parse.parse_qs(body)
            content = fields.get("content", [""])[0]
            sp = schedule_path()
            try:
                sp.parent.mkdir(parents=True, exist_ok=True)
                sp.write_text(content)
                log("schedule.wpi saved")
                self.send_response(302); self.send_header("Location", "/schedule"); self.end_headers()
            except Exception as e:
                self.send_error(500, f"Saving schedule failed: {e}")
            return

        if path == "/start_duration":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", "ignore")
            fields = urllib.parse.parse_qs(body)
            sec_str = fields.get("seconds", [""])[0].strip()
            try:
                sec = int(sec_str)
                if not (1 <= sec <= 18000):
                    raise ValueError()
            except Exception:
                self.send_error(400, "seconds must be integer 1..18000")
            else:
                with state_lock:
                    already = state["recording"]
                if already:
                    self.send_error(409, "Already recording")
                else:
                    threading.Thread(target=worker_duration_once, args=(sec,), daemon=True).start()
                    self.send_response(302); self.send_header("Location", "/"); self.end_headers()
            return

        if path == "/start_wittypi":
            with state_lock:
                already = state["recording"]
            if already:
                self.send_error(409, "Already recording")
            else:
                threading.Thread(target=worker_wittypi_loop, daemon=True).start()
                self.send_response(302); self.send_header("Location", "/"); self.end_headers()
            return

        if path == "/stop":
            with state_lock:
                already = state["recording"]
            if not already:
                self.send_response(302); self.send_header("Location", "/"); self.end_headers()
            else:
                record_stop_event.set()
                self.send_response(302); self.send_header("Location", "/"); self.end_headers()
            return

        self.send_error(404, "Unknown POST")

def serve_http():
    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True
    with ReusableTCPServer(("", WEB_PORT), UIHandler) as httpd:
        log(f"HTTP server at http://0.0.0.0:{WEB_PORT}/ (serving {RECORDINGS_DIR})")
        try:
            httpd.serve_forever()
        except Exception as e:
            log(f"HTTP server stopped: {e}")

# ------------- Main -------------
def main():
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    log("PICT recorder starting...")
    threading.Thread(target=serve_http, daemon=True).start()

    if isinstance(DURATION_SECONDS, int) and 1 <= DURATION_SECONDS <= 18000:
        threading.Thread(target=worker_duration_once, args=(DURATION_SECONDS,), daemon=True).start()
    else:
        threading.Thread(target=worker_wittypi_loop, daemon=True).start()

    try:
        while not stop_requested.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    log("PICT recorder exiting...")

def _on_signal(signum, frame):
    stop_requested.set()
    record_stop_event.set()
signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)

if __name__ == "__main__":
    main()
