"""
reticulum_ingest.py — Reticulum telemetry receiver & SQLite ingestion daemon

AgroNomi Hardware Peripherals Layer

Architecture
~~~~~~~~~~~~
This daemon runs on the farm hub and speaks Reticulum over LoRa/WiFi/etc.
In the µReticulum-native architecture, ALL nodes speak RNS directly — there
is no BLE bridge. Routing is via RNS destination hashes (discovered through
announces), not BLE MACs. OTA firmware updates are handled by ``rncp``/``rngit``
utilities, not a custom Python scheduler.

**SINGLE destinations and shared instances**

This daemon works correctly whether it runs as a standalone Reticulum
instance or as a client of a shared instance (rnsd / MeshChat / Sideband).
When ``register_destination()`` is called for a SINGLE destination, the
shared instance automatically learns about it and adds it to its path
table, so incoming packets are routed to the correct local client.

Per the Reticulum manual: *"A single Reticulum Identity can create
multiple destinations. … When a destination is registered, the shared
instance is informed, and packets arriving for that destination are
forwarded to the local client."*

Running standalone is perfectly fine (and simpler), but NOT required.
If the script detects a shared-instance connection it logs an informational
message and continues.  If packets ever fail to arrive, the fix is to
set ``share_instance=No`` in ``~/.reticulum/config`` and restart.

**Telemetry path (SINGLE destination with announce)**
  Gateways send sensor JSON to the ``farm.telemetry_readings``
  SINGLE destination.  This uses a SINGLE destination (not PLAIN) because
  PLAIN destinations are invisible to a shared Reticulum instance — the
  instance daemon has no way to route PLAIN packets to the correct local
  client.  SINGLE destinations, when announced, are visible to the
  instance and properly routed.

  Proof strategy is PROVE_ALL so that senders receive delivery
  confirmation — this validates the shared-instance routing path and
  keeps path table entries alive.  Without proofs, there is no feedback
  when packets are lost or silently dropped due to decryption failures.

  The hub calls ``announce()`` at startup (with app_data identifying the
  service) and re-announces every 30 seconds so newly started gateways
  discover it automatically via RNS path/announce propagation.  Gateways
  do NOT need to manually copy-paste any destination hash — they discover
  it through announce-based path resolution using
  ``Transport.request_path()`` and ``Identity.recall()``.

**Command ACK path (SINGLE destination)**
  Gateways send authenticated ACKs for actuator commands to the
  ``farm.commands_control`` SINGLE destination.  This requires the
  hub's identity so that encryption and proof-of-origin work correctly.
  Both the telemetry and command-ACK destinations share the SAME identity
  because they represent the same logical entity (the hub).  Per the
  Reticulum manual: *"Identity = Entity"* — using one identity for
  multiple destinations on the same host is the intended pattern.

  Proof strategy is PROVE_ALL so gateways receive delivery confirmation
  for their ACKs, which validates the routing path.

**Command dispatch (outbound SINGLE)**
  Pending commands are dispatched to gateway SINGLE destinations.  The
  gateway must have announced first so its identity is discoverable via
  ``RNS.Identity.recall()``.  Before recalling, the dispatcher actively
  requests the path via ``RNS.Transport.request_path()`` with retry logic
  to handle gateways whose announces have expired from the path table.
  Outbound command packets request proof of delivery (PROVE_ALL) so the
  hub can confirm the gateway received the command.

Flow:
  1. Hub starts → creates SINGLE telemetry dest + SINGLE command-ACK dest.
  2. Hub announces both destinations (with app_data) so the shared
     instance and gateways discover them automatically.
  3. Gateways discover the hub via RNS announce/path resolution and send
     telemetry JSON to ``farm.telemetry_readings`` (SINGLE).
  4. Gateways send command ACKs to ``farm.commands_control`` (SINGLE).
  5. Hub polls ``actuator_commands`` for pending rows and dispatches them
     to the appropriate gateway's SINGLE destination on
     ``farm.gateway_commands``.
  6. Hub registers a ``GatewayAnnounceHandler`` that listens for gateway
     announces on ``farm.gateway_commands``. When a gateway announces
     (with app_data ``agronomi-gateway:GW-MIMI-01``), the handler
     auto-provisions the ``reticulum_gateways`` table with the discovered
     destination hash, eliminating manual hash entry.

Why SINGLE instead of PLAIN:
  PLAIN destinations are invisible to a shared Reticulum instance.  The
  instance daemon doesn't know which local client registered a PLAIN
  destination, so it can't route incoming PLAIN packets to the right
  client.  SINGLE destinations, when announced, are visible to the
  instance — it learns the path and can forward packets correctly.

OTA Firmware Updates:
    In the µR-native architecture, OTA is no longer handled by a custom
    Python scheduler.  Use the standard RNS utilities ``rncp`` and ``rngit``
    to push firmware images to nodes over the RNS network.

Dependencies:
    pip install RNS
"""

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import RNS

# ---------------------------------------------------------------------------
# Increase file descriptor limit to prevent RNS "too many open files" errors.
# RNS Transport stores announce cache as individual files and can exhaust
# the default macOS limit (256) with many gateways announcing frequently.
try:
    import resource

    resource.setrlimit(resource.RLIMIT_NOFILE, (65536, 65536))
except (ImportError, ValueError, OSError):
    pass  # resource module not available on all platforms

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
LOG_FILE = os.environ.get("AGRONOMI_LOG", os.path.expanduser("~/agronomi.log"))
DB_PATH = os.environ.get("DB_PATH", "./farm_data.db")
IDENTITY_PATH = "./farm_hub.identity"
TELEMETRY_APP = "farm"
TELEMETRY_ASPECT = "telemetry_readings"
COMMAND_APP = "farm"
COMMAND_ASPECT = "commands_control"  # Hub's IN aspect for ACKs from gateways
GATEWAY_COMMAND_ASPECT = "gateway_commands"  # Aspect gateways listen on for commands

