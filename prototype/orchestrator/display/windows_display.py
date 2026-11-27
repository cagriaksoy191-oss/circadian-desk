"""Windows hardware backlight (Laptop WMI + External DDC/CI) + GPU Color Transform + OS Sleep Handoff.

Uses Python standard-library `ctypes` (`dxva2.dll`, `gdi32.dll`, `Magnification.dll`, `user32.dll`)
and native Windows WMI (`WmiMonitorBrightness` / `WmiSetBrightness`) with zero external
dependencies, zero admin rights, zero Registry hacks, and zero screen pixel capture
(NO DXGI / ScreenCaptureKit).

v6.1 Kova-1 + Gün-2 Real-Hardware Hardening:
- Native Windows Laptop WMI Hardware Backlight (`WmiMonitorBrightness`) alongside
  external monitor DDC/CI (`VCP 0x10`) for true hardware dimming without contrast crush.
- Zero-Admin Stock-Windows `Magnification.dll` (`MagSetFullscreenColorEffect` 5x5 GPU
  color matrix) automatic fallback whenever `gdi32.SetDeviceGammaRamp` is rejected by
  stock Windows' `GdiIcmGammaRange` (<0.50 RGB scalar clamp on 3000K/2300K) — zero
  Registry edit, zero reboot, applies seamlessly across ALL monitors simultaneously!
- Defensive Write-Safety Guard (BUG-4): NEVER mutates any WMI or DDC/CI display whose
  original brightness could not be read and backed up first; deterministically restores
  GDI gamma LUT, Magnification identity + `MagUninitialize()`, DDC/CI, AND WMI brightness.
- Phase- and alert-aware `SleepHandoffGuard` + `SIGINT` fix (BUG-2).
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable

from orchestrator.lights.base import LightDriver

logger = logging.getLogger(__name__)

WM_DISPLAYCHANGE = 0x007E
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_POWERSETTINGCHANGE = 0x8013
GUID_CONSOLE_DISPLAY_STATE_STR = "6fe69556-704a-47a0-8f24-c28d936fda47"
VCP_LUMINANCE_CODE = 0x10
CREATE_NO_WINDOW = 0x08000000
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "CircadianDesk"

# Windows 11 24H2/25H2/26H2 DisplayConfig GET_ADVANCED_COLOR_INFO_2 (verified live on Build 26300)
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2 = 15
DISPLAYCONFIG_ADVANCED_COLOR_MODE_SDR = 0
DISPLAYCONFIG_ADVANCED_COLOR_MODE_WCG = 1
DISPLAYCONFIG_ADVANCED_COLOR_MODE_HDR = 2

# Strictly whitelisted Windows Settings URIs (zero arbitrary URI execution)
WINDOWS_SETTINGS_URI_WHITELIST: dict[str, str] = {
    "nightlight": "ms-settings:nightlight",
    "colorfilter": "ms-settings:easeofaccess-colorfilter",
    "display": "ms-settings:display",
    "startupapps": "ms-settings:startupapps",
}
COMPETING_COLOR_APPS: tuple[str, ...] = (
    "flux.exe",
    "f.lux.exe",
    "lightbulb.exe",
    "iris.exe",
    "sunsetscreen.exe",
    "careueyes.exe",
)

IDENTITY_COLOR_MATRIX: list[float] = [
    1.0, 0.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 0.0, 1.0,
]


def kelvin_to_rgb_scalars(cct_k: int) -> tuple[float, float, float]:
    """Compute normalized [0.0..1.0] RGB channel multipliers using Tanner Helland's curve.

    Anchored cleanly around 6500K -> ~(1.0, 1.0, 1.0) and smoothly attenuating blue/green
    down to warm evening amber (~2300K -> ~1.0, 0.59, 0.21).
    """
    temp = max(1500.0, min(10000.0, float(cct_k))) / 100.0

    # Red channel
    if temp <= 66.0:
        red = 255.0
    else:
        red = 329.698727446 * ((temp - 60.0) ** -0.1332047592)

    # Green channel
    if temp <= 66.0:
        green = 99.4708025861 * math.log(temp) - 161.1195681661
    else:
        green = 288.1221695283 * ((temp - 60.0) ** -0.0755148492)

    # Blue channel
    if temp >= 66.0:
        blue = 255.0
    elif temp <= 19.0:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(temp - 10.0) - 305.0447927307

    r = max(0.0, min(255.0, red)) / 255.0
    g = max(0.0, min(255.0, green)) / 255.0
    b = max(0.0, min(255.0, blue)) / 255.0

    # Keep minimum floor so extreme low CCT never collapses color validation.
    return (max(0.20, r), max(0.15, g), max(0.10, b))


def build_gamma_ramp(
    cct_k: int,
    software_dim_scale: float = 1.0,
) -> tuple[list[int], list[int], list[int]]:
    """Build a 3x256 16-bit (0..65535) linear Windows GDI gamma LUT for target CCT."""
    r_mul, g_mul, b_mul = kelvin_to_rgb_scalars(cct_k)
    dim = max(0.25, min(1.0, float(software_dim_scale)))

    red_lut: list[int] = []
    green_lut: list[int] = []
    blue_lut: list[int] = []
    for i in range(256):
        base = i * 257  # maps 0..255 evenly onto 0..65535
        red_lut.append(max(0, min(65535, int(round(base * r_mul * dim)))))
        green_lut.append(max(0, min(65535, int(round(base * g_mul * dim)))))
        blue_lut.append(max(0, min(65535, int(round(base * b_mul * dim)))))
    return red_lut, green_lut, blue_lut


def build_color_effect_matrix(
    cct_k: int,
    software_dim_scale: float = 1.0,
) -> list[float]:
    """Build a 25-float 5x5 diagonal RGBA matrix for `Magnification.dll` (`MAGCOLOREFFECT`)."""
    r_mul, g_mul, b_mul = kelvin_to_rgb_scalars(cct_k)
    dim = max(0.25, min(1.0, float(software_dim_scale)))
    return [
        r_mul * dim, 0.0, 0.0, 0.0, 0.0,
        0.0, g_mul * dim, 0.0, 0.0, 0.0,
        0.0, 0.0, b_mul * dim, 0.0, 0.0,
        0.0, 0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 0.0, 1.0,
    ]


_NIGHT_LIGHT_REG_PATHS: tuple[str, ...] = (
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount\Current"
    r"\default$windows.data.bluelightreduction.bluelightreductionstate"
    r"\windows.data.bluelightreduction.bluelightreductionstate",
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\Cache\DefaultAccount"
    r"\$$windows.data.bluelightreduction.bluelightreductionstate\Current",
)


def _parse_night_light_state_bytes(data: bytes) -> bool | None:
    """Decode Windows 10/11 CloudStore `bluelightreductionstate` binary blob (read-only)."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 25:
        return None
    raw = bytes(data)
    # Windows 10/11 embeds an inner 4-byte marker (43 42 01 00); immediately following it,
    # bytes `10 00` signal Night Light ON, while `d0 ..` (or preceding byte 0x13) signals OFF.
    marker = b"\x43\x42\x01\x00"
    inner_idx = raw.find(marker, 4)
    if inner_idx != -1 and inner_idx + 6 <= len(raw):
        flag_bytes = raw[inner_idx + 4 : inner_idx + 6]
        if flag_bytes == b"\x10\x00":
            return True
        if flag_bytes[0:1] == b"\xd0" or (inner_idx >= 1 and raw[inner_idx - 1] == 0x13):
            return False

    if raw[18] == 0x15 and raw[23:25] == b"\x10\x00":
        return True
    if raw[18] == 0x13:
        return False
    return None


def detect_windows_night_light_hint() -> bool | None:
    """Safely detect whether Windows Night Light is currently enabled (strictly READ-ONLY).

    Reads HKCU CloudStore `bluelightreductionstate` via `winreg.KEY_READ` with zero writes.
    Returns `True` if Night Light appears ON, `False` if OFF, or `None` on non-Windows
    or whenever the key/format cannot be read.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    for subkey in _NIGHT_LIGHT_REG_PATHS:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                subkey,
                0,
                winreg.KEY_READ,
            ) as key:
                data, _reg_type = winreg.QueryValueEx(key, "Data")
            if isinstance(data, (bytes, bytearray)):
                return _parse_night_light_state_bytes(bytes(data))
        except OSError:
            continue
        except Exception as exc:
            logger.debug("Read-only Windows Night Light probe skipped: %s", exc)
            return None
    return None


def is_identity_color_matrix(matrix: list[float], tol: float = 1e-4) -> bool:
    """Return True when a 5x5 color matrix matches identity within `tol`."""
    if len(matrix) != 25:
        return False
    return all(abs(float(a) - float(b)) <= tol for a, b in zip(matrix, IDENTITY_COLOR_MATRIX))


def detect_windows_color_filters_enabled() -> bool:
    """Read-only probe for Windows Accessibility Color Filters (`HKCU\\Software\\Microsoft\\ColorFiltering`)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\ColorFiltering",
            0,
            winreg.KEY_READ,
        ) as key:
            val, _ = winreg.QueryValueEx(key, "Active")
            return bool(int(val) == 1)
    except (OSError, ValueError, TypeError):
        return False
    except Exception:
        return False


