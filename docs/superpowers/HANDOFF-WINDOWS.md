# Omni Executor — Windows (Slice C) handoff

Written 2026-08-13 at the end of the Windows session. Everything below was
**run on this machine**; where something is unverified it says so.

Machine: Windows 11 Pro, i7-13700F (16 logical), 32 GB, Kingston SNV3S1000G
NVMe (932 GB, ~31 GB free). Python 3.14.6, Node 24.11, PyInstaller 6.22.

---

## TL;DR — where things stand

Slice C is **functionally done**. A Windows machine can self-install the x86
runtime, boot the Bliss guest under WHPX in **~1 minute**, and run the
arm64-only arceus APK through `libndk_translation`.

**The one thing that does not work is auto-login/join**, and it is an IMAGE
problem, not a code problem — see "The remaining blocker".

| Step | State |
|---|---|
| Backend serves `?os=win` (base + offset + qemu) | ✅ live on the VPS |
| First-boot download + engine config | ✅ verified against the live VPS |
| QEMU auto-install on a machine without QEMU | ✅ verified (1m10s) |
| Bake an x86 arceus offset | ✅ done, uploaded, registered |
| Boot x86 under WHPX | ✅ **0.9 min** |
| arceus runs on x86 (libndk bridge) | ✅ verified |
| `omni-exec.exe` builds + engine dispatch | ✅ verified |
| Account login (browser/selenium) | ✅ verified in the frozen exe |
| Viewer button | ✅ verified, works during boot |
| **Auto-login / join Roblox** | ❌ **blocked — stale kiosk in the x86 base** |
| Warm boot (fast restore) | ❌ impossible under WHPX (see below) |

---

## Do this first

**Deploy the latest build.** The running app is from 21:04 and does NOT have
the 6.5x boot speed-up. The app was open so I could not replace it:

```powershell
# close Omni Executor first, then:
Remove-Item "C:\Users\berat\Desktop\Omni Executor\_internal" -Recurse -Force
Remove-Item "C:\Users\berat\Desktop\Omni Executor\omni-exec.exe" -Force
Copy-Item "C:\Users\berat\Desktop\Omni Apps\omni-executor\out3\omni-exec\*" `
          "C:\Users\berat\Desktop\Omni Executor" -Recurse -Force
```

Then tidy the stale build dirs (`dist`, `out`, `out2` — `out3` is current).

---

## Layout

| What | Where |
|---|---|
| Repos (must be siblings) | `C:\Users\berat\Desktop\Omni Apps\{omnidroid, omni-executor, omni-backend}` |
| The app the user runs | `C:\Users\berat\Desktop\Omni Executor\` |
| Latest build | `omni-executor\out3\omni-exec\` |
| Product runtime (images, accounts, paths.json) | `%LOCALAPPDATA%\OmniExec\` |
| Scratch configs for CLI work | `Omni Apps\_work\*.json` |
| VPS | `72.62.59.232`, root password in a `# VPS` comment in `omni-backend\.env.development.local` |

`omni-executor\.venv` has pytest, pyinstaller, selenium, pillow.
omnidroid has no venv — run its tests with the executor's interpreter.

### Running the engine by hand

```bash
export OMNIDROID_CONFIG_PATH="C:/Users/berat/AppData/Local/OmniExec/paths.json"
export OMNI_DATA_DIR="C:/Users/berat/AppData/Local/OmniExec"
export OMNI_IMAGES_DIR="C:/Users/berat/AppData/Local/OmniExec/images"
export OMNIDROID_SELF_ARGV="--omnidroid"          # REQUIRED, see gotchas
omni-executor/out3/omni-exec/omni-exec.exe --omnidroid doctor --json
omni-executor/out3/omni-exec/omni-exec.exe --omnidroid start admn1b12farm3
```

Build: `.\build-windows.ps1 -SkipFrontend -DistPath out4`

---

## Git state — 23 commits, NONE PUSHED

| Repo | Branch | Commits |
|---|---|---|
| omnidroid | `slice-c-x86-offsets` | 11 |
| omni-executor | `slice-c-windows-exe` | 11 |
| omni-backend | `slice-c-win-artifacts` | 1 |

**omnidroid has ~110 uncommitted tracked files** — that is the pre-existing
WIP the bundle shipped with. It is NOT mine. Never `git checkout`/`stash`/
`reset`/`add -A` in that repo; commit only named files.
(omni-backend also has an incidental `package-lock.json` from `npm install`.)

---

## The remaining blocker: auto-login / join

`omni start` completes every stage but the last:

