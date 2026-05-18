#ifndef OTAMANAGER_H
#define OTAMANAGER_H

#include <Arduino.h>
#include <esp_ota_ops.h>

// BLE chunk protocol constants
#define OTA_CHUNK_SIZE     241          // NUS MTU 244 - 3 byte NimBLE header
#define OTA_HDR_BEGIN      0xA0         // first byte of OTA_BEGIN frame
#define OTA_HDR_DATA       0xA1         // first byte of data chunk frame
#define OTA_HDR_END        0xA2         // first byte of OTA_END frame
#define OTA_HDR_ABORT      0xA3         // gateway aborts transfer

class OTAManager {
public:
    // Existing WiFi path — unchanged
    void begin(String url);

    // New BLE OTA path
    // Called when an OTA_BEGIN frame arrives via BLE NUS notify.
    // Returns false immediately if OTA partition cannot be opened.
    bool beginBLE(size_t total_size, const char* fw_version);

    // Called for each OTA_DATA chunk frame.
    // Frame layout: [OTA_HDR_DATA 1B][seq uint32 LE 4B][payload N bytes]
    // Returns false on write error (caller should send NAK and abort).
    bool writeChunk(const uint8_t* data, size_t len);

    // Called when OTA_END frame is received.
    // Validates image, sets boot partition, reboots.
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