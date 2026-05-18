
# Hardware Peripherals Fleet Architecture
## AgroNomi — Reticulum Sensor Network

**Version:** 2.0  
**Date:** 2026-05-15  
**Status:** Phase 0 — Reticulum Connectivity Testing

---

## 1. Overview

This document defines the current hardware architecture for the AgroNomi sensor network, reflecting the actual devices on hand and the immediate goal: **verify that the Reticulum LoRa transport layer works reliably before adding sensor complexity.**

### Current hardware

| Device | Role | Connected to |
|--------|------|-------------|
| RAK4631 #1 (RNode CE) | LoRa gateway | Mac Mini (farm-pod) via USB |
| RAK4631 #2 (RNode CE) | LoRa gateway | Old Linux laptop via USB |
| sn_soil | Soil sensor node (ESP32-C6) | ready, not yet connected |
| sn_air | Air sensor node (ESP32-C6) | ready, not yet connected |
| sn_vision | Camera vision node (ESP32-CAM) | ready, not yet connected |

### Deferred hardware

**Raspberry Pi Zero 2 W** — sold out; will serve as production field gateway in a future phase. The Linux laptop acts as a functional stand-in for now.

---

## 2. Phase 0 — Reticulum Connectivity Test

**Goal:** Confirm bidirectional Reticulum communication over LoRa between the two RNodes before introducing any sensor nodes.

```
┌──────────────────────────────────┐         LoRa RF (EU868)        ┌──────────────────────────────┐
│   Mac Mini M2 (farm-pod)         │◄──────────────────────────────►│  Linux Laptop                │
│                                  │      RAK4631 #1 ↔ RAK4631 #2   │                              │
│  Reticulum (Python/RNS)          │                                 │  Reticulum (Python/RNS)      │
│  enable_transport = True         │                                 │  enable_transport = True     │
│                                  │                                 │                              │
│  rnsd / rnpath / rnprobe         │                                 │  rnsd / rnpath / rnprobe     │
│                                  │                                 │                              │
│  RAK4631 #1                      │                                 │  RAK4631 #2                  │
│  USB → /dev/tty.usbmodem...      │                                 │  USB → /dev/ttyACM0          │
└──────────────────────────────────┘                                 └──────────────────────────────┘
```

### Success criteria for Phase 0

- `rnprobe` succeeds in both directions
- `rnpath` resolves the remote identity
- Packet loss < 5% in a 30-minute soak test at the intended deployment distance
- No duty-cycle violations (EU868 1% limit respected by RNode CE firmware)

---

## 3. Reticulum Configuration

### 3.1 Mac Mini (farm-pod) — `~/.reticulum/config`

```ini
[reticulum]
enable_transport = True
share_instance = Yes

[RNode Pod]
type = RNodeInterface
port = /dev/tty.usbmodem<RAK_SERIAL>   # fill in actual port, e.g. ls /dev/tty.usbmodem*
frequency = 868000000
bandwidth = 125000
spreadingfactor = 11
codingrate = 5
txpower = 17

[debug]
log_level = 4
```

Find the port with:
```bash
ls /dev/tty.usbmodem* /dev/tty.SLAB* 2>/dev/null
# or
python3 -m serial.tools.list_ports
```

### 3.2 Linux Laptop — `~/.reticulum/config`

```ini
[reticulum]
enable_transport = True
share_instance = Yes

[[RNode AgroNomi]]
type = RNodeInterface
enabled = yes
port = /dev/cu.usbmodem23101
frequency = 868125000
bandwidth = 250000
spreadingfactor = 11
codingrate = 5
txpower = 14

[debug]
log_level = 4
```

