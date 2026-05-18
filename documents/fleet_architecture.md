# AgroNomi Fleet Architecture

## System Overview

AgroNomi is a LoRa-based agricultural sensor/actuator network. ESP32 field nodes collect sensor data and send it over BLE to a Pico 2W, which forwards it via USB serial to an HP field gateway running Ubuntu. The HP gateway and a Mac mini hub each have their own RNode LoRa USB radio and communicate over Reticulum mesh networking. The hub ingests telemetry into SQLite and dispatches commands and OTA firmware updates back through the same chain.

```
┌────────────────────┐  RNode   RNode  ┌──────────────────┐   USB Serial   ┌──────────────┐   BLE NUS   ┌──────────────┐
│   Mac mini (Hub)   │  ◄──LoRa────►   │   mimi (HP/Ubuntu)│◄───────────────►│   Pico 2W    │◄───────────►│  ESP32-C6     │
│                    │  RNS Packet +   │                  │                 │  BLE Radio   │              │  Sensor Node  │
│ reticulum_ingest   │  RNS Link/      │  ble_forwarder   │                 │  (MicroPython)│              │  (Arduino)    │
│ ota_scheduler      │  Resource       │                  │                 │              │              │              │
│ farm_data.db       │                 │  ble_ota         │                 │  Advertises   │              │  BLE Client   │
│ /var/agronomi/fw/  │                 │  fw_cache        │                 │  GW-MIMI-01   │              │  Deep Sleep   │
└────────────────────┘                 └──────────────────┘                 └──────────────┘              └──────────────┘
       RNode USB                        RNode USB + Pico 2W
```

## Devices

| Device | Role | Hardware | RNS Aspect | BLE |
|--------|------|----------|-----------|-----|
| Mac mini | Hub — ingest, command dispatch, OTA scheduling | macOS, RNode LoRa USB (1 of 2) | `farm.telemetry_readings` (IN), `farm.commands_control` (IN) | — |
| mimi | Field Gateway — RNS ↔ serial bridge | HP desktop (Ubuntu), RNode LoRa USB + Pico 2W via USB CDC | `farm.gateway_commands` (IN), `farm.telemetry_readings` (OUT) | — |
| Pico 2W | BLE radio — connects ESP32 nodes to mimi | RP2040 + CYW43439, USB CDC to HP | — | NUS server `GW-MIMI-01` |
| SN-AIR-01 | Air sensor node | ESP32-C6 Super Mini, DHT22 | — | NUS client, connects to `GW-MIMI-01` |
| SN-SOIL-01 | Soil moisture node | ESP32-C6 Super Mini, DS18B20 + capacitive | — | NUS client |
| AN-PUMP-01 | Pump actuator node | ESP32-C6 Super Mini | — | NUS client |
| AN-GREENHOUSE-01 | Greenhouse actuator | ESP32-C6 Super Mini | — | NUS client |
| SN-VIS-GH-01 | Vision node | ESP32-CAM | — | WiFi POST (separate path) |

## Communication Layers

### Layer 1: ESP32 ↔ Pico (BLE NUS)

- **Service**: Nordic UART Service (UUID `6E400001-...`)
- **TX characteristic** (`6E400003`): Pico → ESP32 (commands, OTA frames)
- **RX characteristic** (`6E400002`): ESP32 → Pico (telemetry, ACKs)
- **Pico** runs MicroPython BLE GATT server advertising as `GW-MIMI-01`
- **ESP32** scans for that name, connects as BLE client
- **OTA binary protocol** on the same NUS channel:
  - `[0xA0][size LE32][fw_version]` — BEGIN
  - `[0xA1][seq LE32][payload…241B]` — DATA chunks
  - `[0xA2][fw_version]` — END
  - `[0xA3]` — ABORT
- **Deep sleep**: ESP32 wakes every 300s, connects, sends telemetry, receives commands, disconnects

### Layer 2: Pico ↔ mimi (USB CDC Serial)

