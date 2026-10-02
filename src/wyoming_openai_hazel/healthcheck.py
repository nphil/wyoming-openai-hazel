"""Docker health check: ask the bridge for its Wyoming ``info`` (what Home Assistant does when it connects)."""

from __future__ import annotations

import asyncio
import os
import sys

from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info


async def _probe(host: str, port: int) -> bool:
    async with AsyncTcpClient(host, port) as client:
        await client.write_event(Describe().event())
        while True:
            event = await client.read_event()
            if event is None:
                return False
            if Info.is_type(event.type):
                return True


def main() -> None:
    port = int(os.environ.get("WYOMING_URI", "tcp://0.0.0.0:10300").rsplit(":", 1)[-1])
    try:
        ok = asyncio.run(asyncio.wait_for(_probe("127.0.0.1", port), 4))
    except Exception:
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
