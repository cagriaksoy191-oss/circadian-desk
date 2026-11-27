"""Reproducible Zero-Python Windows Release & MSIX Builder for Circadian Desk v1.0.0.

Supports dual packaging (Guardrails G4 & G5):
- `--mode onedir` (default): Zero-AV `--noupx` PyInstaller directory bundle wrapped cleanly inside
  a top-level `CircadianDesk/` folder in the Portable ZIP (compatible with WinGet schema 1.28.0
  `NestedInstallerFiles`) AND staged directly for Microsoft Store MSIX (`--msix-stage`).
- `--mode onefile`: Optional single-file `--noupx` executable with embedded `asInvoker` +
  `PerMonitorV2` Win32 manifest and `1.0.0.0` PE version info.
- Pure-stdlib transparent Braun PNG generator (zero Pillow!) emitting the complete Microsoft Store
  MSIX icon & unplated taskbar asset matrix (every PNG < 204,800 bytes) + `SHA256SUMS.txt`.

Usage:
    python build_windows.py                          # Full --onedir build + Portable ZIP + SHA256SUMS
    python build_windows.py --mode onedir --msix-stage  # Also stage dist/msix_stage for makepri/makeappx
    python build_windows.py --mode onefile           # Legacy single-file build
    python build_windows.py --icon-only              # Ensure Braun .ico only
    python build_windows.py --assets-only            # Generate MSIX PNG matrix only
    python build_windows.py --check-only             # Validate prerequisites without building
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

APP_NAME = "CircadianDesk"
APP_DISPLAY_NAME = "Circadian Desk"
APP_VERSION = "1.0.0"
APP_VERSION_QUAD = "1.0.0.0"
APP_VERSION_TUPLE = (1, 0, 0, 0)
PROTO_DIR = Path(__file__).resolve().parent
ICON_REL = Path("assets") / "circadian_desk.ico"
ENTRYPOINT_REL = Path("desktop_entry.py")
MSIX_MANIFEST_REL = Path("packaging") / "msix" / "AppxManifest.xml"
DIST_DIR = PROTO_DIR / "dist"
BUILD_META_DIR = PROTO_DIR / "build" / "win_meta"
ZIP_FILENAME = f"{APP_NAME}-v{APP_VERSION}-win64-portable.zip"
MSIX_MAX_ASSET_BYTES = 204_800  # Microsoft Store 200 KiB limit per visual asset

EXCLUDED_STDLIB_MODULES: tuple[str, ...] = (
    "tkinter",
    "_tkinter",
    "unittest",
    "test",
    "idlelib",
    "pydoc",
    "doctest",
)

# Complete Microsoft Store MSIX visual asset matrix: (filename, pixel_size, light_theme)
MSIX_PNG_ASSET_SPECS: tuple[tuple[str, int, bool], ...] = (
    ("StoreLogo.png", 50, False),
    ("StoreLogo.scale-100.png", 50, False),
    ("StoreLogo.scale-200.png", 100, False),
    ("StoreLogo.scale-400.png", 200, False),
    ("Square150x150Logo.png", 150, False),
    ("Square150x150Logo.scale-100.png", 150, False),
    ("Square150x150Logo.scale-200.png", 300, False),
    ("Square150x150Logo.scale-400.png", 600, False),
    ("Square44x44Logo.png", 44, False),
    ("Square44x44Logo.scale-100.png", 44, False),
    ("Square44x44Logo.scale-200.png", 88, False),
    ("Square44x44Logo.scale-400.png", 176, False),
    ("Square44x44Logo.targetsize-16_altform-unplated.png", 16, False),
    ("Square44x44Logo.targetsize-24_altform-unplated.png", 24, False),
    ("Square44x44Logo.targetsize-32_altform-unplated.png", 32, False),
    ("Square44x44Logo.targetsize-48_altform-unplated.png", 48, False),
    ("Square44x44Logo.targetsize-256_altform-unplated.png", 256, False),
    ("Square44x44Logo.targetsize-16_altform-lightunplated.png", 16, True),
    ("Square44x44Logo.targetsize-24_altform-lightunplated.png", 24, True),
    ("Square44x44Logo.targetsize-32_altform-lightunplated.png", 32, True),
    ("Square44x44Logo.targetsize-48_altform-lightunplated.png", 48, True),
    ("Square44x44Logo.targetsize-256_altform-lightunplated.png", 256, True),
    ("StoreListing_300x300.png", 300, False),
)

WIN32_APP_MANIFEST_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity version="{APP_VERSION_QUAD}" processorArchitecture="amd64" name="CircadianDesk.LocalLightOrchestrator" type="win32"/>
  <description>Circadian Desk — Local-First Windows Display &amp; Lighting Orchestrator</description>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
    </windowsSettings>
  </application>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}}"/>
    </application>
  </compatibility>
</assembly>
"""

