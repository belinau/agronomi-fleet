"""µReticulum — Soil Node Boot Script (SN-SOIL-01)

Minimal boot.py that prepares the filesystem and launches main.py.
On ESP32-C6 this runs before main.py on every boot (including deep-sleep
wakeups).
"""

import gc

# Garbage-collect before launching the application
gc.collect()

# The MicroPython runtime automatically executes main.py after boot.py.
# No additional setup is required here — all initialisation happens in
# main.py so that deep-sleep wakeups go through the same code path.
