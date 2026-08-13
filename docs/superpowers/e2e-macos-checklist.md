# Slice B — macOS self-install end-to-end checklist

Reproducible acceptance for the self-installing `.dmg`. Two halves: a **headless**
install/engine proof (scripted, `scripts/e2e-macos.sh`) and a **GUI** proof (launch
the frozen app, confirm it boots straight to the executor on the downloaded runtime).

Last run: **2026-08-13**, against live prod `http://72.62.59.232`, on Apple Silicon macOS.

---

## Prerequisites

- `qemu-system-aarch64` on PATH (Homebrew): `brew install qemu android-platform-tools`.
  (v1 uses system QEMU; a bundled portable QEMU is a later slice.)
- The backend deployed with the `dest_name` manifest field (Task 1) — verify:
  `curl -s 'http://72.62.59.232/omni/dist/manifest?os=mac' | python3 -m json.tool`
  → `offset-arceus-arm` must carry `"dest_name": "base_arm_data_offset_arceusremote.qcow2"`.

## Half 1 — headless install + engine (scripted)

Run: `scripts/e2e-macos.sh /tmp/omniexec-e2e`

### Observed results (2026-08-13)

**STEP 1 — real pull (resumable + sha256-verified):**
- `base-arm` 3703 MB and `offset-arceus-arm` 348 MB downloaded in **368 s** (~11 MB/s).
- `installed.json` recorded:
  - `base-arm` v`lineage-23.2-gameless` sha `9aa8587c357855fd…` (3 882 966 016 B) — matches registry.
  - `offset-arceus-arm` v`2.732.1043` sha `5b599ea36cfc00d4…` (364 576 768 B) — matches registry.
- Placed under `images/arm/`: `base_arm_system_rooted.qcow2` (2635 MB), `base_arm_data_rooted.qcow2`
  (1067 MB), `base_arm_efivars.fd` (1 MB), and the offset as
  **`base_arm_data_offset_arceusremote.qcow2`** (348 MB) — the exact filename omnidroid expects
  (proves the `dest_name` contract).

**STEP 2 — `configure_engine`:** `qemu_ok: True`; `OMNIDROID_CONFIG_PATH` set to `<rt>/paths.json`;
`paths.json` written with the arm base (files under `arm/`), the offset, `current_base: "arm"`,
and the qemu block.

**STEP 3 — engine sees the assets:**
```
$ omnidroid bases --json
{"current_base":"arm","bases":[{"tag":"arm","arch":"arm","type":"arm-uefi","rooted":true,
  "offsets":["arceusremote"],"default_offset":"arceusremote"}],"ok":true}   # exit 0
$ omnidroid offset list --json
{"ok":true,"base":"arm","default":"arceusremote",
  "offsets":[{"name":"arceusremote","present":true,"size_mb":347,"default":true}]}
```
This is the load-bearing proof of the Task 6 frozen-config fix: the engine reads the runtime
`paths.json` via `OMNIDROID_CONFIG_PATH` (not the read-only app-bundle default) and registers the
downloaded base + offset.

**STEP 4 — idempotent relaunch:** a second `ensure_runtime()` returned `changed=[]` (no re-download).

## Half 2 — GUI proof (frozen app)

1. Build once: `./build-macos.sh` → `dist/Omni Executor.app` (53 MB) + `dist/Omni Executor.dmg` (23 MB).
2. Frozen-engine smoke: `"dist/Omni Executor.app/Contents/MacOS/OmniExecutor" --omnidroid version --json` → engine version JSON, exit 0.
3. Launch pointed at an installed runtime:
   `OMNIEXEC_RUNTIME_DIR=<rt> "dist/Omni Executor.app/Contents/MacOS/OmniExecutor"`
   → app boots **straight to the Editor GUI** (no BootstrapView, no re-download), because
   `bootstrap_status` finds `installed.json` shas matching the manifest and `qemu_ok`.

### Observed (2026-08-13)
The frozen app opened directly to the OMNI EXECUTOR editor (sidebar Editor/Instances/Settings,
sample Lua, Run button, `Ready · Lua`) on the runtime the installer downloaded. Screenshot captured
(`slice-b-omni-executor-window.png`). "Engine setup needed" is expected on a fresh runtime with no
logged-in account.

## Full in-game boot (manual, requires a logged-in account + Roblox reachability)

Not exercised in this scripted run (the scratch runtime has no account). To do it live:
`omnidroid login` (account cookie) → `omnidroid start <name> --offset arceusremote` → arceus loads
the OMNI-EXEC UI → run a script via the Editor's Run button (exec bridge). The offset boot + arceus
+ exec-bridge path was demonstrated in a prior session (place `8737899170`); on ISP-blocked networks
it needs the in-guest DoT + zapret bypass or a VPN.

## Known cosmetic notes / follow-ups

- The `base-arm` tar (built on macOS) contains AppleDouble `._*` sidecar files, so `images/arm/`
  gets 0-byte `._base_arm_system_rooted.qcow2` etc. after extract. Harmless (omnidroid resolves
  by exact filename), but the base tar should be rebuilt with `COPYFILE_DISABLE=1` / `--no-mac-metadata`
  to drop them. Deferred.
- v1 requires Homebrew QEMU on a fresh Mac (`brew install qemu …`) — the one non-automated step;
  surfaced in BootstrapView. A bundled portable QEMU is a later slice.
- App is ad-hoc signed only (right-click → Open past Gatekeeper); Developer-ID + notarization is a
  follow-up.
