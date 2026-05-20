"""BLE Interface for MicroReticulum ESP32-C6 gateway

Provides a BLE GATT server (Nordic UART Service compatible) that accepts
write requests from connected clients and forwards received bytes to the
packet handler.  Also supports sending notifications back to clients.

Follows MicroPython BLE best practices from the official docs:
  - Register IRQ handler before active(True) to catch early events
  - Save value handles from gatts_register_services return value
  - Set adequate buffer sizes for the NUS characteristics
  - Handle MTU exchange events for proper payload sizing
  - Restart advertising on disconnect so new clients can connect

Reference: https://docs.micropython.org/en/latest/library/bluetooth.html
"""

import struct
import time

import ubluetooth as bt
from micropython import const

# ---------------------------------------------------------------------------
# IRQ event codes — per official MicroPython bluetooth module docs
# https://docs.micropython.org/en/latest/library/bluetooth.html
# ---------------------------------------------------------------------------
_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)
_IRQ_GATTS_READ_REQUEST = const(4)
_IRQ_MTU_EXCHANGED = const(21)

# ---------------------------------------------------------------------------
# Default UUIDs (Nordic UART Service — widely supported by BLE clients)
# ---------------------------------------------------------------------------
DEFAULT_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
DEFAULT_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # Notify FROM server
DEFAULT_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # Write TO server

# Default BLE ATT MTU (negotiated MTU may be larger)
_DEFAULT_MTU = 23


def advertising_payload(name=None, services=None):
    """Create BLE advertising payload.

    Args:
        name:     Optional device name string (AD type 0x09).
        services: Optional list of bt.UUID objects to advertise.
                  16-bit UUIDs use AD type 0x03 (complete list).
                  128-bit UUIDs use AD type 0x07 (complete list).

    Returns:
        bytes: The encoded advertising payload.
    """
    payload = bytearray()
    if name:
        name_bytes = name.encode()
        payload += struct.pack("BB", len(name_bytes) + 1, 0x09) + name_bytes
    if services:
        for uuid in services:
            b = bytes(uuid)
            if len(b) == 2:
                # 16-bit UUID: AD type 0x03 (Complete List of 16-bit Service UUIDs)
                payload += struct.pack("BB", len(b) + 1, 0x03) + b
            elif len(b) == 16:
                # 128-bit UUID: AD type 0x07 (Complete List of 128-bit Service UUIDs)
                payload += struct.pack("BB", len(b) + 1, 0x07) + b
    return bytes(payload)


