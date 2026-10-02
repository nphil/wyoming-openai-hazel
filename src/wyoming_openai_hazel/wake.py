"""GPU wake hook: touch a file when a voice request starts.

A small root daemon on the GPU host can watch that file's modification time and wake a sleeping graphics card, so the
card is already awake when the audio arrives (the person is still talking for 1-3 s after Home Assistant opens the
request). This module only touches the file; what listens to it is up to you. Off unless ``HAZEL_GPU_WAKE_FILE`` is set.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Wyoming events that mean "a speech request is starting".
WAKE_EVENTS = frozenset({"transcribe", "audio-start", "synthesize", "synthesize-start"})


class WakeFile:
    def __init__(self, path: str, *, min_interval_s: float = 0.25, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = Path(path)
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._last_touch = float("-inf")
        self._last_warning = float("-inf")

    def touch(self, reason: str) -> bool:
        """Update the file's modification time (creating it if its folder exists). Never raises. True if touched."""
        now = self._clock()
        if now - self._last_touch < self._min_interval_s:
            return False
        self._last_touch = now
        try:
            self.path.touch(exist_ok=True)
        except OSError as exc:
            if now - self._last_warning >= 60:
                self._last_warning = now
                _LOGGER.warning("gpu-wake: cannot touch %s (%s) - continuing without the wake-up", self.path, exc)
            return False
        _LOGGER.info("gpu-wake: touched %s on %s", self.path, reason)
        return True
