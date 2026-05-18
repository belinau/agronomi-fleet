#include "Telemetry.h"
#include <ArduinoJson.h>
#include "esp_mac.h"

String Telemetry::buildJson(const SensorReading& r, const char* deviceId, uint32_t bootCount) {
    JsonDocument doc;
    doc["dev_id"] = deviceId;
    doc["ts"] = (uint32_t)(esp_timer_get_time() / 1000000ULL);
    doc["fw_ver"] = FIRMWARE_VERSION;
    doc["seq"] = bootCount;

    // Auto-provisioning: device self-identifies its type and BLE MAC
    doc["device_type"] = DEVICE_TYPE;
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_BT);
    char mac_str[18];
    snprintf(mac_str, sizeof(mac_str), "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    doc["ble_mac"] = mac_str;

#if HAS_BAT_RESISTORS
    doc["bat_v"] = serialized(String(r.batteryV, 2));
#endif

    JsonObject readings = doc["readings"].to<JsonObject>();

    // Soil node fields
#if HAS_SOIL_SENSOR
    readings["soil_moisture_pct"] = serialized(String(r.soilMoisturePct, 2));
#endif

#if HAS_TEMP_SENSOR
    if (r.soilTempValid) {
        readings["soil_temperature_c"] = serialized(String(r.soilTempC, 2));
    }
#endif

    // Air node fields
#if HAS_DHT22
    if (r.airTempValid) {
        readings["air_temperature_c"] = serialized(String(r.airTempC, 2));
    }
    if (r.airHumidityValid) {
        readings["air_humidity_pct"] = serialized(String(r.airHumidityPct, 2));
    }
#endif

    String out;
    serializeJson(doc, out);
    return out;
}
