"""A small Wyoming client that talks to the bridge the way Home Assistant does, and notes *when* things happen.

Used by the test suite and by ``tools/smoke_test.py``. Needs only the ``wyoming`` package. All times are ``time.monotonic()``.

What Home Assistant does (checked against its ``wyoming`` integration):
* speech-to-text: ``transcribe`` (language only), ``audio-start`` (16 kHz, 16-bit, mono), ``audio-chunk`` ..., ``audio-stop``,
  then it waits for the ``transcript`` event;
* text-to-speech with a streaming voice: ``synthesize-start`` (voice), ``synthesize-chunk`` per piece of text, then the whole text
  once more as a plain ``synthesize`` event "for backwards compatibility", then ``synthesize-stop``; it plays ``audio-chunk``
  events until ``synthesize-stopped``.
"""

from __future__ import annotations

import array
import asyncio
import math
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize, SynthesizeChunk, SynthesizeStart, SynthesizeStop, SynthesizeStopped, SynthesizeVoice

RATE = 16000      # what Home Assistant sends for speech-to-text
WIDTH = 2
CHANNELS = 1
BYTES_PER_SECOND = RATE * WIDTH * CHANNELS
READ_TIMEOUT_S = 30.0   # generous: the test machine may be busy


# ----- audio made up for tests ---------------------------------------------------------------------------------------
def samples_of(pcm: bytes) -> int:
    """Number of 16-bit mono samples in ``pcm``."""
    return len(pcm) // WIDTH


def silence(ms: float, *, noise: int = 0, seed: int = 0) -> bytes:
    """Quiet audio: digital zeros, or (``noise`` > 0) random hiss of that peak size, like a microphone's noise floor."""
    n = round(RATE * ms / 1000)
    if not noise:
        return bytes(n * WIDTH)
    rng = random.Random(seed)
    return array.array("h", (rng.randint(-noise, noise) for _ in range(n))).tobytes()


def speech(ms: float, *, amplitude: int = 7000, start_ms: float = 0.0) -> bytes:
    """Loud voice-like audio: a vowel-ish mix of harmonics that swells and dips like syllables (never reaching silence).

    ``start_ms`` continues the waveform smoothly when one sentence is made from several calls.
    """
    n = round(RATE * ms / 1000)
    first = round(RATE * start_ms / 1000)
    out = array.array("h")
    for i in range(first, first + n):
        t = i / RATE
        envelope = 0.65 + 0.35 * math.sin(2 * math.pi * 3.0 * t)     # 3 syllables per second, never below 30 %
        wave_ = (math.sin(2 * math.pi * 140 * t) + 0.5 * math.sin(2 * math.pi * 280 * t)
                 + 0.3 * math.sin(2 * math.pi * 420 * t) + 0.2 * math.sin(2 * math.pi * 840 * t)) / 2.0
        out.append(int(amplitude * envelope * wave_))
    return out.tobytes()


def tone(ms: float, amplitude: int, freq: float = 440.0) -> bytes:
    """A steady sine wave. ``amplitude`` 500 is ~350 rms: audible, but far below speech."""
    n = round(RATE * ms / 1000)
    return array.array("h", (int(amplitude * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n))).tobytes()


def hiss(ms: float, amplitude: int = 300, seed: int = 1) -> bytes:
    """Steady random noise (a fan, a TV in the next room): no speech, but never quiet either."""
    return silence(ms, noise=amplitude, seed=seed)