- Pico exposes USB CDC serial at `/dev/pico` (115200 baud)
- **Line protocols** (newline-delimited):
  - `[JSON] {...}` — telemetry from ESP32
  - `[CMD] {...}` — command to ESP32
  - `[ACK] {...}` — command/OTA acknowledgement from ESP32
  - `[HB]` — heartbeat
  - `[C]` / `[D]` — connect/disconnect events

### Layer 3: mimi ↔ Hub (RNS over LoRa — two RNode radios)

Each device has its own RNode LoRa USB radio. They communicate as peers over the Reticulum mesh — there is no shared radio or relay. All RNS communication uses **SINGLE destinations** with announce-based discovery. No manual destination hashes required — gateways discover the hub via RNS announces and vice versa.

| Direction | RNS Destination | Purpose |
|-----------|----------------|---------|
| Gateway → Hub | `farm.telemetry_readings` | Sensor telemetry JSON |
| Gateway → Hub | `farm.commands_control` | Command ACKs + OTA result ACKs |
| Hub → Gateway | `farm.gateway_commands` | Actuator commands + `ota_request` commands |
| Hub → Gateway | RNS Link → Resource | OTA firmware binary transfer (~1.4MB, ~50–90 min over LoRa) |

Proof strategy is **PROVE_ALL** on all destinations — senders get delivery confirmation and path entries stay alive.

## Data Flows

### Telemetry (ESP32 → Hub)

```
ESP32 wakes → read sensors → build JSON → BLE NUS → Pico [JSON] → serial → mimi RNS → Hub DB
```

Payload (v1.4.0+):
```json
{
  "dev_id": "SN-AIR-01", "ts": 12345, "fw_ver": "1.4.0",
  "device_type": "air_node", "ble_mac": "8C:FD:49:19:7B:BE",
  "seq": 5, "bat_v": "3.12", "gateway_id": "GW-MIMI-01",
  "readings": {"air_temperature_c": 20.6, "air_humidity_pct": 51.0}
}
```

**Auto-provisioning**: First telemetry from a new device creates `hardware_devices` + `sensor_nodes` rows from `device_type`, `fw_ver`, `ble_mac`, and `gateway_id` fields — no manual registration needed.

### Commands (Hub → ESP32)

```
Hub DB (actuator_commands) → CommandDispatcher → RNS Packet → mimi → Pico serial [CMD] → ESP32
```

Command JSON: `{"cmd_id":1, "device_id":"SN-AIR-01", "cmd_type":"fan_on", "cmd_value":1.0, "ble_mac":"8C:FD:49:19:7B:BE", "ts":12345}`

ACK returns: ESP32 → Pico `[ACK]` → mimi RNS → Hub `CommandAckDestination` → DB status update.

### OTA Firmware (Hub → ESP32)

1. **Hub scheduler** (`ota_scheduler.py`) queues `ota_request` commands during maintenance window (21:00–24:00)
2. **CommandDispatcher** marks command `transferring`, establishes RNS Link to gateway
3. **RNS Resource** transfers firmware binary (~1.4MB, ~50–90 min over LoRa, 2h timeout)
4. **RNS Packet** sends `ota_request` command with metadata (`fw_version`, `device_type`, `sha256`, `ble_mac`)
5. **Gateway** (`_on_link_established` → `_on_resource`) receives binary, saves to `fw_cache`
6. **Gateway** (`_on_packet` → `_handle_ota_command`) matches command to cached binary
7. **BLE OTA** (`ble_ota.py`) connects to ESP32 via `ble_mac`, flashes in 241-byte NUS chunks
8. **ESP32** validates SHA-256, writes to OTA partition, reboots
9. **ACK** returns via `TelemetrySender.send_ack()` → hub DB status → `acknowledged`

## Component Reference

### Hub (Mac mini) — `documents/`

| Component | File | Responsibility |
|-----------|------|----------------|
| **Ingest daemon** | `reticulum_ingest.py` | Main daemon: `TelemetryDestination` (IN), `CommandAckDestination` (IN), `CommandDispatcher` (OUT), `GatewayAnnounceHandler` (auto-provision), DB writes, periodic re-announce |
| **OTA scheduler** | `ota_scheduler.py` | Nightly batch scheduling, `dispatch_ota()` — RNS Link + Resource transfer, SHA-256 verification, retry logic |
| **Database** | `farm_data.db` | SQLite: `sensor_readings`, `hardware_devices`, `actuator_commands`, `reticulum_gateways`, `telemetry_ingress`, `ble_link_log` |

