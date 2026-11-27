"""End-to-end tests for photobiology, control safety, light drivers, display LUT, and CLI."""

from __future__ import annotations

from datetime import datetime
import http.client
import json
import signal
import subprocess
import sys
import threading
import time
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

import build_windows
from orchestrator.config import UserConfig, load_config, save_config
from orchestrator.daemon import (
    CommandDispatcher,
    _wait_for_next_tick,
    build_arg_parser,
    compute_open_loop_action,
    format_hud_line,
    main as daemon_main,
    resolve_default_argv,
    run_monitor_safety_check,
    run_setup_wizard,
)
from orchestrator.display.app_watcher import (
    BROWSER_APPS,
    DEDICATED_VIDEO_PLAYERS,
    ForegroundAppInfo,
    ForegroundAppWatcher,
)
from orchestrator.display.windows_display import (
    IDENTITY_COLOR_MATRIX,
    SleepHandoffGuard,
    WindowsDisplayController,
    build_color_effect_matrix,
    build_gamma_ramp,
    detect_display_luminance_profile,
    detect_live_display_conflicts,
    kelvin_to_rgb_scalars,
    parse_cta861_max_nits,
)
from orchestrator.lights.hue_v2 import build_hue_v2_payload, kelvin_to_mired
from orchestrator.lights.mock_bulb import MockBulbDriver
from orchestrator.lights.none_bulb import NoneLightDriver
from orchestrator.lights.wiz_udp import build_set_pilot_payload
from orchestrator.mqtt_ha import (
    HomeAssistantMQTTBridge,
    _encode_remaining_length,
    build_connect_packet,
    build_disconnect_packet,
    build_discovery_configs,
    build_pingreq_packet,
    build_publish_packet,
)
from orchestrator.serial_reader import SerialSensorReader, parse_ndjson_line
from orchestrator.telemetry import TelemetryLogger, generate_compliance_report
from orchestrator.tray_win32 import (
    HOTKEY_ID_TOGGLE_MOVIE,
    HOTKEY_ID_TOGGLE_PAUSE,
    TrayActionController,
    TrayCallbacks,
    TrayStatusSnapshot,
    ensure_icon_file,
    generate_braun_ico_bytes,
)
from orchestrator.web_ui import LocalWebUIServer, OrchestratorState
from spectral.photobiology import (
    AS7343Converter,
    CircadianController,
    ControlAction,
    MOVIE_MODE_CCT_K,
    PhotometricReading,
    RoomProfile,
    SpectralSample,
    SyntheticSensorStream,
    backlight_pct_to_nits,
    compute_movie_mode_target,
    compute_smooth_open_loop_targets,
    nits_to_backlight_pct,
    resolve_biological_schedule,
    sample_24h_curve,
)

PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Photobiology & Spectral Conversion Accuracy
# ---------------------------------------------------------------------------


def test_photobiology_overcast_day_vs_evening_unlinked_neon() -> None:
    """Verify realistic CIE S 026 m-DER and m-EDI bounds across key scenarios."""
    converter = AS7343Converter()

    day_stream = SyntheticSensorStream("overcast_day")
    day_reading = converter.convert(day_stream.next_sample())
    assert 0.72 <= day_reading.m_der <= 1.05, f"Unexpected daytime m_der: {day_reading.m_der}"
    assert day_reading.m_edi_lx >= 150.0, f"Expected high daytime m-EDI, got {day_reading.m_edi_lx}"
    assert day_reading.photopic_lx > 150.0
    assert 4200.0 <= day_reading.cct_k <= 7000.0

    neon_stream = SyntheticSensorStream("evening_unlinked_neon")
    neon_reading = converter.convert(neon_stream.next_sample())
    assert neon_reading.m_edi_lx > 10.0, (
        f"Unlinked cool-blue neon scenario must breach 10 lx evening m-EDI cap, got {neon_reading.m_edi_lx}"
    )

    monitor_stream = SyntheticSensorStream("evening_monitor_glare")
    monitor_reading = converter.convert(monitor_stream.next_sample())
    assert monitor_reading.m_edi_lx > 10.0
    assert monitor_reading.m_der >= 0.65


# ---------------------------------------------------------------------------
# 2. Control Safety: Shadow Outlier Rejection & Evening Non-Negativity
# ---------------------------------------------------------------------------


def test_shadow_outlier_rejection_prevents_bulb_spike() -> None:
    """A sudden hand/mug shadow dip must be flagged and must NOT spike bulb to 100%."""
    converter = AS7343Converter()
    stream = SyntheticSensorStream("hand_shadow_test")
    controller = CircadianController(wake_hour=7.0, sleep_hour=23.0)

    actions = []
    for _ in range(15):
        reading = converter.convert(stream.next_sample())
        actions.append(controller.step(reading, hour_of_day=11.0))

    rejected_actions = [a for a in actions if a.shadow_rejected]
    assert len(rejected_actions) >= 1, "Expected at least one shadow dip to trigger shadow_rejected=True"
    for act in rejected_actions:
        assert act.bulb_brightness_pct < 100, (
            f"Shadow occlusion should NOT slam bulb to 100% (got {act.bulb_brightness_pct}%)"
        )


def test_evening_non_negativity_and_unlinked_light_alert() -> None:
    """In EVENING with an unlinked blue neon lamp, bulb MUST go to 0% and alert MUST fire."""
    converter = AS7343Converter()
    stream = SyntheticSensorStream("evening_unlinked_neon")
    controller = CircadianController(wake_hour=7.0, sleep_hour=23.0)

    last_action = None
    for _ in range(6):
        reading = converter.convert(stream.next_sample())
        last_action = controller.step(reading, hour_of_day=21.5)

    assert last_action is not None
    assert last_action.phase == "EVENING"
    assert last_action.bulb_brightness_pct == 0, (
        f"Non-negativity violation: evening bulb must turn OFF (0%), got {last_action.bulb_brightness_pct}%"
    )
    assert last_action.monitor_cct_k <= 2700
    assert last_action.unlinked_light_alert is True
    assert len(last_action.alert_message.strip()) > 0


# ---------------------------------------------------------------------------
# 3. Local Smart-Light Drivers (WiZ UDP & Philips Hue CLIP v2)
# ---------------------------------------------------------------------------


def test_wiz_udp_payload_formatting() -> None:
    """Verify official WiZ JSON `setPilot` payloads for OFF, low-clamp, and normal ON."""
    off_payload = build_set_pilot_payload(brightness_pct=0, cct_k=2300)
    assert off_payload == {"method": "setPilot", "params": {"state": False}}

    low_payload = build_set_pilot_payload(brightness_pct=3, cct_k=2100)
    assert low_payload["method"] == "setPilot"
    assert low_payload["params"]["state"] is True
    assert low_payload["params"]["dimming"] == 10  # WiZ hardware floor
    assert low_payload["params"]["temp"] == 2200  # WiZ warm clamp

    day_payload = build_set_pilot_payload(brightness_pct=75, cct_k=5200)
    assert day_payload == {
        "method": "setPilot",
        "params": {"state": True, "temp": 5200, "dimming": 75},
    }


def test_hue_v2_mired_and_payload_formatting() -> None:
    """Verify Hue CLIP v2 Kelvin-to-mired math and grouped_light PUT body."""
    assert kelvin_to_mired(6500) == 154
    assert kelvin_to_mired(2300) == 435
    assert kelvin_to_mired(9000) == 153  # clamped cool bound
    assert kelvin_to_mired(1500) == 500  # clamped warm bound

    off_body = build_hue_v2_payload(brightness_pct=0, cct_k=2300, transition_ms=1500)
    assert off_body["on"] == {"on": False}
    assert off_body["dynamics"] == {"duration": 1500}
    assert "dimming" not in off_body

    on_body = build_hue_v2_payload(brightness_pct=45, cct_k=2700, transition_ms=2000)
    assert on_body["on"] == {"on": True}
    assert on_body["dimming"] == {"brightness": 45.0}
    assert on_body["color_temperature"] == {"mirek": 370}
    assert on_body["dynamics"] == {"duration": 2000}


# ---------------------------------------------------------------------------
# 4. Serial NDJSON Parsing, Gamma LUT & SleepHandoffGuard
# ---------------------------------------------------------------------------


def test_serial_ndjson_parser_nested_flat_and_errors() -> None:
    nested = json.dumps(
        {
            "timestamp": 1730000000.0,
            "channels": {"FZ": 120.5, "F3": 140.0, "F4": 160.0, "FY": 200.0, "CLEAR": 900.0},
            "saturated": False,
        }
    )
    sample = parse_ndjson_line(nested)
    assert isinstance(sample, SpectralSample)
    assert sample.channels["FZ"] == 120.5
    assert sample.channels["F1"] == 0.0
    assert sample.saturated is False

    flat = json.dumps({"ts": 12.5, "FZ": 50, "F3": 60, "F4": 70, "FY": 80, "CLEAR": 400})
    sample_flat = parse_ndjson_line(flat)
    assert sample_flat is not None
    assert sample_flat.channels["F3"] == 60.0
    assert sample_flat.timestamp > 1_000_000_000  # uptime remapped to wall clock

    assert parse_ndjson_line("") is None
    assert parse_ndjson_line("not json boot log") is None
    assert parse_ndjson_line('{"error": "AS7343 not found at 0x39"}') is None


def test_gamma_ramp_and_sleep_handoff_idempotency() -> None:
    r_day, g_day, b_day = kelvin_to_rgb_scalars(6500)
    r_eve, g_eve, b_eve = kelvin_to_rgb_scalars(2300)
    assert b_eve < b_day * 0.45
    assert r_eve >= 0.95 and r_day >= 0.95

    red_lut, green_lut, blue_lut = build_gamma_ramp(2300, software_dim_scale=0.8)
    assert len(red_lut) == len(green_lut) == len(blue_lut) == 256
    assert all(0 <= v <= 65535 for v in red_lut + green_lut + blue_lut)
    assert blue_lut[-1] < red_lut[-1]

    bulb = MockBulbDriver()
    guard = SleepHandoffGuard(light=bulb, display=None)
    assert guard.trigger_handoff("test_suspend") is True
    assert guard.trigger_handoff("duplicate_call") is False
    assert len(bulb.history) == 1
    assert bulb.history[0]["cct_k"] == 2300
    assert bulb.history[0]["brightness_pct"] > 0


def test_room_profile_json_roundtrip(tmp_path: Path) -> None:
    profile = RoomProfile(
        lamp_max_m_edi_lx=62.5,
        lamp_cct_shift_k=-420.0,
        lamp_min_stable_pct=10,
        monitor_max_m_edi_lx=34.0,
        mount_mode="bezel_hybrid",
    )
    out = tmp_path / "room_profile.json"
    profile.save(str(out))
    loaded = RoomProfile.load(str(out))
    assert loaded.lamp_max_m_edi_lx == 62.5
    assert loaded.lamp_cct_shift_k == -420.0
    assert loaded.lamp_min_stable_pct == 10
    assert loaded.mount_mode == "bezel_hybrid"


# ---------------------------------------------------------------------------
# 5. End-to-End Daemon CLI Dry-Run Smoke Test
# ---------------------------------------------------------------------------


def test_daemon_cli_day_to_evening_demo_clean_progression() -> None:
    """Verify day_to_evening_demo runs cleanly for both --ticks 4 and --ticks 8 with zero bogus shadow tags."""
    for ticks in (4, 8):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "orchestrator.daemon",
                "--simulate",
                "day_to_evening_demo",
                "--light",
                "mock",
                "--ticks",
                str(ticks),
                "--interval",
                "0.01",
            ],
            cwd=str(PROTOTYPE_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, f"ticks={ticks} failed:\nSTDERR:\n{proc.stderr}"
        assert "DAY" in proc.stdout
        assert "EVENING" in proc.stdout
        assert "OFF [u=0]" in proc.stdout
        assert "UNLINKED LIGHT" in proc.stdout
        assert "SHADOW REJECTED" not in proc.stdout, (
            f"Bogus SHADOW REJECTED in day_to_evening_demo (ticks={ticks}):\n{proc.stdout}"
        )


def test_daemon_cli_dry_run_smoke() -> None:
    """Invoke `python -m orchestrator.daemon` end-to-end in simulated mode."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchestrator.daemon",
            "--simulate",
            "evening_unlinked_neon",
            "--light",
            "mock",
            "--ticks",
            "5",
            "--interval",
            "0.01",
        ],
        cwd=str(PROTOTYPE_ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"Daemon exited with {proc.returncode}:\nSTDERR:\n{proc.stderr}"
    assert "EVENING" in proc.stdout
    assert "OFF [u=0]" in proc.stdout
    assert "UNLINKED LIGHT" in proc.stdout


# ---------------------------------------------------------------------------
# 6. Wave 2 Bug-Fix & Pure Track A v6.1 Verification Suite (BUG-1..BUG-5 + v6.1)
# ---------------------------------------------------------------------------


def _make_reading(
    m_edi_lx: float,
    *,
    ts: float = 1730000000.0,
    cct_k: float = 5000.0,
    m_der: float = 0.80,
    saturated: bool = False,
    below_floor: bool = False,
) -> PhotometricReading:
    """Construct a deterministic PhotometricReading with CIE TN 015 k=2 (+/-17%) uncertainty."""
    photopic = round(m_edi_lx / max(0.15, m_der), 2)
    return PhotometricReading(
        timestamp=ts,
        m_edi_lx=round(m_edi_lx, 2),
        photopic_lx=photopic,
        cct_k=cct_k,
        m_der=m_der,
        flicker_hz=100.0,
        saturated=saturated,
        below_floor=below_floor,
        uncertainty_k2_lx=round(m_edi_lx * 0.17, 2),
    )


def test_bug1_closed_loop_virtual_room_convergence_and_gain_normalized_integrator() -> None:
    """
    BUG-1: Closed-loop virtual room y[k] = ambient + g * u[k] must converge monotonically
    without bang-bang oscillation across both low-gain (g=0.8 lx/%) and high-gain (g=4.5 lx/%)
    rooms, enforce [0, 100] anti-windup clamping, and apply the 8-bit quantization deadband.
    """
    for g_lx_per_pct, ambient_lx in ((0.80, 190.0), (4.50, 90.0)):
        profile = RoomProfile(
            lamp_max_m_edi_lx=g_lx_per_pct * 100.0,
            lamp_min_stable_pct=5,
            monitor_max_m_edi_lx=42.0,
        )
        ctrl = CircadianController(
            wake_hour=7.0,
            sleep_hour=23.0,
            profile=profile,
            loop_kg=0.22,
            min_dimming_step_pct=2,
        )
        u_pct = 0
        history_u: list[int] = []
        history_eff: list[float] = []
        for step_idx in range(25):
            y_lx = ambient_lx + g_lx_per_pct * u_pct
            reading = _make_reading(y_lx, ts=1730000000.0 + step_idx * 2.0)
            action = ctrl.step(reading, hour_of_day=11.0)
            u_pct = action.bulb_brightness_pct
            history_u.append(u_pct)
            mon_lx = ctrl._estimate_monitor_m_edi(
                action.monitor_brightness_pct,
                action.monitor_cct_k,
                0.5,
            )
            history_eff.append(y_lx + mon_lx)

        # Effective eye-level m-EDI (room + monitor) must settle inside the 250 lx +/-8% hysteresis band
        assert 230.0 <= history_eff[-1] <= 270.0, (
            f"Virtual room g={g_lx_per_pct} failed to converge to 250 lx band: eff={history_eff[-1]:.1f}, u={history_u}"
        )
        # Verify monotonic non-decreasing actuation and zero oscillation in steady state
        assert all(history_u[i + 1] >= history_u[i] for i in range(len(history_u) - 1)), (
            f"Virtual room g={g_lx_per_pct} exhibited non-monotonic oscillation: {history_u}"
        )
        tail_deltas = [history_u[i + 1] - history_u[i] for i in range(14, 24)]
        assert all(d == 0 for d in tail_deltas), (
            f"Virtual room g={g_lx_per_pct} failed to lock steady state: tail={history_u[14:]}"
        )

    # Anti-windup test: unreachable daytime target clamps integrator at 100% and recovers immediately
    low_profile = RoomProfile(lamp_max_m_edi_lx=45.0, lamp_min_stable_pct=5)
    ctrl_aw = CircadianController(profile=low_profile, loop_kg=0.22)
    u_aw = 0
    for idx in range(15):
        act_aw = ctrl_aw.step(
            _make_reading(40.0 + 0.45 * u_aw, ts=1730001000.0 + idx * 2.0),
            hour_of_day=10.0,
        )
        u_aw = act_aw.bulb_brightness_pct
    assert u_aw == 100
    assert ctrl_aw._integrator_pct <= 100.0

    # When sunlight suddenly floods the room (320 lx > 250 lx target), integrator winds down without hang
    for idx in range(15, 25):
        act_sun = ctrl_aw.step(
            _make_reading(320.0 + 0.45 * u_aw, ts=1730001000.0 + idx * 2.0),
            hour_of_day=10.5,
        )
        u_aw = act_sun.bulb_brightness_pct
    assert u_aw == 0, f"Anti-windup failed to release saturation when daylight exceeded target: u={u_aw}%"

    # Quantization deadband test: >= 1.5x min_dimming_step_pct (ceil(1.5 * 2) = 3%) suppresses micro-steps
    ctrl_db = CircadianController(min_dimming_step_pct=2)
    assert ctrl_db._apply_quantization_deadband(31, 30) == 30
    assert ctrl_db._apply_quantization_deadband(32, 30) == 30
    assert ctrl_db._apply_quantization_deadband(33, 30) == 33
    assert ctrl_db._apply_quantization_deadband(0, 30) == 0


def test_bug2_phase_and_alert_aware_sleep_handoff_and_sigint() -> None:
    """
    BUG-2: SleepHandoffGuard must respect phase, unlinked_light_alert, and last_bulb_power,
    and SIGINT normal exit must only restore the display without touching smart lamps.
    """

    class DummyDisplay:
        def __init__(self) -> None:
            self.restored_count = 0

        def restore(self) -> bool:
            self.restored_count += 1
            return True

    # 1. DAY phase: only restores display, never touches room lamps
    bulb_day = MockBulbDriver()
    disp_day = DummyDisplay()
    guard_day = SleepHandoffGuard(light=bulb_day, display=disp_day)
    guard_day.update_context(phase="DAY", unlinked_light_alert=False, last_bulb_power=True)
    assert guard_day.trigger_handoff("suspend_day") is True
    assert guard_day.last_handoff_action == "day_display_only"
    assert len(bulb_day.history) == 0
    assert disp_day.restored_count == 1

    # 2. NIGHT phase: forces bulb OFF (0%), never wakes a dark room to 10-12%
    bulb_night = MockBulbDriver()
    disp_night = DummyDisplay()
    guard_night = SleepHandoffGuard(light=bulb_night, display=disp_night)
    guard_night.update_context(phase="NIGHT", unlinked_light_alert=False, last_bulb_power=True)
    assert guard_night.trigger_handoff("suspend_night") is True
    assert guard_night.last_handoff_action == "lamp_off"
    assert len(bulb_night.history) == 1
    assert bulb_night.history[0]["brightness_pct"] == 0
    assert disp_night.restored_count == 1

    # 3. EVENING with unlinked_light_alert=True (u=0 safety clamp active): MUST force lamp OFF (0%)
    bulb_alert = MockBulbDriver()
    guard_alert = SleepHandoffGuard(light=bulb_alert, display=None)
    guard_alert.update_context(phase="EVENING", unlinked_light_alert=True, last_bulb_power=False)
    assert guard_alert.trigger_handoff("suspend_evening_alert") is True
    assert guard_alert.last_handoff_action == "lamp_off"
    assert bulb_alert.history[-1]["brightness_pct"] == 0

    # 4. EVENING with clean lamp ON (alert=False, last_bulb_power=True): warm dim 2300K handoff
    bulb_eve = MockBulbDriver()
    guard_eve = SleepHandoffGuard(light=bulb_eve, display=None)
    guard_eve.update_context(phase="EVENING", unlinked_light_alert=False, last_bulb_power=True)
    assert guard_eve.trigger_handoff("suspend_evening_clean") is True
    assert guard_eve.last_handoff_action == "evening_warm_dim"
    assert bulb_eve.history[-1]["cct_k"] == 2300
    assert bulb_eve.history[-1]["brightness_pct"] == 12

    # 5. SIGINT with default handoff_on_normal_exit=False restores display only and leaves bulb untouched
    bulb_sig = MockBulbDriver()
    disp_sig = DummyDisplay()
    guard_sig = SleepHandoffGuard(light=bulb_sig, display=disp_sig, handoff_on_normal_exit=False)
    guard_sig.update_context(phase="EVENING", unlinked_light_alert=False, last_bulb_power=True)
    assert guard_sig._handle_signal(signal.SIGINT) is False
    assert disp_sig.restored_count == 1
    assert len(bulb_sig.history) == 0


def test_bug3_command_deduplication_and_conflict_observer_backoff() -> None:
    """
    BUG-3 & Kova-1 Conflict-Observer: CommandDispatcher must deduplicate identical bulb/monitor
    setpoints across ticks and enter a 10-minute backoff when an external controller alters the bulb.
    """
    bulb = MockBulbDriver()
    ctrl = CircadianController(wake_hour=7.0, sleep_hour=23.0, conflict_backoff_s=600.0)
    dispatcher = CommandDispatcher(light=bulb, display=None, monitor_enabled=False)

    # Tick 1: first daytime reading dispatches bulb command
    r1 = _make_reading(150.0, ts=1730000000.0)
    a1 = ctrl.step(r1, hour_of_day=11.0)
    d1 = dispatcher.dispatch(a1, controller=ctrl)
    assert d1["bulb_sent"] is True
    assert len(bulb.history) == 1

    # Duplicate dispatch with the exact same action must NOT send a redundant UDP/HTTPS packet
    d1_dup = dispatcher.dispatch(a1, controller=ctrl)
    assert d1_dup["bulb_sent"] is False
    assert len(bulb.history) == 1

    # Simulate external override (user changes bulb via wall switch / Hue app / HomeKit to 25% @ 2700K)
    bulb.simulate_external_change(power=True, brightness_pct=25, cct_k=2700)
    assert dispatcher.check_external_conflict(ctrl) is True

    # Next controller step within 600s must report conflict_observer_active=True and dispatcher must hold off
    r2 = _make_reading(145.0, ts=1730000120.0)
    a2 = ctrl.step(r2, hour_of_day=11.05)
    assert a2.conflict_observer_active is True
    d2 = dispatcher.dispatch(a2, controller=ctrl)
    assert d2["conflict_observer_active"] is True
    assert d2["bulb_sent"] is False
    assert len(bulb.history) == 1  # External user override is preserved!

    # After 600s backoff expires, autonomous control resumes cleanly
    r3 = _make_reading(145.0, ts=1730000750.0)
    a3 = ctrl.step(r3, hour_of_day=11.2)
    assert a3.conflict_observer_active is False


def test_bug4_windows_display_ddc_and_gamma_backup_restore_mocks() -> None:
    """
    BUG-4: WindowsDisplayController must back up original GDI gamma ramp and per-monitor DDC/CI
    VCP 0x10 brightness before first mutation and restore both on exit (tested via pure mocks).
    """

    class MockDDCBackend:
        def __init__(self) -> None:
            self.brightness_by_mon: dict[int, int] = {0: 78, 1: 64}
            self.set_calls: list[tuple[int, int]] = []

        def list_monitors(self) -> list[int]:
            return [0, 1]

        def get_brightness(self, mon_id: int) -> tuple[int, int, int]:
            return (0, self.brightness_by_mon[mon_id], 100)

        def set_brightness(self, mon_id: int, val: int) -> bool:
            self.brightness_by_mon[mon_id] = int(val)
            self.set_calls.append((mon_id, int(val)))
            return True

    class MockGammaBackend:
        def __init__(self) -> None:
            self.current_ramp: Any = ("ORIGINAL_RAMP_SNAPSHOT", [i * 256 for i in range(256)])
            self.set_calls: list[Any] = []

        def get_ramp(self) -> Any:
            return self.current_ramp

        def set_ramp(self, ramp: Any) -> bool:
            self.current_ramp = ramp
            self.set_calls.append(ramp)
            return True

    mock_ddc = MockDDCBackend()
    mock_gamma = MockGammaBackend()
    display = WindowsDisplayController(
        enabled=True,
        ddc_backend=mock_ddc,
        gamma_backend=mock_gamma,
    )
    assert display.gamma_supported is True
    assert display.ddc_supported is True
    assert mock_ddc.brightness_by_mon == {0: 78, 1: 64}
    assert len(mock_ddc.set_calls) == 0

    # Apply evening circadian display target (2400K, 42% brightness)
    res1 = display.apply(brightness_pct=42, cct_k=2400)
    assert res1["applied"] is True
    assert res1["ddc_ok"] is True
    assert res1["gamma_ok"] is True
    assert mock_ddc.brightness_by_mon == {0: 42, 1: 42}
    assert display._original_ddc_brightness == {0: 78, 1: 64}
    assert len(mock_ddc.set_calls) == 2

    # Second distinct mutation (past DDC 450ms EEPROM debounce) must NOT overwrite backed-up original brightness
    display._ddc_min_interval_s = 0.0
    display.apply(brightness_pct=30, cct_k=2200)
    assert mock_ddc.brightness_by_mon == {0: 30, 1: 30}
    assert display._original_ddc_brightness == {0: 78, 1: 64}

    # Restore must restore both the original GDI gamma snapshot and original DDC/CI brightness (78%, 64%)
    assert display.restore() is True
    assert mock_ddc.brightness_by_mon == {0: 78, 1: 64}
    assert mock_gamma.current_ramp == ("ORIGINAL_RAMP_SNAPSHOT", [i * 256 for i in range(256)])


def test_bug5_saturation_usb_reconnect_and_3chip_firmware_mock_i2c(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    BUG-5 & 3-Chip Firmware HAL:
    - 5a: Saturated readings (65535 / AVALID+ASAT) never enter rolling window, mark reading_valid=False
          and compliance_status='BELIRSIZ', hold daytime state, and force 0% in EVENING/NIGHT.
    - 5b: SerialSensorReader reconnects with exponential backoff and raises RuntimeError on exhaustion.
    - 5c: pico_as7343.AS7343Driver auto-detects TCS3448, AS7343, and AS7341 (2-pass SMUX) over mock I2C
          with 100.08ms integration (ATIME=35, ASTEP=999) and FD_STATUS 100/120Hz flicker detection.
    """
    # --- 5a: Saturation fail-safe ---
    converter = AS7343Converter()
    ctrl = CircadianController(wake_hour=7.0, sleep_hour=23.0)

    valid_sample = SpectralSample(
        timestamp=1730000000.0,
        channels={ch: 240.0 for ch in ("FZ", "F1", "F2", "F3", "F4", "FY", "F5", "FXL", "F6", "F7", "F8", "CLEAR")},
        saturated=False,
    )
    valid_reading = converter.convert(valid_sample)
    act_valid = ctrl.step(valid_reading, hour_of_day=11.0)
    assert act_valid.reading_valid is True
    assert len(ctrl._window) == 1
    held_day_pct = act_valid.bulb_brightness_pct

    sat_sample = SpectralSample(
        timestamp=1730000002.0,
        channels={ch: 65535.0 for ch in ("FZ", "F1", "F2", "F3", "F4", "FY", "F5", "FXL", "F6", "F7", "F8", "CLEAR")},
        saturated=True,
    )
    sat_reading = converter.convert(sat_sample)
    assert sat_reading.saturated is True
    assert sat_reading.below_floor is False

    act_sat_day = ctrl.step(sat_reading, hour_of_day=11.0)
    assert act_sat_day.reading_valid is False
    assert act_sat_day.compliance_status == "BELIRSIZ"
    assert len(ctrl._window) == 1  # Saturated reading rejected from rolling median window
    assert act_sat_day.bulb_brightness_pct == held_day_pct

    act_sat_eve = ctrl.step(sat_reading, hour_of_day=21.5)
    assert act_sat_eve.reading_valid is False
    assert act_sat_eve.compliance_status == "BELIRSIZ"
    assert act_sat_eve.bulb_brightness_pct == 0  # Evening non-negativity fail-safe under saturation

    # --- 5b: SerialSensorReader exponential backoff reconnect & exhaustion ---
    sleep_delays: list[float] = []

    class FakeSerialConn:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = list(lines)
            self.closed = False

        def readline(self) -> bytes:
            if not self._lines:
                raise OSError("USB CDC disconnected mid-stream")
            return self._lines.pop(0)

        def close(self) -> None:
            self.closed = True

    attempt_counter = {"count": 0}
    valid_ndjson = (
        json.dumps(
            {
                "timestamp": 1730000010.0,
                "chip": "TCS3448",
                "channels": {"FZ": 110, "F3": 130, "F4": 150, "FY": 180, "CLEAR": 800},
                "saturated": False,
            }
        ).encode("utf-8")
        + b"\n"
    )

    def fake_serial_factory(port: str, baudrate: int, timeout: float) -> FakeSerialConn:
        attempt_counter["count"] += 1
        # Initial open (#1) returns an empty/dropped stream; reconnect #1 (#2) fails;
        # reconnect #2 (#3) succeeds with a valid NDJSON sample; subsequent reconnects fail permanently.
        if attempt_counter["count"] == 1:
            return FakeSerialConn([])
        if attempt_counter["count"] == 2:
            raise OSError("Simulated COM disconnect on reconnect #1")
        if attempt_counter["count"] == 3:
            return FakeSerialConn([valid_ndjson])
        raise OSError("Permanent USB unplug")

    reader = SerialSensorReader(
        port="COM99",
        max_reconnect_attempts=2,
        backoff_base_s=0.5,
        max_backoff_s=4.0,
        serial_factory=fake_serial_factory,
        sleep_fn=lambda d: sleep_delays.append(d),
    )
    recovered_sample = reader.next_sample()
    assert recovered_sample.chip == "TCS3448"
    assert recovered_sample.channels["FZ"] == 110.0
    assert reader.reconnect_count == 1
    assert sleep_delays == [0.5, 1.0]

    with pytest.raises(RuntimeError, match="remained unavailable after 2 reconnect attempts"):
        reader.next_sample()

    # --- 5c: MicroPython pico_as7343.py 3-Chip HAL over Mock I2C ---
    fake_machine = types.ModuleType("machine")
    fake_machine.I2C = object  # type: ignore[attr-defined]
    fake_machine.Pin = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "machine", fake_machine)
    if not hasattr(time, "sleep_ms"):
        monkeypatch.setattr(time, "sleep_ms", lambda ms: None, raising=False)
    if not hasattr(time, "ticks_ms"):
        monkeypatch.setattr(time, "ticks_ms", lambda: int(time.time() * 1000), raising=False)
    if not hasattr(time, "ticks_diff"):
        monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)

    from firmware import pico_as7343

    class MockI2CBus:
        def __init__(self, variant: str, fd_status: int = 0x25) -> None:
            self.variant = variant
            self.fd_status = fd_status
            self.regs: dict[int, int] = {
                0x80: 0x00,
                0x90: 0x40,  # REG_STATUS2: AVALID=1 (bit 6), not saturated
                0x94: 0x00,  # REG_ASTATUS: ASAT_STATUS=0
                0xDB: fd_status,
            }
            # Populate synthetic 16-bit little-endian channel data from 0x95..0xB8 (~2500 counts > LOW_SIGNAL_LO)
            for offset in range(36):
                self.regs[0x95 + offset] = (200 + offset * 3) & 0xFF if offset % 2 == 0 else 0x09

        def scan(self) -> list[int]:
            return [0x39]

        def writeto_mem(self, addr: int, reg: int, data: bytes) -> None:
            for idx, b_val in enumerate(data):
                r = reg + idx
                if r == 0x80:
                    # Hardware clears SMUXEN (bit 4) once SMUX RAM transfer completes
                    b_val = b_val & ~0x10
                self.regs[r] = b_val

        def readfrom_mem(self, addr: int, reg: int, nbytes: int) -> bytes:
            reg_bank_1 = bool(self.regs.get(0xBF, 0) & 0x10)
            out = bytearray()
            for idx in range(nbytes):
                r = reg + idx
                if r == 0x5A and reg_bank_1:
                    out.append(0x81 if self.variant in ("TCS3448", "AS7343") else 0x00)
                elif r in (0x58, 0x59) and reg_bank_1:
                    out.append(0x02 if self.variant == "TCS3448" else 0x01)
                elif r == 0x92 and not reg_bank_1:
                    out.append(0x24 if self.variant == "AS7341" else 0x81)
                elif r == 0x90:
                    out.append(0x40)  # AVALID=1
                elif r == 0xDB:
                    out.append(self.fd_status)
                else:
                    out.append(self.regs.get(r, 0x00))
            return bytes(out)

    # Verify baseline integration constant is 100.08ms (ATIME=35, ASTEP=999) and mid gain is 16x (idx=5)
    assert pico_as7343.INT_100MS == (35, 999)
    assert pico_as7343.CONST_MID_GAIN_IDX == 5

    for chip_variant, fd_byte, expected_hz in (
        ("TCS3448", 0x25, 100.0),
        ("AS7343", 0x2A, 120.0),
        ("AS7341", 0x20, 0.0),
    ):
        bus = MockI2CBus(chip_variant, fd_status=fd_byte)
        drv = pico_as7343.AS7343Driver(bus)
        assert drv.chip_name == chip_variant
        channels, saturated, gain = drv.read_normalized()
        assert saturated is False
        assert gain == 16.0
        assert round(drv.int_ms, 2) == 100.08
        assert channels["FLICKER"] == expected_hz

        frame = {
            "timestamp": 1730000020.0,
            "chip": drv.chip_name,
            "channels": channels,
            "saturated": saturated,
            "gain": gain,
            "int_ms": round(drv.int_ms, 2),
        }
        # Round-trip frame through host NDJSON parser and photobiology converter
        parsed = parse_ndjson_line(json.dumps(frame))
        assert parsed is not None
        assert parsed.chip == chip_variant
        converted = converter.convert(parsed)
        assert converted.m_edi_lx > 0.0
        assert converted.flicker_hz == expected_hz
        assert converted.uncertainty_k2_lx == round(converted.m_edi_lx * 0.17, 2)