def detect_windows_dark_mode() -> bool | None:
    """Read-only probe for Windows Apps Dark Mode (`AppsUseLightTheme == 0`, ~0.04 ms)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            0,
            winreg.KEY_READ,
        ) as key:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(val) == 0
    except (OSError, ValueError, TypeError):
        return None
    except Exception:
        return None


def parse_edid_physical_and_gamut(
    edid_bytes: bytes,
    model_name: str = "",
) -> dict[str, Any]:
    """Parse physical screen dimensions (DTD #1 mm -> bytes 21-22 cm) & CIE 1931 gamut from EDID base block.

    Zero-admin, zero-pixel extraction yielding projected solid angle `solid_angle_sr` and
    conservative `auto_panel_type` (`wled_ips`, `p3_ips`, `woled`, `qd_oled`).
    """
    result: dict[str, Any] = {
        "width_mm": 0,
        "height_mm": 0,
        "diagonal_in": 0.0,
        "solid_angle_sr": 0.35,
        "gamut_dci_p3_pct": 0.0,
        "auto_panel_type": "wled_ips",
    }
    if not isinstance(edid_bytes, (bytes, bytearray)) or len(edid_bytes) < 128:
        return result
    raw = bytes(edid_bytes)
    if raw[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return result

    w_mm = 0
    h_mm = 0
    if raw[54] != 0 or raw[55] != 0:
        dtd_w = raw[66] | ((raw[68] & 0xF0) << 4)
        dtd_h = raw[67] | ((raw[68] & 0x0F) << 8)
        if 100 <= dtd_w <= 2500 and 60 <= dtd_h <= 1600:
            w_mm, h_mm = int(dtd_w), int(dtd_h)
    if w_mm == 0 or h_mm == 0:
        cm_w, cm_h = int(raw[21]), int(raw[22])
        if 10 <= cm_w <= 250 and 6 <= cm_h <= 160:
            w_mm, h_mm = cm_w * 10, cm_h * 10

    if w_mm > 0 and h_mm > 0:
        diag_in = round(math.hypot(w_mm, h_mm) / 25.4, 2)
        view_mm = 550.0 if diag_in <= 18.0 else 600.0
        try:
            from spectral.photobiology import compute_projected_solid_angle_sr

            omega = float(compute_projected_solid_angle_sr(w_mm, h_mm, viewing_dist_mm=view_mm))
        except Exception:
            omega = 0.35
        result["width_mm"] = w_mm
        result["height_mm"] = h_mm
        result["diagonal_in"] = diag_in
        result["solid_angle_sr"] = round(omega, 4)

    # CIE 1931 10-bit primary chromaticities (EDID bytes 25..34)
    rg_lsb = raw[25]
    bw_lsb = raw[26]
    rx = ((raw[27] << 2) | ((rg_lsb >> 6) & 3)) / 1024.0
    ry = ((raw[28] << 2) | ((rg_lsb >> 4) & 3)) / 1024.0
    gx = ((raw[29] << 2) | ((rg_lsb >> 2) & 3)) / 1024.0
    gy = ((raw[30] << 2) | (rg_lsb & 3)) / 1024.0
    bx = ((raw[31] << 2) | ((bw_lsb >> 6) & 3)) / 1024.0
    by = ((raw[32] << 2) | ((bw_lsb >> 4) & 3)) / 1024.0
    tri_area = 0.5 * abs(rx * (gy - by) + gx * (by - ry) + bx * (ry - gy))
    p3_area = 0.5 * abs(0.68 * (0.69 - 0.06) + 0.265 * (0.06 - 0.32) + 0.15 * (0.32 - 0.69))
    p3_pct = round((tri_area / p3_area) * 100.0, 1) if p3_area > 0 else 0.0
    result["gamut_dci_p3_pct"] = p3_pct

    mdl_low = str(model_name or "").lower()
    if ("qd" in mdl_low and "oled" in mdl_low) or "qdoled" in mdl_low:
        result["auto_panel_type"] = "qd_oled"
    elif "oled" in mdl_low:
        result["auto_panel_type"] = "woled"
    elif p3_pct >= 93.0:
        result["auto_panel_type"] = "p3_ips"
    else:
        result["auto_panel_type"] = "wled_ips"
    return result


def parse_cta861_max_nits(edid_bytes: bytes) -> tuple[float, float] | None:
    """Parse hardware peak & min luminance (nits) from CTA-861.3 HDR Static Metadata block (`0x06`).

    Accepts either a single 128-byte CTA-861 extension block (`tag == 0x02`) or a full
    multi-block EDID byte stream. Applies exact CTA-861.3 equations:
      L_max = 50 * 2^(CV_max / 32)
      L_min = L_max * (CV_min / 255)^2 / 100
    Returns `(max_nits, min_nits)` or `None` if no valid HDR Static Metadata block is present.
    """
    if not isinstance(edid_bytes, (bytes, bytearray)) or len(edid_bytes) < 128:
        return None
    raw = bytes(edid_bytes)

    blocks: list[bytes] = []
    if len(raw) == 128:
        blocks.append(raw)
    else:
        for offset in range(0, len(raw) - 127, 128):
            blocks.append(raw[offset : offset + 128])

    for block in blocks:
        if block[0] != 0x02:
            continue
        dtd_offset = int(block[2])
        if dtd_offset < 4 or dtd_offset > 127:
            continue
        pos = 4
        while pos < dtd_offset:
            header = block[pos]
            tag_code = (header >> 5) & 0x07
            length = header & 0x1F
            end = pos + 1 + length
            if end > dtd_offset:
                break
            if tag_code == 7 and length >= 4 and block[pos + 1] == 0x06:
                cv_max = int(block[pos + 4])
                if cv_max > 0:
                    max_nits = round(50.0 * (2.0 ** (cv_max / 32.0)), 1)
                    if 100.0 <= max_nits <= 10000.0:
                        if length >= 6 and int(block[pos + 6]) > 0:
                            cv_min = int(block[pos + 6])
                            min_nits = round(max_nits * ((cv_min / 255.0) ** 2) / 100.0, 3)
                        else:
                            min_nits = round(max(0.1, max_nits * 0.001), 3)
                        return (max_nits, min_nits)
            pos = end
    return None


def _read_win32_edid_and_model(timeout: float = 5.0) -> tuple[bytes, str]:
    """Read raw EDID blocks & model name via read-only WMI (zero admin, `CREATE_NO_WINDOW`)."""
    if sys.platform != "win32":
        return b"", ""
    ps_script = (
        "$id = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorID | Select-Object -First 1; "
        "$m = if ($id -and $id.UserFriendlyName) { "
        "  -join ($id.UserFriendlyName | Where-Object {$_ -ne 0} | ForEach-Object {[char]$_}) "
        "} else { '' }; "
        "$ed = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorDescriptorMethods | Select-Object -First 1; "
        "$hex = ''; "
        "if ($ed) { "
        "  $b0 = Invoke-CimMethod -InputObject $ed -MethodName WmiGetMonitorRawEEdidV1Block -Arguments @{BlockId=[byte]0}; "
        "  if ($b0 -and $b0.BlockContent) { "
        "    $hex += (($b0.BlockContent | ForEach-Object {'{0:X2}' -f $_}) -join ''); "
        "    $ext = [int]$b0.BlockContent[126]; "
        "    for ($i = 1; $i -le $ext; $i++) { "
        "      try { "
        "        $bi = Invoke-CimMethod -InputObject $ed -MethodName WmiGetMonitorRawEEdidV1Block -Arguments @{BlockId=[byte]$i}; "
        "        if ($bi -and $bi.BlockContent) { $hex += (($bi.BlockContent | ForEach-Object {'{0:X2}' -f $_}) -join '') } "
        "      } catch {} "
        "    } "
        "  } "
        "}; "
        "Write-Output \"MODEL=$m\"; Write-Output \"EDID=$hex\""
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("Read-only EDID WMI probe failed: %s", exc)
        return b"", ""

    if proc.returncode != 0:
        return b"", ""
    model_name = ""
    edid_bytes = b""
    for line in proc.stdout.splitlines():
        if line.startswith("MODEL="):
            model_name = line[6:].strip()
        elif line.startswith("EDID="):
            hex_str = line[5:].strip()
            if hex_str:
                try:
                    edid_bytes = bytes.fromhex(hex_str)
                except ValueError:
                    edid_bytes = b""
    return edid_bytes, model_name


def _read_win32_hdr_luminance() -> dict[str, Any] | None:
    """Query active Windows HDR peak luminance via DisplayConfig + DXGI (strictly read-only)."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes
        import uuid

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_uint32),
                ("size", ctypes.c_uint32),
                ("adapterId", LUID),
                ("id", ctypes.c_uint32),
            ]

        class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
            _fields_ = [
                ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("value", ctypes.c_uint32),
                ("colorEncoding", ctypes.c_uint32),
                ("bitsPerColorChannel", ctypes.c_uint32),
            ]

        class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
            _fields_ = [
                ("adapterId", LUID),
                ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32),
            ]

        class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
            _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]

        class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
            _fields_ = [
                ("adapterId", LUID),
                ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32),
                ("rotation", ctypes.c_uint32),
                ("scaling", ctypes.c_uint32),
                ("refreshRate", DISPLAYCONFIG_RATIONAL),
                ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", wintypes.BOOL),
                ("statusFlags", ctypes.c_uint32),
            ]

        class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
            _fields_ = [
                ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
                ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
                ("flags", ctypes.c_uint32),
            ]

        class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
            _fields_ = [
                ("infoType", ctypes.c_uint32),
                ("id", ctypes.c_uint32),
                ("adapterId", LUID),
                ("_pad", ctypes.c_ubyte * 48),
            ]

        num_paths = ctypes.c_uint32(0)
        num_modes = ctypes.c_uint32(0)
        if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(num_paths), ctypes.byref(num_modes)) != 0:
            return None
        if num_paths.value == 0:
            return None
        paths = (DISPLAYCONFIG_PATH_INFO * num_paths.value)()
        modes = (DISPLAYCONFIG_MODE_INFO * max(1, num_modes.value))()
        if user32.QueryDisplayConfig(2, ctypes.byref(num_paths), paths, ctypes.byref(num_modes), modes, None) != 0:
            return None

        hdr_enabled = False
        for idx in range(num_paths.value):
            target = paths[idx].targetInfo
            ac = DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO()
            ac.header.type = 9
            ac.header.size = ctypes.sizeof(ac)
            ac.header.adapterId = target.adapterId
            ac.header.id = target.id
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(ac)) == 0 and bool(ac.value & 2):
                hdr_enabled = True
                break
        if not hdr_enabled:
            return {"hdr_enabled": False, "max_nits": 0.0, "min_nits": 0.0}

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        def _guid(text: str) -> GUID:
            u = uuid.UUID(text)
            g = GUID()
            g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
            for i, b in enumerate(u.bytes[8:]):
                g.Data4[i] = b
            return g

        class DXGI_OUTPUT_DESC1(ctypes.Structure):
            _fields_ = [
                ("DeviceName", wintypes.WCHAR * 32),
                ("DesktopCoordinates", wintypes.RECT),
                ("AttachedToDesktop", wintypes.BOOL),
                ("Rotation", ctypes.c_uint),
                ("Monitor", wintypes.HMONITOR),
                ("BitsPerColor", ctypes.c_uint),
                ("ColorSpace", ctypes.c_uint),
                ("RedPrimary", ctypes.c_float * 2),
                ("GreenPrimary", ctypes.c_float * 2),
                ("BluePrimary", ctypes.c_float * 2),
                ("WhitePoint", ctypes.c_float * 2),
                ("MinLuminance", ctypes.c_float),
                ("MaxLuminance", ctypes.c_float),
                ("MaxFullFrameLuminance", ctypes.c_float),
            ]

        dxgi = ctypes.windll.dxgi  # type: ignore[attr-defined]
        dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        dxgi.CreateDXGIFactory1.restype = ctypes.c_long
        iid_f1 = _guid("770aae78-f26f-4dba-a829-253c83d1b387")
        iid_o6 = _guid("068346e8-aaec-4b84-add7-137f513f77a1")
        factory = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid_f1), ctypes.byref(factory)) != 0 or not factory.value:
            return None

        vtbl_f = ctypes.cast(ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        enum_adapters = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(vtbl_f[7])
        release_f = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl_f[2])
        try:
            adapter = ctypes.c_void_p()
            if enum_adapters(factory, 0, ctypes.byref(adapter)) == 0 and adapter.value:
                vtbl_a = ctypes.cast(ctypes.cast(adapter, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
                enum_outputs = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(vtbl_a[7])
                release_a = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl_a[2])
                try:
                    out = ctypes.c_void_p()
                    if enum_outputs(adapter, 0, ctypes.byref(out)) == 0 and out.value:
                        vtbl_o = ctypes.cast(ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
                        qi = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(vtbl_o[0])
                        release_o = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl_o[2])
                        try:
                            out6 = ctypes.c_void_p()
                            if qi(out, ctypes.byref(iid_o6), ctypes.byref(out6)) == 0 and out6.value:
                                vtbl_o6 = ctypes.cast(ctypes.cast(out6, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
                                get_desc1 = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(DXGI_OUTPUT_DESC1))(vtbl_o6[27])
                                release_o6 = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl_o6[2])
                                try:
                                    desc = DXGI_OUTPUT_DESC1()
                                    if get_desc1(out6, ctypes.byref(desc)) == 0:
                                        max_l = float(desc.MaxFullFrameLuminance or desc.MaxLuminance or 0.0)
                                        min_l = float(desc.MinLuminance or 0.05)
                                        return {
                                            "hdr_enabled": True,
                                            "max_nits": round(max_l, 1),
                                            "min_nits": round(min_l, 3),
                                        }
                                finally:
                                    release_o6(out6)
                        finally:
                            release_o(out)
                finally:
                    release_a(adapter)
        finally:
            release_f(factory)
    except Exception as exc:
        logger.debug("Read-only DXGI HDR luminance probe skipped: %s", exc)
    return None


def detect_display_luminance_profile(
    edid_reader: Any | None = None,
    hdr_reader: Any | None = None,
) -> dict[str, Any]:
    """3-Tier Zero-Admin Automatic Display Peak-Nits Detection (strictly read-only).

    Tier 1: EDID CTA-861.3 HDR Static Metadata block (`0x06`) via WMI — works even with HDR OFF.
    Tier 2: Windows HDR active `IDXGIOutput6::GetDesc1()` (ignoring generic SDR 270.0 placeholder).
    Tier 3: Safe Pure-SDR fallback (`300.0 nits`, `source="estimated_sdr"`).
    """
    has_injected_reader = edid_reader is not None or hdr_reader is not None
    model_name = ""
    edid_bytes = b""

    # Tier 1: EDID CTA-861.3 Static Metadata
    try:
        if edid_reader is not None:
            raw_res = edid_reader()
            if isinstance(raw_res, tuple) and len(raw_res) == 2:
                edid_bytes = bytes(raw_res[0] or b"")
                model_name = str(raw_res[1] or "").strip()
            elif isinstance(raw_res, (bytes, bytearray)):
                edid_bytes = bytes(raw_res)
        elif not has_injected_reader and sys.platform == "win32":
            edid_bytes, model_name = _read_win32_edid_and_model()
    except Exception as exc:
        logger.debug("EDID luminance tier skipped: %s", exc)

    edid_optics = parse_edid_physical_and_gamut(edid_bytes, model_name=model_name)
    parsed_edid = parse_cta861_max_nits(edid_bytes) if edid_bytes else None
    if parsed_edid is not None:
        max_nits, min_nits = parsed_edid
        return {
            "max_nits": float(max_nits),
            "min_nits": float(min_nits),
            "model_name": model_name,
            "source": "edid_cta861",
            "is_auto_detected": True,
            **edid_optics,
        }

    # Tier 2: Active Windows HDR DXGI fallback (ignoring generic SDR 270.0 nits placeholder)
    hdr_info: dict[str, Any] | None = None
    try:
        if hdr_reader is not None:
            res = hdr_reader()
            if isinstance(res, dict):
                hdr_info = res
        elif not has_injected_reader and sys.platform == "win32":
            hdr_info = _read_win32_hdr_luminance()
    except Exception as exc:
        logger.debug("DXGI HDR luminance tier skipped: %s", exc)

    if isinstance(hdr_info, dict) and hdr_info.get("hdr_enabled") is True:
        cand_max = float(hdr_info.get("max_nits", 0.0) or 0.0)
        if cand_max >= 200.0 and abs(cand_max - 270.0) > 0.5:
            cand_min = float(hdr_info.get("min_nits", 0.1) or 0.1)
            eff_model = str(hdr_info.get("model_name", model_name) or model_name).strip()
            optics_t2 = parse_edid_physical_and_gamut(edid_bytes, model_name=eff_model)
            return {
                "max_nits": round(cand_max, 1),
                "min_nits": round(max(0.0, cand_min), 3),
                "model_name": eff_model,
                "source": "dxgi_hdr",
                "is_auto_detected": True,
                **optics_t2,
            }

    # Tier 3: Pure-SDR fallback
    return {
        "max_nits": 300.0,
        "min_nits": 18.0,
        "model_name": model_name,
        "source": "estimated_sdr",
        "is_auto_detected": False,
        **edid_optics,
    }


def enable_per_monitor_dpi_awareness() -> bool:
    """Enable Per-Monitor V2 DPI awareness on Windows so window/monitor rects match 1:1."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (DPI_AWARENESS_CONTEXT)-4
        return bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception as exc:
        logger.debug("SetProcessDpiAwarenessContext unavailable: %s", exc)
        return False


def _probe_win32_hdr_and_acm() -> tuple[bool, bool]:
    """Read-only probe for active Windows HDR (`type=9`) and Win11 24H2+ ACM (`type=16`)."""
    if sys.platform != "win32":
        return False, False
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class HEADER(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_uint32),
                ("size", ctypes.c_uint32),
                ("adapterId", LUID),
                ("id", ctypes.c_uint32),
            ]

        class AC_INFO(ctypes.Structure):
            _fields_ = [
                ("header", HEADER),
                ("value", ctypes.c_uint32),
                ("colorEncoding", ctypes.c_uint32),
                ("bitsPerColorChannel", ctypes.c_uint32),
            ]

        class AC_INFO_2(ctypes.Structure):
            _fields_ = [
                ("header", HEADER),
                ("value", ctypes.c_uint32),
                ("colorEncoding", ctypes.c_uint32),
                ("bitsPerColorChannel", ctypes.c_uint32),
                ("activeColorMode", ctypes.c_uint32),
            ]

        class SRC(ctypes.Structure):
            _fields_ = [
                ("adapterId", LUID),
                ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32),
            ]

        class RAT(ctypes.Structure):
            _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]

        class TGT(ctypes.Structure):
            _fields_ = [
                ("adapterId", LUID),
                ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32),
                ("rotation", ctypes.c_uint32),
                ("scaling", ctypes.c_uint32),
                ("refreshRate", RAT),
                ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", wintypes.BOOL),
                ("statusFlags", ctypes.c_uint32),
            ]

        class PATH_INFO(ctypes.Structure):
            _fields_ = [("sourceInfo", SRC), ("targetInfo", TGT), ("flags", ctypes.c_uint32)]

        class MODE_INFO(ctypes.Structure):
            _fields_ = [
                ("infoType", ctypes.c_uint32),
                ("id", ctypes.c_uint32),
                ("adapterId", LUID),
                ("_pad", ctypes.c_ubyte * 48),
            ]

        num_paths = ctypes.c_uint32(0)
        num_modes = ctypes.c_uint32(0)
        if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(num_paths), ctypes.byref(num_modes)) != 0:
            return False, False
        if num_paths.value == 0:
            return False, False
        paths = (PATH_INFO * num_paths.value)()
        modes = (MODE_INFO * max(1, num_modes.value))()
        if user32.QueryDisplayConfig(2, ctypes.byref(num_paths), paths, ctypes.byref(num_modes), modes, None) != 0:
            return False, False

        hdr_active = False
        acm_active = False
        for idx in range(num_paths.value):
            target = paths[idx].targetInfo
            ac2 = AC_INFO_2()
            ac2.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2
            ac2.header.size = ctypes.sizeof(ac2)
            ac2.header.adapterId = target.adapterId
            ac2.header.id = target.id
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(ac2)) == 0:
                if ac2.activeColorMode == DISPLAYCONFIG_ADVANCED_COLOR_MODE_HDR:
                    hdr_active = True
                elif ac2.activeColorMode == DISPLAYCONFIG_ADVANCED_COLOR_MODE_WCG:
                    acm_active = True
                continue

            # Pre-24H2 fallback: only used when type=15 is unsupported on older Windows builds.
            ac = AC_INFO()
            ac.header.type = 9
            ac.header.size = ctypes.sizeof(ac)
            ac.header.adapterId = target.adapterId
            ac.header.id = target.id
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(ac)) == 0 and bool(ac.value & 0x2):
                hdr_active = True

        return hdr_active, acm_active
    except Exception as exc:
        logger.debug("Read-only HDR/ACM probe failed: %s", exc)
        return False, False


def _probe_win32_competing_color_apps() -> list[str]:
    """Read-only Win32 process snapshot checking for competing color-temperature tools (<2 ms)."""
    if sys.platform != "win32":
        return []
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            return []

        targets = frozenset(COMPETING_COLOR_APPS)
        found: set[str] = set()
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = bool(kernel32.Process32FirstW(snap, ctypes.byref(entry)))
            while ok:
                exe = str(entry.szExeFile or "").strip()
                if exe.lower() in targets:
                    found.add(exe)
                ok = bool(kernel32.Process32NextW(snap, ctypes.byref(entry)))
        finally:
            kernel32.CloseHandle(snap)
        return sorted(found, key=str.lower)
    except Exception as exc:
        logger.debug("Read-only competing-app probe failed: %s", exc)
        return []


def detect_live_display_conflicts(
    *,
    night_light_probe: Callable[[], bool | None] | None = None,
    hdr_acm_probe: Callable[[], tuple[bool, bool]] | None = None,
    process_probe: Callable[[], list[str]] | None = None,
    color_filter_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Poll active Windows display conflicts (100% read-only, zero admin, zero pixels, <3 ms).

    Returns:
        {
            "night_light_on": bool,
            "hdr_active": bool,
            "acm_active": bool,
            "color_filter_on": bool,
            "competing_apps": list[str],
            "warnings": list[str],
        }
    """
    has_injected = (
        night_light_probe is not None
        or hdr_acm_probe is not None
        or process_probe is not None
        or color_filter_probe is not None
    )

    if night_light_probe is not None:
        nl_raw = night_light_probe()
    elif not has_injected and sys.platform == "win32":
        nl_raw = detect_windows_night_light_hint()
    else:
        nl_raw = None
    night_light_on = nl_raw is True

    if hdr_acm_probe is not None:
        hdr_active, acm_active = hdr_acm_probe()
    elif not has_injected and sys.platform == "win32":
        hdr_active, acm_active = _probe_win32_hdr_and_acm()
    else:
        hdr_active, acm_active = False, False

    if color_filter_probe is not None:
        color_filter_on = bool(color_filter_probe())
    elif not has_injected and sys.platform == "win32":
        color_filter_on = detect_windows_color_filters_enabled()
    else:
        color_filter_on = False

    if process_probe is not None:
        competing = [str(x).strip() for x in process_probe() if str(x).strip()]
    elif not has_injected and sys.platform == "win32":
        competing = _probe_win32_competing_color_apps()
    else:
        competing = []

    warnings: list[str] = []
    if night_light_on:
        warnings.append(
            "Windows Night Light is ON — turn it off in Settings -> System -> Display -> Night light "
            "to prevent double-warming."
        )
    if hdr_active:
        warnings.append(
            "Windows HDR is ON — Circadian Desk automatically pauses its GPU color filter while HDR is active."
        )
    if acm_active:
        warnings.append(
            "Windows Auto Color Management (ACM) is ON — color gamut remapping may interact with LUT warmth."
        )
    if color_filter_on:
        warnings.append(
            "Windows Accessibility Color Filter is ON — turn it off in Settings -> Accessibility -> Color filters "
            "to prevent double color tinting."
        )
    if competing:
        joined = ", ".join(competing)
        warnings.append(
            f"Competing color tool detected ({joined}) — close or pause it to avoid double-warming."
        )

    return {
        "night_light_on": bool(night_light_on),
        "hdr_active": bool(hdr_active),
        "acm_active": bool(acm_active),
        "color_filter_on": bool(color_filter_on),
        "competing_apps": competing,
        "warnings": warnings,
    }


def is_packaged_msix() -> bool:
    """Return True when running inside a Windows MSIX / Microsoft Store package identity."""
    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = getattr(kernel32, "GetCurrentPackageFullName", None)
        if fn is None:
            return False
        fn.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p]
        fn.restype = ctypes.c_long
        length = ctypes.c_uint32(0)
        rc = int(fn(ctypes.byref(length), None))
        # ERROR_INSUFFICIENT_BUFFER (122) proves an MSIX package identity exists;
        # APPMODEL_ERROR_NO_PACKAGE (15700) means unpackaged Win32 / portable .exe.
        return rc == 122
    except Exception:
        return False


