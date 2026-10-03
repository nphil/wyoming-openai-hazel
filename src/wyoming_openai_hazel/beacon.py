"""Home Assistant on/off sensor: an MQTT "beacon" that says whether this bridge is running. Off unless ``HAZEL_MQTT_HOST`` is set.

When it is on, one daemon thread keeps a connection to the MQTT broker (usually Home Assistant's Mosquitto) and announces ONE
binary sensor through MQTT discovery. The sensor's state topic carries ``online`` or ``offline`` (retained, QoS 1). Two things keep
that truthful:

* **Self-check.** The thread asks the bridge's own Wyoming port for its ``info`` (the handshake of the Docker health check, see
  ``healthcheck.probe``). The state is ``offline`` until the first success, ``online`` after it, and ``offline`` again after two
  failures in a row. The current state is published on every check, so it also heals a retained message that went missing.
* **Last Will.** The connect packet carries a will, ``offline`` retained. When the connection ends WITHOUT a DISCONNECT packet, the
  broker itself publishes it. That is how a stopped container, a crash and a power cut all turn the sensor off even though the bridge
  can no longer say anything. For that reason this module NEVER calls paho's ``disconnect()`` (a clean DISCONNECT makes the broker
  throw the will away): when the process ends, the operating system drops the connection and the broker does the rest.

The sensor has no availability topic on purpose: with none, Home Assistant shows ``off`` for a bridge that is gone, not ``unavailable``.

Nothing here may stop or slow the bridge: both threads (ours and paho's network thread) are daemon threads, every callback catches
its own errors, and a broker that is down or refuses us is logged (the first time, then at most once a minute) and retried forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from . import __version__
from .config import HazelConfig
from .healthcheck import DEFAULT_URI, PROBE_TIMEOUT_S, probe, wyoming_port

_LOGGER = logging.getLogger(__name__)

ONLINE = "online"
OFFLINE = "offline"
QOS = 1
SUPPORT_URL = "https://github.com/nphil/wyoming-openai-hazel"
STARTUP_CHECK_S = 2.0           # check this often until the bridge has answered once, so "online" does not wait for the long interval
FAILURES_BEFORE_OFFLINE = 2
LOG_EVERY_S = 60.0


def state_topic(mqtt_id: str) -> str:
    return f"wyoming-openai-hazel/{mqtt_id}/state"


def discovery_topic(prefix: str, mqtt_id: str) -> str:
    return f"{prefix}/binary_sensor/wyoming_openai_hazel_{mqtt_id}/running/config"


def discovery_payload(config: HazelConfig, version: str = __version__) -> dict[str, Any]:
    """The MQTT discovery config of the one sensor. There is deliberately no ``availability`` key (see the module docstring)."""
    mqtt_id = config.mqtt_id
    return {
        "name": "Running",
        "unique_id": f"wyoming_openai_hazel_{mqtt_id}_running",
        "device_class": "running",
        "state_topic": state_topic(mqtt_id),
        "payload_on": ONLINE,
        "payload_off": OFFLINE,
        "device": {
            "identifiers": [f"wyoming_openai_hazel_{mqtt_id}"],
            "name": config.mqtt_name,
            "manufacturer": "nphil",
            "model": "wyoming-openai-hazel",
            "sw_version": version,
        },
        "origin": {"name": "wyoming-openai-hazel", "sw_version": version, "support_url": SUPPORT_URL},
    }


class Health:
    """What the self-checks add up to: ``offline`` until the first success, then ``online`` until two failures in a row."""

    def __init__(self) -> None:
        self.state = OFFLINE
        self.answered_once = False
        self._failures = 0

    def record(self, ok: bool) -> str:
        """Add one check result; returns the state to publish."""
        if ok:
            self.answered_once = True
            self._failures = 0
            self.state = ONLINE
        else:
            self._failures += 1
            if self._failures >= FAILURES_BEFORE_OFFLINE:
                self.state = OFFLINE
        return self.state


class Throttle:
    """The first time: allowed. After that: allowed at most once per ``interval_s`` for the same key. Safe from any thread."""

    def __init__(self, interval_s: float = LOG_EVERY_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval_s = interval_s
        self._clock = clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._interval_s:
                return False
            self._last[key] = now
            return True


class Beacon:
    """Announces the sensor and keeps its state true. ``start()`` returns at once; everything runs on daemon threads."""

    def __init__(self, config: HazelConfig, *, version: str = __version__) -> None:
        self.config = config
        self.version = version
        self.state_topic = state_topic(config.mqtt_id)
        self.discovery_topic = discovery_topic(config.mqtt_discovery_prefix, config.mqtt_id)
        self._broker = f"{config.mqtt_host}:{config.mqtt_port}"
        self._health = Health()
        self._throttle = Throttle()
        self._tell_when_connected = True     # log the next successful connection: the first one, and the first one after a problem
        # Guards ``_state`` and ``_link``, and makes "publish what is true now" one step, so the connect callback (paho's thread)
        # and the check loop (ours) can never publish an old state after a newer one.
        self._lock = threading.Lock()
        self._state = OFFLINE
        self._link = "down"          # "up": the broker accepted us. "refused": it said no (its disconnect follows). Else "down"
        self._client = self._make_client()

    def start(self) -> None:
        threading.Thread(target=self._run, name="hazel-mqtt-beacon", daemon=True).start()

    # ----- the thread --------------------------------------------------------------------------------------------
    def _run(self) -> None:
        try:
            port = self._wyoming_port()
            cfg = self.config
            self._client.connect_async(cfg.mqtt_host, cfg.mqtt_port, keepalive=cfg.mqtt_keepalive)
            self._client.loop_start()      # paho's own network thread (a daemon thread); reconnects by itself
            _LOGGER.info("mqtt-status: reporting this bridge (checked on 127.0.0.1:%d) to the MQTT broker at %s as '%s', "
                         "state topic %s", port, self._broker, cfg.mqtt_id, self.state_topic)
            asyncio.run(self._check_forever(port))
        except Exception:
            _LOGGER.exception("mqtt-status: the beacon stopped because of an error (the bridge itself is not affected)")

    @staticmethod
    def _wyoming_port() -> int:
        try:
            return wyoming_port()
        except ValueError:
            fallback = wyoming_port(DEFAULT_URI)
            _LOGGER.warning("mqtt-status: cannot read the port from WYOMING_URI; checking port %d", fallback)
            return fallback

    def _make_client(self) -> mqtt.Client:
        cfg = self.config
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"wyoming-openai-hazel-{cfg.mqtt_id}",
                             protocol=mqtt.MQTTv311)
        if cfg.mqtt_user:
            client.username_pw_set(cfg.mqtt_user, cfg.mqtt_password or None)
        # The will: the broker publishes it when this connection ends without a DISCONNECT packet. Never call disconnect().
        client.will_set(self.state_topic, OFFLINE, qos=QOS, retain=True)
        client.reconnect_delay_set(1, 60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        return client

    async def _check_forever(self, port: int) -> None:
        while True:
            ok = await probe("127.0.0.1", port, PROBE_TIMEOUT_S)
            try:
                self._announce(self._health.record(ok))
            except Exception:
                if self._throttle.allow("publish-error"):
                    _LOGGER.exception("mqtt-status: could not publish the state")
            await asyncio.sleep(self.config.mqtt_check_seconds if self._health.answered_once else STARTUP_CHECK_S)

    # ----- publishing --------------------------------------------------------------------------------------------
    def _announce(self, state: str) -> None:
        """Remember the state and publish it (every check does, so the retained message is refreshed)."""
        with self._lock:
            changed = state != self._state
            self._state = state
            if self._link == "up":
                self._publish(self.state_topic, state)
        if changed:
            _LOGGER.info("mqtt-status: the bridge is now %s", state)

    def _publish(self, topic: str, payload: str) -> None:
        # Retained, so Home Assistant has it after its own restart. publish() only queues the packet: it never blocks. Nothing is
        # published while there is no connection, so a long outage cannot pile up old states that would all be replayed.
        self._client.publish(topic, payload, qos=QOS, retain=True)

    # ----- paho callbacks (run on paho's thread; an exception here would stop its network loop) --------------------
    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        try:
            if reason_code.is_failure:
                with self._lock:
                    self._link = "refused"
                self._problem("refused", "mqtt-status: the MQTT broker at %s refused the connection (%s); check HAZEL_MQTT_USER "
                              "and HAZEL_MQTT_PASSWORD. The bridge is not affected; still trying", self._broker, reason_code)
                return
            with self._lock:
                self._link = "up"
                self._publish(self.discovery_topic, json.dumps(discovery_payload(self.config, self.version)))
                self._publish(self.state_topic, self._state)
            if self._tell_when_connected:
                self._tell_when_connected = False
                _LOGGER.info("mqtt-status: connected to the MQTT broker at %s; the sensor is announced", self._broker)
        except Exception:
            _LOGGER.exception("mqtt-status: error while handling the connection to the MQTT broker")

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        try:
            with self._lock:
                before, self._link = self._link, "down"
            if before == "up":
                self._problem("lost", "mqtt-status: lost the connection to the MQTT broker at %s; reconnecting", self._broker)
            elif before == "down":      # a refusal has been reported by _on_connect already; this one never got an answer at all
                self._problem("closed", "mqtt-status: the MQTT broker at %s closed the connection before it accepted it; is that "
                              "really an MQTT broker? The bridge is not affected; still trying", self._broker)
        except Exception:
            _LOGGER.exception("mqtt-status: error while handling a lost MQTT connection")

    def _on_connect_fail(self, client: mqtt.Client, userdata: Any) -> None:
        try:
            error = sys.exc_info()[1]     # paho calls this from inside its ``except OSError``: the reason is still at hand
            self._problem("unreachable", "mqtt-status: cannot reach the MQTT broker at %s (%s). The bridge is not affected; "
                          "still trying", self._broker, error or "no answer")
        except Exception:
            _LOGGER.exception("mqtt-status: error while handling a failed MQTT connection attempt")

    def _problem(self, kind: str, message: str, *args: Any) -> None:
        """Warn about a problem with the broker: the first time, then at most once a minute (a broker that is down is retried for
        ever). Once one has been logged, the next successful connection is logged too, so the log never ends on a warning."""
        if self._throttle.allow(kind):
            _LOGGER.warning(message, *args)
            self._tell_when_connected = True
