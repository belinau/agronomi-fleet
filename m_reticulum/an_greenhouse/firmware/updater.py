"""µReticulum — Firmware Over-The-Air Update Receiver

Receives firmware files over direct RNS Link (no LXMF) and stages them
for application on the next boot.  Designed for ESP32-C6 MicroPython
with constrained heap.

Wire protocol (all messages flow over a single Link established by the
hub to the node's farm.node destination — see main.py's
_on_link_established / _on_link_packet):

  Packet (link.send / link.set_packet_callback):
    cmd: "manifest_query"   — node responds with /update/+/ file hashes
    cmd: "update_begin"     — fields: {version, file_count, manifest}
    cmd: "update_commit"    — node verifies manifest, writes reboot
                              marker, resets

  Resource (RNS.Resource per chunk, ≤ CHUNK_SIZE bytes plaintext):
    cmd: "update_file"      — fields: {filename, data, sha256,
                              chunk_index, total_chunks, ...}

Each large file is sent as multiple chunked Resources of the same
filename; receive_part appends, the final chunk verifies the
whole-file SHA-256 from update_begin's manifest, and commit verifies
the entire manifest (including files the hub skipped because their
hashes already matched on this node).

Recovery (boot.py):
  1. Apply: backups of every file we're about to overwrite go to
     /backup_<name>.py BEFORE the unconfirmed marker is set, before
     the rename, so a power loss at any point leaves a recoverable
     state.
  2. Confirm: main.py calls confirm_running_firmware() once it's
     reached steady-state listen; that clears /update/.unconfirmed,
     /update/.boot_count, and removes the /backup_*.py copies.
  3. Rollback: if N=3 boots elapse without confirm_running_firmware
     being called (broken firmware), boot.py restores the backups
     over the live files and resets.
"""

import gc

import uhashlib
import uos

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UPDATE_DIR = "/update"
_REBOOT_MARKER = _UPDATE_DIR + "/.reboot_needed"
_UNCONFIRMED_MARKER = _UPDATE_DIR + "/.unconfirmed"
_BOOT_COUNT_FILE = _UPDATE_DIR + "/.boot_count"
_VERSION_MARKER = _UPDATE_DIR + "/.version"
_BACKUP_PREFIX = "backup_"
_HASH_CHUNK = 512  # streaming-hash read size for compute_file_manifest

# Running SHA-256 per filename while chunked transfers are in progress.
# Keyed by filename; created on chunk_index=0, finalized + deleted on the
# final chunk.  Off-the-shelf uhashlib.sha256 supports .update() so we
# never need to hold the whole reconstructed file in RAM.
_chunk_hashers = {}

