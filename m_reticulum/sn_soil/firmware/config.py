"""µReticulum — Soil Sensor Node Configuration (SN-SOIL-01)

Hardware: ESP32-C6 Super Mini
Transport: BLE → RAK4631 RNode (LoRa), WiFi TCP to hub
Sensors:   capacitive soil moisture (ADC), DS18B20 (1-wire), battery ADC
"""

# ---- Node identity ----
NODE_NAME = "SN-SOIL-01"
DEVICE_TYPE = "soil_node"
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
ENABLE_DEEPSLEEP = False
SLEEP_INTERVAL_SEC = 300

# ---- Sensor pins ----
PIN_SOIL_ADC = 2
PIN_ONEWIRE = 3

# ---- Soil moisture calibration ----
CALIB_DRY_V = 1.815  # voltage in air
CALIB_WET_V = 1.378  # voltage in water

# ---- Battery ADC (100k/100k divider on GPIO1) ----
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
RNS_ANNOUNCE_PREFIX = "agronomi-sensor"
