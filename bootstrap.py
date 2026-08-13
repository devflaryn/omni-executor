"""Headless first-boot runtime installer for Omni Executor (macOS).

Fetches the named-blob manifest, downloads + sha256-verifies each artifact
with HTTP Range resume, places it under the OmniExec runtime dir, and records
installed.json. No GUI, no engine import — pure stdlib so it is unit-testable.
"""
import hashlib, json, os, shutil, sys, tarfile, time, urllib.request, urllib.error
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
    last_exc = None
    hashed_mismatch = False
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
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            time.sleep(1); continue  # resume next attempt
        if _hash_file(tmp_path) == want:
            return
        hashed_mismatch = True
        last_exc = None
        tmp_path.unlink(missing_ok=True)  # bad content, redownload from scratch
    if hashed_mismatch:
        raise BootstrapError(f"{artifact['name']}: sha256 mismatch after {_MAX_RETRIES} attempts")
    raise BootstrapError(
        f"{artifact['name']}: download failed after {_MAX_RETRIES} attempts "
        f"(no successful transfer to verify): {last_exc}"
    ) from last_exc


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
        if m.issym() or m.islnk():
            raise BootstrapError(f"unsafe tar member (link): {m.name}")
        target = (dest / m.name).resolve()
        if not (target == dest or dest in target.parents):
            raise BootstrapError(f"unsafe tar member: {m.name}")
    try:
        tf.extractall(dest, filter="data")
    except TypeError:
        # very old interpreters (< 3.12) without the `filter` kwarg; the
        # manual link/containment guard above already vetted every member.
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
