# AgroNomi — System Gaps & OTA Strategy
**Status:** Working document  
**Date:** 2026-05-16

---

## 1. Current State (What Actually Works)

From the code and DB on hand:

- `reticulum_ingest.py` on hub (Mac Mini) — receives telemetry via RNS SINGLE destination, writes `sensor_readings`. Working: 14 readings confirmed in `farm_data.db` from today's tests.
- `ble_forwarder.py` on Mimi — reads `[JSON]` lines from Pico serial, forwards via RNS SINGLE destination to hub using announce-based discovery. Working.
- Pico `main.py` (MicroPython) — NUS GATT peripheral, ESP32-C6 nodes connect as central and write telemetry to RX characteristic, Pico prints `[JSON]` to serial. Working.

**Telemetry path is functional end to end.**

---

## 2. Identified Gaps

### 2.1 Database Provisioning (Blocker for Everything)

`sensor_nodes`, `hardware_devices`, `reticulum_gateways` are all empty in `farm_data.db`. Yet `sensor_readings` has 14 rows with `node_id` values (`SN-SOIL-01`, `SN-AIR-01`) that reference non-existent `sensor_nodes` rows.

Root cause: `PRAGMA foreign_keys=ON` is set per connection in `get_db()`, but `record_telemetry()` writes readings for nodes that were never inserted into `sensor_nodes`. FK constraints are not catching this because the node rows simply don't exist — they were never provisioned.

**What's missing:** a provisioning mechanism that inserts a node into `sensor_nodes`, `hardware_devices`, and (for gateways) `reticulum_gateways` when it first appears, or before deployment via a provisioning script.

### 2.2 Gateway Has No RNS Identity or Destination

`ble_forwarder.py` on Mimi discovers the hub via announce and sends telemetry, but it never registers its own SINGLE destination and never announces itself. Therefore:

- `reticulum_gateways.rns_destination_hash` is empty — the hub's `CommandDispatcher` has no address to dispatch commands to.
- The hub cannot reach the gateway at all for outbound traffic.

**What's missing:** `ble_forwarder.py` must load or create a persistent identity, register a SINGLE destination (e.g. `farm.gateway.commands`), announce it at startup and periodically, and store its own destination hash in the DB (or have it provisioned there).

### 2.3 Command Path Does Not Exist (Hub → Gateway → Pico → C6)

The entire downward command path is unimplemented:

- `ble_forwarder.py` has no RNS listener for inbound command packets from the hub.
- `ble_forwarder.py` never writes to the serial port — only reads from it.
- Pico `main.py` has no serial read loop — only prints to serial.
- Pico never calls `ble.gatts_notify()` on the TX characteristic — it is initialized but never used.
- ESP32-C6 firmware does not exist yet.

The complete command chain that needs to be built:

```
hub actuator_commands table (pending row)
  → CommandDispatcher polls, sends RNS packet to gateway destination
    → ble_forwarder.py receives packet, writes [CMD] JSON line to serial
      → Pico reads [CMD] from serial, calls gatts_notify(_conn, _tx, payload)
        → ESP32-C6 receives notify, executes actuator (GPIO relay etc.)
          → ESP32-C6 writes ACK to RX characteristic
            → Pico receives _IRQ_GATTS_WRITE, prints [ACK] to serial
              → ble_forwarder.py reads [ACK], sends RNS packet to hub
                → hub CommandAckDestination updates actuator_commands to 'acknowledged'
```

### 2.4 OTA on ESP32-C6 Is WiFi-Only

The existing `OTAManager.cpp` uses `HTTPUpdate` over WiFi. Field-deployed C6 nodes are battery-powered and BLE-only — they have no WiFi connection during normal operation. OTA via WiFi requires the node to wake, connect to WiFi, download firmware, and reboot. This is feasible as a triggered OTA mode (hub sends `ota_request` command, C6 switches to WiFi temporarily) but requires WiFi credentials to be provisioned on each node, and fails entirely in fields without WiFi coverage.

---

## 3. OTA via rngit — Analysis

### 3.1 What rngit Actually Is

