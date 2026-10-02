#!/usr/bin/env python3
"""Smoke test for a built image: start it for real, talk to it like Home Assistant does, check every extra, always clean up.

    python tools/smoke_test.py IMAGE [--expect-version 0.7.0-hazel.1] [--expect-upstream 0.7.0]

Run by the release workflow on the freshly built image BEFORE it is pushed. Needs Docker, Python 3.12+ and
``pip install wyoming aiohttp`` (nothing else). The image runs with ``--network host`` next to a fake speech server (the stub from
tests/stubs.py), with early transcription, TTS concurrency 1 and a voice label switched on.

Exit code: 0 = all checks passed, 1 = a check failed (the reason is printed in plain words), 2 = the test could not run
(no Docker, image missing, container did not start).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from stubs import StubBackend, tts_pcm  # noqa: E402
from wyoming_client import WyomingClient, samples_of, silence, speech  # noqa: E402

START_TIMEOUT_S = 90.0
STT_DELAY_S = 0.5                    # how long the fake Whisper takes - the time early transcription can save
SENTENCES = ["The kettle is boiling now.", "The back door is open.", "The garden lamp is on."]
SETTINGS = {                          # the extras this test switches on
    "HAZEL_STT_EARLY": "1",
    "HAZEL_TTS_CONCURRENCY": "1",
    "HAZEL_TTS_VOICE_LABELS": "af_hazel=Hazel",
}


class SmokeFailure(Exception):
    """A check failed; the message says what was expected and what happened, in plain words."""


class CouldNotRun(Exception):
    """The test itself could not be carried out (not a verdict on the image)."""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def docker(*args: str, check: bool = True, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CouldNotRun("the 'docker' command was not found") from None
    except subprocess.TimeoutExpired:
        raise CouldNotRun(f"'docker {' '.join(args[:2])}' did not finish within {timeout:.0f} s") from None
    if check and result.returncode != 0:
        raise CouldNotRun(f"'docker {' '.join(args[:2])}' failed: {(result.stderr or result.stdout).strip()}")
    return result


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    async def check(self, name: str, check) -> None:
        started = time.monotonic()
        try:
            detail = await check()
        except SmokeFailure as failure:
            self.failures.append(f"{name}: {failure}")
            print(f"  FAIL  {name}\n        {failure}", flush=True)
        except Exception as error:      # a bug in the test or an unexpected bridge reply is still a failed check, not a crash
            self.failures.append(f"{name}: {type(error).__name__}: {error}")
            print(f"  FAIL  {name}\n        {type(error).__name__}: {error}", flush=True)
        else:
            self.passed += 1
            print(f"  ok    {name}" + (f"  ({detail})" if detail else "") + f"  [{time.monotonic() - started:.1f} s]", flush=True)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


async def wait_for_describe(client: WyomingClient, container: str) -> object:
    deadline = time.monotonic() + START_TIMEOUT_S
    while True:
        state = docker("inspect", "-f", "{{.State.Running}}", container, check=False).stdout.strip()
        if state != "true":
            raise CouldNotRun("the container stopped before it answered the first 'describe'")
        try:
            return await asyncio.wait_for(client.describe(), 3.0)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            if time.monotonic() > deadline:
                raise CouldNotRun(f"the container did not answer 'describe' within {START_TIMEOUT_S:.0f} s") from None
            await asyncio.sleep(0.25)


def container_logs(container: str, tail: int = 60) -> str:
    result = docker("logs", "--tail", str(tail), container, check=False)
    return (result.stdout + result.stderr).strip()


async def run_checks(image: str, expect_version: str | None, expect_upstream: str | None, docker_args: list[str]) -> Report:
    backend = StubBackend(stt_delay_s=STT_DELAY_S, n_chunks=6, chunk_delay_s=0.1)
    await backend.start()
    port = free_port()
    name = f"hazel-smoke-{os.getpid()}-{random.randrange(10**6):06d}"
    env = {
        "WYOMING_URI": f"tcp://127.0.0.1:{port}", "WYOMING_LANGUAGES": "en",
        "STT_OPENAI_URL": backend.base_url, "TTS_OPENAI_URL": backend.base_url,
        "STT_MODELS": "whisper-1", "STT_BACKEND": "OPENAI",
        "TTS_MODELS": "kokoro", "TTS_STREAMING_MODELS": "kokoro", "TTS_VOICES": "af_hazel af_heart", "TTS_BACKEND": "OPENAI",
        **SETTINGS,
    }
    # No --rm: if the bridge crashes at start-up its log must still be readable. The container is removed in the 'finally' below.
    command = ["run", "-d", "--network", "host", "--name", name, *docker_args]
    for key, value in env.items():
        command += ["-e", f"{key}={value}"]
    report = Report()
    started = complete = False
    try:
        print(f"Starting {image} as {name} (stub speech server on port {backend.port}, bridge on port {port}) ...", flush=True)
        docker(*command, image)
        started = True
        client = WyomingClient("127.0.0.1", port)
        await wait_for_describe(client, name)
        print("The bridge is up. Checking:", flush=True)

        async def info_and_labels() -> str:
            info = await client.describe()
            asr = [(p.name, m.name, tuple(m.languages)) for p in info.asr for m in p.models]
            expect(asr == [("openai", "whisper-1", ("en",))], f"speech-to-text models were {asr}, expected whisper-1 / en")
            tts = [(p.name, p.supports_synthesize_streaming, [(v.name, v.description) for v in p.voices]) for p in info.tts]
            expect(tts == [("openai-streaming", True, [("af_hazel", "Hazel"), ("af_heart", "af_heart")])],
                   f"voices were {tts}; expected one streaming program with af_hazel shown as 'Hazel' and af_heart unchanged")
            return "af_hazel is shown as 'Hazel', af_heart is unchanged"

        async def transcription() -> str:
            backend.reset()
            audio = silence(200) + speech(1000) + silence(1000)
            text, stamps = await client.transcribe(audio)
            expect(len(backend.stt_requests) == 1, f"the speech server got {len(backend.stt_requests)} requests, expected exactly 1")
            request = backend.stt_requests[0]
            expect(text == f"heard {request.samples} samples", f"transcript was {text!r} but the request carried {request.samples} samples")
            lead = stamps.audio_stop_sent - request.arrived
            expect(lead >= 0.3, f"early transcription is not working: the request was sent {lead:.2f} s before audio-stop, expected at least 0.3 s")
            expect(stamps.latency_s < STT_DELAY_S * 0.8,
                   f"the transcript took {stamps.latency_s:.2f} s after audio-stop; with early transcription it should be well under "
                   f"the speech server's {STT_DELAY_S} s")
            expect(request.samples < samples_of(audio), "the early request should have carried the audio up to the pause only")
            return f"request sent {lead:.2f} s before audio-stop, transcript {stamps.latency_s * 1000:.0f} ms after it"

        async def streamed_speech() -> str:
            backend.reset()
            result = await client.synthesize(" ".join(SENTENCES), voice="af_hazel")
            start = result.audio_start
            expect(start is not None and (start.rate, start.width, start.channels) == (24000, 2, 1), f"audio format was {start}")
            expect(result.synthesize_stopped and result.audio_stopped, "the answer did not end with audio-stop and synthesize-stopped")
            expected = b"".join(tts_pcm(s, n_chunks=backend.n_chunks) for s in SENTENCES)
            expect(result.pcm == expected, "the audio is incomplete, repeated or out of order")
            gap = result.last_chunk_at - result.first_chunk_at
            expect(gap >= 0.3, f"the first and last audio chunk arrived only {gap:.2f} s apart: the audio was not streamed")
            backend_done = max(r.finished for r in backend.tts_requests)
            expect(result.first_chunk_at < backend_done, "the first audio arrived only after the speech server had finished everything")
            expect({r.voice for r in backend.tts_requests} == {"af_hazel"}, "the speech server was not asked for voice 'af_hazel'")
            expect(backend.tts_max_in_flight == 1, f"{backend.tts_max_in_flight} speech requests ran at once, HAZEL_TTS_CONCURRENCY=1 allows 1")
            return f"first audio {gap:.2f} s before the last, one request at a time"

        async def banner() -> str:
            logs = container_logs(name, tail=400)
            line = next((ln for ln in logs.splitlines() if "extras on:" in ln), None)
            expect(line is not None, "the start-up line 'extras on:' is missing from the container log:\n" + logs[-1500:])
            for extra in ("early-stt", "tts-concurrency(1)", "voice-labels(1)", "timing-log"):
                expect(extra in line, f"the banner does not list {extra!r}: {line}")
            if expect_version:
                expect(f"wyoming-openai-hazel {expect_version} on " in line, f"banner shows another version than {expect_version}: {line}")
            if expect_upstream:
                expect(f"on wyoming_openai {expect_upstream};" in line, f"banner shows another upstream version than {expect_upstream}: {line}")
            problems = [ln for ln in logs.splitlines() if "Traceback" in ln or "could NOT be installed" in ln or "EXTRAS NOT INSTALLED" in ln]
            expect(not problems, "the container log contains problems:\n" + "\n".join(problems[:5]))
            return line.strip()

        async def health_check() -> str:
            result = await asyncio.to_thread(docker, "exec", name, "python", "-m", "wyoming_openai_hazel.healthcheck", check=False)
            expect(result.returncode == 0, f"the health check command failed (exit {result.returncode}): {(result.stdout + result.stderr).strip()}")
            config = json.loads(docker("inspect", "-f", "{{json .Config.Healthcheck}}", image, check=False).stdout.strip() or "null")
            expect(bool(config and config.get("Test")), "the image has no HEALTHCHECK")
            return "HEALTHCHECK is defined and passes"

        await report.check("describe: models, streaming voice program, voice label", info_and_labels)
        await report.check("speech to text: early request, answer ready at audio-stop", transcription)
        await report.check("text to speech: streamed, in order, one request at a time", streamed_speech)
        await report.check("start-up banner and a clean log", banner)
        await report.check("docker health check", health_check)
        complete = True
    finally:
        if started:
            if report.failures or not complete:         # something went wrong: show what the bridge itself said
                print("\n--- last lines of the container log ---\n" + container_logs(name), file=sys.stderr, flush=True)
            docker("stop", "-t", "5", name, check=False)
            docker("rm", "-f", name, check=False)
        await backend.stop()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", help="image to test, e.g. wyoming-openai-hazel:test (it must already exist locally)")
    parser.add_argument("--expect-version", help="the release version the banner must show, e.g. 0.7.0-hazel.1")
    parser.add_argument("--expect-upstream", help="the upstream version the banner must show, e.g. 0.7.0")
    parser.add_argument("--docker-arg", action="append", default=[], metavar="ARG",
                        help="extra argument for 'docker run' (repeatable), e.g. --docker-arg=--memory=1g")
    parser.add_argument("--timeout", type=float, default=300.0, help="give up after this many seconds in total (default 300)")
    args = parser.parse_args()

    # A cancelled workflow sends SIGTERM; turn it into a normal exit so the container is still stopped and removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        if docker("image", "inspect", args.image, check=False).returncode != 0:
            raise CouldNotRun(f"image {args.image!r} was not found locally - build or pull it first")
        report = asyncio.run(asyncio.wait_for(
            run_checks(args.image, args.expect_version, args.expect_upstream, args.docker_arg), args.timeout))
    except CouldNotRun as problem:
        print(f"\nSMOKE TEST COULD NOT RUN: {problem}", file=sys.stderr)
        return 2
    except asyncio.TimeoutError:
        print(f"\nSMOKE TEST COULD NOT FINISH: still running after {args.timeout:.0f} s", file=sys.stderr)
        return 2
    total = report.passed + len(report.failures)
    if report.failures:
        print(f"\nSMOKE TEST FAILED: {len(report.failures)} of {total} checks failed:", file=sys.stderr)
        for failure in report.failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"\nSMOKE TEST PASSED: all {total} checks ok for {args.image}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
