
# Hardware Peripherals Fleet Architecture
##AgroNomi Fleet — Reticulum-Enabled Sensor & Actuator Network

**Version:** 1.0  
**Date:** 2026-04-25  
**Status:** Architecture Design  

---

## 1. Executive Summary

This document defines the hardware peripherals layer architecture for integrating the **AgroNomi** sensor aggregation system with a **Reticulum mesh network** using a **two-phase gateway strategy**:

- **Phase 1 / Development** — **RAKwireless WisBlock RAK4631** (nRF52840 + SX1262) runs standalone as LoRa/Reticulum gateways with built-in BLE aggregation via Nordic UART Service (NUS). ESP32-C6 sensor/actuator nodes connect directly to the RAK4631 over BLE.
- **Phase 2+ / Production** — **Raspberry Pi Zero 2 W** units become permanent field gateways. The RAK4631 attaches to the Pi W via USB and acts purely as a LoRa modem (RNode). The Pi W runs the full Reticulum stack and a Python BLE→LoRa bridge, eliminating the need for custom RNode firmware forks on production gateways. ESP32-C6 nodes connect via BLE to the Pi W.

**ESP32-C6** microcontrollers serve as distributed sensor and actuator nodes in both phases, communicating over BLE and enabling a low-power, wide-area sensor network for agricultural monitoring and automation.

---

