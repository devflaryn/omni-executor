"""Headless first-boot runtime installer for Omni Executor (macOS).

Fetches the named-blob manifest, downloads + sha256-verifies each artifact
with HTTP Range resume, places it under the OmniExec runtime dir, and records
installed.json. No GUI, no engine import — pure stdlib so it is unit-testable.
"""
import hashlib, json, os, re, shutil, subprocess, sys, tarfile, time, urllib.request, urllib.error, zipfile
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

# ---- x86 (Windows) ----
#
# NOT the arm entry with different filenames. An x86-bliss base boots its
# system disk directly with -kernel/-initrd and no UEFI, so it carries
# disk/kernel/initrd (never system/data/efivars) plus a `src` kernel argument,
# and its per-instance /data is seeded from a shared template recorded at the
# TOP level of the config as "data_template". Names are verbatim from
# omnidroid/bases.py; `src` is its DEFAULT_SRC. qemu_proc.qemu_command()
# indexes base["src"] directly when it builds the x86 command line, so an
# entry without one cannot boot.
_X86_DIR = "x86"
_X86_DISK = "base_x86.qcow2"
_X86_KERNEL = "base_x86.kernel"
_X86_INITRD_CANDIDATES = ("base_x86_rooted.initrd.img", "base_x86.initrd.img")
_X86_DATA_TEMPLATE = "data-template-8g.qcow2"
_X86_SRC = "/android-2024-10-11"
# Dots are legal in an offset name ("2.740.101" is the name a human wants) —
# see omnidroid/offsets.py OFFSET_NAME_RE.
_X86_OFFSET_RE = re.compile(
    r"^base_x86_data_offset_([A-Za-z0-9][A-Za-z0-9._-]*)\.qcow2$")

# omnidroid's ensure_qemu() silently does nothing unless qemu.download_url is
# set (its DEFAULT_QEMU_URL is None). On macOS QEMU is a brew install away; on
# Windows nothing else will ever put it there, so the client has to supply the
# installer URL or the engine can never self-install. Overridable for a
# pinned/mirrored build.
#
# Resolved through the dist API's `qemu-win` entry, which 302s to the real
# installer. weilnetz publishes per-date builds and PRUNES old ones (the first
# URL pinned here was already dead), so the indirection is the point: a rotted
# installer becomes a one-line registry edit on the server instead of a client
# release. omnidroid's ensure_qemu() fetches with urllib, which follows the
# redirect. OMNI_QEMU_WIN_URL overrides for an air-gapped/mirrored build.
_QEMU_WIN_BLOB = "/omni/dist/blob/qemu-win"


def qemu_win_url() -> str:
    return os.environ.get("OMNI_QEMU_WIN_URL") or f"{dist_base()}{_QEMU_WIN_BLOB}"


class BootstrapError(Exception):
    pass


def dist_base() -> str:
    return os.environ.get("OMNI_EXEC_BASE", "http://72.62.59.232").rstrip("/")


def current_os() -> str:
    """The dist API's name for this host: "win" | "mac".

    Every manifest request goes through this. The base a machine downloads
    MUST match its architecture — a Windows box asking for the mac manifest
    would fetch the arm64 base and its arm offset, neither of which it can
    boot — so this is never hardcoded at a call site."""
    return "win" if sys.platform == "win32" else "mac"


def runtime_dir() -> Path:
    override = os.environ.get("OMNIEXEC_RUNTIME_DIR")
    if override:
        p = Path(override)
    elif sys.platform == "win32":
        # Multi-gigabyte base images are machine-local state, not roaming
        # profile data, so LOCALAPPDATA is the correct home; APPDATA is only
        # a fallback for a profile that somehow lacks it.
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        p = Path(root) / APP_DIR_NAME if root else \
            Path.home() / "AppData" / "Local" / APP_DIR_NAME
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


