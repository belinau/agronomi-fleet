"""µReticulum — Pump Actuator Node Firmware (AN-PUMP-01)

MicroPython firmware for a pump relay actuator node that communicates
over µReticulum (LoRa / WiFi / BLE).

Unlike sensor nodes that deep-sleep between readings, actuator nodes stay
awake continuously so they can receive commands at any time.  The main loop
is an asyncio event loop (uasyncio.run(rns.run())) that keeps all
interfaces alive and listening.

Architecture:
  - IN destination:  farm.gateway_commands (SINGLE) — receives commands
  - OUT destination:  farm.telemetry_readings — sends periodic status
  - OUT destination:  farm.commands_control — sends command ACKs

Commands received on the IN destination:
  - pump_on  → activate pump relay
  - pump_off → deactivate pump relay

ACK format (sent to farm.commands_control):
  {
    "cmd_id": 123,
    "device_id": "AN-PUMP-01",
    "status": "acknowledged",
    "error": null
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

# ---------------------------------------------------------------------------
# Actuator hardware
# ---------------------------------------------------------------------------
from sensors import read_all

# ---------------------------------------------------------------------------
# µReticulum
# ---------------------------------------------------------------------------
from urns import Reticulum
from urns.destination import Destination
from urns.identity import Identity
from urns.packet import Packet

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_pump_on = False  # Current pump relay state
_cmd_counter = 0  # Auto-increment for command tracking

# Hub identity — discovered via RNS announce
_hub_identity = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg, level=1):
    """Simple level-gated logger."""
    if config.DEBUG >= level:
        print("[AN-PUMP] " + str(msg))


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
# Actuator control — pump relay
# ---------------------------------------------------------------------------

# Initialise the relay GPIO early (active-HIGH relay on PIN_PUMP_RELAY)
_pump_pin = machine.Pin(config.PIN_PUMP_RELAY, machine.Pin.OUT, value=0)


def pump_on():
    """Activate the pump relay."""
    global _pump_on
    _pump_pin.value(1)
    _pump_on = True
    _log("Pump relay ON")


def pump_off():
    """Deactivate the pump relay."""
    global _pump_on
    _pump_pin.value(0)
    _pump_on = False
    _log("Pump relay OFF")


# ---------------------------------------------------------------------------
# ACK sender — replies to hub via farm.commands_control
# ---------------------------------------------------------------------------


def _send_ack(cmd, cmd_id, rns, error=None):
    """Send a JSON ACK packet back to the hub via farm.commands_control.

    The ACK is sent to the hub's SINGLE destination.  If we have
    discovered the hub's identity via announce, we use an encrypted
    SINGLE OUT destination.  Otherwise we fall back to PLAIN.
    """
    global _hub_identity

    ack_payload = json.dumps(
        {
            "cmd_id": cmd_id,
            "device_id": config.NODE_NAME,
            "status": "acknowledged" if error is None else "error",
            "error": error,
        }
    ).encode("utf-8")

    try:
        if _hub_identity is not None:
            ack_dest = Destination(
                _hub_identity,
                Destination.OUT,
                Destination.SINGLE,
                config.ACK_APP,
                config.ACK_ASPECT,
            )
        else:
            ack_dest = Destination(
                None,
                Destination.OUT,
                Destination.PLAIN,
                config.ACK_APP,
                config.ACK_ASPECT,
            )

        pkt = Packet(ack_dest, ack_payload, Packet.DATA)
        receipt = pkt.send()
        if receipt:
            _log(
                "ACK sent for cmd_id={} status={}".format(
                    cmd_id, "acknowledged" if error is None else "error"
                )
            )
        else:
            _log("ACK send FAILED (no interface online?)", 1)

    except Exception as e:
        _log("ACK send error: " + str(e), 1)


# ---------------------------------------------------------------------------
# Command handler — receives commands on the IN destination
# ---------------------------------------------------------------------------


def _on_command(data, packet):
    """Handle an inbound command packet on farm.gateway_commands.

    Expected JSON payload:
        {"cmd": "pump_on", "cmd_id": 123}
        {"cmd": "pump_off", "cmd_id": 124}
    """
    global _cmd_counter

    try:
        payload = json.loads(
            data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
        )
    except Exception as e:
        _log("Command parse error: " + str(e), 1)
        return

    cmd = payload.get("cmd", "")
    cmd_id = payload.get("cmd_id", _cmd_counter)
    _cmd_counter += 1

    _log("Command received: " + cmd + " (cmd_id=" + str(cmd_id) + ")", 1)

    error = None

    if cmd == "pump_on":
        pump_on()
    elif cmd == "pump_off":
        pump_off()
    else:
        error = "unknown_command: " + cmd
        _log("Unknown command: " + cmd, 1)

    # Send ACK back to hub
    _send_ack(cmd, cmd_id, _rns, error=error)


# Reference to the Reticulum instance (set in main)
_rns = None


# ---------------------------------------------------------------------------
# Announce handler — discovers the hub on the command channel
# ---------------------------------------------------------------------------


def _on_announce(destination_hash, app_data, packet):
    """Callback when an announce is received.

    If the app_data contains a known hub identifier
    prefix, we record the identity so we can send telemetry and ACKs
    to it using encrypted SINGLE destinations.
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
        if _hub_identity is None:
            ident = Identity.recall(destination_hash)
            if ident is not None:
                _hub_identity = ident
                _log("Hub discovered: " + ident.hexhash)

    except Exception as e:
        _log("Announce handler error: " + str(e), 2)