`rngit` (available since RNS 1.2.0) is a Git repository hosting and access system that runs entirely over Reticulum. It provides:

- A server node (`rngit`) that hosts bare Git repositories, accessible via `rns://DESTINATION_HASH/group/repo`
- A `git-remote-rns` helper that makes standard `git` commands (clone, pull, push) work transparently over RNS
- Release management: versioned firmware artifacts can be published via `rngit release create`
- Work documents: issue/task tracking over the mesh

The hub (Mac Mini) could run `rngit` as a service, hosting an `agronomi/firmware` repository. Gateways (Pi Zero 2W in production) could `git pull rns://HUB_HASH/agronomi/firmware` to update their own Python code over LoRa.

### 3.2 What rngit Cannot Do

rngit runs on Linux/Python nodes. It cannot run on ESP32-C6 directly. Firmware binary delivery to the C6 must go through the gateway, which then relays it to the C6 over BLE using the RNS `Resource` API (for reliable large data transfer) or a chunked BLE transfer protocol.

The RNS `Resource` API is the right primitive for this: it handles breaking large data into packets, sequencing, integrity verification, and reassembly — exactly what firmware transfer needs. A `Resource` transfer over an established `Link` would carry the firmware binary from hub to gateway; the gateway then handles the BLE relay to the C6.

### 3.3 Proposed OTA Architecture

**Gateway OTA (Pi Zero 2W / Mimi):**
```
Hub rngit node  →  RNS Link + Resource  →  Gateway pulls firmware binary
                                             git pull rns://HUB/agronomi/gateway
                                             systemctl restart agronomi-gateway
```

**ESP32-C6 OTA:**
```
Hub  →  RNS Resource (firmware binary)  →  Gateway receives binary
                                            Gateway relays over BLE in chunks
                                            C6 writes to OTA partition via esp_ota_ops
                                            C6 reboots into new firmware
                                            C6 sends OTA ACK via BLE → Gateway → RNS → Hub
```

The C6 OTA trigger would be a new command type in `actuator_commands`: `cmd_type = 'ota_request'` with `cmd_value` pointing to a firmware version string. The gateway fetches the binary from the hub via RNS Resource, then performs the BLE relay.

For fields without WiFi, this works entirely over LoRa — slow (SF11, ~0.54 kbps) but a typical ESP32 firmware binary is 1-2MB, which at LoRa rates would take many minutes. Practically, OTA over LoRa is feasible only for small delta updates or in non-time-critical maintenance windows. A compressed binary or a diff-based update (bsdiff) would be necessary for production LoRa OTA.

For greenhouses with WiFi, the existing `OTAManager.cpp` WiFi path remains viable as a fast track, triggered by the same `ota_request` command.

### 3.4 rngit for Gateway Code Management

Even if C6 OTA over LoRa is slow, `rngit` is immediately useful for gateway management:

- Hub hosts `agronomi/gateway` repo
- Pi Zero 2W gateways auto-pull on a schedule or on `ota_request` command
- New gateway Python code (`ble_forwarder.py`, etc.) deployed to all gateways via `git pull` over the existing RNS mesh — no SSH, no internet required
- Work documents track field issues per deployment

---

## 4. Priority Order

1. **Provisioning** — insert `sensor_nodes`, `hardware_devices`, `reticulum_gateways` rows correctly; fix FK integrity. Foundation for everything.
2. **Gateway RNS registration** — `ble_forwarder.py` gets a persistent identity, registers and announces its SINGLE destination, stores hash in DB.
3. **Command path** — bidirectional serial (Pico), `ble_forwarder.py` RNS listener, ACK relay.
4. **ESP32-C6 firmware** — BLE central, NUS client, TX notify subscriber, ACK writer, GPIO actuator.
5. **rngit on hub** — host firmware repo, gateway auto-update.
6. **C6 OTA** — BLE chunked relay from gateway, `esp_ota_ops` on C6, triggered by command.

---

*All items above are grounded in actual code (`reticulum_ingest.py`, `ble_forwarder.py`, Pico `main.py`, `OTAManager.cpp`) and the RNS 1.2.5 manual. No speculative architecture.*
