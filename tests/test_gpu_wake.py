"""GPU wake hook (HAZEL_GPU_WAKE_FILE): a file's modification time moves when a voice request starts.

A small program on the GPU machine can watch that file and wake a sleeping graphics card, so the card is awake when the audio
arrives. The hook must never get in the way of a request.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import pytest
from wyoming.asr import Transcribe
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.info import Describe, SelectProgram
from wyoming.ping import Ping
from wyoming.tts import Synthesize, SynthesizeChunk, SynthesizeStart, SynthesizeStop, SynthesizeVoice
from wyoming_client import WyomingClient, silence, speech
from wyoming_openai_hazel.wake import WAKE_EVENTS, WakeFile

LONG_AGO = 1_000_000          # seconds since 1970: "this file was last touched ages ago"
VOICE = SynthesizeVoice(name="af_hazel")


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def mtime(path: Path) -> float:
    return path.stat().st_mtime


def age_it(path: Path) -> None:
    path.write_text("")
    os.utime(path, (LONG_AGO, LONG_AGO))
    assert mtime(path) == LONG_AGO


def was_touched(path: Path) -> bool:
    return mtime(path) > LONG_AGO + 1000


async def wait_until_touched(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if was_touched(path):
            return True
        await asyncio.sleep(0.02)
    return was_touched(path)


# ----- WakeFile on its own (a fake clock, no network) ----------------------------------------------------------------
def test_the_four_request_start_events_are_the_ones_that_wake() -> None:
    assert WAKE_EVENTS == {"transcribe", "audio-start", "synthesize", "synthesize-start"}


def test_the_first_touch_creates_the_file(tmp_path: Path) -> None:
    wake = WakeFile(str(tmp_path / "wake"), clock=Clock())
    assert wake.touch("transcribe") is True
    assert (tmp_path / "wake").exists()


def test_two_touches_within_a_quarter_second_count_once(tmp_path: Path) -> None:
    path, clock = tmp_path / "wake", Clock()
    wake = WakeFile(str(path), clock=clock)
    assert wake.touch("transcribe") is True
    age_it(path)                                  # make a second touch visible
    clock.advance(0.10)
    assert wake.touch("audio-start") is False
    assert not was_touched(path)
    clock.advance(0.20)                           # 0.30 s after the first
    assert wake.touch("audio-start") is True
    assert was_touched(path)


def test_the_interval_is_a_quarter_second_by_default_and_can_be_changed(tmp_path: Path) -> None:
    path, clock = tmp_path / "wake", Clock()
    wake = WakeFile(str(path), clock=clock)
    assert wake.touch("a")
    clock.advance(0.249)
    assert not wake.touch("b")
    clock.advance(0.001)                          # exactly 0.25 s after the first touch
    assert wake.touch("c")
    slow = WakeFile(str(tmp_path / "slow"), min_interval_s=5.0, clock=clock)
    assert slow.touch("a")
    clock.advance(4.9)
    assert not slow.touch("b")
    clock.advance(0.2)
    assert slow.touch("c")


def test_a_missing_folder_never_raises_and_is_reported_once_a_minute(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    wake = WakeFile(str(tmp_path / "no-such-folder" / "wake"), clock=clock)
    with caplog.at_level(logging.INFO):
        assert wake.touch("transcribe") is False
        for _ in range(5):
            clock.advance(1.0)
            assert wake.touch("transcribe") is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "cannot touch" in warnings[0].getMessage()
        clock.advance(60.0)
        assert wake.touch("transcribe") is False
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
    assert not (tmp_path / "no-such-folder").exists()       # it does not invent folders


def test_a_path_below_a_plain_file_never_raises(tmp_path: Path) -> None:
    (tmp_path / "plain-file").write_text("x")
    assert WakeFile(str(tmp_path / "plain-file" / "wake"), clock=Clock()).touch("synthesize") is False


def test_touching_an_existing_file_only_moves_its_time(tmp_path: Path) -> None:
    path = tmp_path / "wake"
    path.write_text("keep me")
    os.utime(path, (LONG_AGO, LONG_AGO))
    assert WakeFile(str(path), clock=Clock()).touch("transcribe") is True
    assert was_touched(path) and path.read_text() == "keep me"


# ----- through the real bridge -----------------------------------------------------------------------------------------
async def fire(client: WyomingClient, kind: str) -> None:
    """Send one request-start event of the given kind, the way Home Assistant would (nothing else on the connection)."""
    if kind == "transcribe":
        await client.send(Transcribe(language="en").event())
    elif kind == "audio-start":
        await client.send(AudioStart(rate=16000, width=2, channels=1).event())
    elif kind == "synthesize-start":
        await client.send(SynthesizeStart(voice=VOICE).event())
    elif kind == "synthesize":
        await client.synthesize("Hello there.", voice="af_hazel", streaming=False)
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", sorted(WAKE_EVENTS))
async def test_each_request_start_event_touches_the_file(bridge, tmp_path: Path, kind: str) -> None:
    wake = tmp_path / "wake"
    age_it(wake)
    b = await bridge(HAZEL_GPU_WAKE_FILE=str(wake))
    await fire(b.client(), kind)
    assert await wait_until_touched(wake), f"the {kind} event did not touch {wake}"
    assert f"gpu-wake({wake})" in b.stderr          # and the start-up banner said so


async def test_the_file_is_created_when_it_does_not_exist_yet(bridge, tmp_path: Path) -> None:
    wake = tmp_path / "fresh" / "wake"
    wake.parent.mkdir()
    b = await bridge(HAZEL_GPU_WAKE_FILE=str(wake))
    assert not wake.exists()
    await fire(b.client(), "transcribe")
    assert wake.exists()


async def test_other_events_leave_the_file_alone(bridge, tmp_path: Path) -> None:
    wake = tmp_path / "wake"
    age_it(wake)
    b = await bridge(HAZEL_GPU_WAKE_FILE=str(wake))
    client = b.client()
    others = {
        "describe": Describe().event(),
        "select-program": SelectProgram(name="openai").event(),
        "audio-chunk": AudioChunk(rate=16000, width=2, channels=1, audio=bytes(640)).event(),
        "audio-stop": AudioStop().event(),
        "synthesize-chunk": SynthesizeChunk(text="Hello").event(),
        "synthesize-stop": SynthesizeStop().event(),
        "ping": Ping().event(),
    }
    for name, event in others.items():
        # send() returns when the bridge has handled the event and closed the conversation, so the file can be looked at now.
        await client.send(event)
        assert not was_touched(wake), f"the {name} event touched the wake file"
    await fire(client, "transcribe")                 # ...while the hook itself is alive
    assert await wait_until_touched(wake)


@pytest.mark.parametrize("setting", [None, ""])
async def test_without_a_wake_file_setting_the_feature_is_off(bridge, tmp_path: Path, setting: str | None) -> None:
    b = await bridge(HAZEL_GPU_WAKE_FILE=setting)
    client = b.client()
    for kind in ("transcribe", "audio-start", "synthesize-start", "synthesize"):
        await fire(client, kind)
    text, _ = await client.transcribe(silence(300), realtime=False)
    assert text
    assert list(tmp_path.iterdir()) == []            # the bridge runs in tmp_path: nothing was created anywhere near it
    assert "gpu-wake" not in b.output


@pytest.mark.parametrize("where", ["missing-folder", "below-a-plain-file"])
async def test_an_unusable_location_never_breaks_a_request(bridge, backend, tmp_path: Path, where: str) -> None:
    if where == "missing-folder":
        wake = tmp_path / "no-such-folder" / "wake"
    else:
        (tmp_path / "plain-file").write_text("x")
        wake = tmp_path / "plain-file" / "wake"
    b = await bridge(HAZEL_GPU_WAKE_FILE=str(wake))          # also: the bridge starts although the location is unusable
    client = b.client()
    text, _ = await client.transcribe(speech(500) + silence(200), realtime=False)
    assert text.startswith("heard ") and len(backend.stt_requests) == 1
    spoken = await client.synthesize("Hello there.", voice="af_hazel")
    assert spoken.pcm and spoken.synthesize_stopped
    await b.wait_for_log("gpu-wake: cannot touch")            # reported, not raised (a traceback would fail the fixture's check)
    assert not wake.exists()
