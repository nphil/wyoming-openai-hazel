"""A small MQTT 3.1.1 broker for the tests: the other end of the Home Assistant status beacon (``beacon.py``).

It speaks just enough of the protocol for a real client library (paho, the one the image ships) and it *records everything*, so a
test can say exactly what the bridge sent and when:

* CONNECT: protocol, client id, credentials, keep-alive and the Last Will are parsed and kept (``broker.connects``). The answer
  is CONNACK "accepted" or a refusal: code 5 (not authorised) for credentials that do not match ``broker.credentials``, or any
  code in ``broker.refuse_code``; with ``broker.hang_up_on_connect`` it closes the connection without answering at all (a firewall,
  a connection limit, a port that is not a broker). A client that connects with a client id that is already connected takes the
  old connection over.
* PUBLISH QoS 0 and 1 (QoS 1 is answered with PUBACK). Retained messages are kept in ``broker.retained``, like a real broker does;
  an empty retained payload deletes the entry.
* PINGREQ is answered with PINGRESP. A client that is silent for 1.5 x its keep-alive is dropped, as the MQTT standard says.
* When a client's connection ends WITHOUT a DISCONNECT packet (process killed, socket dropped, keep-alive expired, taken over by a
  newer connection) the broker publishes the client's Last Will: it updates the retained store and records the message in
  ``broker.publishes`` with ``will=True``. A DISCONNECT packet makes the broker throw the will away.

Anything the client does that the standard forbids (and the beacon must never do) ends the connection and is put in
``broker.errors``; the ``mqtt_broker`` fixture (conftest.py) fails the test if that list is not empty.

Not implemented, because the beacon never needs them: subscriptions, QoS 2, MQTT 5, TLS, persistent sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")

CONNECT, CONNACK, PUBLISH, PUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 4, 12, 13, 14
REFUSED_PROTOCOL_VERSION, REFUSED_NOT_AUTHORISED = 1, 5
CONNECT_TIMEOUT_S = 5.0
NO_ANSWER = -1                  # ``Connect.return_code`` of a CONNECT the broker hung up on instead of answering


class ProtocolViolation(Exception):
    """The client did something MQTT 3.1.1 does not allow."""


@dataclass(frozen=True)
class Will:
    topic: str
    payload: bytes
    qos: int
    retain: bool

    @property
    def text(self) -> str:
        return self.payload.decode()


@dataclass
class Connect:
    """What one CONNECT packet said, and the code the broker answered with."""

    index: int
    at: float
    protocol_name: str
    protocol_level: int
    client_id: str
    clean_session: bool
    keepalive: int
    username: str | None
    password: str | None
    will: Will | None
    return_code: int = 0

    @property
    def accepted(self) -> bool:
        return self.return_code == 0


@dataclass(frozen=True)
class Published:
    """A PUBLISH the broker received, or a Last Will it published."""

    at: float
    topic: str
    payload: bytes
    qos: int
    retain: bool
    dup: bool
    connection: int                 # index of the CONNECT it belongs to (``broker.connects[connection]``)
    will: bool = False              # published by the broker, for a connection that ended without DISCONNECT

    @property
    def text(self) -> str:
        return self.payload.decode()


class _Parser:
    """Reads the fields of one packet body; a body that is too short is a protocol violation."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0

    def take(self, count: int) -> bytes:
        if self._at + count > len(self._data):
            raise ProtocolViolation("a packet is shorter than its own fields say")
        chunk = self._data[self._at:self._at + count]
        self._at += count
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return int(struct.unpack("!H", self.take(2))[0])

    def binary(self) -> bytes:
        return self.take(self.u16())

    def string(self) -> str:
        try:
            text = self.binary().decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolViolation("a string is not valid UTF-8") from None
        if "\x00" in text:
            raise ProtocolViolation("a string contains a null character")
        return text

    def rest(self) -> bytes:
        chunk = self._data[self._at:]
        self._at = len(self._data)
        return chunk

    def finish(self) -> None:
        if self._at != len(self._data):
            raise ProtocolViolation("a packet has bytes left over")


def _frame(kind: int, body: bytes = b"", flags: int = 0) -> bytes:
    length, encoded = len(body), bytearray()
    while True:
        length, byte = divmod(length, 128)
        encoded.append(byte | (0x80 if length else 0))
        if not length:
            return bytes([kind << 4 | flags]) + bytes(encoded) + body


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    first = (await reader.readexactly(1))[0]
    length = shift = 0
    for _ in range(4):
        byte = (await reader.readexactly(1))[0]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    else:
        raise ProtocolViolation("a remaining length is longer than four bytes")
    body = await reader.readexactly(length) if length else b""
    return first >> 4, first & 0x0F, body


@dataclass
class _Live:
    connect: Connect
    writer: asyncio.StreamWriter
    will_done: bool = False


