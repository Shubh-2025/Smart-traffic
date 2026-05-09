# utils/yamnet.py — YAMNet loader + inference helpers

import csv
import logging
import numpy as np
import scipy.signal

logging.getLogger("tensorflow").setLevel(logging.ERROR)
logging.getLogger("absl").setLevel(logging.ERROR)

import os
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
os.environ["TF_CPP_MIN_LOG_LEVEL"]  = "3"

import tensorflow as tf
import tensorflow_hub as hub

from config import YAMNET_URL, YAMNET_SR, SIREN_KEYWORDS


def load_yamnet():
    """Load YAMNet model + class names. Returns (model, class_names, siren_indices)."""
    print("[YAMNet] Loading model ...")
    model = hub.load(YAMNET_URL)

    class_map_path = model.class_map_path().numpy().decode()
    class_names = []
    with open(class_map_path) as f:
        for row in csv.DictReader(f):
            class_names.append(row["display_name"])

    siren_indices = []
    print("[YAMNet] Siren classes matched:")
    for idx, name in enumerate(class_names):
        if any(kw in name.lower() for kw in SIREN_KEYWORDS):
            siren_indices.append(idx)
            print(f"         [{idx:>3}] {name}")
    print(f"[YAMNet] Ready — {len(class_names)} classes, {len(siren_indices)} siren classes.\n")

    return model, class_names, siren_indices


def infer(model, class_names, siren_indices, audio_16k: np.ndarray) -> dict:
    """Run YAMNet on a 16kHz mono chunk. Returns scores dict."""
    try:
        waveform = tf.constant(audio_16k, dtype=tf.float32)
        scores, _e, _m = model(waveform)
        mean_scores = tf.reduce_mean(scores, axis=0).numpy()

        top1_idx   = int(np.argmax(mean_scores))
        siren_sc   = {class_names[i]: float(mean_scores[i]) for i in siren_indices}
        best_label = max(siren_sc, key=siren_sc.get) if siren_sc else ""
        best_score = siren_sc[best_label] if best_label else 0.0

        return dict(
            best_score = best_score,
            best_label = best_label,
            siren_scores = siren_sc,
            top1_label = class_names[top1_idx],
            top1_score = float(mean_scores[top1_idx]),
        )
    except Exception as e:
        print(f"[YAMNet] Inference error: {e}")
        return dict(best_score=0.0, best_label="", siren_scores={},
                    top1_label="", top1_score=0.0)


def resample(audio: np.ndarray, orig_sr: int) -> np.ndarray:
    if orig_sr == YAMNET_SR:
        return audio.astype(np.float32)
    n = int(len(audio) * YAMNET_SR / orig_sr)
    return scipy.signal.resample(audio, n).astype(np.float32)


def normalize(audio: np.ndarray) -> np.ndarray:
    peak = np.max(np.abs(audio))
    return audio if peak < 1e-6 else audio / peak


def pcm16_bytes_to_float32(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    return arr / 32768.0
