"""µReticulum — Soil Node Firmware (SN-SOIL-01)

MicroPython firmware for a capacitive-soil-moisture + DS18B20 + battery
sensor node that communicates over µReticulum (LoRa / WiFi / BLE).

Lifecycle (each wake cycle):
  1. Boot → connect WiFi (if configured)
  2. Initialise µReticulum + interfaces
  3. Read all sensors
  4. Build JSON telemetry payload
  5. Announce on the command channel (with app_data)
  6. Send telemetry to the hub's SINGLE destination
  7. Listen briefly for inbound commands
  8. Deep sleep for SLEEP_INTERVAL_SEC

The telemetry JSON format is backward-compatible with the BLE gateway:
  {
    "dev_id": "SN-SOIL-01",
    "device_type": "soil_node",
    "fw_ver": "2.0.0-mr",
    "gateway_id": "SN-SOIL-01",
    "readings": {
      "soil_moisture_pct": 42.5,
      "soil_temp_c": 22.3,
      "soil_temp_valid": true
    },
    "bat_v": 3.87,
    "rns_interface": "lora"
  }
"""

import gc
import json
import sys
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
import config
import machine

# ---------------------------------------------------------------------------
# Sensor drivers
# ---------------------------------------------------------------------------
from sensors import read_all, read_ds18b20

# ---------------------------------------------------------------------------
# µReticulum
# ---------------------------------------------------------------------------
from urns import Reticulum
from urns.destination import Destination
from urns.identity import Identity
from urns.packet import Packet

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg, level=1):
    """Simple level-gated logger."""
    if config.DEBUG >= level:
        print("[SN-SOIL] " + str(msg))


def _get_rns_interface_name(rns):
    """Return a human-readable name for the first active interface, or 'none'."""
    for iface in rns.interfaces:
        if hasattr(iface, "online") and iface.online:
            return getattr(iface, "name", iface.__class__.__name__).lower()
    return "none"


def _connect_wifi(ssid, password, timeout=15):
    """Connect to WiFi. Returns IP address string or raises RuntimeError."""
    import network

    wlan = network.WLAN(network.STA_IF)
    wlan.active(False)
    time.sleep(0.1)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        start = time.ticks_ms()
        while not wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), start) // 1000 > timeout:
                wlan.active(False)
                raise RuntimeError("WiFi connection timed out")
            time.sleep(0.2)
    return wlan.ifconfig()[0]


def _find_or_create_identity(storage_path):
    """Load a persisted RNS identity or create a new one."""
    from urns.identity import Identity

    Identity.storagepath = storage_path
    identity_path = storage_path + "/identity"
    try:
        ident = Identity.from_file(identity_path)
        if ident:
            _log("Loaded identity: " + ident.hexhash)
            return ident
    except Exception:
        pass
    ident = Identity()
    try:
        ident.to_file(identity_path)
        _log("Created new identity: " + ident.hexhash)
    except Exception as e:
        _log("Warning: could not persist identity: " + str(e))
    return ident


# ---------------------------------------------------------------------------
# Command handler — receives commands on the IN destination
# ---------------------------------------------------------------------------


def _on_command(data, packet):
    """Handle an inbound command packet on the command destination.

    Expected JSON payload:
        {"cmd": "pump_on"}   or   {"cmd": "pump_off"}

    The handler ACKs by sending a reply back to the source.
    """
    try:
        payload = json.loads(
            data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
        )
        cmd = payload.get("cmd", "")
        _log("Command received: " + cmd, 1)

        if cmd == "pump_on":
            _log("Pump ON (acknowledged)", 1)
            # TODO: drive pump relay GPIO
        elif cmd == "pump_off":
            _log("Pump OFF (acknowledged)", 1)
            # TODO: drive pump relay GPIO
        else:
            _log("Unknown command: " + cmd, 1)

        # Send ACK back through µR
        _send_ack(cmd, packet)

    except Exception as e:
        _log("Command parse error: " + str(e), 1)


