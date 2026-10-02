"""The whole bridge, started the way the container starts it, talked to the way Home Assistant talks to it.

Also: what happens when upstream changes shape (the extras step aside and the stock bridge runs), the timing log, and the
Docker health check.
"""

from __future__ import annotations

import asyncio
import runpy
import sys
from pathlib import Path

import pytest
from bridge_process import PACKAGE_ROOT, free_port
from stubs import tts_pcm
from wyoming_client import silence, speech
from wyoming_openai import __version__ as UPSTREAM_VERSION
from wyoming_openai import handler as upstream_module
from wyoming_openai.handler import OpenAIEventHandler as STOCK
from wyoming_openai_hazel import __main__ as hazel_main

ALL_EXTRAS = {"HAZEL_STT_EARLY": "1", "HAZEL_TTS_CONCURRENCY": "2", "HAZEL_TTS_VOICE_LABELS": "af_hazel=Hazel",
              "HAZEL_VERSION": "9.9.9-test"}
SENTENCES = ["The kettle is boiling now.", "The back door is open."]

# Stands in for an upstream release that renamed something the extras rely on: the package then expects a member the upstream
# handler no longer has. (Everything else is the unchanged upstream bridge, so it must keep working.)
BROKEN_SEAM_LAUNCHER = """
import wyoming_openai_hazel.__main__ as hazel

hazel.REQUIRED_MEMBERS += ("_renamed_in_a_newer_upstream",)
hazel.main()
"""


# ----- a normal day ----------------------------------------------------------------------------------------------------
async def test_a_conversation_the_way_home_assistant_has_it(bridge, backend, tmp_path: Path) -> None:
    b = await bridge(HAZEL_GPU_WAKE_FILE=str(tmp_path / "wake"), TTS_SPEED="1.25", **ALL_EXTRAS)
    client = b.client()

    # 1. Home Assistant asks what the bridge offers
    info = await client.describe()
    assert [(p.name, p.supports_transcript_streaming) for p in info.asr] == [("openai", False)]
    assert [(m.name, m.languages) for p in info.asr for m in p.models] == [("whisper-1", ["en"])]
    assert [(p.name, p.supports_synthesize_streaming) for p in info.tts] == [("openai-streaming", True)]
    voices = [(v.name, v.description, v.languages) for p in info.tts for v in p.voices]
    assert voices == [("af_hazel", "Hazel", ["en"]), ("af_heart", "af_heart", ["en"])]

    # 2. a spoken command becomes text
    text, stamps = await client.transcribe(silence(200) + speech(800) + silence(1000), read_to_end=True)
    assert text == f"heard {backend.stt_requests[-1].samples} samples" and backend.stt_requests[-1].samples > 0
    assert stamps.event_types == ["transcript-start", "transcript", "transcript-stop"]

    # 3. the answer is spoken (streaming voice: text pieces in, audio chunks out, then synthesize-stopped)
    spoken = await client.synthesize(SENTENCES[0] + " " + SENTENCES[1], voice="af_hazel")
    assert spoken.synthesize_stopped and spoken.audio_stopped
    assert spoken.pcm == b"".join(tts_pcm(s, n_chunks=backend.n_chunks) for s in SENTENCES)
    assert {r.voice for r in backend.tts_requests} == {"af_hazel"} and {r.speed for r in backend.tts_requests} == {1.25}
    assert {r.response_format for r in backend.tts_requests} == {"wav"}

    # 4. a client that does not know streaming sends one plain synthesize event
    backend.reset()
    plain = await client.synthesize(SENTENCES[0], voice="af_heart", streaming=False)
    assert plain.audio_stopped and not plain.synthesize_stopped
    assert plain.pcm == tts_pcm(SENTENCES[0], n_chunks=backend.n_chunks)
    assert [r.voice for r in backend.tts_requests] == ["af_heart"]


async def test_the_start_up_banner_names_the_versions_and_the_extras(bridge, tmp_path: Path) -> None:
    b = await bridge(HAZEL_GPU_WAKE_FILE=str(tmp_path / "wake"), **ALL_EXTRAS)
    banner = [line for line in b.stderr_lines if "extras on:" in line]
    assert len(banner) == 1, b.stderr
    line = banner[0]
    assert line.startswith(f"wyoming-openai-hazel 9.9.9-test on wyoming_openai {UPSTREAM_VERSION}; extras on: ")
    for extra in (f"gpu-wake({tmp_path / 'wake'})", "early-stt(silence 300 ms)", "tts-concurrency(2)", "voice-labels(1)", "timing-log"):
        assert extra in line


async def test_a_bridge_with_no_settings_still_prints_a_banner(bridge) -> None:
    b = await bridge()
    assert any(line.endswith("extras on: timing-log") for line in b.stderr_lines), b.stderr


# ----- the timing log ---------------------------------------------------------------------------------------------------
async def test_the_timing_log_reports_speech_to_text_and_first_audio(bridge) -> None:
    b = await bridge()
    client = b.client()
    await client.transcribe(speech(400) + silence(400), realtime=False)
    await client.synthesize("Hello there.")
    assert await b.wait_for_log(r"stt audio-stop -> transcript written: \d+ ms")
    assert await b.wait_for_log(r"tts first-audio \d+ ms after the request started")


