"""µReticulum — Greenhouse Actuator Node Firmware (AN-GREENHOUSE-01)

Stays awake forever; receives vent/shade/fan commands and sends
periodic telemetry + announces.  All transport is direct RNS Link +
Packet — no LXMF.  OTA uses the chunked Link protocol shared with the
rest of the fleet.

Commands accepted (via Packet on cmd_dest):
  - vent_open   → open ventilation relay
  - vent_close  → close ventilation relay
  - fan_on      → turn on circulation fan
  - fan_off     → turn off circulation fan
  - shade_pct   → set shade percentage (0–100), via {"cmd":"shade_pct","value":50}
"""

import gc
import time

import config
import machine
import uasyncio as asyncio
from sensors import read_all
from urns import Reticulum, umsgpack
from urns.destination import Destination
from urns.identity import Identity
from urns.packet import Packet

_vent_state = False
_fan_state = False
_shade_pct_state = 0
_cmd_counter = 0

_hub_identity = None
_fw_update_pending = False
_rns_instance = None
_ble_interface_class = None
_ble_config_dict = None


def _log(msg, level=1):
    if config.DEBUG >= level:
        print("[AN-GREENHOUSE] " + str(msg))


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
# Actuator hardware
# ---------------------------------------------------------------------------

_vent_pin = machine.Pin(config.PIN_VENT_RELAY, machine.Pin.OUT, value=0)
_fan_pin = machine.Pin(config.PIN_FAN_RELAY, machine.Pin.OUT, value=0)
_shade_pwm = machine.PWM(machine.Pin(config.PIN_SHADE_PWM), freq=25000, duty=0)


def _set_vent(open_):
    global _vent_state
    _vent_pin.value(1 if open_ else 0)
    _vent_state = bool(open_)
    _log("Vent " + ("OPEN" if open_ else "CLOSED"))


def _set_fan(on):
    global _fan_state
    _fan_pin.value(1 if on else 0)
    _fan_state = bool(on)
    _log("Fan " + ("ON" if on else "OFF"))


def _set_shade(pct):
    global _shade_pct_state
    pct = max(0, min(100, int(pct)))
    _shade_pwm.duty(int(pct * 10.23))
    _shade_pct_state = pct
    _log("Shade at " + str(pct) + "%")


def _suspend_ble_interface(rns):
    global _ble_interface_class, _ble_config_dict
    try:
        from urns.transport import Transport

        target_iface = None
        for iface in rns.interfaces:
            if iface.__class__.__name__ == "RNodeBLEInterface":
                target_iface = iface
                break

        if target_iface:
            _ble_interface_class = target_iface.__class__
            for cfg in config.CONFIG.get("interfaces", []):
                if cfg.get("type") == "RNodeBLEInterface":
                    _ble_config_dict = cfg
                    break

            target_iface.close()
            if target_iface in rns.interfaces:
                rns.interfaces.remove(target_iface)
            if (
                hasattr(Transport, "interfaces")
                and target_iface in Transport.interfaces
            ):
                Transport.interfaces.remove(target_iface)

            _log("Physically disconnected BLE RNode for firmware priority window", 1)
    except Exception as e:
        _log("BLE suspend failed: " + str(e), 1)


def _resume_ble_interface(rns):
    """Re-create the BLE LoRa interface and re-register it with RNS Transport.

    Mirrors what urns.Reticulum.setup_interfaces does for a single interface:
    instantiate the class with the config dict (constructor signature is
    `__init__(self, config)` — passing `rns` was a stale bug), apply IFAC,
    append to rns.interfaces, register with Transport, then start its
    poll loop.  Without the registration steps the new interface exists
    but Transport.outbound() never picks it for routing.
    """
    global _ble_interface_class, _ble_config_dict
    try:
        if _ble_interface_class and _ble_config_dict:
            _log("Re-instantiating BLE interface for parallel operation...", 1)
            from urns.transport import Transport

            new_iface = _ble_interface_class(_ble_config_dict)
            new_iface.setup_ifac(_ble_config_dict)
            rns.interfaces.append(new_iface)
            Transport.register_interface(new_iface)

            if hasattr(new_iface, "poll_loop"):
                asyncio.create_task(new_iface.poll_loop())

            _log("Resumed parallel BLE/LoRa transport interface", 1)
            _ble_interface_class = None
            _ble_config_dict = None
    except Exception as e:
        _log("BLE resume failed: " + str(e), 1)


def _send_ack(hub_identity, cmd, cmd_id, status, error=None):
    try:
        hub_dest = Destination(
            hub_identity, Destination.OUT, Destination.SINGLE,
            config.HUB_APP, config.HUB_ASPECT,
        )
        ack = {
            "cmd": "ack",
            "dev_id": config.NODE_NAME,
            "for_cmd": cmd,
            "cmd_id": cmd_id,
            "status": status,
        }
        if error:
            ack["error"] = error
        Packet(hub_dest, umsgpack.packb(ack)).send()
        _log("ACK sent for cmd_id=" + str(cmd_id))
    except Exception as e:
        _log("ACK send error: " + str(e), 1)


