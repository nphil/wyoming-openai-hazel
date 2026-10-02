"""The upstream event handler plus the opt-in extras. Each extra is a few lines that hook one clearly named place."""

from __future__ import annotations

import asyncio
import logging
import time
import wave
from typing import Any, ClassVar

from openai import omit
from wyoming.event import Event
from wyoming_openai.handler import OpenAIEventHandler
from wyoming_openai.utilities import NamedBytesIO

from .config import HazelConfig
from .early_stt import AudioFormat, EarlyClient, EarlyStt
from .labels import apply_voice_labels
from .wake import WAKE_EVENTS, WakeFile

_LOGGER = logging.getLogger(__name__)


class HazelEventHandler(OpenAIEventHandler):
    hazel: ClassVar[HazelConfig] = HazelConfig()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cfg = self.hazel
        self._hazel_wake = WakeFile(cfg.gpu_wake_file) if cfg.gpu_wake_file else None
        self._hazel_early: EarlyStt | None = None
        self._hazel_tts_started: float | None = None
        self._hazel_tts_logged = False
        self._hazel_real_transcriptions: Any = None
        self._safely("tts-concurrency", self._init_tts_concurrency)
        self._safely("voice-labels", self._init_voice_labels)
        self._safely("early-stt", self._init_early_stt)

    def _safely(self, name: str, init: Any) -> None:
        try:
            init()
        except Exception:
            _LOGGER.exception("%s could not be set up and is switched off; the stock bridge behaviour is used", name)

    # ----- TTS concurrency -----------------------------------------------------------------------------------------
    def _init_tts_concurrency(self) -> None:
        if self.hazel.tts_concurrency is not None:
            self._tts_semaphore = asyncio.Semaphore(self.hazel.tts_concurrency)

    # ----- voice display names -------------------------------------------------------------------------------------
    def _init_voice_labels(self) -> None:
        if self.hazel.voice_labels:
            apply_voice_labels(self._wyoming_info, self.hazel.voice_labels)

    # ----- early transcription -------------------------------------------------------------------------------------
    def _init_early_stt(self) -> None:
        cfg = self.hazel
        if not cfg.early_stt or self._stt_client is None:
            return
        early = EarlyStt(request=self._early_request, silence_ms=cfg.early_stt_silence_ms,
                         min_speech_ms=cfg.early_stt_min_speech_ms, resume_ms=cfg.early_stt_resume_ms,
                         max_passes=cfg.early_stt_max_passes)
        client = EarlyClient(self._stt_client, early)
        self._hazel_early = early
        self._hazel_real_transcriptions = client.audio.transcriptions
        self._stt_client = client

    def _early_applicable(self) -> bool:
        model = self._current_asr_model
        if model is None or self._wav_buffer is None or self._stt_client is None:
            return False
        if self._is_asr_model_streaming(model.name) or self._is_asr_model_realtime(model.name):
            return False
        body = self._get_stt_extra_body() or {}
        return not body.get("stream")

    async def _early_request(self, pcm: bytes, fmt: AudioFormat) -> Any:
        """The same transcription request upstream sends at audio-stop, for the audio heard so far."""
        rate, width, channels = fmt
        wav = NamedBytesIO(name="recording.wav")
        with wave.open(wav, "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(width)
            writer.setframerate(rate)
            writer.writeframes(pcm)
        wav.seek(0)
        kwargs: dict[str, Any] = {
            "file": wav,
            "model": self._current_asr_model.name,
            "language": self._current_language if self._current_language is not None else omit,
            "temperature": self._stt_temperature if self._stt_temperature is not None else omit,
            "prompt": self._stt_prompt if self._stt_prompt is not None else omit,
            "response_format": "json",
        }
        extra_body = self._get_stt_extra_body()
        if extra_body:
            kwargs["extra_body"] = extra_body
        return await self._hazel_real_transcriptions.create_early(**kwargs)

    async def _handle_audio_start(self, sample_rate: int, audio_width: int, audio_channels: int) -> None:
        await super()._handle_audio_start(sample_rate, audio_width, audio_channels)
        early = self._hazel_early
        if early is None:
            return
        try:
            if self._early_applicable():
                early.begin((sample_rate, audio_width, audio_channels))
            else:
                early.idle()
        except Exception:
            _LOGGER.exception("early-stt: could not start for this utterance")
            early.idle()

    async def _handle_audio_chunk(self, chunk: Any) -> None:
        await super()._handle_audio_chunk(chunk)
        if self._hazel_early is not None:
            self._hazel_early.feed(chunk.audio)

    async def _handle_audio_stop(self) -> None:
        started = time.perf_counter()
        try:
            await super()._handle_audio_stop()
        finally:
            if self._hazel_early is not None:
                self._hazel_early.end()
            if self.hazel.log_timing:
                _LOGGER.info("stt audio-stop -> transcript written: %.0f ms", (time.perf_counter() - started) * 1000)

    # ----- events (GPU wake, timing) -------------------------------------------------------------------------------
    async def handle_event(self, event: Event) -> bool:
        if self._hazel_wake is not None and event.type in WAKE_EVENTS:
            self._hazel_wake.touch(event.type)
        if event.type in ("synthesize", "synthesize-start"):
            self._hazel_tts_started = time.perf_counter()
            self._hazel_tts_logged = False
        return await super().handle_event(event)

    async def write_event(self, event: Event) -> None:
        if (self.hazel.log_timing and event.type == "audio-chunk" and self._hazel_tts_started is not None
                and not self._hazel_tts_logged):
            self._hazel_tts_logged = True
            _LOGGER.info("tts first-audio %.0f ms after the request started",
                         (time.perf_counter() - self._hazel_tts_started) * 1000)
        await super().write_event(event)


def make_handler_class(config: HazelConfig, base: type[OpenAIEventHandler] = HazelEventHandler) -> type[OpenAIEventHandler]:
    """A handler class bound to ``config`` (the bridge builds one handler per connection from the class it is given)."""
    return type("HazelEventHandler", (base,), {"hazel": config})
