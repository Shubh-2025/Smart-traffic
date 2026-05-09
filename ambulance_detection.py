"""
Ambulance / Emergency Siren Detection using YAMNet
===================================================
Real-time audio capture → YAMNet inference → Siren alert

Install:
    pip install tensorflow tensorflow-hub sounddevice numpy scipy

Run:
    python ambulance_detection.py
    python ambulance_detection.py --threshold 0.3
    python ambulance_detection.py --device 1
    python ambulance_detection.py --list-devices
    python ambulance_detection.py --list-classes
"""

# ── Suppress TensorFlow warnings (must be before any TF import) ─────────────
import os
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
os.environ["TF_CPP_MIN_LOG_LEVEL"]  = "3"

import logging
logging.getLogger("tensorflow").setLevel(logging.ERROR)
logging.getLogger("absl").setLevel(logging.ERROR)

# ── Standard library ─────────────────────────────────────────────────────────
import argparse
import csv
import queue
import sys
import threading
import time

# ── Third-party ──────────────────────────────────────────────────────────────
import numpy as np
import scipy.signal
import sounddevice as sd

try:
    import tensorflow as tf
    import tensorflow_hub as hub
except ImportError:
    sys.exit(
        "\n[ERROR] Missing packages.\n"
        "Install with:  pip install tensorflow tensorflow-hub\n"
    )

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

YAMNET_SAMPLE_RATE = 16_000
YAMNET_MODEL_URL   = "https://tfhub.dev/google/yamnet/1"

# Any YAMNet label containing these keywords is treated as a siren class.
# Run --list-classes to see all 521 labels and add more if needed.
SIREN_KEYWORDS = [
    "siren",
    "ambulance",
    "emergency vehicle",
    "fire truck",
    "fire engine",
    "police car",
    "civil defense siren",
    "air horn",
]

# ─────────────────────────────────────────────────────────────────────────────
# Audio helpers
# ─────────────────────────────────────────────────────────────────────────────

def resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Resample mono float32 audio from orig_sr to target_sr."""
    if orig_sr == target_sr:
        return audio
    num_samples = int(len(audio) * target_sr / orig_sr)
    return scipy.signal.resample(audio, num_samples).astype(np.float32)


def peak_normalize(audio: np.ndarray) -> np.ndarray:
    """Normalize audio to [-1, 1]. Returns unchanged if silent."""
    peak = np.max(np.abs(audio))
    if peak < 1e-6:
        return audio
    return audio / peak


# ─────────────────────────────────────────────────────────────────────────────
# Model loader
# ─────────────────────────────────────────────────────────────────────────────

def load_yamnet():
    """
    Load YAMNet from TensorFlow Hub and return (model, class_names).
    The model is cached locally after the first download.
    """
    print("[INFO] Loading YAMNet from TensorFlow Hub ...")
    model = hub.load(YAMNET_MODEL_URL)

    class_map_path = model.class_map_path().numpy().decode()
    class_names = []
    with open(class_map_path) as f:
        for row in csv.DictReader(f):
            class_names.append(row["display_name"])

    print(f"[INFO] YAMNet ready — {len(class_names)} sound classes available.\n")
    return model, class_names


def find_siren_indices(class_names: list) -> list:
    """Return YAMNet class indices whose label matches any SIREN_KEYWORD."""
    indices = []
    print("[INFO] Siren-related classes found in YAMNet:")
    for idx, name in enumerate(class_names):
        if any(kw in name.lower() for kw in SIREN_KEYWORDS):
            indices.append(idx)
            print(f"         [{idx:>3}]  {name}")
    if not indices:
        print("[WARN]  No siren classes matched. Check SIREN_KEYWORDS.")
    print()
    return indices


# ─────────────────────────────────────────────────────────────────────────────
# Main detector class
# ─────────────────────────────────────────────────────────────────────────────

class AmbulanceDetector:
    """
    Captures microphone audio in real time, runs YAMNet on each chunk,
    and raises an alert whenever any siren-related class exceeds threshold.

    Parameters
    ----------
    threshold   : float  - minimum score (0-1) to trigger an alert
    chunk_dur   : float  - seconds of audio per inference call (~0.975 s recommended)
    device      : int    - sounddevice input device index (None = system default)
    input_sr    : int    - microphone sample rate (auto-resampled to 16 kHz)
    cooldown    : float  - min seconds between consecutive alerts
    """

    def __init__(
        self,
        threshold : float = 0.25,
        chunk_dur : float = 0.975,
        device            = None,
        input_sr  : int   = 44_100,
        cooldown  : float = 2.0,
    ):
        self.threshold  = threshold
        self.chunk_dur  = chunk_dur
        self.device     = device
        self.input_sr   = input_sr
        self.cooldown   = cooldown
        self.chunk_size = int(input_sr * chunk_dur)

        self._audio_queue : queue.Queue = queue.Queue(maxsize=10)
        self._stop        : threading.Event = threading.Event()
        self._last_alert  : float = 0.0

        # Load model
        self.model, self.class_names = load_yamnet()
        self.siren_indices = find_siren_indices(self.class_names)

    # ── Microphone callback ──────────────────────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice for every audio block."""
        if status:
            print(f"\n[WARN] Audio status: {status}", file=sys.stderr)
        mono = indata[:, 0].astype(np.float32)
        if not self._audio_queue.full():
            self._audio_queue.put(mono.copy())

    # ── YAMNet inference ─────────────────────────────────────────────────────

    def _run_inference(self, audio_16k: np.ndarray) -> dict:
        """
        Run YAMNet on a 16 kHz mono chunk.

        Returns a dict with:
            best_score    - highest score among all siren classes
            best_label    - label with the highest siren score
            siren_scores  - {label: score} for every siren class
            top1_label    - overall top-1 YAMNet class (any category)
            top1_score    - score of the overall top-1 class
        """
        waveform    = tf.constant(audio_16k, dtype=tf.float32)
        scores, _emb, _mel = self.model(waveform)
        mean_scores = tf.reduce_mean(scores, axis=0).numpy()   # shape (521,)

        # Overall top-1
        top1_idx   = int(np.argmax(mean_scores))
        top1_label = self.class_names[top1_idx]
        top1_score = float(mean_scores[top1_idx])

        # Siren-class scores
        siren_scores = {
            self.class_names[i]: float(mean_scores[i])
            for i in self.siren_indices
        }

        if siren_scores:
            best_label = max(siren_scores, key=siren_scores.get)
            best_score = siren_scores[best_label]
        else:
            best_label, best_score = "", 0.0

        return dict(
            best_score   = best_score,
            best_label   = best_label,
            siren_scores = siren_scores,
            top1_label   = top1_label,
            top1_score   = top1_score,
        )

    # ── Alert printer ────────────────────────────────────────────────────────

    def _print_alert(self, result: dict):
        """Print a formatted alert box with all siren class scores."""
        now = time.time()
        if result["best_score"] < self.threshold:
            return
        if (now - self._last_alert) < self.cooldown:
            return
        self._last_alert = now

        border = "=" * 62
        print(f"\n{border}")
        print(f"  !!!  EMERGENCY VEHICLE / SIREN DETECTED  !!!")
        print(f"  >>   Treat as AMBULANCE - react to all siren types")
        print(f"{border}")

        print(f"\n  Siren class breakdown:")
        sorted_sirens = sorted(
            result["siren_scores"].items(), key=lambda x: x[1], reverse=True
        )
        for label, score in sorted_sirens:
            bar    = "#" * int(score * 35)
            marker = "  <- best match" if label == result["best_label"] else ""
            print(f"    {label:<38} {score:.3f}  {bar}{marker}")

        print(f"\n  Overall top-1 : {result['top1_label']} ({result['top1_score']:.3f})")
        print(f"  Best siren    : {result['best_label']} ({result['best_score']:.3f})")
        print(f"  Threshold     : {self.threshold}")
        print(f"  Time          : {time.strftime('%H:%M:%S')}")
        print(f"{border}\n")

    # ── Background processing thread ─────────────────────────────────────────

    def _processing_loop(self):
        print("[INFO] Inference thread started. Listening for sirens ...\n")

        while not self._stop.is_set():
            try:
                chunk = self._audio_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            # Prepare audio for YAMNet
            chunk_16k = resample(chunk, self.input_sr, YAMNET_SAMPLE_RATE)
            chunk_16k = peak_normalize(chunk_16k)

            result = self._run_inference(chunk_16k)

            # Live status line
            ts = time.strftime("%H:%M:%S")
            print(
                f"[{ts}]  siren={result['best_score']:.3f}  "
                f"best_siren='{result['best_label']}'  "
                f"top1='{result['top1_label']}' ({result['top1_score']:.3f})   ",
                end="\r",
            )

            self._print_alert(result)

    # ── Public entry point ───────────────────────────────────────────────────

    def run(self):
        """Open microphone stream and block until Ctrl-C."""
        print(
            f"[INFO] Microphone  device={self.device}  "
            f"sr={self.input_sr} Hz  chunk={self.chunk_dur:.3f} s  "
            f"threshold={self.threshold}\n"
            f"[INFO] Press Ctrl-C to stop.\n"
        )

        proc = threading.Thread(target=self._processing_loop, daemon=True)
        proc.start()

        try:
            with sd.InputStream(
                samplerate = self.input_sr,
                blocksize  = self.chunk_size,
                device     = self.device,
                channels   = 1,
                dtype      = "float32",
                callback   = self._audio_callback,
            ):
                while not self._stop.is_set():
                    time.sleep(0.1)

        except KeyboardInterrupt:
            print("\n\n[INFO] Stopping detector ...")
        finally:
            self._stop.set()
            proc.join(timeout=3)
            print("[INFO] Detector stopped cleanly.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI helpers
# ─────────────────────────────────────────────────────────────────────────────

def list_devices():
    print("\nAvailable audio input devices:\n")
    print(sd.query_devices())


def list_classes():
    model, class_names = load_yamnet()
    print(f"\nAll {len(class_names)} YAMNet classes:\n")
    for i, name in enumerate(class_names):
        print(f"  {i:>4}  {name}")


# ─────────────────────────────────────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Real-time ambulance / siren detection with YAMNet",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--threshold", type=float, default=0.25,
        help="Siren score (0-1) to trigger alert. Lower = more sensitive.",
    )
    p.add_argument(
        "--chunk-duration", type=float, default=0.975, dest="chunk_dur",
        help="Seconds of audio per YAMNet inference call.",
    )
    p.add_argument(
        "--device", type=int, default=None,
        help="Microphone device index (see --list-devices).",
    )
    p.add_argument(
        "--input-sr", type=int, default=44_100, dest="input_sr",
        help="Microphone sample rate. Auto-resampled to 16 kHz for YAMNet.",
    )
    p.add_argument(
        "--cooldown", type=float, default=2.0,
        help="Minimum seconds between consecutive alerts.",
    )
    p.add_argument(
        "--list-devices", action="store_true",
        help="Print available microphone devices and exit.",
    )
    p.add_argument(
        "--list-classes", action="store_true",
        help="Print all 521 YAMNet class names and exit.",
    )
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = build_parser().parse_args()

    if args.list_devices:
        list_devices()
        return

    if args.list_classes:
        list_classes()
        return

    detector = AmbulanceDetector(
        threshold = args.threshold,
        chunk_dur = args.chunk_dur,
        device    = args.device,
        input_sr  = args.input_sr,
        cooldown  = args.cooldown,
    )
    detector.run()


if __name__ == "__main__":
    main()
