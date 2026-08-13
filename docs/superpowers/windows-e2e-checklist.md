# Windows end-to-end: what is verified, and what is left

Run on a real Windows 11 / amd64 host (i7-13700F) on **2026-08-13**. This is
the honest close-out of Slice C: every line is marked with what was actually
observed, not what should happen.

## Status at a glance

| # | Step | Status |
|---|---|---|
| 1 | Download the arch-appropriate runtime (`?os=win` → x86) | ✅ **verified against the live VPS** |
| 2 | Auto-install / locate QEMU | ✅ URL + redirect verified; ⚠️ silent install not exercised |
| 3 | Detect WHPX and guide the user | ✅ verified (returned `whpx_ok: True`) |
| 4 | Configure the engine for x86 | ✅ verified (`doctor` → `ready: true`) |
| 5 | Bake the x86 arceus offset | ✅ **verified** — 747 MB overlay |
| 6 | Boot the x86 base under WHPX | ✅ **verified** — 0.5–2.0 min |
| 7 | Run the arm64-only arceus on x86 | ✅ **verified** — libndk bridge |
| 8 | Build `omni-exec.exe` | ✅ **verified** — engine dispatch smoke-tested |
| 9 | Inject the Roblox session / play | ❌ **BLOCKED** — see below |
| 10 | Publish the offset to the VPS | ✅ **done + verified live** |

## Verified in detail

**5 — the bake.** `omnidroid offset create arceusremote --apk
arceus-STATIC-REMOTE-v2.apk --default` on the x86 base produced
`x86/base_x86_data_offset_arceusremote.qcow2`: a **747 MB thin COW overlay**
of `x86/data-template-8g.qcow2`, rebased to the bare backing filename so
`images_dir` stays relocatable. Guest reported `OMNI_GAME_BAKE_OK` — the
feared signature clash with the base's pre-installed `com.roblox.client`
(x86 base v4 ships it as a system app, unlike the clean arm base) did **not**
occur; `pm install -r -d` replaced it.

**6/7 — it actually runs.** Booting that offset:

```
ro.dalvik.vm.native.bridge = libndk_translation.so
ro.product.cpu.abilist     = x86_64,arm64-v8a,x86,armeabi-v7a,armeabi
pm path com.roblox.client  = /data/app/~~njyIZ…/com.roblox.client-…/base.apk
versionName                = 2.732.1043
t+10s / t+20s / t+30s      → pid 3075 (alive)
```

The APK ships **only** `lib/arm64-v8a`, so this is the whole question Slice C
turned on: the base's baked ARM-translation bridge survives the bake, the
package resolves out of `/data` (the offset, not the system image), and the
process does not die on a missing arm64 loader. `logcat` showed no
`UnsatisfiedLink` and no FATAL.

The product path reaches the same place: `omni start <user>` cold-booted in
**2.0 min** and the engine's own check printed
`native bridge: libndk_translation.so OK`, having selected
`offset: arceusremote` and `data_image:
x86/base_x86_data_offset_arceusremote.qcow2`.

## 9 — the remaining blocker: the x86 base ships an OUTDATED kiosk

`omni start` completes every stage except the last:

```
session NOT applied: no_kiosk_reply
Broadcasting: Intent { act=com.omni.kiosk.SET_SESSION … } → result=0
```

Diagnosed on the live instance:

```
pm list packages       → com.omni.kiosk    (installed)
dumpsys package com.omni.kiosk → Receiver Resolver Table:
                             only  .OmniDeviceAdminReceiver
```

**There is no `SessionReceiver`.** The x86 base carries an older kiosk build
that predates the `SET_SESSION` cookie-injection contract the engine now
speaks, so the cookie is never delivered and Roblox never logs in. Roblox
itself launches fine when started by hand (step 7), so this is **an image
problem, not a code problem**.

Fix (a base rebuild, not a Slice C change): rebuild `com.omni.kiosk` for the
x86 base — the same kiosk the arm base got — via `omnidroid update-kiosk` /
`rebuild-base`, then re-bake the offset on top. Until then a Windows install
boots to a working, arceus-carrying instance that is **not logged in**.

