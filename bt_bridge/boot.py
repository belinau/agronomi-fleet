# boot.py — AgroNomi Pico 2W BLE Bridge
#
# This file intentionally left minimal.
# The REPL dupterm is NOT removed because on RP2/Pico 2W,
# os.dupterm(None, 0) can interfere with USB CDC output.
#
# main.py auto-runs after boot.py completes. DO NOT use mpremote
# to "start" main.py — let the boot sequence handle it.
# Only use mpremote for file deployment (cp, fs, etc.), never exec/run.
