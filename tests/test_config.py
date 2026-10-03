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
    ("HAZEL_MQTT_PORT", "mqtt_port", 1883, 1),
    ("HAZEL_MQTT_KEEPALIVE", "mqtt_keepalive", 20, 5),
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
               HAZEL_TTS_CONCURRENCY="2", HAZEL_TTS_VOICE_LABELS="a=A;b=B", HAZEL_LOG_TIMING="1",
               HAZEL_MQTT_HOST="broker.lan", HAZEL_MQTT_PORT="1884")
    assert sorted(cfg.active()) == sorted(["gpu-wake(/run/hazel/wake)", "early-stt(silence 450 ms)", "tts-concurrency(2)",
                                           "voice-labels(2)", "mqtt-status(broker.lan:1884 id=main)", "timing-log"])


@pytest.mark.parametrize(("env", "expected"), [
    ({"HAZEL_GPU_WAKE_FILE": "/w"}, "gpu-wake(/w)"),
    ({"HAZEL_STT_EARLY": "1"}, "early-stt(silence 300 ms)"),
    ({"HAZEL_TTS_CONCURRENCY": "3"}, "tts-concurrency(3)"),
    ({"HAZEL_TTS_VOICE_LABELS": "a=A"}, "voice-labels(1)"),
    ({"HAZEL_MQTT_HOST": "192.168.1.146"}, "mqtt-status(192.168.1.146:1883 id=main)"),
])
def test_each_extra_shows_up_in_the_banner_only_when_it_is_set(env: dict[str, str], expected: str) -> None:
    assert expected in load(**env).active()
    assert not any(item.split("(")[0] == expected.split("(")[0] for item in load().active())


# ----- the Home Assistant on/off sensor (MQTT beacon) ----------------------------------------------------------------
def test_the_sensor_is_off_unless_a_host_is_named() -> None:
    cfg = load()
    assert cfg.mqtt_host == ""
    assert (cfg.mqtt_port, cfg.mqtt_user, cfg.mqtt_password) == (1883, "", "")
    assert (cfg.mqtt_id, cfg.mqtt_name, cfg.mqtt_discovery_prefix) == ("main", "Hazel voice bridge", "homeassistant")
    assert (cfg.mqtt_keepalive, cfg.mqtt_check_seconds) == (20, 10)
    # Its other settings alone switch nothing on, and an empty host is the same as none.
    only_others = load(HAZEL_MQTT_HOST="", HAZEL_MQTT_PORT="1884", HAZEL_MQTT_USER="u", HAZEL_MQTT_PASSWORD="p", HAZEL_MQTT_ID="x")
    assert only_others.mqtt_host == "" and only_others.active() == ["timing-log"]


def test_every_sensor_setting_is_read() -> None:
    cfg = load(HAZEL_MQTT_HOST=" 192.168.1.146 ", HAZEL_MQTT_PORT="8883", HAZEL_MQTT_USER=" hazel ", HAZEL_MQTT_PASSWORD=" pw ",
               HAZEL_MQTT_ID="kitchen_2-B", HAZEL_MQTT_NAME=" Kitchen bridge ", HAZEL_MQTT_DISCOVERY_PREFIX="ha/discovery",
               HAZEL_MQTT_KEEPALIVE="30", HAZEL_MQTT_CHECK_SECONDS="12")
    assert (cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_user, cfg.mqtt_password) == ("192.168.1.146", 8883, "hazel", "pw")
    assert (cfg.mqtt_id, cfg.mqtt_name, cfg.mqtt_discovery_prefix) == ("kitchen_2-B", "Kitchen bridge", "ha/discovery")
    assert (cfg.mqtt_keepalive, cfg.mqtt_check_seconds) == (30, 12)


@pytest.mark.parametrize("bad", ["mqtt://broker", "broker/path", "two words", "http://x:1883", "a..b", "a" * 64 + ".lan"])
def test_a_host_that_is_not_a_host_name_keeps_the_sensor_off_with_a_warning(bad: str, caplog: pytest.LogCaptureFixture) -> None:
    cfg = load(HAZEL_MQTT_HOST=bad)
    assert cfg.mqtt_host == "" and not any(item.startswith("mqtt-status") for item in cfg.active())
    assert any("HAZEL_MQTT_HOST" in w for w in warned(caplog))


