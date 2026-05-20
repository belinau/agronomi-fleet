"""µReticulum — Air Quality Node Firmware (SN-AIR-01)

MicroPython firmware for a DHT22 air-temperature + humidity + battery
sensor node that communicates over µReticulum (LoRa / WiFi / BLE).

Lifecycle (each wake cycle):
  1. Boot → connect WiFi (if configured)
  2. Initialise µReticulum + interfaces (async event loop)
  3. Wait for interface to come online (BLE scan → connect → detect)
  4. Read all sensors (DHT22 + battery ADC)
  5. Announce on the command channel (with app_data)
  6. Send telemetry to the hub's SINGLE destination
  7. Listen briefly for inbound commands
  8. Deep sleep for SLEEP_INTERVAL_SEC

Commands received on the command channel:
  - vent_open   → open ventilation actuator
  - vent_close  → close ventilation actuator
  - fan_on      → turn on circulation fan
  - fan_off     → turn off circulation fan

The telemetry JSON format is backward-compatible with the BLE gateway:
  {
    "dev_id": "SN-AIR-01",
    "device_type": "air_node",
    "fw_ver": "2.0.0-mr",
    "gateway_id": "SN-AIR-01",
    "readings": {
      "air_temp_c": 23.5,
      "air_humidity_pct": 65.2,
      "air_temp_valid": true,
      "air_humidity_valid": true
    },
    "bat_v": 3.87,
    "rns_interface": "lora"
  }
"""

import gc
import json
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
import config
import machine
import uasyncio as asyncio

# ---------------------------------------------------------------------------
# Sensor drivers
# ---------------------------------------------------------------------------
from sensors import read_all, read_all_async, read_dht22, read_dht22_async

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

_hub_identity = None


def _log(msg, level=1):
    """Simple level-gated logger."""
    if config.DEBUG >= level:
        print("[SN-AIR] " + str(msg))


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
    """Handle an inbound command packet on the command destination."""
    try:
        payload = json.loads(
            data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
        )
        cmd = payload.get("cmd", "")
        _log("Command received: " + cmd, 1)

        if cmd == "vent_open":
            _log("Vent OPEN (acknowledged)", 1)
            # TODO: drive vent actuator GPIO
        elif cmd == "vent_close":
            _log("Vent CLOSE (acknowledged)", 1)
            # TODO: drive vent actuator GPIO
        elif cmd == "fan_on":
            _log("Fan ON (acknowledged)", 1)
            # TODO: drive fan relay GPIO
        elif cmd == "fan_off":
            _log("Fan OFF (acknowledged)", 1)
            # TODO: drive fan relay GPIO
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

        source_hash = original_packet.destination_hash if original_packet else None
        if source_hash is None:
            _log("Cannot ACK: no source hash", 1)
            return

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


def _on_announce(destination_hash, app_data, packet):
    """Callback when an announce is received on the command channel."""
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
    """Build the JSON telemetry dict matching the legacy BLE format."""
    return {
        "dev_id": config.NODE_NAME,
        "device_type": config.DEVICE_TYPE,
        "fw_ver": config.FIRMWARE_VERSION,
        "gateway_id": config.NODE_NAME,
        "readings": {
            "air_temp_c": readings["air_temp_c"],
            "air_humidity_pct": readings["air_humidity_pct"],
            "air_temp_valid": readings["air_temp_valid"],
            "air_humidity_valid": readings["air_humidity_valid"],
        },
        "bat_v": readings["battery_v"],
        "rns_interface": interface_name,
    }


# ---------------------------------------------------------------------------
# Async main — runs the entire lifecycle with the event loop
# ---------------------------------------------------------------------------


