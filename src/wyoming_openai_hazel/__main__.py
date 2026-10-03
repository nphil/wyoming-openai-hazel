"""``python -m wyoming_openai_hazel``: the upstream bridge, started with its event handler replaced by ours.

The only seam is one module attribute: ``wyoming_openai.handler.OpenAIEventHandler``. Upstream's ``__main__`` does
``from .handler import OpenAIEventHandler`` when it runs, so running it *after* the swap makes it build our handlers. If the
swap cannot be done (upstream renamed something we rely on) the stock bridge is started instead and the reason is logged loudly.

When ``HAZEL_MQTT_HOST`` is set, the Home Assistant on/off sensor (``beacon.py``) is started just before the bridge, whether or not
the extras could be installed: the sensor reports the bridge, and the stock bridge is a running bridge too.
"""

from __future__ import annotations

import logging
import runpy
import sys

from . import __version__
from .config import HazelConfig

# What this package overrides or reads on the upstream handler. Missing names mean upstream changed shape.
REQUIRED_MEMBERS = (
    "handle_event", "write_event", "_handle_audio_start", "_handle_audio_chunk", "_handle_audio_stop",
    "_get_stt_extra_body", "_is_asr_model_streaming", "_is_asr_model_realtime",
)


def install(config: HazelConfig) -> list[str]:
    """Swap upstream's handler class for ours. Returns the active extras. Raises if upstream has changed shape."""
    from wyoming_openai import handler as upstream

    from .handler import HazelEventHandler, make_handler_class

    stock = upstream.OpenAIEventHandler
    while getattr(stock, "__name__", "") == "HazelEventHandler":   # installed twice (tests): start from the real base
        stock = stock.__mro__[1]
    missing = [name for name in REQUIRED_MEMBERS if not hasattr(stock, name)]
    if missing:
        raise RuntimeError(f"upstream OpenAIEventHandler no longer has {missing}")
    if stock is not HazelEventHandler.__mro__[1]:
        raise RuntimeError("wyoming_openai.handler.OpenAIEventHandler is not the class this package was built for")
    upstream.OpenAIEventHandler = make_handler_class(config)
    return config.active()


def start_beacon(config: HazelConfig) -> None:
    """Start the opt-in MQTT status beacon. Whatever goes wrong is reported loudly and the bridge starts anyway."""
    if not config.mqtt_host:
        return
    try:
        # Upstream sets up logging a moment after this (replacing what is set here); until then the beacon's first lines would be lost.
        logging.basicConfig(level=logging.INFO)
        from .beacon import Beacon

        Beacon(config).start()
    except Exception as exc:
        logging.getLogger(__name__).exception("MQTT status beacon could NOT be started (%s); the bridge runs without it", exc)
        print(f"wyoming-openai-hazel {__version__}: MQTT BEACON NOT STARTED ({exc}); the bridge runs without it",
              file=sys.stderr, flush=True)


def main() -> None:
    config = HazelConfig.from_env()
    try:
        from wyoming_openai import __version__ as upstream_version

        active = install(config)
        print(f"wyoming-openai-hazel {__version__} on wyoming_openai {upstream_version}; extras on: "
              f"{', '.join(active) or 'none'}", file=sys.stderr, flush=True)
    except Exception as exc:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger(__name__).exception("Hazel extras could NOT be installed (%s); running the stock bridge", exc)
        print(f"wyoming-openai-hazel {__version__}: EXTRAS NOT INSTALLED ({exc}); running the stock bridge",
              file=sys.stderr, flush=True)
    start_beacon(config)
    runpy.run_module("wyoming_openai", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
