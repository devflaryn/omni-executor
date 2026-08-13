# macOS self-installing `.dmg` (Slice B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `Omni Executor.app` inside a `.dmg` that, on first boot, downloads the Android base + arceus offset from the backend by name, verifies and installs them, points the bundled omnidroid engine at them, and thereafter *is* the Omni Executor.

**Architecture:** A new headless `bootstrap.py` in omni-executor fetches `GET /omni/dist/manifest?os=mac`, streams each blob with HTTP Range resume + sha256 verification into `~/Library/Application Support/OmniExec/`, records `installed.json`, and writes an omnidroid `paths.json` + sets engine env vars so the bundled engine finds the downloaded assets. Two new `Api` bridge methods (`bootstrap_status`, `bootstrap_start`) drive a React `BootstrapView` first-boot screen through the existing `window.omniEvent` progress channel. PyInstaller freezes the app (with an in-binary `--omnidroid` engine dispatch), `create-dmg` wraps it. A small additive backend change adds a `dest_name` field to the manifest so bare blobs land under the exact filename omnidroid expects.

**Tech Stack:** Python 3.13 (stdlib `http.client`/`urllib`, `hashlib`, `tarfile`, `shutil`), pywebview 5, React 19 + Vite 7 + Tailwind 4, PyInstaller, `create-dmg`, Node 18+ `node --test` (backend), pytest (executor).

## Global Constraints

