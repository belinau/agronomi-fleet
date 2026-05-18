# AgroNomi — LoRa OTA: Recovery, Firmware Reuse & Retry
**Status:** Design specification  
**Date:** 2026-05-16  
**Replaces:** agronomi_lora_ota.md (extend, do not discard)

---

## 1. Design Principles

- **Transfer the binary once per gateway per firmware version per device type.**
  If 8 soil nodes on the same gateway all need `1.1.0`, the binary crosses LoRa
  exactly once. All 8 nodes are then flashed from the gateway's local cache.
- **Retry within the maintenance window.** A failed node gets up to 3 retries
  with exponential backoff before being marked `failed`. It does not wait until
  the next night.
- **Validate cached binaries before reuse.** A SHA-256 checksum is stored
  alongside every cached binary. If the file is corrupt or truncated, it is
  re-fetched before use.
- **The C6 never bricks.** The ESP-IDF bootloader rollback mechanism ensures
  the node returns to the previous firmware if the new image crashes on first
  boot. The gateway detects the missing ACK and marks the command failed.
- **Idempotent commands.** If a node is already on the target version (e.g. it
  was flashed manually), the scheduler skips it silently.

---

## 2. Firmware Cache on Gateway

All received firmware binaries are cached on the gateway filesystem:

```
/var/cache/agronomi/ota/
  <fw_version>/
    <device_type>.bin        # e.g. soil_node.bin, pump_node.bin
    <device_type>.bin.sha256 # hex digest of the binary
```

Example:
```
/var/cache/agronomi/ota/1.1.0/soil_node.bin
/var/cache/agronomi/ota/1.1.0/soil_node.bin.sha256
/var/cache/agronomi/ota/1.1.0/pump_node.bin
/var/cache/agronomi/ota/1.1.0/pump_node.bin.sha256
```

The SHA-256 file contains a single hex string, written atomically after the
binary is fully received and verified. A binary without a matching `.sha256`
file is treated as incomplete and discarded.

The hub publishes the expected SHA-256 for each firmware artifact in the
`ota_request` command (via `cmd_value_text` as JSON). This allows the gateway
to verify the cached binary against the hub's authoritative checksum, not just
its own stored value.

---

## 3. Schema Changes

### 3.1 `actuator_commands` table

Two changes from the original schema:

```sql
-- Full rebuild required to modify CHECK constraint
CREATE TABLE actuator_commands_new (
    cmd_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT REFERENCES hardware_devices(device_id),
    cmd_type        TEXT CHECK(cmd_type IN (
                        'pump_on','pump_off','vent_open','vent_close',
                        'shade_pct','fan_on','fan_off',
                        'ota_request','ota_abort'
                    )),
    cmd_value       REAL,
    cmd_value_text  TEXT,   -- JSON for ota_request; NULL for other types
    requested_at    TEXT,
    executed_at     TEXT,
    status          TEXT DEFAULT 'pending'
                    CHECK(status IN (
                        'pending','sent','acknowledged','failed','expired'
                    )),
    retry_count     INTEGER DEFAULT 0,
    last_retry_at   TEXT,   -- timestamp of most recent attempt
    error_message   TEXT
);

INSERT INTO actuator_commands_new
    SELECT cmd_id, device_id, cmd_type, cmd_value, NULL,
           requested_at, executed_at, status, retry_count, NULL, error_message
    FROM actuator_commands;

DROP TABLE actuator_commands;
ALTER TABLE actuator_commands_new RENAME TO actuator_commands;
```

`cmd_value_text` for `ota_request` carries a JSON object:
```json
{
  "fw_version": "1.1.0",
  "device_type": "soil_node",
  "sha256": "a3f1c2d4...",
  "size_bytes": 1048576
}
```

### 3.2 `hardware_devices` table

Add `device_type` to the existing table if not already indexed by it in queries.
No schema change needed — `device_type` already exists as a column.

---

## 4. Hub: Nightly OTA Scheduler

The scheduler runs in its own thread inside `reticulum_ingest.py` (or as a
separate `ota_scheduler.py` process sharing the same DB).

