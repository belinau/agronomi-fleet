// ============================================================================
// Farm Node Firmware — ESP32-CAM Greenhouse Vision Node (sn_vision)
// Mains Powered Version (No Deep Sleep)
// ============================================================================
// Architecture:
//   - Setup runs once.
//   - Loop runs forever.
//   - delay() used for interval timing (saves CPU cycles, keeps WiFi alive).
//   - OTA can be triggered instantly via HTTP Server or polling.
// ============================================================================

#include "esp_camera.h"
#include "esp_timer.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <FleetOTA/OTAManager.h>

// ---------------------------------------------------------------------------
// BUILD FLAG DEFAULTS
// ---------------------------------------------------------------------------
#ifndef DEVICE_ID
  #define DEVICE_ID "SN-VIS-GH-01"
#endif
#ifndef WIFI_SSID
  #define WIFI_SSID "greenhouse_wifi"
#endif
#ifndef WIFI_PASS
  #define WIFI_PASS ""
#endif
#ifndef FARM_POD_HOST
  #define FARM_POD_HOST "192.168.1.100"
#endif
#ifndef FARM_POD_PORT
  #define FARM_POD_PORT 8765
#endif
#ifndef CAPTURE_INTERVAL_SEC
  #define CAPTURE_INTERVAL_SEC 1800  // 30 mins
#endif
#ifndef FIRMWARE_VERSION
  #define FIRMWARE_VERSION "1.0.0"
#endif

// Timeouts
#define WIFI_TIMEOUT_MS     15000
#define HTTP_TIMEOUT_MS     30000

// ---------------------------------------------------------------------------
// AI THINKER CAMERA PIN MAP
// ---------------------------------------------------------------------------
#define CAM_PIN_PWDN     32
#define CAM_PIN_RESET    -1
#define CAM_PIN_XCLK      0
#define CAM_PIN_SIOD     26
#define CAM_PIN_SIOC     27
#define CAM_PIN_D7       35
#define CAM_PIN_D6       34
#define CAM_PIN_D5       39
#define CAM_PIN_D4       36
#define CAM_PIN_D3       21
#define CAM_PIN_D2       19
#define CAM_PIN_D1       18
#define CAM_PIN_D0        5
#define CAM_PIN_VSYNC    25
#define CAM_PIN_HREF     23
#define CAM_PIN_PCLK     22
#define PIN_FLASH_LED     4

// Global State
OTAManager ota;
unsigned long lastCaptureTime = 0;

// ---------------------------------------------------------------------------
// CAMERA INIT
// ---------------------------------------------------------------------------
bool initCamera() {
    camera_config_t config;
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer   = LEDC_TIMER_0;
    config.pin_d0       = CAM_PIN_D0;
    config.pin_d1       = CAM_PIN_D1;
    config.pin_d2       = CAM_PIN_D2;
    config.pin_d3       = CAM_PIN_D3;
    config.pin_d4       = CAM_PIN_D4;
    config.pin_d5       = CAM_PIN_D5;
    config.pin_d6       = CAM_PIN_D6;
    config.pin_d7       = CAM_PIN_D7;
    config.pin_xclk     = CAM_PIN_XCLK;
    config.pin_pclk     = CAM_PIN_PCLK;
    config.pin_vsync    = CAM_PIN_VSYNC;
    config.pin_href     = CAM_PIN_HREF;
    config.pin_sccb_sda = CAM_PIN_SIOD;
    config.pin_sccb_scl = CAM_PIN_SIOC;
    config.pin_pwdn     = CAM_PIN_PWDN;
    config.pin_reset    = CAM_PIN_RESET;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;
    config.grab_mode    = CAMERA_GRAB_LATEST;
    config.fb_location  = CAMERA_FB_IN_PSRAM;

    if (psramFound()) {
        config.frame_size   = FRAMESIZE_VGA;
        config.jpeg_quality = 12;
        config.fb_count     = 2;
    } else {
        config.frame_size   = FRAMESIZE_QVGA;
        config.jpeg_quality = 15;
        config.fb_count     = 1;
    }

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("[CAM] Init failed: 0x%x\n", err);
        return false;
    }

    sensor_t* s = esp_camera_sensor_get();
    if (s) {
        s->set_brightness(s, 0);
        s->set_contrast(s, 0);
        s->set_saturation(s, 0);
        s->set_whitebal(s, 1);
        s->set_awb_gain(s, 1);
        s->set_exposure_ctrl(s, 1);
        s->set_aec2(s, 1);
        s->set_gain_ctrl(s, 1);
        s->set_hmirror(s, 0);
        s->set_vflip(s, 0);
    }
    return true;
}