- **Backend contract is fixed and already deployed.** The manifest is `GET /omni/dist/manifest?os=mac|win&channel=stable` → `{ok, os, channel, app:{version}, artifacts:[{name, version, bytes, sha256, url, dest, unpack}]}`. Blobs are `GET /omni/dist/blob/<name>` with `Accept-Ranges: bytes`, `206 Partial Content`, `Content-Range`, `X-Omni-SHA256`. Do not rename or restructure these; Slice B only *adds* an optional `dest_name` field (Task 1).
- **Distribution base URL:** `os.environ.get("OMNI_EXEC_BASE", "http://72.62.59.232")` — reuse the exact same env var and default already defined at `main.py:49`. Never hardcode the IP anywhere else.
- **Runtime asset dir (macOS):** `~/Library/Application Support/OmniExec/` (note: **`OmniExec`**, distinct from the existing settings dir `omni-executor`). Overridable via `OMNIEXEC_RUNTIME_DIR` for tests. Layout: `installed.json`, `paths.json`, `images/arm/…`, plus omnidroid's own `accounts/`, `runtime/`, `logs/` under the same root.
- **QEMU policy (v1 decision):** macOS v1 downloads **only** `base-arm` + `offset-arceus-arm` and uses **system Homebrew QEMU** (`qemu-system-aarch64`). The bootstrapper must *detect* qemu and, if absent, surface a clear actionable message (`brew install qemu android-platform-tools`) rather than failing opaquely. A bundled portable QEMU is explicitly out of scope for v1 (spec Risk #2). This is the one non-automated step on a truly fresh Mac and must be called out in the BootstrapView UI.
- **Engine invocation is unchanged:** omnidroid is driven as a subprocess emitting one JSON value on stdout (`--json`), progress on stderr. The bundled binary gains a `--omnidroid <args…>` dispatch mode; `engine_prefix()` resolves to `[sys.executable, "--omnidroid"]` when `sys.frozen`. Do not introduce an in-process import path for the engine.
- **Codesigning:** ad-hoc sign only for v1 (`codesign --force --deep --sign -`). Notarization/Developer-ID is a documented follow-up, not in scope.
- **Resumable + verified + idempotent:** every blob download supports Range resume, is sha256-verified before placement, staged in a temp dir and atomically moved; re-running `ensure_runtime` with everything already present and matching must be a no-op (no re-download).
- **Test hygiene:** executor tests never hit the network or a real engine — use a local `http.server` fixture and the existing `captured` monkeypatch fixture (`tests/conftest.py`). Backend tests use `node --test` with the existing tiny fixture blob under `backend/tests/fixtures/dist/`.

---

## File Structure

**omni-backend** (one small additive change):
- Modify `backend/src/omni-exec/registry.js` — surface `dest_name` on artifact entries.
- Modify `backend/src/omni-exec/distApi.js` — include `dest_name` in the manifest artifact objects.
- Modify `dist/registry.json` + `backend/tests/fixtures/dist/registry.json` — add `dest_name` to the offset (and fixture) entries.
- Modify `backend/tests/dist.test.js` — assert `dest_name` in the manifest.

**omni-executor** (the bulk):
- Create `bootstrap.py` — the headless runtime installer (manifest, download, verify, place, installed.json, engine config).
- Modify `main.py` — `engine_prefix()` fix + frozen `--omnidroid` dispatch + two new `Api` methods.
- Create `frontend/src/components/BootstrapView.jsx` — first-boot progress UI.
- Modify `frontend/src/App.jsx` — first-boot gate.
- Modify `frontend/src/api.js` — a `bootstrap()` helper (thin).
- Create `OmniExecutor.spec` — PyInstaller spec (bundles frontend/dist + omnidroid pkg).
- Create `packaging/Info.plist`, `packaging/icon.icns` (placeholder icon acceptable for v1).
- Create `build-macos.sh` — orchestrates frontend build → PyInstaller → dmg.
- Create tests: `tests/test_bootstrap.py`, `tests/test_engine_wiring.py`, `tests/test_bootstrap_api.py`.
- Create `scripts/e2e-macos.sh` + `docs/superpowers/e2e-macos-checklist.md` — Task 7 acceptance.

---

## Task 1: Backend — add `dest_name` to the manifest (omni-backend)

**Repo/branch:** omni-backend, on a `slice-b-manifest-destname` branch off `main`.

**Files:**
- Modify: `backend/src/omni-exec/registry.js`
- Modify: `backend/src/omni-exec/distApi.js:20-23` (the `.map` that builds manifest artifacts)
- Modify: `dist/registry.json`, `backend/tests/fixtures/dist/registry.json`
- Test: `backend/tests/dist.test.js`

**Interfaces:**
- Consumes: existing `loadRegistry(distDir)` → `{distDir, appVersion, list(os,channel), get(name)}` and the manifest builder in `distApi.js`.
- Produces: manifest artifact objects now optionally carry `dest_name: <string>|null` — the exact filename a **bare** (non-`unpack`) blob must be written as on the client. `null`/absent for tar artifacts (their filenames come from the archive). The bootstrapper (Task 2) reads this.

- [ ] **Step 1: Write the failing test** — append to `backend/tests/dist.test.js`:

```javascript
test('manifest exposes dest_name for bare-blob artifacts', async () => {
  const res = await request(app).get('/omni/dist/manifest?os=mac');
  assert.equal(res.status, 200);
  const offset = res.body.artifacts.find(a => a.name === 'offset-arceus-arm');
  assert.ok(offset, 'offset artifact present');
  assert.equal(typeof offset.dest_name, 'string');
  assert.match(offset.dest_name, /^base_arm_data_offset_.+\.qcow2$/);
  const base = res.body.artifacts.find(a => a.name === 'base-arm');
  assert.equal(base.dest_name ?? null, null); // tar artifact: no dest_name
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd omni-backend && node --test backend/tests/dist.test.js`
Expected: FAIL — `dest_name` is `undefined`.

- [ ] **Step 3: Surface `dest_name` in the registry loader.** In `registry.js`, wherever entries are normalized/returned by `list()`/`get()`, pass through `dest_name` (default `null`). If entries are returned as-is from JSON, add `dest_name: e.dest_name ?? null` to the projection.

- [ ] **Step 4: Include it in the manifest.** In `distApi.js:20-23`, add `dest_name: a.dest_name ?? null,` to the mapped artifact object (alongside `dest`, `unpack`).

- [ ] **Step 5: Set it in the registries.** In `dist/registry.json` **and** `backend/tests/fixtures/dist/registry.json`, add to the `offset-arceus-arm` entry (fixture: to whichever bare-blob fixture artifact exists; if the fixture only has `tiny.bin`, give that fixture entry a `dest_name` like `"base_arm_data_offset_arceusfixture.qcow2"` so the assertion’s regex holds) a `dest_name` field. For prod: read the offset’s omnidroid name from the current offset filename on the VPS/registry `file` field and set `dest_name` to the exact `base_arm_data_offset_<name>.qcow2` omnidroid expects (cross-check `omnidroid/offsets.py::offset_image_name`). Do **not** guess the name — derive it from the registry `file`/omnidroid contract.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd omni-backend && node --test backend/tests/dist.test.js backend/tests/registry.test.js`
Expected: PASS (all, including the new assertion). Then `npm test` — full suite still green.

- [ ] **Step 7: Deploy the additive change** (mirrors Slice A’s file-copy deploy): scp the four changed files to the VPS `/root/omni-backend/…`, `pm2 restart omni-backend`, then `curl -s http://72.62.59.232/omni/dist/manifest?os=mac | python3 -m json.tool` and confirm the offset now carries `dest_name`. (Deploy is part of this task because Task 2/7 pull the live manifest.)

- [ ] **Step 8: Commit** (omni-backend)

```bash
git add backend/src/omni-exec/registry.js backend/src/omni-exec/distApi.js dist/registry.json backend/tests/fixtures/dist/registry.json backend/tests/dist.test.js
git commit -m "feat(dist): add dest_name to manifest for bare-blob placement"
```

---

## Task 2: Bootstrapper core — `bootstrap.py` (omni-executor)

**Files:**
- Create: `bootstrap.py`
- Test: `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: the manifest/blob contract (Global Constraints); `dest_name` from Task 1.
- Produces (public API used by Tasks 3 & 7):
  - `runtime_dir() -> pathlib.Path` — `~/Library/Application Support/OmniExec` (or `$OMNIEXEC_RUNTIME_DIR`), created.
  - `dist_base() -> str` — the `OMNI_EXEC_BASE` value.
  - `read_manifest(base_url: str, os_name: str = "mac", channel: str = "stable") -> dict` — GET + JSON, raises `BootstrapError` on non-200/`ok:false`.
  - `installed_state(rt: Path) -> dict` — parsed `installed.json` or `{"artifacts": {}, "app_version": None}`.
  - `plan_downloads(manifest: dict, installed: dict) -> list[dict]` — the artifacts whose `sha256` differs from installed (or missing).
  - `download_blob(base_url, artifact, tmp_path, progress=None) -> None` — Range-resumable, retries on sha256 mismatch (bounded 3), raises `BootstrapError` on exhaustion.
  - `place_artifact(artifact, tmp_path, rt) -> None` — `unpack=="tar"`/`"tar.gz"` → extract into `rt/<dest>`; else move to `rt/<dest>/<dest_name or basename(url)>`.
  - `ensure_runtime(base_url=None, progress=None) -> dict` — orchestrates: manifest → disk precheck → for each planned artifact download+verify+place → write `installed.json` → returns `{"ok": True, "installed": {...}, "changed": [...]}`. `progress(dict)` callback receives `{"phase","artifact","received","total","percent"}` events.
  - `class BootstrapError(Exception)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_bootstrap.py`. Use a local threaded `http.server` serving a fixture manifest + a byte blob supporting Range, so no network. Skeleton:

```python
import hashlib, http.server, json, socketserver, tarfile, threading, io
from pathlib import Path
import pytest
import bootstrap


def _sha(b): return hashlib.sha256(b).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    blobs = {}          # name -> bytes
    manifest = {}       # dict
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/omni/dist/manifest"):
            body = json.dumps(self.manifest).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/omni/dist/blob/"):
            name = self.path.rsplit("/",1)[-1]
            data = self.blobs.get(name)
            if data is None: self.send_response(404); self.end_headers(); return
            rng = self.headers.get("Range")
            start = 0
            if rng and rng.startswith("bytes="):
                start = int(rng.split("=")[1].split("-")[0] or 0)
            chunk = data[start:]
            code = 206 if start else 200
            self.send_response(code)
            self.send_header("Accept-Ranges","bytes")
            self.send_header("X-Omni-SHA256", _sha(data))
            if start: self.send_header("Content-Range", f"bytes {start}-{len(data)-1}/{len(data)}")
            self.send_header("Content-Length", str(len(chunk))); self.end_headers(); self.wfile.write(chunk)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path / "rt"))
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{port}", _Handler
    srv.shutdown()


