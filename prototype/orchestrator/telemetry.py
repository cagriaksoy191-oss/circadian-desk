"""Stdlib-only CSV telemetry logger & offline Braun-minimalist compliance report generator.

Supports both Week-1 zero-sensor (`open_loop`) desktop beta sessions and multi-day
eye-level multi-spectral (`sensor` / `simulated`) m-EDI room logs.

Zero external dependencies: generates GitHub-flavored Markdown (`report_summary.md`)
and a 100% offline standalone HTML report (`report_summary.html`) with an embedded
pure-SVG m-EDI time-series chart (+/- k=2 uncertainty ribbon + 250 lx / 10 lx targets).

Scope & Metrology Hygiene:
- Strictly optical metrology & visual-comfort framing per CIE S 026:2018,
  CIE PS 001:2024, and Brown et al. (2022) daytime-active healthy adults (18-55).
- Zero medical, diagnostic, or clinical treatment claims.
"""

from __future__ import annotations

import csv
import html
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spectral.photobiology import ControlAction

CSV_COLUMNS: tuple[str, ...] = (
    "timestamp_iso",
    "mode",
    "phase",
    "m_edi_lx",
    "uncertainty_k2_lx",
    "compliance_status",
    "photopic_lx",
    "cct_k",
    "m_der",
    "flicker_hz",
    "bulb_power",
    "bulb_pct",
    "bulb_cct_k",
    "monitor_pct",
    "monitor_cct_k",
    "unlinked_alert",
    "sensor_occluded",
    "conflict_observer",
    "reading_valid",
)

SCOPE_DISCLAIMER = (
    "Optical metrology & visual-comfort reference targets follow CIE S 026:2018 / "
    "Brown et al. (2022) recommendations for healthy daytime-active adults aged 18-55 "
    "(Day >=250 lx m-EDI, Evening <=10 lx m-EDI [or <=5 lx high-sensitivity], Night <=1 lx m-EDI "
    "with <0.3 lx sensor floor). Expanded measurement uncertainty is reported at k=2 (+/-17%). "
    "This tool is a lighting-environment measurement and visual-comfort orchestrator, not a "
    "medical device, diagnostic instrument, or clinical treatment."
)


class TelemetryLogger:
    """Append-safe, flush-on-tick UTF-8 CSV logger for circadian orchestrator ticks."""

    def __init__(self, csv_path: str | Path) -> None:
        self.csv_path = Path(csv_path)
        self._file: Any | None = None
        self._writer: Any | None = None
        self._ensure_open()

    def _ensure_open(self) -> None:
        if self._file is not None and not self._file.closed:
            return
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = (not self.csv_path.exists()) or (self.csv_path.stat().st_size == 0)
        self._file = self.csv_path.open("a", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=list(CSV_COLUMNS))
        if needs_header:
            self._writer.writeheader()
            self._file.flush()

    def __enter__(self) -> "TelemetryLogger":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def log_tick(
        self,
        action: ControlAction,
        mode: str,
        photopic_lx: float = 0.0,
        cct_k: int = 0,
        m_der: float = 0.0,
        flicker_hz: float = 0.0,
        timestamp_iso: str | None = None,
    ) -> dict[str, Any]:
        """Serialize one ControlAction tick to CSV and flush immediately."""
        self._ensure_open()
        ts = (
            str(timestamp_iso).strip()
            if timestamp_iso is not None and str(timestamp_iso).strip()
            else datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        bulb_pct = int(getattr(action, "bulb_brightness_pct", 0))
        bulb_power = bool(getattr(action, "bulb_power", bulb_pct > 0))
        row: dict[str, Any] = {
            "timestamp_iso": ts,
            "mode": str(mode or "sensor"),
            "phase": str(getattr(action, "phase", "DAY")).upper(),
            "m_edi_lx": f"{float(getattr(action, 'measured_m_edi_lx', 0.0)):.2f}",
            "uncertainty_k2_lx": f"{float(getattr(action, 'uncertainty_k2_lx', 0.0)):.2f}",
            "compliance_status": str(getattr(action, "compliance_status", "BELIRSIZ")).upper(),
            "photopic_lx": f"{float(photopic_lx):.2f}",
            "cct_k": int(round(float(cct_k))),
            "m_der": f"{float(m_der):.3f}",
            "flicker_hz": f"{float(flicker_hz):.1f}",
            "bulb_power": "1" if bulb_power else "0",
            "bulb_pct": bulb_pct,
            "bulb_cct_k": int(getattr(action, "bulb_cct_k", 0)),
            "monitor_pct": int(getattr(action, "monitor_brightness_pct", 0)),
            "monitor_cct_k": int(getattr(action, "monitor_cct_k", 0)),
            "unlinked_alert": "1" if bool(getattr(action, "unlinked_light_alert", getattr(action, "unlinked_alert", False))) else "0",
            "sensor_occluded": "1" if bool(getattr(action, "sensor_occluded", False)) else "0",
            "conflict_observer": "1" if bool(getattr(action, "conflict_observer_active", False)) else "0",
            "reading_valid": "1" if bool(getattr(action, "reading_valid", True)) else "0",
        }
        assert self._writer is not None and self._file is not None
        self._writer.writerow(row)
        self._file.flush()
        return row

    def close(self) -> None:
        if self._file is not None and not self._file.closed:
            try:
                self._file.flush()
            finally:
                self._file.close()
        self._file = None
        self._writer = None


@dataclass
class _ParsedRow:
    timestamp_iso: str
    dt: datetime | None
    mode: str
    phase: str
    m_edi_lx: float
    uncertainty_k2_lx: float
    compliance_status: str
    photopic_lx: float
    cct_k: int
    m_der: float
    flicker_hz: float
    bulb_power: bool
    bulb_pct: int
    bulb_cct_k: int
    monitor_pct: int
    monitor_cct_k: int
    unlinked_alert: bool
    sensor_occluded: bool
    conflict_observer: bool
    reading_valid: bool


def _parse_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "t"):
        return True
    if s in ("0", "false", "no", "n", "f", ""):
        return False
    return default


