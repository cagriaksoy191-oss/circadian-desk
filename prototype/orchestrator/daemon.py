"""Local multi-source circadian lighting orchestrator CLI daemon (Pure Track A v6.1).

Run directly with zero pip dependencies and ZERO hardware in Week-1 Open-Loop Desktop Beta:
    python -m orchestrator.daemon --setup
    python -m orchestrator.daemon --config --open-loop --monitor
    python -m orchestrator.daemon --open-loop --light none --monitor

Or run simulation scenarios & generate 7-day compliance reports:
    python -m orchestrator.daemon --simulate day_to_evening_demo --light mock --ticks 6 --interval 0.2 --log-csv demo.csv
    python -m orchestrator.daemon --report demo.csv

v6.1 Software Milestone 1 Hardening:
- Week-1 `--open-loop` zero-sensor mode with smooth C1 sunrise/sunset curve, `--light none`
  display-only driver, native Windows laptop WMI backlight, external DDC/CI, zero-admin
  `Magnification.dll` 5x5 GPU color matrix fallback, and read-only Windows Night Light check.
- Persistent JSON config (`--config`, `--setup` 30-second wizard), CSV telemetry (`--log-csv`),
  and offline Markdown + SVG/HTML compliance reporting (`--report`).
- Controlled `--monitor --monitor-safety-check` 3-second backup/preview/restore verifier.
- BUG-3: `CommandDispatcher` deduplication + 10-min Conflict-Observer backoff.
- BUG-2: Live phase- and alert-aware `SleepHandoffGuard` synchronization.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import inspect
import logging
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from orchestrator.config import (
    DEFAULT_CONFIG_PATH,
    UserConfig,
    get_app_data_dir,
    load_config,
    save_config,
)
from orchestrator.display.windows_display import (
    SleepHandoffGuard,
    WindowsDisplayController,
    detect_display_luminance_profile,
    detect_live_display_conflicts,
    detect_windows_dark_mode,
    enable_per_monitor_dpi_awareness,
    get_windows_autostart_enabled,
    is_packaged_msix,
    set_windows_autostart_enabled,
)
from orchestrator.lights.base import LightDriver
from orchestrator.lights.hue_v2 import HueV2Driver
from orchestrator.lights.mock_bulb import MockBulbDriver
from orchestrator.lights.none_bulb import NoneLightDriver
from orchestrator.lights.wiz_udp import WizUdpDriver, discover_wiz_bulbs
from orchestrator.serial_reader import (
    SensorSource,
    SerialSensorReader,
    SimulatedSensorReader,
)
from orchestrator.web_ui import LocalWebUIServer, OrchestratorState
from spectral import photobiology
from spectral.photobiology import (
    AS7343Converter,
    CircadianController,
    ControlAction,
    PhotometricReading,
    RoomProfile,
)

logger = logging.getLogger("orchestrator")

APP_VERSION: str = "1.0.0"
SINGLE_INSTANCE_MUTEX_NAME: str = "Local\\CircadianDesk_SingleInstance_v1"
APP_LOG_MAX_BYTES: int = 2 * 1024 * 1024  # 2 MB cap (Guardrail G3)

SCENARIOS: tuple[str, ...] = (
    "overcast_day",
    "bright_window_harvest",
    "evening_monitor_glare",
    "evening_unlinked_neon",
    "hand_shadow_test",
    "day_to_evening_demo",
)

# Default simulated wall-clock hours when --hour is omitted during --simulate runs.
SCENARIO_DEFAULT_HOURS: dict[str, float] = {
    "overcast_day": 10.5,
    "bright_window_harvest": 12.0,
    "evening_monitor_glare": 21.5,
    "evening_unlinked_neon": 21.5,
    "hand_shadow_test": 11.0,
}


def _supports_ansi() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout is not None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _ensure_noconsole_streams(log_path: Path | None = None) -> Path | None:
    """Redirect `None` stdout/stderr (PyInstaller `--noconsole`) safely to `app.log` (capped <= 2 MB).

    Prevents `AttributeError: 'NoneType' object has no attribute 'write'` whenever
    `CircadianDesk.exe` is launched without a Windows console window, and routes logs into
    `%LOCALAPPDATA%\\CircadianDesk\\app.log` (Guardrail G3 / Store Policy 10.2.7).
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    target = log_path if log_path is not None else (get_app_data_dir() / "app.log")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.is_file() and target.stat().st_size > APP_LOG_MAX_BYTES:
            rotated = target.with_suffix(".log.1")
            try:
                rotated.unlink(missing_ok=True)
            except OSError:
                pass
            target.replace(rotated)
    except OSError:
        pass
    stream = target.open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return target


def acquire_single_instance_mutex(name: str = SINGLE_INSTANCE_MUTEX_NAME) -> tuple[Any, bool]:
    """Acquire a session-scoped Win32 Named Mutex (`Local\\...`, zero admin).

    Returns `(handle, already_running)`. Non-Windows returns `(None, False)`.
    """
    if sys.platform != "win32":
        return None, False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, name)
        last_err = ctypes.get_last_error()
        if not handle:
            return None, False
        ERROR_ALREADY_EXISTS = 183
        return handle, (last_err == ERROR_ALREADY_EXISTS)
    except Exception as exc:
        logger.debug("Single-instance mutex unavailable: %s", exc)
        return None, False


def release_single_instance_mutex(handle: Any) -> None:
    """Close an acquired Win32 mutex handle cleanly."""
    if not handle or sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)
    except Exception:
        pass


def resolve_default_argv(argv: Sequence[str] | None) -> Sequence[str] | None:
    """Return zero-friction Desktop App defaults when frozen `CircadianDesk.exe` is launched.

    - Zero-arg interactive launch (Start Menu / Explorer double-click): opens `127.0.0.1:8765`
      in the browser (`--open-browser`, satisfying Microsoft Store Policy 10.1.2).
    - Windows startup (`--background`): launches quietly to System Tray WITHOUT popping open
      a browser window!
    """
    if argv is None and getattr(sys, "frozen", False):
        raw_args = list(sys.argv[1:])
        is_zero_arg = len(raw_args) == 0
        is_background_only = raw_args == ["--background"]
        if is_zero_arg or is_background_only:
            app_dir = get_app_data_dir()
            app_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                "--config",
                "--open-loop",
                "--monitor",
                "--light",
                "none",
                "--web-ui",
                "--tray",
                "--auto-pause",
                "--log-csv",
                str(app_dir / "desk_log.csv"),
            ]
            if is_background_only:
                cmd.append("--background")
            else:
                cmd.append("--open-browser")
            return cmd
    return argv


def _format_hour(hour: float | None) -> str:
    if hour is None:
        return time.strftime("%H:%M")
    h = int(hour) % 24
    m = int(round((hour - int(hour)) * 60)) % 60
    return f"{h:02d}:{m:02d}"


def _call_smooth_open_loop_targets(
    *,
    hour_of_day: float,
    wake_hour: float,
    sleep_hour: float,
    high_sensitivity_evening: bool,
    comfort_mode: bool = True,
    display_max_nits: float = 300.0,
    backlight_gamma: float = 2.0,
    panel_type: str = "wled_ips",
    solid_angle_sr: float | None = None,
    dark_mode: bool | None = None,
    compensate_lut: bool = True,
) -> dict[str, Any] | None:
    smooth_fn = getattr(photobiology, "compute_smooth_open_loop_targets", None)
    if not callable(smooth_fn):
        return None
    kwargs: dict[str, Any] = {
        "hour_of_day": hour_of_day,
        "wake_hour": wake_hour,
        "sleep_hour": sleep_hour,
        "high_sensitivity_evening": high_sensitivity_evening,
    }
    params = inspect.signature(smooth_fn).parameters
    if "comfort_mode" in params:
        kwargs["comfort_mode"] = comfort_mode
    if "display_max_nits" in params:
        kwargs["display_max_nits"] = display_max_nits
    if "backlight_gamma" in params:
        kwargs["backlight_gamma"] = backlight_gamma
    if "panel_type" in params:
        kwargs["panel_type"] = panel_type
    if "solid_angle_sr" in params and solid_angle_sr is not None:
        kwargs["solid_angle_sr"] = solid_angle_sr
    if "dark_mode" in params and dark_mode is not None:
        kwargs["dark_mode"] = dark_mode
    if "compensate_lut" in params:
        kwargs["compensate_lut"] = compensate_lut
    return smooth_fn(**kwargs)


def compute_open_loop_action(
    hour_of_day: float | None = None,
    wake_hour: float = 7.0,
    sleep_hour: float = 23.0,
    high_sensitivity_evening: bool = False,
    comfort_mode: bool = True,
    display_max_nits: float = 300.0,
    backlight_gamma: float = 2.0,
    panel_type: str = "wled_ips",
    solid_angle_sr: float | None = None,
    dark_mode: bool | None = None,
    compensate_lut: bool = True,
) -> tuple[PhotometricReading, ControlAction, float]:
    """Compute a zero-sensor Brown et al. (2022) smooth wall-clock open-loop schedule step.

    Delegates to `spectral.photobiology.compute_smooth_open_loop_targets(...)` for nit-normalized
    MIRED-continuous sunrise, midday solar arch, afternoon warm-down, sunset, and night ramps.
    """
    if hour_of_day is None:
        now = datetime.now()
        hour = now.hour + (now.minute / 60.0)
    else:
        hour = float(hour_of_day) % 24.0

    targets = _call_smooth_open_loop_targets(
        hour_of_day=hour,
        wake_hour=wake_hour,
        sleep_hour=sleep_hour,
        high_sensitivity_evening=high_sensitivity_evening,
        comfort_mode=comfort_mode,
        display_max_nits=display_max_nits,
        backlight_gamma=backlight_gamma,
        panel_type=panel_type,
        solid_angle_sr=solid_angle_sr,
        dark_mode=dark_mode,
        compensate_lut=compensate_lut,
    )
    if targets is not None:
        phase = str(targets["phase"])
        target_lx = float(targets["target_m_edi_lx"])
        mon_pct = int(targets["monitor_brightness_pct"])
        mon_cct = int(targets["monitor_cct_k"])
        bulb_power = bool(targets.get("bulb_power", int(targets["bulb_brightness_pct"]) > 0))
        bulb_pct = int(targets["bulb_brightness_pct"]) if bulb_power else 0
        bulb_cct = int(targets["bulb_cct_k"])
    else:
        awake_span = (sleep_hour - wake_hour) % 24.0
        if awake_span <= 0.0:
            awake_span = 16.0
        day_span = max(1.0, awake_span - 3.0)
        hours_since_wake = (hour - wake_hour) % 24.0

        if hours_since_wake < day_span:
            phase = "DAY"
            target_lx = 250.0
            mon_pct, mon_cct = 80, 5500
            bulb_pct, bulb_cct = 100, 5000
            bulb_power = True
        elif hours_since_wake < awake_span:
            phase = "EVENING"
            target_lx = 5.0 if high_sensitivity_evening else 10.0
            mon_pct, mon_cct = (15, 2200) if high_sensitivity_evening else (20, 2300)
            bulb_pct, bulb_cct = (8, 2200) if high_sensitivity_evening else (12, 2300)
            bulb_power = True
        else:
            phase = "NIGHT"
            target_lx = 1.0
            mon_pct, mon_cct = 10, 2200
            bulb_pct, bulb_cct = 0, 2200
            bulb_power = False

    ts = time.time()
    reading = PhotometricReading(
        timestamp=ts,
        m_edi_lx=0.0,
        photopic_lx=0.0,
        cct_k=float(mon_cct),
        m_der=0.0,
        flicker_hz=0.0,
    )
    action_kwargs: dict[str, Any] = {
        "phase": phase,
        "target_m_edi_lx": target_lx,
        "measured_m_edi_lx": 0.0,
        "filtered_m_edi_lx": 0.0,
        "bulb_brightness_pct": bulb_pct,
        "bulb_cct_k": bulb_cct,
        "monitor_brightness_pct": mon_pct,
        "monitor_cct_k": mon_cct,
        "shadow_rejected": False,
        "unlinked_light_alert": False,
        "alert_message": "",
    }
    action_fields = getattr(ControlAction, "__dataclass_fields__", {})
    if "bulb_power" in action_fields:
        action_kwargs["bulb_power"] = bulb_power
    if "compliance_status" in action_fields:
        action_kwargs["compliance_status"] = "OPEN-LOOP"
    if "reading_valid" in action_fields:
        action_kwargs["reading_valid"] = True
    return reading, ControlAction(**action_kwargs), hour


