"""Display brightness/color control and OS power-event handoff utilities."""

from .windows_display import (
    SleepHandoffGuard,
    WindowsDisplayController,
    build_gamma_ramp,
    kelvin_to_rgb_scalars,
)

__all__ = [
    "WindowsDisplayController",
    "SleepHandoffGuard",
    "kelvin_to_rgb_scalars",
    "build_gamma_ramp",
]
