"""µReticulum — ESP32-C6 Template Node Sensor Drivers

MicroPython drivers for:
  - Battery voltage divider (configurable pin, default GPIO1)
"""

import time

import machine


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
        print("[SENSOR] Battery ADC error: " + str(e))
        return -1.0


def read_all(config):
    """Read all sensors and return a dict (synchronous).

    Args:
        config: Module or dict with keys:
            PIN_BAT_ADC, BAT_DIVIDER_RATIO.

    Returns:
        dict with key: battery_v
    """
    pin_bat = (
        config.PIN_BAT_ADC if hasattr(config, "PIN_BAT_ADC") else config["PIN_BAT_ADC"]
    )
    ratio = (
        config.BAT_DIVIDER_RATIO
        if hasattr(config, "BAT_DIVIDER_RATIO")
        else config["BAT_DIVIDER_RATIO"]
    )
    return {
        "battery_v": read_battery(pin_bat, ratio),
    }
