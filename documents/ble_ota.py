"""
ble_ota.py — BLE OTA relay for AgroNomi gateway

Sends firmware OTA frames to the Pico over serial, which relays them
to the sensor over BLE NUS. The Pico is the BLE bridge — it has the
direct BLE connection to the sensor.

Serial OTA protocol (Pi → Pico):
  [OTA_BEGIN]<base64>  — base64(0xA0 + total_size_u32LE + fw_version_str)
  [OTA_DATA]<base64>   — base64(0xA1 + seq_u32LE + payload_bytes)
  [OTA_END]<base64>    — base64(0xA2 + fw_version_str)

The Pico decodes the base64 payload and forwards the raw binary frame
to the sensor via ble.gatts_notify().

ACK from sensor (received on Pico serial as [ACK] JSON line):
  {"ota_ok": true, "fw_ver": "1.3.0"}   — success
  {"ota_ok": false, "error": "reason"}    — failure

Protocol (matches OTAManager.cpp on C6):
  [0xA0][total_size uint32 LE][fw_version str]  — OTA_BEGIN
  [0xA1][seq uint32 LE][payload...]              — OTA_DATA chunk
  [0xA2][fw_version str]                         — OTA_END
  [0xA3]                                         — OTA_ABORT
"""

import base64
import json
import threading
import time

import RNS

# BLE OTA protocol constants (must match OTAManager.h on C6)
OTA_HDR_BEGIN = 0xA0
OTA_HDR_DATA = 0xA1
OTA_HDR_END = 0xA2
OTA_HDR_ABORT = 0xA3
OTA_CHUNK_SIZE = 241  # Must match OTAManager.h OTA_CHUNK_SIZE

# Retry configuration
# SN-AIR wakes every 300s (5 min), advertises for ~7s per wake cycle.
# Retries are aligned to catch the sensor during its advertising window.
OTA_MAX_BLE_RETRIES = 8
OTA_BLE_BACKOFF = [
    5,
    30,
    90,
    300,
    300,
    300,
    300,
    300,
]  # aligned with sensor 5-min cycle

# Timeout for OTA ACK from sensor (received as [ACK] line on serial)
OTA_ACK_TIMEOUT = 60  # seconds — sensor reboot can take up to 30s

# Inter-chunk delay: the Pico's BLE NUS has a limited TX buffer and
# the ESP32-C6's NimBLE stack can only queue a few notifications at a
# time. Without pacing, gatts_notify() calls overwhelm the BLE stack
# and the sensor's NimBLE callback drops frames. 10ms per chunk gives
# the BLE stack time to transmit each notification before queuing the
# next one, and matches the ~241-byte chunk at ~24KB/s BLE throughput.
OTA_CHUNK_DELAY = 0.010  # seconds between OTA_DATA frames

# Post-BEGIN delay: esp_ota_begin() on ESP32-C6 must erase the OTA
# partition flash before it can accept writes. This takes 1-2 seconds
# depending on partition size. The sensor's handleNusNotify callback
# calls beginBLE() synchronously, and the NimBLE stack blocks until it
# returns — so the Pico can't send more frames during this window.
# A 3-second delay ensures the sensor is ready to receive data chunks.
OTA_BEGIN_DELAY = 3.0  # seconds after OTA_BEGIN before sending chunks


def get_ble_mac(device_id: str, config: dict, cmd: dict | None = None) -> str | None:
    """Look up the BLE MAC address for a device.

    Priority:
    1. The ble_mac field in the command payload (sent by hub from DB)
    2. The gateway's local ble_mac_map config

    NOTE: The MAC is not used for direct BLE connection anymore (that
    was the old bleak approach). It's kept for logging/identification.
    """
    # Hub-provided MAC takes priority (comes from DB, always up-to-date)
    if cmd and cmd.get("ble_mac"):
        mac = cmd["ble_mac"]
        if mac and not mac.startswith("AA:BB:CC"):  # skip placeholder MACs
            return mac

    # Fallback to local config map
    ble_map = config.get("ble_mac_map", {})
    return ble_map.get(device_id)


class OtaAckWaiter:
    """Thread-safe waiter for OTA ACK from the sensor.

    The ACK arrives as a [ACK] JSON line on the Pico serial port,
    which ble_forwarder.py reads in its serial loop. When an ota_ok
    ACK is received, ble_forwarder calls set() on this waiter.
    """

    def __init__(self):
        self._event = threading.Event()
        self._result = None  # dict from ACK JSON

    def wait(self, timeout: float) -> dict | None:
        """Block until ACK received or timeout. Returns ACK dict or None."""
        if self._event.wait(timeout=timeout):
            return self._result
        return None

    def set(self, result: dict):
        """Called by ble_forwarder when [ACK] ota_ok is received."""
        self._result = result
        self._event.set()

    def reset(self):
        """Reset for a new OTA attempt."""
        self._event.clear()
        self._result = None


