# Privacy Policy — Circadian Desk

**Effective Date:** November 2026  
**Applies to:** Circadian Desk for Windows (Microsoft Store MSIX package, Portable ZIP, and open-source Python runtime)

## 1. Zero Data Collection (100% Local-First by Design)
Circadian Desk is a local-only Windows display comfort and smart-lighting utility. By default, **Circadian Desk collects, transmits, sells, or shares zero personal data, zero usage analytics, zero crash telemetry, and zero cloud accounts.**

- **Zero Outbound Internet Traffic:** Circadian Desk makes **0** outbound internet requests. Its local Braun Control Panel binds strictly to loopback (`127.0.0.1`, default port `8765`) with Host/Origin CSRF protection and contains zero external CDN, font, image, or script URLs.
- **Zero Screen Capture, Camera, or Microphone Access:** Circadian Desk never captures desktop pixels (`DXGI` / `BitBlt` / screen recording), never accesses your webcam or microphone, and never reads browser tabs or window titles.
- **Foreground Process Name Only (Optional Auto-Pause):** When Auto-Pause is enabled, Circadian Desk reads **only** the foreground process executable filename (e.g., `Photoshop.exe`) via Win32 `GetForegroundWindow` + `QueryFullProcessImageNameW` solely in RAM to pause color filtering for creative apps and fullscreen games. Process names are never written to disk or transmitted anywhere.

## 2. Local On-Device Storage (Clean Uninstall)
All user settings and optional telemetry logs stay strictly on your local Windows machine under:
- `%LOCALAPPDATA%\CircadianDesk\` (`config.json`, `app.log`, optional `desk_log.csv` and offline SVG/HTML 7-day compliance reports).
- When installed via the **Microsoft Store (MSIX)**, Windows maps `%LOCALAPPDATA%` writes to the package's `LocalCache` folder and removes them automatically upon uninstall (Microsoft Store Policy `10.2.7`).
- No Windows Registry modifications are made unless you explicitly enable *Start Automatically with Windows* in the unpackaged Portable edition (under MSIX, startup is managed natively by Windows via `windows.startupTask`).

## 3. Optional User-Configured Local Network Integrations
If you explicitly configure an optional local smart-light backend or smart-home bridge, Circadian Desk communicates **only** with the local LAN IP address you specify:
- Local WiZ smart bulbs (LAN UDP port `38899`)
- Local Philips Hue Bridge (LAN HTTPS CLIP API v2)
- Local Home Assistant MQTT broker (LAN TCP port `1883`)

These integrations are disabled by default (`--light none`, display-only mode) and never contact third-party cloud servers.

## 4. Non-Medical Scope
Circadian Desk implements optical visual-comfort guidelines based on Brown et al. (2022) for healthy daytime-active adults (ages 18–55). It is **not a medical device** and does not collect health data or make diagnostic, therapeutic, or sleep claims.

## 5. Contact & Transparency
Circadian Desk is open-source software licensed under the MIT License. You can inspect every line of source code or open a privacy question via the project's public GitHub repository Issues tracker: [https://github.com/cagriaksoy191-oss/circadian-desk/issues](https://github.com/cagriaksoy191-oss/circadian-desk/issues).
