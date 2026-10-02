"""Helpers for tests that run the handler inside the test process (no network, no subprocess).

They build a handler the way the bridge does (``make_handler_class(config)``), but hand it a fake speech-to-text client and
collect the events it writes instead of sending them to a socket.
"""

from __future__ import annotations

from typing import Any

from openai.types.audio import Transcription
from wyoming.event import Event
from wyoming.info import Info
from wyoming_openai.compatibility import (
    OpenAIBackend,
    create_asr_programs,
    create_info,
    create_tts_programs,
    create_tts_voices,
)
from wyoming_openai.handler import OpenAIEventHandler
from wyoming_openai_hazel.config import HazelConfig
from wyoming_openai_hazel.handler import make_handler_class

URL = "http://stub.invalid/v1"


def make_info(*, stt_models: tuple[str, ...] = ("whisper-1",), stt_streaming_models: tuple[str, ...] = (),
              tts_models: tuple[str, ...] = ("kokoro",), tts_streaming_models: tuple[str, ...] = ("kokoro",),
              voices: tuple[str, ...] = ("af_hazel", "af_heart"), languages: tuple[str, ...] = ("en",)) -> Info:
    """The Wyoming ``info`` the real bridge would build from the same settings."""
    asr = create_asr_programs(list(stt_models), list(stt_streaming_models), URL, list(languages))
    tts_voices = create_tts_voices(list(tts_models), list(tts_streaming_models), list(voices), URL, list(languages))
    return create_info(asr, create_tts_programs(tts_voices, list(tts_streaming_models)))


class FakeTranscriptions:
    """Records every call (and the audio it carried) and answers with ``text``."""

    def __init__(self, text: str = "fake transcript") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []
        self.wav: list[bytes] = []        # the audio each call carried, read at call time

    async def create(self, **kwargs: Any) -> Transcription:
        self.calls.append(kwargs)
        file = kwargs.get("file")
        self.wav.append(file.getvalue() if hasattr(file, "getvalue") else b"")
        return Transcription(text=self.text)


class FakeAudio:
    def __init__(self, transcriptions: FakeTranscriptions) -> None:
        self.transcriptions = transcriptions


class FakeSttClient:
    """Stands in for the OpenAI client: only what the handler touches."""

    backend = OpenAIBackend.OPENAI

    def __init__(self, text: str = "fake transcript") -> None:
        self.transcriptions = FakeTranscriptions(text)
        self.audio = FakeAudio(self.transcriptions)


class RecordedHandlerMixin:
    sent: list[Event]

    async def write_event(self, event: Event) -> None:   # type: ignore[override]
        self.sent.append(event)


def build_handler(config: HazelConfig | None = None, *, stock: bool = False, info: Info | None = None,
                  stt_client: Any = None, **kwargs: Any) -> OpenAIEventHandler:
    """A handler (ours, or the untouched upstream one with ``stock=True``) that records what it writes in ``.sent``."""
    base = OpenAIEventHandler if stock else make_handler_class(config or HazelConfig())
    handler_class = type("RecordedHandler", (RecordedHandlerMixin, base), {})
    handler = handler_class(None, None, info=info or make_info(), stt_client=stt_client, tts_client=None, **kwargs)
    handler.sent = []
    return handler
