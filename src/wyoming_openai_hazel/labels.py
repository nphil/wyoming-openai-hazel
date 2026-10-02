"""Voice display names: what Home Assistant's voice picker shows for a voice id.

Wyoming voices carry a ``description``; Home Assistant shows it instead of the id. Upstream sets the description to the
id, so ``af_hazel`` would be shown as ``af_hazel``. With ``HAZEL_TTS_VOICE_LABELS="af_hazel=Hazel"`` it shows ``Hazel``
(the id sent to the speech server stays ``af_hazel``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def apply_voice_labels(info: Any, labels: Mapping[str, str]) -> int:
    """Set the description of every TTS voice in ``info`` (a wyoming ``Info``) whose id is in ``labels``. Idempotent."""
    changed = 0
    for program in getattr(info, "tts", None) or []:
        for voice in program.voices:
            for key in (getattr(voice, "backend_voice_name", None), voice.name):
                label = labels.get(key) if key else None
                if label:
                    if voice.description != label:
                        voice.description = label
                        changed += 1
                    break
    return changed
