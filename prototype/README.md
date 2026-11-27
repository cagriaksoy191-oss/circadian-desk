# Circadian Desk v1.0.0 — Local-First Windows Display & Smart-Light Orchestrator

**Circadian Desk** is a zero-cloud, zero-admin, pure-Python-stdlib Windows desktop utility (MIT License) that synchronizes your laptop/monitor brightness and color temperature — plus optional local LAN smart bulbs (Philips Hue, WiZ, Home Assistant MQTT) and an optional USB-C eye-level spectral sensor (`V1A`) — along a smooth, MIRED-continuous Brown et al. (2022) circadian curve.

---

## Permissions, System APIs & Network Declaration

| Subsystem / API | Exact Scope & Purpose | Privileges / Network |
|---|---|---|
| `WmiMonitorBrightness` / `WmiSetBrightness` | Hardware backlight dimming on Windows internal laptop panels (backed up before write, restored on exit/sleep) | Standard user (`asInvoker`, no admin) · Local |
| `dxva2.dll` (`VCP 0x10`) | Hardware backlight dimming on external DDC/CI monitors (`>=450 ms` debounce + `2%` deadband EEPROM guard) | Standard user · Local I²C |
| `Magnification.dll` (`MagSetFullscreenColorEffect`) | Zero-admin 5×5 diagonal GPU color matrix (`0.0` black offset — true OLED `#000000` off; `MagUninitialize()` on Pause, HDR, and neutral `6500K`) | Standard user · Local GPU |
| `DisplayConfigGetDeviceInfo` (`type=15`) & `EDID` | Read-only Windows HDR/ACM auto-pause, panel peak luminance (`CTA-861.3`), physical screen dimensions ($\Omega_{\text{proj}}$), and CIE 1931 gamut | Read-only · Local |
| `GetForegroundWindow` + `QueryFullProcessImageNameW` | Reads **only** foreground `.exe` filename in RAM to auto-pause for creative apps (`Photoshop`, `Figma`, `Resolve`) and fullscreen games | **Zero pixels, zero screenshots, zero window titles** |
| `HKCU` Registry (`AppsUseLightTheme`, `Night Light`, `ColorFiltering`) | Read-only Dark Mode & display-conflict checks; `HKCU\...\Run` written **only** if user enables Startup in Portable ZIP (never written under MSIX) | Standard user · Local |
| `http.server` (`127.0.0.1:8765`) | Offline Braun Control Panel & 7-Day SVG Report with strict loopback Host/Origin CSRF (`403`) checks and 0 external URLs | `127.0.0.1` loopback ONLY |
| Optional Local Lights (`--light wiz` / `hue`, `--mqtt-host`) | Disabled by default (`--light none`). Connects only to user-specified local LAN IPs (WiZ UDP `38899`, Hue CLIP v2, HA MQTT `1883`) | **0 outbound internet calls · 0 telemetry** |

Full legal & security policies: [`PRIVACY.md`](../PRIVACY.md) · [`CODE_SIGNING_POLICY.md`](../CODE_SIGNING_POLICY.md)

---

## Installation (3-Tier `$0` Distribution)

