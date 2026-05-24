"""µReticulum — Air Quality Node Sensor Drivers (SN-AIR-01)

MicroPython drivers for:
  - DHT22 air temperature + humidity (GPIO2)
  - Battery voltage divider (100k/100k → GPIO1)

DHT22 timing notes (per MicroPython docs):
  - DHT22 requires ≥2 seconds stabilisation after power-on or deep-sleep
    wake (the sensor's internal capacitor needs time to charge).
  - measure() itself takes ~2ms (one-wire bus transaction).
  - Do NOT read more than once every 2 seconds.
  - After measure(), check for NaN — indicates bus error.

For async usage: use read_dht22_async() which yields to the event loop
during the stabilisation wait, so BLE and other async tasks can continue.
"""

import gc
import math
import time

import machine

# ---------------------------------------------------------------------------
# DHT22 — air temperature and humidity (synchronous version)
# ---------------------------------------------------------------------------


def read_dht22(pin, retries=3):
    """Read temperature and humidity from a DHT22 sensor (blocking).

    The DHT22 requires a 2-second stabilisation period after power-on or
    deep-sleep wake. This function blocks for that duration plus any
    retry delays. Use read_dht22_async() instead if running inside an
    asyncio event loop.

    Args:
        pin:     GPIO number for the DHT22 data line.
        retries: Number of read attempts before giving up.

    Returns:
        tuple: (temp_c, humidity_pct, temp_valid, humidity_valid)
    """
    try:
        import dht

        sensor = dht.DHT22(machine.Pin(pin))

        # DHT22 needs ≥2 s stabilisation after power-on / deep-sleep wake
        time.sleep_ms(2000)

        for attempt in range(retries):
            try:
                sensor.measure()
                temp = sensor.temperature()
                hum = sensor.humidity()

                temp_valid = not math.isnan(temp)
                humidity_valid = not math.isnan(hum)

                temp_c = round(temp, 2) if temp_valid else 0.0
                humidity_pct = round(hum, 2) if humidity_valid else 0.0

                if temp_valid and humidity_valid:
                    return (temp_c, humidity_pct, True, True)

                # Partial success — return what we have but keep retrying
                if attempt < retries - 1:
                    time.sleep_ms(500)
                    continue

                return (temp_c, humidity_pct, temp_valid, humidity_valid)

            except Exception as e:
                print(
                    "[SENSOR] DHT22 read attempt {} failed: {}".format(attempt + 1, e)
                )
                if attempt < retries - 1:
                    time.sleep_ms(500)

        # All retries exhausted
        print("[SENSOR] DHT22: all {} read attempts failed".format(retries))
        return (0.0, 0.0, False, False)

    except ImportError as e:
        print("[SENSOR] dht module not available:", e)
        return (0.0, 0.0, False, False)
    except Exception as e:
        print("[SENSOR] DHT22 error:", e)
        return (0.0, 0.0, False, False)


# ---------------------------------------------------------------------------
# DHT22 — async version (yields to event loop during stabilisation wait)
# ---------------------------------------------------------------------------


