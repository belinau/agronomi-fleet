-- AgroNomi v4.0 — µReticulum-Native Architecture Migration
-- Removes BLE-specific columns, adds RNS-native routing
--
-- This migration is applied by reticulum_ingest.py's _migrate_schema() function,
-- but can also be run standalone on an existing database.
--
-- Key changes:
--   1. Add rns_identity_hash, rns_interface, and rns_destination_hash columns to hardware_devices
--   2. Drop ble_link_log table (BLE diagnostics obsolete)
--   3. Rebuild hardware_devices to remove ble_mac/ble_target_gateway from CHECK
--   4. Update gateway_platform CHECK to include esp32c6
--   5. Update device_type CHECK to include vision_node and piw_gateway
--   6. Add rns_destination_hash to hardware_devices for direct command dispatch

-- ============================================================
-- Step 1: Add new RNS routing columns
-- ============================================================
ALTER TABLE hardware_devices ADD COLUMN rns_identity_hash TEXT;
ALTER TABLE hardware_devices ADD COLUMN rns_interface TEXT DEFAULT 'ble'
    CHECK(rns_interface IN ('lora', 'ble', 'wifi', 'serial'));
ALTER TABLE hardware_devices ADD COLUMN rns_destination_hash TEXT;

-- ============================================================
-- Step 2: Drop obsolete BLE diagnostics table
-- ============================================================
DROP TABLE IF EXISTS ble_link_log;

-- ============================================================
-- Step 3: Rebuild hardware_devices with updated schema
-- (SQLite < 3.35.0 doesn't support DROP COLUMN or ALTER CHECK)
-- ============================================================
CREATE TABLE IF NOT EXISTS hardware_devices_new (
    device_id           TEXT PRIMARY KEY,
    device_type         TEXT NOT NULL CHECK(device_type IN (
                            'gateway','piw_gateway','soil_node','air_node',
                            'pump_node','gh_actuator','vision_node'
                        )),
    node_id             TEXT UNIQUE REFERENCES sensor_nodes(node_id) ON DELETE SET NULL,
    field_id            TEXT REFERENCES fields(field_id) ON DELETE SET NULL,
    rns_identity_hash   TEXT,               -- RNS identity hash from announce
    rns_destination_hash TEXT,              -- RNS destination hash for command dispatch
    rns_interface       TEXT DEFAULT 'ble'  -- Which transport: lora, ble, wifi, serial
                        CHECK(rns_interface IN ('lora','ble','wifi','serial')),
    firmware_version    TEXT DEFAULT '0.0.0',
    hardware_revision   TEXT,
    battery_type        TEXT DEFAULT '18650_liion',
    install_date        TEXT,
    status              TEXT DEFAULT 'active'
                        CHECK(status IN
                            ('active','offline','maintenance','decommissioned')),
    last_seen           TEXT
);

-- Migrate data from old table, preserving existing values
-- ble_mac and ble_target_gateway are intentionally NOT migrated
-- (they are replaced by rns_identity_hash and rns_interface)
INSERT OR IGNORE INTO hardware_devices_new
    SELECT
        device_id, device_type, node_id, field_id,
        NULL,               -- rns_identity_hash (populated on announce)
        NULL,               -- rns_destination_hash (populated on announce)
        'ble',              -- rns_interface (default, updated on announce)
        firmware_version, hardware_revision, battery_type,
        install_date, status, last_seen
    FROM hardware_devices;

-- Swap tables
DROP TABLE hardware_devices;
ALTER TABLE hardware_devices_new RENAME TO hardware_devices;

-- Recreate indexes
CREATE INDEX IF NOT EXISTS idx_hw_devices_type ON hardware_devices(device_type);
CREATE INDEX IF NOT EXISTS idx_hw_devices_field ON hardware_devices(field_id);
CREATE INDEX IF NOT EXISTS idx_hw_rns_hash ON hardware_devices(rns_identity_hash);
CREATE INDEX IF NOT EXISTS idx_hw_rns_dest ON hardware_devices(rns_destination_hash);
CREATE INDEX IF NOT EXISTS idx_hw_rns_iface ON hardware_devices(rns_interface);

-- ============================================================
-- Step 4: Update reticulum_gateways to include esp32c6 platform
-- ============================================================
CREATE TABLE IF NOT EXISTS reticulum_gateways_new (
    gateway_id              TEXT PRIMARY KEY,
    device_id               TEXT NOT NULL REFERENCES hardware_devices(device_id),
    rns_destination_hash    TEXT UNIQUE,
    lora_frequency          INTEGER DEFAULT 868000000,
    lora_spreading_factor   INTEGER DEFAULT 11,
    lora_bandwidth          INTEGER DEFAULT 125000,
    lora_coding_rate        INTEGER DEFAULT 5,
    lora_tx_power           INTEGER DEFAULT 17,
    last_heartbeat          TEXT,
    peers_count             INTEGER DEFAULT 0,
    mesh_rank               INTEGER DEFAULT 0,
    gateway_platform        TEXT DEFAULT 'rak4631'
        CHECK(gateway_platform IN ('rak4631', 'piw_gateway', 'esp32c6'))
);

INSERT OR IGNORE INTO reticulum_gateways_new
    SELECT
        gateway_id, device_id, rns_destination_hash,
        lora_frequency, lora_spreading_factor, lora_bandwidth,
        lora_coding_rate, lora_tx_power,
        last_heartbeat, peers_count, mesh_rank,
        COALESCE(gateway_platform, 'rak4631')
    FROM reticulum_gateways;

DROP TABLE reticulum_gateways;
ALTER TABLE reticulum_gateways_new RENAME TO reticulum_gateways;

-- ============================================================
-- Step 5: Add rns_interface to actuator_commands for tracking
-- which transport carried the command (informational only)
-- ============================================================
-- Note: This ALTER TABLE may fail if the column already exists (idempotent)
ALTER TABLE actuator_commands ADD COLUMN rns_interface TEXT
    CHECK(rns_interface IN ('lora', 'ble', 'wifi', 'serial'));

-- ============================================================
-- Step 6: Vacuum to reclaim space from dropped table/columns
-- ============================================================
-- VACUUM;  -- Uncomment to run VACUUM after migration
