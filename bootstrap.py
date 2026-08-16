"""Headless first-boot runtime installer for Omni Executor.

Fetches the named-blob manifest, downloads + sha256-verifies each artifact
with HTTP Range resume, places it under the OmniExec runtime dir, and records
installed.json. Also installs the host TOOLS the engine shells out to (QEMU
and adb on Windows) so a fresh machine needs nothing preinstalled. No GUI, no
engine import — pure stdlib so it is unit-testable.
"""
import ctypes, hashlib, json, os, re, shutil, subprocess, sys, tarfile, time, urllib.request, urllib.error, zipfile
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

# adb has the same problem and no equivalent excuse: omnidroid/adb.py shells
# the BARE NAME "adb", so on a machine without Android platform-tools every
# guest command fails at CreateProcess. Resolved through the dist API for the
# same reason as QEMU — a moved/rotted URL is a server-side edit, not a client
# release. `adb-win` is a redirect entry (no sha256), so plan_downloads skips
# it and this module fetches it directly.
_ADB_WIN_BLOB = "/omni/dist/blob/adb-win"
# Google's "latest" alias, not a dated build: it is the one platform-tools URL
# that has never moved. Used only if the dist API has no adb-win entry yet, so
# a client shipped before the registry edit still installs.
_ADB_WIN_FALLBACK = ("https://dl.google.com/android/repository/"
                     "platform-tools-latest-windows.zip")


def qemu_win_url() -> str:
    return os.environ.get("OMNI_QEMU_WIN_URL") or f"{dist_base()}{_QEMU_WIN_BLOB}"


def adb_win_url() -> str:
    return os.environ.get("OMNI_ADB_WIN_URL") or f"{dist_base()}{_ADB_WIN_BLOB}"


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

    `kind: "tool"` is skipped for the same shape of reason: those are HOST
    tools (the portable QEMU), owned by ensure_tools(), which installs one only
    when the machine does not already have that tool. Leaving it in the plan
    would download it a second time on a fresh machine — and, far worse, would
    make every machine that legitimately skipped it (because QEMU was already
    installed) look permanently un-ready, since readiness is "the plan is
    empty". That is exactly the regression an `app` artifact caused the first
    time one was published.
    """
    have = installed.get("artifacts", {})
    out = []
    for a in manifest.get("artifacts", []):
        if not a.get("sha256"):
            continue
        if a.get("kind") in ("app", "tool"):
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


# ------------------------------------------------------------------ tools
#
# The engine SHELLS OUT to two host programs it does not bundle: QEMU
# (qemu-system-x86_64 / qemu-img / qemu-io) and adb. On a developer box both
# are already on PATH, which is exactly why their absence never showed up
# here — a genuinely fresh machine has neither, and the app dead-ended on the
# setup screen forever.
#
# WHERE they get installed matters as much as that they do:
#
#   * NOT next to the exe. omnidroid's QEMU_DIR is <exe dir>/qemu, and the
#     app's own updater REPLACES that whole directory (updates.py stages the
#     new build and renames the old tree aside) — so a QEMU installed there is
#     destroyed by every app update and re-downloaded, ~200 MB at a time. It is
#     also under Program Files for anyone who installs there, i.e. unwritable.
#   * The runtime dir instead (%LOCALAPPDATA%\OmniExec\...): always writable by
#     the user with NO elevation, untouched by app updates, and beside the
#     images it exists to boot.
#
# The engine finds them because configure_engine() writes the resolved QEMU
# directory into paths.json as `qemu.dir` — which omnidroid's qemu_bin()
# already consults FIRST — and prepends the adb directory to PATH, which the
# engine subprocess inherits (run_engine spawns with env=None).
_QEMU_SUBDIR = "qemu"
_ADB_SUBDIR = "platform-tools"
_ADB_ARTIFACT = "adb-win"
# Everything the engine actually invokes (grep qemu_bin( in omnidroid/engine.py).
# qemu-io is used by the rooted-system baker; a QEMU install missing it is not
# a complete one.
_QEMU_TOOLS = ("qemu-system-x86_64", "qemu-img", "qemu-io")
# A portable QEMU zip WE host, if one has been published. Preferred over the
# vendor installer because it needs no elevation at all: the weilnetz NSIS
# installer is manifested requireAdministrator, so CreateProcess on it fails
# outright with WinError 740 unless the call is elevated. Optional by design —
# when the manifest has no such artifact the elevated installer path below is
# used instead, so this is an optimization, never a requirement.
_QEMU_PORTABLE_ARTIFACT = "qemu-portable-win"
# ...and it lives in its OWN CHANNEL, not "stable".
#
# It is a sha256'd artifact with a dest, so to any client older than the
# `kind: "tool"` rule below it is indistinguishable from a base image: it would
# land in the first-boot download plan, be fetched a second time, and — far
# worse — make every already-installed machine report un-ready forever, because
# readiness is "the plan is empty". Shipping it in `stable` would therefore
# break every 1.0.8/1.0.9 client the moment it went live. A separate channel
# means those clients never see it at all.
_TOOLS_CHANNEL = "tools"


def qemu_dir(rt: Path) -> Path:
    return rt / _QEMU_SUBDIR


def adb_dir(rt: Path) -> Path:
    return rt / _ADB_SUBDIR


def _exe(name: str) -> str:
    return name + (".exe" if sys.platform == "win32" else "")


def _looks_like_qemu_dir(d: Path) -> bool:
    """A directory holding a COMPLETE QEMU for this host.

    Every tool is required, not just the system emulator: a half-extracted or
    part-pruned directory that has qemu-system-x86_64.exe but no qemu-img.exe
    would satisfy a naive check and then fail later, at the point where an
    account's overlay disk is created — far from the cause."""
    if not d or not d.is_dir():
        return False
    names = [_qemu_system_name()] + [t for t in _QEMU_TOOLS
                                     if not t.startswith("qemu-system")]
    return all((d / _exe(n)).exists() for n in names)


