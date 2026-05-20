"""Quick BLE test — scan for RNode and attempt connection."""

import gc

import uasyncio as asyncio
from urns.interfaces.rnode_ble import RNodeBLEInterface


async def test():
    print("=== BLE RNode Test ===")
    gc.collect()
    print(f"Free mem: {gc.mem_free()}")

    config = {
        "type": "RNodeBLEInterface",
        "name": "RNode BLE Test",
        "target_name": "",  # Auto-discover
        # Automatic pairing: set serial_port to the RNode's USB serial device
        # to automatically obtain the pairing PIN. Leave empty for manual pairing.
        "serial_port": "/dev/cu.usbmodem23401",
        # Manual pairing passkey (only used if serial_port is not set).
        # After first successful pair, the bond is stored and this can be 0.
        # Leave at 0 for third-party BLE bridges (Heltec V3) using Just Works.
        "pairing_passkey": 0,
        "frequency": 868000000,
        "bandwidth": 125000,
        "txpower": 17,
        "spreadingfactor": 11,
        "codingrate": 5,
        "enabled": True,
    }

    iface = RNodeBLEInterface(config)
    print(f"Interface created: {iface}")
    print(f"BLE init: {iface._ble is not None}")
    print(f"Scanning: {iface._scanning}")

    # Start poll loop
    task = asyncio.create_task(iface.poll_loop())
    print("Poll loop started, waiting for connection...")

    # Wait up to 30 seconds for the interface to come online
    for i in range(60):
        await asyncio.sleep_ms(500)
        if iface.online:
            print(f"ONLINE! Interface online after ~{i * 0.5}s")
            print(f"  Name: {iface.name}")
            print(f"  Frequency: {iface.frequency}")
            print(f"  Bandwidth: {iface.bandwidth}")
            print(f"  SF: {iface.spreadingfactor}")
            print(f"  CR: {iface.codingrate}")
            if iface.rnode_platform:
                print(f"  Platform: {iface.rnode_platform}")
            if iface.rnode_fw_version:
                print(f"  FW version: {iface.rnode_fw_version}")
            break
        if i % 4 == 0:
            print(
                f"  {i * 0.5}s: scanning={iface._scanning} conn={iface._conn_handle} "
                f"detected={iface._detected} encrypted={iface._encrypted} "
                f"mtu={iface._negotiated_mtu} pairing={iface._pairing_attempted}"
            )

    if not iface.online:
        print("FAILED: Interface did not come online in 30s")
        print(f"  scanning={iface._scanning}")
        print(f"  conn_handle={iface._conn_handle}")
        print(f"  detected={iface._detected}")
        print(f"  radio_configured={iface._radio_configured}")
        print(f"  encrypted={iface._encrypted}")
        print(f"  pairing_attempted={iface._pairing_attempted}")
        print(f"  notify_enabled={iface._notify_enabled}")
        print(f"  mtu={iface._negotiated_mtu}")

    iface.close()
    print("Test complete.")


asyncio.run(test())
