"""
ota_scheduler.py — Nightly OTA scheduler and RNS Resource dispatch for AgroNomi

This module provides:
1. Nightly OTA scheduling — checks for devices needing firmware updates and
   queues ota_request commands during a maintenance window.
2. OTA command dispatch — sends firmware binaries via RNS Link + Resource
   to gateways when ota_request commands are pending.

Both are designed to run as daemon threads inside reticulum_ingest.py.
"""

import hashlib
import json
import os
import threading
import time
from datetime import datetime
from typing import Optional

import RNS

# ---------------------------------------------------------------------------
# CONFIGURATION — adjust these for your deployment
# ---------------------------------------------------------------------------

OTA_WINDOW_START = 2  # 02:00 local time
OTA_WINDOW_END = 4  # 04:00 local time
OTA_MAX_RETRIES = 3  # per node per night
OTA_RETRY_BACKOFF = [60, 180, 420]  # seconds: 1min, 3min, 7min

# device_type → (fw_version, bin_path, sha256)
# Update these when new firmware is published
OTA_CURRENT_FW = {
    "soil_node": (
        "1.3.0",
        "/var/agronomi/fw/soil_node_1.3.0.bin",
        "placeholder_sha256",
    ),
    "air_node": ("1.3.0", "/var/agronomi/fw/air_node_1.3.0.bin", "placeholder_sha256"),
}

# RNS Link establishment timeout for OTA Resource transfer
OTA_LINK_TIMEOUT = 60  # seconds

# Import DB functions from the parent module
# This will be set by reticulum_ingest.py at startup
_db_module = None


def set_db_module(mod):
    """Called by reticulum_ingest.py to inject the DB module reference."""
    global _db_module
    _db_module = mod


def _get_conn():
    """Get a database connection using the injected module."""
    if _db_module and hasattr(_db_module, "get_db"):
        return _db_module.get_db()
    raise RuntimeError("DB module not set — call set_db_module() first")


# ---------------------------------------------------------------------------
# FIRMWARE INTEGRITY
# ---------------------------------------------------------------------------


def sha256_of_file(path: str) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def validate_fw_paths():
    """Verify that all firmware binaries exist and compute their SHA-256 hashes.

    Updates OTA_CURRENT_FW with computed hashes if they were placeholders.
    Returns True if all binaries are present, False otherwise.
    """
    all_ok = True
    for dtype, (ver, path, expected_sha) in list(OTA_CURRENT_FW.items()):
        if not os.path.exists(path):
            RNS.log(f"[OTA] Firmware not found: {path} for {dtype}", RNS.LOG_ERROR)
            all_ok = False
            continue
        actual_sha = sha256_of_file(path)
        if expected_sha.startswith("placeholder"):
            OTA_CURRENT_FW[dtype] = (ver, path, actual_sha)
            RNS.log(
                f"[OTA] Computed SHA-256 for {dtype} {ver}: {actual_sha[:16]}...",
                RNS.LOG_INFO,
            )
        elif actual_sha != expected_sha:
            RNS.log(
                f"[OTA] SHA-256 mismatch for {dtype}: expected {expected_sha[:16]}... got {actual_sha[:16]}...",
                RNS.LOG_ERROR,
            )
            all_ok = False
    return all_ok


# ---------------------------------------------------------------------------
# SCHEDULER
# ---------------------------------------------------------------------------


def is_ota_window() -> bool:
    """Check if current time is within the OTA maintenance window."""
    h = datetime.now().hour
    return OTA_WINDOW_START <= h < OTA_WINDOW_END