def _parse_float(val: Any) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        out = float(s)
        return out if math.isfinite(out) else None
    except ValueError:
        return None


def _parse_int(val: Any, default: int = 0) -> int:
    f = _parse_float(val)
    return int(round(f)) if f is not None else default


def _parse_iso(ts: str) -> datetime | None:
    s = ts.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _load_rows(csv_path: Path) -> tuple[list[_ParsedRow], int]:
    rows: list[_ParsedRow] = []
    skipped = 0
    if not csv_path.exists():
        return rows, skipped

    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return rows, skipped
        for raw in reader:
            if not raw or all((v is None or str(v).strip() == "") for v in raw.values()):
                continue
            ts = str(raw.get("timestamp_iso") or "").strip()
            phase = str(raw.get("phase") or "").strip().upper()
            m_edi = _parse_float(raw.get("m_edi_lx"))
            u_k2 = _parse_float(raw.get("uncertainty_k2_lx"))
            if not ts or phase not in ("DAY", "EVENING", "NIGHT") or m_edi is None:
                skipped += 1
                continue
            photopic = _parse_float(raw.get("photopic_lx"))
            m_der = _parse_float(raw.get("m_der"))
            flicker = _parse_float(raw.get("flicker_hz"))
            rows.append(
                _ParsedRow(
                    timestamp_iso=ts,
                    dt=_parse_iso(ts),
                    mode=str(raw.get("mode") or "sensor").strip().lower(),
                    phase=phase,
                    m_edi_lx=max(0.0, m_edi),
                    uncertainty_k2_lx=max(0.0, u_k2 if u_k2 is not None else 0.0),
                    compliance_status=str(raw.get("compliance_status") or "BELIRSIZ").strip().upper(),
                    photopic_lx=max(0.0, photopic if photopic is not None else 0.0),
                    cct_k=_parse_int(raw.get("cct_k"), 0),
                    m_der=max(0.0, m_der if m_der is not None else 0.0),
                    flicker_hz=max(0.0, flicker if flicker is not None else 0.0),
                    bulb_power=_parse_bool(raw.get("bulb_power"), False),
                    bulb_pct=_parse_int(raw.get("bulb_pct"), 0),
                    bulb_cct_k=_parse_int(raw.get("bulb_cct_k"), 0),
                    monitor_pct=_parse_int(raw.get("monitor_pct"), 0),
                    monitor_cct_k=_parse_int(raw.get("monitor_cct_k"), 0),
                    unlinked_alert=_parse_bool(raw.get("unlinked_alert"), False),
                    sensor_occluded=_parse_bool(raw.get("sensor_occluded"), False),
                    conflict_observer=_parse_bool(raw.get("conflict_observer"), False),
                    reading_valid=_parse_bool(raw.get("reading_valid"), True),
                )
            )
    return rows, skipped