# ---------------------------------------------------------------------------
# Status telemetry builder
# ---------------------------------------------------------------------------


def _build_status_telemetry(interface_name):
    """Build a JSON telemetry dict with actuator state and battery voltage."""
    readings = read_all(config)
    return {
        "dev_id": config.NODE_NAME,
        "device_type": config.DEVICE_TYPE,
        "fw_ver": config.FIRMWARE_VERSION,
        "gateway_id": config.NODE_NAME,
        "readings": {
            "pump_on": _pump_on,
        },
        "bat_v": readings.get("battery_v", -1.0),
        "rns_interface": interface_name,
    }


# ---------------------------------------------------------------------------
# Main entry point — async event loop (NO deep sleep)
# ---------------------------------------------------------------------------


def main():
    """Pump actuator main — runs an async event loop continuously.

    Unlike sensor nodes, actuator nodes never deep-sleep. They stay
    awake to receive commands at any time via the RNS event loop.
    """
    global _rns, _hub_identity

    gc.collect()
    _log("=" * 40)
    _log("AN-PUMP-01 boot — µReticulum v" + config.FIRMWARE_VERSION)
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

    # ------------------------------------------------------------------
    # 2. Initialise µReticulum
    # ------------------------------------------------------------------
    try:
        rns = Reticulum(loglevel={0: 0, 1: 0, 2: 2}.get(config.DEBUG, 0))
        rns.config = config.CONFIG

        storage = rns.storagepath
        ident = _find_or_create_identity(storage)
        rns.identity = ident

        rns.setup_interfaces()
        _rns = rns

        _log("µReticulum initialised — identity: " + ident.hexhash)

    except Exception as e:
        _log("FATAL: µReticulum init failed: " + str(e))
        # Cannot operate without RNS — reboot after a delay
        time.sleep(10)
        machine.reset()
        return

    # ------------------------------------------------------------------
    # 3. Set up RNS destinations
    # ------------------------------------------------------------------

    # IN: farm.gateway_commands — receive commands from the hub (SINGLE, announceable)
    cmd_dest = Destination(
        ident,
        Destination.IN,
        Destination.SINGLE,
        config.COMMAND_IN_APP,
        config.COMMAND_IN_ASPECT,
    )
    cmd_dest.set_proof_strategy(Destination.PROVE_ALL)
    cmd_dest.set_packet_callback(_on_command)
    cmd_dest._announce_handler = _on_announce

    _log("Command IN dest: " + str(cmd_dest))

    # ------------------------------------------------------------------
    # 4. Announce ourselves so the hub can discover us
    # ------------------------------------------------------------------
    app_data = (config.RNS_ANNOUNCE_PREFIX + ":" + config.NODE_NAME).encode("utf-8")
    cmd_dest.announce(app_data=app_data)
    _log("Announced on " + config.COMMAND_IN_APP + "." + config.COMMAND_IN_ASPECT)

    # ------------------------------------------------------------------
    # 5. Run the async event loop (blocks forever)
    # ------------------------------------------------------------------
    import uasyncio as asyncio

    # Periodic tasks
    async def periodic_announce():
        """Re-announce periodically so new/refreshed hubs can discover us."""
        while True:
            await asyncio.sleep(config.ANNOUNCE_INTERVAL_SEC)
            try:
                cmd_dest.announce(app_data=app_data)
                _log(
                    "Re-announced on "
                    + config.COMMAND_IN_APP
                    + "."
                    + config.COMMAND_IN_ASPECT
                )
            except Exception as e:
                _log("Re-announce error: " + str(e), 1)

    async def periodic_telemetry():
        """Send periodic status telemetry to the hub."""
        while True:
            await asyncio.sleep(config.TELEMETRY_INTERVAL_SEC)
            try:
                iface_name = _get_rns_interface_name(rns)
                telemetry = _build_status_telemetry(iface_name)
                payload = json.dumps(telemetry).encode("utf-8")

                # Send to the hub's telemetry destination
                if _hub_identity is not None:
                    tx_dest = Destination(
                        _hub_identity,
                        Destination.OUT,
                        Destination.SINGLE,
                        config.TELEMETRY_APP,
                        config.TELEMETRY_ASPECT,
                    )
                else:
                    tx_dest = Destination(
                        None,
                        Destination.OUT,
                        Destination.PLAIN,
                        config.TELEMETRY_APP,
                        config.TELEMETRY_ASPECT,
                    )

                pkt = Packet(tx_dest, payload, Packet.DATA)
                receipt = pkt.send()
                if receipt:
                    _log("Status telemetry sent (" + str(len(payload)) + " bytes)")
                else:
                    _log("Status telemetry send FAILED", 1)
            except Exception as e:
                _log("Telemetry error: " + str(e), 1)

    async def keep_alive():
        """Minimum-viable keep-alive so the event loop never exits."""
        while True:
            await asyncio.sleep(60)

    _log("Starting async event loop (actuator stays awake)")
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(periodic_announce())
        loop.create_task(periodic_telemetry())
        loop.create_task(keep_alive())
        loop.run_forever()
    except KeyboardInterrupt:
        _log("Shutdown requested")
        rns.shutdown()
    except Exception as e:
        _log("Event loop error: " + str(e), 1)
        # Critical failure — reboot after a delay
        time.sleep(10)
        machine.reset()


# ---------------------------------------------------------------------------
# Auto-run on import (MicroPython entry point)
# ---------------------------------------------------------------------------
main()
