"""Streaming speech: the first audio reaches Home Assistant while the speech server is still producing the rest.

Upstream already does this (TTS_STREAMING_MODELS). The extras sit in the same code path, so this proves they did not break it:
the first audio chunk must arrive well before the last one, and before the backend has finished.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from stubs import tts_pcm
from wyoming_client import Synthesis

SENTENCES = ["The kettle is boiling now.", "The back door is open.", "The garden lamp is on."]
ONE = "The kettle is boiling now."

SETUPS = [
    pytest.param({}, id="extras-off"),
    pytest.param({"HAZEL_TTS_CONCURRENCY": "1"}, id="concurrency-1"),
    pytest.param({"HAZEL_TTS_CONCURRENCY": "2", "HAZEL_STT_EARLY": "1", "HAZEL_TTS_VOICE_LABELS": "af_hazel=Hazel",
                  "HAZEL_GPU_WAKE_FILE": "{tmp}/wake"}, id="all-extras-on"),
]


@pytest.fixture(autouse=True)
def _six_pieces_a_tenth_of_a_second_apart(backend) -> None:
    backend.n_chunks, backend.chunk_delay_s = 6, 0.1          # one sentence takes ~0.5 s to produce


def settings(env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    return {key: value.format(tmp=tmp_path) for key, value in env.items()}


def check_format_and_timestamps(result: Synthesis) -> None:
    start = result.audio_start
    assert start is not None, "no audio-start before the audio"
    assert (start.rate, start.width, start.channels) == (24000, 2, 1)
    assert result.chunks, "no audio came back"
    assert result.event_types[0] == "audio-start"
    assert result.event_types[-2:] == ["audio-stop", "synthesize-stopped"]
    assert set(result.event_types[1:-2]) == {"audio-chunk"}
    elapsed_ms = 0.0
    for _, chunk in result.chunks:
        assert (chunk.rate, chunk.width, chunk.channels) == (24000, 2, 1)
        assert chunk.timestamp is not None and abs(chunk.timestamp - elapsed_ms) <= 1.0, (
            f"chunk timestamp {chunk.timestamp} ms does not match {elapsed_ms:.1f} ms of audio already sent")
        elapsed_ms += chunk.samples / chunk.rate * 1000
    arrivals = [t for t, _ in result.chunks]
    assert arrivals == sorted(arrivals)


@pytest.mark.parametrize("env", SETUPS)
async def test_the_first_audio_arrives_long_before_the_last(bridge, backend, tmp_path: Path, env) -> None:
    b = await bridge(**settings(env, tmp_path))
    result = await b.client().synthesize(" ".join(SENTENCES))

    check_format_and_timestamps(result)
    assert result.pcm == b"".join(tts_pcm(s, n_chunks=6) for s in SENTENCES)
    assert result.last_chunk_at - result.first_chunk_at >= 0.3, "the audio was held back and sent in one go"
    backend_done = max(r.finished for r in backend.tts_requests)
    assert result.first_chunk_at <= backend_done - 0.2, "the first audio waited until the speech server had finished"


@pytest.mark.parametrize("env", SETUPS)
async def test_one_sentence_is_passed_on_while_it_is_still_being_made(bridge, backend, tmp_path: Path, env) -> None:
    b = await bridge(**settings(env, tmp_path))
    result = await b.client().synthesize(ONE)

    check_format_and_timestamps(result)
    assert len(backend.tts_requests) == 1
    request = backend.tts_requests[0]
    assert result.first_chunk_at < request.finished - 0.2, "the audio waited for the speech server to finish the sentence"
    assert result.last_chunk_at - result.first_chunk_at >= 0.3     # 6 pieces, 0.1 s apart => the answer lasts ~0.5 s


async def test_audio_starts_while_the_text_is_still_arriving(bridge, backend) -> None:
    """How Home Assistant sends the answer of a language model: many small pieces of text, a tenth of a second apart."""
    b = await bridge()
    words = [word + " " for word in " ".join(SENTENCES).split()]
    result = await b.client().synthesize(words, piece_delay_s=0.1)

    check_format_and_timestamps(result)
    assert result.pcm == b"".join(tts_pcm(s, n_chunks=6) for s in SENTENCES)    # and nothing was said twice
    # The first sentence is complete as soon as the second one begins (word 6 of 15), so speech starts ~0.8 s before the text ends.
    assert result.first_chunk_at <= result.request_sent - 0.3, "speech waited for the whole text"