@pytest.mark.parametrize("good", ["main", "a", "A_b-9", "kitchen-2_b", "0", "UPPER"])
def test_an_id_of_letters_digits_underscore_and_dash_is_accepted(good: str, caplog: pytest.LogCaptureFixture) -> None:
    assert load(HAZEL_MQTT_ID=good).mqtt_id == good
    assert not warned(caplog)


@pytest.mark.parametrize("bad", ["has space", "slash/x", "plus+", "hash#", "dot.dot", "wild*", "ümlaut", "emoji😀", "a\tb", "x\ny"])
def test_any_other_id_is_replaced_by_main_with_a_warning(bad: str, caplog: pytest.LogCaptureFixture) -> None:
    assert load(HAZEL_MQTT_ID=bad).mqtt_id == "main"                    # it ends up in topics and entity ids: nothing else is safe
    assert any("HAZEL_MQTT_ID" in w for w in warned(caplog))
    caplog.clear()
    assert load(HAZEL_MQTT_ID="  ").mqtt_id == "main"                  # blank is "not set", not "wrong"
    assert not warned(caplog)


@pytest.mark.parametrize("good", ["homeassistant", "ha/discovery", "my-prefix", "a/b/c"])
def test_a_discovery_prefix_of_plain_levels_is_accepted(good: str, caplog: pytest.LogCaptureFixture) -> None:
    assert load(HAZEL_MQTT_DISCOVERY_PREFIX=good).mqtt_discovery_prefix == good
    assert not warned(caplog)


@pytest.mark.parametrize("bad", ["/lead", "trail/", "double//slash", "with space", "plus+", "hash#", "#", "a/+/b"])
def test_a_discovery_prefix_that_cannot_be_a_topic_prefix_is_replaced_with_a_warning(bad: str,
                                                                                      caplog: pytest.LogCaptureFixture) -> None:
    assert load(HAZEL_MQTT_DISCOVERY_PREFIX=bad).mqtt_discovery_prefix == "homeassistant"
    assert any("HAZEL_MQTT_DISCOVERY_PREFIX" in w for w in warned(caplog))


def test_hosts_of_every_usual_kind_are_accepted(caplog: pytest.LogCaptureFixture) -> None:
    for good in ("192.168.1.146", "homeassistant.local", "mqtt", "broker.example.com.", "fe80::1", "a" * 63 + ".lan", "my_broker"):
        assert load(HAZEL_MQTT_HOST=good).mqtt_host == good, good
    assert not warned(caplog)


def test_the_keepalive_must_fit_the_16_bits_mqtt_gives_it(caplog: pytest.LogCaptureFixture) -> None:
    assert load(HAZEL_MQTT_KEEPALIVE="65535").mqtt_keepalive == 65535
    assert not warned(caplog)
    assert load(HAZEL_MQTT_KEEPALIVE="65536").mqtt_keepalive == 20       # the connect packet could not carry it
    assert any("HAZEL_MQTT_KEEPALIVE" in w for w in warned(caplog))


def test_the_port_must_be_a_real_port(caplog: pytest.LogCaptureFixture) -> None:
    assert load(HAZEL_MQTT_PORT="65535").mqtt_port == 65535
    assert not warned(caplog)
    for bad in ("65536", "70000", "-1"):                                 # a port outside 1..65535 would crash paho's network thread
        caplog.clear()
        assert load(HAZEL_MQTT_PORT=bad).mqtt_port == 1883, bad
        assert any("HAZEL_MQTT_PORT" in w for w in warned(caplog)), bad


def test_the_name_is_free_text_and_blank_means_the_default(caplog: pytest.LogCaptureFixture) -> None:
    assert load(HAZEL_MQTT_NAME="Küche: Hazel (Unraid)").mqtt_name == "Küche: Hazel (Unraid)"
    assert load(HAZEL_MQTT_NAME="   ").mqtt_name == "Hazel voice bridge"
    assert not warned(caplog)


