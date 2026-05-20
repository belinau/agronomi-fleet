"""µReticulum — Soil Node Sensor Drivers (SN-SOIL-01)

MicroPython drivers for:
  - Capacitive soil moisture sensor (ADC)
  - DS18B20 soil temperature (OneWire, parasite power)
  - Battery voltage divider (ADC)
"""

import gc
import time

import machine

# ---------------------------------------------------------------------------
# Soil moisture — capacitive sensor on ADC
# ---------------------------------------------------------------------------


def read_soil_moisture(pin, calib_dry, calib_wet, samples=8):
    """Read soil moisture percentage from a capacitive sensor.

    Args:
        pin:        GPIO number for the ADC input.
        calib_dry:  Voltage output in dry air (higher voltage = drier).
        calib_wet:  Voltage output in water (lower voltage = wetter).
        samples:    Number of ADC samples to average (reduces noise).

    Returns:
        float: Moisture percentage 0.0–100.0, or -1.0 on error.
    """
    try:
        adc = machine.ADC(pin)
        adc.atten(machine.ADC.ATTN_DB11)  # 0–3.3 V range (≈ ADC_11db)

        total = 0
        for _ in range(samples):
            total += adc.read()
            time.sleep_ms(2)

        raw = total // samples
        voltage = (raw / 4095.0) * 3.3

        # Linear map: dry voltage → 0%, wet voltage → 100%
        if calib_dry == calib_wet:
            return -1.0  # degenerate calibration
        pct = (voltage - calib_dry) * 100.0 / (calib_wet - calib_dry)
        pct = max(0.0, min(100.0, pct))  # clamp

        return round(pct, 2)

    except Exception as e:
        print("[SENSOR] Soil moisture error:", e)
        return -1.0


# ---------------------------------------------------------------------------
# DS18B20 soil temperature — OneWire with parasite power strong pull-up
# ---------------------------------------------------------------------------


def read_ds18b20(pin):
    """Read temperature from a DS18B20 on OneWire with parasite power.

    Parasite-power DS18B20s require a strong pull-up on the data line
    during the conversion window (~750 ms for 12-bit).  We drive the
    pin HIGH as a push-pull output for the conversion time, then release
    back to open-drain (input) so the 4.7 kΩ pull-up can idle the bus.

    Args:
        pin: GPIO number for the OneWire data line.

    Returns:
        tuple: (temp_c, valid) where temp_c is a float and valid is bool.
               If no sensor is found, returns (0.0, False).
    """
    try:
        from ds18x20 import DS18X20
        from onewire import OneWire

        data_pin = machine.Pin(pin, machine.Pin.IN, machine.Pin.PULL_UP)
        ow = OneWire(data_pin)
        ds = DS18X20(ow)

        roms = ds.scan()
        if not roms:
            print("[SENSOR] No DS18B20 found on pin", pin)
            return (0.0, False)

        # Use first discovered sensor
        rom = roms[0]

        # --- Start conversion ---
        ds.convert_temp()

        # --- Strong pull-up for parasite power ---
        # Drive DQ high as push-pull output for the full conversion time.
        # This supplies the ~1.5 mA the DS18B20 needs during conversion,
        # which the 4.7 kΩ pull-up alone cannot provide.
        pull_pin = machine.Pin(pin, machine.Pin.OUT, value=1)
        time.sleep_ms(750)  # 12-bit resolution: 750 ms max conversion

        # Release: back to open-drain so OneWire can communicate
        pull_pin = machine.Pin(pin, machine.Pin.IN, machine.Pin.PULL_UP)

        # --- Read scratchpad ---
        temp = ds.read_temp(rom)

        # DS18X20 driver returns None on CRC error, and 85.0 °C is the
        # power-on-default — treat both as invalid readings.
        if temp is None:
            print("[SENSOR] DS18B20 CRC error")
            return (0.0, False)
        if abs(temp - 85.0) < 0.5:
            print("[SENSOR] DS18B20 returned 85°C (power-on default)")
            return (0.0, False)

        return (round(temp, 2), True)

    except ImportError as e:
        print("[SENSOR] ds18x20 / onewire module not available:", e)
        return (0.0, False)
    except Exception as e:
        print("[SENSOR] DS18B20 error:", e)
        return (0.0, False)


# ---------------------------------------------------------------------------
# Battery voltage — ADC with 100k/100k voltage divider
# ---------------------------------------------------------------------------


def read_battery(pin, divider_ratio=2.0, samples=4):
    """Read battery voltage through a resistive divider.

    The divider scales V_bat down by *divider_ratio* so the ADC can
    safely measure it (e.g. 100k/100k divides in half).

    Args:
        pin:            GPIO number for the ADC input.
        divider_ratio:  V_bat / V_adc (default 2.0 for equal resistors).
        samples:        Number of ADC samples to average.

    Returns:
        float: Battery voltage in volts, or -1.0 on error.
    """
    try:
        adc = machine.ADC(pin)
        adc.atten(machine.ADC.ATTN_DB11)  # 0–3.3 V range

        total = 0
        for _ in range(samples):
            total += adc.read()
            time.sleep_ms(2)

        raw = total // samples
        v_adc = (raw / 4095.0) * 3.3
        v_bat = v_adc * divider_ratio

        return round(v_bat, 2)

    except Exception as e:
        print("[SENSOR] Battery ADC error:", e)
        return -1.0


# ---------------------------------------------------------------------------
# Convenience: read all sensors at once
# ---------------------------------------------------------------------------


def read_all(config):
    """Read all soil-node sensors and return a dict.

    Args:
        config: Module or dict with keys:
            PIN_SOIL_ADC, PIN_ONEWIRE, PIN_BAT_ADC,
            CALIB_DRY_V, CALIB_WET_V.

    Returns:
        dict with keys:
            soil_moisture_pct, soil_temp_c, soil_temp_valid, bat_v
    """
    gc.collect()

    result = {
        "soil_moisture_pct": -1.0,
        "soil_temp_c": 0.0,
        "soil_temp_valid": False,
        "bat_v": -1.0,
    }

    # Soil moisture
    result["soil_moisture_pct"] = read_soil_moisture(
        config.PIN_SOIL_ADC
        if hasattr(config, "PIN_SOIL_ADC")
        else config["PIN_SOIL_ADC"],
        config.CALIB_DRY_V if hasattr(config, "CALIB_DRY_V") else config["CALIB_DRY_V"],
        config.CALIB_WET_V if hasattr(config, "CALIB_WET_V") else config["CALIB_WET_V"],
    )

    # DS18B20 temperature
    temp_c, valid = read_ds18b20(
        config.PIN_ONEWIRE if hasattr(config, "PIN_ONEWIRE") else config["PIN_ONEWIRE"],
    )
    result["soil_temp_c"] = temp_c
    result["soil_temp_valid"] = valid

    # Battery voltage
    result["bat_v"] = read_battery(
        config.PIN_BAT_ADC if hasattr(config, "PIN_BAT_ADC") else config["PIN_BAT_ADC"],
    )

    gc.collect()
    return result
