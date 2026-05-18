# Reticulum Ingest Port Analysis: pod_peripherals → AgroNomi

## Executive Summary

`reticulum_ingest.py` can be ported to AgroNomi, but it needs 4 new tables, 1 schema alignment, and a database connection refactor. The sensor data flow is clean — `sensor_readings` already exists in AgroNomi with a richer schema than reticulum's version. The main work is adding fleet management tables and switching from raw `sqlite3` to AgroNomi's connection pool.

---

## What AgroNomi Already Has

| Table | Purpose | Compatibility |
|-------|---------|--------------|
| `sensor_nodes` | ESP32-C6 devices (node_id, node_type, field_id, firmware_version, battery_level, calibration, status) | ✅ Superset of reticulum's version. Reticulum's auto-provisioning inserts a subset of these columns. |
| `sensor_readings` | Time-series sensor data (node_id, reading_type, value, unit, depth_cm, recorded_at) | ✅ Superset — has `unit` and `depth_cm` that reticulum's DDL lacks. |
| `sensor_alerts` | Materialized alert snapshot (wiped & rewritten by `sensor_aggregator.py`) | ✅ Reticulum doesn't touch this. Clean separation. |
| `field_thresholds` | Per-field threshold overrides for alerts | ✅ No conflict. |
| `crop_alert_thresholds` | Crop-specific threshold defaults | ✅ No conflict. |
| `node_registry` | BLE address tracking (node_id, ble_address, ble_service_uuid) | ⚠️ Partial overlap with `hardware_devices.ble_mac` — see below. |

## What's Missing (4 new tables needed)

### 1. `hardware_devices` — Physical device fleet registry

Reticulum needs a table that tracks the *physical* device separate from the *logical* sensor node. AgroNomi's `sensor_nodes` is the logical layer; `hardware_devices` is the physical layer.

```sql
CREATE TABLE hardware_devices (
    device_id TEXT PRIMARY KEY,           -- e.g. "SN-AIR-01", "AN-PUMP-01"
    device_type TEXT NOT NULL CHECK(device_type IN (
        'gateway','piw_gateway','soil_node','air_node','pump_node','gh_actuator','vision_node'
    )),
    node_id TEXT UNIQUE REFERENCES sensor_nodes(node_id) ON DELETE SET NULL,
    field_id TEXT REFERENCES fields(field_id) ON DELETE SET NULL,
    ble_mac TEXT,
    ble_target_gateway TEXT,
    firmware_version TEXT DEFAULT '0.0.0',
    hardware_revision TEXT,
    battery_type TEXT DEFAULT '18650_liion',
    install_date TEXT,
    last_seen TEXT,
    status TEXT DEFAULT 'active' CHECK(status IN (
        'active','offline','maintenance','decommissioned'
    ))
);
```

**Relationship**: `hardware_devices.node_id` → `sensor_nodes.node_id`. Auto-provisioning creates rows in both tables on first telemetry.

### 2. `reticulum_gateways` — LoRa gateway tracking

```sql
CREATE TABLE reticulum_gateways (
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
    gateway_platform TEXT DEFAULT 'rpi' CHECK(gateway_platform IN ('rak4631','rpi'))
);
```

Auto-populated via RNS announces — no manual entry needed.

### 3. `actuator_commands` — Outbound command queue

AgroNomi has `irrigation_schedule` for water planning but no general-purpose actuator command queue. This is essential for pump/fan/vent control and OTA dispatch.

```sql
CREATE TABLE actuator_commands (
    cmd_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES hardware_devices(device_id),
    cmd_type TEXT NOT NULL CHECK(cmd_type IN (
        'pump_on','pump_off','vent_open','vent_close','shade_pct',
        'fan_on','fan_off','irrigate_mm','ota_request','ota_abort'
    )),
    cmd_value REAL,
    cmd_value_text TEXT,
    requested_by TEXT,
    requested_at TEXT NOT NULL DEFAULT(datetime('now')),
    executed_at TEXT,
    acknowledged_at TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN (
        'pending','transferring','sent','acknowledged','failed','expired','cancelled'
    )),
    retry_count INTEGER DEFAULT 0,
    error_message TEXT
);
```

