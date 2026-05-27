"""µReticulum — Pump Actuator Node Configuration (AN-PUMP-01)

Hardware: ESP32-C6 Super Mini
Transport: BLE → RAK4631 RNode (LoRa), WiFi TCP to hub
Actuator:  pump relay (active HIGH)
"""

# ---- Node identity ----
NODE_NAME = "AN-PUMP-01"
DEVICE_TYPE = "pump_node"
FIRMWARE_VERSION = "2.0.1-mr"

# ---- WiFi ----
WIFI_SSID = ""
WIFI_PASS = ""
try:
    from secrets import WIFI_PASS as _pass
    from secrets import WIFI_SSID as _ssid

    WIFI_SSID = _ssid
    WIFI_PASS = _pass
except ImportError:
    pass

# ---- Deep sleep ----
# Actuator nodes stay awake — they must respond to commands.
ENABLE_DEEPSLEEP = False
SLEEP_INTERVAL_SEC = 0

# ---- Actuator pins ----
PIN_PUMP_RELAY = 5

# ---- Battery ADC (100k/100k divider on GPIO1) ----
HAS_BAT_RESISTORS = False
PIN_BAT_ADC = 1
BAT_DIVIDER_RATIO = 2.0

# ---- Logging: 0=silent 1=info 2=debug ----
DEBUG = 1

# ---- RNS interfaces ----
CONFIG = {
    "loglevel": 2,
    "enable_transport": False,
    "interfaces": [
        {
            "type": "RNodeBLEInterface",
            "name": "RNode BLE",
            "target_name": "",            # auto-discover by NUS UUID
            "pairing_passkey": 0,         # overwritten from ble_pin.txt at boot
            "frequency": 868000000,
            "bandwidth": 125000,
            "spreadingfactor": 11,
            "codingrate": 5,
            "txpower": 17,
            "enabled": True,
        },
        {
            "type": "UDPInterface",
            "name": "WiFi UDP",
            "listen_port": 4242,
            "forward_port": 4242,
            "enabled": False,
        },
        {
            "type": "TCPClientInterface",
            "name": "Field node to AgroNomi TCP",
            "target_host": "Urbans-Mac-mini.local",
            "target_port": 4243,
            "enabled": True,
        },
    ],
}

# ---- Unified Hub & Node destinations ----
HUB_APP = "farm"
HUB_ASPECT = "hub"
NODE_APP = "farm"
NODE_ASPECT = "node"

# ---- Announce ----
RNS_ANNOUNCE_PREFIX = "agronomi-actuator"

# ---- Actuator initial state ----
PUMP_ON = False

# ---- Telemetry / announce intervals ----
TELEMETRY_INTERVAL_SEC = 60
ANNOUNCE_INTERVAL_SEC = 300