## 2. System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CENTRAL HUB (AgroNomi)                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐ │
│  │ Reticulum    │  │ Sensor       │  │ Alert        │  │ SQLite          │ │
│  │ Transport    │  │ Aggregator   │  │ Engine       │  │ Database        │ │
│  │ (Python/RNS) │  │ (Python)     │  │ (Python)     │  │ (farm_data.db)  │ │
│  └──────┬───────┘  └──────────────┘  └──────────────┘  └─────────────────┘ │
│         │                                                                   │
│         │ USB / BLE / Serial                                                  │
│         ▼                                                                   │
│  ┌──────────────┐                                                           │
│  │ RAK4631      │  ← Local Reticulum Interface (RNode)                      │
│  │ (EU868 LoRa) │                                                           │
│  └──────┬───────┘                                                           │
└─────────┼───────────────────────────────────────────────────────────────────┘
          │ LoRa RF (Reticulum Protocol)
          │ Several km range, mesh-capable
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         FIELD / GREENHOUSE DEPLOYMENT                       │
│                                                                             │
│  ╔═══════════════════════════════════════════════════════════════════════╗  │
│  ║  PHASE 1 / DEVELOPMENT — GW-RAK (RAK4631 Standalone Gateway)         ║  │
│  ╚═══════════════════════════════════════════════════════════════════════╝  │
│                                                                             │
│  ┌─────────────────────────────────────┐                                   │
│  │      RAK4631 EDGE GATEWAY            │  ← Field Reticulum Node           │
│  │  ┌─────────────┐  ┌───────────────┐  │  ┌──────────────┐                 │
│  │  │ nRF52840    │  │ SX1262 LoRa   │  │  │ BLE Central  │                 │
│  │  │ (Cortex-M4) │  │ (868MHz/22dBm)│  │  │ + NUS Server │                 │
│  │  └─────────────┘  └───────────────┘  │  └──────┬───────┘                 │
│  │         RNode Firmware CE v1.75      │         │                         │
│  └─────────────────────────────────────┘         │ BLE 5.0 (up to 4 dBm)    │
│                                                   │                         │
│         ┌─────────────────────────────────────────┼──────────────────┐      │
│         │                     │                   │                  │      │
│         ▼                     ▼                   ▼                  ▼      │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐  ┌──────────────┐│  │
│  │ ESP32-C6     │   │ ESP32-C6     │   │ ESP32-C6     │  │ ESP32-C6     ││  │
│  │ SOIL NODE    │   │ SOIL NODE    │   │ AIR NODE     │  │ PUMP/ACTUATOR││  │
│  │ (BLE Client) │   │ (BLE Client) │   │ (BLE Client) │  │ (BLE Client) ││  │
│  └──────┬───────┘   └──────┬───────┘   └──────┬───────┘  └──────┬───────┘│  │
│         │                   │                   │                  │      │  │
│  ┌──────▼───────┐   ┌──────▼───────┐   ┌──────▼───────┐  ┌──────▼───────┐│  │
│  │ Capacitive   │   │ Capacitive   │   │ SHT40/SHT30  │  │ Relay Module ││  │
│  │ Soil Moisture│   │ Soil Moisture│   │ Air Temp/Hum │  │ (Pumps)      ││  │
│  │ v1.2 (ADC)   │   │ v1.2 (ADC)   │   │ (I2C)        │  │ (GPIO)       ││  │
│  └──────────────┘   └──────────────┘   └──────────────┘  └──────────────┘│  │
│  ┌──────────────┐   ┌──────────────┐                      │  │ Servo/Stepper││  │
│  │ DS18B20      │   │ DS18B20      │                      │  │ (Greenhouse) ││  │
│  │ Soil Temp    │   │ Soil Temp    │                      │  │ (GPIO/PWM)   ││  │
│  │ (1-Wire)     │   │ (1-Wire)     │                      └──────────────┘│  │
│  └──────────────┘   └──────────────┘                                       │
│                                                                             │
│  Power: 18650 Li-ion / Solar Panel + TP4056 + MT3608 boost                 │
│                                                                             │
│  ╔═══════════════════════════════════════════════════════════════════════╗  │
│  ║  PHASE 2+ / PRODUCTION — GW-PIW (Pi Zero 2 W + RAK4631 Gateway)     ║  │
│  ╚═══════════════════════════════════════════════════════════════════════╝  │
│                                                                             │
│  ┌──────────────────────────────────────────────────┐                      │
│  │       RASPBERRY PI ZERO 2 W  — PRODUCTION GW      │  ← Field Reticulum │
│  │  ┌──────────────────────┐  ┌────────────────────┐ │     Node + BLE Hub │
│  │  │ RP3A0 SoC            │  │ Reticulum + Python │ │                    │
│  │  │ 4× Cortex-A53 @1GHz  │  │ BLE→LoRa Bridge    │ │                    │
│  │  │ 512MB RAM            │  │ (RNS Transport)    │ │                    │
│  │  │ RPi OS Lite          │  │ BLE 4.2 Central    │ │                    │
│  │  └──────────┬───────────┘  └─────────┬──────────┘ │                    │
│  │             │ USB OTG                │ BLE 4.2   │                     │
│  │  ┌──────────▼───────────┐            │            │                    │
│  │  │ RAK4631 (USB-attached)│           │            │                    │
│  │  │ SX1262 LoRa Modem    │            │            │                    │
│  │  │ (RNode mode, slave)  │            │            │                    │
│  │  │ 868MHz / 22dBm       │            │            │                    │
│  │  └──────────────────────┘            │            │                    │
│  └──────────────────────────────────────┼────────────┘                    │
│                                         │                                 │
│         ┌───────────────────────────────┼──────────────────┐              │
│         │                               │                  │              │
│         ▼                               ▼                  ▼              │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐  ┌──────────────┐│
│  │ ESP32-C6     │   │ ESP32-C6     │   │ ESP32-C6     │  │ ESP32-C6     ││
│  │ SOIL NODE    │   │ SOIL NODE    │   │ AIR NODE     │  │ PUMP/ACTUATOR││
│  │ (BLE Client) │   │ (BLE Client) │   │ (BLE Client) │  │ (BLE Client) ││
│  └──────┬───────┘   └──────┬───────┘   └──────┬───────┘  └──────┬───────┘│
│         │                   │                   │                  │      │
│  ┌──────▼───────┐   ┌──────▼───────┐   ┌──────▼───────┐  ┌──────▼───────┐│
│  │ Capacitive   │   │ Capacitive   │   │ SHT40/SHT30  │  │ Relay Module ││
│  │ Soil Moisture│   │ Soil Moisture│   │ Air Temp/Hum │  │ (Pumps)      ││
│  │ v1.2 (ADC)   │   │ v1.2 (ADC)   │   │ (I2C)        │  │ (GPIO)       ││
│  └──────────────┘   └──────────────┘   └──────────────┘  └──────────────┘│
│  ┌──────────────┐   ┌──────────────┐                      │  │ Servo/Stepper││
│  │ DS18B20      │   │ DS18B20      │                      │  │ (Greenhouse) ││
│  │ Soil Temp    │   │ Soil Temp    │                      │  │ (GPIO/PWM)   ││
│  │ (1-Wire)     │   │ (1-Wire)     │                      └──────────────┘│
│  └──────────────┘   └──────────────┘                                       │
│                                                                             │
│  Power: 5V micro USB / Solar + 5V regulator / 18650 + MT3608 boost         │
│  Cost: ~$15/unit | Production guaranteed until Jan 2030                    │
└─────────────────────────────────────────────────────────────────────────────┘
SN_VISION — Camera Vision Node (ESP32-CAM) is connected via wifi to local network same as AgroNomi pod and post greenhouse photos to central hub.
```

---

## 3. Network Stack Layers

| Layer | Component | Technology | Role |
|-------|-----------|------------|------|
| **L1 — Physical** | SX1262 LoRa | 868 MHz EU, SF7-SF12, BW125/250 | Long-range radio link (up to 10km LOS) |
| **L2 — Data Link** | RNode Firmware CE | KISS over Serial/BLE, CSMA/CA | Reticulum interface adapter |
| **L3 — Network** | Reticulum RNS | Elliptic-curve crypto, multi-hop transport | Encrypted mesh networking, addressing, routing |
| **L4 — Transport** | Reticulum Channel/Buffer | Reliable sequenced delivery | Data streaming, request/response |
| **L5 — Application** | Farm Telemetry Protocol | JSON/Binary over Reticulum | Sensor readings, actuator commands, config |
| **L6 — Aggregation** | sensor_aggregator.py | SQLite, threshold evaluation | Alert generation, Slovenian messaging |

---

## 4. Hardware Fleet Design

### 4.1 Device Taxonomy

| Device ID | Type | MCU | Radio | Role | Count |
|-----------|------|-----|-------|------|-------|
| **GW-RAK-01..N** | Edge Gateway (Phase 1 / Dev) | nRF52840 + SX1262 | BLE 5.0 + LoRa 868MHz | Development field Reticulum node + BLE aggregator (standalone) | 1 per field/greenhouse |
| **GW-PIW-01..N** | Edge Gateway (Production) | RP3A0 (Cortex-A53) + RAK4631 (SX1262 via USB) | BLE 4.2 + LoRa 868MHz | Production field Reticulum node + BLE aggregator (Pi W runs full stack) | 1 per field/greenhouse |
| **SN-SOIL-01..N** | Soil Sensor Node | ESP32-C6 | BLE 5.0 Client | Soil moisture + soil temperature | 1 per crop row/zone |
| **SN-AIR-01..N** | Air Sensor Node | ESP32-C6 | BLE 5.0 Client | Air temperature + humidity + optional CO2/light | 1 per greenhouse |
| **AN-PUMP-01..N** | Actuator Node | ESP32-C6 | BLE 5.0 Client | Pump relay control, flow monitoring | 1 per irrigation zone |
| **AN-GH-01..N** | Greenhouse Actuator | ESP32-C6 | BLE 5.0 Client | Ventilation servo, shade motor, fan | 1 per greenhouse |
| **HN-PI-01** | Central Hub | Raspberry Pi 4/5 | WiFi + BLE + USB | Reticulum transport node +AgroNomi | 1 per farm |

### 4.2 RAK4631 Edge Gateway (GW-RAK) / LoRa Modem for GW-PIW

**Hardware:**
- **Core:** RAKwireless WisBlock RAK4631 (nRF52840, 64MHz Cortex-M4, 1MB Flash, 256KB RAM)
- **LoRa:** Semtech SX1262 (EU868, TX power up to 22 dBm, PA_BOOST)
- **BLE:** Nordic nRF52840 BLE 5.0 (TX -20 to +4 dBm)
- **Baseboard:** RAK5005-O or RAK19007 (with solar/charging support)
- **Power:** 3.7V 18650 Li-ion + 6V/5W solar panel + TP4056 charger
- **Consumption:** Sleep 2 µA, LoRa TX 125mA @ 20dBm, BLE TX 9mA @ 4dBm

> **Production note:** In Phase 2+, the RAK4631 operates as a USB-attached LoRa modem for the Raspberry Pi Zero 2 W gateway (GW-PIW). It does **not** run BLE aggregation in production — the Pi W handles all BLE connectivity and runs the full Reticulum + BLE→LoRa bridge in Python. No custom RNode firmware fork is required for production gateways; the stock RNode firmware in modem mode is sufficient.

**Firmware — Phase 1 (Standalone GW-RAK):**
- **Primary:** RNode Firmware CE v1.75+ (liberatedsystems/RNode_Firmware_CE)
- **Configuration:** BOARD_RAK4631, MODEL_12 (EU868)
- **LoRa Parameters:** 868.0 MHz, BW 125 kHz, SF 11, CR 4/5 (conservative for range)
- **BLE:** Exposes Nordic UART Service (NUS) for ESP32-C6 client connections
- **Role:** Full standalone gateway — Reticulum transport, BLE aggregation, LoRa TX/RX

**Firmware — Production (USB LoRa Modem for GW-PIW):**
- **Primary:** RNode Firmware CE v1.75+ (stock, no custom fork needed)
- **Configuration:** BOARD_RAK4631, MODEL_12 (EU868), USB KISS interface enabled
- **LoRa Parameters:** Same as above (868.0 MHz, BW 125 kHz, SF 11, CR 4/5)
- **BLE:** Not used — RAK4631 acts purely as a LoRa modem controlled via USB KISS by the Pi W
- **Role:** Dumb LoRa modem — the Pi W owns all Reticulum transport, BLE, and bridge logic

**Reticulum Interface Config (Central Hub):**
```ini
[[RAK4631 Edge Gateway]]
type = RNodeInterface
port = ble://GW-RAK-01
frequency = 868000000
bandwidth = 125000
spreadingfactor = 11
codingrate = 5
txpower = 17
```

### 4.3 ESP32-C6 Soil Sensor Node (SN-SOIL)

**Hardware:**
- **Core:** ESP32-C6 (RISC-V, 160MHz, WiFi 6 + BLE 5.0, ultra-low power)
- **Soil Moisture:** Capacitive v1.2 sensor (resin-coated, corrosion-resistant)
  - Interface: Analog (ADC1, GPIO0-GPIO4)
  - Voltage divider + calibration required
  - Range: 0-100% volumetric water content (calibrated)
- **Soil Temperature:** DS18B20 Waterproof (stainless steel probe, 1m cable)
  - Interface: 1-Wire (GPIO5, 4.7kΩ pull-up to 3.3V)
  - Accuracy: ±0.5°C (-10°C to +85°C)
  - Resolution: 12-bit (0.0625°C)
- **Power:** 3.7V 18650 Li-ion or 2xAA + DC-DC boost
  - Deep sleep between readings: ~7µA
  - Active + BLE TX: ~120mA peak
  - Expected battery life: 3-6 months (1 reading/15 min)

**Firmware Role:**
- BLE GATT Client connecting to GW-RAK Nordic UART Service
- Sensor polling, ADC calibration, 1-Wire bus management
- MessagePack/JSON telemetry encoding
- Deep sleep scheduling with GPIO/RTC wake-up

### 4.4 ESP32-C6 Air Sensor Node (SN-AIR)

**Hardware:**
- **Core:** ESP32-C6
- **Air Temperature/Humidity:** Sensirion SHT40 or SHT30 (I2C, 0x44)
  - Accuracy: ±1.8% RH, ±0.2°C
  - Interface: I2C (SDA=GPIO6, SCL=GPIO7)
- **Optional CO2:** Sensirion SCD40 (I2C, 0x62) or MH-Z19B (UART)
- **Optional Light:** BH1750 (I2C, 0x23) or analog photoresistor
- **Enclosure:** IP65 weatherproof housing with ventilation shield
- **Power:** Solar + 18650 recommended (higher duty cycle than soil nodes)

### 4.5 ESP32-C6 Pump Actuator Node (AN-PUMP)

**Hardware:**
- **Core:** ESP32-C6
- **Relay Module:** 5V relay board (active LOW) or solid-state relay
  - Interface: GPIO8 (pump enable), GPIO9 (pump direction/valve)
  - Relay requires transistor driver + flyback diode
  - Opto-isolated relay board recommended for pump noise isolation
- **Flow Sensor:** YF-S201 Hall-effect water flow meter (optional)
  - Interface: GPIO10 (pulse counter)
- **Pressure Sensor:** 0-1.6MPa analog pressure transducer (optional)
  - Interface: ADC (GPIO3)
- **Power:** 12V/5A PSU or solar + battery; relay coil from separate 5V rail
- **Safety:** Watchdog timer, GPIO hold during sleep, manual override switch

### 4.6 ESP32-C6 Greenhouse Actuator Node (AN-GH)

**Hardware:**
- **Core:** ESP32-C6
- **Ventilation Servo:** MG996R or DS3218 (PWM, GPIO11)
  - 0-180° window opening control
  - Current sensing for stall detection (optional)
- **Shade Motor:** 12V/24V DC motor with H-bridge driver (DRV8871)
  - Interface: GPIO12 (PWM fwd), GPIO13 (PWM rev), GPIO14 (enable)
- **Circulation Fan:** 12V PWM fan or relay-controlled AC fan
  - Interface: GPIO15
- **End-stop switches:** Hall or mechanical (GPIO16-GPIO17, pull-ups)
- **Power:** 12V/10A central PSU with local 3.3V buck for ESP32

---

## 5. BLE Communication Architecture

### 5.1 BLE Topology

```
RAK4631 (Peripheral / GATT Server)                ESP32-C6 (Central / GATT Client)
┌─────────────────────────────┐                      ┌─────────────────────────────┐
│  Nordic UART Service (NUS)  │◄──── BLE Link ────►│  BLE Client + Application   │
│  UUID: 6E400001-B5A3...   │   (up to ~20m)      │                             │
│  ├── TX Char (Notify)       │◄──── Notifications ─│  Receives actuator commands   │
│  │   UUID: 6E400003...      │                     │                             │
│  └── RX Char (Write)        │───── Write Req ───►│  Sends sensor telemetry      │
│      UUID: 6E400002...      │                     │                             │
└─────────────────────────────┘                     └─────────────────────────────┘
```

- **RAK4631** runs as BLE Peripheral with NUS (standard in RNode Firmware CE for nRF52)
- **ESP32-C6** runs as BLE Central, scans for GW-RAK devices by name or MAC
- **Connection:** ESP32-C6 initiates, maintains reconnection logic with exponential backoff
- **Security:** BLE Just Works pairing (sufficient for farm environment), optional Passkey for sensitive zones

**Production Topology — Raspberry Pi Zero 2 W (GW-PIW) as BLE Central:**

```
Raspberry Pi Zero 2 W (BLE Central)                         ESP32-C6 (BLE Peripheral + Sensor)
┌──────────────────────────────────────┐                    ┌─────────────────────────────────┐
│  BlueZ BLE Stack (Linux Kernel)      │◄──── BLE ────────►│  NimBLE Peripheral + NUS GATT   │
│  bleak (Python BLE Client)           │   (up to ~20m)    │  Server                         │
│  ├── Scans for ESP32-C6 by name/MAC  │                    │  ├── TX Char (Notify) — telemetry│
│  ├── Connects to ALL nodes           │◄── Notifications ──│  └── RX Char (Write) — commands  │
│  └── Persistent connections          │──── Write Req ───►│                                  │
│  (no single-connection limit)        │                    └─────────────────────────────────┘
│                                       │
│  Python BLE→LoRa Bridge Service      │
│  (gw_piw_bridge.py, systemd)         │
│  ├── bleak NUS client per node       │
│  ├── FTP-BLE parser                  │
│  └── RNS packet construction         │
└──────────────────────────────────────┘
```

- **Pi Zero 2 W** runs as BLE Central using `bleak` Python library over BlueZ (Linux kernel BLE stack)
- **ESP32-C6** runs as BLE Peripheral exposing NUS GATT server (role reversal from Phase 1)
- **Connection:** Pi W maintains persistent connections to all ESP32-C6 nodes simultaneously — no time-division needed
- **Security:** BLE Just Works or Passkey pairing via BlueZ; Linux supports LE Secure Connections (AES-CCM)

### 5.2 Multi-Client Strategy

The RAK4631 nRF52840 can maintain **up to 1 active BLE central connection** reliably with Bluefruit. For multiple ESP32-C6 nodes per field:

**Option A — Time-Division Multiplexing (Recommended)**
- Each ESP32-C6 connects, sends burst telemetry, disconnects
- GW-RAK accepts connections round-robin
- ESP32-C6 nodes stagger their connection windows (e.g., 5-min cycle offset by node ID)

**Option B — Dedicated Gateway per Node Pair**
- Each greenhouse has 1 GW-RAK for up to 3-4 ESP32-C6 nodes (sequential access)
- More gateways = more LoRa airtime but simpler timing

**Recommended:** Option A with 15-minute reporting cycles, 30-second connection windows per node. A single GW-RAK can serve 8-12 ESP32-C6 nodes efficiently.

**Production Note (GW-PIW):** On the Raspberry Pi Zero 2 W, the Linux BlueZ BLE stack supports **multiple simultaneous BLE connections** (typically 4-8 active LE links depending on adapter). There is no single-connection limit like the nRF52840. This means **time-division multiplexing is not needed** — the Pi W holds persistent connections to all ESP32-C6 nodes in range concurrently, simplifying both the ESP32-C6 firmware (no connect/send/disconnect cycle) and the gateway bridge logic (no round-robin scheduler). For dense deployments (>8 nodes per gateway), deploy additional Pi W gateways.

### 5.3 BLE Protocol Over NUS

Instead of raw KISS (which is host-to-modem), we define a **Farm Telemetry Protocol (FTP-BLE)** layer:

```
+---------+---------+---------+----------------------------+
|  START  |  TYPE   | LENGTH  |          PAYLOAD           |
|  0xAA   | 1 byte  | 1 byte  |        (0-255 bytes)       |
+---------+---------+---------+----------------------------+

