# Windows end-to-end: what is verified, and what is left

Run on a real Windows 11 / amd64 host (i7-13700F) on **2026-08-13**. This is
the honest close-out of Slice C: every line is marked with what was actually
observed, not what should happen.

## Status at a glance

| # | Step | Status |
|---|---|---|
| 1 | Download the arch-appropriate runtime (`?os=win` → x86) | ✅ code + tests; ⚠️ not run against the live VPS |
| 2 | Auto-install / locate QEMU | ✅ URL verified live; ⚠️ silent install not exercised |
| 3 | Detect WHPX and guide the user | ✅ verified (returned `whpx_ok: True`) |
| 4 | Configure the engine for x86 | ✅ verified (`doctor` → `ready: true`) |
| 5 | Bake the x86 arceus offset | ✅ **verified** — 747 MB overlay |
| 6 | Boot the x86 base under WHPX | ✅ **verified** — 0.5–2.0 min |
| 7 | Run the arm64-only arceus on x86 | ✅ **verified** — libndk bridge |
| 8 | Build `omni-exec.exe` | ✅ **verified** — engine dispatch smoke-tested |
| 9 | Inject the Roblox session / play | ❌ **BLOCKED** — see below |
| 10 | Publish the offset to the VPS | ⛔ not done — needs credentials |

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

## 10 — publishing the offset (not done)

`offset-arceus-x86` is already registered in `omni-backend/dist/registry.json`
with the **real** hash of the artifact built here:

```
bytes  783745024
sha256 814a1029ba67ca4fb28d3afc1b9c331ebbaf430604169ba485f8fffe652b19a6
dest   images/x86
dest_name base_x86_data_offset_arceusremote.qcow2
```

Remaining (needs VPS root credentials, which are not on this box —
`omni-backend/.env*.local` is absent here):

```bash
scp base_x86_data_offset_arceusremote.qcow2 \
    root@72.62.59.232:/root/omni-backend/dist/blobs/offset-arceus-x86.qcow2
scp dist/registry.json root@72.62.59.232:/root/omni-backend/dist/registry.json
ssh root@72.62.59.232 'pm2 restart omni-backend'
curl -s 'http://72.62.59.232/omni/dist/manifest?os=win' | jq '.artifacts[].name'
curl -s 'http://72.62.59.232/omni/dist/health' | jq
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

What is **unverified** about this path: it was never run against the live
VPS from a clean `%LOCALAPPDATA%\OmniExec`, and QEMU's silent install
(`<installer> /S /D=<dir>`) was never exercised — this box already had QEMU
11.0.50. Both are covered by unit tests with fixtures, which is not the same
as having done it.

## Gotchas worth keeping

- **Closed loopback ports time out instead of refusing on this host.** That
  made `allocate_ports()` spin forever and `omni start` hang with zero
  output. Fixed (bind test + bounded loop) — see the `runtime.py` commit. Any
  Windows box with a similar endpoint-security filter would have hit it.
- **A stale flat-layout image set breaks base selection.**
  `C:\Users\berat\OmniImages` still holds root-level `base_arm*.qcow2` files
  (Syncthing), which `autoregister_bases()` turns into a phantom `arm` base
  that then wins selection and fails every start. The bake used a dedicated
  `C:\Users\berat\OmniExecImages` holding only `x86/`.
- The engine writes back to whatever `OMNIDROID_CONFIG_PATH` points at
  (auto-registration, offset registration) — expect the file to change.
