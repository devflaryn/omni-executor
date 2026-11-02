"""Omni Executor — the backend behind the Tauri window, and the omnidroid engine.

This module used to BE the window (pywebview). It is now the headless half:
the Tauri shell in `src-tauri/` owns the window and runs this as a child
process, speaking newline-delimited JSON over the pipe between them. Every
method on `Api` is still reachable from the page exactly as it was — the
frontend's `api("engine_start", ...)` lands on `Api.engine_start` — only the
transport changed. See rpc.py for the frame format.

Setup:
    pip install -r requirements.txt
    npm install && npm --prefix frontend install

Run:
    npm run tauri dev            # the app (root package.json, Tauri CLI)
    python main.py --rpc         # this half alone, on stdio

Window chrome is no longer this file's problem. Tauri creates a frameless,
transparent window on all three platforms; the page draws the 33 px sheet and
its own titlebar, macOS keeps its native traffic lights top-left, and
Windows/Linux get our buttons on the right. What used to be windowchrome.py's
Win32 surgery is now src-tauri/src/chrome.rs.

Engine:
    Native omnidroid (droid.py): the executor APK runs on omnidroid's warm
    Androids, one window per instance, driven over omnidroid's MCP server.
    Every saved Roblox account launches the same way -- there are no modes,
    no bases and no architectures to pick any more.
"""

import atexit
import base64
import bootstrap
import cloud
import contextlib
import droid
import json
import miner
import netcheck
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import rpc

APP_NAME = "omni-executor"
PROJECT_DIR = Path(__file__).resolve().parent
# The frontend is the Tauri shell's to serve (tauri.conf.json's
# `frontendDist`), not this process's. Nothing here reads it any more.

# OMNI-EXEC remote-execute bridge base (serves the in-game UI + the exec queue).
# Override with the OMNI_EXEC_BASE env var or a settings.json "execBase" value.
OMNI_EXEC_BASE = os.environ.get("OMNI_EXEC_BASE", "http://179.198.197.7")

# DEV MODE, ABSENT FROM RELEASE BUILDS. Both PyInstaller specs exclude
# `devserver`, so a shipped app has no such module and _exec_base() resolves the
# production server exactly as it did before. See devserver.py.
try:
    import devserver as _devserver
except ImportError:  # pragma: no cover - the production path
    _devserver = None

DEFAULT_SETTINGS = {
    "theme": "dark",
    "activeTab": "home",
    "sidebar": "expanded",
    "launch": {"place": "", "minimizeOnLaunch": False},
    "profile": {"name": "Guest", "tag": ""},
    # Keep the app current without being asked. ON by default: a stale client
    # is how a machine keeps a bug that was fixed weeks ago, and the two
    # behaviours behind this flag are both deliberately unobtrusive — apply at
    # LAUNCH, when nobody is mid-anything, and merely OFFER while the app is
    # open. See Api._update_tick.
    "autoUpdate": True,
    # Account-creation preferences (the Create account modal seeds itself from
    # these and writes its last-used values back). The custom PASSWORD is
    # deliberately NOT persisted: it is a per-run override, and a password at
    # rest in settings.json is one leak away from being every account's.
    "creation": {"amount": 1, "usernameStyle": "name_no"},
    # Master switch for the autoexec folder. ON, because an autoexec folder
    # whose scripts do not run is a confusing default -- but it has to be
    # reachable from the UI, because the scripts run in EVERY instance and one
    # of them putting a full-screen GUI over the game is not something you
    # want to have to fix by deleting files. Off pushes an EMPTY bundle rather
    # than skipping the push; see omnidroid/autoexec.py's NO_AUTOEXEC_ENV.
    "autoexecEnabled": True,
    # `proxy` is the outbound proxy the SIGNUP BROWSER uses. It has nothing to
    # do with captchas: Roblox rate-limits signup per IP (roughly ten attempts
    # before it answers "an unknown error occurred" with no captcha at all), so
    # a fresh address per batch is what keeps creation working. Empty = direct.
    # `solverUrl` is the vision captcha service (captchaserver.py): the
    # executor POSTs screenshots there and gets back an action, never the
    # model. Empty = no automatic solver, the human in the window solves.
    # `solverToken` is the service's optional bearer token, never persisted to
    # a default: it belongs to the deployment, like the proxy.
    "captcha": {"proxy": "",
                "solverUrl": "http://127.0.0.1:8788",
                "solverToken": ""},
}

# Settings an older build wrote that mean nothing now. settings.json outlives
# the update, and a launch block still carrying a mode or a GPU policy would
# only confuse the next reader of the file, so get_settings() drops them once.
#   launch.mode / launch.gpu   the QEMU engine's Gaming/Farming presets and its
#                              display policy
#   autoWarm / pool            its warm pool of pre-booted VMs
#   farming                    the Farming tab's member list
RETIRED_LAUNCH_KEYS = ("mode", "gpu")
RETIRED_SETTINGS = ("autoWarm", "pool", "farming")


