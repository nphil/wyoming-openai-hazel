"""A tiny energy-based end-of-speech detector. No model, no dependencies.

It watches 16-bit PCM and reports two moments:

* ``Signal.LAUNCH``  - speech was heard and then ``silence_ms`` of real quiet followed: the request can start early.
* ``Signal.RESUMED`` - after a launch, sound came back for ``resume_ms``: that early request no longer covers the utterance.

Two thresholds are used on purpose. A frame is *loud* when it is clearly above the background noise (counts as speech) and
*quiet* when it is clearly at the background level (counts as silence). A frame in between - a soft word ending, a breath -
is neither, so it can never start an early request and always makes a started one stale. The noise floor follows the
quietest recent frame, the speech peak decays slowly, so no calibration is needed.
"""

from __future__ import annotations

import array
import math
import operator
import sys
from enum import Enum


class Signal(Enum):
    NONE = "none"
    LAUNCH = "launch"
    RESUMED = "resumed"


def rms(pcm: bytes, width: int) -> float:
    """Root-mean-square level of little-endian 16-bit PCM (0.0 for other widths or empty input)."""
    if width != 2 or len(pcm) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return math.sqrt(sum(map(operator.mul, samples, samples)) / len(samples))


class EndpointDetector:
    def __init__(self, *, silence_ms: float = 300.0, min_speech_ms: float = 250.0, resume_ms: float = 100.0,
                 max_passes: int = 3) -> None:
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.resume_ms = resume_ms
        self.max_passes = max_passes
        self.reset()

    def reset(self) -> None:
        self._noise_floor: float | None = None
        self._peak = 0.0
        self._loud_ms = 0.0
        self._quiet_ms = 0.0
        self._speech_seen = False
        self._pending = False
        self._active_since_launch_ms = 0.0
        self._launches = 0

    @property
    def covers_everything(self) -> bool:
        """True while the last launched early request still covers all audio heard so far."""
        return self._pending

    def feed(self, pcm: bytes, *, rate: int, width: int, channels: int) -> Signal:
        if not pcm or rate <= 0 or width <= 0 or channels <= 0:
            return Signal.NONE
        ms = len(pcm) / (rate * width * channels) * 1000.0
        level = rms(pcm, width)
        self._noise_floor = level if self._noise_floor is None else min(level, self._noise_floor * 1.002 + 0.2)
        self._peak = max(level, self._peak * 0.998)
        loud = level > max(2.5 * self._noise_floor + 30.0, 0.12 * self._peak, 100.0)
        quiet = level <= max(1.8 * self._noise_floor + 20.0, 0.05 * self._peak, 60.0)
        signal = Signal.NONE
        if loud:
            self._loud_ms += ms
            if self._loud_ms >= self.min_speech_ms:
                self._speech_seen = True
        if quiet:
            self._quiet_ms += ms
        else:
            self._quiet_ms = 0.0
            if self._pending:
                self._active_since_launch_ms += ms
                if self._active_since_launch_ms >= self.resume_ms:
                    self._pending = False
                    signal = Signal.RESUMED
        if (quiet and self._speech_seen and not self._pending and self._quiet_ms >= self.silence_ms
                and self._launches < self.max_passes):
            self._launches += 1
            self._pending = True
            self._active_since_launch_ms = 0.0
            signal = Signal.LAUNCH
        return signal
