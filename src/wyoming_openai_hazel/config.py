"""Settings of the extras, read from environment variables. All optional; everything is OFF unless named here.

    HAZEL_GPU_WAKE_FILE        path of a file to touch when a voice request starts (empty = off)
    HAZEL_STT_EARLY            1 = start transcribing as soon as the speaker pauses (default 0)
    HAZEL_STT_EARLY_SILENCE_MS silence after speech that starts the early request (default 300)
    HAZEL_STT_EARLY_MIN_SPEECH_MS  speech needed before silence counts (default 250)
    HAZEL_STT_EARLY_RESUME_MS  new sound after the early request that makes it stale (default 100)
    HAZEL_STT_EARLY_MAX_PASSES early requests per utterance, including re-tries after a pause (default 3)
    HAZEL_TTS_CONCURRENCY      sentences synthesized at the same time (default: upstream's 3)
    HAZEL_TTS_VOICE_LABELS     "voice_id=Display name;other_id=Other name" shown by Home Assistant's voice picker
    HAZEL_LOG_TIMING           1 = log speech-to-text and text-to-speech timings (default 1)

Home Assistant on/off sensor over MQTT (see beacon.py and the README); off unless HAZEL_MQTT_HOST is set:

    HAZEL_MQTT_HOST            MQTT broker to announce "the bridge is running" to, e.g. Home Assistant's Mosquitto (empty = off)
    HAZEL_MQTT_PORT            broker port (default 1883)
    HAZEL_MQTT_USER            broker user name (optional; never logged)
    HAZEL_MQTT_PASSWORD        broker password (optional; never logged)
    HAZEL_MQTT_ID              this bridge's name in the MQTT topics: letters, digits, "_" and "-" (default main)
    HAZEL_MQTT_NAME            device name shown in Home Assistant (default "Hazel voice bridge")
    HAZEL_MQTT_DISCOVERY_PREFIX  Home Assistant's MQTT discovery prefix (default homeassistant)
    HAZEL_MQTT_KEEPALIVE       seconds of silence after which the broker declares the bridge dead (default 20, minimum 5)
    HAZEL_MQTT_CHECK_SECONDS   seconds between self-checks, at most half the keepalive (default 10, minimum 2)

A value that cannot be parsed is reported with a warning and the default is used, so a typo can never stop the bridge.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    _LOGGER.warning("%s=%r is not a yes/no value (use 1 or 0); using %s", name, raw, default)
    return default


def _int(env: Mapping[str, str], name: str, default: int | None, *, minimum: int, maximum: int | None = None) -> int | None:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        _LOGGER.warning("%s=%r is not a whole number; using %s", name, raw, default)
        return default
    if value < minimum:
        _LOGGER.warning("%s=%d is below the minimum %d; using %s", name, value, minimum, default)
        return default
    if maximum is not None and value > maximum:
        _LOGGER.warning("%s=%d is above the maximum %d; using %s", name, value, maximum, default)
        return default
    return value


def parse_voice_labels(text: str) -> dict[str, str]:
    """``"af_hazel=Hazel;af_heart=Heart (original)"`` -> ``{"af_hazel": "Hazel", "af_heart": "Heart (original)"}``."""
    labels: dict[str, str] = {}
    for part in text.split(";"):
        if not part.strip():
            continue
        voice, sep, label = part.partition("=")
        voice, label = voice.strip(), label.strip()
        if not sep or not voice or not label:
            _LOGGER.warning("Ignoring voice label entry %r (expected voice_id=Display name)", part)
            continue
        labels[voice] = label
    return labels


_MQTT_ID = re.compile(r"[A-Za-z0-9_-]+")
_BAD_TOPIC_CHARS = re.compile(r"[\s+#\x00]")


def _usable_host(text: str) -> bool:
    if re.search(r"[\s/]", text):
        return False
    try:
        text.encode("idna")      # what the socket library does with a name: an empty or over-long label would crash paho's thread
    except UnicodeError:
        return False
    return True


def _mqtt_host(env: Mapping[str, str], name: str) -> str:
    raw = env.get(name, "").strip()
    if raw and not _usable_host(raw):
        _LOGGER.warning("%s=%r is not a host name or IP address (no mqtt:// in front, no path); "
                        "the MQTT status beacon stays off", name, raw)
        return ""
    return raw


def _mqtt_id(env: Mapping[str, str], name: str, default: str) -> str:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not _MQTT_ID.fullmatch(raw):
        _LOGGER.warning("%s=%r may only contain letters, digits, '_' and '-'; using %s", name, raw, default)
        return default
    return raw


def _topic_prefix(env: Mapping[str, str], name: str, default: str) -> str:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if any(not level or _BAD_TOPIC_CHARS.search(level) for level in raw.split("/")):
        _LOGGER.warning("%s=%r is not a usable MQTT topic prefix (no spaces, '+' or '#', no empty parts such as a "
                        "trailing '/'); using %s", name, raw, default)
        return default
    return raw


@dataclass(frozen=True)
class HazelConfig:
    gpu_wake_file: str = ""
    early_stt: bool = False
    early_stt_silence_ms: int = 300
    early_stt_min_speech_ms: int = 250
    early_stt_resume_ms: int = 100
    early_stt_max_passes: int = 3
    tts_concurrency: int | None = None
    voice_labels: Mapping[str, str] = field(default_factory=dict)
    log_timing: bool = True
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = field(default="", repr=False)        # repr=False: a user name or password must never reach a log by accident
    mqtt_password: str = field(default="", repr=False)
    mqtt_id: str = "main"
    mqtt_name: str = "Hazel voice bridge"
    mqtt_discovery_prefix: str = "homeassistant"
    mqtt_keepalive: int = 20
    mqtt_check_seconds: int = 10

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HazelConfig:
        env = os.environ if env is None else env
        defaults = cls()
        mqtt_user = env.get("HAZEL_MQTT_USER", "").strip()
        mqtt_password = env.get("HAZEL_MQTT_PASSWORD", "").strip()
        if mqtt_password and not mqtt_user:
            _LOGGER.warning("HAZEL_MQTT_PASSWORD is set but HAZEL_MQTT_USER is empty; MQTT sends a password only together "
                            "with a user name, so the password is ignored")
            mqtt_password = ""
        mqtt_keepalive = (_int(env, "HAZEL_MQTT_KEEPALIVE", defaults.mqtt_keepalive, minimum=5, maximum=65535)    # 16 bits in MQTT
                          or defaults.mqtt_keepalive)
        mqtt_check_seconds = (_int(env, "HAZEL_MQTT_CHECK_SECONDS", defaults.mqtt_check_seconds, minimum=2)
                              or defaults.mqtt_check_seconds)
        if mqtt_check_seconds > mqtt_keepalive // 2:     # the self-check is also what keeps the connection busy
            if env.get("HAZEL_MQTT_CHECK_SECONDS", "").strip():
                _LOGGER.warning("HAZEL_MQTT_CHECK_SECONDS=%d is more than half of HAZEL_MQTT_KEEPALIVE=%d; using %d",
                                mqtt_check_seconds, mqtt_keepalive, mqtt_keepalive // 2)
            mqtt_check_seconds = mqtt_keepalive // 2
        return cls(
            gpu_wake_file=env.get("HAZEL_GPU_WAKE_FILE", "").strip(),
            early_stt=_flag(env, "HAZEL_STT_EARLY", defaults.early_stt),
            early_stt_silence_ms=_int(env, "HAZEL_STT_EARLY_SILENCE_MS", defaults.early_stt_silence_ms, minimum=50) or 300,
            early_stt_min_speech_ms=_int(env, "HAZEL_STT_EARLY_MIN_SPEECH_MS", defaults.early_stt_min_speech_ms, minimum=0) or 0,
            early_stt_resume_ms=_int(env, "HAZEL_STT_EARLY_RESUME_MS", defaults.early_stt_resume_ms, minimum=10) or 100,
            early_stt_max_passes=_int(env, "HAZEL_STT_EARLY_MAX_PASSES", defaults.early_stt_max_passes, minimum=1) or 1,
            tts_concurrency=_int(env, "HAZEL_TTS_CONCURRENCY", None, minimum=1),
            voice_labels=parse_voice_labels(env.get("HAZEL_TTS_VOICE_LABELS", "")),
            log_timing=_flag(env, "HAZEL_LOG_TIMING", defaults.log_timing),
            mqtt_host=_mqtt_host(env, "HAZEL_MQTT_HOST"),
            mqtt_port=_int(env, "HAZEL_MQTT_PORT", defaults.mqtt_port, minimum=1, maximum=65535) or defaults.mqtt_port,
            mqtt_user=mqtt_user,
            mqtt_password=mqtt_password,
            mqtt_id=_mqtt_id(env, "HAZEL_MQTT_ID", defaults.mqtt_id),
            mqtt_name=env.get("HAZEL_MQTT_NAME", "").strip() or defaults.mqtt_name,
            mqtt_discovery_prefix=_topic_prefix(env, "HAZEL_MQTT_DISCOVERY_PREFIX", defaults.mqtt_discovery_prefix),
            mqtt_keepalive=mqtt_keepalive,
            mqtt_check_seconds=mqtt_check_seconds,
        )

    def active(self) -> list[str]:
        """Human-readable list of the extras that are switched on (for the start-up banner)."""
        on = []
        if self.gpu_wake_file:
            on.append(f"gpu-wake({self.gpu_wake_file})")
        if self.early_stt:
            on.append(f"early-stt(silence {self.early_stt_silence_ms} ms)")
        if self.tts_concurrency is not None:
            on.append(f"tts-concurrency({self.tts_concurrency})")
        if self.voice_labels:
            on.append(f"voice-labels({len(self.voice_labels)})")
        if self.mqtt_host:
            on.append(f"mqtt-status({self.mqtt_host}:{self.mqtt_port} id={self.mqtt_id})")
        if self.log_timing:
            on.append("timing-log")
        return on
