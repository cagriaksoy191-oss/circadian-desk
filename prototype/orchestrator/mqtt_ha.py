"""Zero-dependency MQTT v3.1.1 client & Home Assistant Auto-Discovery bridge (stdlib only).

Publishes eye-level optical metrology & orchestrator state to any local MQTT broker
(e.g., Mosquitto / Home Assistant) without requiring `paho-mqtt` or external packages.

Features:
- RFC-compliant MQTT v3.1.1 wire packet builders (CONNECT + LWT, PUBLISH QoS 0,
  PINGREQ, DISCONNECT, variable-length Remaining Length encoding, CONNACK validation).
- Automatic Home Assistant MQTT Discovery registration for 7 retained entities
  (5 sensors + 2 binary_sensors) sharing one device & availability topic.
- Never-crash resilience: logs once on broker loss, applies bounded reconnect cooldown
  (>= 10s), and returns False cleanly without ever raising into daemon.py.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import struct
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# MQTT v3.1.1 Packet Types (high nibble of fixed header byte 0)
_PKT_CONNECT = 0x10
_PKT_CONNACK = 0x20
_PKT_PUBLISH = 0x30
_PKT_PINGREQ = 0xC0
_PKT_PINGRESP = 0xD0
_PKT_DISCONNECT = 0xE0

_MAX_REMAINING_LEN = 268_435_455  # 0x0FFFFFFF per MQTT 3.1.1 §2.2.3


def _sanitize_id(raw: str, default: str = "desk_primary") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(raw or "").strip()).strip("_").lower()
    return cleaned or default


def _encode_remaining_length(n: int) -> bytes:
    """Encode MQTT v3.1.1 variable-length Remaining Length (1..4 bytes)."""
    if not isinstance(n, int) or isinstance(n, bool) or n < 0 or n > _MAX_REMAINING_LEN:
        raise ValueError(f"MQTT remaining length out of range [0, {_MAX_REMAINING_LEN}]: {n!r}")
    out = bytearray()
    val = n
    while True:
        encoded = val % 128
        val //= 128
        if val > 0:
            encoded |= 0x80
        out.append(encoded)
        if val == 0:
            break
    return bytes(out)


def _encode_utf8(text: str) -> bytes:
    """Encode a length-prefixed UTF-8 string per MQTT v3.1.1 §1.5.3."""
    raw = text.encode("utf-8")
    if len(raw) > 65535:
        raise ValueError(f"MQTT UTF-8 string exceeds 65535 bytes ({len(raw)} bytes)")
    return struct.pack("!H", len(raw)) + raw


def build_connect_packet(
    client_id: str,
    username: str = "",
    password: str = "",
    keepalive_s: int = 60,
    will_topic: str = "",
    will_payload: str | bytes = "",
    will_retain: bool = True,
) -> bytes:
    """Build an RFC-compliant MQTT v3.1.1 CONNECT packet (Clean Session + optional LWT/auth)."""
    flags = 0x02  # Clean Session = 1
    has_will = bool(will_topic)
    has_user = bool(username)
    # Per MQTT 3.1.1 [MQTT-3.1.2-22]: Password flag MUST be 0 if User Name flag is 0.
    has_pass = has_user and bool(password)

    if has_will:
        flags |= 0x04  # Will Flag (QoS 0)
        if will_retain:
            flags |= 0x20  # Will Retain
    if has_user:
        flags |= 0x80
    if has_pass:
        flags |= 0x40

    ka = max(0, min(65535, int(keepalive_s)))
    var_header = _encode_utf8("MQTT") + bytes((0x04, flags)) + struct.pack("!H", ka)

    # Payload order per MQTT 3.1.1 §3.1.3: Client ID, Will Topic, Will Message, User, Pass
    payload_parts: list[bytes] = [_encode_utf8(str(client_id))]
    if has_will:
        payload_parts.append(_encode_utf8(will_topic))
        will_bytes = (
            will_payload.encode("utf-8") if isinstance(will_payload, str) else bytes(will_payload)
        )
        if len(will_bytes) > 65535:
            raise ValueError("MQTT will payload exceeds 65535 bytes")
        payload_parts.append(struct.pack("!H", len(will_bytes)) + will_bytes)
    if has_user:
        payload_parts.append(_encode_utf8(username))
    if has_pass:
        payload_parts.append(_encode_utf8(password))

    body = var_header + b"".join(payload_parts)
    return bytes((_PKT_CONNECT,)) + _encode_remaining_length(len(body)) + body


def build_publish_packet(
    topic: str,
    payload: str | bytes,
    retain: bool = False,
) -> bytes:
    """Build an MQTT v3.1.1 QoS-0 PUBLISH packet."""
    if not topic:
        raise ValueError("MQTT publish topic must be non-empty")
    header_byte = _PKT_PUBLISH | (0x01 if retain else 0x00)
    payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    body = _encode_utf8(topic) + payload_bytes
    return bytes((header_byte,)) + _encode_remaining_length(len(body)) + body


def build_pingreq_packet() -> bytes:
    """Build an MQTT v3.1.1 PINGREQ packet."""
    return bytes((_PKT_PINGREQ, 0x00))


def build_disconnect_packet() -> bytes:
    """Build an MQTT v3.1.1 DISCONNECT packet."""
    return bytes((_PKT_DISCONNECT, 0x00))


def _validate_connack(data: bytes) -> tuple[bool, int]:
    """Validate a 4-byte MQTT v3.1.1 CONNACK frame; returns (accepted, return_code)."""
    if len(data) != 4:
        return False, -1
    if data[0] != _PKT_CONNACK or data[1] != 0x02:
        return False, -1
    rc = data[3]
    return rc == 0x00, rc


def _recv_exact(sock: Any, n: int) -> bytes:
    """Read exactly n bytes from a stream socket (handles partial TCP chunks)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def build_discovery_configs(
    discovery_prefix: str = "homeassistant",
    node_id: str = "desk_primary",
) -> list[tuple[str, dict[str, Any]]]:
    """Build the 7 Home Assistant MQTT Auto-Discovery (topic, config_dict) pairs."""
    prefix = str(discovery_prefix or "homeassistant").strip().strip("/") or "homeassistant"
    nid = _sanitize_id(node_id)
    device_id = f"circadian_desk_{nid}"
    state_topic = f"circadian_desk/{nid}/state"
    availability_topic = f"circadian_desk/{nid}/availability"

    device_block: dict[str, Any] = {
        "identifiers": [device_id],
        "name": "Eye-Level Spectral Light Orchestrator",
        "manufacturer": "Circadian Desk (Local)",
        "model": "V1A / Open-Loop Desktop",
    }

    def _base(key: str, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "unique_id": f"{device_id}_{key}",
            "state_topic": state_topic,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_block,
        }

    entities: list[tuple[str, str, dict[str, Any]]] = [
        (
            "sensor",
            "m_edi",
            {
                **_base("m_edi", "Eye-Level m-EDI"),
                "value_template": "{{ value_json.m_edi_lx }}",
                "unit_of_measurement": "lx",
                "state_class": "measurement",
                "device_class": "illuminance",
            },
        ),
        (
            "sensor",
            "compliance",
            {
                **_base("compliance", "Circadian Compliance (k=2)"),
                "value_template": "{{ value_json.compliance_status }}",
            },
        ),
        (
            "sensor",
            "phase",
            {
                **_base("phase", "Circadian Phase"),
                "value_template": "{{ value_json.phase }}",
            },
        ),
        (
            "sensor",
            "cct",
            {
                **_base("cct", "Ambient Color Temperature"),
                "value_template": "{{ value_json.cct_k }}",
                "unit_of_measurement": "K",
                "state_class": "measurement",
            },
        ),
        (
            "sensor",
            "monitor_brightness",
            {
                **_base("monitor_brightness", "Monitor Brightness"),
                "value_template": "{{ value_json.monitor_brightness_pct }}",
                "unit_of_measurement": "%",
            },
        ),
        (
            "binary_sensor",
            "unlinked_light",
            {
                **_base("unlinked_light", "Unlinked Light Alert"),
                "value_template": "{{ 'ON' if value_json.unlinked_light_alert else 'OFF' }}",
                "device_class": "problem",
            },
        ),
        (
            "binary_sensor",
            "sensor_occluded",
            {
                **_base("sensor_occluded", "Sensor Occluded"),
                "value_template": "{{ 'ON' if value_json.sensor_occluded else 'OFF' }}",
                "device_class": "problem",
            },
        ),
    ]

    return [
        (f"{prefix}/{component}/{device_id}/{key}/config", cfg)
        for component, key, cfg in entities
    ]