def _tar_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name); ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_downloads_verifies_places_and_records(server):
    base, H = server
    offset = b"OFFSETDATA" * 100
    tar = _tar_bytes({"base_arm_system_rooted.qcow2": b"SYS", "base_arm_data_rooted.qcow2": b"DATA"})
    H.blobs = {"offset-arceus-arm": offset, "base-arm": tar}
    H.manifest = {"ok": True, "os":"mac", "channel":"stable", "app":{"version":"1.0.0"},
        "artifacts":[
          {"name":"base-arm","version":"lineage-23.2","bytes":len(tar),"sha256":_sha(tar),
           "url":"/omni/dist/blob/base-arm","dest":"images/arm","unpack":"tar","dest_name":None},
          {"name":"offset-arceus-arm","version":"2.732.1043","bytes":len(offset),"sha256":_sha(offset),
           "url":"/omni/dist/blob/offset-arceus-arm","dest":"images/arm","unpack":None,
           "dest_name":"base_arm_data_offset_arceusremote.qcow2"},
        ]}
    res = bootstrap.ensure_runtime(base_url=base)
    rt = bootstrap.runtime_dir()
    assert res["ok"] is True
    assert (rt/"images/arm/base_arm_system_rooted.qcow2").read_bytes() == b"SYS"
    assert (rt/"images/arm/base_arm_data_offset_arceusremote.qcow2").read_bytes() == offset
    inst = json.loads((rt/"installed.json").read_text())
    assert inst["artifacts"]["offset-arceus-arm"]["sha256"] == _sha(offset)


def test_idempotent_second_run_downloads_nothing(server):
    base, H = server
    blob = b"X"*512
    H.blobs = {"offset-arceus-arm": blob}
    H.manifest = {"ok":True,"os":"mac","channel":"stable","app":{"version":"1.0.0"},
      "artifacts":[{"name":"offset-arceus-arm","version":"1","bytes":len(blob),"sha256":_sha(blob),
        "url":"/omni/dist/blob/offset-arceus-arm","dest":"images/arm","unpack":None,
        "dest_name":"base_arm_data_offset_x.qcow2"}]}
    bootstrap.ensure_runtime(base_url=base)
    plan = bootstrap.plan_downloads(H.manifest, bootstrap.installed_state(bootstrap.runtime_dir()))
    assert plan == []


def test_sha_mismatch_rejected_then_retry_succeeds(server, monkeypatch):
    base, H = server
    good = b"G"*300
    H.blobs = {"offset-arceus-arm": good}
    art = {"name":"offset-arceus-arm","version":"1","bytes":len(good),
           "sha256":"0"*64,  # wrong on purpose
           "url":"/omni/dist/blob/offset-arceus-arm","dest":"images/arm","unpack":None,
           "dest_name":"base_arm_data_offset_x.qcow2"}
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.download_blob(base, art, bootstrap.runtime_dir()/"tmp.bin")
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd omni-executor && pytest tests/test_bootstrap.py -v`
Expected: FAIL — `bootstrap` module not found.

- [ ] **Step 3: Implement `bootstrap.py`.** Concrete implementation:

```python
"""Headless first-boot runtime installer for Omni Executor (macOS).

Fetches the named-blob manifest, downloads + sha256-verifies each artifact
with HTTP Range resume, places it under the OmniExec runtime dir, and records
installed.json. No GUI, no engine import — pure stdlib so it is unit-testable.
"""
import hashlib, json, os, shutil, sys, tarfile, tempfile, time, urllib.request, urllib.error
from pathlib import Path

APP_DIR_NAME = "OmniExec"
MANIFEST_PATH = "/omni/dist/manifest"
_CHUNK = 1 << 20
_MAX_RETRIES = 3


class BootstrapError(Exception):
    pass


def dist_base() -> str:
    return os.environ.get("OMNI_EXEC_BASE", "http://72.62.59.232").rstrip("/")


def runtime_dir() -> Path:
    override = os.environ.get("OMNIEXEC_RUNTIME_DIR")
    if override:
        p = Path(override)
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    else:  # dev on other OSes
        p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _installed_file(rt: Path) -> Path:
    return rt / "installed.json"


def installed_state(rt: Path) -> dict:
    f = _installed_file(rt)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"artifacts": {}, "app_version": None}


def read_manifest(base_url: str, os_name: str = "mac", channel: str = "stable") -> dict:
    url = f"{base_url.rstrip('/')}{MANIFEST_PATH}?os={os_name}&channel={channel}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            if r.status != 200:
                raise BootstrapError(f"manifest HTTP {r.status}")
            data = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BootstrapError(f"manifest fetch failed: {e}") from e
    if not data.get("ok"):
        raise BootstrapError("manifest not ok")
    return data


