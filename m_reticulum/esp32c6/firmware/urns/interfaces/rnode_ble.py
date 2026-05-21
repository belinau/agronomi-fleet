# µReticulum RNode BLE Interface - Streamlined & Genuine
# KISS-over-Nordic-UART-Service BLE client for ESP32-C6

import gc
import struct
import time

import ubluetooth as bt

from ..log import LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_VERBOSE, LOG_WARNING, log
from . import Interface

_IRQ_SCAN_RESULT, _IRQ_SCAN_DONE = 5, 6
_IRQ_PERIPHERAL_CONNECT, _IRQ_PERIPHERAL_DISCONNECT = 7, 8
_IRQ_GATTC_SERVICE_RESULT, _IRQ_GATTC_SERVICE_DONE = 9, 10
_IRQ_GATTC_CHARACTERISTIC_RESULT, _IRQ_GATTC_CHARACTERISTIC_DONE = 11, 12
_IRQ_GATTC_DESCRIPTOR_RESULT, _IRQ_GATTC_DESCRIPTOR_DONE = 13, 14
_IRQ_GATTC_WRITE_DONE, _IRQ_GATTC_NOTIFY = 17, 18
_IRQ_MTU_EXCHANGED, _IRQ_ENCRYPTION_UPDATE = 21, 28
_IRQ_GET_SECRET, _IRQ_SET_SECRET, _IRQ_PASSKEY_ACTION = 29, 30, 31

_PASSKEY_ACTION_INPUT = 2
_IO_CAPABILITY_DISPLAY_ONLY = 0
_IO_CAPABILITY_DISPLAY_YESNO = 1
_IO_CAPABILITY_KEYBOARD_ONLY = 2
_IO_CAPABILITY_NO_INPUT_OUTPUT = 3
_IO_CAPABILITY_KEYBOARD_DISPLAY = 4

UART_SERVICE_UUID = bt.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
UART_RX_CHAR_UUID = bt.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E")
UART_TX_CHAR_UUID = bt.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")
_UART_SERVICE_UUID_STR = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_DATA, CMD_FREQUENCY, CMD_BANDWIDTH, CMD_TXPOWER, CMD_SF, CMD_CR, CMD_RADIO_STATE = (
    0,
    1,
    2,
    3,
    4,
    5,
    6,
)
(
    CMD_DETECT,
    CMD_READY,
    CMD_STAT_RSSI,
    CMD_STAT_SNR,
    CMD_STAT_BAT,
    CMD_PLATFORM,
    CMD_MCU,
    CMD_FW_VERSION,
) = 8, 15, 35, 36, 39, 72, 73, 80
RADIO_STATE_OFF, RADIO_STATE_ON, DETECT_REQ, DETECT_ACK = 0, 1, 0x73, 0x46


def diag_print(msg):
    print("[RNode BLE] " + str(msg))


def _kiss_escape(data):
    out = bytearray()
    for b in data:
        if b == 0xDB:
            out.extend(b"\xdb\xdd")
        elif b == 0xC0:
            out.extend(b"\xdb\xdc")
        else:
            out.append(b)
    return bytes(out)