def _default_socket_factory(host: str, port: int, timeout_s: float) -> socket.socket:
    sock = socket.create_connection((host, int(port)), timeout=float(timeout_s))
    sock.settimeout(float(timeout_s))
    return sock


class HomeAssistantMQTTBridge:
    """Stdlib-only MQTT v3.1.1 publisher with Home Assistant Auto-Discovery."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        discovery_prefix: str = "homeassistant",
        node_id: str = "desk_primary",
        socket_factory: Callable[[str, int, float], Any] | None = None,
        timeout_s: float = 2.0,
        keepalive_s: int = 60,
        reconnect_cooldown_s: float = 10.0,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        self.host = str(host or "").strip()
        self.port = int(port)
        self.username = str(username or "")
        self.password = str(password or "")
        self.discovery_prefix = str(discovery_prefix or "homeassistant").strip().strip("/") or "homeassistant"
        self.node_id = _sanitize_id(node_id)
        self.timeout_s = max(0.2, float(timeout_s))
        self.keepalive_s = max(10, int(keepalive_s))
        self.reconnect_cooldown_s = max(10.0, float(reconnect_cooldown_s))

        self._socket_factory = socket_factory or _default_socket_factory
        self._monotonic = monotonic_fn or time.monotonic

        self.device_id = f"circadian_desk_{self.node_id}"
        self.state_topic = f"circadian_desk/{self.node_id}/state"
        self.availability_topic = f"circadian_desk/{self.node_id}/availability"
        self.client_id = f"{self.device_id}"

        self._sock: Any | None = None
        self._connected: bool = False
        self._warned_unreachable: bool = False
        self._next_retry_at: float = 0.0
        self._last_tx_at: float = 0.0

    @property
    def connected(self) -> bool:
        return self._connected and self._sock is not None

    def build_discovery_configs(self) -> list[tuple[str, dict[str, Any]]]:
        """Return the 7 Home Assistant MQTT Discovery (topic, config_dict) tuples."""
        return build_discovery_configs(
            discovery_prefix=self.discovery_prefix,
            node_id=self.node_id,
        )

    def _drop_connection(self, reason: str) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        self._connected = False
        self._next_retry_at = self._monotonic() + self.reconnect_cooldown_s
        if not self._warned_unreachable:
            self._warned_unreachable = True
            logger.warning(
                "MQTT broker %s:%d unavailable (%s) — pausing MQTT publish for %.0fs.",
                self.host,
                self.port,
                reason,
                self.reconnect_cooldown_s,
            )

    def _send_all(self, data: bytes) -> None:
        assert self._sock is not None
        self._sock.sendall(data)
        self._last_tx_at = self._monotonic()

    def connect(self) -> bool:
        """Connect to broker, validate CONNACK, publish 7 retained HA discovery configs & online LWT."""
        if not self.host:
            return False
        if self.connected:
            return True

        now = self._monotonic()
        if self._next_retry_at > 0.0 and now < self._next_retry_at:
            return False

        try:
            sock = self._socket_factory(self.host, self.port, self.timeout_s)
            self._sock = sock

            connect_pkt = build_connect_packet(
                client_id=self.client_id,
                username=self.username,
                password=self.password,
                keepalive_s=self.keepalive_s,
                will_topic=self.availability_topic,
                will_payload="offline",
                will_retain=True,
            )
            self._send_all(connect_pkt)

            connack = _recv_exact(sock, 4)
            ok, rc = _validate_connack(connack)
            if not ok:
                self._drop_connection(f"invalid or refused CONNACK (rc={rc})")
                return False

            # Publish all 7 retained Home Assistant Auto-Discovery configs
            for topic, cfg in self.build_discovery_configs():
                cfg_json = json.dumps(cfg, separators=(",", ":"), ensure_ascii=False)
                self._send_all(build_publish_packet(topic, cfg_json, retain=True))

            # Publish retained online availability
            self._send_all(build_publish_packet(self.availability_topic, "online", retain=True))

            self._connected = True
            self._warned_unreachable = False
            self._next_retry_at = 0.0
            return True
        except Exception as exc:
            self._drop_connection(str(exc) or exc.__class__.__name__)
            return False

    def publish_state(self, payload: dict[str, Any]) -> bool:
        """Publish JSON state payload to `circadian_desk/<node_id>/state` (never raises)."""
        if not self.host:
            return False
        if not self.connected and not self.connect():
            return False

        try:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            self._send_all(build_publish_packet(self.state_topic, body, retain=False))
            return True
        except Exception as exc:
            self._drop_connection(str(exc) or exc.__class__.__name__)
            return False

    def ping(self) -> bool:
        """Send MQTT PINGREQ and check for PINGRESP (never raises)."""
        if not self.connected:
            return False
        try:
            self._send_all(build_pingreq_packet())
            resp = _recv_exact(self._sock, 2)
            if resp == bytes((_PKT_PINGRESP, 0x00)):
                return True
            self._drop_connection("invalid PINGRESP")
            return False
        except Exception as exc:
            self._drop_connection(str(exc) or exc.__class__.__name__)
            return False

    def close(self) -> None:
        """Publish retained 'offline' availability, send DISCONNECT, and close socket safely."""
        sock = self._sock
        was_connected = self._connected
        self._sock = None
        self._connected = False
        if sock is None:
            return
        if was_connected:
            try:
                sock.sendall(build_publish_packet(self.availability_topic, "offline", retain=True))
                sock.sendall(build_disconnect_packet())
            except Exception:
                pass
        try:
            sock.close()
        except Exception:
            pass

    def __enter__(self) -> "HomeAssistantMQTTBridge":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