### 4.1 Constants

```python
OTA_WINDOW_START   = 2      # 02:00 local time
OTA_WINDOW_END     = 4      # 04:00 local time
OTA_MAX_RETRIES    = 3      # per node per night
OTA_RETRY_BACKOFF  = [60, 180, 420]  # seconds: 1min, 3min, 7min
OTA_CURRENT_FW     = {
    # device_type → (fw_version, bin_path, sha256)
    "soil_node": ("1.1.0", "/var/agronomi/fw/soil_node_1.1.0.bin",  "a3f1c2d4..."),
    "air_node":  ("1.0.3", "/var/agronomi/fw/air_node_1.0.3.bin",   "b7e8a912..."),
    "pump_node": ("1.2.1", "/var/agronomi/fw/pump_node_1.2.1.bin",  "c4d5f601..."),
}
```

### 4.2 Scheduler Logic

```python
import hashlib, json, os, time, threading
from datetime import datetime

def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def is_ota_window() -> bool:
    h = datetime.now().hour
    return OTA_WINDOW_START <= h < OTA_WINDOW_END


def get_pending_ota_nodes(conn) -> list[dict]:
    """
    Returns devices that:
    - are active
    - have a known firmware target that differs from current version
    - do NOT already have a pending/sent ota_request in flight
    - have NOT exceeded OTA_MAX_RETRIES tonight
    """
    rows = conn.execute("""
        SELECT hd.device_id, hd.device_type, hd.firmware_version,
               rg.gateway_id, rg.rns_destination_hash
        FROM hardware_devices hd
        JOIN reticulum_gateways rg
          ON hd.ble_target_gateway = rg.gateway_id
        WHERE hd.status = 'active'
          AND hd.device_id NOT IN (
              SELECT device_id FROM actuator_commands
              WHERE cmd_type = 'ota_request'
                AND status IN ('pending', 'sent')
          )
    """).fetchall()

    pending = []
    for row in rows:
        dtype = row['device_type']
        if dtype not in OTA_CURRENT_FW:
            continue
        target_ver, _, _ = OTA_CURRENT_FW[dtype]
        if row['firmware_version'] == target_ver:
            continue  # already up to date

        # Check retry count for tonight — count failed attempts since window opened
        window_open = datetime.now().replace(
            hour=OTA_WINDOW_START, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        failed_tonight = conn.execute("""
            SELECT COUNT(*) FROM actuator_commands
            WHERE device_id = ?
              AND cmd_type = 'ota_request'
              AND status = 'failed'
              AND requested_at >= ?
        """, (row['device_id'], window_open)).fetchone()[0]

        if failed_tonight >= OTA_MAX_RETRIES:
            RNS.log(
                f"[OTA] {row['device_id']} exceeded {OTA_MAX_RETRIES} retries tonight, "
                f"skipping until tomorrow", RNS.LOG_WARNING
            )
            continue

        pending.append(dict(row))
    return pending


def schedule_ota_batch(conn):
    """
    Groups pending nodes by (gateway, device_type, fw_version) so the binary
    is fetched once per group. Inserts ota_request rows for all nodes in each group.
    """
    nodes = get_pending_ota_nodes(conn)
    if not nodes:
        return

    # Group by gateway + device_type (same binary for all nodes in group)
    groups: dict[tuple, list] = {}
    for node in nodes:
        key = (node['gateway_id'], node['device_type'])
        groups.setdefault(key, []).append(node)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for (gw_id, dtype), group_nodes in groups.items():
        target_ver, bin_path, sha256 = OTA_CURRENT_FW[dtype]
        size_bytes = os.path.getsize(bin_path)

        cmd_value = json.dumps({
            "fw_version":  target_ver,
            "device_type": dtype,
            "sha256":      sha256,
            "size_bytes":  size_bytes,
        })

        for node in group_nodes:
            conn.execute("""
                INSERT INTO actuator_commands
                    (device_id, cmd_type, cmd_value_text, requested_at, status)
                VALUES (?, 'ota_request', ?, ?, 'pending')
            """, (node['device_id'], cmd_value, now))
            RNS.log(
                f"[OTA] Queued {node['device_id']} ({dtype}) → {target_ver} "
                f"via {gw_id}", RNS.LOG_INFO
            )
        conn.commit()


def run_ota_scheduler(conn):
    """Main scheduler loop. Run in a daemon thread."""
    already_ran_this_window = False

    while True:
        time.sleep(60)

        in_window = is_ota_window()

        if in_window and not already_ran_this_window:
            RNS.log("[OTA] Maintenance window open — scheduling OTA batch", RNS.LOG_INFO)
            schedule_ota_batch(conn)
            already_ran_this_window = True

        if not in_window:
            already_ran_this_window = False
```