class BLEInterface:
    """BLE GATT server interface for µReticulum.

    Advertises a Nordic UART Service (NUS) compatible GATT service:
      - TX characteristic (notify): server sends data TO the client
      - RX characteristic (write): client sends data TO the server

    When a client writes to the RX characteristic, the received bytes
    are forwarded to the user-provided packet_handler callback.

    Usage:
        iface = BLEInterface(packet_handler=my_handler, name="SN-AIR-01")
        # ... later, to send data to the connected client:
        iface.notify(b"hello")
    """

    def __init__(
        self,
        packet_handler,
        name="BLEGateway",
        service_uuid=DEFAULT_SERVICE_UUID,
        tx_char_uuid=DEFAULT_TX_CHAR_UUID,
        rx_char_uuid=DEFAULT_RX_CHAR_UUID,
    ):
        """Initialise the BLE GATT server.

        Args:
            packet_handler: Callable(data, packet) called when a client writes data.
            name:           BLE advertising name (shown to scanning devices).
            service_uuid:   128-bit UUID for the GATT service.
            tx_char_uuid:   128-bit UUID for the TX (notify) characteristic.
            rx_char_uuid:   128-bit UUID for the RX (write) characteristic.
        """
        self._handler = packet_handler
        self._service_uuid = service_uuid
        self._tx_char_uuid = tx_char_uuid
        self._rx_char_uuid = rx_char_uuid
        self._name = name
        self._connections = set()
        self._mtu = _DEFAULT_MTU
        self._tx_handle = None  # Value handle for TX characteristic (notify)
        self._rx_handle = None  # Value handle for RX characteristic (write)

        # Initialise BLE — per MicroPython docs, register IRQ before active(True)
        # so we don't miss early connection events.
        self._ble = bt.BLE()
        self._ble.irq(self._irq_handler)
        self._ble.active(True)

        # Register GATT services — must be done after active(True)
        self._register_services()

        # Start advertising
        self._advertise()

    # ---------------------------------------------------------------------
    # GATT service registration
    # ---------------------------------------------------------------------
    def _register_services(self):
        """Register the NUS GATT service and characteristics.

        Per MicroPython docs, gatts_register_services returns a nested tuple
        of value handles: ((tx_handle, rx_handle),) for one service with two
        characteristics.

        The TX characteristic supports READ + NOTIFY (server pushes data).
        The RX characteristic supports WRITE + WRITE_NO_RESPONSE (client
        pushes data).
        """
        svc = bt.UUID(self._service_uuid)

        # TX characteristic: server sends data TO the client via notifications
        # Client must subscribe (write CCCD = 0x0001) to receive them
        tx_char = (
            bt.UUID(self._tx_char_uuid),
            bt.FLAG_READ | bt.FLAG_NOTIFY,
        )

        # RX characteristic: client sends data TO the server via writes
        rx_char = (
            bt.UUID(self._rx_char_uuid),
            bt.FLAG_WRITE | bt.FLAG_WRITE_NO_RESPONSE,
        )

        # Service tuple: (service_uuid, (characteristic_1, characteristic_2, ...))
        service_spec = (svc, (tx_char, rx_char))

        # Register — returns handles: ((tx_value_handle, rx_value_handle),)
        handles = self._ble.gatts_register_services((service_spec,))
        self._tx_handle = handles[0][0]
        self._rx_handle = handles[0][1]

        # Per MicroPython docs: "useful when implementing something like the
        # Nordic UART Service" — set buffer sizes to allow larger writes.
        # RX buffer: append=True so each write appends to a buffer we can read.
        # TX buffer: size = MTU target so gatts_notify can send up to (MTU-3) bytes.
        self._ble.gatts_set_buffer(self._rx_handle, 512, True)
        self._ble.gatts_set_buffer(self._tx_handle, 247, False)

        # Write initial value for TX so reads return something (not mandatory
        # but avoids spurious read errors from some clients)
        self._ble.gatts_write(self._tx_handle, b"\x00")

    # ---------------------------------------------------------------------
    # BLE IRQ handler
    # ---------------------------------------------------------------------
    def _irq_handler(self, event, data):
        """Central BLE IRQ handler.

        Per MicroPython docs, the IRQ handler must be lightweight.  We
        read data with gatts_read() inside the handler because the data
        is only valid during the IRQ callback.
        """
        if event == _IRQ_CENTRAL_CONNECT:
            # A central (client) connected to our GATT server
            conn_handle, addr_type, addr = data
            self._connections.add(conn_handle)
            # Stop advertising while connected (per BLE etiquette)
            self._ble.gap_advertise(None)
            print("[BLE] Central connected: handle=" + str(conn_handle))

        elif event == _IRQ_CENTRAL_DISCONNECT:
            # A central disconnected — restart advertising
            conn_handle, addr_type, addr = data
            self._connections.discard(conn_handle)
            print("[BLE] Central disconnected: handle=" + str(conn_handle))
            self._advertise()

        elif event == _IRQ_GATTS_WRITE:
            # A client wrote to one of our characteristics
            conn_handle, value_handle = data

            if value_handle == self._rx_handle:
                # Per MicroPython docs: "memoryview fields are only valid
                # during the invocation of the IRQ handler."  Read the
                # value inside the handler and copy it to bytes.
                try:
                    raw = self._ble.gatts_read(value_handle)
                    # gatts_read returns bytes or memoryview — copy to bytes
                    if not isinstance(raw, bytes):
                        raw = bytes(raw)
                except Exception as exc:
                    print("[BLE] gatts_read error: " + str(exc))
                    return

                # Forward the raw bytes to the gateway's packet handler
                try:
                    self._handler(raw, None)
                except Exception as exc:
                    # Never let an exception in the handler crash the BLE stack
                    print("[BLE] packet handler error: " + str(exc))

        elif event == _IRQ_GATTS_READ_REQUEST:
            # A client wants to read a characteristic — accept by returning 0
            # Per docs: "Return 0 to accept the read"
            conn_handle, value_handle = data
            # Return 0 is implicit (we don't block)

        elif event == _IRQ_MTU_EXCHANGED:
            # ATT MTU exchange completed — track the negotiated MTU so
            # we know the maximum payload size for notifications.
            conn_handle, mtu = data
            self._mtu = mtu
            print(
                "[BLE] MTU exchanged: " + str(mtu) + " (payload=" + str(mtu - 3) + ")"
            )

    # ---------------------------------------------------------------------
    # Advertising
    # ---------------------------------------------------------------------
    def _advertise(self, interval_us=500000):
        """Start BLE advertising with the configured name and service UUID.

        Args:
            interval_us: Advertising interval in microseconds (default 500ms).
        """
        adv_payload = advertising_payload(
            name=self._name, services=[bt.UUID(self._service_uuid)]
        )
        # Include scan-response data with the device name (appears in
        # scan results on phones/tablets)
        resp_payload = advertising_payload(name=self._name)
        self._ble.gap_advertise(
            interval_us, adv_data=adv_payload, resp_data=resp_payload
        )
        print("[BLE] advertising started as '" + self._name + "'")

    # ---------------------------------------------------------------------
    # Send data to connected clients
    # ---------------------------------------------------------------------
    def notify(self, data, conn_handle=None):
        """Send a notification to connected client(s).

        Args:
            data:        bytes to send (must fit within MTU-3 payload).
            conn_handle: Specific connection to notify. If None, notify all.
        """
        if self._tx_handle is None:
            print("[BLE] notify: TX handle not registered yet")
            return

        targets = [conn_handle] if conn_handle is not None else list(self._connections)
        for conn in targets:
            try:
                self._ble.gatts_notify(conn, self._tx_handle, data)
            except Exception as exc:
                print("[BLE] notify error (handle=" + str(conn) + "): " + str(exc))

    @property
    def mtu(self):
        """Return the currently negotiated MTU (default 23 before exchange)."""
        return self._mtu

    @property
    def max_notify_size(self):
        """Return the maximum notification payload size (MTU - 3 bytes ATT header)."""
        return max(self._mtu - 3, 20)

    @property
    def is_connected(self):
        """Return True if at least one client is connected."""
        return len(self._connections) > 0

    @property
    def connections(self):
        """Return a copy of the current connection handle set."""
        return set(self._connections)

    def close(self):
        """Shutdown the BLE interface cleanly."""
        # Stop advertising
        try:
            self._ble.gap_advertise(None)
        except Exception:
            pass

        # Disconnect any connected clients
        for conn in list(self._connections):
            try:
                self._ble.gap_disconnect(conn)
            except Exception:
                pass
        self._connections.clear()

        # Deactivate BLE
        try:
            self._ble.active(False)
        except Exception:
            pass

        print("[BLE] interface closed")

    def __str__(self):
        return "BLEInterface[" + self._name + "]"