class CommandDispatcher:
    """Deduplicates outgoing bulb/display commands and detects external bulb overrides.

    Prevents Hue CLIP v2 1 req/s HTTP 429/503 spam and monitor DDC/CI NVRAM wear (BUG-3),
    and defers to manual wall switches / Apple Adaptive Lighting / Hue Natural Light via
    a 10-minute Conflict-Observer backoff instead of fighting the user.
    """

    def __init__(
        self,
        light: LightDriver,
        display: WindowsDisplayController | None = None,
        monitor_enabled: bool = False,
        transition_ms: int = 1000,
    ) -> None:
        self.light = light
        self.display = display
        self.monitor_enabled = monitor_enabled
        self.transition_ms = transition_ms
        self._last_bulb_cmd: tuple[bool, int, int] | None = None
        self._last_monitor_cmd: tuple[int, int] | None = None
        self._conflict_logged: bool = False
        self._conflict_until_s: float = 0.0
        self.last_color_method: str = "none"

    def reset_cache(self) -> None:
        """Clear cached bulb/display setpoints so resuming from pause re-applies immediately."""
        self._last_bulb_cmd = None
        self._last_monitor_cmd = None

    def check_external_conflict(self, controller: Any | None = None) -> bool:
        """Poll `light.get_state()` and compare against our last sent command.

        MUST compare against `self._last_bulb_cmd` (never against a not-yet-sent
        next action) so normal controller transitions never self-trigger backoff.
        """
        now_s = time.monotonic()
        if now_s < self._conflict_until_s:
            return True

        if self._last_bulb_cmd is None or not hasattr(self.light, "get_state"):
            return False

        try:
            ext_state = self.light.get_state()
        except Exception as exc:
            logger.debug("light.get_state() failed: %s", exc)
            return False

        if not isinstance(ext_state, dict):
            return False

        ext_power = bool(ext_state.get("power", False))
        ext_pct = int(ext_state.get("brightness_pct", 0))
        ext_cct = int(ext_state.get("cct_k", 2700))

        last_power, last_pct, last_cct = self._last_bulb_cmd
        diverged = (
            ext_power != last_power
            or (ext_power and (abs(ext_pct - last_pct) >= 5 or abs(ext_cct - last_cct) >= 150))
        )
        if not diverged:
            return False

        if controller is not None and hasattr(controller, "notify_external_bulb_state"):
            try:
                return bool(
                    controller.notify_external_bulb_state(
                        bulb_power=ext_power,
                        bulb_brightness_pct=ext_pct,
                        bulb_cct_k=ext_cct,
                    )
                )
            except Exception as exc:
                logger.debug("notify_external_bulb_state() failed: %s", exc)

        self._conflict_until_s = now_s + 600.0
        return True

    def dispatch(
        self,
        action: ControlAction,
        controller: Any | None = None,
    ) -> dict[str, bool]:
        bulb_power = bool(getattr(action, "bulb_power", action.bulb_brightness_pct > 0))
        bulb_cmd = (bulb_power, int(action.bulb_brightness_pct), int(action.bulb_cct_k))
        mon_cmd = (int(action.monitor_brightness_pct), int(action.monitor_cct_k))

        conflict_active = bool(getattr(action, "conflict_observer_active", False))
        if not conflict_active and self.check_external_conflict(controller=controller):
            conflict_active = True

        bulb_sent = False
        if conflict_active:
            if not self._conflict_logged:
                self._conflict_logged = True
                logger.warning(
                    "External bulb change detected — entering 10-min Conflict-Observer backoff. "
                    "Tip: Disable Apple Adaptive Lighting / Hue Natural Light on this bulb if unintended."
                )
        else:
            self._conflict_logged = False
            if bulb_cmd != self._last_bulb_cmd:
                self.light.set_state(
                    brightness_pct=action.bulb_brightness_pct,
                    cct_k=action.bulb_cct_k,
                    transition_ms=self.transition_ms,
                )
                self._last_bulb_cmd = bulb_cmd
                bulb_sent = True

        monitor_sent = False
        if self.monitor_enabled and self.display is not None:
            if mon_cmd != self._last_monitor_cmd:
                apply_res = self.display.apply(
                    brightness_pct=action.monitor_brightness_pct,
                    cct_k=action.monitor_cct_k,
                )
                if isinstance(apply_res, dict):
                    self.last_color_method = str(apply_res.get("color_method", "none"))
                self._last_monitor_cmd = mon_cmd
                monitor_sent = True

        return {
            "bulb_sent": bulb_sent,
            "monitor_sent": monitor_sent,
            "conflict_observer_active": conflict_active,
        }


