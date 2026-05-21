#!/usr/bin/env python3
# pair_rnode.py - Run on Mac

import sys
import time

import serial

RNODE_PORT = "/dev/cu.usbmodem23401"  # your RNode
C6_PORT = "/dev/cu.usbmodem23101"  # your C6 - adjust


class LineReader:
    def __init__(self, port, prefix):
        self.port = port
        self.prefix = prefix
        self.buffer = bytearray()

    def read_and_print(self):
        if self.port.in_waiting:
            data = self.port.read(self.port.in_waiting)
            self.buffer.extend(data)
            while b"\n" in self.buffer:
                line_bytes, self.buffer = self.buffer.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="ignore").strip()
                if line:
                    print(f"[{self.prefix}] {line}")


print("Connecting to RNode...")
s_rnode = serial.Serial(RNODE_PORT, 115200, timeout=0.1)

print("Connecting to ESP32-C6...")
s_c6 = serial.Serial(C6_PORT, 115200, timeout=0.1)

# Step 1: Trigger pairing mode on RNode and get PIN (ONLY ONCE)
print("Triggering pairing mode on RNode...")
s_rnode.write(bytes([0xC0, 0x46, 0x02, 0xC0]))  # CMD_BT_CTRL 0x02 = enable pairing
time.sleep(1)

pin = None
buf = bytearray()
in_frame = False
deadline = time.time() + 10
while time.time() < deadline:
    b = s_rnode.read(1)
    if not b:
        continue
    b = b[0]
    if b == 0xC0:
        if in_frame and len(buf) >= 5 and buf[0] == 0x62:
            p = buf[1:5]
            pin = (p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3]
            break
        in_frame = True
        buf = bytearray()
    elif in_frame:
        buf.append(b)

if not pin:
    print("Failed to get PIN from RNode")
    s_rnode.close()
    s_c6.close()
    sys.exit(1)

print(f"RNode PIN Obtained: {pin:06d}")

# Step 2: Use MicroPython REPL to program the C6 automatically
print("Sending Ctrl+C to C6 to seize prompt...")
s_c6.write(b"\x03\x03")  # Send Ctrl+C twice to break out of any loops
time.sleep(0.5)

print("Writing PIN to ble_pin.txt on C6...")
s_c6.write(f'\r\nopen("ble_pin.txt","w").write("{pin:06d}")\r\n'.encode())
time.sleep(0.5)

print("Forcing fresh pairing session on C6...")
s_c6.write(b'\r\nopen("force_pair.txt","w").write("1")\r\n')
time.sleep(0.5)

print("Sending soft-reboot (Ctrl+D) to C6...")
s_c6.write(b"\x04")  # Ctrl+D triggers soft reset
time.sleep(1.0)

# Refresh RNode pairing window: Reset its pairing timer without changing the PIN
print("Refreshing RNode pairing timer for another 35 seconds...")
s_rnode.write(bytes([0xC0, 0x46, 0x02, 0xC0]))
time.sleep(0.5)

print("Triggering 'import main' on C6...")
s_c6.write(b"\r\nimport main\r\n")

print("\n" + "=" * 70)
print("MONITORING MODE ACTIVE: INTERLEAVED DEVICE LOGS")
print("Press Ctrl+C to terminate when bonding succeeds.")
print("=" * 70 + "\n")

rnode_reader = LineReader(s_rnode, "RNode Serial")
c6_reader = LineReader(s_c6, "ESP32-C6")

try:
    while True:
        rnode_reader.read_and_print()
        c6_reader.read_and_print()
        time.sleep(0.01)
except KeyboardInterrupt:
    print("\nExiting and releasing serial ports...")
finally:
    s_rnode.close()
    s_c6.close()