def config_dir() -> Path:
    """Per-user config directory following each OS's convention."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    directory = base / APP_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


SETTINGS_FILE = config_dir() / "settings.json"
# The update watcher reads settings on its own thread. On Windows a read that
# meets a save can fail the save, or come back empty and look like defaults.
_SETTINGS_LOCK = threading.RLock()
# Open editor tabs (name, contents, caret) -- its own file, because script
# bodies are large and churn on every keystroke while settings barely change.
EDITOR_FILE = config_dir() / "editor.json"
# Mining enrollment ({minerToken, stratumHost, stratumPort, algos}). PERSISTED
# on purpose -- it used to live only on the Api instance, so any app restart
# after a real enroll left `miner.is_installed()` true (nothing on disk
# changes it) while `self._mining` came back None, and mining_start refused
# every attempt with "not_enrolled" with no way to recover short of a fresh
# server enroll. Same directory/atomic-write/0600 pattern as cloud.py's
# auth.json, because a minerToken is a bearer credential too.
MINING_FILE = config_dir() / "mining.json"


def _mining_file() -> Path:
    return MINING_FILE


def _save_mining(info):
    f = _mining_file()
    tmp = f.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Windows has no POSIX mode bits
    tmp.replace(f)


def _load_mining():
    try:
        with open(_mining_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# xmrig prints e.g. "speed 10s/60s/15m 14.62 12.76 14.17 MH/s max 15.18 MH/s".
# Parse the 10s figure + unit into H/s so the Earn tab can show a live hashrate
# (the pool-side accrual has no hashrate field). "n/a" during warmup -> None.
_XMRIG_SPEED_RE = re.compile(
    r"speed\s+\S+\s+([\d.]+|n/a)\s+\S+\s+\S+\s+([KMG]?H/s)")
_HASH_UNIT = {"H/s": 1, "KH/s": 1e3, "MH/s": 1e6, "GH/s": 1e9}


def _parse_hashrate(line):
    m = _XMRIG_SPEED_RE.search(line)
    if not m or m.group(1) == "n/a":
        return None
    try:
        return float(m.group(1)) * _HASH_UNIT.get(m.group(2), 1)
    except ValueError:
        return None


# ------------------------------------------------------------- engine

# Account names are Roblox usernames: [A-Za-z0-9_-]+ (Roblox allows `_` only,
# but `-` costs nothing and older stores may hold one).
ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Hide console windows spawned on Windows.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# What engine_version() reports as its contract. The frontend still compares
# it; native omnidroid ships inside this app, so it is a constant.
ENGINE_CONTRACT = "1.0"


def _configure_engine_on_launch():
    """Point this process at its runtime (OMNI_DATA_DIR: the account store and
    the autoexec folder) on every launch, not only after a first-boot
    download. Never fails the launch."""
    try:
        bootstrap.configure_engine(bootstrap.runtime_dir())
    except Exception as e:  # noqa: BLE001 — engine config must never crash the app launch
        print(f"[omni-exec] engine pre-config skipped: {e}", file=sys.stderr)


def accountsync():
    """The local<->cloud account sync module, imported lazily.

    It imports omnidroid's own accounts module, which on a source checkout means
    touching sys.path — work that has no business happening during GUI startup,
    and that must not run at all in the `--omnidroid` engine child."""
    import accountsync as mod
    return mod


def _run_elevated(inner):
    """Run one PowerShell command line elevated (one UAC prompt). Windows only.

    `inner` is the command to run AS ADMINISTRATOR, as a plain string. It is
    passed through as base64 of its UTF-16LE bytes via `-EncodedCommand`
    rather than interpolated into the outer `-ArgumentList` string: a
    single-quoted `-Command` argument nested inside a single-quoted
    `-ArgumentList` value double-wraps any quote the inner command needs
    (e.g. the path around `-ExclusionPath`), and PowerShell's own quoting
    rules eject it — the exclusion silently never lands. Base64 has no
    quotes or spaces, so the outer wrapping can never corrupt it.
    """
    if sys.platform != "win32":
        return {"ok": False, "error": "not_windows"}
    b64 = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    # -Wait: block until the elevated shell exits, so a caller gets an
    # answer only after the command actually ran (best-effort -- this only
    # proves the elevated process exited, not that e.g. Add-MpPreference
    # itself succeeded; verifying that is out of scope here).
    ps = ("Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait "
          f"-ArgumentList '-NoProfile','-EncodedCommand','{b64}'")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=120)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "elevation_failed", "message": str(e)}


class Api:
    """The backend's whole surface.

    Every public method here is callable from the page as
    `api("<name>", ...args)` — rpc.dispatch looks the name up on this object.
    A leading underscore makes a method internal and unreachable from JS."""

    # How often this machine renews the running lease on its instances. Must be
    # comfortably under the server's RUNNING_LEASE_MS (90 s) so a single missed
    # beat — a slow poll, a hiccup — never flips a live instance to "Stopped"
    # on the user's other machine.
    HEARTBEAT_SECONDS = 25

    # How often a RUNNING app asks whether a new build has been published.
    # 30 minutes: the manifest is a few hundred bytes, and the thing being
    # detected is a human publishing a release, which does not happen on a
    # scale where a tighter poll would tell anyone anything sooner.
    UPDATE_POLL_SECONDS = 600
    # How long a launch's check may take and still count as "at launch".
    LAUNCH_APPLY_SECONDS = 20

    def __init__(self):
        # The write end of the Tauri bridge, set by rpc.serve(). Only _push
        # uses it; a None bridge means nothing is listening yet, which is the
        # case for the brief window between construction and serve().
        self._bridge = None
        self._bootstrapping = False
        # Set while a launch is installing an update by itself. Reported with
        # every check, so a page that loaded after the event still finds out.
        self._launch_update = None
        bootstrap.use_channel_source(self._dist_channel, cloud.token)
        # The omnidroid pool (started on the first launch, not here), and the
        # instance this app started for each account: name -> instance id.
        self._pool = droid.Pool()
        self._instances = {}
        # Accounts this machine last reported as running, so the loop can send
        # exactly one "stopped" on the transition instead of every tick.
        self._known_running = set()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._updating = False
        self._update_stop = threading.Event()
        self._update_thread = None
        # Account-creation batch (one worker thread, sequential accounts).
        self._creation_thread = None
        self._creation_stop = threading.Event()
        # index/total of the account currently being created, for status polls.
        self._creation_at = (0, 0)
        # Mining / earn: enrollment info from the server (minerToken,
        # stratumHost, stratumPort, algos) and the running xmrig Popen(s), if
        # any — a list because "both" mode is two separate processes (one
        # xmrig cannot mine two coins on two backends at once). Restored from
        # disk (see MINING_FILE) rather than hardcoded None: is_installed()
        # is a disk fact that survives a restart, and without this the app
        # would show "installed" with an enrollment nothing can recover
        # short of enrolling again against the server.
        self._mining = _load_mining()
        self._mining_procs = []
        # H/s, last measured per kind — kept even after a kind stops so an
        # idle Earn tab can still show an estimate ("estHashrate").
        self._hashrate = {"cpu": 0.0, "gpu": 0.0}
        self._running_kinds = set()   # kinds with a live xmrig process right now

    # ---- platform ----

    def get_platform(self):
        """'darwin' | 'win32' | 'linux' — the frontend picks its window chrome
        from this: native traffic lights on macOS, our own buttons elsewhere.

        The window CONTROLS are Tauri's now (see frontend/src/window.js); this
        only says which layout to draw."""
        return sys.platform

    # ---- settings ----

    def get_settings(self):
        with _SETTINGS_LOCK:
            return self._read_settings()

    def _read_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        settings = {**DEFAULT_SETTINGS, **saved}
        # Drop what the QEMU engine's settings left behind, at the one point
        # they enter the app, and write the fix straight back so this runs
        # once per install. Best-effort: a settings file that can't be written
        # must not stop the app reading the settings it already has.
        launch = settings.get("launch")
        stale = [k for k in RETIRED_LAUNCH_KEYS if isinstance(launch, dict) and k in launch]
        dead = [k for k in RETIRED_SETTINGS if k in settings]
        if stale or dead:
            if stale:
                settings["launch"] = {k: v for k, v in launch.items() if k not in stale}
            for k in dead:
                settings.pop(k, None)
            try:
                self._write_settings(settings)
            except OSError:
                pass
        return settings

    @staticmethod
    def _write_settings(settings):
        """Write settings.json via a temp file, so a crash mid-write can't
        corrupt it."""
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        cloud.replace_file(tmp, SETTINGS_FILE)

    def save_settings(self, settings):
        with _SETTINGS_LOCK:
            current = self.get_settings()
            if isinstance(settings, dict):
                current.update(settings)
            self._write_settings(current)
            return current

    # ---- editor tabs (persisted whole; the frontend owns the shape) ----

    def get_editor_state(self):
        try:
            with open(EDITOR_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return None
        return state if isinstance(state, dict) else None

    def save_editor_state(self, state):
        if not isinstance(state, dict):
            return {"ok": False, "error": "bad_state"}
        tmp = EDITOR_FILE.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            tmp.replace(EDITOR_FILE)
        except OSError as exc:
            return {"ok": False, "error": "write_failed", "message": str(exc)}
        return {"ok": True}

    # ---- remote execute (Editor tab) ----

    def _exec_base(self):
        base = OMNI_EXEC_BASE
        try:
            saved = self.get_settings()
            if isinstance(saved.get("execBase"), str) and saved["execBase"].strip():
                base = saved["execBase"].strip()
        except Exception:
            pass
        base = base.rstrip("/")
        # Dev mode, absent from release builds (see devserver.py). Last, so it
        # also catches an execBase that names the production server outright.
        return _devserver.redirect(base) if _devserver else base

    def execute_script(self, name, script):
        """Run a Luau script in the LIVE game session for `name` — MANUAL only,
        fired when the user clicks Run. Submits to the OMNI-EXEC bridge; the
        in-game UI polls it, loadstring()s it, and reports the result, which we
        wait briefly for and return so the editor can show ok/output.

        The bridge now refuses an unauthenticated submit, and refuses one aimed
        at an account the signed-in user does not own — so this call carries the
        session token, and a 401/403 is reported as what it is rather than as a
        generic failure."""
        bad = self._bad_name(name)
        if bad:
            return bad
        if not isinstance(script, str) or not script.strip():
            return {"ok": False, "error": "empty_script", "message": "Nothing to run — the editor is empty."}
        if not cloud.signed_in():
            return {"ok": False, "error": "signed_out",
                    "message": "Sign in to run scripts."}
        base = self._exec_base()
        try:
            sub = cloud.request("POST", "/omni/exec/submit",
                                {"channel": name, "script": script}, base=base, timeout=15)
        except cloud.CloudError as exc:
            if exc.error == "not_your_account":
                return {"ok": False, "error": "not_your_account", "message": exc.message}
            if exc.status == 401:
                return {"ok": False, "error": "signed_out",
                        "message": "Your session expired — sign in again."}
            if exc.status == 402:
                return {"ok": False, "error": "subscription_inactive", "message": exc.message}
            return {"ok": False, "error": "unreachable", "message": exc.message}
        if not sub.get("ok"):
            return {"ok": False, "error": "submit_failed", "message": sub.get("error", "submit failed")}
        job_id = sub.get("id")
        if not sub.get("connected"):
            return {"ok": False, "error": "no_session", "id": job_id,
                    "message": f"No live session for '{name}'. Launch the game and let Arceus + the "
                               "OMNI-EXEC UI load, then Run again."}
        deadline = time.time() + 8.0
        while time.time() < deadline:
            try:
                res = cloud.request("GET", f"/omni/exec/result?id={job_id}", base=base, timeout=6)
            except cloud.CloudError:
                res = {}
            if res.get("done"):
                return {"ok": True, "ran": bool(res.get("ok")), "output": res.get("output", ""),
                        "id": job_id, "connected": True}
            time.sleep(0.4)
        return {"ok": True, "ran": None, "pending": True, "id": job_id, "connected": True,
                "output": "Submitted — no result yet (the script may still be running)."}

    # ---- autoexec ----

    def autoexec_dir(self):
        """The host folder whose scripts every instance auto-runs at session
        start. Under the runtime dir (beside accounts.json), which is the exact
        path engine_start reads and pushes at launch, so what the user drops
        here is what runs."""
        import bootstrap
        d = bootstrap.runtime_dir() / "autoexec"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return str(d)

    # One script switched off WITHOUT deleting it. Must match
    # omnidroid/autoexec.py's DISABLED_SUFFIX -- the ENGINE is what actually
    # skips these, this app only renames.
    #
    # A suffix rather than a prefix or a subfolder, because in an autoexec
    # folder the FILENAME IS THE RUN ORDER: `20-loot.lua` ->
    # `20-loot.lua.disabled` keeps its place in the sequence, so switching it
    # back on cannot silently reorder everything around it.
    AUTOEXEC_DISABLED_SUFFIX = ".disabled"

    def list_autoexec(self):
        """The scripts currently in the autoexec folder, in the order they run
        (filename order). Powers the Home widget so the user can see at a glance
        what every instance will execute at start.

        `name` is always the ENABLED name (no `.disabled`), so the UI, the
        editor tabs and the run-order numbering do not change when a script is
        switched off — only `enabled` does. `master` is the folder-wide switch.
        """
        from pathlib import Path
        d = Path(self.autoexec_dir())
        skip = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".zip", ".gz",
                ".exe", ".dll", ".so", ".ico", ".bmp", ".ttf", ".otf"}
        out = []
        try:
            for p in sorted(d.iterdir()):
                if not p.is_file():
                    continue
                off = p.name.lower().endswith(self.AUTOEXEC_DISABLED_SUFFIX)
                stem = p.name[:-len(self.AUTOEXEC_DISABLED_SUFFIX)] if off else p.name
                if Path(stem).suffix.lower() in skip:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                out.append({"name": stem, "bytes": size, "enabled": not off})
        except OSError:
            pass
        return {"ok": True, "dir": str(d), "scripts": out,
                "master": self.autoexec_master()}

    def autoexec_master(self):
        """Is the autoexec folder armed at all? Never raises."""
        try:
            return self.get_settings().get("autoexecEnabled") is not False
        except Exception:      # noqa: BLE001 - a settings problem must not
            return True        # silently disarm the folder

    def set_autoexec_master(self, enabled):
        """Arm or disarm the whole autoexec folder.

        Takes effect on the NEXT launch, not on instances already running:
        the scripts do not live on this machine at run time, they live in the
        exec server's channel for the account, and only a launch rewrites it.
        `_apply_autoexec_env` is what carries the decision to the engine.
        """
        want = bool(enabled)
        try:
            self.save_settings({"autoexecEnabled": want})
        except OSError:
            return {"ok": False, "error": "settings_unwritable"}
        self._apply_autoexec_env()
        return {"ok": True, "master": want,
                "message": ("Autoexec is ON — every script in the folder runs "
                            "in every instance at start."
                            if want else
                            "Autoexec is OFF — nothing auto-runs. Instances "
                            "already running keep whatever they were given; "
                            "the change lands on the next launch.")}

    def _apply_autoexec_env(self):
        """Carry the master switch into the environment autoexec.py reads.

        The push runs in this process now (engine_start), so the environment
        is this process's own. Set it on every launch rather than once at startup so a toggle takes
        effect without restarting the app, and DELETE the variable when armed
        rather than setting it to "0" — the engine reads truthiness, and a
        stale "1" left behind would be a folder that silently never runs.
        """
        if self.autoexec_master():
            os.environ.pop("OMNI_NO_AUTOEXEC", None)
        else:
            os.environ["OMNI_NO_AUTOEXEC"] = "1"

    def set_autoexec_enabled(self, name, enabled):
        """Switch ONE script on or off, by renaming it. Never deletes.

        The rename is the mechanism on purpose: the file stays in the folder,
        keeps its contents and keeps its position in the run order, and the
        engine is the thing that decides to skip it. Nothing here has to stay
        in sync with a list of disabled names in settings.json — the folder is
        still the single source of truth, which is what makes "drop a file in
        and it runs" keep working.
        """
        from pathlib import Path
        want = bool(enabled)
        base = str(name or "")
        if base.lower().endswith(self.AUTOEXEC_DISABLED_SUFFIX):
            base = base[:-len(self.AUTOEXEC_DISABLED_SUFFIX)]
        on_p = self._autoexec_path(base)
        off_p = self._autoexec_path(base + self.AUTOEXEC_DISABLED_SUFFIX)
        if on_p is None or off_p is None:
            return {"ok": False, "error": "bad_name",
                    "message": f"{name!r} is not a file in the autoexec folder."}
        src, dst = (off_p, on_p) if want else (on_p, off_p)
        if not src.exists():
            # Already in the state that was asked for is a SUCCESS, not an
            # error: the UI toggles optimistically and a double-click must not
            # produce a red banner for a folder that is already correct.
            if dst.exists():
                return {"ok": True, "name": base, "enabled": want}
            return {"ok": False, "error": "not_found",
                    "message": f"{base} is not in the autoexec folder."}
        try:
            if dst.exists():
                dst.unlink()
            src.rename(dst)
        except OSError as e:
            return {"ok": False, "error": "rename_failed", "message": str(e)}
        return {"ok": True, "name": base, "enabled": want}

    def _autoexec_path(self, name):
        """Resolve a bare filename inside the autoexec folder, or None. Bare
        means bare: anything that could step outside the folder (separators,
        dot-dots) is refused rather than sanitised."""
        from pathlib import Path
        name = str(name or "").strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            return None
        d = Path(self.autoexec_dir()).resolve()
        p = (d / name).resolve()
        return p if p.parent == d else None

    def _autoexec_live_path(self, name):
        """The file behind `name`, whether or not it is switched off.

        The UI, the editor tabs and `list_autoexec` all speak the ENABLED name
        (`20-loot.lua`), because a tab whose title changed when you flicked a
        switch would be its own bug. On disk a disabled script is
        `20-loot.lua.disabled`, so every read/write has to look for both --
        otherwise switching a script off makes it un-editable, which is the
        opposite of "off, not deleted".
        """
        p = self._autoexec_path(name)
        if p is not None and p.is_file():
            return p
        off = self._autoexec_path(str(name) + self.AUTOEXEC_DISABLED_SUFFIX)
        if off is not None and off.is_file():
            return off
        return p

    def read_autoexec(self, name):
        """One autoexec script's text, for editing in the app's editor."""
        p = self._autoexec_live_path(name)
        if not p or not p.is_file():
            return {"ok": False, "error": "not_found", "name": name}
        try:
            return {"ok": True, "name": name,
                    "content": p.read_text(encoding="utf-8", errors="replace")}
        except OSError as exc:
            return {"ok": False, "error": str(exc), "name": name}

    def save_autoexec(self, name, content=""):
        """Write (or create) one autoexec script. The editor autosaves through
        this, so it is called on the same debounce as editor.json.

        Writes THROUGH to a disabled file rather than resurrecting it: editing
        a script you have switched off must not switch it back on."""
        p = self._autoexec_live_path(name)
        if not p:
            return {"ok": False, "error": "bad_name", "name": name}
        try:
            p.write_text(str(content or ""), encoding="utf-8")
            return {"ok": True, "name": name}
        except OSError as exc:
            return {"ok": False, "error": str(exc), "name": name}

    def rename_autoexec(self, old, new):
        """Rename an autoexec script — filename order IS run order, so renaming
        is how a script moves in the sequence. Refuses to overwrite."""
        src = self._autoexec_path(old)
        dst = self._autoexec_path(new)
        if not src or not src.is_file():
            return {"ok": False, "error": "not_found", "name": old}
        if not dst:
            return {"ok": False, "error": "bad_name", "name": new}
        if dst.exists() and dst != src:
            return {"ok": False, "error": "exists", "name": new}
        try:
            src.rename(dst)
            return {"ok": True, "name": dst.name}
        except OSError as exc:
            return {"ok": False, "error": str(exc), "name": old}

    def open_autoexec_folder(self):
        """Open the autoexec folder in the OS file manager. Every file here is
        run, in filename order, in EVERY instance at session start -- drop a
        .lua in and the next launch executes it."""
        path = self.autoexec_dir()
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606 — a known dir, not user input
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return {"ok": True, "path": path}
        except Exception as exc:  # noqa: BLE001 — report, never crash the app
            return {"ok": False, "error": str(exc), "path": path}

    # ---- stat track (premium) ----
    #
    # Turning Stat Track on writes ONE small file into the autoexec folder, and
    # turning it off deletes it. That is the whole local mechanism, and it is
    # deliberately the same folder the user already owns and can open: a
    # feature whose "on" state is a file you can see and delete is one nobody
    # has to trust a hidden setting for.
    #
    # The file only loadstrings the collector from the server. Keeping the
    # collector server-side means it can be fixed under a fleet that is already
    # running, and that 25 guests cannot end up with 25 versions of it. The
    # premium check is the server's, on every report — this side is a
    # convenience, not a wall.

    STATTRACK_FILE = "10_omni_stattrack.lua"

    def _stattrack_path(self):
        from pathlib import Path
        return Path(self.autoexec_dir()) / self.STATTRACK_FILE

    def _stattrack_body(self):
        base = self._exec_base()
        return (
            "-- OMNI STAT TRACK — written by Omni Executor, safe to delete.\n"
            "--\n"
            "-- Deleting this file (or switching Stat Track off in the app) stops\n"
            "-- the tracker. It reports this account's in-game numbers — gems,\n"
            "-- coins, level, whatever the game will show it — to your Omni\n"
            "-- dashboard while the account is playing.\n"
            "--\n"
            "-- The collector itself lives on the Omni server so it can be fixed\n"
            "-- without re-writing this file on every machine; this is only the\n"
            "-- pointer to it.\n"
            f'loadstring(game:HttpGet("{base}/omni/exec/stattrack.lua", true))()\n'
        )

    def stattrack_status(self):
        """Is the tracker armed on this machine, and where does it report?"""
        path = self._stattrack_path()
        installed = path.exists()
        stale = False
        if installed:
            # A file left behind by an older install can still point at a
            # server this app no longer talks to, which would look "on" while
            # reporting nowhere. Cheap to detect, so say so rather than lie.
            try:
                stale = self._exec_base() not in path.read_text(encoding="utf-8")
            except OSError:
                stale = True
        return {
            "ok": True,
            "enabled": installed and not stale,
            "installed": installed,
            "stale": stale,
            "file": str(path),
            "dir": self.autoexec_dir(),
            "base": self._exec_base(),
        }

    def stattrack_set(self, enabled):
        """Arm or disarm the tracker by writing or deleting the autoexec file.

        Rewrites the file when it is already there: that is what repairs a
        stale pointer, and it makes the call idempotent so the toggle can be
        driven from anywhere without checking first."""
        path = self._stattrack_path()
        try:
            if enabled:
                path.write_text(self._stattrack_body(), encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            return {"ok": False, "error": "write_failed", "message": str(exc),
                    "file": str(path)}
        # The engine pushes the autoexec folder to the server on every launch,
        # so a change here is live on the next start with nothing to rebuild —
        # but it does NOT reach an instance that is already running.
        return {**self.stattrack_status(), "changed": True,
                "message": ("Stat Track is on — it starts reporting on the next launch."
                            if enabled else "Stat Track is off.")}

    def stattrack_stats(self):
        """The dashboard payload: every account with its latest reading.

        A free account gets 402 from the server, which comes back here as
        `error: subscription_inactive` so the tab renders the locked state
        instead of an error — the same distinction the paywall middleware was
        written to preserve."""
        if not cloud.signed_in():
            return {"ok": False, "error": "signed_out", "message": "Sign in to see your stats."}
        try:
            data = cloud.list_stats()
        except cloud.CloudError as exc:
            if exc.status == 402:
                return {"ok": False, "error": "subscription_inactive", "message": exc.message}
            if exc.status == 401:
                return {"ok": False, "error": "signed_out",
                        "message": "Your session expired — sign in again."}
            return {"ok": False, "error": "unreachable", "message": exc.message}
        return {"ok": True, **data}

    def stattrack_account(self, username):
        """One account's reading plus its recent history, for the detail pane."""
        bad = self._bad_name(username)
        if bad:
            return bad
        if not cloud.signed_in():
            return {"ok": False, "error": "signed_out", "message": "Sign in to see your stats."}
        try:
            return {"ok": True, **cloud.account_stats(username)}
        except cloud.CloudError as exc:
            if exc.status == 402:
                return {"ok": False, "error": "subscription_inactive", "message": exc.message}
            return {"ok": False, "error": "unreachable", "message": exc.message}

    # ---- mining / earn ----

    def mining_status(self):
        # Local facts (disk state, this process's own child procs) win over
        # whatever the server reports under the same key — the server cannot
        # know a process this machine spawned better than this machine does.
        local = {
            "installed": miner.is_installed(),
            "running": bool(self._mining_procs),
            # Live total across whatever kinds are actually running right now.
            "hashrate": sum(self._hashrate[k] for k in self._running_kinds),
            # Last-known per-kind reading, kept after a kind stops, so the UI
            # can still estimate credits/hour for a kind that isn't running.
            "estHashrate": dict(self._hashrate),
        }
        try:
            remote = cloud.mining_status()
        except cloud.CloudError as e:
            return {"ok": True, **local, "enrolled": bool(self._mining), "remote_error": str(e)}
        merged = {"ok": True, **remote, **local}
        # "enrolled" is LOCAL-only: Start needs self._mining (the miner token +
        # stratum host, restored from MINING_FILE on restart). A server-side
        # MinerSession without a local token is useless — mining_start would
        # return not_enrolled — so it must NOT flip the UI to the Start panel.
        # If the local token is missing (e.g. mining.json was cleared) the tab
        # shows the enroll action again, and re-enrolling mints a fresh one.
        merged["enrolled"] = bool(self._mining)
        return merged

    def mining_enroll(self):
        try:
            info = cloud.mining_enroll()
        except cloud.CloudError as e:
            return {"ok": False, "error": e.error or "enroll_failed", "message": str(e)}
        self._mining = info
        try:
            _save_mining(info)
        except OSError:
            pass  # a restart before the next enroll would just re-enroll
        try:
            miner.install(progress=lambda p: self._push("mining-progress", p))
        except Exception as e:  # noqa: BLE001
            self._push("mining-error", {"error": str(e)})
            return {"ok": False, "error": "download_failed", "message": str(e)}
        self._push("mining-done", {"phase": "install"})
        return {"ok": True, **info}

    # One xmrig process is single-algo/single-pool: it cannot mine Monero on
    # CPU and Ravencoin on GPU at the same time in one process. "both" is
    # therefore two SEPARATE processes, each with its own coin/algo/backend
    # flags, tracked as a list rather than a single Popen.
    _MINING_KIND_ARGS = {
        # RandomX runs on CPU by default in xmrig — no GPU-disabling flags
        # needed (and none wanted: the shipped xmrig build REJECTS
        # --no-cuda with "unknown option", which used to abort the process
        # before it could ever mine).
        "cpu": lambda token: ["--user", f"{token}.xmr", "--algo", "rx/0"],
        # BETA is NVIDIA-only: select the NVIDIA OpenCL platform explicitly
        # (xmrig's --opencl default hunts for an AMD platform and finds no GPU
        # on NVIDIA boxes). --cuda is intentionally omitted: the shipped xmrig
        # has no bundled CUDA plugin, so OpenCL is the working backend. AMD
        # support (a different platform) is a post-beta item.
        "gpu": lambda token: ["--user", f"{token}.rvn", "--algo", "kawpow",
                              "--opencl", "--opencl-platform=NVIDIA", "--no-cpu"],
    }

    # Eco mode: mine on the slack a working machine leaves, instead of at
    # full tilt. The two backends need different levers because a GPU game
    # and GPU mining fight over the same card while a CPU miner and a GPU
    # game do not:
    #  - CPU: a quarter of the cores at below-normal priority — the OS then
    #    hands foreground apps the cores first, so the desktop/game stays
    #    smooth and mining takes only what's idle.
    #  - GPU: there is no OpenCL intensity knob on this xmrig build, and a
    #    3D game needs the whole card anyway, so the miner steps aside while
    #    the user is active and resumes after this many seconds of idle.
    _ECO_CPU_THREADS_HINT = "25"
    _ECO_CPU_PRIORITY = "1"
    _ECO_GPU_PAUSE_ON_ACTIVE_S = "10"

    def _spawn_miner(self, kind, intensity=50, eco=False):
        """One xmrig Popen for a single backend+coin ('cpu' or 'gpu').

        `eco` throttles for a still-usable machine — see the _ECO_* constants
        above for the per-backend levers and why they differ."""
        token = self._mining["minerToken"]
        host = self._mining["stratumHost"]
        port = self._mining["stratumPort"]
        args = [str(miner.binary_path()), "--url", f"{host}:{port}",
                "--pass", "x", "--donate-level", "0",
                *self._MINING_KIND_ARGS[kind](token)]
        if kind == "cpu":
            if eco:
                args += ["--cpu-max-threads-hint", self._ECO_CPU_THREADS_HINT,
                         "--cpu-priority", self._ECO_CPU_PRIORITY]
            else:
                try:
                    pct = max(1, min(100, int(intensity)))
                except (TypeError, ValueError):
                    pct = 50
                args += ["--cpu-max-threads-hint", str(pct)]
        elif kind == "gpu" and eco:
            args += ["--pause-on-active", self._ECO_GPU_PAUSE_ON_ACTIVE_S]
        return subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            # DEVNULL, not inherited: this process's stdin is the JSON-RPC
            # bridge's request stream (rpc.py), and a child that can read it
            # would be reading commands meant for us.
            stdin=subprocess.DEVNULL,
            text=True, bufsize=1, encoding="utf-8", errors="replace")

    def mining_start(self, mode="gpu", intensity=50, eco=False):
        if not self._mining:
            return {"ok": False, "error": "not_enrolled"}
        if not miner.is_installed():
            return {"ok": False, "error": "not_installed"}
        if self._mining_procs:
            return {"ok": False, "error": "already_running"}
        kinds = {"cpu": ["cpu"], "gpu": ["gpu"], "both": ["cpu", "gpu"]}.get(mode)
        if kinds is None:
            return {"ok": False, "error": "bad_mode", "message": f"unknown mode {mode!r}"}
        spawned = []
        spawned_kinds = []
        try:
            for kind in kinds:
                proc = self._spawn_miner(kind, intensity, eco)
                spawned.append(proc)
                spawned_kinds.append(kind)
                self._mining_procs.append(proc)
                self._running_kinds.add(kind)
                threading.Thread(target=self._mining_reader, args=(proc, kind),
                                 daemon=True).start()
        except Exception as e:  # noqa: BLE001
            for proc, kind in zip(spawned, spawned_kinds):
                try:
                    self._mining_procs.remove(proc)
                except ValueError:
                    # a fast-exiting proc's own reader may have already
                    # removed it — not an error, just a race with cleanup.
                    pass
                self._running_kinds.discard(kind)
                proc.kill()
            return {"ok": False, "error": "spawn_failed", "message": str(e)}
        return {"ok": True, "started": True, "mode": mode, "procs": len(spawned)}

    def _mining_reader(self, proc, kind):
        if proc.stdout:
            for line in proc.stdout:
                line = line.rstrip()
                hr = _parse_hashrate(line)
                if hr is not None:
                    self._hashrate[kind] = hr
                self._push("mining-stat", {"line": line, "kind": kind})
        code = proc.wait()
        try:
            self._mining_procs.remove(proc)
        except ValueError:
            pass
        # Do NOT zero self._hashrate[kind] here — the last measured value is
        # kept so an idle Earn tab can still show an estCreditsHr for this
        # kind. Only the "currently running" marker goes away.
        self._running_kinds.discard(kind)
        self._push("mining-done", {"phase": "run", "kind": kind, "code": code})

    def mining_stop(self):
        procs, self._mining_procs = self._mining_procs, []
        self._running_kinds.clear()   # self._hashrate values are kept
        if not procs:
            return {"ok": True, "stopped": 0}
        for proc in procs:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
            except Exception:  # noqa: BLE001 — one stuck proc must not skip the rest
                pass
        return {"ok": True, "stopped": len(procs)}

    def mining_add_defender_exclusion(self):
        path = str(miner.miner_dir()).replace("'", "''")
        inner = f"Add-MpPreference -ExclusionPath '{path}'"
        return _run_elevated(inner)

    def buy_day_with_credits(self):
        try:
            data = cloud.redeem_credits_for_day()
        except cloud.CloudError as e:
            return {"ok": False, "error": e.error or "purchase_failed", "message": str(e)}
        return {"ok": True, **data}

    # ---- account / sign-in ----

    def auth_status(self):
        """Who is signed in on this machine, and is the plan good?

        Called on every launch to decide between the sign-in screen and the app.
        A server that cannot be reached must NOT sign the user out: the token is
        still valid, the instances on this machine are still theirs, and kicking
        them to a login form they cannot complete offline would be the worst
        possible response to a flaky network."""
        saved = cloud.auth()
        dev = cloud.device()
        base = {"ok": True, "device": {"id": dev["deviceId"], "name": dev["deviceName"]},
                "apiBase": self._api_base()}
        if not saved.get("token"):
            return {**base, "signedIn": False}
        out = {**base, "signedIn": True, "email": saved.get("email"),
               "username": saved.get("username"),
               "subscription": saved.get("subscription") or {}, "stale": True}
        try:
            fresh = cloud.me()
            out.update(email=fresh.get("email"), username=fresh.get("username"),
                       subscription=fresh.get("subscription") or {}, stale=False)
        except cloud.CloudError as exc:
            if exc.status == 401:
                cloud.sign_out()
                return {**base, "signedIn": False, "message": "Your session expired — sign in again."}
            out["message"] = exc.message      # offline: keep working with what we have
        return out

    def _api_base(self):
        try:
            return cloud.api_base(self.get_settings())
        except Exception:  # noqa: BLE001
            return cloud.api_base()

    def auth_register(self, email, username, password):
        """Create a free account. Sign-up takes no license key — see
        keys_redeem for how an account gets a plan."""
        try:
            data = cloud.register(email, username, password)
        except cloud.CloudError as exc:
            return {"ok": False, "error": exc.error or "register_failed", "message": exc.message}
        self._after_sign_in()
        return {"ok": True, **data}

    def keys_redeem(self, code):
        """Redeem a license key against the signed-in account: adds the key's
        days to whatever is left, or converts to lifetime."""
        if not cloud.signed_in():
            return {"ok": False, "error": "signed_out", "message": "Sign in first."}
        if not (code or "").strip():
            return {"ok": False, "error": "no_code", "message": "Enter a key first."}
        try:
            subscription = cloud.redeem(code)
        except cloud.CloudError as exc:
            return {"ok": False, "error": exc.error or "redeem_failed", "message": exc.message}
        # The fresh subscription rides back on the response, and the caller
        # re-asks auth_status (same as sign-out does) so the Home greeting stops
        # saying "Free" on an account that just went premium.
        return {"ok": True, "subscription": subscription}

    def auth_login(self, email, password):
        try:
            data = cloud.login(email, password)
        except cloud.CloudError as exc:
            return {"ok": False, "error": exc.error or "login_failed", "message": exc.message}
        self._after_sign_in()
        return {"ok": True, **data}

    def auth_logout(self):
        """Sign out: release this machine's leases, then drop the accounts that
        were pulled down for the departing user.

        Both halves matter. Without the lease release, the accounts this device
        is running keep their lease until it expires and the next user sees
        phantom "Running on <this machine>" rows for accounts that are not
        theirs. Without the purge, the next user's first sync UPLOADS the
        previous user's cookies into their own cloud account — measured, on the
        Mac, during the multi-device run. The purged records live in the cloud
        and come back on the owner's next sign-in; accounts nobody has claimed
        are left alone, because those exist only here."""
        departing = (cloud.auth() or {}).get("userId") or ""
        try:
            self._release_all_leases()
        except Exception:  # noqa: BLE001 — never block a sign-out on the network
            pass
        removed = []
        if departing:
            try:
                removed = accountsync().forget_user(departing)
            except Exception as exc:  # noqa: BLE001
                print(f"[omni-exec] could not purge local accounts: {exc}",
                      file=sys.stderr)
        cloud.sign_out()
        self._known_running = set()
        self._push("auth-changed", {"signedIn": False})
        self._push("accounts-changed", {})
        return {"ok": True, "removed": removed}

    def _after_sign_in(self):
        """Pull this user's accounts down (and push anything only on this
        machine) as soon as they sign in, in the background so the window can
        render immediately."""
        def _run():
            try:
                res = accountsync().sync(progress=lambda line: self._push(
                    "engine-progress", {"scope": "sync", "line": line}))
                self._push("accounts-changed", {})
                self._push("sync-done", res)
            except Exception as exc:  # noqa: BLE001 — surface, never crash
                self._push("sync-error", {"message": str(exc)})
        threading.Thread(target=_run, daemon=True).start()

    def cloud_sync(self):
        """Manual "sync now" (Settings). Same work as the post-sign-in sync,
        but synchronous so the button can report a result."""
        if not cloud.signed_in():
            return {"ok": False, "error": "signed_out", "message": "Sign in first."}
        try:
            res = accountsync().sync(progress=lambda line: self._push(
                "engine-progress", {"scope": "sync", "line": line}))
        except cloud.CloudError as exc:
            return {"ok": False, "error": exc.error or "sync_failed", "message": exc.message}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "sync_failed", "message": str(exc)}
        self._push("accounts-changed", {})
        return res

    def cloud_presence(self):
        """username -> presence, for the instances list. Returns an empty map
        (never an error) when signed out or offline: presence is decoration on
        top of the engine's own local truth, and losing it must not blank the
        list."""
        if not cloud.signed_in():
            return {"ok": True, "presence": {}}
        try:
            return {"ok": True, "presence": accountsync().presence_map()}
        except Exception:  # noqa: BLE001
            return {"ok": True, "presence": {}}

    def set_device_name(self, name):
        dev = cloud.set_device_name(name)
        return {"ok": True, "device": {"id": dev["deviceId"], "name": dev["deviceName"]}}

    # ---- updates ----

    def update_check(self):
        """What is out of date: the base images/offsets, this app, or neither.

        Answers from the manifest only — no downloads — so it is cheap enough
        to run on every launch. Offline is reported as `ok: false` with the
        reason, never as "up to date": telling someone they are current when
        you could not reach the server is how a machine sits on a broken base
        image for a week."""
        return self._update_report()

    def _update_report(self):
        """The check, plus everything the update UI reads alongside it. One
        place, so a manual check can't answer with less than the background one
        does: leaving `staged` out made a downloaded build's Restart button
        vanish on the next refresh."""
        import updates
        report = updates.check()
        report["staged"] = updates.staged_info()
        report["autoUpdate"] = self._auto_update_enabled()
        report["channel"] = self._channel_state()
        report["launchUpdate"] = self._launch_update
        return report

    def _channel_state(self):
        """Which update channel this machine follows.

        `picked` is what Settings saved, `active` is what is really used: a
        machine set to Dev that signs in to a normal account follows Prod, and
        goes back to Dev when a dev account signs in. The server enforces this
        too, so editing settings.json gets nobody a dev build."""
        allowed = cloud.auth().get("updateChannels") or ["prod", "beta"]
        try:
            picked = self.get_settings().get("updateChannel") or "prod"
        except Exception:  # noqa: BLE001
            picked = "prod"
        return {"picked": picked, "allowed": allowed,
                "active": picked if picked in allowed else "prod"}

    def _dist_channel(self):
        return bootstrap.UPDATE_CHANNELS.get(self._channel_state()["active"],
                                             bootstrap.NATIVE_CHANNEL)

    def update_set_channel(self, name):
        """Follow a different update channel, starting now."""
        import updates
        if name not in self._channel_state()["allowed"]:
            return {"ok": False, "error": "channel_forbidden",
                    "message": "This account can't use that channel."}
        if self._updating:
            return {"ok": False, "error": "already_running",
                    "message": "An update is downloading. Try again when it's finished."}
        self.save_settings({"updateChannel": name})
        # A build downloaded for the old channel is not what they just chose.
        updates.discard_staged()
        report = self._update_report()

        # Fetch anything newer straight away rather than at the next poll.
        def _tick():
            try:
                self._update_tick()
            except Exception as exc:  # noqa: BLE001 — never break a running app
                self._push("update-status", {"ok": False, "error": str(exc)})
        threading.Thread(target=_tick, daemon=True).start()
        return report

    def _check_updates_on_launch(self):
        """Start the update watcher: check now, then keep checking.

        Off the startup path deliberately: the manifest is a network round trip
        and the window must not wait on it. Every result arrives as an event,
        so a slow or unreachable server costs nothing visible."""
        if self._update_thread and self._update_thread.is_alive():
            return
        self._update_thread = threading.Thread(target=self._update_watch_loop,
                                               daemon=True)
        self._update_thread.start()

    def _update_watch_loop(self):
        """Check at launch and every UPDATE_POLL_SECONDS after that.

        A release published while someone has the app open used to be invisible
        until they restarted — which, for the app most likely to be left open
        for hours, meant the update banner was seen mainly by people who did
        not need it. The loop is a daemon thread and every iteration is
        wrapped: a check must never be able to end the process it is checking
        for.
        """
        first = True
        while not self._update_stop.is_set():
            try:
                self._update_tick(launch=first)
            except Exception as exc:  # noqa: BLE001 — never break a running app
                self._push("update-status", {"ok": False, "error": str(exc)})
            first = False
            self._update_stop.wait(self.UPDATE_POLL_SECONDS)

    def _update_tick(self, launch=False):
        """One check, plus whatever it implies.

        `launch` distinguishes the two behaviours the product wants, and the
        distinction is about what the user is in the middle of:

          at launch    nobody is doing anything yet, so an update applies
                       ITSELF: an "Updating" screen covers the app while it
                       downloads, the window closes, the build is swapped and
                       the new version opens.
          while open   they ARE doing something. The download is still
                       automatic and invisible, but the restart is theirs to
                       schedule: the UI shows a banner with a button, never a
                       surprise.

        A launch only counts while the check came back quickly, because a window
        that closes itself half a minute after someone started using it is a
        surprise too. may_auto_apply stops a bad build restarting in a loop.
        """
        import updates
        started = time.monotonic()
        report = self._update_report()
        self._push("update-status", report)

        if not report.get("ok") or not report.get("app", {}).get("update"):
            return
        if not report["app"].get("canApply") or not report["autoUpdate"]:
            return

        version = report["app"].get("available")
        auto = (launch
                and time.monotonic() - started <= self.LAUNCH_APPLY_SECONDS
                and updates.may_auto_apply(version))
        if auto:
            self._launch_update = {"version": version, "closing": False}
            self._push("update-applying", {"version": version})

        staged = report.get("staged")
        if not staged or staged.get("version") != version:
            if self._updating:
                self._launch_update = None
                self._push("update-error", {"scope": "app", "auto": True,
                                            "message": "already downloading"})
                return
            staged = self._stage_app_quietly(version)
            if not staged:
                # _stage_app_quietly has told the UI, which clears the screen.
                self._launch_update = None
                return

        if auto:
            updates.note_auto_apply(version)
            res = self.update_app_restart()
            if res.get("ok"):
                # Everything is in place, and only the window has to go. The
                # window is the shell's, so the page closes it (UpdateBanner).
                self._launch_update = {"version": version, "closing": True}
                self._push("update-applying", {"version": version, "closing": True})
                return
            self._launch_update = None
            self._push("update-error", {"scope": "app", "auto": True,
                                        "message": res.get("message")})

        # The banner is driven by the staged state. This event says it just landed.
        self._push("update-ready", {"version": version, "staged": staged,
                                    "launch": bool(launch)})

    def _stage_app_quietly(self, version):
        """Download and stage a build with no user interaction. Returns the
        staged info dict, or None if it did not land.

        SYNCHRONOUS, unlike update_app(): the caller is already on the watcher
        thread and needs to know whether to go on to apply it. `_updating` is
        still held so the Settings buttons cannot start a second download over
        the top of this one."""
        import updates
        self._updating = True
        try:
            updates.stage_app(
                progress=lambda p: self._push("update-progress",
                                              {**p, "auto": True}))
            info = updates.staged_info()
            if info:
                self._push("update-done", {"scope": "app", "auto": True,
                                           "staged": info})
            return info
        except Exception as exc:  # noqa: BLE001
            # Quiet by design: a failed background download is not something to
            # interrupt anyone about, and the next tick will try again. The UI
            # still hears about it so Settings can show it.
            self._push("update-error", {"scope": "app", "auto": True,
                                        "message": str(exc)})
            return None
        finally:
            self._updating = False

    def _auto_update_enabled(self):
        try:
            return self.get_settings().get("autoUpdate", True) is not False
        except Exception:  # noqa: BLE001
            return True

    def update_runtime(self):
        """Download the changed base images/offsets.

        Refused while anything is running: these files are open by a live QEMU,
        so replacing them would either fail halfway (Windows holds the lock) or
        pull an image out from under a booted guest."""
        import updates
        live = self._running_names()
        if live:
            return {"ok": False, "error": "instances_running",
                    "message": f"Stop {', '.join(sorted(live))} first — the base "
                               f"images are in use by a running instance."}
        if self._updating:
            return {"ok": False, "error": "already_running"}
        self._updating = True

        def _run():
            try:
                res = updates.apply_runtime(
                    progress=lambda p: self._push("update-progress", p))
                bootstrap.configure_engine(bootstrap.runtime_dir())
                self._push("update-done", {"scope": "runtime", **res})
            except Exception as exc:  # noqa: BLE001
                self._push("update-error", {"scope": "runtime", "message": str(exc)})
            finally:
                self._updating = False
                self._check_updates_on_launch()
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "started": True}

    def update_app(self, allow_downgrade=False):
        """Download a new build of this app and stage it.

        Staging only. The swap needs this process gone, so it happens in
        update_app_restart() — which is also the point where the user gets to
        say when. `allow_downgrade` is the Settings button for moving to an
        older build on a steadier channel; nothing automatic ever passes it."""
        import updates
        ok, reason = updates.can_apply_app()
        if not ok:
            return {"ok": False, "error": "not_applicable", "message": reason}
        if self._updating:
            return {"ok": False, "error": "already_running"}
        self._updating = True

        def _run():
            try:
                build = updates.stage_app(
                    progress=lambda p: self._push("update-progress", p),
                    allow_downgrade=bool(allow_downgrade))
                self._push("update-done", {"scope": "app", "build": str(build),
                                           "staged": updates.staged_info()})
            except Exception as exc:  # noqa: BLE001
                self._push("update-error", {"scope": "app", "message": str(exc)})
            finally:
                self._updating = False
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "started": True}

    def update_app_restart(self):
        """Hand the swap to the staged build and close this window.

        This does NOT require instances to be stopped, and an earlier version
        that did was simply wrong: QEMU is spawned detached, so the VMs outlive
        this process — killing the GUI and watching them keep running is how
        that was established. The presence lease is 90 s and the app is back in
        a few, so a restart does not even lapse it. Refusing here meant the
        Install button did nothing whenever anything was running, which is most
        of the time.

        The RUNTIME update still requires a stop, and that distinction is real:
        those files are open by a live QEMU.

        ⚠ THIS DOES NOT CLOSE THE WINDOW, AND MUST NOT TRY. It used to end with
        `threading.Timer(0.6, self.close).start()` -- a leftover from pywebview,
        where the Api object owned a window and had a `close()`. Under Tauri the
        WINDOW IS THE SHELL'S: this process is a child speaking JSON over stdio
        and has no handle on it. So the timer raised

            AttributeError: 'Api' object has no attribute 'close'

        on a background thread, the window stayed open, and the helper -- which
        waits for this pid to disappear before it touches anything -- sat out
        its 90 s and gave up. **Every auto-update was dead in the water**, and
        the error was visible only inside the update modal, which is the one
        place nobody screenshots. Found 2026-09-02 by clicking the button.

        The caller closes the window (UpdateBanner's restartIntoUpdate ->
        window.js `close()`), which ends the shell, which ends this process --
        and the shell holds `omni-exec.exe` open, so it has to be the shell that
        goes, not just the backend."""
        import updates
        try:
            pid = updates.launch_apply()
        except updates.UpdateError as exc:
            return {"ok": False, "error": "apply_failed", "message": str(exc)}
        return {"ok": True, "helper_pid": pid, "close_window": True}

    # ---- presence heartbeat ----

    def _beat_now(self, name, state, mode=None, place=None):
        """Report one account's state immediately, off the UI thread.

        An account the SERVER does not know about yet (added on this machine and
        not yet synced) answers 404; push it up and retry once, so a launch is
        never invisible to the user's other devices just because the sync had
        not run."""
        if not cloud.signed_in():
            return

        def _run():
            try:
                cloud.set_state(name, state, mode=mode, place_id=place)
            except cloud.CloudError as exc:
                if exc.status != 404:
                    return
                try:
                    local = accountsync().read_local().get(name)
                    if local and local.get("cookie"):
                        cloud.push_account(name, cookie=local["cookie"],
                                           user_id=local.get("user_id"))
                        cloud.set_state(name, state, mode=mode, place_id=place)
                except Exception:  # noqa: BLE001
                    pass
            if state == "running":
                self._known_running.add(name)
            else:
                self._known_running.discard(name)

        threading.Thread(target=_run, daemon=True).start()

    def _beat_once(self):
        if not cloud.signed_in():
            self._known_running = set()
            return
        live = self._running_names()
        if live is None:
            return
        for name in live:
            try:
                cloud.set_state(name, "running")
            except cloud.CloudError:
                pass                          # offline: the lease lapses, which is correct
        for name in self._known_running - live:
            try:
                cloud.set_state(name, "stopped")
            except cloud.CloudError:
                pass
        self._known_running = live

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(self.HEARTBEAT_SECONDS):
            try:
                self._beat_once()
            except Exception:  # noqa: BLE001 — a heartbeat must never kill the app
                pass

    def start_heartbeat(self):
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _release_all_leases(self):
        """Tell the server this machine no longer runs anything it claimed."""
        for name in list(self._known_running):
            try:
                cloud.set_state(name, "stopped")
            except cloud.CloudError:
                pass
        self._known_running = set()

    # ---- engine ----

    def _push(self, event, payload=None):
        """Fire an event at the page (arrives as `window.omniEvent`).

        Was `evaluate_js` into pywebview's window; it is now a frame on the
        bridge, which the Tauri shell re-emits and api.js turns back into the
        same `window.omniEvent(event, payload)` call the frontend always had.
        Best-effort by design: these fire from engine progress callbacks on
        whatever thread ran the call, and a closing window must not turn one
        into an error."""
        bridge = self._bridge
        if bridge is None:
            return
        bridge.push(event, payload)

    def _progress(self, scope):
        return lambda line: self._push("engine-progress", {"scope": scope, "line": line})

    # ---- bootstrap (first-boot runtime install) ----

    def bootstrap_status(self):
        rt = bootstrap.runtime_dir()
        installed = bootstrap.installed_state(rt)
        eng = bootstrap.engine_ready(rt)
        error = None
        ready = False
        try:
            manifest = bootstrap.read_manifest(bootstrap.dist_base())
            # Ask plan_downloads, rather than re-deriving readiness by
            # comparing every manifest entry. It is the ONE place that knows
            # which artifacts are actually installable — app builds
            # (kind: "app") are neither downloaded nor recorded here, and
            # counting one made every installed client fall back into setup
            # the first time an app build was published.
            ready = not bootstrap.plan_downloads(manifest, installed)
        except bootstrap.BootstrapError as e:
            error = str(e)
            ready = bool(installed.get("artifacts"))  # offline-tolerant
        # The files on disk are part of being ready: a receipt for a bundle
        # somebody deleted by hand is not an engine.
        ready = ready and eng.get("tools_ok", False)
        # Running from source there is nothing to download: droid.py finds the
        # sibling omnidroid checkout's build and the APK beside the repo.
        if not getattr(sys, "frozen", False) and droid.bundle_dir() and droid.apk_path():
            ready = True
        return {"ok": True, "ready": ready, "installed": installed.get("artifacts", {}),
                "omnidroid_ok": eng.get("omnidroid_ok", False),
                "apk_ok": eng.get("apk_ok", False),
                "tools_ok": eng.get("tools_ok", False),
                "error": error}

    def bootstrap_start(self):
        if self._bootstrapping:
            return {"ok": False, "error": "already running"}
        self._bootstrapping = True
        def _run():
            try:
                rt = bootstrap.runtime_dir()
                res = bootstrap.ensure_runtime(progress=lambda p: self._push("bootstrap-progress", p))
                bootstrap.configure_engine(rt)
                self._push("bootstrap-done", res)
            except Exception as e:  # noqa: BLE001 — surface any failure to the UI
                self._push("bootstrap-error", {"error": str(e)})
            finally:
                self._bootstrapping = False
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "started": True}

    @staticmethod
    def _bad_name(name):
        if isinstance(name, str) and ACCOUNT_NAME_RE.match(name):
            return None
        return {
            "ok": False,
            "error": "bad_account_name",
            "message": "Account names may only contain letters, digits, '_' and '-' (no spaces).",
        }

    def engine_version(self):
        """What engine this is. The UI used to gate on a contract handshake
        with the bundled QEMU engine (its `version --json`, its advertised
        command list, `--help` probes for flags); native omnidroid is shipped
        WITH this app and moves with it, so there is no version skew left to
        detect. The fields the UI still reads are kept."""
        return {"ok": True, "engine": "omnidroid", "contract": ENGINE_CONTRACT,
                "arch_aware": True, "missing_commands": [],
                "bundle": droid.bundle_version()}

    def engine_doctor(self):
        """Readiness: is there an omnidroid and an APK, and does the pool
        answer if it is up. Never STARTS the pool -- that is a launch's job,
        and booting Androids to answer a status poll would be a surprise."""
        home = droid.bundle_dir()
        apk = droid.apk_path()
        if home is None:
            return {"ok": False, "ready": False, "error": "engine_missing",
                    "message": "omnidroid is not installed yet -- finish setup to download it."}
        if apk is None:
            return {"ok": False, "ready": False, "error": "apk_missing",
                    "message": "The executor APK is missing -- finish setup to download it."}
        return {"ok": True, "ready": True, "omnidroid_dir": str(home),
                "apk": str(apk), "bundle": droid.bundle_version(),
                "pool_port": self._pool.port, "pool_up": self._pool._alive()}

    def engine_list(self):
        """Every saved Roblox account, with what its instance is doing.

        The accounts are the store's (robloxaccounts.py); an account is
        "running" while the instance this app started for it is alive in the
        pool. Instances the pool has that this app did not start (the person's
        own, from the omnidroid app) are not accounts and are not listed."""
        try:
            stored = accountsync().read_local()
        except Exception as e:  # noqa: BLE001 - an unreadable store is an error the UI can show
            return {"ok": False, "error": "accounts_unreadable", "message": str(e), "accounts": []}
        live = self._pool.instances() if self._instances else {}
        out = []
        for name, rec in sorted(stored.items(), key=lambda kv: kv[0].lower()):
            inst = live.get(self._instances.get(name))
            state = inst.get("state") if inst else None
            out.append({
                "name": name,
                "user_id": rec.get("user_id"),
                "custom_name": rec.get("custom_name"),
                "place_id": rec.get("place_id"),
                "running": droid.is_live(state),
                "state": state or "stopped",
                "instance_id": inst.get("instance_id") if inst else None,
                "up_s": inst.get("up_s") if inst else None,
                "details": inst.get("details") if inst else None,
            })
        return {"ok": True, "accounts": out}

    def _running_names(self):
        """Accounts whose instance is alive on this machine right now, or None
        when that cannot be read (then nothing is reported or changed)."""
        if not self._instances:
            return set()
        live = self._pool.instances()
        return {name for name, iid in self._instances.items()
                if droid.is_live((live.get(iid) or {}).get("state"))}

    def _publish_account(self, result):
        """Upload a freshly-captured Roblox account to the signed-in Omni user.

        Adding an account is the moment it becomes worth having elsewhere, so it
        goes up immediately instead of waiting for the next sync — that is what
        makes "add it on the PC, launch it on the Mac" work without a thought.
        Best-effort: a failure here must not turn a successful local login into
        an error the user sees."""
        name = result.get("username") or result.get("name") if isinstance(result, dict) else None
        if not name or not result.get("ok") or not cloud.signed_in():
            return

        def _run():
            try:
                rec = accountsync().read_local().get(name) or {}
                if rec.get("cookie"):
                    cloud.push_account(name, cookie=rec["cookie"], user_id=rec.get("user_id"))
                    self._push("sync-done", {"ok": True, "pushed": [name], "pulled": []})
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _login_result(res):
        """The store's answer in the shape the UI reads, never the cookie."""
        if not isinstance(res, dict):
            return {"ok": False, "error": "login_failed", "message": "The sign-in returned nothing."}
        out = {k: v for k, v in res.items() if k != "cookie"}
        if out.get("ok") and out.get("username"):
            out.setdefault("name", out["username"])
        return out

    def engine_login_browser(self):
        """Add an account by interactive Roblox sign-in in a real browser. The
        person signs in; the cookie is picked up once Roblox confirms it, and
        the account is saved under its Roblox username."""
        import robloxaccounts
        say = self._progress("login")
        try:
            res = robloxaccounts.capture_login(accountsync()._local_store_root(),
                                               on_status=say)
        except Exception as e:  # noqa: BLE001 - a browser that will not start is a message, not a crash
            res = {"ok": False, "error": "login_failed", "message": f"{type(e).__name__}: {e}"}
        res = self._login_result(res)
        self._publish_account(res)
        self._push("accounts-changed", {})
        return res

    def engine_login_token(self, token):
        """Add an account from a pasted Roblox cookie. Verified before it is
        trusted (a headless browser must see it as a signed-in session), and
        never written anywhere but the store."""
        if not isinstance(token, str) or not token.strip():
            return {"ok": False, "error": "bad_token",
                    "message": "A Roblox cookie/token is required."}
        import robloxaccounts
        try:
            res = robloxaccounts.capture_login_from_cookie(
                accountsync()._local_store_root(), token.strip(),
                on_status=self._progress("login"))
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": "login_failed", "message": f"{type(e).__name__}: {e}"}
        res = self._login_result(res)
        self._publish_account(res)
        self._push("accounts-changed", {})
        return res

    # ---- network ----

    def net_probe(self):
        """Latency and reachability for Roblox, the Omni server, and the
        outbound proxy when one is configured.

        The Network tab polls this. It runs on whatever worker thread the
        bridge hands the call, and netcheck probes its targets in parallel
        behind a 6s timeout, so the worst case the UI ever waits is one
        timeout — not one per target. The proxy is read from settings on every call rather than
        cached, so editing the field in the panel above and re-checking tests
        what was just typed."""
        try:
            proxy = str((self.get_settings().get("captcha") or {}).get("proxy") or "")
        except Exception:  # noqa: BLE001 - an unreadable settings file is "no proxy"
            proxy = ""
        try:
            return netcheck.probe_all(self._api_base(), proxy=proxy)
        except Exception as exc:  # noqa: BLE001 - a probe must never kill the tab
            return {"ok": False, "error": "probe_failed",
                    "message": f"{type(exc).__name__}: {exc}", "targets": []}

    # ---- account creation (roblox.com signup, captcha, vault) ----

    def creation_get_config(self):
        """Saved creation + proxy preferences, merged over defaults.

        settings.json outlives code, so it still carries the retired
        third-party solver block (`provider`, `apiKeys`) from when captchas
        were bought from 2captcha/CapSolver. Those keys are secrets that no
        longer buy anything, so they are DROPPED and the fix written back once
        — same self-healing discipline as RETIRED_SETTINGS, but here it also
        stops a dead API key sitting in a config file forever.
        """
        s = self.get_settings()
        creation = {**DEFAULT_SETTINGS["creation"], **(s.get("creation") or {})}
        stored = s.get("captcha") or {}
        proxy = str(stored.get("proxy") or "").strip()
        solver_url = str(stored.get("solverUrl")
                         or DEFAULT_SETTINGS["captcha"]["solverUrl"]).strip()
        solver_token = str(stored.get("solverToken") or "").strip()
        captcha = {"proxy": proxy, "solverUrl": solver_url,
                   "solverToken": solver_token}

        if set(stored) - {"proxy", "solverUrl", "solverToken"} \
                or stored.get("proxy") != proxy:
            try:
                self.save_settings({"captcha": captcha})
            except OSError:
                pass  # a settings file that can't be written must not block reads
        return {"ok": True, "creation": creation, "captcha": captcha}

    def creation_save_config(self, config):
        """Persist creation/captcha preferences. Validated through
        accountcreator so the UI contract has exactly one authority. A partial
        patch (only the API key, say) never wipes an unrelated saved value."""
        import accountcreator
        cfg = config if isinstance(config, dict) else {}
        current = self.creation_get_config()

        # Only forward the fields this patch actually carries; absent ones keep
        # their saved values.
        patch_creation = cfg.get("creation")
        patch_captcha = cfg.get("captcha")
        amount_in = cfg.get("amount",
                            patch_creation.get("amount") if isinstance(patch_creation, dict) else None)
        style_in = cfg.get("usernameStyle",
                           patch_creation.get("usernameStyle") if isinstance(patch_creation, dict) else None)
        proxy_in = cfg.get("captchaProxy",
                           patch_captcha.get("proxy") if isinstance(patch_captcha, dict) else None)

        clean, error = accountcreator.validate_creation_config(
            amount=amount_in if amount_in is not None else current["creation"].get("amount", 1),
            username_style=style_in,
            custom_password=None,          # never persisted (see DEFAULT_SETTINGS)
            captcha_proxy=proxy_in if proxy_in is not None else current["captcha"].get("proxy"),
        )
        if error:
            return {"ok": False, "error": "bad_config", "message": error}
        captcha_patch = {"proxy": clean["captchaProxy"]}
        for key in ("solverUrl", "solverToken"):
            val_in = (patch_captcha.get(key)
                      if isinstance(patch_captcha, dict) else None)
            if val_in is None:
                val_in = cfg.get(key)
            captcha_patch[key] = (str(val_in).strip() if val_in is not None
                                  else current["captcha"].get(key, ""))
        return self.save_settings({
            "creation": {"amount": clean["amount"],
                         "usernameStyle": clean["usernameStyle"]},
            "captcha": captcha_patch,
        })

    def creation_start(self, config=None):
        """Create N Roblox accounts in one background batch.

        Each account: open roblox.com's registration page, fill a random 18+
        birthday, generated username/password/gender, solve any captcha
        (2captcha.com when an API key is configured; otherwise the human solves
        it in the opened browser window), then on landing at roblox.com/home
        capture `.ROBLOSECURITY`, verify it, save the cookie into omnidroid's
        store AND the password/birthday into the vault — so an expired cookie
        can later be replayed by an automated login instead of losing the
        account."""
        if self._creation_thread and self._creation_thread.is_alive():
            return {"ok": False, "error": "busy",
                    "message": "An account-creation batch is already running."}
        import accountcreator

        saved = self.creation_get_config()
        cfg = config if isinstance(config, dict) else {}
        clean, error = accountcreator.validate_creation_config(
            amount=cfg.get("amount", saved["creation"].get("amount", 1)),
            username_style=cfg.get("usernameStyle", saved["creation"].get("usernameStyle")),
            custom_password=cfg.get("customPassword"),
            captcha_proxy=cfg.get("captchaProxy", saved["captcha"].get("proxy")),
        )
        if error:
            return {"ok": False, "error": "bad_config", "message": error}
        total = clean["amount"]

        self._creation_stop.clear()
        results = []

        def _worker():
            for i in range(total):
                if self._creation_stop.is_set():
                    break
                self._creation_at = (i + 1, total)
                self._push("creation-progress",
                           {"index": i + 1, "total": total, "phase": "start",
                            "message": f"Starting account {i + 1} of {total}"})

                def say(line, i=i, total=total):
                    self._push("creation-progress",
                               {"index": i + 1, "total": total,
                                "phase": "run", "message": str(line)[-300:]})

                try:
                    # The vision solver is wired from settings: screenshots go
                    # to the captcha service, only an action comes back. A
                    # service that is down degrades to the human in the
                    # window — create_account handles that itself.
                    solver_client = None
                    solver_url = (saved["captcha"].get("solverUrl") or "").strip()
                    if solver_url:
                        import visioncaptcha
                        solver_client = visioncaptcha.SolverClient(
                            solver_url,
                            token=saved["captcha"].get("solverToken") or "")
                    res = accountcreator.create_account(
                        on_status=say,
                        stop_check=self._creation_stop.is_set,
                        username_style=clean["usernameStyle"],
                        custom_password=clean["customPassword"],
                        solver_client=solver_client,
                        proxy=clean["captchaProxy"] or None)
                except Exception as e:  # noqa: BLE001 - one bad batch must not kill the thread silently
                    res = {"ok": False, "error": "unexpected",
                           "message": f"{type(e).__name__}: {e}"}

                if res.get("ok"):
                    try:
                        # Cookie -> omnidroid's accounts.json (the engine's own
                        # store), password/birthday -> the executor's vault.
                        accountsync().write_local(res["username"], res["cookie"],
                                                  user_id=res.get("user_id"))
                        self._vault().add(res["username"], res["password"],
                                          birthday=res.get("birthday"),
                                          style=clean["usernameStyle"])
                    except Exception as e:  # noqa: BLE001
                        res = {"ok": False, "error": "save_failed",
                               "message": f"account created but saving failed: {e}",
                               "username": res.get("username")}
                results.append({k: v for k, v in res.items() if k != "cookie"})
                self._push("creation-account", {
                    "index": i + 1, "total": total,
                    "ok": bool(res.get("ok")),
                    "username": res.get("username"),
                    # One-time reveal to the UI that asked for this run — same
                    # trust boundary as the browser window it just watched.
                    "password": res.get("password"),
                    "error": res.get("message") or res.get("error"),
                })
                if res.get("ok"):
                    self._publish_account(res)
                    self._push("accounts-changed", {})
            stopped = self._creation_stop.is_set()
            ok_count = sum(1 for r in results if r.get("ok"))
            self._push("creation-done", {
                "ok": ok_count > 0, "results": results,
                "created": ok_count, "failed": len(results) - ok_count,
                "stopped": stopped,
                "message": (f"{'Stopped' if stopped else 'Done'} — {ok_count} created, "
                            f"{len(results) - ok_count} failed"),
            })
            self._creation_at = (0, 0)

        self._creation_thread = threading.Thread(target=_worker,
                                                 name="account-creation", daemon=True)
        self._creation_thread.start()
        return {"ok": True, "started": True, "total": total}

    def creation_status(self):
        running = bool(self._creation_thread and self._creation_thread.is_alive())
        index, total = self._creation_at if running else (0, 0)
        return {"ok": True, "running": running, "index": index, "total": total}

    def creation_stop(self):
        """Cooperative stop: finishes the account in flight's current step
        check, then skips the rest of the batch."""
        self._creation_stop.set()
        return {"ok": True, "stopping": True}

    def _vault(self):
        """The created-account credential vault, rooted where omnidroid keeps
        accounts.json (OMNI_DATA_DIR / runtime dir)."""
        import accountcreator
        root = os.environ.get("OMNI_DATA_DIR")
        if not root:
            import bootstrap
            root = bootstrap.runtime_dir()
        return accountcreator.Vault(root)

    def vault_list(self):
        """Created accounts WITHOUT secrets (has_password booleans only)."""
        try:
            return {"ok": True, "accounts": self._vault().list()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "vault_read_failed", "message": str(e)}

    def vault_reveal(self, username):
        """Full record INCLUDING the password — an explicit user action (the
        UI's copy button), never part of a list response."""
        if not isinstance(username, str) or not username.strip():
            return {"ok": False, "error": "bad_username"}
        rec = self._vault().get(username.strip())
        if not rec:
            return {"ok": False, "error": "not_found",
                    "message": f"No created account named {username!r}."}
        return {"ok": True, "account": rec}

    # ---- instances ----
    #
    # ONE KIND OF LAUNCH. There used to be a choice before every start --
    # Gaming or Farming, a GPU policy, a base, x86 or arm, a warm pool to keep
    # -- because each was a different virtual machine with different costs.
    # omnidroid has none of those trades: an instance is the executor APK on
    # one of the pool's Androids, in a window of its own, and the pool decides
    # where it goes. So Launch just launches.

    # Not `cookies/`: that is the old engine's legacy per-account store, which
    # robloxaccounts.py still folds into accounts.json (and marks with a
    # `.migrated` file). Launch files have nothing to do with it.
    LAUNCH_COOKIE_DIR = "launch-cookies"

    def _cookie_file(self, name, cookie):
        """The account's cookie as a file for omnidroid's roblox plugin
        (`--cookie <file>`), private to this user.

        A file and never the value: the plugin refuses a cookie on its command
        line, and a value in an argv is readable by every process on the
        machine. Kept for as long as the instance is, because a restart of the
        instance plants it again; removed when it stops (see _forget_cookie)."""
        d = bootstrap.runtime_dir() / Api.LAUNCH_COOKIE_DIR
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name}.txt"
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(cookie.strip())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # Windows: the per-user runtime dir is the protection
        tmp.replace(path)
        return path

    @staticmethod
    def _forget_cookie(name):
        try:
            (bootstrap.runtime_dir() / Api.LAUNCH_COOKIE_DIR / f"{name}.txt").unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _clear_cookie_files():
        """Every cookie file a previous run left (it ended without stopping
        its instances). Nothing can be using them: the pool ends with the app."""
        d = bootstrap.runtime_dir() / Api.LAUNCH_COOKIE_DIR
        try:
            for f in d.glob("*.txt"):
                f.unlink(missing_ok=True)
        except OSError:
            pass

    def engine_start(self, name, place=None):
        """Launch the executor APK for one account, signed in, and send it to
        `place` if one is given.

        Returns as soon as omnidroid has taken the launch (its instance id);
        the boot of an Android, the install and the sign-in happen after, and
        the Accounts list shows each state as it comes (waiting, installing,
        starting, running)."""
        error = self._bad_name(name)
        if error:
            return error
        if name in self._running_names():
            return {"ok": True, "already_running": True,
                    "instance_id": self._instances.get(name)}
        rec = accountsync().read_local().get(name)
        if not rec or not rec.get("cookie"):
            return {"ok": False, "error": "no_account",
                    "message": f"No saved sign-in for {name}. Add the account again."}
        apk = droid.apk_path()
        if apk is None:
            return {"ok": False, "error": "apk_missing",
                    "message": "The executor APK is missing -- finish setup to download it."}
        place = str(place).strip() if place is not None else ""
        if place and not place.isdigit():
            return {"ok": False, "error": "bad_place",
                    "message": f"{place!r} is not a place id (a number)."}

        say = self._progress(name)
        # The autoexec bundle goes up BEFORE the instance exists: the in-game
        # menu fetches it at session start, keyed by this account's name.
        self._apply_autoexec_env()
        try:
            import autoexec
            with contextlib.redirect_stdout(_LineSink(say)):
                autoexec.push_autoexec(name, None, accountsync()._local_store_root(),
                                       name, base=self._exec_base())
        except Exception as e:  # noqa: BLE001 - autoexec must never cost a launch
            say(f"autoexec push skipped: {e}")

        cookie = self._cookie_file(name, rec["cookie"])
        # Quoted for omnidroid's option parser, which takes quotes as the
        # command line does: the runtime dir has spaces on some machines.
        options = f'--cookie "{cookie}"'
        if place:
            options += f" --place {place}"
        say("starting omnidroid" if not self._pool._alive() else "asking omnidroid for an instance")
        try:
            res = self._pool.call("start_instance", {"apk": str(apk), "count": 1,
                                                     "options": options}, timeout=120)
        except droid.DroidError as e:
            self._forget_cookie(name)
            self._push("accounts-changed", {})
            return {"ok": False, "error": "start_failed", "message": str(e)}
        ids = res.get("instances") or []
        if not ids:
            self._forget_cookie(name)
            return {"ok": False, "error": "start_failed",
                    "message": res.get("note") or "omnidroid started no instance."}
        self._instances[name] = ids[0]
        say(res.get("note") or f"instance #{ids[0]}")
        # Claim the lease NOW rather than waiting up to a heartbeat period:
        # the in-game exec bridge cannot claim its poll token until this
        # account is registered as launched.
        self._beat_now(name, "running", place=place or None)
        self._push("accounts-changed", {})
        return {"ok": True, "instance_id": ids[0], "note": res.get("note")}

    def engine_stop(self, name):
        """Close the account's instance: its app stops and its window goes.
        The Android it ran on stays warm for the next launch."""
        error = self._bad_name(name)
        if error:
            return error
        iid = self._instances.pop(name, None)
        result = {"ok": True, "was_running": iid is not None}
        if iid is not None:
            try:
                self._pool.call("stop_instance", {"instance_id": iid}, timeout=30)
            except droid.DroidError as e:
                # Already gone (ended, or its window closed) is a stop that
                # has happened; anything else is worth saying.
                if "ended already" not in str(e) and "no instance" not in str(e):
                    result = {"ok": False, "error": "stop_failed", "message": str(e)}
        self._forget_cookie(name)
        self._beat_now(name, "stopped")
        self._push("accounts-changed", {})
        return result

    def engine_remove(self, name):
        """Forget a saved account: stop its instance, then delete its record
        (and its cookie) from this machine's store. The Roblox account itself
        is untouched."""
        error = self._bad_name(name)
        if error:
            return error
        self.engine_stop(name)
        import robloxaccounts
        existed = robloxaccounts.remove_account(accountsync()._local_store_root(), name)
        self._push("accounts-changed", {})
        return {"ok": True, "removed": name, "existed": existed}

    def engine_view(self, name):
        """Every instance already has a window of its own -- omnidroid opens it
        when the app draws its first frame -- so there is nothing to open.
        Kept so an older page that still calls it gets an answer, not an
        unknown-method error."""
        error = self._bad_name(name)
        if error:
            return error
        if name not in self._running_names():
            return {"ok": False, "error": "not_running",
                    "message": f"{name} is not running. Launch it first."}
        return {"ok": True, "message": "Its window is open on your desktop."}

    # ---- shutdown ----

    def _shutdown(self):
        """The pool is this process's child (droid.py), so ending it here ends
        every instance this app started -- and their cookie files go with
        them. A pool somebody else was running is left alone."""
        self._heartbeat_stop.set()
        # Stop the update watcher too, so a 30-minute wait cannot hold the
        # interpreter open past the window closing.
        self._update_stop.set()
        if self._pool.owned():
            self._release_all_leases()
        else:
            # Somebody else's pool keeps our instances running; close them, so
            # closing the app still means what it says.
            for name in list(self._instances):
                try:
                    self.engine_stop(name)
                except Exception:  # noqa: BLE001 - we are on the way out
                    pass
        try:
            self._pool.shutdown()
        except Exception:      # noqa: BLE001 - we are on the way out
            pass
        self._clear_cookie_files()
        # Unlike the engine's own instances, a mining process is not something
        # the user asked to keep running unattended -- it is this process's
        # own child, spawned straight into this machine's CPU/GPU, and
        # leaving it up after the window closes is an orphaned xmrig nobody
        # can see or stop from the app any more.
        try:
            self.mining_stop()
        except Exception:      # noqa: BLE001 - we are on the way out
            pass


