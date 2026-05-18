# Hardware Fleet Deployment Checklist

## Pico 2W BLE Bridge Deployment (Mimi)

### One-time setup
- [ ] Copy `bt_bridge/boot.py` to Pico 2W: `mpremote cp bt_bridge/boot.py :boot.py`
- [ ] Copy `bt_bridge/main.py` to Pico 2W: `mpremote cp bt_bridge/main.py :main.py`
- [ ] Reset Pico: `mpremote reset`
- [ ] Verify Pico serial output: `python3 scripts/test_pico_serial.py --port /dev/pico`
- [ ] Expected output: `[SER] Pico boot:`, `[SER] MicroPython`, `[SER] Freq=`, `Advertising`, `[SER] Ready for commands`
- [ ] If REPL prompt (`>>>`) appears instead, boot.py was not deployed correctly

### Gateway (Mimi) deployment
- [ ] Install udev rule: `sudo cp scripts/99-pico.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger`
- [ ] Verify `/dev/pico` symlink exists: `ls -la /dev/pico`
- [ ] Add `livada` to dialout group: `sudo usermod -aG dialout livada` (then re-login)
- [ ] Copy `bt_bridge/ble_forwarder.py` and `bt_bridge/ble_forwarder.toml` to `~/CascadeProjects/ble_forwarder/`
- [ ] Copy `scripts/agronomi-gateway-wrapper.sh` to `~/CascadeProjects/ble_forwarder/`
- [ ] Copy `scripts/agronomi-gateway.service` to `/etc/systemd/system/`
- [ ] Enable service: `sudo systemctl enable agronomi-gateway && sudo systemctl start agronomi-gateway`
- [ ] Check logs: `tail -f ~/agronomi.log`
- [ ] Verify Pico boot banner in logs: `[SER] Pico boot confirmed — output detected`

### Key files on Pico
| File | Purpose |
|------|----------|
| `boot.py` | Redirects REPL to UART0, freeing USB CDC for data. **Critical** — without this, no serial output. |
| `main.py` | BLE GATT peripheral + serial bridge. Emits `[JSON]`, `[ACK]`, `[HB]`, `[SER]` lines. |

### Key files on Mimi
| File | Purpose |
|------|----------|
| `ble_forwarder.py` | Reads serial from Pico, forwards to hub via RNS. Handles `[JSON]`, `[ACK]`, `[HB]`, `[SER]` lines. |
| `ble_forwarder.toml` | Config: serial port, baud, gateway ID, command aspect. |
| `agronomi-gateway-wrapper.sh` | Service wrapper: waits for `/dev/pico`, launches `ble_forwarder.py`. |
| `agronomi-gateway.service` | Systemd unit for the gateway. |
| `99-pico.rules` | Udev rule: creates `/dev/pico` symlink with mode 0666. |

## Pre-Deployment (Lab)
- [ ] Flash all RAK4631 gateways with RNode Firmware CE v1.75 (MODEL_12, EU868)
- [ ] Verify LoRa range between Farm Hub and each planned gateway location (> -120 dBm RSSI)
- [ ] Flash all ESP32-C6 nodes with appropriate firmware (sn_soil / sn_air / an_pump / an_gh / sn_vision)
- [x ] Calibrate soil moisture sensors (record dry/wet ADC values per node)
- [ ] Pair each ESP32-C6 with its assigned GW-RAK; record BLE MAC addresses
- [ ] Run 48-hour burn-in test on bench power; verify no memory leaks or hangs
- [ ] Verify deep sleep current: < 50 µA for ESP32-C6, < 10 µA for RAK4631
- [ ] Test actuator safety interlocks (pump auto-off on disconnect, end-stop homing)
- [ ] Prepare Pi W SD card images: flash OS, install dependencies, pre-configure Reticulum identity
- [ ] Test Pi W + RAK4631 USB OTG connectivity: verify `/dev/ttyACM0` enumerates
- [ ] Test `gw_piw_bridge.py` on bench: verify BLE→LoRa→Farm Hub end-to-end

## Database Preparation
- [ ] Run `003_hardware_fleet_migration.sql` against production SQLite database
- [ ] Insert all hardware_devices, reticulum_gateways records
- [ ] Map each sensor_nodes.node_id to its hardware_devices.device_id
- [ ] Verify `reticulum_ingest.py` starts and creates Reticulum destination hashes
- [ ] Test ingestion loop: inject fake JSON payload → verify sensor_readings row

## Field Installation (Per Site)
- [ ] Mount GW-RAK at highest available point, LoRa antenna vertical, clear view
- [ ] Install solar panel facing south (northern hemisphere), 30-45° tilt
- [ ] Place SN-SOIL nodes at 20cm depth (moisture probe) + 10cm depth (temp probe)
- [ ] Place SN-AIR nodes inside radiation shield, 1.5m above ground
- [ ] Install AN-PUMP with relay in waterproof junction box; fuse pump circuit
- [ ] Install AN-GH servos with mechanical linkage; test full range without binding
- [ ] Label every device with QR code linking to device_id + field_id

## Pi W Gateway Installation (Production)
- [ ] Flash Raspberry Pi OS Lite (64-bit Bookworm) to microSD card
- [ ] Enable SSH, configure WiFi credentials in `wpa_supplicant.conf` or NetworkManager
- [ ] Install RNS: `pip install RNS`
- [ ] Install bleak: `pip install bleak`
- [ ] Copy Reticulum config to `~/.reticulum/config` with RNodeInterface on `/dev/ttyACM0`
- [ ] Copy `gw_piw_bridge.py` bridge service to `/opt/farm/`
- [ ] Install systemd service file for `gw_piw_bridge.py`
- [ ] Connect RAK4631 to Pi W via micro USB OTG adapter
- [ ] Verify RAK4631 appears as `/dev/ttyACM0` (`lsusb` or `dmesg`)
- [ ] Verify Reticulum transport node starts: `rnstatus` shows interface up
- [ ] Verify BLE scanning discovers ESP32-C6 nodes in range
- [ ] Seal enclosure with cable glands for LoRa antenna and solar power
- [ ] Label with QR code: device_id, gateway_id, RNS destination hash

## Commissioning
- [ ] Power on GW-RAK; verify it appears in Reticulum path table (`rnstatus`)
- [ ] Power on each ESP32-C6; verify BLE connection log entry within 5 minutes
- [ ] Confirm first telemetry packet received at Farm Hub; verify DB insertion
- [ ] Run `refresh_alert_cache()` manually; confirm no OFFLINE false positives
- [ ] Send test actuator command; verify physical action and ACK in DB

## Monitoring (Ongoing)
- [ ] Daily: Check `v_node_status` view for any nodes with connectivity_status = 'offline'
- [ ] Weekly: Review `ble_link_log` RSSI trends; reposition nodes if signal degrades
- [ ] Monthly: Battery voltage trend analysis; replace/recharge before critical
- [ ] Quarterly: Soil moisture calibration check against gravimetric samples
- [ ] Quarterly: Firmware OTA update cycle (test on 1 node, then fleet)
