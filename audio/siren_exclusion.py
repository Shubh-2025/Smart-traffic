# audio/siren_exclusion.py — Shared ambulance exclusion zone
#
# Problem: A real ambulance passes through the intersection sequentially.
# Side 1 hears it first and alerts. As the vehicle moves, Side 2 also picks
# it up — but that's the *same* ambulance, not a second emergency.
#
# Solution: The first side to fire a confirmed alert "claims" the exclusion
# zone for SIREN_EXCLUSION_WINDOW seconds. Other sides can still detect sound
# and update their scores, but they won't fire a new alert or gain
# siren_active=True while the window is held by a different side.
#
# The owning side can always renew the window (its own ambulance is still there).
# Once the window expires, any side is free to claim it again.

import threading
import time


class SirenExclusionZone:
    """
    Shared, thread-safe exclusion zone.

    Usage (called from SideListener inference threads):

        # When a confirmed alert is about to fire on `side`:
        if exclusion.try_claim(side, window_seconds):
            # We own it — fire the alert, set siren_active=True
            ...
        else:
            # Another side owns it — suppress alert for this side
            ...

        # On every inference tick where a siren score is above threshold:
        exclusion.renew_if_owner(side, window_seconds)
    """

    def __init__(self):
        self._lock       = threading.Lock()
        self._owner      : int | None = None   # side that holds the zone
        self._expires_at : float      = 0.0    # epoch time when zone expires

    # ── Public API ─────────────────────────────────────────────────────────

    def try_claim(self, side: int, window: float) -> bool:
        """
        Attempt to claim the exclusion zone for `side`.

        Returns True  — zone was free or already owned by `side` → alert can fire.
        Returns False — zone is owned by a *different* side    → suppress alert.
        """
        with self._lock:
            now = time.time()

            # Zone is free (never claimed, or expired)
            if self._owner is None or now >= self._expires_at:
                self._owner      = side
                self._expires_at = now + window
                return True

            # Same side renewing its own window
            if self._owner == side:
                self._expires_at = now + window
                return True

            # Different side owns the active window — suppress
            return False

    def renew_if_owner(self, side: int, window: float) -> None:
        """
        If `side` already owns the zone, push the expiry forward.
        Called each inference tick where the score stays above threshold,
        so the window stays open as long as the ambulance is audible.
        """
        with self._lock:
            now = time.time()
            if self._owner == side and now < self._expires_at:
                self._expires_at = now + window

    def release_if_owner(self, side: int) -> None:
        """
        Immediately release the zone if `side` owns it.
        Called when the siren score drops below threshold and the decay
        window has expired — the ambulance has passed.
        """
        with self._lock:
            if self._owner == side:
                self._owner      = None
                self._expires_at = 0.0

    def owner(self) -> int | None:
        """Return the side that currently holds the zone, or None."""
        with self._lock:
            if time.time() < self._expires_at:
                return self._owner
            return None

    def is_suppressed(self, side: int) -> bool:
        """Return True if `side` should suppress its siren alert right now."""
        with self._lock:
            now = time.time()
            return (
                self._owner is not None
                and self._owner != side
                and now < self._expires_at
            )

    def status(self) -> dict:
        """Snapshot for logging / dashboard (non-blocking)."""
        with self._lock:
            now     = time.time()
            active  = self._owner is not None and now < self._expires_at
            return {
                "active"    : active,
                "owner_side": self._owner if active else None,
                "ttl"       : max(0.0, self._expires_at - now) if active else 0.0,
            }


# ── Module-level singleton ─────────────────────────────────────────────────
#
# Both audio/listeners.py and any future modules import this one instance.
# Created once at import time; no initialisation needed.

exclusion_zone = SirenExclusionZone()