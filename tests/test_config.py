"""HazelConfig: the defaults, every variable, and what a typo does (warn, keep the default, never stop the bridge)."""

from __future__ import annotations

import dataclasses
import logging

import pytest
from wyoming_openai_hazel.config import HazelConfig


def load(**env: str) -> HazelConfig:
    """Read settings from exactly these variables (never from the real environment of the machine running the tests)."""
    return HazelConfig.from_env(env)


def warned(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


def test_nothing_set_means_every_extra_is_off_except_the_timing_log() -> None:
    cfg = load()
    assert cfg.gpu_wake_file == ""
    assert cfg.early_stt is False
    assert (cfg.early_stt_silence_ms, cfg.early_stt_min_speech_ms, cfg.early_stt_resume_ms, cfg.early_stt_max_passes) == (300, 250, 100, 3)
    assert cfg.tts_concurrency is None          # None = leave upstream's own limit alone
    assert dict(cfg.voice_labels) == {}
    assert cfg.log_timing is True
    assert cfg.active() == ["timing-log"]
    assert cfg == HazelConfig()                 # "nothing set" and the class defaults are the same thing


def test_settings_of_other_programs_are_ignored() -> None:
    assert load(STT_MODELS="whisper-1", TTS_VOICES="a b", WYOMING_URI="tcp://0.0.0.0:1") == HazelConfig()


def test_from_env_without_arguments_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAZEL_TTS_CONCURRENCY", "4")
    monkeypatch.setenv("HAZEL_STT_EARLY", "1")
    cfg = HazelConfig.from_env()
    assert cfg.tts_concurrency == 4 and cfg.early_stt is True


def test_the_config_cannot_be_changed_after_it_is_read() -> None:
    # One config object is shared by every connection of the bridge; changing it in one place would change it everywhere.
    with pytest.raises(dataclasses.FrozenInstanceError):
        load().early_stt = True  # type: ignore[misc]


# ----- yes/no settings -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", " 1 "])
def test_early_stt_accepts_yes(raw: str, caplog: pytest.LogCaptureFixture) -> None:
    assert load(HAZEL_STT_EARLY=raw).early_stt is True
    assert not warned(caplog)


@pytest.mark.parametrize("raw", ["0", "false", "No", "off", "", "   "])
def test_early_stt_accepts_no(raw: str, caplog: pytest.LogCaptureFixture) -> None:
    assert load(HAZEL_STT_EARLY=raw).early_stt is False
    assert not warned(caplog)


@pytest.mark.parametrize("raw", ["maybe", "2", "enabled"])
def test_a_yes_no_typo_warns_and_keeps_the_default(raw: str, caplog: pytest.LogCaptureFixture) -> None:
    assert load(HAZEL_STT_EARLY=raw).early_stt is False         # default off stays off
    assert any("HAZEL_STT_EARLY" in w and raw in w for w in warned(caplog))
    caplog.clear()
    assert load(HAZEL_LOG_TIMING=raw).log_timing is True        # default on stays on
    assert any("HAZEL_LOG_TIMING" in w for w in warned(caplog))


def test_the_timing_log_can_be_switched_off() -> None:
    cfg = load(HAZEL_LOG_TIMING="0")
    assert cfg.log_timing is False
    assert cfg.active() == []


# ----- number settings -----------------------------------------------------------------------------------------------
NUMBERS = [
    ("HAZEL_STT_EARLY_SILENCE_MS", "early_stt_silence_ms", 300, 50),
    ("HAZEL_STT_EARLY_MIN_SPEECH_MS", "early_stt_min_speech_ms", 250, 0),
    ("HAZEL_STT_EARLY_RESUME_MS", "early_stt_resume_ms", 100, 10),
    ("HAZEL_STT_EARLY_MAX_PASSES", "early_stt_max_passes", 3, 1),
]


@pytest.mark.parametrize(("name", "attr", "default", "minimum"), NUMBERS)
def test_number_settings(name: str, attr: str, default: int, minimum: int, caplog: pytest.LogCaptureFixture) -> None:
    assert getattr(load(**{name: str(minimum)}), attr) == minimum        # the smallest allowed value is accepted
    assert getattr(load(**{name: " 777 "}), attr) == 777                 # stray spaces are fine
    assert getattr(load(**{name: ""}), attr) == default                  # an empty value is the same as not set
    assert not warned(caplog)
    for bad in ("abc", "1.5", str(minimum - 1)):                         # not a whole number / below the minimum
        caplog.clear()
        assert getattr(load(**{name: bad}), attr) == default, bad
        assert any(name in w for w in warned(caplog)), bad


def test_tts_concurrency(caplog: pytest.LogCaptureFixture) -> None:
    assert load().tts_concurrency is None
    assert load(HAZEL_TTS_CONCURRENCY="").tts_concurrency is None
    assert load(HAZEL_TTS_CONCURRENCY="1").tts_concurrency == 1
    assert load(HAZEL_TTS_CONCURRENCY=" 8 ").tts_concurrency == 8        # more than upstream's 3 is allowed
    assert not warned(caplog)
    for bad in ("0", "-2", "two", "1.5"):
        caplog.clear()
        assert load(HAZEL_TTS_CONCURRENCY=bad).tts_concurrency is None, bad     # falls back to upstream's own limit
        assert any("HAZEL_TTS_CONCURRENCY" in w for w in warned(caplog)), bad


# ----- text settings -------------------------------------------------------------------------------------------------
def test_gpu_wake_file() -> None:
    assert load(HAZEL_GPU_WAKE_FILE="/run/hazel/wake").gpu_wake_file == "/run/hazel/wake"
    assert load(HAZEL_GPU_WAKE_FILE="  /run/hazel/wake \n").gpu_wake_file == "/run/hazel/wake"
    assert load(HAZEL_GPU_WAKE_FILE="   ").gpu_wake_file == ""            # blank = off


def test_voice_labels_are_read_and_bad_entries_are_dropped(caplog: pytest.LogCaptureFixture) -> None:
    cfg = load(HAZEL_TTS_VOICE_LABELS="af_hazel=Hazel;af_heart=Heart (original);broken;=nameless")
    assert dict(cfg.voice_labels) == {"af_hazel": "Hazel", "af_heart": "Heart (original)"}
    assert len(warned(caplog)) == 2                                      # one warning per dropped entry
    assert dict(load(HAZEL_TTS_VOICE_LABELS="").voice_labels) == {}


# ----- one typo does not disturb the rest ----------------------------------------------------------------------------
def test_a_typo_in_one_setting_leaves_the_others_alone(caplog: pytest.LogCaptureFixture) -> None:
    cfg = load(HAZEL_STT_EARLY="yes", HAZEL_STT_EARLY_SILENCE_MS="soon", HAZEL_TTS_CONCURRENCY="2", HAZEL_GPU_WAKE_FILE="/w")
    assert cfg.early_stt is True
    assert cfg.early_stt_silence_ms == 300
    assert cfg.tts_concurrency == 2
    assert cfg.gpu_wake_file == "/w"
    assert len(warned(caplog)) == 1


# ----- the start-up banner -------------------------------------------------------------------------------------------
def test_active_names_exactly_the_extras_that_are_on() -> None:
    cfg = load(HAZEL_GPU_WAKE_FILE="/run/hazel/wake", HAZEL_STT_EARLY="1", HAZEL_STT_EARLY_SILENCE_MS="450",
               HAZEL_TTS_CONCURRENCY="2", HAZEL_TTS_VOICE_LABELS="a=A;b=B", HAZEL_LOG_TIMING="1")
    assert sorted(cfg.active()) == sorted(["gpu-wake(/run/hazel/wake)", "early-stt(silence 450 ms)", "tts-concurrency(2)",
                                           "voice-labels(2)", "timing-log"])


@pytest.mark.parametrize(("env", "expected"), [
    ({"HAZEL_GPU_WAKE_FILE": "/w"}, "gpu-wake(/w)"),
    ({"HAZEL_STT_EARLY": "1"}, "early-stt(silence 300 ms)"),
    ({"HAZEL_TTS_CONCURRENCY": "3"}, "tts-concurrency(3)"),
    ({"HAZEL_TTS_VOICE_LABELS": "a=A"}, "voice-labels(1)"),
])
def test_each_extra_shows_up_in_the_banner_only_when_it_is_set(env: dict[str, str], expected: str) -> None:
    assert expected in load(**env).active()
    assert not any(item.split("(")[0] == expected.split("(")[0] for item in load().active())
