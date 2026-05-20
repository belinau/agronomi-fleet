# µReticulum RNode BLE Interface
# KISS-over-Nordic-UART-Service BLE client for ESP32-C6 — connects to a
# RAK4631 RNode over BLE and sends/receives RNS packets via KISS framing.
#
# Protocol flow:
#   1. BLE scan for devices advertising the NUS service UUID or named "RNode *"
#   2. Connect to the matching device
#   3. Discover the NUS service and its RX/TX characteristics
#   4. Subscribe to TX characteristic notifications (incoming data)
#   5. Write KISS-escaped data frames to RX characteristic (outgoing data)
#   6. Incoming data from TX notifications is KISS-decoded and reassembled
#      into RNS packets, then passed to process_incoming()
#
# This mirrors the official RNS RNodeInterface BLE client, but uses
# MicroPython's ubluetooth API on ESP32-C6 instead of bleak/Python3.

import gc
import struct
import time

import ubluetooth as bt

from ..log import LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_VERBOSE, LOG_WARNING, log
from . import Interface

# ---------------------------------------------------------------------------
# ubluetooth IRQ event constants (MicroPython ESP32-C6)
# ---------------------------------------------------------------------------
_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_GATTS_READ_REQUEST = 4
_IRQ_SCAN_RESULT = 5
_IRQ_SCAN_DONE = 6
_IRQ_PERIPHERAL_CONNECT = 7
_IRQ_PERIPHERAL_DISCONNECT = 8
_IRQ_GATTC_SERVICE_RESULT = 9
_IRQ_GATTC_SERVICE_DONE = 10
_IRQ_GATTC_CHARACTERISTIC_RESULT = 11
_IRQ_GATTC_CHARACTERISTIC_DONE = 12
_IRQ_GATTC_DESCRIPTOR_RESULT = 13
_IRQ_GATTC_DESCRIPTOR_DONE = 14
_IRQ_GATTC_READ_RESULT = 15
_IRQ_GATTC_READ_DONE = 16
_IRQ_GATTC_WRITE_DONE = 17
_IRQ_GATTC_NOTIFY = 18
_IRQ_GATTC_INDICATE = 19
_IRQ_GATTS_INDICATE_DONE = 20
_IRQ_MTU_EXCHANGED = 21
_IRQ_CONNECTION_UPDATE = 27
_IRQ_ENCRYPTION_UPDATE = 28
_IRQ_PASSKEY_ACTION = 31

# Passkey action types for _IRQ_PASSKEY_ACTION
# These match MicroPython's official constants
_PASSKEY_ACTION_INPUT = 2  # Client should enter the passkey displayed on the server
_PASSKEY_ACTION_DISPLAY = (
    3  # Client should display a passkey for the user to enter on the server
)
_PASSKEY_ACTION_NUMERIC_COMPARISON = (
    4  # Client should confirm the numeric comparison matches
)

# BLE IO capability types for BLE.config('io')
_IO_CAPABILITY_DISPLAY_ONLY = 0
_IO_CAPABILITY_DISPLAY_YESNO = 1
_IO_CAPABILITY_KEYBOARD_ONLY = 2
_IO_CAPABILITY_NO_INPUT_OUTPUT = 3
_IO_CAPABILITY_KEYBOARD_DISPLAY = 4

# ---------------------------------------------------------------------------
# BLE constants
# ---------------------------------------------------------------------------
_BLE_SCAN_INTERVAL_US = 30000  # 30 ms active scan interval
_BLE_SCAN_WINDOW_US = 30000  # 30 ms active scan window

# Nordic UART Service (NUS) UUIDs
UART_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # Client writes to this
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # Client reads notifications

# ---------------------------------------------------------------------------
# KISS protocol constants
# ---------------------------------------------------------------------------
FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

CMD_DATA = 0x00
CMD_FREQUENCY = 0x01
CMD_BANDWIDTH = 0x02
CMD_TXPOWER = 0x03
CMD_SF = 0x04
CMD_CR = 0x05
CMD_RADIO_STATE = 0x06
CMD_RADIO_LOCK = 0x07
CMD_DETECT = 0x08
CMD_LEAVE = 0x0A
CMD_READY = 0x0F
CMD_STAT_RX = 0x21
CMD_STAT_TX = 0x22
CMD_STAT_RSSI = 0x23
CMD_STAT_SNR = 0x24
CMD_STAT_BAT = 0x27
CMD_RANDOM = 0x40
CMD_PLATFORM = 0x48
CMD_MCU = 0x49
CMD_FW_VERSION = 0x50
CMD_RESET = 0x55

# BLE control commands (used for serial-assisted auto-pairing)
CMD_BT_CTRL = 0x46
CMD_BT_PIN = 0x62
CMD_BT_ON = 0x01  # Sub-value for CMD_BT_CTRL: enable Bluetooth
CMD_BT_OFF = 0x00  # Sub-value for CMD_BT_CTRL: disable Bluetooth
CMD_BT_PAIR = 0x02  # Sub-value for CMD_BT_CTRL: enter pairing mode

# Radio states
RADIO_STATE_OFF = 0x00
RADIO_STATE_ON = 0x01
RADIO_STATE_ASK = 0xFF

# Detection byte values — must match official RNode firmware
DETECT_REQ = 0x73
DETECT_ACK = 0x46

# BLE MTU — requested MTU for negotiation (ESP32-C6 supports up to 247)
_BLE_MTU_REQUEST = 247

# Default radio parameters (868 MHz EU band)
_DEFAULT_FREQUENCY = 868000000
_DEFAULT_BANDWIDTH = 125000
_DEFAULT_TXPOWER = 17
_DEFAULT_SF = 11
_DEFAULT_CR = 5


def _kiss_escape(data):
    """Escape bytes for KISS framing: 0xDB → 0xDB 0xDD, 0xC0 → 0xDB 0xDC."""
    out = bytearray()
    for b in data:
        if b == 0xDB:
            out.extend(b"\xdb\xdd")
        elif b == 0xC0:
            out.extend(b"\xdb\xdc")
        else:
            out.append(b)
    return bytes(out)


def _kiss_unescape(data):
    """Unescape KISS bytes: 0xDB 0xDD → 0xDB, 0xDB 0xDC → 0xC0."""
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0xDB and i + 1 < len(data):
            if data[i + 1] == 0xDD:
                out.append(0xDB)
                i += 2
                continue
            elif data[i + 1] == 0xDC:
                out.append(0xC0)
                i += 2
                continue
        out.append(data[i])
        i += 1
    return bytes(out)


