"""
reticulum_ingest.py — Shared-Instance Compatible LXMF Telemetry Ingestion Engine
"""

import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime

import LXMF
import RNS

# ---------------------------------------------------------------------------
# CONFIGURATION CONSTANTS
# ---------------------------------------------------------------------------
LOG_FILE = os.environ.get("AGRONOMI_LOG", os.path.expanduser("~/agronomi.log"))
DB_PATH = os.environ.get("DB_PATH", "./farm_data.db")
IDENTITY_PATH = "./farm_hub.identity"


def setup_hub_logging():
    log_fh = open(LOG_FILE, "a")

    class TeeStream:
        def __init__(self, original, file_link):
            self._original = original
            self._file_link = file_link

        def write(self, data):
            self._original.write(data)
            if data and data.strip():
                self._file_link.write(data if data.endswith("\n") else data + "\n")
                self._file_link.flush()

        def flush(self):
            self._original.flush()
            self._file_link.flush()

        def fileno(self):
            return self._original.fileno()

    sys.stdout = TeeStream(sys.stdout, log_fh)
    sys.stderr = TeeStream(sys.stderr, log_fh)
    print(f"[HUB] === Unified Logging Engine Live at: {LOG_FILE} ===")


# ---------------------------------------------------------------------------
# SQLITE STORAGE ENGINE SCHEMA
# ---------------------------------------------------------------------------
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sensor_nodes (
    node_id         TEXT PRIMARY KEY,
    name            TEXT,
    location        TEXT,
    last_seen       TEXT,
    battery_level   REAL
);
CREATE TABLE IF NOT EXISTS sensor_readings (
    reading_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      TEXT NOT NULL REFERENCES sensor_nodes(node_id),
    reading_type TEXT NOT NULL,
    value        REAL NOT NULL,
    unit         TEXT DEFAULT '',
    recorded_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hardware_devices (
    device_id           TEXT PRIMARY KEY,
    device_type         TEXT,
    node_id             TEXT REFERENCES sensor_nodes(node_id),
    rns_identity_hash   TEXT,
    rns_destination_hash TEXT,
    rns_interface       TEXT DEFAULT 'wifi',
    firmware_version    TEXT,
    status              TEXT DEFAULT 'active',
    last_seen           TEXT
);
CREATE TABLE IF NOT EXISTS actuator_commands (
    cmd_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL REFERENCES hardware_devices(device_id),
    cmd_type        TEXT,
    cmd_value       REAL,
    cmd_value_text  TEXT,
    requested_at    TEXT NOT NULL DEFAULT (datetime('now')),
    executed_at     TEXT,
    status          TEXT DEFAULT 'pending'
);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_DDL)
    return conn


