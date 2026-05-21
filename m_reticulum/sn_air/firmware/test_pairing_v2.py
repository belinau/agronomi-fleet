
import time
import ubluetooth as bt

print("=== RNode Pairing Test v2 ===")
print("IMPORTANT: Edit the PIN variable below before running!")

_IRQ_PERIPHERAL_CONNECT = 7
_IRQ_PERIPHERAL_DISCONNECT = 8
_IRQ_GATTC_SERVICE_RESULT = 9
_IRQ_GATTC_SERVICE_DONE = 10
_IRQ_GATTC_CHARACTERISTIC_RESULT = 11
_IRQ_GATTC_CHARACTERISTIC_DONE = 12
_IRQ_GATTC_DESCRIPTOR_RESULT = 13
_IRQ_GATTC_DESCRIPTOR_DONE = 14
_IRQ_GATTC_WRITE_DONE = 17
_IRQ_GATTC_NOTIFY = 18
_IRQ_MTU_EXCHANGED = 21
_IRQ_ENCRYPTION_UPDATE = 28
_IRQ_GET_SECRET = 29
_IRQ_SET_SECRET = 30
_IRQ_PASSKEY_ACTION = 31

_PASSKEY_ACTION_INPUT = 2

# ============================================================
# SET YOUR RNode PIN HERE (the 6-digit number on RNode OLED)
# Set to 0 to test WITHOUT pairing (Just Works)
# ============================================================
PIN = 0  # <-- CHANGE THIS!

ble = bt.BLE()
ble.active(True)
ble.config(mtu=247)
ble.config(bond=True)
ble.config(mitm=True)
ble.config(io=2)
try:
    ble.config(le_secure=True)
    print("le_secure: OK")
except Exception as e:
    print("le_secure:", e)

state = {
    "connected": False,
    "encrypted": False,
    "mtu": 23,
    "service": None,
    "rx": None,
    "tx": None,
    "cccd": None,
    "subscribed": False,
    "notified": False,
    "passkey_action": False,
    "secrets": {},
    "conn_handle": None,
    "events": [],
}

def log_event(msg):
    state["events"].append(msg)
    print(msg)

def irq(event, data):
    try:
        if event == _IRQ_PERIPHERAL_CONNECT:
            conn_handle, addr_type, addr = data
            state["connected"] = True
            state["conn_handle"] = conn_handle
            log_event("[CONNECT] handle={}, addr={}".format(conn_handle, ":".join("%02x" % b for b in addr)))
            if PIN:
                log_event("[PAIR] gap_pair with PIN {:06d}".format(PIN))
                ble.gap_pair(conn_handle)
            else:
                log_event("[PAIR] No PIN, skipping gap_pair")

        elif event == _IRQ_PERIPHERAL_DISCONNECT:
            conn_handle, addr_type, addr = data
            log_event("[DISCONNECT] handle={}".format(conn_handle))
            state["connected"] = False
            state["encrypted"] = False

        elif event == _IRQ_ENCRYPTION_UPDATE:
            conn_handle, encrypted, authenticated, bonded, key_size = data
            log_event("[ENCRYPT] enc={} auth={} bond={} key={}".format(encrypted, authenticated, bonded, key_size))
            if conn_handle == state.get("conn_handle"):
                state["encrypted"] = encrypted

        elif event == _IRQ_PASSKEY_ACTION:
            conn_handle, action, passkey = data
            state["passkey_action"] = True
            log_event("[PASSKEY] action={} passkey={}".format(action, passkey))
            if action == _PASSKEY_ACTION_INPUT and PIN:
                log_event("[PASSKEY] gap_passkey({:06d})".format(PIN))
                ble.gap_passkey(conn_handle, action, PIN)
            elif action == _PASSKEY_ACTION_INPUT and not PIN:
                log_event("[PASSKEY] ERROR: action=INPUT but no PIN configured!")

        elif event == _IRQ_GET_SECRET:
            sec_type, index, key = data
            return state["secrets"].get((sec_type, key), None)

        elif event == _IRQ_SET_SECRET:
            sec_type, key, value = data
            state["secrets"][(sec_type, key)] = value
            return True

        elif event == _IRQ_MTU_EXCHANGED:
            conn_handle, mtu = data
            log_event("[MTU] {}".format(mtu))
            state["mtu"] = mtu

        elif event == _IRQ_GATTC_SERVICE_RESULT:
            conn_handle, start, end, uuid = data
            log_event("[SERVICE] {}-{}".format(start, end))
            state["service"] = (start, end)

        elif event == _IRQ_GATTC_SERVICE_DONE:
            conn_handle, status = data
            log_event("[SERVICE_DONE] status={}".format(status))
            if state["service"] and status == 0:
                ble.gattc_discover_characteristics(conn_handle, state["service"][0], state["service"][1])
            elif status != 0:
                log_event("[SERVICE_DONE] ERROR status={}".format(status))

        elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
            conn_handle, end_handle, value_handle, properties, uuid = data
            uuid_obj = bt.UUID(uuid)
            log_event("[CHAR] uuid={} h={} props=0x{:02x}".format(uuid_obj, value_handle, properties))
            rx_uuid = bt.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E")
            tx_uuid = bt.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")
            if uuid_obj == rx_uuid:
                state["rx"] = value_handle
            elif uuid_obj == tx_uuid:
                state["tx"] = value_handle

        elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
            conn_handle, status = data
            log_event("[CHAR_DONE] status={}".format(status))
            if state["tx"] and state["rx"] and status == 0:
                start = state["tx"] + 1
                end = state["service"][1] if state["service"] else 0xFFFF
                ble.gattc_discover_descriptors(conn_handle, start, end)
            elif status != 0:
                log_event("[CHAR_DONE] ERROR status={}".format(status))

        elif event == _IRQ_GATTC_DESCRIPTOR_RESULT:
            conn_handle, dsc_handle, uuid = data
            uuid_obj = bt.UUID(uuid)
            log_event("[DESC] uuid={} h={}".format(uuid_obj, dsc_handle))
            if uuid_obj == bt.UUID(0x2902):
                state["cccd"] = dsc_handle

        elif event == _IRQ_GATTC_DESCRIPTOR_DONE:
            conn_handle, status = data
            log_event("[DESC_DONE] status={}".format(status))
            if state["cccd"] is None:
                state["cccd"] = state["tx"] + 1
                log_event("[DESC] Assuming CCCD={}".format(state["cccd"]))
            if status == 0:
                log_event("[SUBSCRIBE] Writing CCCD h={}".format(state["cccd"]))
                ble.gattc_write(conn_handle, state["cccd"], b"\x01\x00", 1)
            else:
                log_event("[DESC_DONE] ERROR status={}".format(status))

        elif event == _IRQ_GATTC_WRITE_DONE:
            conn_handle, value_handle, status = data
            log_event("[WRITE_DONE] h={} status={}".format(value_handle, status))
            if value_handle == state.get("cccd"):
                if status == 0:
                    log_event("[SUBSCRIBE] SUCCESS")
                    state["subscribed"] = True
                else:
                    log_event("[SUBSCRIBE] FAILED status={}".format(status))

        elif event == _IRQ_GATTC_NOTIFY:
            conn_handle, value_handle, notify_data = data
            log_event("[NOTIFY] {} bytes".format(len(notify_data)))
            state["notified"] = True
    except Exception as e:
        log_event("[IRQ_ERROR] event={} err={}".format(event, e))