class RNodeBLEInterface(Interface):
    """KISS-over-BLE RNode interface for µReticulum.

    Connects to a RAK4631 RNode via the Nordic UART Service (NUS) BLE GATT
    profile.  The RNode exposes NUS when BLE is enabled with
    ``rnodeconf --bluetooth-on``.

    Data is KISS-framed over the NUS UART: outgoing RNS packets are escaped
    and wrapped in KISS DATA frames before writing to the RX characteristic;
    incoming TX notifications are KISS-decoded and reassembled into RNS
    packets that are fed to process_incoming().
    """

    # Maximum RNS packet we'll reassemble
    MAX_REASSEMBLY = 4096

    def __init__(self, config):
        name = config.get("name", "RNode BLE")
        super().__init__(name)

        # ----- Radio configuration -----
        self.frequency = config.get("frequency", _DEFAULT_FREQUENCY)
        self.bandwidth = config.get("bandwidth", _DEFAULT_BANDWIDTH)
        self.txpower = config.get("txpower", _DEFAULT_TXPOWER)
        self.spreadingfactor = config.get("spreadingfactor", _DEFAULT_SF)
        self.codingrate = config.get("codingrate", _DEFAULT_CR)

        # ----- BLE target configuration -----
        self.target_name = config.get("target_name", "")
        self.target_address = config.get("target_address", None)
        self.scan_timeout = config.get("scan_timeout", 10)
        self.reconnect_delay = config.get("reconnect_delay", 5)

        # Pairing passkey for official RNode firmware (RAK4631 nRF52).
        # The RNode requires MITM-protected pairing with a passkey.
        #
        # Pairing flow:
        #   1. Put RNode in pairing mode: rnodeconf --bluetooth-on or button
        #   2. RNode displays a random 6-digit PIN on its screen
        #   3. Set pairing_passkey to that PIN
        #   4. ESP32-C6 connects, pairs using the PIN, RNode disconnects us
        #   5. ESP32-C6 reconnects — bond is stored, no PIN needed after first pair
        #   6. Set pairing_passkey back to 0 after successful first pair
        #
        # For third-party BLE bridges (Heltec V3 RTReticulum) that use
        # Just Works pairing, leave this at 0.
        self.pairing_passkey = config.get("pairing_passkey", 0)

        # Serial port for automatic pairing assistance.
        # When set, the interface can automatically put the RNode into
        # pairing mode and read the PIN via its USB serial KISS interface,
        # eliminating the need for manual PIN configuration.
        self.serial_port = config.get("serial_port", None)

        # Automatic pairing state
        self._auto_pairing_in_progress = False
        self._serial_pairing_pin = None  # PIN obtained from serial KISS
        self._serial_pairing_attempts = 0
        self._max_serial_pairing_attempts = 3

        self.bitrate = config.get("bitrate", 250000)

        # ----- BLE state -----
        self._ble = None
        self._conn_handle = None
        self._rx_char_handle = None  # Client writes data here
        self._tx_char_handle = None  # Client reads notifications here
        self._service_handle = None  # (start, end) handle range

        # Scanning
        self._scanning = False
        self._target_addr_found = None  # (addr_type, addr_bytes)
        self._scan_result = None  # None=pending, True=found, False=timeout

        # Connection state machine
        self._connecting = False
        self._discovering = False
        self._subscribing = False
        self._service_discovered = False
        self._char_discovered = False
        self._desc_discovered = False
        self._notify_enabled = False
        self._cccd_handle = None  # CCCD descriptor handle for TX characteristic

        # ----- KISS state -----
        self._kiss_buf = bytearray()  # Incoming KISS reassembly buffer
        self._in_frame = False  # Between FEND delimiters
        self._escaping = False  # Processing escape sequence
        self._current_cmd = None  # Current KISS command byte

        # ----- Detection handshake -----
        self._detected = False
        self._detect_retries = 0
        self._max_detect_retries = 3

        # ----- MTU -----
        self._negotiated_mtu = 23  # Default BLE ATT MTU before exchange
        self._mtu_exchanged = False  # True after _IRQ_MTU_EXCHANGED fires
        self._encrypted = False  # True after _IRQ_ENCRYPTION_UPDATE fires

        # ----- Radio config state -----
        self._radio_configured = False

        # ----- Statistics -----
        self.rssi = None
        self.snr = None
        self.battery_level = None
        self.rnode_platform = None
        self.rnode_mcu = None
        self.rnode_fw_version = None

        # ----- Outgoing write queue -----
        self._write_queue = []

        # ----- Reconnection tracking -----
        self._reconnect_count = 0
        self._last_reconnect = 0
        self._shutting_down = False
        self._pairing_attempted = False
        self._detect_sent_time = 0

        # ----- Initialize BLE -----
        try:
            self._ble = bt.BLE()
            self._ble.active(True)
            # Request a larger MTU before any connections — ESP32-C6 supports up to 247.
            # The actual negotiated MTU will be confirmed by _IRQ_MTU_EXCHANGED.
            self._ble.config(mtu=_BLE_MTU_REQUEST)
            # Enable bonding and set security for pairing with the RNode.
            # The official RNode firmware (RAK4631 nRF52) requires MITM-protected
            # pairing (SECMODE_ENC_WITH_MITM) with a passkey.
            # We configure:
            #   bond=True — store pairing keys persistently
            #   mitm=True — require MITM protection (passkey entry)
            #   io=KEYBOARD_ONLY — we can enter a passkey displayed by the RNode
            #   le_secure=True — use LE Secure Connections (required by nRF52)
            #
            # For third-party BLE bridges (Heltec V3) that use Just Works,
            # these settings won't cause problems — they just won't trigger a
            # passkey challenge since the bridge doesn't require MITM.
            self._ble.config(bond=True)
            # MITM and IO capability configuration for official RNode pairing.
            # The nRF52 RNode uses Display Only IO caps and requires MITM.
            # We set KEYBOARD_ONLY so we can enter the passkey.
            # If pairing_passkey is 0 (third-party bridge), these settings
            # don't cause issues since Just Works bridges don't require pairing.
            self._ble.config(mitm=True)
            self._ble.config(io=_IO_CAPABILITY_KEYBOARD_ONLY)
            self._ble.irq(self._irq)
            log("RNode BLE " + self.name + " initialized", LOG_NOTICE)
        except Exception as e:
            log("RNode BLE init failed: " + str(e), LOG_ERROR)
            self._ble = None
            return

        # Trigger initial scan+connect cycle
        self._start_scan()

    # ==================================================================
    # Serial-Assisted Auto-Pairing
    # ==================================================================

    def _serial_pair(self):
        """Automatically obtain a pairing PIN from the RNode via USB serial.

        Opens the RNode's serial port, sends CMD_BT_CTRL 0x02 to trigger
        pairing mode, reads the CMD_BT_PIN response containing the random
        6-digit PIN, and updates self.pairing_passkey.

        Returns True if a PIN was obtained, False otherwise.
        """
        if not self.serial_port:
            log("RNode BLE auto-pairing: no serial_port configured", LOG_WARNING)
            return False

        if self._serial_pairing_attempts >= self._max_serial_pairing_attempts:
            log(
                "RNode BLE auto-pairing: max attempts ("
                + str(self._max_serial_pairing_attempts)
                + ") reached",
                LOG_ERROR,
            )
            return False

        self._serial_pairing_attempts += 1
        log(
            "RNode BLE auto-pairing: attempting via serial (attempt "
            + str(self._serial_pairing_attempts)
            + ")",
            LOG_NOTICE,
        )

        try:
            from machine import UART

            # Use UART 1 for serial KISS communication with the RNode.
            # The default UART 0 is used by the REPL.
            uart_id = 1
            serial = UART(uart_id, 115200)
        except Exception as e:
            log("RNode BLE auto-pairing: UART init failed: " + str(e), LOG_ERROR)
            return False

        try:
            # Step 1: Send CMD_BT_CTRL with CMD_BT_PAIR to trigger pairing mode
            pair_cmd = bytes([FEND, CMD_BT_CTRL, CMD_BT_PAIR, FEND])
            serial.write(pair_cmd)
            log("RNode BLE auto-pairing: sent CMD_BT_CTRL PAIR via serial", LOG_NOTICE)

            # Step 2: Wait for CMD_BT_PIN response (up to 10 seconds).
            # The RNode responds with a KISS frame: FEND CMD_BT_PIN <4 bytes PIN> FEND
            # The 4 bytes are a big-endian 32-bit integer (the 6-digit PIN).
            buf = bytearray()
            in_frame = False
            pin_data = None
            deadline = time.time() + 10  # 10 second timeout

            while time.time() < deadline:
                if serial.any():
                    b = serial.read(1)
                    if b:
                        b = b[0]
                        if b == FEND:
                            if in_frame and len(buf) >= 1:
                                cmd_byte = buf[0]
                                if cmd_byte == CMD_BT_PIN and len(buf) >= 5:
                                    # Parse the 4-byte big-endian PIN
                                    pin_data = buf[1:5]
                                    break
                            # Start new frame
                            in_frame = True
                            buf = bytearray()
                        elif in_frame:
                            buf.append(b)
                else:
                    time.sleep_ms(10)

            # Deinitialize the UART — we only need it for pairing
            serial.deinit()

            if pin_data is not None:
                pin = (
                    (pin_data[0] << 24)
                    | (pin_data[1] << 16)
                    | (pin_data[2] << 8)
                    | pin_data[3]
                )
                log(
                    "RNode BLE auto-pairing: obtained PIN "
                    + "{:06d}".format(pin)
                    + " from RNode",
                    LOG_NOTICE,
                )
                self.pairing_passkey = pin
                self._serial_pairing_pin = pin
                return True
            else:
                log(
                    "RNode BLE auto-pairing: no CMD_BT_PIN response received",
                    LOG_ERROR,
                )
                return False

        except Exception as e:
            try:
                serial.deinit()
            except:
                pass
            log("RNode BLE auto-pairing error: " + str(e), LOG_ERROR)
            return False

    # ==================================================================
    # BLE Scanning
    # ==================================================================

    def _start_scan(self):
        """Start scanning for RNode devices."""
        if not self._ble or self._shutting_down:
            return

        self._target_addr_found = None
        self._scan_result = None
        self._scanning = True

        try:
            self._ble.gap_scan(
                self.scan_timeout * 1000,
                _BLE_SCAN_INTERVAL_US,
                _BLE_SCAN_WINDOW_US,
                True,  # active scan
            )
            target_desc = (
                "'" + self.target_name + "'" if self.target_name else "any RNode"
            )
            log("RNode BLE scanning for " + target_desc, LOG_VERBOSE)
        except Exception as e:
            log("RNode BLE scan start failed: " + str(e), LOG_ERROR)
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
            # BLE advertising sends 128-bit UUIDs little-endian — reverse bytes before formatting
            if ad_type == 0x07:
                for j in range(0, len(field_data), 16):
                    if j + 16 <= len(field_data):
                        uuid_bytes = bytes(reversed(field_data[j : j + 16]))
                        uuid_hex = uuid_bytes.hex()
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
                except Exception:
                    name = field_data.decode("latin-1")
            # Shortened local name (AD type 0x08)
            elif ad_type == 0x08 and name is None:
                try:
                    name = field_data.decode("utf-8")
                except Exception:
                    pass

            i += length + 1

        return services, name

    def _match_scan_result(self, addr_type, addr, adv_data):
        """Check if a scan result matches our target RNode device.

        Matching logic:
          1. If target_address is configured, match on MAC address only.
          2. If target_name is configured (non-empty), match on NUS service UUID
             AND device name.
          3. Auto-discovery (target_name empty): match any device advertising
             the NUS service UUID OR whose name starts with "RNode ".
        """
        # Match by MAC address if configured
        if self.target_address:
            addr_str = ":".join(("%02x" % b) for b in addr)
            if addr_str.lower() == self.target_address.lower():
                return True
            return False

        services, name = self._decode_adv_data(adv_data)
        nus_uuid_lower = UART_SERVICE_UUID.lower()
        has_nus = nus_uuid_lower in [s.lower() for s in services]

        # Auto-discovery: match any device with NUS service or name starting with "RNode "
        if not self.target_name:
            if has_nus:
                return True
            if name is not None and name.startswith("RNode "):
                return True
            return False

        # Explicit target_name: match on NUS service UUID AND device name
        service_match = has_nus
        name_match = name is not None and name == self.target_name

        return service_match and name_match

    # ==================================================================
    # BLE Connection
    # ==================================================================

    def _on_connect(self, data):
        """Handle _IRQ_PERIPHERAL_CONNECT: we connected to the RNode.

        Connection flow depends on the target RNode's security requirements:

        - Third-party BLE bridges (Heltec V3, etc.) use Just Works pairing
          (sm_mitm=0). No pairing is needed — we proceed directly to MTU
          exchange and service discovery.

        - Official RNode firmware (RAK4631 nRF52) requires MITM-protected pairing
          (SECMODE_ENC_WITH_MITM). We must call gap_pair() and provide the
          passkey configured in `pairing_passkey`.

        The strategy is:
          1. If pairing_passkey is configured, call gap_pair() immediately
             to start the pairing process. The _IRQ_PASSKEY_ACTION handler
             will respond with the configured passkey.
          2. If pairing_passkey is 0 (default), skip pairing and proceed
             directly. If the CCCD write fails with a security error,
             we'll attempt pairing then.
        """
        conn_handle, _, _ = data
        self._conn_handle = conn_handle
        self._connecting = False
        self._service_discovered = False
        self._char_discovered = False
        self._desc_discovered = False
        self._notify_enabled = False
        self._mtu_exchanged = False
        self._negotiated_mtu = 23  # Reset to BLE default before MTU exchange
        self._encrypted = False
        self._pairing_attempted = False
        self._rx_char_handle = None
        self._tx_char_handle = None
        self._cccd_handle = None
        self._detect_sent_time = 0
        self._serial_pairing_attempts = 0
        # _discovering stays False until poll_loop starts discovery
        log("RNode BLE connected (handle=" + str(conn_handle) + ")", LOG_NOTICE)

        # If a pairing passkey is configured (or dynamically obtained from
        # serial auto-pairing), initiate pairing immediately.
        # This is needed for official RNode firmware (RAK4631) which requires
        # MITM-protected pairing (SECMODE_ENC_WITH_MITM).
        # On reconnect after successful pairing, the bond is already stored
        # so gap_pair() would be redundant — but it's harmless to call.
        pin = self.pairing_passkey or self._serial_pairing_pin
        if pin:
            try:
                self._ble.gap_pair(conn_handle)
                self._pairing_attempted = True
                log(
                    "RNode BLE pairing initiated (passkey="
                    + "{:06d}".format(pin)
                    + ")",
                    LOG_NOTICE,
                )
            except Exception as e:
                log("RNode BLE pair failed: " + str(e), LOG_WARNING)
        elif self.serial_port and not self._auto_pairing_in_progress:
            # No passkey configured but serial port available — try auto-pairing.
            # We'll connect first, and if pairing fails (which it will since
            # RNode requires MITM), the disconnect handler will trigger
            # auto-pairing via serial.
            log(
                "RNode BLE no passkey configured but serial_port available — "
                "will auto-pair on disconnect if needed",
                LOG_NOTICE,
            )

    def _on_disconnect(self, data):
        """Handle _IRQ_PERIPHERAL_DISCONNECT: lost connection to RNode.

        The official RNode firmware (nRF52) disconnects the client after
        successful pairing. This is by design — after pairing, we must
        reconnect with the stored bond. We detect this case by checking
        if we were in the middle of pairing but hadn't completed service
        discovery yet.
        """
        conn_handle, addr_type, addr = data
        addr_str = ":".join(("%02x" % b) for b in addr)
        log(
            "RNode BLE disconnected (handle="
            + str(conn_handle)
            + ", addr="
            + addr_str
            + ")",
            LOG_NOTICE,
        )

        # Auto-pairing: if pairing failed (not encrypted, service not discovered)
        # and we have a serial port configured, try to obtain the PIN automatically
        # from the RNode's USB serial KISS interface.
        if (
            self._pairing_attempted
            and not self._encrypted
            and not self._service_discovered
            and self.serial_port
            and not self._auto_pairing_in_progress
        ):
            log(
                "RNode BLE pairing failed — attempting auto-pairing via serial",
                LOG_NOTICE,
            )
            self._auto_pairing_in_progress = True
            try:
                if self._serial_pair():
                    # Got a PIN from serial, retry BLE connection quickly
                    self._last_reconnect = time.time() - self.reconnect_delay + 1.0
                else:
                    self._last_reconnect = time.time()
            except Exception as e:
                log("RNode BLE auto-pairing exception: " + str(e), LOG_ERROR)
                self._last_reconnect = time.time()
            self._auto_pairing_in_progress = False
        elif self._pairing_attempted and not self._service_discovered:
            log(
                "RNode BLE disconnected after pairing — bond stored, reconnecting",
                LOG_NOTICE,
            )
            # Short reconnect delay since the bond is already stored
            self._last_reconnect = time.time() - self.reconnect_delay + 1.0
        else:
            self._last_reconnect = time.time()

        self._conn_handle = None
        self._rx_char_handle = None
        self._tx_char_handle = None
        self._service_handle = None
        self._service_discovered = False
        self._char_discovered = False
        self._desc_discovered = False
        self._notify_enabled = False
        self._mtu_exchanged = False
        self._encrypted = False
        self._connecting = False
        self._discovering = False
        self._subscribing = False
        self._cccd_handle = None
        # Don't reset _pairing_attempted — we may still need it on reconnect
        self._detect_sent_time = 0
        self.online = False
        self._detected = False
        self._radio_configured = False

        # Clear KISS state
        self._kiss_buf = bytearray()
        self._in_frame = False
        self._escaping = False
        self._current_cmd = None

        # Clear write queue
        self._write_queue = []

        if not self._shutting_down:
            pass  # _last_reconnect already set above

    def _on_passkey_action(self, action, passkey):
        """Handle _IRQ_PASSKEY_ACTION: respond to BLE pairing challenges.

        The official RNode firmware (RAK4631 nRF52) uses Display Only IO
        capability (setIOCaps(true, false, false)), which means the RNode
        displays a passkey and the client must enter it.

        We configure our IO capability as KEYBOARD_ONLY so the RNode
        knows we can enter a passkey.

        Pairing flow:
          1. User puts RNode into pairing mode (rnodeconf --bluetooth-on or button)
          2. RNode displays a random 6-digit PIN
          3. We call gap_pair() which triggers _IRQ_PASSKEY_ACTION with
             action=_PASSKEY_ACTION_INPUT
          4. We respond with gap_passkey(conn_handle, action, pin) using
             the configured pairing_passkey
          5. After successful pairing, the RNode disconnects us (by design)
          6. We reconnect — the bond is stored, no PIN needed anymore

        For third-party BLE bridges (Heltec V3) that use Just Works,
        no passkey action is triggered.
        """
        log(
            "RNode BLE passkey action: action="
            + str(action)
            + " passkey="
            + str(passkey),
            LOG_NOTICE,
        )

        if action == _PASSKEY_ACTION_INPUT:
            # RNode is displaying a passkey and we must enter it.
            # Use dynamically obtained PIN (from serial auto-pairing) or
            # the statically configured pairing_passkey.
            pin = self.pairing_passkey or self._serial_pairing_pin
            if pin:
                log(
                    "RNode BLE entering passkey: " + "{:06d}".format(pin),
                    LOG_NOTICE,
                )
                self._ble.gap_passkey(self._conn_handle, action, pin)
            else:
                log(
                    "RNode BLE passkey required but no pairing_passkey configured. "
                    "Set pairing_passkey in config or configure serial_port "
                    "for automatic PIN retrieval.",
                    LOG_ERROR,
                )
        elif action == _PASSKEY_ACTION_NUMERIC_CMP:
            # Numeric comparison — both devices show the same number.
            # Auto-confirm since we trust the RNode we specifically scanned for.
            log("RNode BLE numeric comparison: confirming", LOG_NOTICE)
            self._ble.gap_passkey(self._conn_handle, action, 1)
        elif action == _PASSKEY_ACTION_DISPLAY:
            # We should display a passkey for the RNode to accept.
            # Log it for debugging.
            log("RNode BLE display passkey: " + str(passkey), LOG_NOTICE)

    # ==================================================================
    # GATT Service Discovery
    # ==================================================================

    def _on_service_result(self, data):
        """Handle _IRQ_GATTC_SERVICE_RESULT."""
        conn_handle, start_handle, end_handle, uuid = data
        # We filtered by UUID in discover_services, so any result is
        # our target service. Store the handle range immediately.
        self._service_handle = (start_handle, end_handle)
        log(
            "RNode BLE found NUS service (handles "
            + str(start_handle)
            + "-"
            + str(end_handle)
            + ")",
            LOG_NOTICE,
        )

    def _on_service_done(self, data):
        """Handle _IRQ_GATTC_SERVICE_DONE: service enumeration complete."""
        conn_handle, status = data
        self._discovering = False
        self._service_discovered = True  # Mark service discovery as done
        log("RNode BLE service discovery done (status=" + str(status) + ")", LOG_DEBUG)

        if self._service_handle is None:
            log("RNode BLE NUS service not found on device", LOG_ERROR)
            self._disconnect()
            return

        # Start characteristic discovery within the NUS service
        start_handle, end_handle = self._service_handle
        self._char_discovered = False
        self._rx_char_handle = None
        self._tx_char_handle = None
        self._discovering = True

        try:
            # Discover all characteristics in the service range
            self._ble.gattc_discover_characteristics(
                self._conn_handle,
                start_handle,
                end_handle,
            )
        except Exception as e:
            log("RNode BLE char discover failed: " + str(e), LOG_ERROR)
            self._disconnect()

    # ==================================================================
    # Characteristic Discovery
    # ==================================================================

    def _on_char_result(self, data):
        """Handle _IRQ_GATTC_CHARACTERISTIC_RESULT."""
        conn_handle, end_handle, value_handle, properties, uuid = data
        # Per MicroPython docs: uuid is a memoryview only valid during IRQ.
        # bt.UUID(uuid) creates a proper UUID object. str(UUID) returns
        # "UUID('xxxx')" on MicroPython, so we extract the hex string.
        try:
            uuid_obj = bt.UUID(uuid)
            # bt.UUID.hex() returns the raw hex without dashes on MicroPython
            # bt.UUID string format varies — compare using the UUID object directly
        except Exception:
            uuid_obj = None

        log(
            "RNode BLE char result: uuid="
            + str(uuid_obj)
            + " value_h="
            + str(value_handle)
            + " props=0x"
            + ("%02x" % properties),
            LOG_NOTICE,
        )

        # Compare UUID objects directly — MicroPython bt.UUID supports equality
        tx_uuid = bt.UUID(UART_TX_CHAR_UUID)
        rx_uuid = bt.UUID(UART_RX_CHAR_UUID)

        if uuid_obj == rx_uuid:
            self._rx_char_handle = value_handle
            self._rx_char_props = properties
            log(
                "RNode BLE found RX char (handle="
                + str(value_handle)
                + ", props=0x"
                + ("%02x" % properties)
                + ")",
                LOG_NOTICE,
            )
        elif uuid_obj == tx_uuid:
            self._tx_char_handle = value_handle
            self._tx_char_props = properties
            log(
                "RNode BLE found TX char (handle="
                + str(value_handle)
                + ", props=0x"
                + ("%02x" % properties)
                + ")",
                LOG_NOTICE,
            )

    def _on_char_done(self, data):
        """Handle _IRQ_GATTC_CHARACTERISTIC_DONE: characteristic discovery complete."""
        conn_handle, status = data
        self._discovering = False
        log("RNode BLE char discovery done (status=" + str(status) + ")", LOG_DEBUG)

        if self._tx_char_handle is None:
            log("RNode BLE TX characteristic not found", LOG_ERROR)
            self._disconnect()
            return
        if self._rx_char_handle is None:
            log("RNode BLE RX characteristic not found", LOG_ERROR)
            self._disconnect()
            return

        self._char_discovered = True

        # Discover descriptors to find the CCCD for the TX characteristic.
        # We need the CCCD handle to subscribe to notifications.
        self._cccd_handle = None
        self._discovering = True
        try:
            # Discover descriptors in the TX characteristic's range:
            # from (tx_value_handle + 1) to the end of the service or
            # next characteristic. We'll use the end_handle of the service
            # as the upper bound.
            start_desc = self._tx_char_handle + 1
            # Use the service end handle, but cap it at the next char - 1
            # Since we don't know the next char, use the service end handle
            end_desc = self._service_handle[1] if self._service_handle else 0xFFFF
            if start_desc <= end_desc:
                log(
                    "RNode BLE discovering descriptors ("
                    + str(start_desc)
                    + "-"
                    + str(end_desc)
                    + ")",
                    LOG_DEBUG,
                )
                self._ble.gattc_discover_descriptors(
                    self._conn_handle, start_desc, end_desc
                )
            else:
                # No room for descriptors, assume CCCD = tx_handle + 1
                self._cccd_handle = self._tx_char_handle + 1
                self._discovering = False
                log(
                    "RNode BLE no descriptor range, assuming CCCD="
                    + str(self._cccd_handle),
                    LOG_DEBUG,
                )
        except Exception as e:
            log("RNode BLE descriptor discover failed: " + str(e), LOG_ERROR)
            self._disconnect()

    def _on_descriptor_result(self, data):
        """Handle _IRQ_GATTC_DESCRIPTOR_RESULT: found a descriptor."""
        conn_handle, dsc_handle, uuid = data
        try:
            uuid_obj = bt.UUID(uuid)
        except Exception:
            uuid_obj = None

        # CCCD UUID is 0x2902
        if uuid_obj == bt.UUID(0x2902):
            self._cccd_handle = dsc_handle
            log(
                "RNode BLE found CCCD descriptor (handle=" + str(dsc_handle) + ")",
                LOG_NOTICE,
            )
        else:
            log(
                "RNode BLE descriptor: handle="
                + str(dsc_handle)
                + " uuid="
                + str(uuid_obj),
                LOG_DEBUG,
            )

    def _on_descriptor_done(self, data):
        """Handle _IRQ_GATTC_DESCRIPTOR_DONE: descriptor discovery complete."""
        conn_handle, status = data
        self._discovering = False
        self._desc_discovered = True

        if self._cccd_handle is None:
            # Fallback: assume CCCD is at tx_char_handle + 1
            self._cccd_handle = self._tx_char_handle + 1
            log(
                "RNode BLE CCCD not found, assuming handle=" + str(self._cccd_handle),
                LOG_NOTICE,
            )
        log("RNode BLE descriptor discovery done", LOG_DEBUG)

        # _subscribing will be set True when poll_loop calls

    # ==================================================================
    # Notification Subscription
    # ==================================================================

    def _subscribe_notifications(self):
        """Write the CCCD to enable notifications on the TX characteristic.

        Uses write-with-response (mode=1) for the CCCD write to ensure
        the RNode's BLE stack has processed the subscription before we
        send detection bytes. The RNode bridge C++ code triggers
        CMD_READY in its BLE_GAP_EVENT_SUBSCRIBE handler, so we need the
        write to complete before sending data.

        If the CCCD write fails (e.g. insufficient encryption), we
        attempt gap_pair() and retry.
        """
        if not self._ble or self._conn_handle is None or self._tx_char_handle is None:
            return
        if self._cccd_handle is None:
            log("RNode BLE no CCCD handle available", LOG_ERROR)
            self._disconnect()
            return

        try:
            self._ble.gattc_write(
                self._conn_handle,
                self._cccd_handle,
                b"\x01\x00",  # Enable notifications (little-endian)
                1,  # mode=1: write-with-response — ensures RNode processes CCCD before we send data
            )
            self._subscribing = True  # Wait for _on_write_done before proceeding
            log(
                "RNode BLE CCCD write sent (handle="
                + str(self._cccd_handle)
                + "), waiting for response",
                LOG_NOTICE,
            )
        except Exception as e:
            log("RNode BLE CCCD write failed: " + str(e), LOG_ERROR)
            # If encryption error, try pairing first then retry
            if not self._pairing_attempted and not self._encrypted:
                log("RNode BLE attempting pairing before CCCD retry", LOG_NOTICE)
                self._pairing_attempted = True
                try:
                    self._ble.gap_pair(self._conn_handle)
                except Exception as pe:
                    log("RNode BLE pair failed: " + str(pe), LOG_WARNING)
                # Don't disconnect — poll_loop will retry subscribe after encryption
            else:
                self._subscribing = False
                self._disconnect()

    def _on_write_done(self, data):
        """Handle _IRQ_GATTC_WRITE_DONE.

        Triggered by write-with-response (mode=1) calls. For CCCD writes,
        a successful response means the RNode has processed the subscription
        and will begin sending notifications (including CMD_READY).
        """
        conn_handle, value_handle, status = data

        if conn_handle != self._conn_handle:
            return

        if value_handle == self._cccd_handle and self._subscribing:
            # CCCD write response — now we know the RNode has processed our
            # subscription request
            if status == 0:
                log("RNode BLE subscribed to TX notifications", LOG_NOTICE)
                self._notify_enabled = True
                self._subscribing = False
                # Send the detection handshake now that notifications are enabled
                self._send_detection()
                self._detect_sent_time = time.time()
            else:
                log(
                    "RNode BLE CCCD write failed: status=" + str(status),
                    LOG_WARNING,
                )
                # Status 261 (0x105) = insufficient encryption — try pairing
                if not self._pairing_attempted and not self._encrypted:
                    log("RNode BLE pairing and retrying CCCD", LOG_NOTICE)
                    self._pairing_attempted = True
                    try:
                        self._ble.gap_pair(self._conn_handle)
                    except Exception as pe:
                        log("RNode BLE pair failed: " + str(pe), LOG_WARNING)
                    # _subscribing stays True — we'll retry after encryption
                    # or timeout in poll_loop
                else:
                    self._subscribing = False
                    self._disconnect()
        elif status != 0:
            log(
                "RNode BLE write failed: handle="
                + str(value_handle)
                + " status="
                + str(status),
                LOG_WARNING,
            )

    # ==================================================================
    # KISS Protocol — Detection Handshake
    # ==================================================================

    def _send_detection(self):
        """Send the RNode detection command sequence.

        After connecting and enabling notifications, we send:
          [FEND][CMD_DETECT][DETECT_REQ][FEND]
          [FEND][CMD_FW_VERSION][0x00][FEND]
          [FEND][CMD_PLATFORM][0x00][FEND]
          [FEND][CMD_MCU][0x00][FEND]

        Then we wait for response frames to confirm detection.
        """
        log("RNode BLE sending detection handshake", LOG_NOTICE)
        self._detect_retries += 1

        # Build detection command sequence
        detect_frame = bytes(
            [
                FEND,
                CMD_DETECT,
                DETECT_REQ,
                FEND,
                CMD_FW_VERSION,
                0x00,
                FEND,
                CMD_PLATFORM,
                0x00,
                FEND,
                CMD_MCU,
                0x00,
                FEND,
            ]
        )
        self._ble_write_raw(detect_frame)

    def _send_radio_config(self):
        """Send radio configuration commands to the RNode.

        Sends frequency, bandwidth, TX power, spreading factor, and coding rate.
        Then sets radio state to ON.
        """
        log("RNode BLE sending radio configuration", LOG_DEBUG)

        # Frequency: 4 bytes big-endian
        freq_bytes = struct.pack(">I", self.frequency)
        # Bandwidth: 4 bytes big-endian
        bw_bytes = struct.pack(">I", self.bandwidth)

        config_frame = bytearray()
        # CMD_FREQUENCY
        config_frame.extend(bytes([FEND, CMD_FREQUENCY]))
        config_frame.extend(_kiss_escape(freq_bytes))
        config_frame.append(FEND)
        # CMD_BANDWIDTH
        config_frame.extend(bytes([FEND, CMD_BANDWIDTH]))
        config_frame.extend(_kiss_escape(bw_bytes))
        config_frame.append(FEND)
        # CMD_TXPOWER
        config_frame.extend(bytes([FEND, CMD_TXPOWER]))
        config_frame.extend(_kiss_escape(bytes([self.txpower])))
        config_frame.append(FEND)
        # CMD_SF
        config_frame.extend(bytes([FEND, CMD_SF]))
        config_frame.extend(_kiss_escape(bytes([self.spreadingfactor])))
        config_frame.append(FEND)
        # CMD_CR
        config_frame.extend(bytes([FEND, CMD_CR]))
        config_frame.extend(_kiss_escape(bytes([self.codingrate])))
        config_frame.append(FEND)
        # CMD_RADIO_STATE = ON
        config_frame.extend(bytes([FEND, CMD_RADIO_STATE, RADIO_STATE_ON, FEND]))

        self._ble_write_raw(bytes(config_frame))

    # ==================================================================
    # KISS Protocol — Incoming Data Processing
    # ==================================================================

    def _on_notify(self, data):
        """Handle _IRQ_GATTC_NOTIFY: data received from the RNode's TX char."""
        conn_handle, value_handle, notify_data = data

        if value_handle != self._tx_char_handle:
            return

        if not notify_data:
            return

        # Copy memoryview to bytes – data only valid during IRQ handler
        raw_bytes = bytes(notify_data)
        log("RNode BLE notify: " + str(len(raw_bytes)) + " bytes", LOG_NOTICE)
        self._kiss_feed(raw_bytes)

    def _kiss_feed(self, data):
        """Process incoming BLE bytes through the KISS decoder.

        KISS framing:
          FEND command data_bytes... FEND

        Escape sequences inside frames:
          FESC TFESC → 0xDB
          FESC TFEND → 0xC0

        When we receive a complete frame, we dispatch it via
        _kiss_dispatch().
        """
        for b in data:
            if b == FEND:
                # End of frame — dispatch if we have data
                if self._in_frame and len(self._kiss_buf) > 0:
                    self._kiss_dispatch(self._current_cmd, bytes(self._kiss_buf))
                # Reset frame state
                self._in_frame = False
                self._escaping = False
                self._kiss_buf = bytearray()
                self._current_cmd = None
                continue

            if not self._in_frame:
                # Start of frame — first byte after FEND is the command
                self._in_frame = True
                self._current_cmd = b
                self._kiss_buf = bytearray()
                self._escaping = False
                continue

            # Inside frame
            if b == FESC:
                self._escaping = True
                continue

            if self._escaping:
                if b == TFEND:
                    self._kiss_buf.append(0xC0)
                elif b == TFESC:
                    self._kiss_buf.append(0xDB)
                else:
                    # Invalid escape — skip (protocol error, keep going)
                    self._kiss_buf.append(b)
                self._escaping = False
                continue

            self._kiss_buf.append(b)

    def _kiss_dispatch(self, cmd, data):
        """Dispatch a complete KISS frame based on its command byte.

        CMD_DATA: RNS packet data → process_incoming()
        CMD_DETECT: Detection response
        CMD_READY: Radio is ready
        CMD_STAT_RSSI/SNR/BAT: Status reports
        CMD_PLATFORM/MCU/FW_VERSION: Device info responses
        CMD_FREQUENCY/BANDWIDTH/TXPOWER/SF/CR: Config acknowledgements
        CMD_RADIO_STATE: Radio state response
        """
        if cmd == CMD_DATA:
            # RNS packet data
            if data and len(data) > 0:
                self.process_incoming(data)

        elif cmd == CMD_DETECT:
            # Detection response from RNode
            if len(data) > 0 and data[0] == DETECT_ACK:
                self._detected = True
                self._detect_retries = 0
                log("RNode BLE detected (RNode acknowledged)", LOG_NOTICE)
                # Now configure the radio
                self._send_radio_config()
            else:
                log(
                    "RNode BLE detection unexpected response: " + str(list(data)),
                    LOG_WARNING,
                )

        elif cmd == CMD_READY:
            # Radio is ready — we are now fully online
            self._radio_configured = True
            self.online = True
            self._reconnect_count = 0
            self._serial_pairing_attempts = 0
            self._serial_pairing_pin = None
            log("RNode BLE interface ONLINE (radio ready)", LOG_NOTICE)

        elif cmd == CMD_RADIO_STATE:
            if len(data) > 0:
                state = data[0]
                if state == RADIO_STATE_ON:
                    self._radio_configured = True
                    self.online = True
                    self._reconnect_count = 0
                    self._serial_pairing_attempts = 0
                    self._serial_pairing_pin = None
                    log("RNode BLE radio state ON", LOG_NOTICE)
                elif state == RADIO_STATE_OFF:
                    log("RNode BLE radio state OFF", LOG_WARNING)

        elif cmd == CMD_STAT_RSSI:
            if len(data) >= 2:
                # RSSI is sent as 2-byte signed big-endian
                self.rssi = struct.unpack(">h", data[0:2])[0]
                log("RNode BLE RSSI: " + str(self.rssi), LOG_DEBUG)

        elif cmd == CMD_STAT_SNR:
            if len(data) >= 1:
                # SNR is a single signed byte (dB × 4 in some implementations)
                self.snr = struct.unpack(">b", data[0:1])[0]
                log("RNode BLE SNR: " + str(self.snr), LOG_DEBUG)

        elif cmd == CMD_STAT_BAT:
            if len(data) >= 2:
                # Battery level as 2-byte value (percentage or mV)
                self.battery_level = struct.unpack(">H", data[0:2])[0]
                log("RNode BLE battery: " + str(self.battery_level), LOG_DEBUG)

        elif cmd == CMD_PLATFORM:
            if len(data) > 0:
                try:
                    self.rnode_platform = (
                        data.decode("utf-8") if isinstance(data, bytes) else str(data)
                    )
                except Exception:
                    self.rnode_platform = str(list(data))
                log("RNode BLE platform: " + str(self.rnode_platform), LOG_DEBUG)

        elif cmd == CMD_MCU:
            if len(data) > 0:
                try:
                    self.rnode_mcu = (
                        data.decode("utf-8") if isinstance(data, bytes) else str(data)
                    )
                except Exception:
                    self.rnode_mcu = str(list(data))
                log("RNode BLE MCU: " + str(self.rnode_mcu), LOG_DEBUG)

        elif cmd == CMD_FW_VERSION:
            if len(data) >= 2:
                major = data[0]
                minor = data[1]
                self.rnode_fw_version = str(major) + "." + str(minor)
                log("RNode BLE firmware: " + self.rnode_fw_version, LOG_DEBUG)

        elif cmd == CMD_FREQUENCY:
            log("RNode BLE frequency confirmed", LOG_DEBUG)

        elif cmd == CMD_BANDWIDTH:
            log("RNode BLE bandwidth confirmed", LOG_DEBUG)

        elif cmd == CMD_TXPOWER:
            log("RNode BLE TX power confirmed", LOG_DEBUG)

        elif cmd == CMD_SF:
            log("RNode BLE spreading factor confirmed", LOG_DEBUG)

        elif cmd == CMD_CR:
            log("RNode BLE coding rate confirmed", LOG_DEBUG)

        else:
            # Unknown KISS command — log but don't crash
            log("RNode BLE unknown KISS cmd: 0x" + ("%02x" % cmd), LOG_DEBUG)

    # ==================================================================
    # Outgoing Data — KISS Framing
    # ==================================================================

    def process_outgoing(self, data):
        """Send an RNS packet out through the RNode BLE interface.

        Applies IFAC signing, then KISS-frames the packet and queues it
        for transmission in the poll loop.
        """
        if not self.online or self._conn_handle is None or self._rx_char_handle is None:
            log(
                "RNode BLE TX: not connected, dropping " + str(len(data)) + "B",
                LOG_DEBUG,
            )
            return False

        try:
            data = self.ifac_sign(data)

            # KISS-frame the data: FEND + CMD_DATA + escaped_data + FEND
            escaped = _kiss_escape(data)
            frame = bytes([FEND, CMD_DATA]) + escaped + bytes([FEND])

            self._write_queue.append(frame)
            self.txb += len(data)
            self.tx += 1
            self._last_activity = time.time()
            return True
        except Exception as e:
            log("RNode BLE TX error: " + str(e), LOG_ERROR)
            return False

    @property
    def _effective_mtu(self):
        """Effective write payload size = negotiated MTU - 3 bytes ATT overhead."""
        return max(20, self._negotiated_mtu - 3)

    def _ble_write_raw(self, data):
        """Write raw bytes to the BLE RX characteristic (no KISS framing).

        Used for detection handshake and radio config commands that are
        already KISS-framed.
        """
        if self._conn_handle is None or self._rx_char_handle is None:
            log("RNode BLE raw write: not connected", LOG_WARNING)
            return
        log(
            "RNode BLE raw write: "
            + str(len(data))
            + " bytes to RX handle "
            + str(self._rx_char_handle),
            LOG_NOTICE,
        )
        # Send in chunks that fit within the effective BLE MTU
        mtu = self._effective_mtu
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + mtu]
            try:
                self._ble.gattc_write(
                    self._conn_handle,
                    self._rx_char_handle,
                    chunk,
                    0,  # mode=0: write-without-response (no confirmation)
                )
            except Exception as e:
                log("RNode BLE raw write error: " + str(e), LOG_ERROR)
                return
            offset += len(chunk)

    def _ble_write_kiss(self, frame):
        """Write a KISS-framed packet to the BLE RX characteristic.

        Splits the frame into MTU-sized chunks for BLE transmission.
        """
        if self._conn_handle is None or self._rx_char_handle is None:
            return

        mtu = self._effective_mtu
        offset = 0
        while offset < len(frame):
            chunk = frame[offset : offset + mtu]
            try:
                self._ble.gattc_write(
                    self._conn_handle,
                    self._rx_char_handle,
                    chunk,
                    0,  # mode=0: write-without-response (no confirmation)
                )
            except Exception as e:
                log("RNode BLE KISS write error: " + str(e), LOG_ERROR)
                return
            offset += len(chunk)

    # ==================================================================
    # BLE Disconnect / Reconnect
    # ==================================================================

    def _disconnect(self):
        """Disconnect from the RNode."""
        if self._conn_handle is not None and self._ble:
            try:
                self._ble.gap_disconnect(self._conn_handle)
            except:
                pass

    def _reconnect(self):
        """Initiate reconnection to the RNode."""
        self.online = False
        self._detected = False
        self._radio_configured = False
        self._kiss_buf = bytearray()
        self._in_frame = False
        self._escaping = False
        self._write_queue = []

        self._reconnect_count += 1
        self._last_reconnect = time.time()
        log(
            "RNode BLE reconnecting (attempt " + str(self._reconnect_count) + ")",
            LOG_DEBUG,
        )
        self._start_scan()

    # ==================================================================
    # Async Poll Loop
    # ==================================================================

    async def poll_loop(self):
        """Async event loop that manages the BLE connection lifecycle.

        Handles:
          - Scanning for the target RNode device
          - Connecting and discovering services
          - Detection handshake and radio configuration
          - Reconnection on disconnection
          - Processing the outgoing write queue
          - Periodic garbage collection
        """
        import uasyncio as asyncio

        log("RNode BLE poll loop started for " + self.name, LOG_VERBOSE)

        _last_gc = time.time()
        _scan_start = 0
        _write_yield_counter = 0

        while self.enabled and not self._shutting_down:
            try:
                now = time.time()

                # Periodic GC
                if now - _last_gc >= 10:
                    gc.collect()
                    _last_gc = now

                # ----- State: Not connected, not scanning -----
                if (
                    self._conn_handle is None
                    and not self._scanning
                    and not self._connecting
                ):
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

                # ----- State: Scanning -----
                if self._scanning:
                    # Check for timeout
                    if (
                        self._scan_result is False
                        or (now - _scan_start) >= self.scan_timeout
                    ):
                        self._stop_scan()
                        if self._target_addr_found is None:
                            log(
                                "RNode BLE scan: target not found, retrying",
                                LOG_DEBUG,
                            )
                            self._last_reconnect = now
                        continue

                    # Check if we found a target
                    if self._target_addr_found is not None:
                        addr_type, addr_bytes = self._target_addr_found
                        self._stop_scan()
                        log(
                            "RNode BLE connecting to "
                            + ":".join(("%02x" % b) for b in addr_bytes),
                            LOG_VERBOSE,
                        )
                        try:
                            self._ble.gap_connect(addr_type, addr_bytes)
                            self._connecting = True
                        except Exception as e:
                            log("RNode BLE connect failed: " + str(e), LOG_ERROR)
                            self._last_reconnect = now

                    await asyncio.sleep(0.01)
                    continue

                # ----- State: Connecting / Subscribing -----
                if self._connecting or self._subscribing:
                    await asyncio.sleep(0.01)
                    continue

                # ----- State: Connected, wait for encryption (if pairing was triggered) -----
                # We only wait for encryption if we explicitly called gap_pair()
                # after a CCCD write failure. The RNode uses Just Works (no MITM)
                # so encryption may happen automatically without our init.
                if (
                    self._conn_handle is not None
                    and not self._encrypted
                    and self._pairing_attempted
                    and not self._mtu_exchanged
                ):
                    # Wait for encryption to complete after pairing attempt
                    await asyncio.sleep(0.01)
                    continue

                # ----- State: Connected, exchange MTU before service discovery -----
                # Skip encryption wait if we haven't attempted pairing — the
                # RNode uses Just Works and doesn't require encryption for CCCD.
                if (
                    self._conn_handle is not None
                    and not self._mtu_exchanged
                    and not self._discovering
                    and (self._encrypted or not self._pairing_attempted)
                ):
                    try:
                        self._ble.gattc_exchange_mtu(self._conn_handle)
                        log("RNode BLE requesting MTU exchange", LOG_DEBUG)
                    except OSError as e:
                        # EALREADY (errno 120) means MTU was already exchanged
                        # by the BLE stack automatically during connection.
                        # Treat this as success.
                        if getattr(e, "errno", None) == 120 or "EALREADY" in str(e):
                            log("RNode BLE MTU already exchanged (EALREADY)", LOG_DEBUG)
                            self._mtu_exchanged = True
                            self._negotiated_mtu = self._ble.config("mtu")
                        else:
                            log("RNode BLE MTU exchange error: " + str(e), LOG_ERROR)
                    except Exception as e:
                        log("RNode BLE MTU exchange failed: " + str(e), LOG_ERROR)
                    await asyncio.sleep(0.05)
                    continue

                # ----- State: Connected, start GATT discovery (after MTU exchange) -----
                if (
                    self._conn_handle is not None
                    and self._mtu_exchanged
                    and not self._service_discovered
                    and not self._discovering
                ):
                    # MTU exchanged, safe to start service discovery
                    self._discovering = True
                    try:
                        self._ble.gattc_discover_services(
                            self._conn_handle, bt.UUID(UART_SERVICE_UUID)
                        )
                        log("RNode BLE starting service discovery", LOG_DEBUG)
                    except Exception as e:
                        log("RNode BLE service discover failed: " + str(e), LOG_ERROR)
                        self._disconnect()
                    await asyncio.sleep(0.05)
                    continue

                # ----- State: Discovering services/characteristics -----
                if self._discovering:
                    await asyncio.sleep(0.01)
                    continue

                # ----- State: Characteristics and descriptors discovered, subscribe -----
                if (
                    self._conn_handle is not None
                    and self._desc_discovered
                    and not self._notify_enabled
                    and not self._subscribing
                ):
                    self._subscribe_notifications()
                    await asyncio.sleep(0.1)
                    continue

                # ----- State: Subscribing — waiting for CCCD write-done or pairing -----
                if self._subscribing:
                    # If we're waiting for CCCD write response, check for timeout
                    if self._pairing_attempted and not self._encrypted:
                        # Still waiting for encryption after pairing
                        await asyncio.sleep(0.01)
                        continue
                    elif self._pairing_attempted and self._encrypted:
                        # Encryption established after failed CCCD — retry CCCD
                        log(
                            "RNode BLE encryption established, retrying CCCD",
                            LOG_NOTICE,
                        )
                        self._subscribing = False
                        # Fall through to subscribe check on next iteration
                        continue
                    # Timeout: if we've been subscribing for too long, bail
                    await asyncio.sleep(0.01)
                    continue

                # ----- State: Connected but not yet detected/configured -----
                if self._conn_handle is not None and not self._detected:
                    # Detection timeout — retry or reconnect
                    if (
                        self._detect_sent_time > 0
                        and (now - self._detect_sent_time) > 5
                    ):
                        if self._detect_retries < self._max_detect_retries:
                            log("RNode BLE detection timeout, retrying", LOG_WARNING)
                            self._send_detection()
                            self._detect_sent_time = now
                        else:
                            log(
                                "RNode BLE detection failed after max retries",
                                LOG_ERROR,
                            )
                            self._disconnect()
                            self._last_reconnect = now
                    await asyncio.sleep(0.05)
                    continue

                # ----- State: Connected and online -----
                if self.online:
                    # Process outgoing write queue
                    if self._write_queue:
                        frame = self._write_queue.pop(0)
                        self._ble_write_kiss(frame)
                        _write_yield_counter += 1
                        # Yield to event loop periodically to avoid blocking
                        if _write_yield_counter >= 5:
                            _write_yield_counter = 0
                            await asyncio.sleep(0)

                    await asyncio.sleep(0.01)
                    continue

                # ----- State: Disconnected, waiting to reconnect -----
                await asyncio.sleep(0.1)

            except Exception as e:
                log("RNode BLE poll error: " + str(e), LOG_ERROR)
                import sys

                sys.print_exception(e)
                await asyncio.sleep(1)

        log("RNode BLE poll loop EXITED for " + self.name, LOG_ERROR)

    # ==================================================================
    # BLE IRQ Handler
    # ==================================================================

    def _irq(self, event, data):
        """Central BLE IRQ handler — dispatches all BLE events."""
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
            elif event == _IRQ_GATTC_DESCRIPTOR_RESULT:
                self._on_descriptor_result(data)
            elif event == _IRQ_GATTC_DESCRIPTOR_DONE:
                self._on_descriptor_done(data)
            elif event == _IRQ_GATTC_WRITE_DONE:
                self._on_write_done(data)
            elif event == _IRQ_GATTC_NOTIFY:
                self._on_notify(data)
            elif event == _IRQ_MTU_EXCHANGED:
                conn_handle, mtu = data
                if conn_handle == self._conn_handle:
                    self._negotiated_mtu = mtu
                    self._mtu_exchanged = True
                    log("RNode BLE MTU exchanged: " + str(mtu), LOG_DEBUG)
            elif event == _IRQ_ENCRYPTION_UPDATE:
                conn_handle, encrypted, authenticated, bonded, key_size = data
                if conn_handle == self._conn_handle:
                    self._encrypted = encrypted
                    log(
                        "RNode BLE encryption: enc="
                        + str(encrypted)
                        + " bond="
                        + str(bonded)
                        + " key_size="
                        + str(key_size),
                        LOG_NOTICE,
                    )
            elif event == _IRQ_PASSKEY_ACTION:
                conn_handle, action, passkey = data
                if conn_handle == self._conn_handle:
                    self._on_passkey_action(action, passkey)
        except Exception as e:
            log("RNode BLE IRQ error (event=" + str(event) + "): " + str(e), LOG_ERROR)

    def _on_scan_result(self, data):
        """Handle _IRQ_SCAN_RESULT: check if this device matches our target.

        MicroPython ESP32-C6 callback signature:
            addr_type, addr, adv_type, rssi, adv_data = data
        addr and adv_data are memoryview – must copy before leaving IRQ.
        """
        addr_type, addr, adv_type, rssi, adv_data = data
        # Copy memoryview to bytes – required since data is only valid during IRQ
        addr = bytes(addr)
        adv_data = bytes(adv_data)

        if self._target_addr_found is not None:
            return

        if self._match_scan_result(addr_type, addr, adv_data):
            self._target_addr_found = (addr_type, addr)
            self.rssi = rssi

    def _on_scan_done(self, data):
        """Handle _IRQ_SCAN_DONE: scan period ended."""
        self._scanning = False
        if self._target_addr_found is None:
            self._scan_result = False

    # ==================================================================
    # Cleanup
    # ==================================================================

    def close(self):
        """Shutdown the RNode BLE interface."""
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

        self._kiss_buf = bytearray()
        self._write_queue = []
        self._detected = False
        self._radio_configured = False

        super().close()
        log("RNode BLE " + self.name + " closed", LOG_NOTICE)

    def __str__(self):
        return "RNodeBLEInterface[" + self.name + "]"