def test_v61_metrology_3state_k2_night_floor_occlusion_and_presets() -> None:
    """
    Pure Track A v6.1 Metrology & Clinical Guardrails:
    - CIE TN 015 k=2 (+/-17%) 3-state compliance ('SAGLANDI', 'SAGLANMADI', 'BELIRSIZ').
    - < 0.3 lx night measurement floor ('below_floor=True' -> 'SAGLANDI' in NIGHT).
    - > 10s persistent occlusion ('sensor_occluded=True', 'reading_valid=False', HUD stale banner).
    - <= 5.0 lx high-sensitivity evening preset ('high_sensitivity_evening=True') & CLI flags.
    """
    converter = AS7343Converter()
    ctrl = CircadianController(wake_hour=7.0, sleep_hour=23.0, occlusion_timeout_s=10.0)

    # 1. DAY (target >= 250.0 lx) k=2 (+/-17%) 3-state compliance
    act_day_pass = ctrl.step(_make_reading(310.0, ts=1730000000.0), hour_of_day=11.0)
    assert act_day_pass.compliance_status == "SAGLANDI"  # 310 - 52.7 = 257.3 >= 250

    ctrl_unc_day = CircadianController()
    act_day_unc = ctrl_unc_day.step(_make_reading(255.0, ts=1730000000.0), hour_of_day=11.0)
    assert act_day_unc.compliance_status == "BELIRSIZ"  # 255 +/- 43.35 straddles 250.0 lx

    ctrl_fail_day = CircadianController()
    act_day_fail = ctrl_fail_day.step(_make_reading(180.0, ts=1730000000.0), hour_of_day=11.0)
    assert act_day_fail.compliance_status == "SAGLANMADI"  # 180 + 30.6 = 210.6 < 250

    # 2. EVENING (default target <= 10.0 lx, including ~1.58 lx bezel monitor estimate) k=2 3-state compliance
    ctrl_eve_pass = CircadianController()
    assert ctrl_eve_pass.step(_make_reading(5.0, ts=1730000000.0), hour_of_day=21.0).compliance_status == "SAGLANDI"

    ctrl_eve_unc = CircadianController()
    assert ctrl_eve_unc.step(_make_reading(8.0, ts=1730000000.0), hour_of_day=21.0).compliance_status == "BELIRSIZ"

    ctrl_eve_fail = CircadianController()
    assert ctrl_eve_fail.step(_make_reading(14.0, ts=1730000000.0), hour_of_day=21.0).compliance_status == "SAGLANMADI"

    # 3. < 0.3 lx night measurement floor (below_floor=True)
    dark_sample = SpectralSample(
        timestamp=1730000000.0,
        channels={ch: 0.001 for ch in ("FZ", "F1", "F2", "F3", "F4", "FY", "F5", "FXL", "F6", "F7", "F8", "CLEAR")},
        saturated=False,
    )
    dark_reading = converter.convert(dark_sample)
    assert dark_reading.below_floor is True
    ctrl_night = CircadianController()
    act_night = ctrl_night.step(dark_reading, hour_of_day=1.0)
    assert act_night.phase == "NIGHT"
    assert act_night.compliance_status == "SAGLANDI"
    hud_night = format_hud_line(1, dark_reading, act_night, 1.0)
    assert "[<0.3 lx NIGHT FLOOR]" in hud_night

    # 4. > 10s persistent occlusion detection & HUD invalid/stale banner
    ctrl_occ = CircadianController(occlusion_timeout_s=10.0)
    base_reading = _make_reading(240.0, ts=1730001000.0)
    act_base = ctrl_occ.step(base_reading, hour_of_day=12.0)
    assert act_base.sensor_occluded is False
    held_pct = act_base.bulb_brightness_pct

    # Transient dip at t+2.0s (< 10s): rejected as transient shadow, not yet persistent occlusion
    dip_short = _make_reading(25.0, ts=1730001002.0)
    act_short = ctrl_occ.step(dip_short, hour_of_day=12.0)
    assert act_short.shadow_rejected is True
    assert act_short.sensor_occluded is False

    # Sustained dip at t+12.5s (> 10.0s): triggers persistent sensor_occluded=True freeze
    dip_long = _make_reading(25.0, ts=1730001012.5)
    act_long = ctrl_occ.step(dip_long, hour_of_day=12.0)
    assert act_long.sensor_occluded is True
    assert act_long.reading_valid is False
    assert act_long.compliance_status == "BELIRSIZ"
    assert act_long.bulb_brightness_pct == held_pct
    hud_occ = format_hud_line(3, dip_long, act_long, 12.0)
    assert "--.- (HELD/INVALID)" in hud_occ
    assert "[SENSOR OCCLUDED" in hud_occ

    # 5. <= 5.0 lx High-Sensitivity Evening Preset (Spitschan 2022 / Zeitzer) & CLI dry-run flags
    ctrl_sens = CircadianController(high_sensitivity_evening=True)
    act_sens = ctrl_sens.step(_make_reading(5.0, ts=1730002000.0), hour_of_day=21.0)
    assert act_sens.target_m_edi_lx == 5.0
    assert act_sens.compliance_status == "SAGLANMADI"  # 5.0 lx + ~1.58 lx monitor passes 10 lx default but fails 5 lx preset

    assert daemon_main(["--monitor-safety-check"]) == 0
    assert (
        daemon_main(
            [
                "--simulate",
                "evening_monitor_glare",
                "--sensitive-evening",
                "--light",
                "mock",
                "--ticks",
                "2",
                "--interval",
                "0",
            ]
        )
        == 0
    )


# ---------------------------------------------------------------------------
# 7. Gün-2 Live Laptop WMI Backlight, Defensive DDC Guard & Week-1 --open-loop
# ---------------------------------------------------------------------------


def test_wmi_laptop_backlight_backup_restore_and_no_sw_dim() -> None:
    """
    Native Windows laptop WMI backlight must back up original CurrentBrightness (92%)
    before first mutation, apply hardware brightness (45%) with sw_dim=1.0 (no software
    gamma contrast crush), and deterministically restore both WMI (92%) and GDI gamma.
    """

    class MockWMIBackend:
        def __init__(self, initial: int = 92) -> None:
            self.brightness: int = initial
            self.set_calls: list[int] = []

        def get_brightness(self) -> int | None:
            return self.brightness

        def set_brightness(self, pct: int) -> bool:
            self.brightness = int(pct)
            self.set_calls.append(int(pct))
            return True

    class MockGammaBackend:
        def __init__(self) -> None:
            self.current_ramp: Any = ("ORIG_LAPTOP_GAMMA", [i * 256 for i in range(256)])
            self.set_calls: list[Any] = []

        def get_ramp(self) -> Any:
            return self.current_ramp

        def set_ramp(self, ramp: Any) -> bool:
            self.current_ramp = ramp
            self.set_calls.append(ramp)
            return True

    mock_wmi = MockWMIBackend(initial=92)
    mock_gamma = MockGammaBackend()
    display = WindowsDisplayController(
        enabled=True,
        ddc_backend=None,
        gamma_backend=mock_gamma,
        wmi_backend=mock_wmi,
    )

    res = display.apply(brightness_pct=45, cct_k=3000)
    assert res["applied"] is True
    assert res["wmi_ok"] is True
    assert res["ddc_ok"] is False
    assert res["gamma_ok"] is True
    assert mock_wmi.brightness == 45
    assert display._original_wmi_brightness == 92

    # Because WMI hardware backlight succeeded, software_dim_scale MUST be 1.0 (full red peak 65535)
    expected_full_scale_ramp = build_gamma_ramp(3000, software_dim_scale=1.0)
    assert mock_gamma.current_ramp == expected_full_scale_ramp
    assert mock_gamma.current_ramp[0][-1] == 65535

    # Subsequent apply must not overwrite backed-up original 92%
    display.apply(brightness_pct=20, cct_k=2300)
    assert mock_wmi.brightness == 20
    assert display._original_wmi_brightness == 92

    # Deterministic restore brings WMI back to 92% and restores original gamma ramp
    assert display.restore() is True
    assert mock_wmi.brightness == 92
    assert mock_gamma.current_ramp == ("ORIG_LAPTOP_GAMMA", [i * 256 for i in range(256)])


def test_defensive_ddc_skips_write_when_original_unreadable() -> None:
    """
    Defensive DDC write-safety guard: if get_brightness() returns None (unreadable VCP 0x10),
    WindowsDisplayController must make 0 writes to set_brightness() and fall back to gamma.
    """

    class UnreadableDDCBackend:
        def __init__(self) -> None:
            self.set_calls: list[tuple[int, int]] = []

        def list_monitors(self) -> list[int]:
            return [0, 1]

        def get_brightness(self, mon_id: int) -> None:
            return None

        def set_brightness(self, mon_id: int, val: int) -> bool:
            self.set_calls.append((mon_id, int(val)))
            return True

    class MockGammaBackend:
        def __init__(self) -> None:
            self.current_ramp: Any = ("ORIG_GAMMA", [i * 256 for i in range(256)])

        def get_ramp(self) -> Any:
            return self.current_ramp

        def set_ramp(self, ramp: Any) -> bool:
            self.current_ramp = ramp
            return True

    bad_ddc = UnreadableDDCBackend()
    mock_gamma = MockGammaBackend()
    display = WindowsDisplayController(
        enabled=True,
        ddc_backend=bad_ddc,
        gamma_backend=mock_gamma,
        wmi_backend=None,
    )

    res = display.apply(brightness_pct=45, cct_k=3000)
    assert res["ddc_ok"] is False
    assert res["wmi_ok"] is False
    assert res["gamma_ok"] is True
    assert res["applied"] is True
    assert len(bad_ddc.set_calls) == 0, "Must NEVER write DDC/CI to a monitor whose original brightness could not be read!"
    assert display._original_ddc_brightness == {}

    # Because both DDC and WMI failed, gamma fallback applies gentle software_dim_scale = max(0.45, 0.45)
    assert mock_gamma.current_ramp == build_gamma_ramp(3000, software_dim_scale=0.45)
    assert display.restore() is True
    assert mock_gamma.current_ramp == ("ORIG_GAMMA", [i * 256 for i in range(256)])


def test_open_loop_week1_beta_and_monitor_safety_check_mock() -> None:
    """
    Verify Week-1 zero-sensor --open-loop mode across DAY (11:00), EVENING (21:00), and NIGHT (02:00)
    plus run_monitor_safety_check() deterministic PASS & restore verification with injected mocks.
    """
    # 1. Verify compute_open_loop_action targets across all 3 circadian phases (Comfort Mode default)
    _, act_day_climb, _ = compute_open_loop_action(hour_of_day=11.0, wake_hour=7.0, sleep_hour=23.0)
    assert act_day_climb.phase == "DAY"
    assert act_day_climb.compliance_status == "OPEN-LOOP"
    assert 5500 <= act_day_climb.monitor_cct_k <= 5800  # Climbing smoothly toward 12:00 solar peak
    assert 80 <= act_day_climb.monitor_brightness_pct <= 90
    assert 95 <= act_day_climb.bulb_brightness_pct <= 100

    _, act_day_peak, _ = compute_open_loop_action(hour_of_day=12.0, wake_hour=7.0, sleep_hour=23.0)
    assert act_day_peak.monitor_cct_k == 5800
    assert (act_day_peak.bulb_brightness_pct, act_day_peak.bulb_cct_k) == (100, 5000)

    _, act_eve_ramp, _ = compute_open_loop_action(
        hour_of_day=21.0,
        wake_hour=7.0,
        sleep_hour=23.0,
        high_sensitivity_evening=True,
    )
    assert act_eve_ramp.phase == "EVENING"
    assert act_eve_ramp.target_m_edi_lx == 5.0
    assert 2200 <= act_eve_ramp.monitor_cct_k <= 3200  # Mid-2h smooth sunset ramp [19:30..21:30]

    _, act_eve_deep, _ = compute_open_loop_action(
        hour_of_day=22.0,
        wake_hour=7.0,
        sleep_hour=23.0,
        high_sensitivity_evening=True,
    )
    assert act_eve_deep.phase == "EVENING"
    assert act_eve_deep.monitor_cct_k == 2200
    assert (act_eve_deep.bulb_brightness_pct, act_eve_deep.bulb_cct_k) == (8, 2200)

    _, act_night, _ = compute_open_loop_action(hour_of_day=2.0, wake_hour=7.0, sleep_hour=23.0)
    assert act_night.phase == "NIGHT"
    assert (act_night.monitor_brightness_pct, act_night.monitor_cct_k) == (55, 3000)
    assert act_night.bulb_brightness_pct == 0

    # 2. Verify CLI --open-loop runs cleanly with zero sensor across DAY, EVENING, and NIGHT
    for hr in ("11.0", "21.0", "2.0"):
        rc = daemon_main(
            [
                "--open-loop",
                "--hour",
                hr,
                "--light",
                "mock",
                "--ticks",
                "2",
                "--interval",
                "0",
            ]
        )
        assert rc == 0

    # 3. Verify run_monitor_safety_check() with injected WMI + gamma mocks (hold_seconds=0.0)
    class MockWMIBackend:
        def __init__(self) -> None:
            self.val = 88

        def get_brightness(self) -> int:
            return self.val

        def set_brightness(self, pct: int) -> bool:
            self.val = int(pct)
            return True

    class MockGammaBackend:
        def __init__(self) -> None:
            self.ramp: Any = ("INIT_RAMP", [i * 256 for i in range(256)])

        def get_ramp(self) -> Any:
            return self.ramp

        def set_ramp(self, ramp: Any) -> bool:
            self.ramp = ramp
            return True

    wmi_mock = MockWMIBackend()
    gamma_mock = MockGammaBackend()
    disp = WindowsDisplayController(enabled=True, wmi_backend=wmi_mock, gamma_backend=gamma_mock)
    assert run_monitor_safety_check(disp, hold_seconds=0.0) == 0
    assert wmi_mock.val == 88
    assert gamma_mock.ramp == ("INIT_RAMP", [i * 256 for i in range(256)])


