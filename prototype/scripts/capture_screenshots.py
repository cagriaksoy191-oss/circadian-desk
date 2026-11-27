"""Deterministic studio-quality README screenshot generator for Circadian Desk.

Starts an isolated in-process `LocalWebUIServer` on a random `127.0.0.1` loopback port
with realistic, hardware-free `OrchestratorState` snapshots (NEVER touches live display
hardware or user config files), then drives headless Microsoft Edge / Chrome at 2x HiDPI
to regenerate every PNG under `docs/screenshots/`.

Re-run anytime the UI changes so GitHub README visuals stay 100% in sync:
    .\\prototype\\.venv\\Scripts\\python.exe prototype\\scripts\\capture_screenshots.py
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE_ROOT = REPO_ROOT / "prototype"
OUT_DIR = REPO_ROOT / "docs" / "screenshots"

if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from orchestrator.telemetry import CSV_COLUMNS
from orchestrator.web_ui import LocalWebUIServer, OrchestratorState
from spectral.photobiology import (
    compute_smooth_open_loop_targets,
    nits_to_backlight_pct,
    sample_24h_curve,
)


def find_chromium_browser() -> Path:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    for cmd in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(cmd)
        if found:
            return Path(found)
    raise RuntimeError(
        "Microsoft Edge or Google Chrome not found. Install Edge/Chrome to capture screenshots."
    )


def write_demo_7day_csv(csv_path: Path) -> None:
    """Create a realistic 7-day sample CSV for the offline SVG compliance report."""
    now_utc = datetime(2026, 11, 27, 21, 0, tzinfo=timezone.utc)
    start_utc = now_utc - timedelta(days=7)

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for day_idx in range(7):
            for hour in (8, 10, 12, 14, 16, 18, 20, 21, 22):
                ts = start_utc + timedelta(days=day_idx, hours=hour)
                day_jitter = (day_idx - 3) * 4.0
                if hour < 17:
                    phase = "DAY"
                    m_edi = max(290.0, 355.0 - abs(hour - 12.5) * 12.0 + day_jitter)
                    cct = 5600 if hour <= 14 else 4900
                    m_der = 0.84
                    photopic = round(m_edi / m_der, 1)
                    mon_pct = 74
                    mon_cct = cct
                    bulb_pct = 72
                    bulb_cct = cct
                    unc = round(m_edi * 0.17, 2)
                    status = "SAGLANDI" if (m_edi - unc) >= 250.0 else "BELIRSIZ"
                elif hour < 20:
                    phase = "DAY"
                    m_edi = round(148.0 + day_jitter * 0.5, 1)
                    cct = 3900
                    m_der = 0.58
                    photopic = round(m_edi / m_der, 1)
                    mon_pct = 54
                    mon_cct = 3900
                    bulb_pct = 42
                    bulb_cct = 3700
                    unc = round(m_edi * 0.17, 2)
                    status = "BELIRSIZ"
                else:
                    phase = "EVENING"
                    m_edi = round(max(3.2, 7.6 - (hour - 20) * 1.9 + day_jitter * 0.08), 2)
                    cct = 3050 if hour == 20 else 2750
                    m_der = 0.36
                    photopic = round(m_edi / m_der, 1)
                    mon_pct = 38 if hour == 20 else 32
                    mon_cct = 3150 if hour == 20 else 3000
                    bulb_pct = 24 if hour == 20 else 14
                    bulb_cct = 2700 if hour == 20 else 2350
                    unc = round(m_edi * 0.17, 2)
                    status = "SAGLANDI" if (m_edi + unc) <= 10.0 else "BELIRSIZ"

                writer.writerow(
                    {
                        "timestamp_iso": ts.isoformat().replace("+00:00", "Z"),
                        "mode": "sensor",
                        "phase": phase,
                        "m_edi_lx": round(m_edi, 2),
                        "uncertainty_k2_lx": unc,
                        "compliance_status": status,
                        "photopic_lx": photopic,
                        "cct_k": cct,
                        "m_der": m_der,
                        "flicker_hz": 0.0,
                        "bulb_power": True,
                        "bulb_pct": bulb_pct,
                        "bulb_cct_k": bulb_cct,
                        "monitor_pct": mon_pct,
                        "monitor_cct_k": mon_cct,
                        "unlinked_alert": False,
                        "sensor_occluded": False,
                        "conflict_observer": False,
                        "reading_valid": True,
                    }
                )


def capture_url(
    browser: Path,
    url: str,
    out_png: Path,
    *,
    width: int = 1400,
    height: int = 860,
    scale: int = 2,
    virtual_time_ms: int = 3500,
) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cdesk_shot_") as profile_dir:
        cmd = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-scrollbars",
            "--disable-background-networking",
            f"--user-data-dir={profile_dir}",
            f"--force-device-scale-factor={scale}",
            f"--window-size={width},{height}",
            f"--virtual-time-budget={virtual_time_ms}",
            f"--screenshot={out_png}",
            url,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if res.returncode != 0 or not out_png.is_file() or out_png.stat().st_size < 10_000:
            raise RuntimeError(
                f"Screenshot failed for {out_png.name} (rc={res.returncode}):\n"
                f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
            )


def _base_capabilities(*, max_nits: float = 496.7, model: str = "NE160QDM-NZL") -> dict[str, Any]:
    return {
        "is_windows": True,
        "wmi_laptop_available": True,
        "ddc_external_supported": True,
        "gdi_gamma_supported": False,
        "magnification_available": True,
        "windows_night_light_detected": False,
        "display_profile": {
            "max_nits": max_nits,
            "min_nits": 0.4,
            "model_name": model,
            "source": "edid_cta861",
            "is_auto_detected": True,
            "diagonal_in": 16.0,
            "solid_angle_sr": 0.225,
            "gamut_dci_p3_pct": 80.9,
            "auto_panel_type": "wled_ips",
        },
    }


def build_evening_cockpit_state(csv_path: Path) -> OrchestratorState:
    wake_h, sleep_h = 7.5, 23.5
    cur_h = 21.25  # 21:15 smooth sunset ramp
    curve = sample_24h_curve(
        wake_hour=wake_h,
        sleep_hour=sleep_h,
        display_max_nits=496.7,
        comfort_mode=True,
        panel_type="wled_ips",
        dark_mode=True,
        solid_angle_sr=0.225,
        include_end=True,
    )
    target = compute_smooth_open_loop_targets(
        cur_h,
        wake_hour=wake_h,
        sleep_hour=sleep_h,
        display_max_nits=496.7,
        comfort_mode=True,
        panel_type="wled_ips",
        dark_mode=True,
        solid_angle_sr=0.225,
    )
    pct = nits_to_backlight_pct(
        target["target_screen_nits"],
        max_nits=496.7,
        gamma=2.0,
        cct_k=target["monitor_cct_k"],
        panel_type="wled_ips",
        compensate_lut=True,
    )
    state = OrchestratorState(
        mode="open_loop",
        light_backend="none",
        wake_hour=wake_h,
        sleep_hour=sleep_h,
        high_sensitivity_evening=False,
        auto_pause_enabled=True,
        auto_pause_browser_fullscreen=False,
        detected_max_nits=496.7,
        detected_panel_model="NE160QDM-NZL",
        detected_nits_source="edid_cta861",
        comfort_mode=True,
        onboarding_completed=True,
        awaiting_setup=False,
        weekly_schedule={
            "mon": {"wake_hour": 7.5, "sleep_hour": 23.5},
            "tue": {"wake_hour": 7.5, "sleep_hour": 23.5},
            "wed": {"wake_hour": 7.5, "sleep_hour": 23.5},
            "thu": {"wake_hour": 7.5, "sleep_hour": 23.5},
            "fri": {"wake_hour": 7.5, "sleep_hour": 23.5},
            "sat": {"wake_hour": 9.5, "sleep_hour": 1.0},
            "sun": {"wake_hour": 9.5, "sleep_hour": 23.5},
        },
        active_day_key="thu",
        social_jetlag_hours=1.5,
        start_with_windows=True,
        capabilities=_base_capabilities(),
        panel_type="auto",
        auto_panel_type="wled_ips",
        diagonal_in=16.0,
        solid_angle_sr=0.225,
        dark_mode=True,
        estimated_corneal_m_edi_lx=float(target["estimated_corneal_m_edi_lx"]),
        hdr_auto_paused=False,
        ddc_diagnostic="WMI laptop backlight OK",
        is_packaged_msix=False,
        csv_path=csv_path,
    )
    state.update_snapshot(
        phase=str(target["phase"]),
        transition_label=str(target["transition_label"]),
        compliance_status="OPEN-LOOP · COMFORT",
        target_screen_nits=float(target["target_screen_nits"]),
        monitor_brightness_pct=pct,
        monitor_cct_k=int(target["monitor_cct_k"]),
        cct_k=int(target["monitor_cct_k"]),
        color_method="magnification",
        schedule_curve=curve,
        current_hour=cur_h,
        estimated_corneal_m_edi_lx=float(target["estimated_corneal_m_edi_lx"]),
        dark_mode=True,
    )
    return state


def build_onboarding_state(csv_path: Path) -> OrchestratorState:
    curve = sample_24h_curve(
        wake_hour=7.5,
        sleep_hour=23.5,
        display_max_nits=496.7,
        comfort_mode=True,
        panel_type="wled_ips",
        dark_mode=True,
        solid_angle_sr=0.225,
        include_end=True,
    )
    state = OrchestratorState(
        mode="open_loop",
        light_backend="none",
        wake_hour=7.5,
        sleep_hour=23.5,
        detected_max_nits=496.7,
        detected_panel_model="NE160QDM-NZL",
        detected_nits_source="edid_cta861",
        onboarding_completed=False,
        awaiting_setup=True,
        weekly_schedule={
            "mon": {"wake_hour": 7.5, "sleep_hour": 23.5},
            "tue": {"wake_hour": 7.5, "sleep_hour": 23.5},
            "wed": {"wake_hour": 7.5, "sleep_hour": 23.5},
            "thu": {"wake_hour": 7.5, "sleep_hour": 23.5},
            "fri": {"wake_hour": 7.5, "sleep_hour": 23.5},
            "sat": {"wake_hour": 9.5, "sleep_hour": 1.0},
            "sun": {"wake_hour": 9.5, "sleep_hour": 23.5},
        },
        capabilities=_base_capabilities(),
        panel_type="auto",
        auto_panel_type="wled_ips",
        diagonal_in=16.0,
        solid_angle_sr=0.225,
        dark_mode=True,
        is_packaged_msix=True,
        csv_path=csv_path,
    )
    state.update_snapshot(
        phase="AWAITING_SETUP",
        transition_label="awaiting_setup",
        compliance_status="AWAITING SETUP",
        target_screen_nits=180.0,
        monitor_brightness_pct=60,
        monitor_cct_k=6500,
        cct_k=6500,
        schedule_curve=curve,
        current_hour=20.0,
        awaiting_setup=True,
    )
    return state


def build_hdr_and_hue_state(csv_path: Path) -> OrchestratorState:
    wake_h, sleep_h = 8.0, 23.5
    cur_h = 22.0
    curve = sample_24h_curve(
        wake_hour=wake_h,
        sleep_hour=sleep_h,
        display_max_nits=600.0,
        comfort_mode=True,
        panel_type="qd_oled",
        dark_mode=True,
        solid_angle_sr=0.4604,
        include_end=True,
    )
    state = OrchestratorState(
        mode="open_loop",
        light_backend="hue",
        wake_hour=wake_h,
        sleep_hour=sleep_h,
        auto_pause_enabled=True,
        detected_max_nits=600.0,
        detected_panel_model="AW2725DF QD-OLED",
        detected_nits_source="edid_cta861",
        comfort_mode=True,
        onboarding_completed=True,
        awaiting_setup=False,
        active_day_key="fri",
        start_with_windows=True,
        live_conflicts={
            "night_light_on": False,
            "color_filter_on": False,
            "hdr_active": True,
            "acm_active": False,
            "competing_apps": [],
            "warnings": [],
        },
        capabilities=_base_capabilities(max_nits=600.0, model="AW2725DF QD-OLED"),
        panel_type="qd_oled",
        auto_panel_type="qd_oled",
        diagonal_in=26.7,
        solid_angle_sr=0.4604,
        dark_mode=True,
        estimated_corneal_m_edi_lx=3.4,
        hdr_auto_paused=True,
        ddc_diagnostic="HDR active — DDC/CI frozen cleanly",
        is_packaged_msix=True,
        csv_path=csv_path,
    )
    state.update_snapshot(
        phase="EVENING",
        transition_label="sunset_ramp",
        compliance_status="HDR AUTO-PAUSED",
        target_screen_nits=55.0,
        monitor_brightness_pct=42,
        monitor_cct_k=2850,
        cct_k=2850,
        bulb_power=True,
        bulb_brightness_pct=28,
        bulb_cct_k=2700,
        color_method="none",
        schedule_curve=curve,
        current_hour=cur_h,
        estimated_corneal_m_edi_lx=3.4,
        hdr_auto_paused=True,
        dark_mode=True,
    )
    return state


def main() -> int:
    browser = find_chromium_browser()
    print(f"Using Chromium engine: {browser}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cdesk_csv_") as tmp_dir:
        csv_path = Path(tmp_dir) / "demo_7day.csv"
        write_demo_7day_csv(csv_path)

        shots: list[tuple[str, OrchestratorState, str, int, int]] = [
            ("cockpit-evening.png", build_evening_cockpit_state(csv_path), "/", 1400, 740),
            ("onboarding-setup.png", build_onboarding_state(csv_path), "/", 1400, 700),
            ("cockpit-hdr-pause.png", build_hdr_and_hue_state(csv_path), "/", 1400, 740),
            ("report-7day.png", build_evening_cockpit_state(csv_path), "/report", 1280, 926),
        ]

        for filename, state, route, width, height in shots:
            out_path = OUT_DIR / filename
            with LocalWebUIServer(state, host="127.0.0.1", port=0) as srv:
                url = f"http://127.0.0.1:{srv.actual_port}{route}"
                capture_url(browser, url, out_path, width=width, height=height, scale=2)
                print(f"  [OK] {out_path.relative_to(REPO_ROOT)} ({out_path.stat().st_size:,} bytes)")

    print("All studio screenshots generated cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