def plan_downloads(manifest: dict, installed: dict) -> list:
    have = installed.get("artifacts", {})
    out = []
    for a in manifest.get("artifacts", []):
        cur = have.get(a["name"])
        if not cur or cur.get("sha256") != a["sha256"]:
            out.append(a)
    return out


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def download_blob(base_url: str, artifact: dict, tmp_path: Path, progress=None) -> None:
    url = f"{base_url.rstrip('/')}{artifact['url']}"
    total = int(artifact.get("bytes") or 0)
    want = artifact["sha256"]
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, _MAX_RETRIES + 1):
        have = tmp_path.stat().st_size if tmp_path.exists() else 0
        if have > total:  # corrupt partial
            tmp_path.unlink(); have = 0
        req = urllib.request.Request(url)
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp_path, "ab") as out:
                received = have
                while True:
                    chunk = r.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk); received += len(chunk)
                    if progress:
                        progress({"phase": "download", "artifact": artifact["name"],
                                  "received": received, "total": total,
                                  "percent": (received / total * 100) if total else 0})
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(1); continue  # resume next attempt
        if _hash_file(tmp_path) == want:
            return
        tmp_path.unlink(missing_ok=True)  # bad content, redownload from scratch
    raise BootstrapError(f"{artifact['name']}: sha256 mismatch after {_MAX_RETRIES} attempts")


def place_artifact(artifact: dict, tmp_path: Path, rt: Path) -> None:
    dest_dir = rt / artifact["dest"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    unpack = artifact.get("unpack")
    if unpack in ("tar", "tar.gz", "tgz"):
        mode = "r:gz" if unpack != "tar" else "r:"
        with tarfile.open(tmp_path, mode) as tf:
            _safe_extract(tf, dest_dir)
        tmp_path.unlink(missing_ok=True)
    else:
        name = artifact.get("dest_name") or artifact["url"].rsplit("/", 1)[-1]
        shutil.move(str(tmp_path), str(dest_dir / name))


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    for m in tf.getmembers():
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(dest)):
            raise BootstrapError(f"unsafe tar member: {m.name}")
    tf.extractall(dest)


def _precheck_space(rt: Path, plan: list) -> None:
    need = int(sum(a.get("bytes") or 0 for a in plan) * 1.1)
    free = shutil.disk_usage(rt).free
    if free < need:
        raise BootstrapError(f"insufficient disk: need ~{need // 2**30}GB, free {free // 2**30}GB")


def ensure_runtime(base_url: str = None, progress=None) -> dict:
    base_url = (base_url or dist_base()).rstrip("/")
    rt = runtime_dir()
    manifest = read_manifest(base_url)
    installed = installed_state(rt)
    plan = plan_downloads(manifest, installed)
    if not plan:
        return {"ok": True, "installed": installed.get("artifacts", {}), "changed": []}
    _precheck_space(rt, plan)
    staging = rt / "staging"; staging.mkdir(parents=True, exist_ok=True)
    changed = []
    for a in plan:
        if progress:
            progress({"phase": "start", "artifact": a["name"], "received": 0,
                      "total": int(a.get("bytes") or 0), "percent": 0})
        tmp = staging / f"{a['name']}.part"
        download_blob(base_url, a, tmp, progress)
        place_artifact(a, tmp, rt)
        installed.setdefault("artifacts", {})[a["name"]] = {
            "version": a.get("version"), "sha256": a["sha256"], "bytes": a.get("bytes")}
        changed.append(a["name"])
    installed["app_version"] = manifest.get("app", {}).get("version")
    tmpf = _installed_file(rt).with_suffix(".tmp")
    tmpf.write_text(json.dumps(installed, indent=2))
    tmpf.replace(_installed_file(rt))
    shutil.rmtree(staging, ignore_errors=True)
    if progress:
        progress({"phase": "done", "artifact": None, "received": 0, "total": 0, "percent": 100})
    return {"ok": True, "installed": installed["artifacts"], "changed": changed}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd omni-executor && pytest tests/test_bootstrap.py -v`
Expected: PASS (3/3).

- [ ] **Step 5: Commit**

```bash
git add bootstrap.py tests/test_bootstrap.py
git commit -m "feat: bootstrap.py — resumable, verified first-boot runtime installer"
```

---

## Task 3: Engine wiring — point the bundled engine at downloaded assets (omni-executor)

**Files:**
- Modify: `main.py:88-119` (`engine_prefix`), and `main()` top (`main.py:595`) for the `--omnidroid` dispatch
- Modify: `bootstrap.py` (add `configure_engine`)
- Test: `tests/test_engine_wiring.py`

**Interfaces:**
- Consumes: `bootstrap.runtime_dir()`, `installed_state()`.
- Produces:
  - `bootstrap.configure_engine(rt: Path) -> dict` — sets `os.environ` for `OMNI_DATA_DIR=rt`, `OMNI_IMAGES_DIR=rt/"images"`; writes/updates `rt/"paths.json"` with the omnidroid qemu block + the arm base + default offset registered from what’s present under `rt/images/arm`; returns `{"images_dir","data_dir","qemu_ok": bool, "qemu_hint": str|None}`. Detects `qemu-system-aarch64` via `shutil.which`; if absent sets `qemu_ok=False`, `qemu_hint="brew install qemu android-platform-tools"`.
  - `engine_prefix()` frozen branch → `[sys.executable, "--omnidroid"]`; source fallback fixed to the real engine.

- [ ] **Step 1: Write the failing tests** — `tests/test_engine_wiring.py`:

```python
import os, sys, json, types
from pathlib import Path
import pytest
import main, bootstrap


