"""
MicroReticulum ESP32-C6 gateway node (no sensors)
"""

# ---- Node settings ----
from config import WIFI_SSID, WIFI_PASS, NODE_NAME, DEBUG, CONFIG

import machine, gc, time
from urns.ble_interface import BLEInterface

gc.collect()

# Simple WiFi connection helper
def connect_wifi(ssid, password, timeout=15):
    import network
    wlan = network.WLAN(network.STA_IF)
    # Reset the interface to clear any stale state
    wlan.active(False)
    time.sleep(0.1)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        start = time.ticks_ms()
        while not wlan.isconnected():
            if (time.ticks_diff(time.ticks_ms(), start) // 1000) > timeout:
                raise RuntimeError("WiFi connection timed out")
            time.sleep(0.2)
    return wlan.ifconfig()[0]

# Connect if any enabled network interface requires WiFi
if any(i.get('type') in ('TCPClientInterface', 'UDPInterface') for i in CONFIG.get('interfaces', [])):
    ip = connect_wifi(WIFI_SSID, WIFI_PASS)
    if DEBUG >= 1:
        print("[GW] WiFi connected – IP:", ip)

# Initialise µReticulum core
from urns import Reticulum
rns = Reticulum(loglevel={0:0, 1:0, 2:2}.get(DEBUG, 1))
# Apply our configuration (interfaces, transport etc.)
rns.config = CONFIG

# Create a SINGLE IN destination for inbound traffic (acts as the gateway entrypoint)
from urns.destination import Destination
gateway_dest = Destination(rns.identity, Destination.IN, Destination.SINGLE, "gateway", NODE_NAME)
# Require delivery proofs so senders know packets arrived
gateway_dest.set_proof_strategy(Destination.PROVE_ALL)

# Simple packet callback – just log received data
def on_packet(data, packet):
    try:
        txt = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    except Exception:
        txt = "<binary>"
    print("[GW] Received packet from", packet.source_hash.hex()[:8], ":", txt)

# Simple BLE data handler – just logs the raw bytes received via BLE
def ble_handler(data, _):
    try:
        txt = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    except Exception:
        txt = "<binary>"
    print("[BLE] Received packet via BLE:", txt)

gateway_dest.set_packet_callback(on_packet)
# Instantiate BLE interface (advertises automatically)
ble = BLEInterface(packet_handler=ble_handler)

# Announce our SINGLE destination – discovery via the shared Reticulum instance
gateway_dest.announce()
if DEBUG >= 1:
    print("[GW] Announced SINGLE destination", gateway_dest.hexhash)

# Keep the node alive – a minimal async loop (no deep‑sleep for now)
import uasyncio as asyncio

async def keep_alive():
    while True:
        await asyncio.sleep(60)

try:
    asyncio.run(keep_alive())
except KeyboardInterrupt:
    if DEBUG >= 1:
        print("[GW] Shutdown requested")
    rns.shutdown()
