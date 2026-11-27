"""MicroPython firmware for Raspberry Pi Pico 2 (RP2350A) / Pico + OLGA-8 Spectral Sensor.

Supports automatic I2C (0x39) detection across all 3 pin-compatible OLGA-8 sensors:
  - ams-OSRAM TCS3448 (14-ch, 3-cycle auto_smux, DS001121 — EVT/MP target)
  - ams-OSRAM AS7343  (14-ch, 3-cycle auto_smux, DS001046 — Aşama 0 PoC breakout)
  - ams-OSRAM AS7341  (11-ch, 2-pass manual SMUX — Aşama 0 PoC breakout)

Key v6.1 Metrology & Hardening Features:
  - Acta IMEKO constant mid-gain (16x default; 64x only in deep low-light) to eliminate
    multi-decade gain-switching variance jumps.
  - 100.0 ms mains-null integration (exact integer multiple of BOTH 50 Hz [5 cycles] and
    60 Hz [6 cycles]), auto-extending to 300 ms / 1000 ms below ~1 lx for evening SNR.
  - Startup dark-pedestal tracking (capped safely) for low-light offset hygiene.
  - BUG-5c Fix: Hardware flicker engine (FDEN) decoding FD_STATUS (0xDB) to emit real
    100.0 Hz, 120.0 Hz, or 0.0 Hz in the "FLICKER" field (never raw ADC counts!).

Wiring (Raspberry Pi Pico 2 -> Adafruit / SparkFun OLGA-8 Breakout with onboard 1.8V LDO):
  - Pin 36 (3V3 OUT) -> VIN / 3V3
  - Pin 38 (GND)     -> GND
  - Pin 6  (GP4)     -> SDA
  - Pin 7  (GP5)     -> SCL
  NOTE: Bare OLGA-8 ICs require external 1.8V LDO (TLV75518PDBVR) + 1.8V<->3.3V I2C
  level shifter (PCA9306DCTR) on the EVT PCB.
"""

import json
import sys
import time
from machine import I2C, Pin

# --- Shared ams-OSRAM OLGA-8 Constants (I2C 0x39) ---
AS7343_ADDR = 0x39

REG_ENABLE = 0x80      # Bit0=PON, Bit1=SP_EN, Bit4=SMUXEN(AS7341), Bit6=FDEN
REG_ATIME = 0x81       # Integration cycle steps (0..255)
REG_STATUS2 = 0x90     # Bit6=AVALID, Bit4=ASAT_DIGITAL, Bit3=ASAT_ANALOG
REG_AS7341_ID = 0x92   # AS7341 Chip ID in Bank 0 ((val >> 2) == 0x09 => 0x24)
REG_ASTATUS = 0x94     # Latches DATA registers on read; Bit7=ASAT_STATUS
REG_DATA_0_L = 0x95    # 18x uint16 (AS7343/TCS3448) or 6x uint16 (AS7341 per SMUX pass)
REG_CFG0 = 0xBF        # Bit4=REG_BANK (0: 0x80..0xFF, 1: 0x58..0x7F)
REG_CFG1 = 0xC6        # AGAIN index (AS7343/TCS3448); on AS7341 AGAIN is at 0xAA
REG_AS7341_CFG1 = 0xAA # AS7341 AGAIN register
REG_AS7341_CFG6 = 0xAF # AS7341 SMUX_CMD bits [4:3] (0x10 = write RAM)
REG_ASTEP_L = 0xD4     # AS7343/TCS3448 16-bit ASTEP low byte (AS7341 uses 0xCA/0xCB)
REG_ASTEP_H = 0xD5
REG_AS7341_ASTEP_L = 0xCA
REG_AS7341_ASTEP_H = 0xCB
REG_CFG20 = 0xD6       # AS7343/TCS3448 auto_smux bits [6:5] (0x60 = 18-ch 3-cycle)
REG_FD_STATUS = 0xDB   # Bit0=100Hz, Bit1=120Hz, Bit2=100Hz_valid, Bit3=120Hz_valid, Bit5=meas_valid