def test_engine_prefix_frozen_dispatch(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Omni Executor.app/Contents/MacOS/OmniExecutor", raising=False)
    monkeypatch.delenv("OMNIDROID_ENGINE", raising=False)
    assert main.engine_prefix() == [sys.executable, "--omnidroid"]


def test_engine_prefix_source_fallback_is_module(monkeypatch, tmp_path):
    # no env, not frozen, no adjacent binary, sibling omnidroid checkout present
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("OMNIDROID_ENGINE", raising=False)
    sib = tmp_path / "omnidroid" / "omnidroid"
    sib.mkdir(parents=True); (sib / "__main__.py").write_text("")
    monkeypatch.setattr(main, "PROJECT_DIR", tmp_path / "omni-executor")
    (tmp_path / "omni-executor").mkdir()
    prefix = main.engine_prefix()
    assert prefix[0] == sys.executable and "omnidroid" in prefix  # -m omnidroid form


def test_configure_engine_sets_env_and_writes_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "images/arm").mkdir(parents=True)
    (tmp_path / "images/arm/base_arm_system_rooted.qcow2").write_bytes(b"x")
    (tmp_path / "images/arm/base_arm_data_offset_arceusremote.qcow2").write_bytes(b"x")
    info = bootstrap.configure_engine(tmp_path)
    assert os.environ["OMNI_DATA_DIR"] == str(tmp_path)
    assert os.environ["OMNI_IMAGES_DIR"] == str(tmp_path / "images")
    cfg = json.loads((tmp_path / "paths.json").read_text())
    assert "qemu" in cfg and "images_dir" in cfg
    assert isinstance(info["qemu_ok"], bool)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd omni-executor && pytest tests/test_engine_wiring.py -v`
Expected: FAIL (no frozen branch; no `configure_engine`).

- [ ] **Step 3: Fix `engine_prefix()` (`main.py:88`).** Add a frozen branch **first** and correct the source fallback:

```python
def engine_prefix():
    env = os.environ.get("OMNIDROID_ENGINE")
    if env:
        p = Path(env)
        return [sys.executable, str(p)] if p.suffix == ".py" else [str(p)]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--omnidroid"]           # in-binary engine dispatch
    exe = "omnidroid.exe" if sys.platform == "win32" else "omnidroid"
    adjacent = PROJECT_DIR / exe
    if adjacent.exists():
        return [str(adjacent)]
    sibling = PROJECT_DIR.parent / "omnidroid"           # sibling checkout root
    if (sibling / "omnidroid" / "__main__.py").exists():
        return [sys.executable, "-m", "omnidroid"]        # was: manager.py (stale)
    return None
```

Ensure `run_engine` sets `cwd` so `-m omnidroid` resolves the package: when using the sibling module form, run with `cwd=str(sibling)` (add a small branch, or prepend the sibling to `PYTHONPATH` in the `Popen` env). Keep the existing `cwd=PROJECT_DIR` for other forms. (Minimal: in `run_engine`, if prefix is the `-m omnidroid` form, pass `env={**os.environ, "PYTHONPATH": str(PROJECT_DIR.parent / "omnidroid")}`.)

- [ ] **Step 4: Add the frozen `--omnidroid` dispatch.** At the very top of `main()` (`main.py:595`), before any webview work:

```python
def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--omnidroid":
        sys.argv = ["omnidroid", *sys.argv[2:]]
        from omnidroid.cli import main as engine_main
        engine_main()
        return
    ...
```

- [ ] **Step 5: Implement `bootstrap.configure_engine(rt)`.** It must produce a `paths.json` omnidroid accepts. Read `omnidroid/config.py`, `engine.py` (`DEFAULT_CONFIG`, `ensure_config`, `load_config`, `autoregister_bases`), `bases.py` (`ARM_DIR`), and `offsets.py` (`offset_image_name`, `default_offset_name`) for the exact schema, and mirror the **proven working** dev `configs/paths.json` (its `qemu` block + arm base registration boot daily on this Mac). Set `OMNI_DATA_DIR`, `OMNI_IMAGES_DIR`; detect qemu via `shutil.which("qemu-system-aarch64")`. Concrete shape:

```python
def configure_engine(rt: Path) -> dict:
    images = rt / "images"
    os.environ["OMNI_DATA_DIR"] = str(rt)
    os.environ["OMNI_IMAGES_DIR"] = str(images)
    qemu_bin = shutil.which("qemu-system-aarch64")
    cfg = {
        "images_dir": str(images),
        "current_base": None,     # omnidroid autoregisters arm base from images/arm
        "data_template": "data-template-8g.qcow2",
        "bases": {},
        "qemu": {"mem_mb": 4096, "smp": 4, "data_disk_size": "8G",
                 "adb_port_start": 16001, "qmp_port_start": 17001, "vnc_port_start": 18001},
    }
    if qemu_bin:
        cfg["qemu"]["dir"] = str(Path(qemu_bin).parent)
    (rt / "paths.json").write_text(json.dumps(cfg, indent=2))
    return {"images_dir": str(images), "data_dir": str(rt),
            "qemu_ok": bool(qemu_bin),
            "qemu_hint": None if qemu_bin else "brew install qemu android-platform-tools"}
```

> **Implementer note:** verify against the real omnidroid loader that this minimal `paths.json` + `OMNI_IMAGES_DIR` makes `omnidroid bases --json` list the downloaded arm base and `omnidroid offset list` show the offset. If `load_config` needs `current_base` set explicitly (not just autoregistered), set it to the arm base tag omnidroid derives. The acceptance is: with a fake `images/arm` containing the base trio + offset file, `python -m omnidroid bases --json` exits 0 and lists the base. Add that as an integration assertion guarded by an import-availability skip (mirror `tests/test_engine_capabilities.py`).

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd omni-executor && pytest tests/test_engine_wiring.py tests/test_bootstrap.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add main.py bootstrap.py tests/test_engine_wiring.py
git commit -m "feat: frozen --omnidroid dispatch + engine_prefix fix + configure_engine"
```

