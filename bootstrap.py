"""Headless first-boot runtime installer for Omni Executor (macOS).

Fetches the named-blob manifest, downloads + sha256-verifies each artifact
with HTTP Range resume, places it under the OmniExec runtime dir, and records
installed.json. No GUI, no engine import — pure stdlib so it is unit-testable.
"""
import hashlib, json, os, re, shutil, sys, tarfile, time, urllib.request, urllib.error
from pathlib import Path

APP_DIR_NAME = "OmniExec"
MANIFEST_PATH = "/omni/dist/manifest"
_CHUNK = 1 << 20
_MAX_RETRIES = 3

# ---------------------------------------------------------- engine wiring
#
# Mirrors omnidroid's own arm-base schema (omnidroid/bases.py, offsets.py)
# and the proven-working dev configs/paths.json: bases + their offsets live
# in the "arm/" arch subfolder, filenames are recorded RELATIVE to
# images_dir (so "arm/<file>" — not bare filenames), and base_disk is
# optional (the current shipped lineage is a standalone rooted system+data
# pair with no separate pristine backing disk).
_ARM_SYSTEM_CANDIDATES = ("base_arm_system_rooted.qcow2", "base_arm_system.qcow2")
_ARM_DATA_CANDIDATES = ("base_arm_data_rooted.qcow2", "base_arm_data.qcow2")
_ARM_EFIVARS = "base_arm_efivars.fd"
_ARM_BASE_DISK = "base_arm.qcow2"
_ARM_OFFSET_RE = re.compile(r"^base_arm_data_offset_([A-Za-z0-9_-]+)\.qcow2$")


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


def _first_present(arm_dir: Path, names) -> str | None:
    for name in names:
        if (arm_dir / name).exists():
            return name
    return None


def configure_engine(rt: Path) -> dict:
    """Point the bundled omnidroid engine at the assets ensure_runtime just
    downloaded, and write a paths.json in the schema omnidroid's loader
    (engine.py load_config / bases.py autoregister_bases+base_missing_files)
    expects.

    Sets OMNI_DATA_DIR / OMNI_IMAGES_DIR (the env overrides omnidroid.config
    honors), then writes rt/paths.json: the qemu block, and — when the
    downloaded assets are present under rt/images/arm/ — an "arm" base entry
    (system + data, optionally efivars/base_disk) plus any offsets found
    there (base_arm_data_offset_<name>.qcow2), with default_offset set when
    exactly one is present. Detects qemu-system-aarch64 via shutil.which.

    Returns {"images_dir", "data_dir", "qemu_ok", "qemu_hint"}.
    """
    images = rt / "images"
    arm_dir = images / "arm"
    os.environ["OMNI_DATA_DIR"] = str(rt)
    os.environ["OMNI_IMAGES_DIR"] = str(images)

    qemu_path = shutil.which("qemu-system-aarch64")

    cfg = {
        "images_dir": str(images),
        "current_base": None,      # set below once an arm base is detected
        "data_template": "data-template-8g.qcow2",
        "bases": {},
        "qemu": {"mem_mb": 4096, "smp": 4, "data_disk_size": "8G",
                 "adb_port_start": 16001, "qmp_port_start": 17001,
                 "vnc_port_start": 18001},
    }
    if qemu_path:
        cfg["qemu"]["dir"] = str(Path(qemu_path).parent)

    system_name = _first_present(arm_dir, _ARM_SYSTEM_CANDIDATES)
    data_name = _first_present(arm_dir, _ARM_DATA_CANDIDATES)
    if system_name and data_name:
        base = {
            "type": "arm-uefi",
            "system": f"arm/{system_name}",
            "data": f"arm/{data_name}",
            "rooted": "rooted" in system_name and "rooted" in data_name,
        }
        if (arm_dir / _ARM_EFIVARS).exists():
            base["efivars"] = f"arm/{_ARM_EFIVARS}"
        if (images / _ARM_BASE_DISK).exists():
            base["base_disk"] = _ARM_BASE_DISK

        offsets = {}
        if arm_dir.is_dir():
            for f in sorted(arm_dir.iterdir()):
                m = _ARM_OFFSET_RE.match(f.name)
                if m:
                    offsets[m.group(1)] = {"data": f"arm/{f.name}"}
        if offsets:
            base["offsets"] = offsets
            if len(offsets) == 1:
                base["default_offset"] = next(iter(offsets))

        cfg["bases"]["arm"] = base
        cfg["current_base"] = "arm"

    (rt / "paths.json").write_text(json.dumps(cfg, indent=2))
    return {
        "images_dir": str(images),
        "data_dir": str(rt),
        "qemu_ok": bool(qemu_path),
        "qemu_hint": None if qemu_path else "brew install qemu android-platform-tools",
    }
