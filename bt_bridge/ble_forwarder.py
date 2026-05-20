"""
ble_forwarder.py — BLE gateway serial reader → Reticulum forwarder
AgroNomi Field Gateway — Mimi

Reads JSON telemetry lines from Pico 2W over USB serial,
injects gateway_id, and forwards to reticulum_ingest.py
on Mac Mini via RNS using a SINGLE destination with automatic
announce-based path discovery.

**Why SINGLE instead of PLAIN?**
  PLAIN destinations don't work reliably when sending through a
  shared Reticulum instance (e.g. MeshChat on Mimi). A shared
  instance cannot route PLAIN packets to destinations registered on
  local clients. SINGLE destinations solve this: the sender discovers
  the ingest daemon's identity via its announce, then creates an OUT
  SINGLE destination that the shared Reticulum can route properly.

**Proof strategy (PROVE_ALL):**
  Both the sender (OUT) and receiver (IN) use PROVE_ALL. The receiver
  sends delivery proofs back to the sender, which validates that the
  shared-instance routing path is working and provides feedback that
  packets actually arrived. Without proofs, there is no way to tell
  whether packets are being silently dropped.

**Announce-based discovery pattern:**
1. The ingest daemon (reticulum_ingest.py) creates an IN SINGLE
   destination with PROVE_ALL and periodically calls
   `destination.announce()`.
2. This forwarder registers an announce handler that listens for
   announces matching the app name "farm" and aspect
   "telemetry_readings".
3. When the announce arrives, the handler captures the destination
   hash and remote identity.
4. From then on, the sender creates an OUT SINGLE destination with
   PROVE_ALL using the discovered identity and sends packets through
   the shared Reticulum instance — no manual hash copy-pasting needed.
   Delivery proofs flow back from the ingest daemon to confirm
   that packets arrived.

Usage:
    python ble_forwarder.py [--config path/to/ble_forwarder.toml]

Dependencies:
    pip install rns pyserial tomllib  # tomllib is stdlib in Python 3.11+
"""

import argparse
import json
import logging
import os
import sys
import threading
import time

import RNS
import serial

# ---------------------------------------------------------------------------
# LOGGING — single file + journal, everything visible in one place
# ---------------------------------------------------------------------------

GATEWAY_LOG = os.environ.get("AGRONOMI_LOG", os.path.expanduser("~/agronomi.log"))


class RNSLogHandler(logging.Handler):
    """Captures RNS log output and writes it to both stderr (for journal)
    and the unified log file."""

    def emit(self, record):
        try:
            msg = record.getMessage()
            print(msg, flush=True)
            # Also write to unified log file
            with open(GATEWAY_LOG, "a") as f:
                f.write(msg + "\n")
        except Exception:
            pass


def setup_logging():
    """Set up unified logging — tee all stdout/stderr to the log file."""
    import io

    log_fh = open(GATEWAY_LOG, "a")

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

    sys.stdout = TeeStream(sys.__stdout__, log_fh)
    sys.stderr = TeeStream(sys.__stderr__, log_fh)
    print(f"[GW] === All logs now go to {GATEWAY_LOG} ===")

    # Also add the Python logging handler for any code that uses logging module
    rns_logger = logging.getLogger("RNS")
    rns_logger.addHandler(RNSLogHandler())
    rns_logger.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------


