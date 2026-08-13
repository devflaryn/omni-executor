# Building `omni-exec.exe` on Windows

Everything here was **run on a real Windows 11 / amd64 host** (i7-13700F,
Windows PowerShell 5.1, Python 3.14.6, Node 24.11, PyInstaller 6.22) on
2026-08-13. Where something is unverified it says so.

## Prerequisites

| Need | Why | Check |
|---|---|---|
| Windows x86-64 | PyInstaller cannot cross-build; the Bliss guest is x86 | — |
| Python 3.13+ | the app + engine are stdlib-heavy | `python --version` |
| `pip install pyinstaller` | freezing | `python -m PyInstaller --version` |
| Node 20+ | React frontend | `npm --version` |
| Sibling `omnidroid` checkout | the engine is frozen INTO the exe | `..\omnidroid\omnidroid\__init__.py` exists |
| WHPX enabled | the VM will not boot without it | see below |

The repos must be **siblings** — `...\Omni Apps\{omni-executor, omnidroid}` —
because the spec adds `..\omnidroid` to `pathex` and there is no
`omnidroid.exe` beside the app at runtime.

## Build

```powershell
cd omni-executor
.\build-windows.ps1            # add -Zip to also produce dist\omni-exec-win64.zip
                              # add -SkipFrontend to reuse frontend\dist
```

Output: **`dist\omni-exec\omni-exec.exe`** — 5.8 MB exe inside a 40 MB
one-dir bundle.

The script builds the frontend, freezes via `OmniExecutor-win.spec`, and then
**smoke-tests engine dispatch**, which is the thing most likely to be
silently broken:

```powershell
.\dist\omni-exec\omni-exec.exe --omnidroid version --json
```

Verified output includes `"engine": "omnidroid"`, `"arch_aware": true`,
`"host_arch": "x86"`, `"ok": true` — i.e. the frozen binary re-executes
itself and runs the engine CLI in-process, with no `omnidroid.exe` and no
source checkout beside it.

### One-dir, not one-file

Deliberate. `main.py` resolves the engine as `[sys.executable, "--omnidroid"]`
when frozen, so **every engine call re-executes the binary**. A one-file
build re-unpacks the whole bundle to a fresh `%TEMP%\_MEIxxxx` on each of
those calls, and `omnidroid/config.py::_app_root()` — which is
`Path(sys.executable).parent` when frozen — would resolve into that throwaway
directory. One-dir keeps dispatch cheap and the app root stable.

*Unverified:* a one-file build was not attempted.

### Two PowerShell 5.1 traps (both hit while writing this)

1. **`$IsWindows` does not exist before PowerShell 6.** Under
   `Set-StrictMode -Version Latest` referencing it is a *hard error*, not
   `$false`, so an `if (-not $IsWindows)` guard aborts the build on the exact
   shell it was meant to protect. Use `$env:OS -ne "Windows_NT"`.
2. **5.1 wraps every stderr line from a native command in an ErrorRecord.**
   With `$ErrorActionPreference = "Stop"`, a *successful* tool that logs
   progress to stderr — npm and PyInstaller both do — kills the script.
   `Invoke-Native` in `build-windows.ps1` judges by `$LASTEXITCODE` instead.

## Signing

The exe is **unsigned**; SmartScreen shows "Windows protected your PC" on
first run. Authenticode signing is out of scope for v1. Nothing in the build
depends on it.

## WHPX

The app detects this itself (`bootstrap.windows_accel_status()`), but to
enable it by hand, in an **administrator** PowerShell:

```powershell
DISM /Online /Enable-Feature /FeatureName:HypervisorPlatform /All
```

then **reboot**.

> **Do not detect WHPX with DISM or `Get-WindowsOptionalFeature`.** Both
> require elevation — from a normal user session `Get-WindowsOptionalFeature`
> raises `COMException` (verified). A detector built on them reports "WHPX
> disabled" on a machine where WHPX demonstrably works. The app instead asks
> QEMU, which is the actual consumer: it starts a tiny VM paused (`-S`) and
> treats "still alive after the timeout" as proof the accelerator came up.
> The result is tri-state — `True` / `False` / `None` (unknown, e.g. QEMU not
> installed yet) — and only an explicit `False` is shown to the user.

On the test box WHPX was **already enabled** and
`windows_accel_status()` correctly returned `{'whpx_ok': True}`.

## Known environmental test failures on Windows

These fail on a Windows dev box for reasons unrelated to the code, and were
confirmed failing identically before any Slice C change:

- `omni-executor`: `test_resume_after_interruption_uses_range` (the Range
  test harness) — fails identically on `HEAD`'s `bootstrap.py`.
- `omnidroid`: anything importing `PIL` (not installed), the arm
  `qemu_command_arm` tests (`edk2-aarch64-code.fd` UEFI firmware is not
  present in a Windows QEMU install), POSIX file-mode assertions
  (`0o666 != 0600`), and selenium-dependent account tests.
- `omni-backend`: `app.harness`, `downloads`, `keys` — each hangs ~31 s on a
  MongoDB connect timeout; there is no DB on the dev box. Also `npm test`'s
  quoted glob (`'backend/tests/**/*.test.js'`) does not expand on Windows and
  silently runs **zero** tests — use `node --test backend/tests/*.test.js`.
