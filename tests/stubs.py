"""Stand-ins for the OpenAI-compatible speech servers (a Whisper server and a Kokoro server) that the bridge talks to.

Used by the test suite and by ``tools/smoke_test.py``. Needs only ``aiohttp`` and the standard library, so the smoke test can run
on a bare CI machine. Everything that happens is *recorded* (what was asked, when, how many at the same time), so a test can say
exactly what the bridge sent to the backend and in which order.

All times are ``time.monotonic()`` values, the same clock the Wyoming test client uses, so a test can compare "the backend got the
request" with "the client sent audio-stop" directly.
"""

from __future__ import annotations

import array
import asyncio
import io
import math
import socket
import struct
import time
import wave
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from aiohttp import web

TTS_RATE = 24000        # what Kokoro (and upstream's fallback) produces: 24 kHz, 16-bit, mono
TTS_WIDTH = 2
TTS_CHANNELS = 1
WAV_UNBOUNDED = 0xFFFFFFFF   # the "size unknown" marker a streaming WAV carries


def streaming_wav_header(rate: int = TTS_RATE, width: int = TTS_WIDTH, channels: int = TTS_CHANNELS) -> bytes:
    """A 44-byte WAV header whose RIFF and data sizes say "unknown", exactly as Kokoro-FastAPI streams it."""
    return (b"RIFF" + struct.pack("<I", WAV_UNBOUNDED) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * width * channels, width * channels, width * 8)
            + b"data" + struct.pack("<I", WAV_UNBOUNDED))


def _normalise(text: str) -> str:
    # The bridge may hand the backend "Hello there. " or "Hello there." - both must give the same audio.
    return " ".join(text.split())


def tts_frequency(text: str) -> int:
    """The tone (Hz) the stub "speaks" for this text. Different sentences give different tones, so order can be checked."""
    return 200 + zlib.crc32(_normalise(text).encode()) % 700


@lru_cache(maxsize=512)
def tts_pcm(text: str, *, n_chunks: int = 6, chunk_samples: int = 2400) -> bytes:
    """The PCM the stub returns for ``text``: a sine whose pitch is picked by the text, ``n_chunks * chunk_samples`` samples long."""
    freq = tts_frequency(text)
    total = n_chunks * chunk_samples
    samples = array.array("h", (int(8000 * math.sin(2 * math.pi * freq * i / TTS_RATE)) for i in range(total)))
    return samples.tobytes()


def read_wav(data: bytes) -> tuple[int, int, bytes]:
    """(sample frames, sample rate, raw PCM) of an uploaded WAV; (-1, -1, b"") if it is not a readable WAV."""
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            return reader.getnframes(), reader.getframerate(), reader.readframes(reader.getnframes())
    except (wave.Error, EOFError):
        return -1, -1, b""


@dataclass
class SttRequest:
    """One call to ``POST /v1/audio/transcriptions``."""

    index: int
    arrived: float                        # when the request reached the backend
    fields: dict[str, str]                # every plain form field: model, language, prompt, response_format, extra fields...
    filename: str | None
    content_type: str | None
    file_bytes: int
    samples: int                          # sample frames in the uploaded WAV
    rate: int
    pcm: bytes                            # the raw audio of the uploaded WAV
    status: int = 200
    answered: float | None = None         # when the answer was sent
    hung_up: bool = False                 # the caller went away before getting the answer (e.g. a dropped early request)
    text: str | None = None
    retry_count: str | None = None        # the SDK's "x-stainless-retry-count" header (set on automatic retries)


@dataclass
class TtsRequest:
    """One call to ``POST /v1/audio/speech``."""

    index: int
    arrived: float
    body: dict[str, Any]
    started: float | None = None          # when the first audio bytes were sent
    finished: float | None = None         # when the last audio bytes were sent
    cancelled: bool = False               # the caller hung up before the end
    concurrent_at_start: int = 0          # requests in flight when this one started (itself included)

    @property
    def input(self) -> str:
        return self.body.get("input", "")

    @property
    def voice(self) -> str | None:
        return self.body.get("voice")

    @property
    def speed(self) -> float | None:
        return self.body.get("speed")

    @property
    def response_format(self) -> str | None:
        return self.body.get("response_format")

    @property
    def model(self) -> str | None:
        return self.body.get("model")


def default_stt_text(request: SttRequest) -> str:
    """The stub's "transcript": it states how much audio it got, so tests can tell which audio was transcribed."""
    return f"heard {request.samples} samples"