# Bank 1 ID registers (AS7343 / TCS3448)
REG_AUXID_B1 = 0x58
REG_REVID_B1 = 0x59
REG_ID_B1 = 0x5A       # Expected 0x81 for AS7343 / TCS3448 family

AGAIN_TABLE = (
    0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0, 2048.0
)

# Acta IMEKO Constant Mid-Gain + 50/60 Hz Mains-Null Integration Tiers
# t_int = (ATIME + 1) * (ASTEP + 1) * 2.78 us (ASTEP=999 => 2.78 ms per ATIME+1)
DEFAULT_INTERVAL_S = 2.0
CONST_MID_GAIN_IDX = 5   # 16x constant mid-gain (minimizes gain-switch variance jumps)
LOW_LIGHT_GAIN_IDX = 7   # 64x engaged only when 1000 ms integration is still dim
HIGH_LIGHT_GAIN_IDX = 2  # 2x fallback only if saturated at 100 ms

# Integration presets: (ATIME, ASTEP) -> exact multiples of 100 ms (5x50Hz & 6x60Hz cycles)
INT_100MS = (35, 999)    # 100.08 ms (default daytime/evening mains-null)
INT_300MS = (107, 999)   # 300.24 ms (dim evening <15 lx)
INT_1000MS = (255, 1405) # 1000.62 ms (<1 lx night low-light tier)

SATURATION_HI = 58000
LOW_SIGNAL_LO = 600      # extend integration time before touching analog gain