### 1. Recommended (Zero SmartScreen): Microsoft Store MSIX (`$0`)
Install from the **Microsoft Store** (automatically signed by Microsoft, zero SmartScreen or Smart App Control prompts, native Windows Startup Apps integration, clean `%LOCALAPPDATA%\CircadianDesk\` uninstall):
```powershell
winget install <StoreProductID> --source msstore
```
*(Build & Partner Center certification instructions: [`packaging/msix/README_MSIX.md`](packaging/msix/README_MSIX.md).)*

### 2. Portable GitHub Release (`--onedir --noupx`) & Package Managers
Every official portable release is compiled reproducibly on public GitHub Actions (`windows-latest`, official PSF x64 CPython) with `SHA256SUMS.txt` and SLSA build provenance:
- **Portable ZIP:** Download `CircadianDesk-v1.0.0-win64-portable.zip` from [GitHub Releases](https://github.com/cagriaksoy191-oss/circadian-desk/releases/latest), verify its checksum against `SHA256SUMS.txt` (or run `gh attestation verify CircadianDesk-v1.0.0-win64-portable.zip -R cagriaksoy191-oss/circadian-desk`), extract `CircadianDesk\`, and launch `CircadianDesk.exe`.
- **WinGet (Portable):** `winget install CircadianDesk.CircadianDesk` *(Schema `1.28.0`)*
- **Custom Scoop Bucket:** `scoop bucket add circadiandesk https://github.com/cagriaksoy191-oss/scoop-circadiandesk && scoop install circadiandesk`
- **SmartScreen Note:** Portable GitHub builds are currently unsigned while SignPath Foundation OSS signing approval is pending; Windows Defender SmartScreen may show *"Unknown publisher"* on first launch (click *More info → Run anyway*, verify `SHA256SUMS.txt`, or use the Microsoft-signed Store MSIX above).

### 3. Run Directly from Source (Python 3.10+, Zero Runtime Pip Dependencies)
```powershell
cd prototype
# Interactive first-run setup (saves to %LOCALAPPDATA%\CircadianDesk\config.json):
python -m orchestrator.daemon --setup

# Launch local Braun Control Panel (http://127.0.0.1:8765) + Tray + HDR/Game Auto-Pause:
python -m orchestrator.daemon --config --open-loop --monitor --web-ui --tray --auto-pause --open-browser

# Verify CLI version (fast-path exit 0):
python -m orchestrator.daemon --version
```

---

## Key Capabilities (v1.0.0)
- **Mandatory First-Run Setup Gate:** Holds your screen untouched (`AWAITING_SETUP`) until you confirm your wake & sleep schedule (Every Day, Weekdays/Weekends, or wake-anchored 7-Day schedule), followed by an 8-second gentle evening ramp.
- **Physically Calibrated Photobiology:** MIRED-space transitions, 1D-LUT luminance retention (`Y_lut`) compensation (no double-dimming), panel spectrum profiles (`W-LED IPS`, `DCI-P3 IPS`, `WOLED`, `QD-OLED` with `>=42 nit` OLED anti-PWM floor), EDID physical solid-angle ($\Omega_{\text{proj}}$) corneal `m-EDI` estimate, and bounded Windows Dark Mode `APL_eff`.
- **Windows 11 Reliability:** `GET_ADVANCED_COLOR_INFO_2 (type=15)` **HDR Auto-Pause**, Modern Standby (`S0ix`) wake re-sync (`WS_POPUP` + `GUID_CONSOLE_DISPLAY_STATE` + `PowerRegisterSuspendResumeNotification`), neutral `6500K` `MagUninitialize()` for native VRR/MPO gaming, DDC/CI EEPROM debounce, global hotkeys (`Ctrl+Alt+P` Pause 2h, `Ctrl+Alt+M` Movie Mode 2.5h @ `3400K`), and live hover tooltip.
- **Interactive 24-Hour Curve Scrubber:** Drag anywhere along the 24h Kelvin-gradient curve to live-preview any hour on your real display; snaps smoothly back to live time `1.5s` after release.
- **Optional Local Smart Bulbs & Hardware Sensor (`V1A`):** Supports `--light none` (display-only), `--light wiz`, `--light hue`, `--mqtt-host` (zero-dep Home Assistant MQTT auto-discovery), `--log-csv` + offline SVG `--report`, and `--port COMx` for Raspberry Pi Pico 2 (`RP2350A`) spectral bench testing (see [`firmware/`](firmware/)).

---

## Testing & Local Packaging
```powershell
# Run deterministic offline pytest suite (zero hardware required):
.\.venv\Scripts\pytest.exe -v

# Dry-run simulation demo & offline 7-day SVG compliance report:
.\.venv\Scripts\python.exe -m orchestrator.daemon --simulate day_to_evening_demo --light none --ticks 8 --interval 0.1 --log-csv demo.csv
.\.venv\Scripts\python.exe -m orchestrator.daemon --report demo.csv

# Controlled 3-second Windows display safety restore check (45% @ 3000K -> exact restore):
.\.venv\Scripts\python.exe -m orchestrator.daemon --monitor --monitor-safety-check --safety-hold-s 3.0

# Build local --onedir --noupx Portable ZIP + MSIX staging directory:
.\.venv\Scripts\python.exe build_windows.py --mode onedir --msix-stage
```

---

## Scientific Scope & License
Target corneal illuminance thresholds (`>=250 lx` daytime, `<=10 lx` evening / `<=5 lx` high-sensitivity strict, `<=1 lx` night `m-EDI`) follow **Brown et al. (2022)** visual-comfort guidelines for healthy daytime-active adults (**ages 18–55**). Circadian Desk is **not a medical device** and makes no diagnostic, therapeutic, or sleep claims.

Released under the **MIT License** ([`LICENSE`](LICENSE)).
