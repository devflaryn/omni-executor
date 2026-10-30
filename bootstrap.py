"""Headless first-boot runtime installer for Omni Executor.

Fetches the named-blob manifest, downloads + sha256-verifies each artifact
with HTTP Range resume, places it under the OmniExec runtime dir, and records
installed.json. No GUI, no engine import -- pure stdlib so it is unit-testable.

WHAT A RUNTIME IS NOW. Two artifacts, both from the `native` channel:

  omnidroid-<os>   the native omnidroid bundle (its binaries, the roblox
                   plugin and the Android sysroot it boots), unpacked to
                   <runtime>/omnidroid
  omniexec-apk-<os> the executor APK every instance runs, placed at
                   <runtime>/apk/OmniExecutor.apk

There used to be far more: a portable QEMU and adb fetched as host tools,
Windows Hypervisor Platform switched on with a UAC prompt and a reboot, and a
multi-gigabyte Bliss OS base plus a Roblox "offset" disk per architecture,
registered into a paths.json for the QEMU engine. Native omnidroid needs none
of it -- no VM, no hypervisor, no adb -- so none of it is here any more.

WHY A CHANNEL OF ITS OWN. Clients up to 1.0.44 read the `stable` channel and
treat every hashed artifact in it as something they must download before they
are ready (see plan_downloads). Putting the omnidroid bundle there would have
sent every one of them a gigabyte they cannot use; putting this app's runtime
on `stable` would have handed IT their base images. `native` keeps the two
generations from ever seeing each other's files.
"""
import hashlib, json, os, shutil, sys, tarfile, time, urllib.request, urllib.error, zipfile
from pathlib import Path

# DEV MODE, ABSENT FROM RELEASE BUILDS. Every PyInstaller spec here excludes
# `devserver`, so in a shipped app this import fails and dist_base() resolves
# the production server exactly as it did before the module existed. See
# devserver.py.
try:
    import devserver as _devserver
except ImportError:  # pragma: no cover - the production path
    _devserver = None

APP_DIR_NAME = "OmniExec"
MANIFEST_PATH = "/omni/dist/manifest"
_CHUNK = 1 << 20
_MAX_RETRIES = 3

# The dist channel this app's runtime AND its own app builds are published on.
# See the module docstring for why it is not `stable`.
NATIVE_CHANNEL = "native"


class BootstrapError(Exception):
    pass


def dist_base() -> str:
    """Where the update manifest and every artifact blob come from.

    Redirected to the local omni-backend in dev mode, so `installed.json`, the
    manifest, the QEMU/adb blobs and the app-update artifact are all exercised
    against a backend you can edit -- which is the only way to test an update
    before it is published. See devserver.py.
    """
    base = os.environ.get("OMNI_EXEC_BASE", "http://179.198.197.7").rstrip("/")
    return _devserver.redirect(base) if _devserver else base


def current_os() -> str:
    """The dist API's name for this host: "win" | "linux" | "mac".

    Every manifest request goes through this. The base a machine downloads
    MUST match its architecture — a Windows box asking for the mac manifest
    would fetch the arm64 base and its arm offset, neither of which it can
    boot — so this is never hardcoded at a call site.

    Linux is its own answer and not a synonym for "mac". This used to be a
    two-way win/mac split, which quietly sent every Linux host down the mac
    branch: it asked for the mac manifest and started downloading a 3.9 GB
    arm64 base onto an x86_64 machine that cannot boot it. Linux is an x86
    host and takes the SAME base as Windows; only the tooling policy differs
    (system QEMU from apt rather than the portable download)."""
    if sys.platform == "win32":
        return "win"
    if sys.platform.startswith("linux"):
        return "linux"
    return "mac"


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


def read_manifest(base_url: str, os_name: str = None, channel: str = NATIVE_CHANNEL) -> dict:
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


def _part_file(staging: Path, artifact: dict) -> Path:
    """Where a resumable download accumulates.

    KEYED BY sha256, NOT BY NAME. A partial is only resumable against the
    exact blob it came from, and artifact NAMES are stable across versions --
    `qemu-portable-win` meant 11.0.50 yesterday and 11.1.0 today. A leftover
    partial of the old blob is longer than (or simply different from) the new
    one, so the Range request asks for bytes past its end and the server
    answers 416: MEASURED, "download failed after 3 attempts ... HTTP Error
    416: Range Not Satisfiable", with no way out but deleting the file by
    hand. Rebuilt base images have the same shape of problem.
    """
    tag = (artifact.get("sha256") or "nohash")[:12]
    return staging / f"{artifact['name']}-{tag}.part"


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