### 4. `ble_link_log` — BLE diagnostics

```sql
CREATE TABLE ble_link_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT REFERENCES hardware_devices(device_id),
    gateway_id TEXT REFERENCES reticulum_gateways(gateway_id),
    event TEXT CHECK(event IN (
        'connected','disconnected','timeout','rx_packet','tx_packet','rssi_update'
    )),
    rssi INTEGER,
    payload_bytes INTEGER,
    latency_ms INTEGER,
    recorded_at TEXT NOT NULL DEFAULT(datetime('now'))
);
```

## Schema Conflicts to Resolve

| Issue | Reticulum | Pod-Farm | Resolution |
|-------|-----------|----------|------------|
| `sensor_readings` PK | `reading_id` (reticulum) | `id` (AgroNomi) | Use AgroNomi's `id`. Reticulum's `record_telemetry()` just needs the INSERT adjusted. |
| `sensor_nodes` columns | 5 cols (node_id, name, location, last_seen, battery_level) | 9 cols (node_id, node_type, field_id, firmware_version, battery_level, registered_at, calibration_date, calibration_data, status) | Use AgroNomi's richer schema. Reticulum's auto-provision INSERT only populates `node_id` and `name`; the rest default to NULL. `node_type` has no default and is NOT NULL — must be set during auto-provision. |
| `sensor_readings.unit` | Defaults to `''` (reticulum) | NOT NULL, no default (AgroNomi) | Reticulum's `_get_unit()` must always return a value. Currently returns `''` for unknown types — needs to return a non-empty default like `'unknown'` or adjust the AgroNomi schema to allow empty string. |
| `sensor_nodes.node_type` | Not in reticulum schema | NOT NULL in AgroNomi | **Critical**: Auto-provisioning must set `node_type` during INSERT. Map from `device_type` in telemetry: `air_node→'air'`, `soil_node→'soil'`, `pump_node→'pump'`, `gh_actuator→'greenhouse'`. |
| DB path | `./farm_data.db` | `farm_knowledge.db` via `db_pool.py` | **Must switch**. Reticulum needs to use `DatabaseConnectionPool` from AgroNomi's `db_pool.py`. |
| Connection model | `sqlite3.connect()` per call, manual WAL/FK pragmas | `DatabaseConnectionPool` with `get_connection()` / `transaction()` | **Major refactor**. Every `get_db()` call in reticulum_ingest must be replaced with AgroNomi's pool access. Pool already sets WAL, FK, busy_timeout. |
| Thread safety | Fresh `sqlite3.connect()` per call (thread-safe by isolation) | Pool manages connection recycling | Pool handles this, but RNS callbacks must not share connection objects across threads — use `pool.get_connection()` inside each callback. |

## Database Connection Refactor (Critical)

`reticulum_ingest.py` currently uses a standalone `get_db()` function that creates a new `sqlite3.connect()` each time. AgroNomi uses a `DatabaseConnectionPool` with context-managed connections.

**Required changes:**
1. Remove `get_db()` and `_SCHEMA_DDL` from reticulum_ingest.py
2. Import `get_db` / `pool` from AgroNomi's `db_pool.py` (or `farm_knowledge.py`)
3. Replace all `with get_db() as conn:` patterns with `with pool.transaction() as conn:` or `conn = pool.get_connection()`
4. Ensure RNS callbacks (which run on different threads) get fresh connections — the pool handles this, but RNS callbacks must not share connection objects across threads

## `node_registry` vs `hardware_devices` Overlap

AgroNomi's `node_registry` has `ble_address` and `ble_service_uuid`. Reticulum's `hardware_devices` has `ble_mac` and `ble_target_gateway`.

**Recommendation**: Keep `node_registry` for BLE service-level discovery and merge `ble_mac` into `hardware_devices` for fleet routing. They serve different purposes:
- `node_registry`: BLE scanning/discovery metadata
- `hardware_devices`: Fleet management and command routing

## `sensor_aggregator.py` Integration

