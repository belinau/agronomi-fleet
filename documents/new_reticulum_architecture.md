
# AgroNomi µReticulum-Native Architecture & Migration Plan

> **Status**: Active migration — Phase 1 complete, Phase 2 in progress  
> **Last updated**: 2025-07 (major revision: corrected LoRa topology, OTA strategy, rnsd setup)  
> **Replaces**: `documents/fleet_architecture.md` (BLE-based architecture)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Architecture Comparison](#2-architecture-comparison)
3. [New Architecture — µReticulum Native](#3-new-architecture--µreticulum-native)
4. [µReticulum Stack Reference](#4-µreticulum-stack-reference)
5. [Fleet Device Reference](#5-fleet-device-reference)
6. [Communication Topology](#6-communication-topology)
7. [Data Flows](#7-data-flows)
8. [Hub Simplification](#8-hub-simplification)
9. [Database Schema Changes](#9-database-schema-changes)
10. [Migration Phases](#10-migration-phases)
11. [Firmware Directory Structure](#11-firmware-directory-structure)
12. [Per-Device Configuration](#12-per-device-configuration)
13. [Identity Auto-Provisioning](#13-identity-auto-provisioning)
14. [BLE as a RNS Interface](#14-ble-as-a-rns-interface)
15. [OTA via Reticulum Utilities (rncp / rngit)](#15-ota-via-reticulum-utilities-rncp--rngit)
16. [Decommissioned Components](#16-decommissioned-components)
17. [rnsd Service Setup](#17-rnsd-service-setup)
18. [Risk Assessment](#18-risk-assessment)

---

## 1. Executive Summary

The AgroNomi fleet is migrating from a **BLE-bridged architecture** (ESP32 → BLE → Pico 2W → USB Serial → RNode LoRa → Hub) to a **µReticulum-native architecture** where every ESP32 sensor/actuator node speaks the Reticulum Network Stack (RNS) protocol directly. This eliminates the Pico 2W BLE bridge entirely, replaces the proprietary BLE NUS binary protocol with standard RNS Packet/Resource transfers, and unifies all inter-device communication under a single mesh protocol.

**Key outcomes:**

- The `bt_bridge/` Pico 2W component becomes **obsolete** (replaced by direct BLE-to-RNode or optional ESP32-C6 gateway)
- All nodes speak RNS natively — same protocol, same packet format, same encryption
- **LoRa is via RNodes (RAK4631) connected via USB** to hub (Mac mini) and optionally to the field gateway computer (mimi HP/Ubuntu). ESP32-C6 nodes do **NOT** have LoRa radios.
- **The ESP32-C6 gateway is OPTIONAL** — the minimal deployment is Hub + RNode + sensor nodes. RNodes (RAK4631) support BLE connections, so ESP32-C6 µR nodes can connect directly to an RNode over BLE using `BLEClientInterface`, eliminating the need for a separate gateway in the simplest deployment.
- The ESP32-C6 gateway is only needed for: (a) WiFi bridging in greenhouses without WiFi at the RNode location, or (b) BLE relay when the RNode can't reach all sensor nodes directly
- **rnsd** runs as a system service on the hub (and optionally on mimi), providing always-on RNS transport and enabling `rncp`/`rngit` for OTA
- Hub's `reticulum_ingest.py` drops all BLE-specific code
- **OTA firmware distribution uses `rncp`/`rngit`** (standard Reticulum utilities) instead of custom BLE binary protocol
- Announce-based auto-discovery replaces manual BLE pairing
- **RNS handles path selection automatically** — if a node has multiple interfaces (BLE + WiFi), RNS picks the fastest path. No manual routing needed.
- **RNode capacity is generous**: 50–100 nodes per RNode at SF7/BW125. The constraint is airtime, not device count.

**IMPORTANT CORRECTION**: Earlier versions of this document incorrectly showed ESP32-C6 nodes with LoRa (E32/SX1262) radios. The current hardware does **NOT** have LoRa modules on ESP32-C6 boards. LoRa connectivity comes exclusively from RAK4631 RNodes connected via USB to the hub and optionally to the field gateway. The E32/SX1262 interface configs are retained in µR code for **FUTURE** use when LoRa modules may be mounted on ESP32-C6 boards.

---

## 2. Architecture Comparison

### OLD Architecture — BLE-Bridged

```
┌────────────────────┐   RNode LoRa   RNode LoRa   ┌──────────────────┐   USB Serial   ┌──────────────┐   BLE NUS   ┌──────────────┐
│   Mac mini (Hub)   │  ◄──────────────────────────► │  mimi (HP/Ubuntu)│◄──────────────►│   Pico 2W    │◄───────────►│  ESP32-C6     │
│                    │                               │                  │                │  (MicroPython)│              │  Sensor Node  │
│ reticulum_ingest   │                               │  ble_forwarder   │                │  BLE GATT SRV │              │  BLE Client   │
│ ota_scheduler      │                               │  ble_ota         │                │  GW-MIMI-01   │              │  (Arduino)    │
│ farm_data.db       │                               │  fw_cache        │                │              │              │  Deep Sleep   │
└────────────────────┘                               └──────────────────┘                └──────────────┘              └──────────────┘
       RNode USB                                          RNode USB + Pico 2W
```

**Problems with OLD architecture:**

| Problem | Detail |
|---------|--------|
| **Single point of failure** | Pico 2W bridges BLE ↔ Serial ↔ RNS — if it crashes, all field nodes are offline |
| **Protocol multiplication** | BLE NUS binary protocol, serial line protocol (`[JSON]`, `[CMD]`, `[ACK]`, `[HB]`), RNS Packet, RNS Resource — 4 distinct protocols |
| **No end-to-end encryption** | BLE NUS traffic is unencrypted between ESP32 and Pico; encryption only starts at the RNS layer |
| **Manual BLE pairing** | ESP32 nodes scan for gateway name (`GW-MIMI-01`) — no cryptographic authentication |
| **Complex OTA** | 8-step OTA chain: Hub → RNS Resource → mimi → fw_cache → BLE OTA → NUS chunks → ESP32 → validate → reboot |
| **BLE MAC hardcoding** | `ble_mac` and `ble_target_gateway` stored in DB per device — brittle |

### NEW Architecture — µReticulum Native

**Minimal deployment (recommended — no gateway required):**

```
Hub (Mac mini)                    Field
┌─────────────────┐              ┌─────────────────┐
│ rnsd             │              │ RAK4631 RNode   │
│ reticulum_ingest │◄──LoRa─────│  (USB to mini   │
│ farm_data.db     │              │   OR standalone)│
│ RNode USB        │              │                 │
│ rncp/rngit       │              │  BLE ←──────────│── ESP32-C6 nodes
└─────────────────┘              └─────────────────┘
```

ESP32-C6 µR nodes connect **directly** to the RNode over BLE using `BLEClientInterface`. No gateway computer needed. The RNode serves as both the LoRa radio (to hub) and the BLE access point (for field nodes). RNS handles path selection automatically.

**Extended deployment (with WiFi bridging / BLE relay):**

```
Hub (Mac mini)                         Field (mimi HP/Ubuntu)
┌──────────────────┐                    ┌──────────────────────────┐
│  rnsd            │                    │  rnsd                    │
│  reticulum_ingest│◄── LoRa ────────► │  (transport node)        │
│  farm_data.db    │    RNode↔RNode     │                          │
│  RNode USB       │                    │  RNode USB               │
└──────────────────┘                    │       │ USB               │
                                        │       ▼                   │
                                        │  ESP32-C6 Gateway       │
                                        │  (µR Transport=OFF)     │
                                        │  BLE server + WiFi      │
                                        │       │ BLE/WiFi         │
                                        │       ▼                  │
                                        │  ┌─────────────────┐    │
                                        │  │ ESP32-C6 Nodes  │    │
                                        │  │ µR sensors/     │    │
                                        │  │ actuators       │    │
                                        │  └─────────────────┘    │
                                        └──────────────────────────┘
```

The extended deployment adds a field gateway (mimi) with an ESP32-C6 gateway for:
- **WiFi bridging**: indoor/greenhouse nodes that have WiFi but are out of BLE range of the RNode
- **BLE relay**: when the RNode can't physically reach all sensor nodes directly (e.g., nodes in metal enclosures or far from the RNode)

**Key points:**
- **LoRa is via RNodes (RAK4631)** connected via USB to the hub (Mac mini) and optionally to the field gateway computer (mimi). ESP32-C6 nodes do **NOT** have LoRa radios.
- **ESP32-C6 gateway is OPTIONAL** — in the minimal deployment, nodes connect directly to the RNode via BLE using `BLEClientInterface`
- **rnsd** runs as a system service on the hub (and optionally on mimi), providing always-on RNS transport and enabling `rncp`/`rngit` for OTA

**Future possibility — ESP32-C6 + E32/SX1262 LoRa module (autonomous):**

```
┌──────────────────────┐
│  ESP32-C6 Node        │
│  µReticulum           │
│  LoRa (E32/SX1262)    │──── LoRa ────► Hub RNode
│  Sensors/Actuators    │
└──────────────────────┘
```

This would make ESP32-C6 nodes fully autonomous LoRa nodes, but requires hardware E32/SX1262 modules connected to the ESP32-C6. The µR stack already supports `E32Interface` and `LoRaInterface` for this case. Mark these interface configs as **FUTURE: requires LoRa module hardware**.

### Side-by-Side Comparison

| Aspect | OLD (BLE Bridge) | NEW (µR Native) |
|--------|------------------|-----------------|
| **Node firmware** | PlatformIO/Arduino (C++) | MicroPython + µR stack |
| **Transport to gateway** | BLE NUS only | BLE (primary for field nodes), WiFi (indoor/greenhouse), LoRa (future, requires E32/SX1262 module) |
| **Bridge device** | Pico 2W (mandatory) | ESP32-C6 gateway (**optional** — only for WiFi bridging or BLE relay; nodes can connect directly to RNode via BLE) |
| **End-to-end encryption** | No — BLE is cleartext | Yes — RNS identity-based encryption from node to hub |
| **Discovery** | BLE name scan (`GW-MIMI-01`) | RNS Announce-based (cryptographic) |
| **Protocol count** | 4 (BLE NUS, Serial Line, RNS Packet, RNS Resource) | 2 (RNS Packet, RNS Resource) |
| **OTA** | BLE binary protocol (8 steps) | `rncp` / `rngit` (RNS utilities, 3 steps) |
| **Hub BLE code** | `ble_link_log`, `ble_mac`, `ble_target_gateway` | None — all RNS |
| **SPOF** | Pico 2W | Eliminated — RNS mesh routing; RNode is shared LoRa+BLE; ESP32-C6 gateway only needed for extended deployments |

---

## 3. New Architecture — µReticulum Native

### Design Principles

1. **Every node speaks RNS** — same protocol, same packet format, same encryption, regardless of physical transport
2. **Transport mode is handled by rnsd on hub/mimi** — the ESP32-C6 gateway is optional; `rnsd` on hub (and optionally mimi) handles all RNS transport including LoRa (via RNodes) and path forwarding
3. **Announce-based discovery** — nodes and gateways announce their destinations; no manual pairing or MAC addressing
4. **Encryption end-to-end** — RNS identity-based encryption from source to destination, even through gateways
5. **Three transport tiers** — LoRa (via RNodes on hub/gateway computers, primary/field), BLE (secondary/local, ESP32-C6 nodes direct to RNode), WiFi (tertiary/greenhouse)
6. **RNS handles path selection automatically** — if a node has multiple interfaces (BLE + WiFi), RNS picks the fastest path. No manual routing needed.
7. **Minimal deployment is Hub + RNode + sensor nodes** — no gateway computer required. RNodes support BLE, so ESP32-C6 nodes connect directly via `RNodeBLEInterface` (KISS over BLE NUS — same protocol as USB serial RNodeInterface).

### System Topology

#### Minimal Deployment (Recommended)

The simplest deployment needs only a Hub (Mac mini) with an RNode and sensor nodes in the field. No gateway computer or ESP32-C6 gateway required.

```
                         ┌───────────────────────────────────────┐
                         │            CLOUD / LAN                │
                         │                                       │
                         │   ┌─────────────────────┐             │
                         │   │   Mac mini (Hub)     │             │
                         │   │                     │             │
                         │   │  rnsd (system svc)  │             │
                         │   │  reticulum_ingest   │             │
                         │   │  TelemetryDest (IN) │◄── RNS Announce ─┐
                         │   │  CommandAckDest(IN) │              │ │
                         │   │  GatewayAnnounceHdlr│              │ │
                         │   │  CommandDispatcher  │──► RNS Packet ──┐│ │
                         │   │  farm_data.db       │              │ │ │
                         │   │  RNode USB (RAK4631)│              │ │ │
                         │   └─────────┬───────────┘              │ │ │
                         │             │ LoRa (via RNode)          │ │ │
                         └─────────────┼──────────────────────────┘ │ │
                                       │                            │ │
                         ┌─────────────┼────────────────────────────┘ │
                         │             │      RNS Mesh (LoRa)         │ │
                         │             │                              │ │
                         │  ┌──────────┴──────────┐                │ │
                         │  │  RAK4631 RNode       │                │ │
                         │  │  (standalone in field)│                │ │
                         │  │  LoRa ↔ Hub          │                │ │
                         │  │  BLE  ↔ sensor nodes │                │ │
                         │  │       │ BLE             │                │ │
                         │  │       ▼                 │                │ │
                         │  │  ┌─────────────────┐ │                │ │
                         │  │  │ ESP32-C6 Nodes  │ │                │ │
                         │  │  │ µR sensors/     │ │                │ │
                         │  │  │ actuators       │ │                │ │
                         │  │  RNodeBLEInterface│                │ │
                         │  │  │ (→ RNode BLE)  │ │                │ │
                         │  │  └─────────────────┘ │                │ │
                         │  └───────────────────────┘                │ │
                         └──────────────────────────────────────────┘ │
```

In the minimal deployment:
- The **RNode** is in the field (powered independently or via solar), connected to the Hub via LoRa
- ESP32-C6 sensor/actuator nodes connect **directly** to the RNode over BLE using `BLEClientInterface`
- The RNode acts as both the LoRa link (to Hub) and the BLE access point (for field nodes)
- **No gateway computer or ESP32-C6 gateway needed**
- RNS Transport on the Hub's `rnsd` handles all routing between interfaces

#### Extended Deployment (with mimi as redundant transport node)

For larger deployments or redundancy, add a second computer (mimi) with its own RNode. mimi can also host an ESP32-C6 gateway for WiFi bridging or BLE relay.

```
                              ┌─────────────────────────────────────────────────┐
                              │                   CLOUD / LAN                   │
                              │                                                   │
                              │   ┌─────────────────────┐                        │
                              │   │   Mac mini (Hub)     │                        │
                              │   │  rnsd (system svc)  │                        │
                              │   │  RNode USB (RAK4631) │                        │
                              │   └─────────┬───────────┘                        │
                              │             │ LoRa (via RNode)                    │
                              └─────────────┼───────────────────────────────────┘
                              ┌─────────────┼─────────────────────────────────────┐
                              │             │         RNS Mesh (LoRa via RNodes)    │
                              │             │                                       │
                              │  ┌──────────┴──────────┐                           │
                              │  │  mimi (HP/Ubuntu)    │                           │
                              │  │  rnsd (system svc)   │                           │
                              │  │  RNode USB (RAK4631) │◄──── LoRa ──────────────┘ │
                              │  │       │ USB           │                           │
                              │  │       ▼               │                           │
                              │  │  ESP32-C6 Gateway    │                           │
                              │  │  (OPTIONAL)          │                           │
                              │  │  BLE server + WiFi   │                           │
                              │  │       │ BLE/WiFi      │                           │
                              │  │       ▼               │                           │
                              │  │  ┌─────────────────┐ │                           │
                              │  │  │ ESP32-C6 Nodes  │ │                           │
                              │  │  │ µR sensors/     │ │                           │
                              │  │  │ actuators       │ │                           │
                              │  │  └─────────────────┘ │                           │
                              │  └──────────────────────┘                           │
                              └──────────────────────────────────────────────────┘
                              ┌──────────────────────────────────────────────────┐
                              │           INDOOR / GREENHOUSE (WiFi)                │
                              │                                                     │
                              │   ┌──────────────────┐     ┌──────────────────┐    │
                              │   │  mimi + ESP32-C6 │     │  ESP32-C6 Node   │    │
                              │   │  Gateway (WiFi)   │────►│  (WiFi only)     │    │
                              │   │  µR Transport=OFF│WiFi │  µReticulum      │    │
                              │   └──────────────────┘     │  Sensors/Actuators│    │
                              │                            └──────────────────┘    │
                              │   ┌──────────────────┐                              │
                              │   │  ESP32-CAM       │                              │
                              │   │  SN-VIS-GH-01    │                              │
                              │   │  WiFi POST       │  (Special case — may stay    │
                              │   └──────────────────┘   outside RNS mesh)          │
                              └──────────────────────────────────────────────────────┘
```

In the extended deployment:
- **mimi** runs `rnsd` with its own RNode, providing redundancy for LoRa transport
- The **ESP32-C6 gateway** on mimi bridges BLE/WiFi nodes that can't reach the RNode directly
- Nodes within BLE range of the RNode connect directly via `RNodeBLEInterface` (KISS/NUS — no gateway needed)
- Nodes outside BLE range of the RNode use the gateway as a relay
- Greenhouse nodes use WiFi when available

**Future possibility — ESP32-C6 with direct LoRa module:**

```
┌──────────────────────┐
│  ESP32-C6 Node        │
│  µReticulum           │
│  LoRa (E32/SX1262)    │──── LoRa ────► Hub RNode
│  Sensors/Actuators    │                (autonomous — no gateway needed)
└──────────────────────┘
```

This requires hardware E32/SX1262 LoRa modules connected to the ESP32-C6. The µR
stack already supports `E32Interface` and `LoRaInterface` for this case, but the
current physical hardware does **NOT** have these modules mounted.

**RNode BLE connectivity:** RAK4631 RNodes running standard RNode firmware support
BLE connections (enabled with `rnodeconf --bluetooth-on`). This allows ESP32-C6 µR
nodes to connect directly to an RNode over BLE, creating a direct
ESP32→BLE→RNode→LoRa path without needing the ESP32-C6 gateway.

The µR `RNodeBLEInterface` (`urns/interfaces/rnode_ble.py`) implements the **same
KISS-over-NUS protocol** that the official RNS `RNodeInterface` uses over USB serial —
it scans for RNodes advertising the Nordic UART Service, connects via GATT, performs
the KISS detection handshake (DETECT_REQ=0x73, expects DETECT_ACK=0x46), configures
radio parameters (frequency, bandwidth, SF, CR, TX power), and then sends/receives
RNS packets via KISS DATA frames. This is **not** a custom protocol — it's a MicroPython
port of the official BLE connection path.

Full RNS on mimi/hub can also connect to the same RNode over BLE using
`RNodeInterface` with `port = ble://RNode-XXXX`.

This means an RNode in the field can serve as a **shared LoRa radio**: mimi connects
via USB, while ESP32-C6 nodes connect via BLE simultaneously. Reticulum handles
path selection across interfaces automatically — if a node has both BLE-to-RNode
and WiFi paths, it will use whichever is fastest.

**Primary paths (minimal deployment first):**

```
# Path A: Direct to RNode via BLE (RECOMMENDED — minimal deployment)
ESP32-C6 µR node → BLE → RAK4631 RNode (BLEClientInterface) → LoRa → Hub RNode → Hub rnsd

# Path B: Via ESP32-C6 gateway (extended deployment only)
ESP32-C6 µR node → BLE → ESP32-C6 gateway (BLE↔Serial bridge) → WiFi/Serial → mimi → RNode USB → LoRa → Hub

# Path C: WiFi (greenhouse/indoor)
ESP32-C6 µR node → WiFi → Hub/mimi rnsd (AutoInterface)
```

---

## 4. µReticulum Stack Reference

The µR stack lives in `m_reticulum/esp32c6/firmware/urns/` and is already functional on ESP32-C6.

### Core Modules

| Module | File | Purpose |
|--------|------|---------|
| **Reticulum** | `urns/reticulum.py` | Core engine: JSON config, async event loop, identity management, interface lifecycle |
| **Identity** | `urns/identity.py` | X25519 + Ed25519 identity, key storage, ratchets |
| **Destination** | `urns/destination.py` | SINGLE/GROUP/PLAIN destinations, announce, encrypt/decrypt |
| **Packet** | `urns/packet.py` | Packet framing, HDR_1/HDR_2, proof receipts |
| **Transport** | `urns/transport.py` | Blind flood forwarding between interfaces, transport mode |
| **Link** | `urns/link.py` | ECDH link establishment (X25519 key exchange) |
| **LXMF** | `urns/lxmf.py` | LXMF messaging (destination-based, encrypted, signed) |
| **Resource** | `urns/resource.py` | Large resource transfer (split into packets) |
| **Constants** | `urns/const.py` | All protocol constants (MTU, header sizes, packet types, etc.) |
| **Logging** | `urns/log.py` | Structured logging |
| **Msgpack** | `urns/umsgpack.py` | Lightweight serialization |
| **BZ2** | `urns/bz2dec.py` | BZ2 decompression for resources |

### Crypto Modules (`urns/crypto/`)

| Module | File | Purpose |
|--------|------|---------|
| **X25519** | `crypto/x25519.py` | Elliptic-curve Diffie-Hellman key exchange |
| **Ed25519** | `crypto/ed25519.py` | Ed25519 digital signatures |
| **AES** | `crypto/aes.py` | AES-128-CTR + AES-256-CBC encryption |
| **SHA256** | `crypto/hashes.py` | SHA-256 hashing |
| **HKDF** | `crypto/hkdf.py` | HMAC-based key derivation |
| **HMAC** | `crypto/hmac.py` | HMAC message authentication |
| **PKCS7** | `crypto/pkcs7.py` | PKCS7 padding |
| **SHA512** | `crypto/sha512.py` | SHA-512 hashing |
| **Token** | `crypto/token.py` | Token generation |
| **Pure25519** | `crypto/pure25519/` | Pure-Python Ed25519 and X25519 implementation |

### Interface Modules (`urns/interfaces/`)

| Interface | File | Transport | Direction |
|-----------|------|-----------|-----------|
| **UDP** | `interfaces/udp.py` | WiFi/Ethernet UDP | Bidirectional |
| **Serial** | `interfaces/serial.py` | USB UART | Bidirectional |
| **E32 LoRa** | `interfaces/e32.py` | EBYTE E32 LoRa modules | Bidirectional |
| **SX1262 LoRa** | `interfaces/lora.py` | Semtech SX1262 LoRa | Bidirectional |
| **TCP** | `interfaces/tcp.py` | TCP client | Bidirectional |
| **RNode BLE** | `interfaces/rnode_ble.py` | BLE KISS/NUS to RAK4631 RNode | Bidirectional (PRIMARY) |
| **BLE Client** | `interfaces/ble_client.py` | BLE GATT client to ESP32-C6 gateway | Bidirectional |
| **BLE** | `urns/ble_interface.py` | BLE GATT server (gateway role) | Bidirectional |

### Key Protocol Constants (`urns/const.py`)

| Constant | Value | Notes |
|----------|-------|-------|
| `MTU` | 500 | Maximum Transfer Unit |
| `TRUNCATED_HASHLENGTH` | 128 bits | Destination address length |
| `MDU` | 464 | Maximum Data Unit (MTU - header overhead) |
| `DEST_SINGLE` | `0x00` | Single-destination (announce-based) |
| `DEST_GROUP` | `0x01` | Group destination |
| `DEST_PLAIN` | `0x02` | Plaintext destination |
| `PKT_DATA` | `0x00` | Data packet type |
| `PKT_ANNOUNCE` | `0x01` | Announce packet type |
| `PKT_PROOF` | `0x03` | Proof packet type |
| `TRANSPORT_TRANSPORT` | `0x01` | Transport mode (forwarding) |
| `MAX_DESTINATIONS` | 64 | MCU memory limit |
| `MAX_ACTIVE_LINKS` | 4 | Concurrent link limit |
| `TRANSPORT_HOPLIMIT` | 128 | Maximum hops |

---

## 5. Fleet Device Reference

From `platformio.ini` build configurations and `hardware_devices` table:

| Device ID | Type | Platform | Sensors/Actuators | Transport (OLD) | Transport (NEW) |
|-----------|------|----------|-------------------|------------------|------------------|
| `SN-SOIL-01` | `soil_node` | ESP32-C6 | Capacitive soil moisture, DS18B20 temp, battery ADC | BLE NUS → Pico | BLE (primary, to gateway), WiFi (if available). LoRa only with E32/SX1262 module (FUTURE) |
| `SN-AIR-01` | `air_node` | ESP32-C6 | DHT22 temp/humidity, battery ADC | BLE NUS → Pico | BLE (primary, to gateway), WiFi (if available). LoRa only with E32/SX1262 module (FUTURE) |
| `AN-GREENHOUSE-01` | `gh_actuator` | ESP32-C6 | None (actuator only) | BLE NUS → Pico | WiFi (primary, in greenhouse), BLE (fallback). LoRa only with E32/SX1262 module (FUTURE) |
| `AN-PUMP-01` | `pump_node` | ESP32-C6 | None (actuator only) | BLE NUS → Pico | BLE (primary, to gateway). LoRa only with E32/SX1262 module (FUTURE) |
| `SN-VIS-GH-01` | `vision_node` | ESP32-CAM | ESP32-CAM camera | WiFi POST | WiFi POST (special case — may stay outside RNS mesh) |
| `GW-MIMI-01` | `gateway` | ESP32-C6 | None (transport bridge) — **OPTIONAL** | USB Serial → mimi RNode | BLE server + WiFi. **Optional** — only needed for WiFi bridging in greenhouses or BLE relay when RNode can't reach all nodes directly. LoRa comes from RNode, NOT from ESP32-C6 |
| `RN-RAK-01` | `rnode_lora` | RAK4631 (nRF52840+SX1276) | LoRa radio + BLE + USB | LoRa (primary, to Hub) + BLE (for ESP32-C6 nodes) | RNode serves as both LoRa link to Hub and BLE access point for field nodes. ESP32-C6 nodes connect directly via `RNodeBLEInterface` (KISS/NUS). Enabled with `rnodeconf --bluetooth-on` |

### Sensor Pin Mappings (from `platformio.ini` build flags)

| Device | Build Flags | Key Pins |
|--------|------------|----------|
| `SN-SOIL-01` | `HAS_SOIL_SENSOR=1`, `HAS_TEMP_SENSOR=1`, `HAS_BAT_RESISTORS=1` | Soil moisture ADC, DS18B20 OneWire, battery voltage divider |
| `SN-AIR-01` | `HAS_DHT22=1`, `HAS_BAT_RESISTORS=0` | DHT22 data pin |
| `AN-GREENHOUSE-01` | No sensor flags | GPIO outputs for vent/fan/shade actuators |
| `AN-PUMP-01` | No sensor flags | GPIO output for pump relay |

---

## 6. Communication Topology

### Priority Order

| Priority | Transport | Use Case | Interface Class | Notes |
|----------|-----------|----------|-----------------|-------|
| 1 (primary) | **LoRa** | Hub ↔ Field long-range transport | RNode USB on hub (RAK4631) | LoRa is via RNodes connected to hub (Mac mini) and optionally mimi. RNodes also accept BLE connections from field nodes. |
| 2 (secondary) | **BLE** | ESP32-C6 nodes ↔ RNode (direct, no gateway needed) | `rnode_ble.py` (RNodeBLEInterface) | ESP32-C6 nodes connect directly to RNode over BLE using KISS/NUS protocol — same as USB serial but over BLE. Primary field transport in minimal deployment. |
| 2a | **BLE→Gateway** | ESP32-C6 nodes ↔ ESP32-C6 gateway (extended deployment only) | `ble_client.py` (BLEClientInterface) | Only needed when RNode can't reach all nodes directly, or for WiFi bridging in greenhouses. |
| 3 (tertiary) | **WiFi** | Indoor/greenhouse — high bandwidth, short range | `interfaces/udp.py` + AutoInterface | ESP32-C6 nodes in greenhouse can connect directly to hub/mimi via WiFi |

### Packet Flow — Telemetry (Node → Hub)

```
┌──────────────┐   RNS Packet    ┌──────────────────┐   RNS Packet    ┌──────────────┐
│  ESP32-C6    │   (BLE)         │  ESP32-C6 GW     │   (Serial)      │  mimi         │
│  Sensor Node │────────────────►│  BLE→Serial       │───────────────►│  (HP/Ubuntu)  │
│              │                 │  bridge           │                │              │
│  µR:         │                 │                   │                │  rnsd        │
│  Dest SINGLE │                 │  Forwards BLE     │                │  RNode USB   │──► LoRa ──► Hub
│  announce    │                 │  to Serial        │                │              │
└──────────────┘                 └───────────────────┘                └──────────────┘
                                         │ BLE
                                         │
                                  ┌──────────────┐
                                  │  ESP32-C6    │
                                  │  Sensor Node │
                                  │  (BLE-only)  │
                                  └──────────────┘

OR (future, with E32/SX1262 LoRa module on ESP32-C6):

┌──────────────┐   RNS Packet (LoRa)    ┌──────────────┐
│  ESP32-C6    │───────────────────────►│  Hub RNode   │
│  + LoRa      │   Direct LoRa          │  → Hub       │
│  Sensor Node │                        │              │
└──────────────┘                        └──────────────┘

OR (BLE direct to RNode — with BLEClientInterface):

┌──────────────┐   RNS Packet (BLE)   ┌──────────────┐   RNS Packet (LoRa)   ┌──────────────┐
│  ESP32-C6    │────────────────────►│  RAK4631     │─────────────────────►│  Hub RNode    │
│  Sensor Node │   BLEClientInterface│  RNode        │   LoRa mesh          │  → Hub rnsd  │
│  µR          │                     │  (BLE+LoRa)  │                      │              │
└──────────────┘                     └──────────────┘                      └──────────────┘

OR (WiFi in greenhouse):

┌──────────────┐   RNS Packet (WiFi)    ┌──────────────┐
│  ESP32-C6    │───────────────────────►│  Hub/mimi    │
│  Sensor Node │   WiFi AutoInterface   │  rnsd        │
└──────────────┘                        └──────────────┘
```

### Packet Flow — Commands (Hub → Node)

```
┌──────────────┐   RNS Packet    ┌──────────────────┐   RNS Packet    ┌──────────────┐
│  Mac mini    │   (LoRa)       │  mimi + RNode    │   (Serial)      │  ESP32-C6 GW │
│  Hub         │───────────────►│  rnsd            │───────────────►│  BLE/WiFi     │──────► Node
│              │                │  RNode USB       │                │  bridge       │
│  Command-    │                │                   │                │              │
│  Dispatcher  │                │  Forwards RNS     │                │  µR:         │
│              │                │  packets          │                │  Dest SINGLE │
└──────────────┘                └───────────────────┘                └──────────────┘
```

### Packet Flow — Announce/Discovery

```
┌──────────────┐  Announce    ┌──────────────────┐  Announce    ┌──────────────┐
│  Mac mini    │  (LoRa)      │  mimi + RNode     │  (Serial)    │  ESP32-C6 GW │
│  Hub         │◄─────────────│  rnsd             │◄─────────────│  BLE/WiFi     │◄── Announce ── Node
│              │              │  RNode USB        │              │  bridge       │
│  Announce    │─────────────►│  Floods to all    │─────────────►│  Floods BLE/  │───► Node
│  Handler     │              │  RNS interfaces    │              │  WiFi         │
└──────────────┘              └───────────────────┘              └──────────────┘
```

### RNS Destination Addressing

All destinations use **SINGLE** type with announce-based discovery. No manual hash copy-pasting.

| Role | RNS Aspect | Direction | Description |
|------|-----------|-----------|-------------|
| Hub telemetry | `farm.telemetry_readings` | IN | Sensor data JSON arrives here |
| Hub command ACK | `farm.commands_control` | IN | Command acknowledgements arrive here |
| Hub commands | `farm.gateway_commands` | IN | Gateway receives commands from hub |
| Sensor node telemetry | `farm.telemetry_readings` | OUT | Nodes send telemetry to hub |
| Actuator node commands | `farm.actuator_commands` | IN | Actuator nodes receive commands |

### RNode Capacity

The RAK4631 RNode is not a bottleneck for device count. The constraint is **airtime**, not the number of connected devices. An RNode can easily serve 50–100 field nodes because:

1. **RNodes support simultaneous BLE and USB connections.** mimi (or hub) connects via USB, while ESP32-C6 nodes connect via BLE. These are independent channels — BLE traffic does not block USB traffic.

2. **RNS Transport handles multiple destinations through a single interface.** When a node sends an announce, the RNode forwards it over LoRa. The hub's `rnsd` learns the path and routes return traffic back through the same RNode. There is no per-device connection limit at the RNS level — it's all packet-switched.

3. **Airtime is the real constraint, and it's generous at SF7/BW125.**

   At SF7/BW125 (~2.4 kbps effective data rate after encoding overhead):

   | Devices | Announce Interval | Airtime per announce | Total airtime/cycle | % of available airtime |
   |---------|-------------------|---------------------|---------------------|----------------------|
   | 10 | 5 min | ~200 ms | ~2 s / 5 min | **~0.7%** |
   | 50 | 5 min | ~200 ms | ~10 s / 5 min | **~3.3%** |
   | 100 | 5 min | ~200 ms | ~20 s / 5 min | **~6.7%** |
   | 10 | 5 min | ~200 ms | ~2 s / 5 min | **~0.7%** |

   Even 50 devices announcing every 5 minutes uses only ~3.3% of available airtime. Telemetry payloads are similarly small (~50–150 bytes), so even with 50 devices sending telemetry every 5 minutes, total LoRa airtime remains well under 10%.

4. **BLE connections are point-to-point and low-latency.** Each ESP32-C6 node establishes its own BLE connection to the RNode. BLE has a practical limit of ~7–8 simultaneous connections, but RNS packets are small and nodes sleep between transmissions, so connection slots are shared efficiently. If more than 7–8 nodes are active simultaneously, the BLEClientInterface handles queuing and reconnection.

**Summary:** An RNode can easily handle 50–100 nodes. The only scaling concern is airtime at very high telemetry frequencies, not device count. For the AgroNomi fleet (10–50 nodes, 5-minute intervals), a single RNode is more than sufficient.

---

## 7. Data Flows

### Telemetry (Node → Hub)

**OLD flow:**
```
ESP32 wakes → read sensors → build JSON → BLE NUS → Pico → serial [JSON] → mimi RNS → Hub DB
```

**NEW flow:**
```
ESP32 wakes → read sensors → build JSON → µR Packet (SINGLE dest) → BLE/WiFi → ESP32-C6 GW → Serial → mimi rnsd → LoRa (RNode) → Hub rnsd → Hub DB
```

Or (future, with E32/SX1262 LoRa module on ESP32-C6):
```
ESP32 wakes → read sensors → build JSON → µR Packet (SINGLE dest) → LoRa (E32/SX1262) → Hub rnsd → Hub DB
```

Payload format (unchanged):
```json
{
  "dev_id": "SN-AIR-01",
  "ts": 12345,
  "fw_ver": "2.0.0",
  "device_type": "air_node",
  "seq": 5,
  "bat_v": "3.12",
  "rns_interface": "ble",
  "readings": {
    "air_temperature_c": 20.6,
    "air_humidity_pct": 51.0
  }
}
```

**Changes from OLD payload:**
- `ble_mac` → **removed** (replaced by RNS identity hash)
- `gateway_id` → **kept** (populated from announce `app_data`)
- `rns_interface` → **added** (tracks which transport carried the packet: `ble`/`wifi`/`serial` — note: `lora` only for future E32/SX1262 modules)

### Commands (Hub → Node)

**OLD flow:**
```
Hub DB → CommandDispatcher → RNS Packet → mimi → Pico serial [CMD] → ESP32
```

**NEW flow:**
```
Hub DB → CommandDispatcher → RNS Packet → rnsd (hub) → LoRa (RNode) → rnsd (mimi) → Serial → ESP32-C6 GW → BLE/WiFi → ESP32 µR node
```

Command JSON (simplified):
```json
{
  "cmd_id": 1,
  "device_id": "AN-PUMP-01",
  "cmd_type": "pump_on",
  "cmd_value": 1.0,
  "ts": 12345
}
```

**Changes from OLD:**
- `ble_mac` → **removed** (RNS identity hash routes to the correct node)
- Command addressed by RNS destination hash, not BLE MAC

### ACK (Node → Hub)

**OLD flow:**
```
ESP32 → BLE NUS → Pico [ACK] → serial → mimi RNS → Hub CommandAckDestination → DB update
```

**NEW flow:**
```
ESP32 µR → RNS Packet (proof/response) → BLE/WiFi → ESP32-C6 GW → Serial → mimi rnsd → LoRa (RNode) → Hub rnsd → Hub CommandAckDestination → DB update
```

---

## 8. Hub Simplification

### What Changes in `reticulum_ingest.py`

The hub daemon (`documents/reticulum_ingest.py`) is dramatically simplified:

#### REMOVE

| Component | Location | Reason |
|-----------|----------|--------|
| `ble_link_log` table references | `log_ble_meta()` (L515-522) | BLE diagnostics no longer relevant |
| `ble_mac` column usage | Throughout `_migrate_schema()`, `record_telemetry()`, `CommandDispatcher` | Replaced by RNS identity hash |
| `ble_target_gateway` column | `hardware_devices` schema | Replaced by RNS announce-based routing |
| BLE-specific DB migration | `_migrate_schema()` (L271-338) | No BLE tables needed |
| OTA via BLE binary protocol | `ota_scheduler.py` references to `ble_mac` | Replaced by RNS Resource transfer |

#### KEEP

| Component | Location | Notes |
|-----------|----------|-------|
| `TelemetryDestination` | L561-667 | Unchanged — receives RNS Packets |
| `CommandAckDestination` | L670-747 | Unchanged — receives ACKs |
| `GatewayAnnounceHandler` | L755-831 | Unchanged — discovers gateways via announce |
| `CommandDispatcher` | L839-1056 | Simplified — no `ble_mac` routing |
| `load_or_create_identity()` | L530-553 | Unchanged |

#### SIMPLIFY

| Component | Change |
|-----------|--------|
| `hardware_devices` table | Remove `ble_mac`, `ble_target_gateway` columns; add `rns_interface` column |
| `CommandDispatcher._send_command()` | Route by RNS destination hash, not BLE MAC |
| `record_telemetry()` | Remove `ble_mac` extraction from payload |
| `_migrate_schema()` | Remove BLE-specific migrations |

#### ADD

| Component | Purpose |
|-----------|---------|
| `rns_interface` column on `hardware_devices` | Track which transport each device uses (`lora`/`ble`/`wifi`) |
| `rns_identity_hash` column on `hardware_devices` | Store RNS identity hash for announce-based routing |
| OTA via RNS Resource transfer | Replace BLE binary protocol with `rncp`/`rngit` (RNS utilities, already available on hub) |

### Simplified `main()` Flow

```python
# BEFORE (simplified):
identity = load_or_create_identity(IDENTITY_PATH)
gateway_handler = GatewayAnnounceHandler()
RNS.Transport.register_announce_handler(gateway_handler)
telem_dest = TelemetryDestination(identity)     # ← KEEP
cmd_ack_dest = CommandAckDestination(identity)   # ← KEEP
dispatcher = CommandDispatcher()                  # ← SIMPLIFIED (no BLE)
ota_thread = start_ota_scheduler()                 # ← REPLACED by rncp/rngit

# AFTER (further simplified):
# No BLE code at all. All communication is RNS-native.
# rnsd runs as a system service providing always-on transport.
# Gateway auto-discovery via announce handler already works.
# OTA uses rncp/rngit standard RNS utilities.
# reticulum_ingest.py connects to local rnsd instance.
```

---

## 9. Database Schema Changes

### Migration: `004_rns_native_migration.sql`

```sql
-- AgroNomi v4.0 — µReticulum-Native Architecture Migration
-- Removes BLE-specific columns, adds RNS-native routing

-- 1. Add RNS routing columns to hardware_devices
ALTER TABLE hardware_devices ADD COLUMN rns_identity_hash TEXT;
ALTER TABLE hardware_devices ADD COLUMN rns_interface TEXT
    CHECK(rns_interface IN ('lora', 'ble', 'wifi', 'serial'))
    DEFAULT 'ble';

-- 2. Populate rns_identity_hash from announce data (backfill when nodes announce)
-- This will be populated dynamically by GatewayAnnounceHandler

-- 3. Remove BLE-specific columns (requires table rebuild in SQLite)
CREATE TABLE IF NOT EXISTS hardware_devices_new (
    device_id TEXT PRIMARY KEY,
    device_type TEXT NOT NULL CHECK(device_type IN (
        'gateway', 'soil_node', 'air_node', 'pump_node',
        'gh_actuator', 'vision_node'
    )),
    node_id TEXT UNIQUE REFERENCES sensor_nodes(node_id) ON DELETE SET NULL,
    field_id TEXT REFERENCES fields(field_id) ON DELETE SET NULL,
    rns_identity_hash TEXT,
    rns_interface TEXT DEFAULT 'ble'
        CHECK(rns_interface IN ('lora', 'ble', 'wifi', 'serial')),
    firmware_version TEXT DEFAULT '0.0.0',
    hardware_revision TEXT,
    battery_type TEXT DEFAULT '18650_liion',
    install_date TEXT,
    last_seen TEXT,
    status TEXT DEFAULT 'active'
        CHECK(status IN ('active', 'offline', 'maintenance', 'decommissioned'))
);

INSERT OR IGNORE INTO hardware_devices_new
    SELECT
        device_id, device_type, node_id, field_id,
        NULL,  -- rns_identity_hash (populated on announce)
        'ble', -- rns_interface default (most nodes are BLE-only in current hardware)
        firmware_version, hardware_revision, battery_type,
        install_date, last_seen, status
    FROM hardware_devices;

DROP TABLE hardware_devices;
ALTER TABLE hardware_devices_new RENAME TO hardware_devices;

CREATE INDEX IF NOT EXISTS idx_hw_rns_hash ON hardware_devices(rns_identity_hash);
CREATE INDEX IF NOT EXISTS idx_hw_rns_iface ON hardware_devices(rns_interface);

-- 4. Drop BLE link log table (no longer relevant)
DROP TABLE IF EXISTS ble_link_log;

-- 5. Update reticulum_gateways to remove BLE-specific columns
-- (gateway_platform constraint expanded to include esp32c6)
-- This also requires a rebuild for CHECK constraints
CREATE TABLE IF NOT EXISTS reticulum_gateways_new (
    gateway_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES hardware_devices(device_id),
    rns_destination_hash TEXT UNIQUE,
    lora_frequency INTEGER DEFAULT 868000000,
    lora_spreading_factor INTEGER DEFAULT 11,
    lora_bandwidth INTEGER DEFAULT 125000,
    lora_coding_rate INTEGER DEFAULT 5,
    lora_tx_power INTEGER DEFAULT 17,
    last_heartbeat TEXT,
    peers_count INTEGER DEFAULT 0,
    mesh_rank INTEGER DEFAULT 0,
    gateway_platform TEXT DEFAULT 'esp32c6'
        CHECK(gateway_platform IN ('rak4631', 'pi_zero_2w', 'esp32c6'))
);

INSERT OR IGNORE INTO reticulum_gateways_new
    SELECT
        gateway_id, device_id, rns_destination_hash,
        lora_frequency, lora_spreading_factor, lora_bandwidth,
        lora_coding_rate, lora_tx_power,
        last_heartbeat, peers_count, mesh_rank,
        gateway_platform
    FROM reticulum_gateways;

DROP TABLE reticulum_gateways;
ALTER TABLE reticulum_gateways_new RENAME TO reticulum_gateways;

-- 6. Add rns_interface to telemetry_ingress for tracking transport
ALTER TABLE telemetry_ingress ADD COLUMN rns_interface TEXT;
```

### Schema Diff Summary

| Table | Column Removed | Column Added |
|-------|---------------|--------------|
| `hardware_devices` | `ble_mac`, `ble_target_gateway` | `rns_identity_hash`, `rns_interface` |
| `ble_link_log` | *(entire table dropped)* | — |
| `reticulum_gateways` | — | `gateway_platform` CHECK expanded to include `esp32c6` |
| `telemetry_ingress` | — | `rns_interface` (values: `lora`/`ble`/`wifi`/`serial`) |

---

## 10. Migration Phases

### Phase 1: LoRa Bridge + Direct BLE to RNode ✅ (COMPLETE)

**Goal**: RNode connected to hub, providing both LoRa transport to hub and BLE access point for field nodes. The ESP32-C6 gateway is optional — only needed for WiFi bridging or BLE relay in extended deployments.

**Minimal deployment**: Hub (Mac mini) + RNode (RAK4631) + ESP32-C6 sensor nodes. Nodes connect directly to RNode over BLE using `BLEClientInterface`.

**Extended deployment**: Add mimi with its own RNode for redundancy, and optionally an ESP32-C6 gateway for WiFi bridging or BLE relay.

| Component | Status | File Reference |
|-----------|--------|----------------|
| µR core engine | ✅ Working | `m_reticulum/esp32c6/firmware/urns/reticulum.py` |
| Transport mode | ✅ Working | `m_reticulum/esp32c6/firmware/urns/transport.py` |
| BLE GATT server (BLEInterface) | ✅ Working | `m_reticulum/esp32c6/firmware/urns/ble_interface.py` |
| BLE GATT client (BLEClientInterface) | ✅ Working | `m_reticulum/esp32c6/firmware/urns/interfaces/ble_client.py` |
| E32 LoRa interface | ✅ Working | `m_reticulum/esp32c6/firmware/urns/interfaces/e32.py` |
| SX1262 LoRa interface | ✅ Working | `m_reticulum/esp32c6/firmware/urns/interfaces/lora.py` |
| UDP interface | ✅ Working | `m_reticulum/esp32c6/firmware/urns/interfaces/udp.py` |
| Gateway config (optional) | ✅ Working | `m_reticulum/esp32c6/firmware/config.py` |
| Gateway main | ✅ Working | `m_reticulum/esp32c6/firmware/main.py` |

**What's already proven:**
- Gateway boots µR, connects WiFi, starts AutoInterface (optional, for extended deployments)
- BLE GATT server advertises and accepts writes (optional, for BLE relay)
- BLEClientInterface connects to RNode over BLE (primary path for minimal deployment)
- Serial interface connects to mimi via USB (optional, for extended deployments)
- rnsd on hub handles LoRa (via RNode) and path forwarding
- Announce-based discovery works
- **RNode BLE**: `rnodeconf --bluetooth-on` enables BLE on RAK4631 RNodes; ESP32-C6 nodes connect directly via `BLEClientInterface`

### Phase 2: Port Sensor Nodes to µR MicroPython (soil, air first)

**Goal**: `SN-SOIL-01` and `SN-AIR-01` run MicroPython + µR, sending telemetry via RNS instead of BLE NUS.

| Task | Details |
|------|---------|
| Create `m_reticulum/sn_soil/` | Device directory with `main.py`, `config.py`, `boot.py` |
| Create `m_reticulum/sn_air/` | Device directory with `main.py`, `config.py`, `boot.py` |
| Port sensor drivers | DS18B20 (OneWire), DHT22, capacitive soil moisture, ADC battery — all in MicroPython |
| Implement telemetry destination | `SINGLE` OUT destination → `farm.telemetry_readings` |
| Implement announce | Periodic announce with `app_data` containing `device_id` and `device_type` |
| Implement deep sleep | Wake every 300s → read sensors → send telemetry → sleep |
| Test LoRa path | Node → BLE → Gateway → Serial → mimi rnsd → LoRa (RNode) → Hub |
| Test BLE path | Node → BLE → Gateway → Serial → mimi rnsd → Hub (via LoRa or WiFi) |
| Deprecate `src/sn_soil/main.cpp` | Arduino C++ firmware no longer needed |
| Deprecate `src/sn_air/main.cpp` | Arduino C++ firmware no longer needed |

### Phase 3: Port Actuator Nodes (pump, greenhouse)

**Goal**: `AN-PUMP-01` and `AN-GREENHOUSE-01` run MicroPython + µR, receiving commands via RNS.

| Task | Details |
|------|---------|
| Create `m_reticulum/an_pump/` | Device directory with `main.py`, `config.py`, `boot.py` |
| Create `m_reticulum/an_greenhouse/` | Device directory with `main.py`, `config.py`, `boot.py` |
| Implement command destination | `SINGLE` IN destination → receives `farm.actuator_commands` |
| Implement actuator drivers | GPIO relay control for pump, vent, fan, shade |
| Implement command callback | Parse JSON command → actuate → send ACK |
| Test command dispatch | Hub `CommandDispatcher` → RNS Packet → Gateway → Node → ACK |
| Deprecate `src/an_pump/main.cpp` | Arduino C++ firmware no longer needed |
| Deprecate `src/an_greenhouse/` | Arduino C++ firmware no longer needed |

### Phase 4: Simplify Hub (remove BLE code)

**Goal**: `reticulum_ingest.py` no longer contains any BLE-specific logic.

| Task | Details |
|------|---------|
| Remove `ble_link_log` references | Delete `log_ble_meta()` function, remove `ble_link_log` table |
| Remove `ble_mac` column usage | Remove from `record_telemetry()`, `CommandDispatcher._send_command()` |
| Remove `ble_target_gateway` column | Remove from `hardware_devices` references |
| Add `rns_interface` column | Track which transport each device uses |
| Add `rns_identity_hash` column | Store RNS identity hash from announce data |
| Update `GatewayAnnounceHandler` | Parse `rns_interface` from announce `app_data` |
| Simplify `CommandDispatcher` | Route by RNS destination hash only |
| Run schema migration | `004_rns_native_migration.sql` |

### Phase 5: Vision Node (Special Case)

**Goal**: Evaluate whether `SN-VIS-GH-01` joins the RNS mesh or stays WiFi-based.

| Task | Details |
|------|---------|
| Evaluate ESP32-CAM µR feasibility | RAM constraints (ESP32-CAM has 4MB PSRAM but limited SRAM) |
| Option A: µR on ESP32-CAM | If feasible, run µR with WiFi interface only |
| Option B: Keep WiFi POST | Current `src/sn_vision/main.cpp` approach — HTTP POST to hub |
| Decision point | Vision node may remain outside RNS mesh due to image payload size |

### Phase 6: OTA via Reticulum Utilities (rncp/rngit)

**Goal**: Replace BLE binary OTA protocol with standard Reticulum utilities (`rncp`/`rngit`).

| Task | Details |
|------|---------|
| Install rncp/rngit on hub | `pip install rns` includes rncp, rngit, rnx, rnstatus |
| Verify rnsd is running on hub and mimi | Both must have `rnsd` running as a system service |
| Implement rncp OTA flow | Hub pushes firmware binary via `rncp` to node destination hash |
| Implement rngit OTA flow (optional) | Host firmware repo via `rngit serve` on hub; nodes pull via `rngit pull` |
| Implement MicroPython OTA flasher | ESP32-C6: write received binary to OTA partition via `esp_ota` API |
| Deprecate BLE OTA | Remove `ble_ota.py` from `bt_bridge/` |
| Test OTA cycle | Full cycle: `rncp` → node receives → validate → flash → reboot → verify |
| Deprecate `documents/ble_ota.py` | Reference document no longer needed |

---

## 11. Firmware Directory Structure

### New Node Directory Layout

Each µR node follows this structure:

```
m_reticulum/
├── esp32c6/                              # Gateway (Phase 1 — COMPLETE)
│   ├── firmware/
│   │   ├── main.py                      # Gateway-specific: transport bridge, announce
│   │   ├── config.py                    # Gateway config: WiFi, LoRa, BLE, transport=ON
│   │   ├── boot.py                      # MicroPython boot
│   │   └── urns/                        # Shared µR stack (symlinked or copied)
│   │       ├── __init__.py
│   │       ├── reticulum.py             # Core engine
│   │       ├── identity.py             # X25519 + Ed25519 identity
│   │       ├── destination.py          # SINGLE/GROUP/PLAIN destinations
│   │       ├── packet.py               # Packet framing, HDR_1/HDR_2
│   │       ├── transport.py            # Blind flood forwarding
│   │       ├── link.py                 # ECDH link establishment
│   │       ├── lxmf.py                # LXMF messaging
│   │       ├── resource.py            # Large resource transfer
│   │       ├── const.py               # Protocol constants
│   │       ├── log.py                  # Logging
│   │       ├── umsgpack.py            # MessagePack serialization
│   │       ├── bz2dec.py             # BZ2 decompression
│   │       ├── ble_interface.py       # BLE GATT server
│   │       ├── crypto/
│   │       │   ├── __init__.py
│   │       │   ├── aes.py              # AES-128-CTR + AES-256-CBC
│   │       │   ├── ed25519.py          # Ed25519 signatures
│   │       │   ├── hashes.py           # SHA-256
│   │       │   ├── hkdf.py             # HKDF key derivation
│   │       │   ├── hmac.py             # HMAC
│   │       │   ├── pkcs7.py            # PKCS7 padding
│   │       │   ├── sha512.py           # SHA-512
│   │       │   ├── token.py            # Token generation
│   │       │   ├── x25519.py           # X25519 ECDH
│   │       │   └── pure25519/
│   │       │       ├── __init__.py
│   │       │       ├── _ed25519.py
│   │       │       ├── basic.py
│   │       │       ├── ed25519_oop.py
│   │       │       └── eddsa.py
│   │       └── interfaces/
│   │           ├── __init__.py
│   │           ├── e32.py               # EBYTE E32 LoRa
│   │           ├── lora.py              # SX1262 LoRa
│   │           ├── serial.py            # USB UART
│   │           ├── tcp.py               # TCP client
│   │           └── udp.py               # UDP (AutoInterface)
│   └── repo/
│       ├── README.md
│       └── firmware/
│           └── config.py               # Mirror of gateway config
│
├── sn_soil/                              # Soil moisture sensor node (Phase 2)
│   └── firmware/
│       ├── main.py                      # Soil sensor logic + RNS telemetry
│       ├── config.py                    # SN-SOIL-01 config: LoRa/BLE pins, sensor pins
│       ├── boot.py                      # MicroPython boot
│       └── urns/                        # → symlink to ../esp32c6/firmware/urns/
│
├── sn_air/                               # Air quality sensor node (Phase 2)
│   └── firmware/
│       ├── main.py                      # DHT22 sensor logic + RNS telemetry
│       ├── config.py                    # SN-AIR-01 config: LoRa/BLE, DHT22 pin
│       ├── boot.py                      # MicroPython boot
│       └── urns/                        # → symlink to ../esp32c6/firmware/urns/
│
├── an_pump/                              # Pump actuator node (Phase 3)
│   └── firmware/
│       ├── main.py                      # Pump relay control + RNS command receiver
│       ├── config.py                    # AN-PUMP-01 config: LoRa/BLE, GPIO pins
│       ├── boot.py                      # MicroPython boot
│       └── urns/                        # → symlink to ../esp32c6/firmware/urns/
│
├── an_greenhouse/                        # Greenhouse actuator node (Phase 3)
│   └── firmware/
│       ├── main.py                      # Vent/fan/shade control + RNS command receiver
│       ├── config.py                    # AN-GREENHOUSE-01 config: LoRa/BLE, GPIO pins
│       ├── boot.py                      # MicroPython boot
│       └── urns/                        # → symlink to ../esp32c6/firmware/urns/
│
└── sn_vision/                            # Vision node (Phase 5 — special case)
    └── firmware/
        ├── main.py                      # Camera capture + WiFi POST or RNS
        ├── config.py                    # SN-VIS-GH-01 config: WiFi, camera pins
        ├── boot.py                      # MicroPython boot
        └── urns/                        # → symlink to ../esp32c6/firmware/urns/
```

### Symlink Strategy

The `urns/` directory in each node's `firmware/` is a **symlink** to `esp32c6/firmware/urns/`. This ensures all nodes run the same µR stack version. When deploying, the symlink is resolved and the stack is copied into the device's filesystem.

```bash
# Creating a new node directory with symlink
mkdir -p m_reticulum/sn_soil/firmware
cd m_reticulum/sn_soil/firmware
ln -s ../../esp32c6/firmware/urns urns
```

### rnsd Service Configuration

Both the hub (Mac mini) and the field gateway (mimi HP/Ubuntu) must run `rnsd` as a system service.
See **[Section 16 — rnsd Service Setup](#16-rnsd-service-setup)** for full configuration details including:
- RNS configuration files for hub and mimi
- Systemd/Launchd service setup
- rnsd and ESP32-C6 gateway relationship
- RNode (RAK4631) hardware details
- `rncp`/`rngit` OTA commands

---

## 12. Per-Device Configuration

Each node's `config.py` specifies device identity, interface selection, and hardware pin mappings. This replaces the PlatformIO `build_flags` system with a clean Python configuration.

### Gateway Config (OPTIONAL — reference, for extended deployment only)

The ESP32-C6 gateway is **optional** in the minimal deployment. See `m_reticulum/esp32c6/firmware/config.py` for the full config.
In the minimal deployment, field nodes connect directly to the RNode via `BLEClientInterface`.

```python
# m_reticulum/esp32c6/firmware/config.py
WIFI_SSID = "FRITZ!Box 5490 ME"
WIFI_PASS = "99141440711753817435"
NODE_NAME = "AgroNomi GW 02"
DEBUG = 2

CONFIG = {
    "loglevel": 3,
    "enable_transport": False,  # Transport handled by mimi's rnsd, not ESP32-C6
    "interfaces": [
        {"type": "AutoInterface", "name": "Auto", "enabled": True},  # WiFi
        {"type": "BLEInterface", "name": "BLE", "enabled": True},     # BLE GATT server for field nodes
        {"type": "SerialInterface", "name": "Serial", "enabled": True, # USB serial to mimi
         "port": "/dev/ttyACM0", "speed": 115200},
    ],
}

SENSOR_HUB = ""  # Discovered via announce
```

> **Note**: The ESP32-C6 gateway does NOT run `rnsd` or handle Transport mode itself.
> It bridges BLE ↔ Serial (to mimi). The `rnsd` instance on mimi handles all
> RNS transport including LoRa (via its RNode). The ESP32-C6 is essentially a
> BLE-to-Serial bridge running µReticulum, not a full RNS transport node.

### Soil Node Config (example — Phase 2)

In the minimal deployment, soil nodes connect directly to the RNode via `BLEClientInterface`. See `m_reticulum/sn_soil/firmware/config.py` for the full config.

```python
# m_reticulum/sn_soil/firmware/config.py
NODE_NAME = "SN-SOIL-01"
DEVICE_TYPE = "soil_node"
DEVICE_ID = "SN-SOIL-01"
FIRMWARE_VERSION = "2.0.0"

# Transport interfaces
# PRIMARY: BLE → RNode via BLEClientInterface (minimal deployment, no gateway needed)
# SECONDARY: BLE → ESP32-C6 gateway (extended deployment only)
# TERTIARY: WiFi (greenhouse with WiFi coverage)
# RNS handles path selection automatically — uses fastest available path.
INTERFACES = {
    "ble_client": {
        "enabled": True,
        "type": "BLEClientInterface",
        "target_name": "RNode-XXXX",  # RNode BLE name (from rnodeconf --bluetooth-on)
    },
    "wifi": {
        "enabled": False,  # Soil nodes are in the field — no WiFi available
    },
    # FUTURE: Requires E32/SX1262 LoRa module hardware to be mounted on ESP32-C6
    # "lora": {
    #     "enabled": True,
    #     "module": "E32",
    #     "tx_pin": 4, "rx_pin": 5,
    #     "m0_pin": 6, "m1_pin": 7, "aux_pin": 8,
    #     "frequency": 868000000,
    #     "spreading_factor": 11,
    #     "bandwidth": 125000,
    #     "coding_rate": 5,
    #     "tx_power": 17,
    # },
}

# Sensor pins
SENSORS = {
    "soil_moisture_pin": 2,       # ADC pin for capacitive sensor
    "ds18b20_pin": 3,             # OneWire data pin
    "battery_adc_pin": 1,         # Voltage divider ADC pin
}

# RNS destination for telemetry
TELEMETRY_ASPECT = "farm.telemetry_readings"

# Deep sleep
ENABLE_DEEPSLEEP = True
SLEEP_INTERVAL_SEC = 300
TELEMETRY_INTERVAL_SEC = 300
```

### Air Node Config (example — Phase 2)

In the minimal deployment, air nodes connect directly to the RNode via `BLEClientInterface`. See `m_reticulum/sn_air/firmware/config.py` for the full config.

```python
# m_reticulum/sn_air/firmware/config.py
NODE_NAME = "SN-AIR-01"
DEVICE_TYPE = "air_node"
DEVICE_ID = "SN-AIR-01"
FIRMWARE_VERSION = "2.0.0"

# Transport interfaces
# PRIMARY: BLE → RNode via BLEClientInterface (minimal deployment, no gateway needed)
# SECONDARY: BLE → ESP32-C6 gateway (extended deployment only)
# TERTIARY: WiFi (greenhouse with WiFi coverage)
INTERFACES = {
    "ble_client": {
        "enabled": True,
        "type": "BLEClientInterface",
        "target_name": "RNode-XXXX",  # RNode BLE name (from rnodeconf --bluetooth-on)
    },
    "wifi": {
        "enabled": False,
    },
    # FUTURE: Requires E32/SX1262 LoRa module hardware
    # "lora": {
    #     "enabled": True,
    #     "module": "E32",
    #     "tx_pin": 4, "rx_pin": 5,
    #     "m0_pin": 6, "m1_pin": 7, "aux_pin": 8,
    #     "frequency": 868000000,
    #     "spreading_factor": 11,
    #     "bandwidth": 125000,
    #     "coding_rate": 5,
    #     "tx_power": 17,
    # },
}

SENSORS = {
    "dht22_pin": 10,              # DHT22 data pin
    "battery_adc_pin": 1,
}

TELEMETRY_ASPECT = "farm.telemetry_readings"
ENABLE_DEEPSLEEP = True
SLEEP_INTERVAL_SEC = 300
TELEMETRY_INTERVAL_SEC = 300
```

### Pump Node Config (example — Phase 3)

In the minimal deployment, pump nodes connect directly to the RNode via `BLEClientInterface`. See `m_reticulum/an_pump/firmware/config.py` for the full config.

```python
# m_reticulum/an_pump/firmware/config.py
NODE_NAME = "AN-PUMP-01"
DEVICE_TYPE = "pump_node"
DEVICE_ID = "AN-PUMP-01"
FIRMWARE_VERSION = "2.0.0"

# Transport interfaces
# PRIMARY: BLE → RNode via BLEClientInterface (minimal deployment, no gateway needed)
# SECONDARY: BLE → ESP32-C6 gateway (extended deployment only)
INTERFACES = {
    "ble_client": {
        "enabled": True,
        "type": "BLEClientInterface",
        "target_name": "RNode-XXXX",  # RNode BLE name (from rnodeconf --bluetooth-on)
    },
    "wifi": {
        "enabled": False,
    },
    # FUTURE: Requires E32/SX1262 LoRa module hardware
    # "lora": {
    #     "enabled": True,
    #     "module": "E32",
    #     "tx_pin": 4, "rx_pin": 5,
    #     "m0_pin": 6, "m1_pin": 7, "aux_pin": 8,
    #     "frequency": 868000000,
    #     "spreading_factor": 11,
    #     "bandwidth": 125000,
    #     "coding_rate": 5,
    #     "tx_power": 17,
    # },
}

# Actuator pins (no sensors)
ACTUATORS = {
    "pump_relay_pin": 9,
}

# RNS destination for receiving commands
COMMAND_ASPECT = "farm.actuator_commands"
COMMAND_ACK_ASPECT = "farm.commands_control"

ENABLE_DEEPSLEEP = False           # Actuators stay awake to receive commands
```

### Greenhouse Actuator Config (example — Phase 3)

In the minimal deployment, greenhouse nodes connect directly to the RNode via `BLEClientInterface`. In greenhouses with WiFi, WiFi can be the primary transport for higher bandwidth. See `m_reticulum/an_greenhouse/firmware/config.py` for the full config.

```python
# m_reticulum/an_greenhouse/firmware/config.py
NODE_NAME = "AN-GREENHOUSE-01"
DEVICE_TYPE = "gh_actuator"
DEVICE_ID = "AN-GREENHOUSE-01"
FIRMWARE_VERSION = "2.0.0"

# Transport interfaces
# PRIMARY: BLE → RNode via BLEClientInterface (minimal deployment, no gateway needed)
# SECONDARY: WiFi (greenhouse — primary when WiFi is available)
# RNS handles path selection automatically — uses fastest available path.
INTERFACES = {
    "ble_client": {
        "enabled": True,
        "type": "BLEClientInterface",
        "target_name": "RNode-XXXX",  # RNode BLE name (from rnodeconf --bluetooth-on)
    },
    "wifi": {
        "enabled": True,            # Greenhouse has WiFi
        "ssid": "GREENHOUSE_WIFI",
        "password": "GREENHOUSE_PASS",
    },
    # FUTURE: Requires E32/SX1262 LoRa module hardware
    # "lora": {
    #     "enabled": True,
    #     "module": "E32",
    #     "tx_pin": 4, "rx_pin": 5,
    #     "m0_pin": 6, "m1_pin": 7, "aux_pin": 8,
    #     "frequency": 868000000,
    #     "spreading_factor": 11,
    #     "bandwidth": 125000,
    #     "coding_rate": 5,
    #     "tx_power": 17,
    # },
}

ACTUATORS = {
    "vent_relay_pin": 9,
    "fan_relay_pin": 10,
    "shade_pwm_pin": 11,
}

COMMAND_ASPECT = "farm.actuator_commands"
COMMAND_ACK_ASPECT = "farm.commands_control"

ENABLE_DEEPSLEEP = False
```

---

## 13. Identity Auto-Provisioning

The µR architecture eliminates all manual key exchange and destination-hash configuration. Nodes generate their own cryptographic identities on first boot, and the hub discovers and registers them automatically via RNS announces.

### RNS Identity Auto-Generation on µR Nodes

Each µR node calls `_find_or_create_identity()` on first boot. This function:

1. Checks persistent storage (`/rns/identity`) for an existing RNS Identity file.
2. If none exists, generates a new RNS Identity and saves it to `/rns/identity`.
3. On subsequent boots, loads the existing identity — no manual key exchange needed.

The identity hash is embedded in all announces and telemetry packets, providing a permanent cryptographic identifier for the node.

### Announce-Based Discovery (No Manual Configuration)

Nodes announce on `farm.gateway_commands` with an `app_data` prefix that encodes their role:

| Node Type | `app_data` Format | Example |
|-----------|-------------------|----------|
| Sensor | `agronomi-sensor:NODE_NAME` | `agronomi-sensor:SN-SOIL-01` |
| Actuator | `agronomi-actuator:NODE_NAME` | `agronomi-actuator:PUMP-01` |
| Gateway | `agronomi-gateway:NODE_NAME` | `agronomi-gateway:GW-02` |

The hub's `GatewayAnnounceHandler` catches all three prefixes and:

- Auto-provisions the `hardware_devices` table with `device_id`, `device_type` (derived from prefix), `rns_identity_hash`, and `rns_destination_hash`.
- Auto-provisions `reticulum_gateways` for gateway-type devices.
- No manual destination-hash copy-pasting needed.

### RNS Identity Hash vs Destination Hash

| Property | Identity Hash | Destination Hash |
|-----------|--------------|------------------|
| Scope | Permanent, created once | Derived per destination |
| Derivation | From the RNS Identity keys | From identity + `app_name` + `aspect` |
| Purpose | Identifies **WHO** the node is | Identifies **WHERE** to send commands |
| Storage | `hardware_devices.rns_identity_hash` | `hardware_devices.rns_destination_hash` |
| Populated by | `GatewayAnnounceHandler` at runtime | `GatewayAnnounceHandler` at runtime |

Both fields are populated automatically when the announce handler processes an incoming announce — no manual configuration required.

### Firmware Config is Transport-Only

All firmware `config.py` files contain **only** transport configuration (which interfaces to enable). Specifically:

- **No** identity hashes, destination hashes, or MAC addresses.
- `RNS_ANNOUNCE_PREFIX` and `NODE_NAME` are the only node-specific values.
- WiFi credentials are left empty — the deployer fills them in per-deployment and never commits them.
- `HUB_ANNOUNCE_FILTER` is prepared for future hub verification.

This design ensures that firmware images are identical across nodes of the same type, differing only in `NODE_NAME` and WiFi credentials.

### Database Auto-Provisioning

`provision_nodes.py` seeds baseline data (device IDs, types, locations) into `hardware_devices`. It does **not** seed identity hashes or destination hashes — those are populated at runtime by `GatewayAnnounceHandler` when nodes announce.

| Node Type | `rns_interface` Default |
|-----------|------------------------|
| Sensor / Actuator | `'ble'` |
| Gateway (RNode) | `'lora'` |

### rnid Utility for Manual Operations

`rnid` is a RNS identity management utility available for debugging or pre-generating identities:

| Command | Purpose |
|---------|---------|
| `rnid -g ./new_identity` | Generate a new identity |
| `rnid -i ./identity -p` | Display identity key info |

In normal operation, nodes auto-generate their own identities on first boot — `rnid` is only needed for debugging or pre-provisioning.

### Command Dispatch Flow

1. Hub polls `actuator_commands` for pending rows.
2. Looks up `rns_destination_hash` directly from `hardware_devices` (not through `reticulum_gateways`).
3. Works for **all** node types (sensors, actuators, gateways).
4. Sends command to the node's single destination on `farm.gateway_commands`.

---

## 14. BLE as a RNS Interface

### How It Works

BLE in the µR architecture serves two roles:

**Primary (minimal deployment):** ESP32-C6 sensor/actuator nodes connect **directly** to the RAK4631 RNode over BLE using `RNodeBLEInterface`. This uses the same KISS-over-Nordic-UART-Service protocol that the official RNS `RNodeInterface` uses over USB serial — just transported over BLE instead. The RNode bridges BLE ↔ LoRa, providing end-to-end connectivity without any gateway. This is the recommended path.

**Secondary (extended deployment):** The `BLEInterface` (`urns/ble_interface.py`) operates as a **BLE GATT server** on the ESP32-C6 gateway. Sensor nodes connect as BLE centrals and write RNS packets to the GATT characteristic. The gateway then forwards these packets via its Serial interface to mimi, where `rnsd` handles LoRa transport via the RNode. This is only needed for WiFi bridging or BLE relay.

```
PRIMARY (minimal deployment — no gateway needed):
┌──────────────┐  KISS/NUS BLE ┌──────────────┐  LoRa (RNode)  ┌──────────────┐
│  ESP32-C6    │──────────────►│  RAK4631     │──────────────►│  Hub rnsd     │
│  Sensor Node │  RNS Packet   │  RNode        │  RNode LoRa    │              │
│  µReticulum  │               │  (BLE+LoRa)  │               │  Telemetry-   │
│  RNodeBLEInterface          │              │               │  Destination  │
└──────────────┘               └──────────────┘               └──────────────┘

SECONDARY (extended deployment — via ESP32-C6 gateway):
┌──────────────┐  BLE GATT Write  ┌──────────────┐  Serial/USB  ┌──────────────┐  LoRa (RNode)  ┌──────────────┐
│  ESP32-C6    │─────────────────►│  ESP32-C6 GW │─────────────►│  mimi rnsd    │───────────────►│  Hub rnsd     │
│  Sensor Node │  RNS Packet      │  (BLE→Serial  │              │  (RNode USB)  │  RNode LoRa    │              │
│  µReticulum   │                  │   bridge)     │              │               │               │  Telemetry-   │
│  BLEClient   │                  │  µReticulum   │              │               │               │  Destination  │
└──────────────┘                  └──────────────┘              └──────────────┘               └──────────────┘
```

### BLE Interface Details

The `BLEInterface` class in `urns/ble_interface.py`:

- **Service UUID**: `12345678-1234-5678-1234-56789abcdef0` (configurable)
- **Characteristic UUID**: `12345678-1234-5678-1234-56789abcdef1` (configurable)
- **Properties**: `WRITE_NO_RESPONSE` — nodes write RNS packets, gateway reads them
- **Advertising**: Gateway advertises as `BLEGateway` with service UUID
- **Packet handler**: Received bytes are passed to `packet_handler(data, None)` — the same handler pattern used by all µR interfaces

### RNode BLE Capability

RAK4631 RNodes running standard RNode firmware support BLE connections (enabled with
`rnodeconf --bluetooth-on`). This is the **primary connection method** for field nodes in
the minimal deployment — ESP32-C6 nodes connect directly to the RNode over BLE using
`RNodeBLEInterface`, eliminating the need for a separate gateway.

```
ESP32-C6 µR node → KISS/NUS BLE → RAK4631 RNode → LoRa → Hub RNode → Hub rnsd
```

An ESP32-C6 node connects **directly** to an RNode over BLE, bypassing the
ESP32-C6 gateway entirely. This uses `RNodeBLEInterface` (`urns/interfaces/rnode_ble.py`),
which implements the same KISS-over-NUS protocol that the official RNS `RNodeInterface`
uses over USB serial.

**Full RNS on mimi/hub** also supports BLE connections to RNodes via the
`RNodeInterface` with `port = ble://RNode-XXXX` or `port = ble://` (first available
paired device). This means an RNode in the field can serve as a **shared LoRa radio**:

```
                         ┌─────────────────────────────────────┐
                         │          RAK4631 RNode              │
                         │    (BLE + USB + LoRa simultaneously) │
                         │                                      │
                         │  USB ──── mimi (rnsd, RNodeInterface)│
                         │  BLE ──── ESP32-C6 nodes (RNodeBLEInterface)│
                         │  LoRa ── Hub RNode ── Hub rnsd       │
                         └─────────────────────────────────────┘
```

**Key points:**
- **`rnodeconf --bluetooth-on`** enables BLE on the RNode; it advertises as `RNode-XXXX`
- **Full RNS** uses `RNodeInterface` with `port = ble://RNode-XXXX` or `port = ble://`
- **µR nodes** use `RNodeBLEInterface` to connect via KISS-over-NUS — same protocol as USB serial
- **Reticulum handles path selection automatically** — if a node has both BLE-to-RNode
  and WiFi interfaces, it will route via the fastest path
- The RNode can serve both mimi (USB) and ESP32-C6 nodes (BLE) **simultaneously**
- USB takes priority for LoRa traffic; BLE traffic is queued and interleaved

### RNodeBLEInterface — KISS over BLE NUS (PRIMARY)

The `RNodeBLEInterface` (`urns/interfaces/rnode_ble.py`) is the **primary BLE interface** for field nodes. It implements the exact same KISS-over-Nordic-UART-Service protocol that the official RNS `RNodeInterface` uses over USB serial — just transported over BLE instead.

**How it works:**

1. Scans for BLE peripherals advertising the NUS service UUID (`6E400001-B5A3-F393-E0A9-E50E24DCCA9E`) or named `RNode *`
2. Connects to the RNode's GATT server and discovers the NUS RX/TX characteristics
3. Subscribes to TX characteristic notifications (incoming data from RNode)
4. Sends KISS detection handshake: `DETECT_REQ` (0x73) → expects `DETECT_ACK` (0x46)
5. Configures radio parameters: frequency, bandwidth, TX power, spreading factor, coding rate
6. Sets radio state to ON
7. Sends/receives RNS packets as KISS DATA frames over the NUS UART

**Key configuration (`config.py`):**

```python
{
    "type": "RNodeBLEInterface",
    "name": "RNode BLE",
    "target_name": "",  # Auto-discover any RNode
    "frequency": 868000000,
    "bandwidth": 125000,
    "txpower": 17,
    "spreadingfactor": 11,  # Must match hub RNode
    "codingrate": 5,
    "enabled": True,
}
```

**This is NOT a custom protocol** — it's a MicroPython port of the official `RNodeInterface` BLE connection path (`BLEConnection` class in the official Reticulum repo). The KISS framing, detection handshake, and radio configuration are identical.

### BLEClientInterface — BLE GATT Client (SECONDARY — Extended Deployment Only)

The `BLEClientInterface` (`urns/interfaces/ble_client.py`) is the **secondary** BLE interface for
extended deployments. Where `BLEInterface` runs as a GATT **server** on the gateway
(accepting connections), `BLEClientInterface` runs as a GATT **client** on
sensor/actuator nodes (initiating connections).

**Interface comparison:**

| Interface | Role | Runs on | Connects to | Priority |
|-----------|------|---------|-------------|----------|
| `RNodeBLEInterface` | KISS/NUS client | ESP32-C6 sensor/actuator nodes | RNode (NUS service) | **Primary** for minimal deployment |
| `BLEClientInterface` (client) | GATT client | ESP32-C6 sensor/actuator nodes | ESP32-C6 gateway | **Secondary** — extended deployment only |
| `BLEInterface` (server) | GATT server | ESP32-C6 gateway | Accepts connections from µR nodes | **Secondary** — extended deployment only |

**How BLEClientInterface works:**

1. Scans for BLE peripherals advertising the RNS service UUID
2. Connects to the matching device (gateway)
3. Discovers the RNS GATT characteristic
4. Enables notifications (to receive data FROM the server)
5. Writes RNS packets to the characteristic (write without response)
6. Receives data back via BLE notifications

**Key configuration (`config.py`):**

```python
{
    "type": "BLEClientInterface",
    "name": "BLE to Gateway",
    "target_name": "BLEGateway",
    "enabled": True,
}
```

**Use cases:**
- **Secondary**: Connect to the ESP32-C6 gateway's BLEInterface as a fallback when the RNode is out of BLE range
- Only needed for WiFi bridging or BLE relay scenarios

**Fragmentation:** BLE limits payloads to ~244 bytes on ESP32-C6. RNS packets exceeding
this are fragmented using a 2-byte big-endian length-prefix protocol:
`[2-byte length][data chunk][2-byte length][data chunk]...`

**Auto-reconnection:** Both BLE interfaces handle disconnections gracefully — they
automatically re-scan and reconnect with configurable delay (`reconnect_delay`).

### Key Differences from OLD BLE NUS

| Aspect | OLD (BLE NUS) | NEW (µR RNodeBLEInterface) |
|--------|---------------|------------------------|
| **Protocol** | Nordic UART Service (NUS) | KISS over NUS (same as USB serial RNodeInterface) |
| **Encryption** | None (cleartext JSON) | RNS identity-based encryption |
| **Framing** | Line-delimited text (`[JSON]`, `[CMD]`) | KISS framing (FEND/escapes) → RNS packets |
| **Discovery** | BLE name scan, hardcoded MAC | BLE NUS service UUID scan + KISS detection handshake |
| **OTA** | Custom binary protocol (8-step) | RNS Resource transfer (rncp/rngit) |
| **Routing** | Static (`ble_mac` → `ble_target_gateway`) | RNS path routing via rnsd (automatic) |
| **Radio config** | N/A (gateway handled LoRa) | KISS commands: freq, BW, SF, CR, TX power |
| **Detection** | N/A (BLE connection = ready) | KISS DETECT_REQ (0x73) → DETECT_ACK (0x46) |

### BLE-Only Node Considerations

Nodes using only BLE (no LoRa radio on the ESP32-C6) have two connection paths:

**Path 1: Direct to RNode (RECOMMENDED — RNodeBLEInterface, KISS/NUS pattern)**
1. **Scan for the RNode's BLE advertisement** (NUS service UUID or name starting with `RNode `)
2. **Connect via RNodeBLEInterface** — KISS detection handshake, radio config, then data frames
3. **RNS packets flow as KISS DATA frames** directly to the RNode — no gateway needed
4. **Reticulum handles path selection** — if the node also has WiFi, it uses whichever path is fastest
5. This is the **primary path** in the minimal deployment (Hub + RNode + sensor nodes, no gateway)

**Path 2: Via ESP32-C6 gateway (BLEClientInterface, GATT client pattern — extended deployment only)**
1. **Scan for the gateway's BLE advertisement** (custom service UUID)
2. **Connect and write RNS packets** to the GATT characteristic
3. **Rely on the gateway's Serial connection** to mimi, which forwards packets via `rnsd` and its RNode to the hub
4. **Periodically re-announce** their own destination so the hub can route return traffic through mimi → Serial → gateway → BLE
5. Only needed when the RNode can't reach all nodes directly via BLE

> **Note**: All ESP32-C6 nodes in the current hardware setup are BLE-only (no LoRa module mounted).
> LoRa connectivity is provided by RAK4631 RNodes connected to the hub (and optionally mimi) via USB,
> or directly via RNodeBLEInterface to an RNode in the field. The minimal deployment requires
> no gateway — just Hub + RNode + sensor nodes.

---

## 15. OTA via Reticulum Utilities (rncp / rngit)

Instead of a custom BLE binary OTA protocol, the fleet can now use standard Reticulum tools
for firmware distribution. This is **MUCH simpler** than the old 8-step BLE OTA protocol.

### Available RNS OTA Tools

| Tool | Purpose | Usage |
|------|---------|-------|
| **`rncp`** | Transfer files over RNS to any node | `rncp firmware.bin <destination_hash>` |
| **`rngit`** | Host firmware as a git repo over RNS | `rngit serve` on hub, nodes `rngit pull` |
| **`rnx` / `rnsh`** | Remote command execution | Trigger update scripts on nodes |
| **`rnstatus`** | Monitor RNS transport status | Verify link before OTA |

### OLD OTA Process (8 steps, BLE-dependent)

```
1. Hub ota_scheduler queues ota_request command
2. CommandDispatcher marks command 'transferring'
3. Hub establishes RNS Link to gateway
4. Hub sends firmware binary via RNS Resource (~1.4MB, 50-90 min over LoRa)
5. Gateway receives binary, saves to fw_cache
6. Gateway matches command to cached binary
7. Gateway ble_ota.py connects to ESP32 via BLE MAC, flashes in 241-byte NUS chunks
8. ESP32 validates SHA-256, writes OTA partition, reboots
```

### NEW OTA Process (RNS Utilities-based)

**Method 1: `rncp` — Direct file push**

```
1. Build new MicroPython firmware image on development machine
2. Use `rncp` to push firmware to the node over RNS:
   rncp /path/to/soil_node-v2.1.0.bin <node_destination_hash>
3. Node receives firmware binary, validates SHA-256, flashes OTA partition, reboots
```

**Method 2: `rngit` — Git repository over RNS**

```
1. Build new MicroPython firmware image
2. Commit and push to firmware git repository (hosted via rngit on hub)
3. Nodes periodically pull updates via `rngit pull` or hub triggers update via `rnx`
4. Node validates SHA-256, flashes OTA partition, reboots
```

**Method 3: Hub-initiated via `reticulum_ingest.py`**

```python
# Hub schedules OTA, pushes firmware binary via rncp to node or gateway
import subprocess

# Push firmware to a specific node (via gateway if indirect)
subprocess.run(["rncp", "/var/agronomi/fw/soil_node-v2.1.0.bin", node_destination_hash])

# Or use rnx to trigger update on the node
subprocess.run(["rnx", node_destination_hash, "update", "--version", "2.1.0"])
```

### OTA Firmware File Structure

```
/var/agronomi/fw/
├── soil_node-v2.1.0.bin        # ESP32-C6 MicroPython firmware image
├── air_node-v2.1.0.bin
├── pump_node-v2.1.0.bin
├── gh_actuator-v2.1.0.bin
└── checksums.sha256             # SHA-256 checksums for verification
```

### OTA Command Flow (NEW — via rncp)

```python
# Hub sends OTA request via RNS Packet to node
ota_command = {
    "cmd_type": "ota_request",
    "device_id": "SN-SOIL-01",
    "fw_version": "2.1.0",
    "fw_size": 1400000,
    "sha256": "abc123...",
    "rns_interface": "ble"  # Transport that will carry the firmware
}

# Node receives command, acknowledges, and starts listening for rncp transfer
# Hub then pushes firmware binary:
#   rncp /var/agronomi/fw/soil_node-v2.1.0.bin <node_hash>
#
# Node receives binary, validates SHA-256, flashes OTA partition, reboots
```

### OTA via rngit (Git Repository over RNS)

```bash
# On hub: initialize firmware repository
mkdir -p /var/agronomi/fw/repo
cd /var/agronomi/fw/repo
git init
cp /var/agronomi/fw/soil_node-v2.1.0.bin .
git add .
git commit -m "soil_node v2.1.0"
rngit serve &  # Host repo over RNS

# On node: pull updates
rngit pull <hub_hash>  # Pull latest firmware
# Or trigger remotely:
rnx <node_hash> update --pull-latest
```

### Comparison: OLD vs NEW OTA

| Aspect | OLD (BLE OTA) | NEW (rncp/rngit) |
|--------|---------------|-------------------|
| **Steps** | 8 steps | 3 steps |
| **Protocol** | Custom BLE binary | Standard RNS tools |
| **Transfer** | BLE NUS chunks (241 bytes) | rncp (RNS Resource) or rngit (git) |
| **Verification** | SHA-256 in step 8 | SHA-256 before flash |
| **Gateway dependency** | Pico 2W must forward BLE chunks | Gateway forwards RNS packets (or direct) |
| **Hub dependency** | ota_scheduler.py custom code | rncp/rngit standard tools |
| **Monitoring** | Custom logging | rnstatus, rncp progress |
| **Error recovery** | Manual intervention | rncp retries, rngit re-pull |

---

## 16. Decommissioned Components

The following components become **obsolete** after the migration is complete:

### Firmware (Arduino/C++ → MicroPython)

| Old Component | File | Replacement |
|---------------|------|-------------|
| BLE client | `lib/FleetBLE/BLEManager.cpp` | `urns/ble_interface.py` (µR native) |
| Telemetry builder | `lib/FleetCommon/Telemetry.cpp` | Device `main.py` (µR Packet) |
| OTA receiver | `lib/FleetOTA/OTAManager.cpp` | `rncp` / `rngit` (RNS utilities) |
| Soil node (Arduino) | `src/sn_soil/main.cpp` | `m_reticulum/sn_soil/firmware/main.py` |
| Air node (Arduino) | `src/sn_air/main.cpp` | `m_reticulum/sn_air/firmware/main.py` |
| Vision node (Arduino) | `src/sn_vision/main.cpp` | `m_reticulum/sn_vision/firmware/main.py` (or keep WiFi POST) |

### Gateway Bridge (Pico 2W → ESP32-C6 µR) — DECOMMISSIONED

The Pico 2W (`bt_bridge/`) has been replaced by the ESP32-C6 gateway running µReticulum.
Note: The RAK4631 RNodes are **NOT** decommissioned — they continue as the LoRa interface
for both the hub (Mac mini) and the field gateway computer (mimi HP/Ubuntu).

| Old Component | File | Replacement |
|---------------|------|-------------|
| BLE GATT server | `bt_bridge/main.py` | `m_reticulum/esp32c6/firmware/main.py` |
| RNS forwarder | `bt_bridge/ble_forwarder.py` | `m_reticulum/esp32c6/firmware/urns/transport.py` |
| BLE OTA relay | `bt_bridge/ble_ota.py` | `rncp` / `rngit` (RNS utilities) |
| Firmware cache | `bt_bridge/fw_cache.py` | `rncp` / `rngit` (no local cache needed) |
| Gateway config | `bt_bridge/ble_forwarder.toml` | `m_reticulum/esp32c6/firmware/config.py` |
| Pico boot | `bt_bridge/boot.py` | ESP32-C6 MicroPython boot |

### Hub (Simplified)

| Old Component | Location | Change |
|---------------|----------|--------|
| `log_ble_meta()` | `reticulum_ingest.py` L515-522 | **REMOVE** |
| `ble_link_log` table | `003_hardware_fleet_migration.sql` | **DROP TABLE** |
| `ble_mac` column | `hardware_devices` table | **REMOVE** |
| `ble_target_gateway` column | `hardware_devices` table | **REMOVE** |
| BLE OTA protocol | `documents/ble_ota.py` | **DEPRECATE** — replaced by `rncp` / `rngit` |
| OTA via BLE | `ota_scheduler.py` BLE references | **REPLACE** with `rncp` / `rngit` calls |
| Custom OTA scheduler | `ota_scheduler.py` custom code | **SIMPLIFY** — use `rncp` as standard tool |

### Build System (PlatformIO → MicroPython)

| Old | New |
|-----|-----|
| `platformio.ini` build flags | `config.py` per device |
| `src/<device>/main.cpp` | `m_reticulum/<device>/firmware/main.py` |
| `lib/FleetBLE/` | `m_reticulum/esp32c6/firmware/urns/ble_interface.py` |
| `lib/FleetCommon/` | Inline in device `main.py` |
| `lib/FleetOTA/` | `m_reticulum/esp32c6/firmware/urns/resource.py` |
| `post_build.py` | Not needed (MicroPython flash via `mpflash` or `rshell`) |

---

## 17. rnsd Service Setup

Both the hub (Mac mini) and the field gateway computer (mimi HP/Ubuntu) must run `rnsd` as a
system service. This is a critical infrastructure component — without `rnsd`, there is no RNS
transport between the LoRa links and the rest of the network.

### Why rnsd?

- **Always-on RNS transport**: RNS packets are forwarded between interfaces (RNode, Serial, TCP) by the `rnsd` daemon
- **Shared instance**: Both `reticulum_ingest.py` and other programs (rncp, rngit, rnstatus) connect to the same `rnsd` instance
- **OTA availability**: `rncp` and `rngit` require a running `rnsd` instance to transfer firmware
- **Monitoring**: `rnstatus` provides real-time transport status
- **Path management**: `rnsd` maintains path tables and announce caches automatically

### Hub (Mac mini) — RNS Configuration

**File**: `~/.reticulum/config` (or `/etc/reticulum/config`)

```toml
# Hub RNS Configuration
# RNode connected via USB for LoRa transport

[[interfaces]]
  type = RNodeInterface
  name = RNodeHub
  port = /dev/ttyUSB0    # Adjust to actual RNode USB device
  frequency = 868000000
  bandwidth = 125000
  spreading_factor = 11
  coding_rate = 5
  tx_power = 17

# Optional: TCP server for local programs (reticulum_ingest.py, rncp, etc.)
[[interfaces]]
  type = TCPServerInterface
  name = TCPServer
  listen_ip = 127.0.0.1
  listen_port = 4242
```

**Launchd plist** (macOS): `~/Library/LaunchAgents/org.reticulum.rnsd.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>org.reticulum.rnsd</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/rnsd</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/rnsd.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/rnsd_err.log</string>
</dict>
</plist>
```

```bash
# Install and start rnsd on macOS
launchctl load ~/Library/LaunchAgents/org.reticulum.rnsd.plist
launchctl start org.reticulum.rnsd

# Monitor
rnstatus
```

### Field Gateway (mimi HP/Ubuntu) — RNS Configuration (OPTIONAL — Extended Deployment Only)

This configuration is only needed for the **extended deployment** with a field gateway computer (mimi).
In the minimal deployment, only the Hub needs `rnsd` — field nodes connect directly to the RNode via BLE.

**File**: `~/.reticulum/config` (or `/etc/reticulum/config`)

```toml
# Field Gateway RNS Configuration
# RNode connected via USB for LoRa transport
# ESP32-C6 gateway connected via USB/Serial for BLE bridge

[[interfaces]]
  type = RNodeInterface
  name = RNodeField
  port = /dev/ttyUSB1    # Adjust to actual RNode USB device
  frequency = 868000000
  bandwidth = 125000
  spreading_factor = 11
  coding_rate = 5
  tx_power = 17

# ESP32-C6 gateway serial interface
[[interfaces]]
  type = SerialInterface
  name = ESP32Gateway
  port = /dev/ttyACM0     # Adjust to ESP32-C6 USB device
  speed = 115200

# Optional: TCP server for local programs
[[interfaces]]
  type = TCPServerInterface
  name = TCPServer
  listen_ip = 127.0.0.1
  listen_port = 4242
```

**Systemd service** (Linux): `/etc/systemd/system/rnsd.service`

```ini
[Unit]
Description=Reticulum Network Stack Daemon
After=network.target

[Service]
Type=simple
User=agronomi
ExecStart=/usr/local/bin/rnsd
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# Install and start rnsd on Linux
sudo systemctl enable rnsd
sudo systemctl start rnsd
sudo systemctl status rnsd

# Monitor
rnstatus

# Transfer firmware via rncp
rncp /path/to/firmware.bin <destination_hash>

# Host firmware repo via rngit
cd /path/to/firmware/repo && rngit serve
```

### rnsd and ESP32-C6 Gateway Relationship

The ESP32-C6 gateway is **optional** and only used in the extended deployment. In the minimal deployment, field nodes connect directly to the RNode via `BLEClientInterface`.

When the ESP32-C6 gateway IS used (extended deployment):

1. **ESP32-C6 gateway** runs µReticulum with BLE GATT server + Serial interface
2. **mimi's rnsd** sees the ESP32-C6 as a Serial interface and routes packets to/from it
3. **rnsd** on mimi also has the RNode LoRa interface, bridging Serial ↔ LoRa
4. The ESP32-C6 does **NOT** run `rnsd` or handle Transport mode — it's a BLE-to-Serial bridge
5. All RNS transport (path management, announce forwarding, etc.) is handled by `rnsd` on mimi

In the minimal deployment:
1. **Field nodes** connect directly to the RNode via `BLEClientInterface`
2. **Hub's rnsd** handles all RNS transport including LoRa (via its RNode) and path forwarding
3. No mimi, no ESP32-C6 gateway, no serial bridge needed

### RNode Hardware (RAK4631)

The RAK4631 RNodes continue to serve as the LoRa interface for both the hub and mimi. They
are **NOT** decommissioned. Key details:

- **Firmware**: Standard RNode firmware (nRF52 + SX1276/SX1278)
- **Connection**: USB serial (`/dev/ttyUSB0` on hub, `/dev/ttyUSB1` on mimi)
- **Frequency**: 868 MHz (EU868 band)
- **Spreading Factor**: SF11
- **Bandwidth**: 125 kHz
- **BLE capability**: RAK4631 RNodes support BLE (enabled with `rnodeconf --bluetooth-on`). ESP32-C6 µR nodes can connect directly to an RNode over BLE using `BLEClientInterface`, bypassing the ESP32-C6 gateway. Full RNS on mimi/hub can also connect via `RNodeInterface` with `port = ble://`. The RNode can be shared between mimi (USB) and ESP32-C6 nodes (BLE) simultaneously.

---

## 18. Risk Assessment

### HIGH RISK

| Risk | Impact | Mitigation |
|------|--------|-------------|
| **MicroPython memory on ESP32-C6** | µR stack + crypto + sensor drivers may exceed RAM | Profile memory with `gc.mem_free()`; consider frozen bytecode; strip unused interfaces |
| **LoRa bandwidth** | RNS overhead (headers, encryption) reduces effective throughput; LoRa is via RNodes on computers, not on ESP32-C6 | Use SF11/BW125 for range; keep payloads under MDU (464 bytes); batch readings |
| **Deep sleep + RNS state** | Node identity and announce state lost on deep sleep | Store identity to flash; re-announce on wake; cache path tables |
| **rnsd single point of failure** | If `rnsd` crashes on hub or mimi, all RNS transport stops (including LoRa) | Run `rnsd` as systemd/launchd service with automatic restart; monitor with `rnstatus` |

### MEDIUM RISK

| Risk | Impact | Mitigation |
|------|--------|-------------|
| **OTA reliability over LoRa** | 1.4MB firmware over LoRa at ~250bps = ~90 min; prefer rncp over WiFi when available | Use `rncp` over WiFi where available; keep LoRa OTA as fallback; use `rngit` for incremental updates |
| **BLE throughput** | BLE GATT write-no-response is slower than NUS | Benchmark BLE interface; use notification for node-to-gateway if needed |
| **ESP32-C6 gateway SPOF** | If the ESP32-C6 gateway crashes, all BLE-only nodes are offline | Deploy multiple ESP32-C6 gateways; consider RNode BLE path as alternative; restart watchdog |
| **USB connectivity** | ESP32-C6 gateway connects to mimi via USB; cable issues can disrupt Serial interface | Use quality USB cables; monitor Serial interface health; auto-reconnect logic in µR |

### LOW RISK

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Vision node integration** | ESP32-CAM has limited SRAM for µR | Keep WiFi POST path as fallback; evaluate µR feasibility separately |
| **Config migration** | Moving from PlatformIO build flags to Python config | Mechanical change; no logic complexity |
| **Testing coverage** | Need to validate all transport paths | Phase-by-phase testing; each phase has clear acceptance criteria |

### Rollback Strategy

Each migration phase has a **rollback window** where the old system can be restored:

| Phase | Rollback | Window |
|-------|----------|--------|
| Phase 1 | Revert to Pico 2W + RNode | Already running in parallel |
| Phase 2 | Flash Arduino firmware back to sensor nodes | Until Phase 4 removes hub BLE code |
| Phase 3 | Flash Arduino firmware back to actuator nodes | Until Phase 4 removes hub BLE code |
| Phase 4 | Re-add BLE columns and code to hub | Before Phase 6 removes OTA over BLE |
| Phase 5 | Keep WiFi POST for vision node | Indefinite — vision may never join RNS |
| Phase 6 | Keep BLE OTA as fallback, or use rncp over WiFi | After rncp/rngit OTA is proven stable |

---

*Document maintained alongside `documents/fleet_architecture.md` (OLD) and `documents/reticulum_ingest.py` (hub daemon).*