# ------------------------------------------- replacing an installed app tree
#
# Windows will not rename or delete a DIRECTORY while any process holds a
# handle inside it, and this app deliberately leaves such processes behind:
# the per-instance `_windowlock` aspect lock and the VNC viewer are detached
# copies of omni-exec.exe launched FROM the install directory and designed to
# outlive the window that started them, and every QEMU inherits that directory
# as its working directory. So `install-dir -> install-dir.old` -- the obvious
# way to swap a build -- fails with
#
#     [WinError 32] The process cannot access the file because it is being
#     used by another process: '...\Programs\OmniExecutor'
#         -> '...\Programs\OmniExecutor.old'
#
# for any user who has an instance running, which is most of them. Recorded
# three times in update.log on the dev machine (2026-08-16, -18, -22) before
# anyone read it; the app's own comment in update_app_restart() says outright
# that VMs outlive the process, which is exactly what makes it the normal case
# rather than an edge one.
#
# FILES are different. Renaming a file whose image is loaded, or which is open
# for reading, is allowed -- it is how every self-updating program on Windows
# works. So this replaces a build one file at a time and never touches a
# directory: existing files are parked in a subdirectory, the new ones are
# copied in, and anything the old build had that the new one does not is gone
# because it was parked, not because a tree was deleted.

_TRASH_PREFIX = ".omni-replaced-"


