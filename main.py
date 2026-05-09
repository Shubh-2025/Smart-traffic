#!/usr/bin/env python3
"""
main.py — Smart Traffic System with Ambulance Siren Detection
=============================================================
Entry point. Orchestrates all modules.

Usage:
    python main.py                        # UDP audio from phones (default)
    python main.py --audio-mode http      # IP Webcam HTTP stream
    python main.py --audio-mode device    # USB audio adapters
    python main.py --audio-mode simulate  # No phones needed (testing)
    python main.py --list-devices         # Show sounddevice indices
"""

import argparse
import os
import random
import sys
import time

import cv2

# Suppress TF logs before any TF import happens (via utils/yamnet.py)
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
os.environ["TF_CPP_MIN_LOG_LEVEL"]  = "3"

from config import ALL_IMAGES
from audio.listeners import SideListener
from traffic.detector import VehicleDetector
from traffic.controller import SignalController
from utils.yamnet import load_yamnet
from utils.mqtt_pub import MQTTPublisher


# ── Helpers ───────────────────────────────────────────────────────────────────

def print_status(counts: dict, decision: dict):
    sides      = decision["scores"]
    siren_sides = decision["siren_sides"]
    print("\n  ┌─ CYCLE DECISION ──────────────────────────────")
    for s in range(1, 5):
        siren_tag = "  🚨 SIREN" if s in siren_sides else ""
        print(f"  │ Side {s}: vehicles={counts[s]:>2}  score={sides[s]:>8.2f}{siren_tag}")
    print(f"  ├────────────────────────────────────────────")
    print(f"  │ ✅ GREEN  → Side {decision['open_side']}")
    print(f"  │ ⏱ Time   → {decision['green_time']} sec")
    if siren_sides:
        print(f"  │ 🚨 SIREN OVERRIDE → Sides {siren_sides}")
    print(f"  └────────────────────────────────────────────\n")


def update_images(images: dict, green_side: int) -> dict:
    """Rotate the image for the side that just had green (original logic)."""
    used      = set(images.values())
    available = list(set(ALL_IMAGES) - used)
    if available:
        images[green_side] = random.choice(available)
    return images


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(audio_mode: str):
    # Initial random 4 images (unchanged from original)
    images = {i + 1: p for i, p in enumerate(random.sample(ALL_IMAGES, 4))}

    # Load shared YAMNet model once
    yamnet_model, class_names, siren_indices = load_yamnet()

    # Start 4 siren listeners (one per road side)
    listeners = {}
    for side in range(1, 5):
        l = SideListener(side, yamnet_model, class_names, siren_indices, audio_mode)
        l.start()
        listeners[side] = l

    detector   = VehicleDetector()
    controller = SignalController()
    publisher  = MQTTPublisher()

    print("\n🚦 Smart Traffic System Running  (Ctrl-C to stop)\n")

    try:
        while True:
            print("\n─── New Cycle ──────────────────────────────────────")

            # 1. Count vehicles
            counts = {}
            for side, path in images.items():
                counts[side] = detector.count(path)
                print(f"  Side {side}: {counts[side]} vehicles")

            publisher.publish_counts(counts)

            # 2. Publish siren status
            for side, l in listeners.items():
                publisher.publish_siren(side, l.siren_score, l.siren_active)

            # 3. Signal decision
            decision = controller.decide(counts, listeners)

            # 4. Publish control to ESP32
            publisher.publish_control(decision)

            # 5. Print status
            print_status(counts, decision)

            # 6. Visualize
            detector.visualize(images, counts, decision["siren_sides"])

            # 7. Green phase
            time.sleep(decision["green_time"])

            # 8. Rotate image for the green side
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
        description="Smart Traffic + Siren Detection",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "--audio-mode", default="udp",
        choices=["udp", "http", "device", "simulate"],
        help=(
            "udp      — phones send raw PCM16 UDP (recommended)\n"
            "http     — IP Webcam HTTP stream\n"
            "device   — USB audio adapters\n"
            "simulate — fake audio, no phones needed\n"
        ),
    )
    p.add_argument("--list-devices", action="store_true",
                   help="List sounddevice input devices and exit.")
    args = p.parse_args()

    if args.list_devices:
        try:
            import sounddevice as sd
            print(sd.query_devices())
        except ImportError:
            print("sounddevice not installed.")
        return

    run(args.audio_mode)


if __name__ == "__main__":
    main()