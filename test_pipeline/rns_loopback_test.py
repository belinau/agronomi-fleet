#!/usr/bin/env python3
"""
rns_loopback_test.py — Self-contained RNS loopback test

Tests that RNS packet delivery works correctly in the current environment.
Creates an IN SINGLE destination and an OUT SINGLE destination in the same
process, sends a packet to itself, and verifies the on_packet callback fires.

This is the FIRST test to run when debugging RNS connectivity issues.
If this test fails, the problem is with RNS itself (shared instance routing,
destination registration, or packet delivery) — NOT with your application code.

If this test passes but your application doesn't receive packets, the problem
is in your application's destination configuration or identity management.

Usage:
    python rns_loopback_test.py [--config /path/to/reticulum/config]

Exit codes:
    0 — Test passed (packet received by callback)
    1 — Test failed (packet NOT received within timeout)
    2 — Setup error
"""

import argparse
import sys
import threading
import time

import RNS

APP_NAME = "farm"
ASPECT = "loopback_test"
APP_DATA = b"AgroNomi Loopback Test v1.0"

# Global state
received_event = threading.Event()
received_data = None
received_packet = None


def on_packet(data: bytes, packet: RNS.Packet):
    """Callback for the IN destination — fires when a packet is received."""
    global received_data, received_packet
    received_data = data
    received_packet = packet
    RNS.log(f"[LOOPBACK] on_packet fired! {len(data)} bytes received", RNS.LOG_INFO)
    RNS.log(f"[LOOPBACK] Data: {data!r}", RNS.LOG_INFO)
    RNS.log(
        f"[LOOPBACK] Packet dest hash: {RNS.prettyhexrep(packet.destination_hash)}",
        RNS.LOG_INFO,
    )
    RNS.log(f"[LOOPBACK] Packet type: {packet.packet_type}", RNS.LOG_INFO)
    received_event.set()


def delivery_confirmed(receipt):
    """Callback for delivery proof from the OUT side."""
    RNS.log(
        f"[LOOPBACK] Delivery confirmed! RTT={receipt.get_rtt():.3f}s", RNS.LOG_INFO
    )


def delivery_timeout(receipt):
    """Callback for delivery timeout from the OUT side."""
    RNS.log("[LOOPBACK] Delivery TIMED OUT — no proof received", RNS.LOG_WARNING)