class _LineSink:
    """A file-like that hands each complete line to `say` -- for the autoexec
    module's prints, which belong in the launch's progress, not on stdout
    (stdout is the RPC bridge)."""

    def __init__(self, say):
        self._say = say
        self._buf = ""
        self._busy = False

    def write(self, text):
        # `say` may print (a test double does; a log handler could): while it
        # runs, stdout is still this sink, so a line written from inside it
        # must not come back here -- that recursed until Python gave up.
        if self._busy:
            return len(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._busy = True
                try:
                    self._say(line)
                finally:
                    self._busy = False
        return len(text)

    def flush(self):
        pass


def _apply_update_mode(argv):
    """`--apply-update <target-dir> <pid>`: replace an installed build.

    This runs in the STAGED copy, launched by the outgoing one (see
    updates.launch_apply). It is a mode of the app rather than a separate
    helper binary so there is nothing extra to ship, sign or keep in step —
    and because the code that knows how to lay this build out is right here.

    Returns a process exit code, and NEVER raises. A windowed PyInstaller
    build turns an uncaught exception into "Failed to execute script 'main'"
    over a Python traceback, and that is precisely what a user saw the three
    times the swap failed: an alarming dialog about a program they never
    launched, naming files they have no reason to know about, and no way back
    to the app that was in fact still installed and perfectly fine.
    """
    import updates

    def note(line):
        """Leave a trace where the app -- and the next support question --
        will look. Nobody is watching a detached console."""
        try:
            with open(bootstrap.runtime_dir() / "update.log", "a",
                      encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass

    target, pid = argv[2], argv[3]
    try:
        updates.apply_staged_app(target, pid, log=note)
    except Exception as exc:  # noqa: BLE001 — this is the last line of defence
        note(f"apply-update failed: {exc}")
        _fatal_dialog(
            "Omni Executor could not update",
            f"The update could not be installed, so the version you already "
            f"have has been left exactly as it was.\n\n{exc}\n\n"
            f"It will try again next time. Closing every Omni Executor window "
            f"first — instance windows included — is usually enough.")
        # Put the user back where they were. apply_staged_app is all-or-
        # nothing, so the existing install is intact and launchable.
        exe = updates._executable_in(Path(target))
        if exe is not None:
            try:
                subprocess.Popen(
                    [str(exe)], stdin=subprocess.DEVNULL,
                    **({"creationflags": 0x00000008} if sys.platform == "win32"
                       else {"start_new_session": True}))
            except OSError:
                pass
        return 1
    return 0


# Windows stamps every file extracted from a DOWNLOADED zip with this
# alternate data stream, recording that it came from the Internet zone.
_MOTW_STREAM = ":Zone.Identifier"


def _has_motw(path):
    try:
        return os.path.exists(f"{path}{_MOTW_STREAM}")
    except OSError:
        return False


def _unblock_app_files(log=None):
    """Strip Mark-of-the-Web from this install, or the app cannot start at all.

    A user who downloads the zip and extracts it with Explorer gets a
    `Zone.Identifier` stream on EVERY extracted file.

    HISTORY, because it explains why this is still here. Under pywebview the
    .NET Framework assembly loader refused to load Python.Runtime.dll out of
    the Internet zone, clr_loader could not resolve its entry point, and the
    WinForms backend died on import before a single line of this program's own
    logic ran:

        RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
        ...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

    The Tauri shell has no CLR and no pythonnet, so that exact failure is gone
    with them. This stays as insurance rather than as a cure: MotW still marks
    every extracted file, SmartScreen still reads it, and clearing it costs one
    stat on a normal launch. The real fix remains shipping the installer —
    files an installer writes are never marked.

    Best-effort throughout: an install in a read-only location cannot be
    unmarked, but it also cannot have been marked, because an installer put it
    there.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return 0
    root = Path(sys.executable).resolve().parent
    # This process is the BACKEND, so sys.executable is omni-exec-py.exe and
    # `root` is the install directory it shares with the Tauri omni-exec.exe.
    # base_library.zip is the one file every PyInstaller one-dir build has.
    probe = root / "_internal" / "base_library.zip"
    if not _has_motw(probe) and not _has_motw(Path(sys.executable)):
        return 0
    cleared = 0
    # The whole tree, not one file: a marked extraction marks everything, the
    # Tauri shell beside us included.
    for path in root.rglob("*"):
        try:
            if not path.is_file() or not _has_motw(path):
                continue
            os.remove(f"{path}{_MOTW_STREAM}")
            cleared += 1
        except OSError:
            continue
    if cleared and log:
        log(f"[omni-exec] cleared the downloaded-file mark from {cleared} "
            f"file(s) so Windows will load them")
    return cleared


def _fatal_dialog(title, message):
    """Say what went wrong somewhere a GUI user can actually see it.

    A windowed PyInstaller build has no console, so an uncaught exception
    surfaces as a raw traceback dialog that means nothing to the person
    reading it."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:  # noqa: BLE001 — this IS the error path; never raise here
        print(f"{title}: {message}", file=sys.stderr)


def main():
    if len(sys.argv) > 3 and sys.argv[1] == "--apply-update":
        return _apply_update_mode(sys.argv)

    # The other half of the file-by-file swap (bootstrap.replace_tree): the
    # build we replaced is PARKED in a `.omni-replaced-*` folder rather than
    # deleted, because a DLL that was still loaded at swap time can be renamed
    # but not removed. Now is when it can go -- whatever was holding it
    # belonged to the previous build, and the previous build is gone.
    if getattr(sys, "frozen", False):
        try:
            bootstrap.sweep_replaced(Path(sys.executable).resolve().parent)
        except OSError:
            pass

    # First, because everything below it is downstream of this install being
    # loadable at all.
    _unblock_app_files(log=lambda m: print(m, file=sys.stderr))

    if len(sys.argv) > 1 and sys.argv[1] == "--doctor":
        # "Why won't it start?" in one line, from the SHIPPED binary, without
        # a GUI or a support call. Prints where this install found (or failed
        # to find) QEMU and adb, and what the hypervisor probe says.
        #
        # It also makes a frozen build verifiable: the app is a GUI subsystem
        # binary, so short of launching it there was previously no way to
        # confirm which bootstrap code a given omni-exec.exe actually carries.
        import updates          # module-local, like every other call site
        rt = bootstrap.runtime_dir()
        print(json.dumps({
            "app_version": updates.APP_VERSION,
            "runtime_dir": str(rt),
            "frozen": bool(getattr(sys, "frozen", False)),
            **bootstrap.engine_ready(rt),
            "omnidroid": str(droid.bundle_dir() or ""),
            "omnidroid_bundle": droid.bundle_version(),
            "apk": str(droid.apk_path() or ""),
            "installed": sorted(bootstrap.installed_state(rt)
                                .get("artifacts", {})),
        }, indent=2))
        return

    # Every launch, not only a first boot: see _configure_engine_on_launch.
    _configure_engine_on_launch()

    if "--rpc" not in sys.argv:
        # The window is the Tauri shell's, and it is what launches this. Say
        # so rather than sitting on a stdin nobody is writing to.
        sys.exit(
            "This is the Omni Executor BACKEND; it has no window of its own."
            "\n"
            "Run the app:        npm run tauri dev"
            "\n"
            "Run this half:      python main.py --rpc   (JSON-RPC on stdio)"
        )

    api = Api()
    # Renew running leases from launch, so a second machine sees this one's
    # instances as "Running on <name>" without waiting for a UI action here.
    api.start_heartbeat()
    # Ask what is out of date on every launch — base images, offsets and this
    # app alike. Background, so nothing waits on the network.
    api._check_updates_on_launch()
    atexit.register(api._shutdown)

    # Serves until the shell closes our stdin, which is how a Tauri process
    # that has exited (cleanly or not) tells us it is gone. No separate
    # shutdown message to miss, and no orphaned backend.
    try:
        rpc.serve(api)
    finally:
        api._shutdown()


if __name__ == "__main__":
    main()