TYPE byte:
  0x01 = SENSOR_TELEMETRY (JSON)
  0x02 = ACTUATOR_STATUS (JSON)
  0x03 = ACTUATOR_COMMAND (JSON)
  0x04 = CONFIG_REQUEST
  0x05 = CONFIG_RESPONSE
  0x06 = HEARTBEAT / PING
  0xFF = KISS_FRAME (passthrough to LoRa — for advanced use)
```

**Example SENSOR_TELEMETRY payload:**
```json
{
  "dev_id": "SN-SOIL-03",
  "ts": 1714042800,
  "bat_v": 3.72,
  "readings": {
    "soil_moisture_pct": 42.5,
    "soil_temp_c": 18.3,
    "soil_ec_raw": 512
  }
}
```

---

## 6. Reticulum Network Integration

### 6.1 Reticulum Transport Layer

The central hub runs **Reticulum Reference Implementation (Python)** and connects to its local RAK4631 via USB or BLE as an RNodeInterface. Remote field GW-RAK units are Reticulum transport nodes that mesh with each other and the central hub.

**Reticulum node types in this network:**

| Node | Type | Role |
|------|------|------|
| Farm Hub | Transport Node | Runs full Reticulum stack, persists paths, bridges to Internet if needed |
| GW-RAK | Transport Node | Forwards LoRa packets, participates in path discovery, acts as BLE ingress |
| GW-RAK (distant) | Transport Node | Mesh repeater for range extension (can be solar-powered, no BLE clients) |
| **GW-PIW** | **Transport Node** | **Raspberry Pi Zero 2 W running full Reticulum Python stack; RAK4631 connected via USB as RNodeInterface. Full Linux transport node with persistent storage, path table, and announce caching. Bridges BLE telemetry from ESP32-C6 nodes into Reticulum mesh.** |

### 6.2 Addressing & Identity

Each GW-RAK and the Farm Hub hold a Reticulum Identity (256-bit Ed25519 keypair). Sensor data is sent as **plain announces** or **link-encrypted packets** to the Farm Hub's destination hash.

**Destinations:**
- `farm.hub.telemetry` — Sensor data inbound
- `farm.hub.commands` — Actuator commands outbound
- `farm.hub.config` — Node configuration and OTA updates

**Production Note (GW-PIW):** Pi Zero 2 W gateways run the full Reticulum Python stack with **persistent identity storage on the SD card** (`~/.reticulum/storage`). Unlike the nRF52840-based GW-RAK (which stores keys in flash), the Pi W benefits from a standard Linux filesystem — identity keys, path tables, and transport data survive reboots and firmware updates without special provisions. Reticulum's `RNodeInterface` connects to the USB-attached RAK4631 running stock RNode Firmware CE in slave mode.

### 6.3 LoRa Airtime Budget

With EU868 regulations (1% duty cycle or LBT), we must budget airtime carefully:

| SF | BW | Bitrate | Time/500B packet | Max packets/hour (1% DC) |
|----|----|---------|-----------------|--------------------------|
| 7 | 125k | 5.47 kbps | ~730ms | ~49 |
| 9 | 125k | 1.76 kbps | ~2.3s | ~15 |
| 11 | 125k | 0.54 kbps | ~7.4s | ~5 |
| 12 | 125k | 0.29 kbps | ~13.9s | ~3 |

**Recommendation:** Use **SF 11, BW 125kHz** for reliable 2-3km range in mixed terrain. With 8 nodes sending every 15 minutes = 32 packets/hour, well within 1% duty cycle budget for a single gateway. If scaling beyond 20 nodes, deploy additional GW-RAK units or use **SF 9** with shorter range but higher capacity.

---

## 7. Firmware Architecture

### 7.1 GW-RAK (RAK4631) — Firmware Stack (Phase 1 / Development Only)

> **Note:** This custom firmware approach is used for Phase 1 development and proof-of-concept. Production deployments use the GW-PIW gateway (see §7.1b) with stock RNode Firmware CE on the RAK4631, eliminating the need for custom firmware.

```
┌──────────────────────────────────────────┐
│  Application Layer (Custom Bridge)       │
│  - BLE NUS server                        │
│  - Farm Telemetry Protocol parser      │
│  - Reticulum packet assembler            │
│  - Command dispatcher (BLE → LoRa)      │
├──────────────────────────────────────────┤
│  RNode Firmware CE v1.75                 │
│  - KISS protocol handler                 │
│  - SX1262 radio driver                   │
│  - CSMA/CA MAC                           │
│  - BLEUart (Bluefruit)                   │
├──────────────────────────────────────────┤
│  nRF52840 HAL (Arduino/Adafruit)         │
│  - SPI (SX1262)                          │
│  - BLE stack (SoftDevice)                │
│  - GPIO / Timers / Sleep                 │
└──────────────────────────────────────────┘
```

**Important:** The stock RNode Firmware CE exposes KISS over BLE NUS for host connection. For our architecture, we need a **modified firmware** that:
1. Accepts FTP-BLE frames from ESP32-C6 clients over NUS
2. Translates them into Reticulum application packets (not raw KISS radio frames)
3. Sends outbound Reticulum packets destined for local BLE clients by writing to NUS TX

**Alternative (Simpler):** Keep stock RNode firmware on GW-RAK, but add a **companion ESP32-S3** or **Raspberry Pi Zero 2W** wired to the GW-RAK via USB. The companion runs the full Reticulum stack and acts as the BLE-to-LoRa bridge. However, this contradicts "ESP speaks to RAK via BLE."

**Recommended Path (Phase 1):** Fork RNode Firmware CE and add a `BLE_BRIDGE_MODE` compile flag that enables the custom telemetry forwarding logic. This is acceptable for development but introduces maintenance burden (tracking upstream RNode changes, testing custom builds).

> **Production Path:** See §7.1b — the GW-PIW approach eliminates the custom firmware entirely. The RAK4631 runs stock RNode Firmware CE in slave mode, and all bridge logic runs in Python on the Pi Zero 2 W.

### 7.1b GW-PIW (Raspberry Pi Zero 2 W) — Software Stack (Production)

> **This is the production gateway architecture.** The Pi Zero 2 W runs a full Linux stack with Python, replacing all custom RAK4631 firmware logic with standard software components.

```
┌──────────────────────────────────────────────────────────────┐
│  Application Layer (Python — gw_piw_bridge.py)              │
│  - BLE scanning + connection to ESP32-C6 nodes (bleak)      │
│  - NUS GATT client — subscribe to telemetry notifications   │
│  - FTP-BLE frame parser (JSON telemetry extraction)         │
│  - Reticulum packet construction + forwarding (RNS)         │
│  - Inbound command routing (RNS → BLE write to ESP32-C6)    │
│  - Local SQLite cache for offline buffering                 │
│  - Health monitoring + watchdog                              │
├──────────────────────────────────────────────────────────────┤
│  Reticulum Network Stack (RNS Python)                       │
│  - Full transport node with path discovery                   │
│  - RNodeInterface (USB serial to RAK4631)                    │
│  - Identity management (Ed25519, stored on SD card)          │
│  - Link encryption, announces, packet assembly               │
├──────────────────────────────────────────────────────────────┤
│  Raspberry Pi OS Lite (Bookworm, 32-bit ARM)                │
│  - Linux kernel with BlueZ BLE stack                        │
│  - USB serial driver (cdc_acm / USB-OTG to RAK4631)         │
│  - systemd service management (gw_piw_bridge.service)       │
│  - NetworkManager (WiFi for hub uplink, optional)           │
│  - cron / logrotate / unattended-upgrades                   │
├──────────────────────────────────────────────────────────────┤
│  Hardware: Raspberry Pi Zero 2 W                            │
│  - Quad-core ARM Cortex-A53 @ 1GHz, 512MB RAM              │
│  - Built-in WiFi 2.4GHz 802.11 b/g/n                       │
│  - Bluetooth 4.2 + BLE ( Broadcom BCM43430 )                │
│  - micro USB OTG → RAK4631 (USB serial)                     │
│  - micro USB power (5V/2.5A recommended)                    │
│  - RAK4631 running stock RNode Firmware CE (slave mode)     │
│  - 65×30mm form factor, ~$15 unit cost                      │
└──────────────────────────────────────────────────────────────┘
```

**Key Design Decisions:**
1. **No custom firmware on RAK4631** — runs stock RNode Firmware CE in slave mode. The Pi W controls the radio entirely via KISS over USB serial using RNS's `RNodeInterface`.
2. **BLE role reversal** — the Pi W acts as BLE Central (using `bleak` + BlueZ), while ESP32-C6 nodes run as BLE Peripherals exposing NUS GATT servers. This is more natural for a Linux gateway.
3. **Python bridge script** (`gw_piw_bridge.py`) handles all bridge logic as a single systemd service. It:
   - Scans for and connects to ESP32-C6 nodes by name prefix (e.g., `SN-SOIL-*`)
   - Maintains persistent BLE connections (no time-division multiplexing needed)
   - Subscribes to NUS TX notifications for incoming telemetry
   - Parses FTP-BLE frames, constructs Reticulum packets addressed to `farm.hub.telemetry`
   - Routes inbound commands from `farm.hub.commands` to the target ESP32-C6 via BLE NUS write
   - Caches unsent data in local SQLite if LoRa link is down
4. **Runs as systemd service** — auto-starts on boot, restarts on failure, logs to journald.

**Key Python Packages:**
| Package | Version | Purpose |
|---------|---------|----------|
| `RNS` | ≥0.8.x | Reticulum network stack, RNodeInterface, transport node |
| `bleak` | ≥0.22.x | Cross-platform BLE client (BlueZ backend on Linux) |
| `sqlite3` | (stdlib) | Local offline cache for telemetry buffering |
| `pyserial` | ≥3.5 | USB serial interface to RAK4631 (used by RNS internally) |

**Production Availability:** Raspberry Pi Zero 2 W is committed to production until at least **January 2030**, ensuring long-term availability for fleet deployment.

### 7.2 ESP32-C6 — Firmware Stack

```
┌──────────────────────────────────────────┐
│  Application (Farm Node)                 │
│  - Sensor polling scheduler              │
│  - ADC/DallasTemperature/SHT drivers     │
│  - Telemetry JSON builder                │
│  - Actuator state machine                │
│  - OTA update handler                    │
├──────────────────────────────────────────┤
│  BLE Client (NUS)                        │
│  - GATT service discovery                │
│  - Connection management                 │
│  - Notify subscription                   │
│  - Write fragmentation (if needed)       │
├──────────────────────────────────────────┤
│  ESP-IDF / Arduino Core                  │
│  - FreeRTOS tasks                        │
│  - Deep sleep / ULP                      │
│  - GPIO / ADC / I2C / 1-Wire / PWM       │
│  - BLE 5.0 stack (Bluedroid/NimBLE)      │
└──────────────────────────────────────────┘
```

**Framework:** ESP-IDF with C++ or Arduino-ESP32 (ESP32-C6 variant supported in v3.x+)
**Key Libraries:**
- `DallasTemperature` + `OneWire` (DS18B20)
- `SensirionI2CSht4x` or `Adafruit_SHT31` (air sensors)
- NimBLE-Arduino (lightweight BLE stack, preferred over Bluedroid for RAM efficiency)
- ArduinoJson (telemetry serialization)

---

## 8. Data Flow: Sensor to Database

```
1. SENSOR ACQUISITION (ESP32-C6)
   └─> Read ADC (soil moisture), 1-Wire (soil temp), I2C (air temp/hum)
   └─> Apply calibration curves, median filters
   └─> Package into JSON + metadata (battery, timestamp, device ID)

