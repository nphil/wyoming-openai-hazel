"""Voice display names (HAZEL_TTS_VOICE_LABELS): what Home Assistant's voice picker shows for a voice.

The label is only a display name. The voice id that is sent to the speech server must never change.
"""

from __future__ import annotations

import logging

import pytest
from inproc import make_info
from wyoming.info import Info
from wyoming_openai_hazel.config import parse_voice_labels
from wyoming_openai_hazel.labels import apply_voice_labels


def descriptions(info: Info) -> dict[str, str]:
    return {voice.name: voice.description for program in info.tts for voice in program.voices}


# ----- parsing the setting -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("text", "expected"), [
    ("", {}),
    ("   ", {}),
    ("af_hazel=Hazel", {"af_hazel": "Hazel"}),
    ("af_hazel=Hazel;af_heart=Heart (original)", {"af_hazel": "Hazel", "af_heart": "Heart (original)"}),
    (" af_hazel = Hazel Prime ; af_heart=Heart ", {"af_hazel": "Hazel Prime", "af_heart": "Heart"}),   # outer spaces go, inner stay
    (";;af_hazel=Hazel;;", {"af_hazel": "Hazel"}),                      # extra semicolons
    ("a=1;a=2", {"a": "2"}),                                            # the same id twice: the last one wins
    ("a=b=c", {"a": "b=c"}),                                            # only the first "=" separates id and label
    ("af_hazel=Zoë, the 2nd", {"af_hazel": "Zoë, the 2nd"}),            # any characters in a label
    ("af_hazel (kokoro)=Hazel", {"af_hazel (kokoro)": "Hazel"}),        # ids as upstream shows them with several models
])
def test_parse_voice_labels(text: str, expected: dict[str, str]) -> None:
    assert parse_voice_labels(text) == expected


@pytest.mark.parametrize("text", ["af_hazel", "af_hazel=", "=Hazel", "=", " = ", "a;b;c"])
def test_entries_without_both_an_id_and_a_label_are_ignored_with_a_warning(text: str, caplog: pytest.LogCaptureFixture) -> None:
    assert parse_voice_labels(text) == {}
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


def test_one_bad_entry_does_not_spoil_the_good_ones(caplog: pytest.LogCaptureFixture) -> None:
    assert parse_voice_labels("good=Good;broken;also_good=Also") == {"good": "Good", "also_good": "Also"}
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


# ----- applying labels to the voice list -----------------------------------------------------------------------------
def test_a_label_replaces_the_description_of_that_voice_only() -> None:
    info = make_info()
    assert descriptions(info) == {"af_hazel": "af_hazel", "af_heart": "af_heart"}      # upstream shows the id
    assert apply_voice_labels(info, {"af_hazel": "Hazel"}) == 1
    assert descriptions(info) == {"af_hazel": "Hazel", "af_heart": "af_heart"}


def test_labels_for_unknown_voices_are_ignored() -> None:
    info = make_info()
    assert apply_voice_labels(info, {"nope": "Nope", "af_hazel": "Hazel"}) == 1
    assert sorted(descriptions(info)) == ["af_hazel", "af_heart"]                      # no voice was invented
    assert apply_voice_labels(make_info(), {"nope": "Nope"}) == 0


def test_the_voice_id_and_backend_id_are_never_touched() -> None:
    info = make_info()
    apply_voice_labels(info, {"af_hazel": "Hazel", "af_heart": "Heart"})
    voices = [voice for program in info.tts for voice in program.voices]
    assert [v.name for v in voices] == ["af_hazel", "af_heart"]
    assert [v.backend_voice_name for v in voices] == ["af_hazel", "af_heart"]


def test_applying_twice_changes_nothing_the_second_time() -> None:
    info = make_info()
    assert apply_voice_labels(info, {"af_hazel": "Hazel"}) == 1
    assert apply_voice_labels(info, {"af_hazel": "Hazel"}) == 0
    assert descriptions(info)["af_hazel"] == "Hazel"


def test_with_several_models_a_raw_voice_id_labels_every_copy_and_a_full_name_labels_one() -> None:
    info = make_info(tts_models=("kokoro", "other"), tts_streaming_models=())
    voices = [voice for program in info.tts for voice in program.voices]
    hazels = [v for v in voices if v.backend_voice_name == "af_hazel"]
    hearts = [v for v in voices if v.backend_voice_name == "af_heart"]
    assert len(hazels) == len(hearts) == 2 and len({v.name for v in voices}) == 4     # upstream gives each copy its own name
    apply_voice_labels(info, {"af_hazel": "Hazel"})                                   # the raw id labels every copy...
    assert {v.description for v in hazels} == {"Hazel"}
    assert all(v.description == v.name for v in hearts)
    apply_voice_labels(info, {hearts[1].name: "Just this one"})                       # ...a full name labels just one
    assert hearts[1].description == "Just this one" and hearts[0].description == hearts[0].name


def test_a_voice_list_without_tts_is_fine() -> None:
    assert apply_voice_labels(make_info(tts_models=(), tts_streaming_models=(), voices=()), {"a": "A"}) == 0


# ----- through the real bridge ---------------------------------------------------------------------------------------
async def test_home_assistant_sees_the_labels_and_the_speech_server_still_gets_the_voice_id(bridge, backend) -> None:
    b = await bridge(HAZEL_TTS_VOICE_LABELS="af_hazel=Hazel;af_heart=Heart (original)")
    assert descriptions(await b.client().describe()) == {"af_hazel": "Hazel", "af_heart": "Heart (original)"}
    assert "voice-labels(2)" in b.stderr

    for voice in ("af_hazel", "af_heart"):        # Home Assistant picks a voice by its id (name), not by the label
        backend.reset()
        result = await b.client().synthesize("Hello there.", voice=voice)
        assert result.pcm, "no audio came back"
        assert [r.voice for r in backend.tts_requests] == [voice]


async def test_only_listed_voices_change_and_unknown_ids_are_ignored(bridge) -> None:
    b = await bridge(HAZEL_TTS_VOICE_LABELS="nope=Nope;af_hazel=Hazel")
    info = await b.client().describe()
    assert descriptions(info) == {"af_hazel": "Hazel", "af_heart": "af_heart"}


async def test_without_the_setting_the_voice_list_is_exactly_upstreams(bridge) -> None:
    b = await bridge()
    assert descriptions(await b.client().describe()) == {"af_hazel": "af_hazel", "af_heart": "af_heart"}
    assert "voice-labels" not in b.stderr


async def test_a_malformed_label_setting_never_stops_the_bridge(bridge) -> None:
    b = await bridge(HAZEL_TTS_VOICE_LABELS="af_hazel;=;;af_heart=Heart", allow_errors=True)
    assert descriptions(await b.client().describe()) == {"af_hazel": "af_hazel", "af_heart": "Heart"}
    assert b.lines_matching("Ignoring voice label entry")
