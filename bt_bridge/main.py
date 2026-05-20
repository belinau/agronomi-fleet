import binascii
import gc
import json
import sys
import time

import bluetooth
import machine
import micropython
import uselect
from micropython import const

# Allocate emergency exception buffer for ISR debugging per official docs.
# Per: https://docs.micropython.org/en/latest/library/micropython.html
micropython.alloc_emergency_exception_buf(100)

# BLE IRQ event codes — per official bluetooth module docs:
# https://docs.micropython.org/en/latest/library/bluetooth.html
_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)
_IRQ_MTU_EXCHANGED = const(21)

# OTA protocol constants — must match OTAManager.h on ESP32-C6
OTA_HDR_BEGIN = 0xA0
OTA_HDR_DATA = 0xA1
OTA_HDR_END = 0xA2
OTA_HDR_ABORT = 0xA3

# Target BLE ATT MTU. Per MicroPython docs: "Get/set the MTU that will be
# used during a ATT MTU exchange. The resulting MTU will be the minimum
# of this and the remote device's MTU."
# 247 is a common BLE large-MTU target (244-byte notification payload).
_TARGET_MTU = 247

# --- BLE state ---
ble = bluetooth.BLE()
_conn = None
_rx = None
_tx = None
_mtu = 23  # Current negotiated MTU; updated on _IRQ_MTU_EXCHANGED
_wdt = None  # WDT instance; set in main() after WDT init so _send_ota_frame can feed it

# --- BLE IRQ ring buffer ---
# Per ISR rules docs: "Where an ISR returns multiple bytes use a
# pre-allocated bytearray." The ring buffer uses pre-allocated slots.
# _head is written only by the IRQ handler. _tail is written only by
# the main loop. This makes the ring buffer safe for concurrent access
# without locks — as long as we never append/pop from both contexts.
_SIZE = 32  # Increased for OTA traffic
_buf = [None] * _SIZE
_head = 0
_tail = 0
_write_count = 0


def _print(msg):
    try:
        print(msg)
    except OSError:
        pass


def irq(event, data):
    """BLE IRQ handler — per official docs and ISR rules, keep minimal.

    Per docs: "memoryview fields are only valid during the invocation of the
    IRQ handler." We call gatts_read() here because the data is only valid
    during the IRQ. We store the decoded string in the ring buffer for the
    main loop to process.

    Per ISR rules docs: "ISR code should not wait on an event." gatts_read()
    is a quick in-RAM operation that returns immediately.

    Connect/disconnect events update _conn directly. The main loop reads
    _conn when sending commands. Since _conn is a single integer written
    only by the IRQ and read by the main loop, this is safe — the worst
    case is reading a stale value during a brief transition.
    """
    global _head, _conn, _mtu
    if event == _IRQ_CENTRAL_CONNECT:
        conn_handle, addr_type, addr = data
        _conn = conn_handle
        nxt = (_head + 1) % _SIZE
        if nxt != _tail:
            _buf[_head] = ("__CONNECT__", conn_handle)
            _head = nxt
    elif event == _IRQ_CENTRAL_DISCONNECT:
        conn_handle, addr_type, addr = data
        if conn_handle == _conn:
            _conn = None
            _mtu = 23  # Reset to default on disconnect
        nxt = (_head + 1) % _SIZE
        if nxt != _tail:
            _buf[_head] = ("__DISCONNECT__", conn_handle)
            _head = nxt
        start_adv()
    elif event == _IRQ_GATTS_WRITE:
        conn_handle, value_handle = data
        global _write_count
        _write_count += 1
        nxt = (_head + 1) % _SIZE
        if nxt != _tail:
            try:
                raw = ble.gatts_read(value_handle)
                msg = raw.decode("utf-8")
                _buf[_head] = ("__DATA__", msg)
                _head = nxt
            except Exception as e:
                _buf[_head] = ("__ERR__", str(e))
                _head = nxt
    elif event == _IRQ_MTU_EXCHANGED:
        # Per docs: "_IRQ_MTU_EXCHANGED: ATT MTU exchange complete
        # (either initiated by us or the remote device)."
        # data = (conn_handle, mtu)
        conn_handle, mtu = data
        if conn_handle == _conn:
            _mtu = mtu
            nxt = (_head + 1) % _SIZE
            if nxt != _tail:
                _buf[_head] = ("__MTU__", mtu)
                _head = nxt


