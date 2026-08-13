"""Omni Executor — desktop GUI (pywebview + React) for the omnidroid engine.

Setup:
    pip install -r requirements.txt
    cd frontend && npm install && npm run build

Run:
    python main.py

Platform notes (pywebview picks the native backend automatically):
    - Windows: uses WebView2 / EdgeChromium (preinstalled on Windows 10/11).
    - macOS:   uses Cocoa / WKWebView (built into the OS).
    - Linux:   needs GTK WebKit:  sudo apt install python3-gi gir1.2-webkit2-4.1
               (or install the Qt backend instead: pip install pywebview[qt])

Engine:
    The Accounts tab drives the omnidroid engine (omnidroid.exe on Windows,
    ./omnidroid on Linux) sitting next to this file. Every account is an
    isolated headless Android instance; all interaction goes through the
    engine CLI with --json (one JSON line on stdout, progress on stderr).
    Viewing an instance opens the omnidroid engine's own viewer window
    (`omnidroid view <name> --start`); the executor neither embeds a VNC
    client nor hands the screen to the OS one (macOS Screen Sharing).
    Disconnecting a viewer never stops the instance — only an explicit
    `stop` powers it off.
"""

import atexit
import bootstrap
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import webview

APP_NAME = "omni-executor"
PROJECT_DIR = Path(__file__).resolve().parent
FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"
IS_MAC = sys.platform == "darwin"

# OMNI-EXEC remote-execute bridge base (serves the in-game UI + the exec queue).
# Override with the OMNI_EXEC_BASE env var or a settings.json "execBase" value.
OMNI_EXEC_BASE = os.environ.get("OMNI_EXEC_BASE", "http://72.62.59.232")

# Some Windows installs register .js as text/plain, which makes the webview
# refuse to load ES modules.
mimetypes.add_type("text/javascript", ".js")

DEFAULT_SETTINGS = {
    "theme": "dark",
    "activeTab": "editor",
    "sidebar": "expanded",
    "launch": {"mode": "playable", "multiInstance": False, "minimizeOnLaunch": False},
    "profile": {"name": "Guest", "tag": ""},
}


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

# ------------------------------------------------------------- engine bridge

# Engine contract (see omnidroid.md): account names are [A-Za-z0-9_-]+.
ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Hide console windows spawned on Windows (engine).
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def engine_prefix():
    """Command prefix used to invoke the omnidroid engine.

    Priority:
      1. OMNIDROID_ENGINE env var — a path to omnidroid(.exe), OR a `.py`
         (e.g. the engine's manager/omni.py) which is run with the current
         Python. Lets a build point at a specific engine, and lets dev drive
         the source checkout without building an exe.
      2. Frozen build: the app re-invokes ITSELF with `--omnidroid` — the
         engine is dispatched in-process (see the top of main()), so no
         separate binary needs to be found or shipped next to the app.
      3. The platform-appropriate NATIVE binary sitting next to main.py (the
         product, installed by the CDN installer): omnidroid.exe on Windows,
         extensionless omnidroid on macOS/Linux. Never cross platforms —
         an omnidroid.exe next to main.py on macOS/Linux is not executable
         and must never be returned there, and vice versa.
      4. Python-source fallback (dev/test, e.g. this Mac where no native
         binary is bundled): a sibling omnidroid source checkout, run as
         `python -m omnidroid` with the current Python (the checkout's own
         console-script shim, `omnidroid.cli:main`).
    Returns a subprocess argv prefix (list), or None if nothing is found.
    """
    override = os.environ.get("OMNIDROID_ENGINE")
    if override:
        p = Path(override)
        if p.is_file():
            return [sys.executable, str(p)] if p.suffix == ".py" else [str(p)]

    if getattr(sys, "frozen", False):
        return [sys.executable, "--omnidroid"]  # in-binary engine dispatch

    native_name = "omnidroid.exe" if sys.platform == "win32" else "omnidroid"
    candidate = PROJECT_DIR / native_name
    if candidate.is_file():
        return [str(candidate)]

    sibling = PROJECT_DIR.parent / "omnidroid"  # sibling checkout root
    if (sibling / "omnidroid" / "__main__.py").is_file():
        return [sys.executable, "-m", "omnidroid"]  # was: manager.py (stale)

    return None


