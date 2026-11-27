"""100% Python stdlib Windows System Tray (`Shell_NotifyIconW`) & Braun `.ico` generator.

Zero external packages (`pystray` / `Pillow` NOT required):
- `generate_braun_ico_bytes(size=32)`: synthesizes a universal 32-bit BGRA BMP-payload
  Windows `.ico` (compatible with both Win32 `LoadImageW` and PyInstaller PE icons)
  depicting a Braun warm light-gray rounded-square bezel, matte dark ring, and warm
  amber (`#E88D2A`) center indicator dot.
- `TrayActionController`: pure testable controller decoupling tray clicks/menu commands
  from Win32 message-pump plumbing.
- `WindowsSystemTray`: Win64-safe `Shell_NotifyIconW` daemon-thread tray icon with
  dynamic context menu, tooltip updates, and clean `NIM_DELETE` teardown (zero ghost icons).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from typing import Any, Callable
import webbrowser
import zlib

logger = logging.getLogger(__name__)

# Menu command IDs
_IDM_OPEN_DASHBOARD = 1001
_IDM_TOGGLE_PAUSE = 1002
_IDM_TOGGLE_MOVIE_MODE = 1003
_IDM_TOGGLE_AUTOPAUSE = 1004
_IDM_TOGGLE_BROWSER_FS = 1005
_IDM_TOGGLE_SENSITIVE = 1006
_IDM_QUIT = 1007

# Global Win32 Hotkey IDs & Constants
HOTKEY_ID_TOGGLE_PAUSE = 1
HOTKEY_ID_TOGGLE_MOVIE = 2
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_NOREPEAT = 0x4000
_VK_M = 0x4D
_VK_P = 0x50
_WM_HOTKEY = 0x0312


@dataclass
class TrayStatusSnapshot:
    """Optional status snapshot DTO for decoupled System Tray menu rendering."""

    paused: bool = False
    movie_mode_active: bool = False
    auto_pause_enabled: bool = True
    auto_pause_browser_fullscreen: bool = False
    high_sensitivity_evening: bool = False


@dataclass
class TrayCallbacks:
    """Optional callback bundle for decoupled System Tray & global hotkey actions."""

    on_open_dashboard: Callable[[], Any] | None = None
    on_toggle_pause: Callable[[], Any] | None = None
    on_pause_2h: Callable[[], Any] | None = None
    on_resume: Callable[[], Any] | None = None
    on_toggle_movie_mode: Callable[[], Any] | None = None
    on_toggle_auto_pause: Callable[[], Any] | None = None
    on_toggle_browser_fullscreen: Callable[[], Any] | None = None
    on_toggle_sensitive_evening: Callable[[], Any] | None = None
    on_quit: Callable[[], Any] | None = None


def _rounded_rect_sdf(px: float, py: float, half_w: float, half_h: float, radius: float) -> float:
    """Signed distance to an axis-aligned rounded rectangle centered at (0, 0)."""
    qx = abs(px) - (half_w - radius)
    qy = abs(py) - (half_h - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def _blend_over(dst: tuple[float, float, float, float], src: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Porter-Duff source-over alpha composite in RGBA [0..255] / [0..1]."""
    sr, sg, sb, sa = src
    dr, dg, db, da = dst
    out_a = sa + da * (1.0 - sa)
    if out_a <= 1e-6:
        return (0.0, 0.0, 0.0, 0.0)
    out_r = (sr * sa + dr * da * (1.0 - sa)) / out_a
    out_g = (sg * sa + dg * da * (1.0 - sa)) / out_a
    out_b = (sb * sa + db * da * (1.0 - sa)) / out_a
    return (out_r, out_g, out_b, out_a)