def get_msix_package_family_name() -> str | None:
    """Return the active MSIX Package Family Name (PFN), or None if unpackaged."""
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = getattr(kernel32, "GetCurrentPackageFamilyName", None)
        if fn is None:
            return None
        fn.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p]
        fn.restype = ctypes.c_long
        length = ctypes.c_uint32(0)
        rc = int(fn(ctypes.byref(length), None))
        if rc != 122 or length.value <= 1:
            return None
        buf = ctypes.create_unicode_buffer(length.value)
        if int(fn(ctypes.byref(length), buf)) == 0:
            return str(buf.value or "").strip() or None
        return None
    except Exception:
        return None


def get_msix_startup_state(
    task_id: str = "CircadianDeskStartup",
    pfn: str | None = None,
) -> int | None:
    """Read MSIX `windows.startupTask` state read-only (`2` = Enabled, `0`/`1`/`3` = Disabled)."""
    if sys.platform != "win32":
        return None
    family = pfn if pfn is not None else get_msix_package_family_name()
    if not family:
        return None
    subkey = (
        r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion"
        rf"\AppModel\SystemAppData\{family}\{task_id}"
    )
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "State")
            return int(val)
    except (OSError, ValueError, TypeError):
        return None
    except Exception:
        return None


def open_windows_settings_uri(target: str) -> tuple[bool, str]:
    """Launch a strictly whitelisted `ms-settings:` Windows Settings page (zero arbitrary URIs)."""
    key = str(target or "").strip().lower()
    uri = WINDOWS_SETTINGS_URI_WHITELIST.get(key)
    if not uri:
        return False, f"Unsupported settings target: {target!r}"
    if sys.platform != "win32":
        return False, "Windows Settings URI launch is only supported on Windows."
    try:
        os.startfile(uri)  # type: ignore[attr-defined]
        return True, uri
    except OSError as exc:
        return False, f"Failed to open {uri}: {exc}"