2. BLE TRANSMISSION (ESP32-C6 → GW-RAK)
   └─> Connect to GW-RAK Nordic UART Service
   └─> Send FTP-BLE SENSOR_TELEMETRY frame
   └─> Wait for ACK or timeout, disconnect

   **Production (ESP32-C6 → GW-PIW):**
   └─> ESP32-C6 exposes NUS GATT server (BLE Peripheral role)
   └─> Pi Zero 2 W maintains persistent BLE connection to each node
   └─> ESP32-C6 sends telemetry via NUS notification (no connect/disconnect cycle)
   └─> Pi W `bleak` client receives notification, parses FTP-BLE frame

3. LoRa TRANSMISSION (GW-RAK → Farm Hub)
   └─> GW-RAK parses JSON, constructs Reticulum packet
   └─> Addresses to farm.hub.telemetry destination hash
   └─> Adds origin tag (node_id, field_id)
   └─> Transmits over SX1262 LoRa

   **Production (GW-PIW → Farm Hub):**
   └─> Pi W Python bridge (`gw_piw_bridge.py`) constructs Reticulum packet
   └─> RNS library addresses to `farm.hub.telemetry` destination hash
   └─> Sends via `RNodeInterface` (KISS over USB serial to RAK4631)
   └─> RAK4631 transmits over SX1262 LoRa — no custom firmware, stock RNode CE slave mode

