# Windows self-installing `omni-exec.exe` (Slice C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Windows `omni-exec.exe` that, on first boot, downloads the **arch-appropriate** runtime — for Windows the **x86 Bliss OS base + an x86 arceus offset** (not the arm64 base) — auto-installs QEMU, detects/guides WHPX, points the bundled omnidroid engine at the x86 assets, and thereafter is the executor. Same self-install architecture as Slice B (macOS), made arch-aware.

**Architecture:** The dist manifest is already `?os=mac|win` parameterized; Slice C makes the **client arch-aware** (Windows requests `os=win` → x86 base + x86 offset) and adds the **x86 half** everywhere the code was arm-only: omnidroid gains x86-offset naming/scanning, `bootstrap.configure_engine` gains an x86 branch (registers the `base_x86.*` triple as an `x86-bliss` base, detects `qemu-system-x86_64`, wires QEMU auto-download + WHPX guidance), the backend registers `os:"win"` artifacts, and a `build-windows.ps1` + PyInstaller spec freeze `omni-exec.exe`.

**Tech Stack:** Python 3.13 (stdlib), pywebview 5, React 19 + Vite 7, PyInstaller (Windows), omnidroid (QEMU `qemu-system-x86_64` + `-accel whpx`, NSIS QEMU silent-install), Node `node --test` (backend), pytest (executor).

## Global Constraints