def _tree_files(root: Path, skip_prefix: str = None) -> list:
    """Every file under `root`, as paths relative to it. Directories are not
    listed: this module never acts on one."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_prefix:
            dirnames[:] = [d for d in dirnames if not d.startswith(skip_prefix)]
        for name in filenames:
            out.append(Path(dirpath, name).relative_to(root))
    return out


def replace_tree(source: Path, target: Path, log=None) -> dict:
    r"""Make `target` hold exactly what `source` holds. Returns a small report.

    A REPLACE, not a merge: a file the old build had and the new one does not
    is removed, because a leftover that loads instead of its replacement is the
    kind of bug nobody ever diagnoses.

    ALL OR NOTHING. Every existing file is parked before a single new one is
    written, and if any part of it fails the parked files go back exactly where
    they were -- a half-copied application directory is the one outcome worse
    than not updating at all.

    What it will NOT do is rename or delete a directory, for the reason above.
    The cost is a `.omni-replaced-*` folder left inside the target whenever the
    old files were still locked when the swap finished (a loaded DLL can be
    renamed but not deleted). sweep_replaced() clears those on a later launch,
    when nothing is holding them any more.
    """
    source, target = Path(source), Path(target)
    say = log if callable(log) else (lambda _m: None)
    if not source.is_dir():
        raise BootstrapError(f"there is nothing to install from: {source}")
    target.mkdir(parents=True, exist_ok=True)
    # Anything a PREVIOUS swap had to leave behind. Now is a good moment: that
    # build has not run since, so whatever was holding its files is gone.
    sweep_replaced(target, log=log)

    # Inside the target on purpose. A rename only ever works within one volume,
    # and this is the one directory guaranteed to be on the same volume as the
    # files being moved.
    trash = target / f"{_TRASH_PREFIX}{int(time.time())}"
    n = 0
    while trash.exists():            # a leftover the sweep above could not take
        n += 1
        trash = target / f"{_TRASH_PREFIX}{int(time.time())}-{n}"
    trash.mkdir(parents=True)

    moved = []                       # (parked, original) -- the rollback list
    try:
        for rel in _tree_files(target, skip_prefix=_TRASH_PREFIX):
            original, parked = target / rel, trash / rel
            parked.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(original, parked)
            except OSError as e:
                raise BootstrapError(
                    f"{original} is in use and cannot be replaced ({e}). Close "
                    f"Omni Executor and anything running out of its folder, "
                    f"then try again; a reboot always clears it.") from e
            moved.append((parked, original))
        if moved:
            say(f"[replace] parked {len(moved)} file(s) in {trash.name}")

        copied = 0
        for rel in _tree_files(source):
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, dest)
            copied += 1
        say(f"[replace] wrote {copied} file(s) to {target}")
    except BaseException:
        say("[replace] failed -- putting the previous build back")
        for parked, original in reversed(moved):
            try:
                original.parent.mkdir(parents=True, exist_ok=True)
                # replace, not rename: a file we had already copied over the
                # top of this one has to lose.
                os.replace(parked, original)
            except OSError:
                pass
        shutil.rmtree(trash, ignore_errors=True)
        raise

    # Empty directories the new build has no use for. Best effort by
    # definition -- one that is some process's working directory will not go,
    # and an empty folder harms nothing.
    _prune_empty_dirs(target, keep=source)

    shutil.rmtree(trash, ignore_errors=True)
    leftover = trash.exists()
    if leftover:
        say(f"[replace] {trash.name} is still held; it will be swept later")
    return {"files": copied, "parked": len(moved),
            "leftover": str(trash) if leftover else None}


def _prune_empty_dirs(target: Path, keep: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(target, topdown=False):
        here = Path(dirpath)
        if here == target:
            continue
        rel = here.relative_to(target)
        if any(part.startswith(_TRASH_PREFIX) for part in rel.parts):
            continue
        if (keep / rel).is_dir():
            continue
        try:
            here.rmdir()
        except OSError:
            pass


def sweep_replaced(target: Path, log=None) -> int:
    """Delete the parked leftovers replace_tree could not remove at the time.

    A DLL that was still loaded during the swap can be renamed but not deleted
    until the process holding it exits, so it is left behind rather than
    failing an update that otherwise worked. This is the other half: call it at
    startup, when the previous build's helpers are long gone. Returns how many
    folders it cleared."""
    target = Path(target)
    cleared = 0
    try:
        entries = list(target.iterdir())
    except OSError:
        return 0
    for child in entries:
        if not child.name.startswith(_TRASH_PREFIX) or not child.is_dir():
            continue
        shutil.rmtree(child, ignore_errors=True)
        if not child.exists():
            cleared += 1
            if log:
                log(f"[replace] swept {child.name}")
    return cleared


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


def _unlink_before_rewrite(dest: Path, names) -> None:
    """Remove each file an archive is about to write, so it lands on a NEW inode.

    extractall opens an existing file with "wb": same inode, new bytes. On
    Apple Silicon the kernel keeps a binary's code signature per vnode, and a
    Mach-O rewritten in place is SIGKILLed at its next exec ("Code Signature
    Invalid") though `codesign --verify` passes -- an omnidroid bundle
    updated over itself killed every device it booted. Windows is left alone:
    there a file in use can be neither rewritten nor unlinked, as before."""
    if sys.platform == "win32":
        return
    for name in names:
        try:
            (dest / name).unlink()
        except OSError:
            pass


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    for m in tf.getmembers():
        if m.issym() or m.islnk():
            raise BootstrapError(f"unsafe tar member (link): {m.name}")
        target = (dest / m.name).resolve()
        if not (target == dest or dest in target.parents):
            raise BootstrapError(f"unsafe tar member: {m.name}")
    _unlink_before_rewrite(dest, [m.name for m in tf.getmembers() if m.isfile()])
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
    _unlink_before_rewrite(dest, [i.filename for i in zf.infolist() if not i.is_dir()])
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
        tmp = _part_file(staging, a)
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


# ------------------------------------------------------------- the engine

# Where each runtime artifact lands. Mirrored in droid.py, which is what reads
# them; kept here too so bootstrap can answer "is it installed" with no import
# of the pool client.
_BUNDLE_SUBDIR = "omnidroid"
_APK_PATH = Path("apk") / "OmniExecutor.apk"


def _omnidroid_exe() -> str:
    return "omnidroid.exe" if sys.platform == "win32" else "omnidroid"


def engine_ready(rt: Path) -> dict:
    """Read-only readiness probe: is there an omnidroid to run, and an APK
    for it to run? No network, no disk write -- bootstrap_status polls it."""
    bundle = rt / _BUNDLE_SUBDIR
    omnidroid_ok = (bundle / _omnidroid_exe()).is_file() and         (bundle / "sysroot").is_dir()
    apk_ok = (rt / _APK_PATH).is_file()
    return {"omnidroid_ok": omnidroid_ok,
            "omnidroid_dir": str(bundle) if omnidroid_ok else None,
            "apk_ok": apk_ok,
            "tools_ok": omnidroid_ok and apk_ok}


def configure_engine(rt: Path) -> dict:
    """Point this process at its runtime.

    All that is left of a function that used to write a whole QEMU paths.json:
    OMNI_DATA_DIR is where the account store (robloxaccounts.py) and the
    autoexec folder live, and every module that reads them resolves it from
    here. Safe to call before anything is downloaded."""
    os.environ["OMNI_DATA_DIR"] = str(rt)
    return {"data_dir": str(rt), **engine_ready(rt)}