def read_manifest(base_url: str, os_name: str = None, channel: str = "stable") -> dict:
    os_name = os_name or current_os()
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
    """Which artifacts still need fetching.

    Artifacts with NO sha256 are POINTERS, not payloads — a registry entry
    served as a 302 (`qemu-win`) carries a redirect instead of a stored blob,
    so the manifest reports `sha256: null` and there is nothing to verify.
    They are skipped entirely: whoever consumes them (omnidroid's
    ensure_qemu(), via qemu.download_url) fetches them itself.

    Planning them was a real first-boot failure — download_blob compared the
    downloaded bytes against None, which never matches, so the install pulled
    197 MB and then died with "qemu-win: sha256 mismatch after 3 attempts".

    Artifacts with `kind: "app"` are skipped too, for a different reason: they
    are builds of THIS APP. Downloading one during first boot would fetch
    another ~86 MB copy of the program already running, and placing it would
    put a second app inside the runtime dir where nothing looks for it. The
    updater (updates.py) asks for it by name when there is actually a newer
    version to install.
    """
    have = installed.get("artifacts", {})
    out = []
    for a in manifest.get("artifacts", []):
        if not a.get("sha256"):
            continue
        if a.get("kind") == "app":
            continue
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
    want = artifact.get("sha256")
    if not want:
        # Refuse rather than "verify" against None, which reports a hash
        # mismatch and blames the network for a manifest problem.
        raise BootstrapError(
            f"{artifact.get('name')}: no sha256 in the manifest, so it cannot "
            f"be verified. Redirect/pointer artifacts are not downloadable "
            f"blobs — see plan_downloads.")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    last_exc = None
    hashed_mismatch = False
    for attempt in range(1, _MAX_RETRIES + 1):
        have = tmp_path.stat().st_size if tmp_path.exists() else 0
        if have > total:  # corrupt partial
            _discard(tmp_path); have = 0
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
        _discard(tmp_path)   # bad content, redownload from scratch
    if hashed_mismatch:
        raise BootstrapError(f"{artifact['name']}: sha256 mismatch after {_MAX_RETRIES} attempts")
    raise BootstrapError(
        f"{artifact['name']}: download failed after {_MAX_RETRIES} attempts "
        f"(no successful transfer to verify): {last_exc}"
    ) from last_exc


def _discard(path: Path) -> None:
    """Throw away a partial download, without turning a retryable problem into
    a crash.

    On Windows an open handle makes unlink raise PermissionError (WinError 32),
    and this is called from the RECOVERY path — a hash mismatch, which is
    exactly when the retry matters. Losing the whole multi-gigabyte download to
    a traceback about deleting the scratch file is the worst possible trade, so
    truncating in place is an acceptable second best: the next attempt resumes
    from zero either way.
    """
    try:
        path.unlink(missing_ok=True)
        return
    except OSError:
        pass
    try:
        with open(path, "wb"):
            pass
    except OSError as e:
        raise BootstrapError(
            f"could not discard the partial download at {path}: {e}. Another "
            f"copy of the app may be updating at the same time.") from e