ble.irq(irq)

# Scan for RNode
print("\nScanning 10s for RNode...")
found = None

def scan_irq(event, data):
    global found
    if event == 5:  # SCAN_RESULT
        addr_type, addr, adv_type, rssi, adv_data = data
        addr = bytes(addr)
        adv_data = bytes(adv_data)
        name = None
        has_nus = False
        i = 0
        while i + 1 < len(adv_data):
            length = adv_data[i]
            if length == 0 or i + length + 1 > len(adv_data):
                break
            ad_type = adv_data[i + 1]
            field = bytes(adv_data[i + 2 : i + 1 + length])
            if ad_type == 0x09:
                try: name = field.decode("utf-8")
                except: name = field.decode("latin-1")
            elif ad_type == 0x07:
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
            i += length + 1
        if has_nus or (name and name.startswith("RNode ")):
            if found is None:
                found = (addr_type, addr)
                print("FOUND: {} @ {}".format(name, ":".join("%02x" % b for b in addr)))

ble.irq(scan_irq)
ble.gap_scan(10000, 30000, 30000, True)

t0 = time.time()
while time.time() - t0 < 12:
    time.sleep(1)
    if found:
        break

if not found:
    print("RNode not found!")
else:
    ble.irq(irq)
    addr_type, addr = found
    print("\nConnecting to RNode...")
    ble.gap_connect(addr_type, addr)

    print("Waiting up to 45s for full flow...")
    t0 = time.time()
    while time.time() - t0 < 45:
        time.sleep(1)
        if state["subscribed"]:
            print("\n*** SUCCESS: CONNECTED, ENCRYPTED, SUBSCRIBED ***")
            break

    print("\n=== FINAL STATE ===")
    print("Connected:", state["connected"])
    print("Encrypted:", state["encrypted"])
    print("MTU:", state["mtu"])
    print("RX handle:", state["rx"])
    print("TX handle:", state["tx"])
    print("CCCD handle:", state["cccd"])
    print("Subscribed:", state["subscribed"])
    print("Passkey action:", state["passkey_action"])
    print("\nEvent log:")
    for ev in state["events"]:
        print("  ", ev)