def _send_ack(cmd, original_packet):
    """Send a simple JSON ACK back to the source of a command."""
    try:
        ack_payload = json.dumps(
            {
                "type": "ack",
                "cmd": cmd,
                "dev_id": config.NODE_NAME,
                "status": "ok",
            }
        ).encode("utf-8")

        # Build an OUT destination pointing back at the sender
        # The source hash from the incoming packet tells us who sent it.
        source_hash = original_packet.destination_hash if original_packet else None
        if source_hash is None:
            _log("Cannot ACK: no source hash", 1)
            return

        # Create a single-use OUT destination for the reply.
        # We don't have the sender's identity so we send PLAIN (unencrypted)
        # to the hash.  In a production deployment you would use a Link.
        from urns.destination import Destination as D

        reply_dest = D(None, D.OUT, D.PLAIN, config.COMMAND_APP, config.COMMAND_ASPECT)
        reply_dest.hash = source_hash

        pkt = Packet(reply_dest, ack_payload, Packet.DATA)
        pkt.send()
        _log("ACK sent for: " + cmd)

    except Exception as e:
        _log("ACK send failed: " + str(e), 1)


# ---------------------------------------------------------------------------
# Announce handler — discovers the hub on the command channel
# ---------------------------------------------------------------------------

_hub_identity = None


def _on_announce(destination_hash, app_data, packet):
    """Callback when an announce is received on the command channel.

    If the app_data contains our expected hub identifier, we record the
    identity so we can address telemetry to it.
    """
    global _hub_identity
    if app_data is None:
        return

    try:
        data_str = (
            app_data.decode("utf-8")
            if isinstance(app_data, (bytes, bytearray))
            else str(app_data)
        )
        _log("Announce from " + destination_hash.hex()[:8] + ": " + data_str, 2)

        # Accept any announce on the command channel as a potential hub
        # (in production, filter by known hub names / signing key)
        if _hub_identity is None:
            # Try to recall the full identity from the announce
            ident = Identity.recall(destination_hash)
            if ident is not None:
                _hub_identity = ident
                _log("Hub discovered: " + ident.hexhash)

    except Exception as e:
        _log("Announce handler error: " + str(e), 2)


# ---------------------------------------------------------------------------
# Telemetry payload builder
# ---------------------------------------------------------------------------


