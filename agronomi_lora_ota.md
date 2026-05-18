# AgroNomi — LoRa OTA for ESP32-C6 Fleet
**Status:** Design specification  
**Date:** 2026-05-16

---

## 1. Overview

The existing `OTAManager` handles WiFi-based OTA via `HTTPUpdate`. This document
specifies a parallel LoRa OTA path for field-deployed C6 nodes that have no WiFi
coverage. Both paths share the same trigger mechanism (an `ota_request` command
row in `actuator_commands`) and the same C6-side `esp_ota_ops` write logic.
The only difference is how the binary reaches the C6.

**Deployment strategy:** OTA is pushed during a nightly maintenance window
(e.g. 02:00–04:00 local time) when sensor polling is paused and LoRa airtime
is not competing with telemetry traffic.

---

## 2. Full Chain

```
Hub (Mac Mini)
  firmware .bin file on disk (published via rngit release)
  OTA command inserted into actuator_commands:
    cmd_type = 'ota_request'
    cmd_value_text = '1.1.0'          ← target firmware version
    device_id = 'SN-SOIL-03'
    status = 'pending'
        │
        │ RNS Link (established by hub to gateway destination)
        │ RNS Resource (firmware binary sent over link)
        │ — RNS handles chunking, sequencing, SHA-256 integrity,
        │   reassembly automatically. No custom protocol needed here.
        ▼
Gateway (Pi Zero 2W / Mimi for testing)
  ble_forwarder.py receives OTA command from hub
  fetches firmware binary via RNS Resource from hub link
  writes binary to temp file: /tmp/ota_SN-SOIL-03_1.1.0.bin
  opens BLE connection to target C6 (by MAC from hardware_devices)
  sends binary in chunks over NUS TX characteristic (notify)
        │
        │ BLE NUS chunks
        │ chunk size: negotiated MTU − 3 bytes (typically 241 bytes)
        │ each chunk prefixed with 4-byte sequence number
        │ final chunk flagged with OTA_END marker
        ▼
ESP32-C6 node (SN-SOIL-03)
  wakes from deep sleep (or stays awake if OTA window flag received)
  receives OTA_BEGIN command via NUS notify
  calls esp_ota_begin() → gets OTA partition handle
  receives chunks → calls esp_ota_write() per chunk
  receives OTA_END → calls esp_ota_end()
  validates image → calls esp_ota_set_boot_partition()
  writes ACK via NUS RX: {"ota_ok": true, "fw_ver": "1.1.0"}
  reboots → boots new firmware
        │
        │ BLE ACK → serial → RNS → hub
        ▼
Hub
  CommandAckDestination receives ACK
  updates actuator_commands: status = 'acknowledged'
  updates hardware_devices: firmware_version = '1.1.0'
```

---

## 3. Timing Budget

SF11, BW 125kHz, CR 4/5 gives ~540 bits/second usable throughput.

| Firmware size | Transfer time (raw) | With 20% RNS overhead |
|---------------|--------------------|-----------------------|
| 512 KB        | ~7.8 min           | ~9.4 min              |
| 1 MB          | ~15.6 min          | ~18.7 min             |
| 1.5 MB        | ~23.3 min          | ~28 min               |

A typical ESP32-C6 Arduino firmware is 1–1.5 MB. Within a 2-hour nightly window,
you can comfortably OTA **4–5 nodes sequentially** at 1 MB each with time to spare.

For the fleet: schedule nodes in batches by gateway. One gateway handles its
attached nodes sequentially. Multiple gateways can run OTA in parallel since
they are on separate BLE links and share LoRa airtime via RNS CSMA/CA.

EU868 duty cycle (1%) is not a concern for a single gateway doing sequential
node OTA — the total airtime per node is well within limits when spread over
a 2-hour window.

---

## 4. Schema Changes Required

`actuator_commands.cmd_value` is currently `REAL`. OTA needs a version string.
Add `cmd_value_text TEXT` column:

```sql
ALTER TABLE actuator_commands ADD COLUMN cmd_value_text TEXT;
```

Add `'ota_request'` and `'ota_abort'` to the `cmd_type` CHECK constraint.
Because SQLite does not support ALTER TABLE to modify CHECK constraints,
this requires a table rebuild:

