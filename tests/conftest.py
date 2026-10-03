"""Shared fixtures.

* ``backend``  - a stub Whisper/Kokoro server (see stubs.py); tests change its settings before they use it.
* ``bridge``   - starts the REAL entrypoint (``python -m wyoming_openai_hazel``) as a separate process, wired to ``backend``, and
                 stops it again after the test. Tests talk to it over the network, exactly like Home Assistant does.
* ``mqtt_broker`` - a stand-in MQTT broker (see mqtt_broker.py) for the Home Assistant on/off sensor. Ask for it BEFORE ``bridge``
                 (so the bridge is stopped first); the test fails if the bridge broke the MQTT standard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from bridge_process import BridgeFactory
from mqtt_broker import FakeBroker
from stubs import StubBackend

HARD_TEST_TIMEOUT_S = 180     # no test may hang the build


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "timeout(seconds): fail the test if it takes longer (pytest-timeout)")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(HARD_TEST_TIMEOUT_S))


@pytest_asyncio.fixture
async def backend():
    stub = StubBackend()
    await stub.start()
    try:
        yield stub
    finally:
        await stub.stop()


@pytest_asyncio.fixture
async def mqtt_broker():
    broker = FakeBroker()
    await broker.start()
    try:
        yield broker
    finally:
        await broker.stop()
    assert not broker.errors, "the client broke the MQTT standard: " + "; ".join(broker.errors)


@pytest_asyncio.fixture
async def bridge(backend: StubBackend, tmp_path: Path):
    factory = BridgeFactory(backend, tmp_path)
    try:
        yield factory
        for running in factory.started:
            if not running.allow_errors and running.process.returncode is None:
                running.assert_clean()
    finally:
        await factory.stop_all()