def adv():
    p = bytearray()
    p += bytes([2, 0x01, 0x06])
    u = bytes(bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E"))
    p += bytes([17, 0x07]) + u
    return bytes(p)


def resp():
    n = b"GW-MIMI-01"
    return bytes([len(n) + 1, 0x09]) + n


def start_adv():
    ble.gap_advertise(250000, adv_data=adv(), resp_data=resp(), connectable=True)


def process_events():
    """Process all queued BLE events from the ring buffer.

    The ring buffer is safe for concurrent IRQ/main-loop access:
    - _head is written only by the IRQ handler
    - _tail is written only by this function
    - We drain all available events each call.
    """
    global _tail
    while _tail != _head:
        ev = _buf[_tail]
        _tail = (_tail + 1) % _SIZE
        tag, payload = ev

        if tag == "__CONNECT__":
            _print("[C] %d" % payload)
        elif tag == "__DISCONNECT__":
            _print("[D] %d" % payload)
            _print("Advertising")
        elif tag == "__MTU__":
            # Report negotiated MTU on serial so Mimi can use it
            # for chunk sizing. Format matches ble_forwarder.py parser.
            _print("[MTU] %d payload=%d" % (payload, payload - 3))
        elif tag == "__DATA__":
            msg = payload
            # FIX: check for OTA ACK via string probe BEFORE json.loads.
            # If the C6 sends the ACK near the BLE fragmentation boundary
            # (small MTU), json.loads could fail on a partial frame. The
            # string probe is safe even on partial data and ensures Mimi
            # always gets the [ACK] line so ack_waiter.set() fires.
            if "ota_ok" in msg:
                _print("[ACK] " + msg)
            try:
                data = json.loads(msg)
                dev = data.get("dev_id", "?")
                seq = data.get("seq", 0)
                readings = data.get("readings", {})

                _print("[JSON] " + msg)

                if "ack" in data and "ota_ok" not in msg:
                    # Non-OTA ack (command ack) — also forward as [ACK]
                    _print("[ACK] " + msg)

                _print("[W] %s seq=%d" % (dev, seq))
                for k, v in readings.items():
                    _print("  %s=%s" % (k, v))
                if not readings:
                    _print("  (no readings)")
            except Exception as e:
                _print("  ERR: %s" % e)
        elif tag == "__ERR__":
            _print("  IRQ ERR: %s" % payload)


def _send_ota_frame(raw_bytes):
    """Send a raw binary OTA frame to the sensor via BLE NUS notify.

    The frame is sent via gatts_notify on the NUS TX characteristic.
    Per MicroPython docs: "Sends a notification request to a connected
    client." The ESP32-C6 subscribed to NUS TX notifications during
    Phase 1 connection (BLEManager.cpp _pTxChar->subscribe(true, notifyCB)).

    The notify callback on the C6 (notifyCB) checks the first byte
    for OTA header bytes (0xA0-0xA3) and dispatches to handleNusNotify().

    FIX: wdt.feed() is called before gatts_notify. OTA transfers take
    tens of seconds; if the BLE stack stalls briefly the WDT fires and
    reboots the Pico mid-transfer. Feeding here keeps the WDT satisfied
    for the full duration of the OTA transfer.

    Args:
        raw_bytes: Raw binary OTA frame (header byte + payload)
        wdt: machine.WDT instance or None
    """
    if _conn is None:
        _print("[OTA NOP] No connection, dropping %d bytes" % len(raw_bytes))
        return
    try:
        # FIX: feed WDT before each notify. Module-level _wdt is set in main()
        # after WDT init. This prevents a mid-OTA WDT reboot if gatts_notify
        # stalls briefly under BLE congestion.
        if _wdt is not None:
            _wdt.feed()
        ble.gatts_notify(_conn, _tx, raw_bytes)
    except Exception as e:
        _print("[OTA ERR] gatts_notify failed: %s" % e)


def main():
    global _conn, _rx, _tx, _mtu

    _print("[SER] Pico boot: %s" % machine.unique_id().hex())
    _print("[SER] MicroPython %s" % sys.version)
    _print("[SER] Freq=%dMHz" % (machine.freq() // 1_000_000))
    _print("[SER] Step 1: imports OK")

    # BLE init — all synchronous, per official example pattern.
    # ble.active(True) must come before ble.config(mtu=) on MicroPython/
    # BTstack — the BLE device does not exist until active(True) and
    # config() raises OSError ENODEV if called before it.
    _print("[SER] Step 2: ble.active(True)...")
    ble.active(True)
    _print("[SER] Step 2: ble.active OK")

    _print("[SER] Step 3: ble.irq()...")
    ble.irq(irq)
    _print("[SER] Step 3: ble.irq OK")

    _print("[SER] Step 3b: ble.config(gap_name=...)...")
    ble.config(gap_name="GW-MIMI-01")
    _print("[SER] Step 3b: gap_name OK")

    _print("[SER] Step 4: gatts_register_services()...")
    UART_UUID = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
    UART_TX = (
        bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E"),
        bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY,
    )
    UART_RX = (
        bluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E"),
        bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE,
    )
    UART_SERVICE = (UART_UUID, (UART_TX, UART_RX))
    handles = ble.gatts_register_services((UART_SERVICE,))

    _rx, _tx = handles[0][0], handles[0][1]
    _print("[SER] Step 4: gatts_register OK rx=%d tx=%d" % (_rx, _tx))

    # Per docs: "useful when implementing something like the Nordic UART Service"
    # Buffer size increased to match MTU target — allows gatts_notify to
    # send up to (MTU - 3) bytes in a single notification.
    ble.gatts_set_buffer(_rx, 512, True)
    ble.gatts_set_buffer(_tx, _TARGET_MTU, False)
    ble.gatts_write(_tx, b"\x00")
    _print("[SER] Step 5: buffers set OK (tx_buf=%d)" % _TARGET_MTU)

    # Watchdog — per docs: max timeout on RP2 is 8388ms, cannot be stopped.
    global _wdt
    wdt = None
    try:
        wdt = machine.WDT(timeout=8000)
        _wdt = wdt  # make accessible to _send_ota_frame
        _print("[SER] WDT started, timeout=8000ms")
    except Exception as e:
        _print("[SER] WDT unavailable: %s (continuing without)" % e)

    start_adv()
    _print("[SER] Step 6: Advertising")
    _print("[SER] Ready for commands")

    # --- Main loop: synchronous, per official BLE UART example pattern ---
    poller = uselect.poll()
    poller.register(sys.stdin, uselect.POLLIN)

    hb_count = 0
    last_hb = time.ticks_ms()
    last_gc = time.ticks_ms()
    empty_reads = 0
    EMPTY_READ_LIMIT = 10

    def _reregister_stdin():
        """Re-register stdin with the poller after USB reconnect."""
        try:
            poller.unregister(sys.stdin)
        except (OSError, ValueError):
            pass
        try:
            poller.register(sys.stdin, uselect.POLLIN)
        except OSError as e:
            _print("[SER] stdin re-register failed: %s" % e)

    while True:
        try:
            # Feed watchdog
            if wdt is not None:
                wdt.feed()

            # Process BLE events from ring buffer
            process_events()

            # Poll stdin for commands from Mimi (100ms timeout)
            try:
                polled = poller.ipoll(100)
            except OSError:
                _print("[SER] poll failed, re-registering stdin")
                _reregister_stdin()
                time.sleep_ms(50)
                continue

            for s, ev in polled:
                try:
                    if ev & uselect.POLLIN:
                        try:
                            line = s.readline()
                        except (OSError, ValueError):
                            _print("[SER] readline failed, re-registering stdin")
                            _reregister_stdin()
                            break

                        if line:
                            empty_reads = 0
                            line = line.strip()
                            # --- [CMD] JSON commands (existing) ---
                            if line.startswith(b"[CMD] "):
                                try:
                                    cmd_json = line[6:].decode("utf-8")
                                    json.loads(cmd_json)  # validate
                                    cmd_bytes = cmd_json.encode("utf-8")
                                    if _conn is not None:
                                        ble.gatts_notify(_conn, _tx, cmd_bytes)
                                        _print("[SENT] " + cmd_json)
                                    else:
                                        _print(
                                            "[NOP] No connection, dropping: " + cmd_json
                                        )
                                except Exception as e:
                                    _print("[CMD ERR] %s" % e)
                            # --- [OTA_BEGIN] Start BLE OTA (new) ---
                            elif line.startswith(b"[OTA_BEGIN]"):
                                try:
                                    b64 = line[len(b"[OTA_BEGIN]") :]
                                    raw = binascii.a2b_base64(b64)
                                    _send_ota_frame(raw)
                                    _print("[OTA] BEGIN sent (%d bytes raw)" % len(raw))
                                except Exception as e:
                                    _print("[OTA ERR] BEGIN decode: %s" % e)
                            # --- [OTA_DATA] Chunk frame (new) ---
                            elif line.startswith(b"[OTA_DATA]"):
                                try:
                                    b64 = line[len(b"[OTA_DATA]") :]
                                    raw = binascii.a2b_base64(b64)
                                    _send_ota_frame(raw)
                                except Exception as e:
                                    _print("[OTA ERR] DATA decode: %s" % e)
                            # --- [OTA_END] Finish OTA (new) ---
                            elif line.startswith(b"[OTA_END]"):
                                try:
                                    b64 = line[len(b"[OTA_END]") :]
                                    raw = binascii.a2b_base64(b64)
                                    _send_ota_frame(raw)
                                    _print("[OTA] END sent (%d bytes raw)" % len(raw))
                                except Exception as e:
                                    _print("[OTA ERR] END decode: %s" % e)
                            # --- [OTA_ABORT] Abort OTA (new) ---
                            elif line.startswith(b"[OTA_ABORT]"):
                                try:
                                    b64 = line[len(b"[OTA_ABORT]") :]
                                    raw = binascii.a2b_base64(b64)
                                    _send_ota_frame(raw)
                                    _print("[OTA] ABORT sent")
                                except Exception as e:
                                    _print("[OTA ERR] ABORT decode: %s" % e)
                        else:
                            # Empty bytes — stdin may be disconnected;
                            # re-register after repeated empty reads.
                            empty_reads += 1
                            if empty_reads >= EMPTY_READ_LIMIT:
                                _print(
                                    "[SER] stdin empty %d times, re-registering"
                                    % empty_reads
                                )
                                _reregister_stdin()
                                empty_reads = 0
                except OSError:
                    _print("[SER] stdin ev error, re-registering")
                    _reregister_stdin()
                    break

            # Periodic heartbeat (every 30s)
            now = time.ticks_ms()
            if time.ticks_diff(now, last_hb) >= 30000:
                last_hb = now
                hb_count += 1
                _print(
                    "[HB] %d conn=%s mtu=%d wr=%d"
                    % (
                        hb_count,
                        "yes" if _conn is not None else "no",
                        _mtu,
                        _write_count,
                    )
                )

            # Periodic GC (every 60s)
            if time.ticks_diff(now, last_gc) >= 60000:
                last_gc = now
                gc.collect()
                _print("[GC] free=%d alloc=%d" % (gc.mem_free(), gc.mem_alloc()))

        except Exception as e:
            # Last-resort: if ANY unhandled exception occurs in the main
            # loop, try to recover instead of falling to REPL.
            _print("[SER] main loop error: %s" % e)
            try:
                _reregister_stdin()
            except Exception:
                pass
            time.sleep_ms(500)


main()