- **Arch-aware, always ("download the arch-appropriate base each time"):** the client determines its OS at runtime and requests the matching manifest — Windows → `?os=win` (x86 Bliss base + x86 arceus offset), macOS → `?os=mac` (arm base + arm offset). Never hardcode `os=mac`. The base a machine downloads must match its architecture.
- **omnidroid x86 filenames (from `bases.py`, verbatim):** base = `base_x86.qcow2` / `base_x86.kernel` / `base_x86.initrd.img` (rooted variant `base_x86_rooted.initrd.img` preferred if present); base type `"x86-bliss"`; images subfolder `X86_DIR = "x86/"`; data template `x86/data-template-8g.qcow2`; base tag `"x86"`. The x86 offset filename MUST be `base_x86_data_offset_<name>.qcow2` (symmetric to the arm `base_arm_data_offset_<name>.qcow2`).
- **Backend os filter is a flat string field:** a registry entry belongs to exactly one `os` (`"mac"` | `"win"`); `list(os,channel)` filters `a.os === os`. A win entry mirrors the mac shape with `dest:"images/x86"` and x86 filenames. `?os=win` is already accepted by the validator.
- **Windows QEMU is auto-installed by omnidroid** (`ensure_qemu()` Windows branch downloads an NSIS installer and silent-installs to `QEMU_DIR` via `"<installer>" /S /D=<dir>`), BUT only if `read_config().qemu.download_url` is set (`DEFAULT_QEMU_URL` is `None`). Slice C MUST populate `qemu.download_url` on Windows (in `configure_engine`'s written `paths.json`, and/or as a manifest `qemu-win` artifact).
- **WHPX is not auto-checked:** `qemu_proc.default_accel()` returns `"whpx,kernel-irqchip=off"` on Windows and QEMU simply fails at launch if the "Windows Hypervisor Platform" feature is off. Slice C MUST detect this pre-boot and guide the user (`DISM /Online /Enable-Feature /FeatureName:HypervisorPlatform /All`, admin + reboot) rather than fail opaquely.
- **The arceus APK is arm-only** and runs on x86 Bliss via the base's baked `libndk_translation` (`ro.dalvik.vm.native.bridge=libndk_translation.so`). No houdini. This is a hard constraint: the x86 base's ARM-translation must be preserved by any bake.
- **Unverifiable-on-macOS is expected and must be labeled.** This Apple-Silicon Mac cannot run x86 WHPX/KVM, cannot cross-build a Windows `.exe`, and has no baked x86 offset. Every task states exactly what is unit-tested here vs. what is a documented Windows-host step. Do NOT fake a Windows build or a boot.
- **Test hygiene:** executor/omnidroid tests use fixtures + stdlib, never real network/VM; backend tests use `node --test`. Mirror the existing arm tests for the x86 paths.
- **Cross-repo git safety:** the omnidroid repo has large uncommitted WIP — NEVER `git checkout`/`stash`/`reset`/`git add -A` there; commit ONLY the specific files you changed, on a dedicated branch (`git checkout -b` carries WIP forward safely).

---

## The x86 arceus offset (the one hard prerequisite)

A real Windows install boots nothing until `base_x86_data_offset_<name>.qcow2` is baked from the arm-only arceus APK onto the libndk-translating x86 base, then uploaded and registered. This plan makes the **code** ready for it and investigates the bake (Task 6), but the actual bake+verify is a Windows/x86-host step. Tasks 1–5 are written so that the day the offset exists, dropping it in the registry + on the VPS is the only remaining action.

---

## Task 1: omnidroid — x86 offset support (`offsets.py`)

**Repo/branch:** omnidroid, `slice-c-x86-offsets` off current HEAD (carries WIP forward; commit ONLY the files below).

**Files:**
- Modify: `omnidroid/omnidroid/offsets.py`
- Test: `omnidroid/tests/test_offsets_x86.py` (new)

**Interfaces:**
- Consumes: `bases.py` `ARM_DIR`, `X86_DIR`, `X86_BASE_TAG`, `BASE_TYPE_X86`, `base_type()`.
- Produces: offset naming/scan that is arch-aware by base type. `offset_image_name(name, base_type=...)` (or a base-aware overload) returns `X86_DIR + f"base_x86_data_offset_{name}.qcow2"` for x86 bases, the existing arm path otherwise. `offsets_of(base)` and `default_offset_name(base)` resolve x86 offsets for an x86 base.

- [ ] **Step 1: Read the current `offsets.py`** to learn the exact signatures of `offset_image_name`, `offsets_of`, `default_offset_name`, `OFFSET_NAME_RE`, and how a `base` dict carries its type. Do not assume — the arm functions are the template.

- [ ] **Step 2: Write the failing test** — `omnidroid/tests/test_offsets_x86.py`:

```python
import omnidroid.offsets as off
import omnidroid.bases as bases

def test_x86_offset_image_name():
    # x86 base -> x86 offset path; arm base -> arm offset path (unchanged)
    x86_base = {"tag": "x86", "type": bases.BASE_TYPE_X86}
    arm_base = {"tag": "arm", "type": bases.BASE_TYPE_ARM}
    assert off.offset_image_name("arceusremote", base=x86_base) == bases.X86_DIR + "base_x86_data_offset_arceusremote.qcow2"
    assert off.offset_image_name("arceusremote", base=arm_base) == bases.ARM_DIR + "base_arm_data_offset_arceusremote.qcow2"

def test_x86_offset_name_regex_allows_dotted():
    assert off.OFFSET_NAME_RE.match("2.740.101")   # dotted names allowed
    assert off.OFFSET_NAME_RE.match("arceusremote")
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd omnidroid && python -m pytest tests/test_offsets_x86.py -v`
Expected: FAIL (signature/behavior mismatch).

- [ ] **Step 4: Implement** the arch-aware naming. Generalize `offset_image_name` to accept the base (or a base_type) and branch on `base_type(base) == BASE_TYPE_X86` → `X86_DIR + f"base_x86_data_offset_{name}.qcow2"`, else the existing `ARM_DIR` path. Keep backward compatibility: existing callers that pass only `name` must still get the arm path (default). Update `offsets_of(base)` / `default_offset_name(base)` to scan the correct subfolder for the base's type. Do not break any existing arm test.

- [ ] **Step 5: Run the new test + the existing offset tests**

Run: `cd omnidroid && python -m pytest tests/test_offsets_x86.py tests/ -k offset -v`
Expected: PASS; no arm regressions.

- [ ] **Step 6: Commit (omnidroid, dedicated branch, targeted files only)**

```bash
cd omnidroid && git checkout -b slice-c-x86-offsets
git add omnidroid/offsets.py tests/test_offsets_x86.py
git commit -m "feat(offsets): arch-aware offset naming/scan (x86 base support)"
```
(Do NOT `git add -A`; leave all other WIP untouched. Do not push.)

---

## Task 2: `bootstrap.py` — arch-aware install + x86 `configure_engine`

**Files:**
- Modify: `omni-executor/bootstrap.py`
- Test: `omni-executor/tests/test_bootstrap_win.py` (new)

**Interfaces:**
- Consumes: the manifest/blob contract; omnidroid x86 base/offset naming (Task 1, Global Constraints).
- Produces:
  - `current_os() -> str` — `"win"` on `sys.platform=="win32"`, else `"mac"` (Linux deferred; treat as mac-shape only if ever needed).
  - `runtime_dir()` gains a win32 branch → `%LOCALAPPDATA%/OmniExec` (fallback `%APPDATA%/OmniExec`), still overridable by `OMNIEXEC_RUNTIME_DIR`.
  - `read_manifest` is called with `current_os()` (no more hardcoded `"mac"`); `ensure_runtime` passes it through.
  - `configure_engine(rt)` becomes arch-aware: on Windows registers the **x86** base (`base_x86.qcow2`/`.kernel`/`.initrd.img` under `x86/`, type `x86-bliss`, `current_base="x86"`), scans `images/x86` for `base_x86_data_offset_<name>.qcow2`, detects `qemu-system-x86_64`, and sets `qemu.download_url` (from `OMNI_QEMU_WIN_URL` env or a constant) so omnidroid's `ensure_qemu` can auto-install. `engine_ready(rt)` detects the arch-appropriate qemu binary.

- [ ] **Step 1: Read the current `bootstrap.py`** (`runtime_dir`, `read_manifest`, `ensure_runtime`, `configure_engine`, `engine_ready`, and the `_ARM_*` constants) so the x86 branch mirrors the arm one exactly.

- [ ] **Step 2: Write the failing tests** — `tests/test_bootstrap_win.py`. Monkeypatch `bootstrap.sys.platform = "win32"` and point `OMNIEXEC_RUNTIME_DIR` at a tmp dir seeded with fake `images/x86/base_x86.qcow2|.kernel|.initrd.img` + `base_x86_data_offset_arceusremote.qcow2`:

```python
import json, sys
from pathlib import Path
import pytest
import bootstrap

def test_current_os_win(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    assert bootstrap.current_os() == "win"

def test_runtime_dir_win(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    monkeypatch.delenv("OMNIEXEC_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    p = bootstrap.runtime_dir()
    assert p == tmp_path / "OmniExec" and p.exists()

def test_configure_engine_x86(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    x = tmp_path / "images" / "x86"; x.mkdir(parents=True)
    for f in ("base_x86.qcow2", "base_x86.kernel", "base_x86.initrd.img",
              "base_x86_data_offset_arceusremote.qcow2"):
        (x / f).write_bytes(b"x")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: "/q/qemu-system-x86_64" if "x86_64" in n else None)
    eng = bootstrap.configure_engine(tmp_path)
    cfg = json.loads((tmp_path / "paths.json").read_text())
    assert cfg["current_base"] == "x86"
    b = cfg["bases"]["x86"]
    assert b["type"] == "x86-bliss" and b["disk"] == "x86/base_x86.qcow2"
    assert "arceusremote" in b.get("offsets", {}) or "arceusremote" in str(b.get("offsets"))
    assert cfg["qemu"].get("download_url")            # wired for ensure_qemu
    assert eng["qemu_ok"] is True
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd omni-executor && pytest tests/test_bootstrap_win.py -v`
Expected: FAIL.

- [ ] **Step 4: Implement.** Add `current_os()`; add the win32 `runtime_dir()` branch; thread `current_os()` into `read_manifest`/`ensure_runtime`; refactor `configure_engine` to branch on `current_os()`/platform — factor a shared writer and two arch blocks (arm as-is; x86 registers the base triple under `x86/` with `type:"x86-bliss"`, scans `base_x86_data_offset_*`, `current_base="x86"`, `qemu-system-x86_64` detection, and `qemu.download_url = os.environ.get("OMNI_QEMU_WIN_URL", <default NSIS url>)`). Update `engine_ready` to detect the arch-appropriate qemu. Keep `bootstrap.py` pure stdlib. Do NOT regress the arm tests (`tests/test_bootstrap.py`, `tests/test_engine_wiring.py`).

- [ ] **Step 5: Run x86 + arm tests**

Run: `cd omni-executor && pytest tests/test_bootstrap_win.py tests/test_bootstrap.py tests/test_engine_wiring.py -v`
Expected: PASS; no arm regressions.

- [ ] **Step 6: Commit**

```bash
git add bootstrap.py tests/test_bootstrap_win.py
git commit -m "feat: arch-aware bootstrap (win os detection, x86 runtime dir + configure_engine)"
```

---

## Task 3: Backend — register `os:"win"` x86 artifacts

**Files:**
- Modify: `omni-backend/dist/registry.json`, `omni-backend/backend/tests/fixtures/dist/registry.json`
- Test: `omni-backend/backend/tests/dist.test.js`

**Interfaces:**
- Consumes: `registry.js` `list(os)` flat-`os` filter; `distApi.js` manifest builder (already emits `dest`, `dest_name`, `unpack`).
- Produces: `?os=win` returns the x86 base + (pending) x86 offset + a QEMU installer entry.

- [ ] **Step 1: Write the failing test** — append to `backend/tests/dist.test.js`:

```javascript
test('manifest os=win returns x86 base + qemu', async () => {
  const res = await request(makeApp()).get('/omni/dist/manifest?os=win');
  assert.equal(res.status, 200);
  const names = res.body.artifacts.map(a => a.name);
  assert.ok(names.includes('base-x86'), 'x86 base present');
  const base = res.body.artifacts.find(a => a.name === 'base-x86');
  assert.equal(base.dest, 'images/x86');
  // qemu installer delivered by redirect (302) or as a named blob
  assert.ok(names.includes('qemu-win'), 'qemu-win present');
});
```
(Ensure the fixture registry has `base-x86`/`qemu-win` win entries so this passes; keep the existing `tiny-win` os-filter test green.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd omni-backend && node --test backend/tests/dist.test.js`
Expected: FAIL.

- [ ] **Step 3: Add the win entries to `dist/registry.json`.**
  - `base-x86`: `{os:"win", channel:"stable", version:"bliss-16.9.7", dest:"images/x86", unpack:"tar", file:"base-x86.tar", bytes:<n>, sha256:"<hex>"}` — compute `bytes`/`sha256` from a real tar of the x86 base triple (`base_x86.qcow2` + `.kernel` + `.initrd.img`, optionally `data-template-8g.qcow2`) built with `COPYFILE_DISABLE=1` (avoid AppleDouble sidecars). Its `dest_name` is null (tar).
  - `qemu-win`: `{os:"win", channel:"stable", version:"<qemu ver>", dest:"qemu", redirect:"<official qemu win64 NSIS installer URL>"}` — served as a `302` (the `/blob` handler already redirects when `entry.redirect` is set). This is what `configure_engine`'s `qemu.download_url` points at (or the client resolves via the manifest).
  - `offset-arceus-x86`: **register as PENDING** — add the entry with `dest:"images/x86"`, `dest_name:"base_x86_data_offset_arceusremote.qcow2"`, and a clear marker (`"pending": true` + a comment field) but WITHOUT bytes/sha until the offset is baked (Task 6). Do NOT invent a sha. If the manifest test would choke on a bytes-less entry, gate `pending` entries out of the manifest (the client can't download them yet anyway) and log the omission — a `pending` win offset must be visibly tracked, not silently absent.
  - Mirror the needed entries into `backend/tests/fixtures/dist/registry.json` for the test.

- [ ] **Step 4: Run tests to verify pass**

Run: `cd omni-backend && node --test backend/tests/dist.test.js backend/tests/registry.test.js` then `npm test`.
Expected: PASS (full suite green).

- [ ] **Step 5: Commit** (omni-backend `slice-c-win-artifacts` branch)

```bash
git add backend/src/omni-exec/ dist/registry.json backend/tests/fixtures/dist/registry.json backend/tests/dist.test.js
git commit -m "feat(dist): register os=win x86 base + qemu (+ pending arceus x86 offset)"
```
(The actual VPS upload of `base-x86.tar` + the QEMU redirect target is a controller deploy step, mirroring Slice A/B; not part of this task's code review.)

---

## Task 4: Windows first-boot enablers — WHPX detection + OS-aware UI

**Files:**
- Modify: `omni-executor/bootstrap.py` (add `windows_accel_status()`), `omni-executor/main.py` (expose it via `bootstrap_status`), `omni-executor/frontend/src/components/BootstrapView.jsx`
- Test: `omni-executor/tests/test_whpx.py` (new)

**Interfaces:**
- Produces: `bootstrap.windows_accel_status() -> {"os":"win", "whpx_ok": bool, "hint": str|None}` — on non-Windows returns `{"os":<os>, "whpx_ok": True, "hint": None}` (no-op). On Windows, detects whether the "Windows Hypervisor Platform" feature is enabled (query via `DISM /Online /Get-FeatureInfo /FeatureName:HypervisorPlatform`, or PowerShell `Get-WindowsOptionalFeature`, parsed for State=Enabled; be defensive — any failure → `whpx_ok:False` with the enable hint). `bootstrap_status` includes `whpx_ok`/`whpx_hint`; `BootstrapView` shows the WHPX enable guidance (`DISM /Online /Enable-Feature /FeatureName:HypervisorPlatform /All` — admin + reboot) on Windows when not enabled, and does NOT block macOS.

- [ ] **Step 1: Write the failing test** — `tests/test_whpx.py`: on non-Windows, `windows_accel_status()` returns `whpx_ok True, hint None`; with `sys.platform` monkeypatched to `win32` and the detection command monkeypatched to report "Disabled", returns `whpx_ok False` + a hint containing `HypervisorPlatform`. (Do not actually run DISM in the test — monkeypatch the subprocess call.)

- [ ] **Step 2: Run to verify it fails.** `pytest tests/test_whpx.py -v` → FAIL.

- [ ] **Step 3: Implement** `windows_accel_status()` in `bootstrap.py` (subprocess-based detection, fully guarded); thread `whpx_ok`/`whpx_hint` into `main.py::bootstrap_status`; extend `BootstrapView.jsx` to render the WHPX panel on Windows (reuse the qemu-hint panel pattern; `api("get_platform")` already tells the UI it's `win32`). The macOS qemu hint (`brew install qemu`) must become OS-aware: on Windows QEMU auto-installs (no brew hint); the Windows blocker surfaced to the user is WHPX + (if absent) the pending offset.

- [ ] **Step 4: Run tests + `npm run build`.** `pytest tests/test_whpx.py tests/test_bootstrap_api.py -v` → PASS; `cd frontend && npm run build` → succeeds.

- [ ] **Step 5: Commit** `git add bootstrap.py main.py frontend/src/components/BootstrapView.jsx tests/test_whpx.py` → `feat(win): WHPX detection + OS-aware first-boot guidance`.

---

## Task 5: Windows packaging — `omni-exec.exe` (write-only; cannot build on macOS)

**Files:**
- Create: `omni-executor/OmniExecutor-win.spec`, `omni-executor/build-windows.ps1`, `omni-executor/packaging/omni-exec.ico` (placeholder acceptable)
- Modify: `omni-executor/docs/superpowers/e2e-macos-checklist.md` sibling → create `docs/superpowers/windows-build-notes.md`

**Interfaces:**
- Consumes: the frozen `--omnidroid` dispatch (already in `main.py`), `frontend/dist`, the sibling omnidroid package, `bootstrap.py`.
- Produces: a PyInstaller spec + PowerShell build script that, RUN ON WINDOWS, produce `dist/omni-exec.exe`.

> **Hard limitation (state it in the report):** PyInstaller cannot cross-build a Windows exe from macOS. This task WRITES and self-consistency-checks the spec/script; it does NOT build or smoke-test the exe. Acceptance = the spec is syntactically valid Python and the script is valid PowerShell, and both are internally consistent with `main.py`'s frozen dispatch. The real build/smoke (`omni-exec.exe --omnidroid version --json`) is a Windows-host step documented in `windows-build-notes.md`.

- [ ] **Step 1: Write `OmniExecutor-win.spec`** — mirror `OmniExecutor.spec` but for Windows: entry `main.py`; `datas += [('frontend/dist','frontend/dist')]`; `hiddenimports += collect_submodules('omnidroid')`; `pathex=[PROJECT_DIR, OMNIDROID_REPO]`; bundle `bootstrap.py`; `EXE(... name='omni-exec', console=False, icon='packaging/omni-exec.ico')`. Prefer one-dir (faster start, easier QEMU/engine adjacency) OR one-file — document the choice. Ensure `omnidroid.exe`-adjacency is NOT required (the frozen `--omnidroid` self-dispatch handles the engine in-binary, same as macOS).

- [ ] **Step 2: Write `build-windows.ps1`** — `npm run build` (frontend) → `pyinstaller --noconfirm OmniExecutor-win.spec` → (optional) `signtool` Authenticode note (out of scope v1; SmartScreen warning documented) → output `dist/omni-exec.exe`. `Set-StrictMode`; fail on missing deps with actionable messages.

- [ ] **Step 3: Write `docs/superpowers/windows-build-notes.md`** — the exact Windows-host steps: install Python 3.13 + `pip install pyinstaller` + Node, run `build-windows.ps1`, then the smoke test `.\dist\omni-exec.exe --omnidroid version --json`; the WHPX enable step; and the pending x86-offset dependency. Be explicit about what is unverified.

- [ ] **Step 4: Self-consistency check** — `python -c "import ast; ast.parse(open('OmniExecutor-win.spec').read())"` (valid Python); `pwsh -NoProfile -Command "..."` syntax check if `pwsh` is available, else a documented manual check. No exe is produced on macOS.

- [ ] **Step 5: Commit** `git add OmniExecutor-win.spec build-windows.ps1 packaging/omni-exec.ico docs/superpowers/windows-build-notes.md` → `feat(packaging): Windows omni-exec.exe spec + build-windows.ps1 (build on Windows)`.

---

## Task 6: x86 arceus offset — bake investigation + Windows e2e checklist

**Files:**
- Create: `omni-executor/docs/superpowers/windows-e2e-checklist.md`
- Create: `omni-executor/scripts/bake-x86-offset-notes.md` (or extend the engine's offset docs) — the exact procedure + feasibility finding.

This task is investigation + documentation (no TDD). It is the honest close-out of the one hard prerequisite.

- [ ] **Step 1: Determine the x86 offset bake path.** Read the omnidroid engine's `offset create`/bake code (how the arm offset was baked) and determine whether an x86 offset can be baked **offline** (inject the arm-only arceus APK into the x86 base's `/product` or `/data` via `debugfs`/`qemu-img`, preserving `libndk_translation`) — like the arm offline bake — or whether it requires **booting** the x86 Android (which needs x86 virtualization this Apple-Silicon Mac lacks).

- [ ] **Step 2: If offline-bakeable here, ATTEMPT it** — produce `base_x86_data_offset_arceusremote.qcow2` using the omnidroid x86 offset naming from Task 1, verify it registers via `configure_engine` + `omnidroid offset list` (config-level, no boot). If it requires an x86 boot, STOP and document precisely why (this Mac can't), leaving the artifact unbaked.

- [ ] **Step 3: Write `windows-e2e-checklist.md`** — the full Windows install flow (download x86 base+offset+qemu → configure_engine x86 → WHPX enable → boot → arceus via libndk → exec bridge), marking each step verified/unverified, and the exact remaining prerequisites: (a) bake+upload+register the x86 offset, (b) build `omni-exec.exe` on Windows, (c) enable WHPX on the target box.

- [ ] **Step 4: Commit** `git add docs/superpowers/windows-e2e-checklist.md scripts/bake-x86-offset-notes.md` → `docs(win): x86 offset bake finding + Windows e2e checklist`.

---

## Self-Review notes (author)

- **Spec coverage:** arch-aware manifest+client (Global Constraints, Task 2) ✅; x86 base registration (Task 2/3) ✅; omnidroid x86 offset code (Task 1) ✅; QEMU auto-install wiring (Task 2 `qemu.download_url` + Task 3 `qemu-win`) ✅; WHPX detect+guide (Task 4) ✅; Windows packaging (Task 5) ✅; the x86-offset prerequisite investigated + documented (Task 6) ✅; "download the arch-appropriate base each time" (Global Constraints + Task 2) ✅.
- **Honestly out of reach on this Mac (labeled, not faked):** building `omni-exec.exe` (needs Windows), running WHPX/x86 boot (needs x86 host), and — unless Step-1 finds an offline path — baking the x86 arceus offset. These are documented Windows-host steps, not claimed as done.
- **Type consistency:** `current_os`, `runtime_dir`, `configure_engine`, `engine_ready`, `windows_accel_status` names are stable across Tasks 2/4; x86 filenames match `bases.py` verbatim; `offset_image_name` x86 path (Task 1) matches `configure_engine`'s x86 offset regex (Task 2) and the `dest_name` in the registry (Task 3): all `base_x86_data_offset_<name>.qcow2`.
- **Cross-repo:** Task 1 → omnidroid `slice-c-x86-offsets`; Task 3 → omni-backend `slice-c-win-artifacts`; Tasks 2/4/5/6 → omni-executor `slice-c-windows-exe`. SDD ledger lives in the omni-executor workspace.