```
session NOT applied: no_kiosk_reply
Broadcasting: Intent { act=com.omni.kiosk.SET_SESSION ... } → result=0
```

Diagnosed on a live instance:

```
pm list packages                → com.omni.kiosk IS installed
dumpsys package com.omni.kiosk  → Receiver Resolver Table:
                                     only .OmniDeviceAdminReceiver
```

**There is no `SessionReceiver`.** The x86 base carries an older kiosk build
that predates the `SET_SESSION` cookie-injection contract the engine speaks,
so the cookie is never delivered and Roblox never logs in. arceus itself runs
fine when launched by hand (verified: `monkey -p com.roblox.client`, pid stays
alive), so this is purely the automated handoff.

**Fix:** rebuild `com.omni.kiosk` into the x86 base — the same kiosk the arm
base already has — via `omnidroid update-kiosk` / `rebuild-base`, then re-bake
the offset on top and re-upload. That is the whole remaining path to
auto-login + join.

Related: the x86 base is **unrooted** (`no su`), so DenyList/prop hiding,
cpuset pinning and the Roblox-settings CPU tune are all skipped. Not a
blocker, just the documented unrooted behaviour.

---

## Gotchas that cost real time — read before debugging

**Environment / platform**

1. **Closed loopback ports TIME OUT here instead of refusing.** This hung
   `omni start` forever with zero output. Fixed (bind test + bounded loop),
   but the host behaviour remains — suspect it for any new port probe.
2. **`Get-WindowsOptionalFeature`/DISM need elevation.** Unelevated they raise
   COMException, so they cannot detect WHPX from the app. Ask QEMU instead.
3. **WHPX is Hyper-V.** VBS is running and Hyper-V is installed (WSL2/Docker
   need it). You cannot "turn Hyper-V off to go faster" — WHPX *is* that API.
4. **`chrome.exe --version` prints NOTHING on Windows** (GUI subsystem
   binary). Read the version-named folder beside the exe instead.

**PowerShell 5.1** (the shell that ships with Windows)

5. `$IsWindows` does not exist before PS6; under StrictMode referencing it is
   a hard error, not `$false`. Use `$env:OS`.
6. 5.1 wraps a native command's **stderr** in ErrorRecords, so with
   `$ErrorActionPreference='Stop'` a *successful* tool that logs progress to
   stderr (npm, PyInstaller) aborts your script. Judge by `$LASTEXITCODE`.
7. Variable names are case-INSENSITIVE: a local `$zip` clobbers a `[switch]$Zip`
   parameter.

**Builds**

8. **PyInstaller deletes and recreates its output dir**, so ANY handle on it
   fails the build with WinError 5/32 — an Explorer window is enough, and so
   is a shell whose cwd is inside it (this bit me repeatedly; my own Bash cwd
   was the culprit). Use `-DistPath <fresh>` rather than hunting the lock.
9. **Lazy imports are invisible to PyInstaller.** selenium, tkinter and PIL
   are all imported *inside* functions, so each had to be a hiddenimport —
   and each absence only showed up as a runtime failure in the frozen app,
   never at build time.

**The engine embedded in the GUI**

10. **`OMNIDROID_SELF_ARGV` is required.** The engine re-invokes the frozen
    binary for detached children (viewer, autocap) as `[sys.executable,
    "<subcommand>"]`. omni-exec.exe's entry point is the GUI, which routes to
    the engine only on `--omnidroid` — without the prefix those children
    launch **another copy of the app**. `configure_engine` sets it.
11. **A dead output pipe used to kill a boot.** The GUI owns the engine's
    stdout/stderr; when it stops reading, the next `print` raised
    `OSError(EINVAL)` → PyInstaller crash dialog. Output is now best-effort.
12. **adb reports an offline endpoint on STDERR with empty stdout.** Checking
    stdout alone made the offline-recovery dead code and launches burned their
    full timeout against guests that were already up. Use `adb_state()`.