---

## Task 4: Bridge API — `bootstrap_status` / `bootstrap_start` (omni-executor)

**Files:**
- Modify: `main.py` (the `Api` class, near the other methods ~`main.py:264-560`)
- Test: `tests/test_bootstrap_api.py`

**Interfaces:**
- Consumes: `bootstrap.ensure_runtime`, `bootstrap.configure_engine`, `bootstrap.installed_state`, `bootstrap.runtime_dir`; `Api._push(event, payload)` (`main.py:370`).
- Produces (JS-facing):
  - `Api.bootstrap_status(self) -> dict` — `{"ok":True, "ready": bool, "installed": {...}, "qemu_ok": bool, "qemu_hint": str|None, "error": str|None}`. `ready` is True iff `installed.json` has all manifest artifacts at matching sha256 **and** qemu present. Best-effort: on manifest fetch failure but assets present, returns `ready:True` with `"error"` set to the fetch note (offline-tolerant per spec).
  - `Api.bootstrap_start(self) -> dict` — launches `ensure_runtime` on a background thread, streaming `_push("bootstrap-progress", <progress dict>)` and finally `_push("bootstrap-done", {...})` or `_push("bootstrap-error", {"error":...})`. Returns `{"ok":True,"started":True}` immediately; guarded against concurrent starts.

- [ ] **Step 1: Write the failing tests** — `tests/test_bootstrap_api.py`. Use monkeypatch to stub `bootstrap`:

```python
import types, time
import main


def test_bootstrap_status_reports_ready(monkeypatch):
    monkeypatch.setattr(main.bootstrap, "read_manifest",
        lambda *a, **k: {"ok": True, "app": {"version": "1"},
            "artifacts": [{"name": "offset-arceus-arm", "sha256": "aa", "bytes": 1,
                           "url": "/omni/dist/blob/offset-arceus-arm", "dest": "images/arm"}]})
    monkeypatch.setattr(main.bootstrap, "installed_state",
        lambda rt: {"artifacts": {"offset-arceus-arm": {"sha256": "aa"}}, "app_version": "1"})
    monkeypatch.setattr(main.bootstrap, "configure_engine",
        lambda rt: {"qemu_ok": True, "qemu_hint": None, "images_dir": "x", "data_dir": "y"})
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: __import__("pathlib").Path("/tmp"))
    api = main.Api()
    st = api.bootstrap_status()
    assert st["ok"] and st["ready"] is True and st["qemu_ok"] is True


def test_bootstrap_start_streams_progress(monkeypatch):
    events = []
    def fake_ensure(base_url=None, progress=None):
        progress({"phase": "download", "artifact": "offset-arceus-arm", "percent": 50})
        return {"ok": True, "installed": {}, "changed": ["offset-arceus-arm"]}
    monkeypatch.setattr(main.bootstrap, "ensure_runtime", fake_ensure)
    monkeypatch.setattr(main.bootstrap, "configure_engine", lambda rt: {"qemu_ok": True, "qemu_hint": None})
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: __import__("pathlib").Path("/tmp"))
    api = main.Api()
    api._push = lambda event, payload=None: events.append((event, payload))
    api.bootstrap_start()
    time.sleep(0.3)
    kinds = [e for e, _ in events]
    assert "bootstrap-progress" in kinds and "bootstrap-done" in kinds
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd omni-executor && pytest tests/test_bootstrap_api.py -v`
Expected: FAIL — methods don’t exist. (Add `import bootstrap` at top of `main.py` if not present.)

- [ ] **Step 3: Implement the two methods** in `Api`, plus `import bootstrap` and a `self._bootstrapping = False` guard in `Api.__init__`:

```python
    def bootstrap_status(self):
        rt = bootstrap.runtime_dir()
        installed = bootstrap.installed_state(rt)
        eng = bootstrap.configure_engine(rt)
        error = None
        ready = False
        try:
            manifest = bootstrap.read_manifest(bootstrap.dist_base())
            have = installed.get("artifacts", {})
            ready = all(have.get(a["name"], {}).get("sha256") == a["sha256"]
                        for a in manifest.get("artifacts", []))
        except bootstrap.BootstrapError as e:
            error = str(e)
            ready = bool(installed.get("artifacts"))  # offline-tolerant
        ready = ready and eng.get("qemu_ok", False)
        return {"ok": True, "ready": ready, "installed": installed.get("artifacts", {}),
                "qemu_ok": eng.get("qemu_ok", False), "qemu_hint": eng.get("qemu_hint"),
                "error": error}

    def bootstrap_start(self):
        if self._bootstrapping:
            return {"ok": False, "error": "already running"}
        self._bootstrapping = True
        def _run():
            try:
                res = bootstrap.ensure_runtime(progress=lambda p: self._push("bootstrap-progress", p))
                bootstrap.configure_engine(bootstrap.runtime_dir())
                self._push("bootstrap-done", res)
            except Exception as e:  # noqa: BLE001 — surface any failure to the UI
                self._push("bootstrap-error", {"error": str(e)})
            finally:
                self._bootstrapping = False
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "started": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd omni-executor && pytest tests/test_bootstrap_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_bootstrap_api.py
git commit -m "feat: bootstrap_status/bootstrap_start bridge methods + progress events"
```

---

## Task 5: Frontend — `BootstrapView` + first-boot gate (omni-executor)

**Files:**
- Create: `frontend/src/components/BootstrapView.jsx`
- Modify: `frontend/src/App.jsx` (gate around the shell, `App.jsx:100-140`)
- Modify: `frontend/src/api.js` (add `bootstrapStatus`/`bootstrapStart` helpers)

**Interfaces:**
- Consumes: `api("bootstrap_status")`, `api("bootstrap_start")`, `onEngineEvent(listener)` (`api.js:77`) for `bootstrap-progress|done|error`.
- Produces: a full-screen first-boot component; `App` renders it while not `ready`.