```sql
-- Rebuild actuator_commands with new cmd_type values and cmd_value_text
CREATE TABLE actuator_commands_new (
    cmd_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT REFERENCES hardware_devices(device_id),
    cmd_type        TEXT CHECK(cmd_type IN (
                        'pump_on','pump_off','vent_open','vent_close',
                        'shade_pct','fan_on','fan_off',
                        'ota_request','ota_abort'           -- new
                    )),
    cmd_value       REAL,
    cmd_value_text  TEXT,                                   -- new: version string, URL, etc.
    requested_at    TEXT,
    executed_at     TEXT,
    status          TEXT DEFAULT 'pending'
                    CHECK(status IN (
                        'pending','sent','acknowledged','failed','expired'
                    )),
    retry_count     INTEGER DEFAULT 0,
    error_message   TEXT
);
INSERT INTO actuator_commands_new SELECT
    cmd_id, device_id, cmd_type, cmd_value, NULL,
    requested_at, executed_at, status, retry_count, error_message
FROM actuator_commands;
DROP TABLE actuator_commands;
ALTER TABLE actuator_commands_new RENAME TO actuator_commands;
```

---

## 5. OTAManager Extension (C6 Arduino/ESP-IDF)

The existing `OTAManager` gets a second public method `beginBLE()`. WiFi path
is unchanged.

### OTAManager.h

```cpp
#ifndef OTAMANAGER_H
#define OTAMANAGER_H

#include <Arduino.h>

// BLE chunk protocol constants
#define OTA_CHUNK_SIZE     241          // NUS MTU 244 − 3 byte header
#define OTA_HDR_BEGIN      0xA0         // first byte of OTA_BEGIN frame
#define OTA_HDR_DATA       0xA1         // first byte of data chunk frame
#define OTA_HDR_END        0xA2         // first byte of OTA_END frame
#define OTA_HDR_ABORT      0xA3         // gateway aborts transfer

class OTAManager {
public:
    // Existing WiFi path — unchanged
    void begin(String url);

    // New LoRa/BLE path
    // Called from the NUS notify callback when an OTA_BEGIN frame arrives.
    // Returns false immediately if OTA partition cannot be opened.
    bool beginBLE(size_t total_size, const char* fw_version);

    // Called from the NUS notify callback for each OTA_DATA chunk.
    // frame layout: [OTA_HDR_DATA 1B][seq 4B][payload N bytes]
    // Returns false on write error (caller should send NAK and abort).
    bool writeChunk(const uint8_t* data, size_t len);

    // Called when OTA_END frame is received.
    // Validates image, sets boot partition, sends ACK via NUS RX write.
    // Returns false if validation fails.
    bool finalizeBLE(const char* fw_version);

    // Called on OTA_ABORT — rolls back to previous partition.
    void abortBLE();

    // Returns true if a BLE OTA is currently in progress.
    bool isActive() const { return _active; }

private:
    bool        _active       = false;
    esp_ota_handle_t _handle  = 0;
    const esp_partition_t* _partition = nullptr;
    size_t      _bytes_written = 0;
    size_t      _total_size   = 0;
    uint32_t    _expected_seq = 0;
};

#endif
```

### OTAManager.cpp — BLE methods