def chunked(pcm: bytes, chunk_ms: float = 20.0, *, bytes_per_second: int = BYTES_PER_SECOND) -> list[bytes]:
    """Cut audio into the small pieces a satellite sends (default 20 ms = 640 bytes)."""
    size = max(WIDTH, int(bytes_per_second * chunk_ms / 1000) // WIDTH * WIDTH)
    return [pcm[i:i + size] for i in range(0, len(pcm), size)]


# ----- results -------------------------------------------------------------------------------------------------------
@dataclass
class Stamps:
    """When the interesting things of one speech-to-text exchange happened (``time.monotonic()``)."""

    audio_start_sent: float = 0.0
    audio_stop_sent: float = 0.0
    transcript_received: float | None = None
    events: list[tuple[float, Event]] = field(default_factory=list)   # every event the bridge sent back, with arrival time

    @property
    def latency_s(self) -> float:
        """Seconds from sending ``audio-stop`` to receiving the transcript - what the user waits for."""
        assert self.transcript_received is not None, "no transcript was received"
        return self.transcript_received - self.audio_stop_sent

    @property
    def event_types(self) -> list[str]:
        return [e.type for _, e in self.events]


@dataclass
class Synthesis:
    """What came back for one text-to-speech request."""

    events: list[tuple[float, Event]] = field(default_factory=list)
    chunks: list[tuple[float, AudioChunk]] = field(default_factory=list)     # (arrival time, chunk) in arrival order
    audio_start: AudioStart | None = None
    audio_stopped: bool = False          # an ``audio-stop`` event arrived
    synthesize_stopped: bool = False     # a ``synthesize-stopped`` event arrived
    request_sent: float = 0.0            # when the last text was sent
    done: float = 0.0

    @property
    def pcm(self) -> bytes:
        return b"".join(chunk.audio for _, chunk in self.chunks)

    @property
    def first_chunk_at(self) -> float:
        return self.chunks[0][0]

    @property
    def last_chunk_at(self) -> float:
        return self.chunks[-1][0]

    @property
    def event_types(self) -> list[str]:
        return [e.type for _, e in self.events]


class _Client(AsyncTcpClient):
    async def disconnect(self) -> None:
        try:
            await super().disconnect()
        except OSError:
            pass   # the bridge had already closed (or reset) the connection: nothing is left to close


class WyomingClient:
    """One instance per bridge; every call opens its own connection, like Home Assistant does."""

    def __init__(self, host: str, port: int, *, read_timeout: float = READ_TIMEOUT_S) -> None:
        self.host, self.port, self.read_timeout = host, port, read_timeout

    def connect(self) -> AsyncTcpClient:
        return _Client(self.host, self.port)

    async def _read(self, client: AsyncTcpClient) -> Event:
        event = await asyncio.wait_for(client.read_event(), self.read_timeout)
        if event is None:
            raise ConnectionError("the bridge closed the connection")
        return event

    # ----- info --------------------------------------------------------------------------------------------------
    async def describe(self) -> Info:
        async with self.connect() as client:
            await client.write_event(Describe().event())
            while True:
                event = await self._read(client)
                if Info.is_type(event.type):
                    return Info.from_event(event)

    # ----- speech to text ----------------------------------------------------------------------------------------
    async def transcribe(self, audio: bytes | Iterable[bytes], *, realtime: bool = True, language: str | None = "en",
                         name: str | None = None, chunk_ms: float = 20.0, read_to_end: bool = True) -> tuple[str, Stamps]:
        """Send an utterance and return ``(transcript text, Stamps)``.

        ``realtime=True`` paces the chunks like a live microphone (the bridge hears silence for as long as it lasts).
        ``realtime=False`` sends everything as fast as possible.
        Home Assistant hangs up at the ``transcript`` event. Reading on to ``transcript-stop`` (``read_to_end=True``) gives the
        whole answer in ``Stamps.events`` and a clean goodbye - hanging up with data still on the way makes the TCP stack reset the
        connection, which the bridge's framework (wyoming) then logs as an error, noise that would hide real problems.
        """
        chunks = chunked(audio, chunk_ms) if isinstance(audio, (bytes, bytearray)) else list(audio)
        stamps = Stamps()
        text = ""
        async with self.connect() as client:
            await client.write_event(Transcribe(name=name, language=language).event())
            await client.write_event(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
            stamps.audio_start_sent = due = time.monotonic()
            for chunk in chunks:
                await client.write_event(AudioChunk(rate=RATE, width=WIDTH, channels=CHANNELS, audio=chunk).event())
                if realtime:
                    due += len(chunk) / BYTES_PER_SECOND
                    pause = due - time.monotonic()
                    if pause > 0:
                        await asyncio.sleep(pause)
            stamps.audio_stop_sent = time.monotonic()
            await client.write_event(AudioStop().event())
            while True:
                event = await self._read(client)
                now = time.monotonic()
                stamps.events.append((now, event))
                if Transcript.is_type(event.type):
                    stamps.transcript_received = now
                    text = Transcript.from_event(event).text
                    if not read_to_end:
                        return text, stamps
                elif event.type == "transcript-stop":
                    return text, stamps

    # ----- text to speech ----------------------------------------------------------------------------------------
    async def synthesize(self, text: str | list[str], voice: str | None = "af_hazel", *, streaming: bool = True,
                         compat_synthesize: bool = True, piece_delay_s: float = 0.0) -> Synthesis:
        """Ask for speech and collect the audio, noting when every chunk arrived.

        ``text`` as a list = the pieces of text to send one by one (``piece_delay_s`` apart); a plain string is sent whole.
        ``streaming=True``: synthesize-start / synthesize-chunk / synthesize-stop (what Home Assistant does for a streaming voice;
        ``compat_synthesize`` adds the extra whole-text ``synthesize`` event it sends before the stop).
        ``streaming=False``: one plain ``synthesize`` event, audio until ``audio-stop``.
        """
        pieces = [text] if isinstance(text, str) else list(text)
        whole = "".join(pieces)
        selected = SynthesizeVoice(name=voice) if voice else None
        result = Synthesis()

        async def write_request(client: AsyncTcpClient) -> None:
            if streaming:
                await client.write_event(SynthesizeStart(voice=selected).event())
                for i, piece in enumerate(pieces):
                    if i and piece_delay_s:
                        await asyncio.sleep(piece_delay_s)
                    await client.write_event(SynthesizeChunk(text=piece).event())
                if compat_synthesize:
                    await client.write_event(Synthesize(text=whole, voice=selected).event())
                result.request_sent = time.monotonic()
                await client.write_event(SynthesizeStop().event())
            else:
                result.request_sent = time.monotonic()
                await client.write_event(Synthesize(text=whole, voice=selected).event())

        async with self.connect() as client:
            # Home Assistant writes the text and plays the audio at the same time (two tasks), so audio that starts while
            # text is still being sent is seen the moment it arrives.
            writer = asyncio.ensure_future(write_request(client))
            try:
                while True:
                    event = await self._read(client)
                    now = time.monotonic()
                    result.events.append((now, event))
                    if AudioStart.is_type(event.type):
                        result.audio_start = AudioStart.from_event(event)
                    elif AudioChunk.is_type(event.type):
                        result.chunks.append((now, AudioChunk.from_event(event)))
                    elif AudioStop.is_type(event.type):
                        result.audio_stopped = True
                        if not streaming:
                            break
                    elif SynthesizeStopped.is_type(event.type):
                        result.synthesize_stopped = True
                        break
                await writer
            finally:
                writer.cancel()
        result.done = time.monotonic()
        return result

    # ----- anything else -----------------------------------------------------------------------------------------
    async def send(self, *events: Event, timeout_s: float = 20.0) -> list[Event]:
        """Write ``events`` on a new connection, say "nothing more is coming", and return everything the bridge answers.

        Closing our sending side (a TCP half-close) lets the bridge finish the events in order and then end the conversation by
        itself, so when this returns the bridge has handled every event, and nothing is left unread (which would make the TCP stack
        reset the connection and the bridge's framework log that as an error).
        """
        replies: list[Event] = []
        async with self.connect() as client:
            try:
                for event in events:
                    await client.write_event(event)
                writer = client._writer
                if writer is not None and writer.can_write_eof():
                    writer.write_eof()
            except OSError:
                pass   # the bridge already hung up on an earlier event; whatever it answered is still worth reading
            deadline = time.monotonic() + timeout_s
            while (left := deadline - time.monotonic()) > 0:
                try:
                    event = await asyncio.wait_for(client.read_event(), left)
                except (asyncio.TimeoutError, OSError):
                    break
                if event is None:
                    break
                replies.append(event)
        return replies
