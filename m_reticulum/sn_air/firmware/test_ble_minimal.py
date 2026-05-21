
import time
import ubluetooth as bt

print("=== Minimal BLE Test ===")
print("MicroPython:", end=" "); import sys; print(sys.version)

try:
    ble = bt.BLE()
    ble.active(True)
    print("BLE active: True")
except Exception as e:
    print("BLE init FAILED:", e)
    raise

# Try MTU config
try:
    ble.config(mtu=247)
    print("MTU config: OK")
except Exception as e:
    print("MTU config FAILED:", e)

# Try security config
try:
    ble.config(bond=True)
    ble.config(mitm=True)
    ble.config(io=2)  # KEYBOARD_ONLY
    print("Security config: OK")
except Exception as e:
    print("Security config FAILED:", e)

try:
    ble.config(le_secure=True)
    print("le_secure: OK")
except Exception as e:
    print("le_secure FAILED (may be OK):", e)

# IRQ handler
results = {"scan_found": False, "addr": None, "rssi": None}

def irq(event, data):
    if event == 5:  # _IRQ_SCAN_RESULT
        addr_type, addr, adv_type, rssi, adv_data = data
        addr = bytes(addr)
        adv_data = bytes(adv_data)
        # Check for NUS UUID in adv_data
        has_nus = False
        name = None
        i = 0
        while i + 1 < len(adv_data):
            length = adv_data[i]
            if length == 0 or i + length + 1 > len(adv_data):
                break
            ad_type = adv_data[i + 1]
            field = bytes(adv_data[i + 2 : i + 1 + length])
            if ad_type == 0x07:
                for j in range(0, len(field), 16):
                    if j + 16 <= len(field):
                        uuid_bytes = bytes(reversed(field[j:j+16]))
                        uuid_str = "{:08x}-{:04x}-{:04x}-{:04x}-{:012x}".format(
                            int.from_bytes(uuid_bytes[0:4], 'big'),
                            int.from_bytes(uuid_bytes[4:6], 'big'),
                            int.from_bytes(uuid_bytes[6:8], 'big'),
                            int.from_bytes(uuid_bytes[8:10], 'big'),
                            int.from_bytes(uuid_bytes[10:16], 'big')
                        )
                        if uuid_str.lower() == "6e400001-b5a3-f393-e0a9-e50e24dcca9e":
                            has_nus = True
            elif ad_type == 0x09:
                try: name = field.decode("utf-8")
                except: name = field.decode("latin-1")
            elif ad_type == 0x08 and name is None:
                try: name = field.decode("utf-8")
                except: pass
            i += length + 1

        if has_nus or (name and name.startswith("RNode ")):
            results["scan_found"] = True
            results["addr"] = (addr_type, addr)
            results["rssi"] = rssi
            results["name"] = name
            print("FOUND:", name, "RSSI:", rssi, "ADDR:", ":".join("%02x" % b for b in addr))
    elif event == 6:  # _IRQ_SCAN_DONE
        print("SCAN DONE")

ble.irq(irq)

print("\nStarting 10s scan for RNode...")
ble.gap_scan(10000, 30000, 30000, True)

t0 = time.time()
while time.time() - t0 < 12:
    time.sleep(1)
    if results["scan_found"]:
        print("\nAttempting connect...")
        addr_type, addr = results["addr"]
        try:
            ble.gap_connect(addr_type, addr)
            print("Connect initiated")
        except Exception as e:
            print("Connect FAILED:", e)
        break

# Wait a bit more for connect events
t0 = time.time()
while time.time() - t0 < 5:
    time.sleep(1)

print("\n=== Test Complete ===")
print("Found:", results["scan_found"])
print("Name:", results.get("name"))
print("RSSI:", results.get("rssi"))
