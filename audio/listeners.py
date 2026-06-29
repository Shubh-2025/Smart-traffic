# audio/listeners.py — Per-side YAMNet siren detection
#
# Audio comes from phones via WebSocket (browser mic → ws_server.py).
# main.py calls listener.feed_audio(raw_bytes) to push PCM data here.
# No Termux, no UDP, no app — phones just open a browser URL.

import queue
import threading
import time

import numpy as np

from config import (
    YAMNET_SR, CHUNK_DURATION,
    SIREN_THRESHOLD, SIREN_COOLDOWN, SIREN_DECAY,
    SIREN_BLOCKLIST, SIREN_CONFIRM_CHUNKS,
    SIREN_EXCLUSION_WINDOW,
)
from audio.siren_exclusion import exclusion_zone
from utils.yamnet import infer, normalize, pcm16_to_float32

CHUNK_SAMPLES = int(YAMNET_SR * CHUNK_DURATION)


class SideListener:
    """
    One instance per road side (1-4).

    Audio flow:
        Phone browser mic / ESP32 INMP441
            → WebSocket (ws_server.py)
            → main.py bridge calls feed_audio(raw_bytes)
            → _inference_loop runs YAMNet
            → updates .siren_active / .siren_score

    Public attributes (read by main.py and controller.py):
        .siren_active   bool   — True while siren is detected AND this side
                                 is not suppressed by the exclusion zone
        .siren_score    float  — latest YAMNet siren score (0-1)
        .best_label     str    — matched siren class name

    Optional callback set by main.py:
        .on_siren_detected(side, score, label, active, is_new_alert)

    False-positive mitigation:
        • SIREN_BLOCKLIST       — suppresses alert when top-1 class is a
                                  horn/vehicle sound
        • SIREN_CONFIRM_CHUNKS  — requires N consecutive chunks above threshold
                                  (~2 s) before firing, ignoring single short
                                  horn blasts
        • exclusion_zone        — module-level singleton (siren_exclusion.py):
                                  when one side confirms a siren, all other
                                  sides are suppressed for SIREN_EXCLUSION_WINDOW
                                  seconds so the same ambulance passing through
                                  is not counted as multiple independent events
    """

    def __init__(self, side: int, yamnet_model, class_names, siren_indices):
        self.side          = side
        self.model         = yamnet_model
        self.class_names   = class_names
        self.siren_indices = siren_indices

        self.siren_score   : float = 0.0
        self.siren_active  : bool  = False
        self.best_label    : str   = ""
        self.last_detected : float = 0.0
        self.on_siren_detected     = None   # set by main.py

        # False-positive suppression state
        self._confirm_count: int   = 0      # consecutive chunks above threshold

        self._audio_q = queue.Queue(maxsize=10)
        self._stop    = threading.Event()

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self):
        threading.Thread(
            target=self._inference_loop,
            daemon=True,
            name=f"infer-side{self.side}",
        ).start()
        print(f"[Side {self.side}] Listener ready — waiting for audio")

    def stop(self):
        self._stop.set()

    def feed_audio(self, raw_bytes: bytes):
        """
        Called by main.py WebSocket bridge.
        raw_bytes: raw 16-bit PCM mono 16 kHz from the phone browser mic or ESP32.
        """
        try:
            audio = pcm16_to_float32(raw_bytes)
            if not self._audio_q.full():
                self._audio_q.put_nowait(audio)
        except Exception as e:
            print(f"[Side {self.side}] feed_audio error: {e}")

    # ── YAMNet inference loop ─────────────────────────────────────────────────

    def _inference_loop(self):
        while not self._stop.is_set():

            # ── Wait for audio chunk ──────────────────────────────────────────
            try:
                audio = self._audio_q.get(timeout=1.0)
            except queue.Empty:
                # Decay siren active flag even when no audio arrives.
                # Also release the exclusion zone if our own decay has expired.
                if (time.time() - self.last_detected) >= SIREN_DECAY:
                    self.siren_active = False
                    exclusion_zone.release_if_owner(self.side)
                else:
                    self.siren_active = (time.time() - self.last_detected) < SIREN_DECAY
                continue

            # ── Run YAMNet ────────────────────────────────────────────────────
            try:
                audio  = normalize(audio)
                result = infer(self.model, self.class_names, self.siren_indices, audio)
            except Exception as e:
                print(f"[Side {self.side}] Inference error: {e}")
                continue

            # ── Update running state ──────────────────────────────────────────
            self.siren_score = result["best_score"]
            self.best_label  = result["best_label"]

            # Decay logic: siren_active stays True for SIREN_DECAY seconds
            # after the last confirmed detection, UNLESS we are suppressed
            # by the exclusion zone (another side's ambulance).
            time_since_detected = time.time() - self.last_detected
            within_decay        = time_since_detected < SIREN_DECAY
            suppressed          = exclusion_zone.is_suppressed(self.side)
            self.siren_active   = within_decay and not suppressed

            # If our score is still above threshold and we own the zone, renew it
            if result["best_score"] >= SIREN_THRESHOLD:
                exclusion_zone.renew_if_owner(self.side, SIREN_EXCLUSION_WINDOW)
            else:
                # Score dropped below threshold; if our decay has also expired,
                # release the zone so another side can claim it next time
                if not within_decay:
                    exclusion_zone.release_if_owner(self.side)

            # ── Blocklist check — suppress if top-1 is a horn/vehicle sound ──
            #    e.g. "Car horn", "Vehicle horn", "Air horn" → not an ambulance
            top1_lower = result["top1_label"].lower()
            is_blocked = any(kw.lower() in top1_lower for kw in SIREN_BLOCKLIST)

            if is_blocked:
                # Reset streak — a honk doesn't count toward confirmation
                self._confirm_count = 0
                if self.on_siren_detected:
                    try:
                        self.on_siren_detected(
                            self.side, 0.0, "", self.siren_active, False
                        )
                    except Exception as e:
                        print(f"[Side {self.side}] Callback error: {e}")
                continue

            # ── Consecutive-chunk confirmation ────────────────────────────────
            #    A real siren is sustained; a car horn is a short burst.
            #    Require SIREN_CONFIRM_CHUNKS consecutive chunks above threshold
            #    before firing an alert (~SIREN_CONFIRM_CHUNKS × CHUNK_DURATION s).
            if result["best_score"] >= SIREN_THRESHOLD:
                self._confirm_count += 1
            else:
                self._confirm_count = 0   # any weak chunk resets the streak

            is_new_alert = False
            if self._confirm_count >= SIREN_CONFIRM_CHUNKS:
                now = time.time()
                if (now - self.last_detected) >= SIREN_COOLDOWN:

                    # ── Exclusion zone check ──────────────────────────────────
                    # try_claim() returns True if:
                    #   a) zone is free → we become the owner
                    #   b) we already own the zone → renew and proceed
                    # Returns False if another side owns the active window.
                    if exclusion_zone.try_claim(self.side, SIREN_EXCLUSION_WINDOW):
                        # We are the authoritative side — fire the alert
                        self.last_detected  = now
                        is_new_alert        = True
                        self.siren_active   = True
                        self._confirm_count = 0   # reset after firing
                        self._print_alert(result)
                    else:
                        # Another side owns the zone — same ambulance is passing.
                        # Reset confirmation streak so we don't re-fire the instant
                        # the window expires; the ambulance has already been handled.
                        self._confirm_count = 0
                        ez = exclusion_zone.status()
                        print(
                            f"[Side {self.side}] Siren suppressed — "
                            f"ambulance already claimed by Side {ez['owner_side']} "
                            f"({ez['ttl']:.1f}s remaining in exclusion window)"
                        )

            # ── Notify callback → pushes to dashboard + MQTT ─────────────────
            if self.on_siren_detected:
                try:
                    self.on_siren_detected(
                        self.side,
                        self.siren_score,
                        self.best_label,
                        self.siren_active,
                        is_new_alert,
                    )
                except Exception as e:
                    print(f"[Side {self.side}] Callback error: {e}")

    def _print_alert(self, result: dict):
        ez  = exclusion_zone.status()
        b   = "=" * 55
        print(f"\n{b}")
        print(f"  🚨  SIREN — SIDE {self.side}  [exclusion zone claimed for {SIREN_EXCLUSION_WINDOW:.0f}s]")
        print(f"  Match : {result['best_label']} ({result['best_score']:.3f})")
        print(f"  Top-1 : {result['top1_label']} ({result['top1_score']:.3f})")
        top3 = result.get("top3", [])
        if top3:
            print(f"  Top-3 : {', '.join(f'{n}({s:.2f})' for n, s in top3)}")
        print(f"  Time  : {time.strftime('%H:%M:%S')}")
        print(f"{b}\n")