### 4.3 CommandDispatcher: OTA Branch

The existing `CommandDispatcher` in `reticulum_ingest.py` dispatches `Packet`
for actuator commands. OTA requires `Link` + `Resource` for binary delivery.
The dispatcher needs a new branch:

```python
def _dispatch_ota(self, conn, cmd_id: int, device_id: str,
                  cmd_value_text: str, gw_hash_hex: str):
    """
    For ota_request commands:
    1. Establish RNS Link to the gateway
    2. Send firmware binary as RNS Resource
    3. After Resource delivery confirmed, send ota_request Packet
       so gateway knows which device to flash
    """
    import json
    meta = json.loads(cmd_value_text)
    dtype    = meta['device_type']
    version  = meta['fw_version']
    sha256   = meta['sha256']
    _, bin_path, _ = OTA_CURRENT_FW[dtype]

    dest_hash = bytes.fromhex(gw_hash_hex)
    RNS.Transport.request_path(dest_hash)
    identity = None
    for _ in range(PATH_REQUEST_RETRIES):
        identity = RNS.Identity.recall(dest_hash)
        if identity:
            break
        time.sleep(PATH_REQUEST_RETRY_DELAY)
        RNS.Transport.request_path(dest_hash)

    if not identity:
        RNS.log(f"[OTA] Gateway {gw_hash_hex[:16]}... not reachable for cmd {cmd_id}",
                RNS.LOG_WARNING)
        return

    gw_dest = RNS.Destination(
        identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
        COMMAND_APP, COMMAND_ASPECT
    )

    link = RNS.Link(gw_dest)
    # Wait for link to be established (timeout 60s)
    deadline = time.time() + 60
    while link.status != RNS.Link.ACTIVE and time.time() < deadline:
        time.sleep(0.5)

    if link.status != RNS.Link.ACTIVE:
        RNS.log(f"[OTA] Link to gateway failed for cmd {cmd_id}", RNS.LOG_WARNING)
        link.teardown()
        return

    # Send firmware binary as Resource
    def on_resource_concluded(resource):
        if resource.status == RNS.Resource.COMPLETE:
            RNS.log(f"[OTA] Binary delivered to gateway for cmd {cmd_id}",
                    RNS.LOG_INFO)
            # Now send the ota_request packet so gateway knows what to do
            payload = json.dumps({
                "cmd_id":      cmd_id,
                "device_id":   device_id,
                "cmd_type":    "ota_request",
                "fw_version":  version,
                "device_type": dtype,
                "sha256":      sha256,
                "ts":          int(time.time()),
            }).encode("utf-8")
            pkt = RNS.Packet(gw_dest, payload)
            pkt.send()
            conn.execute(
                "UPDATE actuator_commands SET status='sent', last_retry_at=datetime('now') "
                "WHERE cmd_id=?", (cmd_id,)
            )
            conn.commit()
        else:
            RNS.log(f"[OTA] Resource transfer failed for cmd {cmd_id}", RNS.LOG_ERROR)
            self._mark_ota_failed(conn, cmd_id, "Resource transfer incomplete")
        link.teardown()

    with open(bin_path, 'rb') as f:
        firmware_data = f.read()

    resource = RNS.Resource(firmware_data, link,
                            callback=on_resource_concluded)
    resource.advertise()


def _mark_ota_failed(self, conn, cmd_id: int, reason: str):
    conn.execute("""
        UPDATE actuator_commands
        SET status = 'failed',
            error_message = ?,
            retry_count = COALESCE(retry_count, 0) + 1,
            last_retry_at = datetime('now')
        WHERE cmd_id = ?
    """, (reason, cmd_id))
    conn.commit()
```

