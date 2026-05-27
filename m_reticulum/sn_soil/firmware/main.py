"""µReticulum — Soil Moisture/Temp Node Firmware (SN-SOIL-01)

Lifecycle:
  1. Boot → init RNS / interfaces / identity
  2. Announce on farm.node, wait for hub announce
  3. Read sensors (soil moisture + temp + battery)
  4. Send telemetry as RNS.Packet to hub's farm.hub
  5. Send fw_check
  6. Listen for commands / OTA Link (60s, extended to 300s if firmware push pending)
  7. Deep sleep

All transport is direct RNS Link + Packet — no LXMF.  OTA uses the
chunked Link protocol from m_reticulum/sn_support/firmware/updater.py
(shared, byte-identical across nodes).
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

_hub_identity = None
_fw_update_pending = False
_rns_instance = None
_ble_interface_class = None
_ble_config_dict = None


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


def _suspend_ble_interface(rns):
    """Take BLE LoRa interface offline during WiFi-mode OTA — see
    m_reticulum/sn_support/firmware/main.py for the full rationale
    (urns doesn't pin link-associated packets to their attached_interface).
    """
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


def _on_packet_received(data, packet):
    try:
        fields = umsgpack.unpackb(data)
        cmd = fields.get("cmd", "")

        _log("Packet received: cmd={}".format(cmd), 1)

        if cmd == "fw_check_ack":
            if fields.get("pending"):
                global _fw_update_pending
                _fw_update_pending = True
                _log("Hub signaled pending update — keeping active connection", 1)
            else:
                _log("Hub has no firmware update pending", 1)
                _resume_ble_interface(_rns_instance)
            return

        if cmd == "execute":
            cmd_type = fields.get("type", "")
            _handle_hardware_command(cmd_type)

    except Exception as e:
        _log("Packet callback error: " + str(e), 1)


def _handle_hardware_command(cmd):
    # SN-SOIL is a sensor node — no relays or actuators attached.  Stub
    # logs anything unexpected for diagnostic purposes.
    _log("Unknown/unsupported command for sensor node: " + str(cmd), 1)


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
                    _log(
                        "Firmware staging complete ({} files). rebooting...".format(
                            len(staged)
                        ),
                        1,
                    )
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


def _build_telemetry_fields(readings, interface_name):
    fields = {
        "cmd": "telemetry",
        "dev_id": config.NODE_NAME,
        "type": config.DEVICE_TYPE,
        "fw": config.FIRMWARE_VERSION,
        "if": interface_name,
    }
    bat = readings.get("bat_v")
    if bat is not None:
        fields["bat"] = bat
    moist = readings.get("soil_moisture_pct")
    if moist is not None:
        fields["soil_moist"] = moist
    soil_t = readings.get("soil_temp_c")
    if soil_t is not None and readings.get("soil_temp_valid", True):
        fields["soil_temp"] = soil_t
    return fields


async def main():
    global _hub_identity, _fw_update_pending, _rns_instance

    gc.collect()
    _log("=" * 40)
    _log("SN-SOIL-01 boot — µReticulum v" + config.FIRMWARE_VERSION)
    _log("=" * 40)

    wifi_connected = False
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
            wifi_connected = True
        except Exception as e:
            _log("WiFi offline, using LoRa: " + str(e))

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
        _deep_sleep()
        return

    if wifi_connected:
        _suspend_ble_interface(rns)

    from urns.transport import Transport

    poll_tasks = []
    for iface in rns.interfaces:
        if hasattr(iface, "poll_loop"):
            task = asyncio.create_task(iface.poll_loop())
            poll_tasks.append(task)

    transport_task = asyncio.create_task(Transport.job_loop())
    poll_tasks.append(transport_task)
    _log("Started transport task loops")

    _log("Waiting for interfaces...")
    iface_timeout = 5
    iface_deadline = time.ticks_add(time.ticks_ms(), iface_timeout * 1000)

    while time.ticks_diff(iface_deadline, time.ticks_ms()) > 0:
        online = [i for i in rns.interfaces if getattr(i, "online", False)]
        if online:
            _log("Interface online: " + str(online[0]))
            break
        await asyncio.sleep_ms(500)

    cmd_dest = Destination(
        ident,
        Destination.IN,
        Destination.SINGLE,
        config.NODE_APP,
        config.NODE_ASPECT,
    )
    cmd_dest.set_proof_strategy(Destination.PROVE_ALL)
    cmd_dest.set_packet_callback(_on_packet_received)
    cmd_dest.set_link_established_callback(_on_link_established)
    cmd_dest._announce_handler = _on_announce

    _log("Node aspect registered: " + config.NODE_APP + "." + config.NODE_ASPECT)

    # Announce + fw_check FIRST — a sensor failure must never block the node
    # from joining the hub, otherwise we deadlock (no telemetry → no fw_check
    # → no OTA fix possible from the hub).
    app_data = (config.RNS_ANNOUNCE_PREFIX + ":" + config.NODE_NAME).encode("utf-8")
    cmd_dest.announce(app_data=app_data)
    _log("Announced on " + config.NODE_APP + "." + config.NODE_ASPECT)

    _log("Waiting for hub announce...")
    hub_timeout = 60
    hub_deadline = time.ticks_add(time.ticks_ms(), hub_timeout * 1000)

    while _hub_identity is None and time.ticks_diff(hub_deadline, time.ticks_ms()) > 0:
        await asyncio.sleep_ms(500)

    iface_name = _get_rns_interface_name(rns)
    hub_dest = None
    if _hub_identity is not None:
        hub_dest = Destination(
            _hub_identity,
            Destination.OUT,
            Destination.SINGLE,
            config.HUB_APP,
            config.HUB_ASPECT,
        )

        fw_check_fields = {
            "cmd": "fw_check",
            "dev_id": config.NODE_NAME,
            "fw": config.FIRMWARE_VERSION,
        }
        Packet(hub_dest, umsgpack.packb(fw_check_fields)).send()
        _log("fw_check packet sent to Hub", 1)
    else:
        _log("WARN: Hub not found")

    # Sensors come AFTER hub registration so a sensor failure can never
    # take the node offline.
    readings = {}
    _log("Reading sensors...")
    try:
        readings = read_all(config)
        if readings.get("soil_moisture_pct") is not None:
            _log("Soil moisture: {:.1f}%".format(readings["soil_moisture_pct"]))
        if readings.get("soil_temp_c") is not None:
            _log("Soil temp:     {:.1f}°C (valid={})".format(
                readings["soil_temp_c"], readings.get("soil_temp_valid", True)))
        if readings.get("bat_v") is not None:
            _log("Battery:        {:.2f}V".format(readings["bat_v"]))
    except Exception as e:
        _log("Sensor read failed: " + str(e) + " — continuing without readings", 1)

    if hub_dest is not None:
        telemetry_fields = _build_telemetry_fields(readings, iface_name)
        Packet(hub_dest, umsgpack.packb(telemetry_fields)).send()
        _log("Telemetry packet routed to Hub: " + _hub_identity.hexhash)

    _log("Listening for commands...")

    # Firmware is now fully booted, networked, and reached the listen
    # loop — confirm this build so boot.py won't roll back next restart.
    try:
        import updater as _updater
        _updater.confirm_running_firmware()
    except Exception as e:
        _log("confirm_running_firmware failed: " + str(e), 1)

    listen_end = time.ticks_add(time.ticks_ms(), 60000)

    while True:
        now = time.ticks_ms()
        if _fw_update_pending:
            deadline = time.ticks_add(now, 300000)
            if time.ticks_diff(deadline, listen_end) > 0:
                listen_end = deadline
            _fw_update_pending = False
            _log("Firmware pending — extended listen to 300 s", 1)

        if time.ticks_diff(listen_end, now) <= 0:
            break
        await asyncio.sleep_ms(200)

    _resume_ble_interface(_rns_instance)

    if not config.ENABLE_DEEPSLEEP:
        _log("DEBUG MODE — staying alive.")
        await _idle_loop()
    else:
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


async def _idle_loop():
    while True:
        await asyncio.sleep_ms(5000)


asyncio.run(main())