def test_the_check_interval_is_never_more_than_half_the_keepalive(caplog: pytest.LogCaptureFixture) -> None:
    # The check also keeps the connection busy, so the broker never sees a silence as long as the keep-alive.
    assert load(HAZEL_MQTT_CHECK_SECONDS="2").mqtt_check_seconds == 2                   # the smallest allowed value
    assert load(HAZEL_MQTT_KEEPALIVE="40", HAZEL_MQTT_CHECK_SECONDS="20").mqtt_check_seconds == 20      # exactly half is fine
    assert load(HAZEL_MQTT_KEEPALIVE="7", HAZEL_MQTT_CHECK_SECONDS="3").mqtt_check_seconds == 3
    assert not warned(caplog)
    # The default follows a short keep-alive without comment ...
    assert load(HAZEL_MQTT_KEEPALIVE="6").mqtt_check_seconds == 3
    assert load(HAZEL_MQTT_KEEPALIVE="5").mqtt_check_seconds == 2
    assert not warned(caplog)
    # ... but a value somebody typed is cut down with a warning.
    cfg = load(HAZEL_MQTT_KEEPALIVE="20", HAZEL_MQTT_CHECK_SECONDS="30")
    assert cfg.mqtt_check_seconds == 10
    assert any("HAZEL_MQTT_CHECK_SECONDS" in w and "HAZEL_MQTT_KEEPALIVE" in w for w in warned(caplog))
    caplog.clear()
    for bad in ("1", "0", "often", "2.5"):
        assert load(HAZEL_MQTT_CHECK_SECONDS=bad).mqtt_check_seconds == 10, bad       # below 2 / not a whole number: the default
        assert any("HAZEL_MQTT_CHECK_SECONDS" in w for w in warned(caplog)), bad
        caplog.clear()


def test_a_password_needs_a_user_name(caplog: pytest.LogCaptureFixture) -> None:
    cfg = load(HAZEL_MQTT_HOST="h", HAZEL_MQTT_PASSWORD="very-secret-password")
    assert cfg.mqtt_password == "" and cfg.mqtt_user == ""                 # MQTT cannot send a password without a user name
    assert any("HAZEL_MQTT_PASSWORD" in w for w in warned(caplog))
    assert not any("very-secret" in w for w in warned(caplog))
    caplog.clear()
    assert load(HAZEL_MQTT_USER="only-a-user").mqtt_user == "only-a-user" and not warned(caplog)
    both = load(HAZEL_MQTT_USER="u", HAZEL_MQTT_PASSWORD="p")
    assert (both.mqtt_user, both.mqtt_password) == ("u", "p") and not warned(caplog)


def test_the_user_name_and_the_password_never_show_in_the_settings_text_the_banner_or_the_warnings(
        caplog: pytest.LogCaptureFixture) -> None:
    cfg = load(HAZEL_MQTT_HOST="h", HAZEL_MQTT_USER="very-secret-user", HAZEL_MQTT_PASSWORD="very-secret-password",
               HAZEL_MQTT_PORT="oops", HAZEL_MQTT_ID="bad id", HAZEL_MQTT_KEEPALIVE="1")
    shown = [repr(cfg), str(cfg), " ".join(cfg.active()), " ".join(warned(caplog))]
    assert len(warned(caplog)) == 3
    assert not [text for text in shown if "very-secret" in text]


def test_the_banner_names_the_broker_and_the_id_only_when_a_host_is_set() -> None:
    assert load(HAZEL_MQTT_HOST="192.168.1.146").active() == ["mqtt-status(192.168.1.146:1883 id=main)", "timing-log"]
    cfg = load(HAZEL_MQTT_HOST="broker.lan", HAZEL_MQTT_PORT="8883", HAZEL_MQTT_ID="kitchen")
    assert "mqtt-status(broker.lan:8883 id=kitchen)" in cfg.active()
    assert not any(item.startswith("mqtt-status") for item in load(HAZEL_MQTT_PORT="8883").active())
