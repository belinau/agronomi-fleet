"""µReticulum — Soil Node Firmware (SN-SOIL-01)

Lifecycle:
  1. Initialise µReticulum + interfaces (RNodeBLEInterface reads ble_pin.txt,
     force_pair.txt, ble_mac.txt itself — do NOT connect WiFi before this,
     as wlan.active(True) inside RNodeBLEInterface.__init__ conflicts)
  2. Connect WiFi (after interfaces are constructed)
  3. Start BLE poll loops + transport job loop
  4. Wait for interface online
  5. Set up LXMRouter for LXMF receive
  6. Announce on command channel
  7. Read sensors, send telemetry
  8. Wait for hub announce (yielding to event loop each tick)
  9. Listen for inbound LXMF commands (5 s)
 10. Deep sleep
"""

import gc
import json
import time

import config
import machine
import uasyncio as asyncio
from sensors import read_all
from urns import Reticulum
from urns.destination import Destination
from urns.identity import Identity
from urns.lxmf import LXMessage, LXMRouter
from urns.packet import Packet

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_hub_identity = None
_lxm_router = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg, level=1):
    if config.DEBUG >= level:
        print("[SN-SOIL] " + str(msg))


def _get_rns_interface_name(rns):
    for iface in rns.interfaces:
        if hasattr(iface, "online") and iface.online:
            return getattr(iface, "name", iface.__class__.__name__).lower()
    return "none"


def _connect_wifi(ssid, password, timeout=15):
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
# LXMF command handler
# ---------------------------------------------------------------------------


def _on_lxmf_delivery(message):
    try:
        if isinstance(message.content, (bytes, bytearray)):
            payload_str = message.content.decode("utf-8")
        else:
            payload_str = str(message.content)
        payload = json.loads(payload_str)
        cmd = payload.get("cmd", "")
        _log("LXMF command: " + cmd, 1)
        if cmd == "pump_on":
            _log("Pump ON", 1)
            # TODO: drive pump relay GPIO
        elif cmd == "pump_off":
            _log("Pump OFF", 1)
            # TODO: drive pump relay GPIO
        else:
            _log("Unknown command: " + cmd, 1)
    except Exception as e:
        _log("LXMF handler error: " + str(e), 1)


# ---------------------------------------------------------------------------
# Announce handler
# ---------------------------------------------------------------------------


def _on_announce(destination_hash, app_data, packet):
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
        if _hub_identity is None:
            ident = Identity.recall(destination_hash)
            if ident is not None:
                _hub_identity = ident
                _log("Hub discovered: " + ident.hexhash)
    except Exception as e:
        _log("Announce handler error: " + str(e), 2)


# ---------------------------------------------------------------------------
# Telemetry builder
# ---------------------------------------------------------------------------


def _build_telemetry(readings, interface_name):
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
# Async main
# ---------------------------------------------------------------------------


