"""The stand-in MQTT broker (``mqtt_broker.py``) against a real client library.

The beacon tests rely on how the stand-in treats pings, keep-alives, acknowledgements, retained messages and the Last Will, exactly
as a real broker (Mosquitto) does. These tests pin those behaviours with paho, the library the image ships, so that a mistake in
the stand-in cannot make a beacon test pass for the wrong reason. (They block no event loop: paho runs on its own thread, and
anything that waits for the broker is moved off the loop that serves it.)
"""

from __future__ import annotations

import asyncio
import time

import paho.mqtt.client as mqtt
from mqtt_broker import FakeBroker


def connect(broker: FakeBroker, client_id: str, *, keepalive: int = 20, will: bool = True) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311)
    if will:
        client.will_set("test/will", "gone", qos=1, retain=True)
    client.connect_async("127.0.0.1", broker.port, keepalive=keepalive)
    client.loop_start()
    return client


async def test_a_client_that_pings_stays_and_one_that_falls_silent_is_dropped_with_its_will_published(mqtt_broker) -> None:
    # paho pings between 1 and 1.25 keep-alives apart, the broker waits 1.5: a healthy client has about a second to spare here.
    client = connect(mqtt_broker, "quiet-one", keepalive=4)
    try:
        await mqtt_broker.until("the connection", lambda: mqtt_broker.accepted, 10)
        await asyncio.sleep(7.0)                               # more than 1.5 x the keep-alive: only its pings keep it here
        assert mqtt_broker.pings >= 1
        assert len(mqtt_broker.accepted) == 1 and not mqtt_broker.publishes
        client.loop_stop()                                     # no more pings and no goodbye: the connection just goes quiet
        silent_since = time.monotonic()
        will = await mqtt_broker.until("the will", lambda: next((p for p in mqtt_broker.publishes if p.will), None), 10)
        assert will.at - silent_since < 7.0
        assert (will.topic, will.text, will.qos, will.retain) == ("test/will", "gone", 1, True)
        assert mqtt_broker.retained["test/will"] is will and mqtt_broker.disconnects == []
    finally:
        client.loop_stop()


async def test_a_clean_goodbye_makes_the_broker_throw_the_will_away(mqtt_broker) -> None:
    client = connect(mqtt_broker, "polite-one")
    try:
        await mqtt_broker.until("the connection", lambda: mqtt_broker.accepted, 10)
        client.disconnect()                                    # the MQTT goodbye: the one thing the beacon must never send
        await mqtt_broker.until("the goodbye", lambda: mqtt_broker.disconnects, 5)
        await asyncio.sleep(0.5)
        assert not any(p.will for p in mqtt_broker.publishes) and "test/will" not in mqtt_broker.retained
    finally:
        client.loop_stop()


async def test_qos1_is_acknowledged_and_retained_messages_are_kept_replaced_and_deleted(mqtt_broker) -> None:
    client = connect(mqtt_broker, "publisher", will=False)
    try:
        await mqtt_broker.until("the connection", lambda: mqtt_broker.accepted, 10)

        async def publish(payload: str | None, *, qos: int = 1, retain: bool = True, topic: str = "test/state") -> None:
            info = client.publish(topic, payload, qos=qos, retain=retain)
            await asyncio.to_thread(info.wait_for_publish, 5)
            assert info.is_published()                         # with QoS 1 that means the PUBACK came back

        await publish("one")
        assert mqtt_broker.retained["test/state"].text == "one"
        await publish("two")
        assert mqtt_broker.retained["test/state"].text == "two"
        await publish(None)                                    # an empty retained payload deletes the message
        assert "test/state" not in mqtt_broker.retained
        await publish("not kept", qos=0, retain=False, topic="test/plain")
        await mqtt_broker.until("the QoS 0 message", lambda: any(p.topic == "test/plain" for p in mqtt_broker.publishes), 5)
        assert "test/plain" not in mqtt_broker.retained
        seen = [(p.topic, p.text, p.qos, p.retain) for p in mqtt_broker.publishes]
        assert seen == [("test/state", "one", 1, True), ("test/state", "two", 1, True), ("test/state", "", 1, True),
                        ("test/plain", "not kept", 0, False)]
    finally:
        client.loop_stop()