def _handle_hardware_command(cmd, cmd_id, fields):
    error = None
    if cmd == "vent_open":
        _set_vent(True)
    elif cmd == "vent_close":
        _set_vent(False)
    elif cmd == "fan_on":
        _set_fan(True)
    elif cmd == "fan_off":
        _set_fan(False)
    elif cmd == "shade_pct":
        _set_shade(fields.get("value", 0))
    else:
        error = "unknown_command: " + str(cmd)
        _log(error, 1)

    if _hub_identity is not None:
        _send_ack(
            _hub_identity, cmd, cmd_id,
            "ok" if error is None else "error", error,
        )


def _on_packet_received(data, packet):
    global _cmd_counter, _fw_update_pending
    try:
        fields = umsgpack.unpackb(data)
        cmd = fields.get("cmd", "")
        _log("Packet received: cmd={}".format(cmd), 1)

        if cmd == "fw_check_ack":
            if fields.get("pending"):
                _fw_update_pending = True
                _log("Hub signaled pending update", 1)
            else:
                _log("Hub has no firmware update pending", 1)
                _resume_ble_interface(_rns_instance)
            return

        if cmd == "execute":
            cmd_type = fields.get("type", "")
            cmd_id = fields.get("cmd_id", _cmd_counter)
            _cmd_counter += 1
            _handle_hardware_command(cmd_type, cmd_id, fields)
            return

        if cmd in ("vent_open", "vent_close", "fan_on", "fan_off", "shade_pct"):
            cmd_id = fields.get("cmd_id", _cmd_counter)
            _cmd_counter += 1
            _handle_hardware_command(cmd, cmd_id, fields)

    except Exception as e:
        _log("Packet callback error: " + str(e), 1)


def _on_link_established(link):
    _log("Stateful Link connected from Hub", 1)

    def _packet_handler(data, packet):
        _on_link_packet(data, packet, link)

    link.set_packet_callback(_packet_handler)
    link.resource_concluded_callback = _on_resource_concluded


def _on_link_packet(data, packet, link):
    global _fw_update_pending
    try:
        fields = umsgpack.unpackb(data)
        cmd = fields.get("cmd", "")
        _log("Link control packet: cmd={}".format(cmd), 1)

        if cmd == "manifest_query":
            import updater
            requested = fields.get("files", []) or []
            manifest = updater.compute_file_manifest(requested)
            try:
                link.send(umsgpack.packb({
                    "cmd": "manifest_response",
                    "manifest": manifest,
                }))
                _log("manifest_response: {} entries".format(len(manifest)), 1)
            except Exception as e:
                _log("manifest_response send error: " + str(e), 1)
            return

        if cmd == "update_begin":
            import updater
            updater.handle_update_begin(fields)
            _fw_update_pending = True

        elif cmd in ("update_file", "update_commit"):
            import updater
            resp = updater.handle_update(fields)

            if cmd == "update_commit":
                if resp.get("status") == "ok":
                    staged = updater.list_staged_files()
                    _log("Firmware staging complete ({} files). rebooting...".format(len(staged)), 1)
                    time.sleep_ms(500)
                    machine.reset()
                else:
                    _log("Commit failed: {}".format(resp.get("error")), 1)
                    _fw_update_pending = True
    except Exception as e:
        _log("Link control error: " + str(e), 1)


def _on_resource_concluded(resource):
    global _fw_update_pending
    try:
        from urns.resource import COMPLETE
        if resource.status == COMPLETE:
            data = resource.data
            fields = umsgpack.unpackb(data)
            cmd = fields.get("cmd", "")
            if cmd == "update_file":
                import updater
                resp = updater.handle_update(fields)
                if resp.get("status") == "ok":
                    _log("Saved: {}".format(resp.get("filename")), 1)
                    _fw_update_pending = True
                else:
                    _log("Staging failed: {}".format(resp.get("error")), 1)
        else:
            _log("Resource transfer did not complete successfully", 1)
    except Exception as e:
        _log("Resource processing error: " + str(e), 1)


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
        if _hub_identity is None and data_str == "agronomi_hub":
            ident = Identity.recall(destination_hash)
            if ident is not None:
                _hub_identity = ident
                _log("Hub discovered: " + ident.hexhash)
    except Exception as e:
        _log("Announce handler error: " + str(e), 2)


def _build_telemetry_fields(interface_name):
    readings = read_all(config)
    return {
        "cmd": "telemetry",
        "dev_id": config.NODE_NAME,
        "type": config.DEVICE_TYPE,
        "fw": config.FIRMWARE_VERSION,
        "vent_open": _vent_state,
        "fan_on": _fan_state,
        "shade_pct": _shade_pct_state,
        "bat": readings.get("battery_v", -1.0),
        "if": interface_name,
    }