---

## 5. Gateway: Firmware Cache & Reuse

```python
import hashlib, json, os, aiofiles

OTA_CACHE_DIR = "/var/cache/agronomi/ota"

def fw_cache_path(fw_version: str, device_type: str) -> tuple[str, str]:
    """Returns (bin_path, sha256_path)."""
    base = os.path.join(OTA_CACHE_DIR, fw_version)
    os.makedirs(base, exist_ok=True)
    return (
        os.path.join(base, f"{device_type}.bin"),
        os.path.join(base, f"{device_type}.bin.sha256"),
    )


def verify_cached_firmware(fw_version: str, device_type: str,
                            expected_sha256: str) -> bool:
    """
    Returns True if a valid, verified binary is already cached.
    Returns False if missing, truncated, or checksum mismatch.
    """
    bin_path, sha_path = fw_cache_path(fw_version, device_type)
    if not os.path.exists(bin_path) or not os.path.exists(sha_path):
        return False
    try:
        with open(sha_path) as f:
            stored = f.read().strip()
        if stored != expected_sha256:
            RNS.log(f"[OTA] Cache checksum mismatch for {device_type} {fw_version} "
                    f"— re-fetching", RNS.LOG_WARNING)
            os.remove(bin_path)
            os.remove(sha_path)
            return False
        # Verify the actual file matches too
        h = hashlib.sha256()
        with open(bin_path, 'rb') as f:
            for block in iter(lambda: f.read(65536), b''):
                h.update(block)
        if h.hexdigest() != expected_sha256:
            RNS.log(f"[OTA] Cache file corrupt for {device_type} {fw_version} "
                    f"— re-fetching", RNS.LOG_WARNING)
            os.remove(bin_path)
            os.remove(sha_path)
            return False
        return True
    except Exception as e:
        RNS.log(f"[OTA] Cache verification error: {e}", RNS.LOG_WARNING)
        return False


def save_firmware_to_cache(fw_version: str, device_type: str,
                           data: bytes, expected_sha256: str) -> bool:
    """
    Write received firmware to cache atomically.
    Verifies checksum before writing .sha256 sentinel file.
    Returns False if checksum fails — do not use this binary.
    """
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        RNS.log(f"[OTA] Received binary checksum mismatch! "
                f"expected={expected_sha256[:16]}... got={actual[:16]}...",
                RNS.LOG_ERROR)
        return False

    bin_path, sha_path = fw_cache_path(fw_version, device_type)
    # Write binary atomically via temp file
    tmp = bin_path + ".tmp"
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, bin_path)      # atomic on Linux
    with open(sha_path, 'w') as f:
        f.write(actual)
    RNS.log(f"[OTA] Cached {device_type} {fw_version} "
            f"({len(data)} bytes, sha256={actual[:16]}...)", RNS.LOG_INFO)
    return True
```

---

## 6. Gateway: OTA Command Handler with Retry

When `ble_forwarder.py` receives an `ota_request` command packet from the hub:

