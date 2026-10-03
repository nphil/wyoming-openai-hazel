"""Starting the real bridge as a separate process, and reading what it prints.

``Bridge`` is one running process; ``BridgeFactory`` starts them with the right environment (wired to the stub backend) and
stops them again. conftest.py turns the factory into the ``bridge`` fixture.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import sys
import time
from pathlib import Path

import wyoming_openai_hazel
from stubs import StubBackend
from wyoming_client import WyomingClient, silence, speech

START_TIMEOUT_S = 40.0        # a busy CI machine can take a while to import everything
STOP_TIMEOUT_S = 10.0

PACKAGE_ROOT = Path(wyoming_openai_hazel.__file__).resolve().parent.parent   # what must be on PYTHONPATH for `-m wyoming_openai_hazel`

# Everything the bridge reads from its environment. Anything of these set on the test machine is removed first, so a test
# only ever sees what it asks for.
_SCRUBBED_PREFIXES = ("HAZEL_", "STT_", "TTS_", "WYOMING_", "OPENAI_")
_SCRUBBED_NAMES = {"http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}

TRACEBACK = "Traceback (most recent call last)"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Bridge:
    """A running bridge process plus everything it has printed so far."""

    def __init__(self, process: asyncio.subprocess.Process, port: int, env: dict[str, str], allow_errors: bool) -> None:
        self.process = process
        self.port = port
        self.env = env
        self.allow_errors = allow_errors
        self.started_at = time.monotonic()
        self.stderr_lines: list[str] = []
        self.stdout_lines: list[str] = []
        self._pumps = [
            asyncio.ensure_future(self._pump(process.stderr, self.stderr_lines)),
            asyncio.ensure_future(self._pump(process.stdout, self.stdout_lines)),
        ]

    @staticmethod
    async def _pump(stream: asyncio.StreamReader | None, lines: list[str]) -> None:
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                return
            lines.append(raw.decode(errors="replace").rstrip("\n"))

    # ----- what the process printed ------------------------------------------------------------------------------
    @property
    def stderr(self) -> str:
        return "\n".join(self.stderr_lines)

    @property
    def output(self) -> str:
        return "\n".join(self.stderr_lines + self.stdout_lines)

    def lines_matching(self, pattern: str) -> list[str]:
        regex = re.compile(pattern)
        return [line for line in self.stderr_lines + self.stdout_lines if regex.search(line)]

    async def wait_for_log(self, pattern: str, timeout: float = 10.0) -> str:
        """Wait until a line matching ``pattern`` was printed; returns that line."""
        deadline = time.monotonic() + timeout
        while True:
            found = self.lines_matching(pattern)
            if found:
                return found[0]
            if time.monotonic() > deadline:
                raise AssertionError(f"no log line matching {pattern!r} after {timeout} s. Output was:\n{self.output}")
            await asyncio.sleep(0.02)

    # ----- talking to it -----------------------------------------------------------------------------------------
    @property
    def uri(self) -> str:
        return f"tcp://127.0.0.1:{self.port}"

    def client(self, **kwargs: float) -> WyomingClient:
        return WyomingClient("127.0.0.1", self.port, **kwargs)

    async def wait_until_ready(self, timeout: float = START_TIMEOUT_S) -> None:
        """Wait until the bridge answers a Wyoming ``describe`` (what Home Assistant sends first)."""
        client = self.client()
        deadline = time.monotonic() + timeout
        while True:
            if self.process.returncode is not None:
                await asyncio.sleep(0.2)   # let the pumps collect the last lines
                raise AssertionError(f"the bridge exited with code {self.process.returncode} before it was ready:\n{self.output}")
            try:
                await asyncio.wait_for(client.describe(), 3.0)
                return
            except (OSError, asyncio.TimeoutError, ConnectionError):
                if time.monotonic() > deadline:
                    raise AssertionError(f"the bridge did not answer describe within {timeout} s. Output so far:\n{self.output}") from None
                await asyncio.sleep(0.1)

    def assert_clean(self) -> None:
        """No crash reports in the log - a tolerated problem would otherwise hide behind a test that still passes."""
        bad = [line for line in self.stderr_lines + self.stdout_lines
               if TRACEBACK in line or "could not be set up" in line or "could NOT be installed" in line
               or "could NOT be started" in line]
        assert not bad, "the bridge logged problems:\n" + "\n".join(self.stderr_lines[-200:])

    async def stop(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), STOP_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        await asyncio.gather(*self._pumps, return_exceptions=True)


class BridgeFactory:
    """``await bridge(HAZEL_STT_EARLY="1", ...)`` starts a bridge; every one is stopped after the test."""

    def __init__(self, backend: StubBackend, workdir: Path) -> None:
        self.backend = backend
        self.workdir = workdir
        self.started: list[Bridge] = []

    def environment(self, port: int, overrides: dict[str, str | None]) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUBBED_PREFIXES) and k not in _SCRUBBED_NAMES}
        env.update({
            "PYTHONPATH": os.pathsep.join(filter(None, [str(PACKAGE_ROOT), env.get("PYTHONPATH", "")])),
            "PYTHONUNBUFFERED": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "WYOMING_URI": f"tcp://127.0.0.1:{port}",
            "WYOMING_LANGUAGES": "en",
            "STT_OPENAI_URL": self.backend.base_url,
            "TTS_OPENAI_URL": self.backend.base_url,
            "STT_MODELS": "whisper-1",
            "STT_BACKEND": "OPENAI",
            "TTS_MODELS": "kokoro",
            "TTS_STREAMING_MODELS": "kokoro",
            "TTS_VOICES": "af_hazel af_heart",
            "TTS_BACKEND": "OPENAI",
        })
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    async def _spawn(self, args: list[str] | None, overrides: dict[str, str | None], allow_errors: bool) -> Bridge:
        port = free_port()
        env = self.environment(port, overrides)
        command = args or [sys.executable, "-m", "wyoming_openai_hazel"]
        process = await asyncio.create_subprocess_exec(
            *command, env=env, cwd=self.workdir, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        bridge = Bridge(process, port, env, allow_errors)
        self.started.append(bridge)
        return bridge

    async def __call__(self, *, args: list[str] | None = None, wait: bool = True, allow_errors: bool = False, warm: bool = False,
                       **overrides: str | None) -> Bridge:
        """Start a bridge. Keyword arguments are environment variables (``None`` removes one): ``HAZEL_STT_EARLY="1"``.

        ``warm=True`` also sends one throw-away request of each kind first (see ``warm_up``).
        """
        bridge = await self._spawn(args, overrides, allow_errors)
        if wait:
            await bridge.wait_until_ready()
            if warm:
                await self.warm_up(bridge)
        return bridge

    async def warm_up(self, bridge: Bridge) -> None:
        """One throw-away request of each kind, so the first request a test measures does not also pay for the bridge's lazy
        imports and connection set-up - on a busy machine that can take longer than the margins of the timing tests.

        The stub is put back exactly as it was (settings and records), whatever the test had prepared.
        """
        backend = self.backend
        settings = ("stt_delay_s", "stt_fail_first", "n_chunks", "chunk_delay_s")
        saved = {name: getattr(backend, name) for name in settings}
        backend.stt_delay_s, backend.stt_fail_first, backend.n_chunks, backend.chunk_delay_s = 0.0, 0, 1, 0.0
        try:
            client = bridge.client()
            await client.transcribe(speech(300) + silence(200), realtime=False)    # too little silence to start an early request
            await client.synthesize("Warming up. One more sentence.")
        finally:
            backend.reset()
            for name, value in saved.items():
                setattr(backend, name, value)

    async def run_to_exit(self, *, args: list[str] | None = None, timeout: float = 60.0,
                          **overrides: str | None) -> tuple[int, str]:
        """Start a bridge that is expected to refuse to start. Returns ``(exit code, everything it printed)``."""
        bridge = await self._spawn(args, overrides, allow_errors=True)
        try:
            code = await asyncio.wait_for(bridge.process.wait(), timeout)
        except asyncio.TimeoutError:
            raise AssertionError(f"the bridge was expected to exit but kept running. Output:\n{bridge.output}") from None
        await asyncio.gather(*bridge._pumps, return_exceptions=True)
        return code, bridge.output

    async def stop_all(self) -> None:
        for bridge in self.started:
            await bridge.stop()