def format_hud_line(
    tick: int,
    reading: PhotometricReading,
    action: ControlAction,
    hour_of_day: float | None,
    use_color: bool = False,
    conflict_override: bool = False,
    pause_remaining_s: int = 0,
    auto_pause_reason: str = "",
    movie_mode_remaining_s: int = 0,
) -> str:
    """Format a crisp v6.1 terminal telemetry readout for one orchestrator tick."""
    reset = "\033[0m" if use_color else ""
    bold = "\033[1m" if use_color else ""
    cyan = "\033[36m" if use_color else ""
    amber = "\033[33m" if use_color else ""
    green = "\033[32m" if use_color else ""
    red_bg = "\033[43;30;1m" if use_color else ""
    dim = "\033[2m" if use_color else ""

    phase_color = cyan if action.phase == "DAY" else amber
    lamp_str = (
        "OFF [u=0]"
        if action.bulb_brightness_pct <= 0
        else f"{action.bulb_brightness_pct:3d}% @ {action.bulb_cct_k}K"
    )
    mon_str = f"{action.monitor_brightness_pct:3d}% @ {action.monitor_cct_k}K"

    status = str(getattr(action, "compliance_status", "BELIRSIZ")).upper()
    u_k2 = float(
        getattr(action, "uncertainty_k2_lx", getattr(reading, "uncertainty_k2_lx", 0.0))
    )
    reading_valid = bool(getattr(action, "reading_valid", True))
    sensor_occluded = bool(getattr(action, "sensor_occluded", False))
    saturated = bool(getattr(reading, "saturated", False))
    below_floor = bool(getattr(reading, "below_floor", False))
    conflict_active = bool(getattr(action, "conflict_observer_active", False)) or conflict_override

    status_color = green if status == "SAGLANDI" else (cyan if status == "OPEN-LOOP" else amber)

    tags: list[str] = []
    if action.shadow_rejected:
        tags.append(f"{amber}[SHADOW REJECTED]{reset}")
    if sensor_occluded:
        tags.append(f"{amber}[SENSOR OCCLUDED — STALE NOT LIVE]{reset}")
    elif (saturated or not reading_valid) and status != "OPEN-LOOP":
        tags.append(f"{amber}[READING INVALID / SATURATED]{reset}")
    if below_floor:
        tags.append(f"{dim}[<0.3 lx NIGHT FLOOR]{reset}")
    if conflict_active:
        tags.append(f"{amber}[CONFLICT-OBSERVER 10m]{reset}")
    if pause_remaining_s > 0:
        pause_min = max(1, (int(pause_remaining_s) + 59) // 60)
        tags.append(f"{amber}[PAUSED {pause_min}m]{reset}")
    elif auto_pause_reason:
        tags.append(f"{amber}[AUTO-PAUSED: {auto_pause_reason}]{reset}")
    elif movie_mode_remaining_s > 0:
        movie_min = max(1, (int(movie_mode_remaining_s) + 59) // 60)
        tags.append(f"{amber}[MOVIE MODE {movie_min}m]{reset}")
    tag_str = (" " + " ".join(tags)) if tags else ""

    if status == "OPEN-LOOP":
        header_metrics = (
            f"{status_color}[OPEN-LOOP | No Sensor]{reset} | "
            f"Target: {bold}{action.target_m_edi_lx:5.1f} lx m-EDI{reset}"
        )
    else:
        if not reading_valid or sensor_occluded:
            m_edi_segment = (
                f"m-EDI: {amber}{bold}--.- (HELD/INVALID){reset} "
                f"(tgt {action.target_m_edi_lx:5.1f}) "
                f"{status_color}[{status}]{reset}"
            )
        else:
            u_str = f" ±{u_k2:4.1f}" if u_k2 > 0.0 else ""
            m_edi_segment = (
                f"m-EDI: {bold}{action.measured_m_edi_lx:6.1f}{reset}{u_str} lx "
                f"(filt {action.filtered_m_edi_lx:5.1f} / tgt {action.target_m_edi_lx:5.1f}) "
                f"{status_color}[{status}]{reset}"
            )
        header_metrics = (
            f"{m_edi_segment} | "
            f"Lux: {reading.photopic_lx:6.1f} | "
            f"CCT: {reading.cct_k:5.0f}K | "
            f"m-DER: {reading.m_der:0.2f}"
        )

    lines = [
        (
            f"{dim}[#{tick:03d} {_format_hour(hour_of_day)}]{reset} "
            f"{phase_color}{bold}{action.phase:<7}{reset} | "
            f"{header_metrics}"
        ),
        (
            f"           -> Bulb: {bold}{lamp_str:<15}{reset} | "
            f"Monitor: {bold}{mon_str:<13}{reset}{tag_str}"
        ),
    ]

    if action.unlinked_light_alert:
        msg = action.alert_message or "Unlinked Light — Switch Off"
        lines.append(
            f"  {red_bg} [!] UNLINKED LIGHT — SWITCH OFF {reset} "
            f"{amber}{msg}{reset}"
        )

    return "\n".join(lines)


def build_light_driver(args: argparse.Namespace) -> LightDriver:
    """Instantiate the selected local smart-light driver."""
    if args.light == "none":
        return NoneLightDriver()
    if args.light == "mock":
        return MockBulbDriver(verbose=args.verbose)
    if args.light == "wiz":
        if not args.wiz_ip:
            raise ValueError("--light wiz requires --wiz-ip <LAN_IP> (run --wiz-discover to find it).")
        return WizUdpDriver(ip=args.wiz_ip)
    if args.light == "hue":
        if not (args.hue_ip and args.hue_key and args.hue_group):
            raise ValueError(
                "--light hue requires --hue-ip, --hue-key, and --hue-group."
            )
        return HueV2Driver(
            bridge_ip=args.hue_ip,
            app_key=args.hue_key,
            grouped_light_id=args.hue_group,
        )
    raise ValueError(f"Unsupported light driver: {args.light}")


def build_sensor_source(args: argparse.Namespace) -> SensorSource:
    """Instantiate either live USB-Serial Pico 2 reader or SyntheticSensorStream."""
    if args.port:
        return SerialSensorReader(port=args.port, baudrate=args.baudrate)
    scenario = args.simulate or "overcast_day"
    sim_ticks = args.ticks if args.ticks > 0 else 8
    return SimulatedSensorReader(scenario=scenario, total_ticks=sim_ticks)


def run_calibration_wizard(
    sensor: SensorSource,
    converter: AS7343Converter,
    light: LightDriver,
    profile_path: Path,
    settle_seconds: float = 1.5,
    interactive: bool = True,
) -> RoomProfile:
    """Run the 2-minute one-time feedforward room, lampshade & monitor profiling wizard."""
    print("\n=== 2-Minute Feedforward Room & Lampshade Optical Profiling ===")
    print("Tip: Run this once in a dim/dark room so lampshade tint & wall reflectance are mapped cleanly.\n")

    mount_mode = "bezel_hybrid"
    if interactive and sys.stdin.isatty():
        choice = input("Mount mode [1=bezel_hybrid (default), 2=desk_stand]: ").strip()
        if choice == "2":
            mount_mode = "desk_stand"
        input("Step 1/4: Press ENTER to measure dark ambient baseline (bulb will turn OFF)...")

    light.set_state(brightness_pct=0, cct_k=2300, transition_ms=300)
    time.sleep(settle_seconds)
    baseline = converter.convert(sensor.next_sample())
    print(f"  Baseline ambient: {baseline.m_edi_lx:.1f} lx m-EDI ({baseline.photopic_lx:.1f} lx)")

    print("Step 2/4: Measuring connected lamp at 100% @ 5200K (shade attenuation + CCT shift)...")
    light.set_state(brightness_pct=100, cct_k=5200, transition_ms=400)
    time.sleep(settle_seconds)
    cool_reading = converter.convert(sensor.next_sample())
    lamp_max_m_edi = max(5.0, round(cool_reading.m_edi_lx - baseline.m_edi_lx, 1))
    lamp_cct_shift = round(cool_reading.cct_k - 5200.0, 1)
    print(
        f"  Lamp max contribution: {lamp_max_m_edi:.1f} lx m-EDI | "
        f"Measured CCT: {cool_reading.cct_k:.0f}K (shade/wall shift: {lamp_cct_shift:+.0f}K)"
    )

    print("Step 3/4: Probing low-end dimming stability at 2300K (15% -> 10% -> 5%)...")
    lamp_min_stable = 10
    prev_lx = cool_reading.photopic_lx
    for pct in (15, 10, 5):
        light.set_state(brightness_pct=pct, cct_k=2300, transition_ms=300)
        time.sleep(max(0.4, settle_seconds * 0.6))
        low_reading = converter.convert(sensor.next_sample())
        if low_reading.photopic_lx < prev_lx * 0.98:
            lamp_min_stable = pct
            prev_lx = low_reading.photopic_lx
    if light.name == "wiz":
        lamp_min_stable = max(10, lamp_min_stable)
    print(f"  Minimum stable dimming floor: {lamp_min_stable}%")

    light.set_state(brightness_pct=0, cct_k=2300, transition_ms=300)
    if interactive and sys.stdin.isatty() and mount_mode == "bezel_hybrid":
        input(
            "Step 4/4 (15s): Briefly lift the magnetic sensor to seated eye position facing your "
            "bright monitor, then press ENTER..."
        )
    else:
        print("Step 4/4: Measuring seated eye-level monitor baseline contribution...")
    time.sleep(max(0.4, settle_seconds * 0.6))
    mon_reading = converter.convert(sensor.next_sample())
    monitor_max_m_edi = max(10.0, round(mon_reading.m_edi_lx - baseline.m_edi_lx, 1))
    print(f"  Estimated max display corneal m-EDI: {monitor_max_m_edi:.1f} lx")

    profile = RoomProfile(
        lamp_max_m_edi_lx=lamp_max_m_edi,
        lamp_cct_shift_k=lamp_cct_shift,
        lamp_min_stable_pct=lamp_min_stable,
        monitor_max_m_edi_lx=monitor_max_m_edi,
        mount_mode=mount_mode,
    )
    profile.save(str(profile_path))
    print(f"\nSaved feedforward RoomProfile to: {profile_path}\n")
    return profile


def _resolve_sim_hour(args: argparse.Namespace, tick_idx: int) -> float | None:
    if args.hour is not None:
        return float(args.hour)
    if not args.simulate:
        return None
    if args.simulate == "day_to_evening_demo":
        total = args.ticks if args.ticks > 0 else 8
        frac = min(1.0, tick_idx / float(max(1, total - 1)))
        if frac < 0.65:
            return 10.0 + (frac / 0.65) * 8.5
        if frac < 0.90:
            return 20.5 + ((frac - 0.65) / 0.25) * 0.8
        return 21.5 + ((frac - 0.90) / 0.10) * 0.5
    return SCENARIO_DEFAULT_HOURS.get(args.simulate)


def _build_controller(
    args: argparse.Namespace,
    profile: RoomProfile | None,
) -> CircadianController:
    kwargs: dict[str, Any] = {
        "wake_hour": args.wake_hour,
        "sleep_hour": args.sleep_hour,
        "profile": profile,
    }
    params = inspect.signature(CircadianController.__init__).parameters
    if "high_sensitivity_evening" in params:
        kwargs["high_sensitivity_evening"] = bool(args.sensitive_evening)
    return CircadianController(**kwargs)


def _print_monitor_safety_banner(live_opt_in: bool, hold_seconds: float = 3.0) -> None:
    print("\n=== Windows Display Safety Check (BUG-4 Laptop WMI + External DDC/CI + GPU Color) ===")
    print("  - Backup scope : Original GDI 16-bit gamma ramp, zero-admin Magnification.dll 5x5")
    print("                   GPU matrix fallback, laptop WMI (`WmiMonitorBrightness`), and DDC/CI.")
    print("  - Write guard  : Never mutates any display whose original brightness cannot be read.")
    print("  - Privacy lock : Zero admin rights, zero Registry edit, zero screen recording.")
    print("  - Restore rule : Deterministic restore on normal exit, Ctrl+C (SIGINT), and OS sleep.")
    if live_opt_in:
        print(
            f"  - Mode         : LIVE CONTROLLED SAFETY CHECK (45% @ 3000K for {hold_seconds:.1f}s -> "
            "restore & verify exact original brightness!).\n"
        )
    else:
        print("  - Mode         : DRY CHECK (display NOT touched). Pass BOTH --monitor and")
        print("                   --monitor-safety-check to run the 3-second live restore verify.\n")


def run_monitor_safety_check(
    display: WindowsDisplayController,
    hold_seconds: float = 3.0,
) -> int:
    """Execute a controlled live preview (45% @ 3000K) and verify exact hardware restore."""
    hold = max(0.0, float(hold_seconds))
    _print_monitor_safety_banner(live_opt_in=True, hold_seconds=hold)

    applied_result: dict[str, Any] = {
        "applied": False,
        "ddc_ok": False,
        "wmi_ok": False,
        "gamma_ok": False,
        "color_method": "none",
    }
    restored = False
    try:
        applied_result = display.apply(brightness_pct=45, cct_k=3000)
        color_method = str(applied_result.get("color_method", "none"))
        print(
            f"  [1/3] Applied gentle preview (45% @ 3000K): "
            f"wmi_ok={applied_result['wmi_ok']} | ddc_ok={applied_result['ddc_ok']} | "
            f"gamma_ok={applied_result['gamma_ok']} (color_method={color_method})"
        )
        if hold > 0.0 and applied_result["applied"]:
            print(f"  [2/3] Holding preview for {hold:.1f}s...")
            time.sleep(hold)
    finally:
        restored = display.restore()

    orig_wmi = display._original_wmi_brightness
    orig_ddc = dict(display._original_ddc_brightness)
    after_wmi = display.read_wmi_brightness() if (orig_wmi is not None and applied_result["wmi_ok"]) else None
    after_ddc = display.read_current_ddc_brightness() if (orig_ddc and applied_result["ddc_ok"]) else {}

    wmi_match = (not applied_result["wmi_ok"]) or (after_wmi == orig_wmi)
    ddc_match = (not applied_result["ddc_ok"]) or all(
        after_ddc.get(mon_id) == orig_val for mon_id, orig_val in orig_ddc.items()
    )
    overall_pass = bool(applied_result["applied"] and restored and wmi_match and ddc_match)
    color_method = str(applied_result.get("color_method", "none"))

    print("  [3/3] Restoration Verification Report:")
    if applied_result["wmi_ok"]:
        print(f"        - Laptop WMI Backlight : backed_up={orig_wmi}% -> restored={after_wmi}% ({'OK' if wmi_match else 'MISMATCH'})")
    else:
        print("        - Laptop WMI Backlight : not active on this display")
    if applied_result["ddc_ok"]:
        print(f"        - External DDC/CI VCP  : backed_up={orig_ddc} -> restored={after_ddc} ({'OK' if ddc_match else 'MISMATCH'})")
    else:
        print("        - External DDC/CI VCP  : not active on this display")
    print(
        f"        - GPU Color Transform  : color_method={color_method} -> "
        f"{'restored cleanly' if applied_result['gamma_ok'] and restored else 'skipped/failed'}"
    )
    print(f"\n  RESULT: {'PASS' if overall_pass else 'FAIL'}\n")
    return 0 if overall_pass else 1


def _parse_hour_input(raw: str, default: float) -> float:
    """Parse user hour input (`7`, `7.5`, or `07:30`) into [0.0..24.0)."""
    text = raw.strip()
    if not text:
        return default
    if ":" in text:
        parts = text.split(":", 1)
        try:
            hh = int(parts[0].strip())
            mm = int(parts[1].strip())
            if 0 <= hh < 24 and 0 <= mm < 60:
                return round(hh + (mm / 60.0), 2)
        except ValueError:
            return default
        return default
    try:
        val = float(text)
    except ValueError:
        return default
    return val if 0.0 <= val < 24.0 else default


def _parse_yes_no(raw: str, default: bool) -> bool:
    text = raw.strip().lower()
    if not text:
        return default
    if text in ("y", "yes", "e", "evet", "1", "true"):
        return True
    if text in ("n", "no", "h", "hayir", "hayır", "0", "false"):
        return False
    return default


def _emit_night_light_tip_if_needed(display: WindowsDisplayController) -> bool:
    """Log & print a one-time tip if Windows Night Light is ON during live `--monitor`."""
    try:
        caps = display.get_capabilities()
    except Exception:
        return False
    if caps.get("windows_night_light_detected") is True:
        tip = (
            "Windows Night Light appears to be ON. Disable Windows Night Light "
            "(Settings -> System -> Display -> Night light) to avoid double-warming "
            "with Magnification.dll."
        )
        logger.warning(tip)
        print(f"[Tip] {tip}")
        return True
    return False


def run_setup_wizard(
    config_path: str | Path | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    display: WindowsDisplayController | None = None,
    discover_fn: Callable[..., list[dict[str, Any]]] = discover_wiz_bulbs,
) -> UserConfig:
    """Interactive 30-second first-run setup wizard for Week-1 Open-Loop Desktop Beta."""
    target_path = Path(config_path).expanduser() if config_path is not None else DEFAULT_CONFIG_PATH
    existing = load_config(target_path)

    def _ask(prompt: str, default: str = "") -> str:
        try:
            ans = input_fn(prompt)
        except EOFError:
            return default
        stripped = ans.strip()
        return stripped if stripped else default

    output_fn("\n=== Circadian Desk — 30-Second First-Run Setup (Pure Track A v6.1) ===")
    output_fn("100% local-first | Zero admin rights | Zero Registry edits | Zero cloud\n")

    disp = display if display is not None else WindowsDisplayController(enabled=True)
    caps = disp.get_capabilities()
    output_fn("Detected Display Capabilities (read-only probe):")
    output_fn(
        f"  - Laptop WMI Backlight (`WmiMonitorBrightness`): "
        f"{'Available' if caps.get('wmi_laptop_available') else 'Not detected'}"
    )
    output_fn(
        f"  - External DDC/CI (`VCP 0x10`)                 : "
        f"{'Supported' if caps.get('ddc_external_supported') else 'Not detected'}"
    )
    output_fn(
        f"  - Zero-Admin GPU Color (`Magnification.dll`)   : "
        f"{'Available' if caps.get('magnification_available') else 'Unavailable'} "
        f"(GDI LUT: {'Available' if caps.get('gdi_gamma_supported') else 'Locked/Unavailable'})"
    )
    if caps.get("windows_night_light_detected") is True:
        output_fn(
            "\n  [!] WARNING: Windows Night Light appears to be ON.\n"
            "      Disable Windows Night Light (Settings -> System -> Display -> Night light)\n"
            "      so warm color shifts do not double-stack with Magnification.dll."
        )
    elif caps.get("windows_night_light_detected") is False:
        output_fn("  - Windows Night Light                          : OFF (clean, no double-warm)")
    output_fn("")

    wake_raw = _ask(
        f"1. Wake hour [0-23.99 or HH:MM, default {_format_hour(existing.wake_hour)} ({existing.wake_hour:g})]: ",
        "",
    )
    wake_hour = _parse_hour_input(wake_raw, existing.wake_hour)

    sleep_raw = _ask(
        f"2. Bedtime hour [0-23.99 or HH:MM, default {_format_hour(existing.sleep_hour)} ({existing.sleep_hour:g})]: ",
        "",
    )
    sleep_hour = _parse_hour_input(sleep_raw, existing.sleep_hour)

    sens_default_hint = "Y/n" if existing.sensitive_evening else "y/N"
    sens_raw = _ask(
        f"3. Enable High-Sensitivity Evening cap (<=5 lx m-EDI instead of <=10 lx)? [{sens_default_hint}]: ",
        "",
    )
    sensitive_evening = _parse_yes_no(sens_raw, existing.sensitive_evening)

    output_fn(
        "\n4. Local Smart Light Backend:\n"
        "   [1] none — Display-Only mode (no smart bulb needed; laptop/monitor only)\n"
        "   [2] wiz  — Local WiZ smart bulb (LAN UDP port 38899)\n"
        "   [3] hue  — Local Philips Hue Bridge (CLIP API v2)\n"
        "   [4] mock — Dry-run simulator (terminal only)"
    )
    light_map = {"1": "none", "2": "wiz", "3": "hue", "4": "mock"}
    default_light = existing.light if existing.light in ("none", "wiz", "hue", "mock") else "none"
    light_raw = _ask(f"   Select light mode [1-4 or name, default {default_light}]: ", default_light).lower()
    light_choice = light_map.get(light_raw, light_raw)
    if light_choice not in ("none", "wiz", "hue", "mock"):
        light_choice = default_light

    wiz_ip = existing.wiz_ip
    hue_ip = existing.hue_ip
    hue_key = existing.hue_key
    hue_group = existing.hue_group

    if light_choice == "wiz":
        scan_raw = _ask("   Scan local LAN for WiZ bulbs now? [Y/n]: ", "")
        if _parse_yes_no(scan_raw, True):
            output_fn("   Scanning local LAN (UDP 38899)...")
            try:
                bulbs = discover_fn(timeout=2.5)
            except Exception as exc:
                output_fn(f"   LAN scan skipped ({exc}).")
                bulbs = []
            if bulbs:
                for idx, bulb in enumerate(bulbs, start=1):
                    output_fn(
                        f"     [{idx}] ip={bulb.get('ip')} mac={bulb.get('mac')} "
                        f"temp={bulb.get('temp')}K dimming={bulb.get('dimming')}%"
                    )
                pick_raw = _ask(
                    f"   Enter bulb number [1-{len(bulbs)}] or custom IP (default 1): ",
                    "1",
                )
                if pick_raw.isdigit() and 1 <= int(pick_raw) <= len(bulbs):
                    wiz_ip = str(bulbs[int(pick_raw) - 1].get("ip", "")).strip()
                else:
                    wiz_ip = pick_raw
            else:
                output_fn("   No WiZ bulbs responded to UDP broadcast.")
                wiz_ip = _ask(f"   Enter WiZ bulb LAN IP [default {wiz_ip or 'none'}]: ", wiz_ip)
        else:
            wiz_ip = _ask(f"   Enter WiZ bulb LAN IP [default {wiz_ip or 'none'}]: ", wiz_ip)
    elif light_choice == "hue":
        hue_ip = _ask(f"   Hue Bridge LAN IP [default {hue_ip or 'required'}]: ", hue_ip)
        hue_key = _ask(f"   Hue CLIP v2 application key [default {'<saved>' if hue_key else 'required'}]: ", hue_key)
        hue_group = _ask(
            f"   Hue grouped_light UUID [default {hue_group or 'required'}]: ",
            hue_group,
        )

    mon_hint = "Y/n" if existing.monitor_enabled else "y/N"
    mon_raw = _ask(f"\n5. Enable live Windows monitor brightness + warm color control? [{mon_hint}]: ", "")
    monitor_enabled = _parse_yes_no(mon_raw, existing.monitor_enabled)

    csv_raw = _ask(
        f"6. Optional CSV telemetry log path for 7-day reports [default {existing.log_csv or 'disabled'}]: ",
        "",
    )
    log_csv = csv_raw if csv_raw else existing.log_csv

    cfg = UserConfig(
        wake_hour=wake_hour,
        sleep_hour=sleep_hour,
        mode="open_loop",
        light=light_choice,
        wiz_ip=wiz_ip,
        hue_ip=hue_ip,
        hue_key=hue_key,
        hue_group=hue_group,
        monitor_enabled=monitor_enabled,
        sensitive_evening=sensitive_evening,
        interval_s=existing.interval_s,
        log_csv=log_csv,
        profile_path=existing.profile_path,
        web_ui_enabled=existing.web_ui_enabled,
        web_ui_port=existing.web_ui_port,
        mqtt_host=existing.mqtt_host,
        mqtt_port=existing.mqtt_port,
        mqtt_user=existing.mqtt_user,
        mqtt_password=existing.mqtt_password,
        mqtt_prefix=existing.mqtt_prefix,
        auto_pause_enabled=existing.auto_pause_enabled,
        auto_pause_fullscreen=existing.auto_pause_fullscreen,
        auto_pause_browser_fullscreen=existing.auto_pause_browser_fullscreen,
        auto_pause_apps=list(existing.auto_pause_apps),
        tray_enabled=existing.tray_enabled,
        display_max_nits=existing.display_max_nits,
        panel_type=getattr(existing, "panel_type", "auto"),
        comfort_mode=existing.comfort_mode,
        onboarding_completed=True,
        weekly_schedule={k: dict(v) for k, v in existing.weekly_schedule.items()},
        start_with_windows=existing.start_with_windows,
    )
    saved_path = save_config(cfg, target_path)

    if saved_path.resolve() == DEFAULT_CONFIG_PATH.resolve():
        cli_cmd = "circadian-desk --config --open-loop"
        py_cmd = "python -m orchestrator.daemon --config --open-loop"
    else:
        cli_cmd = f'circadian-desk --config "{saved_path}" --open-loop'
        py_cmd = f'python -m orchestrator.daemon --config "{saved_path}" --open-loop'

    output_fn(f"\nSaved configuration to: {saved_path}")
    output_fn("\nReady! Start your Week-1 Open-Loop Desktop Beta with:")
    output_fn(f"  {cli_cmd}")
    output_fn(f"  (or without editable install: {py_cmd})\n")
    return cfg


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="circadian-desk",
        description="Eye-Level Spectral Light Orchestrator — Local-First Pure Track A Daemon (v6.1)",
    )
    parser.add_argument(
        "--config",
        nargs="?",
        const=str(DEFAULT_CONFIG_PATH),
        default=None,
        metavar="PATH",
        help=(
            "Load saved defaults from JSON config whenever CLI flags are not explicitly overridden "
            f"(defaults to {str(DEFAULT_CONFIG_PATH).replace('%', '%%')} if PATH is omitted)."
        ),
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        default=False,
        help="Run the interactive 30-second first-run setup wizard and save local config.",
    )

    source_grp = parser.add_mutually_exclusive_group()
    source_grp.add_argument(
        "--open-loop",
        action="store_true",
        default=False,
        help=(
            "Week-1 Zero-Hardware Desktop Beta mode: smooth sunrise/sunset Brown (2022) curve "
            "driving Windows laptop WMI / external DDC/CI + Magnification.dll / GDI LUT + local lights."
        ),
    )
    source_grp.add_argument(
        "--simulate",
        choices=SCENARIOS,
        help="Run with SyntheticSensorStream scenario (zero hardware required).",
    )
    source_grp.add_argument(
        "--port",
        help="USB-Serial port for Raspberry Pi Pico 2 (e.g., COM4 or /dev/ttyACM0).",
    )
    parser.add_argument("--baudrate", type=int, default=115200, help="Serial baudrate (default: 115200).")

    parser.add_argument(
        "--light",
        choices=("none", "mock", "wiz", "hue"),
        default=None,
        help="Local smart-light backend: none (display-only), mock, wiz, or hue (default: mock, or config value).",
    )
    parser.add_argument("--wiz-ip", default=None, help="LAN IP address of local WiZ bulb (UDP port 38899).")
    parser.add_argument(
        "--wiz-discover",
        action="store_true",
        help="Scan local LAN for WiZ bulbs via UDP broadcast and exit.",
    )
    parser.add_argument("--hue-ip", default=None, help="LAN IP address of Philips Hue Bridge.")
    parser.add_argument("--hue-key", default=None, help="Hue CLIP API v2 `hue-application-key`.")
    parser.add_argument("--hue-group", default=None, help="Hue CLIP API v2 `grouped_light` resource UUID.")

    parser.add_argument(
        "--monitor",
        dest="monitor",
        action="store_true",
        default=None,
        help="Enable real Windows laptop WMI / external DDC/CI + zero-admin GPU color shift (opt-in).",
    )
    parser.add_argument(
        "--no-monitor",
        dest="monitor",
        action="store_false",
        help="Explicitly disable monitor hardware control (overrides monitor_enabled in --config).",
    )
    parser.add_argument(
        "--monitor-safety-check",
        action="store_true",
        default=False,
        help=(
            "Print BUG-4 display safety summary; combine with --monitor to run a controlled "
            "3-second preview (45%% @ 3000K) and verify exact WMI/DDC/GPU color restoration."
        ),
    )
    parser.add_argument(
        "--safety-hold-s",
        type=float,
        default=3.0,
        help="Preview hold duration in seconds for --monitor --monitor-safety-check (default: 3.0).",
    )
    parser.add_argument(
        "--sensitive-evening",
        action="store_true",
        default=None,
        help=(
            "Enable optional High-Sensitivity Evening visual-comfort preset (<=5 lx m-EDI "
            "evening target instead of default <=10 lx; Spitschan 2022)."
        ),
    )
    parser.add_argument(
        "--hour",
        type=float,
        default=None,
        help="Override clock hour (0.0..23.99, e.g., --hour 21.5 for evening testing).",
    )
    parser.add_argument("--wake-hour", type=float, default=None, help="User wake hour (default: 7.0).")
    parser.add_argument("--sleep-hour", type=float, default=None, help="User bedtime hour (default: 23.0).")
    parser.add_argument(
        "--profile",
        default=None,
        help="Path to feedforward RoomProfile JSON (default: room_profile.json).",
    )
    parser.add_argument(
        "--matrix",
        default=None,
        help="Optional path to custom spectrophotometer calibration matrix JSON.",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Run interactive 2-minute room & lampshade optical profiling wizard and save profile.",
    )
    parser.add_argument(
        "--log-csv",
        default=None,
        metavar="CSV_PATH",
        help="Append per-tick UTF-8 telemetry to CSV for 7-day compliance tracking.",
    )
    parser.add_argument(
        "--report",
        default=None,
        metavar="CSV_PATH",
        help="Generate offline Markdown + pure-SVG HTML compliance report from a logged CSV and exit.",
    )
    parser.add_argument(
        "--report-out",
        default=None,
        metavar="DIR",
        help="Optional output directory for --report files (defaults to the CSV's directory).",
    )
    parser.add_argument(
        "--web-ui",
        dest="web_ui",
        action="store_true",
        default=None,
        help="Start local-only Braun Web Control Panel on 127.0.0.1 (OFF by default in --simulate).",
    )
    parser.add_argument(
        "--no-web-ui",
        dest="web_ui",
        action="store_false",
        help="Explicitly disable the local Braun Web Control Panel (overrides web_ui_enabled in --config).",
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=None,
        help="Localhost port for the Braun Web Control Panel (default: 8765; 0 = ephemeral).",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        default=False,
        help="Open the local Braun Web Control Panel in your default browser on startup.",
    )
    parser.add_argument(
        "--tray",
        dest="tray",
        action="store_true",
        default=None,
        help="Show native Windows System Tray icon (1-click Control Panel, Pause 2h, Quit and Restore).",
    )
    parser.add_argument(
        "--no-tray",
        dest="tray",
        action="store_false",
        help="Explicitly disable the Windows System Tray icon (overrides tray_enabled in --config).",
    )
    parser.add_argument(
        "--auto-pause",
        dest="auto_pause",
        action="store_true",
        default=None,
        help=(
            "Auto-pause and restore display for color-critical apps (Photoshop/Lightroom/Figma/Resolve) "
            "and fullscreen games (zero screen capture; reads foreground exe name only)."
        ),
    )
    parser.add_argument(
        "--no-auto-pause",
        dest="auto_pause",
        action="store_false",
        help="Disable foreground app auto-pause (overrides auto_pause_enabled in --config).",
    )
    parser.add_argument(
        "--no-auto-pause-fullscreen",
        dest="auto_pause_fullscreen",
        action="store_false",
        default=None,
        help="Keep color-critical app auto-pause active but do not pause on generic fullscreen windows.",
    )
    parser.add_argument(
        "--mqtt-host",
        default=None,
        help="Local MQTT broker host for 1-click Home Assistant Auto-Discovery (pure stdlib, 0 deps).",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=None,
        help="Local MQTT broker port (default: 1883).",
    )
    parser.add_argument(
        "--mqtt-user",
        default=None,
        help="Optional MQTT broker username.",
    )
    parser.add_argument(
        "--mqtt-password",
        default=None,
        help="Optional MQTT broker password.",
    )
    parser.add_argument(
        "--mqtt-prefix",
        default=None,
        help="Home Assistant MQTT auto-discovery prefix (default: homeassistant).",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=0,
        help="Number of samples to run before exiting cleanly (0 = run continuously).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Seconds between control loop ticks (default: 2.0, or config interval_s).",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="store_true",
        default=False,
        help="Print Circadian Desk version and exit 0 immediately (before mutex/config/sockets).",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        default=False,
        help="Start quietly to System Tray without auto-opening the browser (used by Windows startup).",
    )
    parser.add_argument(
        "--panel-type",
        choices=("auto", "wled_ips", "p3_ips", "woled", "qd_oled"),
        default=None,
        help="Panel spectrum profile for melanopic/OLED compensation (default: auto).",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging.")
    return parser


def resolve_runtime_args(args: argparse.Namespace) -> argparse.Namespace:
    """Merge optional `--config` defaults underneath explicitly supplied CLI flags."""
    if args.config is not None:
        cfg = load_config(args.config)
        if args.light is None:
            args.light = cfg.light
        if args.wiz_ip is None:
            args.wiz_ip = cfg.wiz_ip or None
        if args.hue_ip is None:
            args.hue_ip = cfg.hue_ip or None
        if args.hue_key is None:
            args.hue_key = cfg.hue_key or None
        if args.hue_group is None:
            args.hue_group = cfg.hue_group or None
        if args.monitor is None:
            args.monitor = bool(cfg.monitor_enabled)
        if args.sensitive_evening is None:
            args.sensitive_evening = bool(cfg.sensitive_evening)
        if args.wake_hour is None:
            args.wake_hour = float(cfg.wake_hour)
        if args.sleep_hour is None:
            args.sleep_hour = float(cfg.sleep_hour)
        if args.profile is None:
            args.profile = cfg.profile_path or str(get_app_data_dir() / "room_profile.json")
        if args.interval is None:
            args.interval = float(cfg.interval_s)
        if args.log_csv is None:
            args.log_csv = cfg.log_csv or None
        if args.ui_port is None:
            args.ui_port = int(cfg.web_ui_port)
        if args.mqtt_host is None:
            args.mqtt_host = cfg.mqtt_host or None
        if args.mqtt_port is None:
            args.mqtt_port = int(cfg.mqtt_port)
        if args.mqtt_user is None:
            args.mqtt_user = cfg.mqtt_user or ""
        if args.mqtt_password is None:
            args.mqtt_password = cfg.mqtt_password or ""
        if args.mqtt_prefix is None:
            args.mqtt_prefix = cfg.mqtt_prefix or "homeassistant"
        if not args.open_loop and not args.port and not args.simulate:
            if cfg.mode == "open_loop":
                args.open_loop = True
            else:
                args.simulate = "overcast_day"
        if args.web_ui is None:
            # Keep Web UI OFF by default in --simulate or finite-tick scripted runs unless explicitly requested.
            args.web_ui = bool(cfg.web_ui_enabled) and (not args.simulate) and (args.ticks <= 0)
        if args.tray is None:
            args.tray = bool(cfg.tray_enabled) and (not args.simulate) and (args.ticks <= 0)
        if args.auto_pause is None:
            args.auto_pause = bool(cfg.auto_pause_enabled) and (not args.simulate) and (args.ticks <= 0)
        if args.auto_pause_fullscreen is None:
            args.auto_pause_fullscreen = bool(cfg.auto_pause_fullscreen)
        args.auto_pause_apps = list(cfg.auto_pause_apps)
        args.auto_pause_browser_fullscreen = bool(
            getattr(cfg, "auto_pause_browser_fullscreen", False)
        )
        args.display_max_nits = float(cfg.display_max_nits)
        if args.panel_type is None:
            args.panel_type = str(getattr(cfg, "panel_type", "auto") or "auto")
        args.comfort_mode = bool(cfg.comfort_mode)
        args.onboarding_completed = bool(getattr(cfg, "onboarding_completed", False))
        raw_weekly = getattr(cfg, "weekly_schedule", None)
        args.weekly_schedule = (
            {k: dict(v) for k, v in raw_weekly.items()} if isinstance(raw_weekly, dict) else {}
        )
        args.start_with_windows = bool(getattr(cfg, "start_with_windows", False))
    else:
        if args.light is None:
            args.light = "mock"
        if args.monitor is None:
            args.monitor = False
        if args.sensitive_evening is None:
            args.sensitive_evening = False
        if args.wake_hour is None:
            args.wake_hour = 7.0
        if args.sleep_hour is None:
            args.sleep_hour = 23.0
        if args.profile is None:
            args.profile = str(get_app_data_dir() / "room_profile.json")
        if args.interval is None:
            args.interval = 2.0
        if args.web_ui is None:
            args.web_ui = False
        if args.ui_port is None:
            args.ui_port = 8765
        if args.mqtt_port is None:
            args.mqtt_port = 1883
        if args.mqtt_user is None:
            args.mqtt_user = ""
        if args.mqtt_password is None:
            args.mqtt_password = ""
        if args.mqtt_prefix is None:
            args.mqtt_prefix = "homeassistant"
        if args.tray is None:
            args.tray = False
        if args.auto_pause is None:
            args.auto_pause = False
        if args.auto_pause_fullscreen is None:
            args.auto_pause_fullscreen = True
        args.auto_pause_apps = []
        args.auto_pause_browser_fullscreen = False
        args.display_max_nits = 0.0
        if args.panel_type is None:
            args.panel_type = "auto"
        args.comfort_mode = True
        args.onboarding_completed = True
        args.weekly_schedule = {}
        args.start_with_windows = False
    if getattr(args, "background", False):
        args.open_browser = False
    return args


def _run_report_command(csv_path: str, output_dir: str | None) -> int:
    source = Path(csv_path).expanduser()
    if not source.exists():
        print(f"Error: Telemetry CSV not found: {source}", file=sys.stderr)
        return 1
    try:
        from orchestrator.telemetry import generate_compliance_report
    except ImportError as exc:
        print(f"Error: Telemetry report module unavailable ({exc}).", file=sys.stderr)
        return 1

    summary = generate_compliance_report(csv_path=source, output_dir=output_dir)
    md_path = summary.get("markdown_path")
    html_path = summary.get("html_path")
    print("\n=== Circadian Compliance Report Generated (Offline, 100% Local) ===")
    print(f"  Source CSV      : {source}")
    print(f"  Markdown report : {md_path}")
    print(f"  HTML/SVG report : {html_path}")
    if "total_ticks" in summary:
        print(
            f"  Ticks analyzed  : {summary.get('total_ticks')} "
            f"(valid: {summary.get('valid_ticks', summary.get('total_ticks'))})"
        )
    print("")
    return 0


def _resolve_open_loop_details(
    hour_of_day: float,
    wake_hour: float,
    sleep_hour: float,
    high_sensitivity_evening: bool,
    comfort_mode: bool,
    display_max_nits: float,
    backlight_gamma: float,
    fallback_phase: str,
    *,
    panel_type: str = "wled_ips",
    solid_angle_sr: float | None = None,
    dark_mode: bool | None = None,
    compensate_lut: bool = True,
) -> tuple[str, float, float]:
    targets = _call_smooth_open_loop_targets(
        hour_of_day=hour_of_day,
        wake_hour=wake_hour,
        sleep_hour=sleep_hour,
        high_sensitivity_evening=high_sensitivity_evening,
        comfort_mode=comfort_mode,
        display_max_nits=display_max_nits,
        backlight_gamma=backlight_gamma,
        panel_type=panel_type,
        solid_angle_sr=solid_angle_sr,
        dark_mode=dark_mode,
        compensate_lut=compensate_lut,
    )
    if targets is not None:
        return (
            str(targets.get("transition_label", fallback_phase.lower())),
            float(targets.get("target_screen_nits", 0.0)),
            float(targets.get("estimated_corneal_m_edi_lx", 0.0)),
        )
    return fallback_phase.lower(), 0.0, 0.0


def _sample_schedule_curve(
    wake_hour: float,
    sleep_hour: float,
    high_sensitivity_evening: bool,
    comfort_mode: bool,
    display_max_nits: float,
    backlight_gamma: float,
    *,
    panel_type: str = "wled_ips",
    solid_angle_sr: float | None = None,
    dark_mode: bool | None = None,
    compensate_lut: bool = True,
) -> list[dict[str, Any]]:
    sample_fn = getattr(photobiology, "sample_24h_curve", None)
    if callable(sample_fn):
        try:
            params = inspect.signature(sample_fn).parameters
            kwargs: dict[str, Any] = {
                "wake_hour": wake_hour,
                "sleep_hour": sleep_hour,
                "high_sensitivity_evening": high_sensitivity_evening,
                "comfort_mode": comfort_mode,
                "display_max_nits": display_max_nits,
                "backlight_gamma": backlight_gamma,
            }
            if "include_end" in params:
                kwargs["include_end"] = True
            if "panel_type" in params:
                kwargs["panel_type"] = panel_type
            if "solid_angle_sr" in params and solid_angle_sr is not None:
                kwargs["solid_angle_sr"] = solid_angle_sr
            if "dark_mode" in params and dark_mode is not None:
                kwargs["dark_mode"] = dark_mode
            if "compensate_lut" in params:
                kwargs["compensate_lut"] = compensate_lut
            return list(sample_fn(**kwargs))
        except Exception:
            pass
    return []


def _filter_supported_kwargs(fn: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Pass only keyword arguments accepted by `fn` (keeps Wave 2 / Wave 3 loosely coupled)."""
    params = inspect.signature(fn).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def _estimate_screen_nits(pct: int, max_nits: float, gamma: float) -> float:
    fn = getattr(photobiology, "backlight_pct_to_nits", None)
    if callable(fn):
        try:
            return round(float(fn(pct=pct, max_nits=max_nits, gamma=gamma)), 1)
        except Exception:
            pass
    min_n = max(15.0, 0.06 * max_nits)
    ratio = max(0.0, min(100.0, float(pct))) / 100.0
    return round(min_n + (max_nits - min_n) * (ratio ** max(0.5, gamma)), 1)


def _wait_for_next_tick(
    interval_s: float,
    stop_event: threading.Event,
    wake_event: threading.Event,
) -> bool:
    """Sleep up to `interval_s`, waking immediately on `stop_event` (returns True) or `wake_event`."""
    if interval_s <= 0.0:
        return stop_event.is_set()
    deadline = time.monotonic() + float(interval_s)
    while True:
        if stop_event.is_set():
            return True
        if wake_event.is_set():
            wake_event.clear()
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return stop_event.is_set()
        slice_s = min(0.15, remaining)
        if stop_event.wait(slice_s):
            return True
        if wake_event.is_set():
            wake_event.clear()
            return False


def _build_mqtt_payload(
    *,
    mode: str,
    reading: PhotometricReading,
    action: ControlAction,
    paused: bool,
) -> dict[str, Any]:
    bulb_power = bool(getattr(action, "bulb_power", action.bulb_brightness_pct > 0))
    return {
        "mode": mode,
        "phase": str(action.phase),
        "m_edi_lx": round(float(action.measured_m_edi_lx), 2),
        "uncertainty_k2_lx": round(
            float(getattr(action, "uncertainty_k2_lx", getattr(reading, "uncertainty_k2_lx", 0.0))),
            2,
        ),
        "compliance_status": str(getattr(action, "compliance_status", "BELIRSIZ")),
        "cct_k": int(round(float(reading.cct_k))),
        "photopic_lx": round(float(reading.photopic_lx), 2),
        "m_der": round(float(reading.m_der), 3),
        "flicker_hz": round(float(reading.flicker_hz), 1),
        "monitor_brightness_pct": int(action.monitor_brightness_pct),
        "monitor_cct_k": int(action.monitor_cct_k),
        "bulb_power": bulb_power,
        "bulb_brightness_pct": int(action.bulb_brightness_pct),
        "bulb_cct_k": int(action.bulb_cct_k),
        "unlinked_light_alert": bool(action.unlinked_light_alert),
        "sensor_occluded": bool(getattr(action, "sensor_occluded", False)),
        "paused": bool(paused),
    }


def _resolve_bundled_icon_path() -> Path | None:
    """Locate `assets/circadian_desk.ico` in either PyInstaller `_MEIPASS` or source tree."""
    meipass = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if meipass:
        candidates.append(Path(meipass) / "assets" / "circadian_desk.ico")
    candidates.append(Path(__file__).resolve().parents[1] / "assets" / "circadian_desk.ico")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _evaluate_auto_pause(
    watcher: Any | None,
    ui_state: OrchestratorState,
    manual_paused: bool,
) -> tuple[bool, str]:
    """Sync live auto-pause toggles into `ForegroundAppWatcher` and check active foreground app."""
    if watcher is None:
        return False, ""
    watcher.enabled = bool(ui_state.auto_pause_enabled)
    if hasattr(watcher, "pause_on_fullscreen_browser"):
        watcher.pause_on_fullscreen_browser = bool(ui_state.auto_pause_browser_fullscreen)
    if manual_paused or not watcher.enabled:
        return False, ""
    try:
        decision = watcher.check()
    except Exception as exc:
        logger.debug("ForegroundAppWatcher.check() failed: %s", exc)
        return False, ""
    if getattr(decision, "should_pause", False):
        return True, str(getattr(decision, "reason", "Auto-paused"))
    return False, ""


def _resolve_effective_schedule(
    hour_override: float | None,
    default_wake: float,
    default_sleep: float,
    weekly_schedule: dict[str, dict[str, float]] | None,
) -> tuple[float, float, str, float]:
    """Resolve wake-anchored 7-day biological schedule without any midnight jump."""
    now_dt = datetime.now()
    if hour_override is not None:
        h_mod = float(hour_override) % 24.0
        hh = int(h_mod) % 24
        rem_m = (h_mod - int(h_mod)) * 60.0
        mm = min(59, max(0, int(rem_m)))
        ss = min(59, max(0, int(round((rem_m - int(rem_m)) * 60.0))))
        now_dt = now_dt.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    resolve_fn = getattr(photobiology, "resolve_biological_schedule", None)
    if callable(resolve_fn):
        try:
            return resolve_fn(
                now_dt,
                default_wake=default_wake,
                default_sleep=default_sleep,
                weekly_schedule=weekly_schedule,
            )
        except Exception as exc:
            logger.debug("resolve_biological_schedule() failed: %s", exc)
    return (float(default_wake), float(default_sleep), "", 0.0)


def _compute_movie_mode_targets(max_nits: float, gamma: float) -> tuple[int, int, float]:
    """Compute comfortable Movie Mode (`3400K`, ~90-150 nits) brightness/CCT/nits tuple."""
    fn = getattr(photobiology, "compute_movie_mode_target", None)
    if callable(fn):
        try:
            return fn(max_nits=max_nits, gamma=gamma)
        except Exception as exc:
            logger.debug("compute_movie_mode_target() failed: %s", exc)
    target_nits = round(min(150.0, max(90.0, float(max_nits) * 0.55)), 1)
    return (50, 3400, target_nits)


def should_enforce_onboarding_gate(
    *,
    onboarding_completed: bool,
    web_ui_enabled: bool,
    simulate_mode: bool,
    ticks: int,
) -> bool:
    """Return True when interactive desktop launch should hold in `AWAITING_SETUP`
    before applying any brightness or color-temperature change to the display or bulb.
    """
    return (
        (not bool(onboarding_completed))
        and bool(web_ui_enabled)
        and (not bool(simulate_mode))
        and int(ticks) <= 0
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    enforce_onboarding_gate: bool | None = None,
    on_state_ready: Callable[[OrchestratorState, LocalWebUIServer | None], None] | None = None,
) -> int:
    _ensure_noconsole_streams()
    effective_argv = resolve_default_argv(argv)

    parser = build_arg_parser()
    args = parser.parse_args(effective_argv)

    # Guardrail G2: Handle --version BEFORE mutex, config, sockets, display, or tray!
    if getattr(args, "version", False):
        print(f"Circadian Desk {APP_VERSION}")
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s [%(name)s] %(message)s",
    )

    if args.wiz_discover:
        print("Scanning local LAN (UDP 38899) for WiZ smart bulbs...")
        bulbs = discover_wiz_bulbs(timeout=2.5)
        if not bulbs:
            print("No WiZ bulbs responded. Ensure bulbs are powered on and on the same subnet.")
            return 0
        for b in bulbs:
            print(
                f"  Found WiZ bulb: ip={b['ip']} mac={b['mac']} "
                f"state={b['state']} temp={b['temp']}K dimming={b['dimming']}%"
            )
        return 0

    if args.report:
        return _run_report_command(csv_path=args.report, output_dir=args.report_out)

    if args.setup:
        run_setup_wizard(config_path=args.config or DEFAULT_CONFIG_PATH)
        return 0

    if args.monitor_safety_check:
        # Require explicit --monitor flag for live hardware preview even if --config was passed.
        if args.monitor is not True:
            _print_monitor_safety_banner(live_opt_in=False, hold_seconds=args.safety_hold_s)
            return 0
        display = WindowsDisplayController(enabled=True)
        _emit_night_light_tip_if_needed(display)
        return run_monitor_safety_check(display=display, hold_seconds=args.safety_hold_s)

    args = resolve_runtime_args(args)

    # Guardrail G1: Single-Instance Win32 Named Mutex for continuous desktop runs
    # (bypassed in --simulate and finite --ticks CLI/test runs).
    mutex_handle: Any = None
    continuous_mode = args.ticks <= 0
    if continuous_mode and (not args.simulate):
        mutex_handle, already_running = acquire_single_instance_mutex()
        if already_running:
            if not getattr(args, "background", False):
                try:
                    webbrowser.open(f"http://127.0.0.1:{int(args.ui_port)}")
                except Exception:
                    pass
            release_single_instance_mutex(mutex_handle)
            return 0

    if args.monitor or args.auto_pause:
        try:
            enable_per_monitor_dpi_awareness()
        except Exception:
            pass

    if not args.open_loop and not args.port and not args.simulate:
        args.simulate = "overcast_day"

    profile_path = Path(args.profile)
    light = build_light_driver(args)
    display = WindowsDisplayController(enabled=bool(args.monitor))
    default_display_profile: dict[str, Any] = {
        "max_nits": 300.0,
        "min_nits": 18.0,
        "model_name": "",
        "source": "estimated_sdr",
        "is_auto_detected": False,
        "width_mm": 0,
        "height_mm": 0,
        "diagonal_in": 0.0,
        "solid_angle_sr": 0.35,
        "gamut_dci_p3_pct": 0.0,
        "auto_panel_type": "wled_ips",
    }
    capabilities: dict[str, Any]
    if args.monitor:
        capabilities = display.get_capabilities()
        if capabilities.get("windows_night_light_detected") is True:
            tip = (
                "Windows Night Light appears to be ON. Disable Windows Night Light "
                "(Settings -> System -> Display -> Night light) to avoid double-warming "
                "with Magnification.dll."
            )
            logger.warning(tip)
            print(f"[Tip] {tip}")
        display_profile = dict(capabilities.get("display_profile") or default_display_profile)
    else:
        display_profile = (
            detect_display_luminance_profile()
            if (args.web_ui and not args.simulate and sys.platform == "win32")
            else dict(default_display_profile)
        )
        capabilities = {
            "is_windows": sys.platform == "win32",
            "wmi_laptop_available": False,
            "ddc_external_supported": False,
            "gdi_gamma_supported": False,
            "magnification_available": False,
            "windows_night_light_detected": None,
            "display_profile": display_profile,
        }
    backlight_gamma = 2.0 if capabilities.get("wmi_laptop_available") else 1.2
    use_color = _supports_ansi()
    display_solid_angle_sr = float(display_profile.get("solid_angle_sr", 0.35) or 0.35)
    auto_panel_type = str(display_profile.get("auto_panel_type", "wled_ips") or "wled_ips")
    packaged_msix = is_packaged_msix()

    dispatcher = CommandDispatcher(
        light=light,
        display=display,
        monitor_enabled=bool(args.monitor),
        transition_ms=1000,
    )

    mode_tag = "open_loop" if args.open_loop else ("serial" if args.port else "simulate")
    config_save_path = Path(args.config).expanduser() if args.config else None

    default_conflicts: dict[str, Any] = {
        "night_light_on": False,
        "hdr_active": False,
        "acm_active": False,
        "color_filter_on": False,
        "competing_apps": [],
        "warnings": [],
    }
    cached_conflicts: dict[str, Any] = dict(default_conflicts)
    cached_dark_mode: bool | None = None
    last_conflict_poll_s: float = -1e9
    skip_live_conflict_poll = bool(args.simulate and not args.monitor)

    def _poll_live_conflicts(force: bool = False) -> dict[str, Any]:
        nonlocal cached_conflicts, cached_dark_mode, last_conflict_poll_s
        if skip_live_conflict_poll:
            return cached_conflicts
        now_mono = time.monotonic()
        if (not force) and (now_mono - last_conflict_poll_s) < 4.0:
            return cached_conflicts
        last_conflict_poll_s = now_mono
        try:
            res = detect_live_display_conflicts()
            if isinstance(res, dict):
                cached_conflicts = res
        except Exception as exc:
            logger.debug("detect_live_display_conflicts() failed: %s", exc)
        try:
            cached_dark_mode = detect_windows_dark_mode()
        except Exception:
            pass
        return cached_conflicts

    initial_conflicts = (
        _poll_live_conflicts(force=True)
        if (not skip_live_conflict_poll and (args.monitor or args.web_ui))
        else dict(default_conflicts)
    )

    initial_start_with_windows = bool(getattr(args, "start_with_windows", False))
    if (
        sys.platform == "win32"
        and args.config is not None
        and (not args.simulate)
        and (args.ticks <= 0)
    ):
        try:
            initial_start_with_windows = bool(get_windows_autostart_enabled())
        except Exception:
            pass

    prev_autostart = bool(initial_start_with_windows)
    live_panel_pref: list[str] = [str(getattr(args, "panel_type", "auto") or "auto")]
    stop_event = threading.Event()
    web_server: LocalWebUIServer | None = None
    mqtt_bridge: Any = None
    app_watcher: Any = None
    tray: Any = None

    def _resolve_effective_panel_type() -> str:
        raw_pref = getattr(ui_state, "panel_type", live_panel_pref[0])
        pref = str(raw_pref or "auto").strip().lower()
        if pref in ("", "auto"):
            return auto_panel_type
        norm_fn = getattr(photobiology, "normalize_panel_type", None)
        return norm_fn(pref) if callable(norm_fn) else pref

    def _persist_ui_settings(updated: dict[str, Any]) -> None:
        nonlocal prev_autostart
        if "panel_type" in updated:
            live_panel_pref[0] = str(updated["panel_type"] or "auto")
        if app_watcher is not None:
            if "auto_pause_enabled" in updated:
                app_watcher.enabled = bool(updated["auto_pause_enabled"])
            if "auto_pause_browser_fullscreen" in updated and hasattr(
                app_watcher, "pause_on_fullscreen_browser"
            ):
                app_watcher.pause_on_fullscreen_browser = bool(
                    updated["auto_pause_browser_fullscreen"]
                )
        if "start_with_windows" in updated:
            desired_autostart = bool(updated["start_with_windows"])
            if desired_autostart != prev_autostart:
                prev_autostart = desired_autostart
                if not packaged_msix:
                    try:
                        set_windows_autostart_enabled(desired_autostart)
                    except Exception as exc:
                        logger.warning("Could not update Windows startup entry (%s).", exc)
        if config_save_path is None:
            return
        cfg = load_config(config_save_path)
        cfg.wake_hour = float(updated["wake_hour"])
        cfg.sleep_hour = float(updated["sleep_hour"])
        cfg.sensitive_evening = bool(updated["high_sensitivity_evening"])
        if "auto_pause_enabled" in updated:
            cfg.auto_pause_enabled = bool(updated["auto_pause_enabled"])
        if "auto_pause_browser_fullscreen" in updated:
            cfg.auto_pause_browser_fullscreen = bool(updated["auto_pause_browser_fullscreen"])
        if "display_max_nits" in updated:
            cfg.display_max_nits = float(updated["display_max_nits"])
        if "panel_type" in updated:
            cfg.panel_type = str(updated["panel_type"] or "auto")
        if "comfort_mode" in updated:
            cfg.comfort_mode = bool(updated["comfort_mode"])
        if "weekly_schedule" in updated and isinstance(updated["weekly_schedule"], dict):
            cfg.weekly_schedule = {k: dict(v) for k, v in updated["weekly_schedule"].items()}
        if "onboarding_completed" in updated:
            cfg.onboarding_completed = bool(updated["onboarding_completed"])
        if "start_with_windows" in updated:
            cfg.start_with_windows = bool(updated["start_with_windows"])
        save_config(cfg, config_save_path)

    onboarding_gate_active = (
        bool(enforce_onboarding_gate and (not bool(getattr(args, "onboarding_completed", True))) and args.web_ui)
        if enforce_onboarding_gate is not None
        else should_enforce_onboarding_gate(
            onboarding_completed=bool(getattr(args, "onboarding_completed", True)),
            web_ui_enabled=bool(args.web_ui),
            simulate_mode=bool(args.simulate),
            ticks=int(args.ticks),
        )
    )

    state_init_kwargs = _filter_supported_kwargs(
        OrchestratorState.__init__,
        {
            "mode": mode_tag,
            "light_backend": str(args.light),
            "wake_hour": float(args.wake_hour),
            "sleep_hour": float(args.sleep_hour),
            "high_sensitivity_evening": bool(args.sensitive_evening),
            "auto_pause_enabled": bool(args.auto_pause),
            "auto_pause_browser_fullscreen": bool(getattr(args, "auto_pause_browser_fullscreen", False)),
            "display_max_nits": float(getattr(args, "display_max_nits", 0.0)),
            "detected_max_nits": float(display_profile.get("max_nits", 300.0)),
            "detected_panel_model": str(display_profile.get("model_name", "")),
            "detected_nits_source": str(display_profile.get("source", "estimated_sdr")),
            "panel_type": live_panel_pref[0],
            "auto_panel_type": auto_panel_type,
            "solid_angle_sr": display_solid_angle_sr,
            "dark_mode": cached_dark_mode,
            "is_packaged_msix": packaged_msix,
            "ddc_diagnostic": display.ddc_diagnostic,
            "comfort_mode": bool(getattr(args, "comfort_mode", True)),
            "backlight_gamma": backlight_gamma,
            "onboarding_completed": bool(getattr(args, "onboarding_completed", True)),
            "awaiting_setup": onboarding_gate_active,
            "weekly_schedule": dict(getattr(args, "weekly_schedule", {}) or {}),
            "start_with_windows": bool(initial_start_with_windows),
            "live_conflicts": initial_conflicts,
            "capabilities": capabilities,
            "csv_path": args.log_csv,
            "on_settings_saved": _persist_ui_settings,
        },
    )
    ui_state = OrchestratorState(**state_init_kwargs)

    def _on_windows_resume() -> None:
        dispatcher.reset_cache()
        ui_state.wake_event.set()

    guard = SleepHandoffGuard(
        light=light,
        display=display,
        handoff_on_normal_exit=continuous_mode and args.light not in ("mock", "none"),
        on_resume=_on_windows_resume,
    )

    if args.web_ui:
        try:
            web_server = LocalWebUIServer(
                state=ui_state,
                host="127.0.0.1",
                port=int(args.ui_port),
            )
            web_server.start()
            print(f"Local Braun Control Panel: {web_server.url} (127.0.0.1 only, 100% offline)")
            if args.open_browser:
                try:
                    webbrowser.open(web_server.url)
                except Exception as exc:
                    logger.debug("Could not auto-open browser: %s", exc)
        except OSError as exc:
            logger.warning("Could not bind LocalWebUIServer on 127.0.0.1:%s (%s).", args.ui_port, exc)
            web_server = None

    if on_state_ready is not None:
        try:
            on_state_ready(ui_state, web_server)
        except Exception as exc:
            logger.debug("on_state_ready callback failed: %s", exc)

    if args.auto_pause:
        try:
            from orchestrator.display.app_watcher import (
                DEFAULT_COLOR_CRITICAL_APPS,
                ForegroundAppWatcher,
            )

            extra_apps = list(getattr(args, "auto_pause_apps", []) or [])
            combined_apps = (
                tuple(dict.fromkeys([*DEFAULT_COLOR_CRITICAL_APPS, *extra_apps]))
                if extra_apps
                else None
            )
            app_watcher = ForegroundAppWatcher(
                enabled=bool(ui_state.auto_pause_enabled),
                pause_on_fullscreen=bool(args.auto_pause_fullscreen),
                pause_on_fullscreen_browser=bool(ui_state.auto_pause_browser_fullscreen),
                pause_on_video_players=True,
                color_critical_apps=combined_apps,
            )
        except Exception as exc:
            logger.warning("ForegroundAppWatcher unavailable (%s); continuing without auto-pause.", exc)
            app_watcher = None

    if args.tray:
        try:
            from orchestrator.tray_win32 import (
                TrayActionController,
                TrayCallbacks,
                TrayStatusSnapshot,
                WindowsSystemTray,
            )

            ui_url = web_server.url if web_server is not None else f"http://127.0.0.1:{int(args.ui_port)}"

            def _tray_toggle_pause() -> None:
                if ui_state.is_paused()[0]:
                    ui_state.set_pause(0)
                else:
                    ui_state.set_pause(120)

            tray_callbacks = TrayCallbacks(
                on_toggle_pause=_tray_toggle_pause,
                on_pause_2h=lambda: ui_state.set_pause(120),
                on_resume=lambda: ui_state.set_pause(0),
                on_toggle_movie_mode=lambda: ui_state.toggle_movie_mode(),
                on_toggle_auto_pause=lambda: ui_state.update_settings(
                    auto_pause_enabled=not ui_state.auto_pause_enabled
                ),
                on_toggle_browser_fullscreen=lambda: ui_state.update_settings(
                    auto_pause_browser_fullscreen=not ui_state.auto_pause_browser_fullscreen
                ),
                on_toggle_sensitive_evening=lambda: ui_state.update_settings(
                    high_sensitivity_evening=not ui_state.high_sensitivity_evening
                ),
            )

            def _tray_status_provider() -> TrayStatusSnapshot:
                return TrayStatusSnapshot(
                    paused=ui_state.is_paused()[0],
                    movie_mode_active=ui_state.is_movie_mode_active(),
                    auto_pause_enabled=ui_state.auto_pause_enabled,
                    auto_pause_browser_fullscreen=ui_state.auto_pause_browser_fullscreen,
                    high_sensitivity_evening=ui_state.high_sensitivity_evening,
                )

            tray_ctrl = TrayActionController(
                state=ui_state,
                stop_event=stop_event,
                ui_url=ui_url,
                callbacks=tray_callbacks,
                status_provider=_tray_status_provider,
            )
            tray = WindowsSystemTray(
                controller=tray_ctrl,
                tooltip="Circadian Desk — Local Light Orchestrator",
                icon_path=_resolve_bundled_icon_path(),
            )
            tray.start()
        except Exception as exc:
            logger.warning("WindowsSystemTray unavailable (%s); continuing without tray icon.", exc)
            tray = None

    if args.mqtt_host:
        try:
            from orchestrator.mqtt_ha import HomeAssistantMQTTBridge

            mqtt_bridge = HomeAssistantMQTTBridge(
                host=str(args.mqtt_host),
                port=int(args.mqtt_port),
                username=str(args.mqtt_user or ""),
                password=str(args.mqtt_password or ""),
                discovery_prefix=str(args.mqtt_prefix or "homeassistant"),
            )
            if mqtt_bridge.connect():
                print(
                    f"Home Assistant MQTT Auto-Discovery connected: "
                    f"{args.mqtt_host}:{args.mqtt_port} (prefix: {args.mqtt_prefix})"
                )
        except Exception as exc:
            logger.warning("Home Assistant MQTT bridge unavailable (%s); continuing locally.", exc)
            mqtt_bridge = None

    was_paused = False
    last_tooltip = ""
    cached_curve_key: tuple[Any, ...] | None = None
    cached_curve: list[dict[str, Any]] = []

    def _get_schedule_curve(
        w_h: float,
        s_h: float,
        sens: bool,
        comfort: bool,
        max_nits: float,
        panel_t: str = "wled_ips",
        dark_m: bool | None = None,
    ) -> list[dict[str, Any]]:
        nonlocal cached_curve_key, cached_curve
        key = (w_h, s_h, sens, comfort, max_nits, backlight_gamma, panel_t, display_solid_angle_sr, dark_m)
        if key != cached_curve_key:
            cached_curve = _sample_schedule_curve(
                wake_hour=w_h,
                sleep_hour=s_h,
                high_sensitivity_evening=sens,
                comfort_mode=comfort,
                display_max_nits=max_nits,
                backlight_gamma=backlight_gamma,
                panel_type=panel_t,
                solid_angle_sr=display_solid_angle_sr,
                dark_mode=dark_m,
                compensate_lut=True,
            )
            cached_curve_key = key
        return cached_curve

    def _update_tray_tooltip(
        phase: str,
        mon_pct: int,
        mon_cct: int,
        manual_paused: bool,
        auto_pause_reason: str,
        movie_mode_active: bool = False,
        awaiting_setup: bool = False,
        target_nits: float = 0.0,
        profile_label: str = "Comfort",
        pause_remaining_s: int = 0,
    ) -> None:
        nonlocal last_tooltip
        if tray is None:
            return
        try:
            from orchestrator.tray_win32 import format_live_tray_tooltip

            tip = format_live_tray_tooltip(
                phase=phase,
                mon_pct=mon_pct,
                mon_cct=mon_cct,
                target_nits=target_nits,
                profile_label=profile_label,
                manual_paused=manual_paused,
                pause_remaining_s=pause_remaining_s,
                auto_pause_reason=auto_pause_reason,
                movie_mode_active=movie_mode_active,
                awaiting_setup=awaiting_setup,
            )
        except Exception:
            tip = f"Circadian Desk — {phase} ({mon_pct}% @ {mon_cct}K)"
        if tip != last_tooltip:
            last_tooltip = tip
            try:
                tray.update_tooltip(tip)
            except Exception:
                pass

    try:
        with ExitStack() as stack:
            telemetry_logger: Any = None
            if args.log_csv:
                from orchestrator.telemetry import TelemetryLogger

                telemetry_logger = stack.enter_context(TelemetryLogger(args.log_csv))

            if args.open_loop:
                guard.install(listen_windows_suspend=continuous_mode)
                eve_label = "<=5 lx (High-Sensitivity)" if args.sensitive_evening else "<=10 lx (Standard)"
                csv_label = f" | CSV: {args.log_csv}" if args.log_csv else ""
                print(
                    f"Eye-Level Spectral Light Orchestrator (v6.1) | Source: OPEN-LOOP (Zero Sensor) | "
                    f"Light: {args.light} | Monitor: {'ON' if args.monitor else 'OFF (dry-run)'} | "
                    f"Evening Cap: {eve_label}{csv_label}"
                )
                print("-" * 98)

                tick = 0
                was_awaiting_setup = onboarding_gate_active
                gentle_ramp_start_s: float | None = None
                gentle_ramp_duration_s: float = 8.0
                gentle_ramp_start_nits: float = 160.0
                gentle_ramp_start_cct: float = 6500.0
                while not stop_event.is_set():
                    ui_state.wake_event.clear()
                    awaiting_setup = bool(onboarding_gate_active and (not ui_state.onboarding_completed))
                    live_wake, live_sleep, live_sens = ui_state.get_settings()
                    live_weekly = ui_state.weekly_schedule
                    eff_wake, eff_sleep, active_day_key, social_jetlag_h = _resolve_effective_schedule(
                        args.hour, live_wake, live_sleep, live_weekly
                    )
                    live_comfort = ui_state.comfort_mode
                    live_max_nits = ui_state.effective_max_nits
                    eff_panel_type = _resolve_effective_panel_type()
                    manual_on, manual_bri, manual_cct = ui_state.get_manual_override()
                    movie_active, movie_rem_s = ui_state.is_movie_mode()

                    live_conflicts = _poll_live_conflicts()
                    hdr_auto_paused = bool(live_conflicts.get("hdr_active", False))

                    manual_paused, pause_rem_s = ui_state.is_paused()
                    if hdr_auto_paused and not manual_paused:
                        auto_paused = True
                        auto_pause_reason = "Windows HDR Active"
                    else:
                        auto_paused, auto_pause_reason = _evaluate_auto_pause(
                            app_watcher, ui_state, manual_paused
                        )
                    effective_paused = manual_paused or auto_paused

                    if (not awaiting_setup) and (not effective_paused):
                        dispatcher.check_external_conflict(controller=None)
                    reading, action, resolved_hour = compute_open_loop_action(
                        hour_of_day=args.hour,
                        wake_hour=eff_wake,
                        sleep_hour=eff_sleep,
                        high_sensitivity_evening=live_sens,
                        comfort_mode=live_comfort,
                        display_max_nits=live_max_nits,
                        backlight_gamma=backlight_gamma,
                        panel_type=eff_panel_type,
                        solid_angle_sr=display_solid_angle_sr,
                        dark_mode=cached_dark_mode,
                        compensate_lut=True,
                    )
                    transition_label, target_screen_nits, est_corneal_m_edi = _resolve_open_loop_details(
                        hour_of_day=resolved_hour,
                        wake_hour=eff_wake,
                        sleep_hour=eff_sleep,
                        high_sensitivity_evening=live_sens,
                        comfort_mode=live_comfort,
                        display_max_nits=live_max_nits,
                        backlight_gamma=backlight_gamma,
                        fallback_phase=action.phase,
                        panel_type=eff_panel_type,
                        solid_angle_sr=display_solid_angle_sr,
                        dark_mode=cached_dark_mode,
                        compensate_lut=True,
                    )

                    # Trigger ~8s First-Evening Gentle Ramp when graduating out of AWAITING_SETUP in evening/night
                    if (not awaiting_setup) and was_awaiting_setup:
                        dispatcher.reset_cache()
                        was_awaiting_setup = False
                        if continuous_mode and action.phase in ("EVENING", "NIGHT"):
                            gentle_ramp_start_s = time.monotonic()
                            gentle_ramp_start_nits = max(120.0, min(live_max_nits, live_max_nits * 0.55))
                            gentle_ramp_start_cct = 6500.0

                    # 5-Level Priority (after AWAITING_SETUP gate):
                    # 1. Manual Pause 2h > 2. Auto-Pause (HDR / Creative / Games) > 3. Manual Sliders > 4. Movie Mode 2.5h > 5. Smooth Curve
                    movie_applied = False
                    gentle_ramp_active = False
                    if (not awaiting_setup) and (not effective_paused):
                        if manual_on:
                            gentle_ramp_start_s = None
                            action.monitor_brightness_pct = manual_bri
                            action.monitor_cct_k = manual_cct
                            target_screen_nits = _estimate_screen_nits(
                                manual_bri, live_max_nits, backlight_gamma
                            )
                            transition_label = "manual_override"
                        elif movie_active:
                            gentle_ramp_start_s = None
                            m_bri, m_cct, m_nits = _compute_movie_mode_targets(
                                live_max_nits, backlight_gamma
                            )
                            action.monitor_brightness_pct = m_bri
                            action.monitor_cct_k = m_cct
                            target_screen_nits = m_nits
                            transition_label = "movie_mode"
                            movie_applied = True
                        elif gentle_ramp_start_s is not None:
                            elapsed_r = time.monotonic() - gentle_ramp_start_s
                            ramp_fn = getattr(photobiology, "compute_first_evening_ramp_step", None)
                            if elapsed_r < gentle_ramp_duration_s and callable(ramp_fn):
                                r_nits, r_cct = ramp_fn(
                                    elapsed_s=elapsed_r,
                                    duration_s=gentle_ramp_duration_s,
                                    start_nits=gentle_ramp_start_nits,
                                    target_nits=max(20.0, target_screen_nits),
                                    start_cct_k=gentle_ramp_start_cct,
                                    target_cct_k=float(action.monitor_cct_k),
                                )
                                n2b = getattr(photobiology, "nits_to_backlight_pct", None)
                                if callable(n2b):
                                    action.monitor_brightness_pct = int(
                                        n2b(
                                            r_nits,
                                            max_nits=live_max_nits,
                                            gamma=backlight_gamma,
                                            cct_k=r_cct,
                                            panel_type=eff_panel_type,
                                            compensate_lut=True,
                                        )
                                    )
                                action.monitor_cct_k = int(r_cct)
                                target_screen_nits = float(r_nits)
                                transition_label = "gentle_ramp"
                                gentle_ramp_active = True
                            else:
                                gentle_ramp_start_s = None

                    guard.update_context(
                        phase=action.phase,
                        unlinked_light_alert=False,
                        last_bulb_power=False if awaiting_setup else (action.bulb_brightness_pct > 0),
                    )
                    if awaiting_setup:
                        was_awaiting_setup = True
                        dispatch_info = {
                            "bulb_sent": False,
                            "monitor_sent": False,
                            "conflict_observer_active": False,
                        }
                    elif effective_paused:
                        if not was_paused:
                            if args.monitor:
                                display.restore()
                            dispatcher.reset_cache()
                            was_paused = True
                        dispatch_info = {
                            "bulb_sent": False,
                            "monitor_sent": False,
                            "conflict_observer_active": False,
                        }
                    else:
                        was_paused = False
                        dispatch_info = dispatcher.dispatch(action, controller=None)

                    curve_pts = _get_schedule_curve(
                        eff_wake,
                        eff_sleep,
                        live_sens,
                        live_comfort,
                        live_max_nits,
                        panel_t=eff_panel_type,
                        dark_m=cached_dark_mode,
                    )
                    snap_kwargs = _filter_supported_kwargs(
                        ui_state.update_snapshot,
                        {
                            "mode": "open_loop",
                            "light_backend": str(args.light),
                            "phase": action.phase,
                            "transition_label": "awaiting_setup" if awaiting_setup else transition_label,
                            "m_edi_lx": float(action.measured_m_edi_lx),
                            "estimated_corneal_m_edi_lx": float(est_corneal_m_edi),
                            "uncertainty_k2_lx": 0.0,
                            "compliance_status": str(getattr(action, "compliance_status", "OPEN-LOOP")),
                            "reading_valid": True,
                            "sensor_occluded": False,
                            "conflict_observer_active": bool(dispatch_info["conflict_observer_active"]),
                            "unlinked_light_alert": False,
                            "alert_message": "",
                            "photopic_lx": float(reading.photopic_lx),
                            "cct_k": int(round(reading.cct_k)),
                            "m_der": float(reading.m_der),
                            "flicker_hz": float(reading.flicker_hz),
                            "bulb_power": bool(getattr(action, "bulb_power", action.bulb_brightness_pct > 0)),
                            "bulb_brightness_pct": int(action.bulb_brightness_pct),
                            "bulb_cct_k": int(action.bulb_cct_k),
                            "monitor_brightness_pct": int(action.monitor_brightness_pct),
                            "monitor_cct_k": int(action.monitor_cct_k),
                            "color_method": "none" if (awaiting_setup or effective_paused) else dispatcher.last_color_method,
                            "ddc_diagnostic": display.ddc_diagnostic,
                            "panel_type": getattr(ui_state, "panel_type", live_panel_pref[0]),
                            "auto_panel_type": auto_panel_type,
                            "solid_angle_sr": display_solid_angle_sr,
                            "dark_mode": cached_dark_mode,
                            "auto_paused": auto_paused,
                            "auto_pause_reason": auto_pause_reason,
                            "awaiting_setup": awaiting_setup,
                            "target_screen_nits": target_screen_nits,
                            "schedule_curve": curve_pts,
                            "current_hour": resolved_hour,
                            "active_day_key": active_day_key,
                            "social_jetlag_hours": social_jetlag_h,
                            "live_conflicts": live_conflicts,
                            "backlight_gamma": backlight_gamma,
                        },
                    )
                    ui_state.update_snapshot(**snap_kwargs)
                    prof_lbl = "Strict" if (live_sens or not live_comfort) else "Comfort"
                    _update_tray_tooltip(
                        action.phase,
                        int(action.monitor_brightness_pct),
                        int(action.monitor_cct_k),
                        manual_paused,
                        auto_pause_reason,
                        movie_mode_active=movie_applied,
                        awaiting_setup=awaiting_setup,
                        target_nits=target_screen_nits,
                        profile_label=prof_lbl,
                        pause_remaining_s=pause_rem_s,
                    )
                    if mqtt_bridge is not None:
                        try:
                            mqtt_bridge.publish_state(
                                _build_mqtt_payload(
                                    mode="open_loop",
                                    reading=reading,
                                    action=action,
                                    paused=effective_paused,
                                )
                            )
                        except Exception as exc:
                            logger.debug("MQTT publish_state failed: %s", exc)
                    if telemetry_logger is not None:
                        telemetry_logger.log_tick(
                            action=action,
                            mode="open_loop",
                            photopic_lx=float(reading.photopic_lx),
                            cct_k=int(round(reading.cct_k)),
                            m_der=float(reading.m_der),
                            flicker_hz=float(reading.flicker_hz),
                        )
                    tick += 1
                    print(
                        format_hud_line(
                            tick,
                            reading,
                            action,
                            resolved_hour,
                            use_color=use_color,
                            conflict_override=dispatch_info["conflict_observer_active"],
                            pause_remaining_s=pause_rem_s,
                            auto_pause_reason=auto_pause_reason,
                            movie_mode_remaining_s=movie_rem_s if movie_applied else 0,
                        ),
                        flush=True,
                    )
                    if args.ticks > 0 and tick >= args.ticks:
                        break
                    wait_s = min(0.5, args.interval) if gentle_ramp_active else args.interval
                    if _wait_for_next_tick(wait_s, stop_event, ui_state.wake_event):
                        break
                return 0

            converter = AS7343Converter(matrix_path=args.matrix)
            sensor = stack.enter_context(build_sensor_source(args))
            if args.calibrate:
                run_calibration_wizard(
                    sensor=sensor,
                    converter=converter,
                    light=light,
                    profile_path=profile_path,
                    settle_seconds=0.1 if args.simulate else 1.5,
                    interactive=not bool(args.simulate),
                )
                return 0

            profile: RoomProfile | None = None
            if profile_path.exists():
                try:
                    profile = RoomProfile.load(str(profile_path))
                except Exception as exc:
                    logger.warning("Could not load %s (%s); continuing with defaults.", profile_path, exc)

            controller = _build_controller(args, profile)
            guard.install(listen_windows_suspend=continuous_mode)

            mode_label = f"USB-Serial ({args.port})" if args.port else f"SIMULATE ({args.simulate})"
            eve_label = "<=5 lx (High-Sensitivity)" if args.sensitive_evening else "<=10 lx (Standard)"
            csv_label = f" | CSV: {args.log_csv}" if args.log_csv else ""
            print(
                f"Eye-Level Spectral Light Orchestrator (v6.1) | Source: {mode_label} | "
                f"Light: {args.light} | Monitor: {'ON' if args.monitor else 'OFF (dry-run)'} | "
                f"Evening Cap: {eve_label}{csv_label}"
            )
            print("-" * 98)

            tick = 0
            was_awaiting_setup = onboarding_gate_active
            while not stop_event.is_set():
                ui_state.wake_event.clear()
                awaiting_setup = bool(onboarding_gate_active and (not ui_state.onboarding_completed))
                live_wake, live_sleep, live_sens = ui_state.get_settings()
                live_weekly = ui_state.weekly_schedule
                live_max_nits = ui_state.effective_max_nits
                manual_on, manual_bri, manual_cct = ui_state.get_manual_override()
                movie_active, movie_rem_s = ui_state.is_movie_mode()

                sim_hour = _resolve_sim_hour(args, tick)
                eff_wake, eff_sleep, active_day_key, social_jetlag_h = _resolve_effective_schedule(
                    sim_hour, live_wake, live_sleep, live_weekly
                )
                controller.wake_hour = eff_wake
                controller.sleep_hour = eff_sleep
                if hasattr(controller, "high_sensitivity_evening"):
                    controller.high_sensitivity_evening = live_sens
                live_conflicts = _poll_live_conflicts()
                hdr_auto_paused = bool(live_conflicts.get("hdr_active", False))
                manual_paused, pause_rem_s = ui_state.is_paused()
                if hdr_auto_paused and not manual_paused:
                    auto_paused = True
                    auto_pause_reason = "Windows HDR Active"
                else:
                    auto_paused, auto_pause_reason = _evaluate_auto_pause(
                        app_watcher, ui_state, manual_paused
                    )
                effective_paused = manual_paused or auto_paused

                # Poll external bulb override BEFORE controller.step() mutates internal state.
                if (not awaiting_setup) and (not effective_paused):
                    dispatcher.check_external_conflict(controller=controller)
                sample = sensor.next_sample()
                reading = converter.convert(sample)
                action = controller.step(reading, hour_of_day=sim_hour)

                # 5-Level Priority (after AWAITING_SETUP gate):
                # 1. Manual Pause 2h > 2. Auto-Pause > 3. Manual Sliders > 4. Movie Mode 2.5h > 5. Closed-Loop
                movie_applied = False
                closed_transition_label = "awaiting_setup" if awaiting_setup else action.phase.lower()
                if (not awaiting_setup) and (not effective_paused):
                    if manual_on:
                        action.monitor_brightness_pct = manual_bri
                        action.monitor_cct_k = manual_cct
                        closed_transition_label = "manual_override"
                    elif movie_active:
                        m_bri, m_cct, _ = _compute_movie_mode_targets(
                            live_max_nits, backlight_gamma
                        )
                        action.monitor_brightness_pct = m_bri
                        action.monitor_cct_k = m_cct
                        closed_transition_label = "movie_mode"
                        movie_applied = True

                guard.update_context(
                    phase=action.phase,
                    unlinked_light_alert=action.unlinked_light_alert,
                    last_bulb_power=False
                    if awaiting_setup
                    else bool(getattr(action, "bulb_power", action.bulb_brightness_pct > 0)),
                )

                if awaiting_setup:
                    was_awaiting_setup = True
                    dispatch_info = {
                        "bulb_sent": False,
                        "monitor_sent": False,
                        "conflict_observer_active": False,
                    }
                elif effective_paused:
                    if not was_paused:
                        if args.monitor:
                            display.restore()
                        dispatcher.reset_cache()
                        was_paused = True
                    dispatch_info = {
                        "bulb_sent": False,
                        "monitor_sent": False,
                        "conflict_observer_active": bool(
                            getattr(action, "conflict_observer_active", False)
                        ),
                    }
                else:
                    if was_awaiting_setup:
                        dispatcher.reset_cache()
                        was_awaiting_setup = False
                    was_paused = False
                    dispatch_info = dispatcher.dispatch(action, controller=controller)

                live_conflicts = _poll_live_conflicts()
                est_screen_nits = _estimate_screen_nits(
                    int(action.monitor_brightness_pct), live_max_nits, backlight_gamma
                )
                ui_state.update_snapshot(
                    mode=mode_tag,
                    light_backend=str(args.light),
                    phase=action.phase,
                    transition_label=closed_transition_label,
                    m_edi_lx=float(action.measured_m_edi_lx),
                    uncertainty_k2_lx=float(
                        getattr(action, "uncertainty_k2_lx", getattr(reading, "uncertainty_k2_lx", 0.0))
                    ),
                    compliance_status=str(getattr(action, "compliance_status", "BELIRSIZ")),
                    reading_valid=bool(getattr(action, "reading_valid", True)),
                    sensor_occluded=bool(getattr(action, "sensor_occluded", False)),
                    conflict_observer_active=bool(
                        getattr(action, "conflict_observer_active", False)
                        or dispatch_info["conflict_observer_active"]
                    ),
                    unlinked_light_alert=bool(action.unlinked_light_alert),
                    alert_message=str(action.alert_message or ""),
                    photopic_lx=float(reading.photopic_lx),
                    cct_k=int(round(reading.cct_k)),
                    m_der=float(reading.m_der),
                    flicker_hz=float(reading.flicker_hz),
                    bulb_power=bool(getattr(action, "bulb_power", action.bulb_brightness_pct > 0)),
                    bulb_brightness_pct=int(action.bulb_brightness_pct),
                    bulb_cct_k=int(action.bulb_cct_k),
                    monitor_brightness_pct=int(action.monitor_brightness_pct),
                    monitor_cct_k=int(action.monitor_cct_k),
                    color_method="none" if (awaiting_setup or effective_paused) else dispatcher.last_color_method,
                    auto_paused=auto_paused,
                    auto_pause_reason=auto_pause_reason,
                    awaiting_setup=awaiting_setup,
                    target_screen_nits=est_screen_nits,
                    current_hour=sim_hour,
                    active_day_key=active_day_key,
                    social_jetlag_hours=social_jetlag_h,
                    live_conflicts=live_conflicts,
                    backlight_gamma=backlight_gamma,
                )
                _update_tray_tooltip(
                    action.phase,
                    int(action.monitor_brightness_pct),
                    int(action.monitor_cct_k),
                    manual_paused,
                    auto_pause_reason,
                    movie_mode_active=movie_applied,
                    awaiting_setup=awaiting_setup,
                )
                if mqtt_bridge is not None:
                    try:
                        mqtt_bridge.publish_state(
                            _build_mqtt_payload(
                                mode=mode_tag,
                                reading=reading,
                                action=action,
                                paused=effective_paused,
                            )
                        )
                    except Exception as exc:
                        logger.debug("MQTT publish_state failed: %s", exc)
                if telemetry_logger is not None:
                    telemetry_logger.log_tick(
                        action=action,
                        mode=mode_tag,
                        photopic_lx=float(reading.photopic_lx),
                        cct_k=int(round(reading.cct_k)),
                        m_der=float(reading.m_der),
                        flicker_hz=float(reading.flicker_hz),
                    )

                tick += 1
                print(
                    format_hud_line(
                        tick,
                        reading,
                        action,
                        sim_hour,
                        use_color=use_color,
                        conflict_override=dispatch_info["conflict_observer_active"],
                        pause_remaining_s=pause_rem_s,
                        auto_pause_reason=auto_pause_reason,
                        movie_mode_remaining_s=movie_rem_s if movie_applied else 0,
                    ),
                    flush=True,
                )

                if args.ticks > 0 and tick >= args.ticks:
                    break
                if _wait_for_next_tick(args.interval, stop_event, ui_state.wake_event):
                    break

    except KeyboardInterrupt:
        print("\nOrchestrator stopped by user.")
    finally:
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        if web_server is not None:
            web_server.stop()
        if mqtt_bridge is not None:
            try:
                mqtt_bridge.close()
            except Exception:
                pass
        guard.stop()
        display.restore()
        light.close()
        release_single_instance_mutex(mutex_handle)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