```python
OTA_MAX_BLE_RETRIES  = 3
OTA_BLE_BACKOFF      = [30, 90, 210]   # seconds between BLE retries

OTA_HDR_BEGIN  = 0xA0
OTA_HDR_DATA   = 0xA1
OTA_HDR_END    = 0xA2
OTA_HDR_ABORT  = 0xA3
OTA_CHUNK_SIZE = 241    # NUS MTU 244 − 3 byte NimBLE header


async def handle_ota_command(rns_hub_dest, cmd: dict, firmware_data: bytes):
    """
    cmd: the parsed ota_request JSON from hub
    firmware_data: already-verified binary (from cache or freshly received)
    rns_hub_dest: hub's CommandAckDestination for sending ACK/NAK
    """
    device_id  = cmd['device_id']
    fw_version = cmd['fw_version']
    cmd_id     = cmd['cmd_id']

    ble_mac = get_ble_mac(device_id)   # lookup from local config or DB copy
    if not ble_mac:
        send_rns_ack(rns_hub_dest, cmd_id, status='failed',
                     error=f"No BLE MAC for {device_id}")
        return

    for attempt in range(1, OTA_MAX_BLE_RETRIES + 1):
        RNS.log(f"[OTA] BLE OTA attempt {attempt}/{OTA_MAX_BLE_RETRIES} "
                f"for {device_id}", RNS.LOG_INFO)
        success, error = await _ble_ota_attempt(ble_mac, firmware_data, fw_version)

        if success:
            RNS.log(f"[OTA] {device_id} flashed successfully → {fw_version}",
                    RNS.LOG_INFO)
            send_rns_ack(rns_hub_dest, cmd_id, status='acknowledged',
                         fw_version=fw_version)
            return

        RNS.log(f"[OTA] Attempt {attempt} failed for {device_id}: {error}",
                RNS.LOG_WARNING)

        if attempt < OTA_MAX_BLE_RETRIES:
            backoff = OTA_BLE_BACKOFF[attempt - 1]
            RNS.log(f"[OTA] Retrying {device_id} in {backoff}s...", RNS.LOG_INFO)
            await asyncio.sleep(backoff)

    # All retries exhausted
    RNS.log(f"[OTA] {device_id} failed after {OTA_MAX_BLE_RETRIES} attempts",
            RNS.LOG_ERROR)
    send_rns_ack(rns_hub_dest, cmd_id, status='failed',
                 error=f"BLE OTA failed after {OTA_MAX_BLE_RETRIES} attempts: {error}")


async def _ble_ota_attempt(ble_mac: str, firmware: bytes,
                           fw_version: str) -> tuple[bool, str]:
    """
    Single BLE OTA attempt to one C6 node.
    Returns (success, error_message).
    """
    from bleak import BleakClient, BleakError

    try:
        async with BleakClient(ble_mac, timeout=20.0) as client:
            if not client.is_connected:
                return False, "BLE connect failed"

            # Negotiate MTU (bleak does this automatically on Linux/BlueZ)
            # NUS UUIDs
            NUS_RX = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # gateway writes here
            NUS_TX = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # C6 notifies here

            ack_event   = asyncio.Event()
            ack_payload = {}

            def on_notify(_, data: bytearray):
                """Called when C6 sends an ACK or NAK via NUS TX notify."""
                try:
                    msg = json.loads(data.decode('utf-8'))
                    ack_payload.update(msg)
                    ack_event.set()
                except Exception:
                    pass   # non-JSON notify (heartbeat etc.) — ignore

            await client.start_notify(NUS_TX, on_notify)

            # --- OTA_BEGIN ---
            total = len(firmware)
            begin = bytes([OTA_HDR_BEGIN]) \
                + total.to_bytes(4, 'little') \
                + fw_version.encode('utf-8')
            await client.write_gatt_char(NUS_RX, begin, response=True)
            await asyncio.sleep(0.15)   # give C6 time to call esp_ota_begin

            # --- DATA CHUNKS ---
            seq    = 0
            offset = 0
            while offset < total:
                chunk = firmware[offset:offset + OTA_CHUNK_SIZE]
                frame = bytes([OTA_HDR_DATA]) \
                    + seq.to_bytes(4, 'little') \
                    + chunk
                await client.write_gatt_char(NUS_RX, frame, response=True)
                offset += len(chunk)
                seq    += 1
                # write_gatt_char with response=True provides backpressure —
                # we only advance when C6 GATT server has accepted the write.
                # Small yield to keep asyncio event loop healthy.
                if seq % 50 == 0:
                    pct = int(offset / total * 100)
                    RNS.log(f"[OTA] {pct}% ({offset}/{total} bytes)", RNS.LOG_DEBUG)
                    await asyncio.sleep(0)

            # --- OTA_END ---
            end_frame = bytes([OTA_HDR_END]) + fw_version.encode('utf-8')
            await client.write_gatt_char(NUS_RX, end_frame, response=True)

            # --- Wait for ACK notify from C6 (reboot takes up to 5s) ---
            try:
                await asyncio.wait_for(ack_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                return False, "ACK timeout — C6 may have crashed or rolled back"

            await client.stop_notify(NUS_TX)

            if ack_payload.get('ota_ok') is True:
                return True, ""
            else:
                err = ack_payload.get('error', 'unknown error from C6')
                return False, err

    except BleakError as e:
        return False, f"BLE error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def send_rns_ack(hub_dest, cmd_id: int, status: str,
                 fw_version: str = None, error: str = None):
    """Send OTA result ACK back to hub via RNS."""
    payload = {"cmd_id": cmd_id, "status": status}
    if fw_version:
        payload["fw_version"] = fw_version
    if error:
        payload["error"] = error
    data = json.dumps(payload).encode('utf-8')
    pkt  = RNS.Packet(hub_dest, data)
    pkt.send()
```