class RNodeBLEInterface(Interface):
    MAX_REASSEMBLY = 4096

    def __init__(self, config):
        super().__init__(config.get("name", "RNode BLE"))
        self.frequency = config.get("frequency", 868000000)
        self.bandwidth = config.get("bandwidth", 125000)
        self.txpower = config.get("txpower", 17)
        self.spreadingfactor = config.get("spreadingfactor", 11)
        self.codingrate = config.get("codingrate", 5)
        self.target_name = config.get("target_name", "")
        self.target_address = config.get("target_address", None)
        self.scan_timeout = config.get("scan_timeout", 10)
        self.reconnect_delay = config.get("reconnect_delay", 5)
        self.pairing_passkey = 0
        self._secrets = {}
        self._secrets_dirty = False
        self._bonded_dirty = False
        self._shutting_down = False
        self._already_bonded = False

        # Check if successfully bonded from a previous run
        try:
            with open("bonded.txt", "r") as f:
                if f.read().strip() == "1":
                    self._already_bonded = True
                    diag_print(
                        "Device is already bonded. Skipping manual pairing on connection."
                    )
        except Exception:
            pass

        # Load PIN from ble_pin.txt
        try:
            with open("ble_pin.txt", "r") as f:
                self.pairing_passkey = int(f.read().strip())
            diag_print("Loaded fresh PIN {:06d}".format(self.pairing_passkey))
        except Exception as e:
            diag_print("No PIN loaded: " + str(e))

        # Check if the Mac pairing script is forcing a fresh pairing session
        force_pair = False
        try:
            with open("force_pair.txt", "r") as f:
                if f.read().strip() == "1":
                    force_pair = True
        except Exception:
            pass

        if force_pair:
            diag_print(
                "Fresh pairing session forced by Mac script. Clearing stale bonds."
            )
            try:
                import os

                os.remove("ble_bond.json")
                os.remove("bonded.txt")
                os.remove("force_pair.txt")
                # Force instant sync of file deletions to SPI flash
                os.sync()
                self._already_bonded = False
            except Exception:
                pass

        # Load or generate a persistent local MAC address.
        # This prevents the C6 from randomizing its MAC every boot, allowing the RNode to reuse bonds.
        temp_mac = None
        try:
            with open("ble_mac.txt", "r") as f:
                temp_mac = bytes.fromhex(f.read().strip())
            diag_print(
                "Loaded persistent MAC: " + ":".join("%02x" % b for b in temp_mac)
            )
        except Exception:
            try:
                import os
                import random

                import network

                wlan = network.WLAN(network.STA_IF)
                wlan.active(True)
                hw_mac = wlan.config("mac")
                # Create a locally-administered MAC using a variation of the hardware MAC
                # First byte must be 0x02 to satisfy driver constraints.
                temp_mac = bytearray(hw_mac)
                temp_mac[0] = 0x02
                with open("ble_mac.txt", "w") as f:
                    f.write(temp_mac.hex())
                # Sync new MAC file directly to SPI flash
                os.sync()
                diag_print(
                    "Generated persistent MAC: "
                    + ":".join("%02x" % b for b in temp_mac)
                )
            except Exception as e:
                diag_print("Failed to generate persistent MAC: " + str(e))

        if temp_mac:
            try:
                import network

                wlan = network.WLAN(network.STA_IF)
                wlan.active(True)
                wlan.config(mac=temp_mac)
            except Exception as e:
                diag_print("MAC override failed: " + str(e))

        try:
            self._ble = bt.BLE()

            # Always activate the BLE controller BEFORE configuring parameters.
            self._ble.active(True)

            # Enforce Legacy MITM Bonding parameters globally.
            self._ble.config(bond=True)
            if self.pairing_passkey:
                diag_print(
                    "Configuring security: mitm=True, keyboard_only, le_secure=False"
                )
                self._ble.config(
                    mitm=True, io=_IO_CAPABILITY_KEYBOARD_ONLY, le_secure=False
                )
            else:
                self._ble.config(mitm=False, io=_IO_CAPABILITY_NO_INPUT_OUTPUT)

            self._ble.config(mtu=247)
            self._ble.irq(self._irq)
            diag_print("BLE controller activated successfully")
        except Exception as e:
            diag_print("Init failed: " + str(e))
            self._ble = None
            return

        self._conn_handle = self._rx_char_handle = self._tx_char_handle = (
            self._service_handle
        ) = self._cccd_handle = None
        self._scanning = self._connecting = self._discovering = self._subscribing = (
            False
        )
        self._service_discovered = self._char_discovered = self._desc_discovered = (
            self._notify_enabled
        ) = self._mtu_exchanged = self._encrypted = self._pairing_attempted = (
            self._pending_pair
        ) = False
        self._kiss_buf = bytearray()
        self._in_frame = self._escaping = False
        self._current_cmd = None
        self._detected = self._radio_configured = False
        self._detect_retries = 0
        self._write_queue = []
        self._reconnect_count = 0
        self._last_reconnect = 0
        self._detect_sent_time = 0
        self._pairing_start_time = 0
        self._negotiated_mtu = 23

        self._load_secrets()
        self._start_scan()

    def _load_secrets(self):
        try:
            import json

            with open("ble_bond.json", "r") as f:
                raw = json.load(f)
            self._secrets = {}
            for k, v in raw.items():
                parts = k.split(",")
                # Strictly restore both key and value as plain bytes objects
                self._secrets[(int(parts[0]), bytes.fromhex(parts[1]))] = bytes.fromhex(
                    v
                )
        except Exception:
            pass

    def _save_secrets(self):
        try:
            import json

            raw = {}
            for k, v in self._secrets.items():
                raw["{},{}".format(k[0], k[1].hex())] = v.hex()
            with open("ble_bond.json", "w") as f:
                json.dump(raw, f)
            # Sync keys instantly to SPI flash to prevent data loss on power pulls
            import os

            os.sync()
        except Exception:
            pass

    def _on_get_secret(self, data):
        # Unpack parameters: sec_type, index, key
        sec_type, index, key = data[0], data[1], data[2]
        if key is None:
            # Index-based query: return the index'th value (bytes) for this sec_type, or None if not found
            i = 0
            for (s, k), v in self._secrets.items():
                if s == sec_type:
                    if i == index:
                        return v
                    i += 1
            return None
        else:
            # Direct key query: key is passed as a memoryview, cast to bytes for dict lookup
            key_bytes = bytes(key)
            return self._secrets.get((sec_type, key_bytes), None)

    def _on_set_secret(self, data):
        sec_type, key, value = data[0], data[1], data[2]
        key_bytes = bytes(key)
        value_bytes = bytes(value) if value is not None else None

        if value_bytes is None:
            if (sec_type, key_bytes) in self._secrets:
                del self._secrets[(sec_type, key_bytes)]
                self._secrets_dirty = True
        else:
            self._secrets[(sec_type, key_bytes)] = value_bytes
            self._secrets_dirty = True
        return True

    def _start_scan(self):
        if not self._ble or self._shutting_down:
            return
        self._target_addr_found = self._scan_result = None
        self._scanning, self._scan_start_time = True, time.time()
        try:
            self._ble.gap_scan(self.scan_timeout * 1000, 100000, 30000, True)
            diag_print("Scanning...")
        except Exception as e:
            diag_print("Scan failed: " + str(e))
            self._scanning = False

    def _stop_scan(self):
        if self._scanning and self._ble:
            try:
                self._ble.gap_scan(None)
            except Exception:
                pass
            self._scanning = False

    def _decode_adv_data(self, adv_data):
        services, name, i = [], None, 0
        while i + 1 < len(adv_data):
            length = adv_data[i]
            if length == 0 or i + length + 1 > len(adv_data):
                break
            ad_type, field = adv_data[i + 1], bytes(adv_data[i + 2 : i + 1 + length])
            if ad_type in (0x06, 0x07):
                for j in range(0, len(field), 16):
                    if j + 16 <= len(field):
                        b = bytes(reversed(field[j : j + 16]))
                        services.append(
                            "{:08x}-{:04x}-{:04x}-{:04x}-{:012x}".format(
                                int.from_bytes(b[0:4], "big"),
                                int.from_bytes(b[4:6], "big"),
                                int.from_bytes(b[6:8], "big"),
                                int.from_bytes(b[8:10], "big"),
                                int.from_bytes(b[10:16], "big"),
                            )
                        )
            elif ad_type in (0x08, 0x09) and name is None:
                try:
                    name = field.decode("utf-8")
                except Exception:
                    name = field.decode("latin-1")
            i += length + 1
        return services, name

    def _match_scan_result(self, addr_type, addr, adv_data):
        addr_str = ":".join("%02x" % b for b in addr)
        if self.target_address:
            return addr_str.lower() == self.target_address.lower()
        services, name = self._decode_adv_data(adv_data)
        has_nus = _UART_SERVICE_UUID_STR in [s.lower() for s in services]
        if self.target_name:
            return has_nus and (
                name == self.target_name or (name and name.startswith(self.target_name))
            )
        return has_nus or (name and name.startswith("RNode"))

    def _on_connect(self, data):
        self._conn_handle, self._connecting = data[0], False
        self._service_discovered = self._char_discovered = self._desc_discovered = (
            self._notify_enabled
        ) = self._mtu_exchanged = self._encrypted = self._pairing_attempted = False
        self._pending_pair = bool(self.pairing_passkey and not self._already_bonded)
        self._rx_char_handle = self._tx_char_handle = self._service_handle = (
            self._cccd_handle
        ) = None
        self._pairing_start_time = 0
        diag_print("Connected! Handle={}".format(self._conn_handle))

    def _on_disconnect(self, data):
        diag_print("Disconnected!")
        self._last_reconnect = time.time() - (
            self.reconnect_delay
            if self._encrypted or self._already_bonded or len(self._secrets) > 0
            else 0
        )
        self._conn_handle = self._rx_char_handle = self._tx_char_handle = (
            self._service_handle
        ) = self._cccd_handle = None
        self._connecting = self._discovering = self._subscribing = self.online = (
            self._detected
        ) = self._radio_configured = False
        self._mtu_exchanged = self._encrypted = self._pairing_attempted = False
        self._service_discovered = self._char_discovered = self._desc_discovered = (
            self._notify_enabled
        ) = False
        self._detect_retries = 0
        self._detect_sent_time = 0
        self._kiss_buf = bytearray()
        self._write_queue = []

    def _on_passkey_action(self, action, passkey):
        if action == _PASSKEY_ACTION_INPUT:
            diag_print(
                "Submitting RNode static PIN: {:06d}".format(self.pairing_passkey)
            )
            self._ble.gap_passkey(self._conn_handle, action, self.pairing_passkey)
        elif action == _PASSKEY_ACTION_NUMERIC_COMPARISON:
            self._ble.gap_passkey(self._conn_handle, action, 1)

    def _on_service_result(self, data):
        self._service_handle = (data[1], data[2])

    def _on_service_done(self, data):
        self._discovering = False
        if self._service_handle is None:
            diag_print("NUS Service not found!")
            self._disconnect()
            return
        self._discovering = True
        try:
            self._ble.gattc_discover_characteristics(
                self._conn_handle, self._service_handle[0], self._service_handle[1]
            )
        except Exception as e:
            diag_print("GATT char discovery failed: " + str(e))
            self._disconnect()

    def _on_char_result(self, data):
        try:
            uuid = bt.UUID(data[4])
        except Exception:
            uuid = None
        if uuid == UART_RX_CHAR_UUID:
            self._rx_char_handle = data[2]
        elif uuid == UART_TX_CHAR_UUID:
            self._tx_char_handle = data[2]

    def _on_char_done(self, data):
        self._discovering = False
        if not self._tx_char_handle or not self._rx_char_handle:
            diag_print("NUS characteristics not found!")
            self._disconnect()
            return
        self._char_discovered = True
        self._discovering = True
        try:
            self._ble.gattc_discover_descriptors(
                self._conn_handle,
                self._tx_char_handle + 1,
                self._service_handle[1] if self._service_handle else 0xFFFF,
            )
        except Exception as e:
            diag_print("GATT descriptor discovery failed: " + str(e))
            self._disconnect()

    def _on_descriptor_result(self, data):
        try:
            uuid = bt.UUID(data[2])
        except Exception:
            uuid = None
        if uuid == bt.UUID(0x2902):
            self._cccd_handle = data[1]

    def _on_descriptor_done(self, data):
        self._discovering = False
        self._desc_discovered = True
        if self._cccd_handle is None:
            self._cccd_handle = self._tx_char_handle + 1

    def _subscribe_notifications(self):
        if self._conn_handle is None or self._cccd_handle is None:
            return
        try:
            self._ble.gattc_write(self._conn_handle, self._cccd_handle, b"\x01\x00", 1)
            self._subscribing, self._subscribe_start_time = True, time.time()
        except Exception:
            self._disconnect()

    def _on_write_done(self, data):
        if data[0] != self._conn_handle:
            return
        if data[1] == self._cccd_handle and self._subscribing:
            self._subscribing = False
            if data[2] == 0:
                diag_print("Subscribed!")
                self._notify_enabled = True
            else:
                self._disconnect()

    def _send_detection(self):
        self._detect_retries += 1
        self._ble_write_raw(
            bytes(
                [
                    FEND,
                    CMD_DETECT,
                    DETECT_REQ,
                    FEND,
                    CMD_FW_VERSION,
                    0,
                    FEND,
                    CMD_PLATFORM,
                    0,
                    FEND,
                    CMD_MCU,
                    0,
                    FEND,
                ]
            )
        )

    def _send_radio_config(self):
        freq, bw = struct.pack(">I", self.frequency), struct.pack(">I", self.bandwidth)
        cfg = bytearray()
        cfg.extend(bytes([FEND, CMD_FREQUENCY]) + _kiss_escape(freq) + bytes([FEND]))
        cfg.extend(bytes([FEND, CMD_BANDWIDTH]) + _kiss_escape(bw) + bytes([FEND]))
        cfg.extend(
            bytes([FEND, CMD_TXPOWER])
            + _kiss_escape(bytes([self.txpower]))
            + bytes([FEND])
        )
        cfg.extend(
            bytes([FEND, CMD_SF])
            + _kiss_escape(bytes([self.spreadingfactor]))
            + bytes([FEND])
        )
        cfg.extend(
            bytes([FEND, CMD_CR])
            + _kiss_escape(bytes([self.codingrate]))
            + bytes([FEND])
        )
        cfg.extend(bytes([FEND, CMD_RADIO_STATE, RADIO_STATE_ON, FEND]))
        self._ble_write_raw(bytes(cfg))

    def _on_notify(self, data):
        if data[1] == self._tx_char_handle and data[2]:
            self._kiss_feed(bytes(data[2]))

    def _kiss_feed(self, data):
        for b in data:
            if b == FEND:
                if self._in_frame and len(self._kiss_buf) > 0:
                    self._kiss_dispatch(self._current_cmd, bytes(self._kiss_buf))
                self._in_frame = self._escaping = False
                self._kiss_buf = bytearray()
                self._current_cmd = None
                continue
            if not self._in_frame:
                self._in_frame, self._current_cmd, self._kiss_buf, self._escaping = (
                    True,
                    b,
                    bytearray(),
                    False,
                )
                continue
            if b == FESC:
                self._escaping = True
                continue
            if self._escaping:
                self._kiss_buf.append(0xC0 if b == TFEND else 0xDB if b == TFESC else b)
                self._escaping = False
                continue
            self._kiss_buf.append(b)

    def _kiss_dispatch(self, cmd, data):
        if cmd == CMD_DATA and data:
            self.process_incoming(data)
        elif cmd == CMD_DETECT and data and data[0] == DETECT_ACK:
            self._detected = True
            diag_print("RNode Handshake ACK!")
            self._send_radio_config()
        elif cmd == CMD_READY or (
            cmd == CMD_RADIO_STATE and data and data[0] == RADIO_STATE_ON
        ):
            self._radio_configured = self.online = True
            diag_print("Interface is ONLINE!")
        elif cmd == CMD_FW_VERSION and len(data) >= 2:
            diag_print("RNode FW Version {}.{}".format(data[0], data[1]))

    def process_outgoing(self, data):
        if not self.online or self._conn_handle is None or self._rx_char_handle is None:
            return False
        try:
            self._write_queue.append(
                bytes([FEND, CMD_DATA]) + _kiss_escape(data) + bytes([FEND])
            )
            self.txb, self.tx, self._last_activity = (
                self.txb + len(data),
                self.tx + 1,
                time.time(),
            )
            return True
        except Exception:
            return False

    def _ble_write_raw(self, data):
        if self._conn_handle is None or self._rx_char_handle is None:
            return
        mtu, offset = max(20, self._negotiated_mtu - 3), 0
        while offset < len(data):
            chunk = data[offset : offset + mtu]
            try:
                self._ble.gattc_write(self._conn_handle, self._rx_char_handle, chunk, 0)
            except Exception:
                return
            offset += len(chunk)

    def _ble_write_kiss(self, frame):
        self._ble_write_raw(frame)

    def _disconnect(self):
        if self._conn_handle is not None and self._ble:
            try:
                self._ble.gap_disconnect(self._conn_handle)
            except Exception:
                pass

    async def _proactive_pin_injection(self):
        import uasyncio as asyncio

        # Wait 1.5 seconds for GATT / SMP exchange to transition to Passkey Entry state over-the-air
        await asyncio.sleep(1.5)
        if self._conn_handle is not None and not self._encrypted:
            diag_print(
                "Proactively injecting RNode static PIN {:06d} (Bypassing Central IRQ bug)...".format(
                    self.pairing_passkey
                )
            )
            try:
                # 2 = _PASSKEY_ACTION_INPUT
                self._ble.gap_passkey(self._conn_handle, 2, self.pairing_passkey)
            except Exception as e:
                diag_print("Proactive PIN injection failed: " + str(e))

    async def poll_loop(self):
        import uasyncio as asyncio

        diag_print("Active poll loop started.")
        _last_gc = time.time()
        _write_yield = 0

        while self.enabled and not self._shutting_down:
            try:
                now = time.time()
                if now - _last_gc >= 10:
                    gc.collect()
                    _last_gc = now

                if self._secrets_dirty:
                    self._secrets_dirty = False
                    self._save_secrets()

                # Safely write bonding status to flash in main thread (Not in IRQ context)
                if self._bonded_dirty:
                    self._bonded_dirty = False
                    try:
                        with open("bonded.txt", "w") as f:
                            f.write("1")
                        # Force instant sync of bond flag to physical flash
                        import os

                        os.sync()
                        diag_print("Bonding status saved to bonded.txt")
                    except Exception as e:
                        diag_print("Failed to save bonding status: " + str(e))

                # --- 1. Connection Negotiation State ---
                if self._conn_handle is None:
                    if not self._scanning and not self._connecting:
                        if (
                            self._last_reconnect > 0
                            and (now - self._last_reconnect) < self.reconnect_delay
                        ):
                            await asyncio.sleep(0.1)
                            continue
                        if self._target_addr_found is not None:
                            addr_type, addr = self._target_addr_found
                            diag_print("Connecting to matched RNode...")
                            try:
                                self._connecting = True
                                self._ble.gap_connect(addr_type, addr)
                            except Exception as e:
                                diag_print("Connect failed: " + str(e))
                                self._connecting = False
                                self._last_reconnect = now
                            continue
                        self._start_scan()
                        continue

                    if self._scanning:
                        if (
                            self._scan_result is False
                            or (now - self._scan_start_time) >= self.scan_timeout
                        ):
                            self._stop_scan()
                            if self._target_addr_found is None:
                                self._last_reconnect = now
                            continue
                        await asyncio.sleep(0.1)
                        continue

                    if self._connecting:
                        await asyncio.sleep(0.1)
                        continue

                # --- 2. Stabilized Connection State (Active Handle) ---

                # Step A: Perform MTU Exchange first
                if not self._mtu_exchanged:
                    try:
                        diag_print("Initiating MTU Exchange...")
                        self._ble.gattc_exchange_mtu(self._conn_handle)
                    except Exception as e:
                        diag_print("MTU Exchange failed: " + str(e))
                    for _ in range(30):  # 3s timeout
                        if self._mtu_exchanged or self._conn_handle is None:
                            break
                        await asyncio.sleep(0.1)
                    if self._conn_handle is None:
                        continue
                    if not self._mtu_exchanged:
                        diag_print("MTU exchange timed out, proceeding.")
                        self._mtu_exchanged = True  # fallback

                # Step B: Perform Encryption / Pairing immediately after MTU Exchange
                if not self._encrypted:
                    if not self._pairing_attempted:
                        self._pairing_attempted = True
                        self._pairing_start_time = time.time()

                        if self._pending_pair:
                            diag_print(
                                "Initiating LE Legacy Pairing for PIN {:06d}...".format(
                                    self.pairing_passkey
                                )
                            )
                            try:
                                self._ble.gap_pair(self._conn_handle)
                                # Workaround for MicroPython Central IRQ bug:
                                # Proactively inject the PIN after a short delay so NimBLE can authenticate.
                                asyncio.create_task(self._proactive_pin_injection())
                            except Exception as e:
                                diag_print("gap_pair() trigger failed: " + str(e))
                                self._disconnect()
                                continue
                        else:
                            diag_print(
                                "Bond found. Initiating encryption using stored keys..."
                            )
                            try:
                                self._ble.gap_pair(self._conn_handle)
                            except Exception as e:
                                diag_print(
                                    "gap_pair() reconnect trigger failed: " + str(e)
                                )
                                self._disconnect()
                                continue

                    # Wait for encryption status update
                    paired_ok = False
                    # Allow up to 25s for the first-time pairing (giving room for pin entry)
                    # and up to 10s for subsequent automatic key reconnection.
                    timeout_val = 25 if self._pending_pair else 10
                    while (time.time() - self._pairing_start_time) < timeout_val:
                        if self._encrypted or self._conn_handle is None:
                            paired_ok = self._encrypted
                            break
                        await asyncio.sleep(0.1)

                    if self._conn_handle is None:
                        continue

                    if not paired_ok:
                        if not self._pending_pair:
                            diag_print(
                                "Encryption failed with stored keys. Bond may have been lost on RNode. Triggering fresh pairing..."
                            )
                            self._pending_pair = True
                            self._pairing_attempted = False
                            try:
                                import os

                                os.remove("bonded.txt")
                                os.sync()
                            except Exception:
                                pass
                            self._already_bonded = False
                            continue
                        else:
                            diag_print(
                                "First-time pairing sequence failed or timed out. Halting interface to prevent endless retries."
                            )
                            self.enabled = False
                            self._disconnect()
                            continue

                # Step C: Perform GATT Discovery over the secured, encrypted link
                if not self._desc_discovered:
                    if not self._discovering:
                        diag_print("Discovering NUS Service over secure link...")
                        self._discovering = True
                        try:
                            self._ble.gattc_discover_services(
                                self._conn_handle, UART_SERVICE_UUID
                            )
                        except Exception as e:
                            diag_print("NUS Service discovery failed: " + str(e))
                            self._disconnect()
                            continue
                    for _ in range(100):  # 10s timeout
                        if self._desc_discovered or self._conn_handle is None:
                            break
                        await asyncio.sleep(0.1)
                    if self._conn_handle is None:
                        continue
                    if not self._desc_discovered:
                        diag_print("GATT service/descriptor discovery timed out!")
                        self._disconnect()
                        continue

                # Step D: Subscribe to characteristics once link is secured and discovered
                if not self._notify_enabled:
                    if not self._subscribing:
                        diag_print("Subscribing to TX Characteristic...")
                        self._subscribe_notifications()
                    for _ in range(50):  # 5s timeout
                        if self._notify_enabled or self._conn_handle is None:
                            break
                        await asyncio.sleep(0.1)
                    if self._conn_handle is None:
                        continue
                    if not self._notify_enabled:
                        diag_print("Subscription to RNode characteristic failed!")
                        self._disconnect()
                        continue

                # Step E: Perform RNode Handshake / Detection
                if not self._detected:
                    if (
                        self._detect_sent_time == 0
                        or (now - self._detect_sent_time) > 4
                    ):
                        if self._detect_retries < 4:
                            diag_print(
                                "Sending RNode probe (attempt {})...".format(
                                    self._detect_retries + 1
                                )
                            )
                            self._send_detection()
                            self._detect_sent_time = time.time()
                        else:
                            diag_print("RNode handshake failed.")
                            self._disconnect()
                            continue
                    await asyncio.sleep(0.1)
                    continue

                # Step F: Transmission Loop
                if self.online:
                    if self._write_queue:
                        self._ble_write_kiss(self._write_queue.pop(0))
                        _write_yield += 1
                        if _write_yield >= 5:
                            _write_yield = 0
                            await asyncio.sleep(0)
                    else:
                        await asyncio.sleep(0.01)
                    continue

                await asyncio.sleep(0.1)

            except Exception as e:
                diag_print("Poll Loop Error: " + str(e))
                await asyncio.sleep(1)

    def _irq(self, event, data):
        try:
            if event == _IRQ_PERIPHERAL_CONNECT:
                self._on_connect(data)
            elif event == _IRQ_PERIPHERAL_DISCONNECT:
                self._on_disconnect(data)
            elif event == _IRQ_SCAN_RESULT:
                if self._target_addr_found is None and self._match_scan_result(
                    data[0], bytes(data[1]), bytes(data[4])
                ):
                    self._target_addr_found = (data[0], bytes(data[1]))
            elif event == _IRQ_SCAN_DONE:
                self._scanning = False
                if self._target_addr_found is None:
                    self._scan_result = False
            elif event == _IRQ_GATTC_SERVICE_RESULT:
                self._on_service_result(data)
            elif event == _IRQ_GATTC_SERVICE_DONE:
                self._on_service_done(data)
            elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
                self._on_char_result(data)
            elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
                self._on_char_done(data)
            elif event == _IRQ_GATTC_DESCRIPTOR_RESULT:
                self._on_descriptor_result(data)
            elif event == _IRQ_GATTC_DESCRIPTOR_DONE:
                self._on_descriptor_done(data)
            elif event == _IRQ_GATTC_WRITE_DONE:
                self._on_write_done(data)
            elif event == _IRQ_GATTC_NOTIFY:
                self._on_notify(data)
            elif event == _IRQ_MTU_EXCHANGED:
                if data[0] == self._conn_handle:
                    self._negotiated_mtu, self._mtu_exchanged = data[1], True
            elif event == _IRQ_ENCRYPTION_UPDATE:
                if data[0] == self._conn_handle:
                    conn_handle = data[0]
                    encrypted = bool(data[1])
                    authenticated = bool(data[2]) if len(data) > 2 else False
                    bonded = bool(data[3]) if len(data) > 3 else False

                    self._encrypted = encrypted
                    diag_print(
                        "Encryption Update: encrypted={}, authenticated={}, bonded={}".format(
                            encrypted, authenticated, bonded
                        )
                    )
                    if self._encrypted:
                        diag_print("Encrypted link established!")
                        self._already_bonded = True
                        self._bonded_dirty = (
                            True  # Safely scheduled outside the interrupt context
                        )
            elif event == _IRQ_PASSKEY_ACTION:
                if data[0] == self._conn_handle:
                    conn_handle, action, passkey = data
                    diag_print(
                        "Passkey Action requested: action={}, passkey={}".format(
                            action, passkey
                        )
                    )
                    self._on_passkey_action(action, passkey)
            elif event == _IRQ_GET_SECRET:
                return self._on_get_secret(data)
            elif event == _IRQ_SET_SECRET:
                return self._on_set_secret(data)
        except Exception as e:
            diag_print("IRQ Error: {}".format(e))

    def close(self):
        self._shutting_down = self.online = self.enabled = False
        self._stop_scan()
        self._disconnect()
        time.sleep_ms(100)
        if self._ble:
            try:
                self._ble.active(False)
            except Exception:
                pass
        self._kiss_buf, self._write_queue = bytearray(), []
        super().close()

    def __str__(self):
        return "RNodeBLEInterface[" + self.name + "]"