def _build_telemetry(readings, interface_name):
    """Build the JSON telemetry dict matching the legacy BLE format.

    The `gateway_id` field now contains the device's own name because
    the node speaks RNS directly (no separate BLE gateway).

    The `rns_interface` field is NEW — tells the hub which transport was used.
    """
    return {
        "dev_id": config.NODE_NAME,
        "device_type": config.DEVICE_TYPE,
        "fw_ver": config.FIRMWARE_VERSION,
        "gateway_id": config.NODE_NAME,
        "readings": {
            "soil_moisture_pct": readings["soil_moisture_pct"],
            "soil_temp_c": readings["soil_temp_c"],
            "soil_temp_valid": readings["soil_temp_valid"],
        },
        "bat_v": readings["bat_v"],
        "rns_interface": interface_name,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main():
    """Soil node main — runs once per wake cycle, then deep sleeps."""
    global _hub_identity

    gc.collect()
    _log("=" * 40)
    _log("SN-SOIL-01 boot — µReticulum v" + config.FIRMWARE_VERSION)
    _log("=" * 40)

    # ------------------------------------------------------------------
    # 1. Connect WiFi if any WiFi interfaces are configured
    # ------------------------------------------------------------------
    wifi_interfaces = [
        i
        for i in config.CONFIG.get("interfaces", [])
        if i.get("enabled", True)
        and i.get("type", "") in ("UDPInterface", "TCPClientInterface")
    ]
    if wifi_interfaces and config.WIFI_SSID:
        try:
            ip = _connect_wifi(config.WIFI_SSID, config.WIFI_PASS)
            _log("WiFi connected — IP: " + ip)
        except Exception as e:
            _log("WiFi failed: " + str(e))
            # Non-fatal: we may have LoRa

    # ------------------------------------------------------------------
    # 2. Initialise µReticulum
    # ------------------------------------------------------------------
    try:
        rns = Reticulum(loglevel={0: 0, 1: 0, 2: 2}.get(config.DEBUG, 0))
        rns.config = config.CONFIG

        # Set up identity with persistent storage
        storage = rns.storagepath
        ident = _find_or_create_identity(storage)
        rns.identity = ident

        # Start interfaces (LoRa, BLE, UDP, etc.)
        rns.setup_interfaces()

        _log("µReticulum initialised — identity: " + ident.hexhash)

    except Exception as e:
        _log("FATAL: µReticulum init failed: " + str(e))
        # Nothing we can do without RNS — sleep and retry
        _deep_sleep()
        return

    # ------------------------------------------------------------------
    # 3. Set up RNS destinations
    # ------------------------------------------------------------------

    # IN destination: receive commands (SINGLE, so we can announce it)
    cmd_dest = Destination(
        ident,
        Destination.IN,
        Destination.SINGLE,
        config.COMMAND_APP,
        config.COMMAND_ASPECT,
    )
    cmd_dest.set_proof_strategy(Destination.PROVE_ALL)
    cmd_dest.set_packet_callback(_on_command)
    cmd_dest._announce_handler = _on_announce

    # OUT destination: send telemetry to the hub
    # We create a PLAIN destination because we may not know the hub's
    # identity yet (discovered via announce).  Once we have the identity,
    # we can create a proper SINGLE OUT destination.
    telemetry_app = config.TELEMETRY_APP
    telemetry_aspect = config.TELEMETRY_ASPECT

    _log("Command dest: " + str(cmd_dest))
    _log("Telemetry target: " + telemetry_app + "." + telemetry_aspect)

    # ------------------------------------------------------------------
    # 4. Read sensors
    # ------------------------------------------------------------------
    _log("Reading sensors...")
    readings = read_all(config)

    if config.DEBUG >= 1:
        _log("Soil moisture: {:.1f}%".format(readings["soil_moisture_pct"]))
        _log(
            "Soil temp:     {:.1f}°C (valid={})".format(
                readings["soil_temp_c"], readings["soil_temp_valid"]
            )
        )
        _log("Battery:        {:.2f}V".format(readings["bat_v"]))

    # ------------------------------------------------------------------
    # 5. Announce ourselves on the command channel
    # ------------------------------------------------------------------
    app_data = (config.RNS_ANNOUNCE_PREFIX + ":" + config.NODE_NAME).encode("utf-8")
    cmd_dest.announce(app_data=app_data)
    _log("Announced on " + config.COMMAND_APP + "." + config.COMMAND_ASPECT)

    # ------------------------------------------------------------------
    # 6. Build & send telemetry
    # ------------------------------------------------------------------
    iface_name = _get_rns_interface_name(rns)
    telemetry = _build_telemetry(readings, iface_name)
    payload = json.dumps(telemetry).encode("utf-8")

    # Determine the outbound destination:
    #   a) If we discovered the hub identity via announce, use SINGLE OUT
    #   b) Otherwise, use PLAIN OUT to the well-known telemetry aspect
    if _hub_identity is not None:
        tx_dest = Destination(
            _hub_identity,
            Destination.OUT,
            Destination.SINGLE,
            telemetry_app,
            telemetry_aspect,
        )
        _log("Telemetry → SINGLE dest (hub identity)")
    else:
        tx_dest = Destination(
            None,
            Destination.OUT,
            Destination.PLAIN,
            telemetry_app,
            telemetry_aspect,
        )
        _log("Telemetry → PLAIN dest (no hub identity yet)")

    pkt = Packet(tx_dest, payload, Packet.DATA)
    receipt = pkt.send()

    if receipt:
        _log("Telemetry sent (" + str(len(payload)) + " bytes)")
    else:
        _log("Telemetry send FAILED (no interface online?)")

    # ------------------------------------------------------------------
    # 7. Listen briefly for inbound commands
    # ------------------------------------------------------------------
    _log("Listening for commands (5 s)...")
    time.sleep(5)

    # ------------------------------------------------------------------
    # 8. Deep sleep
    # ------------------------------------------------------------------
    _deep_sleep()


def _deep_sleep():
    """Enter deep sleep for the configured interval, or spin-wait in debug."""
    if config.ENABLE_DEEPSLEEP:
        _log("Deep sleeping for {} s...".format(config.SLEEP_INTERVAL_SEC))
        time.sleep_ms(100)  # flush UART
        machine.deepsleep(config.SLEEP_INTERVAL_SEC * 1000)
    else:
        _log("DEBUG MODE — no deep sleep. Reset to refresh readings.")
        while True:
            time.sleep(1)


# ---------------------------------------------------------------------------
# Auto-run on import (MicroPython entry point)
# ---------------------------------------------------------------------------
main()