def _is_open_loop(row: _ParsedRow) -> bool:
    return row.mode == "open_loop" or row.compliance_status == "OPEN-LOOP"


def _phase_compliance_stats(rows: list[_ParsedRow], phase: str) -> dict[str, Any]:
    phase_rows = [r for r in rows if r.phase == phase]
    measured_rows = [r for r in phase_rows if not _is_open_loop(r)]
    open_loop_rows = len(phase_rows) - len(measured_rows)

    saglandi = sum(1 for r in measured_rows if r.compliance_status == "SAGLANDI")
    belirsiz = sum(1 for r in measured_rows if r.compliance_status == "BELIRSIZ")
    saglanmadi = sum(1 for r in measured_rows if r.compliance_status == "SAGLANMADI")
    denom = len(measured_rows)

    def _pct(n: int) -> float:
        return round((100.0 * n / denom), 1) if denom > 0 else 0.0

    return {
        "total": len(phase_rows),
        "measured": denom,
        "open_loop": open_loop_rows,
        "saglandi": saglandi,
        "belirsiz": belirsiz,
        "saglanmadi": saglanmadi,
        "saglandi_pct": _pct(saglandi),
        "belirsiz_pct": _pct(belirsiz),
        "saglanmadi_pct": _pct(saglanmadi),
    }


def _compute_open_loop_hours(rows: list[_ParsedRow]) -> float:
    """Sum active open-loop duration from consecutive timestamps (capped at 1h gap)."""
    total_s = 0.0
    prev_dt: datetime | None = None
    for row in rows:
        if _is_open_loop(row) and row.dt is not None:
            if prev_dt is not None:
                dt_s = (row.dt - prev_dt).total_seconds()
                if 0.0 < dt_s <= 3600.0:
                    total_s += dt_s
            prev_dt = row.dt
        else:
            prev_dt = None
    return total_s / 3600.0


def _downsample_indices(n: int, max_points: int = 360) -> list[int]:
    if n <= max_points:
        return list(range(n))
    step = (n - 1) / float(max_points - 1)
    idxs = sorted({int(round(i * step)) for i in range(max_points)})
    if idxs[-1] != n - 1:
        idxs.append(n - 1)
    return idxs


