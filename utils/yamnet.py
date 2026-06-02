# utils/yamnet.py — YAMNet model loader + inference

import csv
import os
import logging
import numpy as np
import scipy.signal

os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
os.environ["TF_CPP_MIN_LOG_LEVEL"]  = "3"
logging.getLogger("tensorflow").setLevel(logging.ERROR)
logging.getLogger("absl").setLevel(logging.ERROR)

import tensorflow as tf
import tensorflow_hub as hub

from config import YAMNET_URL, YAMNET_SR, SIREN_KEYWORDS


def load_yamnet():
    """Load YAMNet. Returns (model, class_names, siren_indices)."""
    print("[YAMNet] Loading model from TF Hub ...")
    model = hub.load(YAMNET_URL)

    class_names = []
    with open(model.class_map_path().numpy().decode()) as f:
        for row in csv.DictReader(f):
            class_names.append(row["display_name"])

    siren_indices = []
    print("[YAMNet] Siren classes matched:")
    for idx, name in enumerate(class_names):
        if any(kw in name.lower() for kw in SIREN_KEYWORDS):
            siren_indices.append(idx)
            print(f"         [{idx:>3}] {name}")
    print(f"[YAMNet] Ready — {len(class_names)} classes, {len(siren_indices)} siren.\n")
    return model, class_names, siren_indices


def infer(model, class_names, siren_indices, audio_16k: np.ndarray) -> dict:
    """Run YAMNet on 16 kHz mono audio. Returns result dict."""
    try:
        scores, _e, _m = model(tf.constant(audio_16k, dtype=tf.float32))
        mean  = tf.reduce_mean(scores, axis=0).numpy()
        top1  = int(np.argmax(mean))
        sc    = {class_names[i]: float(mean[i]) for i in siren_indices}
        best  = max(sc, key=sc.get) if sc else ""
        return dict(
            best_score   = sc[best] if best else 0.0,
            best_label   = best,
            siren_scores = sc,
            top1_label   = class_names[top1],
            top1_score   = float(mean[top1]),
        )
    except Exception as e:
        print(f"[YAMNet] Inference error: {e}")
        return dict(best_score=0.0, best_label="", siren_scores={},
                    top1_label="", top1_score=0.0)


def normalize(audio: np.ndarray) -> np.ndarray:
    peak = np.max(np.abs(audio))
    return audio if peak < 1e-6 else audio / peak


def resample_to_16k(audio: np.ndarray, orig_sr: int) -> np.ndarray:
    if orig_sr == YAMNET_SR:
        return audio.astype(np.float32)
    n = int(len(audio) * YAMNET_SR / orig_sr)
    return scipy.signal.resample(audio, n).astype(np.float32)


def pcm16_to_float32(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0