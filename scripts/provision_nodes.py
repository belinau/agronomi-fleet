#!/usr/bin/env python3
"""Provision the AgroNomi database with sensor nodes, hardware devices, and gateways.

RNS-native provisioning script for the AgroNomi fleet database.  All BLE-era
artefacts (``ble_mac``, ``ble_target_gateway``, hardcoded LoRa radio params)
have been removed in favour of announce-based auto-discovery.

**Key design points**

* RNS identity hashes (``rns_identity_hash``) and destination hashes
  (``rns_destination_hash``) are **auto-populated** by
  ``GatewayAnnounceHandler`` in ``reticulum_ingest.py`` when nodes announce
  on the RNS network — no manual entry is needed.
* ``rns_interface`` specifies the transport layer for each device:
  ``'ble'`` for ESP32-C6 sensors/actuators, ``'lora'`` for the RNode gateway.
* LoRa radio parameters (``lora_frequency``, ``lora_spreading_factor``) come
  from the RNode configuration file, **not** from the database, so they are
  omitted from seed data.
* ``--discover`` mode (stub) will eventually connect to a live RNS network
  and auto-populate rows from announce packets, replacing static seeding.

Usage examples::

    # Default database path — idempotent static seeding
    python3 scripts/provision_nodes.py

    # Custom database path
    python3 scripts/provision_nodes.py --db /tmp/farm_data.db

    # Discover mode (stub — will auto-populate from RNS announces)
    python3 scripts/provision_nodes.py --discover

    # Show current contents of all three tables
    python3 scripts/provision_nodes.py --show
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data constants
# ---------------------------------------------------------------------------

SENSOR_NODES: list[dict] = [
    {
        "node_id": "SN-SOIL-01",
        "name": "Soil Sensor 1",
        "location": "Field A - Row 3",
    },
    {
        "node_id": "SN-AIR-01",
        "name": "Air Sensor 1",
        "location": "Field A - Weather Station",
    },
    {
        "node_id": "AN-PUMP-01",
        "name": "Pump Actuator 1",
        "location": "Field A - Irrigation",
    },
    {
        "node_id": "AN-GREENHOUSE-01",
        "name": "Greenhouse Actuator 1",
        "location": "Greenhouse Bay 1",
    },
]

HARDWARE_DEVICES: list[dict] = [
    {
        "device_id": "SN-SOIL-01",
        "device_type": "soil_node",
        "node_id": "SN-SOIL-01",
        "rns_interface": "ble",
        "firmware_version": "2.0.0-mr",
        "hardware_revision": "rev-c6-v1",
        "battery_type": "18650_liion",
        "install_date": datetime.now(tz=timezone.utc).isoformat(),
        "status": "active",
    },
    {
        "device_id": "SN-AIR-01",
        "device_type": "air_node",
        "node_id": "SN-AIR-01",
        "rns_interface": "ble",
        "firmware_version": "2.0.0-mr",
        "hardware_revision": "rev-c6-v1",
        "battery_type": "18650_liion",
        "install_date": datetime.now(tz=timezone.utc).isoformat(),
        "status": "active",
    },
    {
        "device_id": "AN-PUMP-01",
        "device_type": "pump_node",
        "node_id": None,  # actuators may not be in sensor_nodes table
        "rns_interface": "ble",
        "firmware_version": "2.0.0-mr",
        "hardware_revision": "rev-c6-v1",
        "battery_type": None,
        "install_date": datetime.now(tz=timezone.utc).isoformat(),
        "status": "active",
    },
    {
        "device_id": "AN-GREENHOUSE-01",
        "device_type": "gh_actuator",
        "node_id": None,
        "rns_interface": "ble",
        "firmware_version": "2.0.0-mr",
        "hardware_revision": "rev-c6-v1",
        "battery_type": None,
        "install_date": datetime.now(tz=timezone.utc).isoformat(),
        "status": "active",
    },
    {
        "device_id": "RN-HUB-01",
        "device_type": "gateway",
        "node_id": None,
        "rns_interface": "lora",
        "firmware_version": "1.86",
        "hardware_revision": "rak4631",
        "battery_type": None,
        "install_date": datetime.now(tz=timezone.utc).isoformat(),
        "status": "active",
    },
    {
        "device_id": "RN-RELAY-01",
        "device_type": "gateway",
        "node_id": None,
        "rns_interface": "lora",
        "firmware_version": "1.86",
        "hardware_revision": "rak4631",
        "battery_type": None,
        "install_date": datetime.now(tz=timezone.utc).isoformat(),
        "status": "active",
    },
]

# Gateway seed data — only static identifiers.
# rns_destination_hash is auto-populated by GatewayAnnounceHandler when
# the gateway announces on the RNS network.  LoRa radio parameters
# (lora_frequency, lora_spreading_factor) come from the RNode config, not
# the database, so they are intentionally omitted here.
RETICULUM_GATEWAYS: list[dict] = [
    {
        "gateway_id": "GW-MIMI-01",
        "device_id": "RN-HUB-01",
    },
    {
        "gateway_id": "GW-RELAY-01",
        "device_id": "RN-RELAY-01",
    },
]


# ---------------------------------------------------------------------------
# v4 Schema DDL (for reference / future use)
# ---------------------------------------------------------------------------

SCHEMA_V4_SQL = """
-- v4 hardware_devices schema (BLE columns removed, RNS columns added)
CREATE TABLE IF NOT EXISTS hardware_devices (
    device_id           TEXT PRIMARY KEY,
    device_type         TEXT CHECK(device_type IN
                            ('gateway','soil_node','air_node',
                             'pump_node','gh_actuator',
                             'vision_node','piw_gateway')),
    node_id             TEXT REFERENCES sensor_nodes(node_id),
    rns_identity_hash   TEXT,
    rns_interface       TEXT DEFAULT 'ble' CHECK(rns_interface IN
                            ('lora','ble','wifi','serial')),
    firmware_version    TEXT,
    hardware_revision   TEXT,
    battery_type        TEXT,
    install_date        TEXT,
    status              TEXT DEFAULT 'active'
                        CHECK(status IN
                            ('active','offline','maintenance','decommissioned')),
    last_seen           TEXT
);

