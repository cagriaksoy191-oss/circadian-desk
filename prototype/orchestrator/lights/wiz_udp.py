"""Local-only WiZ smart bulb UDP driver (port 38899, zero cloud)."""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any

from .base import LightDriver

logger = logging.getLogger(__name__)

WIZ_PORT = 38899
WIZ_MIN_TEMP_K = 2200
WIZ_MAX_TEMP_K = 6500
WIZ_MIN_DIMMING_PCT = 10
WIZ_MAX_DIMMING_PCT = 100


def build_set_pilot_payload(brightness_pct: int, cct_k: int) -> dict[str, Any]:
    """Construct an official WiZ local UDP `setPilot` JSON payload.

    WiZ firmware requires dimming in [10, 100] when on, and `state: False`
    when turning completely off (brightness_pct <= 0).
    """
    pct = int(brightness_pct)
    if pct <= 0:
        return {
            "method": "setPilot",
            "params": {"state": False},
        }

    dimming = max(WIZ_MIN_DIMMING_PCT, min(WIZ_MAX_DIMMING_PCT, pct))
    temp = max(WIZ_MIN_TEMP_K, min(WIZ_MAX_TEMP_K, int(cct_k)))
    return {
        "method": "setPilot",
        "params": {
            "state": True,
            "temp": temp,
            "dimming": dimming,
        },
    }


def discover_wiz_bulbs(
    broadcast_ip: str = "255.255.255.255",
    port: int = WIZ_PORT,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
    """Broadcast a local UDP `getPilot` probe to discover WiZ bulbs on LAN."""
    probe = json.dumps({"method": "getPilot", "params": {}}, separators=(",", ":")).encode("utf-8")
    found: dict[str, dict[str, Any]] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.25)
        sock.sendto(probe, (broadcast_ip, port))

        deadline = time.monotonic() + max(0.25, timeout)
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            ip = addr[0]
            try:
                payload = json.loads(data.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue

            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            found[ip] = {
                "ip": ip,
                "mac": result.get("mac", ""),
                "state": result.get("state"),
                "temp": result.get("temp"),
                "dimming": result.get("dimming"),
            }
    finally:
        sock.close()

    return list(found.values())


class WizUdpDriver(LightDriver):
    """Controls a WiZ smart bulb locally over LAN UDP (port 38899)."""

    name = "wiz"

    def __init__(
        self,
        ip: str,
        port: int = WIZ_PORT,
        timeout: float = 1.5,
        dry_run: bool = False,
    ) -> None:
        if not ip:
            raise ValueError("WizUdpDriver requires a non-empty bulb IP address.")
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.dry_run = dry_run
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(timeout)

    def _send_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if self.dry_run:
            return {"sent": payload, "ack": None, "dry_run": True}

        ack: dict[str, Any] | None = None
        try:
            self._sock.sendto(raw, (self.ip, self.port))
            data, _ = self._sock.recvfrom(4096)
            ack = json.loads(data.decode("utf-8", errors="replace"))
        except socket.timeout:
            logger.warning("WiZ bulb %s:%d did not ACK within %.1fs.", self.ip, self.port, self.timeout)
        except OSError as exc:
            logger.warning("WiZ UDP send failed for %s:%d: %s", self.ip, self.port, exc)
        except json.JSONDecodeError:
            logger.debug("WiZ bulb %s returned non-JSON ACK.", self.ip)

        return {"sent": payload, "ack": ack, "dry_run": False}

    def get_state(self) -> dict[str, Any] | None:
        """Query current WiZ bulb state over LAN UDP `getPilot` for conflict detection."""
        if self.dry_run:
            return None

        probe = json.dumps({"method": "getPilot", "params": {}}, separators=(",", ":")).encode("utf-8")
        try:
            self._sock.sendto(probe, (self.ip, self.port))
            data, _ = self._sock.recvfrom(4096)
            payload = json.loads(data.decode("utf-8", errors="replace"))
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            if not isinstance(result, dict):
                return None
            power = bool(result.get("state", False))
            dimming = int(result.get("dimming", 0)) if power else 0
            temp = int(result.get("temp", 2700))
            return {
                "power": power,
                "brightness_pct": max(0, min(100, dimming)),
                "cct_k": max(WIZ_MIN_TEMP_K, min(WIZ_MAX_TEMP_K, temp)),
            }
        except (socket.timeout, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.debug("WiZ get_state failed for %s:%d: %s", self.ip, self.port, exc)
            return None

    def set_state(
        self,
        brightness_pct: int,
        cct_k: int,
        transition_ms: int = 1000,
    ) -> dict[str, Any]:
        # Note: WiZ UDP `setPilot` applies temp/dimming directly without a duration param.
        _ = transition_ms
        payload = build_set_pilot_payload(brightness_pct=brightness_pct, cct_k=cct_k)
        return self._send_payload(payload)

    def handoff_evening_scene(self) -> dict[str, Any]:
        """Leave WiZ bulb in a gentle, low-glare 2300K evening state before PC sleep."""
        return self.set_state(brightness_pct=12, cct_k=2300)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
