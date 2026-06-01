# audio/simulator.py — Fake audio for testing without phones
# Injects random noise + occasional siren tones directly into SideListeners

import threading
import time
import random
import numpy as np

from config import YAMNET_SR, CHUNK_DURATION
from utils.yamnet import pcm16_to_float32

CHUNK_SAMPLES = int(YAMNET_SR * CHUNK_DURATION)


class SirenSimulator:
    """
    Generates fake PCM audio and feeds it into SideListeners.
    Used when --simulate flag is passed to main.py.
    Randomly injects siren-like tones (~4% chance per chunk per side).
    """

    def __init__(self, listeners: dict):
        self.listeners = listeners
        self._stop     = threading.Event()

    def start(self):
        for side, listener in self.listeners.items():
            threading.Thread(
                target=self._run,
                args=(side, listener),
                daemon=True,
                name=f"sim-side{side}",
            ).start()

    def stop(self):
        self._stop.set()

    def _run(self, side: int, listener):
        t = np.linspace(0, CHUNK_DURATION, CHUNK_SAMPLES)
        print(f"[Simulator] Side {side} — generating fake audio")

        while not self._stop.is_set():
            if random.random() < 0.04:
                # Wailing siren tone
                freq  = 700 + 800 * (0.5 + 0.5 * np.sin(2 * np.pi * 2 * t))
                audio = (0.8 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
                print(f"[Simulator] 🔊 Fake siren on Side {side}")
            else:
                # Background noise
                audio = (np.random.randn(CHUNK_SAMPLES) * 0.02).astype(np.float32)

            # Convert to PCM16 bytes then feed (same path as real phone audio)
            pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            listener.feed_audio(pcm16.tobytes())
            time.sleep(CHUNK_DURATION)