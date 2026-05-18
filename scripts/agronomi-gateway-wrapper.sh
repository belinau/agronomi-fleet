#!/bin/bash
# agronomi-gateway-wrapper.sh — Service wrapper for ble_forwarder
#
# Waits for the Pico 2W USB serial device to appear, then launches
# ble_forwarder. The Pico auto-runs main.py on boot — no mpremote
# reset needed under normal operation.
#
# CRITICAL: Do NOT mpremote reset the Pico at service start. It causes
# USB re-enumeration (device disappears, reappears as different ttyACM*)
# which breaks the serial port connection the forwarder depends on.
# The Pico auto-runs main.py from its filesystem. If the Pico needs a
# reset after a firmware update, set PICO_RESET=1 or physically
# replug it.
#
# ble_forwarder handles SerialException by reconnecting via open_serial()
# with retries, so transient USB glitches are self-healing.

set -euo pipefail

PICO_DEV="/dev/pico"
BLE_FORWARDER="/home/livada/CascadeProjects/ble_forwarder/ble_forwarder.py"
BLE_FORWARDER_CONFIG="/home/livada/CascadeProjects/ble_forwarder/ble_forwarder.toml"
LOG_TAG="agronomi-wrapper"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$LOG_TAG] $*" >&2
}

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
log "Starting AgroNomi gateway wrapper"
log "Pico device: $PICO_DEV"
log "Forwarder: $BLE_FORWARDER"

# Wait for /dev/pico if it's not there yet (boot race)
WAIT=0
while [ ! -e "$PICO_DEV" ]; do
    if [ "$WAIT" -ge 30 ]; then
        log "Timed out waiting for $PICO_DEV"
        exit 1
    fi
    log "Waiting for $PICO_DEV to appear..."
    sleep 1
    WAIT=$((WAIT + 1))
done

# Only reset if explicitly requested (e.g. after firmware deployment).
# Default is NO reset — see header comment for rationale.
if [ "${PICO_RESET:-0}" = "1" ]; then
    log "PICO_RESET=1, resetting Pico..."
    mpremote connect "$PICO_DEV" reset 2>/dev/null || true
    log "Waiting 6s for Pico to reboot and USB to re-enumerate..."
    sleep 6
    # Re-wait for port to reappear after reset
    WAIT=0
    while [ ! -e "$PICO_DEV" ]; do
        if [ "$WAIT" -ge 30 ]; then
            log "Timed out waiting for $PICO_DEV after reset"
            exit 1
        fi
        sleep 1
        WAIT=$((WAIT + 1))
    done
else
    log "Pico device present, launching forwarder (no reset)"
fi

log "$PICO_DEV available, launching ble_forwarder"
exec /usr/bin/python3 -u "$BLE_FORWARDER" --config "$BLE_FORWARDER_CONFIG"
