// BLEManager.h
#ifndef BLEMANAGER_H
#define BLEMANAGER_H

#include <Arduino.h>

// Forward declaration — OTAManager is defined in its own header
class OTAManager;

// Command callback type: called when an actuator command is received
typedef void (*CommandCallback)(const char* cmd_type, float cmd_value, int cmd_id);

class BLEManager {
public:
    BLEManager();
    void begin(const char* deviceId, const char* gatewayName);
    bool runDiagnostics();
    void sendTelemetry(String json);
    bool checkOTACommand();
    String getOTAUrl();
    void end();

    // New: command callback registration
    void setCommandCallback(CommandCallback cb);

    // New: send an ACK response back through BLE
    // In client mode: writes to NUS RX characteristic
    // In server mode: notifies on NUS TX characteristic
    void sendAck(int cmd_id, const char* status, const char* error = nullptr);

    // New: send an OTA ACK response
    void sendOtaAck(int cmd_id, bool ok, const char* fw_version = nullptr, const char* error = nullptr);

    // New: set the OTAManager pointer so BLEManager can dispatch BLE OTA frames
    void setOtaManager(OTAManager* ota);

    // Static handlers called from BLE callbacks — must be public for callback access
    static void handleCommand(String cmd);
    static void handleNusNotify(uint8_t* data, size_t length);

private:
    bool _runPhase1Client();
    bool _runPhase2Server();
};

#endif