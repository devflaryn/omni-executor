"""Auto-update: check on every launch, apply on request.

TWO things go stale, and they fail differently, so they are handled separately
even though one manifest describes both.

  runtime  the base image and its Roblox offsets. A stale one is not cosmetic â€”
           this is exactly how the x86 base ended up shipping a kiosk that could
           not deliver a session, and every machine that had already installed it
           kept that bug until someone reinstalled by hand. Detected by sha256,
           so a rebuilt image is picked up even when its version string did not
           change (the offset was rebuilt in place; its "version" is the Roblox
           build, not the bake).

  app      a build of this program. Detected by VERSION, because the running
           app's own bytes are not something it can hash meaningfully (a one-dir
           PyInstaller build is a whole tree, and in dev there is no build at
           all).

Applying is deliberately manual â€” one click, not a surprise. Replacing a base
image while a VM has it open, or swapping the app out from under a running
launch, are both things a user should get to schedule.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import bootstrap

# The app version this build reports. Bumped when a build is published (see
# omni-backend/scripts/push-app.mjs); compared against the manifest's
# `app.version` to decide whether an update exists.
APP_VERSION = "1.0.24"

# Where a downloaded app build is unpacked before it replaces the live one.
STAGING_DIR = "app-update"


class UpdateError(Exception):
    pass


# --------------------------------------------------------------- versions

def _parts(version):
    """A comparable tuple from a dotted version, tolerating suffixes.

    "1.2.3" -> (1, 2, 3); "1.2.3-beta" -> (1, 2, 3). Anything unparseable
    sorts lowest, so a garbled manifest can never look newer than what is
    installed â€” the safe direction for a value that triggers a self-replace.
    """
    out = []
    for chunk in str(version or "").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def is_newer(candidate, current):
    """Strictly newer, and never on a tie or an unreadable value."""
    c, n = _parts(candidate), _parts(current)
    return bool(c) and c > n


# ----------------------------------------------------------------- paths

def app_dir():
    """The directory that would be REPLACED by an app update.

    Only meaningful for a frozen build: one-dir PyInstaller puts the exe and
    `_internal/` in one folder, and on macOS the whole `.app` bundle is the
    unit. Running from source there is nothing to swap, which is why
    can_apply_app() says so instead of pretending.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
    return exe.parent


def staging_root():
    return bootstrap.runtime_dir() / STAGING_DIR


def can_apply_app():
    """(bool, reason) â€” whether this process can replace itself in place."""
    if app_dir() is None:
        return False, ("Running from source, so there is no build to replace. "
                       "Pull and rebuild instead.")
    return True, None


def manages_runtime():
    """(bool, reason) â€” whether the engine actually boots the images we install.

    The updater owns exactly one image directory: the one inside the runtime
    dir it installed. A checkout pointed somewhere else â€” OMNI_IMAGES_DIR, or a
    hand-written paths.json, which is how every dev machine here is set up â€”
    boots images this code has never touched. Offering an update there is worse
    than useless: it would download gigabytes into a directory the engine does
    not read, and report success while changing nothing the user can see.
    """
    configured = os.environ.get("OMNI_IMAGES_DIR")
    if not configured:
        return True, None
    try:
        images = Path(configured).resolve()
        expected = (bootstrap.runtime_dir() / "images").resolve()
    except OSError:
        return True, None
    if images == expected:
        return True, None
    return False, (f"This install boots images from {images}, which it did not "
                   f"download and does not manage. Update them where they are "
                   f"built.")


# ----------------------------------------------------------------- check

def _app_artifact(manifest):
    for a in manifest.get("artifacts", []):
        if a.get("kind") == "app":
            return a
    return None