@dataclass
class StubBackend:
    """An HTTP server on 127.0.0.1 (random port) that imitates a Whisper server and a Kokoro server.

    ``stt_delay_s``     how long a transcription takes
    ``stt_text``        function ``SttRequest -> str`` for the transcript (default: "heard N samples")
    ``stt_fail_first``  answer the first N transcription requests with an HTTP error (``stt_fail_status``)
    ``n_chunks``        pieces a speech answer is streamed in; ``chunk_delay_s`` pause between pieces
    ``chunk_samples``   24 kHz samples per piece (2400 = 100 ms; must stay above 1024 so one piece fills the bridge's read size)
    ``first_chunk_delay_s`` pause before the first piece (time-to-first-audio of a real TTS server)
    """

    stt_delay_s: float = 0.0
    stt_text: Callable[[SttRequest], str] = default_stt_text
    stt_fail_first: int = 0
    stt_fail_status: int = 500
    n_chunks: int = 6
    chunk_delay_s: float = 0.1
    chunk_samples: int = 2400
    first_chunk_delay_s: float = 0.0

    stt_requests: list[SttRequest] = field(default_factory=list)
    tts_requests: list[TtsRequest] = field(default_factory=list)
    tts_in_flight: int = 0
    tts_max_in_flight: int = 0
    port: int = 0

    def __post_init__(self) -> None:
        self._runner: web.AppRunner | None = None
        self._stt_failed = 0
        app = web.Application(client_max_size=256 * 1024 * 1024)
        app.router.add_post("/v1/audio/transcriptions", self._transcriptions)
        app.router.add_post("/v1/audio/speech", self._speech)
        self._app = app

    # ----- lifecycle ---------------------------------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        """What goes into ``STT_OPENAI_URL`` / ``TTS_OPENAI_URL``."""
        return f"http://127.0.0.1:{self.port}/v1"

    async def start(self) -> StubBackend:
        # handler_cancellation: when the bridge drops a request (hangs up), the stub handler is cancelled too, like a real server.
        self._runner = web.AppRunner(self._app, handler_cancellation=True, access_log=None)
        await self._runner.setup()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        await web.SockSite(self._runner, sock).start()
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def __aenter__(self) -> StubBackend:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def reset(self) -> None:
        """Forget what was recorded (the server keeps running)."""
        self.stt_requests.clear()
        self.tts_requests.clear()
        self.tts_in_flight = 0
        self.tts_max_in_flight = 0
        self._stt_failed = 0

    # ----- speech to text ----------------------------------------------------------------------------------------
    @property
    def stt_ok(self) -> list[SttRequest]:
        """Transcription requests that were answered successfully (in arrival order)."""
        return [r for r in self.stt_requests if r.status == 200]

    async def _transcriptions(self, request: web.Request) -> web.StreamResponse:
        arrived = time.monotonic()
        form = await request.post()
        fields: dict[str, str] = {}
        upload: Any = None
        for key in form:
            value = form[key]
            if isinstance(value, web.FileField):
                upload = value
            else:
                fields[key] = str(value)
        data = upload.file.read() if upload is not None else b""
        samples, rate, pcm = read_wav(data)
        record = SttRequest(
            index=len(self.stt_requests), arrived=arrived, fields=fields,
            filename=upload.filename if upload is not None else None,
            content_type=upload.content_type if upload is not None else None,
            file_bytes=len(data), samples=samples, rate=rate, pcm=pcm,
            retry_count=request.headers.get("x-stainless-retry-count"),
        )
        self.stt_requests.append(record)

        if self._stt_failed < self.stt_fail_first:
            self._stt_failed += 1
            record.status = self.stt_fail_status
            record.answered = time.monotonic()
            return web.json_response({"error": {"message": "stub: failing on purpose", "type": "stub_error"}},
                                     status=record.status)
        try:
            if self.stt_delay_s:
                await asyncio.sleep(self.stt_delay_s)
        except asyncio.CancelledError:
            record.hung_up = True
            raise
        record.text = self.stt_text(record)
        record.answered = time.monotonic()
        return web.json_response({"text": record.text})

    # ----- text to speech ----------------------------------------------------------------------------------------
    async def _speech(self, request: web.Request) -> web.StreamResponse:
        record = TtsRequest(index=len(self.tts_requests), arrived=time.monotonic(), body=await request.json())
        self.tts_requests.append(record)
        self.tts_in_flight += 1
        self.tts_max_in_flight = max(self.tts_max_in_flight, self.tts_in_flight)
        record.concurrent_at_start = self.tts_in_flight
        try:
            pcm = tts_pcm(record.input, n_chunks=self.n_chunks, chunk_samples=self.chunk_samples)
            piece_bytes = self.chunk_samples * TTS_WIDTH
            response = web.StreamResponse(headers={"Content-Type": "audio/wav"})
            await response.prepare(request)
            if self.first_chunk_delay_s:
                await asyncio.sleep(self.first_chunk_delay_s)
            record.started = time.monotonic()
            await response.write(streaming_wav_header())
            for i in range(self.n_chunks):
                if i:
                    await asyncio.sleep(self.chunk_delay_s)
                await response.write(pcm[i * piece_bytes:(i + 1) * piece_bytes])
            record.finished = time.monotonic()
            await response.write_eof()
            return response
        except (asyncio.CancelledError, ConnectionResetError):
            record.cancelled = True
            raise
        finally:
            self.tts_in_flight -= 1
