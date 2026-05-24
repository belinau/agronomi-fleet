"""µReticulum — Soil Node Configuration (SN-SOIL-01)

Transport topology:
  SN-SOIL-01 has BLE + WiFi only (no LoRa module).

  PRIMARY: BLE → RAK4631 RNode (via RNodeBLEInterface)
  SECONDARY: WiFi UDP (greenhouse/indoor with WiFi coverage)
"""

# ---- Node settings ----
NODE_NAME = "SN-SOIL-01"
DEVICE_TYPE = "soil_node"
FIRMWARE_VERSION = "2.0.0-mr"  # -mr = microreticulum

# ---- WiFi ----
WIFI_SSID = "FRITZ!Box 5490 ME"
WIFI_PASS = "99141440711753817435"

# ---- Deep sleep ----
ENABLE_DEEPSLEEP = False  # Set to True for production
SLEEP_INTERVAL_SEC = 300

# ---- Sensor pins (ESP32-C6 Super Mini) ----
PIN_SOIL_ADC = 2
PIN_ONEWIRE = 3
PIN_BAT_ADC = 1

# ---- Soil moisture calibration ----
CALIB_DRY_V = 1.815  # Voltage in air
CALIB_WET_V = 1.378  # Voltage in water

# ---- DEBUG levels: 0 = silent, 1 = messages, 2 = full ----
DEBUG = 1

# ---- RNS interfaces ----
CONFIG = {
    "loglevel": 2,
    "enable_transport": False,
    "interfaces": [
        {
            "type": "RNodeBLEInterface",
            "name": "RNode BLE",
            "target_name": "",  # auto-discover by NUS UUID
            "pairing_passkey": 0,  # overwritten from ble_pin.txt at boot
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
            "enabled": True,
        },
    ],
}

# ---- Hub destinations ----
TELEMETRY_APP = "farm"
TELEMETRY_ASPECT = "telemetry_readings"
COMMAND_APP = "farm"
COMMAND_ASPECT = "gateway_commands"

# ---- RNS announce prefix ----
RNS_ANNOUNCE_PREFIX = "agronomi-sensor"
HUB_ANNOUNCE_FILTER = "agronomi"

# ---- Hub LXMF address ----
SENSOR_HUB = ""
