#!/usr/bin/env python3
"""test_pico_serial.py — Diagnostic tool for Pico 2W USB CDC serial output.

Connects to the Pico's serial port and reads lines for a configurable
duration. Use this to verify that:

1. The Pico is outputting data on the serial port
2. boot.py has properly redirected the REPL away from USB CDC
3. main.py is producing [SER], [HB], and [JSON] lines

Usage:
    python3 test_pico_serial.py [--port /dev/pico] [--baud 115200] [--timeout 30]

Exit codes:
    0 — Pico data received and at least one [SER] line seen
    1 — Pico data received but no [SER] lines (REPL may not be redirected)
    2 — No data received at all (USB CDC issue)
    3 — Port not found / cannot open
"""

import argparse
import sys
import time

import serial


def main():
    parser = argparse.ArgumentParser(description="Test Pico 2W serial output")
    parser.add_argument(
        "--port", default="/dev/pico", help="Serial port (default: /dev/pico)"
    )
    parser.add_argument(
        "--baud", type=int, default=115200, help="Baud rate (default: 115200)"
    )
    parser.add_argument(
        "--timeout", type=int, default=30, help="Seconds to listen (default: 30)"
    )
    args = parser.parse_args()

    print(f"[TEST] Opening {args.port} at {args.baud} baud...")
    try:
        s = serial.Serial(args.port, args.baud, timeout=1.0)
    except serial.SerialException as e:
        print(f"[FAIL] Cannot open {args.port}: {e}")
        print("[HINT] Check that:")
        print("  1. The Pico is connected via USB")
        print("  2. /dev/pico udev symlink exists (or use --port)")
        print("  3. No other process (ble_forwarder) is using the port")
        return 3

    print(f"[TEST] Listening for {args.timeout}s...")
    deadline = time.time() + args.timeout
    lines_received = 0
    ser_lines = 0
    hb_lines = 0
    json_lines = 0
    repl_lines = 0
    raw_bytes = 0

    try:
        while time.time() < deadline:
            line_bytes = s.readline()
            if not line_bytes:
                continue

            raw_bytes += len(line_bytes)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            lines_received += 1

            # Classify the line
            if line.startswith("[SER]"):
                ser_lines += 1
                print(f"  [BOOT] {line}")
            elif line.startswith("[HB]"):
                hb_lines += 1
                print(f"  [HEARTBEAT] {line}")
            elif line.startswith("[JSON]"):
                json_lines += 1
                print(f"  [TELEMETRY] {line[:80]}...")
            elif line.startswith("[C] ") or line.startswith("[D] "):
                print(f"  [BLE] {line}")
            elif line.startswith(">>>") or line.startswith("MicroPython"):
                repl_lines += 1
                print(f"  [REPL!] {line}")
            else:
                print(f"  [OTHER] {line}")

    except KeyboardInterrupt:
        print("\n[TEST] Interrupted")

    s.close()

    # Summary
    print(f"\n[TEST] === Results ===")
    print(f"  Total lines:  {lines_received}")
    print(f"  Raw bytes:    {raw_bytes}")
    print(f"  [SER] lines:  {ser_lines}")
    print(f"  [HB] lines:   {hb_lines}")
    print(f"  [JSON] lines: {json_lines}")
    print(f"  REPL lines:   {repl_lines}")

    if lines_received == 0:
        print("\n[FAIL] No data received from Pico at all!")
        print("[HINT] The Pico is not sending serial data. Possible causes:")
        print("  1. main.py is NOT running — Pico is stuck in REPL prompt")
        print("  2. main.py crashed — check for exceptions with mpremote repl")
        print("  3. USB cable is charge-only (no data wires)")
        print("  4. Pico is not powered / not connected")
        print("  5. Another process has the serial port open (stop ble_forwarder)")
        return 2

    if repl_lines > 0:
        print("\n[WARN] REPL output detected on serial port!")
        print("[HINT] main.py is not running, Pico is in REPL. Start it:")
        print("  mpremote cp bt_bridge/main.py :main.py")
        print("  mpremote reset  # hard reset ensures main.py auto-runs")
        return 1

    if ser_lines > 0:
        print("\n[PASS] Pico serial output is working correctly!")
        return 0

    print("\n[WARN] Data received but no [SER] lines found.")
    print("[HINT] The Pico may be running an older version of main.py.")
    print("  Redeploy: mpremote cp bt_bridge/main.py :main.py")
    print("  Then hard reset (power cycle or mpremote reset) to ensure main.py runs.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
