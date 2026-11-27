"""CIE S 026 photobiology engine & hardened 3-layer circadian control algorithm (stdlib only).

Pure Track A v6.1 Architecture — Honest Bezel-Hybrid Physics:
- Room Ambient Spectral Sensing (~+/-15%) via OLGA-8 multi-spectral sensor
  (auto-supports AS7341, AS7343, and TCS3448 channel bands) + Active Display Guardrail
  (Windows/macOS DDC/CI brightness + GPU LUT warm shift — ZERO camera, ZERO screen
  pixel capture / DXGI / ScreenCaptureKit).
- Once the Desktop Agent dims an evening display to 15-25% @ 2300K, worst-case white
  screen corneal emission collapses from ~90 lx down to ~3-6 lx m-EDI (+/-2 lx), keeping
  total eye-level exposure bounded without invasive screen recording.

Implements Kova-1 Hardening & v6.0 Metrology Locks:
- BUG-1 Permanent Fix: Feedforward bulb-contribution subtraction + gain-normalized
  discrete integrator `e[k+1] = (1 - K*g)*e[k]` (K*g in [0.1, 0.3]) with explicit
  anti-windup clamping at 0% / 100% and 8-bit Matter/Hue low-brightness quantization
  deadband (>= 1.5x min_dimming_step_pct).
- BUG-5a Saturation Handling: Saturated frames never enter the median buffer; hold last
  safe command with reading_valid=False and compliance_status="BELIRSIZ".
- v6.0 Metrology: k=2 expanded uncertainty (+/-17%) 3-state compliance
  ("SAGLANDI" / "SAGLANMADI" / "BELIRSIZ"), <0.3 lx night measurement floor,
  >10s persistent occlusion freeze (never presenting stale values as live),
  optional <=5 lx High-Sensitivity Evening preset (Spitschan 2022, visual-comfort framed),
  and 10-minute (600s) external-controller Conflict-Observer backoff.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

AS7343_CHANNELS: tuple[str, ...] = (
    "F1",       # 405 nm (AS7343/TCS3448) | 415 nm (AS7341)
    "F2",       # 425 nm (AS7343/TCS3448) | 445 nm (AS7341)
    "FZ",       # 450 nm (AS7343/TCS3448)
    "F3",       # 475 nm (AS7343/TCS3448) | 480 nm (AS7341) — near 490 nm melanopsin peak
    "F4",       # 515 nm (all three)      — cyan-green melanopsin shoulder
    "FY",       # 555 nm (AS7343/TCS3448)
    "F5",       # 550 nm (AS7343/TCS3448) | 555 nm (AS7341)
    "FXL",      # 600 nm (AS7343/TCS3448)
    "F6",       # 640 nm (AS7343/TCS3448) | 590 nm (AS7341)
    "F7",       # 690 nm (AS7343/TCS3448) | 630 nm (AS7341)
    "F8",       # 745 nm (AS7343/TCS3448) | 680 nm (AS7341)
    "NIR",      # 855 nm / 910 nm
    "CLEAR",    # broadband silicon
    "FLICKER",  # hardware flicker frequency in Hz (0.0 / 100.0 / 120.0)
)

# 10 primary spectral nodes for 14-ch AS7343 / TCS3448
_SPECTRAL_NODES: tuple[tuple[str, float, float], ...] = (
    ("F1",  405.0, 20.0),
    ("F2",  425.0, 22.5),
    ("FZ",  450.0, 25.0),
    ("F3",  475.0, 32.5),
    ("F4",  515.0, 37.5),
    ("F5",  550.0, 42.5),
    ("FXL", 600.0, 45.0),
    ("F6",  640.0, 45.0),
    ("F7",  690.0, 52.5),
    ("F8",  745.0, 55.0),
)

# CIE 1931 2-deg CMFs & CIE S 026:2018 corneal melanopic sensitivity for AS7343 / TCS3448
_CIE_X: dict[str, float] = {
    "F1": 0.0232, "F2": 0.2148, "FZ": 0.3362, "F3": 0.1421, "F4": 0.0291,
    "F5": 0.4334, "FXL": 1.0622, "F6": 0.4479, "F7": 0.0227, "F8": 0.0005,
}
_CIE_Y: dict[str, float] = {
    "F1": 0.0006, "F2": 0.0073, "FZ": 0.0380, "F3": 0.1126, "F4": 0.6082,
    "F5": 0.9950, "FXL": 0.6310, "F6": 0.1750, "F7": 0.0082, "F8": 0.0002,
}
_CIE_Z: dict[str, float] = {
    "F1": 0.1110, "F2": 1.0391, "FZ": 1.7721, "F3": 1.0419, "F4": 0.1117,
    "F5": 0.0087, "FXL": 0.0008, "F6": 0.0000, "F7": 0.0000, "F8": 0.0000,
}
_CIE_MEL: dict[str, float] = {
    "F1": 0.0180, "F2": 0.1550, "FZ": 0.5580, "F3": 0.9420, "F4": 0.7120,
    "F5": 0.1650, "FXL": 0.0075, "F6": 0.0005, "F7": 0.0000, "F8": 0.0000,
}
_D65_REL_SPD: dict[str, float] = {
    "F1": 0.846, "F2": 0.916, "FZ": 1.125, "F3": 1.116, "F4": 1.048,
    "F5": 1.000, "FXL": 0.866, "F6": 0.805, "F7": 0.671, "F8": 0.722,
}

_D65_X_INT = sum(_D65_REL_SPD[ch] * _CIE_X[ch] * dw for ch, _, dw in _SPECTRAL_NODES)
_D65_Y_INT = sum(_D65_REL_SPD[ch] * _CIE_Y[ch] * dw for ch, _, dw in _SPECTRAL_NODES)
_D65_Z_INT = sum(_D65_REL_SPD[ch] * _CIE_Z[ch] * dw for ch, _, dw in _SPECTRAL_NODES)
_D65_MEL_INT = sum(_D65_REL_SPD[ch] * _CIE_MEL[ch] * dw for ch, _, dw in _SPECTRAL_NODES)

_X_QUAD_SCALE = 0.95047 * (_D65_Y_INT / _D65_X_INT)
_Z_QUAD_SCALE = 1.08883 * (_D65_Y_INT / _D65_Z_INT)
_MEL_TO_D65_SCALE = _D65_Y_INT / _D65_MEL_INT

# 8 primary visible spectral nodes for 11-ch AS7341 (415..680 nm)
_AS7341_NODES: tuple[tuple[str, float, float], ...] = (
    ("F1", 415.0, 28.0),
    ("F2", 445.0, 32.5),
    ("F3", 480.0, 35.0),
    ("F4", 515.0, 37.5),
    ("F5", 555.0, 37.5),
    ("F6", 590.0, 37.5),
    ("F7", 630.0, 45.0),
    ("F8", 680.0, 50.0),
)
_AS7341_X: dict[str, float] = {
    "F1": 0.0776, "F2": 0.3483, "F3": 0.0956, "F4": 0.0291,
    "F5": 0.5121, "F6": 1.0263, "F7": 0.6424, "F8": 0.0468,
}
_AS7341_Y: dict[str, float] = {
    "F1": 0.0022, "F2": 0.0298, "F3": 0.1390, "F4": 0.6082,
    "F5": 1.0000, "F6": 0.7570, "F7": 0.2650, "F8": 0.0170,
}
_AS7341_Z: dict[str, float] = {
    "F1": 0.3713, "F2": 1.7826, "F3": 0.8130, "F4": 0.1117,
    "F5": 0.0057, "F6": 0.0011, "F7": 0.0000, "F8": 0.0000,
}
_AS7341_MEL: dict[str, float] = {
    "F1": 0.0680, "F2": 0.4550, "F3": 0.9680, "F4": 0.7120,
    "F5": 0.1250, "F6": 0.0140, "F7": 0.0010, "F8": 0.0000,
}
_AS7341_D65: dict[str, float] = {
    "F1": 0.892, "F2": 1.080, "F3": 1.118, "F4": 1.048,
    "F5": 0.985, "F6": 0.887, "F7": 0.822, "F8": 0.750,
}
_AS7341_D65_X = sum(_AS7341_D65[c] * _AS7341_X[c] * dw for c, _, dw in _AS7341_NODES)
_AS7341_D65_Y = sum(_AS7341_D65[c] * _AS7341_Y[c] * dw for c, _, dw in _AS7341_NODES)
_AS7341_D65_Z = sum(_AS7341_D65[c] * _AS7341_Z[c] * dw for c, _, dw in _AS7341_NODES)
_AS7341_D65_M = sum(_AS7341_D65[c] * _AS7341_MEL[c] * dw for c, _, dw in _AS7341_NODES)
_AS7341_X_SCALE = 0.95047 * (_AS7341_D65_Y / _AS7341_D65_X)
_AS7341_Z_SCALE = 1.08883 * (_AS7341_D65_Y / _AS7341_D65_Z)
_AS7341_MEL_SCALE = _AS7341_D65_Y / _AS7341_D65_M


@dataclass
class SpectralSample:
    """Single gain-and-integration-normalized OLGA-8 spectral capture."""

    timestamp: float
    channels: dict[str, float]
    saturated: bool = False
    chip: str = "AS7343"


@dataclass
class PhotometricReading:
    """CIE 1931 + CIE S 026 photometric & circadian metrics at the eye plane."""

    timestamp: float
    m_edi_lx: float
    photopic_lx: float
    cct_k: float
    m_der: float
    flicker_hz: float = 0.0
    saturated: bool = False
    below_floor: bool = False
    uncertainty_k2_lx: float = 0.0


@dataclass
class ControlAction:
    """Output command of one step of the hardened 3-layer circadian controller."""

    phase: str  # "DAY" | "EVENING" | "NIGHT"
    target_m_edi_lx: float
    measured_m_edi_lx: float
    filtered_m_edi_lx: float
    bulb_brightness_pct: int  # 0..100
    bulb_cct_k: int  # 2200..6500
    monitor_brightness_pct: int  # 10..100
    monitor_cct_k: int  # 2300..6500
    shadow_rejected: bool
    unlinked_light_alert: bool
    alert_message: str
    compliance_status: str = "BELIRSIZ"  # "SAGLANDI" | "SAGLANMADI" | "BELIRSIZ"
    uncertainty_k2_lx: float = 0.0
    sensor_occluded: bool = False
    conflict_observer_active: bool = False
    reading_valid: bool = True

    @property
    def bulb_power(self) -> bool:
        """Convenience boolean matching SleepHandoffGuard context contract."""
        return self.bulb_brightness_pct > 0


@dataclass
class RoomProfile:
    """One-time feedforward optical characterization of the user's room & monitor.

    In `bezel_hybrid` mode, the sensor measures room + window ambient (+/-15%) while
    the Desktop Agent applies an active evening display guardrail (dimming to 15-25%
    @ 2300K, bounding worst-case white screen emission to ~3-6 lx m-EDI without any
    invasive screen pixel capture).
    """

    lamp_max_m_edi_lx: float = 45.0
    lamp_cct_shift_k: float = -350.0
    lamp_min_stable_pct: int = 5
    monitor_max_m_edi_lx: float = 42.0
    mount_mode: str = "bezel_hybrid"  # "bezel_hybrid" | "desk_stand"
    eye_to_bezel_ratio: float = 1.0  # optional alpha_eye/bezel spatial ratio from 15s cal

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "RoomProfile":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            lamp_max_m_edi_lx=float(data.get("lamp_max_m_edi_lx", 45.0)),
            lamp_cct_shift_k=float(data.get("lamp_cct_shift_k", -350.0)),
            lamp_min_stable_pct=int(data.get("lamp_min_stable_pct", 5)),
            monitor_max_m_edi_lx=float(data.get("monitor_max_m_edi_lx", 42.0)),
            mount_mode=str(data.get("mount_mode", "bezel_hybrid")),
            eye_to_bezel_ratio=float(data.get("eye_to_bezel_ratio", 1.0)),
        )


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _planck_relative(wavelength_nm: float, temp_k: float) -> float:
    """Normalized Planck blackbody spectral irradiance at wavelength_nm (relative to 550 nm)."""
    c2 = 1.438776877e7
    lam = wavelength_nm
    ref = 550.0
    val = (lam ** -5) / (math.exp(min(700.0, c2 / (lam * temp_k))) - 1.0)
    val_ref = (ref ** -5) / (math.exp(min(700.0, c2 / (ref * temp_k))) - 1.0)
    return val / val_ref


def _mccamy_cct(x: float, y: float) -> float:
    """McCamy (1992) cubic CCT approximation from CIE 1931 (x, y) chromaticity."""
    denom = 0.1858 - y
    if abs(denom) < 1e-6:
        return 6500.0
    n = _clamp((x - 0.3320) / denom, -1.6, 2.2)
    cct = (449.0 * (n ** 3)) + (3525.0 * (n ** 2)) + (6823.3 * n) + 5520.33
    return _clamp(cct, 1600.0, 12000.0)


class AS7343Converter:
    """Converts normalized OLGA-8 spectral counts (TCS3448 / AS7343 / AS7341) to CIE S 026 m-EDI.

    Default weights are derived from CIE 1931 2-deg CMFs and CIE S 026:2018 melanopic
    sensitivity (normalized so CIE D65 yields m-DER = 1.000). Propagates k=2 expanded
    uncertainty (+/-17%), <0.3 lx night uncertainty floor, and hardware saturation flags.
    """

    _DEFAULT_PHOTOPIC_SCALE: float = 100.0 / _D65_Y_INT
    _AS7341_PHOTOPIC_SCALE: float = 100.0 / _AS7341_D65_Y
    UNCERTAINTY_K2_REL: float = 0.17  # +/-17% k=2 expanded uncertainty (Model #4 metrology lock)
    NIGHT_FLOOR_LX: float = 0.30      # <0.3 lx measurement uncertainty floor

    def __init__(self, matrix_path: str | None = None) -> None:
        self.photopic_scale: float = self._DEFAULT_PHOTOPIC_SCALE
        self.channel_gains: dict[str, float] = {ch: 1.0 for ch, _, _ in _SPECTRAL_NODES}
        self.mlr_photopic: dict[str, float] | None = None
        self.mlr_m_edi: dict[str, float] | None = None

        if matrix_path is not None:
            self._load_calibration(Path(matrix_path))

    def _load_calibration(self, path: Path) -> None:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if "photopic_scale" in data:
            self.photopic_scale = float(data["photopic_scale"])
        if "channel_gains" in data and isinstance(data["channel_gains"], dict):
            for ch, gain in data["channel_gains"].items():
                if ch in self.channel_gains:
                    self.channel_gains[ch] = float(gain)
        if "mlr_photopic" in data and isinstance(data["mlr_photopic"], dict):
            self.mlr_photopic = {k: float(v) for k, v in data["mlr_photopic"].items()}
        if "mlr_m_edi" in data and isinstance(data["mlr_m_edi"], dict):
            self.mlr_m_edi = {k: float(v) for k, v in data["mlr_m_edi"].items()}

    def convert(self, sample: SpectralSample) -> PhotometricReading:
        ch = sample.channels
        is_as7341 = sample.chip.upper() == "AS7341"

        if is_as7341:
            e_spd = {
                name: max(0.0, float(ch.get(name, 0.0))) * self.channel_gains.get(name, 1.0)
                for name, _, _ in _AS7341_NODES
            }
            X = sum(e_spd[n] * _AS7341_X[n] * dw for n, _, dw in _AS7341_NODES) * _AS7341_X_SCALE
            Y = sum(e_spd[n] * _AS7341_Y[n] * dw for n, _, dw in _AS7341_NODES)
            Z = sum(e_spd[n] * _AS7341_Z[n] * dw for n, _, dw in _AS7341_NODES) * _AS7341_Z_SCALE
            M = sum(e_spd[n] * _AS7341_MEL[n] * dw for n, _, dw in _AS7341_NODES)
            scale_factor = self._AS7341_PHOTOPIC_SCALE * (
                self.photopic_scale / self._DEFAULT_PHOTOPIC_SCALE
            )
            mel_scale = _AS7341_MEL_SCALE
        else:
            e_spd = {
                name: max(0.0, float(ch.get(name, 0.0))) * self.channel_gains[name]
                for name, _, _ in _SPECTRAL_NODES
            }
            X = sum(e_spd[n] * _CIE_X[n] * dw for n, _, dw in _SPECTRAL_NODES) * _X_QUAD_SCALE
            Y = sum(e_spd[n] * _CIE_Y[n] * dw for n, _, dw in _SPECTRAL_NODES)
            Z = sum(e_spd[n] * _CIE_Z[n] * dw for n, _, dw in _SPECTRAL_NODES) * _Z_QUAD_SCALE
            M = sum(e_spd[n] * _CIE_MEL[n] * dw for n, _, dw in _SPECTRAL_NODES)
            scale_factor = self.photopic_scale
            mel_scale = _MEL_TO_D65_SCALE

        if self.mlr_photopic is not None:
            photopic_lx = max(
                0.0,
                sum(max(0.0, float(ch.get(k, 0.0))) * w for k, w in self.mlr_photopic.items()),
            )
        else:
            photopic_lx = max(0.0, Y * scale_factor)

        if self.mlr_m_edi is not None:
            m_edi_lx = max(
                0.0,
                sum(max(0.0, float(ch.get(k, 0.0))) * w for k, w in self.mlr_m_edi.items()),
            )
        else:
            m_edi_lx = max(0.0, M * mel_scale * scale_factor)

        m_der = (m_edi_lx / photopic_lx) if photopic_lx > 1e-4 else 0.0

        xyz_sum = X + Y + Z
        cct_k = _mccamy_cct(X / xyz_sum, Y / xyz_sum) if xyz_sum > 1e-9 else 2700.0

        flicker_val = max(0.0, float(ch.get("FLICKER", 0.0)))
        flicker_hz = flicker_val if 40.0 <= flicker_val <= 1000.0 else 0.0

        saturated = bool(sample.saturated)
        below_floor = (not saturated) and (
            photopic_lx < self.NIGHT_FLOOR_LX or m_edi_lx < self.NIGHT_FLOOR_LX
        )
        uncertainty_k2_lx = round(m_edi_lx * self.UNCERTAINTY_K2_REL, 2)

        return PhotometricReading(
            timestamp=sample.timestamp,
            m_edi_lx=round(m_edi_lx, 2),
            photopic_lx=round(photopic_lx, 2),
            cct_k=round(cct_k, 1),
            m_der=round(m_der, 3),
            flicker_hz=round(flicker_hz, 1),
            saturated=saturated,
            below_floor=below_floor,
            uncertainty_k2_lx=uncertainty_k2_lx,
        )


def _estimated_m_der_for_cct(cct_k: float) -> float:
    """Empirical standard-LED m-DER curve across 2200K..6500K (for active display guardrail)."""
    t = _clamp((cct_k - 2200.0) / (6500.0 - 2200.0), 0.0, 1.0)
    return 0.28 + 0.58 * (t ** 0.85)


class CircadianController:
    """Hardened 3-Layer Circadian Dosage Controller (CIE S 026 / Brown et al. 2022 / v6.1).

    Standards enforced (healthy daytime-active adults 18-55; visual-comfort framed):
    - DAY     [wake_hour, sleep_hour - 3h): Target >= 250 lx m-EDI (daylight harvest + boost)
    - EVENING [sleep_hour - 3h, sleep_hour): Target <= 10 lx m-EDI (or <= 5 lx high-sensitivity)
    - NIGHT   [sleep_hour, wake_hour):       Target <= 1 lx m-EDI (<0.3 lx floor assumed met)

    Key Kova-1 stability & safety locks:
    - BUG-1 Fix: Feedforward bulb-contribution subtraction + gain-normalized discrete
      integrator `e[k+1] = (1 - K*g)*e[k]` (K*g = 0.22) with anti-windup and 8-bit
      quantization deadband (>= 1.5x min_dimming_step_pct).
    - BUG-5a Fix: Saturated frames rejected from median filter (reading_valid=False).
    - >10s Persistent Occlusion Freeze: Never reports stale readings as live.
    - 3-State Compliance (k=2 +/-17%): "SAGLANDI" / "SAGLANMADI" / "BELIRSIZ".
    - 10-Min Conflict-Observer Backoff: Yields gracefully when external controllers act.
    """

    DAY_TARGET_M_EDI: float = 250.0
    EVENING_CAP_M_EDI: float = 10.0
    EVENING_HIGH_SENS_CAP_M_EDI: float = 5.0
    NIGHT_CAP_M_EDI: float = 1.0

    SHADOW_DROP_RATIO: float = 0.55        # >45% sudden dip treated as shadow/occlusion
    MAX_CONSECUTIVE_SHADOWS: int = 4       # ticks before escalating persistent cover
    HYSTERESIS_BAND: float = 0.12          # +/- 12% deadband
    UNCERTAINTY_K2_REL: float = 0.17       # +/- 17% k=2 expanded uncertainty

    def __init__(
        self,
        wake_hour: float = 7.0,
        sleep_hour: float = 23.0,
        profile: RoomProfile | None = None,
        high_sensitivity_evening: bool = False,
        occlusion_timeout_s: float = 10.0,
        loop_kg: float = 0.22,
        min_dimming_step_pct: int = 2,
        conflict_backoff_s: float = 600.0,
    ) -> None:
        self.wake_hour = wake_hour % 24.0
        self.sleep_hour = sleep_hour % 24.0
        self.profile = profile if profile is not None else RoomProfile()
        self.high_sensitivity_evening = bool(high_sensitivity_evening)
        self.occlusion_timeout_s = max(1.0, float(occlusion_timeout_s))
        self.loop_kg = _clamp(float(loop_kg), 0.10, 0.30)
        self.min_dimming_step_pct = max(1, int(min_dimming_step_pct))
        self.conflict_backoff_s = max(30.0, float(conflict_backoff_s))

        self._window: deque[float] = deque(maxlen=5)
        self._last_valid_m_edi: float | None = None
        self._consecutive_shadow_drops: int = 0
        self._occlusion_start_ts: float | None = None
        self._last_phase: str | None = None
        self._last_acted_m_edi: float | None = None

        # Actuator state & integrator memory (with explicit anti-windup bounds [0, 100])
        self._integrator_pct: float = 0.0
        self._prev_bulb_pct: int | None = None
        self._prev_bulb_cct: int | None = None
        self._last_bulb_pct: int = 0
        self._last_bulb_cct: int = 5200
        self._last_monitor_pct: int = 80
        self._last_monitor_cct: int = 5500
        self._has_commanded_bulb: bool = False

        # External-controller Conflict Observer state
        self._conflict_backoff_until_s: float = 0.0
        self._last_sample_ts: float = 0.0

    def classify_phase(self, hour_of_day: float) -> str:
        """Map 24h clock hour to DAY, EVENING (3h pre-sleep), or NIGHT (wrap-safe)."""
        h = hour_of_day % 24.0
        evening_start = (self.sleep_hour - 3.0) % 24.0

        def _in_interval(x: float, start: float, end: float) -> bool:
            if start < end:
                return start <= x < end
            return x >= start or x < end

        if _in_interval(h, evening_start, self.sleep_hour):
            return "EVENING"
        if _in_interval(h, self.wake_hour, evening_start):
            return "DAY"
        return "NIGHT"

    def notify_external_bulb_state(
        self,
        bulb_power: bool,
        bulb_brightness_pct: int,
        bulb_cct_k: int,
        now_s: float | None = None,
    ) -> bool:
        """Detect divergence from another controller (Apple Adaptive Lighting / Hue / user).

        If external state diverges meaningfully from our last commanded state, enters a
        10-minute (600 s) Conflict-Observer backoff so the daemon yields instead of
        fighting the external system. Returns True if conflict backoff was triggered.
        """
        if not self._has_commanded_bulb:
            return False

        t_now = float(now_s) if now_s is not None else (self._last_sample_ts or time.time())

        def _matches(target_pct: int, target_cct: int) -> bool:
            exp_power = target_pct > 0
            if bool(bulb_power) != exp_power:
                return False
            if exp_power:
                if abs(int(bulb_brightness_pct) - target_pct) >= 6:
                    return False
                if abs(int(bulb_cct_k) - target_cct) >= 200:
                    return False
            return True

        matches_current = _matches(self._last_bulb_pct, self._last_bulb_cct)
        matches_prev = (
            self._prev_bulb_pct is not None
            and self._prev_bulb_cct is not None
            and _matches(self._prev_bulb_pct, self._prev_bulb_cct)
        )

        if not (matches_current or matches_prev):
            self._conflict_backoff_until_s = t_now + self.conflict_backoff_s
            self._prev_bulb_pct = self._last_bulb_pct
            self._prev_bulb_cct = self._last_bulb_cct
            self._last_bulb_pct = int(_clamp(bulb_brightness_pct if bulb_power else 0, 0, 100))
            self._integrator_pct = float(self._last_bulb_pct)
            if bulb_cct_k > 0:
                self._last_bulb_cct = int(_clamp(bulb_cct_k, 2200, 6500))
            return True
        return False

    def _estimate_monitor_m_edi(
        self,
        monitor_brightness_pct: int,
        monitor_cct_k: int,
        monitor_apl: float,
    ) -> float:
        """Deterministic display guardrail estimate in bezel_hybrid mode (zero screen capture)."""
        if self.profile.mount_mode != "bezel_hybrid":
            return 0.0
        apl = _clamp(monitor_apl, 0.05, 1.0)
        bri = _clamp(monitor_brightness_pct / 100.0, 0.05, 1.0)
        m_der_ratio = _estimated_m_der_for_cct(monitor_cct_k) / _estimated_m_der_for_cct(6500.0)
        return self.profile.monitor_max_m_edi_lx * bri * apl * m_der_ratio

    def _apply_dimming_cliff(self, pct: int) -> int:
        if pct <= 0:
            return 0
        if pct < self.profile.lamp_min_stable_pct:
            return 0
        return int(_clamp(pct, 0, 100))

    def _apply_quantization_deadband(self, candidate_pct: int, prev_pct: int) -> int:
        """Prevent +/-1 LSB dither on 8-bit Matter (0x0008) / Hue low-brightness curves."""
        if candidate_pct == 0 or prev_pct == 0:
            return candidate_pct
        min_delta = math.ceil(1.5 * self.min_dimming_step_pct)
        if abs(candidate_pct - prev_pct) < min_delta:
            return prev_pct
        return candidate_pct

    def _compute_compliance(
        self,
        phase: str,
        eval_m_edi: float,
        target_m_edi: float,
        uncertainty_k2: float,
        reading_valid: bool,
        below_floor: bool,
    ) -> str:
        """3-State k=2 compliance decision rule: SAGLANDI / SAGLANMADI / BELIRSIZ."""
        if not reading_valid:
            return "BELIRSIZ"
        if phase == "NIGHT" and below_floor:
            return "SAGLANDI"
        if phase == "DAY":
            if (eval_m_edi - uncertainty_k2) >= target_m_edi:
                return "SAGLANDI"
            if (eval_m_edi + uncertainty_k2) < target_m_edi:
                return "SAGLANMADI"
            return "BELIRSIZ"
        # EVENING / NIGHT (upper cap <= target)
        if (eval_m_edi + uncertainty_k2) <= target_m_edi:
            return "SAGLANDI"
        if (eval_m_edi - uncertainty_k2) > target_m_edi:
            return "SAGLANMADI"
        return "BELIRSIZ"

    def step(
        self,
        reading: PhotometricReading,
        hour_of_day: float | None = None,
        monitor_apl: float = 0.5,
    ) -> ControlAction:
        if hour_of_day is None:
            now = datetime.now()
            hour_of_day = now.hour + (now.minute / 60.0)

        self._last_sample_ts = float(reading.timestamp)
        phase = self.classify_phase(hour_of_day)
        raw_m_edi = max(0.0, float(reading.m_edi_lx)) * max(0.25, self.profile.eye_to_bezel_ratio)
        phase_changed = self._last_phase is not None and phase != self._last_phase
        is_first_or_phase_change = (self._last_phase is None) or phase_changed

        target_m_edi = (
            self.DAY_TARGET_M_EDI
            if phase == "DAY"
            else (
                (self.EVENING_HIGH_SENS_CAP_M_EDI if self.high_sensitivity_evening else self.EVENING_CAP_M_EDI)
                if phase == "EVENING"
                else self.NIGHT_CAP_M_EDI
            )
        )

        conflict_active = (
            self._conflict_backoff_until_s > 0.0
            and reading.timestamp < self._conflict_backoff_until_s
        )

        # --- BUG-5a Lock: Hardware Saturation Guard ---
        # Never push saturated samples into the median buffer; hold last safe state.
        if reading.saturated:
            held_filtered = (
                statistics.median(self._window) if self._window else raw_m_edi
            )
            held_bulb = 0 if phase in ("EVENING", "NIGHT") else self._last_bulb_pct
            held_cct = 2300 if phase in ("EVENING", "NIGHT") else self._last_bulb_cct
            if self._has_commanded_bulb:
                self._prev_bulb_pct = self._last_bulb_pct
                self._prev_bulb_cct = self._last_bulb_cct
            self._last_phase = phase
            self._last_bulb_pct = held_bulb
            self._last_bulb_cct = held_cct
            return ControlAction(
                phase=phase,
                target_m_edi_lx=target_m_edi,
                measured_m_edi_lx=round(raw_m_edi, 2),
                filtered_m_edi_lx=round(held_filtered, 2),
                bulb_brightness_pct=held_bulb,
                bulb_cct_k=held_cct,
                monitor_brightness_pct=self._last_monitor_pct,
                monitor_cct_k=self._last_monitor_cct,
                shadow_rejected=False,
                unlinked_light_alert=False,
                alert_message="Sensor saturated — holding last safe output (reading not live)",
                compliance_status="BELIRSIZ",
                uncertainty_k2_lx=round(
                    reading.uncertainty_k2_lx if reading.uncertainty_k2_lx > 0 else raw_m_edi * self.UNCERTAINTY_K2_REL,
                    2,
                ),
                sensor_occluded=False,
                conflict_observer_active=conflict_active,
                reading_valid=False,
            )

        # --- Layer 2A: Transient Shadow Rejection + >10s Persistent Occlusion Freeze ---
        shadow_rejected = False
        sensor_occluded = False
        reading_valid = True

        if phase_changed:
            self._window.clear()
            self._consecutive_shadow_drops = 0
            self._occlusion_start_ts = None
            self._window.append(raw_m_edi)
            filtered_m_edi = raw_m_edi
            self._last_valid_m_edi = raw_m_edi
        elif (
            self._last_valid_m_edi is not None
            and self._last_valid_m_edi >= 15.0
            and raw_m_edi < (self._last_valid_m_edi * self.SHADOW_DROP_RATIO)
        ):
            if self._occlusion_start_ts is None:
                self._occlusion_start_ts = reading.timestamp
            self._consecutive_shadow_drops += 1
            elapsed_occ_s = max(0.0, reading.timestamp - self._occlusion_start_ts)

            shadow_rejected = True
            filtered_m_edi = (
                statistics.median(self._window) if self._window else self._last_valid_m_edi
            )
            if (
                elapsed_occ_s > self.occlusion_timeout_s
                or self._consecutive_shadow_drops > self.MAX_CONSECUTIVE_SHADOWS
            ):
                sensor_occluded = True
                reading_valid = False
        else:
            self._consecutive_shadow_drops = 0
            self._occlusion_start_ts = None
            self._window.append(raw_m_edi)
            filtered_m_edi = statistics.median(self._window)
            self._last_valid_m_edi = raw_m_edi

        # Persistent occlusion freeze: hold last safe lamp/monitor command, NEVER claim live
        if sensor_occluded:
            if self._has_commanded_bulb:
                self._prev_bulb_pct = self._last_bulb_pct
                self._prev_bulb_cct = self._last_bulb_cct
            self._last_phase = phase
            return ControlAction(
                phase=phase,
                target_m_edi_lx=target_m_edi,
                measured_m_edi_lx=round(raw_m_edi, 2),
                filtered_m_edi_lx=round(filtered_m_edi, 2),
                bulb_brightness_pct=self._last_bulb_pct,
                bulb_cct_k=self._last_bulb_cct,
                monitor_brightness_pct=self._last_monitor_pct,
                monitor_cct_k=self._last_monitor_cct,
                shadow_rejected=True,
                unlinked_light_alert=False,
                alert_message="Sensor occluded (>10s) — holding last safe state (reading not live)",
                compliance_status="BELIRSIZ",
                uncertainty_k2_lx=round(filtered_m_edi * self.UNCERTAINTY_K2_REL, 2),
                sensor_occluded=True,
                conflict_observer_active=conflict_active,
                reading_valid=False,
            )

        unlinked_light_alert = False
        alert_message = ""
        g_day = max(0.10, self.profile.lamp_max_m_edi_lx / 100.0)
        g_eve = max(0.05, g_day * 0.40)

        # --- Layer 2B & 3: Phase-Specific Optical Control (BUG-1 Gain-Normalized Integrator) ---
        if phase == "DAY":
            desired_room_cct = 5200.0
            bulb_cct = int(_clamp(round(desired_room_cct - self.profile.lamp_cct_shift_k), 2200, 6500))
            monitor_cct = 5500
            monitor_bri = 90 if filtered_m_edi >= 350.0 else 80

            mon_m_edi = self._estimate_monitor_m_edi(monitor_bri, monitor_cct, monitor_apl)
            effective_m_edi = filtered_m_edi + mon_m_edi
            eval_m_edi = effective_m_edi

            if conflict_active:
                bulb_bri = self._last_bulb_pct
                bulb_cct = self._last_bulb_cct
                alert_message = "Conflict observer active (10-min yield to external light controller)"
            elif shadow_rejected:
                bulb_bri = self._last_bulb_pct
                bulb_cct = self._last_bulb_cct if not is_first_or_phase_change else bulb_cct
            else:
                # BUG-1 Fix: Subtract estimated self-lamp contribution & apply gain-normalized
                # discrete integrator: e[k+1] = (1 - K*g)*e[k] with explicit anti-windup [0, 100].
                error_lx = target_m_edi - effective_m_edi
                est_external_m_edi = max(0.0, filtered_m_edi - (self._last_bulb_pct * g_day))

                if (est_external_m_edi + mon_m_edi) >= target_m_edi * (1.0 + self.HYSTERESIS_BAND):
                    # Bright daylight harvesting: fast clean off + anti-windup reset
                    self._integrator_pct = 0.0
                    bulb_bri = 0
                elif (est_external_m_edi + mon_m_edi) >= target_m_edi and error_lx <= 0.0:
                    self._integrator_pct = 0.0
                    bulb_bri = 0
                else:
                    # Feedforward-anchored gain-normalized update:
                    # u_new = clamp(u_prev + (K*g / g) * error_lx, 0, 100)
                    # => closed-loop residual decays monotonically as (1 - K*g)^k!
                    within_hysteresis = (
                        not is_first_or_phase_change
                        and self._last_acted_m_edi is not None
                        and abs(filtered_m_edi - self._last_acted_m_edi)
                        <= max(1.0, self._last_acted_m_edi) * self.HYSTERESIS_BAND
                        and abs(error_lx) <= target_m_edi * 0.08
                    )
                    if within_hysteresis:
                        bulb_bri = self._last_bulb_pct
                    else:
                        if is_first_or_phase_change and self._last_bulb_pct == 0:
                            # First daytime step: take a damped feedforward step without windup
                            ff_pct = _clamp((target_m_edi - (est_external_m_edi + mon_m_edi)) / g_day, 0.0, 100.0)
                            self._integrator_pct = _clamp(ff_pct * max(self.loop_kg, 0.5), 0.0, 100.0)
                        else:
                            delta_pct = (self.loop_kg / g_day) * error_lx
                            # Anti-windup: do not integrate further into saturation bounds
                            if (self._integrator_pct >= 100.0 and delta_pct > 0.0) or (
                                self._integrator_pct <= 0.0 and delta_pct < 0.0
                            ):
                                delta_pct = 0.0
                            self._integrator_pct = _clamp(self._integrator_pct + delta_pct, 0.0, 100.0)

                        raw_pct = self._apply_dimming_cliff(int(round(self._integrator_pct)))
                        bulb_bri = self._apply_quantization_deadband(raw_pct, self._last_bulb_pct)
                        self._last_acted_m_edi = filtered_m_edi

        elif phase == "EVENING":
            bulb_cct = 2300
            monitor_cct = 2300

            # Asymmetric fast-attack evening guard: upward jump above cap reacts immediately
            guard_m_edi = filtered_m_edi if shadow_rejected else max(filtered_m_edi, raw_m_edi)

            if guard_m_edi > target_m_edi:
                monitor_bri = 15
            elif guard_m_edi > (target_m_edi * 0.6):
                monitor_bri = 20
            else:
                monitor_bri = 25

            post_mon_m_edi = self._estimate_monitor_m_edi(monitor_bri, monitor_cct, monitor_apl)
            total_eye_m_edi = guard_m_edi + post_mon_m_edi
            eval_m_edi = total_eye_m_edi

            if conflict_active:
                bulb_bri = self._last_bulb_pct
                bulb_cct = self._last_bulb_cct
                alert_message = "Conflict observer active (10-min yield to external light controller)"
            elif guard_m_edi >= target_m_edi or total_eye_m_edi > target_m_edi:
                # STRICT NON-NEGATIVITY (u >= 0) + Anti-Windup Reset:
                # NEVER add warm bulb light to mask cool/bright ambient light!
                self._integrator_pct = 0.0
                bulb_bri = 0
                if total_eye_m_edi > target_m_edi:
                    unlinked_light_alert = True
                    alert_message = (
                        "Unlinked Light — Switch Off (eye-level m-EDI exceeds cap)"
                    )
            else:
                if shadow_rejected:
                    bulb_bri = self._last_bulb_pct
                else:
                    # Subtract own evening lamp estimate so cozy evening lamp doesn't self-oscillate
                    est_ext_eve = max(0.0, guard_m_edi - (self._last_bulb_pct * g_eve))
                    headroom = max(0.0, target_m_edi - (est_ext_eve + post_mon_m_edi))
                    desired_target_pct = _clamp((headroom * 0.45) / g_eve, 0.0, 25.0)
                    if is_first_or_phase_change:
                        self._integrator_pct = desired_target_pct
                    else:
                        self._integrator_pct = _clamp(
                            self._integrator_pct + self.loop_kg * (desired_target_pct - self._integrator_pct),
                            0.0,
                            25.0,
                        )
                    raw_pct = self._apply_dimming_cliff(int(round(self._integrator_pct)))
                    bulb_bri = self._apply_quantization_deadband(raw_pct, self._last_bulb_pct)
                self._last_acted_m_edi = guard_m_edi

        else:  # NIGHT
            bulb_cct = 2200
            monitor_cct = 2300
            monitor_bri = 10
            guard_m_edi = filtered_m_edi if shadow_rejected else max(filtered_m_edi, raw_m_edi)
            eval_m_edi = guard_m_edi

            if conflict_active:
                bulb_bri = self._last_bulb_pct
                bulb_cct = self._last_bulb_cct
                alert_message = "Conflict observer active (10-min yield to external light controller)"
            else:
                self._integrator_pct = 0.0
                bulb_bri = 0
                if guard_m_edi > target_m_edi and not reading.below_floor:
                    unlinked_light_alert = True
                    alert_message = "Unlinked Light — Switch Off (eye-level m-EDI exceeds cap)"
                elif reading.below_floor:
                    alert_message = "Night target assumed met (<0.3 lx measurement floor)"
            self._last_acted_m_edi = guard_m_edi

        uncertainty_k2 = round(eval_m_edi * self.UNCERTAINTY_K2_REL, 2)
        compliance_status = self._compute_compliance(
            phase=phase,
            eval_m_edi=eval_m_edi,
            target_m_edi=target_m_edi,
            uncertainty_k2=uncertainty_k2,
            reading_valid=reading_valid,
            below_floor=reading.below_floor,
        )

        if self._has_commanded_bulb:
            self._prev_bulb_pct = self._last_bulb_pct
            self._prev_bulb_cct = self._last_bulb_cct
        self._last_phase = phase
        self._last_bulb_pct = bulb_bri
        self._last_bulb_cct = bulb_cct
        self._last_monitor_pct = monitor_bri
        self._last_monitor_cct = monitor_cct
        if not conflict_active:
            self._has_commanded_bulb = True

        return ControlAction(
            phase=phase,
            target_m_edi_lx=target_m_edi,
            measured_m_edi_lx=round(raw_m_edi, 2),
            filtered_m_edi_lx=round(filtered_m_edi, 2),
            bulb_brightness_pct=bulb_bri,
            bulb_cct_k=bulb_cct,
            monitor_brightness_pct=monitor_bri,
            monitor_cct_k=monitor_cct,
            shadow_rejected=shadow_rejected,
            unlinked_light_alert=unlinked_light_alert,
            alert_message=alert_message,
            compliance_status=compliance_status,
            uncertainty_k2_lx=uncertainty_k2,
            sensor_occluded=False,
            conflict_observer_active=conflict_active,
            reading_valid=reading_valid,
        )


def _cosine_ease(u: float) -> float:
    """C1-continuous half-cosine ease in [0.0, 1.0] (zero derivative at endpoints)."""
    u_c = _clamp(u, 0.0, 1.0)
    return 0.5 * (1.0 - math.cos(math.pi * u_c))


def _lerp(start: float, end: float, factor: float) -> float:
    return start + (end - start) * factor


def kelvin_to_mired(cct_k: float) -> float:
    """Convert correlated color temperature (Kelvin) to reciprocal megakelvin (MIRED)."""
    return 1_000_000.0 / max(1000.0, float(cct_k))


def mired_to_kelvin(mired: float) -> float:
    """Convert reciprocal megakelvin (MIRED) back to Kelvin."""
    return 1_000_000.0 / max(1.0, float(mired))


def lerp_cct_mired(cct_a: float, cct_b: float, s: float) -> float:
    """Perceptually uniform MIRED-space interpolation between two CCT values (von Kries)."""
    f = _clamp(float(s), 0.0, 1.0)
    m_a = kelvin_to_mired(cct_a)
    m_b = kelvin_to_mired(cct_b)
    return mired_to_kelvin(_lerp(m_a, m_b, f))


def clamp_mired_step(
    prev_cct_k: float,
    target_cct_k: float,
    dt_s: float,
    max_mired_per_min: float = 4.5,
) -> float:
    """Rate-limit a CCT transition to <= `max_mired_per_min` (zero-JND von Kries ceiling)."""
    m_prev = kelvin_to_mired(prev_cct_k)
    m_tgt = kelvin_to_mired(target_cct_k)
    max_delta = max(0.0, float(dt_s) / 60.0) * max(0.01, float(max_mired_per_min))
    diff = m_tgt - m_prev
    if abs(diff) <= max_delta:
        return float(target_cct_k)
    return round(mired_to_kelvin(m_prev + math.copysign(max_delta, diff)), 1)


def compute_first_evening_ramp_step(
    elapsed_s: float,
    duration_s: float,
    start_nits: float,
    target_nits: float,
    start_cct_k: float,
    target_cct_k: float,
) -> tuple[float, int]:
    """Compute one step of the gentle 8-10s first-evening activation glide (log-nits + MIRED)."""
    dur = max(1e-6, float(duration_s))
    s = _cosine_ease(float(elapsed_s) / dur)
    n_a = max(1.0, float(start_nits))
    n_b = max(1.0, float(target_nits))
    cur_nits = math.exp(_lerp(math.log(n_a), math.log(n_b), s))
    cur_cct = lerp_cct_mired(start_cct_k, target_cct_k, s)
    return (round(cur_nits, 1), int(_clamp(round(cur_cct), 2200, 6500)))


PANEL_TYPES: tuple[str, ...] = ("wled_ips", "p3_ips", "woled", "qd_oled")
OLED_SAFE_MIN_NITS: float = 42.0

_PANEL_ALIASES: dict[str, str] = {
    "": "wled_ips",
    "auto": "wled_ips",
    "wled": "wled_ips",
    "wled_ips": "wled_ips",
    "ips": "wled_ips",
    "lcd": "wled_ips",
    "sdr_ips": "wled_ips",
    "standard": "wled_ips",
    "p3_ips": "p3_ips",
    "p3": "p3_ips",
    "ksf_ips": "p3_ips",
    "ksf": "p3_ips",
    "pfs_ips": "p3_ips",
    "dci_p3": "p3_ips",
    "wide_gamut_ips": "p3_ips",
    "woled": "woled",
    "wrgb": "woled",
    "wrgb_oled": "woled",
    "oled": "woled",
    "qd_oled": "qd_oled",
    "qdoled": "qd_oled",
    "qd_ips": "qd_oled",
    "qdips": "qd_oled",
    "quantum_dot": "qd_oled",
}

# CIE S 026:2018 independently verified m-DER anchors across (2200K, 2700K, 3400K, 4300K, 6500K)
_PANEL_M_DER_ANCHORS: dict[str, tuple[tuple[float, float], ...]] = {
    "wled_ips": ((2200.0, 0.242), (2700.0, 0.342), (3400.0, 0.461), (4300.0, 0.585), (6500.0, 0.790)),
    "p3_ips":   ((2200.0, 0.311), (2700.0, 0.406), (3400.0, 0.516), (4300.0, 0.626), (6500.0, 0.814)),
    "woled":    ((2200.0, 0.248), (2700.0, 0.337), (3400.0, 0.440), (4300.0, 0.550), (6500.0, 0.734)),
    "qd_oled":  ((2200.0, 0.403), (2700.0, 0.494), (3400.0, 0.609), (4300.0, 0.735), (6500.0, 0.949)),
}

# 1D-LUT white luminance retention Y_lut(cct, panel) when max(R,G,B)=1.0 at warm CCTs
_PANEL_Y_LUT_ANCHORS: dict[str, tuple[tuple[float, float], ...]] = {
    "wled_ips": ((2200.0, 0.354), (2700.0, 0.432), (3400.0, 0.539), (4300.0, 0.672), (6500.0, 1.000)),
    "p3_ips":   ((2200.0, 0.410), (2700.0, 0.490), (3400.0, 0.580), (4300.0, 0.710), (6500.0, 1.000)),
    "woled":    ((2200.0, 0.360), (2700.0, 0.440), (3400.0, 0.540), (4300.0, 0.680), (6500.0, 1.000)),
    "qd_oled":  ((2200.0, 0.576), (2700.0, 0.654), (3400.0, 0.745), (4300.0, 0.835), (6500.0, 1.000)),
}


def normalize_panel_type(panel_type: str | None) -> str:
    """Normalize user/config panel type strings into one of `PANEL_TYPES`."""
    raw = str(panel_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _PANEL_ALIASES.get(raw, "wled_ips")


def _interp_anchors(x: float, anchors: tuple[tuple[float, float], ...]) -> float:
    val = float(x)
    if val <= anchors[0][0]:
        return anchors[0][1]
    if val >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i + 1]
        if x0 <= val <= x1:
            frac = (val - x0) / max(1e-6, x1 - x0)
            return _lerp(y0, y1, frac)
    return anchors[-1][1]


def estimate_panel_m_der(cct_k: float, panel_type: str = "wled_ips") -> float:
    """Estimate CIE S 026 melanopic Daylight Efficacy Ratio (m-DER) for panel spectrum & CCT."""
    pt = normalize_panel_type(panel_type)
    return round(_interp_anchors(float(cct_k), _PANEL_M_DER_ANCHORS[pt]), 3)


def estimate_lut_luminance_retention(cct_k: float, panel_type: str = "wled_ips") -> float:
    """Estimate fractional white luminance retained under a 1D warm LUT (`R=1.0` ceiling)."""
    pt = normalize_panel_type(panel_type)
    return round(_interp_anchors(float(cct_k), _PANEL_Y_LUT_ANCHORS[pt]), 3)


def compute_projected_solid_angle_sr(
    width_mm: float,
    height_mm: float,
    viewing_dist_mm: float = 580.0,
) -> float:
    """Compute exact projected corneal solid angle `Omega_proj = integral(cos(theta) dOmega)` in sr.

    Returns ~0.225 sr for a 16\" 16:10 laptop (345x215 mm @ 550 mm) and ~0.460 sr for a
    27\" 16:9 desktop monitor (598x336 mm @ 600 mm). Safely falls back to 0.35 on invalid dims.
    """
    try:
        w = float(width_mm)
        h = float(height_mm)
        d = float(viewing_dist_mm)
    except (TypeError, ValueError):
        return 0.35
    if (
        not (math.isfinite(w) and math.isfinite(h) and math.isfinite(d))
        or w < 50.0
        or h < 50.0
        or d < 150.0
        or w > 3000.0
        or h > 2000.0
    ):
        return 0.35
    hw, hh = w * 0.5, h * 0.5
    r1 = math.sqrt((d * d) + (hh * hh))
    r2 = math.sqrt((d * d) + (hw * hw))
    omega = 2.0 * ((hh / r1) * math.atan(hw / r1) + (hw / r2) * math.atan(hh / r2))
    return round(_clamp(omega, 0.05, 1.8), 4)


def estimate_corneal_m_edi_lx(
    emitted_nits: float,
    cct_k: float,
    *,
    panel_type: str = "wled_ips",
    solid_angle_sr: float = 0.35,
    dark_mode: bool | None = None,
) -> float:
    """Estimate eye-level melanopic EDI (`lx`) from emitted white nits, panel m-DER, solid angle & theme.

    Uses bounded `APL_eff` (`0.35` Dark Mode, `0.75` Light Mode, `0.50` unknown — ~2.1x cap
    accounting for Watson & Yellott pupil dilation and mixed white browser windows).
    """
    apl_eff = 0.35 if dark_mode is True else (0.75 if dark_mode is False else 0.50)
    m_der = estimate_panel_m_der(cct_k, panel_type)
    om = _clamp(float(solid_angle_sr), 0.05, 1.8)
    return round(max(0.0, float(emitted_nits)) * apl_eff * om * m_der, 2)


def nits_to_backlight_pct(
    target_nits: float,
    max_nits: float = 300.0,
    min_nits: float | None = None,
    gamma: float = 2.0,
    floor_pct: int = 12,
    *,
    cct_k: float | None = None,
    panel_type: str = "wled_ips",
    compensate_lut: bool = False,
) -> int:
    """Invert a display's perceptual/linear backlight transfer curve `L(p)` to a `0..100%` command.

    Uses `L(p) = L_min + (L_max - L_min) * (p/100)^gamma`:
    - Windows 10/11 internal laptop WMI backlights follow roughly `gamma ~= 2.0`.
    - External SDR DDC/CI VCP 0x10 monitors are closer to linear (`gamma ~= 1.2`).
    - When `compensate_lut=True` and `cct_k` is given, divides by `Y_lut(cct_k, panel_type)`
      to eliminate double-dimming from the warm GPU color matrix.
    - When `panel_type` is `"woled"` or `"qd_oled"`, enforces `OLED_SAFE_MIN_NITS` (42 nits)
      hardware floor to prevent near-black chrominance shift and deep PWM flicker.
    """
    mx = max(50.0, float(max_nits))
    mn = max(15.0, 0.06 * mx) if min_nits is None else max(0.0, float(min_nits))
    if mn >= mx:
        mn = max(0.0, 0.06 * mx)
    g = max(0.5, float(gamma))
    pt = normalize_panel_type(panel_type)

    eff_target = float(target_nits)
    if compensate_lut and cct_k is not None:
        y_lut = max(0.30, estimate_lut_luminance_retention(cct_k, pt))
        eff_target = min(mx, eff_target / y_lut)
    if pt in ("woled", "qd_oled"):
        eff_target = max(eff_target, min(mx, OLED_SAFE_MIN_NITS))

    clamped = _clamp(eff_target, mn, mx)
    ratio = (clamped - mn) / max(1.0, mx - mn)
    pct = int(round(100.0 * (ratio ** (1.0 / g))))
    fl = int(_clamp(floor_pct, 0, 100))
    return int(_clamp(pct, fl, 100))


def backlight_pct_to_nits(
    pct: int,
    max_nits: float = 300.0,
    min_nits: float | None = None,
    gamma: float = 2.0,
) -> float:
    """Forward display luminance estimate `L(p)` in `cd/m²` (nits) for a backlight percentage."""
    mx = max(50.0, float(max_nits))
    mn = max(15.0, 0.06 * mx) if min_nits is None else max(0.0, float(min_nits))
    if mn >= mx:
        mn = max(0.0, 0.06 * mx)
    g = max(0.5, float(gamma))
    frac = _clamp(float(pct), 0.0, 100.0) / 100.0
    return round(mn + (mx - mn) * (frac ** g), 1)


def compute_smooth_open_loop_targets(
    hour_of_day: float,
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
) -> dict[str, Any]:
    """Compute a continuous minute-by-minute MIRED & nit-normalized C1 circadian curve (zero sensor).

    Uses wraparound-safe elapsed hours since `wake_hour` and perceptually uniform MIRED-space
    CCT interpolation (`1e6 / T`), panel-aware `m-DER` & `Y_lut` compensation, and bounded
    `AppsUseLightTheme` Dark/Light Mode corneal `m-EDI` estimation.
    """
    wake = float(wake_hour) % 24.0
    sleep = float(sleep_hour) % 24.0
    h = float(hour_of_day) % 24.0

    awake_h = (sleep - wake) % 24.0
    if awake_h <= 1e-6:
        awake_h = 16.0
    t = (h - wake) % 24.0

    pt = normalize_panel_type(panel_type)
    om = 0.35 if solid_angle_sr is None else round(_clamp(float(solid_angle_sr), 0.05, 1.8), 4)
    apl_eff = 0.35 if dark_mode is True else (0.75 if dark_mode is False else 0.50)

    # Phase & optical reference target matching CircadianController.classify_phase()
    evening_start_t = max(0.0, awake_h - 3.0)
    eve_target_lx = (
        CircadianController.EVENING_HIGH_SENS_CAP_M_EDI
        if high_sensitivity_evening
        else CircadianController.EVENING_CAP_M_EDI
    )
    if t < evening_start_t:
        phase = "DAY"
        target_m_edi_lx = CircadianController.DAY_TARGET_M_EDI
    elif t < awake_h:
        phase = "EVENING"
        target_m_edi_lx = eve_target_lx
    else:
        phase = "NIGHT"
        target_m_edi_lx = CircadianController.NIGHT_CAP_M_EDI

    strict = bool(high_sensitivity_evening) or (not bool(comfort_mode))
    curve_profile = "strict" if strict else "comfort"
    floor_pct = 10 if strict else (25 if float(backlight_gamma) >= 1.8 else 15)

    # Non-overlapping continuous solar/PRC milestones along elapsed waking time t
    sunrise_end = min(1.5, max(0.0, awake_h - 3.5))
    sunset_start = max(sunrise_end, awake_h - 3.5)
    solar_peak_t = min(5.0, max(sunrise_end, sunrise_end + (sunset_start - sunrise_end) * 0.32))
    sunset_end = max(sunset_start, awake_h - 1.5)
    winddown_label_start = max(sunset_end, awake_h - 0.5)
    night_start = awake_h

    if not strict:
        # Comfort Mode: panel-aware evening CCT (QD-OLED -350K / P3 -150K; Dark Mode +150K cleaner cap)
        panel_eve_shift = -350.0 if pt == "qd_oled" else (-150.0 if pt == "p3_ips" else 0.0)
        dark_eve_shift = 150.0 if dark_mode is True else 0.0
        eve_mid_cct = _clamp(3200.0 + panel_eve_shift + dark_eve_shift, 2500.0, 3400.0)
        eve_end_cct = _clamp(3000.0 + panel_eve_shift + dark_eve_shift, 2400.0, 3200.0)

        if t < sunrise_end and sunrise_end > 1e-6:
            s = _cosine_ease(t / sunrise_end)
            target_nits = _lerp(55.0, 195.0, s)
            mon_cct = lerp_cct_mired(3400.0, 5200.0, s)
            bulb_pct = _lerp(30.0, 92.0, s)
            bulb_cct = lerp_cct_mired(3000.0, 4600.0, s)
            transition_label = "sunrise_ramp"
        elif t < solar_peak_t and (solar_peak_t - sunrise_end) > 1e-6:
            s = _cosine_ease((t - sunrise_end) / (solar_peak_t - sunrise_end))
            target_nits = _lerp(195.0, 220.0, s)
            mon_cct = lerp_cct_mired(5200.0, 5800.0, s)
            bulb_pct = _lerp(92.0, 100.0, s)
            bulb_cct = lerp_cct_mired(4600.0, 5000.0, s)
            transition_label = "day"
        elif t < sunset_start and (sunset_start - solar_peak_t) > 1e-6:
            s = _cosine_ease((t - solar_peak_t) / (sunset_start - solar_peak_t))
            target_nits = _lerp(220.0, 155.0, s)
            mon_cct = lerp_cct_mired(5800.0, 4300.0, s)
            bulb_pct = _lerp(100.0, 72.0, s)
            bulb_cct = lerp_cct_mired(5000.0, 4000.0, s)
            transition_label = "afternoon"
        elif t < sunset_end and (sunset_end - sunset_start) > 1e-6:
            s = _cosine_ease((t - sunset_start) / (sunset_end - sunset_start))
            target_nits = _lerp(155.0, 75.0, s)
            mon_cct = lerp_cct_mired(4300.0, eve_mid_cct, s)
            bulb_pct = _lerp(72.0, 15.0, s)
            bulb_cct = lerp_cct_mired(4000.0, 2700.0, s)
            transition_label = "sunset_ramp"
        elif t < night_start and (night_start - sunset_end) > 1e-6:
            s = _cosine_ease((t - sunset_end) / (night_start - sunset_end))
            target_nits = _lerp(75.0, 55.0, s)
            mon_cct = lerp_cct_mired(eve_mid_cct, eve_end_cct, s)
            bulb_cct = 2700.0
            if t < winddown_label_start:
                sb = _cosine_ease((t - sunset_end) / max(1e-6, winddown_label_start - sunset_end))
                bulb_pct = _lerp(15.0, 12.0, sb)
                transition_label = "evening"
            else:
                sb = _cosine_ease((t - winddown_label_start) / max(1e-6, night_start - winddown_label_start))
                bulb_pct = _lerp(12.0, 0.0, sb)
                transition_label = "night_winddown"
        else:
            target_nits = 50.0
            mon_cct = eve_end_cct
            bulb_pct = 0.0
            bulb_cct = 2700.0
            transition_label = "night"
    else:
        # Strict Circadian Mode (high_sensitivity_evening=True or comfort_mode=False)
        eve_mid_cct = 2200.0 if high_sensitivity_evening else 2350.0
        eve_bulb_pct = 8.0 if high_sensitivity_evening else 12.0
        eve_bulb_cct = 2200.0 if high_sensitivity_evening else 2300.0

        if t < sunrise_end and sunrise_end > 1e-6:
            s = _cosine_ease(t / sunrise_end)
            target_nits = _lerp(55.0, 195.0, s)
            mon_cct = lerp_cct_mired(3200.0, 5400.0, s)
            bulb_pct = _lerp(30.0, 92.0, s)
            bulb_cct = lerp_cct_mired(3000.0, 4600.0, s)
            transition_label = "sunrise_ramp"
        elif t < solar_peak_t and (solar_peak_t - sunrise_end) > 1e-6:
            s = _cosine_ease((t - sunrise_end) / (solar_peak_t - sunrise_end))
            target_nits = _lerp(195.0, 220.0, s)
            mon_cct = lerp_cct_mired(5400.0, 6000.0, s)
            bulb_pct = _lerp(92.0, 100.0, s)
            bulb_cct = lerp_cct_mired(4600.0, 5000.0, s)
            transition_label = "day"
        elif t < sunset_start and (sunset_start - solar_peak_t) > 1e-6:
            s = _cosine_ease((t - solar_peak_t) / (sunset_start - solar_peak_t))
            target_nits = _lerp(220.0, 155.0, s)
            mon_cct = lerp_cct_mired(6000.0, 4300.0, s)
            bulb_pct = _lerp(100.0, 70.0, s)
            bulb_cct = lerp_cct_mired(5000.0, 4000.0, s)
            transition_label = "afternoon"
        elif t < sunset_end and (sunset_end - sunset_start) > 1e-6:
            s = _cosine_ease((t - sunset_start) / (sunset_end - sunset_start))
            target_nits = _lerp(155.0, 65.0, s)
            mon_cct = lerp_cct_mired(4300.0, eve_mid_cct, s)
            bulb_pct = _lerp(70.0, eve_bulb_pct, s)
            bulb_cct = lerp_cct_mired(4000.0, eve_bulb_cct, s)
            transition_label = "sunset_ramp"
        elif t < night_start and (night_start - sunset_end) > 1e-6:
            s = _cosine_ease((t - sunset_end) / (night_start - sunset_end))
            target_nits = _lerp(65.0, 42.0, s)
            mon_cct = 2200.0 if high_sensitivity_evening else lerp_cct_mired(2350.0, 2200.0, s)
            if t < winddown_label_start:
                bulb_pct = eve_bulb_pct
                bulb_cct = eve_bulb_cct
                transition_label = "evening"
            else:
                sb = _cosine_ease((t - winddown_label_start) / max(1e-6, night_start - winddown_label_start))
                bulb_pct = _lerp(eve_bulb_pct, 0.0, sb)
                bulb_cct = lerp_cct_mired(eve_bulb_cct, 2200.0, sb)
                transition_label = "night_winddown"
        else:
            target_nits = 30.0
            mon_cct = 2200.0
            bulb_pct = 0.0
            bulb_cct = 2200.0
            transition_label = "night"

    mon_cct_int = int(_clamp(round(mon_cct), 2200, 6500))
    y_lut = estimate_lut_luminance_retention(mon_cct_int, pt)
    mon_pct_int = nits_to_backlight_pct(
        target_nits=target_nits,
        max_nits=display_max_nits,
        gamma=backlight_gamma,
        floor_pct=floor_pct,
        cct_k=mon_cct_int,
        panel_type=pt,
        compensate_lut=compensate_lut,
    )
    hw_nits = backlight_pct_to_nits(mon_pct_int, max_nits=display_max_nits, gamma=backlight_gamma)
    emitted_nits = hw_nits * y_lut
    est_corneal_m_edi = estimate_corneal_m_edi_lx(
        emitted_nits=emitted_nits,
        cct_k=mon_cct_int,
        panel_type=pt,
        solid_angle_sr=om,
        dark_mode=dark_mode,
    )
    bulb_pct_int = int(_clamp(round(bulb_pct), 0, 100))
    return {
        "phase": phase,
        "target_m_edi_lx": float(target_m_edi_lx),
        "target_screen_nits": round(target_nits, 1),
        "curve_profile": curve_profile,
        "monitor_brightness_pct": mon_pct_int,
        "monitor_cct_k": mon_cct_int,
        "bulb_power": bulb_pct_int > 0,
        "bulb_brightness_pct": bulb_pct_int,
        "bulb_cct_k": int(_clamp(round(bulb_cct), 2200, 6500)),
        "transition_label": transition_label,
        "estimated_corneal_m_edi_lx": est_corneal_m_edi,
        "panel_type": pt,
        "lut_retention": round(y_lut, 3),
        "solid_angle_sr": om,
        "dark_mode_apl_eff": apl_eff,
    }


evaluate_circadian_curve = compute_smooth_open_loop_targets


WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MOVIE_MODE_CCT_K: int = 3400
MOVIE_MODE_DURATION_S: float = 9000.0  # 2.5 hours


def compute_movie_mode_target(
    max_nits: float = 300.0,
    gamma: float = 2.0,
) -> tuple[int, int, float]:
    """Return `(brightness_pct, MOVIE_MODE_CCT_K, target_nits)` for comfortable 3400K Movie Mode."""
    mx = max(50.0, float(max_nits))
    lower_bound = 90.0 if mx >= 180.0 else (mx * 0.5)
    target_nits = max(lower_bound, min(mx * 0.65, 150.0))
    target_nits = min(target_nits, mx)
    pct = nits_to_backlight_pct(
        target_nits=target_nits,
        max_nits=mx,
        gamma=gamma,
        floor_pct=20,
    )
    return (pct, MOVIE_MODE_CCT_K, round(target_nits, 1))


def _extract_day_wake_sleep(
    weekly_schedule: dict[str, dict[str, float]] | None,
    day_key: str,
    default_wake: float,
    default_sleep: float,
) -> tuple[float, float]:
    if not weekly_schedule or not isinstance(weekly_schedule, dict):
        return (float(default_wake) % 24.0, float(default_sleep) % 24.0)
    entry = weekly_schedule.get(day_key)
    if not isinstance(entry, dict):
        return (float(default_wake) % 24.0, float(default_sleep) % 24.0)
    raw_wake = entry.get("wake", entry.get("wake_hour", default_wake))
    raw_sleep = entry.get("sleep", entry.get("sleep_hour", default_sleep))
    try:
        w = float(raw_wake) % 24.0
    except (TypeError, ValueError):
        w = float(default_wake) % 24.0
    try:
        s = float(raw_sleep) % 24.0
    except (TypeError, ValueError):
        s = float(default_sleep) % 24.0
    return (w, s)


def resolve_biological_schedule(
    now_dt: datetime,
    default_wake: float = 7.0,
    default_sleep: float = 23.0,
    weekly_schedule: dict[str, dict[str, float]] | None = None,
) -> tuple[float, float, str, float]:
    """Resolve wake-anchored biological-day schedule without any midnight step jump.

    If `now_dt` falls before today's `wake_hour`, it belongs to yesterday's biological
    night (`effective_sleep = yesterday.sleep_hour`, `effective_wake = today.wake_hour`,
    `active_day_key = yesterday_key`). Otherwise it uses today's `(wake, sleep, today_key)`.

    Returns `(effective_wake_hour, effective_sleep_hour, active_day_key, social_jetlag_hours)`
    where `social_jetlag_hours` is the maximum circular sleep-midpoint difference (0..12h)
    across configured weekdays (0.0 if uniform or empty).
    """
    today_idx = now_dt.weekday() % 7
    today_key = WEEKDAY_KEYS[today_idx]
    yest_key = WEEKDAY_KEYS[(today_idx - 1) % 7]

    today_wake, today_sleep = _extract_day_wake_sleep(
        weekly_schedule, today_key, default_wake, default_sleep
    )
    _, yest_sleep = _extract_day_wake_sleep(
        weekly_schedule, yest_key, default_wake, default_sleep
    )

    hour = now_dt.hour + (now_dt.minute / 60.0) + (now_dt.second / 3600.0)
    if hour < today_wake:
        effective_wake = today_wake
        effective_sleep = yest_sleep
        active_day_key = yest_key
    else:
        effective_wake = today_wake
        effective_sleep = today_sleep
        active_day_key = today_key

    # Compute social jetlag = max circular sleep-midpoint spread across configured days
    social_jetlag = 0.0
    if weekly_schedule and isinstance(weekly_schedule, dict):
        midpoints: list[float] = []
        for dk in WEEKDAY_KEYS:
            if dk in weekly_schedule and isinstance(weekly_schedule[dk], dict):
                dw, ds = _extract_day_wake_sleep(weekly_schedule, dk, default_wake, default_sleep)
                sleep_dur = (dw - ds) % 24.0
                if sleep_dur <= 1e-6:
                    sleep_dur = 8.0
                midpoints.append((ds + (sleep_dur * 0.5)) % 24.0)
        if len(midpoints) >= 2:
            max_diff = 0.0
            for i in range(len(midpoints)):
                for j in range(i + 1, len(midpoints)):
                    raw_d = abs(midpoints[i] - midpoints[j])
                    circ_d = min(raw_d, 24.0 - raw_d)
                    if circ_d > max_diff:
                        max_diff = circ_d
            social_jetlag = round(max_diff, 2)

    return (effective_wake, effective_sleep, active_day_key, social_jetlag)


def sample_24h_curve(
    wake_hour: float = 7.0,
    sleep_hour: float = 23.0,
    high_sensitivity_evening: bool = False,
    comfort_mode: bool = True,
    display_max_nits: float = 300.0,
    backlight_gamma: float = 2.0,
    step_minutes: int = 30,
    include_end: bool = False,
    panel_type: str = "wled_ips",
    solid_angle_sr: float | None = None,
    dark_mode: bool | None = None,
    compensate_lut: bool = True,
) -> list[dict[str, Any]]:
    """Sample 24-hour curve points (`step_minutes=30` -> 48 points; `include_end=True` adds 24.0)."""
    step_m = max(5, min(60, int(step_minutes)))
    n_steps = max(1, int(round(1440.0 / step_m)))
    points: list[dict[str, Any]] = []
    for idx in range(n_steps):
        hr = round((idx * step_m) / 60.0, 4)
        if hr >= 24.0:
            break
        tgt = compute_smooth_open_loop_targets(
            hour_of_day=hr,
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
        points.append(
            {
                "hour": hr,
                "phase": tgt["phase"],
                "target_screen_nits": tgt["target_screen_nits"],
                "monitor_brightness_pct": tgt["monitor_brightness_pct"],
                "monitor_cct_k": tgt["monitor_cct_k"],
                "estimated_corneal_m_edi_lx": tgt["estimated_corneal_m_edi_lx"],
            }
        )
    if include_end and points:
        closing = dict(points[0])
        closing["hour"] = 24.0
        points.append(closing)
    return points


def _synthesize_spd_channels(
    photopic_target_lx: float,
    rel_weights: dict[str, float],
    flicker_hz: float = 0.0,
) -> dict[str, float]:
    """Build physically exact AS7343/TCS3448 normalized counts for a given relative SPD & lux."""
    y_raw = sum(rel_weights[n] * _CIE_Y[n] * dw for n, _, dw in _SPECTRAL_NODES)
    scale = 0.0 if y_raw <= 1e-12 else photopic_target_lx / (y_raw * AS7343Converter._DEFAULT_PHOTOPIC_SCALE)

    out: dict[str, float] = {}
    for n, _, _ in _SPECTRAL_NODES:
        out[n] = round(max(0.0, rel_weights[n] * scale), 5)

    out["FY"] = round(out["F5"] * 1.02, 5)
    out["NIR"] = round(out["F8"] * 0.45, 5)
    out["CLEAR"] = round(sum(out[n] for n, _, _ in _SPECTRAL_NODES) * 0.38, 5)
    out["FLICKER"] = round(flicker_hz, 1)
    return out


def _blackbody_weights(temp_k: float, cyan_dip: float = 0.0, blue_bump: float = 0.0) -> dict[str, float]:
    """Generate realistic white-source relative SPD weights across OLGA-8 spectral bands."""
    w: dict[str, float] = {}
    for name, lam, _ in _SPECTRAL_NODES:
        val = _planck_relative(lam, temp_k)
        if name == "F3" and cyan_dip > 0.0:
            val *= (1.0 - cyan_dip)
        if name == "FZ" and blue_bump > 0.0:
            val *= (1.0 + blue_bump)
        w[name] = max(0.0, val)
    return w


class SyntheticSensorStream:
    """Physically grounded synthetic OLGA-8 sensor stream for Day-1 bench simulation."""

    VALID_SCENARIOS: tuple[str, ...] = (
        "overcast_day",
        "bright_window_harvest",
        "evening_monitor_glare",
        "evening_unlinked_neon",
        "hand_shadow_test",
        "day_to_evening_demo",
    )

    def __init__(self, scenario: str = "overcast_day", total_ticks: int = 8) -> None:
        if scenario not in self.VALID_SCENARIOS:
            raise ValueError(
                f"Unknown scenario '{scenario}'. Valid scenarios: {', '.join(self.VALID_SCENARIOS)}"
            )
        self.scenario = scenario
        self.total_ticks = max(2, int(total_ticks))
        self._tick: int = 0

    def next_sample(self) -> SpectralSample:
        t = time.time()
        i = self._tick
        self._tick += 1

        ripple = 1.0 + 0.015 * math.sin(i * 1.1)

        if self.scenario == "overcast_day":
            w = _blackbody_weights(5200.0, cyan_dip=0.0, blue_bump=0.02)
            channels = _synthesize_spd_channels(258.0 * ripple, w)

        elif self.scenario == "bright_window_harvest":
            w = _blackbody_weights(5900.0, cyan_dip=0.0, blue_bump=0.03)
            channels = _synthesize_spd_channels(475.0 * ripple, w)

        elif self.scenario == "evening_monitor_glare":
            w = _blackbody_weights(6300.0, cyan_dip=0.45, blue_bump=0.22)
            channels = _synthesize_spd_channels(38.0 * ripple, w, flicker_hz=240.0)

        elif self.scenario == "evening_unlinked_neon":
            w = _blackbody_weights(2300.0, cyan_dip=0.10, blue_bump=0.0)
            w["FZ"] += 1.9
            w["F3"] += 4.2
            w["F4"] += 0.7
            channels = _synthesize_spd_channels(32.0 * ripple, w, flicker_hz=100.0)

        elif self.scenario == "hand_shadow_test":
            w = _blackbody_weights(5200.0, cyan_dip=0.0, blue_bump=0.02)
            lux = 65.0 if i in (3, 4) else (268.0 * ripple)
            channels = _synthesize_spd_channels(lux, w)

        else:  # "day_to_evening_demo"
            frac = min(1.0, i / float(max(1, self.total_ticks - 1)))
            if frac < 0.65:
                s = frac / 0.65
                if s <= 0.45:
                    u = s / 0.45
                    cct = 5200.0 + 600.0 * u
                    lux = 260.0 + 160.0 * u
                else:
                    u = (s - 0.45) / 0.55
                    cct = 5800.0 - 1500.0 * u
                    lux = 420.0 - 180.0 * u
                w = _blackbody_weights(cct, cyan_dip=0.04 * s, blue_bump=0.02)
                channels = _synthesize_spd_channels(lux, w)
            elif frac < 0.90:
                w = _blackbody_weights(2300.0, cyan_dip=0.10, blue_bump=0.0)
                channels = _synthesize_spd_channels(18.0, w)
            else:
                w = _blackbody_weights(2300.0, cyan_dip=0.10, blue_bump=0.0)
                w["FZ"] += 1.9
                w["F3"] += 4.2
                w["F4"] += 0.7
                channels = _synthesize_spd_channels(32.0, w, flicker_hz=100.0)

        return SpectralSample(timestamp=t, channels=channels, saturated=False, chip="TCS3448")
