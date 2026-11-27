# Code signing policy

This document defines the release signing, governance, and offline privacy commitments for **Circadian Desk** (MIT License), structured to meet the **SignPath Foundation** open-source code signing eligibility criteria and Microsoft Store MSIX distribution standards.

## Current Signing Status (Transparent Declaration)
- **Microsoft Store MSIX Package:** Automatically signed by Microsoft upon Store certification (`AppxMetadata\CodeIntegrity.cat`), providing zero-SmartScreen installation on Windows 10 and Windows 11.
- **GitHub Releases Portable ZIP (`CircadianDesk-v1.0.0-win64-portable.zip`):** Built reproducibly on public GitHub Actions (`windows-latest` using official PSF x64 CPython) with `SHA256SUMS.txt` and GitHub Artifact Build Provenance (`actions/attest-build-provenance`). Portable GitHub binaries are **currently unsigned** while the project's SignPath Foundation OSS application is pending approval; we do not claim active SignPath Authenticode signing until approval is granted and verified in release logs.

## Privacy Declaration
> This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it.

By default, Circadian Desk operates 100% locally (`127.0.0.1` loopback web UI only, zero outbound internet requests, zero telemetry, zero screen capture). Optional local-LAN communication (WiZ UDP, Philips Hue Bridge CLIP v2, or Home Assistant MQTT) activates **only** when explicitly configured by the user with a local IP address.

## Team Roles & Release Governance
Circadian Desk is maintained as an open-source solo-maintainer project with strict CI-only release hygiene:
- **Authors / Committers:** Project maintainer (`@cagriaksoy191-oss`) and accepted public pull-request contributors whose commits land on `main` only after automated `pytest` and syntax checks pass.
- **Reviewers:** Project maintainer reviews all code changes, dependency boundaries (zero runtime third-party pip dependencies), and GitHub Actions workflow definitions prior to tagging a release.
- **Approvers:** Project maintainer holds exclusive release-tag and signing-approval authority; signing requests are submitted exclusively from tagged, reproducible `.github/workflows/release.yml` runs on GitHub-hosted `windows-latest` runners (never from local developer workstations).

## Reproducible Build & Verification Controls
1. Every official release artifact is built exclusively from a tagged commit via `.github/workflows/release.yml` on `windows-latest` using official `python.org` PSF-signed CPython (`python3*.dll` Authenticode-verified during CI) and PyInstaller `--onedir --noupx`.
2. Release builds publish `SHA256SUMS.txt` and SLSA build provenance attestations verifiable via `gh attestation verify`.
3. Once SignPath Foundation OSS signing is approved and `SIGNPATH_API_TOKEN` is provisioned, the conditional `signpath/github-action-submit-signing-request@v3` step in `.github/workflows/release.yml` signs release binaries using SignPath HSM-backed certificates with zero local private-key handling.