def place_artifact(artifact: dict, tmp_path: Path, rt: Path) -> None:
    dest_dir = rt / artifact["dest"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    unpack = artifact.get("unpack")
    if unpack in ("tar", "tar.gz", "tgz"):
        mode = "r:gz" if unpack != "tar" else "r:"
        with tarfile.open(tmp_path, mode) as tf:
            _safe_extract(tf, dest_dir)
        tmp_path.unlink(missing_ok=True)
    elif unpack == "zip":
        # App builds ship as zips: it is what Compress-Archive produces on
        # Windows and what preserves an .app bundle's layout on macOS.
        with zipfile.ZipFile(tmp_path) as zf:
            _safe_extract_zip(zf, dest_dir)
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


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """Same containment rule as _safe_extract, for zips.

    Zip has no link members to reject, but it very much has `../` traversal in
    entry names, and ZipFile.extract's own sanitising is not something to rely
    on when the archive replaces an executable directory.
    """
    dest = dest.resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if not (target == dest or dest in target.parents):
            raise BootstrapError(f"unsafe zip member: {info.filename}")
    zf.extractall(dest)
    # Zip does not carry the executable bit on every toolchain (PowerShell's
    # Compress-Archive drops POSIX modes entirely), so a macOS bundle unpacked
    # from one would have a non-executable binary inside it.
    if sys.platform != "win32":
        for info in zf.infolist():
            mode = info.external_attr >> 16
            if mode:
                try:
                    os.chmod(dest / info.filename, mode)
                except OSError:
                    pass


def _precheck_space(rt: Path, plan: list) -> None:
    need = int(sum(a.get("bytes") or 0 for a in plan) * 1.1)
    free = shutil.disk_usage(rt).free
    if free < need:
        raise BootstrapError(f"insufficient disk: need ~{need // 2**30}GB, free {free // 2**30}GB")


def _write_installed(rt: Path, installed: dict, manifest: dict = None) -> None:
    """Atomically persist the install receipt (tmp file + replace), so a crash
    mid-write can never leave a truncated installed.json that would be
    unparseable and silently re-download everything."""
    if manifest is not None:
        installed.setdefault("app_version",
                             manifest.get("app", {}).get("version"))
    tmpf = _installed_file(rt).with_suffix(".tmp")
    tmpf.write_text(json.dumps(installed, indent=2))
    tmpf.replace(_installed_file(rt))


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
        # Record after EVERY artifact, not once at the end. These are
        # multi-gigabyte downloads: writing the receipt only after the whole
        # plan succeeded meant one failure at the tail threw away the record
        # of everything already on disk, and the next launch re-downloaded
        # all of it.
        _write_installed(rt, installed, manifest)
    installed["app_version"] = manifest.get("app", {}).get("version")
    _write_installed(rt, installed, manifest)
    shutil.rmtree(staging, ignore_errors=True)
    if progress:
        progress({"phase": "done", "artifact": None, "received": 0, "total": 0, "percent": 100})
    return {"ok": True, "installed": installed["artifacts"], "changed": changed}


def _first_present(arm_dir: Path, names) -> str | None:
    for name in names:
        if (arm_dir / name).exists():
            return name
    return None


def _register_x86_base(cfg: dict, images: Path) -> None:
    """Register the downloaded x86 Bliss base (+ any baked offsets) into cfg.

    Mirrors omnidroid's own autoregister_bases() x86 branch: the base is
    complete only when the disk + kernel + initrd triple is all present, the
    ROOTED initrd is preferred when it has been built, and every recorded
    filename carries its "x86/" arch subfolder because an offset must resolve
    beside the /data template it overlays.

    A base-less cfg is not an error — on a genuinely fresh first boot nothing
    has been downloaded yet, and ensure_runtime() calls this again afterwards.
    """
    x86_dir = images / _X86_DIR
    initrd = _first_present(x86_dir, _X86_INITRD_CANDIDATES)
    if not ((x86_dir / _X86_DISK).exists()
            and (x86_dir / _X86_KERNEL).exists() and initrd):
        return

    base = {
        "type": "x86-bliss",
        "disk": f"{_X86_DIR}/{_X86_DISK}",
        "kernel": f"{_X86_DIR}/{_X86_KERNEL}",
        "initrd": f"{_X86_DIR}/{initrd}",
        "rooted": initrd.startswith("base_x86_rooted"),
        "src": _X86_SRC,
    }

    offsets = {}
    for f in sorted(x86_dir.iterdir()):
        m = _X86_OFFSET_RE.match(f.name)
        if m:
            offsets[m.group(1)] = {"data": f"{_X86_DIR}/{f.name}"}
    if offsets:
        base["offsets"] = offsets
        # Exactly one baked version is unambiguously what a bare launch
        # means; with two or more, guessing is how you debug the wrong
        # Roblox for an hour (see omnidroid/offsets.py default_offset_name).
        if len(offsets) == 1:
            base["default_offset"] = next(iter(offsets))

    cfg["bases"]["x86"] = base
    cfg["current_base"] = "x86"


def _qemu_system_name() -> str:
    """The QEMU emulator THIS host needs: x86_64 for the Bliss base on
    Windows, aarch64 for the arm64 base on macOS."""
    return "qemu-system-x86_64" if current_os() == "win" \
        else "qemu-system-aarch64"


def _qemu_hint() -> str:
    """What the user should do when no QEMU is on PATH. Windows never says
    'brew': there the engine downloads and silently installs QEMU itself
    (see DEFAULT_QEMU_WIN_URL), so this is a status line, not a chore."""
    if current_os() == "win":
        return ("QEMU is not installed yet — Omni Executor will download and "
                "install it automatically on first run.")
    return "brew install qemu android-platform-tools"


_WHPX_HINT = (
    "Windows Hypervisor Platform is turned off, so the Android VM cannot "
    "start. Enable it in an ADMINISTRATOR PowerShell:\n\n"
    "    DISM /Online /Enable-Feature /FeatureName:HypervisorPlatform /All\n\n"
    "then reboot. (Windows Features > \"Windows Hypervisor Platform\" does "
    "the same thing.)")

_WHPX_NO_QEMU_HINT = (
    "Virtualization support has not been checked yet — QEMU is still being "
    "installed.")

# WHPX state cannot change without a reboot, so probe once per process.
_whpx_cache = {}


def _whpx_probe(qemu: str, timeout: float = 6.0):
    """True/False/None — can QEMU actually initialize the WHPX accelerator?

    Starts a tiny VM PAUSED (-S). QEMU brings the accelerator up during
    startup, so a process that is still alive after the timeout has already
    proved WHPX works; one that exits immediately complaining about whpx has
    proved it does not. Anything else (a missing BIOS, a broken install) is
    NOT evidence about WHPX and must stay unknown rather than be reported as
    "virtualization is off"."""
    proc = subprocess.Popen(
        [qemu, "-accel", "whpx", "-machine", "q35", "-m", "32",
         "-display", "none", "-nodefaults", "-no-user-config", "-S"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()                      # never leave a probe VM behind
        return True
    err = (err or "").lower()
    if "whpx" in err or "hypervisor" in err:
        return False
    return None


def windows_accel_status(probe=None) -> dict:
    """{"os", "whpx_ok", "hint"} — is this host able to run the VM at all?

    `whpx_ok` is TRI-STATE: True (works), False (the feature is off, and
    `hint` says exactly how to turn it on), or None (not determinable yet,
    e.g. QEMU is not installed). None must not be rendered as a failure: a
    scary "virtualization is off" panel on a machine that is actually fine is
    worse than saying nothing.

    A no-op returning ok on every non-Windows host."""
    if current_os() != "win":
        return {"os": current_os(), "whpx_ok": True, "hint": None}
    if "win" in _whpx_cache:
        return dict(_whpx_cache["win"])

    qemu = shutil.which(_qemu_system_name())
    if not qemu:
        # Deliberately NOT cached: QEMU is about to be installed, and the
        # next call should get a real answer.
        return {"os": "win", "whpx_ok": None, "hint": _WHPX_NO_QEMU_HINT}
    try:
        ok = (probe or _whpx_probe)(qemu)
    except Exception:  # noqa: BLE001 — a probe failure is never fatal
        ok = None
    status = {"os": "win", "whpx_ok": ok,
              "hint": _WHPX_HINT if ok is False else None}
    if ok is not None:
        _whpx_cache["win"] = dict(status)
    return status


def engine_ready(rt: Path) -> dict:
    """Read-only engine readiness probe — no disk write, no env mutation.
    Mirrors configure_engine's qemu detection so bootstrap_status can poll cheaply."""
    qemu_bin = shutil.which(_qemu_system_name())
    return {"qemu_ok": bool(qemu_bin),
            "qemu_hint": None if qemu_bin else _qemu_hint()}


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
    # Frozen (PyInstaller) omnidroid subprocesses compute their default
    # CONFIG_PATH as <exe-dir>/configs/paths.json, which is NOT the
    # rt/paths.json this function writes below -- without this override the
    # frozen engine can never see the arm base we just registered and can't
    # boot. run_engine() spawns the --omnidroid subprocess with env=None
    # (inherits this process's env), so setting it here reaches the engine.
    os.environ["OMNIDROID_CONFIG_PATH"] = str(rt / "paths.json")
    # The engine re-invokes the FROZEN BINARY for its detached children (the
    # VNC viewer, the autocap recorder) as `[sys.executable, "<subcommand>"]`.
    # That is right for the standalone omnidroid.exe, whose entry point is the
    # engine CLI -- but this binary's entry point is the GUI, which routes to
    # the engine only when argv[1] is "--omnidroid". Without this, clicking
    # "Open viewer" ran `omni-exec.exe _vncview ...` and launched a SECOND
    # COPY OF THE APP instead of the viewer.
    os.environ["OMNIDROID_SELF_ARGV"] = "--omnidroid"

    qemu_path = shutil.which(_qemu_system_name())
    win = current_os() == "win"

    cfg = {
        "images_dir": str(images),
        "current_base": None,      # set below once a base is detected
        "data_template": (f"{_X86_DIR}/{_X86_DATA_TEMPLATE}" if win
                          else "data-template-8g.qcow2"),
        "bases": {},
        "qemu": {"mem_mb": 4096, "smp": 4, "data_disk_size": "8G",
                 "adb_port_start": 16001, "qmp_port_start": 17001,
                 "vnc_port_start": 18001},
    }
    if qemu_path:
        cfg["qemu"]["dir"] = str(Path(qemu_path).parent)
    if win:
        # Without this omnidroid's ensure_qemu() is a silent no-op and a
        # machine with no QEMU can never acquire one.
        cfg["qemu"]["download_url"] = qemu_win_url()

    if win:
        _register_x86_base(cfg, images)
        (rt / "paths.json").write_text(json.dumps(cfg, indent=2))
        return {
            "images_dir": str(images),
            "data_dir": str(rt),
            "qemu_ok": bool(qemu_path),
            "qemu_hint": None if qemu_path else _qemu_hint(),
        }

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
