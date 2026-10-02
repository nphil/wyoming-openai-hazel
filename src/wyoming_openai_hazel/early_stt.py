"""Early ("speculative") transcription.

Home Assistant decides the command is finished only after ~0.7 s of silence and only then sends ``audio-stop``; a stock
bridge starts the speech-to-text request at that moment. Here the request starts as soon as ``silence_ms`` (300 ms) of real
silence follows speech, using the audio received so far. When ``audio-stop`` arrives and nothing but silence came in between,
the finished (or nearly finished) answer is used - the answer is ready when Home Assistant asks for it. If sound came back, the
early answer is thrown away and the normal request runs exactly as upstream would run it, so the text can only differ from the
stock bridge for sounds quieter than the silence threshold (see detector.py).

A slow early request is never given up on: it is the one request this utterance needs, the stock bridge would wait for its own
request just as long, and sending the same audio again would only queue behind it on a busy speech server. Only an early request
that FAILED is replaced by the normal request.

``EarlyStt`` is the per-connection controller (pure asyncio, testable with a fake request function) and ``EarlyClient`` is a
thin stand-in for the OpenAI client that hands the early answer to upstream's own code at ``audio-stop``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .detector import EndpointDetector, Signal

_LOGGER = logging.getLogger(__name__)

AudioFormat = tuple[int, int, int]   # rate, width (bytes), channels


@dataclass(frozen=True)
class Taken:
    task: asyncio.Future
    lead_ms: float   # how long before audio-stop the early request started


class EarlyStt:
    def __init__(self, *, request: Callable[[bytes, AudioFormat], Awaitable[Any]], silence_ms: float = 300.0,
                 min_speech_ms: float = 250.0, resume_ms: float = 100.0, max_passes: int = 3,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self._request = request
        self._detector = EndpointDetector(silence_ms=silence_ms, min_speech_ms=min_speech_ms, resume_ms=resume_ms,
                                          max_passes=max_passes)
        self._clock = clock
        self._active = False
        self._fmt: AudioFormat = (16000, 2, 1)
        self._pcm = bytearray()
        self._task: asyncio.Future | None = None
        self._launched_at = 0.0

    # ----- per-utterance lifecycle -------------------------------------------------------------------------------
    def begin(self, fmt: AudioFormat) -> None:
        self._cancel()
        self._detector.reset()
        self._fmt = fmt
        self._pcm = bytearray()
        self._active = True

    def idle(self) -> None:
        """This utterance cannot use early transcription (e.g. streaming or realtime model)."""
        self._cancel()
        self._active = False

    def feed(self, audio: bytes) -> None:
        if not self._active or not audio:
            return
        self._pcm += audio
        try:
            rate, width, channels = self._fmt
            signal = self._detector.feed(audio, rate=rate, width=width, channels=channels)
        except Exception:   # never let the extra break the stock behaviour
            _LOGGER.exception("early-stt: detector failed, switching it off for this utterance")
            self.idle()
            return
        if signal is Signal.LAUNCH:
            self._launch()
        elif signal is Signal.RESUMED and self._task is not None:
            _LOGGER.info("early-stt: dropped, audio resumed %.0f ms after the early request started",
                         (self._clock() - self._launched_at) * 1000)
            self._cancel()

    def take(self) -> Taken | None:
        """At ``audio-stop``: the early request if it still covers everything that was heard, else None."""
        task = self._task
        if not self._active or task is None or not self._detector.covers_everything:
            self._cancel()
            return None
        self._task = None   # handed over: the caller owns it now
        return Taken(task, (self._clock() - self._launched_at) * 1000)

    def end(self) -> None:
        self._cancel()
        self._active = False

    # ----- internals ---------------------------------------------------------------------------------------------
    def _launch(self) -> None:
        pcm, fmt = bytes(self._pcm), self._fmt
        self._launched_at = self._clock()
        self._task = asyncio.ensure_future(self._request(pcm, fmt))
        self._task.add_done_callback(lambda t: t.cancelled() or t.exception())   # mark the error as seen
        _LOGGER.info("early-stt: request started after %.0f ms of silence (%.0f ms of audio so far)",
                     self._detector.silence_ms, len(pcm) / (fmt[0] * fmt[1] * fmt[2]) * 1000)

    def _cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None


class _Transcriptions:
    def __init__(self, real: Any, early: EarlyStt) -> None:
        self._real, self._early = real, early

    async def create(self, **kwargs: Any) -> Any:
        taken = self._early.take()
        if taken is not None:
            state = "already finished" if taken.task.done() else "still running"
            try:
                # No time limit and no shield: a request that is merely slow is waited for. The stock bridge would wait just as
                # long for its own request, and a second request for the same audio would only queue behind this one on a busy
                # speech server. If this handler is cancelled, asyncio cancels the awaited request with it: nothing is orphaned.
                result = await taken.task
            except Exception as exc:   # the early request FAILED - only then is the normal request sent instead
                _LOGGER.warning("early-stt: early request failed (%r) - transcribing normally", exc)
            else:
                _LOGGER.info("early-stt: used the early answer (request began %.0f ms before audio-stop, %s)",
                             taken.lead_ms, state)
                return result
        return await self._real.create(**kwargs)

    async def create_early(self, **kwargs: Any) -> Any:
        """The real request, used for the early pass itself."""
        return await self._real.create(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _Audio:
    def __init__(self, real: Any, early: EarlyStt) -> None:
        self._real = real
        self.transcriptions = _Transcriptions(real.transcriptions, early)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class EarlyClient:
    """Stands in for the OpenAI client held by the handler; everything but ``audio.transcriptions.create`` is forwarded."""

    def __init__(self, real: Any, early: EarlyStt) -> None:
        self._real = real
        self.audio = _Audio(real.audio, early)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)