def test_magnification_zero_admin_fallback_and_restore() -> None:
    """
    When stock Windows GDI SetDeviceGammaRamp rejects warm CCTs (<0.50 blue scalar at 3000K/2300K
    due to GdiIcmGammaRange), WindowsDisplayController must restore any prior GDI ramp (no tint
    double-stacking), fall back to zero-admin Magnification.dll (5x5 GPU matrix), report
    gamma_ok=True and color_method='magnification', and reset to IDENTITY_COLOR_MATRIX +
    MagUninitialize() on restore().
    """

    class StockWindowsGdiGammaBackend:
        """Simulates stock Windows GDI accepting 5500K (b>=0.50) but rejecting <=3000K (b<0.50)."""

        def __init__(self) -> None:
            self.orig_ramp: Any = ("ORIG_GDI_RAMP", [i * 256 for i in range(256)])
            self.current_ramp: Any = self.orig_ramp
            self.set_calls: list[Any] = []

        def get_ramp(self) -> Any:
            return self.current_ramp

        def set_ramp(self, ramp: Any) -> bool:
            self.set_calls.append(ramp)
            if ramp == self.orig_ramp:
                self.current_ramp = ramp
                return True
            # Stock Windows GdiIcmGammaRange rejects ramps whose peak blue is < 50% of 65535
            blue_peak = ramp[2][-1]
            if blue_peak < int(65535 * 0.50):
                return False
            self.current_ramp = ramp
            return True

    class MockMagnificationBackend:
        def __init__(self) -> None:
            self.initialized = False
            self.uninitialized_count = 0
            self.current_matrix: list[float] | None = None
            self.matrix_history: list[list[float]] = []

        def initialize(self) -> bool:
            self.initialized = True
            return True

        def set_matrix(self, matrix: list[float]) -> bool:
            assert len(matrix) == 25
            self.current_matrix = list(matrix)
            self.matrix_history.append(list(matrix))
            return True

        def uninitialize(self) -> bool:
            self.initialized = False
            self.uninitialized_count += 1
            return True

    class MockWMIBackend:
        def __init__(self) -> None:
            self.brightness = 100

        def get_brightness(self) -> int:
            return self.brightness

        def set_brightness(self, pct: int) -> bool:
            self.brightness = int(pct)
            return True

    stock_gdi = StockWindowsGdiGammaBackend()
    mag_mock = MockMagnificationBackend()
    wmi_mock = MockWMIBackend()
    disp = WindowsDisplayController(
        enabled=True,
        wmi_backend=wmi_mock,
        gamma_backend=stock_gdi,
        mag_backend=mag_mock,
    )

    # 1. Daytime 5500K (blue scalar >= 0.50): stock GDI accepts it directly
    res_day = disp.apply(brightness_pct=80, cct_k=5500)
    assert res_day["applied"] is True
    assert res_day["wmi_ok"] is True
    assert res_day["gamma_ok"] is True
    assert res_day["color_method"] == "gdi_gamma"
    assert mag_mock.initialized is False

    # 2. Evening 3000K / 2300K (blue scalar < 0.50): stock GDI rejects -> auto-restores GDI to
    #    orig_ramp (preventing double tint) and falls back to zero-admin Magnification 5x5 matrix!
    res_eve = disp.apply(brightness_pct=45, cct_k=3000)
    assert res_eve["applied"] is True
    assert res_eve["wmi_ok"] is True
    assert res_eve["gamma_ok"] is True
    assert res_eve["color_method"] == "magnification"
    assert stock_gdi.current_ramp == stock_gdi.orig_ramp, "Prior GDI ramp must be restored before applying Magnification!"
    assert mag_mock.initialized is True
    assert mag_mock.current_matrix == build_color_effect_matrix(3000, software_dim_scale=1.0)

    # 3. Deeper evening transition (2300K @ 20%): stays smoothly on Magnification
    res_deep = disp.apply(brightness_pct=20, cct_k=2300)
    assert res_deep["gamma_ok"] is True
    assert res_deep["color_method"] == "magnification"
    assert mag_mock.current_matrix == build_color_effect_matrix(2300, software_dim_scale=1.0)

    # 4. Deterministic restore(): resets Magnification to 5x5 IDENTITY_COLOR_MATRIX, calls
    #    MagUninitialize(), and restores WMI backlight to 100%
    assert disp.restore() is True
    assert mag_mock.current_matrix == list(IDENTITY_COLOR_MATRIX)
    assert mag_mock.initialized is False
    assert mag_mock.uninitialized_count == 1
    assert wmi_mock.brightness == 100
    assert disp._color_method == "none"

    # 5. run_monitor_safety_check() passes end-to-end when GDI rejects 3000K and Magnification handles it
    assert run_monitor_safety_check(disp, hold_seconds=0.0) == 0
    assert wmi_mock.brightness == 100
    assert mag_mock.current_matrix == list(IDENTITY_COLOR_MATRIX)
    assert mag_mock.initialized is False


# ---------------------------------------------------------------------------
# 8. Software Milestone 1: Smooth Circadian Curve, Telemetry/Report, Config,
#    --light none, Setup Wizard, Display Capabilities & CLI E2E
# ---------------------------------------------------------------------------


def test_smooth_open_loop_circadian_curve_and_midnight_wraparound() -> None:
    """
    Verify all 6 C1 cosine-eased segments of compute_smooth_open_loop_targets()
    (sunrise_ramp, day, sunset_ramp with phase flip at sleep-3.0, evening Comfort vs
    Strict high_sensitivity, night_winddown, night) and midnight wraparound (wake=9.0, sleep=1.0).
    """
    # 1. Sunrise ramp [7.0, 8.5): starts gently at 55 nits @ 3400K / 30%@3000K (MIRED smooth)
    sr_start = compute_smooth_open_loop_targets(7.0, wake_hour=7.0, sleep_hour=23.0)
    sr_mid = compute_smooth_open_loop_targets(7.75, wake_hour=7.0, sleep_hour=23.0)
    assert sr_start["transition_label"] == "sunrise_ramp"
    assert sr_start["phase"] == "DAY"
    assert sr_start["target_screen_nits"] == 55.0
    assert sr_start["monitor_cct_k"] == 3400
    assert (sr_start["bulb_brightness_pct"], sr_start["bulb_cct_k"]) == (30, 3000)
    assert sr_start["monitor_brightness_pct"] < sr_mid["monitor_brightness_pct"] < 85
    assert 3400 < sr_mid["monitor_cct_k"] < 5200

    # 2. Morning climb [8.5, 12.0) -> solar peak 12:00 (220 nits @ 5800K) -> afternoon glide [12.0, 19.5)
    day_peak = compute_smooth_open_loop_targets(12.0, wake_hour=7.0, sleep_hour=23.0)
    assert day_peak["transition_label"] in ("day", "afternoon")
    assert day_peak["phase"] == "DAY"
    assert day_peak["target_m_edi_lx"] == 250.0
    assert day_peak["monitor_cct_k"] == 5800
    assert day_peak["target_screen_nits"] == 220.0
    assert (day_peak["bulb_brightness_pct"], day_peak["bulb_cct_k"]) == (100, 5000)

    aft_mid = compute_smooth_open_loop_targets(15.0, wake_hour=7.0, sleep_hour=23.0)
    assert aft_mid["transition_label"] == "afternoon" and aft_mid["phase"] == "DAY"
    assert aft_mid["monitor_cct_k"] < day_peak["monitor_cct_k"]
    assert aft_mid["target_screen_nits"] < day_peak["target_screen_nits"]

    # 3. 2-hour Primary Sunset ramp [19.5, 21.5): monotonic decrease & phase flip at 20.0 (sleep - 3.0)
    sunset_hours = [19.5, 19.8, 19.95, 20.0, 20.75, 21.45]
    sunset_pts = [compute_smooth_open_loop_targets(h, wake_hour=7.0, sleep_hour=23.0) for h in sunset_hours]
    assert all(p["transition_label"] == "sunset_ramp" for p in sunset_pts)
    assert sunset_pts[2]["phase"] == "DAY"
    assert sunset_pts[3]["phase"] == "EVENING"  # Exact phase boundary at 20.0h
    for i in range(len(sunset_pts) - 1):
        assert sunset_pts[i + 1]["monitor_brightness_pct"] <= sunset_pts[i]["monitor_brightness_pct"]
        assert sunset_pts[i + 1]["monitor_cct_k"] <= sunset_pts[i]["monitor_cct_k"]
        assert sunset_pts[i + 1]["bulb_brightness_pct"] <= sunset_pts[i]["bulb_brightness_pct"]

    # 4. Deep EVENING [21.5, 22.5): Comfort (~3000-3200K, 55..75 nits, 10 lx) vs Strict sensitive (2200K, bulb 8%@2200K, 5 lx)
    eve_std = compute_smooth_open_loop_targets(22.0, wake_hour=7.0, sleep_hour=23.0, high_sensitivity_evening=False)
    eve_sens = compute_smooth_open_loop_targets(22.0, wake_hour=7.0, sleep_hour=23.0, high_sensitivity_evening=True)
    assert eve_std["transition_label"] == "evening" and eve_std["phase"] == "EVENING"
    assert eve_std["target_m_edi_lx"] == 10.0
    assert eve_std["curve_profile"] == "comfort"
    assert 3000 <= eve_std["monitor_cct_k"] <= 3200
    assert eve_std["bulb_cct_k"] == 2700
    assert eve_sens["target_m_edi_lx"] == 5.0
    assert eve_sens["curve_profile"] == "strict"
    assert eve_sens["monitor_cct_k"] == 2200
    assert (eve_sens["bulb_brightness_pct"], eve_sens["bulb_cct_k"]) == (8, 2200)

    # 5. 30-min Night wind-down [22.5, 23.0) & NIGHT [23.0, 7.0)
    wd = compute_smooth_open_loop_targets(22.75, wake_hour=7.0, sleep_hour=23.0)
    assert wd["transition_label"] == "night_winddown"
    assert wd["phase"] == "EVENING"
    assert 50.0 <= wd["target_screen_nits"] <= 90.0
    assert 0 <= wd["bulb_brightness_pct"] <= 15

    ngt = compute_smooth_open_loop_targets(2.0, wake_hour=7.0, sleep_hour=23.0)
    assert ngt["transition_label"] == "night"
    assert ngt["phase"] == "NIGHT"
    assert ngt["target_m_edi_lx"] == 1.0
    assert ngt["target_screen_nits"] == 50.0
    assert ngt["monitor_cct_k"] == 3000
    assert ngt["monitor_brightness_pct"] >= 25
    assert ngt["bulb_power"] is False and ngt["bulb_brightness_pct"] == 0

    # 6. Midnight wraparound schedule (wake=9.0, sleep=1.0 -> evening starts at 22.0, winddown 0.5..1.0, night 1.0..9.0)
    wrap_day = compute_smooth_open_loop_targets(12.5, wake_hour=9.0, sleep_hour=1.0)
    wrap_aft = compute_smooth_open_loop_targets(16.0, wake_hour=9.0, sleep_hour=1.0)
    wrap_sunset = compute_smooth_open_loop_targets(22.1, wake_hour=9.0, sleep_hour=1.0)
    wrap_eve = compute_smooth_open_loop_targets(23.5, wake_hour=9.0, sleep_hour=1.0)
    wrap_wd = compute_smooth_open_loop_targets(0.75, wake_hour=9.0, sleep_hour=1.0)
    wrap_night = compute_smooth_open_loop_targets(3.0, wake_hour=9.0, sleep_hour=1.0)
    assert wrap_day["phase"] == "DAY" and wrap_day["transition_label"] == "day"
    assert wrap_aft["phase"] == "DAY" and wrap_aft["transition_label"] == "afternoon"
    assert wrap_sunset["phase"] == "EVENING" and wrap_sunset["transition_label"] == "sunset_ramp"
    assert wrap_eve["phase"] == "EVENING" and wrap_eve["transition_label"] == "evening"
    assert wrap_wd["phase"] == "EVENING" and wrap_wd["transition_label"] == "night_winddown"
    assert wrap_night["phase"] == "NIGHT" and wrap_night["transition_label"] == "night"


def test_telemetry_csv_logger_and_offline_compliance_report(tmp_path: Path) -> None:
    """
    Verify TelemetryLogger flush-on-tick CSV serialization, malformed-row resilience,
    and generate_compliance_report() offline Markdown & SVG/HTML output with zero external links,
    mandatory Brown et al. (2022) 18-55 scope disclaimer, and zero medical claims.
    """
    csv_path = tmp_path / "logs" / "room_7day.csv"
    out_dir = tmp_path / "reports"

    with TelemetryLogger(csv_path) as logger:
        # 1. DAY compliant tick (100 Hz flicker)
        logger.log_tick(
            ControlAction(
                phase="DAY",
                target_m_edi_lx=250.0,
                measured_m_edi_lx=310.0,
                filtered_m_edi_lx=310.0,
                bulb_brightness_pct=40,
                bulb_cct_k=5200,
                monitor_brightness_pct=80,
                monitor_cct_k=5500,
                shadow_rejected=False,
                unlinked_light_alert=False,
                alert_message="",
                compliance_status="SAGLANDI",
                uncertainty_k2_lx=52.7,
            ),
            mode="sensor",
            photopic_lx=355.0,
            cct_k=5300,
            m_der=0.873,
            flicker_hz=100.0,
            timestamp_iso="2026-11-22T10:00:00+00:00",
        )
        # 2. DAY occluded tick (>10s cover)
        logger.log_tick(
            ControlAction(
                phase="DAY",
                target_m_edi_lx=250.0,
                measured_m_edi_lx=20.0,
                filtered_m_edi_lx=310.0,
                bulb_brightness_pct=40,
                bulb_cct_k=5200,
                monitor_brightness_pct=80,
                monitor_cct_k=5500,
                shadow_rejected=True,
                unlinked_light_alert=False,
                alert_message="Sensor occluded",
                compliance_status="BELIRSIZ",
                uncertainty_k2_lx=52.7,
                sensor_occluded=True,
                reading_valid=False,
            ),
            mode="sensor",
            photopic_lx=25.0,
            cct_k=5100,
            m_der=0.80,
            flicker_hz=0.0,
            timestamp_iso="2026-11-22T11:00:00+00:00",
        )
        # 3. EVENING unlinked light alert tick (120 Hz flicker)
        logger.log_tick(
            ControlAction(
                phase="EVENING",
                target_m_edi_lx=10.0,
                measured_m_edi_lx=44.5,
                filtered_m_edi_lx=44.5,
                bulb_brightness_pct=0,
                bulb_cct_k=2300,
                monitor_brightness_pct=15,
                monitor_cct_k=2300,
                shadow_rejected=False,
                unlinked_light_alert=True,
                alert_message="Unlinked Light — Switch Off",
                compliance_status="SAGLANMADI",
                uncertainty_k2_lx=7.57,
            ),
            mode="sensor",
            photopic_lx=38.0,
            cct_k=6200,
            m_der=1.171,
            flicker_hz=120.0,
            timestamp_iso="2026-11-22T21:00:00+00:00",
        )
        # 4. Two consecutive OPEN-LOOP ticks (30 min = 0.5h duration)
        for ts_iso in ("2026-11-22T22:00:00+00:00", "2026-11-22T22:30:00+00:00"):
            logger.log_tick(
                ControlAction(
                    phase="EVENING",
                    target_m_edi_lx=10.0,
                    measured_m_edi_lx=0.0,
                    filtered_m_edi_lx=0.0,
                    bulb_brightness_pct=12,
                    bulb_cct_k=2300,
                    monitor_brightness_pct=20,
                    monitor_cct_k=2300,
                    shadow_rejected=False,
                    unlinked_light_alert=False,
                    alert_message="",
                    compliance_status="OPEN-LOOP",
                ),
                mode="open_loop",
                timestamp_iso=ts_iso,
            )
        # 5. NIGHT compliant tick
        logger.log_tick(
            ControlAction(
                phase="NIGHT",
                target_m_edi_lx=1.0,
                measured_m_edi_lx=0.12,
                filtered_m_edi_lx=0.12,
                bulb_brightness_pct=0,
                bulb_cct_k=2200,
                monitor_brightness_pct=10,
                monitor_cct_k=2200,
                shadow_rejected=False,
                unlinked_light_alert=False,
                alert_message="",
                compliance_status="SAGLANDI",
                uncertainty_k2_lx=0.02,
            ),
            mode="sensor",
            timestamp_iso="2026-11-23T01:00:00+00:00",
        )

    # Inject a corrupted/malformed CSV row to verify graceful skipping
    with csv_path.open("a", encoding="utf-8") as fh:
        fh.write("corrupt_timestamp,sensor,INVALID_PHASE,not_a_number,0,BELIRSIZ\n")

    summary = generate_compliance_report(csv_path, output_dir=out_dir)
    assert summary["total_ticks"] == 6
    assert summary["valid_ticks"] == 5
    assert summary["skipped_rows"] == 1
    assert summary["unlinked_alert_count"] == 1
    assert summary["peak_unlinked_m_edi_lx"] == 44.5
    assert summary["occluded_tick_count"] == 1
    assert summary["flicker_100_120_count"] == 2
    assert summary["open_loop_ticks"] == 2
    assert summary["open_loop_active_hours"] == 0.5
    assert summary["day_compliance"]["saglandi"] == 1
    assert summary["day_compliance"]["belirsiz"] == 1
    assert summary["evening_compliance"]["saglanmadi"] == 1
    assert summary["night_compliance"]["saglandi_pct"] == 100.0

    md_file = Path(summary["markdown_path"])
    html_file = Path(summary["html_path"])
    assert md_file.exists() and html_file.exists()

    md_text = md_file.read_text(encoding="utf-8")
    html_text = html_file.read_text(encoding="utf-8")

    # Verify offline purity (zero external http:// or https:// links), embedded SVG, and Brown 18-55 scope
    assert "http://" not in html_text and "https://" not in html_text
    assert "<svg" in html_text and "</svg>" in html_text
    assert "18-55" in html_text and "18-55" in md_text
    assert "not a medical device" in html_text.lower() and "not a medical device" in md_text.lower()


def test_user_config_atomic_save_load_and_corrupt_recovery(tmp_path: Path) -> None:
    """
    Verify UserConfig atomic save_config/load_config roundtrip, missing-file default fallback,
    corrupt JSON recovery, and CLI flag override precedence over --config.
    """
    cfg_path = tmp_path / "nested" / "config.json"

    # 1. Missing file returns safe defaults
    default_cfg = load_config(cfg_path)
    assert default_cfg == UserConfig()

    # 2. Atomic save & load roundtrip
    custom_cfg = UserConfig(
        wake_hour=6.5,
        sleep_hour=22.5,
        mode="simulate",
        light="mock",
        monitor_enabled=False,
        sensitive_evening=True,
        interval_s=0.0,
        log_csv=str(tmp_path / "cfg_ticks.csv"),
    )
    saved = save_config(custom_cfg, cfg_path)
    assert saved == cfg_path and cfg_path.exists()
    loaded = load_config(cfg_path)
    assert loaded == custom_cfg

    # 3. Corrupt JSON recovers gracefully to defaults without raising
    bad_path = tmp_path / "corrupt.json"
    bad_path.write_text("{broken json: [1, 2", encoding="utf-8")
    recovered = load_config(bad_path)
    assert recovered == UserConfig()

    # 4. CLI flags override --config values cleanly
    override_csv = tmp_path / "cli_override.csv"
    rc = daemon_main(
        [
            "--config",
            str(cfg_path),
            "--open-loop",
            "--light",
            "none",
            "--no-monitor",
            "--hour",
            "21.5",
            "--ticks",
            "2",
            "--interval",
            "0",
            "--log-csv",
            str(override_csv),
        ]
    )
    assert rc == 0
    assert override_csv.exists()


def test_none_light_driver_and_setup_wizard_mock(tmp_path: Path) -> None:
    """
    Verify NoneLightDriver (--light none) no-op behavior and run_setup_wizard()
    with injected input_fn/output_fn/mock display/discover_fn (including Night Light warning).
    """
    none_drv = NoneLightDriver()
    assert none_drv.name == "none"
    assert none_drv.set_state(50, 4000) is True
    assert none_drv.handoff_evening_scene() is True
    assert none_drv.get_state() is None

    class FakeSetupDisplay:
        def get_capabilities(self) -> dict[str, Any]:
            return {
                "is_windows": True,
                "wmi_laptop_available": True,
                "ddc_external_supported": False,
                "gdi_gamma_supported": True,
                "magnification_available": True,
                "windows_night_light_detected": True,
            }

    answers = iter(
        [
            "07:30",  # 1. Wake hour
            "23:30",  # 2. Bedtime hour
            "y",      # 3. High-Sensitivity Evening
            "2",      # 4. Light backend: wiz
            "y",      # Scan LAN for WiZ bulbs
            "1",      # Pick discovered bulb #1
            "n",      # 5. Disable monitor for dry-run config
            str(tmp_path / "wizard_log.csv"),  # 6. CSV path
        ]
    )
    outputs: list[str] = []
    cfg_file = tmp_path / "wizard_config.json"

    cfg = run_setup_wizard(
        config_path=cfg_file,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda line: outputs.append(str(line)),
        display=FakeSetupDisplay(),  # type: ignore[arg-type]
        discover_fn=lambda timeout=2.5: [{"ip": "192.168.1.77", "mac": "aabbccddeeff", "temp": 2700, "dimming": 40}],
    )
    joined_out = "\n".join(outputs)
    assert "Windows Night Light appears to be ON" in joined_out
    assert cfg_file.exists()
    assert cfg.wake_hour == 7.5
    assert cfg.sleep_hour == 23.5
    assert cfg.sensitive_evening is True
    assert cfg.light == "wiz"
    assert cfg.wiz_ip == "192.168.1.77"
    assert cfg.monitor_enabled is False


def test_display_capabilities_and_night_light_hint_hermetic() -> None:
    """
    Verify WindowsDisplayController.get_capabilities() respects injected mock backends
    hermetically without probing live Win32 DLLs, PowerShell WMI, or Windows Registry.
    """

    class MockWMI:
        def get_brightness(self) -> int:
            return 75

        def set_brightness(self, pct: int) -> bool:
            return True

    class MockMag:
        def initialize(self) -> bool:
            return True

        def set_matrix(self, matrix: list[float]) -> bool:
            return True

        def uninitialize(self) -> bool:
            return True

    disp = WindowsDisplayController(
        enabled=True,
        wmi_backend=MockWMI(),
        mag_backend=MockMag(),
        ddc_backend=None,
        gamma_backend=None,
    )
    caps = disp.get_capabilities()
    assert caps["wmi_laptop_available"] is True
    assert caps["magnification_available"] is True
    assert caps["ddc_external_supported"] is False
    assert caps["gdi_gamma_supported"] is False
    assert caps["windows_night_light_detected"] is None


