#include "Telemetry.h"
#include <ArduinoJson.h>

String Telemetry::buildJson(const SensorReading& r, const char* deviceId, uint32_t bootCount) {
    JsonDocument doc;
    doc["dev_id"] = deviceId;
    doc["ts"] = (uint32_t)(esp_timer_get_time() / 1000000ULL);
    doc["fw_ver"] = FIRMWARE_VERSION;
    doc["seq"] = bootCount;

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
