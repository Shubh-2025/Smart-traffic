# traffic/controller.py — Priority scoring + green signal decision

from config import (
    MIN_GREEN, MAX_GREEN, FACTOR, ALPHA,
    WEIGHT_TRAFFIC, WEIGHT_WAIT, MAX_CONSECUTIVE, SIREN_PRIORITY_BOOST,
)


class SignalController:
    def __init__(self):
        self.wait_time        = {s: 0   for s in range(1, 5)}
        self.smoothed         = {s: 0.0 for s in range(1, 5)}
        self.consecutive_wins = {s: 0   for s in range(1, 5)}

    def decide(self, counts: dict, listeners: dict) -> dict:
        """
        counts    : {side: vehicle_count}
        listeners : {side: SideListener}
        Returns   : {open_side, green_time, siren_override, siren_sides, scores}
        """
        for s in range(1, 5):
            self.smoothed[s]  = ALPHA * counts.get(s, 0) + (1 - ALPHA) * self.smoothed[s]
            self.wait_time[s] += 1

        siren_sides = [s for s, l in listeners.items() if l.siren_active]

        scores = {}
        for s in range(1, 5):
            base = self.smoothed[s] * WEIGHT_TRAFFIC + self.wait_time[s] * WEIGHT_WAIT
            if self.consecutive_wins[s] >= MAX_CONSECUTIVE:
                base *= 0.6
            if s in siren_sides:
                base += SIREN_PRIORITY_BOOST + listeners[s].siren_score * 500
            scores[s] = base

        best = max(scores, key=scores.get)

        if siren_sides and best in siren_sides:
            green_time = MIN_GREEN
        else:
            green_time = min(MAX_GREEN, max(MIN_GREEN,
                             int(MIN_GREEN + self.smoothed[best] * FACTOR)))

        for s in range(1, 5):
            if s == best:
                self.consecutive_wins[s] += 1
                self.wait_time[s] = 0
            else:
                self.consecutive_wins[s] = 0

        return dict(
            open_side      = best,
            green_time     = green_time,
            siren_override = bool(siren_sides),
            siren_sides    = siren_sides,
            scores         = scores,
        )