def get_windows_autostart_enabled() -> bool:
    """Return True if Circadian Desk startup is enabled (MSIX StartupTask or HKCU Run)."""
    if sys.platform != "win32":
        return False
    if is_packaged_msix():
        return get_msix_startup_state() == 2
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_READ
        ) as key:
            val, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            return bool(str(val or "").strip())
    except OSError:
        return False
    except Exception:
        return False


def set_windows_autostart_enabled(
    enabled: bool,
    command: str | None = None,
) -> tuple[bool, str]:
    """Enable or disable zero-admin HKCU Windows startup entry (MSIX Policy 10.2.8 safe).

    Under Microsoft Store MSIX packaging (`is_packaged_msix() == True`), NEVER writes `HKCU\\Run`;
    returns `(False, "msix_use_windows_settings")` so the UI directs users cleanly to
    `ms-settings:startupapps`.
    """
    if sys.platform != "win32":
        return False, "Windows autostart is only supported on Windows."
    if is_packaged_msix():
        return False, "msix_use_windows_settings"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                if command is not None and command.strip():
                    cmd = command.strip()
                elif getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}" --background'
                else:
                    cmd = (
                        f'"{sys.executable}" -m orchestrator.daemon '
                        "--config --open-loop --monitor --web-ui --tray --auto-pause --background"
                    )
                winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, cmd)
                return True, f"Enabled Start with Windows ({cmd})"
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass
            return True, "Disabled Start with Windows"
    except OSError as exc:
        return False, f"Failed to update Start with Windows: {exc}"


