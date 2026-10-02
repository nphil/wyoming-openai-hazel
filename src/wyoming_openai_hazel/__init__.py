"""Opt-in extras for the wyoming_openai bridge (https://github.com/roryeckel/wyoming_openai).

The upstream bridge is not modified: its event handler is subclassed (see ``handler.py``) and every extra is switched on
by an environment variable (see ``config.py`` and the README).
"""

import os

__version__ = os.environ.get("HAZEL_VERSION", "dev")

__all__ = ["__version__"]
