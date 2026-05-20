"""µReticulum — Pump Actuator Node Boot Script (AN-PUMP-01)

Minimal boot.py for the pump actuator node.  Since this node never
deep-sleeps, boot.py only runs on initial power-on or hard reset.
"""

import gc

# Garbage-collect before launching the application
gc.collect()

# The MicroPython runtime automatically executes main.py after boot.py.
# All initialisation happens in main.py — this includes the async event
# loop that keeps the actuator node alive and listening for commands.