async def main():
    """Air node main — runs the full lifecycle, then deep sleeps."""
    global _hub_identity

    gc.collect()
    _log("=" * 40)
    _log("SN-AIR-01 boot — µReticulum v" + config.FIRMWARE_VERSION)
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
            # Non-fatal: we may have BLE/LoRa

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

        # Start interfaces (RNode BLE, WiFi UDP, etc.)
        rns.setup_interfaces()

        _log("µReticulum initialised — identity: " + ident.hexhash)

    except Exception as e:
        _log("FATAL: µReticulum init failed: " + str(e))
        _deep_sleep()
        return

    # ------------------------------------------------------------------
    # 3. Start the async event loop for BLE and transport
    # ------------------------------------------------------------------
    # RNodeBLEInterface needs its poll_loop() running to drive the BLE
    # connection lifecycle (scan → connect → detect → config → online).
    # Transport.job_loop() processes inbound/outbound packets.
    # Both must run concurrently before the interface can come online.
    from urns.transport import Transport

    poll_tasks = []

    # Start interface poll loops (BLE state machine, etc.)
    for iface in rns.interfaces:
        if hasattr(iface, "poll_loop"):
            task = asyncio.create_task(iface.poll_loop())
            poll_tasks.append(task)
            _log("Started poll loop for " + str(iface))

    # Start transport job loop (packet processing)
    transport_task = asyncio.create_task(Transport.job_loop())
    poll_tasks.append(transport_task)
    _log("Started transport job loop")

    # ------------------------------------------------------------------
    # 4. Wait for at least one interface to come online
    # ------------------------------------------------------------------
    _log("Waiting for interface to come online...")
    iface_timeout = 30  # seconds
    iface_deadline = time.ticks_add(time.ticks_ms(), iface_timeout * 1000)

    while time.ticks_diff(iface_deadline, time.ticks_ms()) > 0:
        online_ifaces = [i for i in rns.interfaces if getattr(i, "online", False)]
        if online_ifaces:
            _log("Interface online: " + str(online_ifaces[0]))
            break
        await asyncio.sleep_ms(500)  # Yield to event loop while waiting
    else:
        _log("WARN: No interface came online in " + str(iface_timeout) + "s")

    # ------------------------------------------------------------------
    # 5. Set up RNS destinations
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

    telemetry_app = config.TELEMETRY_APP
    telemetry_aspect = config.TELEMETRY_ASPECT

    _log("Command dest: " + str(cmd_dest))
    _log("Telemetry target: " + telemetry_app + "." + telemetry_aspect)

    # ------------------------------------------------------------------
    # 6. Read sensors (async — yields to BLE event loop during DHT22 wait)
    # ------------------------------------------------------------------
    _log("Reading sensors...")
    readings = await read_all_async(config)

    if config.DEBUG >= 1:
        _log(
            "Air temp:      {:.1f}°C (valid={})".format(
                readings["air_temp_c"], readings["air_temp_valid"]
            )
        )
        _log(
            "Air humidity:  {:.1f}% (valid={})".format(
                readings["air_humidity_pct"], readings["air_humidity_valid"]
            )
        )
        _log("Battery:        {:.2f}V".format(readings["battery_v"]))

    # ------------------------------------------------------------------
    # 7. Announce ourselves on the command channel
    # ------------------------------------------------------------------
    app_data = (config.RNS_ANNOUNCE_PREFIX + ":" + config.NODE_NAME).encode("utf-8")
    cmd_dest.announce(app_data=app_data)
    _log("Announced on " + config.COMMAND_APP + "." + config.COMMAND_ASPECT)

    # ------------------------------------------------------------------
    # 8. Wait for hub announce, then send telemetry
    # ------------------------------------------------------------------
    _log("Waiting for hub announce...")
    hub_timeout = 60  # seconds — hub re-announces every 30s
    hub_deadline = time.ticks_add(time.ticks_ms(), hub_timeout * 1000)

    while _hub_identity is None and time.ticks_diff(hub_deadline, time.ticks_ms()) > 0:
        await asyncio.sleep_ms(500)  # Yield to event loop while waiting

    iface_name = _get_rns_interface_name(rns)
    telemetry = _build_telemetry(readings, iface_name)
    payload = json.dumps(telemetry).encode("utf-8")

    if _hub_identity is not None:
        tx_dest = Destination(
            _hub_identity,
            Destination.OUT,
            Destination.SINGLE,
            telemetry_app,
            telemetry_aspect,
        )
        _log("Telemetry -> SINGLE dest (hub identity discovered)")
    else:
        _log("WARN: Hub not discovered after " + str(hub_timeout) + "s")
        _log("Sending PLAIN (may not arrive over multi-hop)")
        tx_dest = Destination(
            None,
            Destination.OUT,
            Destination.PLAIN,
            telemetry_app,
            telemetry_aspect,
        )

    pkt = Packet(tx_dest, payload, Packet.DATA)
    receipt = pkt.send()

    if receipt:
        _log("Telemetry sent (" + str(len(payload)) + " bytes)")
    else:
        _log("Telemetry send FAILED (no interface online?)")

    # ------------------------------------------------------------------
    # 9. Listen briefly for inbound commands
    # ------------------------------------------------------------------
    _log("Listening for commands (5 s)...")
    await asyncio.sleep_ms(5000)

    # ------------------------------------------------------------------
    # 10. Clean up and deep sleep
    # ------------------------------------------------------------------
    # Cancel poll tasks
    for task in poll_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Close interfaces
    for iface in rns.interfaces:
        if hasattr(iface, "close"):
            try:
                iface.close()
            except Exception:
                pass

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
# Entry point — run the async main with uasyncio
# ---------------------------------------------------------------------------
asyncio.run(main())
