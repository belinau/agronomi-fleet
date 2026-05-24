"""µReticulum — Air Quality Node Firmware (SN-AIR-01)"""

import gc
import json
import time

import config
import machine
import uasyncio as asyncio
from sensors import read_all_async
from urns import Reticulum
from urns.destination import Destination
from urns.identity import Identity
from urns.lxmf import LXMessage, LXMRouter
from urns.packet import Packet

_hub_identity = None
_lxm_router = None


def _log(msg, level=1):
    if config.DEBUG >= level:
        print("[SN-AIR] " + str(msg))


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


def _on_lxmf_delivery(message):
    try:
        # Standard LXMF: parse commands sent in the fields dictionary
        fields = message.fields or {}
        cmd = fields.get("cmd", "")
        if not cmd and message.content:
            try:
                payload_str = message.content.decode("utf-8")
                payload = json.loads(payload_str)
                cmd = payload.get("cmd", "")
            except:
                pass

        _log("LXMF command: " + cmd, 1)
        if cmd == "vent_open":
            _log("Vent OPEN", 1)
        elif cmd == "vent_close":
            _log("Vent CLOSE", 1)
        elif cmd == "fan_on":
            _log("Fan ON", 1)
        elif cmd == "fan_off":
            _log("Fan OFF", 1)
        else:
            _log("Unknown command: " + cmd, 1)
    except Exception as e:
        _log("LXMF handler error: " + str(e), 1)


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


# ALIGNED COMPACT PAYLOAD: Keeps content size small to force robust opportunistic delivery
def _build_telemetry(readings, interface_name):
    return {
        "dev_id": config.NODE_NAME,
        "type": config.DEVICE_TYPE,
        "fw": config.FIRMWARE_VERSION,
        "bat": readings["battery_v"],
        "readings": {
            "temp": readings["air_temp_c"],
            "hum": readings["air_humidity_pct"],
        },
        "if": interface_name,
    }


async def main():
    global _hub_identity, _lxm_router

    gc.collect()
    _log("=" * 40)
    _log("SN-AIR-01 boot — µReticulum v" + config.FIRMWARE_VERSION)
    _log("=" * 40)

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

    _log("Reading sensors...")
    readings = await read_all_async(config)
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

    # Initial announcement
    app_data = (config.RNS_ANNOUNCE_PREFIX + ":" + config.NODE_NAME).encode("utf-8")
    cmd_dest.announce(app_data=app_data)
    _log("Announced on " + config.COMMAND_APP + "." + config.COMMAND_ASPECT)

    _log("Waiting for hub announce...")
    hub_timeout = 60
    hub_deadline = time.ticks_add(time.ticks_ms(), hub_timeout * 1000)

    while _hub_identity is None and time.ticks_diff(hub_deadline, time.ticks_ms()) > 0:
        await asyncio.sleep_ms(500)

    iface_name = _get_rns_interface_name(rns)

    if _hub_identity is not None:
        # ALIGNED FOR TIMING: Re-announce ourselves immediately now that we know the Hub is active.
        # This guarantees the Hub receives and caches our public key right before the packet is sent.
        cmd_dest.announce(app_data=app_data)

        # Native LXMF fields send
        telemetry_fields = {
            "dev_id": config.NODE_NAME,
            "type": config.DEVICE_TYPE,
            "fw": config.FIRMWARE_VERSION,
            "bat": readings["battery_v"],
            "temp": readings["air_temp_c"],
            "hum": readings["air_humidity_pct"],
            "if": iface_name,
        }
        _lxm_router.send_message(
            _hub_identity.hash, content=b"", fields=telemetry_fields
        )
        _log(
            "Telemetry routed natively via LXMF fields to Hub: " + _hub_identity.hexhash
        )
    else:
        _log(
            "WARN: hub not found — skipping telemetry (LXMF requires recipient identity)"
        )

    _log("Listening for commands (5 s)...")
    await asyncio.sleep_ms(5000)

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
