#!/usr/bin/env python3
from picamera2 import Picamera2
from time import sleep

picam2 = Picamera2()

# Configure for still capture
config = picam2.create_still_configuration()
picam2.configure(config)

print("Starting camera...")
picam2.start()
sleep(2)  # Let camera warm up

print("Capturing test image...")
picam2.capture_file("test.jpg")

print("✅ Capture complete. Saved as test.jpg")
picam2.stop()
