"""µReticulum — ESP32-C6 Gateway Node Configuration (AgroNomi GW 02)

*** OPTIONAL — Only needed for extended deployments ***

This gateway is OPTIONAL in the minimal deployment. In the simplest setup, ESP32-C6
nodes connect directly to an RNode over BLE using BLEClientInterface — no gateway
required.

This gateway is only needed when:
  1. WiFi bridging: greenhouse nodes need WiFi but the RNode location has no WiFi.
     The gateway bridges BLE/WiFi nodes to the RNS network via mimi.
  2. BLE relay: the RNode can't physically reach all sensor nodes via BLE
     (e.g., nodes in metal enclosures or far from the RNode). The gateway acts
     as a BLE range extender.

In the minimal deployment (Hub + RNode + sensor nodes), this gateway is NOT used.

Transport topology (extended deployment):
  ESP32-C6 gateway ←USB/Serial→ mimi (Ubuntu) ←USB→ RNode (RAK4631) ←LoRa→ Hub

  The gateway's role:
    1. Accept RNS packets from BLE-connected sensor nodes
    2. Forward them via its WiFi/Serial interface to mimi's rnsd
    3. Receive commands from the hub via mimi's rnsd → forward to BLE nodes

  Alternatively, if mimi runs rnsd with an RNode, all RNS traffic flows
  through rnsd and the ESP32-C6 just bridges BLE nodes into that network.

  FUTURE: If an E32 or SX1262 LoRa module is wired to this ESP32-C6,
  it could act as a standalone LoRa gateway without needing mimi.
"""

# ---- Node settings ----
# ---- WiFi (fill in for your deployment — NEVER commit real credentials) ----
WIFI_SSID = ""
WIFI_PASS = ""
NODE_NAME = "AgroNomi GW 02"

# DEBUG levels: 0 = silent, 1 = messages & announces only, 2 = full debug
DEBUG = 2

# ---- RNS config ----
# Gateway bridges BLE nodes ↔ WiFi/Serial ↔ mimi rnsd ↔ RNode LoRa ↔ Hub
# enable_transport should be True so the gateway can relay between BLE and WiFi
CONFIG = {
    "loglevel": 3,
    "enable_transport": True,
    "interfaces": [
        # --- WiFi UDP (connects to mimi's network for RNS traffic) ---
        # mimi runs rnsd with an AutoInterface or TCPClientInterface,
        # and this gateway sends/receives RNS packets via WiFi UDP.
        {
            "type": "UDPInterface",
            "name": "WiFi UDP",
            "enabled": True,
            "listen_port": 4242,
            "forward_port": 4242,
        },
        # --- BLE (accepts RNS packets from BLE-connected sensor nodes) ---
        # BLEInterface is instantiated manually in main.py (not via setup_interfaces)
        # because it's not in urns/interfaces/. It receives RNS packets from
        # nearby ESP32-C6 sensor/actuator nodes and forwards them to the
        # WiFi interface via Transport mode.
        # --- FUTURE: Serial interface (direct USB to mimi) ---
        # If WiFi is not available, serial connection to mimi can be used:
        # {
        #     "type": "SerialInterface",
        #     "name": "USB Serial",
        #     "enabled": True,
        #     "uart_id": 1,
        #     "speed": 115200,
        # },
        # --- FUTURE: LoRa via E32 (requires E32 module wired to ESP32-C6) ---
        # Enable for direct LoRa path, bypassing mimi's RNode entirely:
        # {
        #     "type": "E32Interface",
        #     "name": "LoRa E32",
        #     "enabled": True,
        #     "uart_id": 1,
        #     "tx_pin": 4,
        #     "rx_pin": 5,
        #     "speed": 9600,
        #     "m0_pin": 15,
        #     "m1_pin": 2,
        #     "aux_pin": 6,
        #     "auto_configure": False,
        #     "channel": 6,
        #     "air_rate": 2,
        #     "tx_power": 3,
        # },
    ],
}

# ---- Sensor Network config ----
# LXMF Destination address (discovered via announce-based lookup if empty)
SENSOR_HUB = ""