def find_qemu(rt: Path) -> Path | None:
    """The directory containing a usable QEMU, or None.

    Ordered so an install we control wins over one we merely found, and a
    machine that already has QEMU is never made to download 200 MB of it
    again. Mirrors omnidroid's qemu_bin() resolution so the app's idea of
    "QEMU is installed" cannot disagree with the engine's — they DID disagree:
    engine_ready() used shutil.which (PATH only) while qemu_bin() on Windows
    deliberately never consults PATH, so each was capable of reporting ready
    when the other could not run."""
    env = os.environ.get("OMNI_QEMU_DIR")
    if env and _looks_like_qemu_dir(Path(env)):
        return Path(env)
    if _looks_like_qemu_dir(qemu_dir(rt)):
        return qemu_dir(rt)
    on_path = shutil.which(_qemu_system_name())
    if on_path and _looks_like_qemu_dir(Path(on_path).parent):
        return Path(on_path).parent
    if sys.platform == "win32":
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            cand = Path(root) / "qemu"
            if _looks_like_qemu_dir(cand):
                return cand
    return None


def find_adb(rt: Path) -> Path | None:
    """The directory containing adb, or None. Same ordering rationale as
    find_qemu. omnidroid/adb.py runs the BARE NAME "adb", so whatever this
    returns has to end up on PATH — see configure_engine()."""
    env = os.environ.get("OMNI_ADB_DIR")
    if env and (Path(env) / _exe("adb")).exists():
        return Path(env)
    if (adb_dir(rt) / _exe("adb")).exists():
        return adb_dir(rt)
    on_path = shutil.which("adb")
    if on_path:
        return Path(on_path).parent
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cand = Path(local) / "Android" / "Sdk" / "platform-tools"
            if (cand / "adb.exe").exists():
                return cand
    return None


