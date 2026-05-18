"""
ble_ota.py — BLE OTA relay for AgroNomi gateway

Handles the BLE side of OTA: connecting to a C6 node, sending firmware
chunks over NUS, and reporting results back to the hub via RNS.

This module is called from ble_forwarder.py when an ota_request command
is received from the hub. The firmware binary has already been delivered
via RNS Resource and cached locally by fw_cache.py.

Protocol (matches OTAManager.cpp on C6):
  [0xA0][total_size uint32 LE][fw_version str]  — OTA_BEGIN
  [0xA1][seq uint32 LE][payload...]              — OTA_DATA chunk
  [0xA2][fw_version str]                         — OTA_END
  [0xA3]                                         — OTA_ABORT

ACK from C6 (JSON over NUS):
  {"ota_ok": true, "fw_ver": "1.3.0"}            — success
  {"ota_ok": false, "error": "reason"}           — failure
"""

import asyncio
import json
import os

import RNS

# BLE OTA protocol constants (must match OTAManager.h on C6)
OTA_HDR_BEGIN = 0xA0
OTA_HDR_DATA = 0xA1
OTA_HDR_END = 0xA2
OTA_HDR_ABORT = 0xA3
OTA_CHUNK_SIZE = 241  # NUS MTU 244 - 3 byte NimBLE header

# Retry configuration
OTA_MAX_BLE_RETRIES = 3
OTA_BLE_BACKOFF = [30, 90, 210]  # seconds between retries

# NUS UUIDs
NUS_RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # gateway writes to C6
NUS_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # C6 notifies to gateway


def get_ble_mac(device_id: str, config: dict, cmd: dict | None = None) -> str | None:
    """Look up the BLE MAC address for a device.

    Priority:
    1. The ble_mac field in the command payload (sent by hub from DB)
    2. The gateway's local ble_mac_map config
    """
    # Hub-provided MAC takes priority (comes from DB, always up-to-date)
    if cmd and cmd.get("ble_mac"):
        mac = cmd["ble_mac"]
        if mac and not mac.startswith("AA:BB:CC"):  # skip placeholder MACs
            return mac

    # Fallback to local config map
    ble_map = config.get("ble_mac_map", {})
    return ble_map.get(device_id)


async def handle_ota_command(
    rns_hub_dest,
    cmd: dict,
    firmware_data: bytes,
    gateway_id: str,
    config: dict,
    send_ack_fn=None,
):
    """Orchestrate BLE OTA for a single device.

    Args:
        rns_hub_dest: RNS destination to send ACK back to hub (or None).
        cmd: Parsed ota_request command JSON from hub.
        firmware_data: Verified firmware binary bytes (from cache or RNS Resource).
        gateway_id: This gateway's ID for logging.
        config: Gateway config dict (contains BLE MAC map).
        send_ack_fn: Optional async function(cmd_id, status, fw_version, error)
                     that sends ACK back to hub. If None, ACK is sent via RNS Packet.
    """
    device_id = cmd.get("device_id", "?")
    fw_version = cmd.get(
        "fw_version", cmd.get("cmd_value_text", {}).get("fw_version", "?")
    )
    cmd_id = cmd.get("cmd_id", -1)

    ble_mac = get_ble_mac(device_id, config, cmd)
    if not ble_mac:
        RNS.log(f"[OTA] No BLE MAC for {device_id} — cannot flash", RNS.LOG_ERROR)
        if send_ack_fn:
            send_ack_fn(cmd_id, status="failed", error=f"No BLE MAC for {device_id}")
        else:
            _send_rns_ack(
                rns_hub_dest,
                cmd_id,
                status="failed",
                error=f"No BLE MAC for {device_id}",
            )
        return

    for attempt in range(1, OTA_MAX_BLE_RETRIES + 1):
        RNS.log(
            f"[OTA] BLE attempt {attempt}/{OTA_MAX_BLE_RETRIES} for {device_id}",
            RNS.LOG_INFO,
        )
        success, error = await _ble_ota_attempt(ble_mac, firmware_data, fw_version)

        if success:
            RNS.log(
                f"[OTA] {device_id} flashed successfully → {fw_version}", RNS.LOG_INFO
            )
            if send_ack_fn:
                send_ack_fn(cmd_id, status="acknowledged", fw_version=fw_version)
            else:
                _send_rns_ack(
                    rns_hub_dest, cmd_id, status="acknowledged", fw_version=fw_version
                )
            return

        RNS.log(
            f"[OTA] Attempt {attempt} failed for {device_id}: {error}", RNS.LOG_WARNING
        )

        if attempt < OTA_MAX_BLE_RETRIES:
            backoff = OTA_BLE_BACKOFF[attempt - 1]
            RNS.log(f"[OTA] Retrying {device_id} in {backoff}s...", RNS.LOG_INFO)
            await asyncio.sleep(backoff)

    # All retries exhausted
    RNS.log(
        f"[OTA] {device_id} failed after {OTA_MAX_BLE_RETRIES} attempts", RNS.LOG_ERROR
    )
    if send_ack_fn:
        send_ack_fn(
            cmd_id,
            status="failed",
            error=f"BLE OTA failed after {OTA_MAX_BLE_RETRIES} attempts: {error}",
        )
    else:
        _send_rns_ack(
            rns_hub_dest,
            cmd_id,
            status="failed",
            error=f"BLE OTA failed after {OTA_MAX_BLE_RETRIES} attempts: {error}",
        )


