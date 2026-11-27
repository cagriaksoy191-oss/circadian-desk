"""Local-only Braun-minimalist Web Control Panel (`127.0.0.1:8765`) for Circadian Desk.

100% Python standard library (`http.server.ThreadingHTTPServer`) — zero external
dependencies, zero cloud links, zero CDN/font requests, strictly loopback-bound with
Host/Origin CSRF & DNS-rebinding guards on all `POST` endpoints.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_REQUEST_BODY_BYTES = 65_536
MAX_PAUSE_MINUTES = 480
DEFAULT_MOVIE_MODE_DURATION_S = 9000.0  # 2.5 hours
MAX_MOVIE_MODE_DURATION_S = 28_800.0    # 8 hours cap
ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
VALID_PANEL_TYPES: tuple[str, ...] = ("auto", "wled_ips", "p3_ips", "woled", "qd_oled")
ALLOWED_SETTINGS_TARGETS: frozenset[str] = frozenset(
    {"nightlight", "colorfilter", "display", "startupapps"}
)
DEFAULT_APP_VERSION = "1.0.0"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_conflicts() -> dict[str, Any]:
    return {
        "night_light_on": False,
        "color_filter_on": False,
        "hdr_active": False,
        "acm_active": False,
        "competing_apps": [],
        "warnings": [],
    }


def _is_loopback_host_header(host_header: str | None) -> bool:
    """Validate that the HTTP `Host` header strictly targets `127.0.0.1` or `localhost`."""
    if not host_header:
        return False
    raw = host_header.strip().lower()
    if not raw or "/" in raw or "@" in raw or " " in raw:
        return False
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) != 2 or not parts[1].isdigit():
            return False
        hostname = parts[0]
    else:
        hostname = raw
    return hostname in ALLOWED_LOOPBACK_HOSTS


def _is_loopback_origin_header(origin_header: str | None) -> bool:
    """Validate optional `Origin` header on `POST` requests to block cross-site CSRF."""
    if origin_header is None or origin_header.strip() == "":
        return True
    raw = origin_header.strip()
    if raw.lower() == "null":
        return False
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    if parsed.scheme != "http":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname in ALLOWED_LOOPBACK_HOSTS


def _validate_weekly_schedule(raw: Any) -> dict[str, dict[str, float]]:
    if not isinstance(raw, dict):
        raise ValueError("weekly_schedule must be an object mapping weekday keys to {wake_hour, sleep_hour}")
    cleaned: dict[str, dict[str, float]] = {}
    for k, entry in raw.items():
        day = str(k or "").strip().lower()[:3]
        if day not in WEEKDAY_KEYS:
            raise ValueError(f"Invalid weekday key in weekly_schedule: {k!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"weekly_schedule[{day!r}] must be an object")
        w_raw = entry.get("wake_hour")
        s_raw = entry.get("sleep_hour")
        if (
            isinstance(w_raw, bool)
            or isinstance(s_raw, bool)
            or not isinstance(w_raw, (int, float))
            or not isinstance(s_raw, (int, float))
        ):
            raise ValueError(f"weekly_schedule[{day!r}] wake_hour/sleep_hour must be numbers")
        w_val = float(w_raw)
        s_val = float(s_raw)
        if not (math.isfinite(w_val) and math.isfinite(s_val)):
            raise ValueError("weekly_schedule hours must be finite")
        if not (0.0 <= w_val < 24.0) or not (0.0 <= s_val < 24.0):
            raise ValueError("weekly_schedule hours must be in [0.0, 24.0)")
        if abs(w_val - s_val) < 1e-6:
            raise ValueError(f"weekly_schedule[{day!r}] wake_hour and sleep_hour must differ")
        cleaned[day] = {"wake_hour": round(w_val, 2), "sleep_hour": round(s_val, 2)}
    return cleaned


class OrchestratorState:
    """Thread-safe shared state between the control loop and `LocalWebUIServer`."""

    def __init__(
        self,
        *,
        mode: str = "open_loop",
        light_backend: str = "none",
        wake_hour: float = 7.0,
        sleep_hour: float = 23.0,
        high_sensitivity_evening: bool = False,
        auto_pause_enabled: bool = True,
        auto_pause_browser_fullscreen: bool = False,
        display_max_nits: float = 0.0,
        detected_max_nits: float = 300.0,
        detected_panel_model: str = "",
        detected_nits_source: str = "estimated_sdr",
        comfort_mode: bool = True,
        backlight_gamma: float = 2.0,
        onboarding_completed: bool = True,
        awaiting_setup: bool = False,
        weekly_schedule: dict[str, dict[str, float]] | None = None,
        active_day_key: str = "",
        social_jetlag_hours: float = 0.0,
        start_with_windows: bool = False,
        live_conflicts: dict[str, Any] | None = None,
        capabilities: dict[str, Any] | None = None,
        panel_type: str = "auto",
        auto_panel_type: str = "wled_ips",
        diagonal_in: float = 0.0,
        solid_angle_sr: float = 0.35,
        dark_mode: bool | None = None,
        estimated_corneal_m_edi_lx: float = 0.0,
        hdr_auto_paused: bool = False,
        ddc_diagnostic: str = "",
        is_packaged_msix: bool = False,
        app_version: str = DEFAULT_APP_VERSION,
        csv_path: str | Path | None = None,
        on_settings_saved: Callable[[dict[str, Any]], None] | None = None,
        max_history: int = 60,
    ) -> None:
        self._lock = threading.Lock()
        self.wake_event: threading.Event = threading.Event()

        self._mode: str = str(mode)
        self._light_backend: str = str(light_backend or "none").lower()
        self._phase: str = "DAY"
        self._transition_label: str = "day"
        self._m_edi_lx: float = 0.0
        self._uncertainty_k2_lx: float = 0.0
        self._compliance_status: str = "OPEN-LOOP" if mode == "open_loop" else "BELIRSIZ"
        self._reading_valid: bool = True
        self._sensor_occluded: bool = False
        self._conflict_observer_active: bool = False
        self._unlinked_light_alert: bool = False
        self._alert_message: str = ""
        self._photopic_lx: float = 0.0
        self._cct_k: int = 5500
        self._m_der: float = 0.0
        self._flicker_hz: float = 0.0
        self._bulb_power: bool = False
        self._bulb_brightness_pct: int = 0
        self._bulb_cct_k: int = 2300
        self._monitor_brightness_pct: int = 80
        self._monitor_cct_k: int = 5500
        self._color_method: str = "none"
        self._wake_hour: float = float(wake_hour) % 24.0
        self._sleep_hour: float = float(sleep_hour) % 24.0
        self._high_sensitivity_evening: bool = bool(high_sensitivity_evening)
        self._auto_pause_enabled: bool = bool(auto_pause_enabled)
        self._auto_pause_browser_fullscreen: bool = bool(auto_pause_browser_fullscreen)
        self._auto_paused: bool = False
        self._auto_pause_reason: str = ""
        self._display_max_nits: float = float(display_max_nits)
        self._detected_max_nits: float = float(detected_max_nits)
        self._detected_panel_model: str = str(detected_panel_model or "")
        self._detected_nits_source: str = str(detected_nits_source or "estimated_sdr")
        self._target_screen_nits: float = 0.0
        self._comfort_mode: bool = bool(comfort_mode)
        self._backlight_gamma: float = float(backlight_gamma)
        self._manual_override_enabled: bool = False
        self._manual_brightness_pct: int = 70
        self._manual_cct_k: int = 4500
        self._movie_mode_until_ts: float = 0.0
        self._onboarding_completed: bool = bool(onboarding_completed)
        self._awaiting_setup: bool = bool(awaiting_setup) and (not self._onboarding_completed)
        self._weekly_schedule: dict[str, dict[str, float]] = (
            {k: dict(v) for k, v in weekly_schedule.items()} if weekly_schedule else {}
        )
        self._active_day_key: str = str(active_day_key or "")
        self._social_jetlag_hours: float = round(float(social_jetlag_hours), 2)
        self._start_with_windows: bool = bool(start_with_windows)
        self._live_conflicts: dict[str, Any] = (
            dict(live_conflicts) if live_conflicts is not None else _default_conflicts()
        )
        self._schedule_curve: list[dict[str, Any]] = []
        self._current_hour: float | None = None
        self._paused_until_epoch: float = 0.0
        self._capabilities: dict[str, Any] = dict(capabilities or {})
        pt_init = str(panel_type or "auto").strip().lower()
        self._panel_type: str = pt_init if pt_init in VALID_PANEL_TYPES else "auto"
        apt_init = str(auto_panel_type or "wled_ips").strip().lower()
        self._auto_panel_type: str = (
            apt_init if apt_init in VALID_PANEL_TYPES and apt_init != "auto" else "wled_ips"
        )
        self._diagonal_in: float = round(float(diagonal_in), 2)
        self._solid_angle_sr: float = round(float(solid_angle_sr), 4)
        self._dark_mode: bool | None = None if dark_mode is None else bool(dark_mode)
        self._estimated_corneal_m_edi_lx: float = round(float(estimated_corneal_m_edi_lx), 2)
        self._hdr_auto_paused: bool = bool(hdr_auto_paused) or bool(
            self._live_conflicts.get("hdr_active", False)
        )
        self._ddc_diagnostic: str = str(ddc_diagnostic or "")
        self._is_packaged_msix: bool = bool(is_packaged_msix)
        self._app_version: str = str(app_version or DEFAULT_APP_VERSION)
        self._csv_path: str = str(csv_path) if csv_path else ""
        self._updated_at_iso: str = ""
        self._on_settings_saved = on_settings_saved
        self._history: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_history)))

    @property
    def panel_type(self) -> str:
        with self._lock:
            return self._panel_type

    @property
    def csv_path(self) -> str:
        with self._lock:
            return self._csv_path

    @property
    def auto_pause_enabled(self) -> bool:
        with self._lock:
            return self._auto_pause_enabled

    @property
    def auto_pause_browser_fullscreen(self) -> bool:
        with self._lock:
            return self._auto_pause_browser_fullscreen

    @property
    def high_sensitivity_evening(self) -> bool:
        with self._lock:
            return self._high_sensitivity_evening

    @property
    def comfort_mode(self) -> bool:
        with self._lock:
            return self._comfort_mode

    @property
    def display_max_nits(self) -> float:
        with self._lock:
            return self._display_max_nits

    @property
    def effective_max_nits(self) -> float:
        with self._lock:
            return (
                self._display_max_nits
                if self._display_max_nits > 0.0
                else self._detected_max_nits
            )

    @property
    def onboarding_completed(self) -> bool:
        with self._lock:
            return self._onboarding_completed

    @property
    def awaiting_setup(self) -> bool:
        with self._lock:
            return self._awaiting_setup

    @property
    def start_with_windows(self) -> bool:
        with self._lock:
            return self._start_with_windows

    @property
    def weekly_schedule(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {k: dict(v) for k, v in self._weekly_schedule.items()}

    def set_csv_path(self, csv_path: str | Path | None) -> None:
        with self._lock:
            self._csv_path = str(csv_path) if csv_path else ""

    def get_settings(self) -> tuple[float, float, bool]:
        """Return `(wake_hour, sleep_hour, high_sensitivity_evening)` atomically."""
        with self._lock:
            return (
                self._wake_hour,
                self._sleep_hour,
                self._high_sensitivity_evening,
            )

    def get_manual_override(self) -> tuple[bool, int, int]:
        """Return `(enabled, brightness_pct, cct_k)` atomically."""
        with self._lock:
            return (
                self._manual_override_enabled,
                self._manual_brightness_pct,
                self._manual_cct_k,
            )

    def is_movie_mode(self, now: float | None = None) -> tuple[bool, int]:
        """Return `(active, remaining_s)` for Movie Mode (2.5h — 3400K); clears if expired."""
        cur = time.time() if now is None else float(now)
        with self._lock:
            if self._movie_mode_until_ts > cur:
                rem = max(1, int(math.ceil(self._movie_mode_until_ts - cur)))
                return True, rem
            if self._movie_mode_until_ts > 0.0:
                self._movie_mode_until_ts = 0.0
            return False, 0

    def is_movie_mode_active(self, now: float | None = None) -> bool:
        return self.is_movie_mode(now=now)[0]

    def activate_movie_mode(
        self,
        duration_s: float = DEFAULT_MOVIE_MODE_DURATION_S,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Activate Movie Mode (`3400K`) for `duration_s` seconds (default 2.5h)."""
        if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
            raise ValueError("duration_s must be a positive number")
        dur = float(duration_s)
        if not math.isfinite(dur) or dur <= 0.0 or dur > MAX_MOVIE_MODE_DURATION_S:
            raise ValueError(f"duration_s must be in (0, {int(MAX_MOVIE_MODE_DURATION_S)}]")
        cur = time.time() if now is None else float(now)
        with self._lock:
            self._movie_mode_until_ts = cur + dur
            until_ts = self._movie_mode_until_ts
        self.wake_event.set()
        return {
            "movie_mode_active": True,
            "movie_mode_remaining_s": int(math.ceil(dur)),
            "movie_mode_until_ts": until_ts,
        }

    def clear_movie_mode(self) -> dict[str, Any]:
        """Exit Movie Mode immediately and return to automatic circadian schedule."""
        with self._lock:
            self._movie_mode_until_ts = 0.0
        self.wake_event.set()
        return {
            "movie_mode_active": False,
            "movie_mode_remaining_s": 0,
            "movie_mode_until_ts": 0.0,
        }

    def toggle_movie_mode(
        self,
        duration_s: float = DEFAULT_MOVIE_MODE_DURATION_S,
        now: float | None = None,
    ) -> dict[str, Any]:
        if self.is_movie_mode_active(now=now):
            return self.clear_movie_mode()
        return self.activate_movie_mode(duration_s=duration_s, now=now)

    def set_movie_mode(self, minutes: int, now: float | None = None) -> dict[str, Any]:
        """Helper compatible with TrayActionController: 0 exits, >0 activates for `minutes`."""
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise ValueError("minutes must be an integer")
        if minutes <= 0:
            return self.clear_movie_mode()
        return self.activate_movie_mode(duration_s=float(minutes) * 60.0, now=now)

    def update_snapshot(
        self,
        *,
        mode: str | None = None,
        light_backend: str | None = None,
        phase: str | None = None,
        transition_label: str | None = None,
        m_edi_lx: float | None = None,
        uncertainty_k2_lx: float | None = None,
        compliance_status: str | None = None,
        reading_valid: bool | None = None,
        sensor_occluded: bool | None = None,
        conflict_observer_active: bool | None = None,
        unlinked_light_alert: bool | None = None,
        alert_message: str | None = None,
        photopic_lx: float | None = None,
        cct_k: int | float | None = None,
        m_der: float | None = None,
        flicker_hz: float | None = None,
        bulb_power: bool | None = None,
        bulb_brightness_pct: int | None = None,
        bulb_cct_k: int | None = None,
        monitor_brightness_pct: int | None = None,
        monitor_cct_k: int | None = None,
        color_method: str | None = None,
        auto_paused: bool | None = None,
        auto_pause_reason: str | None = None,
        awaiting_setup: bool | None = None,
        target_screen_nits: float | None = None,
        detected_max_nits: float | None = None,
        detected_panel_model: str | None = None,
        detected_nits_source: str | None = None,
        backlight_gamma: float | None = None,
        schedule_curve: list[dict[str, Any]] | None = None,
        current_hour: float | None = None,
        active_day_key: str | None = None,
        social_jetlag_hours: float | None = None,
        live_conflicts: dict[str, Any] | None = None,
        capabilities: dict[str, Any] | None = None,
        panel_type: str | None = None,
        auto_panel_type: str | None = None,
        diagonal_in: float | None = None,
        solid_angle_sr: float | None = None,
        dark_mode: bool | None = None,
        estimated_corneal_m_edi_lx: float | None = None,
        hdr_auto_paused: bool | None = None,
        ddc_diagnostic: str | None = None,
        is_packaged_msix: bool | None = None,
        app_version: str | None = None,
        csv_path: str | Path | None = None,
        updated_at_iso: str | None = None,
        append_history: bool = True,
    ) -> None:
        """Atomically publish the latest orchestrator tick snapshot and append sparkline point."""
        ts_iso = updated_at_iso or _utc_now_iso()
        with self._lock:
            if mode is not None:
                self._mode = str(mode)
            if light_backend is not None:
                self._light_backend = str(light_backend).lower()
            if phase is not None:
                self._phase = str(phase)
            if transition_label is not None:
                self._transition_label = str(transition_label)
            if m_edi_lx is not None:
                self._m_edi_lx = round(float(m_edi_lx), 2)
            if uncertainty_k2_lx is not None:
                self._uncertainty_k2_lx = round(float(uncertainty_k2_lx), 2)
            if compliance_status is not None:
                self._compliance_status = str(compliance_status)
            if reading_valid is not None:
                self._reading_valid = bool(reading_valid)
            if sensor_occluded is not None:
                self._sensor_occluded = bool(sensor_occluded)
            if conflict_observer_active is not None:
                self._conflict_observer_active = bool(conflict_observer_active)
            if unlinked_light_alert is not None:
                self._unlinked_light_alert = bool(unlinked_light_alert)
            if alert_message is not None:
                self._alert_message = str(alert_message)
            if photopic_lx is not None:
                self._photopic_lx = round(float(photopic_lx), 2)
            if cct_k is not None:
                self._cct_k = int(round(float(cct_k)))
            if m_der is not None:
                self._m_der = round(float(m_der), 3)
            if flicker_hz is not None:
                self._flicker_hz = round(float(flicker_hz), 1)
            if bulb_power is not None:
                self._bulb_power = bool(bulb_power)
            if bulb_brightness_pct is not None:
                self._bulb_brightness_pct = int(bulb_brightness_pct)
            if bulb_cct_k is not None:
                self._bulb_cct_k = int(bulb_cct_k)
            if monitor_brightness_pct is not None:
                self._monitor_brightness_pct = int(monitor_brightness_pct)
            if monitor_cct_k is not None:
                self._monitor_cct_k = int(monitor_cct_k)
            if color_method is not None:
                self._color_method = str(color_method)
            if auto_paused is not None:
                self._auto_paused = bool(auto_paused)
            if auto_pause_reason is not None:
                self._auto_pause_reason = str(auto_pause_reason)
            if awaiting_setup is not None:
                self._awaiting_setup = bool(awaiting_setup) and (not self._onboarding_completed)
            if target_screen_nits is not None:
                self._target_screen_nits = round(float(target_screen_nits), 1)
            if detected_max_nits is not None:
                self._detected_max_nits = round(float(detected_max_nits), 1)
            if detected_panel_model is not None:
                self._detected_panel_model = str(detected_panel_model)
            if detected_nits_source is not None:
                self._detected_nits_source = str(detected_nits_source)
            if backlight_gamma is not None:
                self._backlight_gamma = float(backlight_gamma)
            if schedule_curve is not None:
                self._schedule_curve = list(schedule_curve)
            if current_hour is not None:
                self._current_hour = round(float(current_hour) % 24.0, 2)
            if active_day_key is not None:
                self._active_day_key = str(active_day_key)
            if social_jetlag_hours is not None:
                self._social_jetlag_hours = round(float(social_jetlag_hours), 2)
            if live_conflicts is not None:
                self._live_conflicts = dict(live_conflicts)
                if self._capabilities is not None:
                    self._capabilities["windows_night_light_detected"] = bool(
                        live_conflicts.get("night_light_on", False)
                    )
                if hdr_auto_paused is None:
                    self._hdr_auto_paused = bool(live_conflicts.get("hdr_active", False))
            if capabilities is not None:
                self._capabilities = dict(capabilities)
                dp = capabilities.get("display_profile")
                if diagonal_in is None and isinstance(dp, dict) and dp.get("diagonal_in"):
                    try:
                        self._diagonal_in = round(float(dp["diagonal_in"]), 2)
                    except (TypeError, ValueError):
                        pass
            if panel_type is not None:
                pt_norm = str(panel_type).strip().lower()
                if pt_norm in VALID_PANEL_TYPES:
                    self._panel_type = pt_norm
            if auto_panel_type is not None:
                apt_norm = str(auto_panel_type).strip().lower()
                if apt_norm in VALID_PANEL_TYPES and apt_norm != "auto":
                    self._auto_panel_type = apt_norm
            if diagonal_in is not None:
                self._diagonal_in = round(float(diagonal_in), 2)
            if solid_angle_sr is not None:
                self._solid_angle_sr = round(float(solid_angle_sr), 4)
            if dark_mode is not None:
                self._dark_mode = bool(dark_mode)
            if estimated_corneal_m_edi_lx is not None:
                self._estimated_corneal_m_edi_lx = round(float(estimated_corneal_m_edi_lx), 2)
            if hdr_auto_paused is not None:
                self._hdr_auto_paused = bool(hdr_auto_paused)
            if ddc_diagnostic is not None:
                self._ddc_diagnostic = str(ddc_diagnostic)
            if is_packaged_msix is not None:
                self._is_packaged_msix = bool(is_packaged_msix)
            if app_version is not None:
                self._app_version = str(app_version)
            if csv_path is not None:
                self._csv_path = str(csv_path)
            self._updated_at_iso = ts_iso

            if append_history:
                self._history.append(
                    {
                        "timestamp_iso": ts_iso,
                        "m_edi_lx": self._m_edi_lx,
                        "phase": self._phase,
                        "compliance_status": self._compliance_status,
                    }
                )

    def set_pause(self, minutes: int, now: float | None = None) -> dict[str, Any]:
        """Pause automated control for `1..480` minutes, or pass `0` to resume immediately."""
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise ValueError("minutes must be an integer between 0 and 480")
        if minutes < 0 or minutes > MAX_PAUSE_MINUTES:
            raise ValueError(f"minutes must be between 0 and {MAX_PAUSE_MINUTES}")

        cur = time.time() if now is None else float(now)
        with self._lock:
            if minutes == 0:
                self._paused_until_epoch = 0.0
                paused = False
                remaining_sec = 0
            else:
                self._paused_until_epoch = cur + (minutes * 60.0)
                paused = True
                remaining_sec = minutes * 60
            paused_until = self._paused_until_epoch

        self.wake_event.set()
        return {
            "paused": paused,
            "remaining_sec": remaining_sec,
            "paused_until_epoch": paused_until,
        }

    def is_paused(self, now: float | None = None) -> tuple[bool, int]:
        """Return `(paused, remaining_sec)`; clears expired pause windows cleanly."""
        cur = time.time() if now is None else float(now)
        with self._lock:
            if self._paused_until_epoch > cur:
                rem = max(1, int(math.ceil(self._paused_until_epoch - cur)))
                return True, rem
            if self._paused_until_epoch > 0.0:
                self._paused_until_epoch = 0.0
            return False, 0

    def set_manual_override(
        self,
        enabled: bool,
        brightness_pct: int | None = None,
        cct_k: int | None = None,
    ) -> dict[str, Any]:
        """Enable or disable live manual Brightness (`10..100`) & Kelvin (`2200..6500`) sliders."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")

        parsed_bri: int | None = None
        parsed_cct: int | None = None

        if brightness_pct is not None:
            if isinstance(brightness_pct, bool) or not isinstance(brightness_pct, (int, float)):
                raise ValueError("brightness_pct must be an integer in [10, 100]")
            if not math.isfinite(float(brightness_pct)):
                raise ValueError("brightness_pct must be finite")
            b_val = int(round(float(brightness_pct)))
            if not (10 <= b_val <= 100):
                raise ValueError("brightness_pct must be between 10 and 100")
            parsed_bri = b_val

        if cct_k is not None:
            if isinstance(cct_k, bool) or not isinstance(cct_k, (int, float)):
                raise ValueError("cct_k must be an integer in [2200, 6500]")
            if not math.isfinite(float(cct_k)):
                raise ValueError("cct_k must be finite")
            c_val = int(round(float(cct_k)))
            if not (2200 <= c_val <= 6500):
                raise ValueError("cct_k must be between 2200 and 6500")
            parsed_cct = c_val

        with self._lock:
            self._manual_override_enabled = enabled
            if parsed_bri is not None:
                self._manual_brightness_pct = parsed_bri
            if parsed_cct is not None:
                self._manual_cct_k = parsed_cct
            result = {
                "enabled": self._manual_override_enabled,
                "brightness_pct": self._manual_brightness_pct,
                "cct_k": self._manual_cct_k,
            }

        self.wake_event.set()
        return result

    def update_settings(
        self,
        wake_hour: float | None = None,
        sleep_hour: float | None = None,
        high_sensitivity_evening: bool | None = None,
        auto_pause_enabled: bool | None = None,
        display_max_nits: float | None = None,
        comfort_mode: bool | None = None,
        weekly_schedule: dict[str, dict[str, float]] | None = None,
        onboarding_completed: bool | None = None,
        auto_pause_browser_fullscreen: bool | None = None,
        start_with_windows: bool | None = None,
        panel_type: str | None = None,
    ) -> dict[str, Any]:
        """Validate & apply live schedule/display settings; invoke `on_settings_saved` if configured."""
        parsed_wake: float | None = None
        parsed_sleep: float | None = None
        parsed_sens: bool | None = None
        parsed_auto_pause: bool | None = None
        parsed_max_nits: float | None = None
        parsed_comfort: bool | None = None
        parsed_weekly: dict[str, dict[str, float]] | None = None
        parsed_onboarding: bool | None = None
        parsed_browser_fs: bool | None = None
        parsed_autostart: bool | None = None
        parsed_panel_type: str | None = None

        if wake_hour is not None:
            if isinstance(wake_hour, bool) or not isinstance(wake_hour, (int, float)):
                raise ValueError("wake_hour must be a number in [0.0, 24.0)")
            val = float(wake_hour)
            if not math.isfinite(val) or not (0.0 <= val < 24.0):
                raise ValueError("wake_hour must be in [0.0, 24.0)")
            parsed_wake = round(val, 2)

        if sleep_hour is not None:
            if isinstance(sleep_hour, bool) or not isinstance(sleep_hour, (int, float)):
                raise ValueError("sleep_hour must be a number in [0.0, 24.0)")
            val = float(sleep_hour)
            if not math.isfinite(val) or not (0.0 <= val < 24.0):
                raise ValueError("sleep_hour must be in [0.0, 24.0)")
            parsed_sleep = round(val, 2)

        if high_sensitivity_evening is not None:
            if not isinstance(high_sensitivity_evening, bool):
                raise ValueError("high_sensitivity_evening must be a boolean")
            parsed_sens = high_sensitivity_evening

        if auto_pause_enabled is not None:
            if not isinstance(auto_pause_enabled, bool):
                raise ValueError("auto_pause_enabled must be a boolean")
            parsed_auto_pause = auto_pause_enabled

        if display_max_nits is not None:
            if isinstance(display_max_nits, bool) or not isinstance(display_max_nits, (int, float)):
                raise ValueError("display_max_nits must be 0 (auto) or in [150, 2000]")
            nval = float(display_max_nits)
            if not math.isfinite(nval) or not (nval == 0.0 or (150.0 <= nval <= 2000.0)):
                raise ValueError("display_max_nits must be 0 (auto) or between 150 and 2000")
            parsed_max_nits = round(nval, 1)

        if comfort_mode is not None:
            if not isinstance(comfort_mode, bool):
                raise ValueError("comfort_mode must be a boolean")
            parsed_comfort = comfort_mode

        if weekly_schedule is not None:
            parsed_weekly = _validate_weekly_schedule(weekly_schedule)

        if onboarding_completed is not None:
            if not isinstance(onboarding_completed, bool):
                raise ValueError("onboarding_completed must be a boolean")
            parsed_onboarding = onboarding_completed

        if auto_pause_browser_fullscreen is not None:
            if not isinstance(auto_pause_browser_fullscreen, bool):
                raise ValueError("auto_pause_browser_fullscreen must be a boolean")
            parsed_browser_fs = auto_pause_browser_fullscreen

        if start_with_windows is not None:
            if not isinstance(start_with_windows, bool):
                raise ValueError("start_with_windows must be a boolean")
            parsed_autostart = start_with_windows

        if panel_type is not None:
            if not isinstance(panel_type, str):
                raise ValueError(
                    f"panel_type must be one of: {', '.join(VALID_PANEL_TYPES)}"
                )
            pt_norm = panel_type.strip().lower()
            if pt_norm not in VALID_PANEL_TYPES:
                raise ValueError(
                    f"Invalid panel_type {panel_type!r}; must be one of: {', '.join(VALID_PANEL_TYPES)}"
                )
            parsed_panel_type = pt_norm

        if all(
            x is None
            for x in (
                parsed_wake,
                parsed_sleep,
                parsed_sens,
                parsed_auto_pause,
                parsed_max_nits,
                parsed_comfort,
                parsed_weekly,
                parsed_onboarding,
                parsed_browser_fs,
                parsed_autostart,
                parsed_panel_type,
            )
        ):
            raise ValueError("At least one setting field must be provided")

        with self._lock:
            next_wake = self._wake_hour if parsed_wake is None else parsed_wake
            next_sleep = self._sleep_hour if parsed_sleep is None else parsed_sleep
            if abs(next_wake - next_sleep) < 1e-6:
                raise ValueError("wake_hour and sleep_hour must differ")

            self._wake_hour = next_wake
            self._sleep_hour = next_sleep
            if parsed_sens is not None:
                self._high_sensitivity_evening = parsed_sens
            if parsed_auto_pause is not None:
                self._auto_pause_enabled = parsed_auto_pause
                if not parsed_auto_pause:
                    self._auto_paused = False
                    self._auto_pause_reason = ""
            if parsed_max_nits is not None:
                self._display_max_nits = parsed_max_nits
            if parsed_comfort is not None:
                self._comfort_mode = parsed_comfort
            if parsed_weekly is not None:
                self._weekly_schedule = parsed_weekly
            if parsed_onboarding is not None:
                self._onboarding_completed = parsed_onboarding
                if parsed_onboarding:
                    self._awaiting_setup = False
            if parsed_browser_fs is not None:
                self._auto_pause_browser_fullscreen = parsed_browser_fs
            if parsed_autostart is not None:
                self._start_with_windows = parsed_autostart
            if parsed_panel_type is not None:
                self._panel_type = parsed_panel_type

            result = {
                "wake_hour": self._wake_hour,
                "sleep_hour": self._sleep_hour,
                "high_sensitivity_evening": self._high_sensitivity_evening,
                "auto_pause_enabled": self._auto_pause_enabled,
                "auto_pause_browser_fullscreen": self._auto_pause_browser_fullscreen,
                "display_max_nits": self._display_max_nits,
                "comfort_mode": self._comfort_mode,
                "weekly_schedule": {k: dict(v) for k, v in self._weekly_schedule.items()},
                "onboarding_completed": self._onboarding_completed,
                "start_with_windows": self._start_with_windows,
                "panel_type": self._panel_type,
            }
            callback = self._on_settings_saved

        self.wake_event.set()
        if callback is not None:
            try:
                callback(dict(result))
            except Exception as exc:
                logger.warning("Settings persistence callback failed: %s", exc)

        return result

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of all live telemetry, settings, and curves."""
        cur = time.time() if now is None else float(now)
        with self._lock:
            if self._paused_until_epoch > cur:
                paused = True
                pause_remaining_sec = max(1, int(math.ceil(self._paused_until_epoch - cur)))
            else:
                paused = False
                pause_remaining_sec = 0
                self._paused_until_epoch = 0.0

            if self._movie_mode_until_ts > cur:
                movie_active = True
                movie_rem_s = max(1, int(math.ceil(self._movie_mode_until_ts - cur)))
            else:
                movie_active = False
                movie_rem_s = 0
                self._movie_mode_until_ts = 0.0

            eff_nits = (
                self._display_max_nits
                if self._display_max_nits > 0.0
                else self._detected_max_nits
            )
            diag_val = self._diagonal_in
            if diag_val <= 0.0:
                dp = self._capabilities.get("display_profile")
                if isinstance(dp, dict) and dp.get("diagonal_in"):
                    try:
                        diag_val = round(float(dp["diagonal_in"]), 2)
                    except (TypeError, ValueError):
                        diag_val = 0.0
            hdr_paused = bool(
                self._hdr_auto_paused or self._live_conflicts.get("hdr_active", False)
            )
            curve_copy = list(self._schedule_curve)

            return {
                "mode": self._mode,
                "light_backend": self._light_backend,
                "phase": self._phase,
                "transition_label": self._transition_label,
                "m_edi_lx": self._m_edi_lx,
                "uncertainty_k2_lx": self._uncertainty_k2_lx,
                "compliance_status": self._compliance_status,
                "reading_valid": self._reading_valid,
                "sensor_occluded": self._sensor_occluded,
                "conflict_observer_active": self._conflict_observer_active,
                "unlinked_light_alert": self._unlinked_light_alert,
                "alert_message": self._alert_message,
                "photopic_lx": self._photopic_lx,
                "cct_k": self._cct_k,
                "m_der": self._m_der,
                "flicker_hz": self._flicker_hz,
                "bulb_power": self._bulb_power,
                "bulb_brightness_pct": self._bulb_brightness_pct,
                "bulb_cct_k": self._bulb_cct_k,
                "monitor_brightness_pct": self._monitor_brightness_pct,
                "monitor_cct_k": self._monitor_cct_k,
                "color_method": self._color_method,
                "wake_hour": self._wake_hour,
                "sleep_hour": self._sleep_hour,
                "high_sensitivity_evening": self._high_sensitivity_evening,
                "auto_pause_enabled": self._auto_pause_enabled,
                "auto_pause_browser_fullscreen": self._auto_pause_browser_fullscreen,
                "auto_paused": self._auto_paused,
                "auto_pause_reason": self._auto_pause_reason,
                "display_max_nits": self._display_max_nits,
                "effective_max_nits": round(eff_nits, 1),
                "detected_max_nits": self._detected_max_nits,
                "detected_panel_model": self._detected_panel_model,
                "detected_nits_source": self._detected_nits_source,
                "target_screen_nits": self._target_screen_nits,
                "target_display_nits": self._target_screen_nits,
                "comfort_mode": self._comfort_mode,
                "backlight_gamma": self._backlight_gamma,
                "manual_override_enabled": self._manual_override_enabled,
                "manual_brightness_pct": self._manual_brightness_pct,
                "manual_cct_k": self._manual_cct_k,
                "movie_mode_active": movie_active,
                "movie_mode_remaining_s": movie_rem_s,
                "movie_mode_until_ts": self._movie_mode_until_ts,
                "onboarding_completed": self._onboarding_completed,
                "awaiting_setup": self._awaiting_setup,
                "weekly_schedule": {k: dict(v) for k, v in self._weekly_schedule.items()},
                "active_day_key": self._active_day_key,
                "social_jetlag_hours": self._social_jetlag_hours,
                "start_with_windows": self._start_with_windows,
                "live_conflicts": dict(self._live_conflicts),
                "panel_type": self._panel_type,
                "auto_panel_type": self._auto_panel_type,
                "diagonal_in": diag_val,
                "solid_angle_sr": self._solid_angle_sr,
                "dark_mode": self._dark_mode,
                "estimated_corneal_m_edi_lx": self._estimated_corneal_m_edi_lx,
                "hdr_auto_paused": hdr_paused,
                "ddc_diagnostic": self._ddc_diagnostic,
                "is_packaged_msix": self._is_packaged_msix,
                "app_version": self._app_version,
                "schedule_curve": curve_copy,
                "curve_24h": curve_copy,
                "current_hour": self._current_hour,
                "paused": paused,
                "pause_remaining_sec": pause_remaining_sec,
                "paused_until_epoch": self._paused_until_epoch,
                "capabilities": dict(self._capabilities),
                "csv_path": self._csv_path,
                "updated_at_iso": self._updated_at_iso,
                "history": list(self._history),
            }

    def get_snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Alias for `to_dict()` returning the complete live state snapshot."""
        return self.to_dict(now=now)


# 100% offline Braun/Dieter Rams bento cockpit (ZERO external URLs, ZERO <input type="time">, ZERO <select>).
_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Circadian Desk — Local Control Panel</title>
<style>
  :root {
    --bg: #F6F4EF;
    --card: #FFFFFF;
    --well: #FAF9F5;
    --border: #E6E2D8;
    --border-strong: #CFC9BC;
    --ink: #161513;
    --muted: #666159;
    --amber: #D97706;
    --amber-bg: #FEF3C7;
    --amber-ink: #8A4F0A;
    --blue: #3B7CA5;
    --blue-bg: #EAF3F9;
    --green: #1E7A4C;
    --green-bg: #E7F4EC;
    --red: #C84B31;
    --red-bg: #FCECE8;
    --radius: 10px;
    --mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
    line-height: 1.4;
    -webkit-font-smoothing: antialiased;
  }

  /* ---------- Full-Viewport Dedicated Onboarding Gate ---------- */
  .onboarding-gate {
    display: none;
    min-height: 100vh;
    width: 100%;
    padding: 18px 20px;
    align-items: center;
    justify-content: center;
    background: var(--bg);
  }
  .onboarding-gate.show { display: flex; }
  .onboarding-card {
    width: 100%;
    max-width: 780px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 24px 28px 22px;
    box-shadow: 0 14px 36px rgba(22, 21, 19, 0.08);
  }
  .ob-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    padding: 5px 11px;
    border-radius: 999px;
    background: var(--amber-bg);
    color: var(--amber-ink);
    border: 1px solid #F2D08B;
    margin-bottom: 12px;
  }
  .ob-badge.edit-mode {
    background: var(--well);
    color: var(--muted);
    border-color: var(--border);
  }
  .ob-pulse {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--amber);
    box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.22);
  }
  .ob-badge.edit-mode .ob-pulse { background: var(--green); box-shadow: none; }
  .ob-head h2 {
    margin: 0 0 6px;
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--ink);
  }
  .ob-head p {
    margin: 0 0 16px;
    font-size: 13px;
    color: var(--muted);
  }

  /* Segmented 3-mode tabs (reused in Onboarding & Dashboard) */
  .seg-tabs {
    display: inline-flex;
    background: var(--well);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 2px;
    gap: 2px;
  }
  .seg-btn {
    border: none;
    background: transparent;
    color: var(--muted);
    font-family: inherit;
    font-size: 11.5px;
    font-weight: 600;
    padding: 5px 11px;
    border-radius: 6px;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.12s, color 0.12s;
  }
  .seg-btn:hover { color: var(--ink); }
  .seg-btn.active {
    background: var(--card);
    color: var(--ink);
    box-shadow: 0 1px 2px rgba(22, 21, 19, 0.08);
  }
  .seg-tabs.compact .seg-btn { font-size: 11px; padding: 4px 8px; }

  .ob-pane {
    margin-top: 14px;
    background: var(--well);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
  }
  .ob-uniform-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }
  .ob-time-pair {
    display: flex;
    align-items: center;
    gap: 18px;
    flex-wrap: wrap;
  }
  .ob-field { display: flex; flex-direction: column; gap: 5px; }
  .ob-field-lbl {
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .dur-chip {
    display: inline-flex;
    align-items: center;
    padding: 6px 12px;
    border-radius: 999px;
    background: var(--card);
    border: 1px solid var(--border);
    font-size: 12px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
  }
  .weekend-cards {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .wk-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 12px 14px;
  }
  .wk-card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
  }
  .wk-card-title { font-size: 12px; font-weight: 700; color: var(--ink); }
  .wk-card-dur { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .wk-pill-row { display: flex; align-items: center; gap: 10px; }
  .wk-pill-grp { display: flex; flex-direction: column; gap: 3px; flex: 1; }
  .wk-pill-grp span { font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); font-weight: 600; }

  .custom-7-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 7px 12px;
  }
  .day-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 7px;
    padding: 5px 9px;
  }
  .day-code {
    width: 30px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--ink);
  }
  .day-pills { display: flex; align-items: center; gap: 5px; }
  .day-arrow { color: var(--muted); font-size: 11px; }
  .copy-slot {
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .copy-mon-btn {
    width: 100%;
    height: 100%;
    min-height: 32px;
    border: 1px dashed var(--border-strong);
    background: var(--card);
    color: var(--ink);
    border-radius: 7px;
    font-family: inherit;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    transition: border-color 0.12s, background 0.12s;
  }
  .copy-mon-btn:hover { border-color: var(--amber); background: #FFFBF2; color: var(--amber-ink); }

  .ob-preview-strip {
    margin-top: 14px;
    padding: 10px 14px;
    border-radius: 9px;
    background: var(--well);
    border: 1px solid var(--border);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-size: 12px;
  }
  .ob-preview-panel {
    font-weight: 600;
    color: var(--ink);
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }
  .ob-preview-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--blue); }
  .ob-preview-now {
    font-family: var(--mono);
    font-size: 11.5px;
    font-weight: 600;
    color: var(--amber-ink);
    background: var(--amber-bg);
    padding: 3px 9px;
    border-radius: 999px;
  }

  .ob-footer {
    margin-top: 18px;
    padding-top: 14px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .ob-actions { display: flex; align-items: center; gap: 10px; }

  /* ---------- Zero-Chevron Single-Click Time Pill & Popover ---------- */
  .time-pill {
    appearance: none;
    -webkit-appearance: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.02em;
    color: var(--ink);
    background: var(--card);
    border: 1px solid var(--border-strong);
    border-radius: 7px;
    padding: 6px 12px;
    min-width: 72px;
    cursor: pointer;
    transition: border-color 0.12s, background 0.12s, box-shadow 0.12s;
  }
  .time-pill.lg { font-size: 17px; padding: 8px 16px; min-width: 88px; border-radius: 8px; }
  .time-pill.sm { font-size: 12px; padding: 4px 8px; min-width: 54px; border-radius: 6px; }
  .time-pill:hover { border-color: var(--amber); background: #FFFDF8; }
  .time-pill:focus { outline: none; border-color: var(--amber); box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.18); }
  .time-pill.open { border-color: var(--ink); background: #161513; color: #FFFFFF; }

  .time-popover {
    display: none;
    position: fixed;
    z-index: 9999;
    width: 332px;
    background: var(--card);
    border: 1px solid var(--border-strong);
    border-radius: 12px;
    box-shadow: 0 16px 40px rgba(22, 21, 19, 0.18), 0 2px 6px rgba(22, 21, 19, 0.06);
    padding: 12px;
    user-select: none;
  }
  .time-popover.show { display: block; }
  .tp-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 8px;
    margin-bottom: 9px;
    border-bottom: 1px solid var(--border);
  }
  .tp-title { font-size: 10.5px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
  .tp-head-r { display: flex; align-items: center; gap: 8px; }
  .tp-live {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
    color: var(--ink);
    background: var(--well);
    border: 1px solid var(--border);
    padding: 2px 8px;
    border-radius: 5px;
  }
  .tp-done {
    border: 1px solid var(--ink);
    background: var(--ink);
    color: #FFFFFF;
    font-family: inherit;
    font-size: 11px;
    font-weight: 600;
    padding: 3px 9px;
    border-radius: 5px;
    cursor: pointer;
  }
  .tp-done:hover { background: #33312D; }
  .tp-body { display: grid; grid-template-columns: 1.32fr 1fr; gap: 10px; }
  .tp-col-lbl {
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .tp-hour-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 3px; }
  .tp-min-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 3px; }
  .tp-cell {
    appearance: none;
    border: 1px solid transparent;
    background: var(--well);
    color: var(--ink);
    font-family: var(--mono);
    font-variant-numeric: tabular-nums;
    font-size: 11.5px;
    font-weight: 600;
    padding: 5px 0;
    border-radius: 5px;
    cursor: pointer;
    text-align: center;
    transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .tp-cell:hover { background: #EFECE4; border-color: var(--border-strong); }
  .tp-cell.active { background: var(--ink); color: #FFFFFF; border-color: var(--ink); }
  .tp-cell.min-active { background: var(--amber); color: #FFFFFF; border-color: var(--amber); }
  .tp-legacy {
    margin-top: 6px;
    font-size: 10.5px;
    color: var(--amber-ink);
    background: var(--amber-bg);
    border-radius: 5px;
    padding: 3px 6px;
    text-align: center;
    font-family: var(--mono);
    display: none;
  }
  .tp-legacy.show { display: block; }
  .tp-hint {
    margin-top: 8px;
    padding-top: 6px;
    border-top: 1px solid var(--border);
    font-size: 10px;
    color: var(--muted);
    text-align: center;
  }

  /* ---------- Braun Tactile Toggle Switch ---------- */
  .switch-wrap {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    cursor: pointer;
    user-select: none;
  }
  .switch-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    user-select: none;
  }
  .switch-row:first-of-type { padding-top: 2px; }
  .switch-row:last-of-type { border-bottom: none; padding-bottom: 0; }
  .switch-text { min-width: 0; }
  .switch-title { font-size: 12.5px; font-weight: 600; color: var(--ink); line-height: 1.25; }
  .switch-sub { font-size: 11px; color: var(--muted); line-height: 1.25; margin-top: 1px; }
  .switch {
    position: relative;
    flex-shrink: 0;
    width: 38px;
    height: 22px;
    display: inline-block;
  }
  .switch input {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }
  .switch-track {
    position: absolute;
    inset: 0;
    border-radius: 999px;
    background: #DDD8CC;
    border: 1px solid #C9C3B6;
    transition: background 0.16s, border-color 0.16s;
  }
  .switch-track::after {
    content: "";
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: #FFFFFF;
    box-shadow: 0 1px 3px rgba(22, 21, 19, 0.25);
    transition: transform 0.16s;
  }
  .switch input:checked + .switch-track {
    background: var(--amber);
    border-color: #B86104;
  }
  .switch input:checked + .switch-track::after {
    transform: translateX(16px);
  }
  .switch input:focus-visible + .switch-track {
    box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.25);
  }

  /* ---------- Zero-Scroll Widescreen Bento Cockpit ---------- */
  .cockpit-shell {
    max-width: 1320px;
    width: min(1320px, 96vw);
    margin: 0 auto;
    padding: 12px 0 10px;
  }
  .cockpit-shell.hidden-for-gate { display: none !important; }

  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 40px;
    padding-bottom: 10px;
    margin-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .brand-row { display: flex; align-items: center; gap: 9px; min-width: 0; }
  .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 0 3px rgba(30, 122, 76, 0.16);
    flex-shrink: 0;
  }
  .dot.paused { background: var(--amber); box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.2); }
  .dot.alert { background: var(--red); box-shadow: 0 0 0 3px rgba(200, 75, 49, 0.2); }
  .brand-title {
    margin: 0;
    font-size: 13.5px;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .brand-sub {
    font-size: 11.5px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .hdr-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--ink);
    white-space: nowrap;
  }
  .pill-amber { background: var(--amber-bg); border-color: #F2D08B; color: var(--amber-ink); }
  .pill-hdr { background: var(--amber-bg); border-color: #ECC985; color: var(--amber-ink); text-transform: none; letter-spacing: 0.01em; }
  .source-pill {
    font-size: 10px;
    font-weight: 600;
    color: var(--muted);
    background: var(--well);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 2px 8px;
    white-space: nowrap;
  }
  .corneal-hint {
    font-size: 11px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    margin-top: 2px;
    min-height: 0;
  }
  .corneal-hint:empty { display: none; }
  .banner-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    width: 100%;
    flex-wrap: wrap;
  }
  .banner-act {
    border: 1px solid #C68B2C;
    background: #FFFFFF;
    color: #744207;
    border-radius: 6px;
    padding: 3px 9px;
    font-family: inherit;
    font-size: 11px;
    font-weight: 700;
    cursor: pointer;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .banner-act:hover { background: #744207; color: #FFFFFF; border-color: #744207; }
  #obConflictsBox { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
  #obConflictsBox:empty { display: none; margin: 0; }
  .top-btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 5px 11px;
    border-radius: 7px;
    border: 1px solid var(--border-strong);
    background: var(--card);
    color: var(--ink);
    font-family: inherit;
    font-size: 11.5px;
    font-weight: 600;
    text-decoration: none;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  .top-btn:hover { background: var(--ink); border-color: var(--ink); color: #FFFFFF; }

  /* Conditional Banners (0px when inactive!) */
  .banner {
    display: none;
    padding: 8px 13px;
    border-radius: 8px;
    margin-bottom: 10px;
    font-size: 12px;
    font-weight: 500;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .banner.show { display: flex; }
  .banner-red { background: var(--red-bg); border: 1px solid #E5A092; color: #7C2211; }
  .banner-amber { background: var(--amber-bg); border: 1px solid #ECC985; color: #744207; }
  #conflictsContainer { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
  #conflictsContainer:empty { display: none; margin: 0; }

  /* 2-Column Asymmetric Bento Grid */
  .bento-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.32fr) minmax(0, 1fr);
    gap: 14px;
    align-items: stretch;
  }
  .col-left, .col-right {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 15px 18px;
    box-shadow: 0 1px 2px rgba(22, 21, 19, 0.03);
  }
  .card-grow { flex: 1; display: flex; flex-direction: column; }

  .card-hdr {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 8px;
  }
  .kicker {
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .kicker strong { color: var(--ink); }

  /* Card L1: Hero Telemetry + 24h Kelvin-Gradient SVG Curve */
  .hero-row {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 14px;
    margin-bottom: 10px;
  }
  .metric-big {
    font-size: 34px;
    font-weight: 700;
    letter-spacing: -0.025em;
    line-height: 1.05;
    font-variant-numeric: tabular-nums;
  }
  .metric-unit {
    font-size: 13px;
    font-weight: 500;
    color: var(--muted);
    margin-left: 5px;
    font-variant-numeric: tabular-nums;
  }
  .badge {
    display: inline-block;
    padding: 3px 9px;
    border-radius: 6px;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .badge-green { background: var(--green-bg); color: var(--green); border: 1px solid #B7DEC6; }
  .badge-amber { background: var(--amber-bg); color: var(--amber-ink); border: 1px solid #F2D08B; }
  .badge-red { background: var(--red-bg); color: var(--red); border: 1px solid #E5A092; }
  .badge-neutral { background: var(--well); color: var(--ink); border: 1px solid var(--border); }

  .hw-strip { text-align: right; min-width: 0; }
  .hw-val {
    font-size: 15px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
  }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; justify-content: flex-end; }
  .chip {
    font-size: 10px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 5px;
    background: var(--well);
    border: 1px solid var(--border);
    color: var(--muted);
    white-space: nowrap;
  }
  .chip.active { background: var(--green-bg); border-color: #B7DEC6; color: var(--green); }
  .chip.warn { background: var(--amber-bg); border-color: #F2D08B; color: var(--amber-ink); }

  .curve-well {
    background: var(--well);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 9px 12px 6px;
  }
  .curve-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    color: var(--muted);
    margin-bottom: 4px;
    font-variant-numeric: tabular-nums;
  }
  .curve-head-right { font-weight: 600; color: var(--ink); }
  .curve-scrub-hint { font-size: 10px; color: var(--muted); font-weight: 500; margin-left: 5px; }
  svg#curveSvg, svg#sparkline { width: 100%; height: 145px; display: block; }
  svg#curveSvg { cursor: ew-resize; touch-action: none; user-select: none; }

  .telemetry-footer {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
  }
  .openloop-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
  }
  .nits-group { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .panel-badge {
    font-size: 11.5px;
    font-weight: 600;
    color: var(--ink);
    background: var(--well);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 3px 9px;
    font-variant-numeric: tabular-nums;
  }
  .nits-pills { display: inline-flex; gap: 4px; }
  .nits-btn {
    border: 1px solid var(--border-strong);
    background: var(--card);
    color: var(--ink);
    padding: 3px 8px;
    border-radius: 5px;
    font-family: inherit;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    font-variant-numeric: tabular-nums;
  }
  .nits-btn:hover { background: var(--well); }
  .nits-btn.active { background: var(--ink); border-color: var(--ink); color: #FFFFFF; }
  .panel-pills { display: inline-flex; gap: 3px; flex-wrap: wrap; }
  .panel-btn {
    border: 1px solid var(--border-strong);
    background: var(--card);
    color: var(--ink);
    padding: 2px 6px;
    border-radius: 5px;
    font-family: inherit;
    font-size: 10.5px;
    font-weight: 600;
    cursor: pointer;
  }
  .panel-btn:hover { background: var(--well); }
  .panel-btn.active { background: var(--ink); border-color: var(--ink); color: #FFFFFF; }
  .sensor-optional { font-size: 10.5px; color: var(--muted); white-space: nowrap; }

  .sensor-grid {
    display: none;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
  }
  .sensor-grid.show { display: grid; }
  .mini-k { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
  .mini-v { font-size: 14px; font-weight: 700; margin-top: 1px; font-variant-numeric: tabular-nums; }

  /* Card L2: Live Manual Sliders & Linked Smart Bulb */
  .slider-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
  }
  .slider-item { display: flex; flex-direction: column; gap: 5px; }
  .slider-top {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 11.5px;
  }
  .slider-lbl { color: var(--muted); font-weight: 600; }
  .slider-val { font-family: var(--mono); font-weight: 700; color: var(--ink); font-variant-numeric: tabular-nums; }
  input[type="range"] {
    width: 100%;
    height: 6px;
    accent-color: var(--amber);
    cursor: pointer;
    margin: 2px 0;
  }
  .bulb-strip {
    margin-top: 11px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .bulb-left { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
  .bulb-val { font-size: 13px; font-weight: 700; color: var(--ink); font-variant-numeric: tabular-nums; }

  /* Buttons */
  button.btn {
    border: 1px solid var(--ink);
    background: var(--ink);
    color: #FFFFFF;
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    padding: 7px 13px;
    border-radius: 7px;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  button.btn:hover { background: #2F2D29; }
  button.btn-amber { background: var(--amber); border-color: #B86104; color: #FFFFFF; }
  button.btn-amber:hover { background: #C26804; }
  button.btn-ghost { background: var(--card); border-color: var(--border-strong); color: var(--ink); }
  button.btn-ghost:hover { background: var(--well); border-color: var(--ink); }
  button.btn-sm { font-size: 11px; padding: 4px 10px; border-radius: 6px; }
  button.btn-link-subtle {
    border: none; background: transparent; color: var(--muted);
    font-family: inherit; font-size: 12px; font-weight: 600;
    padding: 6px 10px; cursor: pointer;
  }
  button.btn-link-subtle:hover { color: var(--ink); }

  /* Card R1: Quick Overrides & Sleep Schedule */
  .quick-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 7px;
  }
  .quick-btn {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 1px;
    padding: 7px 6px;
    border-radius: 8px;
    border: 1px solid var(--border-strong);
    background: var(--well);
    color: var(--ink);
    font-family: inherit;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  .quick-btn:hover { background: var(--card); border-color: var(--ink); }
  .quick-btn .qk-sub { font-size: 9.5px; font-weight: 600; color: var(--muted); font-family: var(--mono); }
  .quick-btn.primary { background: var(--ink); border-color: var(--ink); color: #FFFFFF; }
  .quick-btn.primary .qk-sub { color: #CFC9BC; }
  .quick-btn.primary:hover { background: #2F2D29; }
  .quick-btn.amber-active { background: var(--amber); border-color: #B86104; color: #FFFFFF; }
  .quick-btn.amber-active .qk-sub { color: #FFF3D6; }
  .pause-line {
    font-size: 11px;
    color: var(--amber-ink);
    font-weight: 600;
    margin-top: 6px;
    min-height: 0;
  }
  .pause-line:empty { display: none; }

  .sched-divider { height: 1px; background: var(--border); margin: 11px 0 10px; }
  .sched-hdr {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 10px;
    flex-wrap: wrap;
  }
  .dash-uniform {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    background: var(--well);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 11px 14px;
  }
  .dash-wk-stack { display: flex; flex-direction: column; gap: 7px; }
  .dash-wk-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    background: var(--well);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 7px 11px;
  }
  .dash-wk-lbl { font-size: 11.5px; font-weight: 700; color: var(--ink); min-width: 68px; }
  .dash-wk-pills { display: flex; align-items: center; gap: 6px; }
  .dash-wk-tag { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }

  .jetlag-note {
    display: none;
    margin-top: 8px;
    padding: 6px 9px;
    border-radius: 6px;
    background: var(--amber-bg);
    border: 1px solid #F2D08B;
    color: var(--amber-ink);
    font-size: 11px;
    line-height: 1.3;
  }
  .jetlag-note.show { display: block; }
  .sched-footer {
    margin-top: 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .status-msg { font-size: 11.5px; color: var(--green); font-weight: 600; min-height: 16px; }
  .status-msg.err { color: var(--red); }

  /* Ultra-slim 1-line Footer Disclaimer */
  .cockpit-footer {
    margin-top: 9px;
    font-size: 10.5px;
    color: var(--muted);
    text-align: center;
    line-height: 1.35;
  }

  @media (max-width: 980px) {
    .bento-grid { grid-template-columns: 1fr; }
    .weekend-cards { grid-template-columns: 1fr; }
    .slider-grid { grid-template-columns: 1fr; }
  }
  @media (max-width: 600px) {
    .custom-7-grid { grid-template-columns: 1fr; }
    .hero-row { flex-direction: column; align-items: flex-start; }
    .hw-strip { text-align: left; }
    .chips { justify-content: flex-start; }
  }
</style>
</head>
<body>

<!-- ==================== DEDICATED FULL-VIEWPORT ONBOARDING GATE ==================== -->
<div id="onboardingGate" class="onboarding-gate">
  <section id="onboardingCard" class="onboarding-card" role="dialog" aria-labelledby="obHeadline">
    <div id="obStatusBadge" class="ob-badge">
      <span class="ob-pulse"></span>
      <span id="obBadgeText">SETUP REQUIRED &middot; YOUR SCREEN HAS NOT BEEN MODIFIED YET</span>
    </div>
    <div class="ob-head">
      <h2 id="obHeadline">When do you wake up and go to sleep?</h2>
      <p>Circadian Desk calibrates your display minute-by-minute around your rhythm. Complete this quick setup to activate automatic control.</p>
    </div>

    <div class="seg-tabs" role="tablist" aria-label="Onboarding schedule mode">
      <button type="button" class="seg-btn active" id="obTabUniform" role="tab">Same Every Day</button>
      <button type="button" class="seg-btn" id="obTabWeekend" role="tab">Weekdays &amp; Weekends</button>
      <button type="button" class="seg-btn" id="obTabCustom" role="tab">Custom 7 Days</button>
    </div>

    <div id="obPaneUniform" class="ob-pane">
      <div class="ob-uniform-row">
        <div class="ob-time-pair">
          <div class="ob-field">
            <span class="ob-field-lbl">Wake Time</span>
            <span id="obWakeSlot"></span>
          </div>
          <div class="ob-field">
            <span class="ob-field-lbl">Sleep Time</span>
            <span id="obSleepSlot"></span>
          </div>
        </div>
        <span id="obUniformDur" class="dur-chip">16h awake &middot; 8h sleep</span>
      </div>
    </div>

    <div id="obPaneWeekend" class="ob-pane" style="display:none;">
      <div class="weekend-cards">
        <div class="wk-card">
          <div class="wk-card-head">
            <span class="wk-card-title">Mon &ndash; Fri</span>
            <span id="obWdDur" class="wk-card-dur">16h awake</span>
          </div>
          <div class="wk-pill-row">
            <div class="wk-pill-grp"><span>Wake</span><span id="obWdWakeSlot"></span></div>
            <div class="wk-pill-grp"><span>Sleep</span><span id="obWdSleepSlot"></span></div>
          </div>
        </div>
        <div class="wk-card">
          <div class="wk-card-head">
            <span class="wk-card-title">Sat &ndash; Sun</span>
            <span id="obWeDur" class="wk-card-dur">15h awake</span>
          </div>
          <div class="wk-pill-row">
            <div class="wk-pill-grp"><span>Wake</span><span id="obWeWakeSlot"></span></div>
            <div class="wk-pill-grp"><span>Sleep</span><span id="obWeSleepSlot"></span></div>
          </div>
        </div>
      </div>
    </div>

    <div id="obPaneCustom" class="ob-pane" style="display:none;">
      <div id="obCustomGrid" class="custom-7-grid"></div>
    </div>

    <div id="obConflictsBox" role="status" aria-live="polite"></div>

    <div id="obPreviewStrip" class="ob-preview-strip">
      <span class="ob-preview-panel">
        <span class="ob-preview-dot"></span>
        <span id="obPanelPreview">Detecting display luminance profile...</span>
      </span>
      <span id="obTargetPreview" class="ob-preview-now">Right now --:-- &rarr; Comfort Mode</span>
    </div>

    <div class="ob-footer">
      <label class="switch-wrap">
        <span class="switch">
          <input id="obAutostart" type="checkbox">
          <span class="switch-track"></span>
        </span>
        <span id="obAutostartText" style="font-size:12.5px; font-weight:600; color:var(--ink);">Start automatically with Windows</span>
      </label>
      <div class="ob-actions">
        <button id="obCancelBtn" type="button" class="btn-link-subtle" style="display:none;">Cancel</button>
        <button id="obSubmitBtn" type="button" class="btn btn-amber">Save Schedule &amp; Activate Circadian Desk &rarr;</button>
      </div>
    </div>
  </section>
</div>

<!-- ==================== ZERO-SCROLL WIDESCREEN BENTO COCKPIT ==================== -->
<div id="dashboardRoot" class="cockpit-shell">
  <header class="topbar">
    <div class="brand-row">
      <span id="statusDot" class="dot"></span>
      <h1 class="brand-title">Circadian Desk</h1>
      <span class="brand-sub">&middot; Local-Only &middot; Zero Cloud</span>
    </div>
    <div class="hdr-actions">
      <span id="modePill" class="pill">OPEN-LOOP</span>
      <span id="hdrPill" class="pill pill-hdr" style="display:none;">HDR Active &middot; Color filter auto-paused</span>
      <span id="moviePill" class="pill pill-amber" style="display:none;">MOVIE MODE (3400K)</span>
      <span id="manualPill" class="pill pill-amber" style="display:none;">MANUAL SLIDER</span>
      <span id="pausePill" class="pill pill-amber" style="display:none;">PAUSED</span>
      <span id="autoPausePill" class="pill pill-amber" style="display:none;">AUTO-PAUSED</span>
      <button id="openWizardBtn" type="button" class="top-btn">Setup Wizard</button>
      <a class="top-btn" href="/report">7-Day Report</a>
    </div>
  </header>

  <div id="unlinkedBanner" class="banner banner-red" role="alert">
    <div>
      <strong>UNLINKED LIGHT &mdash; SWITCH OFF</strong>
      <span id="unlinkedMsg" style="font-weight:400; margin-left:8px;"></span>
    </div>
  </div>
  <div id="conflictsContainer" role="status" aria-live="polite"></div>

  <main class="bento-grid">
    <!-- ========== LEFT COLUMN: OPTICAL COCKPIT ========== -->
    <div class="col-left">
      <!-- Card L1: Hero Telemetry + 24h Kelvin-Gradient SVG Curve -->
      <section class="card card-grow">
        <div class="card-hdr">
          <div class="kicker">
            Active Phase: <strong id="phaseText">DAY</strong>
            <span id="transitionText" style="margin-left:5px; font-weight:500;"></span>
          </div>
          <div style="display:flex; align-items:center; gap:7px; flex-wrap:wrap; justify-content:flex-end;">
            <span id="sourcePill" class="source-pill">Source: Clock / Solar Estimate</span>
            <span class="kicker" style="font-size:9.5px;">CIE TN 015 k=2</span>
            <span id="complianceBadge" class="badge badge-neutral">OPEN-LOOP</span>
          </div>
        </div>

        <div class="hero-row">
          <div>
            <div class="metric-big">
              <span id="mediVal">--.-</span>
              <span id="mediUnit" class="metric-unit">nits</span>
            </div>
            <div id="cornealHint" class="corneal-hint"></div>
          </div>
          <div class="hw-strip">
            <div id="monitorVal" class="hw-val">80% @ 5500K</div>
            <div id="monitorChips" class="chips"></div>
          </div>
        </div>

        <div class="curve-well">
          <div class="curve-head">
            <span><span id="graphTitle">24-Hour Smooth Circadian Curve</span><span id="curveScrubHint" class="curve-scrub-hint">&middot; Drag curve to preview any hour</span></span>
            <span id="sparkRange" class="curve-head-right">Loading curve...</span>
          </div>
          <svg id="curveSvg" viewBox="0 0 600 130" preserveAspectRatio="none" role="img" aria-label="24-hour circadian curve">
            <defs>
              <linearGradient id="curveCctGrad" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="600" y2="0">
                <stop offset="0%" stop-color="#D97706"></stop>
                <stop offset="50%" stop-color="#3B7CA5"></stop>
                <stop offset="100%" stop-color="#D97706"></stop>
              </linearGradient>
              <linearGradient id="curveAreaGrad" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="600" y2="0">
                <stop offset="0%" stop-color="#D97706" stop-opacity="0.16"></stop>
                <stop offset="50%" stop-color="#3B7CA5" stop-opacity="0.14"></stop>
                <stop offset="100%" stop-color="#D97706" stop-opacity="0.16"></stop>
              </linearGradient>
            </defs>
            <line x1="0" y1="36" x2="600" y2="36" stroke="#ECE8DE" stroke-width="0.8" stroke-dasharray="2,4"></line>
            <line x1="0" y1="70" x2="600" y2="70" stroke="#ECE8DE" stroke-width="0.8" stroke-dasharray="2,4"></line>
            <line x1="0" y1="106" x2="600" y2="106" stroke="#DAD5C9" stroke-width="1"></line>
            <line id="wakeLine" x1="-20" y1="14" x2="-20" y2="106" stroke="#1E7A4C" stroke-width="1.2" stroke-dasharray="3,3"></line>
            <text id="wakeTag" x="-40" y="12" font-size="9.5" fill="#1E7A4C" font-weight="700">WAKE</text>
            <line id="sleepLine" x1="-20" y1="14" x2="-20" y2="106" stroke="#8A4F0A" stroke-width="1.2" stroke-dasharray="3,3"></line>
            <text id="sleepTag" x="-40" y="12" font-size="9.5" fill="#8A4F0A" font-weight="700">SLEEP</text>
            <path id="curveArea" fill="url(#curveAreaGrad)" d=""></path>
            <polyline id="curvePoly" fill="none" stroke="url(#curveCctGrad)" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round" points=""></polyline>
            <circle id="curveNowGlow" cx="-20" cy="60" r="9" fill="rgba(217,119,6,0.28)"></circle>
            <circle id="curveNowDot" cx="-20" cy="60" r="4.5" fill="#D97706" stroke="#161513" stroke-width="1.4"></circle>
            <text x="4" y="123" font-size="10.5" fill="#666159">00:00</text>
            <text x="142" y="123" font-size="10.5" fill="#666159">06:00</text>
            <text x="290" y="123" font-size="10.5" fill="#666159">12:00</text>
            <text x="438" y="123" font-size="10.5" fill="#666159">18:00</text>
            <text x="566" y="123" font-size="10.5" fill="#666159">24:00</text>
          </svg>
          <svg id="sparkline" viewBox="0 0 600 130" preserveAspectRatio="none" role="img" aria-label="m-EDI sparkline" style="display:none;">
            <line x1="0" y1="110" x2="600" y2="110" stroke="#DAD5C9" stroke-width="1"></line>
            <polyline id="sparkPoly" fill="none" stroke="#D97706" stroke-width="2.4" points=""></polyline>
          </svg>
        </div>

        <div class="telemetry-footer">
          <div id="openLoopFooter" class="openloop-bar">
            <div class="nits-group">
              <span id="panelBadge" class="panel-badge">300 nit &middot; Estimated SDR</span>
              <div class="nits-pills" aria-label="Panel peak luminance presets">
                <button type="button" class="nits-btn active" data-nits="0">Auto</button>
                <button type="button" class="nits-btn" data-nits="300">300</button>
                <button type="button" class="nits-btn" data-nits="500">500</button>
                <button type="button" class="nits-btn" data-nits="800">800</button>
              </div>
              <div class="panel-pills" aria-label="Panel spectrum profile">
                <button type="button" class="panel-btn active" data-panel="auto">Auto</button>
                <button type="button" class="panel-btn" data-panel="wled_ips">W-LED</button>
                <button type="button" class="panel-btn" data-panel="p3_ips">P3</button>
                <button type="button" class="panel-btn" data-panel="woled">WOLED</button>
                <button type="button" class="panel-btn" data-panel="qd_oled">QD-OLED</button>
              </div>
            </div>
            <span id="sensorHint" class="sensor-optional">USB-C V1A sensor optional</span>
          </div>
          <div id="sensorFooter" class="sensor-grid">
            <div><div class="mini-k">Photopic lx</div><div id="photopicVal" class="mini-v">&mdash;</div></div>
            <div><div class="mini-k">Measured CCT</div><div id="cctVal" class="mini-v">&mdash;</div></div>
            <div><div class="mini-k">Melanopic DER</div><div id="mderVal" class="mini-v">&mdash;</div></div>
            <div><div class="mini-k">Flicker Hz</div><div id="flickerVal" class="mini-v">&mdash;</div></div>
          </div>
        </div>
      </section>

      <!-- Card L2: Live Manual Sliders & Linked Smart Bulb -->
      <section class="card">
        <div class="card-hdr">
          <div class="kicker">Live Manual Brightness &amp; Color Temperature</div>
          <button id="resetAutoBtn" type="button" class="btn btn-ghost btn-sm">Return to Auto Curve</button>
        </div>
        <div class="slider-grid">
          <div class="slider-item">
            <div class="slider-top">
              <span class="slider-lbl">Brightness</span>
              <span id="briReadout" class="slider-val">70% (~160 nits)</span>
            </div>
            <input id="manualBriSlider" type="range" min="10" max="100" step="1" value="70">
          </div>
          <div class="slider-item">
            <div class="slider-top">
              <span class="slider-lbl">Color Temperature</span>
              <span id="cctReadout" class="slider-val">4500 K</span>
            </div>
            <input id="manualCctSlider" type="range" min="2200" max="6500" step="50" value="4500">
          </div>
        </div>
        <div class="bulb-strip">
          <div class="bulb-left">
            <span class="kicker">Local Smart Bulb</span>
            <span id="bulbVal" class="bulb-val">No Bulb Linked</span>
          </div>
          <div id="bulbChips" class="chips" style="margin-top:0;"></div>
        </div>
      </section>
    </div>

    <!-- ========== RIGHT COLUMN: SCHEDULE & PROTECTIONS ========== -->
    <div class="col-right">
      <!-- Card R1: Quick Overrides & Sleep Schedule -->
      <section class="card">
        <div class="quick-grid">
          <button id="pauseBtn" type="button" class="quick-btn primary">
            <span>Pause 2h</span>
            <span class="qk-sub">Ctrl+Alt+P</span>
          </button>
          <button id="movieBtn" type="button" class="quick-btn">
            <span id="movieBtnTitle">Movie 2.5h</span>
            <span class="qk-sub">Ctrl+Alt+M</span>
          </button>
          <button id="resumeBtn" type="button" class="quick-btn">
            <span>Resume</span>
            <span class="qk-sub">Auto Curve</span>
          </button>
        </div>
        <div id="pauseCountdown" class="pause-line"></div>

        <div class="sched-divider"></div>

        <form id="settingsForm">
          <div class="sched-hdr">
            <span class="kicker">Sleep Schedule</span>
            <div class="seg-tabs compact" role="tablist" aria-label="Dashboard schedule mode">
              <button type="button" class="seg-btn active" id="tabUniform">Every Day</button>
              <button type="button" class="seg-btn" id="tabWeekend">Weekdays / Weekend</button>
              <button type="button" class="seg-btn" id="tabCustom">7 Days</button>
            </div>
          </div>

          <div id="dashPaneUniform">
            <div class="dash-uniform">
              <div class="ob-time-pair" style="gap:14px;">
                <div class="ob-field">
                  <span class="ob-field-lbl">Wake</span>
                  <span id="dashWakeSlot"></span>
                </div>
                <div class="ob-field">
                  <span class="ob-field-lbl">Sleep</span>
                  <span id="dashSleepSlot"></span>
                </div>
              </div>
              <span id="dashUniformDur" class="dur-chip">16h awake</span>
            </div>
          </div>

          <div id="dashPaneWeekend" class="dash-wk-stack" style="display:none;">
            <div class="dash-wk-row">
              <span class="dash-wk-lbl">Mon &ndash; Fri</span>
              <div class="dash-wk-pills">
                <span class="dash-wk-tag">Wake</span><span id="dashWdWakeSlot"></span>
                <span class="day-arrow">&rarr;</span>
                <span class="dash-wk-tag">Sleep</span><span id="dashWdSleepSlot"></span>
              </div>
            </div>
            <div class="dash-wk-row">
              <span class="dash-wk-lbl">Sat &ndash; Sun</span>
              <div class="dash-wk-pills">
                <span class="dash-wk-tag">Wake</span><span id="dashWeWakeSlot"></span>
                <span class="day-arrow">&rarr;</span>
                <span class="dash-wk-tag">Sleep</span><span id="dashWeSleepSlot"></span>
              </div>
            </div>
          </div>

          <div id="dashPaneCustom" style="display:none;">
            <div id="dashCustomGrid" class="custom-7-grid"></div>
          </div>

          <div id="jetlagNote" class="jetlag-note"></div>

          <div class="sched-footer">
            <span id="formMsg" class="status-msg"></span>
            <button type="submit" class="btn">Save Schedule</button>
          </div>
        </form>
      </section>

      <!-- Card R2: Smart Protections & Preferences -->
      <section class="card card-grow">
        <div class="card-hdr">
          <span class="kicker">Smart Protections &amp; Preferences</span>
          <span class="kicker" style="font-size:9.5px; color:var(--green);">Instant Save</span>
        </div>

        <label class="switch-row">
          <div class="switch-text">
            <div class="switch-title">Strict Circadian (2200K / &le;5 lx)</div>
            <div class="switch-sub">Off = Comfort Mode (3000K&ndash;3200K readable floor)</div>
          </div>
          <span class="switch">
            <input id="sensToggle" type="checkbox">
            <span class="switch-track"></span>
          </span>
        </label>

        <label class="switch-row">
          <div class="switch-text">
            <div class="switch-title">Auto-Pause (Games &amp; Creative Apps)</div>
            <div class="switch-sub">Photoshop, Lightroom, Figma, Resolve, fullscreen games</div>
          </div>
          <span class="switch">
            <input id="autoPauseToggle" type="checkbox" checked>
            <span class="switch-track"></span>
          </span>
        </label>

        <label class="switch-row">
          <div class="switch-text">
            <div class="switch-title">Auto-Pause Fullscreen Browser Video</div>
            <div class="switch-sub">YouTube / Netflix fullscreen (off by default)</div>
          </div>
          <span class="switch">
            <input id="browserFsInput" type="checkbox">
            <span class="switch-track"></span>
          </span>
        </label>

        <label class="switch-row">
          <div class="switch-text">
            <div class="switch-title">Start Automatically with Windows</div>
            <div id="autostartSub" class="switch-sub">Launch quietly to System Tray at Windows sign-in</div>
          </div>
          <span class="switch">
            <input id="autostartInput" type="checkbox">
            <span class="switch-track"></span>
          </span>
        </label>
      </section>
    </div>
  </main>

  <footer class="cockpit-footer">
    Brown et al. (2022) visual comfort reference (ages 18&ndash;55; not a medical device) &middot; 100% local on 127.0.0.1 &middot; Zero screen pixel capture &middot; Zero cloud.
  </footer>
</div>

<!-- ==================== SINGLETON FLOATING DUAL-GRID TIME POPOVER ==================== -->
<div id="timePopover" class="time-popover" role="dialog" aria-label="Select time">
  <div class="tp-head">
    <span id="tpTitle" class="tp-title">Select Time</span>
    <div class="tp-head-r">
      <span id="tpLiveVal" class="tp-live">07:00</span>
      <button id="tpDoneBtn" type="button" class="tp-done">Done</button>
    </div>
  </div>
  <div class="tp-body">
    <div>
      <div class="tp-col-lbl">Hour</div>
      <div id="tpHourGrid" class="tp-hour-grid"></div>
    </div>
    <div>
      <div class="tp-col-lbl">Min</div>
      <div id="tpMinGrid" class="tp-min-grid"></div>
      <div id="tpLegacyMin" class="tp-legacy"></div>
    </div>
  </div>
  <div class="tp-hint">Click hour &rarr; click minute, or type 4 digits on pill</div>
</div>

<script>
(function() {
  var DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  var DAY_LABELS = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
  var MIN_STEPS = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55];

  var latestState = null;
  var latestCurve = [];
  var latestMaxNits = 300.0;
  var latestGamma = 2.0;
  var sliderDragging = false;
  var sliderTimer = null;
  var curveScrubbing = false;
  var curveScrubHoldUntil = 0;
  var curveScrubRevertTimer = null;
  var curveLastPostMs = 0;
  var formDirty = false;
  var wizardOpen = false;
  var obInitialized = false;
  var serverOnboardingCompleted = true;
  var PANEL_LABELS = { auto: "Auto", wled_ips: "W-LED", p3_ips: "P3", woled: "WOLED", qd_oled: "QD-OLED" };

  var obMode = "uniform";
  var dashMode = "uniform";

  var formMsg = document.getElementById("formMsg");
  var briSlider = document.getElementById("manualBriSlider");
  var cctSlider = document.getElementById("manualCctSlider");
  var briReadout = document.getElementById("briReadout");
  var cctReadout = document.getElementById("cctReadout");
  var sensEl = document.getElementById("sensToggle");
  var autoPauseEl = document.getElementById("autoPauseToggle");
  var browserFsEl = document.getElementById("browserFsInput");
  var autostartEl = document.getElementById("autostartInput");

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function hourToParts(h) {
    var totalM = Math.round(((((Number(h) || 0) % 24) + 24) % 24) * 60) % 1440;
    return { hh: Math.floor(totalM / 60), mm: totalM % 60 };
  }

  function partsToHour(hh, mm) {
    return Math.round((((hh % 24) + 24) % 24 + (mm / 60.0)) * 100) / 100;
  }

  function hourToHHMM(h) {
    var p = hourToParts(h);
    return pad2(p.hh) + ":" + pad2(p.mm);
  }

  function formatAwakeDuration(wakeH, sleepH, includeSleep) {
    var awake = (((Number(sleepH) || 0) - (Number(wakeH) || 0)) % 24 + 24) % 24;
    if (awake < 0.05) awake = 16;
    var sleep = 24 - awake;
    var aStr = (Math.abs(awake - Math.round(awake)) < 0.05) ? (Math.round(awake) + "h") : (awake.toFixed(1) + "h");
    if (!includeSleep) return aStr + " awake";
    var sStr = (Math.abs(sleep - Math.round(sleep)) < 0.05) ? (Math.round(sleep) + "h") : (sleep.toFixed(1) + "h");
    return aStr + " awake \u00b7 " + sStr + " sleep";
  }

  function setMsg(txt, isErr) {
    formMsg.textContent = txt || "";
    formMsg.className = isErr ? "status-msg err" : "status-msg";
  }

  /* ---------- Singleton Floating Dual-Grid Braun Time Popover ---------- */
  var popoverEl = document.getElementById("timePopover");
  var tpTitleEl = document.getElementById("tpTitle");
  var tpLiveEl = document.getElementById("tpLiveVal");
  var tpHourGrid = document.getElementById("tpHourGrid");
  var tpMinGrid = document.getElementById("tpMinGrid");
  var tpLegacyMin = document.getElementById("tpLegacyMin");
  var activePicker = null;
  var hourBtns = [];
  var minBtns = [];

  for (var hIdx = 0; hIdx < 24; hIdx++) {
    (function(hv) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "tp-cell";
      b.textContent = pad2(hv);
      b.addEventListener("click", function(ev) {
        ev.stopPropagation();
        if (!activePicker) return;
        activePicker.setParts(hv, activePicker.mm, true);
        syncPopoverSelection();
      });
      tpHourGrid.appendChild(b);
      hourBtns.push(b);
    })(hIdx);
  }

  for (var mIdx = 0; mIdx < MIN_STEPS.length; mIdx++) {
    (function(mv) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "tp-cell";
      b.textContent = pad2(mv);
      b.addEventListener("click", function(ev) {
        ev.stopPropagation();
        if (!activePicker) return;
        activePicker.setParts(activePicker.hh, mv, true);
        closePopover();
      });
      tpMinGrid.appendChild(b);
      minBtns.push({ val: mv, el: b });
    })(MIN_STEPS[mIdx]);
  }

  function syncPopoverSelection() {
    if (!activePicker) return;
    tpLiveEl.textContent = pad2(activePicker.hh) + ":" + pad2(activePicker.mm);
    for (var i = 0; i < hourBtns.length; i++) {
      hourBtns[i].className = (i === activePicker.hh) ? "tp-cell active" : "tp-cell";
    }
    var exactFound = false;
    for (var j = 0; j < minBtns.length; j++) {
      var isM = (minBtns[j].val === activePicker.mm);
      if (isM) exactFound = true;
      minBtns[j].el.className = isM ? "tp-cell min-active" : "tp-cell";
    }
    if (!exactFound) {
      tpLegacyMin.textContent = "Exact minute :" + pad2(activePicker.mm);
      tpLegacyMin.className = "tp-legacy show";
    } else {
      tpLegacyMin.className = "tp-legacy";
    }
  }

  function positionPopover(anchorBtn) {
    popoverEl.className = "time-popover show";
    var rect = anchorBtn.getBoundingClientRect();
    var popW = popoverEl.offsetWidth || 332;
    var popH = popoverEl.offsetHeight || 262;
    var vw = window.innerWidth || document.documentElement.clientWidth || 1280;
    var vh = window.innerHeight || document.documentElement.clientHeight || 720;

    var top = rect.bottom + 6;
    if (top + popH + 10 > vh && (rect.top - popH - 6) >= 10) {
      top = rect.top - popH - 6;
    } else if (top + popH + 10 > vh) {
      top = Math.max(10, vh - popH - 10);
    }
    var left = rect.left;
    if (left + popW + 12 > vw) {
      left = Math.max(10, rect.right - popW);
    }
    if (left + popW + 12 > vw) {
      left = Math.max(10, vw - popW - 12);
    }
    popoverEl.style.top = Math.round(top) + "px";
    popoverEl.style.left = Math.round(left) + "px";
  }

  function closePopover() {
    if (activePicker) {
      activePicker.btn.classList.remove("open");
      activePicker.btn.setAttribute("aria-expanded", "false");
      activePicker = null;
    }
    popoverEl.className = "time-popover";
  }

  function openPopover(picker) {
    if (activePicker === picker) {
      closePopover();
      return;
    }
    closePopover();
    activePicker = picker;
    picker.btn.classList.add("open");
    picker.btn.setAttribute("aria-expanded", "true");
    tpTitleEl.textContent = picker.label || "Select Time";
    syncPopoverSelection();
    positionPopover(picker.btn);
  }

  document.getElementById("tpDoneBtn").addEventListener("click", function(ev) {
    ev.stopPropagation();
    closePopover();
  });
  popoverEl.addEventListener("click", function(ev) { ev.stopPropagation(); });
  document.addEventListener("click", function() { closePopover(); });
  document.addEventListener("keydown", function(ev) {
    if (ev.key === "Escape") closePopover();
  });
  window.addEventListener("resize", function() {
    if (activePicker) positionPopover(activePicker.btn);
  });

  function createTimePill(hostEl, initialHour, label, sizeClass, onChange) {
    var p = hourToParts(initialHour);
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "time-pill" + (sizeClass ? (" " + sizeClass) : "");
    btn.setAttribute("data-time-picker", "1");
    btn.setAttribute("aria-haspopup", "dialog");
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-label", label || "Time");

    var digitBuf = "";
    var digitTimer = null;

    var api = {
      hh: p.hh,
      mm: p.mm,
      label: label || "Select Time",
      btn: btn,
      setParts: function(newH, newM, notify) {
        api.hh = ((parseInt(newH, 10) || 0) % 24 + 24) % 24;
        api.mm = ((parseInt(newM, 10) || 0) % 60 + 60) % 60;
        btn.textContent = pad2(api.hh) + ":" + pad2(api.mm);
        if (notify && onChange) onChange(api.getHour());
      },
      setHour: function(hourVal) {
        var pt = hourToParts(hourVal);
        api.setParts(pt.hh, pt.mm, false);
      },
      getHour: function() {
        return partsToHour(api.hh, api.mm);
      }
    };
    api.setParts(p.hh, p.mm, false);

    btn.addEventListener("click", function(ev) {
      ev.stopPropagation();
      openPopover(api);
    });

    btn.addEventListener("keydown", function(ev) {
      if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
        ev.preventDefault();
        var step = ev.shiftKey ? 60 : 15;
        var delta = ev.key === "ArrowUp" ? step : -step;
        var tot = (api.hh * 60 + api.mm + delta + 1440) % 1440;
        api.setParts(Math.floor(tot / 60), tot % 60, true);
        if (activePicker === api) syncPopoverSelection();
      } else if (ev.key >= "0" && ev.key <= "9") {
        ev.preventDefault();
        digitBuf += ev.key;
        if (digitTimer) clearTimeout(digitTimer);
        digitTimer = setTimeout(function() { digitBuf = ""; }, 1600);
        if (digitBuf.length === 2) {
          var h2 = parseInt(digitBuf, 10);
          if (h2 >= 0 && h2 <= 23) {
            api.setParts(h2, api.mm, true);
            if (activePicker === api) syncPopoverSelection();
          }
        } else if (digitBuf.length >= 4) {
          var hh4 = parseInt(digitBuf.slice(0, 2), 10);
          var mm4 = parseInt(digitBuf.slice(2, 4), 10);
          digitBuf = "";
          if (hh4 >= 0 && hh4 <= 23 && mm4 >= 0 && mm4 <= 59) {
            api.setParts(hh4, mm4, true);
            if (activePicker === api) syncPopoverSelection();
          }
        }
      }
    });

    hostEl.appendChild(btn);
    return api;
  }

  /* ---------- Build Schedule Editors (Onboarding + Dashboard) ---------- */
  function buildCustom7Grid(containerEl, sizeClass, onMutate) {
    var rows = {};
    DAYS.forEach(function(day) {
      var row = document.createElement("div");
      row.className = "day-row";
      var code = document.createElement("span");
      code.className = "day-code";
      code.textContent = DAY_LABELS[day];
      var pills = document.createElement("div");
      pills.className = "day-pills";
      var wHost = document.createElement("span");
      var arr = document.createElement("span");
      arr.className = "day-arrow";
      arr.textContent = "\u2192";
      var sHost = document.createElement("span");
      pills.appendChild(wHost);
      pills.appendChild(arr);
      pills.appendChild(sHost);
      row.appendChild(code);
      row.appendChild(pills);
      containerEl.appendChild(row);
      var defW = (day === "sat" || day === "sun") ? 9.0 : 7.0;
      var defS = (day === "sat" || day === "sun") ? 0.0 : 23.0;
      rows[day] = {
        wake: createTimePill(wHost, defW, DAY_LABELS[day] + " Wake", sizeClass, onMutate),
        sleep: createTimePill(sHost, defS, DAY_LABELS[day] + " Sleep", sizeClass, onMutate)
      };
    });
    var copySlot = document.createElement("div");
    copySlot.className = "copy-slot";
    var copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "copy-mon-btn";
    copyBtn.textContent = "Copy Mon \u2192 Weekdays";
    copyBtn.addEventListener("click", function() {
      var mw = rows.mon.wake.getHour();
      var ms = rows.mon.sleep.getHour();
      ["tue", "wed", "thu", "fri"].forEach(function(d) {
        rows[d].wake.setHour(mw);
        rows[d].sleep.setHour(ms);
      });
      if (onMutate) onMutate();
    });
    copySlot.appendChild(copyBtn);
    containerEl.appendChild(copySlot);
    return { rows: rows, copyBtn: copyBtn };
  }

  function detectScheduleMode(ws) {
    if (!ws || typeof ws !== "object") return "uniform";
    var keys = Object.keys(ws);
    if (keys.length === 0) return "uniform";
    var mon = ws.mon;
    var sat = ws.sat;
    if (mon && sat) {
      var wdSame = ["tue", "wed", "thu", "fri"].every(function(d) {
        return ws[d] && Math.abs(ws[d].wake_hour - mon.wake_hour) < 0.02 && Math.abs(ws[d].sleep_hour - mon.sleep_hour) < 0.02;
      });
      var weSame = ws.sun && Math.abs(ws.sun.wake_hour - sat.wake_hour) < 0.02 && Math.abs(ws.sun.sleep_hour - sat.sleep_hour) < 0.02;
      if (wdSame && weSame) return "weekend";
    }
    return "custom";
  }

  /* Onboarding Pickers */
  function evalPreviewCurve(h, wake, sleep) {
    var awake = (((sleep - wake) % 24) + 24) % 24;
    if (awake < 0.01) awake = 16.0;
    var t = (((h - wake) % 24) + 24) % 24;
    var sunriseEnd = Math.min(1.5, Math.max(0.0, awake - 3.5));
    var sunsetStart = Math.max(sunriseEnd, awake - 3.5);
    var peakT = Math.min(5.0, Math.max(sunriseEnd, sunriseEnd + (sunsetStart - sunriseEnd) * 0.32));
    var sunsetEnd = Math.max(sunsetStart, awake - 1.5);
    function ease(u) { var c = Math.max(0, Math.min(1, u)); return 0.5 * (1 - Math.cos(Math.PI * c)); }
    function lerp(a, b, f) { return a + (b - a) * f; }
    if (t < sunriseEnd && sunriseEnd > 0.01) {
      var s1 = ease(t / sunriseEnd);
      return { nits: lerp(55, 195, s1), cct: lerp(3200, 5200, s1) };
    }
    if (t < peakT && (peakT - sunriseEnd) > 0.01) {
      var s2 = ease((t - sunriseEnd) / (peakT - sunriseEnd));
      return { nits: lerp(195, 220, s2), cct: lerp(5200, 5800, s2) };
    }
    if (t < sunsetStart && (sunsetStart - peakT) > 0.01) {
      var s3 = ease((t - peakT) / (sunsetStart - peakT));
      return { nits: lerp(220, 160, s3), cct: lerp(5800, 4300, s3) };
    }
    if (t < sunsetEnd && (sunsetEnd - sunsetStart) > 0.01) {
      var s4 = ease((t - sunsetStart) / (sunsetEnd - sunsetStart));
      return { nits: lerp(160, 90, s4), cct: lerp(4300, 2850, s4) };
    }
    if (t < awake && (awake - sunsetEnd) > 0.01) {
      var s5 = ease((t - sunsetEnd) / (awake - sunsetEnd));
      return { nits: lerp(90, 65, s5), cct: lerp(2850, 2700, s5) };
    }
    return { nits: 50, cct: 2700 };
  }

  function updateObPreview() {
    document.getElementById("obUniformDur").textContent = formatAwakeDuration(obUniformWake.getHour(), obUniformSleep.getHour(), true);
    document.getElementById("obWdDur").textContent = formatAwakeDuration(obWdWake.getHour(), obWdSleep.getHour(), false);
    document.getElementById("obWeDur").textContent = formatAwakeDuration(obWeWake.getHour(), obWeSleep.getHour(), false);

    var nowD = new Date();
    var nowH = (latestState && latestState.current_hour !== null && latestState.current_hour !== undefined)
      ? Number(latestState.current_hour)
      : (nowD.getHours() + nowD.getMinutes() / 60.0);

    var wH = obUniformWake.getHour();
    var sH = obUniformSleep.getHour();
    var dayIdx = (nowD.getDay() + 6) % 7;
    var dayKey = DAYS[dayIdx];
    if (obMode === "weekend") {
      if (dayKey === "sat" || dayKey === "sun") { wH = obWeWake.getHour(); sH = obWeSleep.getHour(); }
      else { wH = obWdWake.getHour(); sH = obWdSleep.getHour(); }
    } else if (obMode === "custom") {
      wH = obCustom.rows[dayKey].wake.getHour();
      sH = obCustom.rows[dayKey].sleep.getHour();
    }
    var prev = evalPreviewCurve(nowH, wH, sH);
    document.getElementById("obTargetPreview").textContent =
      "Right now " + hourToHHMM(nowH) + " \u2192 Comfort Mode \u00b7 " + Math.round(prev.nits) + " nits @ " + Math.round(prev.cct) + "K";
  }

  var obUniformWake = createTimePill(document.getElementById("obWakeSlot"), 7.0, "Wake Time", "lg", updateObPreview);
  var obUniformSleep = createTimePill(document.getElementById("obSleepSlot"), 23.0, "Sleep Time", "lg", updateObPreview);
  var obWdWake = createTimePill(document.getElementById("obWdWakeSlot"), 7.0, "Weekday Wake", "", updateObPreview);
  var obWdSleep = createTimePill(document.getElementById("obWdSleepSlot"), 23.0, "Weekday Sleep", "", updateObPreview);
  var obWeWake = createTimePill(document.getElementById("obWeWakeSlot"), 9.0, "Weekend Wake", "", updateObPreview);
  var obWeSleep = createTimePill(document.getElementById("obWeSleepSlot"), 0.0, "Weekend Sleep", "", updateObPreview);
  var obCustom = buildCustom7Grid(document.getElementById("obCustomGrid"), "sm", updateObPreview);
  obCustom.copyBtn.id = "obCopyMonBtn";

  function setObMode(mode) {
    obMode = mode;
    document.getElementById("obTabUniform").className = mode === "uniform" ? "seg-btn active" : "seg-btn";
    document.getElementById("obTabWeekend").className = mode === "weekend" ? "seg-btn active" : "seg-btn";
    document.getElementById("obTabCustom").className = mode === "custom" ? "seg-btn active" : "seg-btn";
    document.getElementById("obPaneUniform").style.display = mode === "uniform" ? "block" : "none";
    document.getElementById("obPaneWeekend").style.display = mode === "weekend" ? "block" : "none";
    document.getElementById("obPaneCustom").style.display = mode === "custom" ? "block" : "none";
    closePopover();
    updateObPreview();
  }
  document.getElementById("obTabUniform").addEventListener("click", function() { setObMode("uniform"); });
  document.getElementById("obTabWeekend").addEventListener("click", function() { setObMode("weekend"); });
  document.getElementById("obTabCustom").addEventListener("click", function() { setObMode("custom"); });

  function collectSchedule(mode, uWake, uSleep, wdWake, wdSleep, weWake, weSleep, customRows) {
    if (mode === "uniform") {
      return { wake_hour: uWake.getHour(), sleep_hour: uSleep.getHour(), weekly_schedule: {} };
    }
    var out = {};
    if (mode === "weekend") {
      var wdw = wdWake.getHour(), wds = wdSleep.getHour();
      var wew = weWake.getHour(), wes = weSleep.getHour();
      DAYS.forEach(function(d) {
        out[d] = (d === "sat" || d === "sun")
          ? { wake_hour: wew, sleep_hour: wes }
          : { wake_hour: wdw, sleep_hour: wds };
      });
      return { wake_hour: wdw, sleep_hour: wds, weekly_schedule: out };
    }
    DAYS.forEach(function(d) {
      out[d] = { wake_hour: customRows[d].wake.getHour(), sleep_hour: customRows[d].sleep.getHour() };
    });
    return { wake_hour: customRows.mon.wake.getHour(), sleep_hour: customRows.mon.sleep.getHour(), weekly_schedule: out };
  }

  function hydrateEditor(ws, defW, defS, uWake, uSleep, wdWake, wdSleep, weWake, weSleep, customRows) {
    uWake.setHour(defW);
    uSleep.setHour(defS);
    var mon = (ws && ws.mon) ? ws.mon : { wake_hour: defW, sleep_hour: defS };
    var sat = (ws && ws.sat) ? ws.sat : { wake_hour: (defW + 1.5) % 24, sleep_hour: (defS + 1.0) % 24 };
    wdWake.setHour(mon.wake_hour);
    wdSleep.setHour(mon.sleep_hour);
    weWake.setHour(sat.wake_hour);
    weSleep.setHour(sat.sleep_hour);
    DAYS.forEach(function(d) {
      var entry = (ws && ws[d]) ? ws[d] : ((d === "sat" || d === "sun") ? sat : mon);
      customRows[d].wake.setHour(entry.wake_hour);
      customRows[d].sleep.setHour(entry.sleep_hour);
    });
  }

  /* Dashboard Pickers */
  function markDashDirty() {
    formDirty = true;
    document.getElementById("dashUniformDur").textContent = formatAwakeDuration(dashUniformWake.getHour(), dashUniformSleep.getHour(), false);
  }
  var dashUniformWake = createTimePill(document.getElementById("dashWakeSlot"), 7.0, "Wake Time", "", markDashDirty);
  var dashUniformSleep = createTimePill(document.getElementById("dashSleepSlot"), 23.0, "Sleep Time", "", markDashDirty);
  var dashWdWake = createTimePill(document.getElementById("dashWdWakeSlot"), 7.0, "Weekday Wake", "sm", markDashDirty);
  var dashWdSleep = createTimePill(document.getElementById("dashWdSleepSlot"), 23.0, "Weekday Sleep", "sm", markDashDirty);
  var dashWeWake = createTimePill(document.getElementById("dashWeWakeSlot"), 9.0, "Weekend Wake", "sm", markDashDirty);
  var dashWeSleep = createTimePill(document.getElementById("dashWeSleepSlot"), 0.0, "Weekend Sleep", "sm", markDashDirty);
  var dashCustom = buildCustom7Grid(document.getElementById("dashCustomGrid"), "sm", markDashDirty);
  dashCustom.copyBtn.id = "copyMonBtn";

  function setDashMode(mode, byUser) {
    dashMode = mode;
    if (byUser) formDirty = true;
    document.getElementById("tabUniform").className = mode === "uniform" ? "seg-btn active" : "seg-btn";
    document.getElementById("tabWeekend").className = mode === "weekend" ? "seg-btn active" : "seg-btn";
    document.getElementById("tabCustom").className = mode === "custom" ? "seg-btn active" : "seg-btn";
    document.getElementById("dashPaneUniform").style.display = mode === "uniform" ? "block" : "none";
    document.getElementById("dashPaneWeekend").style.display = mode === "weekend" ? "flex" : "none";
    document.getElementById("dashPaneCustom").style.display = mode === "custom" ? "block" : "none";
    closePopover();
  }
  document.getElementById("tabUniform").addEventListener("click", function() { setDashMode("uniform", true); });
  document.getElementById("tabWeekend").addEventListener("click", function() { setDashMode("weekend", true); });
  document.getElementById("tabCustom").addEventListener("click", function() { setDashMode("custom", true); });

  /* Setup Wizard Re-open & Submit */
  function syncGateVisibility() {
    var showGate = (!serverOnboardingCompleted) || wizardOpen;
    var gateEl = document.getElementById("onboardingGate");
    var dashEl = document.getElementById("dashboardRoot");
    var badgeEl = document.getElementById("obStatusBadge");
    var badgeTxt = document.getElementById("obBadgeText");
    var cancelBtn = document.getElementById("obCancelBtn");
    var submitBtn = document.getElementById("obSubmitBtn");

    if (showGate) {
      gateEl.className = "onboarding-gate show";
      dashEl.className = "cockpit-shell hidden-for-gate";
      if (!serverOnboardingCompleted) {
        badgeEl.className = "ob-badge";
        badgeTxt.textContent = "SETUP REQUIRED \u00b7 YOUR SCREEN HAS NOT BEEN MODIFIED YET";
        cancelBtn.style.display = "none";
        submitBtn.textContent = "Save Schedule & Activate Circadian Desk \u2192";
      } else {
        badgeEl.className = "ob-badge edit-mode";
        badgeTxt.textContent = "EDIT SCHEDULE \u00b7 SETUP WIZARD";
        cancelBtn.style.display = "inline-block";
        submitBtn.textContent = "Save Schedule & Return to Dashboard \u2192";
      }
      updateObPreview();
    } else {
      gateEl.className = "onboarding-gate";
      dashEl.className = "cockpit-shell";
    }
  }

  document.getElementById("openWizardBtn").addEventListener("click", function() {
    wizardOpen = true;
    if (latestState) {
      var ws = latestState.weekly_schedule || {};
      hydrateEditor(ws, latestState.wake_hour, latestState.sleep_hour, obUniformWake, obUniformSleep, obWdWake, obWdSleep, obWeWake, obWeSleep, obCustom.rows);
      setObMode(detectScheduleMode(ws));
      document.getElementById("obAutostart").checked = Boolean(latestState.start_with_windows);
    }
    syncGateVisibility();
  });

  document.getElementById("obCancelBtn").addEventListener("click", function() {
    wizardOpen = false;
    closePopover();
    syncGateVisibility();
  });

  document.getElementById("obAutostart").addEventListener("change", function() {
    var obAutoEl = document.getElementById("obAutostart");
    if (latestState && latestState.is_packaged_msix) {
      obAutoEl.checked = Boolean(latestState.start_with_windows);
      openSettings("startupapps", "Opened Windows Startup Apps (MSIX).");
    }
  });

  document.getElementById("obSubmitBtn").addEventListener("click", function() {
    var collected = collectSchedule(obMode, obUniformWake, obUniformSleep, obWdWake, obWdSleep, obWeWake, obWeSleep, obCustom.rows);
    var payload = {
      wake_hour: collected.wake_hour,
      sleep_hour: collected.sleep_hour,
      weekly_schedule: collected.weekly_schedule,
      onboarding_completed: true
    };
    if (!(latestState && latestState.is_packaged_msix)) {
      payload.start_with_windows = Boolean(document.getElementById("obAutostart").checked);
    }
    fetch("/api/onboarding", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
      .then(function(res) {
        if (res.ok) {
          serverOnboardingCompleted = true;
          wizardOpen = false;
          formDirty = false;
          closePopover();
          syncGateVisibility();
          setMsg("Schedule active (" + hourToHHMM(res.d.wake_hour) + " \u2192 " + hourToHHMM(res.d.sleep_hour) + ").", false);
          poll();
        } else {
          alert(res.d.error || "Wake and sleep times must differ.");
        }
      }).catch(function() { alert("Local server unreachable."); });
  });

  /* ---------- Live Manual Sliders ---------- */
  function estNits(pct, maxNits, gamma) {
    var minN = Math.max(15.0, 0.06 * maxNits);
    var norm = Math.max(0, Math.min(100, Number(pct) || 0)) / 100.0;
    return minN + (maxNits - minN) * Math.pow(norm, Math.max(0.5, gamma || 2.0));
  }

  function updateSliderLabels() {
    var pct = parseInt(briSlider.value, 10) || 70;
    var cct = parseInt(cctSlider.value, 10) || 4500;
    var nits = estNits(pct, latestMaxNits, latestGamma);
    briReadout.textContent = pct + "% (~" + Math.round(nits) + " nits)";
    cctReadout.textContent = cct + " K";
  }

  function pushManualOverride() {
    var payload = {
      enabled: true,
      brightness_pct: parseInt(briSlider.value, 10),
      cct_k: parseInt(cctSlider.value, 10)
    };
    fetch("/api/override", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
      .then(function(res) {
        if (res.ok) {
          setMsg("Manual override (" + res.d.brightness_pct + "% @ " + res.d.cct_k + "K).", false);
          poll();
        } else setMsg(res.d.error || "Override failed.", true);
      }).catch(function() { setMsg("Local server unreachable.", true); });
  }

  function onSliderInput() {
    sliderDragging = true;
    updateSliderLabels();
    if (sliderTimer) clearTimeout(sliderTimer);
    sliderTimer = setTimeout(function() {
      pushManualOverride();
      setTimeout(function() { sliderDragging = false; }, 350);
    }, 120);
  }
  briSlider.addEventListener("input", onSliderInput);
  cctSlider.addEventListener("input", onSliderInput);

  document.getElementById("resetAutoBtn").addEventListener("click", function() {
    sliderDragging = false;
    fetch("/api/override", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: false })
    }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
      .then(function(res) {
        if (res.ok) { setMsg("Returned to automatic circadian curve.", false); poll(); }
        else setMsg(res.d.error || "Reset failed.", true);
      }).catch(function() { setMsg("Local server unreachable.", true); });
  });

  /* ---------- Instant-Save Toggles, Panel Presets & Windows Settings ---------- */
  function postSetting(obj, okText) {
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(obj)
    }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
      .then(function(res) {
        if (res.ok) { setMsg(okText, false); poll(); }
        else setMsg(res.d.error || "Failed to update setting.", true);
      }).catch(function() { setMsg("Local server unreachable.", true); });
  }

  function openSettings(target, okText) {
    fetch("/api/open-settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: target })
    }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
      .then(function(res) {
        if (res.ok && res.d.opened) setMsg(okText || "Opened Windows Settings.", false);
        else if (res.ok) setMsg(res.d.uri || "Could not open Windows Settings.", true);
        else setMsg(res.d.error || "Could not open Windows Settings.", true);
      }).catch(function() { setMsg("Local server unreachable.", true); });
  }

  sensEl.addEventListener("change", function() {
    var strictOn = Boolean(sensEl.checked);
    postSetting(
      { high_sensitivity_evening: strictOn, comfort_mode: !strictOn },
      strictOn ? "Strict Circadian Mode (2200K / <=5 lx) ON." : "Comfort Mode (3000K-3200K floor) active."
    );
  });
  autoPauseEl.addEventListener("change", function() {
    postSetting({ auto_pause_enabled: Boolean(autoPauseEl.checked) }, "Auto-Pause (Games / Creative): " + (autoPauseEl.checked ? "ON" : "OFF"));
  });
  browserFsEl.addEventListener("change", function() {
    postSetting({ auto_pause_browser_fullscreen: Boolean(browserFsEl.checked) }, "Fullscreen Browser Video Auto-Pause: " + (browserFsEl.checked ? "ON" : "OFF"));
  });
  autostartEl.addEventListener("change", function() {
    if (latestState && latestState.is_packaged_msix) {
      autostartEl.checked = Boolean(latestState.start_with_windows);
      openSettings("startupapps", "Opened Windows Startup Apps (MSIX).");
      return;
    }
    postSetting({ start_with_windows: Boolean(autostartEl.checked) }, "Start with Windows: " + (autostartEl.checked ? "Enabled" : "Disabled"));
  });

  Array.prototype.forEach.call(document.querySelectorAll(".nits-btn"), function(btn) {
    btn.addEventListener("click", function() {
      var val = parseFloat(btn.getAttribute("data-nits"));
      postSetting({ display_max_nits: val }, val === 0 ? "Display peak: Auto-detect." : "Display peak: " + val + " nits.");
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll(".panel-btn"), function(btn) {
    btn.addEventListener("click", function() {
      var pt = btn.getAttribute("data-panel") || "auto";
      var lbl = PANEL_LABELS[pt] || pt;
      postSetting({ panel_type: pt }, "Panel spectrum: " + lbl + ".");
    });
  });

  /* ---------- Quick Override Buttons ---------- */
  document.getElementById("pauseBtn").addEventListener("click", function() {
    fetch("/api/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes: 120 })
    }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
      .then(function(res) {
        if (res.ok) { setMsg("Paused 2h. Display restored cleanly.", false); poll(); }
        else setMsg(res.d.error || "Pause failed.", true);
      }).catch(function() { setMsg("Local server unreachable.", true); });
  });

  document.getElementById("resumeBtn").addEventListener("click", function() {
    fetch("/api/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes: 0 })
    }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
      .then(function(res) {
        if (res.ok) { setMsg("Resumed live circadian schedule.", false); poll(); }
        else setMsg(res.d.error || "Resume failed.", true);
      }).catch(function() { setMsg("Local server unreachable.", true); });
  });

  document.getElementById("movieBtn").addEventListener("click", function() {
    fetch("/api/movie", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ toggle: true })
    }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
      .then(function(res) {
        if (res.ok) {
          setMsg(res.d.movie_mode_active ? "Movie Mode ON (2.5h @ 3400K)." : "Exited Movie Mode.", false);
          poll();
        } else setMsg(res.d.error || "Movie Mode failed.", true);
      }).catch(function() { setMsg("Local server unreachable.", true); });
  });

  document.getElementById("settingsForm").addEventListener("submit", function(ev) {
    ev.preventDefault();
    var collected = collectSchedule(dashMode, dashUniformWake, dashUniformSleep, dashWdWake, dashWdSleep, dashWeWake, dashWeSleep, dashCustom.rows);
    var payload = {
      wake_hour: collected.wake_hour,
      sleep_hour: collected.sleep_hour,
      weekly_schedule: collected.weekly_schedule,
      onboarding_completed: true
    };
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
      .then(function(res) {
        if (res.ok) {
          formDirty = false;
          setMsg("Saved schedule (" + hourToHHMM(res.d.wake_hour) + " \u2192 " + hourToHHMM(res.d.sleep_hour) + ").", false);
          poll();
        } else {
          setMsg(res.d.error || "Invalid schedule.", true);
        }
      }).catch(function() { setMsg("Local server unreachable.", true); });
  });

  /* ---------- 24-Hour Kelvin-Gradient SVG Curve ---------- */
  function cctToRgb(cctK) {
    var k = Math.max(2200, Math.min(6200, Number(cctK) || 4000));
    var stops = [
      { k: 2200, r: 214, g: 98,  b: 22 },
      { k: 2850, r: 217, g: 119, b: 6 },
      { k: 4300, r: 218, g: 168, b: 74 },
      { k: 5200, r: 112, g: 154, b: 184 },
      { k: 6000, r: 59,  g: 124, b: 165 }
    ];
    if (k <= stops[0].k) return stops[0];
    var last = stops[stops.length - 1];
    if (k >= last.k) return last;
    for (var i = 0; i < stops.length - 1; i++) {
      var a = stops[i], b = stops[i + 1];
      if (k >= a.k && k <= b.k) {
        var f = (k - a.k) / (b.k - a.k);
        return {
          r: Math.round(a.r + (b.r - a.r) * f),
          g: Math.round(a.g + (b.g - a.g) * f),
          b: Math.round(a.b + (b.b - a.b) * f)
        };
      }
    }
    return last;
  }

  function cctToColor(cctK) {
    var c = cctToRgb(cctK);
    return "rgb(" + c.r + "," + c.g + "," + c.b + ")";
  }

  function sampleCurveAtHour(curve, hour) {
    if (!curve || curve.length === 0) {
      return { pct: 70, nits: 160, cct: 4500 };
    }
    var h = Math.max(0.0, Math.min(24.0, Number(hour) || 0));
    var lo = curve[0], hi = curve[curve.length - 1];
    for (var i = 0; i < curve.length - 1; i++) {
      var a = curve[i], b = curve[i + 1];
      var ah = Number(a.hour) || 0;
      var bh = Number(b.hour) || 0;
      if (h >= ah && h <= bh) {
        lo = a;
        hi = b;
        var span = Math.max(1e-6, bh - ah);
        var f = Math.max(0, Math.min(1, (h - ah) / span));
        return {
          pct: ((Number(lo.monitor_brightness_pct) || 0) * (1 - f)) + ((Number(hi.monitor_brightness_pct) || 0) * f),
          nits: ((Number(lo.target_screen_nits) || 0) * (1 - f)) + ((Number(hi.target_screen_nits) || 0) * f),
          cct: ((Number(lo.monitor_cct_k) || 4500) * (1 - f)) + ((Number(hi.monitor_cct_k) || 4500) * f)
        };
      }
    }
    var edge = (h <= (Number(lo.hour) || 0)) ? lo : hi;
    return {
      pct: Number(edge.monitor_brightness_pct) || 0,
      nits: Number(edge.target_screen_nits) || 0,
      cct: Number(edge.monitor_cct_k) || 4500
    };
  }

  function render24hCurve(curve, curHour, profileLabel, wakeH, sleepH, liveNits, liveCct) {
    var poly = document.getElementById("curvePoly");
    var area = document.getElementById("curveArea");
    var dot = document.getElementById("curveNowDot");
    var glow = document.getElementById("curveNowGlow");
    var rangeEl = document.getElementById("sparkRange");
    var wakeLine = document.getElementById("wakeLine");
    var wakeTag = document.getElementById("wakeTag");
    var sleepLine = document.getElementById("sleepLine");
    var sleepTag = document.getElementById("sleepTag");
    var cctGrad = document.getElementById("curveCctGrad");
    var areaGrad = document.getElementById("curveAreaGrad");

    if (!curve || curve.length === 0) {
      latestCurve = [];
      poly.setAttribute("points", "");
      area.setAttribute("d", "");
      rangeEl.textContent = "Computing 24h curve...";
      return;
    }
    latestCurve = curve.slice();
    var w = 600, topY = 20, botY = 104;
    var wx = (((Number(wakeH) || 7) % 24) / 24.0) * w;
    var sx = (((Number(sleepH) || 23) % 24) / 24.0) * w;
    wakeLine.setAttribute("x1", wx.toFixed(1)); wakeLine.setAttribute("x2", wx.toFixed(1));
    wakeTag.setAttribute("x", Math.min(w - 34, Math.max(2, wx + 3)).toFixed(1));
    sleepLine.setAttribute("x1", sx.toFixed(1)); sleepLine.setAttribute("x2", sx.toFixed(1));
    sleepTag.setAttribute("x", Math.min(w - 38, Math.max(2, sx + 3)).toFixed(1));

    var svgNs = poly.namespaceURI;
    if (cctGrad) cctGrad.innerHTML = "";
    if (areaGrad) areaGrad.innerHTML = "";

    var pts = [];
    for (var i = 0; i < curve.length; i++) {
      var p = curve[i];
      var hClamped = Math.min(24.0, Math.max(0.0, Number(p.hour) || 0));
      var x = (hClamped / 24.0) * w;
      var pct = Math.max(0, Math.min(100, Number(p.monitor_brightness_pct) || 0));
      var y = botY - (pct / 100.0) * (botY - topY);
      pts.push(x.toFixed(1) + "," + y.toFixed(1));

      var stopPct = ((hClamped / 24.0) * 100.0).toFixed(2) + "%";
      var stopColor = cctToColor(p.monitor_cct_k || 4000);
      if (cctGrad && svgNs) {
        var st = document.createElementNS(svgNs, "stop");
        st.setAttribute("offset", stopPct);
        st.setAttribute("stop-color", stopColor);
        cctGrad.appendChild(st);
      }
      if (areaGrad && svgNs) {
        var ast = document.createElementNS(svgNs, "stop");
        ast.setAttribute("offset", stopPct);
        ast.setAttribute("stop-color", stopColor);
        ast.setAttribute("stop-opacity", "0.16");
        areaGrad.appendChild(ast);
      }
    }
    poly.setAttribute("points", pts.join(" "));
    if (pts.length >= 2) {
      area.setAttribute("d", "M 0," + botY + " L " + pts.join(" L ") + " L " + w + "," + botY + " Z");
    }

    if (curveScrubbing || Date.now() < curveScrubHoldUntil) {
      return;
    }

    var nowH = (curHour !== null && curHour !== undefined) ? Number(curHour) : (new Date().getHours() + new Date().getMinutes() / 60.0);
    var sampled = sampleCurveAtHour(curve, nowH);
    var nx = (nowH / 24.0) * w;
    var ny = botY - (Math.max(0, Math.min(100, sampled.pct)) / 100.0) * (botY - topY);
    var dispNits = (liveNits !== null && liveNits !== undefined && isFinite(liveNits) && liveNits > 0)
      ? Math.round(liveNits)
      : Math.round(sampled.nits);
    var dispCct = (liveCct !== null && liveCct !== undefined && isFinite(liveCct) && liveCct > 0)
      ? Math.round(liveCct)
      : Math.round(sampled.cct);

    var dotRgb = cctToRgb(dispCct);
    dot.setAttribute("cx", nx.toFixed(1));
    dot.setAttribute("cy", ny.toFixed(1));
    dot.setAttribute("fill", "rgb(" + dotRgb.r + "," + dotRgb.g + "," + dotRgb.b + ")");
    glow.setAttribute("cx", nx.toFixed(1));
    glow.setAttribute("cy", ny.toFixed(1));
    glow.setAttribute("fill", "rgba(" + dotRgb.r + "," + dotRgb.g + "," + dotRgb.b + ",0.30)");
    rangeEl.textContent = profileLabel + " | Now: " + hourToHHMM(nowH) + " (" + dispNits + " nits \u00b7 " + dispCct + "K)";
  }

  /* ---------- Interactive 5-Second 24-Hour Curve Scrubber ---------- */
  (function initCurveScrubber() {
    var svgEl = document.getElementById("curveSvg");
    var dot = document.getElementById("curveNowDot");
    var glow = document.getElementById("curveNowGlow");
    var rangeEl = document.getElementById("sparkRange");
    if (!svgEl) return;

    function applyCurveScrub(clientX, forcePost) {
      if (!latestCurve || latestCurve.length === 0) return;
      var rect = svgEl.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;
      var ratio = Math.max(0.0, Math.min(0.998, (clientX - rect.left) / rect.width));
      var hour = ratio * 24.0;
      var sPt = sampleCurveAtHour(latestCurve, hour);
      var w = 600, topY = 20, botY = 104;
      var nx = ratio * w;
      var ny = botY - (Math.max(0, Math.min(100, sPt.pct)) / 100.0) * (botY - topY);
      var briPct = Math.max(10, Math.min(100, Math.round(sPt.pct)));
      var cctK = Math.max(2200, Math.min(6500, Math.round(sPt.cct / 50) * 50));
      var rgb = cctToRgb(cctK);

      dot.setAttribute("cx", nx.toFixed(1));
      dot.setAttribute("cy", ny.toFixed(1));
      dot.setAttribute("fill", "rgb(" + rgb.r + "," + rgb.g + "," + rgb.b + ")");
      glow.setAttribute("cx", nx.toFixed(1));
      glow.setAttribute("cy", ny.toFixed(1));
      glow.setAttribute("fill", "rgba(" + rgb.r + "," + rgb.g + "," + rgb.b + ",0.32)");
      rangeEl.textContent = "Preview: " + hourToHHMM(hour) + " (" + Math.round(sPt.nits) + " nits \u00b7 " + cctK + "K)";

      var nowMs = Date.now();
      if (forcePost || (nowMs - curveLastPostMs) >= 100) {
        curveLastPostMs = nowMs;
        fetch("/api/override", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: true, brightness_pct: briPct, cct_k: cctK })
        }).catch(function() {});
      }
    }

    function finishCurveScrub(ev) {
      if (!curveScrubbing) return;
      curveScrubbing = false;
      if (ev && ev.pointerId !== undefined && svgEl.releasePointerCapture) {
        try { svgEl.releasePointerCapture(ev.pointerId); } catch (e) {}
      }
      curveScrubHoldUntil = Date.now() + 1700;
      if (curveScrubRevertTimer) clearTimeout(curveScrubRevertTimer);
      curveScrubRevertTimer = setTimeout(function() {
        curveScrubHoldUntil = 0;
        fetch("/api/override", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: false })
        }).then(function() {
          setMsg("Preview ended \u2014 returned to live curve.", false);
          poll();
        }).catch(function() {});
      }, 1500);
    }

    svgEl.addEventListener("pointerdown", function(ev) {
      if (!latestCurve || latestCurve.length === 0) return;
      curveScrubbing = true;
      if (curveScrubRevertTimer) {
        clearTimeout(curveScrubRevertTimer);
        curveScrubRevertTimer = null;
      }
      if (ev.pointerId !== undefined && svgEl.setPointerCapture) {
        try { svgEl.setPointerCapture(ev.pointerId); } catch (e) {}
      }
      applyCurveScrub(ev.clientX, true);
    });
    svgEl.addEventListener("pointermove", function(ev) {
      if (!curveScrubbing) return;
      applyCurveScrub(ev.clientX, false);
    });
    svgEl.addEventListener("pointerup", finishCurveScrub);
    svgEl.addEventListener("pointercancel", finishCurveScrub);
  })();

  function renderSparkline(history) {
    var poly = document.getElementById("sparkPoly");
    var rangeEl = document.getElementById("sparkRange");
    if (!history || history.length === 0) {
      poly.setAttribute("points", "");
      rangeEl.textContent = "Waiting for sensor ticks...";
      return;
    }
    var vals = history.map(function(p) { return Number(p.m_edi_lx) || 0; });
    var maxV = Math.max(10, Math.max.apply(null, vals));
    var w = 600, h = 110, padY = 12;
    var pts = vals.map(function(v, idx) {
      var x = vals.length === 1 ? w / 2 : (idx / (vals.length - 1)) * w;
      var y = padY + (1 - (v / (maxV || 1))) * (h - padY);
      return x.toFixed(1) + "," + y.toFixed(1);
    });
    poly.setAttribute("points", pts.join(" "));
    rangeEl.textContent = "Points: " + vals.length + " | Peak: " + maxV.toFixed(1) + " lx";
  }

  function makeConflictBanner(text, btnLabel, targetKey) {
    var div = document.createElement("div");
    div.className = "banner banner-amber show";
    div.style.marginBottom = "0";
    var row = document.createElement("div");
    row.className = "banner-row";
    var span = document.createElement("span");
    span.textContent = text;
    row.appendChild(span);
    if (btnLabel && targetKey) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "banner-act";
      btn.textContent = btnLabel;
      btn.addEventListener("click", function() {
        openSettings(targetKey, "Opened Windows Settings (" + btnLabel + ").");
      });
      row.appendChild(btn);
    }
    div.appendChild(row);
    return div;
  }

  function renderConflicts(s) {
    var box = document.getElementById("conflictsContainer");
    var obBox = document.getElementById("obConflictsBox");
    var lc = s.live_conflicts || {};
    var caps = s.capabilities || {};
    var rawWarnings = Array.isArray(lc.warnings) ? lc.warnings.slice() : [];
    var nightOn = Boolean(lc.night_light_on || caps.windows_night_light_detected === true);
    var filterOn = Boolean(lc.color_filter_on);

    var items = [];
    var hasNightWarning = false;
    var hasFilterWarning = false;

    rawWarnings.forEach(function(wText) {
      var low = String(wText || "").toLowerCase();
      if (nightOn && low.indexOf("night light") !== -1) {
        hasNightWarning = true;
        items.push({ text: wText, label: "Open Night Light Settings", target: "nightlight", obShow: true });
      } else if (filterOn && low.indexOf("color filter") !== -1) {
        hasFilterWarning = true;
        items.push({ text: wText, label: "Open Color Filter Settings", target: "colorfilter", obShow: true });
      } else {
        items.push({ text: wText, label: "", target: "", obShow: false });
      }
    });

    if (nightOn && !hasNightWarning) {
      items.unshift({
        text: "Windows Night Light is ON \u2014 turn it off in Windows Display Settings to prevent double-warming.",
        label: "Open Night Light Settings",
        target: "nightlight",
        obShow: true
      });
    }
    if (filterOn && !hasFilterWarning) {
      items.push({
        text: "Windows Color Filters are ON \u2014 disable them to avoid conflicting color matrices.",
        label: "Open Color Filter Settings",
        target: "colorfilter",
        obShow: true
      });
    }

    box.innerHTML = "";
    items.forEach(function(it) {
      box.appendChild(makeConflictBanner(it.text, it.label, it.target));
    });

    if (obBox) {
      obBox.innerHTML = "";
      items.forEach(function(it) {
        if (it.obShow) {
          obBox.appendChild(makeConflictBanner(it.text, it.label, it.target));
        }
      });
    }
  }

  /* ---------- Main State Hydrator ---------- */
  function renderState(s) {
    latestState = s;
    latestMaxNits = Number(s.effective_max_nits) || Number(s.detected_max_nits) || 300.0;
    latestGamma = Number(s.backlight_gamma) || 2.0;
    var isOpenLoop = (s.mode === "open_loop");
    var isMsix = Boolean(s.is_packaged_msix);
    var hdrPaused = Boolean(s.hdr_auto_paused || (s.live_conflicts && s.live_conflicts.hdr_active));
    serverOnboardingCompleted = (s.onboarding_completed !== false);

    if (!serverOnboardingCompleted && !obInitialized) {
      obInitialized = true;
      var wsInit = s.weekly_schedule || {};
      hydrateEditor(wsInit, s.wake_hour, s.sleep_hour, obUniformWake, obUniformSleep, obWdWake, obWdSleep, obWeWake, obWeSleep, obCustom.rows);
      setObMode(detectScheduleMode(wsInit));
      document.getElementById("obAutostart").checked = Boolean(s.start_with_windows);
    }
    var mdlStr = s.detected_panel_model ? " \u00b7 " + s.detected_panel_model : "";
    document.getElementById("obPanelPreview").textContent = Math.round(latestMaxNits) + " nit panel" + mdlStr;
    syncGateVisibility();

    renderConflicts(s);

    document.getElementById("modePill").textContent = (s.mode || "open_loop").toUpperCase();
    document.getElementById("phaseText").textContent = s.phase || "DAY";
    var trLabel = s.transition_label ? s.transition_label.replace(/_/g, " ") : "";
    document.getElementById("transitionText").textContent = trLabel ? ("\u00b7 " + trLabel) : "";

    var dot = document.getElementById("statusDot");
    var hdrPill = document.getElementById("hdrPill");
    var sourcePill = document.getElementById("sourcePill");
    var cornealHint = document.getElementById("cornealHint");
    var pausePill = document.getElementById("pausePill");
    var autoPausePill = document.getElementById("autoPausePill");
    var manualPill = document.getElementById("manualPill");
    var moviePill = document.getElementById("moviePill");
    var movieBtn = document.getElementById("movieBtn");
    var movieBtnTitle = document.getElementById("movieBtnTitle");
    var pauseCountdown = document.getElementById("pauseCountdown");

    if (hdrPill) hdrPill.style.display = hdrPaused ? "inline-flex" : "none";
    if (sourcePill) {
      sourcePill.textContent = isOpenLoop ? "Source: Clock / Solar Estimate" : "Source: USB-C V1A Sensor";
    }

    manualPill.style.display = s.manual_override_enabled ? "inline-flex" : "none";
    if (s.movie_mode_active) {
      var mMin = Math.max(1, Math.ceil((s.movie_mode_remaining_s || 0) / 60));
      moviePill.style.display = "inline-flex";
      moviePill.textContent = "MOVIE MODE (3400K \u00b7 " + mMin + "m)";
      movieBtnTitle.textContent = "Exit Movie";
      movieBtn.className = "quick-btn amber-active";
    } else {
      moviePill.style.display = "none";
      movieBtnTitle.textContent = "Movie 2.5h";
      movieBtn.className = "quick-btn";
    }

    if (s.paused) {
      var mins = Math.max(1, Math.ceil((s.pause_remaining_sec || 0) / 60));
      dot.className = "dot paused";
      pausePill.style.display = "inline-flex";
      pausePill.textContent = "PAUSED " + mins + "m";
      autoPausePill.style.display = "none";
      pauseCountdown.textContent = "Paused (" + mins + " min remaining \u2014 display restored)";
    } else if (s.auto_paused) {
      dot.className = "dot paused";
      pausePill.style.display = "none";
      autoPausePill.style.display = "inline-flex";
      autoPausePill.textContent = "AUTO-PAUSED: " + (s.auto_pause_reason || "App");
      pauseCountdown.textContent = "Auto-paused (" + (s.auto_pause_reason || "active app") + " \u2014 display restored)";
    } else if (hdrPaused) {
      dot.className = "dot paused";
      pausePill.style.display = "none";
      autoPausePill.style.display = "none";
      pauseCountdown.textContent = "HDR Active \u2014 color filter auto-paused to preserve native HDR tone mapping";
    } else {
      dot.className = s.unlinked_light_alert ? "dot alert" : "dot";
      pausePill.style.display = "none";
      autoPausePill.style.display = "none";
      pauseCountdown.textContent = "";
    }

    var unlinked = document.getElementById("unlinkedBanner");
    if (s.unlinked_light_alert) {
      unlinked.className = "banner banner-red show";
      document.getElementById("unlinkedMsg").textContent = s.alert_message || "Unlinked ambient light exceeds evening cap.";
    } else {
      unlinked.className = "banner banner-red";
    }

    var badge = document.getElementById("complianceBadge");
    var mediEl = document.getElementById("mediVal");
    var unitEl = document.getElementById("mediUnit");
    var curveSvg = document.getElementById("curveSvg");
    var sparkSvg = document.getElementById("sparkline");
    var graphTitle = document.getElementById("graphTitle");
    var openLoopFooter = document.getElementById("openLoopFooter");
    var sensorFooter = document.getElementById("sensorFooter");

    var liveNitsRaw = (s.target_display_nits !== undefined && s.target_display_nits !== null && Number(s.target_display_nits) > 0)
      ? Number(s.target_display_nits)
      : (Number(s.target_screen_nits) || Math.round(estNits(s.monitor_brightness_pct, latestMaxNits, latestGamma)));

    if (s.sensor_occluded || !s.reading_valid) {
      mediEl.textContent = "--.-";
      unitEl.textContent = "HELD / NOT LIVE";
      badge.textContent = "OCCLUDED";
      badge.className = "badge badge-amber";
    } else if (isOpenLoop || s.compliance_status === "OPEN-LOOP") {
      mediEl.textContent = Math.round(liveNitsRaw) + " nits";
      unitEl.textContent = "@ " + (s.monitor_cct_k || 5500) + "K";
      if (s.awaiting_setup) {
        badge.textContent = "AWAITING SETUP";
        badge.className = "badge badge-amber";
      } else if (hdrPaused) {
        badge.textContent = "HDR AUTO-PAUSED";
        badge.className = "badge badge-amber";
      } else if (s.manual_override_enabled) {
        badge.textContent = "MANUAL OVERRIDE";
        badge.className = "badge badge-amber";
      } else if (s.movie_mode_active) {
        badge.textContent = "MOVIE MODE (3400K)";
        badge.className = "badge badge-amber";
      } else {
        badge.textContent = (s.high_sensitivity_evening || !s.comfort_mode) ? "OPEN-LOOP \u00b7 STRICT" : "OPEN-LOOP \u00b7 COMFORT";
        badge.className = "badge badge-neutral";
      }
    } else {
      var u = Number(s.uncertainty_k2_lx) || 0;
      mediEl.textContent = (Number(s.m_edi_lx) || 0).toFixed(1) + (u > 0 ? " \u00b1" + u.toFixed(1) : "");
      unitEl.textContent = "lx m-EDI";
      badge.textContent = s.compliance_status || "BELIRSIZ";
      if (s.compliance_status === "SAGLANDI") badge.className = "badge badge-green";
      else if (s.compliance_status === "SAGLANMADI") badge.className = "badge badge-red";
      else badge.className = "badge badge-amber";
    }

    if (cornealHint) {
      var estMedi = Number(s.estimated_corneal_m_edi_lx) || 0;
      var diagIn = Number(s.diagonal_in) || 0;
      if (isOpenLoop && (estMedi > 0 || diagIn > 0)) {
        var cParts = [];
        if (estMedi > 0) cParts.push("Est. corneal ~" + estMedi.toFixed(1) + " lx m-EDI");
        if (diagIn > 0) cParts.push(diagIn.toFixed(1) + '"');
        cornealHint.textContent = cParts.join(" \u00b7 ");
      } else {
        cornealHint.textContent = "";
      }
    }

    var scrubHint = document.getElementById("curveScrubHint");
    if (isOpenLoop) {
      openLoopFooter.style.display = "flex";
      sensorFooter.className = "sensor-grid";
      curveSvg.style.display = "block";
      sparkSvg.style.display = "none";
      if (scrubHint) scrubHint.style.display = "inline";
      var dayTag = s.active_day_key ? " \u00b7 " + s.active_day_key.toUpperCase() : "";
      graphTitle.textContent = "24-Hour Circadian Curve (" + Math.round(latestMaxNits) + " nit" + dayTag + ")";
      var pLabel = s.movie_mode_active ? "Movie Mode (3400K)" : ((s.high_sensitivity_evening || !s.comfort_mode) ? "Strict Profile" : "Comfort Profile");
      var activeLiveNits = (!s.paused && !s.auto_paused && liveNitsRaw > 0) ? Math.round(liveNitsRaw) : null;
      var activeLiveCct = (!s.paused && !s.auto_paused && Number(s.monitor_cct_k) > 0) ? Math.round(Number(s.monitor_cct_k)) : null;
      render24hCurve(s.curve_24h || s.schedule_curve || [], s.current_hour, pLabel, s.wake_hour, s.sleep_hour, activeLiveNits, activeLiveCct);
    } else {
      openLoopFooter.style.display = "none";
      sensorFooter.className = "sensor-grid show";
      document.getElementById("photopicVal").textContent = (Number(s.photopic_lx) || 0).toFixed(1) + " lx";
      document.getElementById("cctVal").textContent = (Number(s.cct_k) || 0) + " K";
      document.getElementById("mderVal").textContent = (Number(s.m_der) || 0).toFixed(2);
      document.getElementById("flickerVal").textContent = (Number(s.flicker_hz) || 0).toFixed(0) + " Hz";
      curveSvg.style.display = "none";
      sparkSvg.style.display = "block";
      if (scrubHint) scrubHint.style.display = "none";
      graphTitle.textContent = "Recent m-EDI Sparkline (last 60 ticks)";
      renderSparkline(s.history || []);
    }

    document.getElementById("monitorVal").textContent = (s.monitor_brightness_pct || 0) + "% @ " + (s.monitor_cct_k || 5500) + "K";
    var caps = s.capabilities || {};
    var mChips = [];
    if (caps.wmi_laptop_available) mChips.push('<span class="chip active">WMI</span>');
    if (caps.ddc_external_supported) mChips.push('<span class="chip active">DDC/CI</span>');
    if (s.ddc_diagnostic && String(s.ddc_diagnostic).toLowerCase().indexOf("blocked") !== -1) {
      mChips.push('<span class="chip warn">DDC/CI blocked &mdash; check monitor OSD</span>');
    }
    if (s.color_method === "magnification") mChips.push('<span class="chip active">Magnification 5x5</span>');
    else if (s.color_method === "gdi_gamma") mChips.push('<span class="chip active">GDI 1D LUT</span>');
    else if (caps.magnification_available) mChips.push('<span class="chip">Magnification Ready</span>');
    else mChips.push('<span class="chip">Idle / Untouched</span>');
    document.getElementById("monitorChips").innerHTML = mChips.join("");

    var panelBadge = document.getElementById("panelBadge");
    var userMax = Number(s.display_max_nits) || 0;
    if (userMax > 0) {
      panelBadge.textContent = Math.round(userMax) + " nit \u00b7 Custom";
    } else if (s.detected_nits_source === "edid_cta861" || s.detected_nits_source === "dxgi_hdr") {
      var pMdl = s.detected_panel_model ? ": " + s.detected_panel_model : "";
      panelBadge.textContent = Math.round(Number(s.detected_max_nits) || 300) + " nit \u00b7 Auto" + pMdl;
    } else {
      panelBadge.textContent = Math.round(Number(s.detected_max_nits) || 300) + " nit \u00b7 Est. SDR";
    }
    Array.prototype.forEach.call(document.querySelectorAll(".nits-btn"), function(btn) {
      var bVal = parseFloat(btn.getAttribute("data-nits"));
      var isAct = (bVal === 0 && userMax === 0) || (bVal > 0 && Math.abs(userMax - bVal) < 1);
      btn.className = isAct ? "nits-btn active" : "nits-btn";
    });
    var activePanelType = String(s.panel_type || "auto").toLowerCase();
    var autoPanelName = PANEL_LABELS[String(s.auto_panel_type || "wled_ips").toLowerCase()] || "W-LED";
    Array.prototype.forEach.call(document.querySelectorAll(".panel-btn"), function(btn) {
      var pVal = btn.getAttribute("data-panel");
      btn.className = (pVal === activePanelType) ? "panel-btn active" : "panel-btn";
      if (pVal === "auto") {
        btn.title = "Auto-detected from EDID (" + autoPanelName + ")";
      }
    });

    if (s.light_backend === "none") {
      document.getElementById("bulbVal").textContent = "No Bulb Linked";
      document.getElementById("bulbChips").innerHTML = '<span class="chip">Display-Only Mode</span>';
    } else {
      var bVal = s.bulb_power && (s.bulb_brightness_pct || 0) > 0
        ? (s.bulb_brightness_pct + "% @ " + (s.bulb_cct_k || 2300) + "K")
        : "OFF [u=0]";
      document.getElementById("bulbVal").textContent = bVal;
      var bChips = [];
      if (s.conflict_observer_active) bChips.push('<span class="chip warn">Conflict-Observer 10m</span>');
      bChips.push('<span class="chip">' + (s.bulb_power ? "Power ON" : "Power OFF") + '</span>');
      document.getElementById("bulbChips").innerHTML = bChips.join("");
    }

    if (!sliderDragging) {
      briSlider.value = s.manual_override_enabled ? s.manual_brightness_pct : (s.monitor_brightness_pct || 70);
      cctSlider.value = s.manual_override_enabled ? s.manual_cct_k : (s.monitor_cct_k || 4500);
      updateSliderLabels();
    }

    sensEl.checked = Boolean(s.high_sensitivity_evening || s.comfort_mode === false);
    autoPauseEl.checked = s.auto_pause_enabled !== false;
    browserFsEl.checked = Boolean(s.auto_pause_browser_fullscreen);
    autostartEl.checked = Boolean(s.start_with_windows);

    var autostartSub = document.getElementById("autostartSub");
    var obAutostartText = document.getElementById("obAutostartText");
    if (autostartSub) {
      autostartSub.textContent = isMsix
        ? "Managed in Windows Startup Apps (MSIX)"
        : "Launch quietly to System Tray at Windows sign-in";
    }
    if (obAutostartText) {
      obAutostartText.textContent = isMsix
        ? "Manage Windows Startup Apps (MSIX)"
        : "Start automatically with Windows";
    }

    if (!formDirty) {
      var ws = s.weekly_schedule || {};
      hydrateEditor(ws, s.wake_hour, s.sleep_hour, dashUniformWake, dashUniformSleep, dashWdWake, dashWdSleep, dashWeWake, dashWeSleep, dashCustom.rows);
      setDashMode(detectScheduleMode(ws), false);
      document.getElementById("dashUniformDur").textContent = formatAwakeDuration(s.wake_hour, s.sleep_hour, false);
    }

    var jlEl = document.getElementById("jetlagNote");
    var jl = Number(s.social_jetlag_hours) || 0;
    if (jl >= 2.0) {
      jlEl.textContent = "Social jetlag: " + jl.toFixed(1) + "h shift between weekday & weekend sleep midpoints (keeping <=1.5h anchors rhythm).";
      jlEl.className = "jetlag-note show";
    } else {
      jlEl.className = "jetlag-note";
    }
  }

  function poll() {
    fetch("/api/state", { cache: "no-store" })
      .then(function(r) { return r.json(); })
      .then(renderState)
      .catch(function() {});
  }

  poll();
  setInterval(poll, 2000);
})();
</script>
</body>
</html>
"""

