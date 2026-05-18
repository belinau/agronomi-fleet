// ============================================================================
// AgroNomi Node Firmware — SN-SOIL
// ============================================================================
#include <Arduino.h>
#include <esp_sleep.h>
#include <esp_ota_ops.h>
#include "Telemetry.h"
#include "BLEManager.h"
#include "OTAManager.h"
#include <OneWire.h>              // OneWireNg compatibility layer (ESP32-C6 safe)
#include <DallasTemperature.h>
#define PIN_ONEWIRE 3 // DS18B20 data line (4.7k pull-up to 3.3V)
OneWire oneWire(PIN_ONEWIRE);
DallasTemperature ds18b20(&oneWire);
#define PIN_SOIL_ADC    2
#define PIN_BAT_ADC     1 // Battery voltage divider (100k/100k → GPIO1)

RTC_DATA_ATTR uint32_t bootCount = 0;
BLEManager bleManager;
OTAManager otaManager;
OTAManager* otaManagerPtr = nullptr;

float calibDry = 1.815f;
float calibWet = 1.378f;

void onActuatorCommand(const char* cmd_type, float cmd_value, int cmd_id) {
    Serial.printf("[CMD] Received: type=%s value=%.2f id=%d\n", cmd_type, cmd_value, cmd_id);

    // Handle actuator commands
    if (strcmp(cmd_type, "pump_on") == 0) {
        // TODO: Turn on pump relay
        Serial.println("[CMD] Pump ON");
    } else if (strcmp(cmd_type, "pump_off") == 0) {
        // TODO: Turn off pump relay
        Serial.println("[CMD] Pump OFF");
    } else {
        Serial.printf("[CMD] Unknown command: %s\n", cmd_type);
    }

    // Send ACK back through BLE
    bleManager.sendAck(cmd_id, "acknowledged");
}

SensorReading readSensors() {
    SensorReading r;

    // --- Soil Moisture Reading ---
    analogReadResolution(12);
    analogSetAttenuation(ADC_11db);
    int raw = analogRead(PIN_SOIL_ADC);
    float voltage = (raw / 4095.0f) * 3.3f;
    r.soilMoisturePct = constrain((voltage - calibDry) * 100.0f / (calibWet - calibDry), 0.0f, 100.0f);

    // --- DEBUG OUTPUT: Capacitive Sensor ---
    Serial.println("\n--- SENSOR DEBUG ---");
    Serial.printf("Soil Raw ADC: %d\n", raw);
    Serial.printf("Soil Voltage: %.3fV\n", voltage);
    Serial.printf("Soil Moisture: %.2f%%\n", r.soilMoisturePct);

#if HAS_TEMP_SENSOR
    ds18b20.begin();
    ds18b20.setResolution(12);
    ds18b20.setWaitForConversion(false);  // Do not block; we handle strong pull-up manually
    ds18b20.requestTemperatures();

    // === STRONG PULL-UP for parasite power ===
    // The DS18B20 draws ~1.5 mA during conversion. A 4.7 kΩ pull-up cannot
    // supply enough current, so the bus voltage collapses and the sensor
    // resets (yielding 85 °C or DEVICE_DISCONNECTED_C). We must actively
    // drive the DQ line HIGH as an output for the full conversion time.
    pinMode(PIN_ONEWIRE, OUTPUT);
    digitalWrite(PIN_ONEWIRE, HIGH);
    delay(750);                          // 12-bit resolution = 750 ms max
    pinMode(PIN_ONEWIRE, INPUT);         // Release strong pull-up; external 4.7k holds line high
    // ========================================

    float temp = ds18b20.getTempCByIndex(0);
    r.soilTempValid = (temp != DEVICE_DISCONNECTED_C) && !isnan(temp) && (temp != 85.0f);
    r.soilTempC = r.soilTempValid ? temp : 0.0f;

    if (r.soilTempValid) {
        Serial.printf("Soil Temp: %.2f C\n", r.soilTempC);
    } else {
        Serial.println("Soil Temp: ERROR (Check probe)");
    }
#else
    r.soilTempValid = false;
    r.soilTempC = 0.0f;
#endif

#if HAS_BAT_RESISTORS
    analogSetAttenuation(ADC_11db);
    int rawBat = analogRead(PIN_BAT_ADC);
    r.batteryV = ((rawBat / 4095.0f) * 3.3f) * 2.0f;
    Serial.printf("Battery: %.2fV (Raw: %d)\n", r.batteryV, rawBat);
#else
    r.batteryV = 0.0f;
#endif
    Serial.println("--------------------\n");

    return r;
}

void setup() {
    Serial.begin(115200);

    // Mark firmware as valid to prevent bootloader rollback
    esp_ota_mark_app_valid_cancel_rollback();

    bootCount++;

    Serial.println("========================================");
    Serial.printf("BOOT %u | Device: %s\n", bootCount, DEVICE_ID);
    Serial.println("========================================");

    // 1. Read sensors immediately (even if BLE is disconnected)
    SensorReading r = readSensors();

    // 2. Initialize BLE
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
    // In Debug Mode (No Sleep), we just stay idle.
    // New readings are triggered by the hardware Reset button.
    delay(1000);
}
