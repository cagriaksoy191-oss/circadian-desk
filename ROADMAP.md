# Circadian Desk Roadmap

Circadian Desk follows a **software-first, user-signal-gated** engineering roadmap. Every milestone prioritizes Windows display reliability, zero-cloud privacy, and physically calibrated photobiology over feature bloat.

---

## Shipped — `v1.0.0` (Current Release)

- **Photobiology & Display Metrology**
  - Smooth, continuous **MIRED-space** (`10^6 / K`) circadian curve with von Kries transition rate limiting and an 8-second gentle first-evening activation glide.
  - **1D-LUT luminance retention (`Y_lut`) compensation** inside backlight mapping to prevent double-dimming when color temperature warms.
  - **Panel spectrum profiles** (`W-LED IPS`, `DCI-P3 IPS`, `WOLED`, `QD-OLED`) with QD-OLED `-350K` / P3 `-150K` melanopic compensation and a mandatory **`>= 42 nit` OLED hardware floor** (anti near-black crush & deep PWM).
  - Read-only **EDID physical dimensions (`Ω_proj`) & CIE 1931 gamut detection** + bounded Windows Dark/Light Mode (`AppsUseLightTheme`) corneal `m-EDI` estimation.
- **Windows 11 Reliability & Zero-Admin Architecture**
  - `GET_ADVANCED_COLOR_INFO_2 (type=15)` accurate SDR / WCG (ACM) / HDR detection with **automatic Windows HDR Auto-Pause** and auto-resume.
  - **Modern Standby (`S0ix`)** suspend/resume protection (`WS_POPUP` + `GUID_CONSOLE_DISPLAY_STATE` + `PowerRegisterSuspendResumeNotification` + `WM_DISPLAYCHANGE`).
  - Automatic **`MagUninitialize()`** at neutral `>= 6450K` to preserve native DWM MPO / DirectFlip / VRR behavior during daytime and gaming.
  - Hardware **DDC/CI (`VCP 0x10`) EEPROM wear guard** (`>= 450 ms` debounce, `>= 2%` deadband, transient retry) and clear diagnostic hints.
  - Zero-admin `asInvoker` execution, single-instance Win32 mutex, `--background` & `--version` CLI support, `%LOCALAPPDATA%\CircadianDesk\` clean storage, and MSIX Store manifest compliance.
- **UX & Local Integrations**
  - Mandatory untouched first-run setup gate (`AWAITING_SETUP`), zero-scroll 1320px Braun Bento local cockpit (`127.0.0.1:8765`), single-click zero-chevron time popover, **Interactive 24-Hour SVG Curve Scrubber**, 1-click Windows Settings shortcuts (`Night Light`, `Color Filters`, `Startup Apps`), live System Tray hover tooltip, global hotkeys (`Ctrl+Alt+P`, `Ctrl+Alt+M`), and offline 7-day SVG compliance report.
  - Optional local LAN smart-light sync: **Philips Hue (CLIP v2)**, **WiZ (UDP 38899)**, and **Home Assistant (MQTT auto-discovery)**.

---

## Distribution Milestones (`v1.0.x`)

- [ ] **Microsoft Store MSIX (`$0`)** — Microsoft-signed zero-SmartScreen package + automatic `winget install <StoreID> --source msstore`.
- [ ] **WinGet Community Manifest (`winget-pkgs`, Schema `1.28.0`)** — Portable ZIP installer pointing at reproducible GitHub Actions releases.
- [ ] **Scoop Distribution** — Day-1 custom bucket (`cagriaksoy191-oss/scoop-circadiandesk`) followed by `ScoopInstaller/Extras` once community eligibility thresholds (`>= 100` stars / `>= 50` forks) are met.
- [ ] **SignPath Foundation OSS Authenticode Signing (`$0`)** — Automated HSM signing of Portable ZIP releases directly from `.github/workflows/release.yml` (see [`CODE_SIGNING_POLICY.md`](CODE_SIGNING_POLICY.md)).

---

## Planned — `v1.1` (Gated by Real User Feedback)

Items below are intentionally scoped for `v1.1` so engineering effort follows real-world user requests from `v1.0.0`:

1. **Dynamic Per-Monitor `MHC2 ICC v4` Color Pipeline (`mscms.dll`)**
   - Native Windows 11 24H2/25H2 hardware 3×3 CSC + 4096-entry LUT profiles enabling smooth circadian warmth even while **Windows HDR / ACM stays enabled**, with strict driver/MPO safety fallbacks.
2. **Opt-In Experimental HDR SDR-White Brightness Control**
   - Optional crash-safe HDR luminance scaling (`SET_SDR_WHITE_LEVEL`) gated behind an explicit experimental toggle with guaranteed restore on exit/crash.
3. **Isolated Ambient Light Sensor & Fn-Key Sync**
   - Optional WinRT `LightSensor` ambient-glare input and WMI laptop Fn-brightness sync running in crash-isolated workers so manual brightness adjustments are respected smoothly.
4. **Offline Solar Twilight & Region Selector (NOAA Formula)**
   - Optional, user-confirmed latitude/region selector (zero GPS, zero internet) to decouple evening luminance glare control from biological sunset on winter night-owl schedules.
5. **Asymmetric Weekend Social-Jetlag Smoothing**
   - Gentle weekday/weekend circadian phase buffering (`<= 0.5–1.0 h/day` advance, `<= 1.0–1.5 h/day` delay; Eastman 2005/2009) to avoid premature bright light before `CBTmin` on Monday mornings.
6. **Multi-Monitor & Power-User Ergonomics**
   - Secondary-monitor *Soft Standby* dimming, 7-day manual override drift markers on the compliance chart, and live USB-CDC / MQTT JSON flicker diagnostic views.
7. **Cross-Platform Exploration**
   - macOS (`CoreDisplay` / `CGSetDisplayTransferByTable` + menu-bar agent) and Linux (`Wayland` / `ddcutil`) feasibility after Windows `v1.0` stabilizes.

---

## Future Optional Hardware — `V1A` Eye-Level Spectral Sensor

Circadian Desk will **always remain 100% free and complete as standalone software**. In parallel, an optional open-source **wired 5V USB-C multi-spectral desk sensor (`V1A`)** is in prototyping (`RP2350A` + `OLGA-8` multi-spectral front-end, PTFE 180° cosine diffuser, 100 ms mains-null integration, zero radio, zero battery, unified B+C+D magnetic mounts) to unlock live room ambient `m-EDI` (±15%) closed-loop lamp orchestration and 100/120 Hz flicker detection. Hardware crowdfunding will open **only** after sustained desktop software traction and validated community demand.

---

## Permanent Non-Goals (Architectural Guardrails)

To keep Circadian Desk trustworthy, lightweight, and safe on Windows:
- **Never** add cloud accounts, analytics, crash telemetry, or outbound internet connections.
- **Never** capture screen pixels (`DXGI` / `BitBlt`), webcam frames, microphone audio, or browser window titles.
- **Never** install global low-level mouse/keyboard hooks (`WH_MOUSE_LL` / `WH_KEYBOARD_LL`) that trigger AV/SmartScreen false positives.
- **Never** call external weather or IP-geolocation APIs.
- **Never** make unphysical "zero-color-shift 3-primary RGB blue blocking" or medical/diagnostic claims.
- **Never** gate core eye-comfort features behind subscriptions, nag screens, or padlocks.