def test_cli_end_to_end_open_loop_log_csv_and_report(tmp_path: Path) -> None:
    """
    Verify CLI --help formatting, end-to-end execution for both --open-loop --light none --log-csv
    and --simulate day_to_evening_demo --log-csv followed by --report <csv> --report-out <dir>.
    """
    help_text = build_arg_parser().format_help()
    assert "--open-loop" in help_text and "--monitor-safety-check" in help_text

    open_csv = tmp_path / "open_loop.csv"
    sim_csv = tmp_path / "sim_demo.csv"
    rep_dir = tmp_path / "sim_report"

    assert (
        daemon_main(
            [
                "--open-loop",
                "--light",
                "none",
                "--hour",
                "20.0",
                "--ticks",
                "3",
                "--interval",
                "0",
                "--log-csv",
                str(open_csv),
            ]
        )
        == 0
    )
    assert open_csv.exists()

    assert (
        daemon_main(
            [
                "--simulate",
                "day_to_evening_demo",
                "--light",
                "mock",
                "--ticks",
                "6",
                "--interval",
                "0",
                "--log-csv",
                str(sim_csv),
            ]
        )
        == 0
    )
    assert sim_csv.exists()

    assert (
        daemon_main(
            [
                "--report",
                str(sim_csv),
                "--report-out",
                str(rep_dir),
            ]
        )
        == 0
    )
    assert (rep_dir / "report_summary.md").exists()
    assert (rep_dir / "report_summary.html").exists()


# ---------------------------------------------------------------------------
# 9. Software Milestone 2: MQTT v3.1.1 Home Assistant Bridge & Local Braun Web UI
# ---------------------------------------------------------------------------


def test_mqtt_v311_wire_packets_and_ha_7_entity_discovery() -> None:
    """
    Verify RFC-compliant MQTT v3.1.1 wire packet builders (_encode_remaining_length,
    build_connect_packet, build_publish_packet, PINGREQ, DISCONNECT) and
    build_discovery_configs() registering all 7 Home Assistant entities.
    """
    # 1. MQTT v3.1.1 §2.2.3 Table 2.4 Remaining Length boundary vectors
    assert _encode_remaining_length(0) == b"\x00"
    assert _encode_remaining_length(127) == b"\x7f"
    assert _encode_remaining_length(128) == b"\x80\x01"
    assert _encode_remaining_length(16383) == b"\xff\x7f"
    assert _encode_remaining_length(16384) == b"\x80\x80\x01"
    with pytest.raises(ValueError):
        _encode_remaining_length(-1)

    # 2. CONNECT packet: header 0x10, protocol "MQTT" (0x04), CleanSession|Will|WillRetain|User|Pass = 0xE6
    conn_pkt = build_connect_packet(
        client_id="circadian_desk_primary",
        username="ha_user",
        password="ha_secret",
        keepalive_s=60,
        will_topic="circadian_desk/desk_primary/availability",
        will_payload="offline",
        will_retain=True,
    )
    assert conn_pkt[0] == 0x10
    assert b"\x00\x04MQTT\x04\xe6\x00\x3c" in conn_pkt
    assert b"circadian_desk_primary" in conn_pkt
    assert b"circadian_desk/desk_primary/availability" in conn_pkt
    assert b"offline" in conn_pkt

    # 3. PUBLISH QoS 0 (0x30 non-retained, 0x31 retained), PINGREQ (0xC0 0x00), DISCONNECT (0xE0 0x00)
    pub_norm = build_publish_packet("circadian_desk/desk_primary/state", '{"m_edi_lx":250.0}', retain=False)
    pub_ret = build_publish_packet("circadian_desk/desk_primary/availability", "online", retain=True)
    assert pub_norm[0] == 0x30
    assert pub_ret[0] == 0x31
    assert build_pingreq_packet() == b"\xc0\x00"
    assert build_disconnect_packet() == b"\xe0\x00"

    # 4. 7 Home Assistant Auto-Discovery configs (5 sensors + 2 binary_sensors)
    configs = build_discovery_configs(discovery_prefix="homeassistant", node_id="desk_primary")
    assert len(configs) == 7
    topics = [t for t, _ in configs]
    expected_suffixes = [
        "sensor/circadian_desk_desk_primary/m_edi/config",
        "sensor/circadian_desk_desk_primary/compliance/config",
        "sensor/circadian_desk_desk_primary/phase/config",
        "sensor/circadian_desk_desk_primary/cct/config",
        "sensor/circadian_desk_desk_primary/monitor_brightness/config",
        "binary_sensor/circadian_desk_desk_primary/unlinked_light/config",
        "binary_sensor/circadian_desk_desk_primary/sensor_occluded/config",
    ]
    for suf in expected_suffixes:
        assert f"homeassistant/{suf}" in topics

    for _topic, cfg in configs:
        assert cfg["availability_topic"] == "circadian_desk/desk_primary/availability"
        assert cfg["state_topic"] == "circadian_desk/desk_primary/state"
        assert cfg["device"]["identifiers"] == ["circadian_desk_desk_primary"]


def test_mqtt_bridge_lifecycle_and_broker_down_resilience() -> None:
    """
    Verify HomeAssistantMQTTBridge connect() -> online + 7 discovery publishes ->
    publish_state() -> close() (offline + DISCONNECT), plus never-crash resilience
    and reconnect cooldown when broker is unreachable or drops mid-stream.
    """

    class FakeMQTTSocket:
        def __init__(self, connack: bytes = b"\x20\x02\x00\x00", fail_after_writes: int | None = None) -> None:
            self._rx = bytearray(connack)
            self.tx_packets: list[bytes] = []
            self.closed = False
            self._fail_after = fail_after_writes

        def settimeout(self, timeout: float) -> None:
            pass

        def sendall(self, data: bytes) -> None:
            if self._fail_after is not None and len(self.tx_packets) >= self._fail_after:
                raise BrokenPipeError("Simulated MQTT broker TCP drop")
            self.tx_packets.append(bytes(data))

        def recv(self, n: int) -> bytes:
            if not self._rx:
                return b""
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

        def close(self) -> None:
            self.closed = True

    sockets: list[FakeMQTTSocket] = []

    def ok_socket_factory(host: str, port: int, timeout_s: float) -> FakeMQTTSocket:
        sock = FakeMQTTSocket()
        sockets.append(sock)
        return sock

    bridge = HomeAssistantMQTTBridge(
        host="127.0.0.1",
        port=1883,
        username="ha",
        password="pw",
        socket_factory=ok_socket_factory,
    )
    assert bridge.connect() is True
    assert bridge.connected is True
    sock = sockets[0]
    # 1 CONNECT + 1 retained "online" availability + 7 retained discovery configs = 9 packets
    assert len(sock.tx_packets) == 9
    assert sock.tx_packets[0][0] == 0x10
    assert b"online" in sock.tx_packets[1]

    # Publish state tick
    assert bridge.publish_state({"phase": "DAY", "m_edi_lx": 280.5, "compliance_status": "SAGLANDI"}) is True
    assert len(sock.tx_packets) == 10
    assert b'"m_edi_lx":280.5' in sock.tx_packets[-1] or b'"m_edi_lx": 280.5' in sock.tx_packets[-1]

    # Clean close sends retained "offline" + DISCONNECT (0xE0 0x00)
    bridge.close()
    assert bridge.connected is False
    assert sock.closed is True
    assert b"offline" in sock.tx_packets[-2]
    assert sock.tx_packets[-1] == b"\xe0\x00"

    # Unreachable broker & cooldown resilience: never raises, enforces >=10s cooldown
    fake_clock = {"t": 100.0}
    connect_attempts = {"count": 0}

    def down_socket_factory(host: str, port: int, timeout_s: float) -> FakeMQTTSocket:
        connect_attempts["count"] += 1
        raise OSError("Broker connection refused")

    down_bridge = HomeAssistantMQTTBridge(
        host="127.0.0.1",
        socket_factory=down_socket_factory,
        reconnect_cooldown_s=10.0,
        monotonic_fn=lambda: fake_clock["t"],
    )
    assert down_bridge.publish_state({"phase": "DAY"}) is False
    assert connect_attempts["count"] == 1

    # Immediate retry 2s later is suppressed by cooldown
    fake_clock["t"] = 102.0
    assert down_bridge.publish_state({"phase": "DAY"}) is False
    assert connect_attempts["count"] == 1

    # After 11s (>10s cooldown), retry is attempted cleanly
    fake_clock["t"] = 112.0
    assert down_bridge.publish_state({"phase": "DAY"}) is False
    assert connect_attempts["count"] == 2

    # Mid-run BrokenPipeError returns False without raising
    drop_sock = FakeMQTTSocket(fail_after_writes=9)
    drop_bridge = HomeAssistantMQTTBridge(
        host="127.0.0.1",
        socket_factory=lambda h, p, t: drop_sock,
        monotonic_fn=lambda: fake_clock["t"],
    )
    assert drop_bridge.connect() is True
    assert drop_bridge.publish_state({"phase": "EVENING"}) is False
    assert drop_bridge.connected is False


def test_web_ui_http_endpoints_security_csrf_and_offline_html(tmp_path: Path) -> None:
    """
    Verify LocalWebUIServer loopback enforcement, 100% offline Braun HTML (0 external links),
    GET /api/state, POST /api/pause, POST /api/settings + callback, 400 validation,
    403 Host/Origin CSRF & DNS-rebinding guards, and GET /report (both empty & CSV-backed SVG).
    """
    # 1. Non-loopback bind host must be rejected immediately
    with pytest.raises(ValueError, match="non-loopback"):
        LocalWebUIServer(OrchestratorState(), host="0.0.0.0", port=0)

    saved_callbacks: list[dict[str, Any]] = []
    state = OrchestratorState(
        mode="sensor",
        wake_hour=7.0,
        sleep_hour=23.0,
        high_sensitivity_evening=False,
        capabilities={"wmi_laptop_available": True, "magnification_available": True},
        on_settings_saved=lambda cfg: saved_callbacks.append(dict(cfg)),
    )
    state.update_snapshot(
        phase="DAY",
        m_edi_lx=275.4,
        uncertainty_k2_lx=46.8,
        compliance_status="SAGLANDI",
        photopic_lx=310.0,
        cct_k=5400,
        m_der=0.888,
        monitor_brightness_pct=80,
        monitor_cct_k=5500,
        color_method="magnification",
    )

    def _req(
        port: int,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            hdrs = {"Content-Type": "application/json"} if body is not None else {}
            if headers:
                hdrs.update(headers)
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    with LocalWebUIServer(state, host="127.0.0.1", port=0) as srv:
        port = srv.actual_port
        assert port > 0

        # 2. GET / returns offline Braun HTML with 0 external links and Brown 18-55 disclaimer
        st_root, html_root = _req(port, "GET", "/")
        assert st_root == 200
        assert "http://" not in html_root and "https://" not in html_root
        assert "18&ndash;55" in html_root or "18-55" in html_root
        assert "not a medical device" in html_root.lower()
        assert "pauseBtn" in html_root and "sensToggle" in html_root and "/report" in html_root

        # 3. GET /api/state returns live snapshot + sparkline history
        st_api, body_api = _req(port, "GET", "/api/state")
        assert st_api == 200
        data_api = json.loads(body_api)
        assert data_api["m_edi_lx"] == 275.4
        assert data_api["compliance_status"] == "SAGLANDI"
        assert len(data_api["history"]) == 1

        # 4. POST /api/pause (120 min pause -> resume 0 min) & 400 validation
        st_p1, body_p1 = _req(port, "POST", "/api/pause", {"minutes": 120})
        assert st_p1 == 200 and json.loads(body_p1)["paused"] is True
        assert state.is_paused()[0] is True

        st_p0, body_p0 = _req(port, "POST", "/api/pause", {"minutes": 0})
        assert st_p0 == 200 and json.loads(body_p0)["paused"] is False
        assert state.is_paused()[0] is False

        st_p_bad, _ = _req(port, "POST", "/api/pause", {"minutes": 999})
        assert st_p_bad == 400

        # 5. POST /api/settings updates schedule & triggers on_settings_saved callback
        st_set, body_set = _req(
            port,
            "POST",
            "/api/settings",
            {"wake_hour": 6.5, "sleep_hour": 22.5, "high_sensitivity_evening": True},
        )
        assert st_set == 200
        assert state.get_settings() == (6.5, 22.5, True)
        assert len(saved_callbacks) == 1
        assert saved_callbacks[0]["wake_hour"] == 6.5

        st_set_bad, _ = _req(port, "POST", "/api/settings", {"wake_hour": 25.0})
        assert st_set_bad == 400

        # 6. Host & Origin CSRF / DNS-rebinding guards return 403 Forbidden
        st_csrf_origin, _ = _req(
            port,
            "POST",
            "/api/pause",
            {"minutes": 30},
            headers={"Origin": "https://evil.example"},
        )
        assert st_csrf_origin == 403

        st_csrf_host, _ = _req(
            port,
            "POST",
            "/api/pause",
            {"minutes": 30},
            headers={"Host": "evil.example"},
        )
        assert st_csrf_host == 403

        # 7. GET /report: without CSV returns offline instruction page; with CSV returns live SVG report
        st_rep0, html_rep0 = _req(port, "GET", "/report")
        assert st_rep0 == 200
        assert "http://" not in html_rep0 and "https://" not in html_rep0
        assert "--log-csv" in html_rep0

        csv_file = tmp_path / "ui_report.csv"
        with TelemetryLogger(csv_file) as tlog:
            tlog.log_tick(
                ControlAction(
                    phase="DAY",
                    target_m_edi_lx=250.0,
                    measured_m_edi_lx=290.0,
                    filtered_m_edi_lx=290.0,
                    bulb_brightness_pct=20,
                    bulb_cct_k=5200,
                    monitor_brightness_pct=80,
                    monitor_cct_k=5500,
                    shadow_rejected=False,
                    unlinked_light_alert=False,
                    alert_message="",
                    compliance_status="SAGLANDI",
                    uncertainty_k2_lx=49.3,
                ),
                mode="sensor",
                photopic_lx=330.0,
                cct_k=5300,
                m_der=0.879,
                timestamp_iso="2026-11-22T12:00:00+00:00",
            )
        state.set_csv_path(csv_file)
        st_rep1, html_rep1 = _req(port, "GET", "/report")
        assert st_rep1 == 200
        assert "<svg" in html_rep1 and "http://" not in html_rep1 and "https://" not in html_rep1


def test_daemon_web_ui_live_pause_and_settings_integration() -> None:
    """
    Verify daemon.main() runs cleanly with --web-ui --ui-port 0 across both --simulate
    and --open-loop modes, tears down the HTTP server with zero dangling sockets, and
    build_arg_parser().format_help() renders all new --web-ui / --mqtt-* flags cleanly.
    """
    help_out = build_arg_parser().format_help()
    for flag in ("--web-ui", "--no-web-ui", "--ui-port", "--mqtt-host", "--mqtt-prefix"):
        assert flag in help_out

    assert (
        daemon_main(
            [
                "--simulate",
                "day_to_evening_demo",
                "--light",
                "mock",
                "--ticks",
                "3",
                "--interval",
                "0",
                "--web-ui",
                "--ui-port",
                "0",
            ]
        )
        == 0
    )

    assert (
        daemon_main(
            [
                "--open-loop",
                "--light",
                "none",
                "--hour",
                "21.0",
                "--ticks",
                "2",
                "--interval",
                "0",
                "--web-ui",
                "--ui-port",
                "0",
            ]
        )
        == 0
    )


def test_config_v61_backward_compat_and_mqtt_ui_fields(tmp_path: Path) -> None:
    """
    Verify an older Milestone-1 config.json (missing web_ui_* and mqtt_* fields) loads
    cleanly with safe defaults, and a full Milestone-2 UserConfig roundtrips atomically.
    """
    legacy_path = tmp_path / "legacy_m1_config.json"
    legacy_path.write_text(
        json.dumps(
            {
                "wake_hour": 7.5,
                "sleep_hour": 23.25,
                "mode": "open_loop",
                "light": "none",
                "monitor_enabled": True,
                "sensitive_evening": True,
            }
        ),
        encoding="utf-8",
    )
    loaded_legacy = load_config(legacy_path)
    assert loaded_legacy.wake_hour == 7.5
    assert loaded_legacy.sleep_hour == 23.25
    assert loaded_legacy.sensitive_evening is True
    assert loaded_legacy.web_ui_enabled is True
    assert loaded_legacy.web_ui_port == 8765
    assert loaded_legacy.mqtt_host == ""
    assert loaded_legacy.mqtt_port == 1883
    assert loaded_legacy.mqtt_prefix == "homeassistant"

    m2_path = tmp_path / "m2_full_config.json"
    m2_cfg = UserConfig(
        wake_hour=6.75,
        sleep_hour=22.75,
        mode="open_loop",
        light="wiz",
        wiz_ip="192.168.1.55",
        web_ui_enabled=True,
        web_ui_port=8799,
        mqtt_host="192.168.1.10",
        mqtt_port=1883,
        mqtt_user="desk_user",
        mqtt_password="desk_pass",
        mqtt_prefix="ha_custom",
    )
    save_config(m2_cfg, m2_path)
    assert load_config(m2_path) == m2_cfg


# ---------------------------------------------------------------------------
# 10. Ship-Ready Polish: Zero-Capture Auto-Pause, Native Win32 Tray, Braun ICO,
#     Frozen Double-Click Entry & Microsoft Store MSIX Manifest
# ---------------------------------------------------------------------------


def test_foreground_app_watcher_color_critical_fullscreen_and_shell_filter() -> None:
    """
    Verify ForegroundAppWatcher with injected foreground_provider across Photoshop/Figma/DaVinci
    (color_critical), borderless fullscreen games (fullscreen), normal maximized Chrome/VS Code
    (False), shell Progman/WorkerW/explorer.exe (False), custom app list, and enabled=False.
    """
    current_fg: dict[str, ForegroundAppInfo | None] = {"info": None}

    watcher = ForegroundAppWatcher(
        enabled=True,
        pause_on_fullscreen=True,
        foreground_provider=lambda: current_fg["info"],
    )

    # 1. Color-critical creative apps trigger color_critical auto-pause
    for app_exe in ("Photoshop.exe", "Figma.exe", "Resolve.exe"):
        current_fg["info"] = ForegroundAppInfo(exe_name=app_exe, window_class="AppWindow", is_fullscreen=False)
        dec = watcher.check()
        assert dec.should_pause is True
        assert dec.trigger_kind == "color_critical"
        assert app_exe.lower() in dec.reason.lower()

    # 2. Borderless/exclusive fullscreen game triggers fullscreen auto-pause
    current_fg["info"] = ForegroundAppInfo(
        exe_name="Cyberpunk2077.exe",
        window_class=" WNDCLASS",
        is_fullscreen=True,
    )
    dec_game = watcher.check()
    assert dec_game.should_pause is True
    assert dec_game.trigger_kind == "fullscreen"

    # 3. Normal maximized Chrome / VS Code (is_fullscreen=False) does NOT pause
    for normal_exe in ("chrome.exe", "Code.exe"):
        current_fg["info"] = ForegroundAppInfo(exe_name=normal_exe, window_class="Chrome_WidgetWin_1", is_fullscreen=False)
        assert watcher.check().should_pause is False

    # 4. Windows shell desktop/wallpaper/taskbar (Progman, WorkerW, explorer.exe) NEVER pauses even if fullscreen
    for cls_name, exe_name in (("Progman", "explorer.exe"), ("WorkerW", "explorer.exe"), ("CabinetWClass", "explorer.exe")):
        current_fg["info"] = ForegroundAppInfo(exe_name=exe_name, window_class=cls_name, is_fullscreen=True)
        assert watcher.check().should_pause is False

    # 5. Custom color_critical_apps list & enabled=False
    custom_watcher = ForegroundAppWatcher(
        enabled=True,
        pause_on_fullscreen=False,
        color_critical_apps=["Cinema4D.exe"],
        foreground_provider=lambda: ForegroundAppInfo(exe_name="cinema4d.exe", is_fullscreen=False),
    )
    assert custom_watcher.check().should_pause is True
    custom_watcher.enabled = False
    assert custom_watcher.check().should_pause is False


def test_braun_ico_generator_and_tray_action_controller(tmp_path: Path) -> None:
    """
    Verify generate_braun_ico_bytes() ICO header magic (b'\\x00\\x00\\x01\\x00'),
    prototype/assets/circadian_desk.ico on disk, and TrayActionController actions.
    """
    ico_bytes = generate_braun_ico_bytes(size=32)
    assert ico_bytes[:4] == b"\x00\x00\x01\x00"
    assert len(ico_bytes) == 4286

    asset_ico = PROTOTYPE_ROOT / "assets" / "circadian_desk.ico"
    assert asset_ico.is_file()
    assert asset_ico.read_bytes()[:4] == b"\x00\x00\x01\x00"

    tmp_ico = ensure_icon_file(tmp_path / "test_tray.ico")
    assert tmp_ico.is_file() and tmp_ico.stat().st_size == 4286

    state = OrchestratorState(mode="open_loop", high_sensitivity_evening=False)
    stop_event = threading.Event()
    opened_urls: list[str] = []
    ctrl = TrayActionController(
        state=state,
        stop_event=stop_event,
        ui_url="http://127.0.0.1:8765",
        open_url_fn=lambda url: opened_urls.append(url) or True,
    )

    assert ctrl.open_dashboard() is True
    assert opened_urls == ["http://127.0.0.1:8765"]

    # Toggle 2h pause ON -> OFF
    assert ctrl.toggle_pause_2h() is True
    assert state.is_paused()[0] is True
    assert ctrl.toggle_pause_2h() is False
    assert state.is_paused()[0] is False

    # Toggle auto-pause ON -> OFF -> ON
    assert ctrl.get_auto_pause_enabled() is True
    assert ctrl.toggle_auto_pause() is False
    assert state.to_dict()["auto_pause_enabled"] is False
    assert ctrl.toggle_auto_pause() is True
    assert state.to_dict()["auto_pause_enabled"] is True

    # Toggle high-sensitivity evening OFF -> ON
    assert ctrl.toggle_sensitive_evening() is True
    assert state.get_settings()[2] is True

    # Quit sets stop_event
    assert stop_event.is_set() is False
    ctrl.request_quit()
    assert stop_event.is_set() is True


def test_web_ui_auto_pause_state_and_settings_endpoint() -> None:
    """
    Verify GET / includes autoPausePill & autoPauseToggle with 0 external URLs,
    POST /api/settings toggles auto_pause_enabled & triggers callback, and
    GET /api/state exposes auto_paused & auto_pause_reason.
    """
    saved: list[dict[str, Any]] = []
    state = OrchestratorState(
        mode="open_loop",
        on_settings_saved=lambda payload: saved.append(dict(payload)),
    )
    state.update_snapshot(
        phase="EVENING",
        auto_paused=True,
        auto_pause_reason="Color-critical app (Photoshop.exe)",
    )

    with LocalWebUIServer(state, host="127.0.0.1", port=0) as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.actual_port, timeout=5)
        try:
            conn.request("GET", "/")
            resp_root = conn.getresponse()
            html_root = resp_root.read().decode("utf-8")
            assert resp_root.status == 200
            assert "autoPausePill" in html_root and "autoPauseToggle" in html_root
            assert "http://" not in html_root and "https://" not in html_root

            conn.request("GET", "/api/state")
            resp_st = conn.getresponse()
            st_data = json.loads(resp_st.read().decode("utf-8"))
            assert st_data["auto_paused"] is True
            assert "Photoshop.exe" in st_data["auto_pause_reason"]
            assert st_data["auto_pause_enabled"] is True

            body = json.dumps({"auto_pause_enabled": False}).encode("utf-8")
            conn.request("POST", "/api/settings", body=body, headers={"Content-Type": "application/json"})
            resp_post = conn.getresponse()
            post_data = json.loads(resp_post.read().decode("utf-8"))
            assert resp_post.status == 200
            assert post_data["auto_pause_enabled"] is False
            assert len(saved) == 1 and saved[0]["auto_pause_enabled"] is False
        finally:
            conn.close()


