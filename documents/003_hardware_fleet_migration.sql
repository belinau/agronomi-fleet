-- Farm AI Box v3.0 — Hardware Peripherals Fleet Database Extensions
-- Migration: Add hardware fleet registry, Reticulum gateway tracking, actuator command queue

-- Hardware device registry (the physical fleet)
CREATE TABLE IF NOT EXISTS hardware_devices (
    device_id TEXT PRIMARY KEY,              -- e.g., "SN-SOIL-03", "AN-PUMP-01"
    device_type TEXT NOT NULL CHECK(device_type IN ('gateway','piw_gateway','soil_node','air_node','pump_node','gh_actuator')),
    node_id TEXT UNIQUE REFERENCES sensor_nodes(node_id) ON DELETE SET NULL,
    field_id TEXT REFERENCES fields(field_id) ON DELETE SET NULL,
    ble_mac TEXT,                            -- 6-byte MAC as hex, e.g., "A4:B3:C2:D1:E0:FF"
    ble_target_gateway TEXT,                 -- Which GW-RAK this node connects to
    firmware_version TEXT DEFAULT '0.0.0',
    hardware_revision TEXT,
    battery_type TEXT DEFAULT '18650_liion',
    install_date TEXT,                       -- ISO datetime
    last_seen TEXT,                          -- ISO datetime
    status TEXT DEFAULT 'active' CHECK(status IN ('active','offline','maintenance','decommissioned'))
);

CREATE INDEX IF NOT EXISTS idx_hw_devices_type ON hardware_devices(device_type);
CREATE INDEX IF NOT EXISTS idx_hw_devices_field ON hardware_devices(field_id);
CREATE INDEX IF NOT EXISTS idx_hw_devices_gateway ON hardware_devices(ble_target_gateway);

-- Reticulum gateway registry (one per RAK4631 edge gateway)
CREATE TABLE IF NOT EXISTS reticulum_gateways (
    gateway_id TEXT PRIMARY KEY,             -- e.g., "GW-RAK-01"
    device_id TEXT NOT NULL REFERENCES hardware_devices(device_id),
    rns_destination_hash TEXT UNIQUE,        -- Reticulum destination hash
    lora_frequency INTEGER DEFAULT 868000000,
    lora_spreading_factor INTEGER DEFAULT 11,
    lora_bandwidth INTEGER DEFAULT 125000,
    lora_coding_rate INTEGER DEFAULT 5,
    lora_tx_power INTEGER DEFAULT 17,
    last_heartbeat TEXT,
    peers_count INTEGER DEFAULT 0,
    mesh_rank INTEGER DEFAULT 0,             -- Hop distance from farm hub
    gateway_platform TEXT DEFAULT 'rak4631' CHECK(gateway_platform IN ('rak4631','pi_zero_2w'))
);