async def _ble_ota_attempt(ble_mac: str, firmware: bytes, fw_version: str) -> tuple:
    """Single BLE OTA attempt to one C6 node.

    Returns (success: bool, error_message: str).
    """
    try:
        from bleak import BleakClient, BleakError
    except ImportError:
        return False, "bleak not installed"

    try:
        async with BleakClient(ble_mac, timeout=20.0) as client:
            if not client.is_connected:
                return False, "BLE connect failed"

            ack_event = asyncio.Event()
            ack_payload = {}

            def on_notify(_, data: bytearray):
                """Called when C6 sends an ACK or NAK via NUS TX notify."""
                try:
                    msg = json.loads(data.decode("utf-8"))
                    ack_payload.update(msg)
                    ack_event.set()
                except Exception:
                    pass  # non-JSON notify (heartbeat etc.)

            await client.start_notify(NUS_TX_UUID, on_notify)

            # --- OTA_BEGIN ---
            total = len(firmware)
            begin_frame = (
                bytes([OTA_HDR_BEGIN])
                + total.to_bytes(4, "little")
                + fw_version.encode("utf-8")
            )
            await client.write_gatt_char(NUS_RX_UUID, begin_frame, response=True)
            await asyncio.sleep(0.15)  # give C6 time to call esp_ota_begin

            # --- DATA CHUNKS ---
            seq = 0
            offset = 0
            while offset < total:
                chunk = firmware[offset : offset + OTA_CHUNK_SIZE]
                frame = bytes([OTA_HDR_DATA]) + seq.to_bytes(4, "little") + chunk
                await client.write_gatt_char(NUS_RX_UUID, frame, response=True)
                offset += len(chunk)
                seq += 1
                # Yield periodically to keep event loop healthy
                if seq % 50 == 0:
                    pct = int(offset / total * 100)
                    RNS.log(f"[OTA] {pct}% ({offset}/{total} bytes)", RNS.LOG_DEBUG)
                    await asyncio.sleep(0)

            # --- OTA_END ---
            end_frame = bytes([OTA_HDR_END]) + fw_version.encode("utf-8")
            await client.write_gatt_char(NUS_RX_UUID, end_frame, response=True)

            # --- Wait for ACK notify from C6 (reboot takes up to 5s) ---
            try:
                await asyncio.wait_for(ack_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                return False, "ACK timeout — C6 may have crashed or rolled back"

            await client.stop_notify(NUS_TX_UUID)

            if ack_payload.get("ota_ok") is True:
                return True, ""
            else:
                err = ack_payload.get("error", "unknown error from C6")
                return False, err

    except Exception as e:
        # Catch BleakError and any other exceptions
        return False, f"BLE error: {e}"


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
