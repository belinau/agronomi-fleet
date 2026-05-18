#include "OTAManager.h"
#include <WiFi.h>
#include <HTTPUpdate.h>
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
    Serial0.println("[OTA] Downloading: " + url);
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
    delay(500);
    esp_restart();
    return true;  // never reached
}

void OTAManager::abortBLE() {
    if (!_active) return;
    esp_ota_abort(_handle);
    _active = false;
    _bytes_written = 0;
    ESP_LOGW(TAG, "BLE OTA aborted");
    Serial0.println("[OTA] BLE OTA aborted.");
}