# App data included in announces so listeners can verify service identity
HUB_APP_DATA = b"AgroNomi Hub v1.0"


# ---------------------------------------------------------------------------
# LOGGING — single file, always visible, no hunting
# ---------------------------------------------------------------------------
def setup_hub_logging():
    """Set up a single log file that captures everything.
    Writes all RNS.log() output AND our own log messages to ~/agronomi.log.
    RNS uses its own logging system, so we patch stdout to also write to the file."""
    import io

    # Open the unified log file
    log_fh = open(LOG_FILE, "a")

    # Tee stdout so all RNS.log() output (which goes to stdout)
    # also lands in our log file
    class TeeStream:
        def __init__(self, original, log_file):
            self._original = original
            self._log_file = log_file

        def write(self, data):
            self._original.write(data)
            if data and data.strip():
                self._log_file.write(data if data.endswith("\n") else data + "\n")
                self._log_file.flush()

        def flush(self):
            self._original.flush()
            self._log_file.flush()

        def fileno(self):
            return self._original.fileno()

    sys.stdout = TeeStream(sys.stdout, log_fh)
    sys.stderr = TeeStream(sys.stderr, log_fh)
    print(f"[HUB] === All logs now go to {LOG_FILE} ===")


# Path request retry configuration for command dispatch
PATH_REQUEST_RETRIES = 3
PATH_REQUEST_RETRY_DELAY = 2  # seconds between retries

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

# Full schema DDL — creates all tables the ingest daemon needs.
# This ensures a fresh DB works without needing a separate migration step.
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


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Create all tables if they don't exist — ensures a fresh DB works.
    conn.executescript(_SCHEMA_DDL)
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn: sqlite3.Connection):
    """Apply schema migrations for columns added after initial deployment.

    Handles:
    - Adding rns_identity_hash and rns_interface columns to hardware_devices
    - Adding rns_destination_hash column to hardware_devices
    - Adding cmd_value_text and last_retry_at columns to actuator_commands
    - Expanding the cmd_type CHECK constraint to include ota_request, ota_abort
    - Dropping the obsolete ble_link_log table
    - Updating device_type CHECK to add 'vision_node' and 'piw_gateway'
    - Updating gateway_platform CHECK to add 'esp32c6'

    SQLite doesn't support ALTER TABLE to modify CHECK constraints, so if the
    constraint needs updating, we do a full table rebuild.
    """
    # --- hardware_devices migrations ---
    cursor = conn.execute("PRAGMA table_info(hardware_devices)")
    hw_columns = [row[1] for row in cursor.fetchall()]

    if "rns_identity_hash" not in hw_columns:
        RNS.log(
            "[DB] Adding rns_identity_hash column to hardware_devices", RNS.LOG_INFO
        )
        conn.execute("ALTER TABLE hardware_devices ADD COLUMN rns_identity_hash TEXT")

    if "rns_interface" not in hw_columns:
        RNS.log("[DB] Adding rns_interface column to hardware_devices", RNS.LOG_INFO)
        conn.execute(
            "ALTER TABLE hardware_devices ADD COLUMN rns_interface TEXT DEFAULT 'ble'"
        )

    if "rns_destination_hash" not in hw_columns:
        RNS.log(
            "[DB] Adding rns_destination_hash column to hardware_devices", RNS.LOG_INFO
        )
        conn.execute(
            "ALTER TABLE hardware_devices ADD COLUMN rns_destination_hash TEXT"
        )

    # Note: ble_mac and ble_target_gateway columns are left in place if they
    # exist (SQLite < 3.35.0 doesn't support DROP COLUMN).  They are no longer
    # used by the application code.

    # --- actuator_commands migrations ---
    cursor = conn.execute("PRAGMA table_info(actuator_commands)")
    columns = [row[1] for row in cursor.fetchall()]

    if "cmd_value_text" not in columns:
        RNS.log("[DB] Adding cmd_value_text column to actuator_commands", RNS.LOG_INFO)
        conn.execute("ALTER TABLE actuator_commands ADD COLUMN cmd_value_text TEXT")

    if "last_retry_at" not in columns:
        RNS.log("[DB] Adding last_retry_at column to actuator_commands", RNS.LOG_INFO)
        conn.execute("ALTER TABLE actuator_commands ADD COLUMN last_retry_at TEXT")

    # Check if the CHECK constraint includes ota_request
    # We need to check the table schema SQL
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='actuator_commands'"
    )
    row = cursor.fetchone()
    if row and row[0]:
        schema_sql = row[0]
        needs_rebuild = (
            "ota_request" not in schema_sql or "transferring" not in schema_sql
        )
        if needs_rebuild:
            RNS.log(
                "[DB] Rebuilding actuator_commands with expanded constraints",
                RNS.LOG_INFO,
            )
            # Full table rebuild to update CHECK constraint
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS actuator_commands_new (
                    cmd_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id       TEXT NOT NULL,
                    cmd_type        TEXT CHECK(cmd_type IN
                                        ('pump_on','pump_off','vent_open','vent_close',
                                         'shade_pct','fan_on','fan_off',
                                         'ota_request','ota_abort')),
                    cmd_value       REAL,
                    cmd_value_text  TEXT,
                    requested_at    TEXT,
                    executed_at     TEXT,
                    status          TEXT DEFAULT 'pending'
                                    CHECK(status IN
                                        ('pending','transferring','sent','acknowledged','failed','expired')),
                    retry_count     INTEGER DEFAULT 0,
                    last_retry_at   TEXT,
                    error_message   TEXT
                );
                INSERT OR IGNORE INTO actuator_commands_new
                    SELECT cmd_id, device_id, cmd_type, cmd_value, cmd_value_text,
                           requested_at, executed_at, status, retry_count, last_retry_at,
                           error_message
                    FROM actuator_commands;
                DROP TABLE actuator_commands;
                ALTER TABLE actuator_commands_new RENAME TO actuator_commands;
            """)

    # --- hardware_devices: rebuild if CHECK constraints need updating ---
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='hardware_devices'"
    )
    row = cursor.fetchone()
    if row and row[0]:
        schema_sql = row[0]
        needs_rebuild = "vision_node" not in schema_sql
        if needs_rebuild:
            RNS.log(
                "[DB] Rebuilding hardware_devices with expanded device_type and rns columns",
                RNS.LOG_INFO,
            )
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS hardware_devices_new (
                    device_id           TEXT PRIMARY KEY,
                    device_type         TEXT CHECK(device_type IN
                                            ('gateway','soil_node','air_node',
                                             'pump_node','gh_actuator',
                                             'vision_node','piw_gateway')),
                    node_id             TEXT,
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
                INSERT OR IGNORE INTO hardware_devices_new
                    SELECT device_id, device_type, node_id,
                           rns_identity_hash, NULL,
                           rns_interface,
                           firmware_version, hardware_revision, battery_type,
                           install_date, status, last_seen
                    FROM hardware_devices;
                DROP TABLE hardware_devices;
                ALTER TABLE hardware_devices_new RENAME TO hardware_devices;
            """)

    # --- Drop obsolete ble_link_log table ---
    conn.execute("DROP TABLE IF EXISTS ble_link_log")
    RNS.log("[DB] Dropped ble_link_log table if it existed", RNS.LOG_INFO)

    conn.commit()