The instance is also **unrooted** (`base is not rooted (no su)`), so
DenyList/prop hiding, cpuset pinning and the Roblox-settings CPU tune are all
skipped. Not a blocker; it is the documented unrooted-deployment behaviour.

## 10 — publishing the offset (DONE, 2026-08-13)

Uploaded to the VPS and serving live. The upload took **11m20s** (~1.1 MB/s
to that host) and the blob was verified on the far end **byte-for-byte and by
sha256** before anything was registered:

```
bytes  783745024                                                    (match)
sha256 814a1029ba67ca4fb28d3afc1b9c331ebbaf430604169ba485f8fffe652b19a6  (match)
```

The live `registry.json` was diffed against the local one BEFORE overwriting
(the VPS deploys by file-copy, so a server-side-only edit would have been
destroyed): it was a strict subset, and a timestamped `.bak` was taken anyway.

Live verification after `pm2 restart omni-backend`:

```
GET /omni/dist/manifest?os=win
  base-x86           dest=images/x86  bytes=3028269056
  offset-arceus-x86  dest=images/x86  dest_name=base_x86_data_offset_arceusremote.qcow2
                                      bytes=783745024
  qemu-win           dest=qemu        (302)

GET /omni/dist/manifest?os=mac   → base-arm, offset-arceus-arm   (unchanged)
GET /omni/dist/health            → all four blobs present, every bytes==expected
GET /omni/dist/blob/qemu-win     → 302 https://qemu.weilnetz.de/w64/qemu-w64-setup-20260811.exe
GET /omni/dist/blob/offset-arceus-x86  (Range: bytes=0-15)
  → 206, Content-Range: bytes 0-15/783745024
  → 51 46 49 fb …  = "QFI\xfb", the qcow2 magic — a real qcow2 from byte 0
```

`dest_name` must stay exactly `base_x86_data_offset_arceusremote.qcow2`: the
overlay's backing reference is the **bare** name `data-template-8g.qcow2`,
resolved relative to the overlay's own directory, so the file has to land in
`images/x86` beside the template or it will not open.

## Fresh-machine install flow (steps 1–4, unrun end-to-end)

1. `omni-exec.exe` first launch → `BootstrapView`.
2. `bootstrap.current_os()` → `"win"` → `GET /omni/dist/manifest?os=win`.
3. Downloads `base-x86` (tar → `images/x86`), `offset-arceus-x86`
   (bare qcow2 → `images/x86/base_x86_data_offset_arceusremote.qcow2`), and
   follows the `qemu-win` 302 to the NSIS installer.
4. `configure_engine()` writes `paths.json` with an `x86-bliss` base
   (`disk`/`kernel`/`initrd` under `x86/`, **`src`**, top-level
   `data_template`, offsets scanned from `base_x86_data_offset_*.qcow2`) and
   sets `qemu.download_url` so `ensure_qemu()` can self-install.
5. WHPX panel appears only if the probe returns an explicit `False`.

**Steps 1–4 have now been run for real** against the live VPS into
`%LOCALAPPDATA%\OmniExec` (3.6 GB fetched; the offset's sha256 on disk
matches the registry exactly), and `omni-exec.exe --omnidroid doctor --json`
against that runtime reports `ready: true`, `default_offset: arceusremote`,
`present: true`, `missing_files: []`.

That first real run also found two bugs the fixture tests could not:

1. **`qemu-win: sha256 mismatch after 3 attempts`** — a first boot downloaded
   everything and then died. `qemu-win` is a 302 POINTER, so the manifest
   reports `sha256: null`; the client planned it like any other artifact and
   `download_blob` compared the bytes against `None`, which never matches.
   `plan_downloads` now skips artifacts with no sha256 (they are fetched by
   whoever consumes them — `ensure_qemu()` via `qemu.download_url`), and
   `download_blob` refuses an unverifiable artifact with a message that
   blames the manifest instead of the network.
