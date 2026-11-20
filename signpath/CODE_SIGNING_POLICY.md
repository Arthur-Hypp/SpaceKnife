# Code Signing Policy — SpaceKnife

This document describes how release binaries of SpaceKnife are built and signed.
It is provided for the SignPath Foundation open-source application review.

## Project

- Repository: https://github.com/Arthur-Hypp/SpaceKnife
- License: MIT
- Certificate provider: SignPath Foundation (free open-source code signing)
- Signed artifact: `CDriveSentinel.exe` (the entry point of the onedir bundle)

## Roles

This is a single-maintainer project:

| Role | Member |
|---|---|
| Committer / reviewer | @Arthur-Hypp |
| Approver | @Arthur-Hypp |

All source changes are made through this public repository. Every signing
request is approved manually by the maintainer in the SignPath dashboard.
Multi-factor authentication is enabled on both the GitHub and SignPath accounts.

## Build and release process

1. A maintainer pushes a `v*` tag (e.g. `v0.2.0`), or starts the workflow manually.
2. `.github/workflows/build-release.yml` runs entirely on **GitHub-hosted
   runners** (`windows-latest`):
   - checks out this repository,
   - runs the read-only self-test (`python src\main.py selftest`),
   - builds the onedir distributable with PyInstaller (`python build.py`),
   - uploads the built folder as a GitHub Actions artifact,
   - submits that artifact to SignPath via the official
     `signpath/github-action-submit-signing-request` action,
   - waits for manual approval, verifies the Authenticode signature,
   - packs the signed folder into a ZIP with a SHA-256 checksum,
   - publishes the ZIP as a GitHub Release asset.
3. The certificate private key never leaves SignPath's HSM; it is not available
   to the maintainer or to the CI workflow.

## Trusted build system

- Trusted Build System: **GitHub.com** (predefined), linked to this project.
- SignPath GitHub App is installed and granted access to this repository so the
  audit log can be evaluated.
- The signing policy enforces trusted-build-system and origin checks; only
  artifacts built by this repository's workflow on GitHub-hosted runners are
  signed.

## Signing policies

- `test-signing`: test certificate, used to validate the pipeline.
- `release-signing`: SignPath Foundation release certificate, requires manual
  approval by the maintainer.

## User impact

The distributed binary is a standalone, read-only Windows utility. Signing is
needed so Windows SmartScreen / Smart App Control does not block first-time
downloads. Users can verify the signature with:

```powershell
signtool verify /pa CDriveSentinel.exe
```
