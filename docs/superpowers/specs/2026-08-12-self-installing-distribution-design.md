# Self-installing Omni Executor distribution — design

**Date:** 2026-08-12
**Status:** approved (architecture); slices A + B specced for build now, C sketched
**Author:** berat + Claude

## Goal

A person downloads one small file to a fresh machine, runs it, and after a first-boot
"setting up" step they have the full working Omni Executor — GUI + omnidroid engine +
QEMU + Android base + the arceus offset — with no manual steps. The same file, on later
launches, simply *is* the Omni Executor.

- **Windows:** a small `omni-exec.exe`.
- **macOS:** a `.dmg` containing `Omni Executor.app`.
- **Linux:** deferred.

## Key decisions (settled during brainstorming)

1. **Windows runtime = omnidroid on the x86-bliss base** (WHPX-accelerated), i.e. the unified
   product — *not* MuMu. Prerequisite for the Windows slice (step 0): confirm/enable ARM
   translation on x86-bliss so the arm64 arceus APK runs, then bake an **x86** arceus offset.
   macOS uses omnidroid on the arm-uefi base (already proven in daily use).
2. **Blob hosting = the VPS (`72.62.59.232`) now, behind a *named-blob API***. The installer
   never learns real URLs; it asks the backend for an artifact by name. The backend streams
   from VPS disk today and can `302`-redirect to a CDN later — **client unchanged**.
3. **First slice = A (backend API) + B (macOS .dmg)** — both fully buildable and verifiable on
   the current Mac. Windows (C) follows once testable and after the step-0 spike.