async def main():
    global _hub_identity, _fw_update_pending, _rns_instance

    gc.collect()
    _log("=" * 40)
    _log("AN-GREENHOUSE-01 boot — µReticulum v" + config.FIRMWARE_VERSION)
    _log("=" * 40)

    wifi_connected = False
    wifi_interfaces = [
        i for i in config.CONFIG.get("interfaces", [])
        if i.get("enabled", True)
        and i.get("type", "") in ("UDPInterface", "TCPClientInterface")
    ]
    if wifi_interfaces and config.WIFI_SSID:
        try:
            ip = _connect_wifi(config.WIFI_SSID, config.WIFI_PASS)
            _log("WiFi connected — IP: " + ip)
            wifi_connected = True
        except Exception as e:
            _log("WiFi offline: " + str(e))

    try:
        rns = Reticulum(loglevel={0: 0, 1: 3, 2: 6}.get(config.DEBUG, 0))
        rns.config = config.CONFIG
        storage = rns.storagepath
        ident = _find_or_create_identity(storage)
        rns.identity = ident
        rns.setup_interfaces()
        _rns_instance = rns
        _log("µReticulum initialised — identity: " + ident.hexhash)
    except Exception as e:
        _log("FATAL: µReticulum init failed: " + str(e))
        time.sleep(10)
        machine.reset()
        return

    if wifi_connected:
        _suspend_ble_interface(rns)

    from urns.transport import Transport

    poll_tasks = []
    for iface in rns.interfaces:
        if hasattr(iface, "poll_loop"):
            poll_tasks.append(asyncio.create_task(iface.poll_loop()))
    poll_tasks.append(asyncio.create_task(Transport.job_loop()))
    _log("Started transport task loops")

    cmd_dest = Destination(
        ident, Destination.IN, Destination.SINGLE,
        config.NODE_APP, config.NODE_ASPECT,
    )
    cmd_dest.set_proof_strategy(Destination.PROVE_ALL)
    cmd_dest.set_packet_callback(_on_packet_received)
    cmd_dest.set_link_established_callback(_on_link_established)
    cmd_dest._announce_handler = _on_announce
    _log("Node aspect registered: " + config.NODE_APP + "." + config.NODE_ASPECT)

    app_data = (config.RNS_ANNOUNCE_PREFIX + ":" + config.NODE_NAME).encode("utf-8")
    cmd_dest.announce(app_data=app_data)
    _log("Announced on " + config.NODE_APP + "." + config.NODE_ASPECT)

    hub_deadline = time.ticks_add(time.ticks_ms(), 30000)
    while _hub_identity is None and time.ticks_diff(hub_deadline, time.ticks_ms()) > 0:
        await asyncio.sleep_ms(500)

    if _hub_identity is not None:
        iface_name = _get_rns_interface_name(rns)
        hub_dest = Destination(
            _hub_identity, Destination.OUT, Destination.SINGLE,
            config.HUB_APP, config.HUB_ASPECT,
        )
        # fw_check FIRST — must not depend on the sensor read inside
        # _build_telemetry_fields; otherwise a bad pin / disconnected ADC
        # would deadlock OTA recovery from the hub.
        Packet(hub_dest, umsgpack.packb({
            "cmd": "fw_check",
            "dev_id": config.NODE_NAME,
            "fw": config.FIRMWARE_VERSION,
        })).send()
        _log("fw_check sent")
        try:
            Packet(hub_dest, umsgpack.packb(_build_telemetry_fields(iface_name))).send()
            _log("Initial telemetry sent")
        except Exception as e:
            _log("Initial telemetry skipped (sensor read failed: " + str(e) + ")", 1)

    try:
        import updater as _updater
        _updater.confirm_running_firmware()
    except Exception as e:
        _log("confirm_running_firmware failed: " + str(e), 1)

    async def periodic_announce():
        while True:
            await asyncio.sleep(config.ANNOUNCE_INTERVAL_SEC)
            try:
                cmd_dest.announce(app_data=app_data)
                _log("Re-announced", 2)
            except Exception as e:
                _log("Re-announce error: " + str(e), 1)

    async def periodic_telemetry():
        while True:
            await asyncio.sleep(config.TELEMETRY_INTERVAL_SEC)
            try:
                if _hub_identity is not None:
                    iface_name = _get_rns_interface_name(rns)
                    hub_dest = Destination(
                        _hub_identity, Destination.OUT, Destination.SINGLE,
                        config.HUB_APP, config.HUB_ASPECT,
                    )
                    Packet(hub_dest, umsgpack.packb(_build_telemetry_fields(iface_name))).send()
                    _log("Telemetry sent", 2)
            except Exception as e:
                _log("Telemetry error: " + str(e), 1)

    async def keep_alive():
        while True:
            await asyncio.sleep(60)

    _log("Entering main event loop")
    asyncio.create_task(periodic_announce())
    asyncio.create_task(periodic_telemetry())
    asyncio.create_task(keep_alive())
    while True:
        await asyncio.sleep(3600)


asyncio.run(main())