class WindowsDisplayController:
    """Controls Windows laptop WMI backlight, external DDC/CI backlight, and GPU CCT transform.

    Color-temperature strategy:
    1. Attempts `gdi32.SetDeviceGammaRamp` (or injected `gamma_backend`).
    2. If GDI gamma is unsupported or rejected on stock Windows (`GdiIcmGammaRange`
       rejects RGB scalars < 0.50 at 3000K/2300K without admin Registry hacks),
       automatically falls back to zero-admin `Magnification.dll`
       (`MagInitialize` + `MagSetFullscreenColorEffect` 5x5 GPU matrix across all monitors).

    Always reads and backs up original state (gamma ramp, WMI brightness, and every
    external physical monitor's DDC/CI VCP 0x10 brightness) BEFORE writing. Refuses to
    mutate any DDC/CI or WMI display whose original brightness could not be backed up.
    Deterministically restores all modified subsystems when `restore()` is called.
    """

    def __init__(
        self,
        enabled: bool = True,
        ddc_backend: Any | None = None,
        gamma_backend: Any | None = None,
        wmi_backend: Any | None = None,
        mag_backend: Any | None = None,
    ) -> None:
        self.enabled = enabled
        self.is_windows = sys.platform == "win32"
        self._ddc_backend = ddc_backend
        self._gamma_backend = gamma_backend
        self._wmi_backend = wmi_backend
        self._mag_backend = mag_backend
        self.ddc_supported: bool = False
        self.wmi_supported: bool = False
        self.gamma_supported: bool = False
        self.mag_supported: bool = False
        self._original_gamma: Any = None
        self._gamma_modified: bool = False
        self._mag: Any = None
        self._MAGCOLOREFFECT: Any = None
        self._mag_initialized: bool = False
        self._mag_modified: bool = False
        self._color_method: str = "none"
        self._original_ddc_brightness: dict[int, int] = {}
        self._ddc_min_brightness: dict[int, int] = {}
        self._ddc_max_brightness: dict[int, int] = {}
        self._ddc_use_vcp: dict[int, bool] = {}
        self._ddc_modified: bool = False
        self._ddc_last_write_ts: dict[int, float] = {}
        self._ddc_last_pct: dict[int, int] = {}
        self._ddc_min_interval_s: float = 0.45
        self._ddc_deadband_pct: int = 2
        self._ddc_diagnostic: str = "Hardware backlight idle"
        self._original_wmi_brightness: int | None = None
        self._wmi_modified: bool = False
        self._wmi_probed: bool = False
        self._display_profile: dict[str, Any] | None = None
        self._ddc_warned: bool = False
        self._gamma_warned: bool = False
        self._last_applied: tuple[int, int] | None = None
        self._user32: Any = None
        self._gdi32: Any = None
        self._dxva2: Any = None

        if not self.enabled:
            return

        if self._has_injected_backend():
            if self._ddc_backend is not None:
                self.ddc_supported = True
            if self._wmi_backend is not None:
                self.wmi_supported = True
            if self._mag_backend is not None:
                self.mag_supported = True
            if self._gamma_backend is not None:
                try:
                    orig = self._gamma_backend.get_ramp()
                except Exception as exc:
                    logger.warning("Injected gamma_backend.get_ramp() failed: %s", exc)
                    orig = None
                if orig is not None:
                    self._original_gamma = orig
                    self.gamma_supported = True
            return

        if not self.is_windows:
            logger.info(
                "WindowsDisplayController running on %s — display hardware control "
                "is in graceful no-op mode (macOS driver planned for Bucket 2).",
                sys.platform,
            )
            return

        self._init_win32()

    def _has_injected_backend(self) -> bool:
        return (
            self._ddc_backend is not None
            or self._gamma_backend is not None
            or self._wmi_backend is not None
            or self._mag_backend is not None
        )

    @property
    def ddc_diagnostic(self) -> str:
        """Human-readable diagnostic for laptop WMI / external DDC/CI hardware dimming."""
        return self._ddc_diagnostic

    def get_capabilities(self) -> dict[str, Any]:
        """Return a read-only summary of detected display & color-transform capabilities.

        Respects injected mock backends cleanly so automated unit tests never touch Win32
        DLLs, PowerShell WMI, or the Windows Registry.
        """
        has_injected = self._has_injected_backend()
        default_profile = {
            "max_nits": 300.0,
            "min_nits": 18.0,
            "model_name": "",
            "source": "estimated_sdr",
            "is_auto_detected": False,
        }
        if not self.enabled:
            wmi_available = False
            night_light_detected: bool | None = None
            display_profile = dict(default_profile)
        elif has_injected:
            wmi_available = (
                self._wmi_backend is not None and self.read_wmi_brightness() is not None
            )
            night_light_detected = None
            display_profile = dict(default_profile)
        elif self.is_windows:
            if not self._wmi_probed:
                orig = self.read_wmi_brightness()
                self._wmi_probed = True
                self.wmi_supported = orig is not None
                if orig is not None:
                    self._original_wmi_brightness = orig
            if self._display_profile is None:
                self._display_profile = detect_display_luminance_profile()
            wmi_available = bool(self.wmi_supported)
            night_light_detected = detect_windows_night_light_hint()
            display_profile = dict(self._display_profile)
        else:
            wmi_available = False
            night_light_detected = None
            display_profile = dict(default_profile)

        return {
            "is_windows": bool(self.is_windows),
            "wmi_laptop_available": bool(wmi_available),
            "ddc_external_supported": bool(self.ddc_supported),
            "gdi_gamma_supported": bool(self.gamma_supported),
            "magnification_available": bool(self.mag_supported),
            "windows_night_light_detected": night_light_detected,
            "ddc_diagnostic": self._ddc_diagnostic,
            "display_profile": display_profile,
        }

    def _init_win32(self) -> None:
        from ctypes import wintypes

        self._user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        self._gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
        try:
            self._dxva2 = ctypes.windll.dxva2  # type: ignore[attr-defined]
        except OSError:
            self._dxva2 = None

        try:
            self._mag = ctypes.windll.Magnification  # type: ignore[attr-defined]
        except OSError:
            self._mag = None

        class PHYSICAL_MONITOR(ctypes.Structure):
            _fields_ = [
                ("hPhysicalMonitor", wintypes.HANDLE),
                ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
            ]

        class MAGCOLOREFFECT(ctypes.Structure):
            _fields_ = [("transform", ctypes.c_float * 25)]

        self._PHYSICAL_MONITOR = PHYSICAL_MONITOR
        self._GAMMA_RAMP_TYPE = (wintypes.WORD * 256) * 3
        self._MAGCOLOREFFECT = MAGCOLOREFFECT

        # Declare explicit 64-bit-safe prototypes to avoid pointer truncation on Win64.
        self._gdi32.CreateDCW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
        ]
        self._gdi32.CreateDCW.restype = wintypes.HDC
        self._gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self._gdi32.DeleteDC.restype = wintypes.BOOL
        self._gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
        self._gdi32.GetDeviceGammaRamp.restype = wintypes.BOOL
        self._gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
        self._gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL

        if self._mag is not None:
            self._mag.MagInitialize.argtypes = []
            self._mag.MagInitialize.restype = wintypes.BOOL
            self._mag.MagUninitialize.argtypes = []
            self._mag.MagUninitialize.restype = wintypes.BOOL
            self._mag.MagSetFullscreenColorEffect.argtypes = [ctypes.POINTER(MAGCOLOREFFECT)]
            self._mag.MagSetFullscreenColorEffect.restype = wintypes.BOOL
            self.mag_supported = True

        if self._dxva2 is not None:
            self._dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
                wintypes.HMONITOR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            self._dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
            self._dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
                wintypes.HMONITOR,
                wintypes.DWORD,
                ctypes.POINTER(PHYSICAL_MONITOR),
            ]
            self._dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
            self._dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
                wintypes.HANDLE,
                wintypes.BYTE,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
            ]
            self._dxva2.GetVCPFeatureAndVCPFeatureReply.restype = wintypes.BOOL
            self._dxva2.SetVCPFeature.argtypes = [
                wintypes.HANDLE,
                wintypes.BYTE,
                wintypes.DWORD,
            ]
            self._dxva2.SetVCPFeature.restype = wintypes.BOOL
            self._dxva2.GetMonitorBrightness.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
            ]
            self._dxva2.GetMonitorBrightness.restype = wintypes.BOOL
            self._dxva2.SetMonitorBrightness.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self._dxva2.SetMonitorBrightness.restype = wintypes.BOOL
            self._dxva2.DestroyPhysicalMonitors.argtypes = [
                wintypes.DWORD,
                ctypes.POINTER(PHYSICAL_MONITOR),
            ]
            self._dxva2.DestroyPhysicalMonitors.restype = wintypes.BOOL

        # Backup original primary display gamma ramp safely before touching it.
        hdc = self._gdi32.CreateDCW("DISPLAY", None, None, None)
        if hdc:
            try:
                orig = self._GAMMA_RAMP_TYPE()
                if self._gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(orig)):
                    self._original_gamma = orig
                    self.gamma_supported = True
                else:
                    logger.warning(
                        "GetDeviceGammaRamp returned 0 (Windows HDR/WCG or ICC lock may be active). "
                        "Will use zero-admin Magnification.dll fallback when available."
                    )
            finally:
                self._gdi32.DeleteDC(hdc)

        self.ddc_supported = self._dxva2 is not None

    # ------------------------------------------------------------------
    # Native Windows Laptop WMI Hardware Backlight (`WmiMonitorBrightness`)
    # ------------------------------------------------------------------

    def _run_powershell(
        self, command: str, timeout: float = 5.0
    ) -> subprocess.CompletedProcess[str] | None:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        try:
            return subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                **kwargs,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("PowerShell WMI command failed: %s", exc)
            return None

    def read_wmi_brightness(self) -> int | None:
        """Read current Windows internal laptop panel brightness (0..100) via WMI."""
        if self._wmi_backend is not None:
            try:
                val = self._wmi_backend.get_brightness()
            except Exception as exc:
                logger.debug("Injected wmi_backend.get_brightness() failed: %s", exc)
                return None
            if val is None:
                return None
            try:
                return max(0, min(100, int(val)))
            except (TypeError, ValueError):
                return None

        if not self.is_windows:
            return None

        cmd = (
            "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
            "| Select-Object -First 1 -ExpandProperty CurrentBrightness"
        )
        proc = self._run_powershell(cmd, timeout=5.0)
        if proc is None or proc.returncode != 0:
            return None
        raw = proc.stdout.strip()
        if not raw:
            return None
        try:
            val = int(raw.splitlines()[0].strip())
        except ValueError:
            return None
        return val if 0 <= val <= 100 else None

    def _write_wmi_brightness(self, pct: int, clamp_min: int = 5) -> bool:
        target = max(clamp_min, min(100, int(pct)))
        if self._wmi_backend is not None:
            try:
                return bool(self._wmi_backend.set_brightness(target))
            except Exception as exc:
                logger.warning("Injected wmi_backend.set_brightness() failed: %s", exc)
                return False

        if not self.is_windows:
            return False

        cmd = (
            "$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
            "| Select-Object -First 1; "
            "if ($null -eq $m) { exit 2 }; "
            f"Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
            f"-Arguments @{{Timeout=[uint32]1; Brightness=[byte]{target}}} | Out-Null"
        )
        proc = self._run_powershell(cmd, timeout=5.0)
        return proc is not None and proc.returncode == 0

    def _set_wmi_brightness(self, brightness_pct: int) -> bool:
        """Safely back up original WMI laptop brightness before applying new level."""
        if self._wmi_backend is None and not self.is_windows:
            return False

        if self._original_wmi_brightness is None:
            if self._wmi_probed:
                return False
            self._wmi_probed = True
            orig = self.read_wmi_brightness()
            if orig is None:
                self.wmi_supported = False
                return False
            self._original_wmi_brightness = orig
            self.wmi_supported = True

        ok = self._write_wmi_brightness(brightness_pct, clamp_min=5)
        if ok:
            self._wmi_modified = True
        return ok

    def _restore_wmi_brightness(self) -> bool:
        """Idempotently restore backed-up original WMI laptop panel brightness."""
        if not self._wmi_modified or self._original_wmi_brightness is None:
            return False
        ok = self._write_wmi_brightness(self._original_wmi_brightness, clamp_min=0)
        if ok:
            self._wmi_modified = False
            logger.info(
                "Restored original WMI laptop backlight brightness (%d%%) cleanly.",
                self._original_wmi_brightness,
            )
        return ok

    # ------------------------------------------------------------------
    # External Monitor DDC/CI Hardware Backlight (`dxva2.dll` VCP 0x10)
    # ------------------------------------------------------------------

    def _read_win32_ddc_brightness(self, h_phys: Any) -> tuple[int, int, int, bool] | None:
        """Read (min_b, cur_b, max_b, used_vcp) via VCP 0x10 with GetMonitorBrightness fallback."""
        from ctypes import wintypes

        cur_v = wintypes.DWORD(0)
        max_v = wintypes.DWORD(0)
        try:
            if self._dxva2.GetVCPFeatureAndVCPFeatureReply(
                h_phys,
                wintypes.BYTE(VCP_LUMINANCE_CODE),
                None,
                ctypes.byref(cur_v),
                ctypes.byref(max_v),
            ):
                if max_v.value > 0:
                    return (0, int(cur_v.value), int(max_v.value), True)
        except Exception:
            pass

        min_b = wintypes.DWORD(0)
        cur_b = wintypes.DWORD(0)
        max_b = wintypes.DWORD(100)
        try:
            if self._dxva2.GetMonitorBrightness(
                h_phys,
                ctypes.byref(min_b),
                ctypes.byref(cur_b),
                ctypes.byref(max_b),
            ):
                return (int(min_b.value), int(cur_b.value), max(1, int(max_b.value)), False)
        except Exception:
            pass

        return None

    def _write_win32_ddc_brightness(self, h_phys: Any, value: int, prefer_vcp: bool) -> bool:
        from ctypes import wintypes

        if prefer_vcp:
            try:
                if self._dxva2.SetVCPFeature(
                    h_phys,
                    wintypes.BYTE(VCP_LUMINANCE_CODE),
                    wintypes.DWORD(int(value)),
                ):
                    return True
            except Exception:
                pass
        try:
            return bool(self._dxva2.SetMonitorBrightness(h_phys, wintypes.DWORD(int(value))))
        except Exception:
            return False

    def _enum_win32_hmonitors(self) -> list[int]:
        from ctypes import wintypes

        hmonitors: list[int] = []
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )

        def _enum_cb(hmon: int, _hdc: int, _rect: Any, _data: int) -> int:
            hmonitors.append(hmon)
            return 1

        self._user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_enum_cb), 0)
        return hmonitors

    def read_current_ddc_brightness(self) -> dict[int, int]:
        """Read current DDC/CI brightness for all readable external monitors (for safety verification)."""
        readings: dict[int, int] = {}
        if self._ddc_backend is not None:
            for mon_id in self._ddc_backend.list_monitors():
                val = self._ddc_backend.get_brightness(mon_id)
                if isinstance(val, tuple) and len(val) == 3:
                    readings[mon_id] = int(val[1])
                elif isinstance(val, (int, float)):
                    readings[mon_id] = int(val)
            return readings

        if not self.is_windows or not self._dxva2:
            return readings

        from ctypes import wintypes

        mon_idx = 0
        for hmon in self._enum_win32_hmonitors():
            count = wintypes.DWORD(0)
            if not self._dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                wintypes.HMONITOR(hmon), ctypes.byref(count)
            ):
                continue
            if count.value == 0:
                continue

            arr_type = self._PHYSICAL_MONITOR * count.value
            phys_monitors = arr_type()
            if not self._dxva2.GetPhysicalMonitorsFromHMONITOR(
                wintypes.HMONITOR(hmon), count, phys_monitors
            ):
                continue

            try:
                for idx in range(count.value):
                    info = self._read_win32_ddc_brightness(phys_monitors[idx].hPhysicalMonitor)
                    if info is not None:
                        readings[mon_idx] = info[1]
                    mon_idx += 1
            finally:
                self._dxva2.DestroyPhysicalMonitors(count, phys_monitors)

        return readings

    def _set_ddc_brightness(self, brightness_pct: int, *, force: bool = False) -> bool:
        pct = max(5, min(100, int(brightness_pct)))
        now_s = time.monotonic()

        def _should_skip_write(mon_key: int) -> bool:
            if force or mon_key not in self._ddc_last_pct:
                return False
            prev_pct = self._ddc_last_pct[mon_key]
            prev_ts = self._ddc_last_write_ts.get(mon_key, 0.0)
            if abs(pct - prev_pct) < self._ddc_deadband_pct:
                return True
            if (now_s - prev_ts) < self._ddc_min_interval_s:
                return True
            return False

        if self._ddc_backend is not None:
            any_success = False
            for mon_id in self._ddc_backend.list_monitors():
                if mon_id not in self._original_ddc_brightness:
                    orig = self._ddc_backend.get_brightness(mon_id)
                    if orig is None:
                        # Defensive safety guard: never mutate a monitor we cannot restore.
                        continue
                    if isinstance(orig, tuple) and len(orig) == 3:
                        min_b, cur_b, max_b = int(orig[0]), int(orig[1]), max(1, int(orig[2]))
                    elif isinstance(orig, (int, float)):
                        min_b, cur_b, max_b = 0, int(orig), 100
                    else:
                        continue
                    self._original_ddc_brightness[mon_id] = cur_b
                    self._ddc_min_brightness[mon_id] = min_b
                    self._ddc_max_brightness[mon_id] = max_b
                else:
                    min_b = self._ddc_min_brightness.get(mon_id, 0)
                    max_b = self._ddc_max_brightness.get(mon_id, 100)

                if _should_skip_write(mon_id):
                    any_success = True
                    continue

                span = max(1, max_b - min_b)
                target = min_b + int(round((pct / 100.0) * span))
                ok = bool(self._ddc_backend.set_brightness(mon_id, target))
                if not ok:
                    ok = bool(self._ddc_backend.set_brightness(mon_id, target))
                if ok:
                    any_success = True
                    self._ddc_modified = True
                    self._ddc_last_pct[mon_id] = pct
                    self._ddc_last_write_ts[mon_id] = now_s
            return any_success

        if not self.is_windows or not self._dxva2:
            return False

        from ctypes import wintypes

        any_success = False
        mon_idx = 0
        for hmon in self._enum_win32_hmonitors():
            count = wintypes.DWORD(0)
            if not self._dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                wintypes.HMONITOR(hmon), ctypes.byref(count)
            ):
                continue
            if count.value == 0:
                continue

            arr_type = self._PHYSICAL_MONITOR * count.value
            phys_monitors = arr_type()
            if not self._dxva2.GetPhysicalMonitorsFromHMONITOR(
                wintypes.HMONITOR(hmon), count, phys_monitors
            ):
                continue

            try:
                for idx in range(count.value):
                    h_phys = phys_monitors[idx].hPhysicalMonitor
                    if mon_idx not in self._original_ddc_brightness:
                        info = self._read_win32_ddc_brightness(h_phys)
                        if info is None:
                            # Defensive safety guard: cannot read/back up original DDC brightness,
                            # so SKIP writing to this monitor (never mutate what we cannot restore!).
                            mon_idx += 1
                            continue
                        min_b, cur_b, max_b, used_vcp = info
                        self._original_ddc_brightness[mon_idx] = cur_b
                        self._ddc_min_brightness[mon_idx] = min_b
                        self._ddc_max_brightness[mon_idx] = max_b
                        self._ddc_use_vcp[mon_idx] = used_vcp
                    else:
                        min_b = self._ddc_min_brightness.get(mon_idx, 0)
                        max_b = self._ddc_max_brightness.get(mon_idx, 100)
                        used_vcp = self._ddc_use_vcp.get(mon_idx, False)

                    if _should_skip_write(mon_idx):
                        any_success = True
                        mon_idx += 1
                        continue

                    span = max(1, max_b - min_b)
                    target = min_b + int(round((pct / 100.0) * span))
                    wrote = self._write_win32_ddc_brightness(h_phys, target, prefer_vcp=used_vcp)
                    if not wrote:
                        time.sleep(0.06)
                        wrote = self._write_win32_ddc_brightness(h_phys, target, prefer_vcp=used_vcp)
                    if wrote:
                        any_success = True
                        self._ddc_modified = True
                        self._ddc_last_pct[mon_idx] = pct
                        self._ddc_last_write_ts[mon_idx] = time.monotonic()
                    mon_idx += 1
            finally:
                self._dxva2.DestroyPhysicalMonitors(count, phys_monitors)

        return any_success

    def _restore_ddc_brightness(self) -> bool:
        """Restore every physical monitor's backed-up original DDC/CI brightness."""
        if not self._ddc_modified or not self._original_ddc_brightness:
            return False

        if self._ddc_backend is not None:
            restored_any = False
            for mon_id, orig_val in self._original_ddc_brightness.items():
                try:
                    if bool(self._ddc_backend.set_brightness(mon_id, orig_val)):
                        restored_any = True
                except Exception as exc:
                    logger.warning("DDC backend restore failed for monitor %s: %s", mon_id, exc)
            if restored_any:
                self._ddc_modified = False
                self._ddc_last_pct.clear()
                self._ddc_last_write_ts.clear()
                logger.info("Restored original DDC/CI monitor brightness cleanly.")
            return restored_any

        if not self.is_windows or not self._dxva2:
            return False

        from ctypes import wintypes

        restored_any = False
        mon_idx = 0
        for hmon in self._enum_win32_hmonitors():
            count = wintypes.DWORD(0)
            if not self._dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                wintypes.HMONITOR(hmon), ctypes.byref(count)
            ):
                continue
            if count.value == 0:
                continue

            arr_type = self._PHYSICAL_MONITOR * count.value
            phys_monitors = arr_type()
            if not self._dxva2.GetPhysicalMonitorsFromHMONITOR(
                wintypes.HMONITOR(hmon), count, phys_monitors
            ):
                continue

            try:
                for idx in range(count.value):
                    h_phys = phys_monitors[idx].hPhysicalMonitor
                    if mon_idx in self._original_ddc_brightness:
                        orig_val = self._original_ddc_brightness[mon_idx]
                        used_vcp = self._ddc_use_vcp.get(mon_idx, False)
                        if self._write_win32_ddc_brightness(h_phys, orig_val, prefer_vcp=used_vcp):
                            restored_any = True
                    mon_idx += 1
            finally:
                self._dxva2.DestroyPhysicalMonitors(count, phys_monitors)

        if restored_any:
            self._ddc_modified = False
            self._ddc_last_pct.clear()
            self._ddc_last_write_ts.clear()
            logger.info("Restored original DDC/CI monitor brightness cleanly.")
        return restored_any

    # ------------------------------------------------------------------
    # Color Temperature: GDI 1D Gamma LUT + Zero-Admin `Magnification.dll`
    # ------------------------------------------------------------------

    def _restore_gdi_gamma(self) -> bool:
        if not self._gamma_modified or self._original_gamma is None:
            return False
        if self._gamma_backend is not None:
            try:
                ok = bool(self._gamma_backend.set_ramp(self._original_gamma))
                if ok:
                    self._gamma_modified = False
                    logger.info("Restored original display gamma ramp cleanly.")
                return ok
            except Exception as exc:
                logger.warning("Injected gamma_backend restore failed: %s", exc)
                return False
        if self.is_windows and self._gdi32 is not None:
            hdc = self._gdi32.CreateDCW("DISPLAY", None, None, None)
            if hdc:
                try:
                    ok = bool(
                        self._gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(self._original_gamma))
                    )
                    if ok:
                        self._gamma_modified = False
                        logger.info("Restored original display gamma ramp cleanly.")
                    return ok
                finally:
                    self._gdi32.DeleteDC(hdc)
        return False

    def _set_magnification_effect(self, cct_k: int, software_dim_scale: float = 1.0) -> bool:
        """Apply zero-admin 5x5 GPU color transform via `Magnification.dll` (or mock backend)."""
        matrix = build_color_effect_matrix(cct_k, software_dim_scale)

        if self._mag_backend is not None:
            if not self._mag_initialized:
                try:
                    if not bool(self._mag_backend.initialize()):
                        return False
                    self._mag_initialized = True
                except Exception as exc:
                    logger.warning("Injected mag_backend.initialize() failed: %s", exc)
                    return False
            # Avoid stacking prior GDI gamma tint underneath Magnification.
            if self._gamma_modified:
                self._restore_gdi_gamma()
            try:
                ok = bool(self._mag_backend.set_matrix(matrix))
            except Exception as exc:
                logger.warning("Injected mag_backend.set_matrix() failed: %s", exc)
                return False
            if ok:
                self._mag_modified = True
                self._color_method = "magnification"
            return ok

        if not self.is_windows or self._has_injected_backend() or self._mag is None:
            return False

        if not self._mag_initialized:
            try:
                if not bool(self._mag.MagInitialize()):
                    return False
                self._mag_initialized = True
            except Exception as exc:
                logger.warning("Magnification.dll MagInitialize() failed: %s", exc)
                return False

        if self._gamma_modified:
            self._restore_gdi_gamma()

        effect = self._MAGCOLOREFFECT()
        for i in range(25):
            effect.transform[i] = float(matrix[i])
        try:
            ok = bool(self._mag.MagSetFullscreenColorEffect(ctypes.byref(effect)))
        except Exception as exc:
            logger.warning("Magnification.dll MagSetFullscreenColorEffect() failed: %s", exc)
            return False

        if ok:
            self._mag_modified = True
            self._color_method = "magnification"
        return ok

    def _restore_magnification_effect(self) -> bool:
        """Reset `Magnification.dll` to 5x5 identity and call `MagUninitialize()`."""
        if not self._mag_modified and not self._mag_initialized:
            return False

        restored = False
        if self._mag_backend is not None:
            try:
                if self._mag_modified:
                    restored = bool(self._mag_backend.set_matrix(list(IDENTITY_COLOR_MATRIX)))
                if self._mag_initialized:
                    self._mag_backend.uninitialize()
            except Exception as exc:
                logger.warning("Injected mag_backend restore failed: %s", exc)
            finally:
                self._mag_modified = False
                self._mag_initialized = False
            if restored:
                logger.info("Restored Magnification 5x5 color matrix to identity cleanly.")
            return restored

        if not self.is_windows or self._mag is None:
            self._mag_modified = False
            self._mag_initialized = False
            return False

        try:
            if self._mag_modified:
                effect = self._MAGCOLOREFFECT()
                for i in range(25):
                    effect.transform[i] = float(IDENTITY_COLOR_MATRIX[i])
                restored = bool(self._mag.MagSetFullscreenColorEffect(ctypes.byref(effect)))
            if self._mag_initialized:
                self._mag.MagUninitialize()
        except Exception as exc:
            logger.warning("Magnification.dll restore/uninitialize failed: %s", exc)
        finally:
            self._mag_modified = False
            self._mag_initialized = False

        if restored:
            logger.info("Restored Magnification.dll color transform to identity cleanly.")
        return restored

    def _set_gamma_lut(self, cct_k: int, software_dim_scale: float = 1.0) -> bool:
        """Apply CCT via GDI 1D LUT, falling back seamlessly to `Magnification.dll` on stock Windows.

        When the effective transform is neutral identity (`>=6450K` with `software_dim_scale >= 0.999`
        or matrix within `1e-4` of identity), automatically calls `_restore_magnification_effect()`
        (`MagUninitialize()`) / `_restore_gdi_gamma()` so DWM Multiplane Overlays (MPO) and VRR stay native.
        """
        target_matrix = build_color_effect_matrix(cct_k, software_dim_scale)
        if (cct_k >= 6450 and software_dim_scale >= 0.999) or is_identity_color_matrix(target_matrix):
            if self._mag_modified or self._mag_initialized:
                self._restore_magnification_effect()
            if self._gamma_modified:
                self._restore_gdi_gamma()
            self._color_method = "none"
            return True

        # Once Magnification is active on a stock Windows machine (where GDI rejected <0.50),
        # stay on Magnification for smooth flicker-free transitions across all CCTs and never
        # stack a GDI gamma ramp on top of an active Magnification transform.
        if self._mag_modified:
            if self._set_magnification_effect(cct_k, software_dim_scale):
                return True
            self._color_method = "none"
            return False

        gdi_ok = False
        if self._gamma_backend is not None:
            if self.gamma_supported:
                ramp = build_gamma_ramp(cct_k, software_dim_scale)
                try:
                    gdi_ok = bool(self._gamma_backend.set_ramp(ramp))
                except Exception as exc:
                    logger.warning("Injected gamma_backend.set_ramp() failed: %s", exc)
                    gdi_ok = False
                if gdi_ok:
                    self._gamma_modified = True
                    self._color_method = "gdi_gamma"
                    return True
        elif self.is_windows and not self._has_injected_backend() and self.gamma_supported:
            red_lut, green_lut, blue_lut = build_gamma_ramp(cct_k, software_dim_scale)
            ramp = self._GAMMA_RAMP_TYPE()
            for i in range(256):
                ramp[0][i] = red_lut[i]
                ramp[1][i] = green_lut[i]
                ramp[2][i] = blue_lut[i]

            hdc = self._gdi32.CreateDCW("DISPLAY", None, None, None)
            if hdc:
                try:
                    gdi_ok = bool(self._gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)))
                finally:
                    self._gdi32.DeleteDC(hdc)
            if gdi_ok:
                self._gamma_modified = True
                self._color_method = "gdi_gamma"
                return True

        # Automatic zero-admin fallback to Magnification.dll (bypasses stock Windows
        # GdiIcmGammaRange <0.50 clamp on 3000K/2300K without Registry edits or reboot!).
        if self._set_magnification_effect(cct_k, software_dim_scale):
            return True

        if not self._gamma_warned:
            self._gamma_warned = True
            logger.warning(
                "Both GDI SetDeviceGammaRamp and Magnification.dll color transform failed "
                "(common when Windows HDR/WCG is enabled). Color-temperature shift skipped."
            )
        self._color_method = "none"
        return False

    def apply(self, brightness_pct: int, cct_k: int) -> dict[str, Any]:
        """Apply hardware backlight (DDC/CI and/or laptop WMI) + GPU color-temperature shift."""
        pct = max(10, min(100, int(brightness_pct)))
        cct = max(2200, min(6500, int(cct_k)))
        self._last_applied = (pct, cct)

        has_backend = self._has_injected_backend()
        if not self.enabled or (not self.is_windows and not has_backend):
            return {
                "applied": False,
                "ddc_ok": False,
                "wmi_ok": False,
                "gamma_ok": False,
                "color_method": "none",
                "brightness_pct": pct,
                "cct_k": cct,
            }

        ddc_ok = (
            self._set_ddc_brightness(pct)
            if (self._ddc_backend is not None or (self.is_windows and not has_backend))
            else False
        )
        wmi_ok = (
            self._set_wmi_brightness(pct)
            if (self._wmi_backend is not None or (self.is_windows and not has_backend))
            else False
        )

        if wmi_ok and ddc_ok:
            self._ddc_diagnostic = "WMI laptop backlight + DDC/CI OK"
        elif wmi_ok:
            self._ddc_diagnostic = "WMI laptop backlight OK"
        elif ddc_ok:
            self._ddc_diagnostic = "DDC/CI hardware control OK"
        else:
            self._ddc_diagnostic = "DDC/CI blocked — check monitor OSD or USB-C dock"
            if not self._ddc_warned:
                self._ddc_warned = True
                logger.warning(
                    "Hardware backlight control (DDC/CI and WMI) unavailable on current display "
                    "(enable DDC/CI in external monitor OSD or check USB-C hub/DisplayLink). "
                    "Falling back to gentle software color-matrix dimming."
                )

        # Only apply software luminance scaling if BOTH hardware backlight paths failed.
        sw_dim = 1.0 if (ddc_ok or wmi_ok) else max(0.45, pct / 100.0)
        gamma_ok = self._set_gamma_lut(cct_k=cct, software_dim_scale=sw_dim)
        return {
            "applied": ddc_ok or wmi_ok or gamma_ok,
            "ddc_ok": ddc_ok,
            "wmi_ok": wmi_ok,
            "gamma_ok": gamma_ok,
            "color_method": self._color_method if gamma_ok else "none",
            "ddc_diagnostic": self._ddc_diagnostic,
            "brightness_pct": pct,
            "cct_k": cct,
        }

    def restore(self) -> bool:
        """Idempotently restore GDI gamma, Magnification identity, DDC/CI, AND WMI brightness."""
        gamma_restored = self._restore_gdi_gamma()
        mag_restored = self._restore_magnification_effect()
        ddc_restored = self._restore_ddc_brightness()
        wmi_restored = self._restore_wmi_brightness()
        if gamma_restored or mag_restored:
            self._color_method = "none"
        return gamma_restored or mag_restored or ddc_restored or wmi_restored


