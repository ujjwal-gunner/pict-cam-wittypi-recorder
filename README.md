# pict-cam-wittypi-recorder

Records video with legacy PiCamera until 1 min before WittyPi shutdown. Stops RPi Cam Web Interface first. Wraps .h264 into .mp4. Serves recordings at http://<pi>:8080/

## Install

```bash
cd ~
wget https://github.com/ujjwal-gunner/pict-cam-wittypi-recorder/archive/refs/heads/main.zip -O pict-cam-wittypi-recorder.zip
unzip pict-cam-wittypi-recorder.zip
cd pict-cam-wittypi-recorder-main
chmod +x install.sh uninstall.sh
./install.sh
```

## Configure

Edit camera parameters at top of `recorder.py`.

## Uninstall

```bash
./uninstall.sh
```
