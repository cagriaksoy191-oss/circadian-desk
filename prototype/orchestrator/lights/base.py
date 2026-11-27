"""Abstract base contract for local-only smart-light drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LightDriver(ABC):
    """Base interface for local LAN light backends (WiZ, Hue, Mock)."""

    name: str = "base"

    @abstractmethod
    def set_state(
        self,
        brightness_pct: int,
        cct_k: int,
        transition_ms: int = 1000,
    ) -> dict[str, Any]:
        """Apply brightness (0..100, where 0 turns the light off) and CCT (Kelvin)."""

    def handoff_evening_scene(self) -> dict[str, Any]:
        """Push a safe, low-glare local evening scene before OS sleep or shutdown.

        Prevents overhead bulbs from staying frozen at daytime 5200K when a
        laptop lid closes or the host PC enters sleep during evening hours.
        """
        return self.set_state(brightness_pct=12, cct_k=2300, transition_ms=3000)

    def close(self) -> None:
        """Release any open sockets or handles."""
        return None
