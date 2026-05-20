"""µReticulum — Pump Actuator Node Configuration (AN-PUMP-01)

Transport topology:
  AN-PUMP-01 has BLE + WiFi only (no LoRa module).

  PRIMARY (minimal deployment): BLE → RAK4631 RNode (via RNodeBLEInterface)
  The node connects directly to the RNode over BLE using KISS/NUS framing.
  No gateway needed. The RNode bridges BLE → LoRa → Hub.

  SECONDARY (extended deployment): BLE → ESP32-C6 gateway (via BLEClientInterface)
  Only needed when the RNode can't reach this node directly.

  TERTIARY: WiFi UDP (greenhouse/indoor with WiFi coverage)

  FUTURE: If an E32 or SX1262 LoRa module is wired to the ESP32-C6,
  the LoRa interface configs below can be enabled for direct LoRa
  communication to the Hub's RNode, bypassing BLE entirely.
"""

# ---- Node settings ----
NODE_NAME = "AN-PUMP-01"
DEVICE_TYPE = "pump_node"
FIRMWARE_VERSION = "2.0.0-mr"  # -mr = microreticulum

# ---- WiFi (optional, for indoor/greenhouse use) ----
WIFI_SSID = ""
WIFI_PASS = ""

# ---- Deep sleep ----
ENABLE_DEEPSLEEP = (
    False  # Actuator nodes NEVER deep sleep — must stay awake for commands
)
SLEEP_INTERVAL_SEC = 0  # Unused (always-on)

# ---- Actuator pins (ESP32-C6 Super Mini) ----
# TODO: assign actual GPIO pins for pump relay when hardware is finalised
PIN_PUMP_RELAY = 5  # GPIO5 — pump relay control (active HIGH)

# ---- Battery ----
# Set to True if the board has a 100k/100k voltage divider on an ADC pin
HAS_BAT_RESISTORS = False
PIN_BAT_ADC = (
    1  # Battery voltage divider ADC pin (unused if HAS_BAT_RESISTORS is False)
)
BAT_DIVIDER_RATIO = 2.0

# ---- DEBUG levels: 0 = silent, 1 = messages, 2 = full ----
DEBUG = 1

# ---- RNS config ----
# ESP32-C6 has BLE + WiFi only. LoRa requires external module (FUTURE).
# PRIMARY: BLE → RNode via RNodeBLEInterface (minimal deployment, no gateway needed)
# SECONDARY: BLE → ESP32-C6 gateway via BLEClientInterface (extended deployment only)
# TERTIARY: WiFi UDP in greenhouses with WiFi coverage
# RNS handles path selection automatically — if both BLE and WiFi are available,
# it uses the fastest path.
CONFIG = {
    "loglevel": 2,
    "enable_transport": False,  # Actuator nodes don't relay
    "interfaces": [
        # --- RNode BLE (PRIMARY — connects directly to RNode over BLE) ---
        # RNodeBLEInterface is the primary transport for field deployment.
        # It connects to a RAK4631 RNode's BLE NUS service using KISS framing.
        # Enable BLE on the RNode with: rnodeconf --bluetooth-on
        # {
        #     "type": "RNodeBLEInterface",
        #     "name": "RNode BLE",
        #     "target_name": "",           # Auto-discover any RNode (empty = scan for NUS UUID)
        #     "frequency": 868000000,      # 868 MHz EU band
        #     "bandwidth": 125000,
        #     "txpower": 17,
        #     "spreadingfactor": 7,
        #     "codingrate": 5,
        #     "enabled": True,
        # },
        # --- BLE Client (SECONDARY — connects to ESP32-C6 gateway via BLE) ---
        # BLEClientInterface connects to a BLE GATT server (ESP32-C6 gateway).
        # Only needed when the RNode can't reach all nodes directly via BLE.
        # {
        #     "type": "BLEClientInterface",
        #     "name": "BLE to Gateway",
        #     "target_name": "BLEGateway",
        #     "enabled": True,
        # },
        # --- WiFi UDP (tertiary — greenhouse/indoor only) ---
        # Uncomment and fill WIFI_SSID/WIFI_PASS above for indoor use:
        # {
        #     "type": "UDPInterface",
        #     "name": "WiFi UDP",
        #     "enabled": True,
        #     "listen_port": 4242,
        #     "forward_port": 4242,
        # },
        # --- FUTURE: LoRa via E32 (requires E32-900T20D module wired to ESP32-C6) ---
        # Enable for direct LoRa path to Hub's RNode, bypassing gateway entirely.
        # --- E32 LoRa (Waveshare RP2040 + E32-900T20D) ---
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
        #     "air_rate": 2,  # 2400 bps
        #     "tx_power": 3,  # 10 dBm
        # },
        # --- FUTURE: LoRa via SX1262 SPI (requires Wio-SX1262 module) ---
        # {
        #     "type": "LoRaInterface",
        #     "name": "LoRa SX1262",
        #     "enabled": True,
        #     "spi_bus": 1,
        #     "sck_pin": 7,
        #     "mosi_pin": 9,
        #     "miso_pin": 8,
        #     "cs_pin": 41,
        #     "busy_pin": 40,
        #     "dio1_pin": 39,
        #     "reset_pin": 42,
        #     "freq_khz": 868000,
        #     "sf": 7,
        #     "bw": "125",
        #     "coding_rate": 5,
        #     "tx_power": 14,
        #     "syncword": 5156,
        #     "dio2_rf_sw": True,
        #     "dio3_tcxo_millivolts": 1800,
        # },
        # --- BLE (nearby gateway) ---
        # {
        #     "type": "BLEInterface",
        #     "name": "BLE",
        #     "enabled": True,
        # },
        # --- WiFi UDP (greenhouse/indoor only) ---
        # {
        #     "type": "UDPInterface",
        #     "name": "WiFi UDP",
        #     "enabled": True,
        #     "listen_port": 4242,
        #     "forward_port": 4242,
        # },
    ],
}

# ---- RNS destinations ----
# IN:   farm.gateway_commands — receive commands from hub (SINGLE, announceable)
# OUT:  farm.telemetry_readings — send status telemetry
# OUT:  farm.commands_control — send ACKs back to hub
COMMAND_IN_APP = "farm"
COMMAND_IN_ASPECT = "gateway_commands"

TELEMETRY_APP = "farm"
TELEMETRY_ASPECT = "telemetry_readings"

ACK_APP = "farm"
ACK_ASPECT = "commands_control"

# ---- RNS announce prefix ----
RNS_ANNOUNCE_PREFIX = "agronomi-actuator"

# Hub announce app_data filter — only accept announces from hubs
HUB_ANNOUNCE_FILTER = "AgroNomi Hub"

# ---- Actuator state ----
PUMP_ON = False  # Initial pump state

# ---- Status telemetry interval ----
TELEMETRY_INTERVAL_SEC = 60  # Send status every 60 seconds
ANNOUNCE_INTERVAL_SEC = 300  # Re-announce every 5 minutes