# ---------------------------------------------------------------------------
# Sequential transfer state
# ---------------------------------------------------------------------------
# Reset by handle_update_begin().  Tracks expected vs received file count
# so main.py can decide whether to reboot or keep listening.
_transfer_state = {
    "active": False,  # True after update_begin received
    "version": None,  # Firmware version from update_begin
    "file_count": 0,  # Expected number of update_file messages (only sent files, not skipped)
    "received": 0,  # Number of successfully written files this transfer
    "failed": 0,  # Number of failed file writes
    "manifest": {},  # Full {filename: sha256_hex} the hub expects after this push
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_update_dir():
    """Create /update/ if it doesn't already exist."""
    try:
        uos.stat(_UPDATE_DIR)
    except OSError:
        uos.mkdir(_UPDATE_DIR)


def _sha256(data):
    """Return hex-encoded SHA-256 digest of *data* (bytes).

    uhashlib.sha256 only provides digest() returning raw bytes —
    hexdigest() does not exist in MicroPython.
    """
    h = uhashlib.sha256()
    h.update(data)
    return h.digest().hex()


def _sha256_file(path):
    """Stream-hash a file in fixed-size chunks so we never hold the whole
    file in RAM at once.  Returns hex digest or None if the file doesn't
    exist or can't be read.
    """
    try:
        h = uhashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_HASH_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        return h.digest().hex()
    except OSError:
        return None


def compute_file_manifest(filenames):
    """Return {filename: sha256_hex} for every requested filename that
    exists in /update/ (preferred — that's the staged version) OR in /
    (the currently-running version).  Files not present at either path
    are omitted from the returned dict.

    Called over RNS by the hub before it starts pushing, so the hub
    can skip files whose hashes already match its expected versions
    (saves bandwidth on incremental dev cycles and on retries of a
    previously-partially-staged push).
    """
    _ensure_update_dir()
    result = {}
    for name in filenames:
        # Prefer the staged copy if one exists from a previous attempt
        h = _sha256_file(_UPDATE_DIR + "/" + name)
        if h is None:
            # Fall back to the currently-running version
            h = _sha256_file("/" + name)
        if h is not None:
            result[name] = h
    return result


def _write_update_file(filename, data, mode="wb"):
    """Write *data* bytes to /update/<filename>, creating the directory if needed.

    *mode* is "wb" (truncate, default) for the first chunk of a file
    or for unchunked transfers, and "ab" (append) for subsequent chunks.

    On ESP32 MicroPython the VFS layer caches writes in RAM.  To survive
    power loss the sequence must be:
      1. open() → write() → close()   (close flushes the file buffer)
      2. uos.sync()                    (flushes the VFS block cache to flash)
    Calling f.flush() or uos.sync() *before* close is pointless — the
    VFS may not have received all data yet.

    Returns True on success, False on failure.
    """
    _ensure_update_dir()
    path = _UPDATE_DIR + "/" + filename
    try:
        # Ensure parent dirs under /update exist (e.g. /update/lib/foo.py)
        # MicroPython uos.mkdir doesn't support exist_ok, so walk each part.
        parts = filename.split("/")
        if len(parts) > 1:
            partial = _UPDATE_DIR
            for part in parts[:-1]:
                partial = partial + "/" + part
                try:
                    uos.stat(partial)
                except OSError:
                    uos.mkdir(partial)
        with open(path, mode) as f:
            f.write(data)
        # File is now closed.  Sync the entire VFS to flash so the
        # write survives power loss or machine.reset().
        uos.sync()
        return True
    except Exception as e:
        print("[updater] write error " + path + ": " + str(e))
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_staged_files():
    """Return a list of .py files currently staged in /update/.

    Used by main.py to verify that files actually arrived before
    rebooting on update_commit.  Over LoRa, the commit can arrive
    before Resource-based file transfers finish.
    """
    try:
        entries = uos.listdir(_UPDATE_DIR)
    except OSError:
        return []
    return [e for e in entries if e.endswith(".py")]


def transfer_progress():
    """Return the current sequential transfer progress as a dict.

    Keys:
      active     — bool, True if an update_begin has been received
      version    — firmware version string (or None)
      file_count — expected number of files
      received   — number of files successfully written
      failed     — number of files that failed to write
      complete   — bool, True if all expected files have been received
    """
    ts = _transfer_state
    return {
        "active": ts["active"],
        "version": ts["version"],
        "file_count": ts["file_count"],
        "received": ts["received"],
        "failed": ts["failed"],
        "complete": ts["active"]
        and ts["received"] >= ts["file_count"]
        and ts["file_count"] > 0,
    }


def handle_update_begin(fields):
    """Process an LXMF update_begin command and return a response dict.

    The hub sends this before streaming individual update_file messages
    over the same long-lived Link.  It announces the firmware version
    and how many files to expect, allowing the node to prepare state
    and validate completeness before committing.

    Expected fields:
      cmd        - "update_begin"
      version    - firmware version string
      dev_id     - device identifier
      file_count - number of update_file messages to follow

    Returns a dict suitable for use as LXMF fields in an ACK.
    """
    global _transfer_state

    version = fields.get("version", "unknown")
    file_count = fields.get("file_count", 0)
    manifest = fields.get("manifest", {}) or {}
    dev_id = fields.get("dev_id", "")

    _ensure_update_dir()

    # Resume support: only wipe /update/ if the new push is for a
    # DIFFERENT version.  If the hub is retrying the same version, we
    # keep staged files so the manifest_query step can let the hub skip
    # files that have already arrived cleanly.
    try:
        with open(_VERSION_MARKER, "r") as f:
            staged_version = f.read().strip()
    except OSError:
        staged_version = None

    if staged_version != version:
        try:
            entries = uos.listdir(_UPDATE_DIR)
            for entry in entries:
                if entry.endswith(".py"):
                    try:
                        uos.remove(_UPDATE_DIR + "/" + entry)
                    except OSError:
                        pass
        except OSError:
            pass
        try:
            with open(_VERSION_MARKER, "w") as f:
                f.write(version)
        except OSError:
            pass

    # Reset transient state, but remember the full expected manifest so
    # commit can verify the assembled /update/ tree against what the hub
    # actually promised (including files the hub skipped because their
    # hash already matched on the receiver).
    _transfer_state = {
        "active": True,
        "version": version,
        "file_count": file_count,
        "received": 0,
        "failed": 0,
        "manifest": manifest,
    }

    # Also remove stale reboot marker
    try:
        uos.remove(_REBOOT_MARKER)
    except OSError:
        pass

    gc.collect()

    print(
        "[updater] update_begin: version="
        + str(version)
        + " file_count="
        + str(file_count)
        + " dev_id="
        + str(dev_id)
    )

    return {
        "ack": True,
        "cmd": "update_begin",
        "version": version,
        "file_count": file_count,
        "status": "ok",
    }


def handle_update(fields):
    """Process an LXMF update command and return a response dict.

    Handles both update_file and update_commit messages that are part
    of a sequential firmware push.  When _transfer_state is active,
    tracks received/failed file counts so the caller can verify
    completeness before rebooting.

    Expected fields for update_file:
      cmd      — "update_file"
      filename — target filename
      data     — file content as bytes
      sha256   — hex SHA-256 of data
      index    — 0-based file index in the transfer sequence
      total    — total number of files in the transfer

    Expected fields for update_commit:
      cmd      — "update_commit"
      version  — firmware version string
      dev_id   — device identifier
      reboot   — bool, True to reboot after committing

    Returns a dict suitable for use as LXMF fields in an ACK.
    """
    cmd = fields.get("cmd", "")
    cmd_id = fields.get("cmd_id", 0)

    if cmd == "update_file":
        filename = fields.get("filename", "")
        data = fields.get("data")
        expected_hash = fields.get("sha256", "")
        # Chunked transfer fields.  Defaults make a non-chunked sender
        # (chunk_index missing, total_chunks missing or 1) behave exactly
        # as the original single-shot protocol: write whole file, verify
        # full hash, mark received.
        chunk_index = fields.get("chunk_index", 0)
        total_chunks = fields.get("total_chunks", 1)

        if not filename:
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "missing_filename",
            }

        if data is None:
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "missing_data",
            }

        if not expected_hash:
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "missing_hash",
            }

        # Start or continue the running SHA-256 across chunks of this file.
        if chunk_index == 0:
            _chunk_hashers[filename] = uhashlib.sha256()
        elif filename not in _chunk_hashers:
            # chunk_index>0 but we never saw chunk 0 — out of order.
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "out_of_order_chunk",
                "filename": filename,
            }

        _chunk_hashers[filename].update(data)

        # First chunk truncates the file; later chunks append.
        write_mode = "wb" if chunk_index == 0 else "ab"
        if not _write_update_file(filename, data, write_mode):
            _chunk_hashers.pop(filename, None)
            if _transfer_state["active"]:
                _transfer_state["failed"] += 1
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "write_failed",
                "filename": filename,
            }

        gc.collect()

        is_last_chunk = chunk_index >= total_chunks - 1
        if not is_last_chunk:
            # Intermediate chunk — no hash check yet, no received++ yet.
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "ok",
                "filename": filename,
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "index": fields.get("index", 0),
                "total": fields.get("total", 0),
            }

        # Final chunk: finalize the running hash and verify against the
        # whole-file expected hash from the sender.
        actual_hash = _chunk_hashers[filename].digest().hex()
        del _chunk_hashers[filename]

        if actual_hash != expected_hash:
            # The assembled file is wrong — delete the staged copy so
            # check_pending_update() never moves a corrupted file
            # over the running firmware.
            try:
                uos.remove(_UPDATE_DIR + "/" + filename)
            except OSError:
                pass
            if _transfer_state["active"]:
                _transfer_state["failed"] += 1
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "hash_mismatch",
                "filename": filename,
            }

        if _transfer_state["active"]:
            _transfer_state["received"] += 1
        return {
            "ack": True,
            "cmd_id": cmd_id,
            "cmd": cmd,
            "status": "ok",
            "filename": filename,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "index": fields.get("index", 0),
            "total": fields.get("total", 0),
        }

    elif cmd == "update_commit":
        # Signal that all files have been transferred and the node
        # should apply them on next boot.
        #
        # Verification gate at commit time:
        #   If the hub passed an expected_manifest in update_begin
        #   (current protocol), verify that every file in /update/
        #   has the expected hash — that covers BOTH files the hub
        #   sent during this push AND files the hub skipped because
        #   their hash already matched.  A mismatch means either the
        #   staged file is corrupt or the file the hub skipped on
        #   actually wasn't intact — either way, refuse to commit.
        #
        #   If the manifest is absent (old hub), fall back to the
        #   received-count check from the original protocol.
        ts = _transfer_state
        manifest = ts.get("manifest", {}) if ts["active"] else {}

        if manifest:
            missing = []
            mismatched = []
            for fname, expected in manifest.items():
                # Mirror compute_file_manifest's lookup order: prefer the
                # staged copy (just received this push) but fall back to
                # the running copy in / (the hub deliberately skipped
                # pushing this file because its hash already matched).
                # After commit, boot.py only moves files in /update/ to /,
                # so a skipped file legitimately stays at /<file> for the
                # next boot — verification must accept either location.
                actual = _sha256_file(_UPDATE_DIR + "/" + fname)
                if actual is None:
                    actual = _sha256_file("/" + fname)
                if actual is None:
                    missing.append(fname)
                elif actual != expected:
                    mismatched.append(fname)
            if missing or mismatched:
                print(
                    "[updater] commit verification failed: "
                    + "missing=" + str(missing)
                    + " mismatched=" + str(mismatched)
                )
                return {
                    "ack": True,
                    "cmd_id": cmd_id,
                    "cmd": cmd,
                    "status": "error",
                    "error": "manifest_verification_failed",
                    "missing": missing,
                    "mismatched": mismatched,
                }
        elif ts["active"] and ts["file_count"] > 0:
            expected = ts["file_count"]
            actual = ts["received"]
            if actual < expected:
                print(
                    "[updater] commit with incomplete transfer: "
                    + str(actual)
                    + "/"
                    + str(expected)
                    + " files received"
                )
                return {
                    "ack": True,
                    "cmd_id": cmd_id,
                    "cmd": cmd,
                    "status": "error",
                    "error": "incomplete",
                    "received": actual,
                    "expected": expected,
                }

        # Deactivate transfer state — transfer is done
        _transfer_state["active"] = False

        # Clear the resume-version marker — this version has been
        # successfully assembled; the next push starts fresh.
        try:
            uos.remove(_VERSION_MARKER)
        except OSError:
            pass

        _ensure_update_dir()
        try:
            with open(_REBOOT_MARKER, "w") as f:
                f.write("1")
            # Close happened above.  Sync VFS to flash so marker survives reset.
            uos.sync()
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "ok",
            }
        except Exception as e:
            print("[updater] commit marker error: " + str(e))
            return {
                "ack": True,
                "cmd_id": cmd_id,
                "cmd": cmd,
                "status": "error",
                "error": "commit_failed",
            }

    else:
        return {
            "ack": True,
            "cmd_id": cmd_id,
            "cmd": cmd,
            "status": "error",
            "error": "unknown_command",
        }


