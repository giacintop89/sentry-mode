"""The hub's end of the link. Transport only: it carries messages and decides nothing.

Paho is an optional dependency, imported when a connection is actually opened, so a node
with satellites switched off never loads it. What arrives here is a topic and some bytes;
who sent it is worked out from the topic, which the broker's access control is what makes
trustworthy, and never from the contents of the message.
"""

from __future__ import annotations

import logging
import ssl
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sentry_mode.satellites.config import MqttConfig

log = logging.getLogger(__name__)

CHANNELS = ("events", "state", "health", "acks")
"""What a satellite may write. `commands` goes the other way and is never subscribed to."""


class TransportError(RuntimeError):
    """The link could not be opened, or the certificates are not usable."""


@dataclass(frozen=True)
class Message:
    """One message, with the identity the topic gives it already separated out."""

    node_id: str
    channel: str
    payload: bytes
    retained: bool
    topic: str


def parse_topic(topic: str, prefix: str) -> tuple[str, str] | None:
    """Pull the node and the channel out of a topic, or refuse the topic.

    This is the only place the hub learns who sent something. It works because the broker
    refuses to let a node write anywhere but under its own name; without that access
    control the topic would be a claim like any other.
    """
    expected = f"{prefix}/nodes/"
    if not topic.startswith(expected):
        return None
    rest = topic[len(expected) :]
    node_id, _, channel = rest.partition("/")
    if not node_id or channel not in CHANNELS:
        return None
    return node_id, channel


def tls_context(config: MqttConfig) -> ssl.SSLContext:
    """Verify the broker, and prove to it that we are the hub.

    There is no option here for turning verification off. If the certificate does not match
    the address the satellites use, the certificate is what needs fixing.
    """
    for path in (config.tls_ca_file, config.tls_cert_file, config.tls_key_file):
        if not Path(path).exists():
            raise TransportError(f"{path} does not exist")
    try:
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=str(config.tls_ca_file)
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(
            certfile=str(config.tls_cert_file), keyfile=str(config.tls_key_file)
        )
    except (ssl.SSLError, OSError) as error:
        raise TransportError(f"the hub's certificates are not usable: {error}") from error
    return context


class HubTransport:
    """Subscribes to every node's topics and publishes commands to one node at a time."""

    def __init__(
        self,
        config: MqttConfig,
        *,
        on_message: Callable[[Message], None],
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        client_id: str = "sentry-mode-hub",
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._client_id = client_id
        self._client: Any = None
        self._connected = threading.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _build(self):
        try:
            from paho.mqtt import client as paho
        except ImportError as error:  # pragma: no cover - depends on the installation
            raise TransportError(
                "satellites need paho-mqtt: install sentry-mode with the 'satellites' extra"
            ) from error
        client = paho.Client(
            callback_api_version=paho.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            protocol=paho.MQTTv5,
        )
        client.tls_set_context(tls_context(self._config))
        client.max_inflight_messages_set(20)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        return client

    def start(self) -> None:
        self._client = self._build()
        try:
            self._client.connect_async(
                self._config.host, self._config.port, keepalive=self._config.keepalive_seconds
            )
        except OSError as error:
            raise TransportError(f"cannot reach the broker: {error}") from error
        self._client.loop_start()

    def stop(self) -> None:
        client, self._client = self._client, None
        self._connected.clear()
        if client is None:
            return
        try:
            client.disconnect()
        finally:
            client.loop_stop()

    def publish_command(self, node_id: str, command: bytes) -> bool:
        """Send a command to one node. There is no topic here that reaches all of them."""
        if self._client is None:
            return False
        topic = f"{self._config.topic_prefix}/nodes/{node_id}/commands"
        return self._client.publish(topic, command, qos=1, retain=False).rc == 0

    def clear_retained_state(self, node_id: str) -> bool:
        """Erase a node's retained snapshot, so a revoked node does not linger on the wire."""
        if self._client is None:
            return False
        topic = f"{self._config.topic_prefix}/nodes/{node_id}/state"
        return self._client.publish(topic, b"", qos=1, retain=True).rc == 0

    # -- paho callbacks ----------------------------------------------------------

    def _handle_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            log.warning("the broker refused the hub: %s", reason_code)
            return
        self._connected.set()
        for channel in CHANNELS:
            client.subscribe(f"{self._config.topic_prefix}/nodes/+/{channel}", qos=1)
        if self._on_connect:
            self._on_connect()

    def _handle_disconnect(self, client, userdata, *args) -> None:
        self._connected.clear()
        log.warning("the broker connection dropped")
        if self._on_disconnect:
            self._on_disconnect()

    def _handle_message(self, client, userdata, message) -> None:
        parsed = parse_topic(message.topic, self._config.topic_prefix)
        if parsed is None:
            log.debug("ignoring a message on %s", message.topic)
            return
        node_id, channel = parsed
        try:
            self._on_message(
                Message(
                    node_id=node_id,
                    channel=channel,
                    payload=message.payload,
                    retained=bool(getattr(message, "retain", False)),
                    topic=message.topic,
                )
            )
        except Exception:  # noqa: BLE001 - one bad message must not take the link down
            log.exception("a message on %s could not be handled", message.topic)


def new_client_id(prefix: str = "sentry-mode-hub") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
