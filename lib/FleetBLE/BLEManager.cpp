// BLEManager.cpp
#include "BLEManager.h"
#include "OTAManager.h"
#include <NimBLEDevice.h>
#include <ArduinoJson.h>

volatile bool _deviceConnected = false;
volatile bool _otaRequested = false;
String _otaUrl = "";
const char* _deviceId = "";
const char* _gatewayName = "";

// Static state for command callback and OTA dispatch
static CommandCallback _commandCallback = nullptr;
static OTAManager* _otaManager = nullptr;

NimBLEClient* _pClient = nullptr;
NimBLERemoteCharacteristic* _pRxChar = nullptr;
NimBLERemoteCharacteristic* _pTxChar = nullptr;

NimBLEServer* _pServer = nullptr;
NimBLECharacteristic* _pOutChar = nullptr;
NimBLECharacteristic* _pInChar = nullptr;

// Connection handle for server-mode notify
static uint16_t _serverConnHandle = 0xFFFF;

static const char* NUS_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
static const char* NUS_TX_UUID      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E";
static const char* NUS_RX_UUID      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E";

// OTA frame headers (must match gateway ble_ota.py)
#define OTA_HDR_BEGIN  0xA0
#define OTA_HDR_DATA   0xA1
#define OTA_HDR_END    0xA2
#define OTA_HDR_ABORT  0xA3

class ClientCB : public NimBLEClientCallbacks {
    void onConnect(NimBLEClient* pClient) override { _deviceConnected = true; };
    void onDisconnect(NimBLEClient* pClient, int reason) override {
        _deviceConnected = false;
        _pRxChar = nullptr;
        _pTxChar = nullptr;
    };
};

// Called when the Pico (gateway) sends a notify on the NUS TX characteristic.
// This can be a JSON command string OR a binary OTA frame.
static void notifyCB(NimBLERemoteCharacteristic* chr, uint8_t* data, size_t length, bool isNotify) {
    if (length == 0) return;

    // Binary OTA frame? First byte is the header byte.
    uint8_t hdr = data[0];
    if (hdr == OTA_HDR_BEGIN || hdr == OTA_HDR_DATA || hdr == OTA_HDR_END || hdr == OTA_HDR_ABORT) {
        BLEManager::handleNusNotify(data, length);
        return;
    }

    // Otherwise treat as JSON command string
    String cmd((char*)data, length);
    BLEManager::handleCommand(cmd);
}

class ServerCB : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer* pServer, NimBLEConnInfo& connInfo) override {
        _deviceConnected = true;
        _serverConnHandle = connInfo.getConnHandle();
    };
    void onDisconnect(NimBLEServer* pServer, NimBLEConnInfo& connInfo, int reason) override {
        _deviceConnected = false;
        _serverConnHandle = 0xFFFF;
        if (NimBLEDevice::isInitialized()) {
            NimBLEDevice::getAdvertising()->start();
        }
    };
};

class CharCB : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic* pChar, NimBLEConnInfo& connInfo) override {
        String cmd = pChar->getValue().c_str();
        BLEManager::handleCommand(cmd);
    }
};

BLEManager::BLEManager() {}

void BLEManager::begin(const char* deviceId, const char* gatewayName) {
    _deviceId = deviceId;
    _gatewayName = gatewayName;
    _otaRequested = false;
    _otaUrl = "";
    _pRxChar = nullptr;
    _pTxChar = nullptr;
    _pOutChar = nullptr;
    _pInChar = nullptr;
}

void BLEManager::setCommandCallback(CommandCallback cb) {
    _commandCallback = cb;
}

void BLEManager::setOtaManager(OTAManager* ota) {
    _otaManager = ota;
}

void BLEManager::handleCommand(String cmd) {
    Serial.printf("[BLE RX] %s\n", cmd.c_str());
    JsonDocument doc;
    if (deserializeJson(doc, cmd)) {
        Serial.printf("[BLE] JSON parse error: %s\n", cmd.c_str());
        return;
    }

    // Check for OTA start command (WiFi path)
    if (doc["cmd_type"] == "start_ota") {
        _otaRequested = true;
        if (doc["url"].is<String>()) {
            _otaUrl = doc["url"].as<String>();
        }
        return;
    }

    // Check for actuator commands — dispatch to registered callback
    const char* cmd_type = doc["cmd_type"] | "";
    int cmd_id = doc["cmd_id"] | -1;
    float cmd_value = doc["cmd_value"] | 0.0f;

    if (strlen(cmd_type) > 0 && cmd_id >= 0 && _commandCallback) {
        _commandCallback(cmd_type, cmd_value, cmd_id);
    }
}