_NO_REPORT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Circadian Desk — 7-Day Report</title>
<style>
  body {
    margin: 0; padding: 36px 20px; background: #F4F1EA; color: #1A1A1A;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  .card {
    max-width: 640px; margin: 0 auto; background: #FAF8F4;
    border: 1px solid #DCD7CC; border-radius: 10px; padding: 24px 26px;
  }
  h1 { margin: 0 0 10px; font-size: 18px; letter-spacing: 0.04em; }
  p { font-size: 14px; color: #4A4740; line-height: 1.55; }
  pre {
    background: #1A1A1A; color: #F4F1EA; padding: 12px 14px;
    border-radius: 6px; overflow-x: auto; font-size: 12px;
  }
  a {
    display: inline-block; margin-top: 12px; padding: 8px 14px;
    border: 1px solid #1A1A1A; border-radius: 6px; color: #1A1A1A;
    text-decoration: none; font-size: 13px; font-weight: 600;
  }
  a:hover { background: #1A1A1A; color: #F4F1EA; }
</style>
</head>
<body>
  <div class="card">
    <h1>7-Day Compliance Report Not Yet Logged</h1>
    <p>No telemetry CSV file is active yet for this session. Start Circadian Desk with <code>--log-csv</code> (or launch Option 3 from <code>launch_beta.bat</code>) to record local UTF-8 telemetry and view your offline Braun SVG compliance report here:</p>
    <pre>circadian-desk --open-loop --light none --monitor --web-ui --log-csv desk_log.csv</pre>
    <a href="/">&larr; Back to Control Panel</a>
  </div>
</body>
</html>
"""


class _LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler_class(
    state: OrchestratorState,
    open_settings_fn: Callable[[str], tuple[bool, str]] | None = None,
) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug("LocalWebUI %s - " + format, self.address_string(), *args)

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, raw, "application/json; charset=utf-8")

        def _check_post_csrf(self) -> bool:
            host_hdr = self.headers.get("Host")
            origin_hdr = self.headers.get("Origin")
            if not _is_loopback_host_header(host_hdr) or not _is_loopback_origin_header(origin_hdr):
                self._send_json(
                    403,
                    {"error": "Forbidden: POST requires loopback Host and Origin headers"},
                )
                return False
            return True

        def _read_json_body(self) -> dict[str, Any] | None:
            raw_len = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_len)
            except ValueError:
                self._send_json(400, {"error": "Invalid Content-Length"})
                return None
            if length < 0 or length > MAX_REQUEST_BODY_BYTES:
                self._send_json(400, {"error": "Request body size out of bounds"})
                return None
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "Invalid JSON body"})
                return None
            if not isinstance(parsed, dict):
                self._send_json(400, {"error": "JSON body must be an object"})
                return None
            return parsed

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send_bytes(
                    200,
                    _DASHBOARD_HTML.encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return

            if path == "/api/state":
                self._send_json(200, state.to_dict())
                return

            if path == "/report":
                csv_raw = state.csv_path
                csv_file = Path(csv_raw).expanduser() if csv_raw else None
                if csv_file is not None and csv_file.is_file() and csv_file.stat().st_size > 0:
                    try:
                        from orchestrator.telemetry import generate_compliance_report

                        with tempfile.TemporaryDirectory() as tmp_dir:
                            summary = generate_compliance_report(
                                csv_path=csv_file,
                                output_dir=tmp_dir,
                            )
                            html_path = Path(str(summary["html_path"]))
                            html_bytes = html_path.read_bytes()
                        self._send_bytes(200, html_bytes, "text/html; charset=utf-8")
                        return
                    except Exception as exc:
                        logger.warning("Failed to render /report from %s: %s", csv_file, exc)
                self._send_bytes(
                    200,
                    _NO_REPORT_HTML.encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return

            self._send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._check_post_csrf():
                return

            path = urlparse(self.path).path
            body = self._read_json_body()
            if body is None:
                return

            if path == "/api/pause":
                if "minutes" not in body:
                    self._send_json(400, {"error": "Missing required field: minutes"})
                    return
                try:
                    result = state.set_pause(body["minutes"])
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, result)
                return

            if path == "/api/movie":
                try:
                    dur = body.get("duration_s", DEFAULT_MOVIE_MODE_DURATION_S)
                    if body.get("toggle") is True:
                        result = state.toggle_movie_mode(duration_s=dur)
                    elif "enabled" in body:
                        if not isinstance(body["enabled"], bool):
                            raise ValueError("enabled must be a boolean")
                        result = (
                            state.activate_movie_mode(duration_s=dur)
                            if body["enabled"]
                            else state.clear_movie_mode()
                        )
                    else:
                        raise ValueError("Provide 'enabled' (bool) or 'toggle': true")
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, result)
                return

            if path == "/api/override":
                if "enabled" not in body:
                    self._send_json(400, {"error": "Missing required field: enabled"})
                    return
                try:
                    result = state.set_manual_override(
                        enabled=body["enabled"],
                        brightness_pct=body.get("brightness_pct"),
                        cct_k=body.get("cct_k"),
                    )
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, result)
                return

            if path in ("/api/settings", "/api/onboarding"):
                ob_flag = body.get("onboarding_completed")
                if path == "/api/onboarding" and ob_flag is None:
                    ob_flag = True
                try:
                    result = state.update_settings(
                        wake_hour=body.get("wake_hour"),
                        sleep_hour=body.get("sleep_hour"),
                        high_sensitivity_evening=body.get("high_sensitivity_evening"),
                        auto_pause_enabled=body.get("auto_pause_enabled"),
                        display_max_nits=body.get("display_max_nits"),
                        comfort_mode=body.get("comfort_mode"),
                        weekly_schedule=body.get("weekly_schedule"),
                        onboarding_completed=ob_flag,
                        auto_pause_browser_fullscreen=body.get("auto_pause_browser_fullscreen"),
                        start_with_windows=body.get("start_with_windows"),
                        panel_type=body.get("panel_type"),
                    )
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, result)
                return

            if path == "/api/open-settings":
                target = body.get("target")
                if not isinstance(target, str):
                    self._send_json(
                        400,
                        {
                            "error": "Invalid target; allowed: colorfilter, display, nightlight, startupapps"
                        },
                    )
                    return
                target_norm = target.strip().lower()
                if target_norm not in ALLOWED_SETTINGS_TARGETS:
                    self._send_json(
                        400,
                        {
                            "error": f"Unknown settings target {target!r}; allowed: "
                            + ", ".join(sorted(ALLOWED_SETTINGS_TARGETS))
                        },
                    )
                    return
                opener = open_settings_fn
                if opener is None:
                    from orchestrator.display.windows_display import open_windows_settings_uri

                    opener = open_windows_settings_uri
                try:
                    ok, detail = opener(target_norm)
                except Exception as exc:
                    self._send_json(500, {"error": f"Failed to open settings: {exc}"})
                    return
                self._send_json(
                    200,
                    {"opened": bool(ok), "target": target_norm, "uri": str(detail)},
                )
                return

            self._send_json(404, {"error": "Not found"})

    return _Handler


class LocalWebUIServer:
    """Strictly loopback (`127.0.0.1`) threaded HTTP server for the Braun Control Panel."""

    def __init__(
        self,
        state: OrchestratorState,
        host: str = "127.0.0.1",
        port: int = 8765,
        open_settings_fn: Callable[[str], tuple[bool, str]] | None = None,
    ) -> None:
        host_norm = str(host).strip().lower()
        if host_norm not in ALLOWED_LOOPBACK_HOSTS:
            raise ValueError(
                f"LocalWebUIServer refuses non-loopback host {host!r}; "
                "only '127.0.0.1' or 'localhost' is permitted."
            )
        self.state = state
        self._bind_host = "127.0.0.1"
        self._httpd = _LoopbackHTTPServer(
            (self._bind_host, int(port)),
            _make_handler_class(state, open_settings_fn=open_settings_fn),
        )
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def actual_port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.actual_port}"

    def start(self) -> None:
        """Start serving in a daemon thread (idempotent)."""
        if self._closed or (self._thread is not None and self._thread.is_alive()):
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="CircadianLocalWebUI",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Cleanly shut down and close the listening socket (idempotent)."""
        if self._closed:
            return
        self._closed = True
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        try:
            self._httpd.server_close()
        except Exception:
            pass

    def __enter__(self) -> LocalWebUIServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