13. A stale flat-layout image set in `C:\Users\berat\OmniImages` used to create
    a phantom `arm` base that hijacked base selection. **That folder was
    deleted** (with the user's approval). Syncthing still lists it, unpaused,
    against `Adils-Mac-mini.local` — launching Syncthing could pull 8.1 GB back.

---

## Performance — measured, and counter-intuitive

Boot was ~6 minutes. It is now **0.9 min**. The full matrix (same image, same
host, time to `boot_completed`):

| RAM / vCPU | `-cpu` | result |
|---|---|---|
| 8192 / 8 | qemu64 | 5.9 min |
| 8192 / 8 | host | did not complete (6 min) |
| 2048 / 2 | host | did not complete (15 min) |
| 4096 / 4 | host | did not complete (15 min) |
| **4096 / 4** | **qemu64** | **0.9 min** |

- **Autoscaling to host capacity made it 6.5x SLOWER.** WHPX's per-vCPU exit
  cost does not scale like KVM's. Capped to 4 vCPU / 4096 MB on Windows only
  (`WHPX_SMP_CEIL`, `WHPX_MEM_CEIL_MB`); KVM/HVF keep the old ceilings.
- **`-cpu host` is WORSE**, despite the guest binary-translating arm64 (the
  workload you would swear wants real SSE4/AVX). Three runs, zero
  completions. Default stays `qemu64`; `host` remains available via config
  `qemu.cpu`. **Do not re-enable it on reasoning alone — re-measure.**
- Things that were NOT the cause, each measured: the SSD (1.6 GB/s writes),
  vCPU starvation, and the CPU model.
- If a boot is slow, check whether the guest is actually **idle** (low CPU in
  Task Manager). Idle means it is waiting/undetected, not computing.

### Warm boot cannot work on Windows

The x86 warm-restore path is implemented and correct, but QEMU refuses the
snapshot under WHPX:

```
warm bake failed (State blocked due to non-migratable CPUID feature support,
dirty memory tracking support, and XSAVE/XRSTOR support)
```

That is QEMU's migration blocker for WHPX (no dirty-memory tracking, no
migratable CPU state) — `migrate → file:` is how an entry is captured, so no
warm entry can ever be produced here. It degrades cleanly (logged, launch
unaffected, no partial entry) and goes live under KVM. Separately, the cache
needs **10 GiB free** (`FREE_RESERVE_BYTES`) or it silently never engages.

---

## Test baseline — expected failures

Run omnidroid's tests with a valid config or collection sys.exits:

```bash
export OMNIDROID_CONFIG_PATH="C:/Users/berat/Desktop/Omni Apps/_work/bake-paths.json"
omni-executor/.venv/Scripts/python.exe -m pytest tests/ -q \
    --ignore=tests/test_keyframe_thresholds.py
```

- **omnidroid: 21 failed, 751 passed.** All 21 are environmental and
  pre-existing: missing arm UEFI firmware (`edk2-aarch64-code.fd`), missing
  PIL in *that* interpreter, POSIX file-mode assertions (`0o666 != 0600`),
  selenium/network. The baseline was 25 before this session's work.
- **omni-executor: 68 passed.** `test_resume_after_interruption_uses_range`
  is FLAKY (passes and fails on identical code, including on `HEAD`).
- **omni-backend:** `node --test backend/tests/*.test.js` with dummy
  `DB_URI`/`JWT_SECRET`/`ARCJET_KEY` → 40 pass; `app.harness`, `downloads`,
  `keys` fail on a ~31 s Mongo timeout (no DB here). `npm test`'s quoted glob
  does not expand on Windows and silently runs **zero** tests.

---

## Live VPS state (deployed and verified)

```
GET /omni/dist/manifest?os=win
  base-x86           images/x86  tar        3028269056
  offset-arceus-x86  images/x86  base_x86_data_offset_arceusremote.qcow2  783745024
                     sha256 814a1029ba67ca4fb28d3afc1b9c331ebbaf430604169ba485f8fffe652b19a6
  qemu-win           qemu        302 → weilnetz w64 installer
GET ?os=mac  → unchanged (base-arm, offset-arceus-arm)
```

`qemu-win` is a **redirect, not a stored blob** — that is deliberate, so an
expired upstream installer is a one-line registry edit on the server instead
of a client release (the first URL pinned was already 404). Pointer artifacts
have `sha256: null`; the client skips them (`plan_downloads`) because they are
not downloadable payloads. Getting that wrong caused a first-boot failure:
`qemu-win: sha256 mismatch after 3 attempts`.

Deploy flow: `scp` to `/root/omni-backend/dist/blobs/`, copy `registry.json`,
`pm2 restart omni-backend`. **Diff the live registry before overwriting** —
the VPS deploys by file-copy, so there is no merge.

---

## Suggested next steps

1. **Deploy `out3`** (above) — the user is running a build without the speed-up.
2. **Rebuild the x86 kiosk with `SessionReceiver`**, re-bake the offset,
   re-upload → unlocks auto-login and join. This is the last real gap.
3. Consider rooting the x86 base to recover the skipped optimisations
   (hiding, cpuset, the ~2x Roblox-settings CPU saving).
4. Push the three branches when the user is ready — nothing is pushed.
5. Optional: Authenticode-sign the exe (SmartScreen warns on first run).