def record_telemetry(
    device_id: str,
    readings: dict,
    battery_v: Optional[float] = None,
    rns_interface: str = "ble",
    device_type: Optional[str] = None,
    fw_ver: Optional[str] = None,
    gateway_id: Optional[str] = None,
):
    """Write parsed sensor readings into sensor_readings table.

    In the µR-native architecture, gateway_id is the device's own RNS name
    (not a BLE gateway).  Routing is via RNS destination hashes, not BLE MACs.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        row = conn.execute(
            "SELECT node_id FROM hardware_devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        node_id = row["node_id"] if row else device_id

        # Auto-provision: if this device_id hasn't been seen in sensor_nodes yet,
        # create placeholder rows so FK integrity is maintained.
        existing = conn.execute(
            "SELECT node_id FROM sensor_nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if not existing:
            # Look up a human-readable name from sensor_nodes if it was auto-provisioned
            # by GatewayAnnounceHandler; otherwise fall back to device_id itself.
            name_row = conn.execute(
                "SELECT name FROM sensor_nodes WHERE node_id = ?", (device_id,)
            ).fetchone()
            node_name = name_row["name"] if name_row and name_row["name"] else device_id
            conn.execute(
                "INSERT OR IGNORE INTO sensor_nodes (node_id, name) VALUES (?, ?)",
                (node_id, node_name),
            )
            conn.commit()
            RNS.log(f"[DB] Auto-provisioned sensor_nodes for {device_id}", RNS.LOG_INFO)

        # Auto-provision hardware_devices if not present
        hw_existing = conn.execute(
            "SELECT device_id FROM hardware_devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if not hw_existing:
            resolved_device_type = device_type or _derive_device_type(device_id)
            conn.execute(
                """INSERT OR IGNORE INTO hardware_devices
                   (device_id, device_type, node_id, firmware_version,
                    rns_interface, status)
                   VALUES (?, ?, ?, ?, ?, 'active')""",
                (
                    device_id,
                    resolved_device_type,
                    device_id,
                    fw_ver or "0.0.0",
                    rns_interface,
                ),
            )
            conn.commit()
            RNS.log(
                f"[DB] Auto-provisioned hardware_devices for {device_id}", RNS.LOG_INFO
            )

        conn.execute(
            "UPDATE sensor_nodes SET last_seen = ? WHERE node_id = ?", (now, node_id)
        )
        conn.execute(
            "UPDATE hardware_devices SET last_seen = ? WHERE device_id = ?",
            (now, device_id),
        )

        # Enrich existing hardware_devices with auto-discovered fields
        enrich_parts = []
        enrich_params = []
        if rns_interface:
            cur = conn.execute(
                "SELECT rns_interface FROM hardware_devices WHERE device_id = ? AND rns_interface IS NULL",
                (device_id,),
            ).fetchone()
            if cur:
                enrich_parts.append("rns_interface = ?")
                enrich_params.append(rns_interface)
        if fw_ver:
            cur = conn.execute(
                "SELECT firmware_version FROM hardware_devices WHERE device_id = ? AND (firmware_version IS NULL OR firmware_version = '0.0.0')",
                (device_id,),
            ).fetchone()
            if cur:
                enrich_parts.append("firmware_version = ?")
                enrich_params.append(fw_ver)
        if enrich_parts:
            enrich_params.append(device_id)
            set_clause = ", ".join(enrich_parts)
            conn.execute(
                f"UPDATE hardware_devices SET {set_clause} WHERE device_id = ?",
                enrich_params,
            )
            conn.commit()
            RNS.log(
                f"[DB] Enriched {device_id}: {set_clause}",
                RNS.LOG_INFO,
            )

        # Insert battery_v as its own reading if provided
        all_readings = dict(readings)
        if battery_v is not None:
            all_readings["battery_v"] = battery_v

        for reading_type, value in all_readings.items():
            if value is None:
                continue
            conn.execute(
                """INSERT INTO sensor_readings (node_id, reading_type, value, unit, recorded_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (node_id, reading_type, float(value), _get_unit(reading_type), now),
            )
        conn.commit()

    if all_readings:
        RNS.log(f"[DB] {len(all_readings)} readings from {device_id}", RNS.LOG_INFO)
    else:
        RNS.log(f"[DB] {device_id} heartbeat (no readings)", RNS.LOG_INFO)