4. MESH ROUTING (Intermediate GW-RAK repeaters, if any)
   └─> Reticulum transport layer handles hop-by-hop forwarding
   └─> Packet-level encryption, path discovery, acknowledgements

5. RECEPTION (Farm Hub)
   └─> Farm Hub's RAK4631 (local) or WiFi/UDP interface receives packet
   └─> Reticulum Python stack decrypts, validates, delivers to application

6. INGESTION (Python Service)
   └─> Custom Python receiver script parses telemetry JSON
   └─> INSERT INTO sensor_readings (node_id, reading_type, value, unit, recorded_at)
   └─> Updates sensor_nodes.last_seen, battery_level

7. AGGREGATION (sensor_aggregator.py)
   └─> Triggered every 5 minutes by cron or systemd timer
   └─> Reads all current sensor_readings
   └─> Evaluates thresholds (crop-specific, greenhouse-specific, field-specific)
   └─> Generates alerts in Slovenian
   └─> Writes to sensor_alerts table
```

---

## 9. Database Schema Extensions

The existing `sensor_aggregator.py` expects these tables. We recommend these additions:

```sql
-- Existing tables assumed: sensor_nodes, sensor_readings, sensor_alerts, fields, field_crops, etc.

-- New: Track hardware device fleet
CREATE TABLE hardware_devices (
    device_id TEXT PRIMARY KEY,          -- e.g., "SN-SOIL-03"
    device_type TEXT CHECK(device_type IN ('gateway','soil_node','air_node','pump_node','gh_actuator')),
    node_id TEXT REFERENCES sensor_nodes(node_id),
    ble_mac TEXT,                        -- BLE address for pairing
    ble_target_gateway TEXT,             -- e.g., "GW-RAK-01"
    firmware_version TEXT,
    hardware_revision TEXT,
    battery_type TEXT,
    install_date TEXT,
    status TEXT DEFAULT 'active' CHECK(status IN ('active','offline','maintenance','decommissioned'))
);