// ---------------------------------------------------------------------------
// WIFI
// ---------------------------------------------------------------------------
bool connectWiFi() {
    if (WiFi.status() == WL_CONNECTED) return true;

    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.printf("[WiFi] Connecting to %s", WIFI_SSID);

    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - start > WIFI_TIMEOUT_MS) {
            Serial.println("\n[WiFi] Timeout");
            return false;
        }
        delay(250);
        Serial.print(".");
    }
    Serial.printf("\n[WiFi] Connected, IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
}

// ---------------------------------------------------------------------------
// IMAGE CAPTURE + POST
// ---------------------------------------------------------------------------
void captureAndPost() {
    digitalWrite(PIN_FLASH_LED, LOW); // Ensure flash off (active high)

    camera_fb_t* fb = esp_camera_fb_get();
    if (!fb) {
        Serial.println("[CAM] Capture failed");
        return;
    }

    Serial.printf("[CAM] Captured %u bytes\n", fb->len);

    // Build JSON Metadata
    JsonDocument meta;
    meta["dev_id"]    = DEVICE_ID;
    meta["ts"]        = (uint32_t)(esp_timer_get_time() / 1000000ULL);
    meta["width"]     = fb->width;
    meta["height"]    = fb->height;
    meta["size_bytes"] = (uint32_t)fb->len;
    meta["wifi_rssi"] = WiFi.RSSI();
    meta["fw_ver"]    = FIRMWARE_VERSION;
    String metaStr;
    serializeJson(meta, metaStr);

    // POST
    String url = String("http://") + FARM_POD_HOST + ":" + FARM_POD_PORT + "/api/vision/ingest";

    HTTPClient http;
    http.begin(url);
    http.setTimeout(HTTP_TIMEOUT_MS);
    http.addHeader("X-Device-ID", DEVICE_ID);
    http.addHeader("X-Meta", metaStr);
    http.addHeader("Content-Type", "image/jpeg");

    int code = http.POST(fb->buf, fb->len);
    esp_camera_fb_return(fb); // Free memory immediately

    if (code == 200 || code == 201) {
        String response = http.getString();
        Serial.printf("[HTTP] OK: %s\n", response.c_str());

        // Check for OTA Trigger in Response
        JsonDocument doc;
        if (!deserializeJson(doc, response) && doc["ota_url"].is<String>()) {
            String otaUrl = doc["ota_url"].as<String>();
            if (otaUrl.length() > 0) {
                Serial.println("[SYS] OTA Trigger received. Updating...");
                http.end();
                ota.begin(otaUrl);
            }
        }
    } else {
        Serial.printf("[HTTP] Error: %d\n", code);
    }
    http.end();
}

// ---------------------------------------------------------------------------
// SETUP
// ---------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(1000); // Give serial monitor time to connect

    Serial.printf("\n=== Vision Node %s (v%s) ONLINE ===\n", DEVICE_ID, FIRMWARE_VERSION);

    pinMode(PIN_FLASH_LED, OUTPUT);
    digitalWrite(PIN_FLASH_LED, LOW);

    // 1. Connect WiFi (Keep alive)
    if (!connectWiFi()) {
        Serial.println("[ERR] WiFi failed. Retrying in loop...");
    }

    // 2. Init Camera (Keep initialized? Or init per capture?)
    // Init once is fine for mains power.
    if (!initCamera()) {
        Serial.println("[ERR] Camera Init Failed!");
    }
}

// ---------------------------------------------------------------------------
// LOOP
// ---------------------------------------------------------------------------
void loop() {
    // 1. Maintain WiFi Connection
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[SYS] WiFi Lost. Reconnecting...");
        connectWiFi();
    }

    // 2. Capture Interval Logic
    if (millis() - lastCaptureTime > (CAPTURE_INTERVAL_SEC * 1000)) {
        Serial.println("[SYS] Capture Interval Reached");
        lastCaptureTime = millis();

        captureAndPost();
    }

    // 3. Yield / Low Power Idle
    // delay(100) allows the WiFi stack to process background tasks (TCP, DHCP, etc.)
    // It reduces CPU usage significantly compared to a tight loop.
    delay(100);
}