```cpp
#include "OTAManager.h"
#include <esp_ota_ops.h>
#include <esp_log.h>

static const char* TAG = "OTA";

// --- existing WiFi method unchanged ---
void OTAManager::begin(String url) {
    Serial0.println("[OTA] Switching to WiFi...");
    if (strlen(WIFI_SSID) == 0) { Serial0.println("[OTA] No WiFi credentials!"); return; }
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    int retries = 0;
    while (WiFi.status() != WL_CONNECTED && retries < 20) { delay(500); Serial0.print("."); retries++; }
    if (WiFi.status() != WL_CONNECTED) { Serial0.println("[OTA] WiFi Failed."); return; }
    Serial0.println("\n[OTA] WiFi Connected.");
    WiFiClient client;
    t_httpUpdate_return ret = httpUpdate.update(client, url);
    switch (ret) {
        case HTTP_UPDATE_FAILED:   Serial0.printf("[OTA] Failed: %d\n", httpUpdate.getLastError()); break;
        case HTTP_UPDATE_NO_UPDATES: Serial0.println("[OTA] No update."); break;
        case HTTP_UPDATE_OK:       Serial0.println("[OTA] Success! Rebooting..."); ESP.restart(); break;
    }
}

// --- new BLE OTA methods ---

bool OTAManager::beginBLE(size_t total_size, const char* fw_version) {
    if (_active) {
        ESP_LOGW(TAG, "OTA already in progress, aborting previous");
        abortBLE();
    }
    _partition = esp_ota_get_next_update_partition(NULL);
    if (!_partition) {
        ESP_LOGE(TAG, "No OTA partition available");
        return false;
    }
    esp_err_t err = esp_ota_begin(_partition, total_size, &_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
        return false;
    }
    _total_size    = total_size;
    _bytes_written = 0;
    _expected_seq  = 0;
    _active        = true;
    ESP_LOGI(TAG, "BLE OTA started: %u bytes, target fw %s", total_size, fw_version);
    Serial0.printf("[OTA] BLE OTA begin: %u bytes, fw=%s\n", total_size, fw_version);
    return true;
}

bool OTAManager::writeChunk(const uint8_t* data, size_t len) {
    if (!_active) return false;

    // Frame layout: [OTA_HDR_DATA 1B][seq uint32 LE 4B][payload]
    if (len < 5 || data[0] != OTA_HDR_DATA) return false;

    uint32_t seq = data[1] | (data[2] << 8) | (data[3] << 16) | (data[4] << 24);
    if (seq != _expected_seq) {
        ESP_LOGE(TAG, "Sequence error: expected %u got %u", _expected_seq, seq);
        return false;
    }

    const uint8_t* payload = data + 5;
    size_t payload_len     = len - 5;

    esp_err_t err = esp_ota_write(_handle, payload, payload_len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_write failed: %s", esp_err_to_name(err));
        return false;
    }

    _bytes_written += payload_len;
    _expected_seq++;
    return true;
}

bool OTAManager::finalizeBLE(const char* fw_version) {
    if (!_active) return false;

    esp_err_t err = esp_ota_end(_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_end failed: %s", esp_err_to_name(err));
        abortBLE();
        return false;
    }

    err = esp_ota_set_boot_partition(_partition);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_set_boot_partition failed: %s", esp_err_to_name(err));
        abortBLE();
        return false;
    }

    _active = false;
    ESP_LOGI(TAG, "OTA complete. Booting fw %s after ACK.", fw_version);
    Serial0.printf("[OTA] Complete: fw=%s written=%u bytes. Rebooting.\n",
                   fw_version, _bytes_written);

    // Caller is responsible for sending ACK over NUS before reboot.
    // Give NUS stack time to transmit ACK before restart.
    delay(500);
    esp_restart();
    return true;  // never reached
}

void OTAManager::abortBLE() {
    if (!_active) return;
    esp_ota_abort(_handle);
    _active = false;
    _bytes_written = 0;
    ESP_LOGW(TAG, "BLE OTA aborted after %u bytes", _bytes_written);
    Serial0.println("[OTA] BLE OTA aborted.");
}
```

---

## 6. Gateway Side — BLE Chunk Relay (Python outline)

This logic goes into `ble_forwarder.py` when an `ota_request` command is received
from the hub via RNS. The firmware binary has already been delivered by RNS Resource
and written to a temp file.

