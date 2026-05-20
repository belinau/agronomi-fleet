# µReticulum BLE Client Interface
# GATT client for ESP32-C6 — connects to a BLE GATT server
# (BLEInterface or RNode with BLE) and sends/receives RNS packets.
#
# BLE GATT flow:
#   1. Scan for devices advertising the RNS service UUID
#   2. Connect to the matching device
#   3. Discover the RNS characteristic
#   4. Enable notifications (to receive data FROM the server)
#   5. Write RNS packets to the characteristic (write without response)
#   6. Server sends data back via notifications
#
# Fragmentation:
#   BLE limits payloads to ~244 bytes on ESP32-C6.  RNS packets may
#   exceed this, so we use a simple length-prefix protocol:
#     [2-byte big-endian length][data chunk][2-byte big-endian length][data chunk]...
#   A complete packet fits in a single write when it is within the MTU.
#   Only packets exceeding the MTU are fragmented.

import gc
import struct
import time

import ubluetooth as bt

from ..log import LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_VERBOSE, LOG_WARNING, log
from . import Interface

# ---------------------------------------------------------------------------
# ubluetooth IRQ event constants (MicroPython ESP32-C6)
# Redefined here so we don't depend on module internals that may not be
# exposed in every build.  These match the standard MicroPython values.
# ---------------------------------------------------------------------------
_IRQ_CENTRAL_CONNECT = 1  # A central connected to our GATT server
_IRQ_CENTRAL_DISCONNECT = 2  # A central disconnected
_IRQ_GATTS_WRITE = 3  # GATT server: client wrote a value
_IRQ_GATTS_READ_REQUEST = 4  # GATT server: client wants to read
_IRQ_SCAN_RESULT = 5  # Scan found a device
_IRQ_SCAN_DONE = 6  # Scan finished
_IRQ_PERIPHERAL_CONNECT = 7  # We connected to a peripheral (as central)
_IRQ_PERIPHERAL_DISCONNECT = 8  # We disconnected from a peripheral
_IRQ_GATTC_SERVICE_RESULT = 9  # GATT client: service discovered
_IRQ_GATTC_SERVICE_DONE = 10  # GATT client: service discovery done
_IRQ_GATTC_CHARACTERISTIC_RESULT = 11  # GATT client: characteristic found
_IRQ_GATTC_CHARACTERISTIC_DONE = 12  # GATT client: characteristic discovery done
_IRQ_GATTC_READ_DONE = 13  # GATT client: read completed
_IRQ_GATTC_WRITE_DONE = 14  # GATT client: write completed
_IRQ_GATTC_NOTIFY = 15  # GATT client: notification received
_IRQ_GATTC_INDICATE = 16  # GATT client: indication received

# BLE constants
_BLE_SCAN_INTERVAL_US = 30000  # 30 ms active scan interval
_BLE_SCAN_WINDOW_US = 30000  # 30 ms active scan window
_BLE_CONNECT_INTERVAL_MS = None  # Let the stack decide

# Default UUIDs — must match BLEInterface (ble_interface.py)
DEFAULT_SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
DEFAULT_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"

# Fragmentation protocol: 2-byte big-endian length prefix
_FRAG_HEADER_LEN = 2