def _download_to(url: str, dest: Path, progress=None, label: str = "") -> None:
    """Stream a URL to a file with progress, resuming a partial file.

    Deliberately separate from download_blob(): that one verifies a sha256 the
    manifest promised, and REFUSES an artifact without one. These downloads are
    redirect pointers to a vendor (weilnetz, Google) whose bytes we do not hash
    in advance, so correctness is established afterwards, by running the thing
    (_looks_like_qemu_dir / adb --version) rather than by comparing a digest we
    were never given."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        have = dest.stat().st_size if dest.exists() else 0
        req = urllib.request.Request(url)
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                # A server that ignores Range answers 200 with the WHOLE file;
                # appending it to what we already have would silently produce a
                # corrupt double-length download.
                if have and r.status != 206:
                    have = 0
                    dest.unlink(missing_ok=True)
                total = have + int(r.headers.get("Content-Length") or 0)
                mode = "ab" if have else "wb"
                with open(dest, mode) as out:
                    received = have
                    while True:
                        chunk = r.read(_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        received += len(chunk)
                        if progress:
                            progress({"phase": "download", "artifact": label,
                                      "received": received, "total": total,
                                      "percent": (received / total * 100) if total else 0})
            return
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            time.sleep(1)
    raise BootstrapError(f"{label or url}: download failed after "
                         f"{_MAX_RETRIES} attempts: {last_exc}") from last_exc


# ---- elevation -----------------------------------------------------------

def is_elevated() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 — never let a probe decide the app dies
        return False


def run_elevated(command: str, timeout: float = 1800) -> int:
    """Run one command line as administrator and wait for it. Returns its exit
    code; raises BootstrapError if the user declines the UAC prompt.

    ShellExecuteExW, not subprocess: elevation is a SHELL verb ("runas"), and
    CreateProcess — which is all subprocess can do — cannot elevate. It fails
    with WinError 740 instead, which is exactly how the QEMU install was
    failing. SEE_MASK_NOCLOSEPROCESS is what makes the call waitable at all;
    plain ShellExecuteW returns an HINSTANCE and no process handle, so there
    would be no way to know whether the install finished, let alone whether it
    worked.
    """
    if sys.platform != "win32":
        raise BootstrapError("elevation is only implemented on Windows")

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong),
                    ("fMask", ctypes.c_ulong),
                    ("hwnd", ctypes.c_void_p),
                    ("lpVerb", ctypes.c_wchar_p),
                    ("lpFile", ctypes.c_wchar_p),
                    ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory", ctypes.c_wchar_p),
                    ("nShow", ctypes.c_int),
                    ("hInstApp", ctypes.c_void_p),
                    ("lpIDList", ctypes.c_void_p),
                    ("lpClass", ctypes.c_wchar_p),
                    ("hkeyClass", ctypes.c_void_p),
                    ("dwHotKey", ctypes.c_ulong),
                    ("hIcon", ctypes.c_void_p),
                    ("hProcess", ctypes.c_void_p)]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NO_CONSOLE = 0x00008000   # don't hand it our console
    SW_HIDE = 0
    ERROR_CANCELLED = 1223

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    info.lpVerb = "runas"
    # cmd.exe /c, so one elevated prompt can run a whole SCRIPT — the point of
    # batching: a fresh machine needs QEMU installed AND (maybe) the hypervisor
    # feature enabled, and asking for administrator twice for one setup is a
    # worse experience than the thing being manual.
    info.lpFile = os.environ.get("COMSPEC") or "cmd.exe"
    info.lpParameters = f'/c {command}'
    info.nShow = SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error() or ctypes.GetLastError()
        if err == ERROR_CANCELLED:
            raise BootstrapError(
                "Administrator permission was declined. Omni Executor needs it "
                "once, to install QEMU and turn on Windows Hypervisor Platform.")
        raise BootstrapError(f"could not request administrator rights (error {err})")
    if not info.hProcess:
        raise BootstrapError("elevated process did not start")
    WAIT_TIMEOUT = 0x102
    rc = ctypes.windll.kernel32.WaitForSingleObject(
        info.hProcess, int(timeout * 1000))
    if rc == WAIT_TIMEOUT:
        ctypes.windll.kernel32.CloseHandle(info.hProcess)
        raise BootstrapError(f"elevated step timed out after {int(timeout)}s")
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return int(code.value)


# ---- adb -----------------------------------------------------------------

def install_adb_windows(rt: Path, progress=None) -> Path:
    """Download Google's platform-tools and unpack adb into the runtime dir.

    Needs no elevation and no installer: it is a plain zip, and the runtime dir
    is user-writable by construction."""
    staging = rt / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    tmp = staging / "platform-tools.zip"
    try:
        _download_to(adb_win_url(), tmp, progress, "adb")
    except BootstrapError:
        # The dist API may not carry an adb-win entry yet (it is a redirect
        # entry added alongside this code). Falling back to Google's own
        # "latest" alias keeps a client that is newer than the registry working
        # rather than failing on a server that simply has not been edited.
        _download_to(_ADB_WIN_FALLBACK, tmp, progress, "adb")
    dest = adb_dir(rt)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp) as zf:
        _safe_extract_zip(zf, dest)
    tmp.unlink(missing_ok=True)
    # The zip contains a top-level platform-tools/ folder, so the files land at
    # <dest>/platform-tools/adb.exe. Flatten it — find_adb and the PATH entry
    # both name ONE directory, and a nested one silently resolves to nothing.
    nested = dest / "platform-tools"
    if (nested / "adb.exe").exists():
        for item in nested.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))
        shutil.rmtree(nested, ignore_errors=True)
    if not (dest / _exe("adb")).exists():
        raise BootstrapError("adb was downloaded but adb.exe is not where it "
                             "was expected after unpacking")
    return dest


# ---- qemu ----------------------------------------------------------------

def _install_qemu_portable(rt: Path, artifact: dict, progress=None) -> Path:
    """Unpack a portable QEMU zip that we host. No elevation, verified by
    sha256 like any other manifest artifact."""
    staging = rt / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    tmp = staging / f"{artifact['name']}.part"
    download_blob(dist_base(), artifact, tmp, progress)
    dest = qemu_dir(rt)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp) as zf:
        _safe_extract_zip(zf, dest)
    tmp.unlink(missing_ok=True)
    nested = dest / "qemu"
    if _looks_like_qemu_dir(nested):
        for item in nested.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))
        shutil.rmtree(nested, ignore_errors=True)
    if not _looks_like_qemu_dir(dest):
        raise BootstrapError("the portable QEMU archive did not contain a "
                             "complete QEMU install")
    return dest


def tools_manifest() -> dict:
    """The tools channel, or {} if it cannot be read.

    Never raises. A server that is down, or simply has no tools channel yet, is
    not a reason to fail the install — it only means falling back to the vendor
    installer, which costs a UAC prompt and still works. Silence here is the
    difference between "one extra prompt" and "setup failed"."""
    try:
        return read_manifest(dist_base(), channel=_TOOLS_CHANNEL)
    except BootstrapError:
        return {}


def qemu_install_plan(rt: Path, manifest: dict = None, installed: dict = None,
                      have_qemu: bool = None) -> dict:
    """What installing-or-upgrading QEMU here would take, WITHOUT doing it.

    Lets the caller decide up front whether administrator rights are needed at
    all, and batch every elevated action into a single prompt. Returns
    {"needed", "upgrade", "portable", "needs_admin"}.

    THIS USED TO BE A PRESENCE CHECK — `find_qemu() is not None` and nothing
    else — so QEMU was installed once and then never updated for the life of
    the machine. That silently shipped the wrong binary to two whole
    populations: anyone whose QEMU came from the vendor NSIS installer, and
    anyone who simply had QEMU already. Both keep an UNPATCHED build, and the
    patched one is what honours `QEMU_WINDOW_PANEL` — so the guest adopts the
    window's size instead of the panel it was given and the aspect ratio is
    wrong, with nothing anywhere reporting that the tool is stale.

    Now it compares a RECEIPT (installed.json's `tools` block, written by
    record_tool) against the published artifact's sha256. Three cases matter:

      no QEMU at all      install, by whichever route is available
      our build, current  nothing to do — the common path, still no network
      anything else       install the published portable build

    That last case covers both the stale-receipt upgrade and the machine whose
    QEMU is not ours. It does NOT touch their system install: the portable
    build goes into our own runtime dir, and find_qemu() already prefers that
    over PATH or Program Files, so ours simply wins.

    ONLY THE PORTABLE ROUTE EVER UPGRADES. The vendor installer is
    requireAdministrator, and prompting for UAC on every launch to replace a
    QEMU that already works is far worse than being one version behind — so
    when the server offers no portable build, a machine that has QEMU is left
    exactly as it is.

    `manifest` is looked up by NAME, so the channel it came from does not
    matter. `installed` / `have_qemu` are injectable for tests.
    """
    if have_qemu is None:
        have_qemu = find_qemu(rt) is not None
    portable = None
    for a in (manifest or {}).get("artifacts", []):
        if a.get("name") == _QEMU_PORTABLE_ARTIFACT and a.get("sha256"):
            portable = a
            break
    if not have_qemu:
        return {"needed": True, "upgrade": False, "portable": portable,
                "needs_admin": portable is None and not is_elevated()}
    if portable is None:
        # Nothing we could install without elevation. See the note above.
        return {"needed": False, "upgrade": False, "portable": None,
                "needs_admin": False}
    if installed is None:
        installed = installed_state(rt)
    receipt = (installed.get("tools") or {}).get("qemu") or {}
    if receipt.get("sha256") == portable.get("sha256"):
        return {"needed": False, "upgrade": False, "portable": portable,
                "needs_admin": False}
    return {"needed": True, "upgrade": True, "portable": portable,
            "needs_admin": False}


def _tool_artifact(manifest: dict, name: str) -> dict | None:
    """A manifest entry that is a real, verifiable payload — or None.

    An entry with no sha256 is a POINTER (a 302), and a pointer cannot be
    compared against anything: `adb-win` redirects to Google's "latest" alias,
    which by definition has no stable identity. Treating one as an upgradable
    artifact would mean re-downloading it on every single launch forever.
    """
    for a in (manifest or {}).get("artifacts", []):
        if a.get("name") == name and a.get("sha256"):
            return a
    return None


def adb_install_plan(rt: Path, manifest: dict = None, installed: dict = None,
                     have_adb: bool = None) -> dict:
    """Whether adb needs installing or replacing. Mirrors qemu_install_plan.

    TODAY THIS ONLY EVER INSTALLS, and that is a property of the SERVER, not
    of this code: `adb-win` is published as a redirect with no sha256, so
    _tool_artifact returns None and a machine that has adb is left alone.
    Publish a hashed adb artifact the way `qemu-portable-win` is published and
    upgrades start working here with no further change.
    """
    if have_adb is None:
        have_adb = find_adb(rt) is not None
    artifact = _tool_artifact(manifest, _ADB_ARTIFACT)
    if not have_adb:
        return {"needed": True, "upgrade": False, "artifact": artifact}
    if artifact is None:
        return {"needed": False, "upgrade": False, "artifact": None}
    if installed is None:
        installed = installed_state(rt)
    receipt = (installed.get("tools") or {}).get("adb") or {}
    if receipt.get("sha256") == artifact.get("sha256"):
        return {"needed": False, "upgrade": False, "artifact": artifact}
    return {"needed": True, "upgrade": True, "artifact": artifact}


def record_tool(rt: Path, name: str, artifact: dict) -> None:
    """Record which build of a host tool we installed.

    Separate from the `artifacts` receipts on purpose: those drive readiness
    (`plan_downloads` empty == ready), and a tool must never be able to make a
    working machine look un-ready. Tools live beside them and are consulted
    only by their own install plan.
    """
    state = installed_state(rt)
    state.setdefault("tools", {})[name] = {
        "version": (artifact or {}).get("version"),
        "sha256": (artifact or {}).get("sha256"),
    }
    _write_installed(rt, state)


def install_qemu_windows(rt: Path, manifest: dict = None, progress=None) -> Path:
    """Put a working QEMU on this machine and return the directory holding it.

    Two routes, preferred order:

    1. A portable zip we host (`qemu-portable-win` in the manifest) — extracted
       straight into the runtime dir. No installer, no elevation, sha256
       verified.
    2. The vendor's NSIS installer, run SILENTLY and ELEVATED. It is manifested
       requireAdministrator, so an unelevated CreateProcess on it does not run
       and fail — it never starts, raising WinError 740. That is precisely how
       the old engine-side ensure_qemu() broke on a fresh machine.

    Either way the target is the runtime dir, never Program Files and never the
    app dir: no admin is needed to WRITE there, and an app update cannot delete
    it.
    """
    if sys.platform != "win32":
        raise BootstrapError("install_qemu_windows is Windows-only")
    plan = qemu_install_plan(rt, manifest)
    if not plan["needed"]:
        return find_qemu(rt)
    if plan["portable"] is None and manifest is None:
        # Never reach for the elevated installer without having ASKED whether a
        # portable build exists. This is a public entry point, so it cannot
        # assume ensure_tools() already looked.
        plan = qemu_install_plan(rt, tools_manifest())
    if plan["portable"]:
        return _install_qemu_portable(rt, plan["portable"], progress)

    staging = rt / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    installer = staging / "qemu-setup.exe"
    _download_to(qemu_win_url(), installer, progress, "qemu")
    dest = qemu_dir(rt)
    dest.mkdir(parents=True, exist_ok=True)
    if progress:
        progress({"phase": "install", "artifact": "qemu", "received": 0,
                  "total": 0, "percent": 100})
    code = _run_qemu_installer(installer, dest)
    installer.unlink(missing_ok=True)
    if not _looks_like_qemu_dir(dest):
        raise BootstrapError(
            f"the QEMU installer exited with {code} but did not produce a "
            f"working QEMU in {dest}")
    return dest


def _nsis_command(installer: Path, dest: Path) -> str:
    """The silent-install command line for an NSIS installer.

    `/D=` is NSIS's own switch, not a normal argument: it must come LAST and
    must NOT be quoted, even when the path contains spaces (NSIS reads the raw
    command line and takes everything after `/D=` to the end). Quoting it — the
    reflex for any other program — makes it install to the default location
    instead, silently, which then looks like the download was wrong."""
    return f'"{installer}" /S /D={dest}'


def _run_qemu_installer(installer: Path, dest: Path) -> int:
    cmd = _nsis_command(installer, dest)
    if is_elevated():
        # Already administrator (the app was started elevated): no second
        # prompt. A string, not a list — CreateProcess is handed the command
        # line verbatim, which is what keeps an unquoted /D= with spaces intact.
        return subprocess.run(cmd, timeout=1800).returncode
    return run_elevated(cmd)


# ---- Windows Hypervisor Platform ----------------------------------------

_DISM_ENABLE = ('dism.exe /Online /Enable-Feature '
                '/FeatureName:HypervisorPlatform /All /NoRestart')
# DISM's documented "it worked, but Windows must restart" code. Treated as
# success with a reboot flag, not as a failure — it is the NORMAL outcome of
# turning the feature on, and reporting it as an error would tell a user their
# perfectly successful setup had failed.
_DISM_REBOOT_REQUIRED = 3010


def enable_whpx(timeout: float = 1800) -> dict:
    """Turn on Windows Hypervisor Platform. Requires administrator.

    Returns {"ok", "reboot_required", "exit_code"}. Idempotent: on a machine
    where the feature is already on, DISM exits 0 and nothing changes — which
    is what makes it safe to batch into the same elevated step as the QEMU
    install, on a fresh machine where WHPX cannot be probed yet because there
    is no QEMU to probe it with."""
    if sys.platform != "win32":
        return {"ok": True, "reboot_required": False, "exit_code": 0}
    if is_elevated():
        code = subprocess.run(_DISM_ENABLE, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                              ).returncode
    else:
        code = run_elevated(_DISM_ENABLE, timeout=timeout)
    ok = code in (0, _DISM_REBOOT_REQUIRED)
    if ok:
        # The cached "WHPX is off" answer is now stale, and it is the thing
        # gating the Start button.
        _whpx_cache.pop("win", None)
    return {"ok": ok, "reboot_required": code == _DISM_REBOOT_REQUIRED,
            "exit_code": code}


def ensure_tools(rt: Path, manifest: dict = None, progress=None) -> dict:
    """Install everything the ENGINE needs from the host, before any image is
    downloaded. Idempotent; safe to call on every launch.

    This is the fix for "a fresh machine never installs QEMU". Nothing else ran
    an install: the engine's ensure_qemu() only fires when an instance is
    actually started, the first-boot screen refused to begin until QEMU already
    existed, and each of those was waiting on the other.

    Returns {"qemu_dir", "adb_dir", "installed": [...], "reboot_required",
    "whpx_ok"}.
    """
    out = {"qemu_dir": None, "adb_dir": None, "installed": [],
           "reboot_required": False, "whpx_ok": None}
    if current_os() != "win":
        # macOS/Linux policy is SYSTEM tooling (brew/apt) — deliberately not
        # automated here; installing into a user's system package manager
        # behind their back is not ours to do.
        q, a = find_qemu(rt), find_adb(rt)
        out["qemu_dir"] = str(q) if q else None
        out["adb_dir"] = str(a) if a else None
        out["whpx_ok"] = True
        return out

    plan = qemu_install_plan(rt, manifest)
    # A machine that HAS QEMU still has to ask whether the published build is
    # newer than the one it is carrying -- that question is the entire reason
    # tools were never updated before. tools_manifest() never raises and
    # returns {} when the server cannot be reached, so an offline launch keeps
    # exactly the QEMU it already has instead of failing.
    if manifest is None and not plan["needed"]:
        manifest = tools_manifest()
        plan = qemu_install_plan(rt, manifest)
    if plan["needed"] and plan["portable"] is None and manifest is None:
        # Only now is it worth a round trip: this machine really has no QEMU,
        # and a portable build would save it a UAC prompt. A machine that
        # already has one never reaches here, so the common path stays offline.
        #
        # Keep the manifest we resolved, not just the plan. Passing the
        # original `manifest` (None) down to install_qemu_windows made it
        # recompute a plan that could not see the portable build, so it
        # downloaded the 197 MB vendor installer and asked for administrator
        # on a machine where neither was needed.
        manifest = tools_manifest()
        plan = qemu_install_plan(rt, manifest)
    # Decide the elevated work ONCE, before doing any of it, so it costs at
    # most one UAC prompt. WHPX can only be probed with a working QEMU, so on
    # a machine that has none we cannot know whether the feature needs turning
    # on — and DISM's enable is idempotent, so bundling it in is strictly
    # better than a second prompt later.
    # An UPGRADE is not a from-scratch install: there is a working QEMU here
    # right now, so WHPX can still be probed. Gating this on plan["needed"]
    # alone would have made every upgrading machine report whpx_ok=None and
    # re-run the elevated DISM enable it does not need.
    can_probe_now = not plan["needed"] or plan.get("upgrade")
    whpx = windows_accel_status() if can_probe_now else {"whpx_ok": None}
    want_whpx = whpx.get("whpx_ok") is not True
    if plan["needed"] and plan["needs_admin"] and want_whpx:
        if progress:
            progress({"phase": "elevate", "artifact": "qemu", "received": 0,
                      "total": 0, "percent": 0})
        (rt / "staging").mkdir(parents=True, exist_ok=True)
        installer = rt / "staging" / "qemu-setup.exe"
        _download_to(qemu_win_url(), installer, progress, "qemu")
        dest = qemu_dir(rt)
        dest.mkdir(parents=True, exist_ok=True)
        # One prompt, both jobs. `&` and not `&&`: DISM must run even if the
        # QEMU install fails, and its own exit code is the one we need back.
        code = run_elevated(f'{_nsis_command(installer, dest)} & {_DISM_ENABLE}')
        installer.unlink(missing_ok=True)
        if not _looks_like_qemu_dir(dest):
            raise BootstrapError(
                f"the QEMU installer did not produce a working QEMU in {dest}")
        out["installed"].append("qemu")
        out["reboot_required"] = code == _DISM_REBOOT_REQUIRED
        _whpx_cache.pop("win", None)
    elif plan["needed"]:
        install_qemu_windows(rt, manifest, progress)
        out["installed"].append("qemu upgrade" if plan.get("upgrade")
                                else "qemu")
    if plan["needed"] and plan.get("portable"):
        # Write the receipt ONLY for the portable route, because that is the
        # only one whose exact build we know. The vendor installer serves
        # whatever its 302 currently points at, so recording a version for it
        # would be a guess -- and a wrong receipt is worse than none, since it
        # would suppress the very upgrade this exists to perform.
        record_tool(rt, "qemu", plan["portable"])

    adb_plan = adb_install_plan(rt, manifest)
    if adb_plan["needed"]:
        if progress:
            progress({"phase": "start", "artifact": "adb", "received": 0,
                      "total": 0, "percent": 0})
        install_adb_windows(rt, progress)
        out["installed"].append("adb upgrade" if adb_plan["upgrade"] else "adb")
        if adb_plan["artifact"]:
            record_tool(rt, "adb", adb_plan["artifact"])

    q, a = find_qemu(rt), find_adb(rt)
    out["qemu_dir"] = str(q) if q else None
    out["adb_dir"] = str(a) if a else None
    # Now that QEMU exists, ask it directly rather than trusting the plan.
    if q and not out["reboot_required"]:
        _apply_tool_env(rt)
        out["whpx_ok"] = windows_accel_status().get("whpx_ok")
        if out["whpx_ok"] is False:
            res = enable_whpx()
            out["reboot_required"] = res["reboot_required"]
            if res["ok"]:
                out["installed"].append("whpx")
    return out


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
    """What the user should do when no QEMU is installed. On Windows: nothing
    — ensure_tools() installs it. This is a status line, not a chore.

    It used to be a LIE. It promised an automatic install on first run while
    the first-boot screen refused to start until QEMU was already there, and
    nothing in the launch path ever installed it, so the promise resolved to a
    permanent wait."""
    if current_os() == "win":
        return ("QEMU is not installed yet — Omni Executor is installing it "
                "automatically.")
    return "brew install qemu android-platform-tools"


_WHPX_HINT = (
    "Windows Hypervisor Platform is turned off, so the Android VM cannot "
    "start. Omni Executor can turn it on for you — it needs administrator "
    "permission once, and Windows has to restart afterwards.")

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

    qdir = find_qemu(runtime_dir())
    if not qdir:
        # Deliberately NOT cached: QEMU is about to be installed, and the
        # next call should get a real answer.
        return {"os": "win", "whpx_ok": None, "hint": _WHPX_NO_QEMU_HINT}
    qemu = str(qdir / _exe(_qemu_system_name()))
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
    Mirrors configure_engine's tool detection so bootstrap_status can poll
    cheaply.

    It used to call shutil.which() — PATH ONLY — which was wrong in both
    directions. omnidroid's qemu_bin() on Windows never consults PATH, so a
    machine with QEMU on PATH reported qemu_ok while the engine could not find
    it; and a QEMU installed where the engine DOES look (the runtime dir)
    reported not-installed forever. adb was not checked at all, though
    omnidroid/adb.py shells the bare name and fails just as hard without it."""
    qdir = find_qemu(rt)
    adir = find_adb(rt)
    return {"qemu_ok": qdir is not None,
            "qemu_dir": str(qdir) if qdir else None,
            "adb_ok": adir is not None,
            "adb_dir": str(adir) if adir else None,
            "tools_ok": qdir is not None and adir is not None,
            "qemu_hint": None if qdir else _qemu_hint()}


def _apply_tool_env(rt: Path) -> dict:
    """Point THIS process (and therefore every engine subprocess it spawns) at
    the tools ensure_tools() installed.

    adb has to go on PATH because omnidroid/adb.py runs the bare name "adb";
    there is no config knob for it. QEMU does not need PATH — configure_engine
    writes its directory into paths.json as `qemu.dir`, which qemu_bin() reads
    first — but OMNI_QEMU_DIR is set anyway so a subprocess that somehow reads
    no config still resolves it."""
    q, a = find_qemu(rt), find_adb(rt)
    if q:
        os.environ["OMNI_QEMU_DIR"] = str(q)
    if a:
        cur = os.environ.get("PATH", "")
        entries = cur.split(os.pathsep)
        if str(a) not in entries:
            os.environ["PATH"] = str(a) + os.pathsep + cur
    return {"qemu_dir": str(q) if q else None, "adb_dir": str(a) if a else None}


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

    # PATH for adb + OMNI_QEMU_DIR, inherited by every engine subprocess.
    _apply_tool_env(rt)
    qemu_found = find_qemu(rt)
    qemu_path = str(qemu_found / _exe(_qemu_system_name())) if qemu_found else None
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
