"""Canary: every place where this package touches the upstream bridge still exists in the upstream version being tested.

When upstream renames or removes something we override or read, these tests fail on the NEW upstream version, before an image is
published - instead of an extra silently turning itself off in production. (The same list is checked again at every start-up by
``__main__.install``, which then falls back to the stock bridge.)
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import openai
import pytest
import wyoming_openai_hazel.handler as hazel_handler
from inproc import build_handler
from openai import omit
from openai.resources.audio.transcriptions import AsyncTranscriptions
from wyoming_openai import handler as upstream_module
from wyoming_openai.handler import OpenAIEventHandler as STOCK
from wyoming_openai_hazel.__main__ import REQUIRED_MEMBERS, install
from wyoming_openai_hazel.config import HazelConfig

# Set by upstream's __init__ (or assigned by us); the extras read or replace them.
INSTANCE_ATTRIBUTES = (
    "_tts_semaphore", "_wyoming_info", "_stt_client", "_current_asr_model", "_current_language", "_stt_temperature",
    "_stt_prompt", "_wav_buffer", "_is_recording",
)
# What the early request passes to the OpenAI client.
EARLY_REQUEST_ARGUMENTS = ("file", "model", "language", "temperature", "prompt", "response_format", "extra_body")


@pytest.fixture
def stock_instance() -> STOCK:
    return build_handler(stock=True)


def test_every_method_we_override_exists_upstream() -> None:
    missing = [name for name in REQUIRED_MEMBERS if not callable(getattr(STOCK, name, None))]
    assert not missing, f"upstream OpenAIEventHandler lost {missing}"


def test_every_attribute_we_read_or_replace_exists_on_an_upstream_handler(stock_instance) -> None:
    missing = [name for name in INSTANCE_ATTRIBUTES if not hasattr(stock_instance, name)]
    assert not missing, f"upstream handler instances no longer have {missing}"


def test_nothing_is_read_from_upstream_that_the_lists_above_do_not_cover(stock_instance) -> None:
    """Parse our handler's source: every ``self.<name>`` / ``super().<name>`` must be ours or exist on the upstream handler.

    This keeps the lists above honest - a new line of code that reads another upstream attribute is checked automatically.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(hazel_handler)))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            base = node.value
            is_self = isinstance(base, ast.Name) and base.id == "self"
            is_super = isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "super"
            if is_self or is_super:
                used.add(node.attr)
    ours = set(vars(hazel_handler.HazelEventHandler)) | {name for name in used if name.startswith(("_hazel", "hazel"))}
    from_upstream = sorted(used - ours)
    assert from_upstream, "the scan found nothing - it is broken"
    missing = [name for name in from_upstream if not hasattr(stock_instance, name)]
    assert not missing, f"the handler uses {missing}, which upstream no longer has"
    # The two lists above must at least include what the scan found for the attributes (methods are covered by REQUIRED_MEMBERS).
    unlisted = [name for name in from_upstream
                if not callable(getattr(STOCK, name, None)) and name not in INSTANCE_ATTRIBUTES]
    assert not unlisted, f"add {unlisted} to INSTANCE_ATTRIBUTES"


def _dict_keys(source: str, variable: str) -> set[str]:
    """The string keys of the dict literal assigned to ``variable`` in ``source`` (e.g. the keyword arguments of a request)."""
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == variable for t in targets) and isinstance(node.value, ast.Dict):
            return {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
    raise AssertionError(f"no dict literal assigned to {variable!r}")


def test_the_early_request_has_the_same_arguments_as_upstreams_own_request() -> None:
    """If upstream starts sending another field with its transcription request, the early request would lack it."""
    upstream_keys = _dict_keys(inspect.getsource(STOCK._handle_audio_stop), "transcription_kwargs")
    our_keys = _dict_keys(inspect.getsource(hazel_handler.HazelEventHandler._early_request), "kwargs")
    # "stream" is left out of the early request on purpose: streaming models never take the early path.
    assert our_keys == upstream_keys - {"stream"}


def test_the_installed_openai_sdk_accepts_what_we_pass() -> None:
    assert isinstance(omit, openai.Omit)
    parameters = inspect.signature(AsyncTranscriptions.create).parameters
    missing = [name for name in EARLY_REQUEST_ARGUMENTS if name not in parameters]
    assert not missing, f"openai.audio.transcriptions.create no longer accepts {missing}"
    assert openai.DEFAULT_MAX_RETRIES >= 0   # the failure tests derive the number of attempts from it


# ----- the swap itself ---------------------------------------------------------------------------------------------------
@pytest.fixture
def pristine_upstream(monkeypatch: pytest.MonkeyPatch):
    """Whatever a test does to ``wyoming_openai.handler.OpenAIEventHandler`` is undone afterwards."""
    monkeypatch.setattr(upstream_module, "OpenAIEventHandler", STOCK)
    return upstream_module


def test_make_handler_class_binds_the_config_and_builds_working_handlers() -> None:
    config = HazelConfig(tts_concurrency=2)
    cls = hazel_handler.make_handler_class(config)
    assert cls.hazel is config and issubclass(cls, STOCK)
    assert hazel_handler.HazelEventHandler.hazel is not config           # the shared base class is not touched
    other = hazel_handler.make_handler_class(HazelConfig(tts_concurrency=5))
    assert other.hazel.tts_concurrency == 5 and cls.hazel.tts_concurrency == 2


def test_install_swaps_in_the_handler_and_reports_the_active_extras(pristine_upstream) -> None:
    config = HazelConfig(early_stt=True, tts_concurrency=2)
    assert install(config) == config.active()
    installed = pristine_upstream.OpenAIEventHandler
    assert installed is not STOCK and installed.hazel is config and issubclass(installed, STOCK)


def test_install_twice_in_one_process_replaces_the_extras_instead_of_stacking_them(pristine_upstream) -> None:
    first, second = HazelConfig(tts_concurrency=1), HazelConfig(tts_concurrency=2)
    install(first)
    install(second)
    installed = pristine_upstream.OpenAIEventHandler
    assert installed.hazel is second
    assert installed.__mro__[1] is hazel_handler.HazelEventHandler, "a handler layer was stacked on a handler layer"
    assert installed.__mro__[2] is STOCK
    install(first)                                    # and again, back to the first settings
    assert pristine_upstream.OpenAIEventHandler.hazel is first
    assert pristine_upstream.OpenAIEventHandler.__mro__[2] is STOCK


def test_install_refuses_when_upstream_lost_a_member_and_leaves_upstream_alone(pristine_upstream, monkeypatch) -> None:
    monkeypatch.delattr(STOCK, "_is_asr_model_realtime")
    with pytest.raises(RuntimeError, match="_is_asr_model_realtime"):
        install(HazelConfig())
    assert pristine_upstream.OpenAIEventHandler is STOCK


def test_install_refuses_when_the_upstream_class_is_not_the_one_we_were_built_for(pristine_upstream) -> None:
    class Impostor(STOCK):
        pass

    pristine_upstream.OpenAIEventHandler = Impostor
    with pytest.raises(RuntimeError, match="not the class"):
        install(HazelConfig())
    assert pristine_upstream.OpenAIEventHandler is Impostor