void BLEManager::handleNusNotify(uint8_t* data, size_t length) {
    uint8_t hdr = data[0];

    if (hdr == OTA_HDR_BEGIN) {
        // [0xA0][total_size uint32 LE 4B][fw_version str]
        if (length < 5 || !_otaManager) {
            Serial.println("[OTA] OTA_BEGIN but no manager or too short");
            return;
        }
        uint32_t total = data[1] | (data[2] << 8) | (data[3] << 16) | (data[4] << 24);
        char ver[32] = {0};
        size_t vlen = (length > 5) ? min((size_t)(length - 5), (size_t)31) : 0;
        memcpy(ver, data + 5, vlen);

        Serial.printf("[OTA] BLE OTA begin: %u bytes, fw=%s\n", total, ver);
        if (!_otaManager->beginBLE(total, ver)) {
            // Send NAK
            String nak = "{\"ota_ok\":false,\"error\":\"esp_ota_begin failed\"}";
            BLEManager().sendAck(-1, "failed", "esp_ota_begin failed");
            // Also send OTA NAK via NUS if connected
            if (_pClient && _pRxChar) {
                _pRxChar->writeValue(nak.c_str(), nak.length());
            }
        }
    }
    else if (hdr == OTA_HDR_DATA) {
        if (!_otaManager || !_otaManager->isActive()) return;
        if (!_otaManager->writeChunk(data, length)) {
            _otaManager->abortBLE();
            String nak = "{\"ota_ok\":false,\"error\":\"chunk write failed\"}";
            if (_pClient && _pRxChar) {
                _pRxChar->writeValue(nak.c_str(), nak.length());
            }
        }
    }
    else if (hdr == OTA_HDR_END) {
        if (!_otaManager || !_otaManager->isActive()) return;
        char ver[32] = {0};
        size_t vlen = (length > 1) ? min((size_t)(length - 1), (size_t)31) : 0;
        memcpy(ver, data + 1, vlen);

        // Send ACK BEFORE finalize — finalize reboots
        String ack = String("{\"ota_ok\":true,\"fw_ver\":\"") + ver + "\"}";
        if (_pClient && _pRxChar) {
            _pRxChar->writeValue(ack.c_str(), ack.length());
        }
        delay(300);  // Give BLE stack time to transmit ACK before reboot
        _otaManager->finalizeBLE(ver);
    }
    else if (hdr == OTA_HDR_ABORT) {
        if (_otaManager) _otaManager->abortBLE();
    }
}

void BLEManager::sendAck(int cmd_id, const char* status, const char* error) {
    JsonDocument doc;
    doc["cmd_id"] = cmd_id;
    doc["status"] = status;
    if (error) doc["error"] = error;
    String ack;
    serializeJson(doc, ack);

    if (_pClient && _pRxChar && _pClient->isConnected()) {
        _pRxChar->writeValue(ack.c_str(), ack.length());
    } else if (_pServer && _pOutChar && _deviceConnected) {
        _pOutChar->setValue(ack);
        _pOutChar->notify();
    }
    Serial.printf("[BLE ACK] cmd_id=%d status=%s\n", cmd_id, status);
}

void BLEManager::sendOtaAck(int cmd_id, bool ok, const char* fw_version, const char* error) {
    JsonDocument doc;
    doc["cmd_id"] = cmd_id;
    doc["ota_ok"] = ok;
    if (fw_version) doc["fw_ver"] = fw_version;
    if (error) doc["error"] = error;
    String ack;
    serializeJson(doc, ack);

    if (_pClient && _pRxChar && _pClient->isConnected()) {
        _pRxChar->writeValue(ack.c_str(), ack.length());
    } else if (_pServer && _pOutChar && _deviceConnected) {
        _pOutChar->setValue(ack);
        _pOutChar->notify();
    }
    Serial.printf("[BLE OTA ACK] cmd_id=%d ok=%d\n", cmd_id, ok);
}

