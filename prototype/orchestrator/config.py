"""Persistent user configuration for Pure Track A (v6.1) Circadian Desk Beta.

100% Python standard library — zero external dependencies, zero cloud telemetry.
Uses atomic temp-file writes (`os.replace`) and gracefully recovers with a warning
on missing or malformed JSON files.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

APP_DATA_DIR_NAME: str = "CircadianDesk"
LEGACY_APP_DATA_DIR: Path = Path.home() / ".circadian_orchestrator"
LEGACY_CONFIG_PATH: Path = LEGACY_APP_DATA_DIR / "config.json"


def get_app_data_dir() -> Path:
    """Return writable per-user app data directory (`%LOCALAPPDATA%\\CircadianDesk` on Windows).

    Satisfies Microsoft Store Policy 10.2.7 (clean uninstall & zero read-only CWD writes under
    MSIX `WindowsApps`) while staying backward-compatible on non-Windows platforms.
    """
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            return Path(local_app) / APP_DATA_DIR_NAME
        return Path.home() / "AppData" / "Local" / APP_DATA_DIR_NAME
    return LEGACY_APP_DATA_DIR


DEFAULT_CONFIG_PATH: Path = get_app_data_dir() / "config.json"

VALID_MODES: tuple[str, ...] = ("open_loop", "simulate", "serial")
VALID_LIGHTS: tuple[str, ...] = ("none", "mock", "wiz", "hue")
VALID_PANEL_TYPES: tuple[str, ...] = ("auto", "wled_ips", "p3_ips", "woled", "qd_oled")
WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class UserConfig:
    """Saved local defaults for Week-1 Open-Loop Desktop Beta & closed-loop sessions."""

    wake_hour: float = 7.0
    sleep_hour: float = 23.0
    mode: str = "open_loop"
    light: str = "none"
    wiz_ip: str = ""
    hue_ip: str = ""
    hue_key: str = ""
    hue_group: str = ""
    monitor_enabled: bool = True
    sensitive_evening: bool = False
    interval_s: float = 5.0
    log_csv: str = ""
    profile_path: str = "room_profile.json"
    web_ui_enabled: bool = True
    web_ui_port: int = 8765
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_prefix: str = "homeassistant"
    auto_pause_enabled: bool = True
    auto_pause_fullscreen: bool = True
    auto_pause_apps: list[str] = field(default_factory=list)
    tray_enabled: bool = True
    display_max_nits: float = 0.0
    panel_type: str = "auto"
    comfort_mode: bool = True
    onboarding_completed: bool = False
    weekly_schedule: dict[str, dict[str, float]] = field(default_factory=dict)
    auto_pause_browser_fullscreen: bool = False
    start_with_windows: bool = False


def _coerce_weekly_schedule(value: Any) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, dict[str, float]] = {}
    for raw_day, entry in value.items():
        day = str(raw_day or "").strip().lower()[:3]
        if day not in WEEKDAY_KEYS or not isinstance(entry, dict):
            continue
        w_raw = entry.get("wake_hour")
        s_raw = entry.get("sleep_hour")
        if isinstance(w_raw, bool) or isinstance(s_raw, bool):
            continue
        try:
            w_val = float(w_raw)  # type: ignore[arg-type]
            s_val = float(s_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if (0.0 <= w_val < 24.0) and (0.0 <= s_val < 24.0) and abs(w_val - s_val) >= 1e-6:
            cleaned[day] = {"wake_hour": round(w_val, 2), "sleep_hour": round(s_val, 2)}
    return cleaned


def _coerce_max_nits(value: Any, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    if num == 0.0:
        return 0.0
    if 150.0 <= num <= 2000.0:
        return round(num, 1)
    return fallback


def _coerce_str_list(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    cleaned: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip().lower()
        if text:
            cleaned.append(text)
    return cleaned


def _coerce_port(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        num = int(value)
    except (TypeError, ValueError):
        return fallback
    if not (1 <= num <= 65535):
        return fallback
    return num


def _coerce_hour(value: Any, fallback: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    if not (0.0 <= num < 24.0):
        return fallback
    return num


def _coerce_nonneg_float(value: Any, fallback: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    if num < 0.0:
        return fallback
    return num


def _coerce_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "y", "on"):
            return True
        if low in ("false", "0", "no", "n", "off"):
            return False
    return fallback


def _coerce_str(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _coerce_panel_type(value: Any, fallback: str) -> str:
    raw = _coerce_str(value, fallback).lower().replace("-", "_").replace(" ", "_")
    if raw in VALID_PANEL_TYPES:
        return raw
    if raw in ("qd_ips", "qdoled"):
        return "qd_oled"
    if raw in ("ksf_ips", "dci_p3", "p3"):
        return "p3_ips"
    if raw in ("oled", "w_oled"):
        return "woled"
    if raw in ("wled", "ips", "lcd", "srgb"):
        return "wled_ips"
    return fallback


def load_config(path: str | Path | None = None) -> UserConfig:
    """Load `UserConfig` from JSON, recovering cleanly with defaults if missing or corrupt.

    When `path is None` (or matches `DEFAULT_CONFIG_PATH`) and the new `%LOCALAPPDATA%\\CircadianDesk\\config.json`
    does not yet exist, transparently falls back to reading legacy `~/.circadian_orchestrator/config.json`
    so existing beta schedules migrate seamlessly on next save.
    """
    using_default = path is None
    target = Path(path).expanduser() if path is not None else get_app_data_dir() / "config.json"
    defaults = UserConfig()

    if not target.exists():
        default_resolved = (get_app_data_dir() / "config.json").resolve()
        try:
            is_default_target = using_default or (target.resolve() == default_resolved)
        except OSError:
            is_default_target = using_default
        if is_default_target and LEGACY_CONFIG_PATH.is_file():
            logger.info(
                "Migrating legacy config from %s -> %s on next save.",
                LEGACY_CONFIG_PATH,
                target,
            )
            target = LEGACY_CONFIG_PATH
        else:
            return defaults

    try:
        raw_text = target.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "Could not read config at %s (%s); falling back to safe defaults.",
            target,
            exc,
        )
        return defaults

    if not isinstance(data, dict):
        logger.warning(
            "Config at %s is not a JSON object (got %s); falling back to safe defaults.",
            target,
            type(data).__name__,
        )
        return defaults

    mode_val = _coerce_str(data.get("mode"), defaults.mode).lower()
    if mode_val not in VALID_MODES:
        logger.warning("Unknown config mode %r in %s; using %r.", mode_val, target, defaults.mode)
        mode_val = defaults.mode

    light_val = _coerce_str(data.get("light"), defaults.light).lower()
    if light_val not in VALID_LIGHTS:
        logger.warning("Unknown config light %r in %s; using %r.", light_val, target, defaults.light)
        light_val = defaults.light

    return UserConfig(
        wake_hour=_coerce_hour(data.get("wake_hour"), defaults.wake_hour),
        sleep_hour=_coerce_hour(data.get("sleep_hour"), defaults.sleep_hour),
        mode=mode_val,
        light=light_val,
        wiz_ip=_coerce_str(data.get("wiz_ip"), defaults.wiz_ip),
        hue_ip=_coerce_str(data.get("hue_ip"), defaults.hue_ip),
        hue_key=_coerce_str(data.get("hue_key"), defaults.hue_key),
        hue_group=_coerce_str(data.get("hue_group"), defaults.hue_group),
        monitor_enabled=_coerce_bool(data.get("monitor_enabled"), defaults.monitor_enabled),
        sensitive_evening=_coerce_bool(data.get("sensitive_evening"), defaults.sensitive_evening),
        interval_s=_coerce_nonneg_float(data.get("interval_s"), defaults.interval_s),
        log_csv=_coerce_str(data.get("log_csv"), defaults.log_csv),
        profile_path=_coerce_str(data.get("profile_path"), defaults.profile_path)
        or defaults.profile_path,
        web_ui_enabled=_coerce_bool(data.get("web_ui_enabled"), defaults.web_ui_enabled),
        web_ui_port=_coerce_port(data.get("web_ui_port"), defaults.web_ui_port),
        mqtt_host=_coerce_str(data.get("mqtt_host"), defaults.mqtt_host),
        mqtt_port=_coerce_port(data.get("mqtt_port"), defaults.mqtt_port),
        mqtt_user=_coerce_str(data.get("mqtt_user"), defaults.mqtt_user),
        mqtt_password=_coerce_str(data.get("mqtt_password"), defaults.mqtt_password),
        mqtt_prefix=_coerce_str(data.get("mqtt_prefix"), defaults.mqtt_prefix)
        or defaults.mqtt_prefix,
        auto_pause_enabled=_coerce_bool(
            data.get("auto_pause_enabled"), defaults.auto_pause_enabled
        ),
        auto_pause_fullscreen=_coerce_bool(
            data.get("auto_pause_fullscreen"), defaults.auto_pause_fullscreen
        ),
        auto_pause_apps=_coerce_str_list(
            data.get("auto_pause_apps"), defaults.auto_pause_apps
        ),
        tray_enabled=_coerce_bool(data.get("tray_enabled"), defaults.tray_enabled),
        display_max_nits=_coerce_max_nits(
            data.get("display_max_nits"), defaults.display_max_nits
        ),
        panel_type=_coerce_panel_type(data.get("panel_type"), defaults.panel_type),
        comfort_mode=_coerce_bool(data.get("comfort_mode"), defaults.comfort_mode),
        onboarding_completed=_coerce_bool(
            data.get("onboarding_completed"), defaults.onboarding_completed
        ),
        weekly_schedule=_coerce_weekly_schedule(data.get("weekly_schedule")),
        auto_pause_browser_fullscreen=_coerce_bool(
            data.get("auto_pause_browser_fullscreen"),
            defaults.auto_pause_browser_fullscreen,
        ),
        start_with_windows=_coerce_bool(
            data.get("start_with_windows"), defaults.start_with_windows
        ),
    )


def save_config(cfg: UserConfig, path: str | Path | None = None) -> Path:
    """Atomically write `UserConfig` to JSON using a same-directory temp file + `os.replace`."""
    target = Path(path).expanduser() if path is not None else get_app_data_dir() / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(asdict(cfg), indent=2, sort_keys=False) + "\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(target))
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return target