`sensor_aggregator.py` reads from `sensor_readings` and writes to `sensor_alerts`. It does NOT write to `sensor_readings`. This means:

- **Reticulum writes** → `sensor_readings` (and `hardware_devices`, `sensor_nodes` on auto-provision)
- **Sensor aggregator reads** → `sensor_readings`, evaluates thresholds, writes → `sensor_alerts`
- **No conflict.** Reticulum and sensor aggregator are producer/consumer on the same table.

**One gap**: Sensor aggregator currently doesn't read from `hardware_devices`. It should join through `sensor_nodes` → `hardware_devices` to get `ble_mac`, `device_type`, and `firmware_version` for enriched alert context. This is a minor enhancement.

## `irrigation_schedule` vs `actuator_commands`

AgroNomi has `irrigation_schedule` for planned watering. The new `actuator_commands` table is for real-time device commands (pump on/off, fan on/off, OTA). They serve different purposes:

- `irrigation_schedule`: Planned by the AI/farmer, time-based, stored in AgroNomi DB
- `actuator_commands`: Dispatched by `CommandDispatcher`, device-targeted, status-tracked in real-time

An `irrigate_mm` command type in `actuator_commands` could bridge the two — the irrigation scheduler inserts a row into `actuator_commands` to actually trigger the pump.

## Config: Keep Reticulum Separate

Reticulum uses its own `~/.reticulum/config` for interface, transport, and RNS-level settings. This should **not** be merged into AgroNomi's `config.yaml`. The two configs serve different layers:

- **`~/.reticulum/config`** — RNS interfaces (RNode, TCP, etc.), transport settings, share_instance. Already works standalone.
- **`config.yaml`** — AgroNomi application config (models, DB, sensors, UI).

What **does** need adding to AgroNomi's `config.yaml` is a small `ReticulumConfig` in `config/schemas.py` for **application-level** RNS settings only:

```yaml
reticulum:
  identity_path: "./farm_hub.identity"
  announce_interval: 30
  command_poll_interval: 5
  ota:
    firmware_dir: "/var/agronomi/fw"
    window_start: 21
    window_end: 24
    max_retries: 3
```

RNS aspect names (`farm.telemetry_readings`, `farm.commands_control`, `farm.gateway_commands`) are constants in the code, not config — they must match between hub and gateway, so hardcoding them is correct.

## Port Checklist

| # | Task | Effort | Notes |
|---|------|--------|-------|
| 1 | Add 4 migration tables to `farm_knowledge.py` SCHEMA_SQL | Small | `hardware_devices`, `reticulum_gateways`, `actuator_commands`, `ble_link_log` |
| 2 | Refactor DB access: `sqlite3` → `DatabaseConnectionPool` | Medium | Every `get_db()` call, especially in RNS callbacks |
| 3 | Remove `_SCHEMA_DDL` from reticulum_ingest, rely on AgroNomi schema | Small | Schema is now owned by farm_knowledge.py |
| 4 | Fix `record_telemetry()` auto-provision to set `node_type` (NOT NULL) | Medium | Map `device_type`→`node_type`: `air_node→'air'`, `soil_node→'soil'`, `pump_node→'pump'`, `gh_actuator→'greenhouse'` |
| 5 | Fix `sensor_readings.unit` — `_get_unit()` must never return `''` | Small | AgroNomi schema has `unit TEXT NOT NULL` with no default. Return `'unknown'` for unmapped types. |
| 6 | Adjust `sensor_readings` INSERT for AgroNomi schema | Medium | Column `id` (not `reading_id`), include `unit` column |
| 7 | Wire `sensor_aggregator.py` to join `hardware_devices` for enriched alerts | Small | Optional enhancement |
| 8 | Add `ReticulumConfig` to `config/schemas.py` — app-level only, NOT RNS config | Small | `identity_path`, `announce_interval`, `command_poll_interval`, OTA settings |
| 9 | Test auto-provisioning with AgroNomi's existing `sensor_nodes` data | Medium | Ensure INSERT only populates known columns |
| 10 | Add `irrigate_mm` bridge between `irrigation_schedule` and `actuator_commands` | Small | Future: irrigation scheduler → command dispatcher |