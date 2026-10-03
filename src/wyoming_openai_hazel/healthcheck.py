"""Docker health check: ask the bridge for its Wyoming ``info`` (what Home Assistant does when it connects).

``python -m wyoming_openai_hazel.healthcheck`` exits 0 when the bridge answers, 1 when it does not. The same handshake
(``probe``) is what the optional MQTT status beacon (``beacon.py``) repeats every few seconds.
"""

from __future__ import annotations

import asyncio
import os
import sys

from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info

DEFAULT_URI = "tcp://0.0.0.0:10300"
PROBE_TIMEOUT_S = 4.0


def wyoming_port(uri: str | None = None) -> int:
    """The port the bridge listens on: the number after the last ``:`` of ``WYOMING_URI`` (default 10300)."""
    uri = os.environ.get("WYOMING_URI", DEFAULT_URI) if uri is None else uri
    return int(uri.rsplit(":", 1)[-1])


async def _handshake(host: str, port: int) -> bool:
    async with AsyncTcpClient(host, port) as client:
        await client.write_event(Describe().event())
        while True:
            event = await client.read_event()
            if event is None:
                return False
            if Info.is_type(event.type):
                return True


async def probe(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """True if a Wyoming server at ``host:port`` answers ``describe`` with ``info`` within ``timeout`` seconds.

    Never raises: a refused, reset, silent or garbled connection is just False.
    """
    try:
        return await asyncio.wait_for(_handshake(host, port), timeout)
    except Exception:
        return False


def main() -> None:
    port = wyoming_port()
    try:
        ok = asyncio.run(probe("127.0.0.1", port))
    except Exception:
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