-- New: LoRa/Reticulum gateway registry
CREATE TABLE reticulum_gateways (
    gateway_id TEXT PRIMARY KEY,
    device_id TEXT REFERENCES hardware_devices(device_id),
    rns_destination_hash TEXT UNIQUE,
    lora_frequency INTEGER,
    lora_spreading_factor INTEGER,
    last_heartbeat TEXT,
    peers_count INTEGER DEFAULT 0
);

-- New: Command queue for actuators (outbound)
CREATE TABLE actuator_commands (
    cmd_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT REFERENCES hardware_devices(device_id),
    cmd_type TEXT CHECK(cmd_type IN ('pump_on','pump_off','vent_open','vent_close','shade_pct','fan_on','fan_off')),
    cmd_value REAL,
    requested_at TEXT,
    executed_at TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending','sent','acknowledged','failed','expired'))
);

-- New: BLE connectivity log for diagnostics
CREATE TABLE ble_link_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT,
    gateway_id TEXT,
    event TEXT CHECK(event IN ('connected','disconnected','timeout','rx_packet','tx_packet')),
    rssi INTEGER,
    recorded_at TEXT
);
```

---

## 10. Power & Deployment Budget

### 10.1 Power Budget per Node

| Device | Sleep | Active | TX | Duty Cycle | Daily Average | Battery Life (18650/2600mAh) |
|--------|-------|--------|----|-----------|---------------|------------------------------|
| SN-SOIL | 7 µA | 45mA | 120mA@4dBm | 30s every 15min | ~4.5mAh/day | ~150-180 days |
| SN-AIR | 7 µA | 55mA | 120mA@4dBm | 30s every 5min | ~13mAh/day | ~50-60 days |
| AN-PUMP | 7 µA | 65mA | 120mA@4dBm | On-demand | ~2mAh/day + pump | Solar required |
| AN-GH | 7 µA | 80mA | 120mA@4dBm | On-demand | ~5mAh/day + motor | Solar required |
| GW-RAK | 2 µA | 17mA RX | 125mA@20dBm | Always-on mesh | ~500mAh/day | Solar + 18650 required |
| **GW-PIW** | **N/A (always-on)** | **~400mA (idle Linux + BLE)** | **+125mA via RAK LoRa TX** | **Always-on mesh** | **~12000mAh/day** | **5V USB power or 12V solar system required** |

### 10.2 Solar Sizing

| Device | Panel | Battery | Charge Controller | Notes |
|--------|-------|---------|-------------------|-------|
| GW-RAK | 5W/6V | 2x 18650 (5200mAh) | TP4056 + MT3608 | Gateway must never die |
| **GW-PIW** | **10W/5V USB solar or 20W/12V w/ adapter** | **12V/7Ah SLA or 3×18650 USB power bank** | **USB solar charge controller or power bank w/ pass-through** | **Pi W must never die; needs stable 5V/2.5A supply; oversize battery for 3+ cloudy days** |
| SN-SOIL | 2W/5V | 1x 18650 (2600mAh) | TP4056 | Optional; battery-only viable |
| AN-PUMP | 10W/12V | 12V/7Ah SLA | MPPT or PWM | Powers pump + ESP32 |
| AN-GH | 20W/12V | 12V/12Ah SLA | MPPT | Powers motors + ESP32 |

---

## 11. Implementation Roadmap

### Phase 1 — Proof of Concept (Weeks 1-3)
**Goal:** Validate BLE + LoRa chain end-to-end with 1 of each device, using **RAK4631 standalone gateways for development**.

| Task | Owner | Deliverable |
|------|-------|-------------|
| Flash 2x RAK4631 with RNode CE v1.75 | Hardware | 2x LoRa nodes, one as AP/bridge test |
| Build 1x ESP32-C6 + capacitive soil sensor + DS18B20 | Hardware | Soil sensor node on breadboard |
| Write ESP32-C6 NimBLE client connecting to RAK4631 NUS | Firmware | BLE connection + data tx working |
| Write minimal Python Reticulum receiver + SQLite ingest | Backend | `receiver.py` inserts to sensor_readings |
| Test LoRa range between two RAK4631 units | QA | Range measurement report |
| Procure 2x Raspberry Pi Zero 2 W + USB OTG cables | Hardware | Pi W units ready for Phase 2 integration |

### Phase 2 — Gateway Firmware & Pi W Integration (Weeks 4-7)
**Goal:** Enable GW-RAK to bridge BLE telemetry into Reticulum packets (Phase 1 path). **Transition to Pi Zero 2 W production gateway.**

| Task | Owner | Deliverable |
|------|-------|-------------|
| Fork RNode Firmware CE, add `BLE_BRIDGE_MODE` | Firmware | Custom build for RAK4631 (Phase 1 dev) |
| Implement FTP-BLE parser in GW-RAK firmware | Firmware | JSON → Reticulum packet assembly (Phase 1) |
| Implement outbound command path (LoRa → BLE NUS write) | Firmware | Actuator commands reach ESP32-C6 |
| Add OTA update capability for ESP32-C6 over BLE bridge | Firmware | Remote firmware update mechanism |
| **Flash Raspberry Pi OS Lite on Pi Zero 2 W** | **Hardware** | **Pi W bootable with SSH access** |
| **Install RNS + bleak on Pi W** | **Backend** | **`pip install RNS bleak pyserial` validated** |
| **Connect RAK4631 via USB OTG, verify RNodeInterface** | **Hardware** | **`rnodeconf` detects RAK, KISS working** |
| **Deploy `gw_piw_bridge.py` as systemd service** | **Backend** | **BLE→LoRa bridge service auto-starts on boot** |
| **Validate Pi W BLE scanning for ESP32-C6 nodes** | **QA** | **Pi W connects to ESP32-C6, receives telemetry** |
| **End-to-end test: ESP32-C6 → BLE → Pi W → LoRa → Hub** | **QA** | **Full data path validated on production hardware** |

### Phase 3 — Node Firmware Suite (Weeks 8-11)
**Goal:** Production-ready firmware for all 4 ESP32-C6 node types. Production uses **Pi W gateways**.

| Task | Owner | Deliverable |
|------|-------|-------------|
| `sn_soil` firmware — deep sleep, ADC calibration, 1-Wire | Firmware | v1.0 release |
| `sn_air` firmware — I2C sensor fusion, dew point calc | Firmware | v1.0 release |
| `an_pump` firmware — relay safety interlocks, flow count | Firmware | v1.0 release |
| `an_gh` firmware — servo PID, end-stop homing, shade % | Firmware | v1.0 release |
| Unified config system (BLE provisioning + EEPROM) | Firmware | JSON config schema |
| Power profiling and optimization | Hardware | Power budget validation |
| **ESP32-C6 BLE role: update to Peripheral/NUS server for Pi W** | **Firmware** | **GATT server mode for production gateways** |

### Phase 4 — Fleet Management & Backend (Weeks 12-15)
**Goal:** Scale to 20+ nodes with management tools. Production deployment uses **Pi W gateways**.

| Task | Owner | Deliverable |
|------|-------|-------------|
| `reticulum_ingest.py` service — persistent receiver daemon | Backend | systemd service, auto-reconnect |
| Fleet manager web UI (or CLI) — register nodes, view status | Backend | Device registry + status dashboard |
| OTA orchestration server — schedule firmware updates | Backend | Batch OTA to node classes |
| `actuator_controller.py` — queue commands, confirm exec | Backend | Command queue + confirmation |
| Integration with existing `sensor_aggregator.py` thresholds | Backend | No regression in alert logic |
| Field deployment enclosures, cable harnesses, solar mounts | Hardware | 5x production node kits |
| **`gw_piw_bridge.py` — Python BLE→LoRa bridge service for Pi W gateway** | **Backend** | **Production bridge with multi-node BLE, SQLite cache, systemd unit** |
| **Pi W fleet provisioning script** | **Backend** | **Automated Pi OS flash + RNS + bleak install + config** |
| **Pi W OTA update mechanism for `gw_piw_bridge.py`** | **Backend** | **Git-based or rsync deployment pipeline** |

### Phase 5 — Production Deployment (Weeks 16-19)
**Goal:** 20-30 nodes operational, mesh topology validated. **Production uses Pi W gateways with RAK4631 as USB LoRa modem.**

| Task | Owner | Deliverable |
|------|-------|-------------|
| Deploy 3x **GW-PIW** (Pi Zero 2 W + RAK4631) gateways covering all fields | Field | Gateway placement + range tests |
| Deploy 8x SN-SOIL, 3x SN-AIR, 4x AN-PUMP, 3x AN-GH | Field | All nodes reporting via Pi W gateways |
| Mesh path redundancy testing — disable one GW-PIW, verify reroute | QA | Path failover validated |
| Penetration test — BLE pairing, LoRa replay, Reticulum crypto | Security | Security audit report |
| Documentation — wiring diagrams, troubleshooting runbooks | Docs | `hardware_fleet_runbook.md` |
| Pi W solar power systems — install + validate 3+ day autonomy | Field | Stable off-grid power confirmed |

---

## 12. Risk Analysis & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| BLE range < 10m through greenhouse walls | Medium | Position GW-RAK centrally; use external antenna; add repeaters |
| RAK4631 single BLE connection limit | Medium | Time-division multiplexing; 1 gateway per 8-12 nodes |
| LoRa 1% duty cycle exceeded at scale | High | Add more GW-RAK gateways; use SF9 instead of SF11; compress payloads |
| ESP32-C6 deep sleep BLE reconnection failures | Medium | Implement NimBLE quick reconnect; cache bonding keys; use directed advertising |
| RNode Firmware CE fork maintenance burden | Low | Production Pi W gateways use stock RNode CE slave mode — no custom firmware needed; fork only used in Phase 1 dev on RAK4631 standalone |
| Actuator relay failure (pump stuck ON) | Critical | Hardware watchdog; independent timer relay; flow sensor sanity check |
| Solar panel theft/vandalism | Medium | Enclosures with security screws; discrete placement; tamper detection |
| Raspberry Pi Zero 2 W SD card corruption (power loss, wear) | Medium | Use high-quality A2 SD cards; enable overlayfs; consider read-only root filesystem; keep spare cards with pre-flashed images |
| Pi W thermal throttling in direct sunlight enclosures | Low | Enclosures with ventilation; heat sinks; position in shade; Pi W has no thermal throttling below 80°C |

---

## 13. Recommended Configurations

### 13.1 Reticulum Config (`~/.reticulum/config` on Farm Hub)

```ini
[reticulum]
enable_transport = True
share_instance = Yes

