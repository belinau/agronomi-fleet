"""µReticulum — Air Quality Node Configuration (SN-AIR-01)

Transport topology:
  SN-AIR-01 has BLE + WiFi only (no LoRa module).

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
NODE_NAME = "SN-AIR-01"
DEVICE_TYPE = "air_node"
FIRMWARE_VERSION = "2.0.0-mr"  # -mr = microreticulum

# ---- WiFi (for greenhouse/indoor use only) ----
# Leave blank for field deployment (BLE-only)
WIFI_SSID = "FRITZ!Box 5490 ME"
WIFI_PASS = "99141440711753817435"

# ---- Deep sleep ----
ENABLE_DEEPSLEEP = True
SLEEP_INTERVAL_SEC = 300  # 5 minutes

# ---- Sensor pins (ESP32-C6 Super Mini) ----
PIN_DHT = 2  # DHT22 data pin
PIN_BAT_ADC = 1  # Battery voltage divider (100k/100k → GPIO1)

# ---- Battery ----
HAS_BAT_RESISTORS = False  # 100k/100k divider on board
BAT_DIVIDER_RATIO = 2.0  # V_bat / V_adc for equal resistors

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
    "enable_transport": False,  # Sensor nodes don't relay
    "interfaces": [
        # --- RNode BLE (PRIMARY — connects directly to RNode over BLE) ---
        # RNodeBLEInterface is the primary transport for field deployment.
        # It connects to a RAK4631 RNode BLE serial.
        # Enable BLE on the RNode with: rnodeconf --bluetooth-on
        {
            "type": "RNodeBLEInterface",
            "name": "RNode BLE",
            "target_name": "",  # Auto-discover any RNode (empty = scan for NUS UUID)
            # Pairing passkey for official RNode firmware (RAK4631 nRF52).
            # The RNode requires MITM-protected pairing with a passkey.
            #
            # AUTOMATIC PAIRING (recommended): Set serial_port to the RNode's
            # USB serial device path. The interface will automatically:
            #   1. Open the RNode's serial port
            #   2. Send CMD_BT_CTRL 0x02 to put it in pairing mode
            #   3. Read the random 6-digit PIN from the CMD_BT_PIN response
            #   4. Use the PIN to pair over BLE
            # After first successful pairing, bond keys are stored and the
            # serial step is skipped on future connections.
            #
            # MANUAL PAIRING (fallback): If serial_port is not set:
            #   1. Run: rnodeconf /dev/cu.usbmodemXXXX --bluetooth-pair
            #   2. Read the 6-digit PIN from the output
            #   3. Set pairing_passkey to that PIN below
            #   4. After first successful pair, you can set it back to 0
            #
            # For third-party BLE bridges (Heltec V3 RTReticulum) that use
            # Just Works pairing, leave both at 0.
            "pairing_passkey": 0,
            "serial_port": "/dev/cu.usbmodem23401",  # RNode USB serial for auto-pairing
            "frequency": 868000000,  # 868 MHz — must match hub RNode frequency
            "bandwidth": 125000,
            "txpower": 17,
            "spreadingfactor": 11,  # Must match hub RNode SF (SF11 for range)
            "codingrate": 5,
            "enabled": True,
        },
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
        {
            "type": "UDPInterface",
            "name": "WiFi UDP",
            "enabled": True,
            "listen_port": 4242,
            "forward_port": 4242,
        },
        # --- FUTURE: LoRa via E32 (requires E32-900T20D module wired to ESP32-C6) ---
        # Enable for direct LoRa path to Hub's RNode, bypassing gateway entirely.
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
        #     "tx_power": 3,  # 10 dBm (safe for battery)
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
    ],
}

# ---- Hub discovery aspect ----
TELEMETRY_APP = "farm"
TELEMETRY_ASPECT = "telemetry_readings"
COMMAND_APP = "farm"
COMMAND_ASPECT = "gateway_commands"

RNS_ANNOUNCE_PREFIX = "agronomi-sensor"
HUB_ANNOUNCE_FILTER = "agronomi"
SENSOR_HUB = ""