def find_engine():
    """Back-compat truthiness probe: is an engine resolvable? Returns the
    prefix's first element (a path) or None."""
    prefix = engine_prefix()
    return prefix[-1] if prefix else None


def _configure_engine_on_launch():
    """Set the engine env vars (OMNIDROID_CONFIG_PATH / OMNI_DATA_DIR /
    OMNI_IMAGES_DIR) + write rt/paths.json for THIS GUI process, on every
    launch -- not just first-boot.

    bootstrap.bootstrap_start() (the first-boot install flow) already calls
    bootstrap.configure_engine() once the runtime download completes. But on
    a SUBSEQUENT launch of an already-installed app, bootstrap_status()
    returns ready=true, the frontend skips BootstrapView entirely, and
    bootstrap_start() is never called again -- so without this, engine
    subprocesses spawned later (engine_start/engine_view/etc., via
    run_engine()'s env=None inherit) would carry NONE of those vars, and the
    frozen --omnidroid engine would fall back to the read-only app bundle's
    own configs/paths.json and never find the installed arm base.

    Calling configure_engine() here, unconditionally, in the GUI parent
    process before any window/engine subprocess exists, makes every launch
    (first-boot or relaunch) set the same env for every engine subprocess
    this session spawns. Safe on a genuinely fresh runtime dir (nothing
    downloaded yet): configure_engine() just writes a base-less paths.json
    and sets the env vars anyway (no arm/ dir to scan yet is not an error);
    bootstrap_start() overwrites it correctly once the download finishes.

    Must run only in the GUI parent process, never inside the --omnidroid
    child dispatch branch (that subprocess IS the engine; it doesn't spawn
    further engine subprocesses of its own).
    """
    try:
        import bootstrap
        bootstrap.configure_engine(bootstrap.runtime_dir())
    except Exception as e:  # noqa: BLE001 — engine config must never crash the app launch
        print(f"[omni-exec] engine pre-config skipped: {e}", file=sys.stderr)


