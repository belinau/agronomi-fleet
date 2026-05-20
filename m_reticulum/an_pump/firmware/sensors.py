"""µReticulum — Pump Actuator Node Sensors (AN-PUMP-01)

Minimal sensor drivers for the pump node.  Pump nodes have no environmental
sensors — the only "sensor" reading is the battery voltage (if the board
has a voltage divider) and the current pump relay state.
"""

import gc

import machine

# ---------------------------------------------------------------------------
# Battery voltage — ADC with resistive divider (optional)
# ---------------------------------------------------------------------------


def read_battery(pin, divider_ratio=2.0, samples=4):
    """Read battery voltage through a resistive divider.

    Args:
        pin:            GPIO number for the ADC input.
        divider_ratio:  V_bat / V_adc (default 2.0 for equal resistors).
        samples:        Number of ADC samples to average.

    Returns:
        float: Battery voltage in volts, or -1.0 on error / not available.
    """
    try:
        import time

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
    """Read available sensors and return a dict.

    For pump nodes, this is just battery voltage (if HAS_BAT_RESISTORS
    is enabled) and a placeholder for actuator state (pump_on is tracked
    in main.py, not here).

    Args:
        config: Module or dict with keys:
            HAS_BAT_RESISTORS, PIN_BAT_ADC, BAT_DIVIDER_RATIO

    Returns:
        dict with key: battery_v (float, -1.0 if unavailable)
    """
    gc.collect()

    result = {"battery_v": -1.0}

    has_bat = (
        config.HAS_BAT_RESISTORS
        if hasattr(config, "HAS_BAT_RESISTORS")
        else config.get("HAS_BAT_RESISTORS", False)
    )

    if has_bat:
        pin = (
            config.PIN_BAT_ADC
            if hasattr(config, "PIN_BAT_ADC")
            else config["PIN_BAT_ADC"]
        )
        ratio = (
            config.BAT_DIVIDER_RATIO
            if hasattr(config, "BAT_DIVIDER_RATIO")
            else config["BAT_DIVIDER_RATIO"]
        )
        result["battery_v"] = read_battery(pin, ratio)

    gc.collect()
    return result