def main():
    parser = argparse.ArgumentParser(description="RNS loopback self-test")
    parser.add_argument(
        "--config",
        default=None,
        help="path to alternative Reticulum config directory",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="seconds to wait for packet delivery (default: 30)",
    )
    args = parser.parse_args()

    RNS.loglevel = RNS.LOG_INFO
    RNS.log("=" * 60)
    RNS.log("RNS Loopback Self-Test")
    RNS.log("=" * 60)

    # ── 1. Initialize Reticulum ──────────────────────────────────
    RNS.log("[1] Initializing Reticulum...")
    reticulum = RNS.Reticulum(args.config)

    if reticulum.is_connected_to_shared_instance:
        RNS.log(
            "[1] Connected to shared instance (MeshChat/Sideband/rnsd).",
            RNS.LOG_INFO,
        )
        RNS.log(
            "[1] If loopback fails, set share_instance=No in config and retry.",
            RNS.LOG_WARNING,
        )
    else:
        RNS.log("[1] Running as standalone instance.", RNS.LOG_INFO)

    # ── 2. Create IN SINGLE destination ──────────────────────────
    RNS.log("[2] Creating IN SINGLE destination...")
    identity = RNS.Identity()
    in_dest = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        APP_NAME,
        ASPECT,
    )
    # PROVE_ALL: send delivery proofs so we can confirm packets arrive
    in_dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
    in_dest.set_packet_callback(on_packet)

    RNS.log(f"[2] IN destination hash: {RNS.prettyhexrep(in_dest.hash)}")

    # Verify destination is registered in Transport
    if in_dest.hash in RNS.Transport.destinations_map:
        RNS.log(
            "[2] ✓ Destination registered in Transport.destinations_map", RNS.LOG_INFO
        )
    else:
        RNS.log(
            "[2] ✗ Destination NOT in Transport.destinations_map — packets will NOT be delivered!",
            RNS.LOG_CRITICAL,
        )
        sys.exit(2)

    # ── 3. Announce the IN destination ───────────────────────────
    RNS.log("[3] Announcing IN destination...")
    in_dest.announce(app_data=APP_DATA)
    RNS.log("[3] Announce sent.")

    # Give the announce time to propagate (even locally)
    time.sleep(2)

    # ── 4. Create OUT SINGLE destination ──────────────────────────
    RNS.log("[4] Creating OUT SINGLE destination using the same identity...")
    out_dest = RNS.Destination(
        identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        APP_NAME,
        ASPECT,
    )
    out_dest.set_proof_strategy(RNS.Destination.PROVE_ALL)

    RNS.log(f"[4] OUT destination hash: {RNS.prettyhexrep(out_dest.hash)}")

    # Verify both hashes match (they should for the same identity+aspects)
    if in_dest.hash == out_dest.hash:
        RNS.log("[4] ✓ IN and OUT destination hashes match", RNS.LOG_INFO)
    else:
        RNS.log(
            f"[4] ✗ Hash mismatch! IN={RNS.prettyhexrep(in_dest.hash)} OUT={RNS.prettyhexrep(out_dest.hash)}",
            RNS.LOG_CRITICAL,
        )
        sys.exit(2)

    # ── 5. Check path to the destination ─────────────────────────
    RNS.log("[5] Checking path to destination...")
    if RNS.Transport.has_path(in_dest.hash):
        RNS.log("[5] ✓ Path to destination exists", RNS.LOG_INFO)
    else:
        RNS.log("[5] No path yet — requesting path...", RNS.LOG_WARNING)
        RNS.Transport.request_path(in_dest.hash)
        time.sleep(2)
        if RNS.Transport.has_path(in_dest.hash):
            RNS.log("[5] ✓ Path established after request", RNS.LOG_INFO)
        else:
            RNS.log(
                "[5] ⚠ No path after request (may still work for local delivery)",
                RNS.LOG_WARNING,
            )

    # ── 6. Send a test packet ────────────────────────────────────
    test_payload = b"Hello from AgroNomi loopback test!"
    RNS.log(f"[6] Sending test packet ({len(test_payload)} bytes)...")
    RNS.log(f"[6] Payload: {test_payload!r}")

    packet = RNS.Packet(out_dest, test_payload)
    receipt = packet.send()

    if receipt is None:
        RNS.log("[6] ✗ Packet send returned None!", RNS.LOG_CRITICAL)
        sys.exit(2)

    RNS.log(f"[6] Packet sent, receipt hash: {RNS.prettyhexrep(receipt.hash)}")

    # Set up delivery confirmation callbacks
    receipt.set_delivery_callback(delivery_confirmed)
    receipt.set_timeout(args.timeout)
    receipt.set_timeout_callback(delivery_timeout)

    # ── 7. Wait for packet receipt ────────────────────────────────
    RNS.log(f"[7] Waiting up to {args.timeout}s for packet receipt...")
    received = received_event.wait(timeout=args.timeout)

    if received:
        RNS.log("=" * 60, RNS.LOG_INFO)
        RNS.log("✓ TEST PASSED — on_packet callback fired!", RNS.LOG_INFO)
        RNS.log(
            f"  Received {len(received_data)} bytes: {received_data!r}", RNS.LOG_INFO
        )
        RNS.log(f"  Payload match: {received_data == test_payload}", RNS.LOG_INFO)
        RNS.log("=" * 60, RNS.LOG_INFO)
        sys.exit(0)
    else:
        RNS.log("=" * 60, RNS.LOG_CRITICAL)
        RNS.log("✗ TEST FAILED — on_packet callback never fired!", RNS.LOG_CRITICAL)
        RNS.log("", RNS.LOG_CRITICAL)
        RNS.log(
            "This means RNS packet delivery is NOT working in this environment.",
            RNS.LOG_CRITICAL,
        )
        RNS.log("", RNS.LOG_CRITICAL)
        RNS.log("Possible causes:", RNS.LOG_CRITICAL)
        RNS.log(
            "  1. Connected to a shared instance that isn't routing packets",
            RNS.LOG_CRITICAL,
        )
        RNS.log(
            "     → Fix: set share_instance=No in ~/.reticulum/config", RNS.LOG_CRITICAL
        )
        RNS.log("  2. RNode interface is down or misconfigured", RNS.LOG_CRITICAL)
        RNS.log(
            "     → Fix: run 'rnstatus' to check interface status", RNS.LOG_CRITICAL
        )
        RNS.log(
            "  3. Destination not registered in Transport.destinations_map",
            RNS.LOG_CRITICAL,
        )
        RNS.log(
            "     → Fix: check that RNS.Reticulum() initialized correctly",
            RNS.LOG_CRITICAL,
        )
        RNS.log("", RNS.LOG_CRITICAL)
        RNS.log(
            "Try running with RNS.loglevel=RNS.LOG_EXTREME for detailed logging.",
            RNS.LOG_CRITICAL,
        )
        RNS.log(
            "Or run with --config pointing to a standalone config (share_instance=No).",
            RNS.LOG_CRITICAL,
        )
        RNS.log("=" * 60, RNS.LOG_CRITICAL)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("")
        sys.exit(2)