-- Actuator command queue (outbound to ESP32-C6 actuator nodes)
CREATE TABLE IF NOT EXISTS actuator_commands (
    cmd_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES hardware_devices(device_id),
    cmd_type TEXT NOT NULL CHECK(cmd_type IN ('pump_on','pump_off','vent_open','vent_close','shade_pct','fan_on','fan_off','irrigate_mm')),
    cmd_value REAL,                          -- e.g., shade percentage, irrigation volume
    requested_by TEXT,                       -- user_id or 'autotrigger'
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    executed_at TEXT,
    acknowledged_at TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending','sent','acknowledged','failed','expired','cancelled')),
    retry_count INTEGER DEFAULT 0,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_actuator_cmds_status ON actuator_commands(status);
CREATE INDEX IF NOT EXISTS idx_actuator_cmds_device ON actuator_commands(device_id, requested_at);

-- BLE link quality log (diagnostics)
CREATE TABLE IF NOT EXISTS ble_link_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT REFERENCES hardware_devices(device_id),
    gateway_id TEXT REFERENCES reticulum_gateways(gateway_id),
    event TEXT CHECK(event IN ('connected','disconnected','timeout','rx_packet','tx_packet','rssi_update')),
    rssi INTEGER,                            -- BLE RSSI in dBm
    payload_bytes INTEGER,
    latency_ms INTEGER,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ble_log_device ON ble_link_log(device_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_ble_log_gateway ON ble_link_log(gateway_id, recorded_at);

-- Telemetry ingress staging (optional, for buffering before sensor_readings)
CREATE TABLE IF NOT EXISTS telemetry_ingress (
    ingress_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_payload TEXT NOT NULL,               -- JSON from ESP32-C6
    gateway_id TEXT,
    device_id TEXT,
    parsed INTEGER DEFAULT 0,                -- 0=pending, 1=success, 2=error
    parse_error TEXT,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- View: Active node status dashboard
CREATE VIEW IF NOT EXISTS v_node_status AS
SELECT
    hd.device_id,
    hd.device_type,
    hd.node_id,
    hd.field_id,
    f.field_name,
    hd.status AS device_status,
    hd.last_seen,
    hd.ble_target_gateway,
    rg.last_heartbeat AS gateway_heartbeat,
    sn.battery_level,
    sn.last_seen AS sensor_node_last_seen,
    CASE
        WHEN sn.last_seen IS NULL THEN 'unknown'
        WHEN julianday('now') - julianday(sn.last_seen) > OFFLINE_THRESHOLD_HOURS / 24.0 THEN 'offline'
        ELSE 'online'
    END AS connectivity_status
FROM hardware_devices hd
LEFT JOIN fields f ON hd.field_id = f.field_id
LEFT JOIN reticulum_gateways rg ON hd.ble_target_gateway = rg.gateway_id
LEFT JOIN sensor_nodes sn ON hd.node_id = sn.node_id
WHERE hd.status = 'active';

-- Seed data example (uncomment to insert test records)
-- INSERT INTO hardware_devices (device_id, device_type, node_id, field_id, ble_mac, ble_target_gateway, firmware_version, install_date, status)
-- VALUES
--     ('GW-PIW-01', 'piw_gateway', 'gw_piw_01', NULL, NULL, NULL, '1.0.0-pios', '2026-06-01', 'active'),
--     ('GW-RAK-USB-01', 'gateway', NULL, NULL, NULL, 'GW-PIW-01', '1.75.0', '2026-06-01', 'active'),
--     ('GW-RAK-01', 'gateway', 'gw_01', NULL, 'DE:AD:BE:EF:00:01', NULL, '1.75.0', '2026-04-01', 'active'),
--     ('SN-SOIL-01', 'soil_node', 'soil_n1', 'field_1', 'A4:B3:C2:D1:E0:01', 'GW-RAK-01', '1.0.0', '2026-04-01', 'active'),
--     ('SN-SOIL-02', 'soil_node', 'soil_n2', 'field_1', 'A4:B3:C2:D1:E0:02', 'GW-RAK-01', '1.0.0', '2026-04-01', 'active'),
--     ('SN-AIR-01', 'air_node', 'gh_air_1', 'greenhouse_1', 'A4:B3:C2:D1:E0:03', 'GW-RAK-01', '1.0.0', '2026-04-01', 'active'),
--     ('AN-PUMP-01', 'pump_node', 'pump_1', 'field_1', 'A4:B3:C2:D1:E0:04', 'GW-RAK-01', '1.0.0', '2026-04-01', 'active');

-- Seed data example: Reticulum gateways
-- INSERT INTO reticulum_gateways (gateway_id, device_id, rns_destination_hash, gateway_platform, lora_frequency, lora_spreading_factor)
-- VALUES
--     ('GW-RAK-01', 'GW-RAK-01', '<hash>', 'rak4631', 868000000, 11),
--     ('GW-PIW-01', 'GW-PIW-01', '<hash>', 'pi_zero_2w', 868000000, 11);