def send_ota_frames_via_serial(
    ser,
    firmware_data: bytes,
    fw_version: str,
    ack_waiter: OtaAckWaiter,
    device_id: str = "?",
    pico_connected_check=None,
) -> tuple[bool, str]:
    """Send OTA firmware frames to the sensor via Pico serial relay.

    This replaces the old bleak-based direct BLE connection. The Pico
    already has a BLE connection to the sensor (via NUS). We send OTA
    frames over serial to the Pico, which decodes and relays them.

    Args:
        ser: serial.Serial port connected to Pico
        firmware_data: Verified firmware binary bytes
        fw_version: Target firmware version string (e.g. "1.4.0")
        ack_waiter: OtaAckWaiter instance for receiving sensor ACK
        device_id: Device ID for logging
        pico_connected_check: Callable returning True if Pico has BLE connection

    Returns:
        (success: bool, error_message: str)
    """
    try:
        ack_waiter.reset()

        total = len(firmware_data)
        RNS.log(
            f"[OTA] Sending {total} bytes for {device_id} → {fw_version} via Pico serial",
            RNS.LOG_INFO,
        )

        # --- OTA_BEGIN ---
        begin_frame = (
            bytes([OTA_HDR_BEGIN])
            + total.to_bytes(4, "little")
            + fw_version.encode("utf-8")
        )
        line = f"[OTA_BEGIN]{base64.b64encode(begin_frame).decode('ascii')}\n"
        ser.write(line.encode("utf-8"))
        ser.flush()
        RNS.log(f"[OTA] Sent OTA_BEGIN ({total} bytes, v{fw_version})", RNS.LOG_INFO)
        time.sleep(OTA_BEGIN_DELAY)  # give sensor time to call esp_ota_begin

        # --- OTA_DATA chunks ---
        seq = 0
        offset = 0
        while offset < total:
            # Check if sensor is still connected before each chunk
            if pico_connected_check and not pico_connected_check():
                return False, "BLE disconnected during OTA transfer"

            chunk = firmware_data[offset : offset + OTA_CHUNK_SIZE]
            frame = bytes([OTA_HDR_DATA]) + seq.to_bytes(4, "little") + chunk
            line = f"[OTA_DATA]{base64.b64encode(frame).decode('ascii')}\n"
            try:
                ser.write(line.encode("utf-8"))
                ser.flush()
            except OSError as e:
                return False, f"Serial write error: {e}"
            offset += len(chunk)
            seq += 1

            # Pace BLE transmissions: the Pico's gatts_notify() queues
            # BLE notifications and the sensor's NimBLE callback must
            # process each frame (including esp_ota_write flash operations)
            # before the next one arrives. Without this delay, the BLE
            # TX buffer overflows and frames are silently dropped.
            time.sleep(OTA_CHUNK_DELAY)

            if seq % 100 == 0:
                pct = int(offset / total * 100)
                RNS.log(f"[OTA] {pct}% ({offset}/{total} bytes)", RNS.LOG_INFO)

        RNS.log(
            f"[OTA] Sent all {seq} chunks ({total} bytes), sending OTA_END",
            RNS.LOG_INFO,
        )

        # --- OTA_END ---
        end_frame = bytes([OTA_HDR_END]) + fw_version.encode("utf-8")
        line = f"[OTA_END]{base64.b64encode(end_frame).decode('ascii')}\n"
        try:
            ser.write(line.encode("utf-8"))
            ser.flush()
        except OSError as e:
            return False, f"Serial write error on OTA_END: {e}"

        # --- Wait for ACK from sensor (relayed via Pico serial as [ACK]) ---
        RNS.log(
            f"[OTA] Waiting for sensor ACK (timeout={OTA_ACK_TIMEOUT}s)...",
            RNS.LOG_INFO,
        )
        result = ack_waiter.wait(timeout=OTA_ACK_TIMEOUT)

        if result is None:
            return False, "ACK timeout — sensor may have crashed or rolled back"

        if result.get("ota_ok") is True:
            fw_ver = result.get("fw_ver", fw_version)
            RNS.log(f"[OTA] {device_id} flashed successfully → {fw_ver}", RNS.LOG_INFO)
            return True, ""
        else:
            err = result.get("error", "unknown error from sensor")
            return False, err

    except Exception as e:
        return False, f"Serial OTA error: {e}"


def _send_rns_ack(
    hub_dest, cmd_id: int, status: str, fw_version: str = None, error: str = None
):
    """Send OTA result ACK back to hub via RNS Packet.

    This is a fallback for when we don't have a Link to the hub —
    uses a simple Packet to the farm.commands_control destination.
    """
    payload = {"cmd_id": cmd_id, "status": status}
    if fw_version:
        payload["fw_version"] = fw_version
    if error:
        payload["error"] = error

    data = json.dumps(payload).encode("utf-8")

    # If hub_dest is an RNS.Destination, send directly
    if hub_dest is not None and hasattr(hub_dest, "send"):
        try:
            pkt = RNS.Packet(hub_dest, data)
            pkt.send()
            RNS.log(
                f"[OTA] Sent ACK to hub: cmd_id={cmd_id} status={status}", RNS.LOG_INFO
            )
        except Exception as e:
            RNS.log(f"[OTA] Failed to send ACK to hub: {e}", RNS.LOG_WARNING)
    else:
        RNS.log(
            f"[OTA] No hub destination available for ACK: cmd_id={cmd_id}",
            RNS.LOG_WARNING,
        )
