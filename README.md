# PICT WittyPi Camera Recorder

A Raspberry Pi Zero 2 W camera recorder that integrates with **WittyPi** schedules or runs for a **fixed duration**.  
Provides a lightweight **web interface** to control recording, manage files, view logs, view system info, and preview the camera.

---

## Features
- Records using legacy **picamera** module
- Two modes:
  - **WittyPi** mode → records until 1 minute before next shutdown
  - **Fixed duration** mode → record N seconds
- Stops **RPi Cam Web Interface** before recording (no sudo password needed)
- **Annotation overlay** with timestamp, hostname, resolution, fps, quality/bitrate, remaining time
- Files saved as `.mp4` only (raw `.h264` deleted after wrapping)
- Web UI (`http://<pi-ip>:8123`) provides:
  - Status panel (with auto-refresh)
  - Start/Stop controls
  - Config table
  - System info (disk, RAM, CPU usage)
  - Recordings list (download/delete)
  - `schedule.wpi` editor
  - Logs viewer (last lines with scroll)
  - Preview stream (MJPEG, no storage)

---

## Installation

1. SSH into your Pi, then run:

```bash
cd ~
wget https://github.com/<youruser>/pict-cam-wittypi-recorder/archive/refs/heads/main.zip -O pict-cam-wittypi-recorder.zip
unzip pict-cam-wittypi-recorder.zip
mv pict-cam-wittypi-recorder-main pict-cam-wittypi-recorder
cd pict-cam-wittypi-recorder
bash install.sh
```
  
> Systemd unit paths expect the folder to be `~/pict-cam-wittypi-recorder`.

---

## Usage

- Open the web UI at:  
  `http://raspberrypi.local:8123/`  
  or  
  `http://<pi-ip>:8123/`

- Files are saved to:  
  `~/recordings/`

---

## Configuration

- Edit the top of `recorder.py` to change:
  - Resolution
  - Framerate
  - Bitrate/Quality
  - Annotation label
  - File extension
  - Duration mode (fixed seconds vs WittyPi schedule)

- Restart the service after editing configs:
  ```bash
  sudo systemctl restart pict-recorder.service
  ```

---

##  Uninstall

```bash
cd ~/pict-cam-wittypi-recorder
bash uninstall.sh
```

---

##  Systemd management

Check status:
```bash
systemctl status pict-recorder.service
```

View logs:
```bash
journalctl -u pict-recorder.service -n 50
```

Restart service:
```bash
sudo systemctl restart pict-recorder.service
```
