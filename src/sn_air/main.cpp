// ============================================================================
// Farm Node Firmware — SN-AIR
// ============================================================================
#include <Arduino.h>
#include <esp_sleep.h>
#include <esp_ota_ops.h>
#include <DHT.h>
#include "Telemetry.h"
#include "BLEManager.h"
#include "OTAManager.h"

#define PIN_DHT 2
#define PIN_BAT_ADC 1 // Battery voltage divider (100k/100k → GPIO1)
DHT dht(PIN_DHT, DHT22);

RTC_DATA_ATTR uint32_t bootCount = 0;
BLEManager bleManager;
OTAManager otaManager;
OTAManager* otaManagerPtr = nullptr;

void onActuatorCommand(const char* cmd_type, float cmd_value, int cmd_id) {
    Serial.printf("[CMD] Received: type=%s value=%.2f id=%d\n", cmd_type, cmd_value, cmd_id);

    if (strcmp(cmd_type, "vent_open") == 0) {
        // TODO: Open vent actuator
        Serial.println("[CMD] Vent OPEN");
    } else if (strcmp(cmd_type, "vent_close") == 0) {
        // TODO: Close vent actuator
        Serial.println("[CMD] Vent CLOSE");
    } else if (strcmp(cmd_type, "fan_on") == 0) {
        // TODO: Turn on fan
        Serial.println("[CMD] Fan ON");
    } else if (strcmp(cmd_type, "fan_off") == 0) {
        // TODO: Turn off fan
        Serial.println("[CMD] Fan OFF");
    } else {
        Serial.printf("[CMD] Unknown command: %s\n", cmd_type);
    }

    bleManager.sendAck(cmd_id, "acknowledged");
}

SensorReading readSensors() {
    SensorReading r;
    r.airTempC = 0.0f;
    r.airHumidityPct = 0.0f;
    r.airTempValid = false;
    r.airHumidityValid = false;
    r.batteryV = 0.0f;

    Serial.println("\n--- SENSOR DEBUG ---");

    dht.begin();
    delay(2200);  // DHT22 stabilization after begin / deep sleep

    float temp = dht.readTemperature();
    float hum  = dht.readHumidity();

    r.airTempValid = !isnan(temp);
    r.airHumidityValid = !isnan(hum);
    r.airTempC = r.airTempValid ? temp : 0.0f;
    r.airHumidityPct = r.airHumidityValid ? hum : 0.0f;

    if (r.airTempValid) {
        Serial.printf("Air Temp: %.2f C\n", r.airTempC);
    } else {
        Serial.println("Air Temp: ERROR (Check DHT22 wiring)");
    }

    if (r.airHumidityValid) {
        Serial.printf("Air Humidity: %.2f %%\n", r.airHumidityPct);
    } else {
        Serial.println("Air Humidity: ERROR (Check DHT22 wiring)");
    }

#if HAS_BAT_RESISTORS
    analogReadResolution(12);
    analogSetAttenuation(ADC_11db);
    int rawBat = analogRead(PIN_BAT_ADC);
    r.batteryV = ((rawBat / 4095.0f) * 3.3f) * 2.0f;
    Serial.printf("Battery: %.2fV (Raw: %d)\n", r.batteryV, rawBat);
#endif

    Serial.println("--------------------\n");
    return r;
}

void setup() {
    Serial.begin(115200);

    uint32_t startTime = millis();
    while (!Serial && (millis() - startTime < 3000)) {
        delay(10);
    }
    delay(1000);
    esp_ota_mark_app_valid_cancel_rollback();
    bootCount++;

    Serial.println("========================================");
    Serial.printf("BOOT %u | Device: %s\n", bootCount, DEVICE_ID);
    Serial.println("========================================");

    SensorReading r = readSensors();

    Serial.println("[SYS] Starting BLE Diagnostics...");
    bleManager.begin(DEVICE_ID, GATEWAY_NAME);
    bleManager.setCommandCallback(onActuatorCommand);
    bleManager.setOtaManager(&otaManager);
    otaManagerPtr = &otaManager;

    if (bleManager.runDiagnostics()) {
        delay(1000);
        String json = Telemetry::buildJson(r, DEVICE_ID, bootCount);
        bleManager.sendTelemetry(json);
        Serial.println("[TX] " + json);

        if (bleManager.checkOTACommand()) {
            String url = bleManager.getOTAUrl();
            Serial.println("[OTA] Update requested. Entering OTA mode.");
            bleManager.end();
            otaManager.begin(url);
        } else {
            bleManager.end();
        }
    } else {
        Serial.println("[SYS] BLE Gateway not found. Skipping transmission.");
    }

#if ENABLE_DEEPSLEEP
    Serial.printf("[SYS] Sleeping for %us...\n", SLEEP_INTERVAL_SEC);
    Serial.flush();
    esp_sleep_enable_timer_wakeup((uint64_t)SLEEP_INTERVAL_SEC * 1000000ULL);
    esp_deep_sleep_start();
#else
    Serial.println("[SYS] DEBUG MODE — Idle. Press the Reset/EN button to refresh readings.");
    Serial.flush();
#endif
}

void loop() {
    delay(1000);
}
