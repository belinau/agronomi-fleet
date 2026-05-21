
import time
import ubluetooth as bt

print("=== RNode Pairing Test ===")

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

ble = bt.BLE()
ble.active(True)
ble.config(mtu=247)
ble.config(bond=True)
ble.config(mitm=True)
ble.config(io=2)
try:
    ble.config(le_secure=True)
except:
    pass

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
}

def irq(event, data):
    if event == _IRQ_PERIPHERAL_CONNECT:
        conn_handle, addr_type, addr = data
        print("[CONNECT] handle={}, addr={}".format(conn_handle, ":".join("%02x" % b for b in addr)))
        state["connected"] = True
        state["conn_handle"] = conn_handle
        # Pair immediately
        pin = 0  # SET YOUR PIN HERE OR 0 TO TEST WITHOUT
        if pin:
            print("[PAIR] Initiating with PIN {:06d}".format(pin))
            ble.gap_pair(conn_handle)
        else:
            print("[PAIR] No PIN configured, skipping gap_pair")

    elif event == _IRQ_PERIPHERAL_DISCONNECT:
        conn_handle, addr_type, addr = data
        print("[DISCONNECT] handle={}".format(conn_handle))
        state["connected"] = False
        state["encrypted"] = False

    elif event == _IRQ_ENCRYPTION_UPDATE:
        conn_handle, encrypted, authenticated, bonded, key_size = data
        print("[ENCRYPT] enc={} auth={} bond={} key={}".format(encrypted, authenticated, bonded, key_size))
        if conn_handle == state.get("conn_handle"):
            state["encrypted"] = encrypted

    elif event == _IRQ_PASSKEY_ACTION:
        conn_handle, action, passkey = data
        print("[PASSKEY] action={} passkey={}".format(action, passkey))
        state["passkey_action"] = True
        if action == _PASSKEY_ACTION_INPUT:
            pin = 0  # SET YOUR PIN HERE
            if pin:
                print("[PASSKEY] Entering PIN {:06d}".format(pin))
                ble.gap_passkey(conn_handle, action, pin)
            else:
                print("[PASSKEY] No PIN configured!")

    elif event == _IRQ_GET_SECRET:
        sec_type, index, key = data
        return state["secrets"].get((sec_type, key), None)

    elif event == _IRQ_SET_SECRET:
        sec_type, key, value = data
        state["secrets"][(sec_type, key)] = value
        return True

    elif event == _IRQ_MTU_EXCHANGED:
        conn_handle, mtu = data
        print("[MTU] {}".format(mtu))
        state["mtu"] = mtu

    elif event == _IRQ_GATTC_SERVICE_RESULT:
        conn_handle, start, end, uuid = data
        print("[SERVICE] {}-{}".format(start, end))
        state["service"] = (start, end)

    elif event == _IRQ_GATTC_SERVICE_DONE:
        conn_handle, status = data
        print("[SERVICE_DONE] status={}".format(status))
        if state["service"]:
            ble.gattc_discover_characteristics(conn_handle, state["service"][0], state["service"][1])

    elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
        conn_handle, end_handle, value_handle, properties, uuid = data
        uuid_obj = bt.UUID(uuid)
        print("[CHAR] uuid={} h={} props=0x{:02x}".format(uuid_obj, value_handle, properties))
        rx_uuid = bt.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E")
        tx_uuid = bt.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")
        if uuid_obj == rx_uuid:
            state["rx"] = value_handle
        elif uuid_obj == tx_uuid:
            state["tx"] = value_handle

    elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
        conn_handle, status = data
        print("[CHAR_DONE] status={}".format(status))
        if state["tx"] and state["rx"]:
            start = state["tx"] + 1
            end = state["service"][1] if state["service"] else 0xFFFF
            ble.gattc_discover_descriptors(conn_handle, start, end)
        else:
            print("[ERROR] Missing TX or RX char")

    elif event == _IRQ_GATTC_DESCRIPTOR_RESULT:
        conn_handle, dsc_handle, uuid = data
        uuid_obj = bt.UUID(uuid)
        print("[DESC] uuid={} h={}".format(uuid_obj, dsc_handle))
        if uuid_obj == bt.UUID(0x2902):
            state["cccd"] = dsc_handle

    elif event == _IRQ_GATTC_DESCRIPTOR_DONE:
        conn_handle, status = data
        print("[DESC_DONE] status={}".format(status))
        if state["cccd"] is None:
            state["cccd"] = state["tx"] + 1
            print("[DESC] Assuming CCCD={}".format(state["cccd"]))
        # Write CCCD to enable notifications
        print("[SUBSCRIBE] Writing CCCD...")
        ble.gattc_write(conn_handle, state["cccd"], b"\x01\x00", 1)

    elif event == _IRQ_GATTC_WRITE_DONE:
        conn_handle, value_handle, status = data
        print("[WRITE_DONE] h={} status={}".format(value_handle, status))
        if value_handle == state.get("cccd"):
            if status == 0:
                print("[SUBSCRIBE] SUCCESS")
                state["subscribed"] = True
            else:
                print("[SUBSCRIBE] FAILED status={}".format(status))

    elif event == _IRQ_GATTC_NOTIFY:
        conn_handle, value_handle, notify_data = data
        print("[NOTIFY] {} bytes".format(len(notify_data)))
        state["notified"] = True

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
    # Restore main IRQ
    ble.irq(irq)
    addr_type, addr = found
    print("\nConnecting...")
    ble.gap_connect(addr_type, addr)

    # Wait up to 30s for full flow
    print("Waiting 30s for connection + pairing + subscribe...")
    t0 = time.time()
    while time.time() - t0 < 30:
        time.sleep(1)
        print("  state: conn={} enc={} mtu={} rx={} tx={} cccd={} sub={} notify={}".format(
            state["connected"], state["encrypted"], state["mtu"],
            state["rx"], state["tx"], state["cccd"], state["subscribed"], state["notified"]
        ))
        if state["subscribed"]:
            print("\n*** FULLY CONNECTED AND SUBSCRIBED ***")
            break

print("\nFinal state:", state)
