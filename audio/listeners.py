# audio/listeners.py — Per-side audio capture + siren detection
#
# Each SideListener runs two daemon threads:
#   1. _receive_loop  — grabs audio from phone (UDP / HTTP / device / simulate)
#   2. _inference_loop — runs YAMNet; updates .siren_active / .siren_score
#
# ALL network/IO errors are caught; the system never crashes due to a dead phone.

import queue
import random
import socket
import time
import threading
import urllib.request

import numpy as np

from config import (
    YAMNET_SR, CHUNK_DURATION, SIREN_THRESHOLD,
    SIREN_COOLDOWN, SIREN_DECAY,
    PHONE_UDP, PHONE_HTTP, PHONE_DEVICE,
)
from utils.yamnet import infer, normalize, pcm16_bytes_to_float32

CHUNK_SAMPLES = int(YAMNET_SR * CHUNK_DURATION)
CHUNK_BYTES   = CHUNK_SAMPLES * 2   # 16-bit PCM


class SideListener:
    """
    Listens to one road-side phone and exposes:
        .siren_active  bool
        .siren_score   float
        .best_label    str
    """

    def __init__(self, side: int, yamnet_model, class_names,
                 siren_indices, audio_mode: str):
        self.side          = side
        self.model         = yamnet_model
        self.class_names   = class_names
        self.siren_indices = siren_indices
        self.audio_mode    = audio_mode

        self.siren_score   : float = 0.0
        self.siren_active  : bool  = False
        self.best_label    : str   = ""
        self.last_detected : float = 0.0

        self._queue = queue.Queue(maxsize=5)
        self._stop  = threading.Event()

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self):
        threading.Thread(target=self._receive_loop,   daemon=True).start()
        threading.Thread(target=self._inference_loop, daemon=True).start()
        print(f"[Side {self.side}] Listener started ({self.audio_mode} mode)")

    def stop(self):
        self._stop.set()

    # ── Receive router ────────────────────────────────────────────────────────

    def _receive_loop(self):
        dispatch = {
            "udp":      self._recv_udp,
            "http":     self._recv_http,
            "device":   self._recv_device,
            "simulate": self._recv_simulate,
        }
        fn = dispatch.get(self.audio_mode, self._recv_simulate)
        while not self._stop.is_set():
            try:
                fn()
            except Exception as e:
                # Outer safety net: restart receiver after any unhandled error
                print(f"[Side {self.side}] Receiver crashed ({e}), restarting in 5s ...")
                time.sleep(5)

    # ── UDP ───────────────────────────────────────────────────────────────────

    def _recv_udp(self):
        host, port = PHONE_UDP[self.side]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.settimeout(3.0)
        except OSError as e:
            print(f"[Side {self.side}] UDP bind error: {e}. Retrying in 5s ...")
            time.sleep(5)
            return

        print(f"[Side {self.side}] UDP listening on {host}:{port}")
        buf = b""

        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(65535)
                buf += data
                while len(buf) >= CHUNK_BYTES:
                    chunk, buf = buf[:CHUNK_BYTES], buf[CHUNK_BYTES:]
                    audio = pcm16_bytes_to_float32(chunk)
                    self._enqueue(audio)
            except socket.timeout:
                # Phone not sending — silently continue, don't crash
                pass
            except OSError as e:
                print(f"[Side {self.side}] UDP recv error: {e}")
                break

        try:
            sock.close()
        except Exception:
            pass

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _recv_http(self):
        url = PHONE_HTTP[self.side]
        buf = b""
        print(f"[Side {self.side}] HTTP connecting to {url}")

        while not self._stop.is_set():
            try:
                req = urllib.request.urlopen(url, timeout=5)
                while not self._stop.is_set():
                    data = req.read(4096)
                    if not data:
                        break
                    buf += data
                    while len(buf) >= CHUNK_BYTES:
                        chunk, buf = buf[:CHUNK_BYTES], buf[CHUNK_BYTES:]
                        audio = pcm16_bytes_to_float32(chunk)
                        self._enqueue(audio)
            except Exception as e:
                print(f"[Side {self.side}] HTTP error: {e}. Retrying in 5s ...")
                time.sleep(5)

    # ── sounddevice ───────────────────────────────────────────────────────────

    def _recv_device(self):
        try:
            import sounddevice as sd
        except ImportError:
            print(f"[Side {self.side}] sounddevice not installed, falling back to simulate.")
            self._recv_simulate()
            return

        dev = PHONE_DEVICE[self.side]

        def _cb(indata, frames, time_info, status):
            self._enqueue(indata[:, 0].astype(np.float32).copy())

        while not self._stop.is_set():
            try:
                print(f"[Side {self.side}] sounddevice device={dev}")
                with sd.InputStream(samplerate=YAMNET_SR, blocksize=CHUNK_SAMPLES,
                                    device=dev, channels=1, dtype="float32",
                                    callback=_cb):
                    while not self._stop.is_set():
                        time.sleep(0.1)
            except Exception as e:
                print(f"[Side {self.side}] sounddevice error: {e}. Retrying in 5s ...")
                time.sleep(5)

    # ── Simulate ──────────────────────────────────────────────────────────────

    def _recv_simulate(self):
        print(f"[Side {self.side}] SIMULATE mode")
        t_arr = np.linspace(0, CHUNK_DURATION, CHUNK_SAMPLES)

        while not self._stop.is_set():
            if random.random() < 0.04:
                freq  = 700 + 800 * (0.5 + 0.5 * np.sin(2 * np.pi * 2 * t_arr))
                audio = (0.8 * np.sin(2 * np.pi * freq * t_arr)).astype(np.float32)
                print(f"[Side {self.side}] 🔊 Simulated siren!")
            else:
                audio = (np.random.randn(CHUNK_SAMPLES) * 0.02).astype(np.float32)
            self._enqueue(audio)
            time.sleep(CHUNK_DURATION)

    # ── Inference ─────────────────────────────────────────────────────────────

    def _inference_loop(self):
        while not self._stop.is_set():
            try:
                audio = self._queue.get(timeout=1.0)
            except queue.Empty:
                # Update decay even when no new audio arrives
                self.siren_active = (time.time() - self.last_detected) < SIREN_DECAY
                continue

            try:
                audio = normalize(audio)
                result = infer(self.model, self.class_names, self.siren_indices, audio)
            except Exception as e:
                print(f"[Side {self.side}] Inference error: {e}")
                continue

            self.siren_score = result["best_score"]
            self.best_label  = result["best_label"]
            self.siren_active = (time.time() - self.last_detected) < SIREN_DECAY

            if result["best_score"] >= SIREN_THRESHOLD:
                now = time.time()
                if (now - self.last_detected) >= SIREN_COOLDOWN:
                    self.last_detected = now
                    self._alert(result)

    def _alert(self, result: dict):
        b = "═" * 55
        print(f"\n{b}")
        print(f"  🚨  SIREN — SIDE {self.side}  |  {result['best_label']} ({result['best_score']:.3f})")
        print(f"  ⏰  {time.strftime('%H:%M:%S')}")
        print(f"{b}\n")

    def _enqueue(self, audio: np.ndarray):
        if not self._queue.full():
            self._queue.put(audio)
