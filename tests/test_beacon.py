"""The Home Assistant on/off sensor (HAZEL_MQTT_HOST): what it announces, and that what it announces is true.

The sensor is an MQTT "beacon" (``beacon.py``). These tests start the REAL entry point as a separate process, point it at a
stand-in broker (``mqtt_broker.py``) and look at what the broker received, which is everything Home Assistant would ever see:

* the discovery message that creates the sensor, and the state messages ``online`` / ``offline``;
* the Last Will in the connect packet: it is what turns the sensor off when the bridge is killed or the whole machine dies;
* the self-check: it asks the bridge's own Wyoming port, so the sensor follows the listener and not just the process;
* a broker that restarts, drops us, refuses us or is not there at all must never hurt the bridge.

Where a test must switch the Wyoming listener off and on while the process lives, the real entry point still runs, but the upstream
bridge it would start is replaced by a stand-in, and the listener is a tiny Wyoming server inside the test.
Every test that needs a broker asks for ``mqtt_broker`` before ``bridge``, so the bridge is stopped first.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import signal
import socket
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio
from bridge_process import PACKAGE_ROOT, free_port
from mqtt_broker import REFUSED_NOT_AUTHORISED, FakeBroker, Will
from wyoming.event import async_read_event, async_write_event
from wyoming.info import Describe, Info
from wyoming_client import silence, speech
from wyoming_openai_hazel.beacon import Health, Throttle
from wyoming_openai_hazel.healthcheck import probe, wyoming_port

VERSION = "9.9.9-test"
USER, PASSWORD = "beacon-user-7", "s3cr3t-Pw-4711"
SUPPORT_URL = "https://github.com/nphil/wyoming-openai-hazel"
SLOW = 30.0          # seconds a busy machine may need to start a Python process that imports everything

# The real entry point, except that the upstream bridge is replaced by a stand-in that prints which threads exist and then stays
# alive until the file named in TEST_RELEASE_FILE exists. (With the sensor on it first waits for paho's network thread to be there.)
# With the sensor on, paho's ``disconnect`` (the clean goodbye that makes a broker throw the will away) is replaced before anything
# runs by a function that only prints DISCONNECT CALLED, so any call, from anywhere, shows up for certain.
STAND_IN_FOR_THE_BRIDGE = """
import json, os, runpy, sys, threading, time
if os.environ.get("HAZEL_MQTT_HOST", "").strip():
    import paho.mqtt.client as mqtt
    mqtt.Client.disconnect = lambda self, *args, **kwargs: print("DISCONNECT CALLED", flush=True)
import wyoming_openai_hazel.__main__ as hazel

def stand_in(*args, **kwargs):
    if os.environ.get("HAZEL_MQTT_HOST", "").strip():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not any(t.name.startswith("paho-mqtt-client") for t in threading.enumerate()):
            time.sleep(0.02)
    else:
        time.sleep(1.0)           # long enough for a sensor that should not exist to have connected
    print("STATUS " + json.dumps({
        "paho_loaded": "paho" in sys.modules,
        "beacon_loaded": "wyoming_openai_hazel.beacon" in sys.modules,
        "threads": {t.name: t.daemon for t in threading.enumerate() if t is not threading.main_thread()},
    }), flush=True)
    while not os.path.exists(os.environ["TEST_RELEASE_FILE"]):
        time.sleep(0.05)