### 3.3 LoRa parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Frequency | 868.125 MHz | EU868 band, standard for Slovenia |
| Bandwidth | 250 kHz | Higher bandwidth for lower airtime and better throughput |
| Spreading factor | SF11 | ~2–3 km mixed terrain range, adequate for field deployment |
| Coding rate | 5 | Standard |
| TX power | 14 dBm | Reduced power for Phase 0 testing (lower heat, less duty-cycle consumption) |
| Airtime per 500B packet (SF11) | ~7.4 s | |
| Max packets/hour at 1% DC | ~5 | Sufficient for 15-min sensor reporting cycles |

---

## 4. Phase 0 Test Procedure

### Install Reticulum on both machines

```bash
pip install rns          # or: pip3 install rns
```

### Flash RNode Firmware CE onto both RAK4631s

```bash
pip install rnodeconf
rnodeconf /dev/ttyACM0 --autoinstall    # run separately on each machine for each device
```

Select `RAK4631` board and `EU868` model when prompted.

### Verify RNode is recognised

```bash
rnodeconf /dev/ttyACM0 --info
```

Should show frequency, bandwidth, SF, firmware version.

### Start Reticulum on both machines

```bash
rnsd --verbose      # runs in foreground; use -d for daemon
```

Watch logs for `Interface RNode * is up` — that confirms the RNodeInterface initialised.

### Run connectivity tests

```bash
# From pod (Mac Mini) — check if laptop's identity is reachable
rnprobe <laptop-identity-hash>

# Get identity hash on laptop
python3 -c "import RNS; r=RNS.Reticulum(); id=RNS.Identity(); print(id.hash.hex())"
# or read from ~/.reticulum/storage/identity after first rnsd run

# Path lookup
rnpath <laptop-identity-hash>

# Continuous probe for soak test (run from both sides)
watch -n 60 rnprobe <remote-hash>
```

### Optional: rnx echo test

```bash
# On laptop — start a simple echo destination
python3 -m RNS.examples.echo_server

# On Mac Mini
python3 -m RNS.examples.echo_client <laptop-hash>
```

---

## 5. Sensor Nodes — Pending Phase 0 Completion

The three ready nodes will be integrated once the LoRa transport layer is confirmed stable.

| Node | Interface | Integration approach |
|------|-----------|----------------------|
| **sn_soil** | BLE → RNode (Phase 0: direct USB poll for testing) | BLE GATT client on farm-pod; telemetry forwarded over Reticulum |
| **sn_air** | BLE → RNode | Same as sn_soil |
| **sn_vision** | WiFi → farm-pod HTTP | POST images to AgroNomi API; no LoRa path needed |

> **Note on BLE gateway:** With no Pi Zero 2W available, BLE aggregation from sn_soil and sn_air runs on the **Mac Mini** directly (Python + bleak) or the **Linux laptop** (whichever is physically closer to the sensor nodes). When Pi Zero 2W stock recovers, it replaces the laptop as the dedicated field BLE+LoRa gateway.

---

## 6. Network Stack

| Layer | Component | Technology |
|-------|-----------|------------|
| L1 — Physical | SX1262 LoRa | 868 MHz EU, SF11, BW125 |
| L2 — Data Link | RNode Firmware CE | KISS over USB serial, CSMA/CA |
| L3 — Network | Reticulum RNS | EC crypto, multi-hop routing |
| L4 — Transport | Reticulum Channel/Buffer | Reliable sequenced delivery |
| L5 — Application | Farm Telemetry Protocol | JSON over Reticulum links |
| L6 — Aggregation | sensor_aggregator.py | SQLite, threshold alerts |

---

## 7. Future Phases

| Phase | Trigger | Change |
|-------|---------|--------|
| **Phase 1** | Phase 0 soak test passes | Add sn_soil + sn_air BLE integration; validate end-to-end telemetry path |
| **Phase 2** | Pi Zero 2W available | Deploy Pi W as dedicated field gateway; laptop retires from gateway role |
| **Phase 3** | Field deployment | Weatherproof enclosures, solar power, production Reticulum identities |

---

*Living document — update port paths, identity hashes, and phase status as the system evolves.*
