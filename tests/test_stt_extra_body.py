"""STT_EXTRA_BODY is upstream's own setting: a JSON object merged into every transcription request (for example a "prompt" that
helps Whisper with names). The early request must carry it too, and a value upstream refuses must be refused just the same.
"""

from __future__ import annotations

import json
import sys

import pytest
from wyoming_client import silence, speech

PROMPT = "Nitin's Office, Poobot"
LEAD = silence(200)
FULL = LEAD + speech(1000) + silence(1000)                                        # one pause long enough for an early request
DROPPED = LEAD + speech(1000) + silence(700) + speech(600) + silence(200)         # an early request that is dropped, then the normal one
STOCK_BRIDGE = [sys.executable, "-m", "wyoming_openai"]


@pytest.mark.parametrize(("early", "audio", "last_request_is"), [
    pytest.param("0", FULL, "normal", id="normal-request-only"),
    pytest.param("1", FULL, "early", id="early-request-only"),
    pytest.param("1", DROPPED, "normal", id="dropped-early-request-then-normal"),
])
@pytest.mark.parametrize("where", ["extra-body", "prompt-setting"])
async def test_the_prompt_reaches_every_request_to_the_speech_to_text_server(bridge, backend, early, audio, last_request_is,
                                                                           where) -> None:
    backend.stt_delay_s = 1.0
    prompt_env = ({"STT_EXTRA_BODY": json.dumps({"prompt": PROMPT})} if where == "extra-body" else {"STT_PROMPT": PROMPT})
    b = await bridge(warm=True, HAZEL_STT_EARLY=early, **prompt_env)
    text, stamps = await b.client().transcribe(audio)

    # A dropped early request may not have got to the speech server before it was dropped; every request that did carries the prompt.
    assert 1 <= len(backend.stt_requests) <= (2 if audio is DROPPED else 1)
    assert [r.fields.get("prompt") for r in backend.stt_requests] == [PROMPT] * len(backend.stt_requests)
    last = backend.stt_requests[-1]
    assert (last.arrived < stamps.audio_stop_sent - 0.3) == (last_request_is == "early")
    assert text.startswith("heard ")


async def test_every_field_of_the_extra_body_arrives_on_the_early_request(bridge, backend) -> None:
    extra = {"prompt": PROMPT, "hotwords": "Poobot", "beam_size": 5, "vad_filter": True}
    b = await bridge(HAZEL_STT_EARLY="1", STT_EXTRA_BODY=json.dumps(extra))
    _, stamps = await b.client().transcribe(FULL)
    (request,) = backend.stt_requests
    assert request.arrived < stamps.audio_stop_sent - 0.3

    backend.reset()                                           # the stock bridge turns the same setting into these form fields
    stock = await bridge(args=STOCK_BRIDGE, STT_EXTRA_BODY=json.dumps(extra))
    await stock.client().transcribe(FULL)
    (stock_request,) = backend.stt_requests
    assert request.fields == stock_request.fields
    assert request.fields["prompt"] == PROMPT and request.fields["hotwords"] == "Poobot" and "beam_size" in request.fields


# ----- values upstream refuses -----------------------------------------------------------------------------------------------
REFUSED = {
    "not-json": "{not json",
    "a-list-not-an-object": "[1, 2]",
    "response-format-other-than-json": '{"response_format": "text"}',
    "stream-is-not-a-boolean": '{"stream": "yes"}',
}


def error_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if "error:" in line]


@pytest.mark.parametrize("value", REFUSED.values(), ids=REFUSED.keys())
async def test_a_bad_value_is_refused_exactly_as_upstream_refuses_it(bridge, value) -> None:
    stock_code, stock_output = await bridge.run_to_exit(args=STOCK_BRIDGE, STT_EXTRA_BODY=value)
    code, output = await bridge.run_to_exit(STT_EXTRA_BODY=value, HAZEL_STT_EARLY="1")

    assert stock_code != 0, "upstream is expected to refuse this value"
    assert code == stock_code
    assert error_lines(output) and error_lines(output) == error_lines(stock_output)
    assert "extra" in error_lines(output)[0].lower()


async def test_values_upstream_accepts_are_accepted(bridge, backend) -> None:
    b = await bridge(STT_EXTRA_BODY='{"response_format": "json"}', HAZEL_STT_EARLY="1")
    text, _ = await b.client().transcribe(speech(400) + silence(400), realtime=False)
    assert text.startswith("heard ") and backend.stt_requests[0].fields["response_format"] == "json"

    empty = await bridge(STT_EXTRA_BODY="")                    # an empty setting means "none"
    assert (await empty.client().describe()).asr
