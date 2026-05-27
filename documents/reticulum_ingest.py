"""
reticulum_ingest.py — Streamlined RNS-Native Telemetry & Firmware Engine
"""

import hashlib
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime

import RNS
from RNS.vendor import umsgpack

DB_PATH = os.environ.get("DB_PATH", "./farm_data.db")
IDENTITY_PATH = "./farm_hub.identity"

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sensor_nodes (
    node_id TEXT PRIMARY KEY, name TEXT, location TEXT, last_seen TEXT, battery_level REAL
);
CREATE TABLE IF NOT EXISTS sensor_readings (
    reading_id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL, reading_type TEXT NOT NULL, value REAL NOT NULL, unit TEXT DEFAULT '', recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hardware_devices (
    device_id TEXT PRIMARY KEY, device_type TEXT, node_id TEXT, rns_identity_hash TEXT, rns_destination_hash TEXT, rns_interface TEXT DEFAULT 'wifi', firmware_version TEXT, status TEXT DEFAULT 'active', last_seen TEXT
);
CREATE TABLE IF NOT EXISTS actuator_commands (
    cmd_id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, cmd_type TEXT, cmd_value REAL, cmd_value_text TEXT, requested_at TEXT NOT NULL DEFAULT (datetime('now')), executed_at TEXT, status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS firmware_pushes (
    push_id INTEGER PRIMARY KEY AUTOINCREMENT, device_type TEXT NOT NULL, node_id TEXT, version TEXT NOT NULL, filenames TEXT NOT NULL, status TEXT DEFAULT 'pending', no_reboot INTEGER DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT, retries INTEGER DEFAULT 0
);
"""

PATH_TIMEOUT = 30  # seconds to wait for a path to a node
LINK_TIMEOUT = 30  # seconds to wait for a link to establish
RESOURCE_TIMEOUT = 300  # outer watchdog for RNS.Resource conclusion callback
MAX_FILE_ATTEMPTS = 3  # application-level retries on Resource FAILED
MAX_PUSH_RETRIES = 5  # whole-push re-queues across fw_check cycles before giving up

# Maximum chunk size per update_file Resource.  ESP32-C6 MicroPython
# heap is fragmented by the time firmware push runs; empirically the
# largest contiguous chunk available for plaintext allocation during
# urns Resource.assemble is ~6 KB.  4 KB leaves comfortable headroom
# and produces ~4.5 KB packed / ~4.6 KB encrypted Resources, all of
# which fit easily in the receiver heap. Receiver
# (m_reticulum/sn_support/firmware/updater.py:handle_update) reassembles
# chunks in append mode and verifies the whole-file SHA-256 at the
# final chunk.
CHUNK_SIZE = 4096

# Constrained-peer Resource tuning.  Reference RNS defaults assume an
# Ethernet-class receiver and burst up to ~75 parts in flight, which
# overruns the ESP32 lwIP socket buffer (urns) and triggers receive-side
# request-retry exhaustion on multi-part transfers.  Cap the window for
# the duration of a firmware push so the sender paces ACK-by-ACK.
_RESOURCE_WINDOW_CAPS = {
    "WINDOW": 2,
    "WINDOW_MIN": 2,
    "WINDOW_MAX": 4,
    "WINDOW_MAX_FAST": 4,
    "WINDOW_MAX_SLOW": 4,
    "WINDOW_MAX_VERY_SLOW": 4,
}


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_DDL)
    # Add retries column for pre-existing DBs that pre-date auto-retry.
    try:
        conn.execute("ALTER TABLE firmware_pushes ADD COLUMN retries INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    return conn


# ---------------------------------------------------------------------------
# GLOBAL ANNOUNCE & DISCOVERY HANDLER
# ---------------------------------------------------------------------------
class NodeDiscoveryHandler:
    def __init__(self, aspect_filter):
        self.aspect_filter = aspect_filter

    def received_announce(self, destination_hash, announced_identity, app_data):
        if not app_data:
            return
        try:
            data_str = (
                app_data.decode("utf-8")
                if isinstance(app_data, bytes)
                else str(app_data)
            )
            node_id = (
                data_str.split(":")[-1].strip() if ":" in data_str else data_str.strip()
            )
            now_str = datetime.now().isoformat()

            conn = get_db()
            with conn:
                conn.execute(
                    "INSERT INTO sensor_nodes (node_id, name, last_seen) VALUES (?, ?, ?)"
                    " ON CONFLICT(node_id) DO UPDATE SET last_seen=excluded.last_seen",
                    (node_id, node_id, now_str),
                )
                conn.execute(
                    """
                    INSERT INTO hardware_devices
                        (device_id, device_type, node_id, rns_identity_hash, rns_destination_hash, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id) DO UPDATE SET
                        rns_identity_hash=excluded.rns_identity_hash,
                        rns_destination_hash=excluded.rns_destination_hash,
                        last_seen=excluded.last_seen
                    """,
                    (
                        node_id,
                        "gateway" if "GW" in node_id else "support_node",
                        node_id,
                        announced_identity.hash.hex(),
                        destination_hash.hex(),
                        now_str,
                    ),
                )
            RNS.log(
                f"[DISCOVERY] Node registered: {node_id} at <{destination_hash.hex()}>",
                RNS.LOG_NOTICE,
            )
        except Exception as e:
            RNS.log(f"[DISCOVERY ERROR] Failed: {e}", RNS.LOG_WARNING)


# ---------------------------------------------------------------------------
# OUTBOUND COMMAND DISPATCHER
# ---------------------------------------------------------------------------
class OutboundCommandDispatcher:
    def __init__(self, hub_app):
        self.hub_app = hub_app
        self.running = True

    def poll_loop(self):
        while self.running:
            try:
                conn = get_db()
                rows = conn.execute("""
                    SELECT c.cmd_id, c.device_id, c.cmd_type, h.rns_destination_hash
                    FROM actuator_commands c
                    JOIN hardware_devices h ON c.device_id = h.device_id
                    WHERE c.status = 'pending'
                    LIMIT 5
                """).fetchall()

                for row in rows:
                    recipient_identity = RNS.Identity.recall(
                        bytes.fromhex(row["rns_destination_hash"])
                    )
                    if not recipient_identity:
                        continue

                    dest = RNS.Destination(
                        recipient_identity,
                        RNS.Destination.OUT,
                        RNS.Destination.SINGLE,
                        "farm",
                        "node",
                    )
                    RNS.Packet(
                        dest,
                        umsgpack.packb(
                            {
                                "cmd": "execute",
                                "type": row["cmd_type"],
                                "cmd_id": row["cmd_id"],
                            }
                        ),
                    ).send()

                    conn.execute(
                        "UPDATE actuator_commands SET status='sent', executed_at=? WHERE cmd_id=?",
                        (datetime.now().isoformat(), row["cmd_id"]),
                    )
                    conn.commit()
                conn.close()
            except Exception as e:
                RNS.log(f"[COMMAND ERROR] {e}", RNS.LOG_ERROR)
            time.sleep(2)


# ---------------------------------------------------------------------------
# MAIN CENTRAL CORE
# ---------------------------------------------------------------------------
class FarmHub:
    def __init__(self):
        config_dir = next(
            (
                sys.argv[i + 1]
                for i, arg in enumerate(sys.argv)
                if arg in ("--config", "-c") and i + 1 < len(sys.argv)
            ),
            None,
        )
        self.reticulum = RNS.Reticulum(configdir=config_dir, loglevel=RNS.LOG_NOTICE)

        # Load or mint identity
        if os.path.exists(IDENTITY_PATH):
            self.identity = RNS.Identity.from_file(IDENTITY_PATH)
        else:
            self.identity = RNS.Identity()
            self.identity.to_file(IDENTITY_PATH)

        self.hub_addr_hex = self.identity.hash.hex()
        self.transfer_active = False

        # Unified incoming destination
        self.cmd_dest = RNS.Destination(
            self.identity, RNS.Destination.IN, RNS.Destination.SINGLE, "farm", "hub"
        )
        self.cmd_dest.set_packet_callback(self._on_packet_received)

        # Announce discovery
        self.discovery_handler = NodeDiscoveryHandler(aspect_filter="farm.node")
        RNS.Transport.register_announce_handler(self.discovery_handler)

        self.cmd_dest.announce(app_data=b"agronomi_hub")
        RNS.log(
            f"[CORE INIT] Ingestion Channel Open at: <{self.hub_addr_hex}>",
            RNS.LOG_NOTICE,
        )

    def _on_packet_received(self, data, packet):
        try:
            payload = umsgpack.unpackb(data)
            cmd, node_id = payload.get("cmd", ""), payload.get("dev_id")
            if not node_id:
                return

            if cmd == "telemetry":
                self._write_telemetry_to_db(payload)
            elif cmd == "fw_check":
                RNS.log(f"[FW CHECK] Node {node_id} checked in.", RNS.LOG_NOTICE)
                self._handle_fw_check(node_id, payload.get("fw", ""))
        except Exception as e:
            RNS.log(f"[INGEST ERROR] Payload error: {e}", RNS.LOG_ERROR)

    def _write_telemetry_to_db(self, data):
        now_str, node_id = datetime.now().isoformat(), data["dev_id"]
        try:
            conn = get_db()
            with conn:
                conn.execute(
                    "INSERT INTO sensor_nodes (node_id, name, last_seen, battery_level)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(node_id) DO UPDATE SET"
                    " last_seen=excluded.last_seen, battery_level=excluded.battery_level",
                    (node_id, node_id, now_str, data.get("bat", -1.0)),
                )
                conn.execute(
                    "UPDATE hardware_devices SET firmware_version=?, last_seen=? WHERE device_id=?",
                    (data.get("fw"), now_str, node_id),
                )
                conn.execute(
                    "INSERT INTO sensor_readings"
                    " (node_id, reading_type, value, unit, recorded_at)"
                    " VALUES (?, 'battery_voltage', ?, 'V', ?)",
                    (node_id, data.get("bat", -1.0), now_str),
                )

                readings = (
                    data.get("readings", {})
                    if isinstance(data.get("readings"), dict)
                    else {}
                )
                readings_map = {
                    "air_temperature": ("temp", "air_temp_c", "C"),
                    "air_humidity": ("hum", "air_humidity_pct", "%"),
                    "soil_moisture": ("moist", "soil_moisture_pct", "%"),
                    "soil_temperature": ("soil_temp", "soil_temp_c", "C"),
                }
                for r_type, (k1, k2, unit) in readings_map.items():
                    val = readings.get(k1, readings.get(k2, data.get(k1)))
                    if val is not None:
                        conn.execute(
                            "INSERT INTO sensor_readings"
                            " (node_id, reading_type, value, unit, recorded_at)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (node_id, r_type, val, unit, now_str),
                        )
            RNS.log(
                f"[DB SYNC] Ingested telemetry payload for {node_id}", RNS.LOG_NOTICE
            )
        except Exception as e:
            RNS.log(f"[DB ERROR] Sync error: {e}", RNS.LOG_ERROR)

    def _handle_fw_check(self, node_id, current_version):
        prefix_map = {
            "SN-AIR": "sn_air",
            "SN-SOIL": "sn_soil",
            "GW-SUPPORT": "sn_support",
            "AN-PUMP": "an_pump",
            "AN-GREENHOUSE": "an_greenhouse",
        }
        device_type = next(
            (dt for pfx, dt in prefix_map.items() if node_id.startswith(pfx)), None
        )
        if not device_type:
            return

        conn = get_db()
        # Clean up any stale 'sending' rows from a previously crashed push.
        conn.execute(
            "UPDATE firmware_pushes SET status='failed', updated_at=?"
            " WHERE device_type=? AND status='sending'",
            (datetime.now().isoformat(), device_type),
        )
        # The node just reported what firmware version it has running.  If
        # any pending pushes target that same version, mark them as
        # already-installed so we don't re-push the same firmware in a loop
        # every fw_check (the node would reboot every minute otherwise).
        if current_version:
            conn.execute(
                "UPDATE firmware_pushes SET status='sent', updated_at=?"
                " WHERE device_type=? AND status='pending' AND version=?",
                (datetime.now().isoformat(), device_type, current_version),
            )
        conn.commit()

        # Now look for a pending push whose version differs from what the
        # node has — that's the genuine "needs upgrade" case.
        row = conn.execute(
            "SELECT push_id, version, no_reboot FROM firmware_pushes"
            " WHERE device_type=? AND status='pending'"
            " ORDER BY push_id LIMIT 1",
            (device_type,),
        ).fetchone()
        conn.close()

        if row:
            RNS.log(
                f"[FW DISPATCH] Starting direct firmware push {row['push_id']} to {node_id}",
                RNS.LOG_NOTICE,
            )
            conn = get_db()
            conn.execute(
                "UPDATE firmware_pushes SET status='sending', updated_at=? WHERE push_id=?",
                (datetime.now().isoformat(), row["push_id"]),
            )
            conn.commit()
            conn.close()

            def _deferred_push():
                # Run off the RNS packet-callback thread. await_path() inside
                # push_firmware handles path resolution; no manual sleep needed.
                self._run_fw_push(
                    row["push_id"],
                    device_type,
                    row["version"],
                    bool(row["no_reboot"]),
                    node_id,
                )

            threading.Thread(target=_deferred_push, daemon=True).start()
        else:
            self._send_ack_packet(node_id)

    def _send_ack_packet(self, node_id):
        conn = get_db()
        row = conn.execute(
            "SELECT rns_destination_hash FROM hardware_devices WHERE node_id=? LIMIT 1",
            (node_id,),
        ).fetchone()
        conn.close()
        if not (row and row["rns_destination_hash"]):
            return
        ident = RNS.Identity.recall(bytes.fromhex(row["rns_destination_hash"]))
        if not ident:
            return
        dest = RNS.Destination(
            ident, RNS.Destination.OUT, RNS.Destination.SINGLE, "farm", "node"
        )
        RNS.Packet(
            dest,
            umsgpack.packb({"cmd": "fw_check_ack", "dev_id": "hub", "pending": False}),
        ).send()

    def _run_fw_push(self, push_id, device_type, version, no_reboot, node_id):
        success = self.push_firmware(device_type, version, no_reboot, node_id)
        conn = get_db()
        if success:
            conn.execute(
                "UPDATE firmware_pushes SET status='sent', updated_at=? WHERE push_id=?",
                (datetime.now().isoformat(), push_id),
            )
        else:
            # Transient failures (link drop, chunk timeout, receive-side
            # cancellation) are expected in the field.  Re-queue the push
            # so the next fw_check from the node re-dispatches it.  After
            # MAX_PUSH_RETRIES re-queues we give up and mark 'failed' so
            # we don't infinite-loop on a genuinely bad firmware build.
            row = conn.execute(
                "SELECT retries FROM firmware_pushes WHERE push_id=?",
                (push_id,),
            ).fetchone()
            attempts = (row["retries"] if row and row["retries"] is not None else 0) + 1
            if attempts >= MAX_PUSH_RETRIES:
                conn.execute(
                    "UPDATE firmware_pushes SET status='failed', retries=?, updated_at=? WHERE push_id=?",
                    (attempts, datetime.now().isoformat(), push_id),
                )
                RNS.log(
                    f"[FW PUSH] Push {push_id} for {node_id} failed after {attempts} attempts — giving up",
                    RNS.LOG_ERROR,
                )
            else:
                conn.execute(
                    "UPDATE firmware_pushes SET status='pending', retries=?, updated_at=? WHERE push_id=?",
                    (attempts, datetime.now().isoformat(), push_id),
                )
                RNS.log(
                    f"[FW PUSH] Re-queued push {push_id} for {node_id} (attempt {attempts}/{MAX_PUSH_RETRIES})",
                    RNS.LOG_WARNING,
                )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Firmware push — single Link for the whole session (this firmware
    # only accepts one link per cmd_dest), with the rncp utility's
    # polling pattern around each Resource (see
    # /opt/homebrew/.../RNS/Utilities/rncp.py:717-790), and application-
    # level retries on FAILED to absorb proof-window jitter for files
    # 2..N (the receiver's flash sync briefly stalls its event loop, so
    # the proof packet occasionally arrives after the hub's hardcoded
    # 3-retry window inside RNS.Resource expires).
    # ------------------------------------------------------------------

    def _open_link(self, recipient_identity):
        """rncp-style polled link establishment."""
        node_dest_hash = RNS.Destination.hash(recipient_identity, "farm", "node")

        if not RNS.Transport.has_path(node_dest_hash):
            RNS.Transport.request_path(node_dest_hash)
            deadline = time.time() + PATH_TIMEOUT
            while not RNS.Transport.has_path(node_dest_hash) and time.time() < deadline:
                time.sleep(0.1)

        if not RNS.Transport.has_path(node_dest_hash):
            RNS.log("[FW PUSH] No path to receiver", RNS.LOG_ERROR)
            return None

        outbound_dest = RNS.Destination(
            recipient_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "farm",
            "node",
        )

        link = RNS.Link(outbound_dest)
        deadline = time.time() + LINK_TIMEOUT
        while link.status != RNS.Link.ACTIVE and time.time() < deadline:
            time.sleep(0.1)

        if link.status != RNS.Link.ACTIVE:
            RNS.log("[FW PUSH] Link did not become ACTIVE", RNS.LOG_ERROR)
            return None

        # rncp pattern: identify the sender to the receiver
        link.identify(self.identity)
        return link

    def _send_resource_attempt(self, link, data, fname):
        """One rncp-pattern Resource send attempt on an existing link.
        Returns True if status reaches COMPLETE, False on any other
        outcome (FAILED, CORRUPT, link drop, watchdog)."""
        resource_done = [False]

        def sender_progress(resource):
            if resource.status >= RNS.Resource.COMPLETE:
                resource_done[0] = True

        try:
            # auto_compress=False: urns ESP32 receivers without the native
            # bz2_fast_*.mpy module fall back to pure-Python decompression,
            # which on a 4 KB compressed payload takes tens of seconds —
            # far longer than the hub's hardcoded ~46 s AWAITING_PROOF
            # window inside RNS.Resource.  Sending uncompressed produces
            # more parts (~33 vs ~10), but the windowed sender paces them
            # to the receiver and the proof comes back immediately because
            # there is nothing to decompress.  Per urns README §"Native
            # BZ2 Module": "without it, Resources are sent uncompressed
            # (which is valid)" — same protocol applies in reverse.
            resource = RNS.Resource(
                data,
                link,
                callback=sender_progress,
                progress_callback=sender_progress,
                auto_compress=False,
            )
        except Exception as e:
            RNS.log(f"[FW PUSH] {fname}: could not start resource: {e}", RNS.LOG_ERROR)
            return False

        # rncp pattern: wait until transfer is actually moving
        deadline = time.time() + 30
        while resource.status < RNS.Resource.TRANSFERRING and time.time() < deadline:
            if link.status != RNS.Link.ACTIVE:
                return False
            time.sleep(0.1)

        # rncp pattern: status above COMPLETE here means the receiver
        # explicitly rejected before transferring (CORRUPT, FAILED)
        if resource.status > RNS.Resource.COMPLETE:
            return False

        # rncp pattern: poll progress until conclusion or watchdog
        last_log = 0.0
        deadline = time.time() + RESOURCE_TIMEOUT
        while not resource_done[0] and time.time() < deadline:
            if link.status != RNS.Link.ACTIVE:
                return False
            now = time.time()
            if now - last_log >= 5.0:
                pct = resource.get_progress() * 100
                RNS.log(f"[FW PUSH] {fname} — {pct:.1f}%", RNS.LOG_NOTICE)
                last_log = now
            time.sleep(0.1)

        return resource.status == RNS.Resource.COMPLETE

    def _send_link_packet(self, link, payload, wait_for_proof=True):
        """Send a control packet on the link.  When wait_for_proof, use
        PacketReceipt's delivery callback to know it landed before we
        move on — the receiver's destination has PROVE_ALL set, so
        proofs come back automatically."""
        packet = RNS.Packet(link, umsgpack.packb(payload))
        receipt = packet.send()
        if not wait_for_proof or not receipt:
            return True

        delivered = threading.Event()
        receipt.set_delivery_callback(lambda _r: delivered.set())
        receipt.set_timeout_callback(lambda _r: delivered.set())
        delivered.wait(timeout=10.0)
        return True

    def _query_node_manifest(self, link, filenames, timeout=15.0):
        """Ask the receiver what SHA-256 hashes it already has for the
        given filenames (looking in /update/ first, then /).  Returns
        the node's manifest dict, or {} if the receiver doesn't respond
        (old firmware without manifest_query support, or link issue).

        Used so the hub can skip pushing files whose hashes already
        match — saves bandwidth on incremental dev cycles and on
        retries after a partially-staged push.
        """
        response = [None]
        signal = threading.Event()

        # We attach a transient packet callback to the link so we can
        # intercept the manifest_response.  After we get it (or the
        # timeout fires), we restore whatever callback was there before.
        prev_callback = getattr(link.callbacks, "packet", None) if hasattr(link, "callbacks") else None

        def _handle(message, packet):
            try:
                payload = umsgpack.unpackb(message)
            except Exception:
                return
            if payload.get("cmd") == "manifest_response":
                response[0] = payload.get("manifest", {}) or {}
                signal.set()

        link.set_packet_callback(_handle)
        try:
            RNS.Packet(
                link,
                umsgpack.packb({"cmd": "manifest_query", "files": filenames}),
            ).send()
            signal.wait(timeout=timeout)
        finally:
            # Restore (or clear) the previous packet callback so further
            # link packets don't get hijacked.
            if prev_callback is not None:
                link.set_packet_callback(prev_callback)
            else:
                link.callbacks.packet = None

        return response[0] or {}

    def push_firmware(self, device, version=None, no_reboot=False, node_id=None):
        """Push firmware over a single Link, rncp polling per file."""
        DEVICE_DIRS = {
            "sn_air": "sn_air",
            "sn_soil": "sn_soil",
            "sn_support": "sn_support",
            "an_pump": "an_pump",
            "an_greenhouse": "an_greenhouse",
        }
        if device not in DEVICE_DIRS:
            RNS.log(f"[FW PUSH] Unknown device type: {device}", RNS.LOG_ERROR)
            return False

        firmware_dir = os.path.join(
            os.path.dirname(__file__),
            "..",
            "m_reticulum",
            DEVICE_DIRS[device],
            "firmware",
        )

        try:
            files_to_send = sorted(
                f
                for f in os.listdir(firmware_dir)
                if f.endswith(".py") and f not in {"secrets.py", "__init__.py"}
            )
        except OSError as e:
            RNS.log(f"[FW PUSH] Cannot read firmware dir {firmware_dir}: {e}", RNS.LOG_ERROR)
            return False

        if not files_to_send:
            RNS.log(f"[FW PUSH] No firmware files found in {firmware_dir}", RNS.LOG_ERROR)
            return False

        conn = get_db()
        row = conn.execute(
            "SELECT rns_destination_hash FROM hardware_devices WHERE node_id=? LIMIT 1",
            (node_id,),
        ).fetchone()
        conn.close()

        if not row or not row["rns_destination_hash"]:
            RNS.log(f"[FW PUSH] No destination hash on record for {node_id}", RNS.LOG_ERROR)
            return False

        dest_hash_bytes = bytes.fromhex(row["rns_destination_hash"])
        recipient_identity = RNS.Identity.recall(dest_hash_bytes)
        if not recipient_identity:
            RNS.log(
                f"[FW PUSH] Identity for {node_id} not in RNS key store — "
                "node must announce before a firmware push can proceed",
                RNS.LOG_ERROR,
            )
            return False

        file_entries = []
        for filename in files_to_send:
            with open(os.path.join(firmware_dir, filename), "rb") as f:
                data = f.read()
            file_entries.append(
                {
                    "filename": filename,
                    "data": data,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        RNS.log(
            f"[FW PUSH] Prepared {len(file_entries)} files for {node_id} (v{version})",
            RNS.LOG_NOTICE,
        )

        # Apply ESP32-friendly window caps for the duration of this push.
        # Saved here, restored in finally so unrelated RNS traffic on this
        # process is unaffected.
        _saved_window_caps = {}
        for attr, value in _RESOURCE_WINDOW_CAPS.items():
            if hasattr(RNS.Resource, attr):
                _saved_window_caps[attr] = getattr(RNS.Resource, attr)
                setattr(RNS.Resource, attr, value)
        RNS.log(
            f"[FW PUSH] Capped Resource window to {_RESOURCE_WINDOW_CAPS['WINDOW_MAX']} "
            "for constrained receiver",
            RNS.LOG_NOTICE,
        )

        link = self._open_link(recipient_identity)
        if link is None:
            for attr, value in _saved_window_caps.items():
                setattr(RNS.Resource, attr, value)
            return False

        RNS.log(f"[FW PUSH] Link active to {node_id}", RNS.LOG_NOTICE)
        self.transfer_active = True
        try:
            # Build the full expected manifest (filename → hex SHA-256 of
            # the whole file).  This goes to the node so it can verify
            # at commit time that every expected file is intact in
            # /update/ — including files the hub skipped because the
            # node already had them at the right hash.
            expected_manifest = {
                entry["filename"]: entry["sha256"] for entry in file_entries
            }

            # Differential-push: ask the node which of these files it
            # already has at the expected hash.  Skip those.  An empty
            # response (old firmware, no manifest_query handler, or link
            # issue) collapses to "send everything" — same behavior as
            # before, no regression.
            node_manifest = self._query_node_manifest(
                link, [entry["filename"] for entry in file_entries]
            )
            skip_set = {
                fname
                for fname, expected in expected_manifest.items()
                if node_manifest.get(fname) == expected
            }
            if skip_set:
                RNS.log(
                    f"[FW PUSH] Skipping {len(skip_set)}/{len(file_entries)} "
                    f"already-correct files: {sorted(skip_set)}",
                    RNS.LOG_NOTICE,
                )

            files_to_send = [
                e for e in file_entries if e["filename"] not in skip_set
            ]

            # update_begin — carries the full expected manifest plus the
            # count of files we actually plan to transfer (not counting
            # skipped ones).  Receiver uses manifest at commit time to
            # verify the entire /update/ tree, not just the new arrivals.
            self._send_link_packet(
                link,
                {
                    "cmd": "update_begin",
                    "version": version,
                    "dev_id": node_id,
                    "file_count": len(files_to_send),
                    "manifest": expected_manifest,
                },
            )

            # files — chunked (CHUNK_SIZE per Resource), rncp polling per
            # chunk, app-level retries on Resource FAILED.  The receiver
            # reassembles chunks of the same filename in append mode and
            # verifies the whole-file SHA-256 at the final chunk; see
            # m_reticulum/sn_support/firmware/updater.py:handle_update.
            # files_to_send already excludes files the node confirmed it
            # has at the expected hash.
            for idx, entry in enumerate(files_to_send):
                fname = entry["filename"]
                file_data = entry["data"]
                file_hash = entry["sha256"]
                total_chunks = max(1, (len(file_data) + CHUNK_SIZE - 1) // CHUNK_SIZE)

                for chunk_idx in range(total_chunks):
                    chunk_start = chunk_idx * CHUNK_SIZE
                    chunk_end = min(chunk_start + CHUNK_SIZE, len(file_data))
                    chunk_data = file_data[chunk_start:chunk_end]

                    packed = umsgpack.packb(
                        {
                            "cmd": "update_file",
                            "version": version,
                            "dev_id": node_id,
                            "filename": fname,
                            "data": chunk_data,
                            "sha256": file_hash,
                            "chunk_index": chunk_idx,
                            "total_chunks": total_chunks,
                            "index": idx,
                            "total": len(files_to_send),
                        }
                    )

                    if total_chunks > 1:
                        label = f"{fname}[{chunk_idx + 1}/{total_chunks}]"
                    else:
                        label = fname

                    attempts = 0
                    while True:
                        if link.status != RNS.Link.ACTIVE:
                            RNS.log(
                                f"[FW PUSH] {label}: link dropped — aborting",
                                RNS.LOG_ERROR,
                            )
                            return False

                        attempts += 1
                        if self._send_resource_attempt(link, packed, label):
                            break

                        if attempts >= MAX_FILE_ATTEMPTS:
                            RNS.log(
                                f"[FW PUSH] {label}: {MAX_FILE_ATTEMPTS} attempts failed; aborting",
                                RNS.LOG_ERROR,
                            )
                            return False

                        RNS.log(
                            f"[FW PUSH] {label}: attempt {attempts} did not reach COMPLETE; retrying",
                            RNS.LOG_WARNING,
                        )

                RNS.log(
                    f"[FW PUSH] {fname} delivered ({idx + 1}/{len(files_to_send)})"
                    + (f" in {total_chunks} chunks" if total_chunks > 1 else ""),
                    RNS.LOG_NOTICE,
                )

            # update_commit — node reboots on success, link tears down from its side
            if link.status == RNS.Link.ACTIVE:
                self._send_link_packet(
                    link,
                    {
                        "cmd": "update_commit",
                        "version": version,
                        "dev_id": node_id,
                        "reboot": not no_reboot,
                    },
                    wait_for_proof=False,
                )

            RNS.log(f"[FW PUSH] Firmware push complete for {node_id}", RNS.LOG_NOTICE)
            return True

        finally:
            if link.status == RNS.Link.ACTIVE:
                link.teardown()
            self.transfer_active = False
            for attr, value in _saved_window_caps.items():
                setattr(RNS.Resource, attr, value)


if __name__ == "__main__":
    hub_app = FarmHub()

    dispatcher = OutboundCommandDispatcher(hub_app)
    dispatch_thread = threading.Thread(target=dispatcher.poll_loop, daemon=True)
    dispatch_thread.start()

    try:
        while True:
            time.sleep(30)
            if not hub_app.transfer_active:
                hub_app.cmd_dest.announce(app_data=b"agronomi_hub")
    except KeyboardInterrupt:
        dispatcher.running = False
        sys.exit(0)
