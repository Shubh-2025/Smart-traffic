# traffic/controller.py — Priority scoring + green signal decision

from config import (
    MIN_GREEN, MAX_GREEN, FACTOR, ALPHA,
    WEIGHT_TRAFFIC, WEIGHT_WAIT, MAX_CONSECUTIVE,
    SIREN_PRIORITY_BOOST,
)


class SignalController:
    """
    Stateful controller that decides which side gets green
    and for how long, factoring in siren priority.
    """

    def __init__(self):
        self.wait_time        = {s: 0   for s in range(1, 5)}
        self.smoothed         = {s: 0.0 for s in range(1, 5)}
        self.consecutive_wins = {s: 0   for s in range(1, 5)}

    def decide(self, counts: dict, siren_listeners: dict) -> dict:
        """
        counts          : {side: vehicle_count}
        siren_listeners : {side: SideListener}

        Returns {open_side, green_time, siren_override, siren_sides, scores}
        """
        # EWMA + wait accumulation
        for s in range(1, 5):
            self.smoothed[s] = ALPHA * counts[s] + (1 - ALPHA) * self.smoothed[s]
            self.wait_time[s] += 1

        # Active siren sides
        siren_sides = [s for s, l in siren_listeners.items() if l.siren_active]

        # Priority score per side
        scores = {}
        for s in range(1, 5):
            base = (self.smoothed[s] * WEIGHT_TRAFFIC +
                    self.wait_time[s] * WEIGHT_WAIT)
            if self.consecutive_wins[s] >= MAX_CONSECUTIVE:
                base *= 0.6
            if s in siren_sides:
                base += SIREN_PRIORITY_BOOST
                base += siren_listeners[s].siren_score * 500
            scores[s] = base

        best_side = max(scores, key=scores.get)

        # Green time: shorter when clearing an ambulance
        if siren_sides and best_side in siren_sides:
            green_time = MIN_GREEN
        else:
            green_time = min(MAX_GREEN,
                             max(MIN_GREEN,
                                 int(MIN_GREEN + self.smoothed[best_side] * FACTOR)))

        # Update stats
        for s in range(1, 5):
            if s == best_side:
                self.consecutive_wins[s] += 1
                self.wait_time[s] = 0
            else:
                self.consecutive_wins[s] = 0

        return dict(
            open_side      = best_side,
            green_time     = green_time,
            siren_override = bool(siren_sides),
            siren_sides    = siren_sides,
            scores         = scores,
        )