# ---------------------------------------------------------------------------
# COMMAND DISPATCHER (LXMF COMPATIBLE)
# ---------------------------------------------------------------------------
class OutboundCommandDispatcher:
    def __init__(self, lxm_router, local_lxmf_target):
        self.lxm_router = lxm_router
        self.local_lxmf_target = local_lxmf_target
        self.running = True

    def poll_loop(self):
        RNS.log(
            "[COMMAND ENGINE] Outbound queue scheduler loop active.", RNS.LOG_NOTICE
        )
        while self.running:
            try:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT c.cmd_id, c.device_id, c.cmd_type, h.rns_destination_hash
                    FROM actuator_commands c
                    JOIN hardware_devices h ON c.device_id = h.device_id
                    WHERE c.status = 'pending'
                    LIMIT 5
                """)
                rows = cursor.fetchall()

                for row in rows:
                    cmd_id = row["cmd_id"]
                    dev_id = row["device_id"]
                    cmd_type = row["cmd_type"]
                    dest_hex = row["rns_destination_hash"]

                    if not dest_hex or dest_hex == "unknown":
                        RNS.log(
                            f"[COMMAND REJECT] Missing path to {dev_id}.",
                            RNS.LOG_WARNING,
                        )
                        continue

                    RNS.log(
                        f"[COMMAND ROUTE] Relaying LXM command request '{cmd_type}' targeting <{dest_hex}>",
                        RNS.LOG_NOTICE,
                    )
                    clean_hex = dest_hex.replace("<", "").replace(">", "").strip()
                    dest_bytes = RNS.hex2bytes(clean_hex)

                    # Recall the node's Identity using its destination hash
                    recipient_identity = RNS.Identity.recall(dest_bytes)
                    if not recipient_identity:
                        RNS.log(
                            f"[COMMAND REJECT] Identity not resolved for {dev_id}.",
                            RNS.LOG_WARNING,
                        )
                        continue

                    # Construct a proper RNS.Destination object as expected by the LXMessage constructor
                    dest = RNS.Destination(
                        recipient_identity,
                        RNS.Destination.OUT,
                        RNS.Destination.SINGLE,
                        "lxmf",
                        "delivery",
                    )

                    # Structure command packet natively inside standard LXMF fields
                    command_payload = {"cmd": cmd_type}

                    # Instantiate LXMessage correctly with the Destination object
                    outbound_lxm = LXMF.LXMessage(
                        destination=dest,
                        source=self.local_lxmf_target,
                        content=b"",
                        title="Actuator System Directive",
                        fields=command_payload,
                    )

                    # Hand off to the LXMF router
                    self.lxm_router.handle_outbound(outbound_lxm)

                    conn.execute(
                        "UPDATE actuator_commands SET status = 'sent', executed_at = ? WHERE cmd_id = ?",
                        (datetime.now().isoformat(), cmd_id),
                    )
                    conn.commit()
                conn.close()
            except Exception as e:
                RNS.log(
                    f"[CRITICAL LOOP FAULT] Command subsystem error: {e}", RNS.LOG_ERROR
                )
            time.sleep(2)


# ---------------------------------------------------------------------------
# GLOBAL ANNOUNCE HANDLER (Hub Side Discovery Layer)
# ---------------------------------------------------------------------------
# This handler receives announcements on your custom aspect and registers
# the nodes' public keys into Reticulum's identity cache natively
class NodeDiscoveryHandler:
    def __init__(self, aspect_filter):
        self.aspect_filter = aspect_filter

    def received_announce(self, destination_hash, announced_identity, app_data):
        if app_data is None:
            return
        try:
            data_str = (
                app_data.decode("utf-8")
                if isinstance(app_data, (bytes, bytearray))
                else str(app_data)
            )
            # Logs peer discovery natively at notice level
            RNS.log(
                f"[DISCOVERY] Learned identity for node: {RNS.prettyhexrep(destination_hash)} | Metadata: {data_str}",
                RNS.LOG_NOTICE,
            )
        except Exception as e:
            RNS.log(
                f"[DISCOVERY ERROR] Failed parsing node announcement: {e}",
                RNS.LOG_WARNING,
            )


# ---------------------------------------------------------------------------
# CENTRAL CORE ARCHITECTURE
# ---------------------------------------------------------------------------
class FarmLXMFHub:
    def __init__(self):
        # Support alternative config directories via standard -c or --config arguments
        config_dir = None
        for i, arg in enumerate(sys.argv):
            if arg in ("--config", "-c") and i + 1 < len(sys.argv):
                config_dir = sys.argv[i + 1]
                break

        # Standard initialization allows dynamic sharing or standalone mode on host
        self.reticulum = RNS.Reticulum(configdir=config_dir, loglevel=RNS.LOG_NOTICE)
        self.identity = self._load_or_create_identity()

        # VERIFIED API INITIALIZATION
        self.lxm_router = LXMF.LXMRouter(
            identity=self.identity, storagepath="./lxmf_storage"
        )

        # VERIFIED API METHOD SIGNATURE
        self.lxm_router.register_delivery_callback(self._on_lxm_received)

        self.hub_addr_hex = RNS.prettyhexrep(self.identity.hash)
        display_string = f"<Hub> {self.hub_addr_hex}"

        # VERIFIED API METHOD SIGNATURE
        self.lxmf_local_target = self.lxm_router.register_delivery_identity(
            self.identity, display_name=display_string
        )

        # Discovery Announcement Destination (Matches node config: farm.gateway_commands)
        self.cmd_dest = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            "farm",
            "gateway_commands",
        )

        # Register standard global announce handler to capture and cache ESP32 node public keys
        self.discovery_handler = NodeDiscoveryHandler(
            aspect_filter="farm.gateway_commands"
        )
        RNS.Transport.register_announce_handler(self.discovery_handler)

        RNS.log(
            f"[CORE INIT] Unified LXMF Ingestion Target Ready: <{self.hub_addr_hex}>",
            RNS.LOG_NOTICE,
        )
        self.lxm_router.announce(self.lxmf_local_target.hash)
        self.cmd_dest.announce(app_data=b"agronomi")

    def _load_or_create_identity(self) -> RNS.Identity:
        if os.path.exists(IDENTITY_PATH):
            try:
                ident = RNS.Identity.from_file(IDENTITY_PATH)
                if ident:
                    RNS.log(
                        f"[CORE] Loaded root system identity: {RNS.prettyhexrep(ident.hash)}",
                        RNS.LOG_NOTICE,
                    )
                    return ident
            except Exception as e:
                RNS.log(
                    f"[WARN] Cryptographic read structural failure: {e}",
                    RNS.LOG_WARNING,
                )
        ident = RNS.Identity()
        ident.to_file(IDENTITY_PATH)
        RNS.log(
            f"[CORE] Minted new system identity hash: {RNS.prettyhexrep(ident.hash)}",
            RNS.LOG_NOTICE,
        )
        return ident

    def _on_lxm_received(self, lxm_message):
        """Callback executed by LXMRouter when a verified LXM message is delivered."""
        # FAILSAFE DIAGNOSTIC LOG (Bypasses all routing, DB, and JSON parsing checks)
        RNS.log("[DEBUG HUB] _on_lxm_received callback triggered!", RNS.LOG_ERROR)
        try:
            # Standard LXMF: Read the msgpacked fields dictionary natively (bypasses JSON entirely)
            fields = lxm_message.fields

            # Robust fallback to support legacy JSON in message content if needed
            if not fields and lxm_message.content:
                try:
                    raw_payload = lxm_message.content_as_string()
                    if "{" in raw_payload:
                        raw_payload = raw_payload[
                            raw_payload.find("{") : raw_payload.rfind("}") + 1
                        ]
                    fields = json.loads(raw_payload)
                except Exception:
                    pass

            if fields and "dev_id" in fields:
                src_hex = lxm_message.source_hash.hex()
                RNS.log(
                    f"[TELEMETRY INGEST] Decoded LXM fields from device: {fields['dev_id']} | Source: <{src_hex}>",
                    RNS.LOG_NOTICE,
                )
                self._write_telemetry_to_db(fields, src_hex)
        except Exception as e:
            RNS.log(
                f"[DROP] Failed parsing incoming LXM frame payload: {e}", RNS.LOG_ERROR
            )

    def _write_telemetry_to_db(self, data, source_hex: str):
        now_str = datetime.now().isoformat()
        node_id = data["dev_id"]

        # Support both compact and verbose keys dynamically
        device_type = data.get("type", data.get("device_type", "support_node"))
        firmware_version = data.get("fw", data.get("fw_ver"))
        rns_interface = data.get("if", data.get("rns_interface", "wifi"))
        battery_level = data.get("bat", data.get("bat_v", -1.0))

        try:
            conn = get_db()
            with conn:
                conn.execute(
                    """
                    INSERT INTO sensor_nodes (node_id, name, last_seen, battery_level)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        last_seen = excluded.last_seen,
                        battery_level = excluded.battery_level
                """,
                    (node_id, node_id, now_str, battery_level),
                )

                conn.execute(
                    """
                    INSERT INTO hardware_devices (
                        device_id,
                        device_type,
                        node_id,
                        rns_identity_hash,
                        rns_destination_hash,
                        rns_interface,
                        firmware_version,
                        last_seen
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id) DO UPDATE SET
                        rns_destination_hash = excluded.rns_destination_hash,
                        rns_interface = excluded.rns_interface,
                        last_seen = excluded.last_seen
                """,
                    (
                        node_id,
                        device_type,
                        node_id,
                        source_hex,
                        source_hex,
                        rns_interface,
                        firmware_version,
                        now_str,
                    ),
                )

                # Record battery voltage (always present)
                conn.execute(
                    """
                    INSERT INTO sensor_readings (node_id, reading_type, value, unit, recorded_at)
                    VALUES (?, 'battery_voltage', ?, 'V', ?)
                """,
                    (node_id, battery_level, now_str),
                )

                # Extract nested readings dictionary (or fallback directly to flat dictionary)
                readings = (
                    data.get("readings", {})
                    if isinstance(data.get("readings"), dict)
                    else {}
                )

                # --- 1. SN-AIR-01 Node Readings ---
                # Check both compact and verbose keys
                air_temp = readings.get(
                    "temp", readings.get("air_temp_c", data.get("temp"))
                )
                air_temp_valid = readings.get("air_temp_valid", air_temp is not None)
                if air_temp_valid and air_temp is not None:
                    conn.execute(
                        """
                        INSERT INTO sensor_readings (node_id, reading_type, value, unit, recorded_at)
                        VALUES (?, 'air_temperature', ?, 'C', ?)
                    """,
                        (node_id, air_temp, now_str),
                    )

                air_hum = readings.get(
                    "hum", readings.get("air_humidity_pct", data.get("hum"))
                )
                air_hum_valid = readings.get("air_humidity_valid", air_hum is not None)
                if air_hum_valid and air_hum is not None:
                    conn.execute(
                        """
                        INSERT INTO sensor_readings (node_id, reading_type, value, unit, recorded_at)
                        VALUES (?, 'air_humidity', ?, '%', ?)
                    """,
                        (node_id, air_hum, now_str),
                    )

                # --- 2. SN-SOIL-01 Node Readings ---
                soil_moisture = readings.get("soil_moisture_pct", data.get("moist"))
                if soil_moisture is not None and soil_moisture >= 0.0:
                    conn.execute(
                        """
                        INSERT INTO sensor_readings (node_id, reading_type, value, unit, recorded_at)
                        VALUES (?, 'soil_moisture', ?, '%', ?)
                    """,
                        (node_id, soil_moisture, now_str),
                    )

                soil_temp = readings.get("soil_temp_c", data.get("soil_temp"))
                soil_temp_valid = readings.get("soil_temp_valid", soil_temp is not None)
                if soil_temp_valid and soil_temp is not None:
                    conn.execute(
                        """
                        INSERT INTO sensor_readings (node_id, reading_type, value, unit, recorded_at)
                        VALUES (?, 'soil_temperature', ?, 'C', ?)
                    """,
                        (node_id, soil_temp, now_str),
                    )

            # ALIGNED LOGGING: Explicitly set to LOG_NOTICE
            RNS.log(
                f"[DB Sync] Successfully synchronized telemetry entries for {node_id}",
                RNS.LOG_NOTICE,
            )
        except Exception as e:
            RNS.log(
                f"[DB ERROR] Ingestion relational tracking failure: {e}", RNS.LOG_ERROR
            )


if __name__ == "__main__":
    setup_hub_logging()
    hub_app = FarmLXMFHub()

    # Pass the local delivery target directly to the dispatcher
    dispatcher = OutboundCommandDispatcher(
        hub_app.lxm_router, hub_app.lxmf_local_target
    )
    dispatch_thread = threading.Thread(target=dispatcher.poll_loop, daemon=True)
    dispatch_thread.start()

    try:
        while True:
            time.sleep(30)
            # Announce the LXMF target so nodes can consistently resolve routing paths
            hub_app.lxm_router.announce(hub_app.lxmf_local_target.hash)
            # Announce the command channel destination so the nodes can autoprovision
            hub_app.cmd_dest.announce(app_data=b"agronomi")
            # ALIGNED LOGGING: Explicitly set to LOG_NOTICE
            RNS.log(
                "[RNS Shared Daemon] Dispatched standard LXMF target and command channel announces.",
                RNS.LOG_NOTICE,
            )
    except KeyboardInterrupt:
        RNS.log(
            "System shutdown operation called. Closing active processing slots...",
            RNS.LOG_NOTICE,
        )
        dispatcher.running = False
        sys.exit(0)
