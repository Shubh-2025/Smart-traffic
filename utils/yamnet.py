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
    """Load YAMNet from TF Hub. Returns (model, class_names, siren_indices)."""
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

    print(f"[YAMNet] Ready — {len(class_names)} classes, {len(siren_indices)} siren indices.\n")
    return model, class_names, siren_indices


def infer(model, class_names, siren_indices, audio_16k: np.ndarray) -> dict:
    """
    Run YAMNet on 16 kHz mono float32 audio.

    Returns a dict:
        best_score   float  — highest score among siren_indices
        best_label   str    — class name for best_score
        siren_scores dict   — {class_name: score} for all siren_indices
        top1_label   str    — overall top-1 class name (used by blocklist check)
        top1_score   float  — overall top-1 score
        top3         list   — [(name, score), ...] top 3 classes (for debug logging)
    """
    try:
        scores, _embeddings, _spectrogram = model(
            tf.constant(audio_16k, dtype=tf.float32)
        )
        mean = tf.reduce_mean(scores, axis=0).numpy()   # shape: (num_classes,)

        # Overall top-1 (used for blocklist check)
        top1_idx   = int(np.argmax(mean))
        top1_label = class_names[top1_idx]
        top1_score = float(mean[top1_idx])

        # Top-3 for debug/alert printing
        top3_indices = np.argsort(mean)[-3:][::-1]
        top3 = [(class_names[i], float(mean[i])) for i in top3_indices]

        # Siren-specific scores
        siren_scores = {class_names[i]: float(mean[i]) for i in siren_indices}

        if siren_scores:
            best_label = max(siren_scores, key=siren_scores.get)
            best_score = siren_scores[best_label]
        else:
            best_label = ""
            best_score = 0.0

        return dict(
            best_score   = best_score,
            best_label   = best_label,
            siren_scores = siren_scores,
            top1_label   = top1_label,
            top1_score   = top1_score,
            top3         = top3,
        )

    except Exception as e:
        print(f"[YAMNet] Inference error: {e}")
        return dict(
            best_score   = 0.0,
            best_label   = "",
            siren_scores = {},
            top1_label   = "",
            top1_score   = 0.0,
            top3         = [],
        )


def normalize(audio: np.ndarray) -> np.ndarray:
    """Peak-normalize audio to [-1, 1]. Returns unchanged if silent."""
    peak = np.max(np.abs(audio))
    return audio if peak < 1e-6 else audio / peak


def resample_to_16k(audio: np.ndarray, orig_sr: int) -> np.ndarray:
    """Resample audio to 16 kHz if needed."""
    if orig_sr == YAMNET_SR:
        return audio.astype(np.float32)
    n = int(len(audio) * YAMNET_SR / orig_sr)
    return scipy.signal.resample(audio, n).astype(np.float32)


def pcm16_to_float32(data: bytes) -> np.ndarray:
    """Convert raw 16-bit PCM bytes to float32 in [-1, 1]."""
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0