class AS7343Driver:
    """Unified MicroPython I2C driver auto-detecting TCS3448 / AS7343 / AS7341 at 0x39."""

    def __init__(self, i2c, addr=AS7343_ADDR):
        self.i2c = i2c
        self.addr = addr
        self.again_idx = CONST_MID_GAIN_IDX
        self.atime, self.astep = INT_100MS
        self.int_ms = self._calc_int_ms(self.atime, self.astep)
        self.chip_name = "AS7343"
        self.chip_id = 0x00
        self.dark_offset = 0.0

        devices = self.i2c.scan()
        if self.addr not in devices:
            raise OSError(
                "Spectral sensor not found at I2C 0x39 (found: {})".format(
                    [hex(d) for d in devices]
                )
            )

        self._detect_chip()
        self._init_sensor()

    @staticmethod
    def _calc_int_ms(atime, astep):
        return (atime + 1) * (astep + 1) * 0.00278

    def _write_u8(self, reg, val):
        self.i2c.writeto_mem(self.addr, reg, bytes((val & 0xFF,)))

    def _read_u8(self, reg):
        return self.i2c.readfrom_mem(self.addr, reg, 1)[0]

    def _set_reg_bank(self, bank):
        cfg0 = self._read_u8(REG_CFG0)
        if bank == 1:
            cfg0 |= 0x10
        else:
            cfg0 &= ~0x10
        self._write_u8(REG_CFG0, cfg0)

    def _detect_chip(self):
        """Auto-detect AS7341 vs AS7343 vs TCS3448 on shared OLGA-8 I2C address 0x39."""
        # Ensure PON=1 first so register banks respond cleanly
        self._write_u8(REG_ENABLE, 0x01)
        time.sleep_ms(10)

        # Check Bank 1 ID (0x5A) used by 14-ch AS7343 / TCS3448
        self._set_reg_bank(1)
        b1_id = self._read_u8(REG_ID_B1)
        b1_aux = self._read_u8(REG_AUXID_B1)
        b1_rev = self._read_u8(REG_REVID_B1)
        self._set_reg_bank(0)

        if b1_id == 0x81:
            self.chip_id = b1_id
            # TCS3448 and AS7343 share identical 14-ch auto_smux register map (ID 0x81);
            # distinguish newer TCS3448 silicon revision when present.
            self.chip_name = "TCS3448" if (b1_rev >= 0x02 or (b1_aux & 0x0F) >= 0x02) else "AS7343"
            return

        # Fallback: check AS7341 Bank-0 ID register (0x92, upper 6 bits == 0x09)
        id_41 = self._read_u8(REG_AS7341_ID)
        if (id_41 >> 2) == 0x09:
            self.chip_id = id_41
            self.chip_name = "AS7341"
            return

        self.chip_id = b1_id
        self.chip_name = "AS7343"

    def read_chip_id(self):
        return self.chip_id

    def _apply_timing(self, atime, astep):
        self.atime = atime
        self.astep = astep
        self.int_ms = self._calc_int_ms(atime, astep)
        self._write_u8(REG_ATIME, atime & 0xFF)
        if self.chip_name == "AS7341":
            self._write_u8(REG_AS7341_ASTEP_L, astep & 0xFF)
            self._write_u8(REG_AS7341_ASTEP_H, (astep >> 8) & 0xFF)
        else:
            self._write_u8(REG_ASTEP_L, astep & 0xFF)
            self._write_u8(REG_ASTEP_H, (astep >> 8) & 0xFF)

    def set_gain_index(self, idx):
        idx = max(0, min(len(AGAIN_TABLE) - 1, idx))
        self.again_idx = idx
        if self.chip_name == "AS7341":
            # AS7341 supports indices 0..10 (0.5x..512x)
            idx_41 = min(10, idx)
            self.again_idx = idx_41
            self._write_u8(REG_AS7341_CFG1, idx_41 & 0x0F)
        else:
            self._write_u8(REG_CFG1, idx & 0x1F)

    def _init_sensor(self):
        self._set_reg_bank(0)
        self._write_u8(REG_ENABLE, 0x01)
        time.sleep_ms(10)

        self._apply_timing(INT_100MS[0], INT_100MS[1])
        self.set_gain_index(CONST_MID_GAIN_IDX)

        if self.chip_name != "AS7341":
            # Enable 18-channel 3-cycle auto_smux (CFG20 bits [6:5] = 0b11 -> 0x60)
            cfg20 = self._read_u8(REG_CFG20)
            self._write_u8(REG_CFG20, (cfg20 & 0x9F) | 0x60)

        self._capture_startup_dark_pedestal()

    def _capture_startup_dark_pedestal(self):
        """Estimate minimal dark pedestal if powered on covered; capped safely so bright starts stay unaffected."""
        try:
            if self.chip_name == "AS7341":
                vals, sat = self._read_as7341_raw()
            else:
                vals, sat = self._read_auto_smux_raw()
            if (not sat) and max(vals) <= 25:
                self.dark_offset = float(min(vals))
            else:
                self.dark_offset = 0.0
        except Exception:
            self.dark_offset = 0.0

    def _detect_flicker_hz(self, wait_ms=160):
        """BUG-5c Fix: Run hardware FDEN flicker engine and decode FD_STATUS (0xDB) to Hz."""
        try:
            # Clear prior status bits (W1C bits 5..2) and enable PON | FDEN
            self._write_u8(REG_FD_STATUS, 0x3C)
            self._write_u8(REG_ENABLE, 0x41)  # PON | FDEN
            t0 = time.ticks_ms()
            fd_status = 0
            while time.ticks_diff(time.ticks_ms(), t0) < wait_ms:
                fd_status = self._read_u8(REG_FD_STATUS)
                if fd_status & 0x20:  # FD_MEASUREMENT_VALID
                    break
                time.sleep_ms(10)
            self._write_u8(REG_ENABLE, 0x01)  # Stop FDEN, keep PON
            self._write_u8(REG_FD_STATUS, 0x3C)

            if fd_status & 0x10:  # FD_SATURATION_DETECTED
                return 0.0
            if (fd_status & 0x04) and (fd_status & 0x01):
                return 100.0
            if (fd_status & 0x08) and (fd_status & 0x02):
                return 120.0
            return 0.0
        except Exception:
            self._write_u8(REG_ENABLE, 0x01)
            return 0.0

    def _read_auto_smux_raw(self, timeout_ms=4000):
        """Read 18-channel auto_smux capture for TCS3448 / AS7343."""
        self._write_u8(REG_ENABLE, 0x01)
        self._write_u8(REG_ENABLE, 0x03)  # PON | SP_EN

        t0 = time.ticks_ms()
        while True:
            status2 = self._read_u8(REG_STATUS2)
            if status2 & 0x40:  # AVALID
                break
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                self._write_u8(REG_ENABLE, 0x01)
                raise OSError("OLGA-8 AVALID timeout")
            time.sleep_ms(5)

        self._write_u8(REG_ENABLE, 0x01)
        astatus = self._read_u8(REG_ASTATUS)
        raw_bytes = self.i2c.readfrom_mem(self.addr, REG_DATA_0_L, 36)
        vals = [raw_bytes[2 * i] | (raw_bytes[2 * i + 1] << 8) for i in range(18)]
        saturated = bool((astatus & 0x80) or (status2 & 0x18))
        return vals, saturated

    def _as7341_load_smux(self, high_channels=False):
        """Configure AS7341 20-byte SMUX RAM for F1..F4 (low) or F5..F8 (high) + Clear/NIR."""
        self._write_u8(REG_ENABLE, 0x01)
        self._write_u8(REG_AS7341_CFG6, 0x10)  # SMUX_CMD = write from RAM
        if not high_channels:
            # Standard ams-OSRAM F1, F2, F3, F4, Clear, NIR SMUX table
            smux = bytes((
                0x30, 0x01, 0x00, 0x00, 0x00, 0x42, 0x00, 0x00, 0x50, 0x00,
                0x00, 0x00, 0x20, 0x04, 0x00, 0x30, 0x01, 0x50, 0x00, 0x06,
            ))
        else:
            # Standard ams-OSRAM F5, F6, F7, F8, Clear, NIR SMUX table
            smux = bytes((
                0x00, 0x00, 0x00, 0x40, 0x02, 0x00, 0x10, 0x03, 0x50, 0x10,
                0x03, 0x00, 0x00, 0x00, 0x24, 0x00, 0x00, 0x50, 0x00, 0x06,
            ))
        self.i2c.writeto_mem(self.addr, 0x00, smux)
        self._write_u8(REG_ENABLE, 0x11)  # PON | SMUXEN
        t0 = time.ticks_ms()
        while (self._read_u8(REG_ENABLE) & 0x10) and time.ticks_diff(time.ticks_ms(), t0) < 200:
            time.sleep_ms(2)

    def _read_as7341_pass(self, high_channels=False, timeout_ms=2500):
        self._as7341_load_smux(high_channels=high_channels)
        self._write_u8(REG_ENABLE, 0x03)  # PON | SP_EN
        t0 = time.ticks_ms()
        while True:
            status2 = self._read_u8(REG_STATUS2)
            if status2 & 0x40:
                break
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                self._write_u8(REG_ENABLE, 0x01)
                raise OSError("AS7341 AVALID timeout")
            time.sleep_ms(5)
        self._write_u8(REG_ENABLE, 0x01)
        astatus = self._read_u8(REG_ASTATUS)
        raw = self.i2c.readfrom_mem(self.addr, REG_DATA_0_L, 12)
        vals = [raw[2 * i] | (raw[2 * i + 1] << 8) for i in range(6)]
        sat = bool((astatus & 0x80) or (status2 & 0x18))
        return vals, sat

    def _read_as7341_raw(self):
        lo, sat_lo = self._read_as7341_pass(high_channels=False)
        hi, sat_hi = self._read_as7341_pass(high_channels=True)
        # [F1, F2, F3, F4, CLR1, NIR1, F5, F6, F7, F8, CLR2, NIR2]
        return lo + hi, bool(sat_lo or sat_hi)

    def read_normalized(self):
        """Acta IMEKO constant mid-gain + adaptive mains-null integration (100/300/1000 ms)."""
        # Always start from constant mid-gain (16x) and 100 ms mains-null integration
        self.set_gain_index(CONST_MID_GAIN_IDX)
        self._apply_timing(INT_100MS[0], INT_100MS[1])

        read_fn = self._read_as7341_raw if self.chip_name == "AS7341" else self._read_auto_smux_raw
        vals, saturated = read_fn()
        peak = max(vals)

        if saturated or peak >= SATURATION_HI:
            # Bright direct sunlight: drop to 2x gain at 100 ms
            self.set_gain_index(HIGH_LIGHT_GAIN_IDX)
            vals, saturated = read_fn()
        elif peak < LOW_SIGNAL_LO:
            # Prefer extending mains-null integration (300 ms -> 1000 ms) BEFORE raising gain
            self._apply_timing(INT_300MS[0], INT_300MS[1])
            vals, saturated = read_fn()
            if (not saturated) and max(vals) < LOW_SIGNAL_LO:
                self._apply_timing(INT_1000MS[0], INT_1000MS[1])
                vals, saturated = read_fn()
                if (not saturated) and max(vals) < LOW_SIGNAL_LO:
                    self.set_gain_index(LOW_LIGHT_GAIN_IDX)  # 64x only in deep night (<1 lx)
                    vals, saturated = read_fn()

        flicker_hz = self._detect_flicker_hz()
        gain = AGAIN_TABLE[self.again_idx]
        norm_div = gain * self.int_ms

        def _n(v):
            adj = max(0.0, float(v) - self.dark_offset)
            return round(adj / norm_div, 5)

        if self.chip_name == "AS7341":
            clr_avg = (vals[4] + vals[10]) / 2.0
            nir_avg = (vals[5] + vals[11]) / 2.0
            channels = {
                "F1": _n(vals[0]),
                "F2": _n(vals[1]),
                "FZ": 0.0,
                "F3": _n(vals[2]),
                "F4": _n(vals[3]),
                "FY": _n(vals[6]),
                "F5": _n(vals[6]),
                "FXL": 0.0,
                "F6": _n(vals[7]),
                "F7": _n(vals[8]),
                "F8": _n(vals[9]),
                "NIR": _n(nir_avg),
                "CLEAR": _n(clr_avg),
                "FLICKER": round(flicker_hz, 1),
            }
        else:
            # Exact datasheet 18-ch auto_smux order (TCS3448 / AS7343):
            # Cycle 1 [0..5]:   FZ, FY, FXL, NIR, VIS1, FD1
            # Cycle 2 [6..11]:  F2, F3, F4,  F6,  VIS2, FD2
            # Cycle 3 [12..17]: F1, F7, F8,  F5,  VIS3, FD3
            vis_avg = (vals[4] + vals[10] + vals[16]) / 3.0
            channels = {
                "F1": _n(vals[12]),
                "F2": _n(vals[6]),
                "FZ": _n(vals[0]),
                "F3": _n(vals[7]),
                "F4": _n(vals[8]),
                "FY": _n(vals[1]),
                "F5": _n(vals[15]),
                "FXL": _n(vals[2]),
                "F6": _n(vals[9]),
                "F7": _n(vals[13]),
                "F8": _n(vals[14]),
                "NIR": _n(vals[3]),
                "CLEAR": _n(vis_avg),
                "FLICKER": round(flicker_hz, 1),
            }
        return channels, saturated, gain