def check(base_url=None):
    """What is available, without downloading anything but the manifest.

    Never raises for a normal offline case â€” the caller is a launch-time
    background check and a missing network is not an error worth a dialog.
    """
    result = {
        "ok": True,
        "checkedAt": time.time(),
        "app": {"current": APP_VERSION, "available": None, "update": False,
                "canApply": can_apply_app()[0], "reason": can_apply_app()[1]},
        "runtime": {"update": False, "artifacts": [], "bytes": 0},
        "error": None,
    }
    try:
        manifest = bootstrap.read_manifest(base_url or bootstrap.dist_base())
    except bootstrap.BootstrapError as e:
        result["ok"] = False
        result["error"] = str(e)
        return result

    manages, why = manages_runtime()
    installed = bootstrap.installed_state(bootstrap.runtime_dir())
    plan = bootstrap.plan_downloads(manifest, installed) if manages else []
    result["runtime"] = {
        "update": bool(plan),
        "artifacts": [{"name": a["name"], "version": a.get("version"),
                       "bytes": a.get("bytes") or 0} for a in plan],
        "bytes": sum(int(a.get("bytes") or 0) for a in plan),
        "managed": manages,
        "reason": why,
    }

    app_art = _app_artifact(manifest)
    available = (app_art or {}).get("version") or manifest.get("app", {}).get("version")
    result["app"]["available"] = available
    # An app build has to BE published to be installable: the manifest's
    # app.version alone is just a number, and offering an update the client
    # cannot fetch is worse than saying nothing.
    result["app"]["update"] = bool(app_art) and is_newer(available, APP_VERSION)
    result["app"]["bytes"] = int((app_art or {}).get("bytes") or 0)
    return result


# ----------------------------------------------------------------- apply

def apply_runtime(base_url=None, progress=None):
    """Download whatever changed and place it. ensure_runtime is already
    idempotent and sha256-driven, so this is the same code path first boot
    uses â€” an update is just a first boot with most of the work already
    done."""
    return bootstrap.ensure_runtime(base_url=base_url, progress=progress)


def stage_app(base_url=None, progress=None):
    """Download and unpack a new app build beside the runtime dir.

    Staged rather than applied: the swap has to happen with this process gone,
    so it is a separate step that a relaunch performs (see apply_staged_app).
    Returns the staged build directory.
    """
    base_url = (base_url or bootstrap.dist_base()).rstrip("/")
    manifest = bootstrap.read_manifest(base_url)
    art = _app_artifact(manifest)
    if not art:
        raise UpdateError("no app build is published for this platform")
    if not is_newer(art.get("version"), APP_VERSION):
        raise UpdateError(f"published build {art.get('version')} is not newer "
                          f"than {APP_VERSION}")

    root = staging_root()
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{art['name']}.part"
    bootstrap.download_blob(base_url, art, tmp, progress)

    unpacked = root / "unpacked"
    unpacked.mkdir(parents=True, exist_ok=True)
    bootstrap.place_artifact({**art, "dest": "."}, tmp, unpacked)

    build = _staged_build(unpacked, art.get("root"))
    if build is None:
        raise UpdateError("the downloaded build did not contain an app directory")
    (root / "staged.json").write_text(json.dumps({
        "version": art.get("version"), "build": str(build),
        "sha256": art.get("sha256"), "staged": time.time(),
    }, indent=2), encoding="utf-8")
    return build


def _staged_build(unpacked: Path, declared_root=None):
    """The app directory inside a freshly unpacked archive.

    Prefers the name the registry declared; falls back to the single top-level
    directory. Guessing among several would be how the wrong tree gets copied
    over a working install.
    """
    if declared_root and (unpacked / declared_root).is_dir():
        return unpacked / declared_root
    dirs = [p for p in unpacked.iterdir() if p.is_dir()]
    if len(dirs) == 1:
        return dirs[0]
    return None


