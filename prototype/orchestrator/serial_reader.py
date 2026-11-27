"""USB-Serial NDJSON sensor reader (lazy pyserial) + simulated sensor wrapper.

v6.1 Kova-1 Hardening:
- Parses optional `"chip"` field (`TCS3448` / `AS7343` / `AS7341`) into `SpectralSample`.
- BUG-5b: Handles USB-C disconnect / `SerialException` / `OSError` / empty reads with
  bounded exponential-backoff reconnect (0.5s -> 1s -> 2s -> 4s -> max 10s), raising
  a clean `RuntimeError` if attempts are exhausted and NEVER fabricating stale readings.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from spectral.photobiology import SpectralSample, SyntheticSensorStream

logger = logging.getLogger(__name__)

EXPECTED_CHANNELS: tuple[str, ...] = (
    "F1",
    "F2",
    "FZ",
    "F3",
    "F4",
    "FY",
    "F5",
    "FXL",
    "F6",
    "F7",
    "F8",
    "NIR",
    "CLEAR",
    "FLICKER",
)


def parse_ndjson_line(line: str) -> SpectralSample | None:
    """Parse a single NDJSON line streamed from Raspberry Pi Pico 2 into a SpectralSample.

    Accepts both nested (`{"timestamp": ..., "chip": "TCS3448", "channels": {"F1": ...}}`)
    and flat (`{"F1": ..., "F2": ...}`) firmware payloads for resilience.
    Returns None for blank lines, boot messages, or status/error frames.
    """
    text = line.strip()
    if not text or not text.startswith("{"):
        return None

    try:
        obj: Any = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("Skipping malformed serial JSON line: %s", text[:120])
        return None

    if not isinstance(obj, dict):
        return None

    if "error" in obj:
        logger.warning("Sensor firmware reported error: %s", obj["error"])
        return None

    raw_channels = obj.get("channels")
    if isinstance(raw_channels, dict):
        source_map = raw_channels
    else:
        # Support flat firmware payloads where channel keys are top-level.
        source_map = obj

    if not any(ch in source_map for ch in ("FZ", "F3", "F4", "FY", "CLEAR")):
        logger.debug("Ignoring non-spectral JSON frame: %s", list(obj.keys()))
        return None

    channels: dict[str, float] = {}
    for key in EXPECTED_CHANNELS:
        val = source_map.get(key, 0.0)
        try:
            channels[key] = max(0.0, float(val))
        except (TypeError, ValueError):
            channels[key] = 0.0

    raw_ts = obj.get("timestamp", obj.get("ts"))
    now = time.time()
    try:
        ts_val = float(raw_ts) if raw_ts is not None else now
        # Pico uptime counters (seconds/ms since boot) are small numbers; map to wall clock.
        timestamp = ts_val if ts_val > 1_000_000_000 else now
    except (TypeError, ValueError):
        timestamp = now

    saturated = bool(obj.get("saturated", False))
    chip = str(obj.get("chip", "AS7343")).strip() or "AS7343"

    if "chip" in getattr(SpectralSample, "__dataclass_fields__", {}):
        return SpectralSample(timestamp=timestamp, channels=channels, saturated=saturated, chip=chip)
    return SpectralSample(timestamp=timestamp, channels=channels, saturated=saturated)


class SensorSource(ABC):
    """Unified sensor stream interface for both physical USB-Serial and simulation."""

    @abstractmethod
    def next_sample(self) -> SpectralSample:
        """Return the next SpectralSample."""

    def close(self) -> None:
        """Close underlying device handle if any."""
        return None

    def __enter__(self) -> SensorSource:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class SimulatedSensorReader(SensorSource):
    """Wraps `SyntheticSensorStream` behind the `SensorSource` interface."""

    def __init__(self, scenario: str = "overcast_day", total_ticks: int = 8) -> None:
        self.scenario = scenario
        self.total_ticks = total_ticks
        self._stream = SyntheticSensorStream(scenario, total_ticks=total_ticks)

    def next_sample(self) -> SpectralSample:
        return self._stream.next_sample()


class SerialSensorReader(SensorSource):
    """Reads live multi-spectral NDJSON lines from a Raspberry Pi Pico 2 over USB-Serial.

    `pyserial` is imported lazily here so `--simulate` runs with zero third-party deps.
    On USB disconnect or empty read timeout, performs bounded exponential-backoff reconnect
    (`0.5s -> 1s -> 2s -> 4s -> max 10s`) and NEVER fabricates stale readings as live.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 5.0,
        max_skip_lines: int = 20,
        max_reconnect_attempts: int = 5,
        backoff_base_s: float = 0.5,
        max_backoff_s: float = 10.0,
        serial_factory: Callable[..., Any] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._serial_exc_types: tuple[type[BaseException], ...] = (OSError,)
        if serial_factory is None:
            try:
                import serial  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "USB-Serial sensor streaming requires `pyserial`. "
                    "Install it with: pip install pyserial"
                ) from exc
            self._serial_factory: Callable[..., Any] = serial.Serial
            self._serial_exc_types = (OSError, serial.SerialException)
        else:
            self._serial_factory = serial_factory

        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.max_skip_lines = max(1, int(max_skip_lines))
        self.max_reconnect_attempts = max(0, int(max_reconnect_attempts))
        self.backoff_base_s = max(0.0, float(backoff_base_s))
        self.max_backoff_s = max(self.backoff_base_s, float(max_backoff_s))
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        self.reconnect_count: int = 0
        self._serial: Any = None
        self._open_serial()

    def _open_serial(self) -> None:
        self._serial = self._serial_factory(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
        )

    def _reconnect_with_backoff(self, cause: str) -> None:
        """Attempt bounded exponential-backoff reconnect; raise RuntimeError on exhaustion."""
        self.close()
        last_exc: BaseException | None = None
        for attempt in range(self.max_reconnect_attempts):
            delay = min(self.max_backoff_s, self.backoff_base_s * (2**attempt))
            logger.warning(
                "USB-Serial disconnect on %s (%s). Reconnect attempt %d/%d in %.2fs...",
                self.port,
                cause,
                attempt + 1,
                self.max_reconnect_attempts,
                delay,
            )
            if delay > 0:
                self._sleep(delay)
            try:
                self._open_serial()
                self.reconnect_count += 1
                logger.info("Reconnected USB-Serial sensor on %s.", self.port)
                return
            except self._serial_exc_types as exc:
                last_exc = exc

        raise RuntimeError(
            f"USB-Serial sensor on {self.port} remained unavailable after "
            f"{self.max_reconnect_attempts} reconnect attempts ({cause})."
        ) from last_exc

    def next_sample(self) -> SpectralSample:
        reconnect_cycles = 0
        max_cycles = max(1, self.max_reconnect_attempts + 1)

        while reconnect_cycles < max_cycles:
            skipped = 0
            while skipped < self.max_skip_lines:
                try:
                    raw = self._serial.readline() if self._serial is not None else b""
                except self._serial_exc_types as exc:
                    reconnect_cycles += 1
                    if reconnect_cycles >= max_cycles or self.max_reconnect_attempts == 0:
                        raise RuntimeError(
                            f"USB-Serial read error on {self.port}: {exc}"
                        ) from exc
                    self._reconnect_with_backoff(str(exc))
                    break

                if not raw:
                    reconnect_cycles += 1
                    if reconnect_cycles >= max_cycles or self.max_reconnect_attempts == 0:
                        raise RuntimeError(
                            f"No spectral JSON data received from {self.port} after "
                            f"{self.max_reconnect_attempts} reconnect attempts."
                        )
                    self._reconnect_with_backoff(f"timeout/empty read ({self.timeout}s)")
                    break

                line = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
                sample = parse_ndjson_line(line)
                if sample is not None:
                    return sample
                skipped += 1
            else:
                raise RuntimeError(
                    f"Received {self.max_skip_lines} non-spectral lines on {self.port}; "
                    "verify pico_as7343.py firmware is running."
                )

        raise RuntimeError(
            f"USB-Serial sensor on {self.port} failed to yield a live sample."
        )

    def close(self) -> None:
        try:
            if self._serial is not None and getattr(self._serial, "is_open", True):
                self._serial.close()
        except Exception:
            pass
        finally:
            self._serial = None
