"""
test_pipeline.py — Reticulum telemetry pipeline integration test
Run on Mac Mini alongside reticulum_ingest.py

Sends realistic sensor payloads via RNS to reticulum_ingest.py,
verifies the DB received sane data.

Uses SINGLE destinations with announce-based discovery — the ingest daemon
announces itself and this test discovers it automatically.

Just start rnsd, then reticulum_ingest.py, then run this test.

The test and ingest daemon share the same DB file
(../documents/farm_data.db by default).

Usage:
    # Terminal 1: start rnsd (shared RNS instance)
    rnsd

    # Terminal 2: start ingest daemon
    cd documents && python3 reticulum_ingest.py

    # Terminal 3: run the test
    cd test_pipeline && python3 test_pipeline.py

Dependencies:
    pip install rns
"""

import argparse
import json
import os
import sqlite3
import sys
import threading
import time

import RNS

# ---------------------------------------------------------------------------
# SCHEMA — production farm_data.db tables used by reticulum_ingest.py
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

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
                             'pump_node','gh_actuator')),
    node_id             TEXT REFERENCES sensor_nodes(node_id),
    ble_mac             TEXT,
    ble_target_gateway  TEXT,
    firmware_version    TEXT,
    hardware_revision   TEXT,
    battery_type        TEXT,
    install_date        TEXT,
    status              TEXT DEFAULT 'active'
                        CHECK(status IN
                            ('active','offline','maintenance','decommissioned'))
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
    device_id       TEXT REFERENCES hardware_devices(device_id),
    cmd_type        TEXT CHECK(cmd_type IN
                        ('pump_on','pump_off','vent_open','vent_close',
                         'shade_pct','fan_on','fan_off')),
    cmd_value       REAL,
    requested_at    TEXT,
    executed_at     TEXT,
    status          TEXT DEFAULT 'pending'
                    CHECK(status IN
                        ('pending','sent','acknowledged','failed','expired')),
    retry_count     INTEGER DEFAULT 0,
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS ble_link_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT,
    gateway_id  TEXT,
    event       TEXT CHECK(event IN
                    ('connected','disconnected','timeout',
                     'rx_packet','tx_packet','rssi_update')),
    rssi        INTEGER,
    recorded_at TEXT
);
"""

# ---------------------------------------------------------------------------
# TEST FIXTURES — realistic payloads matching real node types and field names
# ---------------------------------------------------------------------------

# Each fixture: sensor_node row, hardware_device row, list of payloads to send
TEST_FIXTURES = [
    {
        "node_id": "SN-SOIL-01",
        "node_name": "Soil Node 1 — Livada south",
        "device": {
            "device_id": "SN-SOIL-01",
            "device_type": "soil_node",
            "ble_target_gateway": "GW-MIMI-01",
        },
        # Two payloads — verify readings accumulate, not overwrite
        "payloads": [
            {
                "dev_id": "SN-SOIL-01",
                "gateway_id": "GW-MIMI-01",
                "seq": 12,
                "ts": int(time.time()),
                "fw_ver": "1.0.2",
                "bat_v": 3.82,
                "readings": {
                    "soil_moisture_pct": "42.50",
                    "soil_temperature_c": "14.20",
                },
            },
            {
                "dev_id": "SN-SOIL-01",
                "gateway_id": "GW-MIMI-01",
                "seq": 13,
                "ts": int(time.time()) + 1,
                "fw_ver": "1.0.2",
                "bat_v": 3.81,
                "readings": {
                    "soil_moisture_pct": "43.10",
                    "soil_temperature_c": "14.30",
                },
            },
        ],
        # Expected reading types in DB (bat_v becomes battery_v reading)
        "expected_types": {"soil_moisture_pct", "soil_temperature_c", "battery_v"},
        # Plausibility bounds per reading type
        "bounds": {
            "soil_moisture_pct": (0.0, 100.0),
            "soil_temperature_c": (-10.0, 60.0),
            "battery_v": (2.5, 4.3),
        },
        # How many total readings we expect (2 payloads × (2 readings + 1 bat_v))
        "expected_min_count": 6,
    },
    {
        "node_id": "SN-AIR-01",
        "node_name": "Air Node 1 — Livada north",
        "device": {
            "device_id": "SN-AIR-01",
            "device_type": "air_node",
            "ble_target_gateway": "GW-MIMI-01",
        },
        "payloads": [
            {
                "dev_id": "SN-AIR-01",
                "gateway_id": "GW-MIMI-01",
                "seq": 7,
                "ts": int(time.time()),
                "fw_ver": "1.0.1",
                "bat_v": 3.91,
                "readings": {
                    "air_temperature_c": "18.30",
                    "air_humidity_pct": "67.10",
                },
            },
        ],
        "expected_types": {"air_temperature_c", "air_humidity_pct", "battery_v"},
        "bounds": {
            "air_temperature_c": (-30.0, 60.0),
            "air_humidity_pct": (0.0, 100.0),
            "battery_v": (2.5, 4.3),
        },
        "expected_min_count": 3,
    },
]

# Payloads ingest must silently reject — no crash, no DB writes
REJECT_PAYLOADS = [
    # Missing dev_id
    {"gateway_id": "GW-TEST", "seq": 1, "readings": {"air_temperature_c": "20.0"}},
    # Empty readings dict
    {"dev_id": "SN-SOIL-01", "seq": 99, "readings": {}},
    # Completely empty
    {},
]


# ---------------------------------------------------------------------------
# DB SETUP
# ---------------------------------------------------------------------------


def create_test_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    for fixture in TEST_FIXTURES:
        conn.execute(
            "INSERT OR IGNORE INTO sensor_nodes (node_id, name, location) VALUES (?,?,?)",
            (fixture["node_id"], fixture["node_name"], "Biotop Livada — test"),
        )
        dev = fixture["device"]
        conn.execute(
            """INSERT OR IGNORE INTO hardware_devices
               (device_id, device_type, node_id, ble_target_gateway,
                firmware_version, status)
               VALUES (?,?,?,?,'test','active')""",
            (
                dev["device_id"],
                dev["device_type"],
                fixture["node_id"],
                dev["ble_target_gateway"],
            ),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# SENDER
# ---------------------------------------------------------------------------


class Sender:
    """Sends telemetry via a RNS SINGLE destination.

    Discovers the ingest daemon automatically via announce handler.
    No manual hash copy-pasting needed — just start reticulum_ingest.py
    and this test will find it when it announces.

    Uses PROVE_ALL on the OUT destination so the receiver's PROVE_ALL
    proof strategy can send delivery confirmations back. This validates
    the routing path and provides feedback that packets actually arrived.
    """

    def __init__(self):
        self.app = "farm"
        self.aspect = "telemetry_readings"
        self._destination_hash = None
        self._remote_identity = None
        self._resolved = threading.Event()
        RNS.Transport.register_announce_handler(self)

    # Announce handler interface
    aspect_filter = "farm.telemetry_readings"

    def received_announce(self, destination_hash, announced_identity, app_data):
        """Called when the ingest daemon announces itself."""
        print(f"  Discovered ingest daemon: {RNS.prettyhexrep(destination_hash)}")
        if app_data:
            try:
                label = app_data.decode("utf-8", errors="replace")
                print(f"  App data: {label}")
            except Exception:
                print(f"  App data (raw): {app_data!r}")
        else:
            print("  App data: (none)")
        self._destination_hash = destination_hash
        self._remote_identity = announced_identity
        # Proactively request path to ensure routing is established
        # even if the announce arrived via a different interface.
        RNS.Transport.request_path(destination_hash)
        self._resolved.set()

    def wait_for_ingest(self, timeout: float = 30.0) -> bool:
        """Wait for the ingest daemon to announce itself."""
        if self._resolved.is_set():
            return True
        print("  Waiting for ingest daemon to announce...")
        return self._resolved.wait(timeout=timeout)

    def send(self, payload: dict) -> bool:
        if not self._resolved.is_set():
            if not self.wait_for_ingest(timeout=10.0):
                print("  [WARN] Ingest daemon not discovered, dropping packet")
                return False

        if self._remote_identity is None:
            self._remote_identity = RNS.Identity.recall(self._destination_hash)
            if self._remote_identity is None:
                print("  [WARN] Could not recall identity")
                return False

        destination = RNS.Destination(
            self._remote_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            self.app,
            self.aspect,
        )
        # Set PROVE_ALL on the OUT destination so the receiver's PROVE_ALL
        # proof strategy can send delivery confirmations back. This validates
        # the routing path and provides feedback that packets arrived.
        destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        data = json.dumps(payload).encode("utf-8")
        packet = RNS.Packet(destination, data)
        receipt = packet.send()

        if receipt is not None:

            def on_delivery(receipt_obj):
                print(
                    f"  [RNS] Delivery confirmed for {payload.get('dev_id', '?')} seq={payload.get('seq', '?')}"
                )

            def on_timeout(receipt_obj):
                print(
                    f"  [RNS] Delivery TIMED OUT for {payload.get('dev_id', '?')} seq={payload.get('seq', '?')}"
                )

            receipt.set_delivery_callback(on_delivery)
            receipt.set_timeout(15.0)
            receipt.set_timeout_callback(on_timeout)

        return receipt is not None


# ---------------------------------------------------------------------------
# VERIFICATION
# ---------------------------------------------------------------------------


def verify(db_path: str, sent_at: float) -> list[str]:
    failures = []
    ts_floor = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sent_at - 2))
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row

    for fixture in TEST_FIXTURES:
        node_id = fixture["node_id"]
        label = node_id

        # 1. Rows exist
        rows = conn.execute(
            """SELECT reading_type, value, unit, recorded_at
               FROM sensor_readings
               WHERE node_id = ? AND recorded_at >= ?
               ORDER BY recorded_at""",
            (node_id, ts_floor),
        ).fetchall()

        if not rows:
            failures.append(f"{label}: no readings found in DB")
            continue

        # 2. Minimum count
        if len(rows) < fixture["expected_min_count"]:
            failures.append(
                f"{label}: expected >= {fixture['expected_min_count']} readings, "
                f"got {len(rows)}"
            )

        # 3. All expected types present
        found_types = {r["reading_type"] for r in rows}
        missing = fixture["expected_types"] - found_types
        if missing:
            failures.append(f"{label}: missing reading types {missing}")

        # 4. Values within plausible bounds
        for row in rows:
            rtype = row["reading_type"]
            value = row["value"]
            if value is None:
                failures.append(f"{label}.{rtype}: NULL value in DB")
                continue
            bounds = fixture["bounds"].get(rtype)
            if bounds:
                lo, hi = bounds
                if not (lo <= value <= hi):
                    failures.append(
                        f"{label}.{rtype}: value {value} outside [{lo}, {hi}]"
                    )

        # 5. Units present
        for row in rows:
            if row["unit"] is None:
                failures.append(
                    f"{label}.{row['reading_type']}: unit is NULL (should be empty string or unit)"
                )

        # 6. last_seen updated on sensor_nodes
        node_row = conn.execute(
            "SELECT last_seen FROM sensor_nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if not node_row or not node_row["last_seen"]:
            failures.append(f"{label}: last_seen not set in sensor_nodes")
        elif node_row["last_seen"] < ts_floor:
            failures.append(
                f"{label}: last_seen {node_row['last_seen']!r} not updated after send"
            )

    # 7. Reject payloads left no unexpected trace
    # Empty-readings payload for SN-SOIL-01 — count before vs after
    # We can't easily separate "before" rows here so just note if count is wrong
    # The key check is total rows == sum of expected_min_counts (no extras from rejects)
    expected_total = sum(f["expected_min_count"] for f in TEST_FIXTURES)
    actual_total = conn.execute(
        "SELECT COUNT(*) FROM sensor_readings WHERE recorded_at >= ?", (ts_floor,)
    ).fetchone()[0]
    if actual_total > expected_total * 1.5:
        failures.append(
            f"Total readings {actual_total} exceeds 1.5× expected "
            f"({expected_total}) — rejected payloads may have leaked into DB"
        )
    # Verify rejected payloads didn't add any rows — exact count check
    if actual_total != expected_total:
        failures.append(
            f"Total readings count mismatch: expected {expected_total}, "
            f"got {actual_total} — rejected payloads may have corrupted the DB"
        )

    conn.close()
    return failures


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Farm telemetry pipeline integration test"
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="path for the test SQLite DB (default: ./test_farm.db next to this script)",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="keep the test DB after the test for manual inspection",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable RNS verbose logging",
    )
    args = parser.parse_args()

    db_path = (
        os.path.abspath(args.db)
        if args.db
        else os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "documents", "farm_data.db")
        )
    )

    passed = 0
    failed = 0

    print("=== AgroNomi Pipeline Integration Test ===")
    print(f"  DB path      : {db_path}")
    print("  Transport    : SINGLE destination with announce discovery")
    print()
    print("  Make sure reticulum_ingest.py is running.")
    print("  It uses the same DB path by default.")
    print()

    # Ensure the DB exists with schema and fixture data.
    # We DON'T delete the DB — the ingest daemon may already have it open.
    # Instead we ensure schema exists and insert fixture nodes if missing.
    if not os.path.exists(db_path):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        create_test_db(db_path)
        print(f"  Created DB with {len(TEST_FIXTURES)} node fixtures")
    else:
        # DB exists — just ensure fixture nodes are present
        conn = sqlite3.connect(db_path, timeout=5)
        conn.executescript(SCHEMA_SQL)  # ensure all tables exist
        for fixture in TEST_FIXTURES:
            conn.execute(
                "INSERT OR IGNORE INTO sensor_nodes (node_id, name, location) VALUES (?,?,?)",
                (fixture["node_id"], fixture["node_name"], "Biotop Livada — test"),
            )
        conn.commit()
        conn.close()
        print(f"  DB already exists, ensured schema + fixtures")

    RNS.loglevel = RNS.LOG_DEBUG if args.verbose else RNS.LOG_WARNING
    RNS.Reticulum()

    sender = Sender()

    # Wait for ingest daemon to announce itself
    print("\n--- Phase 1: Discover Ingest Daemon ---")
    if sender.wait_for_ingest(timeout=30.0):
        print("  PASS  ingest daemon discovered")
        passed += 1
    else:
        print("  FAIL  no announce from ingest daemon after 30s")
        print("         Is reticulum_ingest.py running?")
        failed += 1
        sys.exit(1)

    # Phase 2 — send valid payloads
    print("\n--- Phase 2: Send Valid Payloads ---")
    sent_at = time.time()
    for fixture in TEST_FIXTURES:
        for payload in fixture["payloads"]:
            ok = sender.send(payload)
            tag = "PASS" if ok else "FAIL"
            if not ok:
                failed += 1
            else:
                passed += 1
            print(f"  {tag}  {payload['dev_id']} seq={payload['seq']}")

    # Phase 2 — send reject payloads
    print("\n--- Phase 2: Reject Payloads (ingest must not crash) ---")
    for payload in REJECT_PAYLOADS:
        label = payload.get("dev_id", "(no dev_id)")
        sender.send(payload)
        print(f"  INFO  sent malformed payload dev_id={label!r}")

    # Wait for ingest to process
    print("\n  Waiting 6s for ingest to process...")
    time.sleep(6)

    # Phase 3 — DB verification
    print("\n--- Phase 3: DB Verification ---")
    failures = verify(db_path, sent_at)
    if not failures:
        print("  PASS  all readings present, values sane, last_seen updated")
        passed += 1
    else:
        for f in failures:
            print(f"  FAIL  {f}")
        failed += len(failures)

    # Summary
    print(f"\n=== {passed} passed / {failed} failed ===")

    if not args.keep_db:
        print(f"  DB kept: {db_path}")
    else:
        print(f"  DB kept: {db_path}")

    # Clean up RNS resources before exit
    try:
        RNS.Reticulum.exit_handler()
    except Exception:
        pass
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