def get_pending_ota_nodes(conn) -> list:
    """Return devices that need firmware updates and don't have pending OTA commands."""
    rows = conn.execute("""
        SELECT hd.device_id, hd.device_type, hd.firmware_version,
               rg.gateway_id, rg.rns_destination_hash
        FROM hardware_devices hd
        JOIN reticulum_gateways rg
          ON hd.ble_target_gateway = rg.gateway_id
        WHERE hd.status = 'active'
          AND hd.device_type IN ('soil_node', 'air_node', 'pump_node', 'gh_actuator')
          AND hd.device_id NOT IN (
              SELECT device_id FROM actuator_commands
              WHERE cmd_type = 'ota_request'
                AND status IN ('pending', 'sent')
          )
    """).fetchall()

    pending = []
    for row in rows:
        dtype = row["device_type"]
        if dtype not in OTA_CURRENT_FW:
            continue
        target_ver, _, _ = OTA_CURRENT_FW[dtype]
        if row["firmware_version"] == target_ver:
            continue  # already up to date

        # Check retry count for tonight
        window_open = (
            datetime.now()
            .replace(hour=OTA_WINDOW_START, minute=0, second=0)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        failed_tonight = conn.execute(
            """
            SELECT COUNT(*) FROM actuator_commands
            WHERE device_id = ?
              AND cmd_type = 'ota_request'
              AND status = 'failed'
              AND requested_at >= ?
        """,
            (row["device_id"], window_open),
        ).fetchone()[0]

        if failed_tonight >= OTA_MAX_RETRIES:
            RNS.log(
                f"[OTA] {row['device_id']} exceeded {OTA_MAX_RETRIES} retries tonight, skipping",
                RNS.LOG_WARNING,
            )
            continue

        pending.append(dict(row))
    return pending


def schedule_ota_batch(conn):
    """Group pending nodes by gateway+device_type, insert ota_request rows."""
    nodes = get_pending_ota_nodes(conn)
    if not nodes:
        RNS.log("[OTA] No nodes need updates", RNS.LOG_DEBUG)
        return

    # Group by (gateway, device_type)
    groups = {}
    for node in nodes:
        key = (node.get("gateway_id", "GW-MIMI-01"), node["device_type"])
        groups.setdefault(key, []).append(node)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for (gw_id, dtype), group_nodes in groups.items():
        if dtype not in OTA_CURRENT_FW:
            continue
        target_ver, bin_path, sha256 = OTA_CURRENT_FW[dtype]

        # Check firmware file exists
        if not os.path.exists(bin_path):
            RNS.log(f"[OTA] Firmware binary not found: {bin_path}", RNS.LOG_ERROR)
            continue

        size_bytes = os.path.getsize(bin_path)

        cmd_value = json.dumps(
            {
                "fw_version": target_ver,
                "device_type": dtype,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
        )

        for node in group_nodes:
            conn.execute(
                """
                INSERT INTO actuator_commands
                    (device_id, cmd_type, cmd_value_text, requested_at, status)
                VALUES (?, 'ota_request', ?, ?, 'pending')
            """,
                (node["device_id"], cmd_value, now),
            )
            RNS.log(
                f"[OTA] Queued {node['device_id']} ({dtype}) → {target_ver} via {gw_id}",
                RNS.LOG_INFO,
            )
        conn.commit()


def run_ota_scheduler():
    """Main scheduler loop — run as a daemon thread."""
    RNS.log("[OTA] Scheduler thread started", RNS.LOG_INFO)

    # Validate firmware paths and compute hashes
    if not validate_fw_paths():
        RNS.log(
            "[OTA] Some firmware binaries missing — OTA will skip those types",
            RNS.LOG_WARNING,
        )

    already_ran_this_window = False

    while True:
        time.sleep(60)

        in_window = is_ota_window()

        if in_window and not already_ran_this_window:
            RNS.log(
                "[OTA] Maintenance window open — scheduling OTA batch", RNS.LOG_INFO
            )
            try:
                conn = _get_conn()
                schedule_ota_batch(conn)
                conn.close()
            except Exception as e:
                RNS.log(f"[OTA] Scheduler error: {e}", RNS.LOG_ERROR)
            already_ran_this_window = True

        if not in_window:
            already_ran_this_window = False


# ---------------------------------------------------------------------------
# OTA DISPATCH — RNS Link + Resource
# ---------------------------------------------------------------------------

# Path request config (same as CommandDispatcher)
PATH_REQUEST_RETRIES = 3
PATH_REQUEST_RETRY_DELAY = 2  # seconds


def dispatch_ota(
    conn, cmd_id: int, device_id: str, cmd_value_text: str, gw_hash_hex: str
):
    """Dispatch an ota_request command to a gateway via RNS Link + Resource.

    This function:
    1. Establishes an RNS Link to the gateway
    2. Sends the firmware binary as an RNS Resource over that link
    3. After Resource delivery, sends the ota_request command packet

    Called from CommandDispatcher._dispatch_pending() for ota_request commands.
    """
    if not cmd_value_text:
        RNS.log(f"[OTA] No cmd_value_text for cmd {cmd_id}", RNS.LOG_ERROR)
        _mark_ota_failed(conn, cmd_id, "Missing cmd_value_text")
        return

    try:
        meta = json.loads(cmd_value_text)
    except json.JSONDecodeError as e:
        RNS.log(f"[OTA] Invalid cmd_value_text JSON: {e}", RNS.LOG_ERROR)
        _mark_ota_failed(conn, cmd_id, f"Invalid JSON: {e}")
        return

    dtype = meta.get("device_type")
    version = meta.get("fw_version")
    sha256 = meta.get("sha256")

    if not dtype or dtype not in OTA_CURRENT_FW:
        RNS.log(f"[OTA] Unknown device_type: {dtype}", RNS.LOG_ERROR)
        _mark_ota_failed(conn, cmd_id, f"Unknown device_type: {dtype}")
        return

    target_ver, bin_path, expected_sha = OTA_CURRENT_FW[dtype]

    # Verify firmware file exists and matches expected hash
    if not os.path.exists(bin_path):
        RNS.log(f"[OTA] Firmware binary not found: {bin_path}", RNS.LOG_ERROR)
        _mark_ota_failed(conn, cmd_id, f"Firmware binary missing: {bin_path}")
        return

    actual_sha = sha256_of_file(bin_path)
    if sha256 and actual_sha != sha256:
        RNS.log(
            f"[OTA] SHA-256 mismatch: expected {sha256[:16]}... got {actual_sha[:16]}...",
            RNS.LOG_ERROR,
        )
        _mark_ota_failed(conn, cmd_id, "SHA-256 mismatch")
        return

    # Resolve gateway destination
    try:
        dest_hash = bytes.fromhex(gw_hash_hex)
    except ValueError:
        RNS.log(f"[OTA] Invalid dest hash hex: {gw_hash_hex[:16]}...", RNS.LOG_ERROR)
        _mark_ota_failed(conn, cmd_id, f"Invalid hash: {gw_hash_hex[:16]}...")
        return

    RNS.Transport.request_path(dest_hash)
    remote_identity = None
    for attempt in range(1, PATH_REQUEST_RETRIES + 1):
        remote_identity = RNS.Identity.recall(dest_hash)
        if remote_identity is not None:
            break
        if attempt < PATH_REQUEST_RETRIES:
            RNS.log(
                f"[OTA] Path for {gw_hash_hex[:16]}... not known "
                f"(attempt {attempt}/{PATH_REQUEST_RETRIES}), retrying...",
                RNS.LOG_DEBUG,
            )
            time.sleep(PATH_REQUEST_RETRY_DELAY)
            RNS.Transport.request_path(dest_hash)

    if remote_identity is None:
        RNS.log(
            f"[OTA] Gateway {gw_hash_hex[:16]}... not reachable for cmd {cmd_id}",
            RNS.LOG_WARNING,
        )
        _mark_ota_failed(conn, cmd_id, "Gateway not reachable")
        return

    # Create OUT SINGLE destination for the gateway's command aspect
    # Import the constant from reticulum_ingest to stay in sync
    try:
        from reticulum_ingest import COMMAND_APP, GATEWAY_COMMAND_ASPECT

        gw_app = COMMAND_APP
        gw_aspect = GATEWAY_COMMAND_ASPECT
    except ImportError:
        # Fallback if module not importable (e.g. standalone test)
        gw_app = "farm"
        gw_aspect = "gateway_commands"

    gw_dest = RNS.Destination(
        remote_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        gw_app,
        gw_aspect,
    )
    gw_dest.set_proof_strategy(RNS.Destination.PROVE_ALL)

    # Establish RNS Link (required for Resource transfer)
    RNS.log(f"[OTA] Establishing Link to gateway {gw_hash_hex[:16]}...", RNS.LOG_INFO)
    link = RNS.Link(gw_dest)

    # Wait for link to become active
    deadline = time.time() + OTA_LINK_TIMEOUT
    while link.status != RNS.Link.ACTIVE and time.time() < deadline:
        time.sleep(0.5)

    if link.status != RNS.Link.ACTIVE:
        RNS.log(f"[OTA] Link to gateway failed for cmd {cmd_id}", RNS.LOG_WARNING)
        link.teardown()
        _mark_ota_failed(conn, cmd_id, "Link establishment failed")
        return

    RNS.log(f"[OTA] Link established to gateway for cmd {cmd_id}", RNS.LOG_INFO)

    # Read firmware binary
    with open(bin_path, "rb") as f:
        firmware_data = f.read()

    RNS.log(
        f"[OTA] Sending firmware ({len(firmware_data)} bytes) to gateway for cmd {cmd_id}",
        RNS.LOG_INFO,
    )

    # Send firmware binary as RNS Resource
    def on_resource_concluded(resource):
        if resource.status == RNS.Resource.COMPLETE:
            RNS.log(f"[OTA] Binary delivered to gateway for cmd {cmd_id}", RNS.LOG_INFO)
            # Send the ota_request command packet so gateway knows what to do
            payload = json.dumps(
                {
                    "cmd_id": cmd_id,
                    "device_id": device_id,
                    "cmd_type": "ota_request",
                    "fw_version": version,
                    "device_type": dtype,
                    "sha256": sha256 or actual_sha,
                    "size_bytes": len(firmware_data),
                    "ts": int(time.time()),
                }
            ).encode("utf-8")
            pkt = RNS.Packet(gw_dest, payload)
            pkt.send()
            conn.execute(
                "UPDATE actuator_commands SET status='sent', last_retry_at=datetime('now') "
                "WHERE cmd_id=?",
                (cmd_id,),
            )
            conn.commit()
        else:
            RNS.log(f"[OTA] Resource transfer failed for cmd {cmd_id}", RNS.LOG_ERROR)
            _mark_ota_failed(conn, cmd_id, "Resource transfer incomplete")
        link.teardown()

    resource = RNS.Resource(firmware_data, link, callback=on_resource_concluded)
    resource.advertise()


def _mark_ota_failed(conn, cmd_id: int, reason: str):
    """Mark an OTA command as failed with error message and increment retry count."""
    conn.execute(
        """
        UPDATE actuator_commands
        SET status = 'failed',
            error_message = ?,
            retry_count = COALESCE(retry_count, 0) + 1,
            last_retry_at = datetime('now')
        WHERE cmd_id = ?
    """,
        (reason, cmd_id),
    )
    conn.commit()
    RNS.log(f"[OTA] Cmd {cmd_id} marked failed: {reason}", RNS.LOG_WARNING)