async def read_dht22_async(pin, retries=3):
    """Read temperature and humidity from a DHT22 sensor (async).

    Same as read_dht22() but uses asyncio.sleep_ms() for the stabilisation
    and retry waits, allowing other async tasks (BLE poll loops, etc.) to
    continue running.

    Args:
        pin:     GPIO number for the DHT22 data line.
        retries: Number of read attempts before giving up.

    Returns:
        tuple: (temp_c, humidity_pct, temp_valid, humidity_valid)
    """
    try:
        import dht
        import uasyncio as asyncio

        sensor = dht.DHT22(machine.Pin(pin))

        # DHT22 needs ≥2 s stabilisation after power-on / deep-sleep wake.
        # Yield to the event loop so BLE/transport tasks can keep running.
        await asyncio.sleep_ms(2000)

        for attempt in range(retries):
            try:
                sensor.measure()
                temp = sensor.temperature()
                hum = sensor.humidity()

                temp_valid = not math.isnan(temp)
                humidity_valid = not math.isnan(hum)

                temp_c = round(temp, 2) if temp_valid else 0.0
                humidity_pct = round(hum, 2) if humidity_valid else 0.0

                if temp_valid and humidity_valid:
                    return (temp_c, humidity_pct, True, True)

                # Partial success — return what we have but keep retrying
                if attempt < retries - 1:
                    await asyncio.sleep_ms(500)
                    continue

                return (temp_c, humidity_pct, temp_valid, humidity_valid)

            except Exception as e:
                print(
                    "[SENSOR] DHT22 read attempt {} failed: {}".format(attempt + 1, e)
                )
                if attempt < retries - 1:
                    await asyncio.sleep_ms(500)

        # All retries exhausted
        print("[SENSOR] DHT22: all {} read attempts failed".format(retries))
        return (0.0, 0.0, False, False)

    except ImportError as e:
        print("[SENSOR] dht module not available:", e)
        return (0.0, 0.0, False, False)
    except Exception as e:
        print("[SENSOR] DHT22 error:", e)
        return (0.0, 0.0, False, False)


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
        # ATTENUATION FIX: Complies with modern MicroPython releases
        adc.atten(machine.ADC.ATTN_11DB)  # 0–3.3 V range

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
    """Read all air-node sensors and return a dict (blocking version).

    Use read_all_async() instead when running inside an asyncio event loop.

    Args:
        config: Module or dict with keys:
            PIN_DHT, PIN_BAT_ADC, BAT_DIVIDER_RATIO.

    Returns:
        dict with keys:
            air_temp_c, air_humidity_pct, air_temp_valid,
            air_humidity_valid, battery_v
    """
    gc.collect()

    result = {
        "air_temp_c": 0.0,
        "air_humidity_pct": 0.0,
        "air_temp_valid": False,
        "air_humidity_valid": False,
        "battery_v": -1.0,
    }

    # DHT22 temperature + humidity
    pin_dht = config.PIN_DHT if hasattr(config, "PIN_DHT") else config["PIN_DHT"]
    temp_c, humidity_pct, temp_valid, humidity_valid = read_dht22(pin_dht)
    result["air_temp_c"] = temp_c
    result["air_humidity_pct"] = humidity_pct
    result["air_temp_valid"] = temp_valid
    result["air_humidity_valid"] = humidity_valid

    # Battery voltage
    pin_bat = (
        config.PIN_BAT_ADC if hasattr(config, "PIN_BAT_ADC") else config["PIN_BAT_ADC"]
    )
    ratio = (
        config.BAT_DIVIDER_RATIO
        if hasattr(config, "BAT_DIVIDER_RATIO")
        else config["BAT_DIVIDER_RATIO"]
    )
    result["battery_v"] = read_battery(pin_bat, ratio)

    gc.collect()
    return result


async def read_all_async(config):
    """Read all air-node sensors and return a dict (async version).

    Uses async DHT22 reading so the event loop is not blocked during
    the 2-second stabilisation wait. Battery ADC read is fast enough
    to be synchronous.

    Args:
        config: Module or dict with keys:
            PIN_DHT, PIN_BAT_ADC, BAT_DIVIDER_RATIO.

    Returns:
        dict with keys:
            air_temp_c, air_humidity_pct, air_temp_valid,
            air_humidity_valid, battery_v
    """
    gc.collect()

    result = {
        "air_temp_c": 0.0,
        "air_humidity_pct": 0.0,
        "air_temp_valid": False,
        "air_humidity_valid": False,
        "battery_v": -1.0,
    }

    # DHT22 temperature + humidity (async — yields during stabilisation)
    pin_dht = config.PIN_DHT if hasattr(config, "PIN_DHT") else config["PIN_DHT"]
    temp_c, humidity_pct, temp_valid, humidity_valid = await read_dht22_async(pin_dht)
    result["air_temp_c"] = temp_c
    result["air_humidity_pct"] = humidity_pct
    result["air_temp_valid"] = temp_valid
    result["air_humidity_valid"] = humidity_valid

    # Battery voltage (synchronous — ADC read is fast)
    pin_bat = (
        config.PIN_BAT_ADC if hasattr(config, "PIN_BAT_ADC") else config["PIN_BAT_ADC"]
    )
    ratio = (
        config.BAT_DIVIDER_RATIO
        if hasattr(config, "BAT_DIVIDER_RATIO")
        else config["BAT_DIVIDER_RATIO"]
    )
    result["battery_v"] = read_battery(pin_bat, ratio)

    gc.collect()
    return result