class FakeBroker:
    """``broker = await FakeBroker().start()``; point the bridge at ``127.0.0.1:{broker.port}``; ``await broker.stop()`` at the end."""

    def __init__(self, *, credentials: dict[str, str] | None = None, refuse_code: int | None = None,
                 enforce_keepalive: bool = True) -> None:
        self.credentials = credentials          # None: anybody may connect. Else {user: password}; others get code 5
        self.refuse_code = refuse_code          # refuse every CONNECT with this code (None: do not)
        self.hang_up_on_connect = False         # close every new connection without a word, once it has sent CONNECT
        self.enforce_keepalive = enforce_keepalive
        self.port = 0
        self.connects: list[Connect] = []
        self.publishes: list[Published] = []
        self.retained: dict[str, Published] = {}
        self.disconnects: list[int] = []        # connections that ended with a DISCONNECT packet (a clean goodbye)
        self.pings = 0
        self.errors: list[str] = []
        self._server: asyncio.Server | None = None
        self._live: dict[int, _Live] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        self._started_at = time.monotonic()

    # ----- lifecycle ---------------------------------------------------------------------------------------------
    async def start(self, port: int = 0) -> FakeBroker:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        self.port = sock.getsockname()[1]
        self._stopping = False
        self._server = await asyncio.start_server(self._client, sock=sock)
        return self

    async def stop(self) -> None:
        """Stop listening and drop every client the way a stopping broker does (no wills are published)."""
        self._stopping = True
        if self._server is not None:
            self._server.close()
        for live in list(self._live.values()):
            live.writer.close()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    async def restart(self, *, downtime_s: float = 0.0, forget_retained: bool = False) -> None:
        """Stop, wait ``downtime_s``, start again on the same port. ``forget_retained``: a broker without persistence."""
        await self.stop()
        if forget_retained:
            self.retained.clear()
        if downtime_s:
            await asyncio.sleep(downtime_s)
        await self.start(self.port)

    async def drop_connections(self) -> None:
        """Close every client's socket without any MQTT goodbye (a network failure): the wills are published."""
        for live in list(self._live.values()):
            live.writer.close()
        await asyncio.sleep(0)

    async def __aenter__(self) -> FakeBroker:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ----- what a test asks ------------------------------------------------------------------------------------
    @property
    def accepted(self) -> list[Connect]:
        return [c for c in self.connects if c.accepted]

    def state(self, mqtt_id: str = "main") -> str | None:
        """The retained payload of the bridge's state topic (None: nothing retained)."""
        record = self.retained.get(f"wyoming-openai-hazel/{mqtt_id}/state")
        return record.text if record else None

    def state_log(self, mqtt_id: str = "main") -> list[Published]:
        """Everything that arrived on the state topic, in order; a Last Will is in it too (``will=True``)."""
        topic = f"wyoming-openai-hazel/{mqtt_id}/state"
        return [p for p in self.publishes if p.topic == topic]

    def discovery_topic(self, mqtt_id: str = "main", prefix: str = "homeassistant") -> str:
        return f"{prefix}/binary_sensor/wyoming_openai_hazel_{mqtt_id}/running/config"

    def discovery_log(self, mqtt_id: str = "main", prefix: str = "homeassistant") -> list[Published]:
        topic = self.discovery_topic(mqtt_id, prefix)
        return [p for p in self.publishes if p.topic == topic]

    def dump(self) -> str:
        """Everything that happened, one line each (put into the failure message of ``until``)."""
        lines = [f"broker 127.0.0.1:{self.port}; {len(self.connects)} connect(s), {len(self.publishes)} publish(es), "
                 f"{self.pings} ping(s), {len(self.disconnects)} DISCONNECT(s), errors={self.errors}"]
        for connect in self.connects:
            lines.append(f"  +{connect.at - self._started_at:7.2f}s CONNECT #{connect.index} id={connect.client_id!r} "
                         f"keepalive={connect.keepalive} will={connect.will} -> code {connect.return_code}")
        for p in self.publishes:
            lines.append(f"  +{p.at - self._started_at:7.2f}s {'WILL   ' if p.will else 'PUBLISH'} #{p.connection} qos{p.qos}"
                         f"{' retain' if p.retain else ''} {p.topic} = {p.payload[:60]!r}")
        return "\n".join(lines)

    async def until(self, what: str, check: Callable[[], T], timeout: float = 15.0) -> T:
        """Wait until ``check()`` gives something truthy and return it. A timeout fails the test with everything that was recorded."""
        deadline = time.monotonic() + timeout
        while True:
            value = check()
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out after {timeout:g} s waiting for {what}\n{self.dump()}")
            await asyncio.sleep(0.02)

    # ----- one client --------------------------------------------------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        connect: Connect | None = None
        said_goodbye = False
        try:
            connect = await asyncio.wait_for(self._handshake(reader, writer), CONNECT_TIMEOUT_S)
            if connect is None:
                return
            timeout = connect.keepalive * 1.5 if connect.keepalive and self.enforce_keepalive else None
            while True:
                kind, flags, body = await asyncio.wait_for(_read_packet(reader), timeout)
                if kind == PUBLISH:
                    self._on_publish(connect, flags, body, writer)
                elif kind == PINGREQ and not flags and not body:
                    self.pings += 1
                    writer.write(_frame(PINGRESP))
                elif kind == DISCONNECT and not flags and not body:
                    said_goodbye = True
                    self.disconnects.append(connect.index)
                    return
                else:
                    raise ProtocolViolation(f"unexpected packet type {kind} (flags {flags}, {len(body)} bytes)")
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass                        # the connection ended: process killed, socket dropped, or the keep-alive ran out
        except ProtocolViolation as violation:
            self.errors.append(str(violation))
        finally:
            if connect is not None and connect.accepted:
                live = self._live.get(connect.index)
                self._live.pop(connect.index, None)
                if live is not None and not said_goodbye and not self._stopping:
                    self._publish_will(live)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if task is not None:
                self._tasks.discard(task)

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Connect | None:
        kind, flags, body = await _read_packet(reader)
        if kind != CONNECT or flags:
            raise ProtocolViolation("the first packet of a connection was not CONNECT")
        data = _Parser(body)
        name, level, bits, keepalive = data.string(), data.u8(), data.u8(), data.u16()
        will_flag, will_qos, will_retain = bool(bits & 0x04), (bits >> 3) & 3, bool(bits & 0x20)
        has_user, has_password = bool(bits & 0x80), bool(bits & 0x40)
        if bits & 0x01:
            raise ProtocolViolation("CONNECT with the reserved flag set")
        if not will_flag and (will_qos or will_retain):
            raise ProtocolViolation("CONNECT: will QoS or will retain without a will")
        if has_password and not has_user:
            raise ProtocolViolation("CONNECT: a password without a user name")
        client_id = data.string()
        will = Will(data.string(), data.binary(), will_qos, will_retain) if will_flag else None
        username = data.string() if has_user else None
        password = data.binary().decode() if has_password else None
        data.finish()

        connect = Connect(len(self.connects), time.monotonic(), name, level, client_id, bool(bits & 0x02), keepalive,
                          username, password, will)
        self.connects.append(connect)
        if name != "MQTT" or level != 4:
            self.errors.append(f"CONNECT for {name!r} level {level}: the beacon must speak MQTT 3.1.1 (level 4)")
            connect.return_code = REFUSED_PROTOCOL_VERSION
        elif self.hang_up_on_connect:
            connect.return_code = NO_ANSWER
        elif self.refuse_code is not None:
            connect.return_code = self.refuse_code
        elif self.credentials is not None and self.credentials.get(username or "") != password:
            connect.return_code = REFUSED_NOT_AUTHORISED
        if not connect.accepted:
            if connect.return_code != NO_ANSWER:
                writer.write(_frame(CONNACK, bytes([0, connect.return_code])))
                await writer.drain()
            return None

        for old in [live for live in self._live.values() if live.connect.client_id == client_id]:
            self._publish_will(old)         # a second connection with the same client id replaces the first one
            old.writer.close()
        self._live[connect.index] = _Live(connect, writer)
        writer.write(_frame(CONNACK, bytes([0, 0])))
        await writer.drain()
        return connect

    def _on_publish(self, connect: Connect, flags: int, body: bytes, writer: asyncio.StreamWriter) -> None:
        qos, retain, dup = (flags >> 1) & 3, bool(flags & 1), bool(flags & 8)
        if qos > 1:
            raise ProtocolViolation(f"PUBLISH with QoS {qos}: the beacon uses QoS 0 and 1 only")
        data = _Parser(body)
        topic = data.string()
        if not topic or "+" in topic or "#" in topic:
            raise ProtocolViolation(f"PUBLISH to the invalid topic {topic!r}")
        packet_id = data.u16() if qos else None
        if qos and not packet_id:
            raise ProtocolViolation("PUBLISH QoS 1 with packet identifier 0")
        record = Published(time.monotonic(), topic, data.rest(), qos, retain, dup, connect.index)
        self.publishes.append(record)
        if retain:
            self._retain(record)
        if packet_id is not None:
            writer.write(_frame(PUBACK, struct.pack("!H", packet_id)))

    def _retain(self, record: Published) -> None:
        if record.payload:
            self.retained[record.topic] = record
        else:
            self.retained.pop(record.topic, None)

    def _publish_will(self, live: _Live) -> None:
        will = live.connect.will
        if will is None or live.will_done:
            return
        live.will_done = True
        record = Published(time.monotonic(), will.topic, will.payload, will.qos, will.retain, False, live.connect.index, will=True)
        self.publishes.append(record)
        if will.retain:
            self._retain(record)
