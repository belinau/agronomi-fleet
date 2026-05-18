#ifndef TELEMETRY_H
#define TELEMETRY_H

#include <Arduino.h>

struct SensorReading {
    // Soil node fields
    float soilMoisturePct = 0.0f;
    float soilTempC = 0.0f;
    bool soilTempValid = false;

    // Air node fields
    float airTempC = 0.0f;
    float airHumidityPct = 0.0f;
    bool airTempValid = false;
    bool airHumidityValid = false;

    // Common fields
    float batteryV = 0.0f;
};

class Telemetry {
public:
    static String buildJson(const SensorReading& r, const char* deviceId, uint32_t bootCount);
};

#endif
