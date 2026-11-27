# Microsoft Store MSIX, WinGet & Scoop Release Guide (`$0` First-Try Certification)

## 1. Why `$0` Microsoft Store MSIX Is Our Tier-1 Distribution Channel
- **Microsoft Partner Center Individual Account (`$0`, Turkey Included):** Since September 2025, individual developer registration is **$0** with standard government ID + selfie KYC. Because Circadian Desk is a **$0 free desktop app** (no in-app purchases), **no W-8BEN / tax payout profile is required** to publish!
- **Automatic Microsoft Signing & Zero SmartScreen / Smart App Control Blocks:** Microsoft Store signs the uploaded `.msix` with its trusted production signature (`AppxMetadata\CodeIntegrity.cat`), bypassing both SmartScreen and Windows 11 Smart App Control (SAC) completely.
- **Automatic `msstore` WinGet Listing:** Once published to Store, users can also install via `winget install <StoreProductID> --source msstore`.
- **Mandatory PyInstaller Rule (`--onedir --noupx` + Official PSF CPython):**
  - Always build MSIX with **`--mode onedir --noupx`** using official **python.org PSF x64 CPython** (`python311.dll` Microsoft-signed by Python Software Foundation).
  - Never ship `--onefile` inside MSIX (redundant `%TEMP%\_MEI` extraction hurts cold start & WACK/AV trust) and never build releases with unsigned `python-build-standalone` / `uv` runtimes (blocked by Smart App Control!).

---

## 2. Local MSIX Build & Packaging Steps (`makepri.exe` + `makeappx.exe`)
From `prototype/`:
```powershell
# 1. Build --onedir --noupx bundle + complete transparent Braun PNG matrix + msix_stage/
.\.venv\Scripts\python.exe build_windows.py --mode onedir --msix-stage `
  --msix-identity "YOUR-RESERVED-PARTNER-CENTER-IDENTITY" `
  --msix-publisher "CN=YOUR-PARTNER-CENTER-PUBLISHER-ID"

# 2. Generate resources.pri via Windows SDK makepri.exe (disable AutoMerge; do NOT pass /m)
$SdkBin = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64"
& "$SdkBin\makepri.exe" createconfig /cf "dist\priconfig.xml" /dq en-US /o
# Ensure AutoMerge is off in priconfig.xml, then compile resources.pri:
& "$SdkBin\makepri.exe" new /pr "dist\msix_stage" /cf "dist\priconfig.xml" /mn "dist\msix_stage\AppxManifest.xml" /of "dist\msix_stage\resources.pri" /o

# 3. Pack MSIX (do NOT pass /nv — let makeappx validate manifest & assets!)
& "$SdkBin\makeappx.exe" pack /d "dist\msix_stage" /p "dist\CircadianDesk_1.0.0.0_x64.msix" /o
```

*(Upload the unsigned `.msix` directly to Partner Center — Microsoft Store applies its production signature automatically. For local sideloading tests only, sign with a temporary self-signed cert via `signtool.exe`.)*

---

## 3. Microsoft Partner Center Certification Notes (Copy-Paste Ready)

### A. `runFullTrust` Justification (Store Policy `10.1` / `10.2`)
> **Why `runFullTrust` is required:** Circadian Desk is a local-first Windows display comfort and smart-lighting utility. It requires standard non-elevated Win32 APIs unavailable inside UWP sandboxes: `WmiMonitorBrightness` / `WmiSetBrightness` (laptop backlight), `dxva2.dll` DDC/CI `VCP 0x10` (external monitor backlight), `Magnification.dll` `MagSetFullscreenColorEffect` (zero-admin GPU color temperature), `DisplayConfigGetDeviceInfo` (read-only Windows HDR/ACM auto-pause), `Shell_NotifyIconW` (System Tray), and `GetForegroundWindow` + `QueryFullProcessImageNameW` (reads foreground `.exe` name only to auto-pause during Photoshop/games — zero screen pixel capture, zero window titles). It requests `asInvoker` (never admin), runs strictly offline (`127.0.0.1:8765` local UI, zero outbound internet), and stores settings strictly under `%LOCALAPPDATA%\CircadianDesk\` (Policy `10.2.7`).

### B. Notes for Certification Testers (Store Policy `10.1.2` / `10.2.8` / `10.5.1`)
> 1. **Interactive Launch (Policy `10.1.2`):** Clicking Circadian Desk from the Start Menu opens the local offline Control Panel (`http://127.0.0.1:8765`) in the default browser and places an icon in the System Tray. On first launch, the display is **NOT modified** until the user completes the single-screen wake/sleep schedule setup. Re-clicking the Start Menu icon while already running brings up the existing `127.0.0.1:8765` Control Panel via single-instance mutex detection.
> 2. **Background Startup (Policy `10.2.8`):** `windows.startupTask` (`CircadianDeskStartup`) defaults to `Enabled="false"`. When enabled by the user in Windows Settings → Startup Apps, Windows launches `CircadianDesk.exe --background` quietly into the System Tray without opening a browser window. Under MSIX, the app never writes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
> 3. **VM Testing Note:** Inside Hyper-V/Azure test VMs without physical laptop WMI or DDC/CI monitors, hardware dimming gracefully degrades while the local UI (`127.0.0.1:8765`), 24-hour curve scrubber, System Tray menu, and clean exit restore (`Quit & Restore Display`) all function normally.
> 4. **Privacy Policy (Policy `10.5.1`):** Zero telemetry, zero analytics, zero cloud accounts, zero camera/microphone access, zero screen recording. Full declaration in repository `PRIVACY.md`.

---

## 4. Day-1 `winget-pkgs` (Schema `1.28.0`) & Custom Scoop Bucket
- **WinGet Portable ZIP Manifest (`ManifestVersion: 1.28.0`):**
  - `InstallerType: zip`
  - `NestedInstallerType: portable`
  - `NestedInstallerFiles: [{ RelativeFilePath: "CircadianDesk\\CircadianDesk.exe", PortableCommandAlias: "circadian-desk" }]`
  - `InstallerUrl`: GitHub Releases `CircadianDesk-v1.0.0-win64-portable.zip` (built strictly by `.github/workflows/release.yml`!)
  - Verify `--version` (`CircadianDesk.exe --version` → prints `Circadian Desk 1.0.0`, exits `0`).
- **Day-1 Custom Scoop Bucket (`scoop-bucket`):**
  - Official `ScoopInstaller/Extras` requires `>= 100` GitHub stars / `>= 50` forks. Ship a Day-1 custom bucket (`scoop bucket add circadiandesk https://github.com/cagriaksoy191-oss/scoop-circadiandesk && scoop install circadiandesk`) pointing at the exact same GitHub Release ZIP + SHA-256, then submit to `Extras` once the repo crosses 100 stars.