def _move_update_files(src_dir, dst_dir):
    """Recursively move all .py files from src_dir tree into dst_dir tree.

    Returns (moved, failed) counts.
    """
    moved = 0
    failed = 0
    try:
        entries = uos.listdir(src_dir)
    except OSError:
        return moved, failed

    for entry in entries:
        if entry == ".reboot_needed":
            continue

        src = src_dir + "/" + entry
        dst = dst_dir + "/" + entry

        # Check if it's a directory by attempting stat and checking mode
        try:
            st = uos.stat(src)
            is_dir = (st[0] & 0x4000) != 0
        except OSError:
            continue

        if is_dir:
            # Ensure destination dir exists
            try:
                uos.stat(dst)
            except OSError:
                try:
                    uos.mkdir(dst)
                except OSError as e:
                    print("[updater] mkdir failed " + dst + ": " + str(e))
                    failed += 1
                    continue
            sub_moved, sub_failed = _move_update_files(src, dst)
            moved += sub_moved
            failed += sub_failed
        else:
            if not entry.endswith(".py"):
                continue
            try:
                uos.rename(src, dst)
                moved += 1
                print("[updater] " + src + " -> " + dst)
            except Exception as e:
                failed += 1
                print("[updater] FAILED to move " + src + ": " + str(e))

    return moved, failed