def _parse_engine_stdout(stdout, code):
    """Engine contract: exactly one JSON value on stdout. Be tolerant anyway —
    scan for the last parseable line if the whole output isn't clean JSON."""
    text = (stdout or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


# How long the ENGINE may spend booting an instance, passed to it explicitly
# as `start --timeout` so both sides agree on one number instead of racing.
#
# They used to disagree, and the executor always lost: its watchdog fired at
# 300 s while the engine's own budget was 360 s. Any boot slower than five
# minutes was therefore killed by this process BEFORE the engine could report
# anything — and because the engine spawns QEMU detached, killing it orphans a
# live instance: the UI says the start failed while the VM is running happily.
# (Observed: a boot that took just over five minutes.)
#
# WATCHDOG_GRACE keeps this process's own kill strictly LAST, so the engine
# always gets to finish and emit its JSON verdict. The subprocess watchdog is
# a backstop against a wedged engine, not the normal path.
BOOT_TIMEOUT = 600
# Graceful in-guest power-off budget, passed to `stop --timeout`. The engine
# escalates on its own (adb shutdown -> QMP quit -> kill), so this bounds only
# the polite first step; the same "engine decides, we only backstop" rule
# applies.
STOP_TIMEOUT = 120
WATCHDOG_GRACE = 60

# Engine-side budget for `view`: it waits for QEMU's VNC port to accept
# connections (with --start, after spawning the instance). QEMU binds VNC at
# process start, so this is normally seconds; the same "engine decides, we only
# backstop" rule as start/stop applies.
VIEW_TIMEOUT = 90


def run_engine(args, progress=None, timeout=None):
    """Run an engine subcommand; return its stdout JSON normalized to a dict.

    stderr progress lines are forwarded to `progress`. List results are
    wrapped as {"ok": ..., "accounts": [...]}.
    """
    prefix = engine_prefix()
    if prefix is None:
        return {
            "ok": False,
            "error": "engine_missing",
            "message": "Engine not found — omnidroid.exe must sit next to main.py.",
        }

    env = None
    if len(prefix) == 3 and prefix[1:] == ["-m", "omnidroid"]:
        # Source-fallback `-m omnidroid` form: the sibling checkout root must
        # be importable as a package search path for `python -m omnidroid`
        # to resolve, since it isn't installed/on sys.path otherwise.
        sibling = PROJECT_DIR.parent / "omnidroid"
        env = {**os.environ, "PYTHONPATH": str(sibling)}

    try:
        proc = subprocess.Popen(
            [*prefix, *args],
            cwd=str(PROJECT_DIR),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as err:
        return {"ok": False, "error": "engine_spawn_failed", "message": str(err)}

    stderr_tail = []

    def pump_stderr():
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            stderr_tail.append(line)
            del stderr_tail[:-15]
            if progress:
                try:
                    progress(line)
                except Exception:
                    pass

    threading.Thread(target=pump_stderr, daemon=True).start()

    watchdog = threading.Timer(timeout, proc.kill) if timeout else None
    if watchdog:
        watchdog.start()
    try:
        stdout = proc.stdout.read()
        code = proc.wait()
    finally:
        if watchdog:
            watchdog.cancel()

    data = _parse_engine_stdout(stdout, code)
    if isinstance(data, list):
        result = {"ok": code == 0, "accounts": data}
    elif isinstance(data, dict):
        result = data
    else:
        message = (stdout or "").strip() or "\n".join(stderr_tail[-5:]) or f"engine exited with code {code}"
        result = {"error": "bad_engine_output", "message": message[:1000]}
    result.setdefault("ok", code == 0)
    result["exit_code"] = code
    if not result.get("ok") and "message" not in result:
        # surface something readable for the UI; the engine's stderr tail is
        # the most specific text available on failures
        result["message"] = result.get("error") or "\n".join(stderr_tail[-5:]) or f"exit code {code}"
    return result


def _exec_http(url, payload=None, timeout=10):
    """Minimal JSON HTTP for the exec bridge (stdlib only). POST when payload given."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


class Api:
    """Methods exposed to JavaScript as window.pywebview.api.*"""

    def __init__(self):
        self._window = None  # set in main() after the window is created
        self._maximized = False
        self._bootstrapping = False

    # ---- window controls (used by the custom title bar) ----

    def get_platform(self):
        """'darwin' | 'win32' | 'linux' — the frontend picks its window chrome
        from this: native traffic lights on macOS, custom buttons elsewhere."""
        return sys.platform

    def minimize(self):
        if self._window:
            self._window.minimize()

    def toggle_maximize(self):
        """Maximize or restore the window; returns the resulting maximized state."""
        if not self._window:
            return False
        self._maximized = not self._maximized
        if self._maximized:
            self._window.maximize()
        else:
            self._window.restore()
        return self._maximized

    def close(self):
        if self._window:
            self._window.destroy()

    # ---- settings ----

    def get_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        return {**DEFAULT_SETTINGS, **saved}

    def save_settings(self, settings):
        current = self.get_settings()
        if isinstance(settings, dict):
            current.update(settings)
        # Write to a temp file first so a crash mid-write can't corrupt settings.
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        tmp.replace(SETTINGS_FILE)
        return current

    # ---- remote execute (Editor tab) ----

    def _exec_base(self):
        base = OMNI_EXEC_BASE
        try:
            saved = self.get_settings()
            if isinstance(saved.get("execBase"), str) and saved["execBase"].strip():
                base = saved["execBase"].strip()
        except Exception:
            pass
        return base.rstrip("/")

    def execute_script(self, name, script):
        """Run a Luau script in the LIVE game session for `name` — MANUAL only,
        fired when the user clicks Run. Submits to the OMNI-EXEC bridge; the
        in-game UI polls it, loadstring()s it, and reports the result, which we
        wait briefly for and return so the editor can show ok/output."""
        bad = self._bad_name(name)
        if bad:
            return bad
        if not isinstance(script, str) or not script.strip():
            return {"ok": False, "error": "empty_script", "message": "Nothing to run — the editor is empty."}
        base = self._exec_base()
        try:
            sub = _exec_http(f"{base}/omni/exec/submit", {"channel": name, "script": script}, timeout=10)
        except Exception as exc:
            return {"ok": False, "error": "unreachable",
                    "message": f"Couldn't reach the exec server at {base}: {exc}"}
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
                res = _exec_http(f"{base}/omni/exec/result?id={job_id}", timeout=4)
            except Exception:
                res = {}
            if res.get("done"):
                return {"ok": True, "ran": bool(res.get("ok")), "output": res.get("output", ""),
                        "id": job_id, "connected": True}
            time.sleep(0.4)
        return {"ok": True, "ran": None, "pending": True, "id": job_id, "connected": True,
                "output": "Submitted — no result yet (the script may still be running)."}

    # ---- engine ----

    def _push(self, event, payload=None):
        """Fire an event into the main window's JS (window.omniEvent)."""
        if not self._window:
            return
        try:
            self._window.evaluate_js(
                f"window.omniEvent && window.omniEvent({json.dumps(event)}, {json.dumps(payload)})"
            )
        except Exception:
            pass  # window may be closing; events are best-effort

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
            have = installed.get("artifacts", {})
            ready = all(have.get(a["name"], {}).get("sha256") == a["sha256"]
                        for a in manifest.get("artifacts", []))
        except bootstrap.BootstrapError as e:
            error = str(e)
            ready = bool(installed.get("artifacts"))  # offline-tolerant
        ready = ready and eng.get("qemu_ok", False)
        # WHPX is a hard prerequisite on Windows -- omnidroid boots with
        # -accel whpx and QEMU just dies if the feature is off -- so the UI
        # has to be able to say so before the user hits Start. whpx_ok is
        # tri-state (True/False/None-unknown); only an explicit False is a
        # blocker, and it never gates a non-Windows host.
        accel = bootstrap.windows_accel_status()
        return {"ok": True, "ready": ready, "installed": installed.get("artifacts", {}),
                "qemu_ok": eng.get("qemu_ok", False), "qemu_hint": eng.get("qemu_hint"),
                "whpx_ok": accel.get("whpx_ok"), "whpx_hint": accel.get("hint"),
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

    _FALLBACK_MODES = ["playable", "hard", "brutal", "farming"]

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
        """Contract handshake (omnidroid-api.md §4). The UI gates on
        `contract`/`arch_aware`, and on the commands this app actually calls.

        The contract version alone is NOT enough, and that gap is the bug this
        check exists to close: an engine can speak contract 1.0 and still lack
        `login`, `view` or `setup`. A stale bundled engine therefore sailed
        through the handshake and only failed later, when the user clicked
        "Add account" and got an argparse error from a subprocess. Now the
        mismatch is named up front, in terms of what will not work."""
        if engine_prefix() is None:
            return {
                "ok": False,
                "error": "engine_missing",
                "message": "omnidroid engine not found next to main.py.",
            }
        rep = run_engine(["version", "--json"], timeout=30)
        if isinstance(rep, dict):
            rep["missing_commands"] = self._missing_commands(rep)
        return rep

    # Engine subcommands this app invokes. Checked against the engine's own
    # advertised list at handshake time. Kept next to the calls it describes:
    # adding a run_engine() call for a command not listed here is the mistake
    # this is meant to catch.
    _REQUIRED_COMMANDS = ("version", "doctor", "bases", "use-base", "setup",
                          "list", "login", "start", "stop", "remove", "view")

    def _missing_commands(self, report):
        """Required commands this engine does NOT advertise.

        Returns [] when the engine reports no command list at all — an engine
        that predates the field is not evidence of a missing command, and
        guessing would put a false alarm in front of a working install."""
        advertised = report.get("commands")
        if not isinstance(advertised, list) or not advertised:
            return []
        have = {c for c in advertised if isinstance(c, str)}
        return [c for c in self._REQUIRED_COMMANDS if c not in have]

    def engine_doctor(self):
        """Readiness check: engine present, base images registered, QEMU/adb OK."""
        if engine_prefix() is None:
            return {
                "ok": False,
                "ready": False,
                "error": "engine_missing",
                "message": "omnidroid.exe not found next to main.py.",
            }
        return run_engine(["doctor", "--json"], timeout=120)

    def engine_bases(self):
        """Registered bases with arch/type (contract §6.5) — lets the UI show
        which architecture (x86 vs arm) each base is."""
        return run_engine(["bases", "--json"], timeout=30)

    def engine_use_base(self, tag):
        """Set the default base for NEW accounts (contract §6.5). `use-base`
        is text-only in the engine, so success is conveyed by exit code 0."""
        if not isinstance(tag, str) or not tag:
            return {"ok": False, "error": "bad_base", "message": "A base tag is required."}
        res = run_engine(["use-base", tag])
        return {
            "ok": res.get("exit_code") == 0,
            "message": (res.get("message") if res.get("exit_code") != 0
                        else f"Default base for new accounts set to {tag}."),
        }

    def engine_setup(self):
        """First-run setup (idempotent): folders + portable QEMU download."""
        return run_engine(["setup"], progress=self._progress("setup"))

    def engine_list(self):
        """All accounts with base, ports and running state."""
        return run_engine(["list", "--json"], timeout=60)

    def engine_login_browser(self):
        """Add an account by interactive Roblox sign-in (omnidroid opens its
        own browser). The account is saved under its Roblox username."""
        return run_engine(["login"], progress=self._progress("login"), timeout=360)

    def engine_login_token(self, token):
        """Add an account from a pasted Roblox cookie/token. Written to a
        private temp file (never the argv/ps) and passed via --token-file."""
        if not isinstance(token, str) or not token.strip():
            return {"ok": False, "error": "bad_token",
                    "message": "A Roblox cookie/token is required."}
        import tempfile
        fd, path = tempfile.mkstemp(prefix="omniexec-tok-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token.strip())
            return run_engine(["login", "--token-file", path, "--json"],
                              progress=self._progress("login"), timeout=120)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def engine_modes(self):
        """Mode list DERIVED from the engine's version report; falls back to a
        constant (incl. farming) on an older engine that doesn't report modes."""
        rep = run_engine(["version", "--json"], timeout=30)
        modes = rep.get("modes") if isinstance(rep, dict) else None
        return modes if isinstance(modes, list) and modes else list(self._FALLBACK_MODES)

    def engine_start(self, name, mode=None, place=None):
        """Cold-boot an instance headless and detached (returns pid + ports)."""
        error = self._bad_name(name)
        if error:
            return error
        args = ["start", name, "--json", "--timeout", str(BOOT_TIMEOUT)]
        if isinstance(mode, str) and mode.strip():
            args += ["--mode", mode.strip()]
        if place is not None and str(place).strip():
            args += ["--place", str(place).strip()]
        result = run_engine(args, progress=self._progress(name),
                            timeout=BOOT_TIMEOUT + WATCHDOG_GRACE)
        self._push("accounts-changed", {})
        return result

    def engine_stop(self, name):
        """Explicit power-off (adb shutdown -> QMP quit -> kill)."""
        error = self._bad_name(name)
        if error:
            return error
        result = run_engine(["stop", name, "--json", "--timeout", str(STOP_TIMEOUT)],
                            progress=self._progress(name),
                            timeout=STOP_TIMEOUT + WATCHDOG_GRACE)
        self._push("accounts-changed", {})
        return result

    def engine_remove(self, name):
        """DESTRUCTIVE: stop if running, then delete accounts/<name>/ entirely
        (system overlay + data.qcow2 + state). The account's data is gone forever."""
        error = self._bad_name(name)
        if error:
            return error
        result = run_engine(["remove", name, "--json"], progress=self._progress(name), timeout=300)
        self._push("accounts-changed", {})
        return result

    # ---- viewer ----

    def engine_view(self, name):
        """Open omnidroid's own viewer window on an instance (--start boots it
        if stopped). Disconnecting a viewer never stops the instance.

        One engine call, the same shape as start/stop: the engine owns the
        viewer, this button only asks for it. Deliberately no `--native` —
        that flag hands the screen to the OS client (macOS Screen Sharing),
        which is the one thing the View button must not do.
        """
        error = self._bad_name(name)
        if error:
            return error
        return run_engine(
            ["view", name, "--start", "--json", "--timeout", str(VIEW_TIMEOUT)],
            progress=self._progress(name),
            timeout=VIEW_TIMEOUT + WATCHDOG_GRACE)

    # ---- shutdown ----

    def _shutdown(self):
        """No embedded viewer/proxy state to tear down — the engine owns its
        own viewer windows. Instances keep running regardless; only an
        explicit stop powers them off (engine contract)."""
        pass


def _show_macos_traffic_lights(window):
    """pywebview's frameless mode gives exactly the blended titlebar we want
    (transparent, hidden title, content underneath — applied at creation,
    which matters: macOS builds the titlebar backdrop at first show and
    ignores later transparency flips). It also hides the traffic lights,
    so bring just those back."""
    try:
        import AppKit

        def apply():
            try:
                ns_window = window.native
                for kind in (
                    AppKit.NSWindowCloseButton,
                    AppKit.NSWindowMiniaturizeButton,
                    AppKit.NSWindowZoomButton,
                ):
                    button = ns_window.standardWindowButton_(kind)
                    if button is not None:
                        button.setHidden_(False)
            except Exception:
                pass

        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(apply)
    except Exception:
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--omnidroid":
        # In-binary engine dispatch (frozen build's engine_prefix() returns
        # [sys.executable, "--omnidroid"]): re-exec the frozen app's own
        # Python with the omnidroid CLI's own argv shape and run the engine
        # in-process instead of shelling out to a separate binary.
        sys.argv = ["omnidroid", *sys.argv[2:]]
        # Belt and braces: the engine spawns its own detached children (the
        # VNC viewer, the autocap recorder) by re-running THIS binary, and it
        # must put "--omnidroid" in front or the child relaunches the GUI.
        # bootstrap.configure_engine() normally sets this in the parent and it
        # is inherited; setting it here too covers an engine invoked directly.
        os.environ.setdefault("OMNIDROID_SELF_ARGV", "--omnidroid")
        from omnidroid.cli import main as engine_main
        engine_main()
        return

    # GUI parent process: set the engine env for THIS session on every
    # launch (first-boot AND relaunch of an already-installed runtime), so
    # every engine subprocess spawned below inherits it. See
    # _configure_engine_on_launch's docstring for why this can't be left to
    # bootstrap_start() alone.
    _configure_engine_on_launch()

    index = FRONTEND_DIST / "index.html"
    if not index.exists():
        sys.exit(
            f"Frontend build not found: {index}\n"
            "Build it first:  cd frontend && npm install && npm run build"
        )

    api = Api()
    window = webview.create_window(
        "Omni Executor",
        url=str(index),
        js_api=api,
        width=1024,
        height=720,
        min_size=(680, 460),
        background_color="#0a0a0a",  # matches the dark sheet, prevents a white flash on startup
        # Frameless everywhere: Windows/Linux get the frontend's own controls
        # on the right; macOS re-shows the native traffic lights on the left.
        frameless=True,
        easy_drag=False,  # dragging is limited to .pywebview-drag-region elements
    )
    api._window = window

    if IS_MAC:
        window.events.shown += lambda *a: _show_macos_traffic_lights(window)

    # Keep the maximize state in sync when the OS changes it (e.g. Win+Up snap).
    try:
        window.events.maximized += lambda *a: setattr(api, "_maximized", True)
        window.events.restored += lambda *a: setattr(api, "_maximized", False)
    except AttributeError:
        pass  # older pywebview without these events; manual toggling still works

    window.events.closed += lambda *a: api._shutdown()
    atexit.register(api._shutdown)

    webview.start()


if __name__ == "__main__":
    main()