**Note on tests:** the frontend has no unit-test runner (no vitest/jest configured). Per the existing codebase pattern, the testable deliverable here is **`npm run build` succeeds** and behavior is validated in the Task 7 e2e. Do not add a new test framework for one component.

- [ ] **Step 1: Add the api helpers** to `frontend/src/api.js` (mirroring `loadSettings`):

```javascript
export function bootstrapStatus() { return api("bootstrap_status"); }
export function bootstrapStart()  { return api("bootstrap_start"); }
```

- [ ] **Step 2: Create `frontend/src/components/BootstrapView.jsx`.** A progress screen: on mount calls `bootstrapStatus()`; if not ready, shows a "Set up Omni Executor" panel with a Start button (auto-start if `qemu_ok`), subscribes via `onEngineEvent` to `bootstrap-progress` (bar + current artifact + %), `bootstrap-done` (calls `onReady()`), `bootstrap-error` (shows the error + Retry). If `qemu_ok === false`, prominently render the `qemu_hint` (`brew install qemu …`) with a "Re-check" button (re-calls `bootstrapStatus`). Use the existing Tailwind classes/dark theme (match `SettingsView.jsx`). Full component:

```jsx
import { useEffect, useState, useCallback } from "react";
import { bootstrapStatus, bootstrapStart } from "../api";
import { onEngineEvent } from "../api";

export default function BootstrapView({ onReady }) {
  const [status, setStatus] = useState(null);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    const s = await bootstrapStatus();
    setStatus(s);
    if (s.ready) onReady?.();
    return s;
  }, [onReady]);

  useEffect(() => {
    let started = false;
    refresh().then((s) => {
      if (s && !s.ready && s.qemu_ok && !started) { started = true; bootstrapStart(); }
    });
    const off = onEngineEvent((event, payload) => {
      if (event === "bootstrap-progress") setProgress(payload);
      else if (event === "bootstrap-done") { setProgress(null); refresh(); }
      else if (event === "bootstrap-error") setError(payload?.error || "Setup failed");
    });
    return off;
  }, [refresh]);

  const pct = progress?.percent ? Math.round(progress.percent) : 0;
  return (
    <div className="flex h-screen w-screen flex-col items-center justify-center bg-[#0a0a0a] text-neutral-200 gap-6 p-10">
      <h1 className="text-2xl font-semibold">Setting up Omni Executor</h1>
      {status && !status.qemu_ok && (
        <div className="max-w-md rounded-lg border border-amber-600/40 bg-amber-900/10 p-4 text-sm">
          <p className="mb-2 font-medium text-amber-300">QEMU is required.</p>
          <code className="block rounded bg-black/40 px-2 py-1">{status.qemu_hint}</code>
          <button className="mt-3 rounded bg-neutral-700 px-3 py-1 hover:bg-neutral-600" onClick={refresh}>Re-check</button>
        </div>
      )}
      {status?.qemu_ok && !error && (
        <div className="w-full max-w-md">
          <div className="mb-2 flex justify-between text-xs text-neutral-400">
            <span>{progress?.artifact || "Preparing…"}</span><span>{pct}%</span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded bg-neutral-800">
            <div className="h-full bg-emerald-500 transition-all" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}
      {error && (
        <div className="max-w-md text-center">
          <p className="mb-3 text-sm text-red-400">{error}</p>
          <button className="rounded bg-neutral-700 px-4 py-1.5 hover:bg-neutral-600"
            onClick={() => { setError(null); bootstrapStart(); }}>Retry</button>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Gate the app in `App.jsx`.** Add near the top-level render (around `App.jsx:100`): a `ready` state defaulting to `null` (unknown). On mount, if `hasBackend()`, call `bootstrapStatus()` → set `ready`. While `ready === false`, render `<BootstrapView onReady={() => setReady(true)} />` instead of the shell. While `ready === null` render nothing/splash. When `ready === true` (or no backend — dev/browser), render the existing shell unchanged. Import `BootstrapView` and `bootstrapStatus`. Keep the existing tab shell (`App.jsx:100-140`) intact under the gate.

- [ ] **Step 4: Build to verify**

Run: `cd omni-executor/frontend && npm install && npm run build`
Expected: build succeeds, `frontend/dist/index.html` regenerated.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/BootstrapView.jsx frontend/src/App.jsx frontend/src/api.js frontend/dist
git commit -m "feat(ui): first-boot BootstrapView + app gate"
```

---

## Task 6: Packaging — `build-macos.sh` → `.app` → `.dmg` (omni-executor)

**Files:**
- Create: `OmniExecutor.spec` (PyInstaller)
- Create: `packaging/Info.plist`, `packaging/icon.icns` (a placeholder `.icns` is acceptable v1; document how to replace)
- Create: `build-macos.sh`
- Modify: `requirements.txt` (add `pyinstaller>=6.0`; note `create-dmg` is a brew dep)

**Interfaces:**
- Consumes: the frozen `--omnidroid` dispatch (Task 3), `frontend/dist` (Task 5), the sibling `omnidroid` package.
- Produces: `dist/Omni Executor.app` and `dist/Omni Executor.dmg`.

- [ ] **Step 1: Write `OmniExecutor.spec`.** One-file-per-app (`.app` bundle) PyInstaller spec: entry `main.py`; `datas` include `('frontend/dist', 'frontend/dist')`; `hiddenimports`/`datas` include the sibling `omnidroid` package so `from omnidroid.cli import main` resolves in-binary (add the `omnidroid` repo root to `pathex`, and collect it: `datas += collect_data_files('omnidroid')`, `hiddenimports += collect_submodules('omnidroid')`). `BUNDLE(... name='Omni Executor.app', info_plist='packaging/Info.plist', icon='packaging/icon.icns')`, `console=False`.