async def main():
    global _hub_identity, _lxm_router

    gc.collect()
    _log("=" * 40)
    _log("SN-SOIL-01 boot — µReticulum v" + config.FIRMWARE_VERSION)
    _log("=" * 40)

    # ------------------------------------------------------------------
    # 1. Initialise µReticulum FIRST — before WiFi
    # ------------------------------------------------------------------
    try:
        rns = Reticulum(loglevel={0: 0, 1: 0, 2: 2}.get(config.DEBUG, 0))
        rns.config = config.CONFIG
        storage = rns.storagepath
        ident = _find_or_create_identity(storage)
        rns.identity = ident
        rns.setup_interfaces()
        _log("µReticulum initialised — identity: " + ident.hexhash)
        _log("Interfaces: " + str([str(i) for i in rns.interfaces]))
    except Exception as e:
        _log("FATAL: µReticulum init failed: " + str(e))
        _deep_sleep()
        return

    # ------------------------------------------------------------------
    # 2. Connect WiFi now
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
    # 3. Start poll loops
    # ------------------------------------------------------------------
    from urns.transport import Transport

    poll_tasks = []
    for iface in rns.interfaces:
        if hasattr(iface, "poll_loop"):
            task = asyncio.create_task(iface.poll_loop())
            poll_tasks.append(task)
            _log("Started poll loop for " + str(iface))

    transport_task = asyncio.create_task(Transport.job_loop())
    poll_tasks.append(transport_task)
    _log("Started transport job loop")

    # ------------------------------------------------------------------
    # 4. Wait for interface online
    # ------------------------------------------------------------------
    _log("Waiting for interface to come online...")
    iface_timeout = 45
    iface_deadline = time.ticks_add(time.ticks_ms(), iface_timeout * 1000)

    while time.ticks_diff(iface_deadline, time.ticks_ms()) > 0:
        online = [i for i in rns.interfaces if getattr(i, "online", False)]
        if online:
            _log("Interface online: " + str(online[0]))
            break
        await asyncio.sleep_ms(500)
    else:
        _log("WARN: No interface online after " + str(iface_timeout) + "s")

    # ------------------------------------------------------------------
    # 5. LXMRouter for LXMF receive
    # ------------------------------------------------------------------
    _lxm_router = LXMRouter(storagepath=storage)
    lxmf_dest = _lxm_router.register_delivery_identity(
        ident,
        display_name=config.NODE_NAME,
    )
    _lxm_router.register_delivery_callback(_on_lxmf_delivery)
    _log("LXMF delivery dest: " + lxmf_dest.hexhash)

    cmd_dest = Destination(
        ident,
        Destination.IN,
        Destination.SINGLE,
        config.COMMAND_APP,
        config.COMMAND_ASPECT,
    )
    cmd_dest.set_proof_strategy(Destination.PROVE_ALL)
    cmd_dest._announce_handler = _on_announce

    # ------------------------------------------------------------------
    # 6. Read sensors + announce
    # ------------------------------------------------------------------
    _log("Reading sensors...")
    readings = read_all(config)
    _log("Soil moisture: {:.1f}%".format(readings["soil_moisture_pct"]))
    _log(
        "Soil temp:     {:.1f}°C (valid={})".format(
            readings["soil_temp_c"], readings["soil_temp_valid"]
        )
    )
    _log("Battery:        {:.2f}V".format(readings["bat_v"]))

    app_data = (config.RNS_ANNOUNCE_PREFIX + ":" + config.NODE_NAME).encode("utf-8")
    cmd_dest.announce(app_data=app_data)
    _log("Announced on " + config.COMMAND_APP + "." + config.COMMAND_ASPECT)

    # ------------------------------------------------------------------
    # 7. Wait for hub announce (must yield so BLE packets are processed)
    # ------------------------------------------------------------------
    _log("Waiting for hub announce...")
    hub_timeout = 60
    hub_deadline = time.ticks_add(time.ticks_ms(), hub_timeout * 1000)

    while _hub_identity is None and time.ticks_diff(hub_deadline, time.ticks_ms()) > 0:
        await asyncio.sleep_ms(500)

    # ------------------------------------------------------------------
    # 8. Send telemetry via LXMF
    # ------------------------------------------------------------------
    iface_name = _get_rns_interface_name(rns)
    payload = json.dumps(_build_telemetry(readings, iface_name)).encode("utf-8")

    if _hub_identity is not None:
        tx_dest = Destination(
            _hub_identity, Destination.OUT, Destination.SINGLE, "lxmf", "delivery"
        )
        lxm = LXMessage(tx_dest, lxmf_dest, payload)
        _lxm_router.handle_outbound(lxm)
        _log("Telemetry routed via LXMF to Hub: " + _hub_identity.hexhash)
    else:
        _log(
            "WARN: hub not found — skipping telemetry (LXMF requires recipient identity)"
        )

    # ------------------------------------------------------------------
    # 9. Listen for LXMF commands
    # ------------------------------------------------------------------
    _log("Listening for commands (5 s)...")
    await asyncio.sleep_ms(5000)

    # ------------------------------------------------------------------
    # 10. Clean up + deep sleep
    # ------------------------------------------------------------------
    for task in poll_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    for iface in rns.interfaces:
        if hasattr(iface, "close"):
            try:
                iface.close()
            except Exception:
                pass

    _deep_sleep()


def _deep_sleep():
    if config.ENABLE_DEEPSLEEP:
        _log("Deep sleeping {} s...".format(config.SLEEP_INTERVAL_SEC))
        time.sleep_ms(100)
        machine.deepsleep(config.SLEEP_INTERVAL_SEC * 1000)
    else:
        _log("DEBUG MODE — no deep sleep.")
        while True:
            time.sleep(1)


asyncio.run(main())
