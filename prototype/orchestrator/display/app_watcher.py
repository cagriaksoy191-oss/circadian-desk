"""Zero-screen-capture foreground application watcher for automatic pause (stdlib only).

Detects color-critical creative applications (Photoshop, Lightroom, Premiere, DaVinci
Resolve, Figma, Blender, etc.) and borderless/exclusive fullscreen applications (games,
fullscreen video) strictly via Win32 window metadata (`GetForegroundWindow`, process
executable basename, window class, window style, and monitor bounding rect).

Privacy Guarantee:
- ZERO screen pixel capture (no DXGI Desktop Duplication, no BitBlt, no screenshots).
- Reads only the active window's executable filename, class name, style bits, and rect.
- Gracefully no-ops on non-Windows platforms or access-denied system processes.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import sys
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)

DEFAULT_COLOR_CRITICAL_APPS: tuple[str, ...] = (
    "photoshop.exe",
    "lightroom.exe",
    "lightroomclassic.exe",
    "illustrator.exe",
    "afterfx.exe",
    "adobe premiere pro.exe",
    "resolve.exe",
    "figma.exe",
    "blender.exe",
    "affinityphoto2.exe",
    "affinitydesigner2.exe",
    "captureone.exe",
    "gimp-2.10.exe",
    "gimp-3.0.exe",
    "krita.exe",
    "obs64.exe",
)

BROWSER_APPS: frozenset[str] = frozenset(
    {
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "brave.exe",
        "opera.exe",
        "opera_gx.exe",
        "vivaldi.exe",
        "waterfox.exe",
        "zen.exe",
        "arc.exe",
    }
)

DEDICATED_VIDEO_PLAYERS: frozenset[str] = frozenset(
    {
        "vlc.exe",
        "mpv.exe",
        "mpc-hc64.exe",
        "mpc-hc.exe",
        "mpc-be64.exe",
        "potplayermini64.exe",
        "potplayermini.exe",
        "mediaplayer.exe",
        "video.ui.exe",
        "kodi.exe",
        "plex.exe",
        "stremio.exe",
    }
)

SHELL_WINDOW_CLASSES: frozenset[str] = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
    }
)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MONITOR_DEFAULTTONEAREST = 0x00000002
_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_QUNS_RUNNING_D3D_FULL_SCREEN = 3


@dataclass
class ForegroundAppInfo:
    """Metadata describing the currently active desktop foreground window."""

    exe_name: str = ""
    window_class: str = ""
    is_fullscreen: bool = False
    is_shell_window: bool = False


@dataclass
class AutoPauseDecision:
    """Result of evaluating whether circadian display/lamp adjustments should auto-pause."""

    should_pause: bool = False
    reason: str = ""
    exe_name: str = ""
    trigger_kind: str = ""  # "" | "color_critical" | "fullscreen"


def _is_shell_info(exe_name: str, window_class: str, flagged: bool = False) -> bool:
    if flagged:
        return True
    if window_class in SHELL_WINDOW_CLASSES:
        return True
    if exe_name.strip().lower() == "explorer.exe":
        return True
    return False


def _query_win32_foreground() -> ForegroundAppInfo | None:
    """Query active foreground window metadata on Windows (read-only, 64-bit safe, zero pixels)."""
    if sys.platform != "win32":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND

        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int

        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL

        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HMONITOR

        user32.GetMonitorInfoW.argtypes = [
            wintypes.HMONITOR,
            ctypes.POINTER(_MONITORINFO),
        ]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE

        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        # Window class name
        cls_buf = ctypes.create_unicode_buffer(256)
        cls_len = user32.GetClassNameW(hwnd, cls_buf, len(cls_buf))
        window_class = cls_buf.value if cls_len > 0 else ""

        # Process executable basename (read-only PROCESS_QUERY_LIMITED_INFORMATION)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe_name = ""
        if pid.value:
            hproc = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if hproc:
                try:
                    path_buf = ctypes.create_unicode_buffer(1024)
                    path_len = wintypes.DWORD(len(path_buf))
                    if kernel32.QueryFullProcessImageNameW(
                        hproc, 0, path_buf, ctypes.byref(path_len)
                    ):
                        exe_name = Path(path_buf.value).name
                finally:
                    kernel32.CloseHandle(hproc)

        is_shell = _is_shell_info(exe_name, window_class, flagged=False)

        # Borderless / exclusive / browser HTML5 fullscreen check:
        # Uses DWMWA_EXTENDED_FRAME_BOUNDS (falling back to GetWindowRect) + SHQueryUserNotificationState
        # (ONLY QUNS_RUNNING_D3D_FULL_SCREEN == 3, NEVER state == 2!).
        # Normal maximized Chrome/VS Code only covers rcWork (excluding taskbar), whereas true
        # fullscreen covers full rcMonitor (including taskbar), even when Chromium keeps WS_CAPTION.
        is_fullscreen = False
        if not is_shell:
            d3d_exclusive = False
            try:
                shell32 = ctypes.WinDLL("shell32", use_last_error=True)
                shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
                shell32.SHQueryUserNotificationState.restype = ctypes.c_long
                quns = ctypes.c_int(0)
                if shell32.SHQueryUserNotificationState(ctypes.byref(quns)) == 0:
                    d3d_exclusive = quns.value == _QUNS_RUNNING_D3D_FULL_SCREEN
            except Exception:
                d3d_exclusive = False

            rect = wintypes.RECT()
            got_rect = False
            try:
                dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
                dwmapi.DwmGetWindowAttribute.argtypes = [
                    wintypes.HWND,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                ]
                dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
                if (
                    dwmapi.DwmGetWindowAttribute(
                        hwnd,
                        _DWMWA_EXTENDED_FRAME_BOUNDS,
                        ctypes.byref(rect),
                        ctypes.sizeof(rect),
                    )
                    == 0
                ):
                    got_rect = True
            except Exception:
                got_rect = False

            if not got_rect:
                got_rect = bool(user32.GetWindowRect(hwnd, ctypes.byref(rect)))

            hmon = user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if d3d_exclusive:
                is_fullscreen = True
            elif got_rect and hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                mon = mi.rcMonitor
                work = mi.rcWork
                mon_w = mon.right - mon.left
                mon_h = mon.bottom - mon.top
                tol = 2
                covers_monitor = (
                    mon_w > 0
                    and mon_h > 0
                    and rect.left <= mon.left + tol
                    and rect.top <= mon.top + tol
                    and rect.right >= mon.right - tol
                    and rect.bottom >= mon.bottom - tol
                )
                taskbar_present = (
                    (mon.left < work.left)
                    or (mon.top < work.top)
                    or (mon.right > work.right)
                    or (mon.bottom > work.bottom)
                )
                style = int(user32.GetWindowLongW(hwnd, _GWL_STYLE))
                has_caption = bool(style & _WS_CAPTION)
                is_fullscreen = covers_monitor and (taskbar_present or not has_caption)

        return ForegroundAppInfo(
            exe_name=exe_name,
            window_class=window_class,
            is_fullscreen=is_fullscreen,
            is_shell_window=is_shell,
        )
    except Exception as exc:
        logger.debug("Foreground window query failed: %s", exc)
        return None


class ForegroundAppWatcher:
    """Evaluates foreground application metadata to trigger instant display auto-pause."""

    def __init__(
        self,
        enabled: bool = True,
        pause_on_fullscreen: bool = True,
        color_critical_apps: Sequence[str] | None = None,
        foreground_provider: Callable[[], ForegroundAppInfo | None] | None = None,
        pause_on_fullscreen_browser: bool = False,
        pause_on_video_players: bool = True,
        video_player_apps: Sequence[str] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.pause_on_fullscreen = bool(pause_on_fullscreen)
        self.pause_on_fullscreen_browser = bool(pause_on_fullscreen_browser)
        self.pause_on_video_players = bool(pause_on_video_players)
        apps = DEFAULT_COLOR_CRITICAL_APPS if color_critical_apps is None else color_critical_apps
        self.color_critical_apps: frozenset[str] = frozenset(
            str(a).strip().lower() for a in apps if str(a).strip()
        )
        v_apps = DEDICATED_VIDEO_PLAYERS if video_player_apps is None else video_player_apps
        self.video_player_apps: frozenset[str] = frozenset(
            str(a).strip().lower() for a in v_apps if str(a).strip()
        )
        self._provider: Callable[[], ForegroundAppInfo | None] = (
            foreground_provider if foreground_provider is not None else _query_win32_foreground
        )

    def get_foreground_exe(self) -> str:
        """Return the current foreground process basename (or empty string on error)."""
        try:
            info = self._provider()
        except Exception:
            return ""
        if info is None:
            return ""
        return str(info.exe_name or "").strip()

    def check(self) -> AutoPauseDecision:
        """Inspect current foreground app and return an AutoPauseDecision (never raises)."""
        if not self.enabled:
            return AutoPauseDecision(should_pause=False)

        try:
            info = self._provider()
        except Exception as exc:
            logger.debug("Foreground provider raised: %s", exc)
            return AutoPauseDecision(should_pause=False)

        if info is None:
            return AutoPauseDecision(should_pause=False)

        exe_raw = str(info.exe_name or "").strip()
        exe_lower = exe_raw.lower()
        win_class = str(info.window_class or "").strip()

        if _is_shell_info(exe_raw, win_class, flagged=bool(info.is_shell_window)):
            return AutoPauseDecision(should_pause=False)

        if exe_lower and exe_lower in self.color_critical_apps:
            return AutoPauseDecision(
                should_pause=True,
                reason=f"Color-critical app ({exe_raw})",
                exe_name=exe_raw,
                trigger_kind="color_critical",
            )

        if self.pause_on_video_players and exe_lower and exe_lower in self.video_player_apps:
            return AutoPauseDecision(
                should_pause=True,
                reason=f"Video player ({exe_raw})",
                exe_name=exe_raw,
                trigger_kind="video_player",
            )

        if bool(info.is_fullscreen):
            if exe_lower in BROWSER_APPS:
                if self.pause_on_fullscreen_browser:
                    return AutoPauseDecision(
                        should_pause=True,
                        reason=f"Fullscreen browser ({exe_raw})",
                        exe_name=exe_raw,
                        trigger_kind="fullscreen_browser",
                    )
                return AutoPauseDecision(should_pause=False)
            if self.pause_on_fullscreen:
                label = exe_raw or "active"
                return AutoPauseDecision(
                    should_pause=True,
                    reason=f"Fullscreen app ({label})",
                    exe_name=exe_raw,
                    trigger_kind="fullscreen",
                )

        return AutoPauseDecision(should_pause=False)