PE_VERSION_INFO_TEMPLATE = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={APP_VERSION_TUPLE},
    prodvers={APP_VERSION_TUPLE},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Circadian Desk (Open Source)'),
          StringStruct('FileDescription', 'Circadian Desk — Local Display & Light Orchestrator'),
          StringStruct('FileVersion', '{APP_VERSION_QUAD}'),
          StringStruct('InternalName', '{APP_NAME}'),
          StringStruct('LegalCopyright', 'MIT License — Circadian Desk Contributors'),
          StringStruct('OriginalFilename', '{APP_NAME}.exe'),
          StringStruct('ProductName', '{APP_DISPLAY_NAME}'),
          StringStruct('ProductVersion', '{APP_VERSION_QUAD}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""

QUICKSTART_TEXT = f"""{APP_DISPLAY_NAME} v{APP_VERSION} — Local Windows Desktop Release (Zero-Python Portable Edition)
================================================================================

1. Extract the {APP_NAME} folder and double-click {APP_NAME}.exe — no Python install,
   no admin rights, no Registry hacks, and no Windows reboot required.
2. On first launch, your local Braun Control Panel opens at http://127.0.0.1:8765 (100% offline)
   while holding your screen untouched until you confirm your wake & sleep schedule.
3. Right-click the System Tray icon anytime to Pause 2h (Ctrl+Alt+P), toggle Movie Mode 2.5h
   (Ctrl+Alt+M), open the Control Panel, or click "Quit & Restore Display".
4. Auto-Pause reads ONLY the foreground app's .exe name (zero pixel capture) and automatically
   pauses when Windows HDR, Photoshop/Figma/Resolve, or fullscreen games are active.
5. Verification & SmartScreen Note: This portable ZIP is compiled cleanly on public GitHub Actions
   (--onedir --noupx) with SHA256SUMS.txt and build provenance attestation. For zero SmartScreen
   warnings out of the box, install the Microsoft-signed Store MSIX edition ($0).
6. Optical Scope: Targets (>=250 lx day, <=10 lx evening / <=5 lx strict, <=1 lx night m-EDI)
   follow Brown et al. (2022) visual-comfort guidelines for healthy daytime-active adults
   (ages 18-55). Not a medical device; makes no diagnostic, therapeutic, or sleep claims.
7. Settings & Logs: Saved locally under %LOCALAPPDATA%\\CircadianDesk\\ (zero cloud).
"""


def _ensure_proto_on_syspath() -> None:
    if str(PROTO_DIR) not in sys.path:
        sys.path.insert(0, str(PROTO_DIR))


def _ensure_braun_icon() -> Path:
    _ensure_proto_on_syspath()
    from orchestrator.tray_win32 import ensure_icon_file

    icon_path = PROTO_DIR / ICON_REL
    return ensure_icon_file(icon_path)


def generate_msix_assets(assets_dir: Path) -> list[Path]:
    """Generate the complete transparent Braun PNG matrix for Microsoft Store MSIX (<204,800 B each)."""
    _ensure_proto_on_syspath()
    from orchestrator.tray_win32 import generate_braun_png_bytes

    assets_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    for filename, dim, light_theme in MSIX_PNG_ASSET_SPECS:
        data = generate_braun_png_bytes(size=dim, light_theme=light_theme)
        if len(data) >= MSIX_MAX_ASSET_BYTES:
            raise ValueError(
                f"Generated MSIX asset {filename} ({len(data)} B) exceeds Store limit ({MSIX_MAX_ASSET_BYTES} B)"
            )
        out_path = assets_dir / filename
        out_path.write_bytes(data)
        generated.append(out_path)
    return generated


def write_win32_build_metadata(meta_dir: Path | None = None) -> tuple[Path, Path]:
    """Write Win32 `asInvoker`+`PerMonitorV2` manifest and PE `1.0.0.0` version-info file."""
    target_dir = meta_dir if meta_dir is not None else BUILD_META_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = target_dir / f"{APP_NAME}.exe.manifest"
    version_path = target_dir / "version_info.txt"
    manifest_path.write_text(WIN32_APP_MANIFEST_XML, encoding="utf-8")
    version_path.write_text(PE_VERSION_INFO_TEMPLATE, encoding="utf-8")
    return manifest_path, version_path


def build_pyinstaller_command(
    python_exe: str,
    icon_path: Path,
    *,
    mode: str = "onedir",
    manifest_path: Path | None = None,
    version_file: Path | None = None,
) -> list[str]:
    """Return deterministic PyInstaller CLI flags (`--onedir` or `--onefile`, ALWAYS `--noupx`)."""
    if mode not in ("onedir", "onefile"):
        raise ValueError(f"Unsupported build mode: {mode!r} (expected 'onedir' or 'onefile')")
    data_sep = ";" if sys.platform == "win32" else ":"
    cmd = [
        python_exe,
        "-m",
        "PyInstaller",
        f"--{mode}",
        "--noconsole",
        "--noupx",
        "--name",
        APP_NAME,
        "--icon",
        str(icon_path),
        "--add-data",
        f"{icon_path}{data_sep}assets",
        "--collect-submodules",
        "orchestrator",
        "--collect-submodules",
        "spectral",
        "--clean",
        "--noconfirm",
    ]
    for mod in EXCLUDED_STDLIB_MODULES:
        cmd.extend(["--exclude-module", mod])
    if manifest_path is not None:
        cmd.extend(["--manifest", str(manifest_path)])
    if version_file is not None:
        cmd.extend(["--version-file", str(version_file)])
    cmd.append(str(PROTO_DIR / ENTRYPOINT_REL))
    return cmd


def package_portable_zip(
    build_target: Path,
    output_zip: Path,
    *,
    mode: str = "onedir",
) -> Path:
    """Create the shippable portable ZIP with clean `CircadianDesk/` folder layout (Guardrail G4)."""
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    license_path = PROTO_DIR / "LICENSE"
    if not license_path.is_file():
        raise FileNotFoundError(f"Missing LICENSE file at {license_path}")

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        if mode == "onedir":
            if not build_target.is_dir():
                raise FileNotFoundError(f"Built --onedir folder not found at {build_target}")
            for item in sorted(build_target.rglob("*")):
                if item.is_file():
                    rel = item.relative_to(build_target).as_posix()
                    zf.write(item, arcname=f"{APP_NAME}/{rel}")
            zf.writestr(f"{APP_NAME}/README_QUICKSTART.txt", QUICKSTART_TEXT)
            zf.write(license_path, arcname=f"{APP_NAME}/LICENSE")
        else:
            if not build_target.is_file():
                raise FileNotFoundError(f"Built executable not found at {build_target}")
            zf.write(build_target, arcname=build_target.name)
            zf.writestr("README_QUICKSTART.txt", QUICKSTART_TEXT)
            zf.write(license_path, arcname="LICENSE")
    return output_zip


def write_sha256_sums(files: list[Path], output_path: Path) -> Path:
    """Write deterministic lowercase SHA-256 checksums (`<hex>  <filename>`) for release artifacts."""
    lines: list[str] = []
    for file_path in files:
        if not file_path.is_file():
            continue
        h = hashlib.sha256()
        with file_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        lines.append(f"{h.hexdigest()}  {file_path.name}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return output_path


def stage_msix_directory(
    onedir_dist: Path,
    stage_dir: Path,
    *,
    publisher: str | None = None,
    identity_name: str | None = None,
) -> Path:
    """Stage `AppxManifest.xml`, `--onedir` binaries, and full Braun PNG asset matrix for MSIX."""
    manifest_src = PROTO_DIR / MSIX_MANIFEST_REL
    if not manifest_src.is_file():
        raise FileNotFoundError(f"Missing MSIX manifest template at {manifest_src}")
    if not onedir_dist.is_dir():
        raise FileNotFoundError(f"Missing --onedir build output at {onedir_dist}")

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Copy --onedir payload straight into MSIX package root so Executable="CircadianDesk.exe" resolves directly.
    for item in onedir_dist.iterdir():
        dest = stage_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    # Generate complete transparent Braun PNG asset matrix inside stage_dir/Assets
    generate_msix_assets(stage_dir / "Assets")

    # Copy AppxManifest.xml (optionally overriding Partner Center Identity Name & Publisher)
    import re

    manifest_xml = manifest_src.read_text(encoding="utf-8")
    if identity_name:
        manifest_xml = re.sub(
            r'(<Identity\b[^>]*\bName=")[^"]*(")',
            rf'\g<1>{identity_name}\2',
            manifest_xml,
            count=1,
            flags=re.S,
        )
    if publisher:
        manifest_xml = re.sub(
            r'(<Identity\b[^>]*\bPublisher=")[^"]*(")',
            rf'\g<1>{publisher}\2',
            manifest_xml,
            count=1,
            flags=re.S,
        )
    if "REPLACE-WITH-MICROSOFT-PARTNER-CENTER-PUBLISHER-ID" in manifest_xml:
        print(
            "[WARN] AppxManifest.xml still contains placeholder Publisher CN. "
            "Pass --msix-publisher 'CN=...' before uploading to Microsoft Partner Center."
        )
    (stage_dir / "AppxManifest.xml").write_text(manifest_xml, encoding="utf-8")
    return stage_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build zero-Python Circadian Desk Windows Portable ZIP and MSIX staging bundle."
    )
    parser.add_argument(
        "--mode",
        choices=("onedir", "onefile"),
        default="onedir",
        help="PyInstaller bundle mode: onedir (default, zero-AV & MSIX-ready) or onefile.",
    )
    parser.add_argument(
        "--msix-stage",
        action="store_true",
        default=False,
        help="Also stage dist/msix_stage/ with AppxManifest.xml, binaries, and full Braun PNG matrix.",
    )
    parser.add_argument(
        "--msix-publisher",
        default=None,
        help="Optional Microsoft Partner Center Publisher CN (e.g., CN=XXXXXXXX-XXXX-...).",
    )
    parser.add_argument(
        "--msix-identity",
        default=None,
        help="Optional Microsoft Partner Center reserved Package Identity Name.",
    )
    parser.add_argument(
        "--icon-only",
        action="store_true",
        help="Only generate/verify assets/circadian_desk.ico and exit.",
    )
    parser.add_argument(
        "--assets-only",
        action="store_true",
        help="Only generate the full MSIX transparent Braun PNG matrix under packaging/msix/Assets/ and exit.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate entrypoint, LICENSE, icon, manifest, and PyInstaller availability without building.",
    )
    args = parser.parse_args(argv)

    icon_path = _ensure_braun_icon()
    print(f"[1/4] Braun icon verified: {icon_path} ({icon_path.stat().st_size} bytes)")
    if args.icon_only:
        return 0

    if args.assets_only:
        assets = generate_msix_assets(PROTO_DIR / "packaging" / "msix" / "Assets")
        max_b = max(a.stat().st_size for a in assets)
        print(f"      Generated {len(assets)} Braun MSIX PNG assets (max={max_b} B < {MSIX_MAX_ASSET_BYTES} B)")
        return 0

    entry_path = PROTO_DIR / ENTRYPOINT_REL
    license_path = PROTO_DIR / "LICENSE"
    manifest_tpl = PROTO_DIR / MSIX_MANIFEST_REL
    has_pyinstaller = importlib.util.find_spec("PyInstaller") is not None

    if args.check_only:
        print(f"      Mode        : {args.mode} (--noupx)")
        print(f"      Version     : {APP_VERSION} ({APP_VERSION_QUAD})")
        print(f"      Entrypoint  : {entry_path} (exists={entry_path.is_file()})")
        print(f"      LICENSE     : {license_path} (exists={license_path.is_file()})")
        print(f"      MSIX Manif. : {manifest_tpl} (exists={manifest_tpl.is_file()})")
        print(f"      PyInstaller : {'available' if has_pyinstaller else 'NOT installed'}")
        return 0 if (entry_path.is_file() and license_path.is_file() and manifest_tpl.is_file()) else 1

    if not has_pyinstaller:
        print(
            "[ERROR] PyInstaller is not installed in the active Python environment.\n"
            f'        Install once with: "{sys.executable}" -m pip install pyinstaller',
            file=sys.stderr,
        )
        return 1

    win_manifest, version_file = write_win32_build_metadata()
    cmd = build_pyinstaller_command(
        sys.executable,
        icon_path,
        mode=args.mode,
        manifest_path=win_manifest,
        version_file=version_file,
    )
    print(f"[2/4] Running PyInstaller ({args.mode}, --noupx): {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(PROTO_DIR))
    if proc.returncode != 0:
        print(f"[ERROR] PyInstaller failed with exit code {proc.returncode}.", file=sys.stderr)
        return proc.returncode

    exe_name = f"{APP_NAME}.exe" if sys.platform == "win32" else APP_NAME
    build_target = (DIST_DIR / APP_NAME) if args.mode == "onedir" else (DIST_DIR / exe_name)
    zip_path = DIST_DIR / ZIP_FILENAME
    package_portable_zip(build_target, zip_path, mode=args.mode)

    (DIST_DIR / "README_QUICKSTART.txt").write_text(QUICKSTART_TEXT, encoding="utf-8")
    shutil.copy2(license_path, DIST_DIR / "LICENSE")

    checksum_targets = [zip_path]
    if args.mode == "onefile" and build_target.is_file():
        checksum_targets.append(build_target)
    elif args.mode == "onedir" and (build_target / exe_name).is_file():
        checksum_targets.append(build_target / exe_name)
    sums_path = write_sha256_sums(checksum_targets, DIST_DIR / "SHA256SUMS.txt")

    size_mb = zip_path.stat().st_size / (1024.0 * 1024.0)
    print(f"[3/4] Portable release packaged: {zip_path} ({size_mb:.2f} MB) + {sums_path.name}")

    if args.msix_stage:
        if args.mode != "onedir":
            print("[ERROR] --msix-stage requires --mode onedir (avoid --onefile inside MSIX).", file=sys.stderr)
            return 1
        stage_dir = stage_msix_directory(
            build_target,
            DIST_DIR / "msix_stage",
            publisher=args.msix_publisher,
            identity_name=args.msix_identity,
        )
        print(f"[4/4] MSIX staging directory ready: {stage_dir}")
    else:
        print("[4/4] Done (pass --msix-stage to prepare dist/msix_stage/ for makepri/makeappx).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
