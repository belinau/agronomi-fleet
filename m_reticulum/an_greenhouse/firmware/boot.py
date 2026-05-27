"""µReticulum — ESP32-C6 Boot Script with OTA Update + Rollback Support

Runs on every boot (including deep-sleep wakeups). Three jobs in order:

1. **Rollback check.**  If `/update/.unconfirmed` exists, the last OTA
   update has not yet been confirmed by a successful `main.py` boot.
   The unconfirmed-boot counter at `/update/.boot_count` is incremented;
   once it crosses `_MAX_UNCONFIRMED_BOOTS`, the saved `/backup_*.py`
   files are restored over the running firmware and the device resets.

2. **Apply pending update.**  If `/update/.reboot_needed` exists,
   `updater.check_pending_update()` saves backups of the current files,
   moves the staged files from `/update/` to `/`, sets the unconfirmed
   marker, and resets.

3. **Run main.py inside a guard.**  Any uncaught exception during
   `import main` (syntax error, import error, top-level crash from
   `asyncio.run(main())`, etc.) is caught and the device is reset — so
   the unconfirmed counter can eventually trigger rollback if the new
   firmware is genuinely broken.

This file intentionally imports only the minimum needed (`gc`, `uos`,
`time`, `machine`) so the rollback path itself is small and reliable.
"""

import gc
import time

import uos

gc.collect()

_UPDATE_DIR = "/update"
_UNCONFIRMED_MARKER = _UPDATE_DIR + "/.unconfirmed"
_BOOT_COUNT_FILE = _UPDATE_DIR + "/.boot_count"
_BACKUP_PREFIX = "backup_"

# How many boots without confirm_running_firmware() being reached
# before we decide the new firmware is broken and restore backups.
_MAX_UNCONFIRMED_BOOTS = 3


def _read_boot_count():
    try:
        with open(_BOOT_COUNT_FILE, "r") as f:
            return int(f.read())
    except (OSError, ValueError):
        return 0


def _write_boot_count(count):
    try:
        with open(_BOOT_COUNT_FILE, "w") as f:
            f.write(str(count))
        uos.sync()
    except OSError as e:
        print("[boot] could not persist boot count: " + str(e))


def _rollback_to_backups():
    """Restore every /backup_<name>.py to /<name>.py.  Returns count restored."""
    try:
        entries = uos.listdir("/")
    except OSError:
        return 0
    restored = 0
    for entry in entries:
        if not entry.startswith(_BACKUP_PREFIX):
            continue
        target = "/" + entry[len(_BACKUP_PREFIX):]
        try:
            uos.remove(target)
        except OSError:
            pass
        try:
            uos.rename("/" + entry, target)
            restored += 1
            print("[boot] rolled back " + target)
        except OSError as e:
            print("[boot] rollback rename failed for " + entry + ": " + str(e))
    return restored


def _handle_unconfirmed_boot():
    """If we're booting unconfirmed firmware, decide between counting or rolling back."""
    try:
        uos.stat(_UNCONFIRMED_MARKER)
    except OSError:
        return  # confirmed (or no update ever applied)

    count = _read_boot_count() + 1
    if count >= _MAX_UNCONFIRMED_BOOTS:
        print(
            "[boot] firmware unconfirmed after "
            + str(count)
            + " boots — rolling back"
        )
        restored = _rollback_to_backups()
        print("[boot] restored " + str(restored) + " files")
        for f in (_UNCONFIRMED_MARKER, _BOOT_COUNT_FILE):
            try:
                uos.remove(f)
            except OSError:
                pass
        try:
            uos.sync()
        except Exception:
            pass

        import machine

        time.sleep_ms(500)
        machine.reset()
    else:
        _write_boot_count(count)
        print(
            "[boot] unconfirmed firmware boot "
            + str(count)
            + "/"
            + str(_MAX_UNCONFIRMED_BOOTS)
        )


# ---------------------------------------------------------------------------
# 0. Safe-mode escape hatch.
#
# Holding the BOOT button (GPIO9 on the ESP32-C6 Super Mini, active LOW
# via internal pull-up) during boot drops us straight to REPL without
# running updater or main.  Use this when Thonny / mpremote / firmware-
# push has put the device into a state where you need direct file
# access — without this, main.py's asyncio loop takes over the REPL
# fast enough that there's no reliable window to break in.
#
# Workflow:
#   1. Hold BOOT.
#   2. Press + release RESET (or unplug + replug USB).
#   3. Release BOOT after the "safe mode" log line appears.
#   4. Thonny / mpremote can now manage the filesystem freely.
# ---------------------------------------------------------------------------
try:
    from machine import Pin

    _boot_pin = Pin(9, Pin.IN, Pin.PULL_UP)
    if _boot_pin.value() == 0:
        # Button held — give the user time to verify on the serial log
        # and confirm intent, then leave control at REPL.
        for _i in range(5):
            print("[boot] BOOT held — entering safe mode, main.py will NOT run")
            time.sleep(0.2)
        # Returning from boot.py without importing main hands control to
        # MicroPython's REPL on the USB-serial-JTAG console.
        raise SystemExit
except SystemExit:
    raise
except Exception as _e:
    # No machine module / no GPIO9 / wrong board — just continue with
    # the normal boot path.  Never let the safe-mode check itself
    # prevent the device from booting.
    print("[boot] safe-mode check skipped: " + str(_e))


# ---------------------------------------------------------------------------
# 1. Rollback check (before any heavy module imports)
# ---------------------------------------------------------------------------
_handle_unconfirmed_boot()

# ---------------------------------------------------------------------------
# 2. Apply pending OTA update if one was staged
# ---------------------------------------------------------------------------
try:
    import updater
    updater.check_pending_update()
except Exception as e:
    print("[boot] update check failed: " + str(e))

gc.collect()

# ---------------------------------------------------------------------------
# 3. Run main inside a guard so any crash resets the device — that way the
#    unconfirmed-boot counter eventually triggers rollback for genuinely
#    broken firmware.  A confirmed firmware that crashes mid-run will keep
#    resetting; that's still preferable to dropping into REPL where the
#    field-deployed node would be unreachable.
# ---------------------------------------------------------------------------
try:
    import main  # main.py runs its own asyncio.run(main()) at module scope
except Exception as e:
    print("[boot] main.py failed: " + str(e))
    try:
        uos.sync()
    except Exception:
        pass
    import machine

    time.sleep_ms(2000)
    machine.reset()
