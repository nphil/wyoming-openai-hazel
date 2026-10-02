"""Early transcription (HAZEL_STT_EARLY).

Home Assistant sends ``audio-stop`` only after ~0.7 s of silence; a stock bridge starts the speech-to-text request at that moment.
With the extra, the request starts as soon as 300 ms of real silence follows speech, and the finished answer is used at
``audio-stop`` if nothing but silence arrived in between.

Part 1 talks to the real bridge process with a live-paced "microphone". Part 2 tests the end-of-speech detector, part 3 the
per-utterance controller, part 4 the stand-in client, part 5 the handler - all in-process, no network.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import wave

import openai
import pytest
from inproc import FakeSttClient, build_handler, make_info
from openai import omit
from wyoming.asr import Transcribe
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming_client import RATE, chunked, hiss, samples_of, silence, speech, tone
from wyoming_openai_hazel.config import HazelConfig
from wyoming_openai_hazel.detector import EndpointDetector, Signal, rms
from wyoming_openai_hazel.early_stt import EarlyClient, EarlyStt, Taken

LEAD = silence(200)                 # a satellite streams a little before the person starts to speak
WORDS = speech(1000)
TAIL = silence(1000)                # the quiet before Home Assistant decides the command is over and sends audio-stop
FULL = LEAD + WORDS + TAIL          # 2.2 s = 35200 samples; the early request goes out 300 ms into the tail
SPEECH_ENDS = samples_of(LEAD + WORDS)
STOCK_BRIDGE = [sys.executable, "-m", "wyoming_openai"]


# =====================================================================================================================
# Part 1 - through the real bridge
# =====================================================================================================================
async def test_the_request_is_sent_before_audio_stop_and_its_answer_is_used(bridge, backend) -> None:
    backend.stt_delay_s = 0.3
    b = await bridge(HAZEL_STT_EARLY="1")
    text, stamps = await b.client().transcribe(FULL)

    assert len(backend.stt_requests) == 1, "expected exactly one request to the speech-to-text server"
    request = backend.stt_requests[0]
    assert stamps.audio_stop_sent - request.arrived >= 0.3, "the request did not start before audio-stop was sent"
    assert stamps.latency_s < 0.25, f"the transcript came {stamps.latency_s:.3f} s after audio-stop although the answer was ready"
    # The request carried the speech and the 300 ms of silence that triggered it - the first part of what was said.
    assert FULL.startswith(request.pcm)
    assert SPEECH_ENDS + 0.25 * RATE <= request.samples <= SPEECH_ENDS + 0.6 * RATE
    assert text == f"heard {request.samples} samples"                 # the transcript is that request's answer
    assert text != f"heard {samples_of(FULL)} samples"
    await b.wait_for_log("early-stt: used the early answer")


async def test_without_the_setting_nothing_is_sent_early(bridge, backend) -> None:
    backend.stt_delay_s = 0.3
    for setting in (None, "0"):
        backend.reset()
        b = await bridge(HAZEL_STT_EARLY=setting)
        text, stamps = await b.client().transcribe(FULL)
        assert len(backend.stt_requests) == 1
        request = backend.stt_requests[0]
        assert request.arrived >= stamps.audio_stop_sent, f"HAZEL_STT_EARLY={setting!r}: a request was sent before audio-stop"
        assert request.pcm == FULL
        assert text == f"heard {samples_of(FULL)} samples"
        assert stamps.latency_s >= 0.25, "the user should wait for the whole request when nothing is sent early"
        assert "early-stt" not in b.stderr


NOTHING_TO_SEND_EARLY = {
    "digital silence": silence(1300),
    "microphone hiss only": silence(1300, noise=40),
    "steady loud noise (a fan)": hiss(1500, 300),
    "speech without a pause": LEAD + speech(1500),
    "a short blip and then quiet": LEAD + speech(200) + silence(1100),
}


async def test_nothing_is_sent_early_when_there_was_no_speech_followed_by_quiet(bridge, backend) -> None:
    b = await bridge(HAZEL_STT_EARLY="1")
    for label, audio in NOTHING_TO_SEND_EARLY.items():
        backend.reset()
        text, stamps = await b.client().transcribe(audio)
        assert len(backend.stt_requests) == 1, f"{label}: {len(backend.stt_requests)} requests"
        request = backend.stt_requests[0]
        assert request.arrived >= stamps.audio_stop_sent, f"{label}: a request was sent before audio-stop"
        assert request.pcm == audio and text == f"heard {samples_of(audio)} samples", label
    assert not b.lines_matching("early-stt: request started")


async def test_speech_that_continues_after_a_short_pause_is_never_cut_off(bridge, backend) -> None:
    backend.stt_delay_s = 1.0          # the dropped request is still running when the speaker carries on, even on a slow machine
    b = await bridge(HAZEL_STT_EARLY="1")
    audio = LEAD + WORDS + silence(350) + speech(800) + silence(200)       # the second pause is shorter than 300 ms
    text, stamps = await b.client().transcribe(audio)

    assert len(backend.stt_requests) == 2
    first, last = backend.stt_requests
    assert first.arrived < stamps.audio_stop_sent - 0.3, "an early request should have been sent at the first pause"
    assert first.samples < samples_of(audio) and first.hung_up, "the early request should have been dropped"
    assert last.arrived >= stamps.audio_stop_sent, "the answer must come from a request made after audio-stop"
    assert last.pcm == audio, "the final request must carry ALL of the audio"
    assert text == f"heard {samples_of(audio)} samples"
    await b.wait_for_log("early-stt: dropped, audio resumed")


async def test_a_second_pause_starts_a_second_early_request_that_covers_the_whole_command(bridge, backend) -> None:
    backend.stt_delay_s = 0.3
    b = await bridge(HAZEL_STT_EARLY="1")
    spoken = LEAD + WORDS + silence(350) + speech(800)
    audio = spoken + silence(1000)
    text, stamps = await b.client().transcribe(audio)

    assert len(backend.stt_requests) == 2
    first, second = backend.stt_requests
    assert second.samples > first.samples
    assert second.samples >= samples_of(spoken) + 0.25 * RATE, "the second request misses the second half of the command"
    assert second.arrived < stamps.audio_stop_sent - 0.3 and audio.startswith(second.pcm)
    assert text == f"heard {second.samples} samples"                   # the stale first answer was not used
    assert stamps.latency_s < 0.25


@pytest.mark.parametrize(("status", "failures"), [
    pytest.param(500, openai.DEFAULT_MAX_RETRIES + 1, id="server-error-on-every-attempt"),
    pytest.param(400, 1, id="request-refused"),
])
async def test_when_the_early_request_fails_the_normal_request_still_delivers(bridge, backend, status, failures) -> None:
    backend.stt_fail_first, backend.stt_fail_status = failures, status     # the SDK retries server errors itself: fail all attempts
    b = await bridge(HAZEL_STT_EARLY="1")
    text, stamps = await b.client().transcribe(FULL)

    failed = [r for r in backend.stt_requests if r.status != 200]
    answered = [r for r in backend.stt_requests if r.status == 200]
    assert len(failed) == failures and len(answered) == 1
    assert failed[0].arrived < stamps.audio_stop_sent - 0.3, "the failing request was not an early one"
    assert answered[0].arrived >= stamps.audio_stop_sent, "the answer should come from the normal request, sent at audio-stop"
    assert answered[0].pcm == FULL
    assert text == f"heard {samples_of(FULL)} samples"
    await b.wait_for_log("early-stt: early answer unusable")


async def test_a_server_error_that_the_sdk_retries_is_invisible(bridge, backend) -> None:
    backend.stt_fail_first = 1                     # one 500: the SDK's own retry inside the early request succeeds
    b = await bridge(HAZEL_STT_EARLY="1")
    text, _ = await b.client().transcribe(FULL)
    answered = [r for r in backend.stt_requests if r.status == 200]
    assert len(backend.stt_requests) == 2 and len(answered) == 1
    assert answered[0].samples < samples_of(FULL), "the answer should come from the early request"
    assert text == f"heard {answered[0].samples} samples"
    assert not b.lines_matching("early answer unusable")


async def test_the_early_and_the_normal_request_carry_identical_settings(bridge, backend) -> None:
    backend.stt_delay_s = 0.6
    env = {"STT_PROMPT": "top-level prompt", "STT_TEMPERATURE": "0.2",
           "STT_EXTRA_BODY": json.dumps({"prompt": "Nitin's Office, Poobot", "hotwords": "Poobot", "vad_filter": True})}
    audio = LEAD + WORDS + silence(350) + speech(800) + silence(200)       # => an early request, dropped, then the normal one
    b = await bridge(HAZEL_STT_EARLY="1", **env)
    await b.client().transcribe(audio, language="en")
    assert len(backend.stt_requests) == 2
    early, normal = backend.stt_requests
    assert early.arrived < normal.arrived and early.samples < normal.samples

    assert early.fields == normal.fields, "the early request must ask for exactly what the normal one asks for"
    assert early.filename == normal.filename == "recording.wav" and early.content_type == normal.content_type
    assert normal.fields["model"] == "whisper-1" and normal.fields["language"] == "en"
    assert normal.fields["response_format"] == "json" and normal.fields["temperature"] == "0.2"
    assert normal.fields["prompt"] == "Nitin's Office, Poobot" and normal.fields["hotwords"] == "Poobot"

    backend.reset()                                # ...and both are what the stock bridge sends
    stock = await bridge(args=STOCK_BRIDGE, **env)
    await stock.client().transcribe(audio, language="en")
    assert [r.fields for r in backend.stt_requests] == [normal.fields]


async def test_the_silence_setting_decides_how_early_the_request_goes_out(bridge, backend) -> None:
    backend.stt_delay_s = 0.2
    b = await bridge(HAZEL_STT_EARLY="1", HAZEL_STT_EARLY_SILENCE_MS="600")
    text, stamps = await b.client().transcribe(FULL)

    (request,) = backend.stt_requests
    lead = stamps.audio_stop_sent - request.arrived
    assert 0.1 <= lead <= 0.55, f"with 600 ms of silence needed, the request should go out ~0.4 s before audio-stop, not {lead:.2f} s"
    assert SPEECH_ENDS + 0.55 * RATE <= request.samples <= SPEECH_ENDS + 0.9 * RATE      # 600 ms of silence went with it
    assert text == f"heard {request.samples} samples"
    assert "early-stt(silence 600 ms)" in b.stderr


async def test_the_cap_on_early_requests_per_command_is_honoured(bridge, backend) -> None:
    backend.stt_delay_s = 0.2
    b = await bridge(HAZEL_STT_EARLY="1", HAZEL_STT_EARLY_MAX_PASSES="1")
    audio = LEAD + WORDS + silence(350) + speech(800) + silence(1000)         # two pauses; with the default both would start a request
    text, stamps = await b.client().transcribe(audio)

    assert len(backend.stt_requests) == 2
    first, last = backend.stt_requests
    assert first.arrived < stamps.audio_stop_sent - 0.3
    assert last.arrived >= stamps.audio_stop_sent, "the second pause must not start another early request"
    assert last.pcm == audio and text == f"heard {samples_of(audio)} samples"


async def test_two_commands_at_the_same_time_do_not_mix_up_their_answers(bridge, backend) -> None:
    backend.stt_delay_s = 0.3
    b = await bridge(HAZEL_STT_EARLY="1")
    simple = LEAD + WORDS + TAIL                                                  # one pause -> one early request
    tricky = LEAD + speech(600) + silence(350) + speech(500) + silence(900)       # a dropped early request, then a second one
    (text_simple, _), (text_tricky, _) = await asyncio.gather(b.client().transcribe(simple), b.client().transcribe(tricky))

    assert text_simple == "heard 24000 samples"                                    # speech + the 300 ms that started the request
    heard = int(text_tricky.split()[1])
    assert samples_of(LEAD + speech(600) + silence(350) + speech(500)) + 0.25 * RATE <= heard <= samples_of(tricky), text_tricky
    assert len(backend.stt_requests) == 3                                          # 1 + 2, none of them lost or doubled


# =====================================================================================================================
# Part 2 - the end-of-speech detector
# =====================================================================================================================
def run_detector(pcm: bytes, *, chunk_ms: float = 20.0, width: int = 2, channels: int = 1, rate: int = RATE,
                 **settings) -> tuple[list[tuple[int, Signal]], EndpointDetector]:
    """Feed audio in small chunks; returns (time in ms at which each signal fired, signal) and the detector."""
    detector = EndpointDetector(**settings)
    signals, elapsed_ms = [], 0.0
    for chunk in chunked(pcm, chunk_ms, bytes_per_second=rate * width * channels):
        signal = detector.feed(chunk, rate=rate, width=width, channels=channels)
        elapsed_ms += len(chunk) / (rate * width * channels) * 1000
        if signal is not Signal.NONE:
            signals.append((round(elapsed_ms), signal))
    return signals, detector


def kinds(signals: list[tuple[int, Signal]]) -> list[Signal]:
    return [signal for _, signal in signals]


def test_launch_comes_after_300_ms_of_silence_that_follows_speech() -> None:
    signals, detector = run_detector(FULL)
    assert kinds(signals) == [Signal.LAUNCH]
    assert signals[0][0] == 1500                              # 200 ms lead + 1000 ms speech + 300 ms of silence
    assert detector.covers_everything


@pytest.mark.parametrize(("silence_ms", "setting", "launches"), [
    (280, 300, False), (300, 300, True), (480, 500, False), (500, 500, True), (1000, 500, True),
])
def test_launch_waits_for_the_configured_silence(silence_ms: int, setting: int, launches: bool) -> None:
    signals, _ = run_detector(LEAD + WORDS + silence(silence_ms), silence_ms=setting)
    assert (Signal.LAUNCH in kinds(signals)) is launches


def test_the_launch_time_does_not_depend_on_how_small_the_chunks_are() -> None:
    """Silence is measured in whole chunks, so a launch can be late by up to two chunks (one half-speech, one rounding up)."""
    times = {chunk_ms: run_detector(FULL, chunk_ms=chunk_ms)[0][0][0] for chunk_ms in (10, 20, 32)}
    assert all(1500 <= t <= 1500 + 2 * 32 for t in times.values()), times


@pytest.mark.parametrize(("speech_ms", "launches"), [(100, False), (240, False), (260, True), (1000, True)])
def test_short_blips_of_sound_do_not_count_as_speech(speech_ms: int, launches: bool) -> None:
    signals, _ = run_detector(LEAD + speech(speech_ms) + silence(2000))
    assert (Signal.LAUNCH in kinds(signals)) is launches


@pytest.mark.parametrize("audio", [
    pytest.param(silence(3000), id="digital-silence"),
    pytest.param(silence(3000, noise=40), id="mic-hiss"),
    pytest.param(hiss(3000, 300), id="steady-noise"),
    pytest.param(speech(3000), id="speech-no-pause"),
    pytest.param(LEAD + WORDS, id="speech-then-nothing-more"),
])
def test_no_launch_without_speech_followed_by_quiet(audio: bytes) -> None:
    assert run_detector(audio)[0] == []


def test_background_hum_counts_as_quiet_not_as_speech() -> None:
    def hum(ms: int, seed: int) -> bytes:
        return hiss(ms, 250, seed)                                       # a steady hum around 150 rms

    assert run_detector(hum(1500, 1))[0] == []                           # the hum alone is not speech
    signals, _ = run_detector(hum(300, 1) + WORDS + hum(1000, 2))        # speech over it is found; the hum after it is "quiet"
    assert kinds(signals) == [Signal.LAUNCH] and signals[0][0] == 300 + 1000 + 300


def test_sound_after_a_launch_makes_it_stale_after_100_ms() -> None:
    assert kinds(run_detector(LEAD + WORDS + silence(300))[0]) == [Signal.LAUNCH]
    for sound_ms, resumed in ((60, False), (80, False), (100, True), (200, True)):
        signals, detector = run_detector(LEAD + WORDS + silence(300) + speech(sound_ms))
        assert (Signal.RESUMED in kinds(signals)) is resumed, sound_ms
        assert detector.covers_everything is (not resumed), sound_ms


def test_after_the_speaker_resumes_the_next_pause_launches_again() -> None:
    signals, detector = run_detector(LEAD + WORDS + silence(350) + speech(800) + silence(500))
    assert kinds(signals) == [Signal.LAUNCH, Signal.RESUMED, Signal.LAUNCH]
    assert detector.covers_everything


@pytest.mark.parametrize(("max_passes", "launches"), [(1, 1), (2, 2), (3, 3), (5, 3)])
def test_the_number_of_early_requests_per_utterance_is_capped(max_passes: int, launches: int) -> None:
    burst = WORDS + silence(500)
    signals, detector = run_detector(LEAD + burst + burst + burst, max_passes=max_passes)
    assert kinds(signals).count(Signal.LAUNCH) == launches
    if launches < 3:
        assert not detector.covers_everything, "after the cap, the last pause is not covered by an early request"


def test_soft_sound_between_the_thresholds_counts_as_neither_speech_nor_silence() -> None:
    soft = tone(2000, 350)       # ~250 rms: far above the silence level, well below speech - like a trailing breath or whisper
    assert run_detector(LEAD + WORDS + soft)[0] == [], "soft sound must not look like the silence that ends a command"
    blip = speech(200)           # too short to be speech by itself; if soft sound counted as speech this would add up to it
    assert run_detector(LEAD + blip + soft + silence(1000))[0] == [], "soft sound must not count as speech"
    signals, detector = run_detector(LEAD + WORDS + silence(300) + soft)
    assert kinds(signals) == [Signal.LAUNCH, Signal.RESUMED], "soft sound after a launch must make the early request stale"
    assert not detector.covers_everything


@pytest.mark.parametrize("width", [1, 3, 4])
def test_audio_that_is_not_16_bit_never_launches(width: int) -> None:
    loud = bytes([0x70, 0x90, 0x10, 0xE0])[:width] * 16000                # plenty of "sound", but in a format we cannot read
    quiet = bytes(width) * 16000
    signals, detector = run_detector(loud + quiet + quiet, width=width)
    assert signals == [] and not detector.covers_everything


def test_stereo_and_other_sample_rates_are_measured_in_real_time() -> None:
    mono = LEAD + WORDS + silence(1000)
    stereo = b"".join(mono[i:i + 2] * 2 for i in range(0, len(mono), 2))                    # same sound on two channels
    assert run_detector(stereo, channels=2)[0][0][0] == 1500
    triple = b"".join(mono[i:i + 2] * 3 for i in range(0, len(mono), 2))                    # 48 kHz by repeating each sample
    assert run_detector(triple, rate=48000)[0][0][0] == 1500


def test_unusable_input_is_ignored() -> None:
    detector = EndpointDetector()
    assert detector.feed(b"", rate=RATE, width=2, channels=1) is Signal.NONE
    assert detector.feed(bytes(640), rate=0, width=2, channels=1) is Signal.NONE
    assert detector.feed(bytes(640), rate=RATE, width=0, channels=1) is Signal.NONE
    assert detector.feed(bytes(640), rate=RATE, width=2, channels=0) is Signal.NONE


def test_reset_forgets_the_previous_utterance() -> None:
    detector = EndpointDetector(max_passes=1)

    def feed(pcm: bytes) -> list[Signal]:
        return [detector.feed(chunk, rate=RATE, width=2, channels=1) for chunk in chunked(pcm)]

    assert Signal.LAUNCH in feed(FULL) and detector.covers_everything
    detector.reset()
    assert not detector.covers_everything
    assert Signal.LAUNCH not in feed(silence(2000)), "the speech of the previous utterance must be forgotten"
    assert Signal.LAUNCH in feed(FULL), "the number of launches must start from zero again"


def test_rms() -> None:
    assert rms(bytes(640), 2) == 0.0
    assert rms((1000).to_bytes(2, "little", signed=True) * 100, 2) == pytest.approx(1000)
    assert rms((-1000).to_bytes(2, "little", signed=True) * 100, 2) == pytest.approx(1000)
    assert rms(b"", 2) == 0.0 and rms(b"\x01", 2) == 0.0
    assert rms((500).to_bytes(2, "little", signed=True) * 10 + b"\x07", 2) == pytest.approx(500)     # an odd trailing byte is ignored
    assert rms(bytes([200] * 100), 1) == 0.0                                                       # only 16-bit audio is measured


# =====================================================================================================================
# Part 3 - the per-utterance controller (EarlyStt), with a fake request function
# =====================================================================================================================
class Request:
    """Stands in for the transcription request: records its calls, can be slow, notices when it is cancelled."""

    def __init__(self, answer: object = "early answer", delay: float = 0.0, fail: Exception | None = None) -> None:
        self.calls: list[tuple[bytes, tuple[int, int, int]]] = []
        self.cancelled = 0
        self.answer, self.delay, self.fail = answer, delay, fail

    async def __call__(self, pcm: bytes, fmt: tuple[int, int, int]) -> object:
        self.calls.append((pcm, fmt))
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.fail:
            raise self.fail
        return self.answer


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


FORMAT = (RATE, 2, 1)


async def feed_until_launch(early: EarlyStt, request: Request, audio: bytes) -> bytearray:
    """Feed chunks until the request has started; returns the bytes fed at that moment."""
    heard = bytearray()
    for chunk in chunked(audio):
        early.feed(chunk)
        heard += chunk
        await asyncio.sleep(0)
        if request.calls:
            return heard
    raise AssertionError("the controller never started a request")


async def test_the_controller_sends_exactly_the_audio_heard_when_the_speaker_pauses() -> None:
    request = Request(delay=5)
    early = EarlyStt(request=request)
    early.begin(FORMAT)
    heard = await feed_until_launch(early, request, FULL)
    assert request.calls == [(bytes(heard), FORMAT)]
    assert samples_of(bytes(heard)) == 24000                              # speech + the 300 ms that triggered it
    early.end()
    await asyncio.sleep(0.01)
    assert request.cancelled == 1


async def test_the_controller_drops_the_request_when_the_speaker_carries_on() -> None:
    request = Request(delay=5)
    early = EarlyStt(request=request)
    early.begin(FORMAT)
    await feed_until_launch(early, request, LEAD + WORDS + silence(400))
    for chunk in chunked(speech(300)):
        early.feed(chunk)
    await asyncio.sleep(0.01)
    assert request.cancelled == 1
    assert early.take() is None


async def test_take_hands_over_the_pending_request_once() -> None:
    clock = Clock()
    request = Request(delay=0)
    early = EarlyStt(request=request, clock=clock)
    early.begin(FORMAT)
    await feed_until_launch(early, request, FULL)
    clock.now += 0.7
    taken = early.take()
    assert isinstance(taken, Taken) and taken.lead_ms == pytest.approx(700)
    assert await taken.task == "early answer"
    assert early.take() is None                                            # the caller owns it now
    early.end()
    assert request.cancelled == 0                                           # a handed-over request is not cancelled by end()


async def test_take_gives_nothing_back_when_sound_arrived_after_the_launch() -> None:
    request = Request(delay=5)
    early = EarlyStt(request=request)
    early.begin(FORMAT)
    await feed_until_launch(early, request, LEAD + WORDS + silence(300))
    early.feed(speech(200))
    assert early.take() is None
    await asyncio.sleep(0.01)
    assert request.cancelled == 1


async def test_nothing_happens_outside_an_utterance_or_after_idle() -> None:
    request = Request()
    early = EarlyStt(request=request)
    for chunk in chunked(FULL):
        early.feed(chunk)                                                    # before begin(): ignored
    early.begin(FORMAT)
    early.idle()                                                              # e.g. a streaming model: no early request
    for chunk in chunked(FULL):
        early.feed(chunk)
    await asyncio.sleep(0.01)
    assert request.calls == [] and early.take() is None


async def test_a_new_utterance_starts_from_scratch() -> None:
    request = Request(delay=5)
    early = EarlyStt(request=request, max_passes=1)
    early.begin(FORMAT)
    await feed_until_launch(early, request, FULL)
    early.begin(FORMAT)                                                       # the next command on the same connection
    await asyncio.sleep(0.01)
    assert request.cancelled == 1, "the previous utterance's request must be cancelled"
    for chunk in chunked(silence(2000)):
        early.feed(chunk)
    await asyncio.sleep(0.01)
    assert len(request.calls) == 1, "silence alone starts nothing: the speech of the earlier utterance must be forgotten"
    for chunk in chunked(FULL):
        early.feed(chunk)
    await asyncio.sleep(0.01)
    assert len(request.calls) == 2, "max_passes counts per utterance, so the new one may launch again"
    early.end()


async def test_a_broken_detector_switches_the_extra_off_instead_of_breaking_the_request() -> None:
    request = Request()
    early = EarlyStt(request=request)
    early.begin(FORMAT)

    def explode(*args, **kwargs):
        raise RuntimeError("detector bug")

    early._detector.feed = explode
    early.feed(bytes(640))                                                    # must not raise
    for chunk in chunked(FULL):
        early.feed(chunk)
    await asyncio.sleep(0.01)
    assert request.calls == [] and early.take() is None


# =====================================================================================================================
# Part 4 - the stand-in for the OpenAI client (EarlyClient)
# =====================================================================================================================
async def launched_client(request: Request, *, real_text: str = "from the normal request", **early_settings):
    real = FakeSttClient(real_text)
    early = EarlyStt(request=request, **early_settings)
    client = EarlyClient(real, early)
    early.begin(FORMAT)
    await feed_until_launch(early, request, FULL)
    return client, real, early


async def test_the_client_returns_the_early_answer_without_asking_upstream_again() -> None:
    client, real, _ = await launched_client(Request(answer="EARLY"))
    result = await client.audio.transcriptions.create(file=None, model="whisper-1")
    assert result == "EARLY" and real.transcriptions.calls == []


async def test_the_client_waits_for_a_request_that_is_still_running() -> None:
    client, real, _ = await launched_client(Request(answer="EARLY", delay=0.2))
    started = asyncio.get_running_loop().time()
    assert await client.audio.transcriptions.create(file=None, model="whisper-1") == "EARLY"
    assert asyncio.get_running_loop().time() - started >= 0.1 and real.transcriptions.calls == []


async def test_the_client_falls_back_to_the_normal_request_when_the_early_one_failed() -> None:
    client, real, _ = await launched_client(Request(fail=RuntimeError("backend down")))
    result = await client.audio.transcriptions.create(file="the wav", model="whisper-1", prompt="p")
    assert result.text == "from the normal request"
    assert real.transcriptions.calls == [{"file": "the wav", "model": "whisper-1", "prompt": "p"}]


async def test_the_client_gives_up_on_an_early_request_that_takes_too_long() -> None:
    request = Request(delay=30)
    client, real, early = await launched_client(request, timeout_s=0.05)
    result = await client.audio.transcriptions.create(file="the wav", model="whisper-1")
    assert result.text == "from the normal request" and len(real.transcriptions.calls) == 1
    early.end()
    await asyncio.sleep(0.01)
    assert request.cancelled == 1, "the slow early request must not keep running"


async def test_the_client_uses_the_normal_request_when_no_early_one_exists() -> None:
    real = FakeSttClient("normal")
    early = EarlyStt(request=Request())
    client = EarlyClient(real, early)
    early.begin(FORMAT)
    result = await client.audio.transcriptions.create(file="the wav", model="whisper-1")
    assert result.text == "normal" and len(real.transcriptions.calls) == 1


async def test_everything_but_transcription_is_passed_through_untouched() -> None:
    real = FakeSttClient()
    real.marker = "client attribute"
    real.audio.other = "audio attribute"
    client = EarlyClient(real, EarlyStt(request=Request()))
    assert client.marker == "client attribute" and client.backend is real.backend
    assert client.audio.other == "audio attribute"


# =====================================================================================================================
# Part 5 - the handler (our subclass of upstream's), driven event by event
# =====================================================================================================================
async def drive(handler, fake: FakeSttClient, audio: bytes, *, rate: int = RATE, channels: int = 1) -> int:
    """Send one utterance. Returns how many transcription requests existed at the moment audio-stop arrived."""
    await handler.handle_event(Transcribe(language="en").event())
    await handler.handle_event(AudioStart(rate=rate, width=2, channels=channels).event())
    for chunk in chunked(audio, bytes_per_second=rate * 2 * channels):
        await handler.handle_event(AudioChunk(rate=rate, width=2, channels=channels, audio=chunk).event())
        await asyncio.sleep(0)                                                # lets a started early request run
    before_stop = len(fake.transcriptions.calls)
    await handler.handle_event(AudioStop().event())
    return before_stop


def transcripts(handler) -> list[str]:
    return [event.data["text"] for event in handler.sent if event.type == "transcript"]


EARLY_ON = HazelConfig(early_stt=True)


async def test_the_early_request_is_built_exactly_like_upstreams_own_request() -> None:
    settings = {"stt_prompt": "Poobot", "stt_temperature": 0.2,
                "stt_extra_body": {"prompt": "Nitin's Office, Poobot", "hotwords": "Poobot", "vad_filter": True}}
    audio = LEAD + WORDS + silence(350) + speech(800) + silence(200)         # early request, dropped, then the normal one
    mine, stock = FakeSttClient("hello"), FakeSttClient("hello")
    mine_handler = build_handler(EARLY_ON, stt_client=mine, **settings)
    stock_handler = build_handler(stock=True, stt_client=stock, **settings)
    assert await drive(mine_handler, mine, audio) == 1                        # the early request already existed at audio-stop
    assert await drive(stock_handler, stock, audio) == 0

    early_call, normal_call = mine.transcriptions.calls
    (stock_call,) = stock.transcriptions.calls

    def settings_of(call: dict) -> dict:
        return {key: value for key, value in call.items() if key != "file" and value is not omit}

    assert settings_of(early_call) == settings_of(normal_call) == settings_of(stock_call)
    assert settings_of(stock_call)["extra_body"] == settings["stt_extra_body"]
    assert early_call["file"].name == normal_call["file"].name == stock_call["file"].name == "recording.wav"
    # the normal request of our handler carries the same audio as the stock bridge's; the early one is only the first part of it
    early_wav, normal_wav = mine.transcriptions.wav
    assert normal_wav == stock.transcriptions.wav[0]
    assert early_wav != normal_wav and len(early_wav) < len(normal_wav)
    assert transcripts(mine_handler) == transcripts(stock_handler) == ["hello"]


@pytest.mark.parametrize(("streaming_models", "extra_body", "early_expected"), [
    pytest.param((), None, True, id="plain-model"),
    pytest.param((), {"stream": False}, True, id="stream-false"),
    pytest.param((), {"stream": True}, False, id="stream-true-in-extra-body"),
    pytest.param(("whisper-1",), None, False, id="streaming-model"),
])
async def test_early_requests_are_only_made_where_the_answer_is_one_plain_result(streaming_models, extra_body,
                                                                                early_expected) -> None:
    fake = FakeSttClient("hello")
    handler = build_handler(EARLY_ON, stt_client=fake, info=make_info(stt_streaming_models=streaming_models),
                            stt_extra_body=extra_body)
    requests_at_audio_stop = await drive(handler, fake, FULL)
    assert (requests_at_audio_stop == 1) is early_expected
    assert transcripts(handler) == ["hello"]                                  # either way the answer reaches the client


async def test_each_utterance_on_a_connection_gets_its_own_early_request() -> None:
    fake = FakeSttClient("hello")
    handler = build_handler(EARLY_ON, stt_client=fake)
    assert await drive(handler, fake, FULL) == 1
    before = len(fake.transcriptions.calls)
    assert await drive(handler, fake, FULL) == before + 1
    assert transcripts(handler) == ["hello", "hello"]


async def test_a_failing_early_request_never_stops_the_answer_from_arriving() -> None:
    fake = FakeSttClient("recovered")
    real_create = fake.transcriptions.create

    async def first_call_fails(**kwargs):
        if not fake.transcriptions.calls:
            fake.transcriptions.calls.append(kwargs)
            raise RuntimeError("backend hiccup")
        return await real_create(**kwargs)

    fake.transcriptions.create = first_call_fails
    handler = build_handler(EARLY_ON, stt_client=fake)
    await drive(handler, fake, FULL)
    assert transcripts(handler) == ["recovered"]
    assert len(fake.transcriptions.calls) == 2


async def test_with_the_extra_off_the_client_is_not_wrapped_and_nothing_is_sent_early() -> None:
    fake = FakeSttClient()
    handler = build_handler(HazelConfig(early_stt=False), stt_client=fake)
    assert handler._stt_client is fake
    assert await drive(handler, fake, FULL) == 0


def test_a_bridge_without_speech_to_text_starts_fine_with_the_extra_on() -> None:
    handler = build_handler(EARLY_ON, stt_client=None)                        # text-to-speech only
    assert handler._stt_client is None


def upsample(pcm: bytes, *, factor: int, channels: int) -> bytes:
    """16 kHz mono -> ``16 kHz * factor`` with ``channels`` channels, by repeating every sample."""
    return b"".join(pcm[i:i + 2] * (factor * channels) for i in range(0, len(pcm), 2))


async def test_the_early_request_keeps_the_audio_format_of_the_stream() -> None:
    rate, channels = 48000, 2
    fake = FakeSttClient("hello")
    handler = build_handler(EARLY_ON, stt_client=fake)
    assert await drive(handler, fake, upsample(FULL, factor=3, channels=channels), rate=rate, channels=channels) == 1
    (early_wav,) = fake.transcriptions.wav
    with wave.open(io.BytesIO(early_wav)) as early:
        assert (early.getframerate(), early.getnchannels(), early.getsampwidth()) == (rate, channels, 2)
        assert early.getnframes() == 1500 * rate // 1000              # the speech plus the 300 ms of silence that started it
    assert transcripts(handler) == ["hello"]
