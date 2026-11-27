"""Dry-run mock light driver for safe Day-1 testing and automated tests."""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import LightDriver

logger = logging.getLogger(__name__)


class MockBulbDriver(LightDriver):
    """Records and logs lighting commands without touching network hardware."""

    name = "mock"

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.history: list[dict[str, Any]] = []
        self.last_state: dict[str, Any] | None = None
        self._external_state: dict[str, Any] | None = None

    def set_state(
        self,
        brightness_pct: int,
        cct_k: int,
        transition_ms: int = 1000,
    ) -> dict[str, Any]:
        clamped_pct = max(0, min(100, int(brightness_pct)))
        clamped_cct = max(1800, min(6500, int(cct_k)))
        record: dict[str, Any] = {
            "timestamp": time.time(),
            "state": clamped_pct > 0,
            "brightness_pct": clamped_pct,
            "cct_k": clamped_cct,
            "transition_ms": max(0, int(transition_ms)),
        }
        self.history.append(record)
        self.last_state = record
        self._external_state = {
            "power": record["state"],
            "brightness_pct": clamped_pct,
            "cct_k": clamped_cct,
        }
        if self.verbose:
            if clamped_pct == 0:
                logger.info("[MockBulb] OFF (transition=%dms)", record["transition_ms"])
            else:
                logger.info(
                    "[MockBulb] ON brightness=%d%% cct=%dK (transition=%dms)",
                    clamped_pct,
                    clamped_cct,
                    record["transition_ms"],
                )
        return record

    def simulate_external_change(
        self,
        power: bool,
        brightness_pct: int,
        cct_k: int,
    ) -> None:
        """Simulate a wall switch, Hue app, or Apple Adaptive Lighting override."""
        self._external_state = {
            "power": bool(power),
            "brightness_pct": max(0, min(100, int(brightness_pct))) if power else 0,
            "cct_k": max(1800, min(6500, int(cct_k))),
        }

    def get_state(self) -> dict[str, Any] | None:
        """Return current bulb state for external-controller conflict detection."""
        if self._external_state is None:
            return None
        return dict(self._external_state)