def test_daemon_frozen_default_argv_noconsole_and_auto_pause_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Verify resolve_default_argv() returns zero-friction Desktop Beta defaults when frozen
    CircadianDesk.exe is double-clicked, build_arg_parser().format_help() renders --tray and
    --auto-pause flags cleanly, and daemon.main() runs with --auto-pause.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["CircadianDesk.exe"])
    default_args = resolve_default_argv(None)
    assert default_args is not None
    assert "--open-loop" in default_args
    assert "--tray" in default_args
    assert "--auto-pause" in default_args
    assert "--web-ui" in default_args

    help_text = build_arg_parser().format_help()
    for flag in ("--tray", "--no-tray", "--auto-pause", "--no-auto-pause", "--no-auto-pause-fullscreen"):
        assert flag in help_text

    assert (
        daemon_main(
            [
                "--simulate",
                "day_to_evening_demo",
                "--light",
                "mock",
                "--ticks",
                "3",
                "--interval",
                "0",
                "--auto-pause",
            ]
        )
        == 0
    )


def test_config_v61_auto_pause_and_tray_backward_compat(tmp_path: Path) -> None:
    """
    Verify a legacy config.json without auto_pause_* / tray_enabled loads cleanly with safe
    defaults, and a full UserConfig with custom auto_pause_apps roundtrips atomically.
    """
    old_file = tmp_path / "old_config.json"
    old_file.write_text('{"wake_hour": 7.0, "sleep_hour": 23.0, "light": "none"}', encoding="utf-8")
    loaded = load_config(old_file)
    assert loaded.auto_pause_enabled is True
    assert loaded.auto_pause_fullscreen is True
    assert loaded.auto_pause_apps == []
    assert loaded.tray_enabled is True

    full_file = tmp_path / "full_config.json"
    full_cfg = UserConfig(
        wake_hour=7.25,
        sleep_hour=23.25,
        auto_pause_enabled=False,
        auto_pause_fullscreen=False,
        auto_pause_apps=["CustomColorApp.exe", "GameClient.exe"],
        tray_enabled=False,
    )
    save_config(full_cfg, full_file)
    reloaded = load_config(full_file)
    assert reloaded.auto_pause_enabled is False
    assert reloaded.auto_pause_fullscreen is False
    assert reloaded.auto_pause_apps == ["customcolorapp.exe", "gameclient.exe"]
    assert reloaded.tray_enabled is False


def test_msix_manifest_xml_and_build_windows_check() -> None:
    """
    Verify prototype/packaging/msix/AppxManifest.xml parses as valid XML referencing
    CircadianDesk.exe and runFullTrust, and build_windows.main(['--check-only']) exits 0.
    """
    manifest_path = PROTOTYPE_ROOT / "packaging" / "msix" / "AppxManifest.xml"
    assert manifest_path.is_file()
    xml_text = manifest_path.read_text(encoding="utf-8")
    root = ET.fromstring(xml_text)
    assert root.tag.endswith("Package")
    assert "CircadianDesk.exe" in xml_text
    assert "runFullTrust" in xml_text

    assert build_windows.main(["--check-only"]) == 0


# ---------------------------------------------------------------------------
# 11. Final UX, Auto-Detected Peak Nits (CTA-861.3 EDID), Nit-Normalized
#     Comfort/Strict Curves & Live Manual Override Sliders
# ---------------------------------------------------------------------------


def test_cta861_edid_peak_nits_parser_and_3tier_detection() -> None:
    """
    Verify parse_cta861_max_nits() on CTA-861.3 HDR Static Metadata block bytes
    (E6 06 05 01 6A 6A 48 -> ~496.7 nits max, ~0.396 nits min) and
    detect_display_luminance_profile() across Tier 1 (edid_cta861), Tier 2
    (dxgi_hdr vs generic 270.0 placeholder rejection), and Tier 3 (estimated_sdr 300.0 nits).
    """
    # Construct a 128-byte CTA-861 extension block (tag=0x02, rev=0x03, dtd_offset=0x10)
    # containing HDR Static Metadata block: E6 06 05 01 6A 6A 48
    cta_block = bytearray(128)
    cta_block[0] = 0x02
    cta_block[1] = 0x03
    cta_block[2] = 0x10
    hdr_block = bytes((0xE6, 0x06, 0x05, 0x01, 0x6A, 0x6A, 0x48))
    cta_block[4 : 4 + len(hdr_block)] = hdr_block

    parsed = parse_cta861_max_nits(bytes(cta_block))
    assert parsed is not None
    max_nits, min_nits = parsed
    assert abs(max_nits - 496.7) <= 0.2
    assert abs(min_nits - 0.396) <= 0.01

    # Tier 1: EDID CTA-861.3 succeeds even when Windows HDR is OFF
    t1 = detect_display_luminance_profile(
        edid_reader=lambda: (bytes(cta_block), "NE160QDM-NZL"),
        hdr_reader=lambda: {"hdr_enabled": False, "max_nits": 0.0, "min_nits": 0.0},
    )
    assert t1["source"] == "edid_cta861"
    assert t1["is_auto_detected"] is True
    assert t1["model_name"] == "NE160QDM-NZL"
    assert abs(t1["max_nits"] - 496.7) <= 0.2

    # Tier 2a: EDID lacks HDR block, active Windows HDR reports 600.0 nits -> dxgi_hdr
    t2 = detect_display_luminance_profile(
        edid_reader=lambda: (b"", "OLED-27"),
        hdr_reader=lambda: {"hdr_enabled": True, "max_nits": 600.0, "min_nits": 0.05},
    )
    assert t2["source"] == "dxgi_hdr"
    assert t2["is_auto_detected"] is True
    assert t2["max_nits"] == 600.0

    # Tier 2b: Generic Windows SDR 270.0 placeholder must be rejected -> falls through to Tier 3
    t3 = detect_display_luminance_profile(
        edid_reader=lambda: (b"", "GenericSDR"),
        hdr_reader=lambda: {"hdr_enabled": True, "max_nits": 270.0, "min_nits": 0.5},
    )
    assert t3["source"] == "estimated_sdr"
    assert t3["is_auto_detected"] is False
    assert t3["max_nits"] == 300.0


def test_nit_normalization_comfort_vs_strict_and_24h_curve() -> None:
    """
    Verify nits_to_backlight_pct / backlight_pct_to_nits across 300, 500, and 1000 nit panels
    (strictly decreasing % for equal target nits), Comfort (2700K, night floor >=25% WMI) vs
    Strict (2200K, night 30 nits), and 48-point sample_24h_curve().
    """
    # Equal 200-nit target requires strictly lower backlight % on brighter panels
    pct_300 = nits_to_backlight_pct(200.0, max_nits=300.0, gamma=2.0)
    pct_500 = nits_to_backlight_pct(200.0, max_nits=500.0, gamma=2.0)
    pct_1000 = nits_to_backlight_pct(200.0, max_nits=1000.0, gamma=2.0)
    assert pct_300 > pct_500 > pct_1000
    assert (pct_300, pct_500, pct_1000) == (80, 60, 39)

    # Forward/inverse consistency
    est_nits = backlight_pct_to_nits(pct_500, max_nits=500.0, gamma=2.0)
    assert abs(est_nits - 200.0) <= 15.0

    # Comfort vs Strict at 00:39 (NIGHT) on a 497-nit laptop panel (WMI gamma=2.0)
    night_comfort = compute_smooth_open_loop_targets(
        hour_of_day=0.65,
        wake_hour=7.0,
        sleep_hour=23.0,
        comfort_mode=True,
        high_sensitivity_evening=False,
        display_max_nits=497.0,
        backlight_gamma=2.0,
    )
    night_strict = compute_smooth_open_loop_targets(
        hour_of_day=0.65,
        wake_hour=7.0,
        sleep_hour=23.0,
        comfort_mode=False,
        high_sensitivity_evening=False,
        display_max_nits=497.0,
        backlight_gamma=2.0,
    )
    assert night_comfort["curve_profile"] == "comfort"
    assert night_comfort["monitor_cct_k"] == 3000
    assert night_comfort["target_screen_nits"] == 50.0
    assert night_comfort["monitor_brightness_pct"] >= 25

    assert night_strict["curve_profile"] == "strict"
    assert night_strict["monitor_cct_k"] == 2200
    assert night_strict["target_screen_nits"] == 30.0
    assert night_strict["monitor_brightness_pct"] < night_comfort["monitor_brightness_pct"]

    # 48-point 24-hour curve sampling
    curve = sample_24h_curve(wake_hour=7.0, sleep_hour=23.0, display_max_nits=497.0)
    assert len(curve) == 48
    assert curve[0]["hour"] == 0.0 and curve[-1]["hour"] == 23.5
    assert all("target_screen_nits" in pt and "monitor_cct_k" in pt for pt in curve)