def _backup_current_files(src_dir, dst_dir):
    """Save current copies of files we're about to overwrite.

    For every file in /update/ that has a counterpart in /, rename the
    current / version to /backup_<name>.  If a /backup_<name> already
    exists from a previous failed update, remove it first.  Renames are
    atomic at the VFS layer so we never lose data — at worst, after a
    power loss the file lives under /backup_<name> instead of /<name>
    and boot.py's rollback restores it.
    """
    try:
        entries = uos.listdir(src_dir)
    except OSError:
        return
    for entry in entries:
        if entry.startswith(".") or not entry.endswith(".py"):
            continue
        src = src_dir + "/" + entry
        try:
            st = uos.stat(src)
            if (st[0] & 0x4000) != 0:  # directory — skip
                continue
        except OSError:
            continue
        current = dst_dir + "/" + entry
        backup = dst_dir + "/" + _BACKUP_PREFIX + entry
        try:
            uos.stat(current)
        except OSError:
            continue  # nothing to back up
        try:
            uos.remove(backup)
        except OSError:
            pass
        try:
            uos.rename(current, backup)
        except OSError as e:
            print("[updater] backup failed for " + current + ": " + str(e))


def confirm_running_firmware():
    """Mark the currently running firmware as confirmed-good.

    Call this from main.py once the node has reached a steady-state
    operational point (link established, telemetry sent, etc.).  Until
    this is called after a fresh update, boot.py increments the
    unconfirmed-boot counter on every restart and rolls back to the
    previous backup once the counter exceeds the threshold.

    Safe to call repeatedly — it's a no-op if there's no unconfirmed
    marker.
    """
    cleaned = False
    try:
        uos.remove(_UNCONFIRMED_MARKER)
        cleaned = True
    except OSError:
        pass
    try:
        uos.remove(_BOOT_COUNT_FILE)
    except OSError:
        pass

    # Clean up backup_*.py files from the previous firmware.  Keeps
    # the filesystem from accumulating stale copies.
    try:
        entries = uos.listdir("/")
    except OSError:
        entries = []
    for entry in entries:
        if entry.startswith(_BACKUP_PREFIX) and entry.endswith(".py"):
            try:
                uos.remove("/" + entry)
            except OSError:
                pass

    if cleaned:
        try:
            uos.sync()
        except Exception:
            pass
        print("[updater] Firmware confirmed; backups cleared")