class SleepHandoffGuard:
    """Phase- and alert-aware local lighting handoff & display restore on PC sleep/exit.

    Solves the V1A USB-C laptop lid-close gap safely (BUG-2 fix):
    - DAY: restores display only; never dims or turns on daytime lamps.
    - NIGHT / unlinked_light_alert / lamp already OFF: keeps lamp OFF (never turns ON!).
    - EVENING (lamp ON & no alert): smoothly hands off warm dim evening scene.
    - SIGINT: strictly honors `handoff_on_normal_exit=False` (restores display only).
    """

    def __init__(
        self,
        light: LightDriver,
        display: WindowsDisplayController | None = None,
        handoff_on_normal_exit: bool = False,
        on_resume: Callable[[], None] | None = None,
    ) -> None:
        self.light = light
        self.display = display
        self.handoff_on_normal_exit = handoff_on_normal_exit
        self._on_resume = on_resume
        self._lock = threading.Lock()
        self._triggered = False
        self._installed = False
        self._power_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._hwnd: int = 0
        self._phase: str = "EVENING"
        self._unlinked_light_alert: bool = False
        self._last_bulb_power: bool = True
        self.last_handoff_action: str | None = None

    def update_context(
        self,
        phase: str,
        unlinked_light_alert: bool = False,
        last_bulb_power: bool = True,
    ) -> None:
        """Update current circadian phase, alert status, and lamp power state."""
        with self._lock:
            self._phase = str(phase).strip().upper() or "EVENING"
            self._unlinked_light_alert = bool(unlinked_light_alert)
            self._last_bulb_power = bool(last_bulb_power)

    def trigger_handoff(self, reason: str = "shutdown") -> bool:
        """Execute phase-safe light handoff and display restore at most once."""
        with self._lock:
            if self._triggered:
                return False
            self._triggered = True
            phase = self._phase
            alert = self._unlinked_light_alert
            bulb_on = self._last_bulb_power

        logger.info(
            "SleepHandoffGuard triggered (%s | phase=%s, alert=%s, bulb_on=%s).",
            reason,
            phase,
            alert,
            bulb_on,
        )

        if phase == "DAY":
            self.last_handoff_action = "day_display_only"
            logger.info("DAY phase active — restoring display only (leaving lamps untouched).")
        elif phase == "NIGHT" or alert or not bulb_on:
            self.last_handoff_action = "lamp_off"
            try:
                self.light.set_state(brightness_pct=0, cct_k=2300, transition_ms=1500)
            except Exception as exc:
                logger.warning("Lamp OFF handoff failed during %s: %s", reason, exc)
        else:
            self.last_handoff_action = "evening_warm_dim"
            try:
                self.light.handoff_evening_scene()
            except Exception as exc:
                logger.warning("Evening scene handoff failed during %s: %s", reason, exc)

        if self.display is not None:
            try:
                self.display.restore()
            except Exception as exc:
                logger.warning("Display restore failed during %s: %s", reason, exc)

        return True

    def _handle_suspend(self, reason: str = "windows_suspend") -> bool:
        """Handle OS sleep / Modern Standby S0ix suspend cleanly."""
        return self.trigger_handoff(reason=reason)

    def _handle_resume(self, reason: str = "windows_resume") -> None:
        """Reset one-shot suspend flag on OS wake / S0ix display-on and invoke `on_resume` safely."""
        with self._lock:
            self._triggered = False
            cb = self._on_resume
        logger.info("SleepHandoffGuard OS resume detected (%s) — re-arming & syncing display.", reason)
        if cb is not None:
            try:
                cb()
            except Exception as exc:
                logger.warning("SleepHandoffGuard on_resume callback failed: %s", exc)

    def _handle_signal(self, sig_num: int) -> bool:
        """Handle SIGINT/SIGTERM honoring `handoff_on_normal_exit` for SIGINT."""
        if sig_num == int(signal.SIGINT) and not self.handoff_on_normal_exit:
            if self.display is not None:
                try:
                    self.display.restore()
                except Exception as exc:
                    logger.warning("Display restore failed during SIGINT: %s", exc)
            return False
        return self.trigger_handoff(reason=f"signal:{sig_num}")

    def _on_atexit(self) -> None:
        if self.handoff_on_normal_exit:
            self.trigger_handoff(reason="atexit")
        elif self.display is not None:
            self.display.restore()

    def install(self, listen_windows_suspend: bool = False) -> None:
        """Register atexit, SIGINT/SIGTERM, and optional Windows WM_POWERBROADCAST hook."""
        if self._installed:
            return
        self._installed = True
        atexit.register(self._on_atexit)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                prev_handler = signal.getsignal(sig)

                def _make_handler(sig_num: int, previous: Any) -> Any:
                    def _handler(signum: int, frame: Any) -> None:
                        self._handle_signal(sig_num)
                        if callable(previous) and previous not in (
                            signal.SIG_DFL,
                            signal.SIG_IGN,
                        ):
                            previous(signum, frame)
                        raise KeyboardInterrupt()

                    return _handler

                signal.signal(sig, _make_handler(int(sig), prev_handler))
            except (ValueError, OSError):
                # Signal handlers can only be registered from the main thread.
                pass

        if listen_windows_suspend and sys.platform == "win32":
            self._start_windows_power_listener()

    def _start_windows_power_listener(self) -> None:
        """Spawn a hidden top-level `WS_POPUP` listener watching S0ix Modern Standby & power events.

        Replaces `HWND_MESSAGE` (which cannot receive `WM_POWERBROADCAST`) with a hidden top-level
        `WS_POPUP` window (`hwndParent=0`), registering:
        - `RegisterPowerSettingNotification(hwnd, GUID_CONSOLE_DISPLAY_STATE, DEVICE_NOTIFY_WINDOW_HANDLE)`
        - `PowerRegisterSuspendResumeNotification(DEVICE_NOTIFY_CALLBACK, ...)` via `powrprof.dll`
        - `WM_DISPLAYCHANGE (0x007E)` for dock/monitor hot-plug re-sync.
        """

        def _pump() -> None:
            h_power_notify: Any = None
            h_suspend_reg: Any = None
            user32: Any = None
            powrprof: Any = None
            try:
                from ctypes import wintypes

                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                try:
                    powrprof = ctypes.WinDLL("powrprof.dll")
                except OSError:
                    powrprof = None

                LRESULT = ctypes.c_ssize_t
                WPARAM = ctypes.c_size_t
                LPARAM = ctypes.c_ssize_t
                WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

                class GUID(ctypes.Structure):
                    _fields_ = [
                        ("Data1", wintypes.DWORD),
                        ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8),
                    ]

                class POWERBROADCAST_SETTING(ctypes.Structure):
                    _fields_ = [
                        ("PowerSetting", GUID),
                        ("DataLength", wintypes.DWORD),
                        ("Data", ctypes.c_uint32),
                    ]

                def _make_guid(guid_str: str) -> GUID:
                    u = uuid.UUID(guid_str)
                    g = GUID()
                    g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
                    for idx, b_val in enumerate(u.bytes[8:]):
                        g.Data4[idx] = b_val
                    return g

                guid_display = _make_guid(GUID_CONSOLE_DISPLAY_STATE_STR)

                user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
                user32.DefWindowProcW.restype = LRESULT
                kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
                kernel32.GetModuleHandleW.restype = wintypes.HMODULE
                user32.CreateWindowExW.argtypes = [
                    wintypes.DWORD,
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.HWND,
                    wintypes.HMENU,
                    wintypes.HINSTANCE,
                    wintypes.LPVOID,
                ]
                user32.CreateWindowExW.restype = wintypes.HWND
                user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
                user32.PostMessageW.restype = wintypes.BOOL
                user32.GetMessageW.argtypes = [
                    ctypes.POINTER(wintypes.MSG),
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.UINT,
                ]
                user32.GetMessageW.restype = wintypes.BOOL
                user32.RegisterPowerSettingNotification.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(GUID),
                    wintypes.DWORD,
                ]
                user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
                user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
                user32.UnregisterPowerSettingNotification.restype = wintypes.BOOL

                class WNDCLASSW(ctypes.Structure):
                    _fields_ = [
                        ("style", wintypes.UINT),
                        ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HCURSOR),
                        ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR),
                    ]

                guard_ref = self

                def _wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
                    if msg == WM_POWERBROADCAST:
                        if wparam == PBT_APMSUSPEND:
                            guard_ref._handle_suspend(reason="windows_suspend")
                        elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                            guard_ref._handle_resume(reason=f"windows_resume:{wparam}")
                        elif wparam == PBT_POWERSETTINGCHANGE and lparam:
                            try:
                                pbs = ctypes.cast(lparam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                                state_val = int(pbs.Data)
                                if state_val == 0:
                                    guard_ref._handle_suspend(reason="s0ix_display_off")
                                elif state_val == 1:
                                    guard_ref._handle_resume(reason="s0ix_display_on")
                            except Exception:
                                pass
                    elif msg == WM_DISPLAYCHANGE:
                        guard_ref._handle_resume(reason="wm_displaychange")
                    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

                proc_cb = WNDPROC(_wnd_proc)
                class_name = f"SpectralSleepGuard_{id(self)}"
                hinst = kernel32.GetModuleHandleW(None)

                wc = WNDCLASSW()
                wc.lpfnWndProc = proc_cb
                wc.hInstance = hinst
                wc.lpszClassName = class_name
                user32.RegisterClassW(ctypes.byref(wc))

                # Hidden top-level WS_POPUP window (hwndParent=0) receives broadcast power messages.
                WS_POPUP = 0x80000000
                hwnd = user32.CreateWindowExW(
                    0, class_name, class_name, WS_POPUP, 0, 0, 0, 0, None, None, hinst, None
                )
                if not hwnd:
                    return
                self._hwnd = hwnd

                # Register S0ix console display-state notification (off=0, on=1, dim=2)
                try:
                    DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000
                    h_power_notify = user32.RegisterPowerSettingNotification(
                        hwnd, ctypes.byref(guid_display), DEVICE_NOTIFY_WINDOW_HANDLE
                    )
                except Exception as exc:
                    logger.debug("RegisterPowerSettingNotification skipped: %s", exc)

                # Register Modern Standby S0ix suspend/resume callback via powrprof.dll
                suspend_cb_ref = None
                if powrprof is not None and hasattr(powrprof, "PowerRegisterSuspendResumeNotification"):
                    try:
                        DEVICE_NOTIFY_CALLBACK_FN = ctypes.WINFUNCTYPE(
                            wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p
                        )

                        class DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS(ctypes.Structure):
                            _fields_ = [
                                ("Callback", DEVICE_NOTIFY_CALLBACK_FN),
                                ("Context", ctypes.c_void_p),
                            ]

                        def _power_cb(_ctx: Any, event_type: int, _setting: Any) -> int:
                            if event_type == PBT_APMSUSPEND:
                                guard_ref._handle_suspend(reason="s0ix_suspend")
                            elif event_type in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                                guard_ref._handle_resume(reason=f"s0ix_resume:{event_type}")
                            return 0

                        suspend_cb_ref = DEVICE_NOTIFY_CALLBACK_FN(_power_cb)
                        params = DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS(
                            Callback=suspend_cb_ref, Context=None
                        )
                        powrprof.PowerRegisterSuspendResumeNotification.argtypes = [
                            wintypes.DWORD,
                            ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p),
                        ]
                        powrprof.PowerRegisterSuspendResumeNotification.restype = wintypes.DWORD
                        powrprof.PowerUnregisterSuspendResumeNotification.argtypes = [ctypes.c_void_p]
                        powrprof.PowerUnregisterSuspendResumeNotification.restype = wintypes.DWORD
                        reg_handle = ctypes.c_void_p()
                        DEVICE_NOTIFY_CALLBACK = 2
                        if (
                            powrprof.PowerRegisterSuspendResumeNotification(
                                DEVICE_NOTIFY_CALLBACK,
                                ctypes.byref(params),
                                ctypes.byref(reg_handle),
                            )
                            == 0
                        ):
                            h_suspend_reg = reg_handle
                    except Exception as exc:
                        logger.debug("PowerRegisterSuspendResumeNotification skipped: %s", exc)

                msg = wintypes.MSG()
                while not self._stop_event.is_set():
                    res = user32.GetMessageW(ctypes.byref(msg), hwnd, 0, 0)
                    if res <= 0:
                        break
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                _ = suspend_cb_ref  # Keep callback alive across message loop
            except Exception as exc:
                logger.debug("Windows power broadcast listener unavailable: %s", exc)
            finally:
                if h_suspend_reg and powrprof is not None:
                    try:
                        powrprof.PowerUnregisterSuspendResumeNotification(h_suspend_reg)
                    except Exception:
                        pass
                if h_power_notify and user32 is not None:
                    try:
                        user32.UnregisterPowerSettingNotification(h_power_notify)
                    except Exception:
                        pass

        self._power_thread = threading.Thread(
            target=_pump, name="SleepHandoffPowerListener", daemon=True
        )
        self._power_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._hwnd and sys.platform == "win32":
            try:
                ctypes.windll.user32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # type: ignore[attr-defined]
            except Exception:
                pass
