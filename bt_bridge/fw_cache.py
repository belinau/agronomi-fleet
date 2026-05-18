"""
fw_cache.py — Firmware cache management for AgroNomi gateway

Manages the local firmware binary cache on the gateway. When the hub
sends a firmware binary via RNS Resource, the gateway saves it to disk
with SHA-256 verification so it can be reused for multiple nodes of the
same device type without re-downloading over LoRa.

Cache directory structure:
    /var/cache/agronomi/ota/
      <fw_version>/
        <device_type>.bin        # e.g. soil_node.bin
        <device_type>.bin.sha256 # hex digest

A binary without a matching .sha256 file is treated as incomplete
and will be discarded.
"""

import hashlib
import os

import RNS

OTA_CACHE_DIR = "/var/cache/agronomi/ota"


def fw_cache_path(fw_version: str, device_type: str) -> tuple:
    """Return (bin_path, sha256_path) for a cached firmware binary.

    Creates the version directory if it doesn't exist.
    """
    base = os.path.join(OTA_CACHE_DIR, fw_version)
    os.makedirs(base, exist_ok=True)
    return (
        os.path.join(base, f"{device_type}.bin"),
        os.path.join(base, f"{device_type}.bin.sha256"),
    )


def verify_cached_firmware(
    fw_version: str, device_type: str, expected_sha256: str
) -> bool:
    """Check if a valid, verified firmware binary is already cached.

    Returns True if the binary exists, has a matching .sha256 sentinel
    file, and the actual SHA-256 of the binary matches expected_sha256.
    Returns False if missing, incomplete, or checksum mismatch.
    """
    bin_path, sha_path = fw_cache_path(fw_version, device_type)

    if not os.path.exists(bin_path):
        return False
    if not os.path.exists(sha_path):
        RNS.log(
            f"[OTA] No .sha256 sentinel for {device_type} {fw_version} — "
            f"treating as incomplete",
            RNS.LOG_WARNING,
        )
        return False

    try:
        with open(sha_path) as f:
            stored = f.read().strip()

        if stored != expected_sha256:
            RNS.log(
                f"[OTA] Cache checksum mismatch for {device_type} {fw_version} "
                f"— re-fetching",
                RNS.LOG_WARNING,
            )
            os.remove(bin_path)
            os.remove(sha_path)
            return False

        # Verify the actual file matches too
        h = hashlib.sha256()
        with open(bin_path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)

        if h.hexdigest() != expected_sha256:
            RNS.log(
                f"[OTA] Cache file corrupt for {device_type} {fw_version} "
                f"— re-fetching",
                RNS.LOG_WARNING,
            )
            os.remove(bin_path)
            os.remove(sha_path)
            return False

        return True

    except Exception as e:
        RNS.log(f"[OTA] Cache verification error: {e}", RNS.LOG_WARNING)
        return False


def save_firmware_to_cache(
    fw_version: str, device_type: str, data: bytes, expected_sha256: str
) -> bool:
    """Write received firmware to cache atomically.

    Verifies the SHA-256 checksum before writing the .sha256 sentinel file.
    Returns False if checksum fails — do not use this binary.
    """
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        RNS.log(
            f"[OTA] Received binary checksum mismatch! "
            f"expected={expected_sha256[:16]}... got={actual[:16]}...",
            RNS.LOG_ERROR,
        )
        return False

    bin_path, sha_path = fw_cache_path(fw_version, device_type)

    # Write binary atomically via temp file
    tmp = bin_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, bin_path)  # atomic on Linux

    with open(sha_path, "w") as f:
        f.write(actual)

    RNS.log(
        f"[OTA] Cached {device_type} {fw_version} "
        f"({len(data)} bytes, sha256={actual[:16]}...)",
        RNS.LOG_INFO,
    )
    return True


def get_cached_firmware(
    fw_version: str, device_type: str, expected_sha256: str
) -> bytes | None:
    """Load a cached firmware binary if it passes verification.

    Returns the binary data if valid, None if not cached or invalid.
    """
    if not verify_cached_firmware(fw_version, device_type, expected_sha256):
        return None

    bin_path, _ = fw_cache_path(fw_version, device_type)
    with open(bin_path, "rb") as f:
        return f.read()
