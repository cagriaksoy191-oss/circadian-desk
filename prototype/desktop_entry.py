"""Thin PyInstaller entrypoint for zero-Python `CircadianDesk.exe`.

Enables Per-Monitor V2 DPI awareness before starting `orchestrator.daemon.main()` so
window/monitor bounding rects match 1:1 across 125%/150%/175% Windows scaling, while
keeping frozen double-click defaults and `--noconsole` `app.log` safety intact.
"""

from __future__ import annotations

import multiprocessing
import sys

from orchestrator.daemon import APP_VERSION, main
from orchestrator.display.windows_display import enable_per_monitor_dpi_awareness


__version__ = APP_VERSION


def _entrypoint() -> int:
    multiprocessing.freeze_support()
    try:
        enable_per_monitor_dpi_awareness()
    except Exception:
        pass
    return main()


if __name__ == "__main__":
    sys.exit(_entrypoint())