class BLEClientInterface(Interface):
    """BLE GATT client interface for µReticulum.

    Scans for a BLE peripheral advertising the RNS service UUID,
    connects, discovers the characteristic, enables notifications,
    and sends/receives RNS packets over BLE.
    """

    # ESP32-C6 BLE MTU (typical negotiated MTU minus ATT overhead)
    BLE_MTU = 244

    # Maximum RNS packet size we'll attempt to reassemble
    MAX_REASSEMBLY = 4096

    def __init__(self, config):
        name = config.get("name", "BLE Client")
        super().__init__(name)

        # Configuration
        self.target_name = config.get("target_name", "BLEGateway")
        self.target_address = config.get("target_address", None)
        self.service_uuid = config.get("service_uuid", DEFAULT_SERVICE_UUID)
        self.char_uuid = config.get("char_uuid", DEFAULT_CHAR_UUID)
        self.scan_timeout = config.get("scan_timeout", 10)
        self.reconnect_delay = config.get("reconnect_delay", 5)

        self.bitrate = config.get("bitrate", 250000)  # BLE ~250 kbps effective

        # BLE state
        self._ble = None
        self._conn_handle = None
        self._char_handle = None
        self._service_handle = None

        # Scanning state
        self._scanning = False
        self._target_addr_found = None  # (addr_type, addr_bytes) tuple
        self._scan_result = (
            None  # Event-like flag: None = not started, True = found, False = timeout
        )

        # Connection state machine
        self._connecting = False
        self._discovering = False
        self._subscribing = False
        self._service_discovered = False
        self._char_discovered = False
        self._notify_enabled = False

        # Reassembly buffer for incoming fragmented packets
        self._reassembly_buf = bytearray()

        # Write queue for outgoing packets (simple list, processed in poll_loop)
        self._write_queue = []

        # Reconnection tracking
        self._reconnect_count = 0
        self._last_reconnect = 0
        self._shutting_down = False

        # Initialize BLE
        try:
            self._ble = bt.BLE()
            self._ble.active(True)
            self._ble.irq(self._irq)
            log("BLE Client " + self.name + " initialized", LOG_NOTICE)
        except Exception as e:
            log("BLE Client init failed: " + str(e), LOG_ERROR)
            self._ble = None
            return

        # Trigger initial scan+connect cycle asynchronously — the actual
        # connection will be established from poll_loop.
        self._start_scan()

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------
    def _start_scan(self):
        """Start scanning for the target device."""
        if not self._ble or self._shutting_down:
            return

        self._target_addr_found = None
        self._scan_result = None
        self._scanning = True

        try:
            # Active scanning to get scan-response data (device name)
            self._ble.gap_scan(
                self.scan_timeout * 1000,  # duration in ms
                _BLE_SCAN_INTERVAL_US,
                _BLE_SCAN_WINDOW_US,
                True,  # active scan
            )
            log(
                "BLE scanning for '"
                + self.target_name
                + "' (timeout="
                + str(self.scan_timeout)
                + "s)",
                LOG_VERBOSE,
            )
        except Exception as e:
            log("BLE scan start failed: " + str(e), LOG_ERROR)
            self._scanning = False
            self._scan_result = False

    def _stop_scan(self):
        """Stop an active scan."""
        if self._scanning and self._ble:
            try:
                self._ble.gap_scan(None)
            except:
                pass
            self._scanning = False

    # ------------------------------------------------------------------
    # Connection events
    # ------------------------------------------------------------------
    def _on_connect(self, data):
        """Handle _IRQ_PERIPHERAL_CONNECT: we connected to a peripheral."""
        conn_handle, _, _ = data
        self._conn_handle = conn_handle
        self._connecting = False
        log("BLE connected (handle=" + str(conn_handle) + ")", LOG_NOTICE)

        # Begin service discovery
        self._service_discovered = False
        self._char_discovered = False
        self._notify_enabled = False
        self._char_handle = None
        self._discovering = True

        try:
            self._ble.gattc_discover_services(conn_handle)
        except Exception as e:
            log("BLE service discover failed: " + str(e), LOG_ERROR)
            self._disconnect()

    def _on_disconnect(self, data):
        """Handle _IRQ_PERIPHERAL_DISCONNECT: lost connection."""
        conn_handle, _, _ = data
        log("BLE disconnected (handle=" + str(conn_handle) + ")", LOG_NOTICE)
        self._conn_handle = None
        self._char_handle = None
        self._service_handle = None
        self._service_discovered = False
        self._char_discovered = False
        self._notify_enabled = False
        self._connecting = False
        self._discovering = False
        self._subscribing = False
        self.online = False

        # Clear any in-progress reassembly
        self._reassembly_buf = bytearray()

        if not self._shutting_down:
            # Schedule reconnection from poll_loop
            self._last_reconnect = time.time()

    # ------------------------------------------------------------------
    # Service discovery
    # ------------------------------------------------------------------
    def _on_service_result(self, data):
        """Handle _IRQ_GATTC_SERVICE_RESULT."""
        conn_handle, start_handle, end_handle, uuid = data
        # Check if this is our target service
        uuid_str = str(uuid)
        if uuid_str == self.service_uuid:
            self._service_handle = (start_handle, end_handle)
            log(
                "BLE found target service (handles "
                + str(start_handle)
                + "-"
                + str(end_handle)
                + ")",
                LOG_DEBUG,
            )

    def _on_service_done(self, data):
        """Handle _IRQ_GATTC_SERVICE_DONE: service enumeration complete."""
        (conn_handle,) = data
        self._discovering = False

        if self._service_handle is None:
            log("BLE target service not found on device", LOG_ERROR)
            self._disconnect()
            return

        # Start characteristic discovery within the service
        start_handle, end_handle = self._service_handle
        self._char_discovered = False
        self._char_handle = None
        self._discovering = True

        try:
            self._ble.gattc_discover_characteristics(
                self._conn_handle,
                bt.UUID(self.char_uuid),
                start_handle,
                end_handle,
            )
        except Exception as e:
            log("BLE char discover failed: " + str(e), LOG_ERROR)
            self._disconnect()

    # ------------------------------------------------------------------
    # Characteristic discovery
    # ------------------------------------------------------------------
    def _on_char_result(self, data):
        """Handle _IRQ_GATTC_CHARACTERISTIC_RESULT."""
        conn_handle, decl_handle, value_handle, properties, uuid = data
        uuid_str = str(uuid)
        if uuid_str == self.char_uuid:
            self._char_handle = value_handle
            self._char_props = properties
            log(
                "BLE found target char (value_handle="
                + str(value_handle)
                + ", props=0x"
                + ("%02x" % properties)
                + ")",
                LOG_DEBUG,
            )

    def _on_char_done(self, data):
        """Handle _IRQ_GATTC_CHARACTERISTIC_DONE: char enumeration complete."""
        (conn_handle,) = data
        self._discovering = False

        if self._char_handle is None:
            log("BLE target characteristic not found", LOG_ERROR)
            self._disconnect()
            return

        self._char_discovered = True

        # Enable notifications (write CCCD = 0x0001)
        self._subscribe_notifications()

    # ------------------------------------------------------------------
    # Notification subscription
    # ------------------------------------------------------------------
    def _subscribe_notifications(self):
        """Write the Client Characteristic Configuration Descriptor (CCCD) to
        enable notifications."""
        if not self._ble or self._conn_handle is None or self._char_handle is None:
            return

        # CCCD is typically at char_handle + 1
        # Write 0x0001 (little-endian) to enable notifications
        self._subscribing = True
        try:
            cccd_handle = self._char_handle + 1
            self._ble.gattc_write(
                self._conn_handle,
                cccd_handle,
                b"\x01\x00",  # Enable notifications (little-endian)
                True,  # write_with_response=True for CCCD
            )
            log(
                "BLE subscribing to notifications (CCCD handle="
                + str(cccd_handle)
                + ")",
                LOG_DEBUG,
            )
        except Exception as e:
            log("BLE subscribe failed: " + str(e), LOG_ERROR)
            self._subscribing = False
            self._disconnect()

    def _on_write_done(self, data):
        """Handle _IRQ_GATTC_WRITE_DONE."""
        conn_handle, value_handle, status = data
        if self._subscribing and value_handle == (
            self._char_handle + 1 if self._char_handle else 0
        ):
            self._subscribing = False
            if status == 0:
                self._notify_enabled = True
                self.online = True
                self._reconnect_count = 0
                log("BLE notifications enabled — interface ONLINE", LOG_NOTICE)
            else:
                log("BLE CCCD write failed (status=" + str(status) + ")", LOG_ERROR)
                self._disconnect()

    # ------------------------------------------------------------------
    # Incoming data (notifications from server)
    # ------------------------------------------------------------------
    def _on_notify(self, data):
        """Handle _IRQ_GATTC_NOTIFY: data received from the server."""
        conn_handle, value_handle, notify_data = data

        if value_handle != self._char_handle:
            return

        if not notify_data:
            return

        # Feed the received bytes into the reassembly engine
        self._feed_reassembly(notify_data)

    def _feed_reassembly(self, data):
        """Process incoming BLE data through the length-prefix reassembly engine.

        Protocol:
          Complete unfragmented packet: raw RNS packet bytes (no length prefix).
          Fragmented packet:
            [2-byte big-endian total_length][data_chunk_1]
            [2-byte big-endian total_length][data_chunk_2]
            ...
          The receiver detects fragmentation by checking if the first two
          bytes decode to a total length that exceeds the payload length.
          If so, it reassembles. Otherwise, the payload is a complete packet.
        """
        if not data:
            return

        # Fast path: short payload that is clearly unfragmented
        if len(data) < _FRAG_HEADER_LEN:
            # Too short to be a fragment — treat as a tiny complete packet
            self._dispatch_packet(data)
            return

        # Check if this looks like a length-prefixed fragment
        claimed_length = struct.unpack(">H", data[0:2])[0]

        if claimed_length > len(data) - _FRAG_HEADER_LEN:
            # Length prefix claims more data than this payload contains:
            # this is the FIRST fragment of a fragmented packet.
            self._reassembly_buf = bytearray(data)
            # We'll accumulate more fragments in subsequent notifications
            return

        if len(self._reassembly_buf) > 0:
            # We're in the middle of reassembling — this is a continuation fragment.
            # Continuation fragments also have a length prefix so we know when
            # we're done.
            self._reassembly_buf.extend(data)

            # Check if we have the full packet now
            total_len = struct.unpack(">H", bytes(self._reassembly_buf[0:2]))[0]
            payload_len = len(self._reassembly_buf) - _FRAG_HEADER_LEN

            if payload_len >= total_len:
                # Reassembly complete — extract the full packet
                full_packet = bytes(
                    self._reassembly_buf[
                        _FRAG_HEADER_LEN : _FRAG_HEADER_LEN + total_len
                    ]
                )
                self._reassembly_buf = bytearray()
                self._dispatch_packet(full_packet)

            # If we've exceeded the max reassembly size, discard and reset
            if len(self._reassembly_buf) > self.MAX_REASSEMBLY:
                log(
                    "BLE reassembly buffer overflow ("
                    + str(len(self._reassembly_buf))
                    + "B), discarding",
                    LOG_ERROR,
                )
                self._reassembly_buf = bytearray()
            return

        # No reassembly in progress — this is either:
        #   a) a complete unfragmented packet (claimed_length == len(data) - 2)
        #   b) a length-prefixed packet that fits in one BLE frame
        if claimed_length <= len(data) - _FRAG_HEADER_LEN:
            # Length prefix is valid — strip it and dispatch the packet
            payload = data[_FRAG_HEADER_LEN : _FRAG_HEADER_LEN + claimed_length]
            self._dispatch_packet(payload)
        else:
            # Shouldn't happen — treat raw bytes as a complete packet
            self._dispatch_packet(data)

    def _dispatch_packet(self, data):
        """Dispatch a complete RNS packet to the transport layer."""
        if data and len(data) > 0:
            self.process_incoming(data)

    # ------------------------------------------------------------------
    # Outgoing data (write to characteristic)
    # ------------------------------------------------------------------
    def process_outgoing(self, data):
        """Send an RNS packet out through the BLE GATT characteristic.

        Applies IFAC signing, then fragments if necessary, and queues
        the write(s) for the BLE stack.
        """
        if not self.online or self._conn_handle is None or self._char_handle is None:
            log("BLE TX: not connected, dropping " + str(len(data)) + "B", LOG_DEBUG)
            return False

        try:
            data = self.ifac_sign(data)
            self._fragment_and_write(data)
            self.txb += len(data)
            self.tx += 1
            self._last_activity = time.time()
            return True
        except Exception as e:
            log("BLE TX error: " + str(e), LOG_ERROR)
            return False

    def _fragment_and_write(self, data):
        """Write data to the BLE characteristic, fragmenting if it exceeds MTU.

        Length-prefix protocol:
          Single frame fitting in MTU:  [2-byte big-endian length][payload]
          Multiple fragments:
            Frame 1: [2-byte big-endian total_length][payload_chunk_1]
            Frame 2: [2-byte big-endian total_length][payload_chunk_2]
            ...
          Each fragment carries the SAME total length prefix so the receiver
          can detect and reassemble. The receiver knows reassembly is needed
          when total_length > (fragment_size - 2).
        """
        max_chunk = self.BLE_MTU - _FRAG_HEADER_LEN

        if len(data) <= max_chunk:
            # Fits in a single BLE write
            frame = struct.pack(">H", len(data)) + data
            self._ble_write(frame)
        else:
            # Fragment
            length_prefix = struct.pack(">H", len(data))
            offset = 0
            while offset < len(data):
                chunk = data[offset : offset + max_chunk]
                frame = length_prefix + chunk
                self._ble_write(frame)
                offset += max_chunk

    def _ble_write(self, frame):
        """Write a raw frame to the BLE characteristic (write without response)."""
        if self._conn_handle is None or self._char_handle is None:
            return
        try:
            self._ble.gattc_write(
                self._conn_handle,
                self._char_handle,
                frame,
                False,  # write_without_response=True for speed
            )
        except Exception as e:
            log("BLE write error: " + str(e), LOG_ERROR)

    # ------------------------------------------------------------------
    # Disconnection and reconnection
    # ------------------------------------------------------------------
    def _disconnect(self):
        """Disconnect from the BLE peripheral."""
        if self._conn_handle is not None and self._ble:
            try:
                self._ble.gap_disconnect(self._conn_handle)
            except:
                pass
        # State will be cleaned up in _on_disconnect callback

    def _reconnect(self):
        """Attempt to reconnect to the BLE peripheral."""
        if self._shutting_down:
            return

        now = time.time()
        if now - self._last_reconnect < self.reconnect_delay:
            return

        self._last_reconnect = now
        self._reconnect_count += 1
        log(
            "BLE reconnecting (attempt " + str(self._reconnect_count) + ")...",
            LOG_NOTICE,
        )

        self._disconnect()

        # Small delay before scanning again — will be handled in poll_loop
        # by checking that we're offline and enough time has passed

    # ------------------------------------------------------------------
    # Scan matching helpers
    # ------------------------------------------------------------------
    def _decode_adv_data(self, adv_data):
        """Decode BLE advertising data to extract service UUIDs and device name.

        Returns (services, name) where services is a list of UUID strings
        and name is the decoded device name or None.
        """
        services = []
        name = None
        i = 0
        while i + 1 < len(adv_data):
            length = adv_data[i]
            if length == 0 or i + length + 1 > len(adv_data):
                break
            ad_type = adv_data[i + 1]
            field_data = bytes(adv_data[i + 2 : i + 1 + length])

            # Complete 128-bit service UUID list (AD type 0x07)
            if ad_type == 0x07:
                for j in range(0, len(field_data), 16):
                    if j + 16 <= len(field_data):
                        uuid_hex = field_data[j : j + 16].hex()
                        uuid_str = (
                            uuid_hex[0:8]
                            + "-"
                            + uuid_hex[8:12]
                            + "-"
                            + uuid_hex[12:16]
                            + "-"
                            + uuid_hex[16:20]
                            + "-"
                            + uuid_hex[20:32]
                        )
                        services.append(uuid_str)
            # Complete local name (AD type 0x09)
            elif ad_type == 0x09:
                try:
                    name = field_data.decode("utf-8")
                except:
                    name = field_data.decode("latin-1")
            # Shortened local name (AD type 0x08)
            elif ad_type == 0x08 and name is None:
                try:
                    name = field_data.decode("utf-8")
                except:
                    pass

            i += length + 1

        return services, name

    def _match_scan_result(self, addr_type, addr, adv_data):
        """Check if a scan result matches our target device.

        Matching logic:
          1. If target_address is configured, match on MAC address only.
          2. Otherwise, match on device name AND service UUID.
        """
        # Match by MAC address if configured
        if self.target_address:
            addr_str = ":".join(("%02x" % b) for b in addr)
            if addr_str.lower() == self.target_address.lower():
                return True
            return False

        # Match on service UUID and device name
        services, name = self._decode_adv_data(adv_data)

        service_match = self.service_uuid.lower() in [s.lower() for s in services]
        name_match = name is not None and name == self.target_name

        return service_match and name_match

    # ------------------------------------------------------------------
    # Async poll loop
    # ------------------------------------------------------------------
    async def poll_loop(self):
        """Async event loop that manages the BLE connection lifecycle.

        Handles:
          - Scanning for the target device
          - Connecting
          - Reconnecting on disconnection
          - Processing the outgoing write queue
          - Periodic garbage collection
        """
        import uasyncio as asyncio

        log("BLE Client poll loop started for " + self.name, LOG_VERBOSE)

        _last_gc = time.time()
        _scan_start = 0

        while self.enabled and not self._shutting_down:
            try:
                now = time.time()

                # Periodic GC
                if now - _last_gc >= 10:
                    gc.collect()
                    _last_gc = now

                # --- State: Not connected, not scanning ---
                if self._conn_handle is None and not self._scanning:
                    # Wait for reconnect delay
                    if (
                        self._last_reconnect > 0
                        and (now - self._last_reconnect) < self.reconnect_delay
                    ):
                        await asyncio.sleep(0.1)
                        continue

                    # Start scanning
                    self._start_scan()
                    _scan_start = now
                    continue

                # --- State: Scanning ---
                if self._scanning:
                    # Process scan results via _irq, but also check for timeout
                    if (
                        self._scan_result is False
                        or (now - _scan_start) >= self.scan_timeout
                    ):
                        # Scan completed or timed out without finding device
                        self._stop_scan()
                        if self._target_addr_found is None:
                            log(
                                "BLE scan: target '"
                                + self.target_name
                                + "' not found, retrying",
                                LOG_DEBUG,
                            )
                            self._last_reconnect = now
                        continue

                    # Check if we found a target (set by _irq)
                    if self._target_addr_found is not None:
                        addr_type, addr_bytes = self._target_addr_found
                        self._stop_scan()
                        log(
                            "BLE connecting to "
                            + ":".join(("%02x" % b) for b in addr_bytes),
                            LOG_VERBOSE,
                        )
                        try:
                            self._ble.gap_connect(
                                addr_type, addr_bytes, _BLE_CONNECT_INTERVAL_MS
                            )
                            self._connecting = True
                        except Exception as e:
                            log("BLE connect failed: " + str(e), LOG_ERROR)
                            self._last_reconnect = now

                    await asyncio.sleep(0.01)
                    continue

                # --- State: Connecting / Discovering ---
                if self._connecting or self._discovering or self._subscribing:
                    # BLE events are handled by _irq callbacks.
                    # Just yield to the event loop.
                    await asyncio.sleep(0.01)
                    continue

                # --- State: Connected and online ---
                if self.online:
                    # Nothing to do in the main loop when connected —
                    # incoming data arrives via _irq(_IRQ_GATTC_NOTIFY)
                    # and outgoing data goes via process_outgoing().
                    await asyncio.sleep(0.02)
                    continue

                # --- State: Disconnected, waiting to reconnect ---
                # _on_disconnect already set _last_reconnect
                await asyncio.sleep(0.1)

            except Exception as e:
                log("BLE poll error: " + str(e), LOG_ERROR)
                import sys

                sys.print_exception(e)
                await asyncio.sleep(1)

        log("BLE Client poll loop EXITED for " + self.name, LOG_ERROR)

    # ------------------------------------------------------------------
    # BLE IRQ handler
    # ------------------------------------------------------------------
    # Scan results and GATT events are dispatched from _irq().
    # The matching logic for scan results is in _match_scan_result().

    def _irq(self, event, data):
        """Central BLE IRQ handler — dispatches all BLE events to their handlers."""
        try:
            if event == _IRQ_PERIPHERAL_CONNECT:
                self._on_connect(data)
            elif event == _IRQ_PERIPHERAL_DISCONNECT:
                self._on_disconnect(data)
            elif event == _IRQ_SCAN_RESULT:
                self._on_scan_result(data)
            elif event == _IRQ_SCAN_DONE:
                self._on_scan_done(data)
            elif event == _IRQ_GATTC_SERVICE_RESULT:
                self._on_service_result(data)
            elif event == _IRQ_GATTC_SERVICE_DONE:
                self._on_service_done(data)
            elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
                self._on_char_result(data)
            elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
                self._on_char_done(data)
            elif event == _IRQ_GATTC_WRITE_DONE:
                self._on_write_done(data)
            elif event == _IRQ_GATTC_NOTIFY:
                self._on_notify(data)
        except Exception as e:
            log("BLE IRQ error (event=" + str(event) + "): " + str(e), LOG_ERROR)

    def _on_scan_result(self, data):
        """Handle _IRQ_SCAN_RESULT: check if this device matches our target."""
        addr_type, addr, adv_data, conn_data = data

        # Already found a target — skip
        if self._target_addr_found is not None:
            return

        if self._match_scan_result(addr_type, addr, adv_data):
            self._target_addr_found = (addr_type, bytes(addr))

    def _on_scan_done(self, data):
        """Handle _IRQ_SCAN_DONE: scan period ended."""
        self._scanning = False
        if self._target_addr_found is None:
            self._scan_result = False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def close(self):
        """Shutdown the BLE client interface."""
        self._shutting_down = True
        self.online = False
        self.enabled = False

        self._stop_scan()
        self._disconnect()

        # Give BLE stack time to process disconnect
        time.sleep_ms(100)

        if self._ble:
            try:
                self._ble.active(False)
            except:
                pass

        self._reassembly_buf = bytearray()
        self._write_queue = []

        super().close()
        log("BLE Client " + self.name + " closed", LOG_NOTICE)

    def __str__(self):
        return "BLEClientInterface[" + self.name + "]"