def _render_braun_rgba_grid(
    dim: int,
    *,
    light_theme: bool = False,
) -> list[list[tuple[int, int, int, int]]]:
    """Render 4x4 supersampled RGBA top-down pixel grid of the Braun industrial aperture icon."""
    half = dim * 0.5
    body_half = dim * 0.455
    corner_r = dim * 0.22
    ring_outer_r = dim * 0.325
    ring_inner_r = dim * 0.225
    amber_r = dim * 0.165
    samples = (0.125, 0.375, 0.625, 0.875)
    inv_n = 1.0 / (len(samples) * len(samples))

    # Slightly darker bezel rim on light-unplated Windows 11 light taskbar so silhouette stays crisp.
    rim_rgb = (142.0, 138.0, 128.0) if light_theme else (188.0, 185.0, 176.0)
    pixels: list[list[tuple[int, int, int, int]]] = []
    for y in range(dim):
        row: list[tuple[int, int, int, int]] = []
        for x in range(dim):
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in samples:
                for sx in samples:
                    px = (x + sx) - half
                    py = (y + sy) - half
                    d_box = _rounded_rect_sdf(px, py, body_half, body_half, corner_r)
                    col = (0.0, 0.0, 0.0, 0.0)
                    if d_box <= 0.0:
                        grad = max(0.0, min(1.0, (py + body_half) / (2.0 * body_half)))
                        base_r = 240.0 - 12.0 * grad
                        base_g = 238.0 - 13.0 * grad
                        base_b = 232.0 - 14.0 * grad
                        if d_box > -max(1.0, dim * 0.045):
                            col = (rim_rgb[0], rim_rgb[1], rim_rgb[2], 1.0)
                        else:
                            col = (base_r, base_g, base_b, 1.0)

                        rad = math.hypot(px, py)
                        if rad <= ring_outer_r:
                            if rad >= ring_inner_r:
                                col = _blend_over(col, (36.0, 36.0, 33.0, 1.0))
                            else:
                                col = _blend_over(col, (24.0, 24.0, 22.0, 1.0))

                        if rad <= ring_inner_r:
                            if rad <= amber_r:
                                spec_d = math.hypot(px + dim * 0.035, py + dim * 0.035)
                                spec = max(0.0, 1.0 - (spec_d / (amber_r * 1.15)))
                                ar = min(255.0, 232.0 + 23.0 * spec)
                                ag = min(255.0, 141.0 + 58.0 * spec)
                                ab = min(255.0, 42.0 + 48.0 * spec)
                                col = _blend_over(col, (ar, ag, ab, 1.0))
                            else:
                                glow = max(
                                    0.0,
                                    1.0 - (rad - amber_r) / max(1e-6, ring_inner_r - amber_r),
                                )
                                col = _blend_over(col, (232.0, 141.0, 42.0, 0.35 * glow * glow))

                    acc_r += col[0] * col[3]
                    acc_g += col[1] * col[3]
                    acc_b += col[2] * col[3]
                    acc_a += col[3]

            a_avg = acc_a * inv_n
            if a_avg > 1e-6:
                r_u8 = max(0, min(255, int(round((acc_r * inv_n) / a_avg))))
                g_u8 = max(0, min(255, int(round((acc_g * inv_n) / a_avg))))
                b_u8 = max(0, min(255, int(round((acc_b * inv_n) / a_avg))))
                a_u8 = max(0, min(255, int(round(a_avg * 255.0))))
            else:
                r_u8 = g_u8 = b_u8 = a_u8 = 0
            row.append((r_u8, g_u8, b_u8, a_u8))
        pixels.append(row)
    return pixels


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def generate_braun_png_bytes(size: int = 150, *, light_theme: bool = False) -> bytes:
    """Generate a transparent 32-bit RGBA PNG of the Braun icon (100% stdlib, zero Pillow).

    Used to synthesize the complete Microsoft Store MSIX icon & unplated taskbar asset matrix
    (every generated asset is ~1-20 KB, well below Microsoft Store's 204,800-byte limit).
    """
    dim = int(size)
    if dim < 16 or dim > 600:
        raise ValueError(f"PNG size must be in [16, 600], got {size}")
    grid = _render_braun_rgba_grid(dim, light_theme=light_theme)
    raw = bytearray()
    for row in grid:
        raw.append(0)  # Filter type 0 (None)
        for r_u8, g_u8, b_u8, a_u8 in row:
            raw.extend((r_u8, g_u8, b_u8, a_u8))
    ihdr = struct.pack(">IIBBBBB", dim, dim, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(bytes(raw), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def format_live_tray_tooltip(
    *,
    phase: str,
    mon_pct: int,
    mon_cct: int,
    target_nits: float = 0.0,
    profile_label: str = "Comfort",
    manual_paused: bool = False,
    pause_remaining_s: int = 0,
    auto_pause_reason: str = "",
    movie_mode_active: bool = False,
    awaiting_setup: bool = False,
) -> str:
    """Format a zero-hook live instrument hover tooltip (clamped <= 127 chars for Win32)."""
    if awaiting_setup:
        tip = "Circadian Desk — Awaiting Setup (display untouched)"
    elif manual_paused:
        rem_m = max(1, (int(pause_remaining_s) + 59) // 60) if pause_remaining_s > 0 else 0
        tip = (
            f"Circadian Desk — PAUSED {rem_m}m (display restored)"
            if rem_m > 0
            else "Circadian Desk — PAUSED (display restored)"
        )
    elif auto_pause_reason:
        if "hdr" in auto_pause_reason.lower():
            tip = "Circadian Desk — HDR Auto-Paused (display untouched)"
        else:
            tip = f"Circadian Desk — Auto-Paused ({auto_pause_reason})"
    elif movie_mode_active:
        tip = f"Circadian Desk — Movie Mode ({int(mon_cct)}K · {int(round(target_nits))} nit)"
    elif target_nits > 0.0:
        tip = f"Circadian Desk — {int(mon_cct)}K · {int(round(target_nits))} nit · {profile_label}"
    else:
        tip = f"Circadian Desk — {phase} ({int(mon_pct)}% @ {int(mon_cct)}K)"
    return _clamp_tooltip(tip)


def generate_braun_ico_bytes(size: int = 32) -> bytes:
    """Generate a universal 32-bit BGRA BMP-payload Windows `.ico` in Braun industrial style.

    Design: warm light-gray rounded-square bezel (#EAE8E1) with hairline edge (#C5C2B7),
    matte dark graphite aperture ring (#22221F), and glowing warm amber (#E88D2A) center dot.
    Uses 4x4 supersampling for crisp edges at 16px, 32px, and 48px taskbar scales.
    """
    dim = int(size)
    if dim < 16 or dim > 256:
        raise ValueError(f"ICO size must be in [16, 256], got {size}")

    rgba_grid = _render_braun_rgba_grid(dim, light_theme=False)

    # XOR bitmap (BGRA, bottom-up)
    xor_buf = bytearray()
    for y in range(dim - 1, -1, -1):
        for r_u8, g_u8, b_u8, a_u8 in rgba_grid[y]:
            xor_buf.extend((b_u8, g_u8, r_u8, a_u8))

    # AND mask (1-bpp, bottom-up, each scanline padded to 4 bytes; 1 = transparent, 0 = opaque)
    and_stride = ((dim + 31) // 32) * 4
    and_buf = bytearray()
    for y in range(dim - 1, -1, -1):
        line = bytearray(and_stride)
        for x in range(dim):
            _, _, _, a_u8 = rgba_grid[y][x]
            if a_u8 < 128:
                line[x // 8] |= 0x80 >> (x % 8)
        and_buf.extend(line)

    # BITMAPINFOHEADER (40 bytes; biHeight MUST be 2 * dim in ICO DIB format)
    dib_header = struct.pack(
        "<IIIHHIIIIII",
        40,                       # biSize
        dim,                      # biWidth
        dim * 2,                  # biHeight (XOR + AND)
        1,                        # biPlanes
        32,                       # biBitCount (32-bit BGRA)
        0,                        # biCompression (BI_RGB)
        len(xor_buf) + len(and_buf),  # biSizeImage
        0,                        # biXPelsPerMeter
        0,                        # biYPelsPerMeter
        0,                        # biClrUsed
        0,                        # biClrImportant
    )
    image_data = dib_header + bytes(xor_buf) + bytes(and_buf)

    # ICONDIR (6 bytes) + ICONDIRENTRY (16 bytes)
    wh_byte = 0 if dim == 256 else dim
    icon_dir = struct.pack("<HHH", 0, 1, 1)
    dir_entry = struct.pack(
        "<BBBBHHII",
        wh_byte,                  # bWidth
        wh_byte,                  # bHeight
        0,                        # bColorCount
        0,                        # bReserved
        1,                        # wPlanes
        32,                       # wBitCount
        len(image_data),          # dwBytesInRes
        6 + 16,                   # dwImageOffset
    )
    return icon_dir + dir_entry + image_data


def ensure_icon_file(path: str | Path, size: int = 32, overwrite: bool = False) -> Path:
    """Write the Braun `.ico` file if missing/empty (or when overwrite=True) and return its Path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or (not target.exists()) or target.stat().st_size < 22:
        target.write_bytes(generate_braun_ico_bytes(size=size))
    return target


class TrayActionController:
    """Pure, testable action bridge between System Tray menu events and OrchestratorState."""

    def __init__(
        self,
        state: Any,
        stop_event: threading.Event,
        ui_url: str = "http://127.0.0.1:8765",
        open_url_fn: Callable[[str], Any] | None = None,
        callbacks: TrayCallbacks | None = None,
        status_provider: Callable[[], TrayStatusSnapshot] | None = None,
    ) -> None:
        self.state = state
        self.stop_event = stop_event
        self.ui_url = str(ui_url or "http://127.0.0.1:8765")
        self._open_url_fn = open_url_fn if open_url_fn is not None else webbrowser.open
        self.callbacks = callbacks
        self._status_provider = status_provider

    def _snapshot(self) -> TrayStatusSnapshot | None:
        if self._status_provider is None:
            return None
        try:
            return self._status_provider()
        except Exception:
            return None

    def open_dashboard(self) -> bool:
        """Open the local Braun control panel URL in the user's default browser."""
        if self.callbacks is not None and self.callbacks.on_open_dashboard is not None:
            try:
                res = self.callbacks.on_open_dashboard()
                return True if res is None else bool(res)
            except Exception as exc:
                logger.debug("on_open_dashboard callback failed: %s", exc)
                return False
        try:
            res = self._open_url_fn(self.ui_url)
            return True if res is None else bool(res)
        except Exception as exc:
            logger.debug("open_dashboard failed: %s", exc)
            return False

    def is_paused(self) -> bool:
        """Read current manual-pause status safely from state."""
        snap_dto = self._snapshot()
        if snap_dto is not None:
            return bool(snap_dto.paused)
        if self.state is None:
            return False
        if hasattr(self.state, "is_paused"):
            res = self.state.is_paused()
            if isinstance(res, tuple):
                return bool(res[0])
            return bool(res)
        if hasattr(self.state, "to_dict"):
            return bool(self.state.to_dict().get("paused", False))
        return False

    def toggle_pause_2h(self) -> bool:
        """Toggle 2-hour pause: resumes (0 min) if paused, else pauses for 120 minutes."""
        if self.callbacks is not None:
            if self.callbacks.on_toggle_pause is not None:
                res = self.callbacks.on_toggle_pause()
                return self.is_paused() if res is None else bool(res)
            if self.is_paused() and self.callbacks.on_resume is not None:
                self.callbacks.on_resume()
                return False
            if (not self.is_paused()) and self.callbacks.on_pause_2h is not None:
                self.callbacks.on_pause_2h()
                return True
        if self.state is None or not hasattr(self.state, "set_pause"):
            return False
        if self.is_paused():
            self.state.set_pause(0)
            return False
        self.state.set_pause(120)
        return True

    def is_movie_mode_active(self) -> bool:
        """Read current Movie Mode (2.5h — 3400K) state safely."""
        snap_dto = self._snapshot()
        if snap_dto is not None:
            return bool(snap_dto.movie_mode_active)
        if self.state is None:
            return False
        if hasattr(self.state, "is_movie_mode"):
            res = self.state.is_movie_mode()
            if isinstance(res, tuple):
                return bool(res[0])
            return bool(res)
        if hasattr(self.state, "to_dict"):
            return bool(self.state.to_dict().get("movie_mode_active", False))
        return bool(getattr(self.state, "movie_mode_active", False))

    def toggle_movie_mode(self) -> bool:
        """Toggle 2.5h 3400K Movie Mode cleanly via callback or OrchestratorState."""
        if self.callbacks is not None and self.callbacks.on_toggle_movie_mode is not None:
            res = self.callbacks.on_toggle_movie_mode()
            return self.is_movie_mode_active() if res is None else bool(res)
        active = self.is_movie_mode_active()
        if self.state is not None and hasattr(self.state, "set_movie_mode"):
            self.state.set_movie_mode(0 if active else 150)
            return not active
        if self.state is not None and hasattr(self.state, "set_manual_override"):
            if active:
                self.state.set_manual_override(enabled=False)
                return False
            self.state.set_manual_override(enabled=True, brightness_pct=65, cct_k=3400)
            return True
        return False

    def get_auto_pause_enabled(self) -> bool:
        snap_dto = self._snapshot()
        if snap_dto is not None:
            return bool(snap_dto.auto_pause_enabled)
        if self.state is None:
            return True
        if hasattr(self.state, "to_dict"):
            snap = self.state.to_dict()
            if isinstance(snap, dict) and "auto_pause_enabled" in snap:
                return bool(snap["auto_pause_enabled"])
        return bool(getattr(self.state, "auto_pause_enabled", True))

    def toggle_auto_pause(self) -> bool:
        """Flip foreground Games/Creative auto-pause setting on OrchestratorState."""
        if self.callbacks is not None and self.callbacks.on_toggle_auto_pause is not None:
            res = self.callbacks.on_toggle_auto_pause()
            return self.get_auto_pause_enabled() if res is None else bool(res)
        new_val = not self.get_auto_pause_enabled()
        if self.state is not None and hasattr(self.state, "update_settings"):
            try:
                self.state.update_settings(auto_pause_enabled=new_val)
                return new_val
            except TypeError:
                pass
        if self.state is not None and hasattr(self.state, "auto_pause_enabled"):
            try:
                setattr(self.state, "auto_pause_enabled", new_val)
            except Exception:
                pass
        return new_val

    def get_browser_fullscreen_auto_pause(self) -> bool:
        snap_dto = self._snapshot()
        if snap_dto is not None:
            return bool(snap_dto.auto_pause_browser_fullscreen)
        if self.state is None:
            return False
        if hasattr(self.state, "to_dict"):
            snap = self.state.to_dict()
            if isinstance(snap, dict) and "auto_pause_browser_fullscreen" in snap:
                return bool(snap["auto_pause_browser_fullscreen"])
        return bool(getattr(self.state, "auto_pause_browser_fullscreen", False))

    def toggle_browser_fullscreen_auto_pause(self) -> bool:
        """Flip opt-in fullscreen browser video auto-pause setting cleanly."""
        if self.callbacks is not None and self.callbacks.on_toggle_browser_fullscreen is not None:
            res = self.callbacks.on_toggle_browser_fullscreen()
            return self.get_browser_fullscreen_auto_pause() if res is None else bool(res)
        new_val = not self.get_browser_fullscreen_auto_pause()
        if self.state is not None and hasattr(self.state, "update_settings"):
            try:
                self.state.update_settings(auto_pause_browser_fullscreen=new_val)
                return new_val
            except TypeError:
                pass
        if self.state is not None and hasattr(self.state, "auto_pause_browser_fullscreen"):
            try:
                setattr(self.state, "auto_pause_browser_fullscreen", new_val)
            except Exception:
                pass
        return new_val

    def get_sensitive_evening(self) -> bool:
        snap_dto = self._snapshot()
        if snap_dto is not None:
            return bool(snap_dto.high_sensitivity_evening)
        if self.state is None:
            return False
        if hasattr(self.state, "to_dict"):
            snap = self.state.to_dict()
            if isinstance(snap, dict) and "high_sensitivity_evening" in snap:
                return bool(snap["high_sensitivity_evening"])
        if hasattr(self.state, "get_settings"):
            settings = self.state.get_settings()
            if isinstance(settings, tuple) and len(settings) >= 3:
                return bool(settings[2])
        return bool(getattr(self.state, "high_sensitivity_evening", False))

    def toggle_sensitive_evening(self) -> bool:
        """Flip Strict Circadian (2200K / <=5 lx) setting on OrchestratorState."""
        if self.callbacks is not None and self.callbacks.on_toggle_sensitive_evening is not None:
            res = self.callbacks.on_toggle_sensitive_evening()
            return self.get_sensitive_evening() if res is None else bool(res)
        new_val = not self.get_sensitive_evening()
        if self.state is not None and hasattr(self.state, "update_settings"):
            self.state.update_settings(high_sensitivity_evening=new_val)
        return new_val

    def request_quit(self) -> None:
        """Signal the orchestrator main loop to shut down & restore display immediately."""
        if self.callbacks is not None and self.callbacks.on_quit is not None:
            try:
                self.callbacks.on_quit()
            except Exception:
                pass
        self.stop_event.set()


def _clamp_tooltip(text: str) -> str:
    clean = str(text or "Circadian Desk").replace("\r", " ").replace("\n", " ").strip()
    return clean[:127]


class WindowsSystemTray:
    """Native Win64-safe `Shell_NotifyIconW` system tray icon running on a daemon thread."""

    def __init__(
        self,
        controller: TrayActionController,
        tooltip: str = "Circadian Desk — Local Light Orchestrator",
        icon_path: str | Path | None = None,
    ) -> None:
        self.controller = controller
        self._tooltip = _clamp_tooltip(tooltip)
        self._icon_path = Path(icon_path) if icon_path is not None else None

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._started_ok: bool = False
        self._running: bool = False
        self._hwnd: int = 0
        self._hicon: int = 0
        self._owns_hicon: bool = False
        self._temp_icon_path: Path | None = None
        self._wndproc_ref: Any | None = None
        self._nid_uid: int = 1
        self._wm_tray: int = 0x8001  # WM_APP + 1
        self._last_open_ts: float = 0.0

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def _resolve_icon_path(self) -> Path:
        if self._icon_path is not None:
            if self._icon_path.is_file() and self._icon_path.stat().st_size >= 22:
                return self._icon_path
            try:
                return ensure_icon_file(self._icon_path)
            except OSError:
                pass
        default_asset = Path(__file__).resolve().parents[1] / "assets" / "circadian_desk.ico"
        if default_asset.is_file() and default_asset.stat().st_size >= 22:
            return default_asset
        try:
            return ensure_icon_file(default_asset)
        except OSError:
            tmp_path = Path(tempfile.gettempdir()) / "circadian_desk_tray.ico"
            self._temp_icon_path = ensure_icon_file(tmp_path)
            return self._temp_icon_path

    def start(self) -> bool:
        """Start the Windows System Tray daemon thread; returns False cleanly on non-Windows."""
        if sys.platform != "win32":
            return False
        with self._lock:
            if self._running:
                return True
            self._ready_event.clear()
            self._started_ok = False
            self._thread = threading.Thread(
                target=self._run_message_loop,
                name="CircadianDeskWin32Tray",
                daemon=True,
            )
            self._thread.start()

        self._ready_event.wait(timeout=3.0)
        return self._started_ok

    def _build_nid(self, ctypes_mod: Any, wintypes_mod: Any, hwnd: int, flags: int, tip: str = "") -> Any:
        class _GUID(ctypes_mod.Structure):
            _fields_ = [
                ("Data1", wintypes_mod.DWORD),
                ("Data2", wintypes_mod.WORD),
                ("Data3", wintypes_mod.WORD),
                ("Data4", ctypes_mod.c_ubyte * 8),
            ]

        class _NOTIFYICONDATAW(ctypes_mod.Structure):
            _fields_ = [
                ("cbSize", wintypes_mod.DWORD),
                ("hWnd", wintypes_mod.HWND),
                ("uID", wintypes_mod.UINT),
                ("uFlags", wintypes_mod.UINT),
                ("uCallbackMessage", wintypes_mod.UINT),
                ("hIcon", wintypes_mod.HICON),
                ("szTip", ctypes_mod.c_wchar * 128),
                ("dwState", wintypes_mod.DWORD),
                ("dwStateMask", wintypes_mod.DWORD),
                ("szInfo", ctypes_mod.c_wchar * 256),
                ("uTimeoutOrVersion", wintypes_mod.UINT),
                ("szInfoTitle", ctypes_mod.c_wchar * 64),
                ("dwInfoFlags", wintypes_mod.DWORD),
                ("guidItem", _GUID),
                ("hBalloonIcon", wintypes_mod.HICON),
            ]

        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes_mod.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = self._nid_uid
        nid.uFlags = flags
        nid.uCallbackMessage = self._wm_tray
        nid.hIcon = self._hicon
        nid.szTip = _clamp_tooltip(tip)
        return nid

    def update_tooltip(self, text: str) -> bool:
        """Update the taskbar hover tooltip via `NIM_MODIFY` (clamped <= 127 chars)."""
        self._tooltip = _clamp_tooltip(text)
        if sys.platform != "win32":
            return False
        with self._lock:
            hwnd = self._hwnd
            running = self._running
        if not running or not hwnd:
            return False

        try:
            import ctypes
            from ctypes import wintypes

            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
            shell32.Shell_NotifyIconW.restype = wintypes.BOOL

            NIF_TIP = 0x00000004
            NIM_MODIFY = 0x00000001
            nid = self._build_nid(ctypes, wintypes, hwnd, NIF_TIP, self._tooltip)
            return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
        except Exception as exc:
            logger.debug("update_tooltip failed: %s", exc)
            return False

    def stop(self) -> None:
        """Remove tray icon (`NIM_DELETE`) and terminate message loop cleanly (idempotent)."""
        if sys.platform != "win32":
            return
        with self._lock:
            hwnd = self._hwnd
            thread = self._thread
            self._running = False

        if hwnd:
            try:
                import ctypes
                from ctypes import wintypes

                shell32 = ctypes.WinDLL("shell32", use_last_error=True)
                shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
                shell32.Shell_NotifyIconW.restype = wintypes.BOOL

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostMessageW.argtypes = [
                    wintypes.HWND,
                    wintypes.UINT,
                    ctypes.c_size_t,
                    ctypes.c_ssize_t,
                ]
                user32.PostMessageW.restype = wintypes.BOOL

                NIM_DELETE = 0x00000002
                WM_CLOSE = 0x0010
                nid = self._build_nid(ctypes, wintypes, hwnd, 0, "")
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception as exc:
                logger.debug("Tray stop signal failed: %s", exc)

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run_message_loop(self) -> None:
        import ctypes
        from ctypes import wintypes

        LRESULT = ctypes.c_ssize_t
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

        class _WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        # Explicit Win64-safe signatures
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM

        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.UnregisterClassW.restype = wintypes.BOOL

        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND

        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL

        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL

        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = LRESULT

        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.PostQuitMessage.restype = None

        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL

        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE

        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.LoadIconW.restype = wintypes.HICON

        user32.DestroyIcon.argtypes = [wintypes.HICON]
        user32.DestroyIcon.restype = wintypes.BOOL

        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = wintypes.HMENU

        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.AppendMenuW.restype = wintypes.BOOL

        user32.SetMenuDefaultItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
        user32.SetMenuDefaultItem.restype = wintypes.BOOL

        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL

        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, wintypes.LPVOID,
        ]
        user32.TrackPopupMenu.restype = wintypes.BOOL

        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL

        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterWindowMessageW.restype = wintypes.UINT

        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL

        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL

        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        WM_NULL = 0x0000
        WM_DESTROY = 0x0002
        WM_CLOSE = 0x0010
        WM_LBUTTONUP = 0x0202
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205
        WM_CONTEXTMENU = 0x007B
        WM_TASKBARCREATED = int(user32.RegisterWindowMessageW("TaskbarCreated") or 0)

        NIF_MESSAGE = 0x00000001
        NIF_ICON = 0x00000002
        NIF_TIP = 0x00000004
        NIM_ADD = 0x00000000
        NIM_DELETE = 0x00000002

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040

        MF_STRING = 0x00000000
        MF_SEPARATOR = 0x00000800
        TPM_LEFTALIGN = 0x0000
        TPM_BOTTOMALIGN = 0x0020
        TPM_RIGHTBUTTON = 0x0002
        TPM_RETURNCMD = 0x0100

        def _show_context_menu(hwnd: int) -> None:
            hmenu = user32.CreatePopupMenu()
            if not hmenu:
                return
            try:
                paused = self.controller.is_paused()
                movie_on = self.controller.is_movie_mode_active()
                auto_on = self.controller.get_auto_pause_enabled()
                browser_fs_on = self.controller.get_browser_fullscreen_auto_pause()
                sens_on = self.controller.get_sensitive_evening()

                pause_label = "Resume Now (Ctrl+Alt+P)" if paused else "Pause 2 Hours (Ctrl+Alt+P)"
                movie_label = (
                    "Exit Movie Mode (Ctrl+Alt+M)"
                    if movie_on
                    else "Movie Mode 2.5h — 3400K (Ctrl+Alt+M)"
                )
                auto_label = f"Auto-Pause (Games / Creative): {'ON' if auto_on else 'OFF'}"
                browser_fs_label = f"Auto-Pause Browser Fullscreen: {'ON' if browser_fs_on else 'OFF'}"
                sens_label = f"Strict Circadian (2200K / <=5 lx): {'ON' if sens_on else 'OFF'}"

                user32.AppendMenuW(hmenu, MF_STRING, _IDM_OPEN_DASHBOARD, "Open Control Panel (127.0.0.1)")
                user32.AppendMenuW(hmenu, MF_STRING, _IDM_TOGGLE_PAUSE, pause_label)
                user32.AppendMenuW(hmenu, MF_STRING, _IDM_TOGGLE_MOVIE_MODE, movie_label)
                user32.AppendMenuW(hmenu, MF_STRING, _IDM_TOGGLE_AUTOPAUSE, auto_label)
                user32.AppendMenuW(hmenu, MF_STRING, _IDM_TOGGLE_BROWSER_FS, browser_fs_label)
                user32.AppendMenuW(hmenu, MF_STRING, _IDM_TOGGLE_SENSITIVE, sens_label)
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                user32.AppendMenuW(hmenu, MF_STRING, _IDM_QUIT, "Quit && Restore Display")
                user32.SetMenuDefaultItem(hmenu, _IDM_OPEN_DASHBOARD, 0)

                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                user32.SetForegroundWindow(hwnd)
                cmd = int(
                    user32.TrackPopupMenu(
                        hmenu,
                        TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD,
                        pt.x,
                        pt.y,
                        0,
                        hwnd,
                        None,
                    )
                )
                user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            finally:
                user32.DestroyMenu(hmenu)

            if cmd == _IDM_OPEN_DASHBOARD:
                self.controller.open_dashboard()
            elif cmd == _IDM_TOGGLE_PAUSE:
                self.controller.toggle_pause_2h()
            elif cmd == _IDM_TOGGLE_MOVIE_MODE:
                self.controller.toggle_movie_mode()
            elif cmd == _IDM_TOGGLE_AUTOPAUSE:
                self.controller.toggle_auto_pause()
            elif cmd == _IDM_TOGGLE_BROWSER_FS:
                self.controller.toggle_browser_fullscreen_auto_pause()
            elif cmd == _IDM_TOGGLE_SENSITIVE:
                self.controller.toggle_sensitive_evening()
            elif cmd == _IDM_QUIT:
                self.controller.request_quit()
                nid_del = self._build_nid(ctypes, wintypes, hwnd, 0, "")
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid_del))
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

        def _wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if WM_TASKBARCREATED and msg == WM_TASKBARCREATED:
                nid_readd = self._build_nid(
                    ctypes,
                    wintypes,
                    hwnd,
                    NIF_MESSAGE | NIF_ICON | NIF_TIP,
                    self._tooltip,
                )
                shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid_readd))
                return 0
            if msg == _WM_HOTKEY:
                hk_id = int(wparam)
                if hk_id == HOTKEY_ID_TOGGLE_PAUSE:
                    self.controller.toggle_pause_2h()
                    return 0
                if hk_id == HOTKEY_ID_TOGGLE_MOVIE:
                    self.controller.toggle_movie_mode()
                    return 0
            if msg == self._wm_tray:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    now = time.monotonic()
                    if now - self._last_open_ts >= 0.45:
                        self._last_open_ts = now
                        self.controller.open_dashboard()
                    return 0
                if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    _show_context_menu(hwnd)
                    return 0
            elif msg == WM_CLOSE:
                nid_del = self._build_nid(ctypes, wintypes, hwnd, 0, "")
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid_del))
                user32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

        class_name = f"CircadianDeskTray_{id(self)}"
        hinstance = kernel32.GetModuleHandleW(None)
        hwnd = 0
        added_icon = False
        entered_loop = False

        try:
            self._wndproc_ref = WNDPROC(_wnd_proc)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinstance
            wc.lpszClassName = class_name
            atom = user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                raise OSError("RegisterClassW failed")

            hwnd = int(
                user32.CreateWindowExW(
                    0, class_name, "CircadianDeskTray", 0,
                    0, 0, 0, 0,
                    None, None, hinstance, None,
                )
            )
            if not hwnd:
                raise OSError("CreateWindowExW failed")

            # Load Braun .ico file (fallback cleanly to default application icon)
            ico_file = self._resolve_icon_path()
            hicon = int(
                user32.LoadImageW(
                    None,
                    str(ico_file),
                    IMAGE_ICON,
                    0,
                    0,
                    LR_LOADFROMFILE | LR_DEFAULTSIZE,
                )
                or 0
            )
            if hicon:
                self._hicon = hicon
                self._owns_hicon = True
            else:
                self._hicon = int(user32.LoadIconW(None, ctypes.cast(32512, wintypes.LPCWSTR)) or 0)
                self._owns_hicon = False

            nid = self._build_nid(
                ctypes,
                wintypes,
                hwnd,
                NIF_MESSAGE | NIF_ICON | NIF_TIP,
                self._tooltip,
            )
            added_icon = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
            if not added_icon:
                raise OSError("Shell_NotifyIconW(NIM_ADD) failed")

            # Register global system-wide hotkeys (graceful degradation if already claimed by another app)
            hk_mods = _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT
            registered_hotkeys: list[int] = []
            for hk_id, vk_code, label in (
                (HOTKEY_ID_TOGGLE_PAUSE, _VK_P, "Ctrl+Alt+P"),
                (HOTKEY_ID_TOGGLE_MOVIE, _VK_M, "Ctrl+Alt+M"),
            ):
                try:
                    if user32.RegisterHotKey(hwnd, hk_id, hk_mods, vk_code):
                        registered_hotkeys.append(hk_id)
                    else:
                        logger.debug("Global hotkey %s unavailable (already registered).", label)
                except Exception as exc:
                    logger.debug("RegisterHotKey(%s) skipped: %s", label, exc)

            with self._lock:
                self._hwnd = hwnd
                self._running = True
                self._started_ok = True
            self._ready_event.set()

            entered_loop = True
            msg_struct = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg_struct), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg_struct))
                user32.DispatchMessageW(ctypes.byref(msg_struct))
        except Exception as exc:
            logger.warning("WindowsSystemTray failed to start: %s", exc)
            self._ready_event.set()
        finally:
            if hwnd:
                for hk_id in locals().get("registered_hotkeys", ()):
                    try:
                        user32.UnregisterHotKey(hwnd, hk_id)
                    except Exception:
                        pass
            if hwnd and added_icon:
                try:
                    nid_del = self._build_nid(ctypes, wintypes, hwnd, 0, "")
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid_del))
                except Exception:
                    pass
            if hwnd and not entered_loop:
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            with self._lock:
                self._hwnd = 0
                self._running = False
            if self._owns_hicon and self._hicon:
                try:
                    user32.DestroyIcon(self._hicon)
                except Exception:
                    pass
                self._hicon = 0
                self._owns_hicon = False
            try:
                user32.UnregisterClassW(class_name, hinstance)
            except Exception:
                pass
            if self._temp_icon_path is not None:
                try:
                    self._temp_icon_path.unlink(missing_ok=True)
                except Exception:
                    pass
                self._temp_icon_path = None