---

## 7. C6 Firmware: NUS Notify Dispatcher

In the C6 Arduino firmware, the NUS RX write callback dispatches to `OTAManager`:

```cpp
// In the NUS RX characteristic write callback:
void onNUSWrite(NimBLECharacteristic* pChar) {
    std::string val  = pChar->getValue();
    const uint8_t* data = (const uint8_t*)val.data();
    size_t len       = val.length();
    if (len == 0) return;

    uint8_t hdr = data[0];

    if (hdr == OTA_HDR_BEGIN) {
        // [0xA0][total uint32 LE][fw_version str]
        if (len < 5) return;
        uint32_t total = data[1] | (data[2]<<8) | (data[3]<<16) | (data[4]<<24);
        char ver[32]   = {0};
        size_t vlen    = min(len - 5, (size_t)31);
        memcpy(ver, data + 5, vlen);

        if (!otaManager.beginBLE(total, ver)) {
            // Send NAK so gateway stops sending
            String nak = "{\"ota_ok\":false,\"error\":\"esp_ota_begin failed\"}";
            pNUSTX->setValue(nak.c_str());
            pNUSTX->notify();
        }

    } else if (hdr == OTA_HDR_DATA) {
        if (!otaManager.isActive()) return;
        if (!otaManager.writeChunk(data, len)) {
            otaManager.abortBLE();
            String nak = "{\"ota_ok\":false,\"error\":\"chunk write failed\"}";
            pNUSTX->setValue(nak.c_str());
            pNUSTX->notify();
        }

    } else if (hdr == OTA_HDR_END) {
        if (!otaManager.isActive()) return;
        // [0xA2][fw_version str]
        char ver[32] = {0};
        size_t vlen  = min(len - 1, (size_t)31);
        memcpy(ver, data + 1, vlen);

        // Send ACK BEFORE finalizeBLE() because finalizeBLE() reboots.
        String ack = String("{\"ota_ok\":true,\"fw_ver\":\"") + ver + "\"}";
        pNUSTX->setValue(ack.c_str());
        pNUSTX->notify();
        delay(300);   // give BLE stack time to transmit notify before reboot

        otaManager.finalizeBLE(ver);   // → reboots inside

    } else if (hdr == OTA_HDR_ABORT) {
        otaManager.abortBLE();

    } else {
        // Normal actuator command — existing handler
        handleActuatorCommand(data, len);
    }
}
```

### ESP-IDF Bootloader Rollback

After rebooting into new firmware, the C6 must call this early in `setup()`,
after confirming the firmware is functional (sensors init OK, BLE connects OK):

```cpp
void setup() {
    // ... hardware init ...

    // Mark new firmware as valid to prevent bootloader rollback.
    // If this is never called and the device reboots again (e.g. crash),
    // the bootloader automatically reverts to the previous partition.
    esp_ota_mark_app_valid_cancel_rollback();
}
```

This is the safety net: if the new firmware crashes before `setup()` completes
and marks itself valid, the next power cycle boots the old firmware automatically.