```python
OTA_HDR_BEGIN = 0xA0
OTA_HDR_DATA  = 0xA1
OTA_HDR_END   = 0xA2
OTA_HDR_ABORT = 0xA3
OTA_CHUNK_PAYLOAD = 241  # MTU 244 − 3 byte NimBLE header

async def ble_ota_send(client, bin_path: str, fw_version: str):
    """
    client: bleak BleakClient connected to target C6
    bin_path: local path to firmware .bin file
    Writes to NUS RX characteristic (gateway writes to C6).
    Reads ACK from NUS TX characteristic (C6 notifies).
    """
    with open(bin_path, 'rb') as f:
        firmware = f.read()

    total = len(firmware)

    # OTA_BEGIN frame: [0xA0][total_size uint32 LE 4B][fw_version str]
    begin_frame = bytes([OTA_HDR_BEGIN]) \
        + total.to_bytes(4, 'little') \
        + fw_version.encode('utf-8')
    await client.write_gatt_char(NUS_RX_UUID, begin_frame, response=True)
    await asyncio.sleep(0.1)

    # Send data chunks
    seq = 0
    offset = 0
    while offset < total:
        chunk = firmware[offset:offset + OTA_CHUNK_PAYLOAD]
        frame = bytes([OTA_HDR_DATA]) \
            + seq.to_bytes(4, 'little') \
            + chunk
        await client.write_gatt_char(NUS_RX_UUID, frame, response=True)
        offset += len(chunk)
        seq += 1
        # Throttle: BLE write-with-response already provides backpressure,
        # but a small yield helps the C6 esp_ota_write keep up.
        await asyncio.sleep(0.01)

    # OTA_END frame: [0xA2][fw_version str]
    end_frame = bytes([OTA_HDR_END]) + fw_version.encode('utf-8')
    await client.write_gatt_char(NUS_RX_UUID, end_frame, response=True)

    # Wait for ACK notify from C6 on NUS TX (timeout 30s for reboot)
    # ACK payload: {"ota_ok": true, "fw_ver": "1.1.0"}
    # Handled by the existing NUS notify callback in ble_forwarder.py
```

---

## 7. Hub Side — Triggering OTA

Insert a command row to trigger OTA for a specific node:

```python
def schedule_ota(conn, device_id: str, fw_version: str, bin_path: str):
    """
    Queue an OTA update for a C6 node.
    The gateway picks it up via CommandDispatcher and handles binary delivery.
    bin_path is stored so the gateway knows what to fetch from the hub rngit release.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO actuator_commands
            (device_id, cmd_type, cmd_value_text, requested_at, status)
        VALUES (?, 'ota_request', ?, ?, 'pending')
    """, (device_id, fw_version, now))
    conn.commit()
```

The `CommandDispatcher` in `reticulum_ingest.py` needs a new branch for
`ota_request` that:

1. Opens an RNS `Link` to the gateway (not just a raw `Packet` — `Resource`
   transfer requires a `Link`)
2. Sends the firmware binary as an RNS `Resource` over that link
3. After `Resource` delivery is confirmed, sends the `ota_request` command
   packet so the gateway knows which device to flash and what version to report

Using `Link` + `Resource` is correct here because the firmware binary is large
(up to 1.5 MB) — the `Packet` API has a 465-byte data limit and is not suitable
for bulk transfer. `Resource` handles everything automatically.

---

## 8. Nightly Scheduler (Hub)

A simple scheduler in `reticulum_ingest.py` or a separate `ota_scheduler.py`:

```python
OTA_WINDOW_START = 2   # 02:00 local
OTA_WINDOW_END   = 4   # 04:00 local

def is_ota_window() -> bool:
    h = datetime.now().hour
    return OTA_WINDOW_START <= h < OTA_WINDOW_END

def run_nightly_ota_scheduler(conn):
    """
    Checks once per minute if we are in the OTA window.
    If yes, inserts ota_request rows for all devices with
    firmware_version != current_fw_version in hardware_devices.
    """
    while True:
        time.sleep(60)
        if not is_ota_window():
            continue
        rows = conn.execute("""
            SELECT device_id, firmware_version FROM hardware_devices
            WHERE status = 'active'
              AND firmware_version != ?
              AND device_id NOT IN (
                  SELECT device_id FROM actuator_commands
                  WHERE cmd_type = 'ota_request'
                    AND status IN ('pending','sent')
              )
        """, (CURRENT_FW_VERSION,)).fetchall()
        for row in rows:
            schedule_ota(conn, row['device_id'], CURRENT_FW_VERSION, FW_BIN_PATH)
            RNS.log(f"[OTA] Scheduled OTA for {row['device_id']}", RNS.LOG_INFO)
```

---

## 9. What Is Not Yet Designed

- The NUS notify callback on the C6 side that dispatches to `OTAManager::beginBLE`,
  `writeChunk`, `finalizeBLE` — this is part of the C6 firmware (not yet written)
- RNS `Link` establishment in `CommandDispatcher` — currently only `Packet` is used;
  OTA requires upgrading to `Link` + `Resource` for binary delivery
- Error recovery: what happens if BLE drops mid-transfer (C6 `abortBLE()`, gateway
  marks command `failed`, scheduler retries next night)
- rngit setup on hub for firmware version management and release publishing
