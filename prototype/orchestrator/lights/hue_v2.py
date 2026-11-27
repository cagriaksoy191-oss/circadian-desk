"""Local-only Philips Hue CLIP API v2 driver (HTTPS LAN, zero cloud)."""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any

from .base import LightDriver

logger = logging.getLogger(__name__)

HUE_MIN_MIRED = 153  # ~6535 K
HUE_MAX_MIRED = 500  # 2000 K


def kelvin_to_mired(cct_k: int) -> int:
    """Convert color temperature in Kelvin to Hue CLIP v2 mirek (clamped 153..500)."""
    safe_k = max(1000, int(cct_k))
    mired = round(1_000_000 / safe_k)
    return max(HUE_MIN_MIRED, min(HUE_MAX_MIRED, mired))


def build_hue_v2_payload(
    brightness_pct: int,
    cct_k: int,
    transition_ms: int = 1000,
) -> dict[str, Any]:
    """Build a Hue CLIP API v2 `/resource/grouped_light/{id}` PUT body."""
    duration = max(0, int(transition_ms))
    pct = int(brightness_pct)
    if pct <= 0:
        return {
            "on": {"on": False},
            "dynamics": {"duration": duration},
        }

    dimming = float(max(1, min(100, pct)))
    return {
        "on": {"on": True},
        "dimming": {"brightness": dimming},
        "color_temperature": {"mirek": kelvin_to_mired(cct_k)},
        "dynamics": {"duration": duration},
    }


class HueV2Driver(LightDriver):
    """Controls a Philips Hue room/zone locally via Hue Bridge CLIP API v2."""

    name = "hue"

    def __init__(
        self,
        bridge_ip: str,
        app_key: str,
        grouped_light_id: str,
        timeout: float = 3.0,
        verify_ssl: bool = False,
        dry_run: bool = False,
    ) -> None:
        if not bridge_ip or not app_key or not grouped_light_id:
            raise ValueError(
                "HueV2Driver requires bridge_ip, app_key, and grouped_light_id."
            )
        self.bridge_ip = bridge_ip.strip()
        self.app_key = app_key.strip()
        self.grouped_light_id = grouped_light_id.strip()
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.dry_run = dry_run

        self._ssl_ctx: ssl.SSLContext | None = None
        if not verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx

    @property
    def endpoint_url(self) -> str:
        return (
            f"https://{self.bridge_ip}/clip/v2/resource/"
            f"grouped_light/{self.grouped_light_id}"
        )

    def get_state(self) -> dict[str, Any] | None:
        """Query current Hue CLIP v2 grouped_light state for external conflict detection."""
        if self.dry_run:
            return None

        req = urllib.request.Request(
            url=self.endpoint_url,
            method="GET",
            headers={"hue-application-key": self.app_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                payload = json.loads(raw) if raw else {}
            data = payload.get("data", []) if isinstance(payload, dict) else []
            if not isinstance(data, list) or not data or not isinstance(data[0], dict):
                return None
            item = data[0]
            power = bool(item.get("on", {}).get("on", False))
            brightness = int(round(float(item.get("dimming", {}).get("brightness", 0.0)))) if power else 0
            mirek_raw = item.get("color_temperature", {}).get("mirek")
            if isinstance(mirek_raw, (int, float)) and mirek_raw > 0:
                cct_k = int(round(1_000_000 / float(mirek_raw)))
            else:
                cct_k = 2700
            return {
                "power": power,
                "brightness_pct": max(0, min(100, brightness)),
                "cct_k": max(2000, min(6500, cct_k)),
            }
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            logger.debug("Hue get_state failed at %s: %s", self.endpoint_url, exc)
            return None

    def set_state(
        self,
        brightness_pct: int,
        cct_k: int,
        transition_ms: int = 1000,
    ) -> dict[str, Any]:
        payload = build_hue_v2_payload(
            brightness_pct=brightness_pct,
            cct_k=cct_k,
            transition_ms=transition_ms,
        )
        if self.dry_run:
            return {"url": self.endpoint_url, "sent": payload, "response": None, "dry_run": True}

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=self.endpoint_url,
            data=body,
            method="PUT",
            headers={
                "hue-application-key": self.app_key,
                "Content-Type": "application/json",
            },
        )
        response_data: dict[str, Any] | None = None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                response_data = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            logger.warning("Hue Bridge HTTP %d at %s: %s", exc.code, self.endpoint_url, exc.reason)
        except urllib.error.URLError as exc:
            logger.warning("Hue Bridge unreachable at %s: %s", self.endpoint_url, exc.reason)
        except json.JSONDecodeError:
            logger.debug("Hue Bridge returned non-JSON response.")

        return {
            "url": self.endpoint_url,
            "sent": payload,
            "response": response_data,
            "dry_run": False,
        }

    def handoff_evening_scene(self) -> dict[str, Any]:
        """Smoothly fade Hue group to 10% @ 2300K (435 mirek) over 5 seconds before sleep."""
        return self.set_state(brightness_pct=10, cct_k=2300, transition_ms=5000)