def emit_json(payload):
    """Write one single-line NDJSON frame to USB-Serial stdout."""
    sys.stdout.write(json.dumps(payload) + "\n")


def main(interval_s=DEFAULT_INTERVAL_S):
    i2c = I2C(0, scl=Pin(5), sda=Pin(4), freq=400_000)

    sensor = None
    while sensor is None:
        try:
            sensor = AS7343Driver(i2c)
            emit_json({
                "event": "ready",
                "chip": sensor.chip_name,
                "sensor": sensor.chip_name,
                "chip_id": hex(sensor.read_chip_id()),
                "interval_s": interval_s,
                "int_ms": round(sensor.int_ms, 2),
            })
        except Exception as exc:
            emit_json({
                "event": "error",
                "error": str(exc),
                "hint": "Check wiring: Pico 2 GP4=SDA, GP5=SCL, Pin36=3V3, Pin38=GND",
            })
            time.sleep(3)

    while True:
        try:
            channels, saturated, gain = sensor.read_normalized()
            emit_json({
                "timestamp": time.time(),
                "chip": sensor.chip_name,
                "channels": channels,
                "saturated": saturated,
                "gain": gain,
                "int_ms": round(sensor.int_ms, 2),
            })
        except Exception as exc:
            emit_json({"event": "error", "error": str(exc)})
        time.sleep(interval_s)


if __name__ == "__main__":
    main()
