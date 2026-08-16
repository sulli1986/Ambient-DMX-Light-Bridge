"""Shared BPM / tap-tempo clock for beat-synced effects."""

from __future__ import annotations

import time
from collections import deque


class TempoClock:
    def __init__(self, bpm: float = 120.0):
        self.bpm = float(bpm)
        self._phase_offset = 0.0  # beats at t=0 reference
        self._anchor_t = time.monotonic()
        self._taps: deque[float] = deque(maxlen=8)
        self._beat_count = 0
        self._last_beat_idx = -1
        self.last_kick_t = 0.0

    def set_bpm(self, bpm: float):
        bpm = max(40.0, min(240.0, float(bpm)))
        # Preserve phase when changing BPM
        phase = self.beat_phase()
        self.bpm = bpm
        self._anchor_t = time.monotonic()
        self._phase_offset = phase
        return self.bpm

    def tap(self) -> float:
        """Record a tap; returns updated BPM (or current if not enough taps)."""
        now = time.monotonic()
        if self._taps and (now - self._taps[-1]) > 2.5:
            self._taps.clear()
        self._taps.append(now)
        if len(self._taps) >= 2:
            intervals = [
                self._taps[i] - self._taps[i - 1]
                for i in range(1, len(self._taps))
            ]
            avg = sum(intervals) / len(intervals)
            if avg > 0:
                self.set_bpm(60.0 / avg)
                # Align downbeat to this tap
                self._anchor_t = now
                self._phase_offset = 0.0
        return self.bpm

    def kick(self) -> float:
        """Treat a kick drum hit as a downbeat — updates BPM and snaps phase."""
        now = time.monotonic()
        self.last_kick_t = now
        bpm = self.tap()
        self._anchor_t = now
        self._phase_offset = 0.0
        return bpm

    def kick_age(self, now: float | None = None) -> float:
        """Seconds since last kick (large if never)."""
        now = time.monotonic() if now is None else now
        if self.last_kick_t <= 0:
            return 999.0
        return now - self.last_kick_t

    def beat_phase(self, now: float | None = None) -> float:
        """Fractional beat position [0, 1)."""
        now = time.monotonic() if now is None else now
        elapsed = now - self._anchor_t
        beats = self._phase_offset + elapsed * (self.bpm / 60.0)
        return beats % 1.0

    def beat_index(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        elapsed = now - self._anchor_t
        beats = self._phase_offset + elapsed * (self.bpm / 60.0)
        return int(beats)

    def on_beat(self, now: float | None = None) -> bool:
        """True once per beat crossing (edge detect)."""
        idx = self.beat_index(now)
        if idx != self._last_beat_idx:
            self._last_beat_idx = idx
            self._beat_count += 1
            return True
        return False

    def continuous_beats(self, now: float | None = None) -> float:
        """Continuous beat counter (can be fractional)."""
        now = time.monotonic() if now is None else now
        elapsed = now - self._anchor_t
        return self._phase_offset + elapsed * (self.bpm / 60.0)

    def state(self) -> dict:
        return {
            "bpm": round(self.bpm, 2),
            "phase": round(self.beat_phase(), 4),
            "beat_index": self.beat_index(),
        }