4. **Bundle the app, download the runtime.** The ~25 MB app layer (GUI + engine) ships *inside*
   the exe/app (a machine with no Python can't run a downloaded Python app). Only the multi-GB
   VM assets (QEMU, base, offset) are fetched on first boot.

## Scope

- **In scope now:** Slice A (backend distribution API + artifact upload), Slice B (self-installing
  macOS `.dmg`).
- **Later:** Slice C (Windows `.exe` + the ARM-translation/x86-offset spike), and the CDN switch
  (server-side only).
- **Explicitly not now:** Linux; auto-update of the *app binary* itself (runtime-asset updates ARE
  in scope — see Versioning); user accounts/licensing on the installer.

---

## Architecture

```
 fresh Mac
   │  download + open Omni Executor.dmg  →  drag to /Applications  →  launch
   ▼
 Omni Executor.app  (bundles: pywebview GUI + built React + omnidroid engine)
   │  first boot: runtime assets missing?
   ├── yes → BootstrapView (progress) ─┐
   │                                   │  GET /omni/dist/manifest?os=mac
   │                                   ▼
   │                          backend 72.62.59.232 ── manifest JSON (artifacts by name)
   │                                   │  for each artifact: GET /omni/dist/blob/<name>
   │                                   ▼  (Range/resumable; today streams VPS disk, later 302→CDN)
   │                          verify sha256 → place under ~/Library/Application Support/OmniExec
   │                                   │  write installed-manifest.json (versions)
   └── no  → straight to the normal Executor GUI (Accounts / Editor / …)
                                       │
                                       ▼
                          omnidroid engine (bundled) drives QEMU + base + offset
```

### Component boundaries (each independently testable)

| Unit | Responsibility | Interface | Depends on |
|---|---|---|---|
| **dist API** (omni-backend) | serve manifest + named blobs | `GET /omni/dist/manifest`, `GET /omni/dist/blob/<name>` | artifact registry + files on disk |
| **artifact registry** (omni-backend) | map name→{version,sha256,bytes,source} | JSON config `dist/registry.json` | files under `dist/blobs/` |
| **bootstrapper** (omni-executor) | fetch manifest, download+verify, place, record | `ensure_runtime()` → status/progress | dist API; filesystem |
| **BootstrapView** (frontend) | first-boot progress UI | reads `bootstrap-*` events, calls `api("bootstrap_*")` | pywebview bridge |
| **runtime layout** (omni-executor) | where assets live; point engine at them | `OMNIDROID_ENGINE`, images dir, qemu dir | omnidroid config contract |
| **packager** (omni-executor) | freeze app → .app → .dmg | `build-macos.sh` | PyInstaller, create-dmg |

---

## Slice A — Backend distribution API

Lives in `omni-backend` (`backend/src/omni-exec/` neighborhood), mounted at `/omni/dist`,
before the static catch-all (same pattern as the exec bridge).

### Endpoints

- `GET /omni/dist/manifest?os=mac|win&channel=stable`
  → `200 application/json`:
  ```json
  {
    "ok": true,
    "channel": "stable",
    "os": "mac",
    "app": { "version": "1.0.0" },
    "artifacts": [
      { "name": "qemu-mac-arm64", "version": "9.1.0", "bytes": 78123456,
        "sha256": "…", "url": "/omni/dist/blob/qemu-mac-arm64", "dest": "qemu",
        "unpack": "tar.gz" },
      { "name": "base-arm", "version": "lineage-23.2", "bytes": 3900000000,
        "sha256": "…", "url": "/omni/dist/blob/base-arm", "dest": "images/arm",
        "unpack": "tar" },
      { "name": "offset-arceus-arm", "version": "2.732.1043", "bytes": 378929152,
        "sha256": "…", "url": "/omni/dist/blob/offset-arceus-arm", "dest": "images/arm" }
    ]
  }
  ```
  The manifest is generated from `dist/registry.json` filtered by `os`/`channel`.

- `GET /omni/dist/blob/<name>`
  → streams the file for `<name>` from `dist/blobs/<file>` with **HTTP Range** support
  (`Accept-Ranges: bytes`, `206 Partial Content`) so downloads resume. Sends
  `Content-Length`, `ETag: "<sha256>"`, `X-Omni-SHA256`.
  **Future CDN switch:** if a registry entry has `redirect: "<cdn-url>"`, the handler returns
  `302 Location: <cdn-url>` instead of streaming — the only change needed to go to a CDN.

- `GET /omni/dist/health` → `{ok, blobs:[{name,present,bytes,sha256_ok}]}` for ops sanity.

### Artifact registry & storage

- `omni-backend/dist/registry.json` — hand-maintained (small): per artifact `{name, os, channel,
  version, file, sha256, bytes, dest, unpack?, redirect?}`. `sha256`/`bytes` computed at upload.
- `omni-backend/dist/blobs/<file>` — the actual blobs, **git-ignored** (multi-GB). Uploaded to the
  VPS out-of-band (scp/rsync), not committed.
- A helper script `omni-backend/scripts/dist-add.mjs <name> <file> --os --version` stamps
  sha256+bytes into the registry.

### macOS artifacts (first target)

The arm base is a **set**, not one file, so we tar it into one blob per logical artifact:
- `base-arm` → tar of `base_arm_system_rooted.qcow2` + `base_arm_data_rooted.qcow2` +
  `base_arm_efivars.fd` (the immutable base trio; ~3.9 GB).
- `offset-arceus-arm` → `base_arm_data_offset_arceusae.qcow2` (~361 MB), the proven arceus offset.
- `qemu-mac-arm64` → a **relocatable** `qemu-system-aarch64` + its dylibs + `edk2-aarch64-code.fd`,
  tar.gz. **Known-tricky (see Risks);** fallback is "require Homebrew qemu" for v1.

### Tests (A)

Node/supertest: manifest shape per-os; blob streams full + Range (206, correct bytes); ETag =
sha256; unknown name → 404; a registry entry with `redirect` yields 302; existing routes still
pass through. A tiny fixture blob (few KB) exercises the path without real GBs.

---

## Slice B — macOS self-installing `.dmg`

### Packaging

- Freeze `omni-executor` with **PyInstaller** (windowed) into `Omni Executor.app`, bundling:
  the pywebview GUI (`main.py`), the built `frontend/dist`, and the **omnidroid engine package**
  (vendored at build time). **Engine invocation:** the frozen binary supports an `--omnidroid
  <args…>` mode that dispatches to `omnidroid.cli:main`; omni-executor's `engine_prefix()` resolves
  to `[sys.executable, "--omnidroid"]` when frozen (via `OMNIDROID_ENGINE` set at first boot, or a
  frozen-app detection branch). This keeps the existing subprocess/`--json` engine contract intact
  — no in-process import path. Icon + `Info.plist`. Ship inside a `.dmg` (`create-dmg`) with an
  /Applications drag target.
- `build-macos.sh` orchestrates: `npm run build` (frontend) → PyInstaller → dmg. Documented,
  reproducible. (The stale `manager/omni.py` PyInstaller entry in omnidroid is *not* reused; the
  engine is invoked via its real entry `omnidroid.cli:main`.)
- Codesigning/notarization: out of scope for v1 (ad-hoc sign; user right-clicks→Open past
  Gatekeeper). Noted as a follow-up.

### First-boot bootstrap flow

1. On launch, `ensure_runtime()` reads `~/Library/Application Support/OmniExec/installed.json`.
2. If missing or a required artifact's `version`/`sha256` differs from the fetched manifest →
   show **BootstrapView** (a first-boot screen: overall %, current file, speed, cancel).
3. `GET /omni/dist/manifest?os=mac` → for each artifact: stream `GET /omni/dist/blob/<name>` to a
   temp file with resume, hashing as it goes; verify sha256; unpack per `unpack`; move into place
   under the data dir (`images/`, `qemu/`).
4. Write `installed.json` (per-artifact name→version→sha256). Point the engine at the data dir:
   set `OMNIDROID_ENGINE` (bundled) + the images dir + `qemu.dir`/`download_url` config so
   omnidroid finds the downloaded QEMU and base (writing omnidroid's `paths.json` in the data
   dir, overriding its stale defaults).
5. Hand off to the normal GUI. Subsequent launches: manifest version-check is quick; if nothing
   changed, straight to GUI.

### Runtime layout (macOS)

```
~/Library/Application Support/OmniExec/
   installed.json                 # what we've downloaded (name→version→sha256)
   paths.json                     # omnidroid config we generate (images_dir, qemu, default_offset)
   images/arm/base_arm_*.qcow2    # from base-arm
   images/arm/base_arm_data_offset_arceusae.qcow2
   qemu/qemu-system-aarch64 …     # from qemu-mac-arm64  (or system brew qemu fallback)
```

### Tests (B)

- **Bootstrapper unit/integration (headless, no GUI):** point it at a *local* dist API serving
  tiny fixture blobs; assert it downloads, resumes after a killed connection, rejects a
  corrupted blob (sha256 mismatch) and retries, and writes a correct `installed.json`.
- **End-to-end on this Mac (manual/scripted):** build the `.dmg`, launch, let it pull the REAL
  arm base + arceus offset from the VPS, then confirm the app can start the HezMi_ImYu instance,
  arceus loads the OMNI-EXEC UI, and the exec bridge runs a script — i.e. the full stack the
  installer produced actually works.

---

## Data flow, errors, versioning

- **Resumable + verified:** every blob download supports Range resume and is sha256-verified
  before it's placed; a mismatch deletes the temp and retries (bounded), surfacing a clear error.
- **Disk space:** pre-check free space ≥ sum(artifact bytes)×1.1 before starting; fail early with
  a readable message.
- **Partial install recovery:** artifacts are staged in a temp dir and atomically moved; a crash
  mid-download just re-runs the missing ones next launch (idempotent by name+sha256).
- **Versioning / self-heal:** the manifest is the source of truth. On launch we compare installed
  vs manifest; a newer `version`/changed `sha256` triggers a re-download of *just that artifact*
  (e.g. a new arceus offset ships by bumping `offset-arceus-arm` — no new installer). App-binary
  self-update is out of scope for v1.
- **Offline / server down:** if assets are already installed, launch normally without contacting
  the server (manifest check is best-effort, short timeout); only the *first* boot hard-requires
  the server.

## Risks / open items

1. **(Windows, later) x86-bliss ARM translation** — the arm64 arceus must run on Bliss. Step-0
   spike before slice C; if absent, add libndk_translation/houdini to the base or offset.
2. **Portable QEMU on macOS** — a relocatable `qemu-system-aarch64` bundle (dylib rpaths + edk2
   firmware) is fiddly. v1 fallback: detect/require Homebrew qemu and only download base+offset;
   promote to a bundled portable qemu once built. Decide in the plan.
   **Does NOT carry to Windows:** the official NSIS QEMU installer is self-contained and omnidroid
   already downloads + silent-installs it (`ensure_qemu()`); on Windows QEMU is just another
   named-blob URL — the easy case.
3. **Code-signing** — both OSes warn on unsigned downloaded apps; both clear it with a paid cert,
   but as *separate* mechanisms. macOS: Gatekeeper → Apple Developer ID + notarization (v1 ships
   ad-hoc-signed, right-click→Open). Windows (slice C): SmartScreen → Authenticode cert (EV clears
   it instantly; OV earns reputation). Independent follow-ups on each platform.
4. **VPS bandwidth** — multi-GB × users on one box; acceptable for now by explicit choice, and the
   named-blob `302` indirection is the pre-built escape hatch to a CDN.
5. **(Windows, later) WHPX host enablement** — accelerated x86 needs BIOS virtualization + the
   "Windows Hypervisor Platform" feature (admin + reboot). Fresh boxes may have it off, so the
   Windows first-boot must detect it and enable (`DISM /Online /Enable-Feature
   /FeatureName:HypervisorPlatform /All`) or guide the user. No macOS analog (HVF is always on).

## Build order

A (backend API + upload mac artifacts) → B (bootstrapper → package `.app`/`.dmg` → e2e on this
Mac) → later: C (Windows) + CDN switch. Each slice gets its own implementation plan.