async def test_the_timing_log_can_be_switched_off(bridge) -> None:
    b = await bridge(HAZEL_LOG_TIMING="0")
    client = b.client()
    await client.transcribe(speech(400) + silence(400), realtime=False)
    await client.synthesize("Hello there.")
    await client.describe()                       # by now the bridge has finished with both requests
    assert "timing-log" not in b.stderr
    assert not b.lines_matching(r"audio-stop -> transcript written|tts first-audio")


# ----- the Docker health check ------------------------------------------------------------------------------------------
async def run_healthcheck(uri: str) -> int:
    """Run the command Docker's HEALTHCHECK runs, pointed at ``uri``; returns its exit code."""
    env = {"WYOMING_URI": uri, "PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": str(PACKAGE_ROOT)}
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "wyoming_openai_hazel.healthcheck", env=env,
                                                   stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return await asyncio.wait_for(process.wait(), 30)


async def test_the_health_check_passes_for_a_running_bridge_and_fails_for_a_dead_one(bridge) -> None:
    b = await bridge()
    assert await run_healthcheck(b.uri) == 0
    assert await run_healthcheck(f"tcp://0.0.0.0:{free_port()}") == 1


# ----- upstream changed shape: the extras step aside --------------------------------------------------------------------
async def test_when_the_seam_is_broken_the_stock_bridge_runs_and_says_so(bridge, backend) -> None:
    b = await bridge(args=[sys.executable, "-c", BROKEN_SEAM_LAUNCHER], allow_errors=True,
                     HAZEL_STT_EARLY="1", HAZEL_TTS_CONCURRENCY="1", HAZEL_TTS_VOICE_LABELS="af_hazel=Hazel")
    assert b.lines_matching("Hazel extras could NOT be installed")
    assert b.lines_matching(r"EXTRAS NOT INSTALLED .*_renamed_in_a_newer_upstream.*running the stock bridge")
    assert not b.lines_matching("extras on:")

    client = b.client()
    info = await client.describe()
    assert [v.description for p in info.tts for v in p.voices] == ["af_hazel", "af_heart"]        # labels: off

    # speech to text: the stock behaviour - one request, sent only when audio-stop arrives
    text, stamps = await client.transcribe(silence(200) + speech(800) + silence(1000))
    assert len(backend.stt_requests) == 1
    assert backend.stt_requests[0].arrived >= stamps.audio_stop_sent - 0.01
    assert text == f"heard {backend.stt_requests[0].samples} samples"

    # text to speech: still works, and upstream's own limit of 3 applies (HAZEL_TTS_CONCURRENCY=1 is not honoured)
    backend.n_chunks, backend.chunk_delay_s = 5, 0.1
    sentences = ["The kettle is boiling now.", "The back door is open.", "The garden lamp is on.", "The fan upstairs is off."]
    spoken = await client.synthesize(" ".join(sentences))
    assert spoken.pcm == b"".join(tts_pcm(s, n_chunks=5) for s in sentences)
    assert backend.tts_max_in_flight == 3


def test_main_installs_the_extras_prints_the_banner_and_starts_the_stock_bridge(monkeypatch, capsys) -> None:
    started = []
    monkeypatch.setattr(runpy, "run_module", lambda *args, **kwargs: started.append((args, kwargs)))
    monkeypatch.setattr(upstream_module, "OpenAIEventHandler", STOCK)          # undone after the test
    monkeypatch.setenv("HAZEL_STT_EARLY", "1")
    monkeypatch.setenv("HAZEL_TTS_CONCURRENCY", "2")

    hazel_main.main()

    assert started == [(("wyoming_openai",), {"run_name": "__main__", "alter_sys": True})]
    err = capsys.readouterr().err
    assert "extras on:" in err and "early-stt" in err and "tts-concurrency(2)" in err
    installed = upstream_module.OpenAIEventHandler
    assert installed is not STOCK and issubclass(installed, STOCK)
    assert installed.hazel.tts_concurrency == 2


@pytest.mark.parametrize("breakage", ["impostor-class", "missing-method"])
def test_main_falls_back_to_the_stock_bridge_when_the_seam_is_broken(monkeypatch, capsys, caplog, breakage) -> None:
    started = []
    monkeypatch.setattr(runpy, "run_module", lambda *args, **kwargs: started.append((args, kwargs)))
    monkeypatch.setattr(upstream_module, "OpenAIEventHandler", STOCK)
    if breakage == "impostor-class":
        class Impostor(STOCK):
            pass

        monkeypatch.setattr(upstream_module, "OpenAIEventHandler", Impostor)
        reason = "not the class"
    else:
        monkeypatch.delattr(STOCK, "_handle_audio_stop")
        reason = "_handle_audio_stop"

    hazel_main.main()

    assert len(started) == 1, "the stock bridge must still be started"
    err = capsys.readouterr().err
    assert "EXTRAS NOT INSTALLED" in err and reason in err and "extras on:" not in err
    assert any("could NOT be installed" in record.getMessage() for record in caplog.records)
    assert getattr(upstream_module.OpenAIEventHandler, "hazel", None) is None      # nothing was swapped in