runpy.run_module = stand_in
hazel.main()
"""

# ``import paho`` fails, as it would in an image built without the library; the real upstream bridge then runs.
NO_PAHO_LAUNCHER = """
import sys
sys.modules["paho"] = None
import wyoming_openai_hazel.__main__ as hazel
hazel.main()
"""

# Stands in for an upstream release that renamed something the extras rely on: the stock bridge runs (see test_smoke_e2e.py).
BROKEN_SEAM_LAUNCHER = """
import wyoming_openai_hazel.__main__ as hazel
hazel.REQUIRED_MEMBERS += ("_renamed_in_a_newer_upstream",)
hazel.main()
"""


def beacon_env(broker: FakeBroker, **extra: str | None) -> dict[str, str | None]:
    """The settings that point a bridge at the stand-in broker. Short intervals keep the tests quick."""
    env: dict[str, str | None] = {
        "HAZEL_MQTT_HOST": "127.0.0.1", "HAZEL_MQTT_PORT": str(broker.port), "HAZEL_MQTT_KEEPALIVE": "10",
        "HAZEL_MQTT_CHECK_SECONDS": "2", "HAZEL_VERSION": VERSION,
    }
    env.update(extra)
    return env


def expected_discovery(*, mqtt_id: str = "main", name: str = "Hazel voice bridge") -> dict:
    """The discovery config the sensor must announce, spelled out (not produced by the code under test)."""
    return {
        "name": "Running",
        "unique_id": f"wyoming_openai_hazel_{mqtt_id}_running",
        "device_class": "running",
        "state_topic": f"wyoming-openai-hazel/{mqtt_id}/state",
        "payload_on": "online",
        "payload_off": "offline",
        "device": {"identifiers": [f"wyoming_openai_hazel_{mqtt_id}"], "name": name, "manufacturer": "nphil",
                   "model": "wyoming-openai-hazel", "sw_version": VERSION},
        "origin": {"name": "wyoming-openai-hazel", "sw_version": VERSION, "support_url": SUPPORT_URL},
    }


def status_of(output: str) -> dict:
    """The ``STATUS {...}`` line printed by STAND_IN_FOR_THE_BRIDGE."""
    line = next(line for line in output.splitlines() if line.startswith("STATUS "))
    return json.loads(line[len("STATUS "):])


async def run_python(code: str) -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, env={"PYTHONPATH": str(PACKAGE_ROOT), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(process.communicate(), 60)
    return out.decode()


class WyomingStandIn:
    """A Wyoming server that knows only ``describe`` -> ``info``, on a port the test picks and can switch off and on again.

    ``hang_up_next``   close this many of the next connections without answering (a bridge that is up but does not answer)
    ``silent``         read the request and never answer
    ``answered`` / ``failed``   when each check was answered / hung up on (``time.monotonic()``)
    """

    def __init__(self, port: int, *, hang_up_next: int = 0) -> None:
        self.port = port
        self.hang_up_next = hang_up_next
        self.silent = False
        self.answered: list[float] = []
        self.failed: list[float] = []
        self._server: asyncio.Server | None = None
        self._open: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        self._server = await asyncio.start_server(self._handle, sock=sock)

    async def stop(self) -> None:
        """Stop listening (new connections are refused) and hang up on everybody who is connected."""
        if self._server is not None:
            self._server.close()
            for writer in list(self._open):
                writer.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._open.add(writer)
        try:
            event = await async_read_event(reader)
            if self.silent:
                await reader.read()                    # until the other side gives up
            elif self.hang_up_next > 0:
                self.hang_up_next -= 1
                self.failed.append(time.monotonic())
            elif event is not None and Describe.is_type(event.type):
                self.answered.append(time.monotonic())
                await async_write_event(Info().event(), writer)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self._open.discard(writer)
            writer.close()


@pytest_asyncio.fixture
async def beacon_only(mqtt_broker: FakeBroker, bridge):
    """``b, listener = await beacon_only(**settings)``: the real entry point with the sensor on and a stand-in for the bridge.

    ``listener`` is the Wyoming server the sensor checks (it answers unless the test says otherwise); ``b`` is the process.
    Pass ``listener_hangs_up=True`` for a listener that accepts every check and hangs up. Create the file ``b.release`` to let
    the stand-in bridge end by itself.
    """
    listeners: list[WyomingStandIn] = []

    async def start(*, listener_hangs_up: bool = False, **settings: str | None):
        release = bridge.workdir / f"release-{len(bridge.started)}"
        env = beacon_env(mqtt_broker, TEST_RELEASE_FILE=str(release), **settings)
        b = await bridge(args=[sys.executable, "-c", STAND_IN_FOR_THE_BRIDGE], wait=False, **env)
        b.release = release
        listener = WyomingStandIn(b.port, hang_up_next=10**9 if listener_hangs_up else 0)
        await listener.start()
        listeners.append(listener)
        return b, listener

    yield start
    for listener in listeners:
        await listener.stop()


# ----- the rules on their own (no process, no network) ---------------------------------------------------------------------
def test_the_state_is_offline_until_the_first_success_then_online_until_two_failures_in_a_row() -> None:
    health = Health()
    assert health.state == "offline"
    assert [health.record(False), health.record(False)] == ["offline", "offline"]      # never answered: stays off
    assert not health.answered_once
    assert health.record(True) == "online" and health.answered_once
    assert health.record(False) == "online"                  # one miss is forgiven...
    assert health.record(True) == "online"                   # ...and forgotten: the next miss is the first one again
    assert health.record(False) == "online"
    assert health.record(False) == "offline"                 # two in a row
    assert health.record(False) == "offline"
    assert health.record(True) == "online"                   # the first success is enough to be back


def test_the_throttle_says_the_first_time_and_then_at_most_once_a_minute_per_kind() -> None:
    now = [100.0]
    throttle = Throttle(60.0, clock=lambda: now[0])
    assert throttle.allow("unreachable")
    now[0] += 59.9
    assert not throttle.allow("unreachable")
    assert throttle.allow("refused")                         # every kind has its own minute
    now[0] += 0.1
    assert throttle.allow("unreachable")                     # 60 s after the first time
    assert not throttle.allow("unreachable")


# ----- the check itself: the health check's handshake, reused ---------------------------------------------------------------
def test_the_checked_port_is_the_one_in_wyoming_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    assert wyoming_port("tcp://0.0.0.0:10301") == 10301
    monkeypatch.delenv("WYOMING_URI", raising=False)
    assert wyoming_port() == 10300
    monkeypatch.setenv("WYOMING_URI", "tcp://127.0.0.1:12345")
    assert wyoming_port() == 12345
    with pytest.raises(ValueError):
        wyoming_port("unix:///run/wyoming.sock")             # no port in it: the health check has always refused such a URI


async def test_a_check_succeeds_only_when_info_comes_back() -> None:
    listener = WyomingStandIn(free_port())
    await listener.start()
    try:
        assert await probe("127.0.0.1", listener.port, 2.0) is True
        listener.hang_up_next = 1
        assert await probe("127.0.0.1", listener.port, 2.0) is False        # accepted, then hung up on
        listener.silent = True
        started = time.monotonic()
        assert await probe("127.0.0.1", listener.port, 0.5) is False        # accepted, never answered: gives up at the timeout
        assert 0.4 < time.monotonic() - started < 3.0
    finally:
        await listener.stop()
    assert await probe("127.0.0.1", listener.port, 2.0) is False            # nobody listens: connection refused


async def test_the_health_check_never_loads_paho_or_the_beacon() -> None:
    out = await run_python("import sys, wyoming_openai_hazel.healthcheck; "
                           "print([m for m in sys.modules if m.startswith('paho') or m == 'wyoming_openai_hazel.beacon'])")
    assert out.strip() == "[]"


# ----- off by default ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("host", [None, "", "   "])
async def test_without_a_host_the_sensor_is_off_nothing_connects_and_paho_is_not_even_loaded(mqtt_broker, bridge, tmp_path: Path,
                                                                                         host: str | None) -> None:
    release = tmp_path / "release"
    release.write_text("")                                   # the stand-in bridge ends as soon as it has looked around
    code, output = await bridge.run_to_exit(
        args=[sys.executable, "-c", STAND_IN_FOR_THE_BRIDGE],
        **beacon_env(mqtt_broker, HAZEL_MQTT_HOST=host, HAZEL_MQTT_USER=USER, HAZEL_MQTT_PASSWORD=PASSWORD,
                     TEST_RELEASE_FILE=str(release)))
    assert code == 0, output
    assert status_of(output) == {"paho_loaded": False, "beacon_loaded": False, "threads": {}}
    assert "mqtt-status" not in output
    assert mqtt_broker.connects == []


async def test_a_bridge_without_a_host_is_the_stock_bridge_with_a_clean_banner(bridge) -> None:
    b = await bridge()
    assert any(line.endswith("extras on: timing-log") for line in b.stderr_lines), b.stderr
    assert not b.lines_matching("mqtt")


# ----- what the sensor announces ---------------------------------------------------------------------------------------------
async def test_it_connects_announces_the_sensor_and_reports_online(mqtt_broker, bridge) -> None:
    b = await bridge(**beacon_env(mqtt_broker))
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)

    configs = mqtt_broker.discovery_log()
    assert len(configs) == 1, "the sensor must be announced once per connection"
    config = configs[0]
    assert config.topic == "homeassistant/binary_sensor/wyoming_openai_hazel_main/running/config"
    assert (config.retain, config.qos, config.will) == (True, 1, False)
    announced = json.loads(config.payload)
    assert announced == expected_discovery()
    # No availability: with one, Home Assistant would show the sensor as "unavailable" instead of "off" once the bridge is gone.
    assert "availability" not in announced and "availability_topic" not in announced
    assert mqtt_broker.retained[config.topic] is config      # the broker keeps it: Home Assistant finds it after its own restart

    states = mqtt_broker.state_log()
    assert {p.text for p in states} <= {"online", "offline"} and states[-1].text == "online"
    assert all((p.topic, p.retain, p.qos, p.will) == ("wyoming-openai-hazel/main/state", True, 1, False) for p in states)
    assert mqtt_broker.publishes.index(config) < mqtt_broker.publishes.index(states[0]), "the sensor is announced before it is used"
    assert f"mqtt-status(127.0.0.1:{mqtt_broker.port} id=main)" in b.stderr        # and the start-up banner says so
    assert len(mqtt_broker.accepted) == 1


async def test_the_connect_packet_carries_the_will_the_credentials_and_the_keepalive(mqtt_broker, bridge) -> None:
    mqtt_broker.credentials = {USER: PASSWORD}
    b = await bridge(**beacon_env(mqtt_broker, HAZEL_MQTT_USER=USER, HAZEL_MQTT_PASSWORD=PASSWORD, HAZEL_MQTT_KEEPALIVE="14",
                                  WYOMING_LOG_LEVEL="DEBUG"))
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)
    connect = mqtt_broker.connects[0]
    assert connect.accepted
    assert (connect.protocol_name, connect.protocol_level) == ("MQTT", 4)              # MQTT 3.1.1
    assert connect.keepalive == 14
    assert (connect.username, connect.password) == (USER, PASSWORD)
    assert connect.clean_session is True
    assert connect.client_id == "wyoming-openai-hazel-main"
    assert connect.will == Will("wyoming-openai-hazel/main/state", b"offline", qos=1, retain=True)
    # Never in the log, not even at the most talkative level.
    assert USER not in b.output and PASSWORD not in b.output


async def test_custom_id_name_and_prefix_are_used_in_every_topic_and_identifier(mqtt_broker, bridge) -> None:
    b = await bridge(**beacon_env(mqtt_broker, HAZEL_MQTT_ID="kitchen-2_b", HAZEL_MQTT_NAME="Kitchen bridge",
                                  HAZEL_MQTT_DISCOVERY_PREFIX="ha/discovery"))
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state("kitchen-2_b") == "online", SLOW)
    configs = mqtt_broker.discovery_log("kitchen-2_b", "ha/discovery")
    assert [c.topic for c in configs] == ["ha/discovery/binary_sensor/wyoming_openai_hazel_kitchen-2_b/running/config"]
    assert json.loads(configs[0].payload) == expected_discovery(mqtt_id="kitchen-2_b", name="Kitchen bridge")
    connect = mqtt_broker.connects[0]
    assert connect.client_id == "wyoming-openai-hazel-kitchen-2_b"
    assert connect.will == Will("wyoming-openai-hazel/kitchen-2_b/state", b"offline", qos=1, retain=True)
    assert all("main" not in p.topic for p in mqtt_broker.publishes)                  # nothing under the default names
    assert f"mqtt-status(127.0.0.1:{mqtt_broker.port} id=kitchen-2_b)" in b.stderr


async def test_bad_settings_are_reported_and_replaced_by_the_defaults(mqtt_broker, bridge) -> None:
    b = await bridge(**beacon_env(mqtt_broker, HAZEL_MQTT_ID="my bridge/#", HAZEL_MQTT_DISCOVERY_PREFIX="home assistant/",
                                  HAZEL_MQTT_KEEPALIVE="1", HAZEL_MQTT_CHECK_SECONDS="often", HAZEL_MQTT_NAME="   "))
    await mqtt_broker.until("the sensor to be announced", lambda: mqtt_broker.discovery_log(), SLOW)
    for name in ("HAZEL_MQTT_ID", "HAZEL_MQTT_DISCOVERY_PREFIX", "HAZEL_MQTT_KEEPALIVE", "HAZEL_MQTT_CHECK_SECONDS"):
        assert [line for line in b.stderr_lines if name in line], f"no warning about {name}:\n{b.stderr}"
    assert not [line for line in b.stderr_lines if "HAZEL_MQTT_NAME" in line]       # blank means "not set", not "wrong"
    config = mqtt_broker.discovery_log()[0]
    assert config.topic == "homeassistant/binary_sensor/wyoming_openai_hazel_main/running/config"
    assert json.loads(config.payload) == expected_discovery()
    assert mqtt_broker.connects[0].keepalive == 20 and mqtt_broker.connects[0].client_id == "wyoming-openai-hazel-main"


# ----- the Last Will: how a dead bridge is noticed -----------------------------------------------------------------------------
async def test_killing_the_bridge_makes_the_broker_publish_the_will_within_two_seconds(mqtt_broker, bridge) -> None:
    b = await bridge(**beacon_env(mqtt_broker))
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)
    killed = time.monotonic()
    b.process.kill()                                         # no goodbye of any kind, like a crash or a power cut
    will = await mqtt_broker.until("the will", lambda: next((p for p in mqtt_broker.state_log() if p.will), None), 5.0)
    assert will.at - killed < 2.0
    assert (will.text, will.retain, will.qos) == ("offline", True, 1)
    assert mqtt_broker.state() == "offline"
    assert mqtt_broker.disconnects == []
    assert json.loads(mqtt_broker.retained[mqtt_broker.discovery_topic()].payload) == expected_discovery()   # the sensor stays


async def test_a_normal_stop_ends_the_process_without_a_goodbye_so_the_will_goes_out_too(mqtt_broker, bridge) -> None:
    b = await bridge(**beacon_env(mqtt_broker))
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)
    b.process.terminate()                                    # what `docker stop` sends
    code = await asyncio.wait_for(b.process.wait(), 8)       # the sensor's threads never keep a stopping bridge alive
    assert code in (0, -signal.SIGTERM), code
    await mqtt_broker.until("the will", lambda: mqtt_broker.state() == "offline", 5.0)
    assert mqtt_broker.disconnects == []                     # a DISCONNECT packet would have made the broker drop the will


async def test_both_threads_are_daemons_so_a_bridge_that_ends_by_itself_still_exits_and_the_will_goes_out(
        mqtt_broker, beacon_only) -> None:
    b, _ = await beacon_only()
    status = status_of(await b.wait_for_log(r"^STATUS ", SLOW))          # printed where the bridge would have been started
    assert {"hazel-mqtt-beacon", "paho-mqtt-client-wyoming-openai-hazel-main"} <= set(status["threads"]), status
    assert all(status["threads"].values()), f"a non-daemon thread would keep the process alive: {status['threads']}"
    await mqtt_broker.until("the connection", lambda: mqtt_broker.accepted, SLOW)
    b.release.write_text("")                                 # the "bridge" returns: the main thread has nothing left to do
    assert await asyncio.wait_for(b.process.wait(), 10) == 0
    await mqtt_broker.until("the will", lambda: mqtt_broker.state() == "offline", 5.0)
    assert mqtt_broker.disconnects == []
    assert "DISCONNECT CALLED" not in b.output, "something tried to say goodbye (a clean DISCONNECT makes the broker drop the will)"


# ----- the self-check: the sensor follows the listener -------------------------------------------------------------------------
async def test_the_bridge_is_checked_every_2_seconds_until_it_first_answers_and_then_at_the_set_interval(
        mqtt_broker, beacon_only) -> None:
    b, listener = await beacon_only(listener_hangs_up=True, HAZEL_MQTT_CHECK_SECONDS="4", HAZEL_MQTT_KEEPALIVE="8")
    await mqtt_broker.until("three checks that fail", lambda: len(listener.failed) >= 3, SLOW)
    gaps = [later - earlier for earlier, later in itertools.pairwise(listener.failed)]
    assert all(1.9 <= gap < 3.5 for gap in gaps), f"before the first answer the check should run every 2 s: {gaps}"
    await mqtt_broker.until("the first state message", lambda: mqtt_broker.state_log(), SLOW)
    # Nothing has answered yet, so what is published from the start is "offline" (and every check publishes it again).
    assert {p.text for p in mqtt_broker.state_log()} == {"offline"}
    listener.hang_up_next = 0                                # now it answers
    await mqtt_broker.until("two answered checks", lambda: len(listener.answered) >= 2, SLOW)
    gap = listener.answered[1] - listener.answered[0]
    assert 3.9 <= gap < 8.0, f"after the first answer the check should run every 4 s (HAZEL_MQTT_CHECK_SECONDS): {gap}"
    assert mqtt_broker.state() == "online"


async def test_one_missed_check_is_forgiven_but_two_in_a_row_turn_the_sensor_off(mqtt_broker, beacon_only) -> None:
    b, listener = await beacon_only()
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)

    listener.hang_up_next = 1                                # exactly one check fails
    await mqtt_broker.until("the missed check", lambda: len(listener.failed) == 1, SLOW)
    answered = len(listener.answered)
    await mqtt_broker.until("the next check to be answered", lambda: len(listener.answered) > answered, SLOW)
    texts = [p.text for p in mqtt_broker.state_log()]
    assert "offline" not in texts[texts.index("online"):], f"one miss turned the sensor off: {texts}"

    listener.hang_up_next = 10**9                            # from now on every check fails
    await mqtt_broker.until("the sensor to go offline", lambda: mqtt_broker.state() == "offline", SLOW)
    first_miss, second_miss = listener.failed[1], listener.failed[2]                # the two in a row (failed[0] was forgiven)
    after_first = [p for p in mqtt_broker.state_log() if p.at >= first_miss]
    assert after_first[0].text == "online" and after_first[0].at < second_miss, "the first of two misses must not change anything"
    assert next(p for p in after_first if p.text == "offline").at >= second_miss

    listener.hang_up_next = 0                                # and the first answer is enough to be back
    await mqtt_broker.until("the sensor to come back", lambda: mqtt_broker.state() == "online", SLOW)
    assert len(mqtt_broker.accepted) == 1                    # all of it on the one connection


async def test_stopping_only_the_wyoming_listener_turns_the_sensor_off_and_starting_it_again_turns_it_on(
        mqtt_broker, beacon_only) -> None:
    b, listener = await beacon_only(HAZEL_MQTT_KEEPALIVE="5")           # the broker drops a client that is silent for 7.5 s
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)
    stopped = time.monotonic()
    await listener.stop()                                    # the process lives on; only the Wyoming port is gone
    await mqtt_broker.until("the sensor to go offline", lambda: mqtt_broker.state() == "offline", SLOW)
    assert time.monotonic() - stopped >= 1.9, "the sensor went off after one failed check, not two"
    assert b.process.returncode is None
    assert len(mqtt_broker.accepted) == 1 and not any(p.will for p in mqtt_broker.publishes)    # told by the check, not by a will

    await listener.start()
    await mqtt_broker.until("the sensor to come back", lambda: mqtt_broker.state() == "online", SLOW)
    # The whole time the connection stayed up although the keep-alive is only 5 s: the check keeps it busy.
    await asyncio.sleep(max(0.0, 8.5 - (time.monotonic() - mqtt_broker.connects[0].at)))
    assert len(mqtt_broker.accepted) == 1 and not any(p.will for p in mqtt_broker.publishes)
    assert mqtt_broker.state() == "online"


async def test_a_restarted_bridge_replaces_its_own_stale_connection_so_the_sensor_never_ends_up_wrong(
        mqtt_broker, beacon_only) -> None:
    """A power cut that is over within the broker's keep-alive: the dead bridge's connection is still open when the new one comes."""
    first, _ = await beacon_only()
    await mqtt_broker.until("the first bridge to be online", lambda: mqtt_broker.state() == "online", SLOW)
    first.process.send_signal(signal.SIGSTOP)                # frozen: says nothing, answers nothing, but its connection stays open
    try:
        await beacon_only()
        await mqtt_broker.until("the second bridge to be online",
                                lambda: any(p.connection == 1 and p.text == "online" for p in mqtt_broker.state_log()), SLOW)
        log = [(p.connection, p.will, p.text) for p in mqtt_broker.state_log()]
        will_at = next(i for i, (_, will, _) in enumerate(log) if will)
        assert log[will_at] == (0, True, "offline")
        # The old will went out first; everything after it is from the new connection, so the sensor ends up online.
        assert all(connection == 1 for connection, _, _ in log[will_at + 1:]), log
        assert mqtt_broker.state() == "online"
    finally:
        first.process.kill()


# ----- the broker comes and goes -----------------------------------------------------------------------------------------------
async def test_after_a_dropped_connection_it_reconnects_and_announces_everything_again(mqtt_broker, bridge) -> None:
    # A check interval of 10 s keeps the regular state message out of this test: what arrives within a moment of the reconnect is
    # what the reconnect itself published.
    b = await bridge(**beacon_env(mqtt_broker, HAZEL_MQTT_CHECK_SECONDS="10", HAZEL_MQTT_KEEPALIVE="20"))
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)
    await mqtt_broker.drop_connections()                     # a network failure: no goodbye, the will goes out
    await mqtt_broker.until("the will", lambda: any(p.will for p in mqtt_broker.state_log()), 5.0)
    await mqtt_broker.until("the second connection", lambda: len(mqtt_broker.accepted) == 2, 15.0)
    first, second = mqtt_broker.accepted
    assert second.will == first.will and (second.username, second.keepalive) == (first.username, first.keepalive)

    def from_second() -> list:
        return [p for p in mqtt_broker.publishes if p.connection == second.index]

    again = await mqtt_broker.until("both messages of the new connection", lambda: len(from_second()) >= 2 and from_second(), 3.0)
    assert [p.topic for p in again[:2]] == [mqtt_broker.discovery_topic(), "wyoming-openai-hazel/main/state"]
    assert json.loads(again[0].payload) == expected_discovery() and again[1].text == "online"
    assert again[1].at - second.at < 1.0, "the state must come with the connection, not with the next regular check"
    assert mqtt_broker.state() == "online"
    assert (await b.client().describe()).asr, "the bridge itself never noticed"
    assert len(b.lines_matching("lost the connection to the MQTT broker")) == 1       # the loss is in the log ...
    assert len(b.lines_matching("connected to the MQTT broker")) == 2                 # ... and so is the recovery, though it came soon


async def test_a_broker_that_comes_back_empty_gets_the_sensor_back_without_a_flood_of_old_states(mqtt_broker, bridge) -> None:
    b = await bridge(**beacon_env(mqtt_broker))
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)
    port = mqtt_broker.port
    await mqtt_broker.stop()
    await asyncio.sleep(5.0)                                 # two or three checks happen with nobody to tell
    mqtt_broker.retained.clear()                             # a broker without persistence: it has forgotten everything
    await mqtt_broker.start(port)
    await mqtt_broker.until("the second connection", lambda: len(mqtt_broker.accepted) == 2, 20.0)
    back = mqtt_broker.accepted[1]
    await mqtt_broker.until("the sensor to be announced again",
                            lambda: mqtt_broker.discovery_topic() in mqtt_broker.retained and mqtt_broker.state() == "online", 10.0)
    await asyncio.sleep(0.5)
    first_second = [p for p in mqtt_broker.state_log() if p.connection == back.index and p.at - back.at < 1.0]
    assert 1 <= len(first_second) <= 2, f"{len(first_second)} states in the first second: old ones were queued during the outage"
    assert b.process.returncode is None
    assert (await b.client().describe()).asr
    assert len(b.lines_matching("lost the connection to the MQTT broker")) == 1
    assert len(b.lines_matching("cannot reach the MQTT broker")) == 1                 # five seconds of retries, said once
    assert len(b.lines_matching("connected to the MQTT broker")) == 2                 # the log does not end on a warning


async def test_a_broker_that_refuses_the_credentials_never_hurts_the_bridge(mqtt_broker, bridge) -> None:
    mqtt_broker.credentials = {USER: PASSWORD}
    wrong = "not-" + PASSWORD
    b = await bridge(**beacon_env(mqtt_broker, HAZEL_MQTT_USER=USER, HAZEL_MQTT_PASSWORD=wrong, WYOMING_LOG_LEVEL="DEBUG"))
    await mqtt_broker.until("three refused attempts", lambda: len(mqtt_broker.connects) >= 3, SLOW)
    assert {c.return_code for c in mqtt_broker.connects} == {REFUSED_NOT_AUTHORISED}
    assert mqtt_broker.publishes == []
    # The bridge answers and transcribes as if nothing were wrong ...
    assert (await b.client().describe()).asr
    text, _ = await b.client().transcribe(speech(300) + silence(300), realtime=False)
    assert text
    # ... the refusal is in the log once, not at every retry, and the secrets are not in it ...
    assert len(b.lines_matching("refused the connection")) == 1, b.stderr
    assert USER not in b.output and wrong not in b.output and PASSWORD not in b.output
    # ... and the sensor keeps trying: as soon as the broker accepts what is sent, it is announced.
    mqtt_broker.credentials = {USER: wrong}
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)
    assert b.lines_matching("connected to the MQTT broker")


async def test_a_broker_that_hangs_up_before_accepting_is_reported_once_and_tried_again(mqtt_broker, bridge) -> None:
    mqtt_broker.hang_up_on_connect = True                    # a firewall, a connection limit, or a port that is not a broker at all
    b = await bridge(**beacon_env(mqtt_broker))
    await mqtt_broker.until("three attempts", lambda: len(mqtt_broker.connects) >= 3, SLOW)
    assert mqtt_broker.publishes == []
    assert (await b.client().describe()).asr
    assert len(b.lines_matching("closed the connection before it accepted it")) == 1, b.stderr
    mqtt_broker.hang_up_on_connect = False
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)


async def test_a_broker_that_is_not_there_never_hurts_the_bridge_and_is_found_when_it_appears(mqtt_broker, bridge) -> None:
    port = mqtt_broker.port
    await mqtt_broker.stop()                                 # nothing listens on that port now
    b = await bridge(**beacon_env(mqtt_broker))
    await asyncio.sleep(3.5)                                 # at least three attempts: right away, after 1 s and after 3 s
    assert (await b.client().describe()).asr
    assert len(b.lines_matching("cannot reach the MQTT broker")) == 1, b.stderr
    await mqtt_broker.start(port)
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)


# ----- the sensor can never stop the bridge ------------------------------------------------------------------------------------
async def test_a_missing_mqtt_library_is_reported_loudly_and_the_bridge_starts_anyway(mqtt_broker, bridge) -> None:
    b = await bridge(args=[sys.executable, "-c", NO_PAHO_LAUNCHER], allow_errors=True, **beacon_env(mqtt_broker))
    assert b.lines_matching("MQTT status beacon could NOT be started")
    assert b.lines_matching("MQTT BEACON NOT STARTED")
    text, _ = await b.client().transcribe(speech(300) + silence(300), realtime=False)
    assert text
    assert mqtt_broker.connects == []


async def test_the_sensor_also_reports_a_stock_bridge_that_runs_without_the_extras(mqtt_broker, bridge) -> None:
    b = await bridge(args=[sys.executable, "-c", BROKEN_SEAM_LAUNCHER], allow_errors=True, **beacon_env(mqtt_broker))
    assert b.lines_matching("EXTRAS NOT INSTALLED")
    await mqtt_broker.until("the state to be online", lambda: mqtt_broker.state() == "online", SLOW)
    assert (await b.client().describe()).asr