def _build_svg_chart(rows: list[_ParsedRow]) -> str:
    """Render a crisp offline Braun-minimalist pure-SVG m-EDI + +/-k=2 chart."""
    w, h = 920, 340
    pad_l, pad_r, pad_t, pad_b = 64, 24, 24, 44
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    measured = [r for r in rows if not _is_open_loop(r)]
    if not measured:
        return (
            f'<svg viewBox="0 0 {w} {h}" width="100%" height="auto" role="img" '
            'aria-label="Open-loop mode — no sensor m-EDI trace recorded">'
            f'<rect x="0" y="0" width="{w}" height="{h}" rx="6" fill="#FFFFFF" stroke="#DCDAD3"/>'
            f'<text x="{w / 2:.1f}" y="{h / 2 - 6:.1f}" text-anchor="middle" '
            'font-family="system-ui, -apple-system, sans-serif" font-size="14" fill="#5A5954">'
            'Open-loop session (no physical optical sensor ticks recorded yet)</text>'
            f'<text x="{w / 2:.1f}" y="{h / 2 + 16:.1f}" text-anchor="middle" '
            'font-family="ui-monospace, Consolas, monospace" font-size="11" fill="#8A8880">'
            'Connect an OLGA-8 sensor (--port COMx) or run --simulate to plot m-EDI &amp; k=2 bands'
            '</text></svg>'
        )

    max_val = max(
        320.0,
        max((r.m_edi_lx + r.uncertainty_k2_lx for r in measured if r.reading_valid), default=300.0) * 1.12,
    )
    y_max_log = math.log10(1.0 + max_val)

    def _y_px(val: float) -> float:
        clamped = max(0.0, val)
        frac = math.log10(1.0 + clamped) / y_max_log if y_max_log > 0 else 0.0
        return pad_t + plot_h * (1.0 - max(0.0, min(1.0, frac)))

    idxs = _downsample_indices(len(measured), max_points=360)
    sampled = [measured[i] for i in idxs]
    n_pts = len(sampled)

    def _x_px(k: int) -> float:
        if n_pts <= 1:
            return pad_l + plot_w * 0.5
        return pad_l + (plot_w * k / float(n_pts - 1))

    svg_parts: list[str] = [
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="auto" role="img" '
        'aria-label="Eye-level melanopic EDI time series with k=2 uncertainty band">',
        f'<rect x="0" y="0" width="{w}" height="{h}" rx="6" fill="#FFFFFF" stroke="#DCDAD3"/>',
    ]

    # Phase shading bands
    phase_fill = {"DAY": "#FAF8F2", "EVENING": "#FBF3E6", "NIGHT": "#EFF2F6"}
    if n_pts >= 2:
        seg_start = 0
        for k in range(1, n_pts + 1):
            if k == n_pts or sampled[k].phase != sampled[seg_start].phase:
                x0 = _x_px(seg_start)
                x1 = _x_px(min(k, n_pts - 1))
                band_w = max(1.0, x1 - x0)
                fill = phase_fill.get(sampled[seg_start].phase, "#FAF8F2")
                svg_parts.append(
                    f'<rect x="{x0:.1f}" y="{pad_t}" width="{band_w:.1f}" height="{plot_h}" '
                    f'fill="{fill}" opacity="0.85"/>'
                )
                seg_start = k
    else:
        fill = phase_fill.get(sampled[0].phase, "#FAF8F2")
        svg_parts.append(
            f'<rect x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" fill="{fill}"/>'
        )

    # Horizontal gridlines & ticks (log10(1 + lx))
    grid_vals = [0.0, 1.0, 10.0, 50.0, 250.0]
    if max_val >= 480.0:
        grid_vals.append(500.0)
    if max_val >= 950.0:
        grid_vals.append(1000.0)
    for gv in grid_vals:
        if gv > max_val * 1.02:
            continue
        yp = _y_px(gv)
        svg_parts.append(
            f'<line x1="{pad_l}" y1="{yp:.1f}" x2="{w - pad_r}" y2="{yp:.1f}" '
            'stroke="#E5E3DC" stroke-width="1"/>'
        )
        lbl = f"{int(gv)}" if gv >= 1.0 or gv == 0.0 else f"{gv:.1f}"
        svg_parts.append(
            f'<text x="{pad_l - 8}" y="{yp + 4:.1f}" text-anchor="end" '
            'font-family="ui-monospace, Consolas, monospace" font-size="10.5" fill="#6E6C66">'
            f"{lbl} lx</text>"
        )

    # Target reference lines: 250 lx DAY target & 10 lx EVENING cap
    y_250 = _y_px(250.0)
    svg_parts.append(
        f'<line x1="{pad_l}" y1="{y_250:.1f}" x2="{w - pad_r}" y2="{y_250:.1f}" '
        'stroke="#2E7D5B" stroke-width="1.4" stroke-dasharray="6,4"/>'
    )
    svg_parts.append(
        f'<text x="{w - pad_r - 4}" y="{y_250 - 5:.1f}" text-anchor="end" '
        'font-family="ui-monospace, Consolas, monospace" font-size="10" fill="#2E7D5B">'
        'DAY TARGET 250 lx</text>'
    )

    y_10 = _y_px(10.0)
    svg_parts.append(
        f'<line x1="{pad_l}" y1="{y_10:.1f}" x2="{w - pad_r}" y2="{y_10:.1f}" '
        'stroke="#C98A1C" stroke-width="1.4" stroke-dasharray="5,4"/>'
    )
    svg_parts.append(
        f'<text x="{w - pad_r - 4}" y="{y_10 - 5:.1f}" text-anchor="end" '
        'font-family="ui-monospace, Consolas, monospace" font-size="10" fill="#A66E12">'
        'EVENING CAP 10 lx</text>'
    )

    # +/- k=2 uncertainty ribbon & main m-EDI trace (break across invalid/occluded segments)
    segments: list[list[tuple[float, _ParsedRow]]] = []
    current_seg: list[tuple[float, _ParsedRow]] = []
    for k, row in enumerate(sampled):
        if row.reading_valid and not row.sensor_occluded:
            current_seg.append((_x_px(k), row))
        else:
            if current_seg:
                segments.append(current_seg)
                current_seg = []
    if current_seg:
        segments.append(current_seg)

    for seg in segments:
        if len(seg) >= 2:
            upper_pts = [f"{x:.1f},{_y_px(r.m_edi_lx + r.uncertainty_k2_lx):.1f}" for x, r in seg]
            lower_pts = [
                f"{x:.1f},{_y_px(max(0.0, r.m_edi_lx - r.uncertainty_k2_lx)):.1f}"
                for x, r in reversed(seg)
            ]
            svg_parts.append(
                f'<polygon points="{" ".join(upper_pts + lower_pts)}" '
                'fill="#1F7A8C" fill-opacity="0.16" stroke="none"/>'
            )
            line_pts = " ".join(f"{x:.1f},{_y_px(r.m_edi_lx):.1f}" for x, r in seg)
            svg_parts.append(
                f'<polyline points="{line_pts}" fill="none" stroke="#1F7A8C" '
                'stroke-width="2.1" stroke-linejoin="round" stroke-linecap="round"/>'
            )
        elif len(seg) == 1:
            x, r = seg[0]
            y_hi = _y_px(r.m_edi_lx + r.uncertainty_k2_lx)
            y_lo = _y_px(max(0.0, r.m_edi_lx - r.uncertainty_k2_lx))
            svg_parts.append(
                f'<line x1="{x:.1f}" y1="{y_hi:.1f}" x2="{x:.1f}" y2="{y_lo:.1f}" '
                'stroke="#1F7A8C" stroke-width="2" opacity="0.45"/>'
            )
            svg_parts.append(
                f'<circle cx="{x:.1f}" cy="{_y_px(r.m_edi_lx):.1f}" r="3.5" fill="#1F7A8C"/>'
            )

    # Alert & occluded markers
    for k, row in enumerate(sampled):
        xk = _x_px(k)
        if row.unlinked_alert:
            svg_parts.append(
                f'<circle cx="{xk:.1f}" cy="{_y_px(row.m_edi_lx):.1f}" r="3.8" '
                'fill="#E25B28" stroke="#FFFFFF" stroke-width="1"/>'
            )
        elif row.sensor_occluded or not row.reading_valid:
            svg_parts.append(
                f'<rect x="{xk - 2.5:.1f}" y="{pad_t + plot_h - 10:.1f}" width="5" height="8" '
                'fill="#8A8880" opacity="0.75"/>'
            )

    # Axes frame & start/mid/end timestamps
    svg_parts.append(
        f'<rect x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" '
        'fill="none" stroke="#C8C6BE" stroke-width="1"/>'
    )
    tick_positions = [0, n_pts // 2, n_pts - 1] if n_pts >= 3 else list(range(n_pts))
    seen_x: set[int] = set()
    for pos in tick_positions:
        if pos in seen_x:
            continue
        seen_x.add(pos)
        xk = _x_px(pos)
        ts_short = html.escape(sampled[pos].timestamp_iso[:16].replace("T", " "))
        anchor = "start" if pos == 0 else ("end" if pos == n_pts - 1 else "middle")
        svg_parts.append(
            f'<text x="{xk:.1f}" y="{h - 16}" text-anchor="{anchor}" '
            'font-family="ui-monospace, Consolas, monospace" font-size="10" fill="#6E6C66">'
            f"{ts_short}</text>"
        )

    svg_parts.append(
        f'<text x="{pad_l}" y="{pad_t - 8}" font-family="ui-monospace, Consolas, monospace" '
        'font-size="10" fill="#6E6C66">m-EDI (lx, log1p scale) &amp; +/-k=2 (+/-17%) band</text>'
    )
    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


def _render_markdown(summary: dict[str, Any]) -> str:
    day = summary["day_compliance"]
    eve = summary["evening_compliance"]
    ngt = summary["night_compliance"]
    span = (
        f"`{summary['time_span_start_iso']}` → `{summary['time_span_end_iso']}`"
        if summary["time_span_start_iso"]
        else "_No ticks logged_"
    )
    lines = [
        "# Eye-Level Spectral m-EDI Compliance Report",
        "",
        f"- **Source CSV:** `{Path(summary['csv_path']).name}`",
        f"- **Window:** {span}",
        f"- **Total Ticks:** `{summary['total_ticks']}` "
        f"(Valid: `{summary['valid_ticks']}` | Sensor: `{summary['sensor_ticks']}` | "
        f"Open-Loop: `{summary['open_loop_ticks']}` | Skipped malformed: `{summary['skipped_rows']}`)",
        "",
        "## Phase Compliance (`k = 2`, `±17%` Expanded Uncertainty — 3-State Rule)",
        "",
        "| Phase | Target (m-EDI) | Measured Ticks | SAGLANDI (%) | BELIRSIZ (%) | SAGLANMADI (%) | Open-Loop Ticks |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
        f"| **DAY** | `>= 250 lx` | {day['measured']} | {day['saglandi']} ({day['saglandi_pct']:.1f}%) | "
        f"{day['belirsiz']} ({day['belirsiz_pct']:.1f}%) | {day['saglanmadi']} ({day['saglanmadi_pct']:.1f}%) | {day['open_loop']} |",
        f"| **EVENING** | `<= 10 lx` (`<= 5 lx` sens.) | {eve['measured']} | {eve['saglandi']} ({eve['saglandi_pct']:.1f}%) | "
        f"{eve['belirsiz']} ({eve['belirsiz_pct']:.1f}%) | {eve['saglanmadi']} ({eve['saglanmadi_pct']:.1f}%) | {eve['open_loop']} |",
        f"| **NIGHT** | `<= 1 lx` (`<0.3 lx` floor) | {ngt['measured']} | {ngt['saglandi']} ({ngt['saglandi_pct']:.1f}%) | "
        f"{ngt['belirsiz']} ({ngt['belirsiz_pct']:.1f}%) | {ngt['saglanmadi']} ({ngt['saglanmadi_pct']:.1f}%) | {ngt['open_loop']} |",
        "",
        "## Optical & Control Diagnostics",
        "",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| Unlinked-Light Alerts (`u = 0` non-negativity) | `{summary['unlinked_alert_count']}` |",
        f"| Peak Unlinked `m-EDI` | `{summary['peak_unlinked_m_edi_lx']:.2f} lx` |",
        f"| Occluded Sensor Ticks (`>10s` freeze, not reported live) | `{summary['occluded_tick_count']}` |",
        f"| Mains Flicker Detections (`100 / 120 Hz`) | `{summary['flicker_100_120_count']}` (total `>=90 Hz`: `{summary['flicker_detected_count']}`) |",
        f"| Conflict-Observer Backoff Ticks (`10 min` yield) | `{summary['conflict_observer_count']}` |",
        f"| Open-Loop Active Duration | `{summary['open_loop_active_hours']:.2f} h` |",
        "",
        "## Scope & Metrology Disclaimer",
        "",
        f"> {SCOPE_DISCLAIMER}",
        "",
    ]
    return "\n".join(lines)


def _render_html(summary: dict[str, Any], svg_chart: str) -> str:
    day = summary["day_compliance"]
    eve = summary["evening_compliance"]
    ngt = summary["night_compliance"]
    csv_name = html.escape(Path(summary["csv_path"]).name)
    span_str = (
        f"{html.escape(summary['time_span_start_iso'])} &rarr; {html.escape(summary['time_span_end_iso'])}"
        if summary["time_span_start_iso"]
        else "No ticks recorded"
    )
    disclaimer_html = html.escape(SCOPE_DISCLAIMER)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spectral m-EDI Compliance Report — {csv_name}</title>
<style>
  :root {{
    --bg: #F6F5F1;
    --card: #FFFFFF;
    --ink: #161614;
    --muted: #6E6C66;
    --line: #DCDAD3;
    --cyan: #1F7A8C;
    --green: #2E7D5B;
    --amber: #C98A1C;
    --orange: #E25B28;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 32px 20px 48px;
    background: var(--bg);
    color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    line-height: 1.5;
  }}
  .wrap {{ max-width: 960px; margin: 0 auto; }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 12px;
    padding-bottom: 16px;
    border-bottom: 2px solid var(--ink);
    margin-bottom: 24px;
  }}
  h1 {{ font-size: 20px; font-weight: 600; letter-spacing: -0.02em; margin: 0; }}
  .meta {{ font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: var(--muted); }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }}
  .kpi {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 14px 16px;
  }}
  .kpi-label {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    margin-bottom: 6px;
  }}
  .kpi-val {{
    font-family: ui-monospace, Consolas, monospace;
    font-size: 22px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }}
  .kpi-sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  .card {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 18px 20px;
    margin-bottom: 20px;
  }}
  h2 {{
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin: 0 0 12px;
  }}
  .legend {{
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    margin-top: 10px;
    font-family: ui-monospace, Consolas, monospace;
    font-size: 11px;
    color: var(--muted);
  }}
  .dot {{
    display: inline-block;
    width: 9px;
    height: 9px;
    border-radius: 50%;
    margin-right: 5px;
    vertical-align: middle;
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{
    text-align: left;
    padding: 9px 10px;
    border-bottom: 1px solid var(--line);
    font-variant-numeric: tabular-nums;
  }}
  th {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    font-weight: 600;
  }}
  .num {{ font-family: ui-monospace, Consolas, monospace; text-align: right; }}
  .pill-ok {{ color: var(--green); font-weight: 600; }}
  .pill-mid {{ color: var(--amber); font-weight: 600; }}
  .pill-bad {{ color: var(--orange); font-weight: 600; }}
  footer {{
    margin-top: 24px;
    padding: 14px 16px;
    border: 1px solid var(--line);
    border-left: 3px solid var(--ink);
    border-radius: 4px;
    background: #F0EFEA;
    font-size: 12px;
    color: #3E3D39;
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Eye-Level Spectral m-EDI Compliance Log</h1>
    <div class="meta">{csv_name} &middot; {span_str}</div>
  </header>

  <section class="grid">
    <div class="kpi">
      <div class="kpi-label">DAY Compliance (k=2)</div>
      <div class="kpi-val pill-ok">{day['saglandi_pct']:.1f}%</div>
      <div class="kpi-sub">BELIRSIZ {day['belirsiz_pct']:.1f}% &middot; SAGLANMADI {day['saglanmadi_pct']:.1f}%</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">EVENING Compliance</div>
      <div class="kpi-val pill-ok">{eve['saglandi_pct']:.1f}%</div>
      <div class="kpi-sub">BELIRSIZ {eve['belirsiz_pct']:.1f}% &middot; SAGLANMADI {eve['saglanmadi_pct']:.1f}%</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Unlinked Light Alerts</div>
      <div class="kpi-val pill-bad">{summary['unlinked_alert_count']}</div>
      <div class="kpi-sub">Peak unlinked: {summary['peak_unlinked_m_edi_lx']:.1f} lx m-EDI</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Ticks &amp; Open-Loop</div>
      <div class="kpi-val">{summary['total_ticks']}</div>
      <div class="kpi-sub">Valid {summary['valid_ticks']} &middot; Open-loop {summary['open_loop_active_hours']:.2f} h</div>
    </div>
  </section>

  <section class="card">
    <h2>Melanopic EDI Time Series (100% Offline SVG)</h2>
    {svg_chart}
    <div class="legend">
      <span><i class="dot" style="background:#1F7A8C"></i>Measured m-EDI &amp; &plusmn;k=2 band</span>
      <span><i class="dot" style="background:#2E7D5B"></i>DAY Target (250 lx)</span>
      <span><i class="dot" style="background:#C98A1C"></i>EVENING Cap (10 lx)</span>
      <span><i class="dot" style="background:#E25B28"></i>Unlinked-Light Alert</span>
      <span><i class="dot" style="background:#8A8880"></i>Occluded / Invalid (excluded from live line)</span>
    </div>
  </section>

  <section class="card">
    <h2>Phase-by-Phase 3-State Metrology Breakdown</h2>
    <table>
      <thead>
        <tr>
          <th>Phase</th>
          <th>Target (m-EDI)</th>
          <th class="num">Measured</th>
          <th class="num">SAGLANDI</th>
          <th class="num">BELIRSIZ</th>
          <th class="num">SAGLANMADI</th>
          <th class="num">Open-Loop</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><strong>DAY</strong></td>
          <td>&ge; 250 lx</td>
          <td class="num">{day['measured']}</td>
          <td class="num pill-ok">{day['saglandi']} ({day['saglandi_pct']:.1f}%)</td>
          <td class="num pill-mid">{day['belirsiz']} ({day['belirsiz_pct']:.1f}%)</td>
          <td class="num pill-bad">{day['saglanmadi']} ({day['saglanmadi_pct']:.1f}%)</td>
          <td class="num">{day['open_loop']}</td>
        </tr>
        <tr>
          <td><strong>EVENING</strong></td>
          <td>&le; 10 lx (&le; 5 lx sens.)</td>
          <td class="num">{eve['measured']}</td>
          <td class="num pill-ok">{eve['saglandi']} ({eve['saglandi_pct']:.1f}%)</td>
          <td class="num pill-mid">{eve['belirsiz']} ({eve['belirsiz_pct']:.1f}%)</td>
          <td class="num pill-bad">{eve['saglanmadi']} ({eve['saglanmadi_pct']:.1f}%)</td>
          <td class="num">{eve['open_loop']}</td>
        </tr>
        <tr>
          <td><strong>NIGHT</strong></td>
          <td>&le; 1 lx (&lt;0.3 lx floor)</td>
          <td class="num">{ngt['measured']}</td>
          <td class="num pill-ok">{ngt['saglandi']} ({ngt['saglandi_pct']:.1f}%)</td>
          <td class="num pill-mid">{ngt['belirsiz']} ({ngt['belirsiz_pct']:.1f}%)</td>
          <td class="num pill-bad">{ngt['saglanmadi']} ({ngt['saglanmadi_pct']:.1f}%)</td>
          <td class="num">{ngt['open_loop']}</td>
        </tr>
      </tbody>
    </table>
  </section>

  <section class="card">
    <h2>Hardware &amp; Control Diagnostics</h2>
    <table>
      <tbody>
        <tr><td>Occluded sensor ticks (&gt;10s freeze)</td><td class="num">{summary['occluded_tick_count']}</td></tr>
        <tr><td>Mains flicker detections (100 / 120 Hz)</td><td class="num">{summary['flicker_100_120_count']} (all &ge;90 Hz: {summary['flicker_detected_count']})</td></tr>
        <tr><td>Conflict-Observer backoff ticks (10-min yield)</td><td class="num">{summary['conflict_observer_count']}</td></tr>
        <tr><td>Malformed CSV rows safely skipped</td><td class="num">{summary['skipped_rows']}</td></tr>
      </tbody>
    </table>
  </section>

  <footer>
    <strong>Scope &amp; Metrology Notice:</strong> {disclaimer_html}
  </footer>
</div>
</body>
</html>
"""


def generate_compliance_report(
    csv_path: str | Path,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Parse a telemetry CSV and emit offline `report_summary.md` & `report_summary.html`."""
    csv_p = Path(csv_path)
    out_dir = Path(output_dir) if output_dir is not None else csv_p.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    rows, skipped_rows = _load_rows(csv_p)
    total_ticks = len(rows)
    valid_ticks = sum(1 for r in rows if r.reading_valid)
    open_loop_ticks = sum(1 for r in rows if _is_open_loop(r))
    sensor_ticks = total_ticks - open_loop_ticks

    unlinked_rows = [r for r in rows if r.unlinked_alert]
    unlinked_alert_count = len(unlinked_rows)
    peak_unlinked_m_edi = max((r.m_edi_lx for r in unlinked_rows), default=0.0)
    occluded_tick_count = sum(1 for r in rows if r.sensor_occluded)
    conflict_observer_count = sum(1 for r in rows if r.conflict_observer)
    flicker_detected_count = sum(1 for r in rows if r.flicker_hz >= 90.0)
    flicker_100_120_count = sum(1 for r in rows if 95.0 <= r.flicker_hz <= 125.0)
    open_loop_active_hours = _compute_open_loop_hours(rows)

    day_stats = _phase_compliance_stats(rows, "DAY")
    evening_stats = _phase_compliance_stats(rows, "EVENING")
    night_stats = _phase_compliance_stats(rows, "NIGHT")

    md_path = out_dir / "report_summary.md"
    html_path = out_dir / "report_summary.html"

    summary: dict[str, Any] = {
        "csv_path": str(csv_p),
        "markdown_path": str(md_path),
        "html_path": str(html_path),
        "total_ticks": total_ticks,
        "valid_ticks": valid_ticks,
        "invalid_ticks": total_ticks - valid_ticks,
        "skipped_rows": skipped_rows,
        "sensor_ticks": sensor_ticks,
        "open_loop_ticks": open_loop_ticks,
        "open_loop_active_hours": round(open_loop_active_hours, 3),
        "day_compliance": day_stats,
        "evening_compliance": evening_stats,
        "night_compliance": night_stats,
        "unlinked_alert_count": unlinked_alert_count,
        "peak_unlinked_m_edi_lx": round(peak_unlinked_m_edi, 2),
        "occluded_tick_count": occluded_tick_count,
        "conflict_observer_count": conflict_observer_count,
        "flicker_detected_count": flicker_detected_count,
        "flicker_100_120_count": flicker_100_120_count,
        "time_span_start_iso": rows[0].timestamp_iso if rows else "",
        "time_span_end_iso": rows[-1].timestamp_iso if rows else "",
    }

    md_path.write_text(_render_markdown(summary), encoding="utf-8")
    svg_chart = _build_svg_chart(rows)
    html_path.write_text(_render_html(summary, svg_chart), encoding="utf-8")

    return summary
