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
    Viewing an instance opens the omnidroid engine's own native viewer
    window (`omnidroid view <name> --start`); the executor does not embed
    a VNC client. Disconnecting a viewer never stops the instance — only
    an explicit `stop` powers it off.
"""

import atexit
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import webview

APP_NAME = "omni-executor"
PROJECT_DIR = Path(__file__).resolve().parent
FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"

# Some Windows installs register .js as text/plain, which makes the webview
# refuse to load ES modules.
mimetypes.add_type("text/javascript", ".js")

DEFAULT_SETTINGS = {
    "theme": "dark",
    "activeTab": "editor",
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
      2. The platform-appropriate NATIVE binary sitting next to main.py (the
         product, installed by the CDN installer): omnidroid.exe on Windows,
         extensionless omnidroid on macOS/Linux. Never cross platforms —
         an omnidroid.exe next to main.py on macOS/Linux is not executable
         and must never be returned there, and vice versa.
      3. Python-source fallback (dev/test, e.g. this Mac where no native
         binary is bundled): a sibling omnidroid source checkout's
         self-contained shim, run with the current Python.
    Returns a subprocess argv prefix (list), or None if nothing is found.
    """
    override = os.environ.get("OMNIDROID_ENGINE")
    if override:
        p = Path(override)
        if p.is_file():
            return [sys.executable, str(p)] if p.suffix == ".py" else [str(p)]

    native_name = "omnidroid.exe" if sys.platform == "win32" else "omnidroid"
    candidate = PROJECT_DIR / native_name
    if candidate.is_file():
        return [str(candidate)]

    manager_py = PROJECT_DIR.parent / "omnidroid" / "manager.py"
    if manager_py.is_file():
        return [sys.executable, str(manager_py)]

    return None


def find_engine():
    """Back-compat truthiness probe: is an engine resolvable? Returns the
    prefix's first element (a path) or None."""
    prefix = engine_prefix()
    return prefix[-1] if prefix else None


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

    try:
        proc = subprocess.Popen(
            [*prefix, *args],
            cwd=str(PROJECT_DIR),
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


class Api:
    """Methods exposed to JavaScript as window.pywebview.api.*"""

    def __init__(self):
        self._window = None  # set in main() after the window is created
        self._maximized = False

    # ---- window controls (used by the custom title bar) ----

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
        """Contract handshake (omnidroid-api.md §4). The UI calls this first
        and gates on `contract`/`arch_aware`, warning on a mismatch."""
        if engine_prefix() is None:
            return {
                "ok": False,
                "error": "engine_missing",
                "message": "omnidroid engine not found next to main.py.",
            }
        return run_engine(["version", "--json"], timeout=30)

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
        args = ["start", name, "--json"]
        if isinstance(mode, str) and mode.strip():
            args += ["--mode", mode.strip()]
        if place is not None and str(place).strip():
            args += ["--place", str(place).strip()]
        result = run_engine(args, progress=self._progress(name), timeout=300)
        self._push("accounts-changed", {})
        return result

    def engine_stop(self, name):
        """Explicit power-off (adb shutdown -> QMP quit -> kill)."""
        error = self._bad_name(name)
        if error:
            return error
        result = run_engine(["stop", name, "--json"], progress=self._progress(name), timeout=180)
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
        """Open omnidroid's own native viewer window (--start boots if stopped).
        Fire-and-forget; the engine owns the window. Report only a launch failure."""
        res = run_engine(["view", name, "--start"], timeout=60)
        if isinstance(res, dict) and res.get("ok") is False:
            return res
        return {"ok": True}

    # ---- shutdown ----

    def _shutdown(self):
        """No embedded viewer/proxy state to tear down — the engine owns its
        own viewer windows. Instances keep running regardless; only an
        explicit stop powers them off (engine contract)."""
        pass


def main():
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
        background_color="#14151d",  # matches the dark theme, prevents a white flash on startup
        frameless=True,   # the frontend renders its own title bar
        easy_drag=False,  # dragging is limited to .pywebview-drag-region elements
    )
    api._window = window

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
