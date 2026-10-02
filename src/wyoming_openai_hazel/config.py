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

A value that cannot be parsed is reported with a warning and the default is used, so a typo can never stop the bridge.
"""

from __future__ import annotations

import logging
import os
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


def _int(env: Mapping[str, str], name: str, default: int | None, *, minimum: int) -> int | None:
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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HazelConfig:
        env = os.environ if env is None else env
        defaults = cls()
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
        if self.log_timing:
            on.append("timing-log")
        return on
