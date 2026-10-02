"""HAZEL_TTS_CONCURRENCY: how many sentences are synthesized at the same time.

Upstream always allows 3. A long text arrives as several sentences; the bridge asks the speech server for all of them at once
(up to the limit) and plays them in order. The backend stub counts how many requests it served at the same moment.
"""

from __future__ import annotations

import sys

import pytest
from stubs import tts_frequency, tts_pcm
from wyoming_client import Synthesis

FOUR = ["The kettle is boiling now.", "The back door is open.", "The garden lamp is on.", "The fan upstairs is off."]
SEVEN = [f"Sentence number {word} is here." for word in ("one", "two", "three", "four", "five", "six", "seven")]


def expected_pcm(backend, sentences: list[str]) -> bytes:
    """What the stub's answers add up to when the sentences are played in order."""
    return b"".join(tts_pcm(s, n_chunks=backend.n_chunks, chunk_samples=backend.chunk_samples) for s in sentences)


def asked_for(backend) -> list[str]:
    return sorted(" ".join(r.input.split()) for r in backend.tts_requests)


def check_complete_and_in_order(result: Synthesis, backend, sentences: list[str]) -> None:
    assert len({tts_frequency(s) for s in sentences}) == len(sentences), "the test sentences must sound different"
    assert asked_for(backend) == sorted(sentences), "the text was not split into the expected sentences"
    assert result.synthesize_stopped and result.audio_stopped
    assert result.pcm == expected_pcm(backend, sentences), "audio is missing, repeated or out of order"


@pytest.fixture(autouse=True)
def _slow_enough_to_overlap(backend) -> None:
    # Every answer takes ~0.4 s, far longer than the few milliseconds it takes the bridge to start all of them.
    backend.n_chunks, backend.chunk_delay_s = 5, 0.1


@pytest.mark.parametrize(("limit", "sentences", "expected_in_flight"), [
    pytest.param("1", FOUR, 1, id="limit-1"),
    pytest.param("2", FOUR, 2, id="limit-2"),
    pytest.param("3", FOUR, 3, id="limit-3"),
    pytest.param("5", SEVEN, 5, id="limit-5-is-above-upstreams-3"),
])
async def test_the_speech_server_never_sees_more_requests_at_once_than_the_limit(bridge, backend, limit, sentences,
                                                                              expected_in_flight) -> None:
    b = await bridge(HAZEL_TTS_CONCURRENCY=limit)
    result = await b.client().synthesize(" ".join(sentences))
    assert backend.tts_max_in_flight <= int(limit), "more requests ran at once than the limit allows"
    assert backend.tts_max_in_flight == expected_in_flight, "the limit was not used up although enough sentences were waiting"
    check_complete_and_in_order(result, backend, sentences)
    assert f"tts-concurrency({limit})" in b.stderr


async def test_without_the_setting_upstreams_limit_of_three_applies(bridge, backend) -> None:
    b = await bridge()
    result = await b.client().synthesize(" ".join(SEVEN))
    assert backend.tts_max_in_flight == 3
    check_complete_and_in_order(result, backend, SEVEN)
    assert "tts-concurrency" not in b.stderr


async def test_without_the_setting_the_bridge_behaves_exactly_like_the_stock_bridge(bridge, backend) -> None:
    ours = await (await bridge()).client().synthesize(" ".join(FOUR))
    ours_in_flight = backend.tts_max_in_flight
    backend.reset()
    stock = await (await bridge(args=[sys.executable, "-m", "wyoming_openai"])).client().synthesize(" ".join(FOUR))
    assert backend.tts_max_in_flight == ours_in_flight > 1
    assert ours.pcm == stock.pcm
    assert [len(c.audio) for _, c in ours.chunks] == [len(c.audio) for _, c in stock.chunks]
    assert ours.event_types == stock.event_types
