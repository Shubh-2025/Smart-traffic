#!/usr/bin/env python3
"""
main.py — Smart Traffic System
================================
Phones connect by opening http://<PC-IP>:8000 in their browser.
No Termux. No apps. Just a browser URL.

Usage:
    python main.py              ← normal run
    python main.py --simulate   ← test without phones (fake siren audio)
"""

import argparse
import os
import random
import threading
import time

import cv2

os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
os.environ["TF_CPP_MIN_LOG_LEVEL"]  = "3"

from config import ALL_IMAGES
from audio.ws_server import (
    start_ws_server,
    broadcast_siren_to_managers,
    broadcast_decision_to_managers,
    get_audio_chunk_nowait,
    get_connected_phone_ids,
    _phone_sides,
    _side_to_phone,
)
from audio.listeners    import SideListener
from audio.simulator    import SirenSimulator
from traffic.detector   import VehicleDetector
from traffic.controller import SignalController
from utils.yamnet       import load_yamnet
from utils.mqtt_pub     import MQTTPublisher


# ── Image rotation (your original logic, unchanged) ───────────────────────────

def update_images(images: dict, green_side: int) -> dict:
    used      = set(images.values())
    available = list(set(ALL_IMAGES) - used)
    if available:
        images[green_side] = random.choice(available)
    return images


# ── WebSocket → SideListener audio bridge ────────────────────────────────────

def start_audio_bridge(listeners: dict):
    """
    Background thread: pulls raw PCM bytes from the WebSocket phone queues
    and pushes them into the correct SideListener for YAMNet inference.

    Phone browser mic → ws_server.py queue → this bridge → SideListener.feed_audio()
    """
    def _run():
        while True:
            try:
                for phone_id in get_connected_phone_ids():
                    side = _phone_sides.get(phone_id)
                    if side and side in listeners:
                        raw = get_audio_chunk_nowait(phone_id)
                        if raw:
                            listeners[side].feed_audio(raw)
            except Exception as e:
                print(f"[Bridge] Error: {e}")
            time.sleep(0.02)   # 20 ms poll — low CPU, low latency

    threading.Thread(target=_run, daemon=True, name="audio-bridge").start()
    print("[Bridge] Audio bridge started — routing phone audio to YAMNet\n")


# ── Cycle status print ────────────────────────────────────────────────────────

def print_status(counts: dict, decision: dict):
    print("\n  ┌─ CYCLE ──────────────────────────────────────")
    for s in range(1, 5):
        siren = "  🚨 SIREN" if s in decision["siren_sides"] else ""
        print(f"  │ Side {s}: {counts.get(s,0):>2} vehicles  "
              f"score={decision['scores'][s]:>8.2f}{siren}")
    print(f"  ├──────────────────────────────────────────────")
    print(f"  │ ✅ GREEN → Side {decision['open_side']}  ({decision['green_time']}s)")
    if decision["siren_sides"]:
        print(f"  │ 🚨 SIREN OVERRIDE — Sides {decision['siren_sides']}")
    print(f"  └──────────────────────────────────────────────\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(simulate: bool = False):
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. Start WebSocket server
    #    Phones open http://<PC-IP>:8000 → browser streams mic via WebSocket
    #    Dashboard opens http://<PC-IP>:8001
    start_ws_server(base_dir)
    print("[SYSTEM] Waiting for WebSocket server to start ...")
    time.sleep(2)

    # 2. Pick 4 random images from pool (your original logic)
    images = {i + 1: p for i, p in enumerate(random.sample(ALL_IMAGES, 4))}

    # 3. Load YAMNet once — shared across all 4 listeners
    yamnet_model, class_names, siren_indices = load_yamnet()

    # 4. Create one SideListener per road side
    listeners = {}
    for side in range(1, 5):
        l = SideListener(side, yamnet_model, class_names, siren_indices)
        # Wire siren detections to the dashboard
        l.on_siren_detected = broadcast_siren_to_managers
        l.start()
        listeners[side] = l

    # 5. If simulate mode: inject fake audio directly into listeners
    if simulate:
        sim = SirenSimulator(listeners)
        sim.start()
        print("[SIMULATE] Fake audio injected — no phones needed\n")
    else:
        # 5b. Start bridge: WebSocket audio → SideListeners
        start_audio_bridge(listeners)

    # 6. Other modules
    detector   = VehicleDetector()
    controller = SignalController()
    publisher  = MQTTPublisher()

    print("\n🚦 Smart Traffic System Running")
    if not simulate:
        print("   Phones: open http://<PC-IP>:8000 in phone browser")
        print("   Dashboard: open http://localhost:8001 in PC browser")
    print("   Press Ctrl-C to stop\n")

    try:
        while True:
            print("\n─── New Cycle ────────────────────────────────────")

            # Count vehicles on all 4 sides
            counts = {}
            for side, path in images.items():
                counts[side] = detector.count(path)
                print(f"  Side {side}: {counts[side]} vehicles")

            # Publish to ESP32
            publisher.publish_counts(counts)
            for side, l in listeners.items():
                publisher.publish_siren(side, l.siren_score, l.siren_active)

            # Signal decision (vehicle count + siren priority)
            decision = controller.decide(counts, listeners)
            publisher.publish_control(decision)

            # Push decision to dashboard
            phone_scores = {
                _side_to_phone.get(s, f"side_{s}"): sc
                for s, sc in decision["scores"].items()
            }
            broadcast_decision_to_managers({**decision, "scores": phone_scores})

            # Print + visualize
            print_status(counts, decision)
            detector.visualize(images, counts, decision["siren_sides"])

            # Green phase
            time.sleep(decision["green_time"])

            # Rotate image for the side that just had green (your original logic)
            images = update_images(images, decision["open_side"])
            time.sleep(2)

    except KeyboardInterrupt:
        print("\n🛑 Stopping ...")
    finally:
        for l in listeners.values():
            l.stop()
        cv2.destroyAllWindows()
        publisher.disconnect()
        print("[SYSTEM] Stopped cleanly.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Smart Traffic + Ambulance Siren Detection",
    )
    p.add_argument(
        "--simulate", action="store_true",
        help="Run with fake audio — no phones needed (for testing)",
    )
    args = p.parse_args()
    run(simulate=args.simulate)


if __name__ == "__main__":
    main()