#!/usr/bin/env python3
"""firmware_push.py — Queue firmware push requests for the hub to send.

This script does NOT create its own RNS instance or LXMRouter.
It simply inserts a request into the firmware_pushes database table,
and the running hub (reticulum_ingest.py) picks it up via
FirmwarePushDispatcher and sends it through its existing LXMRouter.

Usage:
  python3 firmware_push.py sn_support               # push latest to sn_support
  python3 firmware_push.py sn_support --version 2.1.0-mr
  python3 firmware_push.py sn_support --no-reboot   # push files, don't commit
  python3 firmware_push.py sn_support --dry-run     # show what would be sent
"""

import argparse
import hashlib
import os
import sqlite3
import sys

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Files that must NOT be sent — they would overwrite device-specific config
EXCLUDED_FILES = {"secrets.py", "__init__.py"}

DEVICE_DIRS = {
    "sn_air": "sn_air",
    "sn_soil": "sn_soil",
    "sn_support": "sn_support",
    "an_pump": "an_pump",
    "an_greenhouse": "an_greenhouse",
}

FIRMWARE_BASE = os.path.join(os.path.dirname(__file__), "..")

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "../../documents/farm_data.db")
)


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def discover_firmware_files(device):
    """Discover all .py firmware files in the device's firmware directory.

    All files come from <device>/firmware/ — including updater.py which
    is device-specific.  secrets.py is excluded to avoid overwriting
    the node's WiFi credentials with the blank template.
    """
    device_dir = DEVICE_DIRS.get(device)
    if not device_dir:
        raise ValueError(f"Unknown device: {device}")

    firmware_dir = os.path.join(FIRMWARE_BASE, device_dir, "firmware")
    try:
        all_files = sorted(os.listdir(firmware_dir))
    except OSError as e:
        raise FileNotFoundError(f"Cannot list firmware dir {firmware_dir}: {e}")

    return [f for f in all_files if f.endswith(".py") and f not in EXCLUDED_FILES]


def read_firmware_file(device, filename):
    device_dir = DEVICE_DIRS.get(device)
    if not device_dir:
        raise ValueError(f"Unknown device: {device}")

    # All files come from the device's own firmware directory
    path = os.path.join(FIRMWARE_BASE, device_dir, "firmware", filename)

    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        return data, sha256_hex(data)

    raise FileNotFoundError(f"Firmware file not found: {filename} for {device}")


def main():
    parser = argparse.ArgumentParser(
        description="Queue firmware push requests (processed by reticulum_ingest hub)"
    )
    parser.add_argument(
        "device", choices=list(DEVICE_DIRS.keys()), help="Device type to update"
    )
    parser.add_argument(
        "--version", default=None, help="Version string (auto-detected from config.py)"
    )
    parser.add_argument(
        "--no-reboot", action="store_true", help="Don't send commit/reboot command"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be sent without sending"
    )
    parser.add_argument("--db", default=DB_PATH, help="Path to farm_data.db")

    args = parser.parse_args()

    # Auto-detect version
    version = args.version
    if not version:
        config_path = os.path.join(
            FIRMWARE_BASE, DEVICE_DIRS[args.device], "firmware", "config.py"
        )
        try:
            with open(config_path, "r") as f:
                for line in f:
                    if line.strip().startswith("FIRMWARE_VERSION"):
                        version = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            version = "unknown"
        if not version:
            version = "unknown"

    # Discover firmware files from the device's firmware directory
    files_to_send = discover_firmware_files(args.device)
    print(f"[push] AgroNomi firmware push (queued via hub)")
    print(f"[push]   Device:  {args.device}")
    print(f"[push]   Version: {version}")
    print(f"")

    total_size = 0
    for filename in files_to_send:
        try:
            data, file_hash = read_firmware_file(args.device, filename)
            total_size += len(data)
            print(f"[push]   {filename}: {len(data)} bytes, sha256={file_hash[:16]}...")
        except FileNotFoundError as e:
            print(f"[push]   {filename}: NOT FOUND - {e}")
            sys.exit(1)

    print(f"[push] Total payload: {total_size} bytes")

    if args.dry_run:
        print("[push] DRY RUN — no request queued. Use without --dry-run to push.")
        return

    # Queue the push request in the database — the hub's FirmwarePushDispatcher
    # will pick it up and send it through the existing LXMRouter
    import json

    conn = sqlite3.connect(args.db)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO firmware_pushes (device_type, version, filenames, no_reboot)
        VALUES (?, ?, ?, ?)
    """,
        (args.device, version, json.dumps(files_to_send), int(args.no_reboot)),
    )
    conn.commit()
    push_id = cursor.lastrowid
    conn.close()

    print(f"[push] Queued push request #{push_id} for {args.device} v{version}")
    print(f"[push] The running hub will process this and send firmware files.")
    print(f"[push] Check hub logs for delivery status.")


if __name__ == "__main__":
    main()