### Gateway (mimi + Pico) — `bt_bridge/`

| Component | File | Responsibility |
|-----------|------|----------------|
| **RNS forwarder** | `ble_forwarder.py` | `TelemetrySender` (OUT SINGLE, announce-based discovery), `GatewayCommandReceiver` (IN SINGLE, receives commands + OTA), serial loop (Pico ↔ RNS), periodic re-announce |
| **BLE OTA relay** | `ble_ota.py` | `handle_ota_command()` — bleak BLE client, NUS chunk protocol (BEGIN/DATA/END), retry with exponential backoff, ACK back to hub |
| **Firmware cache** | `fw_cache.py` | Disk cache at `/var/cache/agronomi/ota/`, SHA-256 verification, atomic writes |
| **Pico firmware** | `main.py` | MicroPython BLE GATT server (NUS), IRQ-driven ring buffer, serial bridge `[JSON]`/`[CMD]`/`[ACK]` line protocol |
| **Gateway config** | `ble_forwarder.toml` | `gateway_id`, `serial_port`, `identity_path`, `command_aspect`, `ble_mac_map` |

### ESP32 Nodes — `src/` + `lib/`

| Component | File | Responsibility |
|-----------|------|----------------|
| **BLE client** | `lib/FleetBLE/BLEManager.cpp` | NimBLE client: scan for gateway, connect, send telemetry JSON on NUS RX, receive commands on NUS TX (notify), command callback dispatch |
| **Telemetry builder** | `lib/FleetCommon/Telemetry.cpp` | JSON builder: `dev_id`, `device_type`, `fw_ver`, `ble_mac`, `seq`, readings dict |
| **OTA receiver** | `lib/FleetOTA/OTAManager.cpp` | BLE OTA protocol: `beginBLE()` → `writeChunk()` × N → `finalizeBLE()` (validate, set boot partition, reboot) |
| **Air node** | `src/sn_air/main.cpp` | DHT22 sensor, deep sleep 300s |
| **Soil node** | `src/sn_soil/main.cpp` | DS18B20 + capacitive moisture, deep sleep 300s |

### Vision Node (separate path)

| Component | File | Responsibility |
|-----------|------|----------------|
| **Vision node** | `src/sn_vision/main.cpp` | ESP32-CAM: captures JPEG, posts via WiFi to hub |
| **Vision ingest** | `documents/vision_ingest.py` | FastAPI server: receives images, runs EfficientNet+ViT plant diagnosis, stores in DB |

## Database Schema (key tables)

```sql
hardware_devices        -- device_id PK, device_type, ble_mac, ble_target_gateway, firmware_version, status
reticulum_gateways      -- gateway_id PK, rns_destination_hash, lora_* config, last_heartbeat
sensor_readings         -- node_id, reading_type, value, unit, recorded_at
actuator_commands       -- cmd_id PK, device_id, cmd_type, cmd_value_text, status, retry_count
```

## OTA Status Tracking

| Status | Meaning |
|--------|---------|
| `pending` | Queued, waiting for dispatch window |
| `transferring` | RNS Resource transfer in progress (prevents duplicate dispatch) |
| `sent` | Binary delivered to gateway, command packet sent |
| `acknowledged` | ESP32 flashed successfully, ACK received |
| `failed` | Transfer failed after max retries |

## RNS Addressing

All destinations use **SINGLE** type with announce-based discovery:

- Hub identity stored in `./farm_hub.identity`
- Gateway identity stored in `./gateway.identity`
- Gateways announce `farm.gateway_commands` with app_data `agronomi-gateway:GW-MIMI-01` — hub auto-provisions `reticulum_gateways` table
- Hub announces `farm.telemetry_readings` and `farm.commands_control` — gateways discover via `Transport.register_announce_handler()`
- Hub re-announces every 30 seconds; gateways re-announce every 30 seconds