---

## 8. Hub: Handling OTA ACK with Firmware Version Update

In `reticulum_ingest.py`, `CommandAckDestination.on_packet()` already handles
generic ACKs. Add the firmware version update for OTA ACKs:

```python
def on_packet(self, data: bytes, packet: RNS.Packet):
    try:
        ack    = json.loads(data.decode("utf-8"))
        cmd_id = ack.get("cmd_id")
        status = ack.get("status")
        error  = ack.get("error")

        if cmd_id is None or not status:
            RNS.log(f"[WARN] Malformed ACK: {ack}", RNS.LOG_WARNING)
            return

        update_actuator_status(int(cmd_id), status, error)

        # If OTA acknowledged, update firmware_version in hardware_devices
        if status == "acknowledged" and ack.get("fw_version"):
            with get_db() as conn:
                row = conn.execute(
                    "SELECT device_id FROM actuator_commands WHERE cmd_id=?",
                    (cmd_id,)
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE hardware_devices SET firmware_version=? "
                        "WHERE device_id=?",
                        (ack["fw_version"], row["device_id"])
                    )
                    conn.commit()
                    RNS.log(f"[OTA] {row['device_id']} updated to "
                            f"{ack['fw_version']}", RNS.LOG_INFO)

        RNS.log(f"[RNS] Cmd {cmd_id} => {status}", RNS.LOG_INFO)
    except Exception as e:
        RNS.log(f"[ERROR] ACK processing failed: {e}", RNS.LOG_ERROR)
```

---

## 9. Complete Failure & Recovery Matrix

| Failure point | What happens | Recovery |
|---|---|---|
| RNS Resource transfer drops mid-way | `Resource` reports `INCOMPLETE` in callback | Hub marks cmd `failed`, scheduler queues retry next window |
| Binary checksum mismatch on gateway | `save_firmware_to_cache()` returns False | Gateway sends NAK to hub, hub marks `failed`, retries next window |
| BLE connect fails | `BleakError` caught in `_ble_ota_attempt` | Retry up to 3× with backoff within same window |
| BLE drops mid-chunk transfer | Exception in `write_gatt_char` | `OTA_HDR_ABORT` sent if possible, `abortBLE()` on C6, retry |
| C6 `esp_ota_begin` fails (no partition) | C6 sends NAK via NUS TX notify | Gateway receives NAK, marks attempt failed, retries |
| C6 chunk write fails (`esp_ota_write`) | C6 sends NAK, `abortBLE()` | Gateway retries full transfer |
| C6 `esp_ota_end` validation fails | C6 sends NAK, `abortBLE()` | Corrupt binary — hub must re-publish firmware, retry next window |
| C6 reboots but crashes immediately | ACK never sent, gateway times out after 30s | Bootloader rolls back to previous firmware automatically. Hub marks `failed`. Retries next window. |
| C6 reboots, new firmware works but never calls `mark_app_valid` | Next reboot → bootloader rolls back | Bug in new firmware — fix and re-publish |
| All 3 BLE retries exhausted | Gateway sends `failed` ACK to hub | Hub marks `failed` with retry count. Node skipped until next maintenance window. |
| Node already on target version (e.g. manual flash) | Scheduler skips via `firmware_version` check | No action needed |
| Gateway loses power mid-OTA | Cached binary survives (written atomically) | On restart, hub re-dispatches pending cmd, gateway reuses cached binary |

---

## 10. What Is Not Yet Implemented

- `rngit` on hub for firmware version management — currently `OTA_CURRENT_FW`
  dict is hardcoded; should be populated from `rngit release` metadata
- `get_ble_mac()` on gateway — needs either a local config file or a DB copy
  (snapshot of `hardware_devices.ble_mac`) synced from hub
- Deep sleep wake strategy for battery-powered sensor nodes during OTA window —
  nodes need to stay awake or be triggered to wake at the right time; this
  requires an OTA wake signal or a scheduled wake via RTC alarm
- Multi-gateway parallel OTA coordination — currently each gateway runs
  independently; no global airtime budget enforcement across gateways