def _get_unit(reading_type: str) -> str:
    return {
        "soil_moisture": "%",
        "soil_moisture_pct": "%",
        "soil_temperature": "°C",
        "soil_temperature_c": "°C",
        "air_temperature": "°C",
        "air_humidity": "%",
        "co2_ppm": "ppm",
        "light_lux": "lux",
        "battery_level": "%",
        "battery_v": "V",
        "flow_l": "L",
        "pressure_kpa": "kPa",
    }.get(reading_type, "")


def update_actuator_status(cmd_id: int, status: str, error: Optional[str] = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        if status == "acknowledged":
            conn.execute(
                "UPDATE actuator_commands SET status = ?, executed_at = ? WHERE cmd_id = ?",
                (status, now, cmd_id),
            )
        elif status == "failed":
            conn.execute(
                """UPDATE actuator_commands
                   SET status = ?, error_message = ?, retry_count = COALESCE(retry_count, 0) + 1
                   WHERE cmd_id = ?""",
                (status, error, cmd_id),
            )
        else:
            conn.execute(
                "UPDATE actuator_commands SET status = ? WHERE cmd_id = ?",
                (status, cmd_id),
            )
        conn.commit()


# REMOVED: log_ble_meta() — BLE link logging is obsolete in the µR-native architecture


# ---------------------------------------------------------------------------
# IDENTITY MANAGEMENT
# ---------------------------------------------------------------------------


def load_or_create_identity(path: str) -> RNS.Identity:
    """Load identity from file, or create and save a new one.

    This identity is used for the SINGLE destinations (telemetry and
    command-ACK).  SINGLE destinations require an identity for announce-
    based discovery and encryption.

    Both TelemetryDestination and CommandAckDestination share the same
    identity because they represent the same logical entity (the hub).
    Per the Reticulum manual: "A single Reticulum Identity can create
    multiple destinations."  Using one identity for multiple destinations
    on the same host is the intended pattern — identity = entity.
    """
    if os.path.exists(path):
        identity = RNS.Identity.from_file(path)
        if identity is not None:
            RNS.log(f"[ID] Loaded identity from {path}")
            return identity
        RNS.log(f"[WARN] Identity file corrupt, generating new one", RNS.LOG_WARNING)

    identity = RNS.Identity()
    identity.to_file(path)
    RNS.log(f"[ID] Generated new identity, saved to {path}")
    return identity


# ---------------------------------------------------------------------------
# RETICULUM DESTINATIONS
# ---------------------------------------------------------------------------


class TelemetryDestination:
    """SINGLE destination for incoming sensor telemetry.

    Uses ``RNS.Destination.SINGLE`` with the hub's identity so that the
    destination is announced to the Reticulum network.  This works correctly
    whether connected to a shared Reticulum instance (e.g. MeshChat/Sideband)
    or running standalone — ``register_destination()`` for a SINGLE
    destination automatically registers with the shared instance, and the
    instance routes incoming packets via its path table.

    Proof strategy is set to PROVE_ALL so that senders receive delivery
    confirmation. This is important for:
    - Validating that the shared instance routing path is working
    - Letting senders know packets actually arrived
    - Keeping the path table entries alive in the network

    Per the Reticulum documentation: "We configure the destination to
    automatically prove all packets addressed to it. By doing this, RNS
    will automatically generate a proof for each incoming packet and
    transmit it back to the sender of that packet."
    """

    def __init__(self, identity: RNS.Identity):
        self.destination = RNS.Destination(
            identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            TELEMETRY_APP,
            TELEMETRY_ASPECT,
        )
        # PROVE_ALL: send delivery proofs back to senders so they know
        # packets arrived. This follows the Echo example pattern from the
        # Reticulum documentation and helps validate shared-instance routing.
        self.destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        self.destination.set_packet_callback(self.on_packet)
        # Announce with app_data so listeners can verify the service identity
        self.destination.announce(app_data=HUB_APP_DATA)
        RNS.log(
            f"[RNS] Telemetry destination: {RNS.prettyhexrep(self.destination.hash)}"
        )
        RNS.log(f"[RNS] Announced on network — gateways will discover automatically")

    def on_packet(self, data: bytes, packet: RNS.Packet):
        RNS.log(f"[RNS] PACKET RECEIVED on telemetry! {len(data)} bytes", RNS.LOG_INFO)
        RNS.log(
            f"[RNS] Packet destination hash: {RNS.prettyhexrep(packet.destination_hash)}",
            RNS.LOG_DEBUG,
        )
        RNS.log(f"[RNS] Packet type: {packet.packet_type}", RNS.LOG_DEBUG)
        RNS.log(
            f"[RNS] Receiving interface: {packet.receiving_interface}", RNS.LOG_DEBUG
        )
        try:
            payload = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            RNS.log(f"[ERROR] Bad telemetry packet: {e}", RNS.LOG_ERROR)
            return

        device_id = payload.get("dev_id")
        readings = payload.get("readings", {})
        battery_v = payload.get("bat_v")
        gateway_id = payload.get("gateway_id", "unknown")
        rns_interface = payload.get("rns_interface", "ble")
        device_type = payload.get("device_type")
        fw_ver = payload.get("fw_ver")

        if not device_id:
            RNS.log(
                f"[WARN] Malformed telemetry (no dev_id): {payload}", RNS.LOG_WARNING
            )
            return

        # Empty readings dict is valid — sensor may send metadata-only packets
        # (e.g. deep-sleep wakeups where sensor read failed).
        # Only reject if readings key is missing entirely.
        if readings is None:
            RNS.log(
                f"[WARN] Malformed telemetry (no readings key): {payload}",
                RNS.LOG_WARNING,
            )
            return

        RNS.log(f"[TELEMETRY] {device_id} via {gateway_id}: {readings}", RNS.LOG_INFO)
        if battery_v is not None:
            RNS.log(f"[TELEMETRY] {device_id} battery: {battery_v}V", RNS.LOG_INFO)

        try:
            record_telemetry(
                device_id,
                readings,
                battery_v,
                rns_interface=rns_interface,
                device_type=device_type,
                fw_ver=fw_ver,
                gateway_id=gateway_id,
            )
            RNS.log(f"[TELEMETRY] {device_id} written to DB", RNS.LOG_INFO)
        except Exception as e:
            RNS.log(f"[ERROR] DB write failed for {device_id}: {e}", RNS.LOG_ERROR)
            return


class CommandAckDestination:
    """Receives actuator acknowledgements from field gateways.

    This uses a SINGLE destination so that gateways must encrypt to
    the hub's identity and provide proof-of-origin.  Commands need
    authentication; telemetry does not.

    Note: This destination shares the same identity as TelemetryDestination.
    This is intentional — both destinations represent the same logical
    entity (the hub).  Per the Reticulum manual: "Identity = Entity."
    Using one identity for multiple destinations on the same host is the
    standard pattern.

    Proof strategy is set to PROVE_ALL so senders know their ACKs arrived.
    """

    def __init__(self, identity: RNS.Identity):
        self.destination = RNS.Destination(
            identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            COMMAND_APP,
            COMMAND_ASPECT,
        )
        # Command ACKs should prove delivery so gateways know they were received
        self.destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        self.destination.set_packet_callback(self.on_packet)
        # Announce with app_data so listeners can verify the service identity
        self.destination.announce(app_data=HUB_APP_DATA)
        RNS.log(
            f"[RNS] Command ACK SINGLE destination: {RNS.prettyhexrep(self.destination.hash)}"
        )

    def on_packet(self, data: bytes, packet: RNS.Packet):
        RNS.log(
            f"[RNS] PACKET RECEIVED on command ACK! {len(data)} bytes", RNS.LOG_INFO
        )
        RNS.log(
            f"[RNS] Packet destination hash: {RNS.prettyhexrep(packet.destination_hash)}",
            RNS.LOG_DEBUG,
        )
        RNS.log(f"[RNS] Packet type: {packet.packet_type}", RNS.LOG_DEBUG)
        RNS.log(
            f"[RNS] Receiving interface: {packet.receiving_interface}", RNS.LOG_DEBUG
        )
        try:
            ack = json.loads(data.decode("utf-8"))
            cmd_id = ack.get("cmd_id")
            status = ack.get("status")
            error = ack.get("error")
            if cmd_id is not None and status:
                update_actuator_status(int(cmd_id), status, error)

                # Track rns_interface if present in ACK payload
                if ack.get("rns_interface"):
                    with get_db() as conn:
                        row = conn.execute(
                            "SELECT device_id FROM actuator_commands WHERE cmd_id=?",
                            (cmd_id,),
                        ).fetchone()
                        if row:
                            conn.execute(
                                "UPDATE hardware_devices SET rns_interface=? "
                                "WHERE device_id=?",
                                (ack["rns_interface"], row["device_id"]),
                            )
                            conn.commit()

                # OTA ACK: update firmware_version in hardware_devices
                if status == "acknowledged" and ack.get("fw_version"):
                    with get_db() as conn:
                        row = conn.execute(
                            "SELECT device_id FROM actuator_commands WHERE cmd_id=?",
                            (cmd_id,),
                        ).fetchone()
                        if row:
                            conn.execute(
                                "UPDATE hardware_devices SET firmware_version=? "
                                "WHERE device_id=?",
                                (ack["fw_version"], row["device_id"]),
                            )
                            conn.commit()
                            RNS.log(
                                f"[OTA] {row['device_id']} updated to "
                                f"{ack['fw_version']}",
                                RNS.LOG_INFO,
                            )

                RNS.log(f"[RNS] Cmd {cmd_id} => {status}", RNS.LOG_INFO)
            else:
                RNS.log(f"[WARN] Malformed ACK: {ack}", RNS.LOG_WARNING)
        except Exception as e:
            RNS.log(f"[ERROR] ACK processing failed: {e}", RNS.LOG_ERROR)


# ---------------------------------------------------------------------------
# HELPER — derive device_type from node_id prefix
# ---------------------------------------------------------------------------


def _derive_device_type(node_id: str) -> str:
    """Map a node_id prefix to the corresponding hardware_devices device_type.

    Prefix conventions:
        SN-SOIL-*       → soil_node
        SN-AIR-*        → air_node
        AN-PUMP-*        → pump_node
        AN-GREENHOUSE-*  → gh_actuator
        RN-* or GW-*     → gateway

    Falls back to 'gateway' if the prefix is unrecognised.
    """
    if node_id.startswith("SN-SOIL-"):
        return "soil_node"
    elif node_id.startswith("SN-AIR-"):
        return "air_node"
    elif node_id.startswith("AN-PUMP-"):
        return "pump_node"
    elif node_id.startswith("AN-GREENHOUSE-"):
        return "gh_actuator"
    elif node_id.startswith("RN-") or node_id.startswith("GW-"):
        return "gateway"
    else:
        return "gateway"


# ---------------------------------------------------------------------------
# GATEWAY ANNOUNCE HANDLER — auto-discover gateways via RNS announces
# ---------------------------------------------------------------------------


class GatewayAnnounceHandler:
    """Discovers field gateways, sensor nodes, AND actuator nodes via RNS announces
    and auto-provisions them in the DB.

    In the µR-native architecture, ALL nodes announce on the network. This handler
    processes ``agronomi-gateway:``, ``agronomi-sensor:``, and ``agronomi-actuator:``
    prefixes.

    When a node announces its command destination (farm.gateway_commands),
    this handler captures the destination hash and identity, then updates
    reticulum_gateways / hardware_devices so the CommandDispatcher can reach it.

    This eliminates the need for manually copying destination hashes into the DB.
    """

    aspect_filter = "farm.gateway_commands"

    def received_announce(self, destination_hash, announced_identity, app_data):
        gateway_id = None
        is_sensor = False
        is_actuator = False
        if app_data:
            try:
                text = app_data.decode("utf-8", errors="replace")
                # app_data format examples:
                #   "agronomi-gateway:GW-MIMI-01"
                #   "agronomi-sensor:SN-SOIL-01"
                #   "agronomi-actuator:AN-PUMP-01"
                if text.startswith("agronomi-gateway:"):
                    gateway_id = text.split(":", 1)[1]
                elif text.startswith("agronomi-sensor:"):
                    gateway_id = text.split(":", 1)[1]
                    is_sensor = True
                elif text.startswith("agronomi-actuator:"):
                    gateway_id = text.split(":", 1)[1]
                    is_actuator = True
            except Exception:
                pass

        # RNS.prettyhexrep() returns '<hex>' with angle brackets.
        # Strip them so bytes.fromhex() works when dispatching commands.
        dest_hash_hex = RNS.prettyhexrep(destination_hash).strip("<>")
        node_kind = (
            "Actuator" if is_actuator else ("Sensor" if is_sensor else "Gateway")
        )
        RNS.log(
            f"[GW-DISCOV] {node_kind} announce: {dest_hash_hex} id={gateway_id}",
            RNS.LOG_INFO,
        )

        # Store identity so we can recall it later when sending commands.
        # RNS already stores it, but let's make sure.
        RNS.Identity.recall(destination_hash)

        # Derive RNS identity hash from the announced identity
        rns_identity_hash = None
        if announced_identity:
            rns_identity_hash = RNS.prettyhexrep(announced_identity.hash).strip("<>")

        # Update DB
        if not gateway_id:
            RNS.log(
                "[GW-DISCOV] No id in app_data — cannot auto-provision",
                RNS.LOG_WARNING,
            )
            return

        # Derive device_type from the node_id prefix
        device_type = _derive_device_type(gateway_id)

        try:
            with get_db() as conn:
                # --- All node types: upsert into hardware_devices ---
                conn.execute(
                    """
                    INSERT INTO hardware_devices
                        (device_id, device_type, rns_identity_hash,
                         rns_destination_hash, rns_interface, status, install_date)
                    VALUES (?, ?, ?, ?, ?, 'active', datetime('now'))
                    ON CONFLICT(device_id) DO UPDATE SET
                        rns_identity_hash   = COALESCE(excluded.rns_identity_hash, hardware_devices.rns_identity_hash),
                        rns_destination_hash = COALESCE(excluded.rns_destination_hash, hardware_devices.rns_destination_hash),
                        status = 'active',
                        last_seen = datetime('now')
                    """,
                    (
                        gateway_id,
                        device_type,
                        rns_identity_hash,
                        dest_hash_hex,
                        "lora" if device_type == "gateway" else "ble",
                    ),
                )
                conn.commit()

                # --- Gateways only: also upsert into reticulum_gateways ---
                if device_type == "gateway":
                    conn.execute(
                        """
                        INSERT INTO reticulum_gateways
                            (gateway_id, device_id, rns_destination_hash, peers_count)
                        VALUES (?, ?, ?, 0)
                        ON CONFLICT(gateway_id) DO UPDATE SET
                            rns_destination_hash = excluded.rns_destination_hash,
                            last_heartbeat = datetime('now')
                        """,
                        (gateway_id, gateway_id, dest_hash_hex),
                    )
                    conn.commit()

                RNS.log(
                    f"[GW-DISCOV] {node_kind} {gateway_id} ({device_type}) announced ({dest_hash_hex})",
                    RNS.LOG_INFO,
                )
        except Exception as e:
            RNS.log(f"[GW-DISCOV] DB update failed: {e}", RNS.LOG_ERROR)


# ---------------------------------------------------------------------------
# COMMAND DISPATCHER
# ---------------------------------------------------------------------------


class CommandDispatcher:
    """
    Polls actuator_commands for pending rows and sends them via Reticulum.

    DB schema assumption:
      reticulum_gateways.rns_destination_hash  — hex string of the *destination*
      hash as reported by RNS.prettyhexrep(dest.hash) when the gateway first
      announces. NOT the identity hash. Store it during commissioning by
      running ``rnstatus`` or reading it from the gateway's own log output.

    Before sending, the dispatcher actively requests the gateway's path via
    ``RNS.Transport.request_path()`` with retry logic, so that gateways whose
    announces have expired from the path table can still be discovered.
    """

    def __init__(self):
        self.running = True

    def run(self):
        while self.running:
            try:
                self._dispatch_pending()
            except Exception as e:
                RNS.log(f"[ERROR] Dispatch loop error: {e}", RNS.LOG_ERROR)
            time.sleep(5)

    def _dispatch_pending(self):
        with get_db() as conn:
            rows = conn.execute(
                """SELECT cmd_id, device_id, cmd_type, cmd_value, cmd_value_text
                   FROM actuator_commands
                   WHERE status = 'pending'
                     AND COALESCE(retry_count, 0) < 3
                   ORDER BY requested_at ASC
                   LIMIT 10"""
            ).fetchall()

            for row in rows:
                cmd_id = row["cmd_id"]
                device_id = row["device_id"]
                cmd_type = row["cmd_type"]

                # OTA requests use RNS Link + Resource (not plain Packet)
                # In µR-native architecture, OTA is now handled by rncp/rngit utilities.
                # The ota_scheduler import is kept for backward compatibility but
                # OTA dispatch should be migrated to rncp/rngit.
                if cmd_type == "ota_request":
                    cmd_value_text = row["cmd_value_text"]
                    if not cmd_value_text:
                        RNS.log(
                            f"[OTA] ota_request cmd {cmd_id} missing cmd_value_text",
                            RNS.LOG_ERROR,
                        )
                        continue

                    gw_row = conn.execute(
                        """SELECT hd.rns_destination_hash
                           FROM hardware_devices hd
                           WHERE hd.device_id = ?""",
                        (device_id,),
                    ).fetchone()

                    if not gw_row or not gw_row["rns_destination_hash"]:
                        RNS.log(
                            f"[WARN] No destination hash for {device_id}",
                            RNS.LOG_WARNING,
                        )
                        continue

                    # Import and call the OTA dispatch function
                    from ota_scheduler import dispatch_ota

                    RNS.log(
                        f"[OTA] Dispatching OTA cmd {cmd_id} for {device_id}",
                        RNS.LOG_INFO,
                    )
                    # Mark as 'transferring' immediately so the dispatcher
                    # doesn't re-dispatch the same cmd on the next poll.
                    conn.execute(
                        "UPDATE actuator_commands SET status='transferring' WHERE cmd_id=?",
                        (cmd_id,),
                    )
                    conn.commit()

                    dispatch_ota(
                        conn,
                        cmd_id,
                        device_id,
                        cmd_value_text,
                        gw_row["rns_destination_hash"],
                    )
                    continue

                # Regular actuator commands use plain RNS Packet
                dest_row = conn.execute(
                    """SELECT rns_destination_hash
                       FROM hardware_devices
                       WHERE device_id = ?""",
                    (device_id,),
                ).fetchone()

                if not dest_row or not dest_row["rns_destination_hash"]:
                    RNS.log(
                        f"[WARN] No destination hash for {device_id}",
                        RNS.LOG_WARNING,
                    )
                    continue

                dest_hash_hex = dest_row["rns_destination_hash"]

                # Look up rns_interface for the target device
                iface_row = conn.execute(
                    "SELECT rns_interface FROM hardware_devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                rns_interface = iface_row["rns_interface"] if iface_row else "ble"

                payload = json.dumps(
                    {
                        "cmd_id": cmd_id,
                        "device_id": device_id,
                        "cmd_type": cmd_type,
                        "cmd_value": row["cmd_value"],
                        "rns_interface": rns_interface,
                        "ts": int(time.time()),
                    }
                ).encode("utf-8")

                self._send_command(conn, cmd_id, dest_hash_hex, payload)

    def _send_command(self, conn, cmd_id: int, dest_hash_hex: str, payload: bytes):
        # Strip angle brackets from RNS.prettyhexrep() if present
        dest_hash_hex = dest_hash_hex.strip("<>")
        try:
            dest_hash = bytes.fromhex(dest_hash_hex)
        except ValueError:
            RNS.log(
                f"[ERROR] Invalid dest hash hex '{dest_hash_hex}' for cmd {cmd_id}",
                RNS.LOG_ERROR,
            )
            return

        # Actively request the path to the gateway before attempting recall.
        # If the gateway's announce has expired from the path table, this
        # triggers path discovery so we can still reach it.
        RNS.Transport.request_path(dest_hash)
        remote_identity = None
        for attempt in range(1, PATH_REQUEST_RETRIES + 1):
            remote_identity = RNS.Identity.recall(dest_hash)
            if remote_identity is not None:
                break
            if attempt < PATH_REQUEST_RETRIES:
                RNS.log(
                    f"[RNS] Path for {dest_hash_hex[:16]}... not yet known "
                    f"(attempt {attempt}/{PATH_REQUEST_RETRIES}), retrying in "
                    f"{PATH_REQUEST_RETRY_DELAY}s...",
                    RNS.LOG_DEBUG,
                )
                time.sleep(PATH_REQUEST_RETRY_DELAY)
                # Re-request in case the first one was lost
                RNS.Transport.request_path(dest_hash)

        if remote_identity is None:
            RNS.log(
                f"[WARN] Identity for {dest_hash_hex[:16]}... not discoverable after "
                f"{PATH_REQUEST_RETRIES} path requests — cmd {cmd_id} deferred",
                RNS.LOG_WARNING,
            )
            return

        try:
            dest = RNS.Destination(
                remote_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                COMMAND_APP,
                GATEWAY_COMMAND_ASPECT,  # matches what gateways listen on
            )
            # Request proof of delivery so we can confirm the gateway
            # received the command.
            dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
            packet = RNS.Packet(dest, payload)
            receipt = packet.send()
            if receipt is not None:
                # Attach a callback so we know when the gateway proves delivery
                def on_delivery_proof(receipt_obj):
                    RNS.log(
                        f"[RNS] Delivery confirmed for cmd {cmd_id} → "
                        f"{dest_hash_hex[:16]}...",
                        RNS.LOG_INFO,
                    )

                receipt.set_delivery_callback(on_delivery_proof)

                conn.execute(
                    "UPDATE actuator_commands SET status = 'sent' WHERE cmd_id = ?",
                    (cmd_id,),
                )
                conn.commit()
                RNS.log(
                    f"[RNS] Dispatched cmd {cmd_id} → {dest_hash_hex[:16]}...",
                    RNS.LOG_INFO,
                )
            else:
                RNS.log(
                    f"[WARN] Packet send returned None for cmd {cmd_id}",
                    RNS.LOG_WARNING,
                )
        except Exception as e:
            RNS.log(f"[ERROR] Send failed for cmd {cmd_id}: {e}", RNS.LOG_ERROR)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def main():
    setup_hub_logging()
    RNS.loglevel = RNS.LOG_INFO
    RNS.log("=== Farm Reticulum Ingest Daemon ===")

    reticulum = RNS.Reticulum()
    if reticulum.is_connected_to_shared_instance:
        RNS.log(
            "[RNS] Connected to shared instance (rnsd).",
            RNS.LOG_INFO,
        )
        RNS.log(
            "[RNS] SINGLE destinations register automatically via "
            "register_destination() — announce-based discovery and packet "
            "routing work through the shared instance.",
            RNS.LOG_INFO,
        )
    else:
        RNS.log(
            "[RNS] Running as standalone instance (owns interfaces directly).",
            RNS.LOG_INFO,
        )
        RNS.log(
            "[RNS] For best results with multiple RNS programs, set "
            "share_instance=Yes in config and run rnsd as a daemon.",
            RNS.LOG_INFO,
        )

    identity = load_or_create_identity(IDENTITY_PATH)

    # Register announce handler so gateway announces auto-populate the DB
    gateway_handler = GatewayAnnounceHandler()
    RNS.Transport.register_announce_handler(gateway_handler)
    RNS.log(
        f"[GW-DISCOV] Registered announce handler for {GatewayAnnounceHandler.aspect_filter}",
        RNS.LOG_INFO,
    )

    telem_dest = TelemetryDestination(identity)
    cmd_ack_dest = CommandAckDestination(identity)

    # Diagnostic: verify destinations are registered in Transport
    telem_hash = telem_dest.destination.hash
    cmd_hash = cmd_ack_dest.destination.hash
    telem_registered = telem_hash in RNS.Transport.destinations_map
    cmd_registered = cmd_hash in RNS.Transport.destinations_map
    RNS.log(
        f"[RNS] Telemetry dest in destinations_map: {telem_registered} "
        f"(hash={RNS.prettyhexrep(telem_hash)})",
        RNS.LOG_INFO,
    )
    RNS.log(
        f"[RNS] Command dest in destinations_map: {cmd_registered} "
        f"(hash={RNS.prettyhexrep(cmd_hash)})",
        RNS.LOG_INFO,
    )
    if not (telem_registered and cmd_registered):
        RNS.log(
            "[ERROR] Destination(s) not registered in Transport! "
            "Packets will NOT be delivered. Check RNS initialization.",
            RNS.LOG_CRITICAL,
        )

    dispatcher = CommandDispatcher()
    dispatch_thread = threading.Thread(
        target=dispatcher.run, daemon=True, name="dispatcher"
    )
    dispatch_thread.start()

    # Start OTA scheduler thread
    # NOTE: In the µR-native architecture, OTA firmware updates are now handled
    # by the standard RNS utilities `rncp` and `rngit`, not a custom Python scheduler.
    # The ota_scheduler import and thread startup are commented out below.
    # If you still need the legacy OTA scheduler for backward compatibility,
    # uncomment these lines.
    RNS.log(
        "[OTA] In µR-native architecture, OTA is handled by rncp/rngit utilities",
        RNS.LOG_INFO,
    )
    # import ota_scheduler
    # ota_scheduler.set_db_module(sys.modules[__name__])
    # ota_thread = threading.Thread(
    #     target=ota_scheduler.run_ota_scheduler, daemon=True, name="ota_scheduler"
    # )
    # ota_thread.start()
    # RNS.log("[OTA] Scheduler thread started", RNS.LOG_INFO)

    RNS.log("=" * 60)
    RNS.log("Farm Reticulum Ingest Daemon is running")
    RNS.log("=" * 60)
    RNS.log(
        f"  Telemetry (SINGLE): {TELEMETRY_APP}.{TELEMETRY_ASPECT}  "
        f"hash: {RNS.prettyhexrep(telem_dest.destination.hash)}"
    )
    RNS.log(
        f"  Commands (SINGLE):  {COMMAND_APP}.{COMMAND_ASPECT}  "
        f"hash: {RNS.prettyhexrep(cmd_ack_dest.destination.hash)}"
    )
    RNS.log("")
    RNS.log(
        "Both destinations announced \u2014 gateways discover them automatically"
        " via RNS path resolution (no manual hash copy-pasting needed)."
    )
    RNS.log(
        f"Gateway discovery: listening for announces on "
        f"{GatewayAnnounceHandler.aspect_filter} \u2014 auto-provisions DB"
    )
    RNS.log("Proof strategy: PROVE_ALL (senders get delivery confirmation)")
    RNS.log("Ctrl-C to exit.")
    try:
        while True:
            time.sleep(30)
            # Periodic self-test: verify destinations are still registered
            telem_ok = telem_hash in RNS.Transport.destinations_map
            cmd_ok = cmd_hash in RNS.Transport.destinations_map
            if not (telem_ok and cmd_ok):
                RNS.log(
                    f"[WARN] Destination registration lost! "
                    f"telemetry={telem_ok} command={cmd_ok}",
                    RNS.LOG_WARNING,
                )
            # Re-announce so newly started gateways can discover us
            telem_dest.destination.announce(app_data=HUB_APP_DATA)
            cmd_ack_dest.destination.announce(app_data=HUB_APP_DATA)
            RNS.log("[RNS] Re-announced destinations on network")
    except KeyboardInterrupt:
        RNS.log("Shutting down...")
        dispatcher.running = False
        dispatch_thread.join(timeout=10)
        RNS.log("Done.")
        # OTA scheduler thread is daemon — exits automatically


if __name__ == "__main__":
    main()