bool BLEManager::runDiagnostics() {
    if (_runPhase1Client()) return true;
    return _runPhase2Server();
}

bool BLEManager::_runPhase1Client() {
    if (!_deviceId || strlen(_deviceId) == 0) return false;

    Serial.println("[Phase 1] Scanning...");

    if (!NimBLEDevice::isInitialized()) {
        NimBLEDevice::init("");
        delay(50);
    }

    NimBLEScan* pScan = NimBLEDevice::getScan();
    if (!pScan) {
        Serial.println("[Phase 1] Scan object null!");
        return false;
    }

    pScan->setActiveScan(true);
    NimBLEScanResults results = pScan->getResults(4000);

    NimBLEAdvertisedDevice* target = nullptr;
    for (int i = 0; i < results.getCount(); i++) {
        const NimBLEAdvertisedDevice* dev = results.getDevice(i);
        if (dev->getName() == _gatewayName) {
            target = const_cast<NimBLEAdvertisedDevice*>(dev);
            break;
        }
    }

    if (!target) {
        Serial.println("[Phase 1] Not found.");
        return false;
    }

    if (!_pClient) {
        _pClient = NimBLEDevice::createClient();
        if (!_pClient) {
            Serial.println("[Phase 1] createClient failed!");
            return false;
        }
        _pClient->setClientCallbacks(new ClientCB(), true);
    }

    if (_pClient->connect(target)) {
        Serial.println("[Phase 1] Connected!");
        NimBLERemoteService* pSvc = _pClient->getService(NUS_SERVICE_UUID);
        if (pSvc) {
            _pRxChar = pSvc->getCharacteristic(NUS_RX_UUID);
            _pTxChar = pSvc->getCharacteristic(NUS_TX_UUID);
            if (_pTxChar && _pTxChar->canNotify())
                _pTxChar->subscribe(true, notifyCB);
            return true;
        }
        _pClient->disconnect();
    }

    Serial.println("[Phase 1] Connect/service failed.");
    return false;
}

bool BLEManager::_runPhase2Server() {
    if (!_deviceId || strlen(_deviceId) == 0) return false;

    Serial.println("[Phase 2] Starting Peripheral...");

    if (!NimBLEDevice::isInitialized()) {
        NimBLEDevice::init("");
        delay(50);
    }

    NimBLEDevice::setPower(ESP_PWR_LVL_P3);

    if (!_pServer) {
        _pServer = NimBLEDevice::createServer();
        if (!_pServer) {
            Serial.println("[Phase 2] createServer failed!");
            return false;
        }
        _pServer->setCallbacks(new ServerCB());

        NimBLEService* pSvc = _pServer->createService(NUS_SERVICE_UUID);
        _pOutChar = pSvc->createCharacteristic(NUS_TX_UUID, NIMBLE_PROPERTY::NOTIFY);
        _pInChar = pSvc->createCharacteristic(NUS_RX_UUID, NIMBLE_PROPERTY::WRITE);
        _pInChar->setCallbacks(new CharCB());
    }

    NimBLEAdvertising* pAdv = NimBLEDevice::getAdvertising();
    pAdv->setName(_deviceId);
    pAdv->addServiceUUID(NUS_SERVICE_UUID);
    pAdv->start();

    int timeout = 50;
    while (!_deviceConnected && timeout > 0) { delay(100); timeout--; }
    return _deviceConnected;
}

void BLEManager::sendTelemetry(String json) {
    if (!_deviceConnected) return;

    if (_pClient && _pRxChar) {
        _pRxChar->writeValue(json.c_str(), json.length());
    } else if (_pServer && _pOutChar) {
        _pOutChar->setValue(json);
        _pOutChar->notify();
    }
}

bool BLEManager::checkOTACommand() { delay(2000); return _otaRequested; }
String BLEManager::getOTAUrl() { return _otaUrl; }

void BLEManager::end() {
    if (_pClient && _pClient->isConnected()) {
        _pClient->disconnect();
    }
    _pRxChar = nullptr;
    _pTxChar = nullptr;
}