CREATE TABLE IF NOT EXISTS reticulum_gateways (
    gateway_id              TEXT PRIMARY KEY,
    device_id               TEXT REFERENCES hardware_devices(device_id),
    rns_destination_hash    TEXT UNIQUE,
    lora_frequency          INTEGER,
    lora_spreading_factor   INTEGER,
    last_heartbeat          TEXT,
    peers_count             INTEGER DEFAULT 0
);
"""


# ---------------------------------------------------------------------------
# SQL statements (v4 schema — no ble_mac / ble_target_gateway)
# ---------------------------------------------------------------------------

INSERT_SENSOR_NODE_SQL = """
INSERT OR IGNORE INTO sensor_nodes (node_id, name, location)
VALUES (:node_id, :name, :location)
"""

INSERT_HARDWARE_DEVICE_SQL = """
INSERT OR IGNORE INTO hardware_devices (
    device_id, device_type, node_id,
    rns_interface,
    firmware_version, hardware_revision,
    battery_type, install_date, status
) VALUES (
    :device_id, :device_type, :node_id,
    :rns_interface,
    :firmware_version, :hardware_revision,
    :battery_type, :install_date, :status
)
"""

# gateways use INSERT OR REPLACE so that a re-provision can update
# rns_destination_hash if it has been discovered since the last run.
INSERT_RETICULUM_GATEWAY_SQL = """
INSERT OR REPLACE INTO reticulum_gateways (
    gateway_id, device_id
) VALUES (
    :gateway_id, :device_id
)
"""


# ---------------------------------------------------------------------------
# Schema DDL — executed before provisioning to ensure tables exist
# ---------------------------------------------------------------------------

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sensor_nodes (
    node_id         TEXT PRIMARY KEY,
    name            TEXT,
    location        TEXT,
    last_seen       TEXT,
    battery_level   REAL
);

CREATE TABLE IF NOT EXISTS hardware_devices (
    device_id           TEXT PRIMARY KEY,
    device_type         TEXT CHECK(device_type IN
                            ('gateway','soil_node','air_node',
                             'pump_node','gh_actuator',
                             'vision_node','piw_gateway')),
    node_id             TEXT REFERENCES sensor_nodes(node_id),
    rns_identity_hash   TEXT,
    rns_destination_hash TEXT,
    rns_interface       TEXT DEFAULT 'ble' CHECK(rns_interface IN
                            ('lora','ble','wifi','serial')),
    firmware_version    TEXT,
    hardware_revision   TEXT,
    battery_type        TEXT,
    install_date        TEXT,
    status              TEXT DEFAULT 'active'
                        CHECK(status IN
                            ('active','offline','maintenance','decommissioned')),
    last_seen           TEXT
);

CREATE TABLE IF NOT EXISTS reticulum_gateways (
    gateway_id              TEXT PRIMARY KEY,
    device_id               TEXT REFERENCES hardware_devices(device_id),
    rns_destination_hash    TEXT UNIQUE,
    lora_frequency          INTEGER,
    lora_spreading_factor   INTEGER,
    last_heartbeat          TEXT,
    peers_count             INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS actuator_commands (
    cmd_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL REFERENCES hardware_devices(device_id),
    cmd_type        TEXT CHECK(cmd_type IN
                        ('pump_on','pump_off','vent_open','vent_close',
                         'shade_pct','fan_on','fan_off',
                         'ota_request','ota_abort')),
    cmd_value       REAL,
    cmd_value_text  TEXT,
    requested_at    TEXT NOT NULL DEFAULT (datetime('now')),
    executed_at     TEXT,
    status          TEXT DEFAULT 'pending'
                    CHECK(status IN
                        ('pending','transferring','sent','acknowledged','failed','expired')),
    retry_count     INTEGER DEFAULT 0,
    last_retry_at   TEXT,
    error_message   TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all tables with the v4 schema if they don't exist yet.

    This must be called before :func:`provision` so that columns like
    ``rns_interface`` are present in ``hardware_devices``.
    """
    conn.executescript(_SCHEMA_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def get_db_path(cli_args: argparse.Namespace) -> str:
    """Return the database path from CLI args or the ``DB_PATH`` env var.

    The CLI ``--db`` flag takes precedence over the environment variable.

    Args:
        cli_args: Parsed command-line arguments.

    Returns:
        Path to the SQLite database file.
    """
    return cli_args.db or os.environ.get("DB_PATH", "./farm_data.db")


def connect(db_path: str) -> sqlite3.Connection:
    """Open the SQLite database with foreign-key enforcement enabled.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        An open ``sqlite3.Connection`` with ``row_factory`` set to
        ``sqlite3.Row`` and foreign keys enabled.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def provision(conn: sqlite3.Connection) -> dict[str, int]:
    """Insert baseline fleet data into the database.

    Uses ``INSERT OR IGNORE`` for hardware devices and sensor nodes, and
    ``INSERT OR REPLACE`` for gateways — so the script is idempotent and
    re-running it will not duplicate rows.

    RNS identity hashes and destination hashes are **not** seeded here;
    they are auto-populated by ``GatewayAnnounceHandler`` in
    ``reticulum_ingest.py`` when nodes announce on the network.

    Args:
        conn: An open database connection with foreign keys enabled.

    Returns:
        A dict mapping table names to the number of rows inserted.
    """
    counts: dict[str, int] = {}

    # -- sensor_nodes --------------------------------------------------------
    cur = conn.executemany(INSERT_SENSOR_NODE_SQL, SENSOR_NODES)
    counts["sensor_nodes"] = cur.rowcount

    # -- hardware_devices ----------------------------------------------------
    cur = conn.executemany(INSERT_HARDWARE_DEVICE_SQL, HARDWARE_DEVICES)
    counts["hardware_devices"] = cur.rowcount

    # -- reticulum_gateways --------------------------------------------------
    cur = conn.executemany(INSERT_RETICULUM_GATEWAY_SQL, RETICULUM_GATEWAYS)
    counts["reticulum_gateways"] = cur.rowcount

    conn.commit()
    return counts


def discover_and_provision(conn: sqlite3.Connection) -> dict[str, int]:
    """Connect to the RNS network and auto-populate devices from announces.

    .. TODO::
        Implement RNS-based discovery.  This should:

        1. Import RNS (``import RNS``) and initialise a shared instance.
        2. Attach an announce handler that listens for ``agronomi-sensor:``
           and ``agronomi-gateway:`` app-data prefixes.
        3. For each announce, INSERT OR IGNORE into ``hardware_devices`` and,
           for gateways, INSERT OR REPLACE into ``reticulum_gateways`` —
           exactly as ``GatewayAnnounceHandler`` does in
           ``reticulum_ingest.py``.
        4. Optionally use ``rnstatus`` / ``rnid`` CLI output as a fallback
           data source when a running RNS instance is not available in-process.

    For now this falls back to static provisioning.
    """
    logger.warning(
        "--discover mode is not yet implemented; falling back to static provisioning"
    )
    print("NOTE: --discover is a stub.  Falling back to static provisioning.")
    return provision(conn)


def show_tables(conn: sqlite3.Connection) -> None:
    """Print the current contents of all three fleet tables.

    Args:
        conn: An open database connection with ``row_factory=sqlite3.Row``.
    """
    for table in ("sensor_nodes", "hardware_devices", "reticulum_gateways"):
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            print(f"\n=== {table} (empty) ===")
            continue
        col_names = rows[0].keys()
        print(f"\n=== {table} ({len(rows)} row(s)) ===")
        # Header
        print(" | ".join(col_names))
        print("-" * (len(" | ".join(col_names))))
        # Rows
        for row in rows:
            print(" | ".join(str(row[col]) for col in col_names))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description="Provision the AgroNomi database with sensor nodes, "
        "hardware devices, and gateways.  RNS identity hashes and "
        "destination hashes are auto-populated by GatewayAnnounceHandler "
        "when nodes announce on the network — no manual entry needed.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite database file (default: ./farm_data.db "
        "or DB_PATH env var).",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Auto-discover devices from RNS announces instead of seeding "
        "static data.  (Stub — falls back to static provisioning for now.)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print the current contents of all three tables and exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point for the provisioning script."""
    parser = build_parser()
    args = parser.parse_args(argv)

    db_path = get_db_path(args)
    conn = connect(db_path)

    try:
        if args.show:
            show_tables(conn)
            return

        ensure_schema(conn)

        if args.discover:
            counts = discover_and_provision(conn)
        else:
            counts = provision(conn)

        print("Provisioning complete:")
        for table, n in counts.items():
            print(f"  {table}: {n} row(s) inserted")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