def load_config(path: str) -> dict:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            RNS.log(
                "[CFG] tomllib not found. Install tomli: pip install tomli",
                RNS.LOG_CRITICAL,
            )
            sys.exit(1)

    with open(path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# GATEWAY IDENTITY — persistent RNS identity for the command destination
# ---------------------------------------------------------------------------


def load_or_create_identity(path: str) -> RNS.Identity:
    """Load a persistent RNS identity from file, or create and save a new one.

    This identity is used for the gateway's SINGLE command destination.
    It must be persistent so the destination hash doesn't change between
    restarts — the hub's database stores the hash to reach this gateway.
    """
    if os.path.exists(path):
        identity = RNS.Identity.from_file(path)
        if identity is not None:
            RNS.log(f"[ID] Loaded gateway identity from {path}", RNS.LOG_INFO)
            return identity
        RNS.log(f"[WARN] Identity file corrupt, generating new one", RNS.LOG_WARNING)

    identity = RNS.Identity()
    identity.to_file(path)
    RNS.log(f"[ID] Generated new gateway identity, saved to {path}", RNS.LOG_INFO)
    return identity


# ---------------------------------------------------------------------------
# RNS SENDER — SINGLE destination with announce-based path discovery
# ---------------------------------------------------------------------------


class TelemetrySender:
    """Sends telemetry payloads via a RNS SINGLE destination.

    Uses announce-based discovery to find the ingest daemon at
    runtime — no manual destination hash configuration needed.

    The sender registers an announce handler for the
    "farm.telemetry_readings" aspect. When the ingest daemon
    announces, the handler captures the destination hash and
    remote identity. From then on, packets are sent through an
    OUT SINGLE destination that the shared Reticulum instance can
    route properly.
    """

    # Announce handler filter — must match "app.aspect" of the
    # ingest daemon's destination.
    aspect_filter = "farm.telemetry_readings"

    def __init__(self):
        self.app = "farm"
        self.aspect = "telemetry_readings"
        self._destination_hash = None
        self._remote_identity = None
        self._resolved = threading.Event()

        # Register ourselves as an announce handler so RNS calls
        # received_announce() when the ingest daemon broadcasts.
        RNS.Transport.register_announce_handler(self)

    # -- announce handler callback ----------------------------------------

    def received_announce(self, destination_hash, announced_identity, app_data):
        """Called by RNS Transport when an announce matching our
        aspect_filter is received.

        Captures the ingest daemon's destination hash and identity
        so we can create an OUT SINGLE destination for sending.
        Also proactively requests the path to ensure routing is
        established even if the announce arrived via a different
        interface.
        """
        RNS.log(
            f"[RNS] Discovered ingest daemon: {RNS.prettyhexrep(destination_hash)}",
            RNS.LOG_INFO,
        )
        if app_data:
            try:
                label = app_data.decode("utf-8", errors="replace")
                RNS.log(f"[RNS] Ingest app_data: {label}", RNS.LOG_INFO)
            except Exception:
                RNS.log(f"[RNS] Ingest app_data (raw): {app_data!r}", RNS.LOG_DEBUG)
        else:
            RNS.log("[RNS] Ingest app_data: (none)", RNS.LOG_DEBUG)
        self._destination_hash = destination_hash
        self._remote_identity = announced_identity
        # Proactively request path to ensure routing is established
        # even if the announce arrived via a different interface.
        RNS.Transport.request_path(destination_hash)
        self._resolved.set()

    # -- path discovery ---------------------------------------------------

    def wait_for_ingest(self, timeout: float = 30.0) -> bool:
        """Block until the ingest daemon has been discovered via
        announce, or *timeout* seconds elapse.

        Returns True if the daemon was discovered (or was already
        known), False on timeout.
        """
        if self._resolved.is_set():
            return True

        RNS.log(
            "[RNS] Waiting for ingest daemon to announce...",
            RNS.LOG_INFO,
        )

        return self._resolved.wait(timeout=timeout)

    # -- send -------------------------------------------------------------

    def send(self, payload: dict) -> bool:
        """Send a telemetry payload dict to the ingest daemon.

        If the ingest daemon hasn't been discovered yet, waits up
        to 10 seconds for an announce before giving up.

        Returns True on success, False on failure.
        """
        if not self._resolved.is_set():
            if not self.wait_for_ingest(timeout=10.0):
                RNS.log(
                    "[RNS] Ingest daemon not discovered yet, dropping packet",
                    RNS.LOG_WARNING,
                )
                return False

        # Recall the identity (stored by RNS when the announce was
        # received; also saved in received_announce as a fallback).
        if self._remote_identity is None:
            self._remote_identity = RNS.Identity.recall(self._destination_hash)
            if self._remote_identity is None:
                RNS.log("[RNS] Could not recall identity", RNS.LOG_WARNING)
                return False

        destination = RNS.Destination(
            self._remote_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            self.app,
            self.aspect,
        )
        # Set PROVE_ALL on the OUT destination so that the receiver's
        # PROVE_ALL proof strategy can send delivery confirmations back.
        # This validates that the routing path is working and gives the
        # sender feedback that packets actually arrived.
        destination.set_proof_strategy(RNS.Destination.PROVE_ALL)

        data = json.dumps(payload).encode("utf-8")
        packet = RNS.Packet(destination, data)
        receipt = packet.send()

        if receipt is not None:
            # Set a delivery callback for diagnostic feedback —
            # confirms packets actually reach the ingest daemon.
            def on_delivery(receipt_obj):
                RNS.log(
                    f"[RNS] Delivery confirmed for telemetry from "
                    f"{payload.get('dev_id', '?')} seq={payload.get('seq', '?')}",
                    RNS.LOG_INFO,
                )

            def on_timeout(receipt_obj):
                RNS.log(
                    f"[RNS] Delivery timed out for telemetry from "
                    f"{payload.get('dev_id', '?')} seq={payload.get('seq', '?')}",
                    RNS.LOG_WARNING,
                )

            receipt.set_delivery_callback(on_delivery)
            receipt.set_timeout(30.0)  # 30-second timeout for LoRa
            receipt.set_timeout_callback(on_timeout)

            RNS.log(
                f"[RNS] Sent telemetry from {payload.get('dev_id', '?')} "
                f"seq={payload.get('seq', '?')} "
                f"(dest={RNS.prettyhexrep(destination.hash)}…)",
                RNS.LOG_INFO,
            )
            return True
        else:
            RNS.log("[RNS] Packet send returned None", RNS.LOG_WARNING)
            return False

    def send_ack(self, ack_payload: dict) -> bool:
        """Send an ACK payload to the hub's command ACK destination.

        Uses the same announce-based discovery as telemetry, but targets
        the farm.commands_control aspect instead of farm.telemetry_readings.
        """
        # Create a temporary destination for command ACKs
        if self._remote_identity is None:
            self._remote_identity = RNS.Identity.recall(self._destination_hash)
            if self._remote_identity is None:
                RNS.log(
                    "[RNS] Cannot send ACK — no hub identity discovered",
                    RNS.LOG_WARNING,
                )
                return False

        # The command ACK destination uses the same identity but different aspect
        ack_dest = RNS.Destination(
            self._remote_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            self.app,
            "commands_control",
        )
        ack_dest.set_proof_strategy(RNS.Destination.PROVE_ALL)

        data = json.dumps(ack_payload).encode("utf-8")
        packet = RNS.Packet(ack_dest, data)
        receipt = packet.send()

        if receipt is not None:
            RNS.log(
                f"[RNS] Sent ACK for cmd {ack_payload.get('cmd_id', '?')}", RNS.LOG_INFO
            )
            return True
        else:
            RNS.log("[RNS] ACK send returned None", RNS.LOG_WARNING)
            return False


# ---------------------------------------------------------------------------
# GATEWAY COMMAND RECEIVER — SINGLE destination for hub→gateway commands
# ---------------------------------------------------------------------------


class GatewayCommandReceiver:
    """Receives commands from the hub via a SINGLE destination.

    The gateway creates a persistent RNS identity (saved to disk) and
    registers an IN SINGLE destination that the hub's CommandDispatcher
    can send packets to. The gateway announces this destination so
    the hub can discover it automatically.

    When a command packet arrives, it is parsed and written to the
    Pico serial port as a [CMD] line for relay to the C6 node.
    """

    COMMAND_APP = "farm"

    def __init__(
        self,
        identity: RNS.Identity,
        aspect: str,
        gateway_id: str,
        ser: serial.Serial,
        config: dict = None,
        telemetry_sender=None,
    ):
        self.destination = RNS.Destination(
            identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            self.COMMAND_APP,
            aspect,
        )
        self.destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        self.destination.set_packet_callback(self._on_packet)
        self.gateway_id = gateway_id
        self.ser = ser
        self._config = config or {}
        self._dest_hash_hex = RNS.prettyhexrep(self.destination.hash)

        # Track pending firmware data from RNS Resource transfers.
        # Key: (fw_version, device_type) -> bytes of firmware binary.
        # When an ota_request command arrives, we check this dict first,
        # then fall back to the disk cache.
        self._pending_firmware = {}
        self._telemetry_sender = telemetry_sender
        self._pending_ble_ota = (
            None  # set by _handle_ota_command when BLE fails to connect
        )
        self._pico_connected = False  # True when Pico reports [C] (BLE connected)
        self._pico_mtu = 23  # Default BLE ATT MTU; updated from [MTU] serial line
        self._ota_ack_waiter = None  # OtaAckWaiter when OTA is in progress

        # Register link establishment callback for RNS Resource (OTA) transfers.
        # The hub opens a Link to this destination, then sends the firmware
        # binary as an RNS Resource over that link.
        self.destination.set_link_established_callback(self._on_link_established)

        RNS.log(
            f"[CMD] Gateway command destination: {self.COMMAND_APP}.{aspect} "
            f"hash: {self._dest_hash_hex}",
            RNS.LOG_INFO,
        )

    def announce(self):
        """Announce the command destination so the hub can discover it."""
        app_data = f"agronomi-gateway:{self.gateway_id}".encode("utf-8")
        self.destination.announce(app_data=app_data)
        RNS.log(f"[CMD] Announced command destination", RNS.LOG_INFO)

    def _on_link_established(self, link):
        """Called when the hub establishes an RNS Link to this gateway.

        This happens when the hub wants to send a firmware binary via
        RNS Resource (for OTA). We accept the link and register a
        resource callback to receive the binary data.
        """
        RNS.log(
            f"[CMD] Link established from {RNS.prettyhexrep(link.destination.hash)}",
            RNS.LOG_INFO,
        )
        link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
        link.set_resource_concluded_callback(self._on_resource)

    def _on_resource(self, resource):
        """Called when an RNS Resource transfer is concluded.

        IMPORTANT: For large resources, RNS stores the reassembled data in a
        temp file and sets resource.data to an open BufferedReader, NOT bytes.
        RNS closes the file handle and deletes the temp file immediately after
        this callback returns. We MUST read the data here before returning.
        See RNS Resource.py L737: self.data = open(self.storagepath, "rb")
        followed by close() + unlink() after callback.

        We also save to disk cache IMMEDIATELY here, using a temporary filename
        based on the SHA-256 of the data. This ensures the firmware persists even
        if the gateway process crashes before the ota_request command arrives.
        When the command arrives, _handle_ota_command will rename the cached file
        to its proper version/type path.
        """
        import hashlib

        from fw_cache import OTA_CACHE_DIR, save_firmware_to_cache

        if resource.status == RNS.Resource.COMPLETE:
            # resource.data may be bytes (small resource) or BufferedReader
            # (large resource stored to temp file). Read it before returning.
            raw = resource.data
            if isinstance(raw, bytes):
                data = raw
            else:
                # BufferedReader — must .read() now; RNS deletes the file after
                # this callback returns (Resource.py L743-744).
                data = raw.read()
            RNS.log(f"[OTA] Resource received: {len(data)} bytes", RNS.LOG_INFO)

            # Compute SHA-256 immediately for cache identification
            data_sha256 = hashlib.sha256(data).hexdigest()
            RNS.log(
                f"[OTA] Firmware SHA-256: {data_sha256[:16]}... ({len(data)} bytes)",
                RNS.LOG_INFO,
            )

            # Save to disk cache IMMEDIATELY under a temporary name based on
            # SHA-256. This ensures the firmware survives a process crash or
            # restart — we don't have to re-transfer 1.4MB over LoRa.
            # _handle_ota_command will rename this to the proper version/type path
            # when it knows the fw_version and device_type from the command.
            try:
                tmp_dir = os.path.join(OTA_CACHE_DIR, "_pending")
                os.makedirs(tmp_dir, exist_ok=True)
                tmp_bin = os.path.join(tmp_dir, f"{data_sha256}.bin")
                tmp_sha = os.path.join(tmp_dir, f"{data_sha256}.bin.sha256")
                with open(tmp_bin, "wb") as f:
                    f.write(data)
                with open(tmp_sha, "w") as f:
                    f.write(data_sha256)
                RNS.log(
                    f"[OTA] Firmware saved to pending cache: {data_sha256[:16]}...bin",
                    RNS.LOG_INFO,
                )
            except Exception as e:
                RNS.log(
                    f"[OTA] Failed to save firmware to pending cache: {e}",
                    RNS.LOG_ERROR,
                )

            # Also keep in memory for immediate use by _handle_ota_command
            self._pending_firmware["_latest"] = data
            RNS.log(
                f"[OTA] Firmware binary cached in memory, awaiting ota_request command",
                RNS.LOG_INFO,
            )
        else:
            RNS.log(
                f"[OTA] Resource transfer failed (status={resource.status})",
                RNS.LOG_ERROR,
            )
            self._pending_firmware.pop("_latest", None)

    def _on_packet(self, data: bytes, packet: RNS.Packet):
        """Handle incoming command packets from the hub.

        For regular actuator commands, writes to Pico serial as [CMD].
        For ota_request commands, triggers the BLE OTA relay pipeline.
        """
        RNS.log(f"[CMD] Received command packet: {len(data)} bytes", RNS.LOG_INFO)
        try:
            cmd = json.loads(data.decode("utf-8"))
            cmd_type = cmd.get("cmd_type", "unknown")
            device_id = cmd.get("device_id", "?")
            RNS.log(f"[CMD] {cmd_type} for {device_id}: {cmd}", RNS.LOG_INFO)

            if cmd_type == "ota_request":
                # OTA commands are handled via BLE OTA relay
                # The firmware binary should have been delivered via RNS Resource
                # and cached by fw_cache before this command arrives.
                # Check cache and trigger the BLE flash.
                self._handle_ota_command(cmd)
            else:
                # Regular actuator command — write to Pico serial as [CMD] line
                cmd_line = f"[CMD] {json.dumps(cmd)}\n"
                self.ser.write(cmd_line.encode("utf-8"))
                RNS.log(
                    f"[CMD] Written to serial: {cmd_type} -> {device_id}", RNS.LOG_INFO
                )

        except json.JSONDecodeError as e:
            RNS.log(f"[CMD] Invalid JSON in command packet: {e}", RNS.LOG_WARNING)
        except Exception as e:
            RNS.log(f"[CMD] Error processing command: {e}", RNS.LOG_ERROR)

    def _handle_ota_command(self, cmd: dict):
        """Handle an ota_request command by checking cache and sending OTA via Pico serial."""
        import hashlib

        from ble_ota import (
            OTA_MAX_BLE_RETRIES,
            OtaAckWaiter,
            get_ble_mac,
            send_ota_frames_via_serial,
        )
        from fw_cache import (
            OTA_CACHE_DIR,
            get_cached_firmware,
            save_firmware_to_cache,
            verify_cached_firmware,
        )

        device_id = cmd.get("device_id", "?")
        # cmd_value_text may contain JSON with fw_version, device_type, sha256
        cmd_value_text = cmd.get("cmd_value_text", "{}")
        try:
            meta = (
                json.loads(cmd_value_text)
                if isinstance(cmd_value_text, str)
                else cmd_value_text
            )
        except (json.JSONDecodeError, TypeError):
            meta = {}

        fw_version = meta.get("fw_version", cmd.get("fw_version", ""))
        device_type = meta.get("device_type", cmd.get("device_type", ""))
        sha256 = meta.get("sha256", cmd.get("sha256", ""))

        if not fw_version or not device_type:
            RNS.log(
                f"[OTA] Missing fw_version or device_type in ota_request", RNS.LOG_ERROR
            )
            return

        RNS.log(
            f"[OTA] Processing ota_request for {device_id} → {fw_version}", RNS.LOG_INFO
        )

        # Check if firmware is available from any source:
        # 1. In-memory (from RNS Resource callback in this process)
        # 2. Pending cache (from RNS Resource callback, saved to disk by SHA-256)
        # 3. Versioned cache (from a previous successful save with version/type)
        firmware_data = None

        # Source 1: In-memory from RNS Resource callback
        if "_latest" in self._pending_firmware:
            firmware_data = self._pending_firmware.pop("_latest")
            RNS.log(
                f"[OTA] Using firmware from RNS Resource ({len(firmware_data)} bytes)",
                RNS.LOG_INFO,
            )

        # Source 2: Pending cache — firmware saved by _on_resource under SHA-256 name
        # This handles the case where the process restarted after Resource delivery
        # but before the ota_request command arrived.
        if firmware_data is None and sha256:
            tmp_dir = os.path.join(OTA_CACHE_DIR, "_pending")
            tmp_bin = os.path.join(tmp_dir, f"{sha256}.bin")
            if os.path.isfile(tmp_bin):
                RNS.log(
                    f"[OTA] Found firmware in pending cache: {sha256[:16]}...bin",
                    RNS.LOG_INFO,
                )
                with open(tmp_bin, "rb") as f:
                    firmware_data = f.read()
                actual_sha = hashlib.sha256(firmware_data).hexdigest()
                if actual_sha != sha256:
                    RNS.log(
                        f"[OTA] Pending cache SHA-256 mismatch! expected={sha256[:16]}... got={actual_sha[:16]}...",
                        RNS.LOG_ERROR,
                    )
                    firmware_data = None

        # Source 3: Versioned disk cache (from a previous full save)
        if firmware_data is None and sha256:
            if verify_cached_firmware(fw_version, device_type, sha256):
                firmware_data = get_cached_firmware(fw_version, device_type, sha256)
                if firmware_data:
                    RNS.log(
                        f"[OTA] Using cached firmware for {device_type} {fw_version} ({len(firmware_data)} bytes)",
                        RNS.LOG_INFO,
                    )

        # Save to versioned disk cache for future reuse
        if firmware_data is not None and fw_version and device_type and sha256:
            try:
                saved = save_firmware_to_cache(
                    fw_version, device_type, firmware_data, sha256
                )
                if saved:
                    RNS.log(
                        f"[OTA] Firmware saved to disk cache: {fw_version}/{device_type}.bin",
                        RNS.LOG_INFO,
                    )
                    # Clean up pending cache since we now have the versioned copy
                    tmp_dir = os.path.join(OTA_CACHE_DIR, "_pending")
                    tmp_bin = os.path.join(tmp_dir, f"{sha256}.bin")
                    tmp_sha = os.path.join(tmp_dir, f"{sha256}.bin.sha256")
                    for p in (tmp_bin, tmp_sha):
                        if os.path.isfile(p):
                            os.remove(p)
                else:
                    RNS.log(
                        f"[OTA] Firmware cache save FAILED — SHA-256 mismatch!",
                        RNS.LOG_ERROR,
                    )
            except Exception as e:
                RNS.log(
                    f"[OTA] Firmware cache save error: {e}",
                    RNS.LOG_ERROR,
                )

        if firmware_data is None:
            RNS.log(
                f"[OTA] Firmware not available for {device_type} {fw_version} — "
                f"needs RNS Resource delivery or cache population",
                RNS.LOG_WARNING,
            )
            return

        # Send OTA frames via Pico serial relay. The Pico forwards binary
        # frames to the sensor over BLE NUS. If the sensor is not connected,
        # the Pico will drop the frames (logged as [NOP]) and we'll store
        # the OTA for retry when the sensor advertises.
        #
        # Check if the Pico has an active BLE connection to the sensor.
        # The serial loop tracks this via [C] and [D] lines from the Pico.
        # If connected, send immediately. If not, store for later.
        def send_ota_ack(
            cmd_id: int, status: str, fw_version: str = None, error: str = None
        ):
            ack_payload = {"cmd_id": cmd_id, "status": status}
            if fw_version:
                ack_payload["fw_version"] = fw_version
            if error:
                ack_payload["error"] = error
            ack_payload["gateway_id"] = self.gateway_id
            self._telemetry_sender.send_ack(ack_payload)

        ble_mac = get_ble_mac(device_id, self._config, cmd)
        if not ble_mac:
            RNS.log(f"[OTA] No BLE MAC for {device_id} — cannot flash", RNS.LOG_ERROR)
            send_ota_ack(
                cmd.get("cmd_id", -1),
                status="failed",
                error=f"No BLE MAC for {device_id}",
            )
            return

        # If the Pico has a BLE connection, send OTA immediately.
        # Otherwise store for later triggering when sensor advertises.
        if self._pico_connected:
            RNS.log(
                f"[OTA] Pico has BLE connection — sending OTA for {device_id} via serial",
                RNS.LOG_INFO,
            )

            # FIX: run OTA in a dedicated thread — never call
            # send_ota_frames_via_serial() directly from the RNS packet
            # callback (here) or from the serial read loop. It blocks for
            # the full OTA duration (tens of seconds) waiting on
            # ack_waiter.wait(). The ack_waiter is fed by the serial read
            # loop via cmd_receiver._ota_ack_waiter.set(). If we block
            # the serial read loop, the ACK can never arrive → deadlock
            # → always times out.
            ack_waiter = OtaAckWaiter()
            self._ota_ack_waiter = ack_waiter

            def run_ota_immediate():
                success, err = send_ota_frames_via_serial(
                    self.ser,
                    firmware_data,
                    fw_version,
                    ack_waiter,
                    device_id,
                    pico_connected_check=lambda: self._pico_connected,
                    mtu=lambda: self._pico_mtu,  # callable — reads live MTU
                )
                self._ota_ack_waiter = None

                if success:
                    RNS.log(
                        f"[OTA] {device_id} flashed successfully → {fw_version}",
                        RNS.LOG_INFO,
                    )
                    send_ota_ack(
                        cmd.get("cmd_id", -1),
                        status="acknowledged",
                        fw_version=fw_version,
                    )
                else:
                    RNS.log(
                        f"[OTA] OTA via Pico serial failed for {device_id}: {err}. "
                        f"Storing for retry on next sensor wake cycle.",
                        RNS.LOG_WARNING,
                    )
                    # Store for retry on next sensor wake cycle.
                    self._pending_ble_ota = {
                        "cmd": cmd,
                        "firmware_data": firmware_data,
                        "ble_mac": ble_mac,
                        "fw_version": fw_version,
                        "attempts": 1,
                    }

            import threading as _threading
            _threading.Thread(target=run_ota_immediate, daemon=True).start()
            return  # serial read loop continues immediately

        else:
            RNS.log(
                f"[OTA] No BLE connection — storing OTA for {device_id} "
                f"until sensor advertises.",
                RNS.LOG_WARNING,
            )

        # Store for later triggering when we see [C] (BLE connected) on serial.
        self._pending_ble_ota = {
            "cmd": cmd,
            "firmware_data": firmware_data,
            "ble_mac": ble_mac,
            "fw_version": fw_version,
            "attempts": 0,
        }

    def has_pending_ble_ota(self) -> bool:
        """Check if there's a pending BLE OTA that hasn't been triggered yet."""
        return self._pending_ble_ota is not None

    def trigger_pending_ble_ota(self):
        """Trigger the pending BLE OTA attempt via Pico serial relay.

        Called from the serial loop when we see '[C]' (BLE connected)
        from a sensor with a pending OTA. The sensor is only connected
        briefly, so we must act immediately.

        The OTA transfer runs in a separate thread so the serial read
        loop stays alive to detect disconnects and read ACKs.
        """
        if self._pending_ble_ota is None:
            return

        import threading

        from ble_ota import (
            OTA_MAX_BLE_RETRIES,
            OtaAckWaiter,
            send_ota_frames_via_serial,
        )

        cmd = self._pending_ble_ota["cmd"]
        firmware_data = self._pending_ble_ota["firmware_data"]
        fw_version = self._pending_ble_ota["fw_version"]
        attempts = self._pending_ble_ota.get("attempts", 0) + 1
        device_id = cmd.get("device_id", "?")

        RNS.log(
            f"[OTA] Triggering pending BLE OTA for {device_id} "
            f"(attempt {attempts}) — sensor is connected now",
            RNS.LOG_INFO,
        )

        self._pending_ble_ota["attempts"] = attempts

        def send_ota_ack(
            cmd_id: int, status: str, fw_version: str = None, error: str = None
        ):
            ack_payload = {"cmd_id": cmd_id, "status": status}
            if fw_version:
                ack_payload["fw_version"] = fw_version
            if error:
                ack_payload["error"] = error
            ack_payload["gateway_id"] = self.gateway_id
            self._telemetry_sender.send_ack(ack_payload)

        ack_waiter = OtaAckWaiter()
        self._ota_ack_waiter = ack_waiter

        def run_ota():
            success, error = send_ota_frames_via_serial(
                self.ser,
                firmware_data,
                fw_version,
                ack_waiter,
                device_id,
                pico_connected_check=lambda: self._pico_connected,
                mtu=lambda: self._pico_mtu,  # callable — reads live MTU after BEGIN delay
            )
            self._ota_ack_waiter = None

            if success:
                RNS.log(
                    f"[OTA] {device_id} flashed successfully → {fw_version}",
                    RNS.LOG_INFO,
                )
                self._pending_ble_ota = None  # clear — OTA done
                send_ota_ack(
                    cmd.get("cmd_id", -1),
                    status="acknowledged",
                    fw_version=fw_version,
                )
                return

            # Failed — keep pending for next wake cycle, up to max retries
            if attempts >= OTA_MAX_BLE_RETRIES:
                RNS.log(
                    f"[OTA] {device_id} failed after {attempts} BLE attempts",
                    RNS.LOG_ERROR,
                )
                send_ota_ack(
                    cmd.get("cmd_id", -1),
                    status="failed",
                    error=f"BLE OTA failed after {attempts} attempts: {error}",
                )
                self._pending_ble_ota = None  # clear — give up
            else:
                RNS.log(
                    f"[OTA] BLE attempt {attempts} failed for {device_id}: {error}. "
                    f"Will retry on next sensor wake cycle.",
                    RNS.LOG_WARNING,
                )

        ota_thread = threading.Thread(target=run_ota, daemon=True)
        ota_thread.start()

    @property
    def destination_hash_hex(self) -> str:
        """Return the hex representation of this destination's hash.

        This is what needs to go in reticulum_gateways.rns_destination_hash
        in the hub's database so the CommandDispatcher can reach us.
        """
        return self._dest_hash_hex


# ---------------------------------------------------------------------------
# SERIAL READER
# ---------------------------------------------------------------------------


def open_serial(port: str, baud: int, retries: int = 10) -> serial.Serial:
    """Open serial port and wait for Pico boot banner.

    The Pico sends [SER] lines on boot. We read for up to 10s
    to confirm the Pico is alive and outputting data. If no
    output is seen, we still return the port — the Pico may
    just be slow to start (boot.py delay, BLE init, etc.).
    """
    for attempt in range(retries):
        try:
            s = serial.Serial(port, baud, timeout=1.0)
            RNS.log(
                f"[SER] Opened {port} at {baud} baud",
                RNS.LOG_INFO,
            )

            # Drain any stale data, then wait for Pico boot banner
            time.sleep(1)
            raw = s.read(s.in_waiting) if s.in_waiting else b""
            if raw:
                RNS.log(
                    f"[SER] Drained {len(raw)} bytes stale data after open",
                    RNS.LOG_INFO,
                )
                for line in raw.decode("utf-8", errors="replace").split("\n"):
                    line = line.strip()
                    if line:
                        RNS.log(f"[SER] Pico boot: {line}", RNS.LOG_INFO)

            # Wait up to 10s for Pico to send its boot banner
            boot_seen = False
            deadline = time.time() + 10
            while time.time() < deadline and not boot_seen:
                line_bytes = s.readline()
                if not line_bytes:
                    continue
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                RNS.log(f"[SER] Pico: {line}", RNS.LOG_INFO)
                if (
                    line.startswith("[SER]")
                    or line.startswith("Advertising")
                    or line.startswith("rx=")
                ):
                    boot_seen = True

            if boot_seen:
                RNS.log("[SER] Pico boot confirmed — output detected", RNS.LOG_INFO)
            else:
                RNS.log(
                    "[SER] No Pico output after 10s — Pico may not be sending data. "
                    "Check: 1) boot.py is on Pico, 2) Pico is running main.py, "
                    "3) USB cable carries data (not charge-only)",
                    RNS.LOG_WARNING,
                )
            return s
        except serial.SerialException as e:
            RNS.log(
                f"[SER] Could not open {port}: {e} (attempt {attempt + 1}/{retries})",
                RNS.LOG_WARNING,
            )
            time.sleep(3.0)
    RNS.log(f"[SER] Failed to open {port} after {retries} attempts", RNS.LOG_CRITICAL)
    sys.exit(1)


def run_forwarder(config: dict, identity: RNS.Identity):
    gateway_id = config["gateway"]["gateway_id"]
    serial_port = config["gateway"]["serial_port"]
    serial_baud = config["gateway"].get("serial_baud", 115200)

    RNS.log(f"[GW] Gateway ID : {gateway_id}", RNS.LOG_INFO)
    RNS.log(f"[GW] Serial     : {serial_port} @ {serial_baud}", RNS.LOG_INFO)
    RNS.log(
        "[GW] Destination: SINGLE farm/telemetry_readings (announce-based discovery)",
        RNS.LOG_INFO,
    )

    sender = TelemetrySender()
    ser = open_serial(serial_port, serial_baud)

    # Create the command receiver for hub→gateway commands
    cmd_aspect = config["gateway"].get("command_aspect", "gateway_commands")
    cmd_receiver = GatewayCommandReceiver(
        identity, cmd_aspect, gateway_id, ser, config=config, telemetry_sender=sender
    )

    # Announce immediately and set up periodic announce
    cmd_receiver.announce()
    last_announce = time.time()

    RNS.log(
        f"[CMD] Gateway destination hash: {cmd_receiver.destination_hash_hex}",
        RNS.LOG_INFO,
    )
    RNS.log(f"[CMD] Add this hash to the hub's reticulum_gateways table!", RNS.LOG_INFO)

    RNS.log("[GW] Forwarder running. Waiting for sensor data...", RNS.LOG_INFO)

    serial_idle_count = 0
    last_heartbeat = time.time()

    while True:
        # Periodic re-announce every 30 seconds
        now = time.time()
        if now - last_announce >= 30:
            cmd_receiver.announce()
            last_announce = now

        # Periodic heartbeat so we know the serial loop is alive
        if now - last_heartbeat >= 60:
            serial_idle_count += 1
            if serial_idle_count >= 5:
                # No Pico data for 5+ minutes — something is wrong
                RNS.log(
                    "[GW] No data from Pico for %d minutes — "
                    "check Pico serial connection" % serial_idle_count,
                    RNS.LOG_WARNING,
                )
            else:
                RNS.log(
                    "[GW] Heartbeat — serial loop alive, no data from Pico",
                    RNS.LOG_INFO,
                )
            last_heartbeat = now

        try:
            line = ser.readline()
            if not line:
                continue

            line = line.decode("utf-8", errors="replace").strip()

            if not line.startswith("[JSON] "):
                # Check for ACK lines from the Pico — these include OTA ACKs
                # from the sensor relayed through the Pico
                if line.startswith("[ACK] "):
                    raw_ack = line[len("[ACK] ") :]
                    try:
                        ack_payload = json.loads(raw_ack)
                        # Add gateway metadata
                        ack_payload["gateway_id"] = gateway_id
                        sender.send_ack(ack_payload)
                        # If this is an OTA ACK and we have a waiter, feed it
                        if "ota_ok" in ack_payload and cmd_receiver._ota_ack_waiter:
                            cmd_receiver._ota_ack_waiter.set(ack_payload)
                    except json.JSONDecodeError as e:
                        RNS.log(
                            f"[SER] ACK JSON parse error: {e} — line: {raw_ack[:80]}",
                            RNS.LOG_WARNING,
                        )
                    continue
                # Pico heartbeat — log and reset idle counter
                if line.startswith("[HB]"):
                    serial_idle_count = 0
                    RNS.log(f"[SER] {line}", RNS.LOG_INFO)
                    continue
                # Pico boot banner lines — log and mark Pico as alive
                if line.startswith("[SER]") or line.startswith("rx="):
                    serial_idle_count = 0
                    RNS.log(f"[SER] {line}", RNS.LOG_INFO)
                    continue
                # Pico BLE connection events
                if line.startswith("[C] "):
                    serial_idle_count = 0
                    RNS.log(f"[SER] {line}", RNS.LOG_INFO)
                    cmd_receiver._pico_connected = True
                    # Parse MTU from [C] <mtu> line — Pico reports negotiated
                    # MTU inline with the connect event, not as a separate [MTU] line.
                    try:
                        mtu_val = int(line.split()[1])
                        cmd_receiver._pico_mtu = mtu_val
                        RNS.log(
                            f"[SER] BLE MTU from connect event: {mtu_val} "
                            f"(max OTA payload: {mtu_val - 8})",
                            RNS.LOG_INFO,
                        )
                    except (ValueError, IndexError):
                        pass  # no MTU in line — keep existing value
                    # If we have a pending BLE OTA, trigger it now
                    if cmd_receiver.has_pending_ble_ota():
                        RNS.log(
                            "[OTA] BLE connected — triggering pending BLE OTA",
                            RNS.LOG_INFO,
                        )
                        cmd_receiver.trigger_pending_ble_ota()
                    continue
                # Pico MTU report — negotiated BLE ATT MTU from Pico.
                # Format: [MTU] <mtu_value> payload=<max_payload>
                # Used by OTA to size chunks correctly.
                if line.startswith("[MTU]"):
                    serial_idle_count = 0
                    try:
                        mtu_val = int(line.split()[1])
                        cmd_receiver._pico_mtu = mtu_val
                        RNS.log(
                            f"[SER] BLE MTU negotiated: {mtu_val} (max payload: {mtu_val - 3})",
                            RNS.LOG_INFO,
                        )
                    except (ValueError, IndexError):
                        RNS.log(
                            f"[SER] Could not parse MTU line: {line}", RNS.LOG_WARNING
                        )
                    continue
                if line.startswith("[D] "):
                    serial_idle_count = 0
                    RNS.log(f"[SER] {line}", RNS.LOG_INFO)
                    cmd_receiver._pico_connected = False
                    cmd_receiver._pico_mtu = 23  # Reset to default on disconnect
                    continue
                # Sensor is advertising — if we have a pending BLE OTA,
                # wait for the [C] connection event instead, as the Pico
                # will connect during the advertising window.
                if line.strip() == "Advertising":
                    serial_idle_count = 0
                    RNS.log(f"[SER] {line}", RNS.LOG_INFO)
                    # Don't trigger OTA on Advertising alone — wait for [C]
                    # which means the Pico actually connected to the sensor.
                    continue
                # Pass-through all other lines to our log
                if line:
                    RNS.log(f"[SER] {line}", RNS.LOG_INFO)
                continue

            raw_json = line[len("[JSON] ") :]

            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError as e:
                RNS.log(
                    f"[SER] JSON parse error: {e} — line: {raw_json[:80]}",
                    RNS.LOG_WARNING,
                )
                continue

            # Inject gateway metadata
            payload["gateway_id"] = gateway_id

            # Reset idle counter — we received sensor data
            serial_idle_count = 0

            sender.send(payload)

        except serial.SerialException as e:
            RNS.log(f"[SER] Serial error: {e}. Reconnecting...", RNS.LOG_ERROR)
            time.sleep(2.0)
            ser = open_serial(serial_port, serial_baud)

        except KeyboardInterrupt:
            RNS.log("[GW] Shutting down.", RNS.LOG_INFO)
            ser.close()
            sys.exit(0)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="BLE gateway → Reticulum forwarder")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "ble_forwarder.toml"),
        help="path to ble_forwarder.toml",
    )
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Config file not found: {args.config}")
        sys.exit(1)

    config = load_config(args.config)

    RNS.loglevel = RNS.LOG_INFO
    RNS.log("=== BLE Forwarder — AgroNomi Field Gateway ===")
    RNS.log(f"[GW] Logging to {GATEWAY_LOG}")
    setup_logging()

    reticulum = RNS.Reticulum()
    if reticulum.is_connected_to_shared_instance:
        RNS.log(
            "[RNS] Connected to shared instance (MeshChat/Sideband/rnsd).",
            RNS.LOG_INFO,
        )
        RNS.log(
            "[RNS] Announce-based discovery should work through shared instance.",
            RNS.LOG_INFO,
        )
        RNS.log(
            "[RNS] If packets don't arrive, set share_instance=No in config.",
            RNS.LOG_INFO,
        )
    else:
        RNS.log(
            "[RNS] Running as standalone instance (no shared instance).",
            RNS.LOG_INFO,
        )

    identity_path = config["gateway"].get("identity_path", "./gateway.identity")
    identity = load_or_create_identity(identity_path)

    run_forwarder(config, identity)


if __name__ == "__main__":
    main()
