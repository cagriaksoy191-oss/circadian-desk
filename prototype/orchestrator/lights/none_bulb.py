"""No-op smart-light driver for display-only Windows laptop & monitor beta users.

Allows users without Philips Hue or WiZ bulbs to run Week-1 Open-Loop Desktop Beta
purely on their Windows display (laptop WMI backlight / external DDC/CI + zero-admin
Magnification.dll / GDI GPU color transform) with zero network traffic.
"""

from __future__ import annotations

from typing import Any

from .base import LightDriver


class NoneLightDriver(LightDriver):
    """Display-only null light backend (`--light none`)."""

    name: str = "none"

    def set_state(
        self,
        brightness_pct: int,
        cct_k: int,
        transition_ms: int = 1000,
    ) -> Any:
        """Accept lighting commands as a clean no-op and return `True`."""
        return True

    def get_state(self) -> dict[str, Any] | None:
        """No external bulb exists; return `None` so Conflict-Observer never triggers."""
        return None

    def handoff_evening_scene(self) -> Any:
        """No physical bulb to hand off on sleep/exit; return `True`."""
        return True