2. **A tail failure threw away the whole install.** `installed.json` was
   written only after the entire plan succeeded, so failing on the LAST
   artifact discarded the receipt for the 3.6 GB already on disk and the next
   launch re-downloaded all of it. It is now written after every artifact.

`qemu.download_url` also now points at the dist API's `qemu-win` blob
(`{base}/omni/dist/blob/qemu-win`) rather than the upstream URL, so a rotted
installer is a registry edit on the server, not a client release.
`ensure_qemu()` fetches with urllib, which follows the 302.

Still **unverified**: QEMU's silent install (`<installer> /S /D=<dir>`) — this
box already had QEMU 11.0.50, so that branch never executed.

## Warm boot on x86 (launch speed)

The warm-restore cache used to refuse x86 outright ("warm-restore cache is
arm-only on this host ... cold-booting"), so a Windows host paid a full cold
boot -- minutes -- for every single launch. The only genuinely arm-specific
part was `efivars.fd` (UEFI pflash) being welded into the cache's
required-file contract; the x86 restore path in `qemu_command` already
existed. efivars is now optional and recorded per entry
(`warmcache.required_files()` keys off `meta["arch"]`), so x86 takes the same
lookup/bake route as arm. Entries with no recorded arch are still treated as
arm, so pre-existing caches validate unchanged.

**It needs disk.** A warm entry stores the guest's RAM, and
`warmcache.FREE_RESERVE_BYTES` refuses to bake unless **10 GiB remains free
afterwards**. `playable` sizes itself to the host and was 8192 MB here, so a
usable cache wants ~18 GB free. Below that `has_room()` returns False, no
entry is written, and every launch quietly cold-boots with no error.

### ...but WHPX cannot be snapshotted at all (verified 2026-08-13)

With 31 GB free, a real cold boot on this host reached the bake and QEMU
refused it:

    warm bake failed (State blocked due to non-migratable CPUID feature
    support, dirty memory tracking support, and XSAVE/XRSTOR support);
    this launch is unaffected

That is QEMU's **migration blocker for the WHPX accelerator**: WHPX exposes
no dirty-memory tracking and no migratable CPU state, so `migrate ->
file:` -- which is exactly how a warm entry is captured -- cannot run. This
is a property of the accelerator, not of this code: nothing in omnidroid can
work around it while the guest runs under `-accel whpx`.

So on Windows:

- The x86 warm-cache plumbing is correct and now ATTEMPTS a bake (it used to
  refuse by name), and it degrades exactly as designed -- the failure is
  caught, logged, and `this launch is unaffected`. A `warm/` directory is
  created and left EMPTY; no partial entry is published.
- But **no warm entry can ever be produced under WHPX**, so launches stay
  cold boots (measured 0.5 - 4.3 min, varying with disk contention).
- The work is not wasted: it is the same code path arm already uses, so it
  becomes live the moment it runs under an accelerator that supports
  migration (KVM on a Linux host).

If launch latency matters on Windows, the realistic levers are NOT the warm
cache: keep an instance running rather than stopping it, and/or lower the
mode's memory so there is less guest RAM to allocate and touch at boot.

## Gotchas worth keeping

- **Closed loopback ports time out instead of refusing on this host.** That
  made `allocate_ports()` spin forever and `omni start` hang with zero
  output. Fixed (bind test + bounded loop) — see the `runtime.py` commit. Any
  Windows box with a similar endpoint-security filter would have hit it.
- **A stale flat-layout image set breaks base selection.**
  `C:\Users\berat\OmniImages` still holds root-level `base_arm*.qcow2` files,
  which `autoregister_bases()` turns into a phantom `arm` base that then wins
  selection and fails every start. The bake used a dedicated
  `C:\Users\berat\OmniExecImages` holding only `x86/`. Those files are
  STATIC leftovers — Syncthing is installed (portable, on the Desktop) and
  has `OmniImages` configured unpaused against `Adils-Mac-mini.local`, but it
  does not run: no process, no service, no scheduled task, no autostart
  entry. It only syncs if launched by hand.
- The engine writes back to whatever `OMNIDROID_CONFIG_PATH` points at
  (auto-registration, offset registration) — expect the file to change.
