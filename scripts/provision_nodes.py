#!/usr/bin/env python3
"""Provision the AgroNomi database with sensor nodes, hardware devices, and gateways.

This script idempotently inserts the baseline fleet of nodes, devices, and the
LoRa gateway into the AgroNomi SQLite database.  It uses ``INSERT OR IGNORE``
so it can be safely re-run without duplicating rows.

Usage examples::

    # Default database path
    python3 scripts/provision_nodes.py

    # Custom database path
    python3 scripts/provision_nodes.py --db /tmp/farm_data.db

    # With a known RNS destination hash (hex string, no ``0x`` prefix)
    python3 scripts/provision_nodes.py --gateway-rns-hash a1b2c3d4e5f67890abcdef1234567890

    # Show current contents of all three tables
    python3 scripts/provision_nodes.py --show
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Sequence

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
]

HARDWARE_DEVICES: list[dict] = [
    {
        "device_id": "SN-SOIL-01",
        "device_type": "soil_node",
        "node_id": "SN-SOIL-01",
        "ble_mac": "AA:BB:CC:DD:EE:01",
        "ble_target_gateway": "GW-MIMI-01",
        "firmware_version": "1.3.0",
        "hardware_revision": "rev-c6-v1",
        "battery_type": None,
        "install_date": datetime.now(tz=timezone.utc).isoformat(),
        "status": "active",
    },
    {
        "device_id": "SN-AIR-01",
        "device_type": "air_node",
        "node_id": "SN-AIR-01",
        "ble_mac": "AA:BB:CC:DD:EE:02",
        "ble_target_gateway": "GW-MIMI-01",
        "firmware_version": "1.3.0",
        "hardware_revision": "rev-c6-v1",
        "battery_type": None,
        "install_date": datetime.now(tz=timezone.utc).isoformat(),
        "status": "active",
    },
    {
        "device_id": "GW-MIMI-01",
        "device_type": "gateway",
        "node_id": None,
        "ble_mac": None,
        "ble_target_gateway": None,
        "firmware_version": "0.1.0",
        "hardware_revision": "pi-zero-2w",
        "battery_type": None,
        "install_date": datetime.now(tz=timezone.utc).isoformat(),
        "status": "active",
    },
]

# ``rns_destination_hash`` and LoRa params are supplied at runtime.
RETICULUM_GATEWAYS: list[dict] = [
    # Populated dynamically in ``main()`` once we know the RNS hash.
]


# ---------------------------------------------------------------------------
# SQL statements
# ---------------------------------------------------------------------------

INSERT_SENSOR_NODE_SQL = """
INSERT OR IGNORE INTO sensor_nodes (node_id, name, location)
VALUES (:node_id, :name, :location)
"""

INSERT_HARDWARE_DEVICE_SQL = """
INSERT OR IGNORE INTO hardware_devices (
    device_id, device_type, node_id,
    ble_mac, ble_target_gateway,
    firmware_version, hardware_revision,
    battery_type, install_date, status
) VALUES (
    :device_id, :device_type, :node_id,
    :ble_mac, :ble_target_gateway,
    :firmware_version, :hardware_revision,
    :battery_type, :install_date, :status
)
"""

INSERT_RETICULUM_GATEWAY_SQL = """
INSERT OR IGNORE INTO reticulum_gateways (
    gateway_id, device_id,
    rns_destination_hash,
    lora_frequency, lora_spreading_factor
) VALUES (
    :gateway_id, :device_id,
    :rns_destination_hash,
    :lora_frequency, :lora_spreading_factor
)
"""


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


def provision(conn: sqlite3.Connection, gateway_rns_hash: str) -> dict[str, int]:
    """Insert baseline fleet data into the database.

    Uses ``INSERT OR IGNORE`` so the script is idempotent — re-running it
    will not duplicate rows that already exist.

    Args:
        conn: An open database connection with foreign keys enabled.
        gateway_rns_hash: Hex-encoded RNS destination hash for the gateway.
            May be an empty string if the gateway has not yet announced.

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
    gateway_row = {
        "gateway_id": "GW-MIMI-01",
        "device_id": "GW-MIMI-01",
        "rns_destination_hash": gateway_rns_hash,
        "lora_frequency": 867200000,
        "lora_spreading_factor": 8,
    }
    cur = conn.execute(INSERT_RETICULUM_GATEWAY_SQL, gateway_row)
    counts["reticulum_gateways"] = cur.rowcount

    conn.commit()
    return counts


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
        "hardware devices, and gateways.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite database file (default: ./farm_data.db "
        "or DB_PATH env var).",
    )
    parser.add_argument(
        "--gateway-rns-hash",
        default="",
        help="Hex-encoded RNS destination hash for the LoRa gateway. "
        "Defaults to empty string (filled in when gateway announces).",
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

        counts = provision(conn, args.gateway_rns_hash)

        print("Provisioning complete:")
        for table, n in counts.items():
            print(f"  {table}: {n} row(s) inserted")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
