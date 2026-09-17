"""The link to the hub. Transport only: nothing here decides anything.

Paho is imported when a connection is actually made, so that `validate`, `doctor` and the
whole test suite run on a board — or a laptop — where it is not installed. What the agent
talks to is the `Transport` protocol below; MQTT is one implementation of it, and the
tests use another.
"""

import logging
import socket
import ssl
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

from sentry_satellite.config import Config

log = logging.getLogger(__name__)

Handler = Callable[[str, bytes], None]


class TransportError(RuntimeError):
    """The link could not be established, or was refused."""


class Transport(Protocol):
    connection_id: str

    @property
    def connected(self) -> bool: ...

    def set_will(self, topic: str, payload: bytes) -> None: ...

    def connect(self, connection_id: str) -> None: ...

    def subscribe(self, topic: str, handler: Handler) -> None: ...

    def publish(self, topic: str, payload: bytes, qos: int = 1, retain: bool = False) -> bool: ...

    def disconnect(self) -> None: ...


def tls_context(ca_file: Path, cert_file: Path, key_file: Path) -> ssl.SSLContext:
    """A context that verifies the hub and proves who we are.

    There is no switch here for turning verification off. An installation that cannot
    verify the hub's certificate is misconfigured, and the fix is a certificate whose name
    matches the address the satellites actually use — including when that is an IP address.
    """
    for path in (ca_file, cert_file, key_file):
        if not path.exists():
            raise TransportError(f"{path} does not exist")
    try:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_file))
    except ssl.SSLError as error:
        raise TransportError(f"{ca_file} is not a certificate authority") from error
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    except (ssl.SSLError, OSError) as error:
        raise TransportError(f"{cert_file} and {key_file} are not a usable pair") from error
    return context


def media_connector(config: Config, *, timeout: float = 10.0) -> Callable[[int], Any]:
    """Connections to the hub's media port: the MQTT host, the node's certificate.

    The host is the one in the configuration file. A command names a port and nothing
    else, so the hub cannot point a camera at anybody but itself.
    """

    def connect(port: int) -> ssl.SSLSocket:
        context = tls_context(config.tls.ca_file, config.tls.cert_file, config.tls.key_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        raw = socket.create_connection((config.hub.mqtt_host, port), timeout=timeout)
        try:
            return context.wrap_socket(raw, server_hostname=config.hub.mqtt_host)
        except BaseException:
            raw.close()
            raise

    return connect


class MqttTransport:
    """Paho, configured the way the hub expects and bounded the way the board requires."""

    def __init__(self, config: Config, *, keepalive: int = 30) -> None:
        self._config = config
        self._keepalive = keepalive
        self._client: Any = None
        self._handlers: dict[str, Handler] = {}
        self._connected = False
        self._will: tuple[str, bytes] | None = None
        self.connection_id = str(uuid.uuid4())

    @property
    def connected(self) -> bool:
        return self._connected

    def _build(self):
        try:
            from paho.mqtt import client as paho
        except ImportError as error:  # pragma: no cover - depends on the installation
            raise TransportError(
                "paho-mqtt is not installed; on Raspberry Pi OS it is python3-paho-mqtt"
            ) from error
        client = paho.Client(
            callback_api_version=paho.CallbackAPIVersion.VERSION2,
            client_id=self._config.node_id,
            protocol=paho.MQTTv5,
        )
        client.tls_set_context(
            tls_context(
                self._config.tls.ca_file, self._config.tls.cert_file, self._config.tls.key_file
            )
        )
        client.max_queued_messages_set(self._config.limits.event_max_count)
        client.max_inflight_messages_set(8)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def set_will(self, topic: str, payload: bytes) -> None:
        """What the broker says on this node's behalf if the link dies without a goodbye.

        It is set before connecting, because afterwards is too late, and it names the
        connection it belongs to so the hub can ignore it if the node has already come
        back on a newer one.
        """
        self._will = (topic, payload)

    def connect(self, connection_id: str) -> None:
        self.connection_id = connection_id
        self.disconnect()  # an attempt that was given up on must not leave its socket open
        self._client = self._build()
        if self._will is not None:
            topic, payload = self._will
            self._client.will_set(topic, payload, qos=1, retain=True)
        try:
            self._client.connect(
                self._config.hub.mqtt_host, self._config.hub.mqtt_port, keepalive=self._keepalive
            )
        except OSError as error:
            raise TransportError(f"cannot reach the hub: {error}") from error
        self._client.loop_start()

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._handlers[topic] = handler
        if self._client is not None:
            self._client.subscribe(topic, qos=1)

    def publish(self, topic: str, payload: bytes, qos: int = 1, retain: bool = False) -> bool:
        if self._client is None:
            return False
        result = self._client.publish(topic, payload, qos=qos, retain=retain)
        return result.rc == 0

    def disconnect(self) -> None:
        client, self._client = self._client, None
        self._connected = False
        if client is None:
            return
        try:
            client.disconnect()
        finally:
            client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._connected = getattr(reason_code, "is_failure", False) is False
        if not self._connected:
            log.warning("the hub refused the connection: %s", reason_code)
            return
        for topic in self._handlers:
            client.subscribe(topic, qos=1)

    def _on_disconnect(self, client, userdata, *args) -> None:
        self._connected = False
        log.info("disconnected from the hub")

    def _on_message(self, client, userdata, message) -> None:
        handler = self._handlers.get(message.topic)
        if handler is None:
            return
        try:
            handler(message.topic, message.payload)
        except Exception:  # noqa: BLE001 - a bad message must not take the link down
            log.exception("a message on %s could not be handled", message.topic)