def check_pending_update():
    """Check if a firmware update is pending and apply it.

    Called from boot.py on every boot.  Flow when /update/.reboot_needed
    exists:
      1. Save backups of every file we're about to overwrite
         (/<file> → /backup_<file>).
      2. Set /update/.unconfirmed marker before any new file lands —
         that way an inopportune power loss still triggers rollback on
         next boot.
      3. Move /update/*.py to / (overwriting).
      4. Remove .reboot_needed (keeps .unconfirmed and backups).
      5. machine.reset() to load the new firmware.

    The new firmware must call confirm_running_firmware() once it's
    happy.  Until then, boot.py's rollback counter keeps incrementing
    on every restart, and after _MAX_UNCONFIRMED_BOOTS it restores the
    backups and resets.
    """
    try:
        uos.stat(_REBOOT_MARKER)
    except OSError:
        return

    import machine

    print("[updater] Pending update found — applying staged files")

    # Step 1: back up every file we're about to overwrite, BEFORE the
    # unconfirmed marker so a power loss right now still leaves us in
    # a recoverable state (either old files still in place, or backups
    # present with no marker → next boot just continues with old).
    _backup_current_files(_UPDATE_DIR, "")

    # Step 2: place the unconfirmed marker BEFORE the new files land.
    # If we crash mid-apply, boot.py will roll back the partial state.
    try:
        with open(_UNCONFIRMED_MARKER, "w") as f:
            f.write("1")
        uos.sync()
    except OSError as e:
        print("[updater] failed to write unconfirmed marker: " + str(e))

    # Step 3: move new files into place
    moved, failed = _move_update_files(_UPDATE_DIR, "")

    # Step 4: clear the original reboot marker (we keep .unconfirmed)
    try:
        uos.remove(_REBOOT_MARKER)
    except OSError:
        pass

    uos.sync()

    if moved > 0:
        print(
            "[updater] Update applied: "
            + str(moved)
            + " files moved, "
            + str(failed)
            + " failed (unconfirmed — main.py must call confirm_running_firmware)"
        )
        machine.reset()
    else:
        print("[updater] No files moved — continuing with old firmware")
        # Nothing actually changed; drop the unconfirmed marker.
        try:
            uos.remove(_UNCONFIRMED_MARKER)
        except OSError:
            pass