- [ ] **Step 2: Write `packaging/Info.plist`** — `CFBundleName=Omni Executor`, `CFBundleIdentifier=com.omniapps.executor`, `CFBundleShortVersionString=1.0.0`, `NSHighResolutionCapable=true`, `LSMinimumSystemVersion=12.0`.

- [ ] **Step 3: Write `build-macos.sh`** (executable):

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "==> building frontend"
( cd frontend && npm install && npm run build )
echo "==> freezing app (PyInstaller)"
python -m PyInstaller --noconfirm OmniExecutor.spec
echo "==> ad-hoc signing"
codesign --force --deep --sign - "dist/Omni Executor.app"
echo "==> building dmg"
command -v create-dmg >/dev/null || { echo "install: brew install create-dmg"; exit 1; }
rm -f "dist/Omni Executor.dmg"
create-dmg --volname "Omni Executor" --app-drop-link 480 200 \
  --icon "Omni Executor.app" 160 200 \
  "dist/Omni Executor.dmg" "dist/Omni Executor.app"
echo "==> done: dist/Omni Executor.dmg"
```

- [ ] **Step 4: Build and smoke-test the frozen engine dispatch**

Run:
```bash
cd omni-executor && ./build-macos.sh
"dist/Omni Executor.app/Contents/MacOS/OmniExecutor" --omnidroid version --json
```
Expected: `.app` and `.dmg` produced; the `--omnidroid version --json` prints the engine’s version JSON (proves the bundled engine works). This is the task’s acceptance.

- [ ] **Step 5: Commit**

```bash
git add OmniExecutor.spec packaging/ build-macos.sh requirements.txt
git commit -m "feat(packaging): build-macos.sh -> .app (bundled engine) -> .dmg"
```

---

## Task 7: End-to-end acceptance on this Mac (omni-executor)

**Files:**
- Create: `scripts/e2e-macos.sh`
- Create: `docs/superpowers/e2e-macos-checklist.md`

**Interfaces:**
- Consumes: everything above + the live VPS manifest (with `dest_name` from Task 1 deployed).

This task is a scripted/manual verification, not TDD. It is the slice’s real acceptance.

- [ ] **Step 1: Fresh-runtime pull.** Remove any existing runtime dir (`rm -rf ~/Library/Application\ Support/OmniExec`), then run the frozen app; confirm BootstrapView appears, downloads `base-arm` (3.88 GB) + `offset-arceus-arm` (348 MB) from `72.62.59.232` with a progress bar, verifies sha256, and writes `installed.json`. Script the headless half in `scripts/e2e-macos.sh`: `python -c "import bootstrap; print(bootstrap.ensure_runtime())"` with a real `OMNIEXEC_RUNTIME_DIR` pointed at a scratch dir, timing the pull.

- [ ] **Step 2: Engine sees the assets.** `OMNI_DATA_DIR=<rt> OMNI_IMAGES_DIR=<rt>/images python -m omnidroid bases --json` lists the arm base; `omnidroid offset list` shows the arceus offset. (configure_engine having written `paths.json`.)

- [ ] **Step 3: Boot + arceus + exec bridge.** Launch the app normally; start an instance (`engine_start`), confirm arceus loads the OMNI-EXEC UI (custom UI served by omni-backend), then run a script through the Editor tab and confirm the exec bridge returns output. Capture a **screenshot** of the running executor.

- [ ] **Step 4: Idempotent relaunch.** Quit and relaunch; confirm the app skips straight to the normal GUI (manifest check finds nothing changed, no re-download).

- [ ] **Step 5: Write the checklist doc** capturing the exact commands, expected outputs, timings, and the screenshot path, so the run is reproducible.

- [ ] **Step 6: Commit**

```bash
git add scripts/e2e-macos.sh docs/superpowers/e2e-macos-checklist.md
git commit -m "test(e2e): macOS self-install end-to-end checklist + script"
```

---

## Self-Review notes (author)

- **Spec coverage:** manifest/blob consumption (Task 2) ✅; resumable+verified+idempotent (Task 2 tests) ✅; disk precheck (Task 2 `_precheck_space`) ✅; runtime layout + `installed.json` + `paths.json` + engine env (Tasks 2–3) ✅; BootstrapView + gate (Task 5) ✅; PyInstaller `--omnidroid` + `.app`/`.dmg` + ad-hoc sign (Task 6) ✅; e2e incl. exec bridge + screenshot (Task 7) ✅; offline-tolerant status (Task 4) ✅; QEMU v1 = brew fallback with detection+hint (Global Constraints, Tasks 3–5) ✅; the stale `manager.py` engine path fixed (Task 3) ✅.
- **Deliberately deferred (spec-approved):** portable bundled QEMU, notarization/Developer-ID signing, Windows slice C, CDN switch (server-side only).
- **The one manual step on a fresh Mac:** `brew install qemu android-platform-tools`. Surfaced in BootstrapView, not hidden. If the user wants true zero-touch, add a `qemu-mac-arm64` artifact task (build a relocatable qemu, register the blob) — a clean follow-up that touches only Task 1’s registry + Task 2’s placement.
- **Type consistency:** `ensure_runtime`, `configure_engine`, `runtime_dir`, `installed_state`, `plan_downloads`, `download_blob`, `place_artifact` names are used identically across Tasks 2–4 and 7. Bridge events `bootstrap-progress|done|error` match between Task 4 (`_push`) and Task 5 (`onEngineEvent`).
- **Cross-repo:** Task 1 is omni-backend (its own branch + deploy); Tasks 2–7 are omni-executor. The SDD ledger lives in the omni-executor workspace; Task 1’s commit lands on the omni-backend branch.