[RAK4631 Local Gateway]
type = RNodeInterface
port = /dev/ttyACM0        # or ble://GW-RAK-LOCAL
frequency = 868000000
bandwidth = 125000
spreadingfactor = 11
codingrate = 5
txpower = 17

[debug]
log_level = 4
```

### 13.2 Pi W Field Gateway Config (`~/.reticulum/config` on Pi Zero 2 W)

The Pi W runs as a **full Reticulum transport node** (`enable_transport = True`), providing both LoRa mesh participation via the RAK4631 USB modem and a TCP server interface for local mesh connectivity.

```ini
[reticulum]
enable_transport = True
share_instance = Yes

[Pi W RNodeInterface]
type = RNodeInterface
port = /dev/ttyACM0        # RAK4631 via USB OTG
frequency = 868000000
bandwidth = 125000
spreadingfactor = 11
codingrate = 5
txpower = 17

[Pi W TCP Server]
type = TCPServerInterface
listen_ip = 0.0.0.0
listen_port = 4242

[debug]
log_level = 4
```

### 13.3 GW-RAK LoRa Parameters

```cpp
// RNode Firmware CE compile-time or EEPROM config
#define CFG_FREQUENCY   868000000
#define CFG_BANDWIDTH   125000
#define CFG_SF          11
#define CFG_CR          5
#define CFG_TXPOWER     17        // dBm (mid-power for heat/efficiency balance)
#define CFG_BLE_BRIDGE  true      // Custom flag for bridge mode
#define CFG_ST_ALOCK    10.0      // Short-term airtime limit %
#define CFG_LT_ALOCK    3.0       // Long-term airtime limit %
```

### 13.4 ESP32-C6 `sdkconfig` / Build Flags

```
CONFIG_BT_ENABLED=y
CONFIG_BT_NIMBLE_ENABLED=y           # Use NimBLE (lighter than Bluedroid)
CONFIG_BT_NIMBLE_MAX_CONNECTIONS=1
CONFIG_BT_NIMBLE_ROLE_CENTRAL=y
CONFIG_BT_NIMBLE_ROLE_OBSERVER=y
CONFIG_ESP32C6_DEFAULT_CPU_FREQ_160=y
CONFIG_FREERTOS_HZ=1000
CONFIG_PM_ENABLE=y
CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ=160
```

---

## 14. Parts List (Bill of Materials)

### Production Gateways — Pi Zero 2 W (per unit)
| Qty | Part | Est. Price |
|-----|------|------------|
| 1 | Raspberry Pi Zero 2 W | €15 |
| 1 | RAK4631 WisBlock Core | €25 |
| 1 | RAK5005-O Base Board | €12 |
| 1 | microSD card 16GB A2 | €5 |
| 1 | micro USB OTG adapter | €2 |
| 1 | USB A to micro USB cable | €3 |
| 1 | 868MHz LoRa Antenna (IP67) | €8 |
| 1 | 10W/5V USB solar panel or 20W/12V with USB adapter | €20 |
| 1 | 12V/7Ah SLA or USB power bank 10000mAh | €15 |
| 1 | IP65 enclosure 150x100x70mm | €8 |
| | **Production Gateway Subtotal** | **~€113** |

### Development Gateways — RAK4631 Standalone (per unit)
| Qty | Part | Est. Price |
|-----|------|------------|
| 1 | RAK4631 WisBlock Core | €25 |
| 1 | RAK5005-O Base Board | €12 |
| 1 | 868MHz LoRa Antenna (IP67) | €8 |
| 1 | 2.4GHz BLE Antenna | €3 |
| 1 | 18650 battery holder + 2 cells | €12 |
| 1 | 5W/6V Solar panel + bracket | €15 |
| 1 | TP4056 + MT3608 + protection | €4 |
| 1 | IP65 enclosure 150x100x70mm | €8 |
| | **Gateway Subtotal** | **~€87** |

### Soil Nodes (per unit)
| Qty | Part | Est. Price |
|-----|------|------------|
| 1 | ESP32-C6 DevKit or XIAO C6 | €6 |
| 1 | Capacitive Soil Moisture v1.2 | €4 |
| 1 | DS18B20 Waterproof (1m) | €3 |
| 1 | 4.7kΩ resistor + header pins | €0.5 |
| 1 | 18650 holder + cell | €6 |
| 1 | IP65 enclosure 80x60x40mm | €4 |
| | **Soil Node Subtotal** | **~€24** |

### Air Nodes (per unit)
| Qty | Part | Est. Price |
|-----|------|------------|
| 1 | ESP32-C6 DevKit | €6 |
| 1 | SHT40 (STEMMA QT/I2C) | €5 |
| 1 | BH1750 (I2C) | €2 |
| 1 | Solar radiation shield (DIY) | €8 |
| 1 | 18650 + holder + small solar | €12 |
| 1 | IP65 enclosure with vents | €6 |
| | **Air Node Subtotal** | **~€39** |

### Pump Actuators (per unit)
| Qty | Part | Est. Price |
|-----|------|------------|
| 1 | ESP32-C6 DevKit | €6 |
| 1 | 5V Relay module (opto-isolated, 2-ch) | €4 |
| 1 | YF-S201 Flow sensor | €6 |
| 1 | Pressure sensor 0-1.6MPa | €12 |
| 1 | 12V/5A PSU or solar kit | €25 |
| 1 | Waterproof enclosure 120x80x50mm | €6 |
| | **Pump Node Subtotal** | **~€59** |

### Greenhouse Actuators (per unit)
| Qty | Part | Est. Price |
|-----|------|------------|
| 1 | ESP32-C6 DevKit | €6 |
| 1 | DRV8871 H-bridge driver | €4 |
| 1 | MG996R servo or 12V linear actuator | €15 |
| 1 | End-stop switches (2x) | €2 |
| 1 | 12V/10A PSU or 20W solar kit | €35 |
| 1 | Large IP65 enclosure 200x150x80mm | €12 |
| | **GH Actuator Subtotal** | **~€74** |

---

## 15. Success Criteria

| Metric | Target | Measurement |
|--------|--------|-------------|
| Telemetry success rate | > 95% of expected readings received | Database count vs. scheduled |
| BLE connection time | < 5 seconds from wake to NUS ready | Logic analyzer / serial log |
| LoRa end-to-end latency | < 30 seconds (single hop) | Packet timestamp delta |
| Mesh path redundancy | < 3% packet loss after single gateway failure | 24-hour soak test |
| Battery life (soil nodes) | > 120 days without solar | Voltage telemetry trend |
| Actuator command latency | < 60 seconds from UI to physical action | End-to-end test |
| Sensor accuracy (soil moisture) | ±5% after calibration | Gravimetric reference samples |
| Security | No plaintext credentials; BLE bonded; LoRa encrypted | Penetration test |
| Gateway transition | Pi W gateways operational within 30 minutes of physical installation | Pre-flashed SD cards + automated provisioning script |

---

*This architecture document is a living specification. Update firmware versions, LoRa parameters, and node counts as the fleet scales.*