def test_web_ui_manual_sliders_override_time_inputs_and_24h_curve() -> None:
    """
    Verify GET / includes <input type="time">, 0 external URLs, Auto/300/500/800 nit presets,
    and manual sliders; test POST /api/override (enabled=True sets wake_event, enabled=False
    resumes auto, 400 on out-of-range, 403 on spoofed Origin/Host CSRF); test POST /api/settings
    updating display_max_nits & comfort_mode.
    """
    saved_settings: list[dict[str, Any]] = []
    state = OrchestratorState(
        mode="open_loop",
        on_settings_saved=lambda cfg: saved_settings.append(dict(cfg)),
    )
    state.update_snapshot(
        phase="EVENING",
        detected_max_nits=496.7,
        detected_panel_model="NE160QDM-NZL",
        detected_nits_source="edid_cta861",
        target_screen_nits=75.0,
        schedule_curve=sample_24h_curve(display_max_nits=496.7),
    )

    def _req(
        port: int,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            hdrs = {"Content-Type": "application/json"} if body is not None else {}
            if headers:
                hdrs.update(headers)
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    with LocalWebUIServer(state, host="127.0.0.1", port=0) as srv:
        port = srv.actual_port

        st_html, html_root = _req(port, "GET", "/")
        assert st_html == 200
        assert "http://" not in html_root and "https://" not in html_root
        assert 'type="time"' not in html_root  # Zero native time inputs
        assert "<select" not in html_root.lower() and 'createElement("select")' not in html_root  # Zero <select> chevrons
        for ui_tok in ("data-time-picker", "timePopover", "onboardingGate", "dashboardRoot", "openWizardBtn", "1320px"):
            assert ui_tok in html_root
        assert "manualBriSlider" in html_root and "manualCctSlider" in html_root
        assert "/api/override" in html_root

        # POST /api/override enables live slider override and fires wake_event
        state.wake_event.clear()
        st_ov, body_ov = _req(
            port,
            "POST",
            "/api/override",
            {"enabled": True, "brightness_pct": 65, "cct_k": 3800},
        )
        assert st_ov == 200
        ov_data = json.loads(body_ov)
        assert ov_data["enabled"] is True
        assert ov_data["brightness_pct"] == 65
        assert ov_data["cct_k"] == 3800
        assert state.wake_event.is_set() is True
        assert state.to_dict()["manual_override_enabled"] is True

        # Return to Auto Curve (enabled=False)
        st_auto, body_auto = _req(port, "POST", "/api/override", {"enabled": False})
        assert st_auto == 200
        assert json.loads(body_auto)["enabled"] is False
        assert state.to_dict()["manual_override_enabled"] is False

        # Out-of-range brightness/CCT returns 400
        st_bad, _ = _req(port, "POST", "/api/override", {"enabled": True, "brightness_pct": 5, "cct_k": 3800})
        assert st_bad == 400

        # Spoofed Origin or Host on /api/override returns 403 Forbidden
        st_csrf_o, _ = _req(
            port,
            "POST",
            "/api/override",
            {"enabled": True, "brightness_pct": 50, "cct_k": 3500},
            headers={"Origin": "https://attacker.example"},
        )
        assert st_csrf_o == 403
        st_csrf_h, _ = _req(
            port,
            "POST",
            "/api/override",
            {"enabled": True, "brightness_pct": 50, "cct_k": 3500},
            headers={"Host": "attacker.example"},
        )
        assert st_csrf_h == 403

        # POST /api/settings updates display_max_nits & comfort_mode
        st_set, body_set = _req(
            port,
            "POST",
            "/api/settings",
            {"display_max_nits": 500.0, "comfort_mode": False},
        )
        assert st_set == 200
        set_data = json.loads(body_set)
        assert set_data["display_max_nits"] == 500.0
        assert set_data["comfort_mode"] is False
        assert saved_settings[-1]["display_max_nits"] == 500.0
        assert saved_settings[-1]["comfort_mode"] is False


def test_tray_quit_escaped_ampersand_and_config_nit_fields(tmp_path: Path) -> None:
    """
    Verify tray_win32.py escapes 'Quit && Restore Display' (preventing Win32 AppendMenuW
    single-& mnemonic swallowing) and UserConfig backward-compatibility for display_max_nits
    and comfort_mode.
    """
    tray_src = (PROTOTYPE_ROOT / "orchestrator" / "tray_win32.py").read_text(encoding="utf-8")
    assert "Quit && Restore Display" in tray_src

    legacy_cfg_path = tmp_path / "legacy_pre_nit.json"
    legacy_cfg_path.write_text('{"wake_hour": 7.0, "sleep_hour": 23.0}', encoding="utf-8")
    loaded_legacy = load_config(legacy_cfg_path)
    assert loaded_legacy.display_max_nits == 0.0
    assert loaded_legacy.comfort_mode is True

    new_cfg_path = tmp_path / "nit_config.json"
    new_cfg = UserConfig(
        wake_hour=7.0,
        sleep_hour=23.0,
        display_max_nits=496.7,
        comfort_mode=False,
    )
    save_config(new_cfg, new_cfg_path)
    reloaded = load_config(new_cfg_path)
    assert reloaded.display_max_nits == 496.7
    assert reloaded.comfort_mode is False


def test_daemon_live_manual_override_priority_and_fast_wake() -> None:
    """
    Verify _wait_for_next_tick(5.0, stop_event, ui_state.wake_event) wakes in <0.4s
    when ui_state.set_manual_override(True, 65, 3800) fires wake_event.
    """
    ui_state = OrchestratorState(mode="open_loop")
    stop_event = threading.Event()
    ui_state.wake_event.clear()

    def _trigger_slider_move() -> None:
        time.sleep(0.05)
        ui_state.set_manual_override(enabled=True, brightness_pct=65, cct_k=3800)

    t_Thread = threading.Thread(target=_trigger_slider_move, daemon=True)
    t0 = time.monotonic()
    t_Thread.start()
    should_stop = _wait_for_next_tick(5.0, stop_event, ui_state.wake_event)
    elapsed_s = time.monotonic() - t0
    t_Thread.join(timeout=1.0)

    assert should_stop is False
    assert elapsed_s < 0.4, f"Expected immediate wake (<0.4s) on slider move, took {elapsed_s:.3f}s"
    assert ui_state.get_manual_override() == (True, 65, 3800)


# ---------------------------------------------------------------------------
# 12. Wave 3 Certification: Live Display Conflicts, Sleep-Resume Re-Arm,
#     Wake-Anchored 7-Day Schedule, Movie Mode, Browser Fullscreen & Onboarding
# ---------------------------------------------------------------------------


def test_live_display_conflicts_and_sleep_resume_rearm() -> None:
    """
    Verify detect_live_display_conflicts() dynamically reports & clears Night Light, HDR,
    ACM, and competing color-temp apps (flux.exe), and SleepHandoffGuard._handle_resume()
    re-arms _triggered, fires on_resume, and works cleanly with CommandDispatcher.reset_cache().
    """
    # 1. All 4 conflict types active
    active = detect_live_display_conflicts(
        night_light_probe=lambda: True,
        hdr_acm_probe=lambda: (True, True),
        process_probe=lambda: ["flux.exe"],
    )
    assert active["night_light_on"] is True
    assert active["hdr_active"] is True
    assert active["acm_active"] is True
    assert active["competing_apps"] == ["flux.exe"]
    assert len(active["warnings"]) >= 4

    # 2. Conflicts cleared cleanly -> empty warnings list
    clean = detect_live_display_conflicts(
        night_light_probe=lambda: False,
        hdr_acm_probe=lambda: (False, False),
        process_probe=lambda: [],
    )
    assert clean["night_light_on"] is False
    assert clean["hdr_active"] is False
    assert clean["acm_active"] is False
    assert clean["competing_apps"] == []
    assert clean["warnings"] == []

    # 3. SleepHandoffGuard suspend -> resume re-arm + CommandDispatcher.reset_cache()
    bulb = MockBulbDriver()
    dispatcher = CommandDispatcher(light=bulb, display=None, monitor_enabled=False)
    _, day_action, _ = compute_open_loop_action(hour_of_day=12.0)
    assert dispatcher.dispatch(day_action)["bulb_sent"] is True
    assert dispatcher.dispatch(day_action)["bulb_sent"] is False  # Deduplicated

    resumed_calls: list[str] = []

    def _on_resume() -> None:
        dispatcher.reset_cache()
        resumed_calls.append("resumed")

    guard = SleepHandoffGuard(light=bulb, display=None, on_resume=_on_resume)
    guard.update_context(phase="EVENING", unlinked_light_alert=False, last_bulb_power=True)
    assert guard.trigger_handoff("suspend") is True
    assert guard.trigger_handoff("duplicate_suspend") is False  # Idempotent while asleep

    guard._handle_resume()
    assert resumed_calls == ["resumed"]
    assert dispatcher._last_bulb_cmd is None
    assert dispatcher._last_monitor_cmd is None
    # After resume re-arm, dispatcher re-sends command and guard can handoff on next sleep
    assert dispatcher.dispatch(day_action)["bulb_sent"] is True
    assert guard.trigger_handoff("next_suspend") is True


def test_wake_anchored_7day_biological_schedule_and_49pt_curve() -> None:
    """
    Verify wake-anchored 7-day biological schedule resolver produces ZERO brightness jump
    across Fri (07/23) 23:59 -> Sat (10/02) 00:01 and smooth evening continuation across
    Sat 23:59 -> Sun (10/23) 00:01 until 02:00, computes social_jetlag_hours >= 2.0,
    sample_24h_curve(include_end=True) returns 49 points closing at 24.0, and Movie Mode is 3400K.
    """
    weekly = {
        "fri": {"wake_hour": 7.0, "sleep_hour": 23.0},
        "sat": {"wake_hour": 10.0, "sleep_hour": 2.0},
        "sun": {"wake_hour": 10.0, "sleep_hour": 23.0},
    }

    # Fri 23:59 (2026-11-27 is Friday) -> Sat 00:01 (2026-11-28 is Saturday): both in NIGHT (50 nits, 0 jump)
    fri_2359 = datetime(2026, 11, 27, 23, 59)
    sat_0001 = datetime(2026, 11, 28, 0, 1)
    w1, s1, day1, jetlag1 = resolve_biological_schedule(fri_2359, 7.0, 23.0, weekly)
    w2, s2, day2, jetlag2 = resolve_biological_schedule(sat_0001, 7.0, 23.0, weekly)
    t_fri = compute_smooth_open_loop_targets(23.0 + 59 / 60.0, wake_hour=w1, sleep_hour=s1, display_max_nits=497.0)
    t_sat = compute_smooth_open_loop_targets(1 / 60.0, wake_hour=w2, sleep_hour=s2, display_max_nits=497.0)
    assert day1 == "fri" and day2 == "fri"  # Still in Friday's biological cycle until Sat 10:00 wake!
    assert t_fri["phase"] == "NIGHT" and t_sat["phase"] == "NIGHT"
    assert abs(t_fri["target_screen_nits"] - t_sat["target_screen_nits"]) <= 0.1
    assert jetlag1 >= 2.0 and jetlag2 >= 2.0

    # Sat 23:59 (2026-11-28) -> Sun 00:01 (2026-11-29): stays in Saturday's evening until 02:00 sleep
    sat_2359 = datetime(2026, 11, 28, 23, 59)
    sun_0001 = datetime(2026, 11, 29, 0, 1)
    w3, s3, day3, _ = resolve_biological_schedule(sat_2359, 7.0, 23.0, weekly)
    w4, s4, day4, _ = resolve_biological_schedule(sun_0001, 7.0, 23.0, weekly)
    assert day3 == "sat" and day4 == "sat"
    t_sat_late = compute_smooth_open_loop_targets(23.0 + 59 / 60.0, wake_hour=w3, sleep_hour=s3, display_max_nits=497.0)
    t_sun_early = compute_smooth_open_loop_targets(1 / 60.0, wake_hour=w4, sleep_hour=s4, display_max_nits=497.0)
    assert t_sat_late["phase"] == "EVENING" and t_sun_early["phase"] == "EVENING"
    assert abs(t_sat_late["target_screen_nits"] - t_sun_early["target_screen_nits"]) < 5.0

    # 49-point closed 24h curve (0.0 .. 24.0)
    curve49 = sample_24h_curve(wake_hour=10.0, sleep_hour=2.0, display_max_nits=497.0, include_end=True)
    assert len(curve49) == 49
    assert curve49[0]["hour"] == 0.0 and curve49[-1]["hour"] == 24.0

    # Movie Mode soft 3400K target
    mv_pct, mv_cct, mv_nits = compute_movie_mode_target(max_nits=497.0, gamma=2.0)
    assert mv_cct == MOVIE_MODE_CCT_K == 3400
    assert 20 <= mv_pct <= 100 and mv_nits > 0.0


def test_app_watcher_browser_fullscreen_vs_maximized_and_video_players() -> None:
    """
    Verify chrome.exe fullscreen is IGNORED when pause_on_fullscreen_browser=False and
    PAUSED (trigger_kind='fullscreen_browser') when True, vlc.exe/mpv.exe triggers
    'video_player', and normal non-fullscreen chrome.exe is ignored.
    """
    assert "chrome.exe" in BROWSER_APPS
    assert "vlc.exe" in DEDICATED_VIDEO_PLAYERS and "mpv.exe" in DEDICATED_VIDEO_PLAYERS

    cur: dict[str, ForegroundAppInfo | None] = {"info": None}

    # 1. Browser fullscreen OFF by default -> chrome.exe fullscreen is IGNORED
    w_default = ForegroundAppWatcher(
        enabled=True,
        pause_on_fullscreen=True,
        pause_on_fullscreen_browser=False,
        pause_on_video_players=True,
        foreground_provider=lambda: cur["info"],
    )
    cur["info"] = ForegroundAppInfo(exe_name="chrome.exe", window_class="Chrome_WidgetWin_1", is_fullscreen=True)
    assert w_default.check().should_pause is False

    # 2. Browser fullscreen ON -> chrome.exe fullscreen triggers 'fullscreen_browser'
    w_browser = ForegroundAppWatcher(
        enabled=True,
        pause_on_fullscreen=True,
        pause_on_fullscreen_browser=True,
        pause_on_video_players=True,
        foreground_provider=lambda: cur["info"],
    )
    dec_fs = w_browser.check()
    assert dec_fs.should_pause is True
    assert dec_fs.trigger_kind == "fullscreen_browser"

    # 3. Normal non-fullscreen maximized chrome.exe stays IGNORED even with pause_on_fullscreen_browser=True
    cur["info"] = ForegroundAppInfo(exe_name="chrome.exe", window_class="Chrome_WidgetWin_1", is_fullscreen=False)
    assert w_browser.check().should_pause is False

    # 4. Dedicated local video players (vlc.exe, mpv.exe) trigger 'video_player'
    for player_exe in ("vlc.exe", "mpv.exe"):
        cur["info"] = ForegroundAppInfo(exe_name=player_exe, window_class="Qt5QWindowIcon", is_fullscreen=False)
        dec_vp = w_default.check()
        assert dec_vp.should_pause is True
        assert dec_vp.trigger_kind == "video_player"


def test_web_ui_onboarding_movie_mode_no_bulb_and_zero_jump_selects() -> None:
    """
    Verify LocalWebUIServer GET / has 0 external URLs, 0 <input type='time'>, onboardingCard,
    conflictsContainer, POST /api/movie (200/400/403 CSRF), POST /api/onboarding (200/400/403 CSRF),
    and light_backend='none' snapshot state.
    """
    saved_events: list[dict[str, Any]] = []
    state = OrchestratorState(
        mode="open_loop",
        light_backend="none",
        onboarding_completed=False,
        live_conflicts={
            "night_light_on": True,
            "hdr_active": False,
            "acm_active": False,
            "competing_apps": [],
            "warnings": ["Windows Night Light is ON — turn it off to avoid double-warming."],
        },
        on_settings_saved=lambda cfg: saved_events.append(dict(cfg)),
    )
    state.update_snapshot(schedule_curve=sample_24h_curve(display_max_nits=496.7, include_end=True))

    def _req(
        port: int,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            hdrs = {"Content-Type": "application/json"} if body is not None else {}
            if headers:
                hdrs.update(headers)
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    with LocalWebUIServer(state, host="127.0.0.1", port=0) as srv:
        port = srv.actual_port

        # 1. GET / checks
        st_root, html_root = _req(port, "GET", "/")
        assert st_root == 200
        assert "http://" not in html_root and "https://" not in html_root
        assert 'type="time"' not in html_root
        assert "onboardingCard" in html_root
        assert "conflictsContainer" in html_root
        assert "No Bulb Linked" in html_root

        # 2. GET /api/state exposes light_backend='none', onboarding_completed=False, live_conflicts, 49-pt curve
        st_api, body_api = _req(port, "GET", "/api/state")
        assert st_api == 200
        snap = json.loads(body_api)
        assert snap["light_backend"] == "none"
        assert snap["onboarding_completed"] is False
        assert len(snap["live_conflicts"]["warnings"]) == 1
        assert len(snap["curve_24h"]) == 49

        # 3. POST /api/movie: enable (200), disable (200), invalid type (400), CSRF (403)
        st_mv1, body_mv1 = _req(port, "POST", "/api/movie", {"enabled": True})
        assert st_mv1 == 200 and json.loads(body_mv1)["movie_mode_active"] is True
        assert state.is_movie_mode_active() is True

        st_mv0, body_mv0 = _req(port, "POST", "/api/movie", {"enabled": False})
        assert st_mv0 == 200 and json.loads(body_mv0)["movie_mode_active"] is False

        st_mv_bad, _ = _req(port, "POST", "/api/movie", {"enabled": "not-a-bool"})
        assert st_mv_bad == 400

        st_mv_csrf, _ = _req(
            port, "POST", "/api/movie", {"enabled": True}, headers={"Origin": "https://evil.example"}
        )
        assert st_mv_csrf == 403

        # 4. POST /api/onboarding: complete onboarding + weekly_schedule (200), bad range (400), CSRF (403)
        st_onb, body_onb = _req(
            port,
            "POST",
            "/api/onboarding",
            {
                "wake_hour": 8.0,
                "sleep_hour": 23.5,
                "onboarding_completed": True,
                "weekly_schedule": {"sat": {"wake_hour": 10.0, "sleep_hour": 1.5}},
                "auto_pause_browser_fullscreen": True,
                "start_with_windows": False,
            },
        )
        assert st_onb == 200
        onb_data = json.loads(body_onb)
        assert onb_data["onboarding_completed"] is True
        assert onb_data["auto_pause_browser_fullscreen"] is True
        assert saved_events[-1]["onboarding_completed"] is True

        st_onb_bad, _ = _req(port, "POST", "/api/onboarding", {"wake_hour": 99.0})
        assert st_onb_bad == 400

        st_onb_csrf, _ = _req(
            port, "POST", "/api/onboarding", {"onboarding_completed": True}, headers={"Host": "evil.example"}
        )
        assert st_onb_csrf == 403


def test_tray_hotkeys_movie_mode_and_browser_menu() -> None:
    """
    Verify HOTKEY_ID_TOGGLE_PAUSE, HOTKEY_ID_TOGGLE_MOVIE, TrayStatusSnapshot,
    TrayCallbacks movie/browser toggles, and 'Quit && Restore Display' escaping.
    """
    assert HOTKEY_ID_TOGGLE_PAUSE == 1
    assert HOTKEY_ID_TOGGLE_MOVIE == 2

    snap = TrayStatusSnapshot(
        paused=False,
        movie_mode_active=True,
        auto_pause_enabled=True,
        auto_pause_browser_fullscreen=True,
        high_sensitivity_evening=False,
    )
    assert snap.movie_mode_active is True and snap.auto_pause_browser_fullscreen is True

    state = OrchestratorState(mode="open_loop", auto_pause_browser_fullscreen=False)
    stop_event = threading.Event()
    called: list[str] = []
    callbacks = TrayCallbacks(
        on_toggle_movie_mode=lambda: (called.append("movie"), state.toggle_movie_mode()),
        on_toggle_browser_fullscreen=lambda: (
            called.append("browser_fs"),
            state.update_settings(auto_pause_browser_fullscreen=not state.to_dict()["auto_pause_browser_fullscreen"]),
        ),
    )
    ctrl = TrayActionController(
        state=state,
        stop_event=stop_event,
        callbacks=callbacks,
        status_provider=lambda: TrayStatusSnapshot(
            paused=state.is_paused()[0],
            movie_mode_active=state.is_movie_mode_active(),
            auto_pause_browser_fullscreen=bool(state.to_dict()["auto_pause_browser_fullscreen"]),
        ),
    )

    assert ctrl.is_movie_mode_active() is False
    ctrl.toggle_movie_mode()
    assert "movie" in called and ctrl.is_movie_mode_active() is True

    assert ctrl.get_browser_fullscreen_auto_pause() is False
    ctrl.toggle_browser_fullscreen_auto_pause()
    assert "browser_fs" in called and ctrl.get_browser_fullscreen_auto_pause() is True

    tray_src = (PROTOTYPE_ROOT / "orchestrator" / "tray_win32.py").read_text(encoding="utf-8")
    assert "Quit && Restore Display" in tray_src


def test_config_weekly_schedule_onboarding_and_new_fields_roundtrip(tmp_path: Path) -> None:
    """
    Verify legacy config.json loads with safe defaults (onboarding_completed=False,
    weekly_schedule={}, auto_pause_browser_fullscreen=False, start_with_windows=False)
    and new Wave 1/2 fields roundtrip atomically in a temp directory without touching HKCU\\Run.
    """
    legacy_path = tmp_path / "legacy_v61.json"
    legacy_path.write_text('{"wake_hour": 7.0, "sleep_hour": 23.0, "light": "none"}', encoding="utf-8")
    legacy = load_config(legacy_path)
    assert legacy.onboarding_completed is False
    assert legacy.weekly_schedule == {}
    assert legacy.auto_pause_browser_fullscreen is False
    assert legacy.start_with_windows is False

    full_path = tmp_path / "full_wave2_config.json"
    full_cfg = UserConfig(
        wake_hour=7.5,
        sleep_hour=23.0,
        onboarding_completed=True,
        weekly_schedule={
            "sat": {"wake_hour": 10.0, "sleep_hour": 2.0},
            "sun": {"wake_hour": 10.0, "sleep_hour": 23.0},
        },
        auto_pause_browser_fullscreen=True,
        start_with_windows=True,
    )
    save_config(full_cfg, full_path)
    reloaded = load_config(full_path)
    assert reloaded.onboarding_completed is True
    assert reloaded.weekly_schedule == {
        "sat": {"wake_hour": 10.0, "sleep_hour": 2.0},
        "sun": {"wake_hour": 10.0, "sleep_hour": 23.0},
    }
    assert reloaded.auto_pause_browser_fullscreen is True
    assert reloaded.start_with_windows is True


def test_continuous_afternoon_warmdown_no_flat_plateau() -> None:
    """
    Verify continuous solar/PRC afternoon warm-down (zero flat 11.5h daytime CCT plateau):
    - On 10:00 -> 02:00, 18:19 is strictly warmer & dimmer than 15:00 peak and 18:20 differs
      from 18:19 (minute-by-minute glide).
    - On 07:00 -> 23:00, 18:15 is strictly warmer & dimmer than 12:00 peak (~164 nits @ ~4400K).
    - GET / renders Kelvin-colored gradient (curveCctGrad), 0 external URLs, and 0 type='time'.
    """
    # 1. Late-chronotype 10:00 -> 02:00 (497-nit panel)
    t_1500 = compute_smooth_open_loop_targets(15.0, wake_hour=10.0, sleep_hour=2.0, display_max_nits=497.0)
    t_1819 = compute_smooth_open_loop_targets(18.0 + 19 / 60.0, wake_hour=10.0, sleep_hour=2.0, display_max_nits=497.0)
    t_1820 = compute_smooth_open_loop_targets(18.0 + 20 / 60.0, wake_hour=10.0, sleep_hour=2.0, display_max_nits=497.0)
    assert t_1500["target_screen_nits"] == 220.0 and t_1500["monitor_cct_k"] == 5800
    assert t_1819["transition_label"] == "afternoon"
    assert t_1819["monitor_cct_k"] < t_1500["monitor_cct_k"]
    assert t_1819["target_screen_nits"] < t_1500["target_screen_nits"]
    assert (t_1820["target_screen_nits"], t_1820["monitor_cct_k"]) != (
        t_1819["target_screen_nits"],
        t_1819["monitor_cct_k"],
    )

    # 2. Standard 07:00 -> 23:00: 18:15 strictly warmer & dimmer than 12:00 peak (~164 nits @ ~4400K)
    s_1200 = compute_smooth_open_loop_targets(12.0, wake_hour=7.0, sleep_hour=23.0, display_max_nits=497.0)
    s_1815 = compute_smooth_open_loop_targets(18.25, wake_hour=7.0, sleep_hour=23.0, display_max_nits=497.0)
    assert s_1200["target_screen_nits"] == 220.0 and s_1200["monitor_cct_k"] == 5800
    assert s_1815["monitor_cct_k"] < s_1200["monitor_cct_k"]
    assert s_1815["target_screen_nits"] < s_1200["target_screen_nits"]
    assert 155.0 <= s_1815["target_screen_nits"] <= 175.0
    assert 4250 <= s_1815["monitor_cct_k"] <= 4550

    # 3. GET / renders Kelvin-colored gradient stroke (curveCctGrad), 0 external URLs, 0 type='time'
    state = OrchestratorState(mode="open_loop")
    with LocalWebUIServer(state, host="127.0.0.1", port=0) as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.actual_port, timeout=5)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            html_out = resp.read().decode("utf-8")
            assert resp.status == 200
            assert "curveCctGrad" in html_out
            assert "http://" not in html_out and "https://" not in html_out
            assert 'type="time"' not in html_out
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 13. Full Braun UX Overhaul: Mandatory AWAITING_SETUP Gate & Zero-Chevron Bento
# ---------------------------------------------------------------------------


def test_mandatory_onboarding_gate_holds_display_until_activated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify daemon.main(..., enforce_onboarding_gate=True, on_state_ready=...) with mocked
    display/bulb and onboarding_completed=False holds in AWAITING_SETUP with ZERO display or
    bulb writes before onboarding, then immediately activates & atomically persists config.json
    upon POST /api/onboarding.
    """
    import orchestrator.daemon as daemon_mod

    cfg_path = tmp_path / "onboarding_gate_config.json"
    save_config(
        UserConfig(
            wake_hour=7.0,
            sleep_hour=23.0,
            mode="open_loop",
            light="mock",
            monitor_enabled=True,
            onboarding_completed=False,
        ),
        cfg_path,
    )

    class RecordingDisplay:
        def __init__(self, enabled: bool = False, **_kwargs: Any) -> None:
            self.enabled = enabled
            self.apply_calls: list[tuple[int, int]] = []
            self.restore_calls: int = 0
            self.wmi_supported = True
            self.ddc_supported = False
            self.gamma_supported = False
            self.mag_supported = True
            self.ddc_diagnostic = "WMI laptop backlight OK"

        def get_capabilities(self) -> dict[str, Any]:
            return {
                "is_windows": True,
                "wmi_laptop_available": True,
                "ddc_external_supported": False,
                "gdi_gamma_supported": False,
                "magnification_available": True,
                "windows_night_light_detected": False,
                "display_profile": {
                    "max_nits": 496.7,
                    "min_nits": 0.4,
                    "model_name": "NE160QDM-NZL",
                    "source": "edid_cta861",
                    "is_auto_detected": True,
                },
            }

        def apply(self, brightness_pct: int, cct_k: int) -> dict[str, Any]:
            self.apply_calls.append((int(brightness_pct), int(cct_k)))
            return {
                "applied": True,
                "wmi_ok": True,
                "ddc_ok": False,
                "gamma_ok": True,
                "color_method": "magnification",
            }

        def restore(self) -> bool:
            self.restore_calls += 1
            return True

    holder: dict[str, Any] = {
        "display": None,
        "bulb": None,
        "pre_onboard_display_writes": None,
        "pre_onboard_bulb_writes": None,
        "pre_onboard_awaiting": None,
        "post_status": None,
        "post_awaiting": None,
    }

    def _fake_display_ctor(*args: Any, **kwargs: Any) -> RecordingDisplay:
        inst = RecordingDisplay(*args, **kwargs)
        holder["display"] = inst
        return inst

    def _fake_light_builder(args: Any) -> MockBulbDriver:
        drv = MockBulbDriver()
        holder["bulb"] = drv
        return drv

    monkeypatch.setattr(daemon_mod, "WindowsDisplayController", _fake_display_ctor)
    monkeypatch.setattr(daemon_mod, "build_light_driver", _fake_light_builder)
    monkeypatch.setattr(
        daemon_mod,
        "detect_display_luminance_profile",
        lambda **_kw: {
            "max_nits": 496.7,
            "min_nits": 0.4,
            "model_name": "NE160QDM-NZL",
            "source": "edid_cta861",
            "is_auto_detected": True,
        },
    )
    monkeypatch.setattr(
        daemon_mod,
        "detect_live_display_conflicts",
        lambda **_kw: {
            "night_light_on": False,
            "hdr_active": False,
            "acm_active": False,
            "competing_apps": [],
            "warnings": [],
        },
    )

    worker_threads: list[threading.Thread] = []

    def _on_state_ready(ui_state: OrchestratorState, web_server: LocalWebUIServer | None) -> None:
        assert web_server is not None

        def _drive_onboarding() -> None:
            # Wait briefly so Tick #1 executes under AWAITING_SETUP
            time.sleep(0.08)
            snap_before = ui_state.to_dict()
            holder["pre_onboard_awaiting"] = snap_before["awaiting_setup"]
            holder["pre_onboard_display_writes"] = len(holder["display"].apply_calls)
            holder["pre_onboard_bulb_writes"] = len(holder["bulb"].history)

            conn = http.client.HTTPConnection("127.0.0.1", web_server.actual_port, timeout=5)
            try:
                body = json.dumps(
                    {
                        "wake_hour": 10.0,
                        "sleep_hour": 2.0,
                        "onboarding_completed": True,
                        "weekly_schedule": {
                            "mon": {"wake_hour": 10.0, "sleep_hour": 2.0},
                            "sat": {"wake_hour": 11.0, "sleep_hour": 3.0},
                        },
                    }
                ).encode("utf-8")
                conn.request("POST", "/api/onboarding", body=body, headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                holder["post_status"] = resp.status
                resp.read()
            finally:
                conn.close()

            # Wait for Tick #2 to process activation
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and ui_state.to_dict()["awaiting_setup"]:
                time.sleep(0.02)
            holder["post_awaiting"] = ui_state.to_dict()["awaiting_setup"]

        t = threading.Thread(target=_drive_onboarding, daemon=True)
        worker_threads.append(t)
        t.start()

    rc = daemon_main(
        [
            "--config",
            str(cfg_path),
            "--open-loop",
            "--hour",
            "15.0",
            "--web-ui",
            "--ui-port",
            "0",
            "--no-tray",
            "--no-auto-pause",
            "--ticks",
            "2",
            "--interval",
            "0.25",
        ],
        enforce_onboarding_gate=True,
        on_state_ready=_on_state_ready,
    )
    for t in worker_threads:
        t.join(timeout=2.0)

    assert rc == 0
    assert holder["pre_onboard_awaiting"] is True
    assert holder["pre_onboard_display_writes"] == 0, "Display MUST NOT be touched before onboarding completes!"
    assert holder["pre_onboard_bulb_writes"] == 0, "Bulb MUST NOT be touched before onboarding completes!"
    assert holder["post_status"] == 200
    assert holder["post_awaiting"] is False
    assert len(holder["display"].apply_calls) >= 1, "Display must activate immediately after onboarding!"
    assert len(holder["bulb"].history) >= 1, "Bulb must activate immediately after onboarding!"

    persisted = load_config(cfg_path)
    assert persisted.onboarding_completed is True
    assert persisted.wake_hour == 10.0
    assert persisted.sleep_hour == 2.0
    assert persisted.weekly_schedule.get("sat") == {"wake_hour": 11.0, "sleep_hour": 3.0}


def test_web_ui_bento_zero_chevron_time_popover_and_setup_wizard() -> None:
    """
    Verify GET / renders the widescreen 1320px Braun Bento cockpit with 0 external URLs,
    0 <input type='time'>, 0 <select> dropdowns, reusable data-time-picker pills + #timePopover,
    #onboardingGate, #dashboardRoot, openWizardBtn, curveCctGrad, valid inline JS (node --check),
    and GET /api/state exposes awaiting_setup + target_display_nits.
    """
    import re
    import tempfile

    state = OrchestratorState(
        mode="open_loop",
        light_backend="none",
        onboarding_completed=False,
        awaiting_setup=True,
        detected_max_nits=496.7,
        detected_panel_model="NE160QDM-NZL",
        detected_nits_source="edid_cta861",
    )
    state.update_snapshot(
        phase="DAY",
        target_screen_nits=210.5,
        monitor_brightness_pct=62,
        monitor_cct_k=5600,
        awaiting_setup=True,
        schedule_curve=sample_24h_curve(display_max_nits=496.7, include_end=True),
    )

    with LocalWebUIServer(state, host="127.0.0.1", port=0) as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.actual_port, timeout=5)
        try:
            conn.request("GET", "/")
            resp_html = conn.getresponse()
            html_out = resp_html.read().decode("utf-8")
            assert resp_html.status == 200
            assert "http://" not in html_out and "https://" not in html_out
            assert 'type="time"' not in html_out
            assert "<select" not in html_out.lower() and 'createElement("select")' not in html_out
            for tok in (
                "data-time-picker",
                "timePopover",
                "onboardingGate",
                "dashboardRoot",
                "openWizardBtn",
                "curveCctGrad",
                "1320px",
            ):
                assert tok in html_out, f"Missing expected Bento UI token: {tok}"

            # Verify inline JS passes node --check with 0 syntax errors
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_out, re.S | re.I)
            assert len(scripts) >= 1
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
                tf.write("\n".join(scripts))
                tmp_js = Path(tf.name)
            try:
                chk = subprocess.run(["node", "--check", str(tmp_js)], capture_output=True, text=True, timeout=15)
                assert chk.returncode == 0, f"Inline JS syntax error:\n{chk.stderr}"
            finally:
                tmp_js.unlink(missing_ok=True)

            conn.request("GET", "/api/state")
            resp_api = conn.getresponse()
            snap = json.loads(resp_api.read().decode("utf-8"))
            assert resp_api.status == 200
            assert snap["awaiting_setup"] is True
            assert snap["target_display_nits"] == 210.5
            assert len(snap["curve_24h"]) == 49
        finally:
            conn.close()


# =============================================================================
# Wave 1–4 Comprehensive Regression & Certification Suite (4-Report v1.0.0)
# =============================================================================


def test_wave1_mired_interpolation_von_kries_and_gentle_ramp() -> None:
    """
    Wave 1 (Report #1): Verify MIRED-space perceptual interpolation (`1e6 / T`),
    von Kries kinetics rate limiting (`clamp_mired_step`), and gentle cosine
    log-luminance + MIRED first-evening activation glide (`compute_first_evening_ramp_step`).
    """
    from spectral.photobiology import (
        clamp_mired_step,
        compute_first_evening_ramp_step,
        kelvin_to_mired,
        lerp_cct_mired,
        mired_to_kelvin,
    )

    assert abs(kelvin_to_mired(6500.0) - (1_000_000.0 / 6500.0)) < 1e-6
    assert abs(mired_to_kelvin(kelvin_to_mired(3200.0)) - 3200.0) < 1e-6

    # Midpoint in MIRED space between 6500K & 3000K is harmonic mean (~4105K), NOT arithmetic 4750K
    mid_cct = lerp_cct_mired(6500.0, 3000.0, 0.5)
    assert 4095.0 <= mid_cct <= 4115.0
    assert abs(lerp_cct_mired(6500.0, 3000.0, 0.0) - 6500.0) < 1e-6
    assert abs(lerp_cct_mired(6500.0, 3000.0, 1.0) - 3000.0) < 1e-6

    # von Kries rate limiter caps 60s transition to <= 4.5 mired/min
    stepped = clamp_mired_step(6500.0, 2700.0, dt_s=60.0, max_mired_per_min=4.5)
    mired_delta = abs(kelvin_to_mired(stepped) - kelvin_to_mired(6500.0))
    assert abs(mired_delta - 4.5) < 0.05
    assert stepped < 6500.0 and stepped > 6200.0
    # Already close target snaps directly without overshoot
    assert clamp_mired_step(3010.0, 3000.0, dt_s=60.0, max_mired_per_min=4.5) == 3000.0

    # First-Evening Gentle Ramp (8s cosine log-luminance + MIRED)
    n0, c0 = compute_first_evening_ramp_step(0.0, 8.0, 160.0, 55.0, 6500.0, 3000.0)
    n_mid, c_mid = compute_first_evening_ramp_step(4.0, 8.0, 160.0, 55.0, 6500.0, 3000.0)
    n_end, c_end = compute_first_evening_ramp_step(8.0, 8.0, 160.0, 55.0, 6500.0, 3000.0)
    assert (round(n0, 1), c0) == (160.0, 6500)
    assert (round(n_end, 1), c_end) == (55.0, 3000)
    assert 55.0 < n_mid < 160.0
    # Log-luminance geometric mean sqrt(160 * 55) ~= 93.8 nits (darker than arithmetic 107.5)
    assert 90.0 <= n_mid <= 98.0
    assert 4095 <= c_mid <= 4115


def test_wave1_panel_m_der_y_lut_compensation_and_oled_floor() -> None:
    """
    Wave 1 (Report #1): Verify panel spectrum profiles (W-LED / P3 / WOLED / QD-OLED),
    QD-OLED +44-66% evening m-DER surge, Y_lut double-dimming compensation, OLED >=42 nit
    hardware floor, and panel-aware evening CCT offsets (-350K QD-OLED, -150K P3).
    """
    from spectral.photobiology import (
        OLED_SAFE_MIN_NITS,
        PANEL_TYPES,
        estimate_lut_luminance_retention,
        estimate_panel_m_der,
        normalize_panel_type,
    )

    assert PANEL_TYPES == ("wled_ips", "p3_ips", "woled", "qd_oled")
    assert OLED_SAFE_MIN_NITS == 42.0
    assert normalize_panel_type("auto") == "wled_ips"
    assert normalize_panel_type(None) == "wled_ips"
    assert normalize_panel_type("qd_ips") == "qd_oled"
    assert normalize_panel_type("ksf_ips") == "p3_ips"
    assert normalize_panel_type("dci_p3") == "p3_ips"

    # QD-OLED evening m-DER surge vs W-LED IPS (+44.4% @ 2700K, +66.5% @ 2200K)
    ratio_2700 = estimate_panel_m_der(2700.0, "qd_oled") / estimate_panel_m_der(2700.0, "wled_ips")
    ratio_2200 = estimate_panel_m_der(2200.0, "qd_oled") / estimate_panel_m_der(2200.0, "wled_ips")
    assert 1.40 <= ratio_2700 <= 1.50
    assert 1.62 <= ratio_2200 <= 1.72

    # Y_lut retention anchors
    assert estimate_lut_luminance_retention(6500.0, "wled_ips") == 1.0
    assert abs(estimate_lut_luminance_retention(2700.0, "wled_ips") - 0.432) < 0.01
    assert abs(estimate_lut_luminance_retention(2700.0, "qd_oled") - 0.654) < 0.01

    # Y_lut compensation boosts backlight % at warm CCT to eliminate double-dimming
    pct_raw = nits_to_backlight_pct(50.0, max_nits=300.0, gamma=2.0, cct_k=3000.0, compensate_lut=False)
    pct_comp = nits_to_backlight_pct(
        50.0, max_nits=300.0, gamma=2.0, cct_k=3000.0, panel_type="wled_ips", compensate_lut=True
    )
    assert pct_comp > pct_raw

    # OLED hardware safe floor (>= 42 nits) prevents near-black crush / deep PWM
    pct_lcd_low = nits_to_backlight_pct(
        20.0, max_nits=300.0, gamma=2.0, cct_k=6500.0, panel_type="wled_ips", compensate_lut=False
    )
    pct_woled_low = nits_to_backlight_pct(
        20.0, max_nits=300.0, gamma=2.0, cct_k=6500.0, panel_type="woled", compensate_lut=False
    )
    pct_woled_42 = nits_to_backlight_pct(
        OLED_SAFE_MIN_NITS, max_nits=300.0, gamma=2.0, cct_k=6500.0, panel_type="woled", compensate_lut=False
    )
    assert pct_woled_low == pct_woled_42 > pct_lcd_low

    # Panel-specific evening CCT offsets (-350K QD-OLED, -150K P3) & Dark Mode +150K cap
    wled_night = compute_smooth_open_loop_targets(
        2.0, wake_hour=7.0, sleep_hour=23.0, panel_type="wled_ips", dark_mode=False
    )
    p3_night = compute_smooth_open_loop_targets(
        2.0, wake_hour=7.0, sleep_hour=23.0, panel_type="p3_ips", dark_mode=False
    )
    qd_night = compute_smooth_open_loop_targets(
        2.0, wake_hour=7.0, sleep_hour=23.0, panel_type="qd_oled", dark_mode=False
    )
    wled_dark = compute_smooth_open_loop_targets(
        2.0, wake_hour=7.0, sleep_hour=23.0, panel_type="wled_ips", dark_mode=True
    )
    assert wled_night["monitor_cct_k"] == 3000
    assert p3_night["monitor_cct_k"] == 2850
    assert qd_night["monitor_cct_k"] == 2650
    assert wled_dark["monitor_cct_k"] == 3150


def test_wave1_edid_solid_angle_and_bounded_dark_mode_corneal_m_edi() -> None:
    """
    Wave 1 (Report #1): Verify analytical rectangular projected solid angle (Omega_proj)
    and bounded Dark/Light Mode corneal m-EDI (APL_eff 0.35 vs 0.75 vs 0.50 unknown).
    """
    from spectral.photobiology import (
        compute_projected_solid_angle_sr,
        estimate_corneal_m_edi_lx,
    )

    omega_16 = compute_projected_solid_angle_sr(345.0, 215.0, viewing_dist_mm=550.0)
    omega_27 = compute_projected_solid_angle_sr(597.0, 336.0, viewing_dist_mm=600.0)
    assert abs(omega_16 - 0.2250) < 0.002
    assert abs(omega_27 - 0.4604) < 0.004
    assert compute_projected_solid_angle_sr(0.0, 215.0, 550.0) == 0.35
    assert compute_projected_solid_angle_sr(345.0, -10.0, 550.0) == 0.35

    m_dark = estimate_corneal_m_edi_lx(50.0, 3000.0, panel_type="wled_ips", solid_angle_sr=omega_16, dark_mode=True)
    m_unk = estimate_corneal_m_edi_lx(50.0, 3000.0, panel_type="wled_ips", solid_angle_sr=omega_16, dark_mode=None)
    m_light = estimate_corneal_m_edi_lx(50.0, 3000.0, panel_type="wled_ips", solid_angle_sr=omega_16, dark_mode=False)
    assert 0.5 < m_dark < m_unk < m_light < 10.0
    assert abs((m_light / m_dark) - (0.75 / 0.35)) < 0.02


def test_wave2_win11_advanced_color_info_2_enum_0_1_2_and_type9_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Wave 2 (Guardrail G5 / Report #2): Verify `_probe_win32_hdr_and_acm()` queries
    `GET_ADVANCED_COLOR_INFO_2 (type=15, size=36)` FIRST with exact 0-based enum
    (`0=SDR`, `1=WCG/ACM`, `2=HDR`) and falls back to `type=9` bit `0x2` only if `type=15` fails.
    """
    import ctypes
    import orchestrator.display.windows_display as wd_mod

    assert wd_mod.DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2 == 15
    assert wd_mod.DISPLAYCONFIG_ADVANCED_COLOR_MODE_SDR == 0
    assert wd_mod.DISPLAYCONFIG_ADVANCED_COLOR_MODE_WCG == 1
    assert wd_mod.DISPLAYCONFIG_ADVANCED_COLOR_MODE_HDR == 2

    def _run_probe_scenario(
        *,
        type15_rc: int,
        active_color_mode: int,
        type9_value: int = 0,
    ) -> tuple[tuple[bool, bool], list[tuple[int, int]]]:
        seen_calls: list[tuple[int, int]] = []

        class FakeUser32:
            def GetDisplayConfigBufferSizes(self, _flags: int, num_paths: Any, num_modes: Any) -> int:
                num_paths._obj.value = 1
                num_modes._obj.value = 1
                return 0

            def QueryDisplayConfig(
                self, _flags: int, _np: Any, _paths: Any, _nm: Any, _modes: Any, _topo: Any
            ) -> int:
                return 0

            def DisplayConfigGetDeviceInfo(self, info_ptr: Any) -> int:
                struct_obj = info_ptr._obj
                req_type = int(struct_obj.header.type)
                req_size = int(struct_obj.header.size)
                seen_calls.append((req_type, req_size))
                if req_type == 15:
                    if type15_rc != 0:
                        return type15_rc
                    struct_obj.activeColorMode = active_color_mode
                    return 0
                if req_type == 9:
                    struct_obj.value = type9_value
                    return 0
                return 87

        monkeypatch.setattr(wd_mod.sys, "platform", "win32")
        monkeypatch.setattr(ctypes.windll, "user32", FakeUser32())
        res = wd_mod._probe_win32_hdr_and_acm()
        return res, seen_calls

    # 0 = SDR -> (False, False)
    res_sdr, calls_sdr = _run_probe_scenario(type15_rc=0, active_color_mode=0)
    assert res_sdr == (False, False)
    assert calls_sdr == [(15, 36)]

    # 1 = WCG (SDR ACM) -> (False, True)
    res_acm, calls_acm = _run_probe_scenario(type15_rc=0, active_color_mode=1)
    assert res_acm == (False, True)
    assert calls_acm == [(15, 36)]

    # 2 = HDR -> (True, False)
    res_hdr, calls_hdr = _run_probe_scenario(type15_rc=0, active_color_mode=2)
    assert res_hdr == (True, False)
    assert calls_hdr == [(15, 36)]

    # Pre-24H2 fallback when type=15 fails (rc=87) -> falls back to type=9 bit 0x2
    res_fb, calls_fb = _run_probe_scenario(type15_rc=87, active_color_mode=0, type9_value=0x2)
    assert res_fb == (True, False)
    assert calls_fb[0] == (15, 36) and calls_fb[1][0] == 9


def test_wave2_hdr_auto_pause_restores_and_auto_resumes_in_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Wave 2 (Report #2 & #3): Verify automatic HDR Auto-Pause in `daemon.main`:
    - When `hdr_active` flips True after an active tick, daemon calls `display.restore()`
      and suppresses further display/bulb writes with `hdr_auto_paused=True`.
    - When `hdr_active` flips back to False, daemon automatically resumes writing targets.
    """
    import orchestrator.daemon as daemon_mod

    cfg_path = tmp_path / "hdr_autopause_cfg.json"
    save_config(
        UserConfig(
            wake_hour=7.0,
            sleep_hour=23.0,
            mode="open_loop",
            light="mock",
            monitor_enabled=True,
            onboarding_completed=True,
        ),
        cfg_path,
    )

    class MockHDRDisplay:
        def __init__(self, enabled: bool = False, **_kwargs: Any) -> None:
            self.enabled = enabled
            self.apply_calls: list[tuple[int, int]] = []
            self.restore_calls: int = 0
            self.wmi_supported = True
            self.ddc_supported = False
            self.gamma_supported = False
            self.mag_supported = True
            self.ddc_diagnostic = "WMI laptop backlight OK"

        def get_capabilities(self) -> dict[str, Any]:
            return {
                "is_windows": True,
                "wmi_laptop_available": True,
                "ddc_external_supported": False,
                "gdi_gamma_supported": False,
                "magnification_available": True,
                "windows_night_light_detected": False,
                "display_profile": {
                    "max_nits": 496.7,
                    "min_nits": 0.4,
                    "model_name": "NE160QDM-NZL",
                    "source": "edid_cta861",
                    "is_auto_detected": True,
                    "solid_angle_sr": 0.225,
                    "auto_panel_type": "wled_ips",
                },
            }

        def apply(self, brightness_pct: int, cct_k: int) -> dict[str, Any]:
            self.apply_calls.append((int(brightness_pct), int(cct_k)))
            return {"applied": True, "wmi_ok": True, "ddc_ok": False, "gamma_ok": True, "color_method": "magnification"}

        def restore(self) -> bool:
            self.restore_calls += 1
            return True

    holder: dict[str, Any] = {"display": None, "hdr_poll_idx": 0}
    # Poll sequence: initial=False -> Tick1=False (applies) -> Tick2=True (HDR auto-pauses & restores!) -> Tick3=False (resumes!)
    hdr_sequence = [False, False, True, False]

    def _fake_conflicts(**_kw: Any) -> dict[str, Any]:
        idx = min(holder["hdr_poll_idx"], len(hdr_sequence) - 1)
        holder["hdr_poll_idx"] += 1
        is_hdr = hdr_sequence[idx]
        return {
            "night_light_on": False,
            "color_filter_on": False,
            "hdr_active": is_hdr,
            "acm_active": False,
            "competing_apps": [],
            "warnings": ["Windows HDR is ON"] if is_hdr else [],
        }

    # Advance monotonic clock by +5s per call so _poll_live_conflicts() polls every tick
    fake_clock = [100.0]

    def _fast_mono() -> float:
        fake_clock[0] += 5.0
        return fake_clock[0]

    def _ctor(*args: Any, **kwargs: Any) -> MockHDRDisplay:
        inst = MockHDRDisplay(*args, **kwargs)
        holder["display"] = inst
        return inst

    monkeypatch.setattr(daemon_mod, "WindowsDisplayController", _ctor)
    monkeypatch.setattr(daemon_mod, "build_light_driver", lambda _a: MockBulbDriver())
    monkeypatch.setattr(daemon_mod, "detect_live_display_conflicts", _fake_conflicts)
    monkeypatch.setattr(daemon_mod, "detect_windows_dark_mode", lambda: True)
    monkeypatch.setattr(daemon_mod.time, "monotonic", _fast_mono)

    snapshots: list[dict[str, Any]] = []

    def _capture_state(ui_state: OrchestratorState, _srv: LocalWebUIServer | None) -> None:
        orig_update = ui_state.update_snapshot

        def _wrapped(*args: Any, **kwargs: Any) -> None:
            orig_update(*args, **kwargs)
            snapshots.append(ui_state.to_dict())

        monkeypatch.setattr(ui_state, "update_snapshot", _wrapped)

    rc = daemon_main(
        [
            "--config",
            str(cfg_path),
            "--open-loop",
            "--hour",
            "21.5",
            "--web-ui",
            "--ui-port",
            "0",
            "--no-tray",
            "--no-auto-pause",
            "--ticks",
            "3",
            "--interval",
            "0",
        ],
        enforce_onboarding_gate=False,
        on_state_ready=_capture_state,
    )
    assert rc == 0
    assert len(snapshots) == 3
    # Tick 1 (SDR): active write
    assert snapshots[0]["hdr_auto_paused"] is False
    # Tick 2 (HDR ON): auto-paused with Windows HDR reason & restore triggered
    assert snapshots[1]["hdr_auto_paused"] is True
    assert snapshots[1]["auto_paused"] is True
    assert "HDR" in snapshots[1]["auto_pause_reason"]
    # Tick 3 (HDR OFF): automatically resumed & re-applied
    assert snapshots[2]["hdr_auto_paused"] is False
    assert snapshots[2]["auto_paused"] is False
    assert len(holder["display"].apply_calls) == 2
    assert holder["display"].restore_calls >= 1


def test_wave2_color_filters_dark_mode_and_edid_physical_gamut_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Wave 2 (Report #1 & #2): Verify `detect_windows_color_filters_enabled()`,
    `detect_windows_dark_mode()`, `detect_live_display_conflicts(color_filter_probe=...)`,
    and `parse_edid_physical_and_gamut()` (DTD #1 mm, cm fallback, CIE 1931 P3 gamut, OLED detection).
    """
    import orchestrator.display.windows_display as wd_mod

    conflicts = wd_mod.detect_live_display_conflicts(
        night_light_probe=lambda: False,
        hdr_acm_probe=lambda: (False, False),
        color_filter_probe=lambda: True,
        process_probe=lambda: [],
    )
    assert conflicts["color_filter_on"] is True
    assert any("Color Filter" in w for w in conflicts["warnings"])

    # Mock winreg read-only for detect_windows_color_filters_enabled & detect_windows_dark_mode
    import winreg

    class FakeRegKey:
        def __init__(self, val: int) -> None:
            self.val = val

        def __enter__(self) -> "FakeRegKey":
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

    reg_values = {
        r"Software\Microsoft\ColorFiltering": 1,
        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize": 0,
    }

    def _fake_open_key(_root: Any, subkey: str, _res: int = 0, _acc: int = 0) -> FakeRegKey:
        if subkey in reg_values:
            return FakeRegKey(reg_values[subkey])
        raise OSError("not found")

    monkeypatch.setattr(winreg, "OpenKey", _fake_open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, _name: (key.val, winreg.REG_DWORD))
    assert wd_mod.detect_windows_color_filters_enabled() is True
    assert wd_mod.detect_windows_dark_mode() is True
    reg_values[r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"] = 1
    assert wd_mod.detect_windows_dark_mode() is False

    # Construct synthetic 128-byte EDID base block with exact DTD #1 + CIE 1931 chromaticities
    def _build_edid(
        w_mm: int,
        h_mm: int,
        rx: float,
        ry: float,
        gx: float,
        gy: float,
        bx: float,
        by: float,
        *,
        use_dtd: bool = True,
    ) -> bytes:
        buf = bytearray(128)
        buf[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
        buf[21] = min(250, max(10, w_mm // 10))
        buf[22] = min(160, max(6, h_mm // 10))
        rxi, ryi = int(round(rx * 1024)), int(round(ry * 1024))
        gxi, gyi = int(round(gx * 1024)), int(round(gy * 1024))
        bxi, byi = int(round(bx * 1024)), int(round(by * 1024))
        buf[25] = ((rxi & 3) << 6) | ((ryi & 3) << 4) | ((gxi & 3) << 2) | (gyi & 3)
        buf[26] = ((bxi & 3) << 6) | ((byi & 3) << 4)
        buf[27] = (rxi >> 2) & 0xFF
        buf[28] = (ryi >> 2) & 0xFF
        buf[29] = (gxi >> 2) & 0xFF
        buf[30] = (gyi >> 2) & 0xFF
        buf[31] = (bxi >> 2) & 0xFF
        buf[32] = (byi >> 2) & 0xFF
        if use_dtd:
            buf[54], buf[55] = 0x36, 0x10  # non-zero pixel clock -> valid DTD #1
            buf[66] = w_mm & 0xFF
            buf[67] = h_mm & 0xFF
            buf[68] = (((w_mm >> 8) & 0x0F) << 4) | ((h_mm >> 8) & 0x0F)
        return bytes(buf)

    # 16" sRGB laptop (345x215 mm)
    edid_16 = _build_edid(345, 215, 0.64, 0.33, 0.30, 0.60, 0.15, 0.06)
    p16 = wd_mod.parse_edid_physical_and_gamut(edid_16, model_name="NE160QDM-NZL")
    assert (p16["width_mm"], p16["height_mm"]) == (345, 215)
    assert abs(p16["diagonal_in"] - 16.0) < 0.1
    assert abs(p16["solid_angle_sr"] - 0.2250) < 0.002
    assert p16["auto_panel_type"] == "wled_ips"

    # 27" wide-gamut DCI-P3 monitor (>=93% P3) -> p3_ips
    edid_p3 = _build_edid(597, 336, 0.68, 0.32, 0.265, 0.69, 0.15, 0.06)
    pp3 = wd_mod.parse_edid_physical_and_gamut(edid_p3, model_name="Dell U2724D")
    assert pp3["gamut_dci_p3_pct"] >= 98.0
    assert pp3["auto_panel_type"] == "p3_ips"
    assert abs(pp3["solid_angle_sr"] - 0.4604) < 0.004

    # OLED model-name overrides & bytes 21-22 cm fallback
    assert wd_mod.parse_edid_physical_and_gamut(edid_p3, model_name="AW3225QF QD-OLED")["auto_panel_type"] == "qd_oled"
    assert wd_mod.parse_edid_physical_and_gamut(edid_p3, model_name="LG 27GR95QE OLED")["auto_panel_type"] == "woled"
    edid_cm = _build_edid(340, 210, 0.64, 0.33, 0.30, 0.60, 0.15, 0.06, use_dtd=False)
    pcm = wd_mod.parse_edid_physical_and_gamut(edid_cm)
    assert (pcm["width_mm"], pcm["height_mm"]) == (340, 210)


def test_wave2_modern_standby_s0ix_and_neutral_magnification_teardown() -> None:
    """
    Wave 2 (Report #2): Verify S0ix Modern Standby `_handle_suspend`/`_handle_resume`
    on `SleepHandoffGuard` and automatic `MagUninitialize()` teardown on neutral `>=6450K`.
    """
    from orchestrator.display.windows_display import (
        IDENTITY_COLOR_MATRIX,
        is_identity_color_matrix,
    )

    # 1. S0ix suspend/resume cycle
    class DummyDisplay:
        def __init__(self) -> None:
            self.restores = 0

        def restore(self) -> bool:
            self.restores += 1
            return True

    resumed_count = [0]
    bulb = MockBulbDriver()
    disp = DummyDisplay()
    guard = SleepHandoffGuard(
        light=bulb,
        display=disp,
        on_resume=lambda: resumed_count.__setitem__(0, resumed_count[0] + 1),
    )
    guard.update_context(phase="DAY", unlinked_light_alert=False, last_bulb_power=True)
    guard._handle_suspend(reason="s0ix_display_off")
    assert disp.restores == 1
    guard._handle_resume(reason="s0ix_display_on")
    assert resumed_count[0] == 1
    # Second suspend after resume must work cleanly (state reset on resume)
    guard._handle_suspend(reason="s0ix_suspend")
    assert disp.restores == 2

    # 2. Neutral >=6450K identity matrix auto-teardown (MPO/VRR bypass)
    assert is_identity_color_matrix(IDENTITY_COLOR_MATRIX) is True
    assert is_identity_color_matrix([0.9] + IDENTITY_COLOR_MATRIX[1:]) is False

    class MockMagBackend:
        def __init__(self) -> None:
            self.init_calls = 0
            self.uninit_calls = 0
            self.matrices: list[list[float]] = []

        def initialize(self) -> bool:
            self.init_calls += 1
            return True

        def set_matrix(self, mat: list[float]) -> bool:
            self.matrices.append(list(mat))
            return True

        def uninitialize(self) -> bool:
            self.uninit_calls += 1
            return True

    mag = MockMagBackend()
    ctrl = WindowsDisplayController(enabled=True, mag_backend=mag)
    res_warm = ctrl.apply(brightness_pct=60, cct_k=3000)
    assert res_warm["color_method"] == "magnification"
    assert mag.init_calls == 1 and mag.uninit_calls == 0

    # Neutral 6500K @ 100% triggers automatic MagUninitialize()!
    res_neutral = ctrl.apply(brightness_pct=100, cct_k=6500)
    assert res_neutral["gamma_ok"] is True
    assert res_neutral["color_method"] == "none"
    assert mag.uninit_calls == 1
    assert ctrl._mag_initialized is False
    assert is_identity_color_matrix(mag.matrices[-1]) is True


def test_wave2_ddc_eeprom_debounce_deadband_retry_and_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Wave 2 (Report #2): Verify DDC/CI EEPROM wear guard (>=450 ms debounce, >=2% deadband,
    transient retry on failure) and human-readable `ddc_diagnostic` status.
    """
    import orchestrator.display.windows_display as wd_mod

    fake_now = [100.0]
    monkeypatch.setattr(wd_mod.time, "monotonic", lambda: fake_now[0])

    class FlakyMockDDC:
        def __init__(self) -> None:
            self.val = 70
            self.set_calls: list[int] = []
            self.fail_next = False

        def list_monitors(self) -> list[int]:
            return [0]

        def get_brightness(self, _m: int) -> tuple[int, int, int]:
            return (0, self.val, 100)

        def set_brightness(self, _m: int, v: int) -> bool:
            self.set_calls.append(int(v))
            if self.fail_next:
                self.fail_next = False
                return False
            self.val = int(v)
            return True

    ddc = FlakyMockDDC()
    ctrl = WindowsDisplayController(enabled=True, ddc_backend=ddc)
    assert ctrl.ddc_diagnostic == "Hardware backlight idle"

    # First write succeeds and updates diagnostic to DDC/CI OK
    ctrl.apply(brightness_pct=80, cct_k=5500)
    assert ddc.set_calls == [80]
    assert "DDC/CI" in ctrl.ddc_diagnostic

    # Immediate write within <450 ms is debounced (EEPROM wear guard)
    fake_now[0] += 0.20
    ctrl.apply(brightness_pct=50, cct_k=5500)
    assert ddc.set_calls == [80]

    # After >=450 ms, 1% delta (<2% deadband) is suppressed
    fake_now[0] += 0.50
    ctrl.apply(brightness_pct=81, cct_k=5500)
    assert ddc.set_calls == [80]

    # After >=450 ms, >=2% delta with 1 transient failure retries and succeeds
    fake_now[0] += 0.50
    ddc.fail_next = True
    ctrl.apply(brightness_pct=60, cct_k=5500)
    assert ddc.set_calls == [80, 60, 60]
    assert ddc.val == 60


def test_wave2_msix_identity_autostart_branching_zero_hkcu_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Wave 2 (Report #4 / Policy 10.2.8): Verify `is_packaged_msix()` branching:
    - Under MSIX (`True`), `set_windows_autostart_enabled()` NEVER opens/writes `HKCU\\Run`
      and returns `(False, "msix_use_windows_settings")`; getter reads `get_msix_startup_state()`.
    - Unpackaged (`False`), writes `"<exe>" --background` into mocked `HKCU\\Run` cleanly.
    """
    import winreg
    import orchestrator.display.windows_display as wd_mod

    monkeypatch.setattr(wd_mod.sys, "platform", "win32")

    # 1. Packaged MSIX: zero HKCU\Run writes allowed
    monkeypatch.setattr(wd_mod, "is_packaged_msix", lambda: True)
    monkeypatch.setattr(wd_mod, "get_msix_startup_state", lambda *_a, **_kw: 2)

    def _forbid_open_key(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("HKCU\\Run MUST NEVER be touched inside MSIX!")

    monkeypatch.setattr(winreg, "OpenKey", _forbid_open_key)
    assert wd_mod.get_windows_autostart_enabled() is True
    ok, msg = wd_mod.set_windows_autostart_enabled(True)
    assert (ok, msg) == (False, "msix_use_windows_settings")

    # 2. Unpackaged portable exe: writes "<exe>" --background into mocked registry store
    reg_store: dict[str, str] = {}

    class MemKey:
        def __enter__(self) -> "MemKey":
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

    monkeypatch.setattr(wd_mod, "is_packaged_msix", lambda: False)
    monkeypatch.setattr(wd_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(wd_mod.sys, "executable", r"C:\Apps\CircadianDesk\CircadianDesk.exe")
    monkeypatch.setattr(winreg, "OpenKey", lambda *_a, **_kw: MemKey())
    monkeypatch.setattr(
        winreg,
        "SetValueEx",
        lambda _k, name, _r, _t, val: reg_store.__setitem__(name, str(val)),
    )
    monkeypatch.setattr(
        winreg,
        "QueryValueEx",
        lambda _k, name: (reg_store[name], winreg.REG_SZ) if name in reg_store else (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(
        winreg,
        "DeleteValue",
        lambda _k, name: reg_store.pop(name, None) if name in reg_store else (_ for _ in ()).throw(FileNotFoundError()),
    )

    ok_on, _ = wd_mod.set_windows_autostart_enabled(True)
    assert ok_on is True
    assert reg_store["CircadianDesk"] == '"C:\\Apps\\CircadianDesk\\CircadianDesk.exe" --background'
    assert wd_mod.get_windows_autostart_enabled() is True

    ok_off, _ = wd_mod.set_windows_autostart_enabled(False)
    assert ok_off is True
    assert "CircadianDesk" not in reg_store
    assert wd_mod.get_windows_autostart_enabled() is False


def test_wave2_g1_single_instance_mutex_and_g2_version_fast_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Wave 2 (Guardrails G1 & G2 / Store Policy 10.1.2):
    - G2: `--version` prints `Circadian Desk 1.0.0` and exits `0` BEFORE mutex/config/sockets.
    - G1: Second interactive launch opens `http://127.0.0.1:8765` and exits `0`;
      second `--background` launch exits `0` silently without opening a browser.
    """
    import orchestrator.daemon as daemon_mod

    assert daemon_mod.APP_VERSION == "1.0.0"
    assert daemon_mod.SINGLE_INSTANCE_MUTEX_NAME == "Local\\CircadianDesk_SingleInstance_v1"

    # G2: --version must never touch mutex
    monkeypatch.setattr(
        daemon_mod,
        "acquire_single_instance_mutex",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("--version must exit BEFORE mutex!")),
    )
    rc_ver = daemon_main(["--version"])
    assert rc_ver == 0
    assert "Circadian Desk 1.0.0" in capsys.readouterr().out

    # G1: Already running instance (ERROR_ALREADY_EXISTS)
    opened_urls: list[str] = []
    released: list[Any] = []
    monkeypatch.setattr(daemon_mod, "acquire_single_instance_mutex", lambda *_a, **_kw: (9999, True))
    monkeypatch.setattr(daemon_mod, "release_single_instance_mutex", lambda h: released.append(h))
    monkeypatch.setattr(daemon_mod.webbrowser, "open", lambda url: opened_urls.append(str(url)) or True)

    rc_interactive = daemon_main(["--open-loop", "--light", "none", "--no-tray", "--ui-port", "8765"])
    assert rc_interactive == 0
    assert opened_urls == ["http://127.0.0.1:8765"]
    assert released == [9999]

    opened_urls.clear()
    released.clear()
    rc_bg = daemon_main(["--open-loop", "--light", "none", "--no-tray", "--background", "--ui-port", "8765"])
    assert rc_bg == 0
    assert opened_urls == []
    assert released == [9999]


def test_wave2_g3_localappdata_storage_legacy_migration_and_frozen_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Wave 2 (Guardrail G3 / Store Policy 10.2.7 & 10.1.2):
    - `get_app_data_dir()` resolves to `%LOCALAPPDATA%\\CircadianDesk`.
    - `load_config(None)` transparently reads legacy `~/.circadian_orchestrator/config.json`
      and `save_config(cfg, None)` migrates atomically into `%LOCALAPPDATA%\\CircadianDesk\\config.json`.
    - `resolve_default_argv()` handles frozen 0-arg (`--open-browser`) vs `--background` (no browser).
    """
    import orchestrator.config as cfg_mod
    import orchestrator.daemon as daemon_mod

    fake_local = tmp_path / "LocalAppData"
    legacy_dir = tmp_path / "legacy_home" / ".circadian_orchestrator"
    legacy_file = legacy_dir / "config.json"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    save_config(UserConfig(wake_hour=9.5, sleep_hour=1.5, panel_type="qd_oled"), legacy_file)

    monkeypatch.setenv("LOCALAPPDATA", str(fake_local))
    monkeypatch.setattr(cfg_mod.sys, "platform", "win32")
    monkeypatch.setattr(cfg_mod, "LEGACY_CONFIG_PATH", legacy_file)

    app_dir = cfg_mod.get_app_data_dir()
    assert app_dir == fake_local / "CircadianDesk"

    # Transparent legacy fallback on load_config(None)
    migrated = load_config(None)
    assert migrated.wake_hour == 9.5
    assert migrated.sleep_hour == 1.5
    assert migrated.panel_type == "qd_oled"

    # Atomic save lands in %LOCALAPPDATA%\CircadianDesk\config.json
    saved_path = save_config(migrated, None)
    assert saved_path == fake_local / "CircadianDesk" / "config.json"
    assert saved_path.is_file()

    # Frozen argv resolution: 0-arg interactive vs --background startup
    monkeypatch.setattr(daemon_mod, "get_app_data_dir", lambda: app_dir)
    monkeypatch.setattr(daemon_mod.sys, "frozen", True, raising=False)

    monkeypatch.setattr(daemon_mod.sys, "argv", ["CircadianDesk.exe"])
    argv_interactive = daemon_mod.resolve_default_argv(None)
    assert argv_interactive is not None
    assert "--open-browser" in argv_interactive
    assert "--background" not in argv_interactive
    assert str(app_dir / "desk_log.csv") in argv_interactive

    monkeypatch.setattr(daemon_mod.sys, "argv", ["CircadianDesk.exe", "--background"])
    argv_bg = daemon_mod.resolve_default_argv(None)
    assert argv_bg is not None
    assert "--background" in argv_bg
    assert "--open-browser" not in argv_bg


def test_wave2_live_tray_tooltip_and_braun_png_generator() -> None:
    """
    Wave 2 (Report #3 & #4): Verify zero-hook `format_live_tray_tooltip()` (`<=127` chars)
    across all states and stdlib `generate_braun_png_bytes()` valid RGBA PNG output (<204,800 B).
    """
    import struct
    from orchestrator.tray_win32 import format_live_tray_tooltip, generate_braun_png_bytes

    tip_active = format_live_tray_tooltip(
        phase="EVENING", mon_pct=48, mon_cct=3150, target_nits=55.0, profile_label="Comfort"
    )
    assert "3150K" in tip_active and "55 nit" in tip_active and len(tip_active) <= 127

    tip_hdr = format_live_tray_tooltip(
        phase="EVENING", mon_pct=48, mon_cct=3150, auto_pause_reason="Windows HDR Active"
    )
    assert "HDR" in tip_hdr and len(tip_hdr) <= 127

    tip_long = format_live_tray_tooltip(
        phase="DAY", mon_pct=80, mon_cct=5500, auto_pause_reason="VeryLongApplicationName_" * 10
    )
    assert len(tip_long) <= 127

    for dim, light in ((44, False), (150, False), (256, True)):
        png = generate_braun_png_bytes(dim, light_theme=light)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert png[12:16] == b"IHDR"
        w, h, bit_depth, color_type = struct.unpack(">IIBB", png[16:26])
        assert (w, h, bit_depth, color_type) == (dim, dim, 8, 6)
        assert png.endswith(b"IEND\xaeB`\x82")
        assert len(png) < 204_800


def test_wave3_web_ui_open_settings_whitelist_and_csrf() -> None:
    """
    Wave 3 (Report #3 & #4): Verify `POST /api/open-settings` strictly whitelists
    `nightlight`, `colorfilter`, `display`, `startupapps` (`200`), rejects unknown targets (`400`),
    and enforces Host/Origin CSRF (`403`).
    """
    opened: list[str] = []

    def _fake_open(target: str) -> tuple[bool, str]:
        opened.append(target)
        return True, f"ms-settings:{target}"

    state = OrchestratorState(mode="open_loop")
    with LocalWebUIServer(state, host="127.0.0.1", port=0, open_settings_fn=_fake_open) as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.actual_port, timeout=5)
        try:
            for tgt in ("nightlight", "colorfilter", "display", "startupapps"):
                conn.request(
                    "POST",
                    "/api/open-settings",
                    body=json.dumps({"target": tgt}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                resp.read()
                assert resp.status == 200
            assert opened == ["nightlight", "colorfilter", "display", "startupapps"]

            # Unknown / dangerous targets must return 400 and never invoke opener
            opened.clear()
            for bad in ("cmd.exe", "https://evil.example", "ms-settings:windowsupdate", ""):
                conn.request(
                    "POST",
                    "/api/open-settings",
                    body=json.dumps({"target": bad}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                resp.read()
                assert resp.status == 400
            assert opened == []

            # Spoofed Origin CSRF must return 403
            conn.request(
                "POST",
                "/api/open-settings",
                body=json.dumps({"target": "nightlight"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            )
            resp_csrf = conn.getresponse()
            resp_csrf.read()
            assert resp_csrf.status == 403
            assert opened == []
        finally:
            conn.close()


def test_wave3_web_ui_new_api_state_keys_and_panel_type_validation() -> None:
    """
    Wave 3: Verify `/api/state` exposes all new Wave 1-3 fields (`panel_type`, `auto_panel_type`,
    `diagonal_in`, `solid_angle_sr`, `dark_mode`, `estimated_corneal_m_edi_lx`, `hdr_auto_paused`,
    `ddc_diagnostic`, `is_packaged_msix`, `app_version == "1.0.0"`) and validates `panel_type`.
    """
    saved: list[dict[str, Any]] = []
    state = OrchestratorState(
        mode="open_loop",
        panel_type="auto",
        auto_panel_type="p3_ips",
        diagonal_in=16.0,
        solid_angle_sr=0.225,
        dark_mode=True,
        estimated_corneal_m_edi_lx=2.3,
        hdr_auto_paused=True,
        ddc_diagnostic="WMI laptop backlight OK",
        is_packaged_msix=True,
        on_settings_saved=lambda cfg: saved.append(dict(cfg)),
    )

    with LocalWebUIServer(state, host="127.0.0.1", port=0) as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.actual_port, timeout=5)
        try:
            conn.request("GET", "/api/state")
            resp = conn.getresponse()
            snap = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            assert snap["panel_type"] == "auto"
            assert snap["auto_panel_type"] == "p3_ips"
            assert snap["diagonal_in"] == 16.0
            assert snap["solid_angle_sr"] == 0.225
            assert snap["dark_mode"] is True
            assert snap["estimated_corneal_m_edi_lx"] == 2.3
            assert snap["hdr_auto_paused"] is True
            assert snap["ddc_diagnostic"] == "WMI laptop backlight OK"
            assert snap["is_packaged_msix"] is True
            assert snap["app_version"] == "1.0.0"

            # Valid panel_type update -> 200
            conn.request(
                "POST",
                "/api/settings",
                body=json.dumps({"panel_type": "qd_oled"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            r_ok = conn.getresponse()
            r_ok.read()
            assert r_ok.status == 200
            assert state.panel_type == "qd_oled"
            assert saved and saved[-1]["panel_type"] == "qd_oled"

            # Invalid panel_type -> 400
            conn.request(
                "POST",
                "/api/settings",
                body=json.dumps({"panel_type": "crt_invalid"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            r_bad = conn.getresponse()
            r_bad.read()
            assert r_bad.status == 400
        finally:
            conn.close()


def test_wave3_dashboard_scrubber_hdr_msix_and_landing_js_check() -> None:
    """
    Wave 3: Verify dashboard HTML enforces 0 external URLs, 0 `type="time"`, 0 `<select>`,
    24h interactive SVG scrubber hooks, HDR Auto-Pause & MSIX UI elements, inline JS `node --check`
    EXIT 0, and `landing/app.js` `node --check` EXIT 0.
    """
    import re
    import subprocess
    import tempfile

    state = OrchestratorState(mode="open_loop", is_packaged_msix=True, hdr_auto_paused=True)
    with LocalWebUIServer(state, host="127.0.0.1", port=0) as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.actual_port, timeout=5)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            html_out = resp.read().decode("utf-8")
            assert resp.status == 200
        finally:
            conn.close()

    assert "http://" not in html_out and "https://" not in html_out
    assert 'type="time"' not in html_out
    assert "<select" not in html_out.lower() and 'createElement("select")' not in html_out
    for tok in ("ew-resize", "pointerdown", "pointermove", "pointerup", "hdrPill", "sourcePill", "panel-btn", "/api/open-settings", "startupapps"):
        assert tok in html_out, f"Missing expected Wave 3 UI token: {tok}"

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_out, re.S | re.I)
    assert len(scripts) >= 1
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write("\n".join(scripts))
        tmp_js = Path(tf.name)
    try:
        chk_ui = subprocess.run(["node", "--check", str(tmp_js)], capture_output=True, text=True, timeout=15)
        assert chk_ui.returncode == 0, f"Dashboard inline JS error:\n{chk_ui.stderr}"
    finally:
        tmp_js.unlink(missing_ok=True)

    repo_root = Path(__file__).resolve().parents[2]
    landing_js = repo_root / "landing" / "app.js"
    assert landing_js.is_file()
    chk_landing = subprocess.run(["node", "--check", str(landing_js)], capture_output=True, text=True, timeout=15)
    assert chk_landing.returncode == 0, f"landing/app.js syntax error:\n{chk_landing.stderr}"


def test_wave3_packaging_msix_manifest_policy_docs_and_release_workflow() -> None:
    """
    Wave 3 & Report #4: Verify `pyproject.toml` 1.0.0, Microsoft Store `AppxManifest.xml`
    schema/capabilities compliance, root `PRIVACY.md` (Policy 10.5.1), `CODE_SIGNING_POLICY.md`
    (SignPath), and `.github/workflows/release.yml` CI controls.
    """
    import xml.etree.ElementTree as ET

    proto_dir = Path(__file__).resolve().parents[1]
    repo_root = proto_dir.parent

    # 1. pyproject.toml version 1.0.0
    pyproject_txt = (proto_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.0.0"' in pyproject_txt

    # 2. AppxManifest.xml Store certification invariants
    manifest_path = proto_dir / "packaging" / "msix" / "AppxManifest.xml"
    tree = ET.parse(manifest_path)
    root = tree.getroot()
    ns = {
        "m": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
        "uap10": "http://schemas.microsoft.com/appx/manifest/uap/windows10/10",
        "rescap": "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities",
        "desktop": "http://schemas.microsoft.com/appx/manifest/desktop/windows10",
    }
    identity = root.find("m:Identity", ns)
    assert identity is not None
    assert identity.attrib["Version"] == "1.0.0.0"
    assert identity.attrib["ProcessorArchitecture"] == "x64"

    app = root.find("m:Applications/m:Application", ns)
    assert app is not None
    assert app.attrib[f"{{{ns['uap10']}}}RuntimeBehavior"] == "packagedClassicApp"
    assert app.attrib[f"{{{ns['uap10']}}}TrustLevel"] == "mediumIL"

    startup_ext = app.find("m:Extensions/desktop:Extension", ns)
    assert startup_ext is not None
    assert startup_ext.attrib["Category"] == "windows.startupTask"
    assert startup_ext.attrib[f"{{{ns['uap10']}}}Parameters"] == "--background"
    startup_task = startup_ext.find("desktop:StartupTask", ns)
    assert startup_task is not None and startup_task.attrib["Enabled"] == "false"

    caps_el = root.find("m:Capabilities", ns)
    assert caps_el is not None
    cap_children = list(caps_el)
    assert len(cap_children) == 1
    assert cap_children[0].attrib["Name"] == "runFullTrust"

    # 3. Root PRIVACY.md & CODE_SIGNING_POLICY.md
    privacy_txt = (repo_root / "PRIVACY.md").read_text(encoding="utf-8")
    assert "%LOCALAPPDATA%\\CircadianDesk\\" in privacy_txt
    assert "10.2.7" in privacy_txt

    signing_txt = (repo_root / "CODE_SIGNING_POLICY.md").read_text(encoding="utf-8")
    assert "# Code signing policy" in signing_txt
    assert "This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it." in signing_txt

    # 4. GitHub Actions release workflow
    wf_txt = (repo_root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for required in (
        "actions/setup-python@v5",
        "Get-AuthenticodeSignature",
        "makepri.exe",
        "makeappx.exe",
        "actions/attest-build-provenance@v2",
        "signpath/github-action-submit-signing-request@v3",
    ):
        assert required in wf_txt, f"Missing CI requirement in release.yml: {required}"
    makeappx_cmd_lines = [
        line for line in wf_txt.splitlines()
        if "makeappx" in line.lower() and not line.lstrip().startswith("#")
    ]
    assert makeappx_cmd_lines and all("/nv" not in line.lower() for line in makeappx_cmd_lines)


def test_wave4_build_windows_onedir_command_zip_layout_and_23_png_assets(tmp_path: Path) -> None:
    """
    Wave 4 (Guardrail G4 & Report #4): Verify `build_windows.py` `--onedir --noupx` flags,
    clean `CircadianDesk/` root folder in Portable ZIP (zero loose files), all 23 transparent
    Braun PNG assets `<204,800 B`, and deterministic `SHA256SUMS.txt`.
    """
    import hashlib
    import zipfile
    import build_windows as bw

    assert bw.APP_VERSION == "1.0.0"
    assert bw.APP_VERSION_QUAD == "1.0.0.0"
    assert len(bw.MSIX_PNG_ASSET_SPECS) == 23

    win_manifest, ver_file = bw.write_win32_build_metadata(tmp_path / "meta")
    cmd = bw.build_pyinstaller_command(
        sys.executable,
        tmp_path / "dummy.ico",
        mode="onedir",
        manifest_path=win_manifest,
        version_file=ver_file,
    )
    assert "--onedir" in cmd and "--noconsole" in cmd and "--noupx" in cmd
    assert "--manifest" in cmd and "--version-file" in cmd
    assert "tkinter" in cmd

    # Verify clean CircadianDesk/ top-level folder inside --onedir Portable ZIP (G4)
    fake_onedir = tmp_path / "CircadianDesk"
    (fake_onedir / "_internal").mkdir(parents=True, exist_ok=True)
    (fake_onedir / "CircadianDesk.exe").write_bytes(b"MZ_TEST_EXE")
    (fake_onedir / "_internal" / "runtime.dll").write_bytes(b"DLL_TEST")
    out_zip = tmp_path / "CircadianDesk-v1.0.0-win64-portable.zip"
    bw.package_portable_zip(fake_onedir, out_zip, mode="onedir")

    with zipfile.ZipFile(out_zip, "r") as zf:
        names = zf.namelist()
    assert names, "Portable ZIP must not be empty"
    assert all(n.startswith("CircadianDesk/") for n in names), f"Loose root files found: {names}"
    assert "CircadianDesk/CircadianDesk.exe" in names
    assert "CircadianDesk/_internal/runtime.dll" in names
    assert "CircadianDesk/README_QUICKSTART.txt" in names
    assert "CircadianDesk/LICENSE" in names

    # Verify SHA256SUMS.txt
    sums_file = bw.write_sha256_sums([out_zip], tmp_path / "SHA256SUMS.txt")
    expected_sha = hashlib.sha256(out_zip.read_bytes()).hexdigest()
    assert f"{expected_sha}  {out_zip.name}" in sums_file.read_text(encoding="utf-8")

    # Verify complete 23-file MSIX PNG matrix (< 204,800 B each)
    generated = bw.generate_msix_assets(tmp_path / "Assets")
    assert len(generated) == 23
    for png_path in generated:
        raw = png_path.read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(raw) < bw.MSIX_MAX_ASSET_BYTES


def test_wave4_open_windows_settings_uri_whitelist_strictness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Wave 4: Unit-test `open_windows_settings_uri()` directly with mocked `os.startfile`
    (never launches real Windows Settings) ensuring strict 4-target whitelist enforcement.
    """
    import orchestrator.display.windows_display as wd_mod

    launched: list[str] = []
    monkeypatch.setattr(wd_mod.sys, "platform", "win32")
    monkeypatch.setattr(wd_mod.os, "startfile", lambda uri: launched.append(str(uri)), raising=False)

    expected = {
        "nightlight": "ms-settings:nightlight",
        "colorfilter": "ms-settings:easeofaccess-colorfilter",
        "display": "ms-settings:display",
        "startupapps": "ms-settings:startupapps",
    }
    for key, uri in expected.items():
        ok, msg = wd_mod.open_windows_settings_uri(key)
        assert (ok, msg) == (True, uri)
    assert launched == list(expected.values())

    launched.clear()
    for bad in ("calc.exe", "ms-settings:windowsupdate", "file:///C:/Windows", "http://127.0.0.1", ""):
        ok, _ = wd_mod.open_windows_settings_uri(bad)
        assert ok is False
    assert launched == []