def staged_info():
    """Details of a build waiting to be applied, or None.

    Three ways to be None, and the third is the interesting one:
      * nothing staged,
      * the receipt points at a directory that is gone (offering an Install
        button that cannot work is worse than offering none),
      * the staged build is no longer NEWER than what is running â€” which is
        exactly the state after a successful update, since the staging dir
        outlives the swap. Without this the app would sit there forever
        offering to install the version it already is.
    """
    try:
        info = json.loads((staging_root() / "staged.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    build = Path(info.get("build", ""))
    if not build.is_dir():
        return None
    if not is_newer(info.get("version"), APP_VERSION):
        return None
    return info


def discard_staged():
    """Throw away a staged build. Called after a successful swap so the next
    launch is not still offering it."""
    shutil.rmtree(staging_root(), ignore_errors=True)


def launch_apply(build_dir=None):
    """Hand the swap to the STAGED copy and ask this process to exit.

    The staged executable is a different file on disk, so it can replace the
    directory this process is running from â€” which is the whole reason the
    swap is not done here. Windows will not let a running image be replaced at
    all, and on macOS replacing a loaded bundle out from under itself is just
    as bad an idea.

    Returns the child's pid. The caller closes the window afterwards; the child
    waits for this pid to disappear before touching anything.
    """
    ok, reason = can_apply_app()
    if not ok:
        raise UpdateError(reason)
    info = staged_info()
    build = Path(build_dir) if build_dir else (Path(info["build"]) if info else None)
    if not build or not build.is_dir():
        raise UpdateError("no staged build to apply")

    target = app_dir()
    exe = _executable_in(build)
    if exe is None:
        raise UpdateError(f"no launchable binary inside {build}")

    creationflags = 0
    kwargs = {}
    if sys.platform == "win32":
        creationflags = 0x00000008 | 0x00000200      # DETACHED_PROCESS | NEW_GROUP
        kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [str(exe), "--apply-update", str(target), str(os.getpid())],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **kwargs)
    return proc.pid


def _executable_in(build: Path):
    if sys.platform == "darwin" and build.suffix == ".app":
        macos = build / "Contents" / "MacOS"
        if macos.is_dir():
            for f in macos.iterdir():
                if f.is_file() and os.access(f, os.X_OK):
                    return f
        return None
    name = "omni-exec.exe" if sys.platform == "win32" else "omni-exec"
    candidate = build / name
    return candidate if candidate.exists() else None


# ------------------------------------------------- the swap (child process)

def apply_staged_app(target_dir, wait_pid, timeout=90, log=print):
    r"""Replace `target_dir` with the build this process is running from.

    Runs in the STAGED copy, launched by launch_apply() from the old one.

    FILE BY FILE, never `target -> target.old`, and that distinction is the
    whole of this function's history. The rename is the obvious swap and it
    does not work: Windows refuses to rename a directory while any process
    holds a handle inside it, and this app leaves such processes behind on
    purpose. update_app_restart() says outright that QEMU is detached and the
    VMs outlive the window; each instance also has a `_windowlock` (and a
    viewer) that is omni-exec.exe running out of the install directory for as
    long as its window is on screen, and every one of them inherits that
    directory as its working directory. So every user with an instance up got

        [WinError 32] ... \Programs\OmniExecutor
                       -> \Programs\OmniExecutor.old

    and a PyInstaller crash dialog instead of an update. Three times in this
    machine's own update.log (2026-08-16, -18, -22) before it was read.

    bootstrap.replace_tree does the same job without ever renaming or deleting
    a directory, and puts every file back if any part of it fails: a
    half-copied application directory is still the one outcome worse than not
    updating.
    """
    target = Path(target_dir)
    source = app_dir()
    if source is None:
        raise UpdateError("--apply-update must be run from a frozen build")
    if source.resolve() == target.resolve():
        raise UpdateError("refusing to replace a directory with itself")

    deadline = time.time() + timeout
    while time.time() < deadline and _pid_alive(int(wait_pid)):
        time.sleep(0.5)
    if _pid_alive(int(wait_pid)):
        raise UpdateError(f"the old app (pid {wait_pid}) is still running after "
                          f"{timeout}s; not replacing it underneath itself")
    # Windows can hold a file briefly after the process is gone.
    time.sleep(1.0)

    log(f"[update] replacing {target} with {source}")
    report = bootstrap.replace_tree(source, target, log=log)
    log(f"[update] {report['files']} file(s) installed")

    # The staging dir has served its purpose. Leaving it costs ~90 MB and, more
    # to the point, leaves a receipt the new build would read on its next
    # launch.
    discard_staged()

    exe = _executable_in(target)
    if exe is not None:
        log(f"[update] relaunching {exe}")
        kwargs = {"creationflags": 0x00000008} if sys.platform == "win32" \
            else {"start_new_session": True}
        subprocess.Popen([str(exe)], stdin=subprocess.DEVNULL, **kwargs)
    return {"ok": True, "target": str(target)}


def _pid_alive(pid):
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
