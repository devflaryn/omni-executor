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
    Viewing an instance uses noVNC over a local websockify bridge to the
    instance's localhost-only VNC port; disconnecting a viewer never stops
    the instance — only an explicit `stop` powers it off.
"""

import atexit
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
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

# Hide console windows spawned on Windows (engine, websockify).
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def engine_prefix():
    """Command prefix used to invoke the omnidroid engine.

    Priority:
      1. OMNIDROID_ENGINE env var — a path to omnidroid(.exe), OR a `.py`
         (e.g. the engine's manager/omni.py) which is run with the current
         Python. Lets a build point at a specific engine, and lets dev drive
         the source checkout without building an exe.
      2. The bundled omnidroid(.exe) sitting next to main.py (the product).
    Returns a subprocess argv prefix (list), or None if nothing is found.
    """
    override = os.environ.get("OMNIDROID_ENGINE")
    if override:
        p = Path(override)
        if p.is_file():
            return [sys.executable, str(p)] if p.suffix == ".py" else [str(p)]
    for suffix in (".exe", ""):
        candidate = PROJECT_DIR / f"omnidroid{suffix}"
        if candidate.is_file():
            return [str(candidate)]
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


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _terminate(proc):
    try:
        if proc.poll() is None:
            proc.terminate()
    except OSError:
        pass


class Api:
    """Methods exposed to JavaScript as window.pywebview.api.*"""

    def __init__(self):
        self._window = None  # set in main() after the window is created
        self._maximized = False
        self._viewers = {}  # account name -> {"window": Window, "allow_close": bool}
        self._proxies = {}  # account name -> {"proc": Popen, "ws_port": int, "vnc_port": int}
        self._proxy_lock = threading.Lock()

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

    def engine_create(self, name):
        """Create an account and provision it (first boot: ~3-15 min).
        Progress is streamed to the UI as 'engine-progress' events."""
        error = self._bad_name(name)
        if error:
            return error
        result = run_engine(["create", name, "--json"], progress=self._progress(name))
        self._push("accounts-changed", {})
        return result

    def engine_start(self, name, mode=None):
        """Cold-boot an instance headless and detached (returns pid + ports)."""
        error = self._bad_name(name)
        if error:
            return error
        args = ["start", name, "--json"]
        if mode in ("playable", "hard", "brutal"):
            args += ["--mode", mode]
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

    # ---- VNC viewer ----

    def _ensure_proxy(self, name, vnc_port):
        """Start (or reuse) a websockify bridge ws://127.0.0.1:<ws> -> VNC port.
        The webview cannot speak raw RFB/TCP, so noVNC connects through this."""
        with self._proxy_lock:
            rec = self._proxies.get(name)
            if rec and rec["vnc_port"] == vnc_port and rec["proc"].poll() is None:
                return rec["ws_port"]
            if rec:
                _terminate(rec["proc"])

            ws_port = _free_port()
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "websockify",
                     f"127.0.0.1:{ws_port}", f"127.0.0.1:{vnc_port}"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError as err:
                raise RuntimeError(f"Could not start websockify: {err}") from err

            deadline = time.time() + 5
            while time.time() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(
                        "websockify exited immediately — is it installed? (pip install websockify)"
                    )
                try:
                    socket.create_connection(("127.0.0.1", ws_port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                _terminate(proc)
                raise RuntimeError("websockify did not start listening in time")

            self._proxies[name] = {"proc": proc, "ws_port": ws_port, "vnc_port": vnc_port}
            return ws_port

    def open_viewer(self, name):
        """Open (or focus) a popup window with a live, controllable VNC view."""
        error = self._bad_name(name)
        if error:
            return error

        rec = self._viewers.get(name)
        if rec:
            try:
                rec["window"].show()
                return {"ok": True, "already_open": True}
            except Exception:
                self._viewers.pop(name, None)  # stale record, reopen below

        listing = run_engine(["list", "--json"], timeout=60)
        entry = next(
            (a for a in listing.get("accounts", []) if isinstance(a, dict) and a.get("name") == name),
            None,
        )
        if entry is None:
            return {"ok": False, "error": "unknown_account", "message": f"'{name}' does not exist."}
        if not entry.get("running"):
            return {"ok": False, "error": "not_running",
                    "message": f"'{name}' is not running — start it first."}
        vnc_port = entry.get("vnc_port")
        if not isinstance(vnc_port, int):
            return {"ok": False, "error": "no_vnc_port",
                    "message": "The engine did not report a VNC port for this account."}

        try:
            ws_port = self._ensure_proxy(name, vnc_port)
        except RuntimeError as err:
            return {"ok": False, "error": "proxy_failed", "message": str(err)}

        viewer_html = FRONTEND_DIST / "viewer.html"
        if not viewer_html.is_file():
            return {"ok": False, "error": "frontend_missing",
                    "message": "viewer.html missing — run `npm run build` in frontend/."}

        window = webview.create_window(
            name,  # popup title = account name
            url=str(viewer_html),
            js_api=self,
            width=960,
            height=600,
            min_size=(480, 320),
            background_color="#14151d",
        )
        rec = {"window": window, "allow_close": False}
        self._viewers[name] = rec

        cfg = json.dumps({"session": name, "ws_port": ws_port})
        window.events.loaded += lambda *a: window.evaluate_js(
            f"window.initViewer && window.initViewer({cfg})"
        )

        def on_closing(*a):
            # Native close: ask keep-running (default) vs stop, instead of closing.
            if rec["allow_close"]:
                return True
            try:
                window.evaluate_js("window.showCloseDialog && window.showCloseDialog()")
            except Exception:
                return True  # can't ask — fall back to plain disconnect (keeps running)
            return False

        window.events.closing += on_closing
        window.events.closed += lambda *a: self._viewers.pop(name, None)
        return {"ok": True, "ws_port": ws_port}

    def viewer_close(self, name, stop_instance=False):
        """Called from the viewer popup: disconnect, optionally stopping the
        instance. Plain disconnect is the default — the instance keeps running."""
        result = {"ok": True}
        if stop_instance:
            result = self.engine_stop(name)
        rec = self._viewers.pop(name, None)
        if rec:
            rec["allow_close"] = True
            try:
                rec["window"].destroy()
            except Exception:
                pass
        return result

    # ---- shutdown ----

    def _shutdown(self):
        """Close viewer popups and websockify bridges. Instances keep running —
        only an explicit stop powers them off (engine contract)."""
        for rec in list(self._viewers.values()):
            rec["allow_close"] = True
            try:
                rec["window"].destroy()
            except Exception:
                pass
        self._viewers.clear()
        with self._proxy_lock:
            for rec in self._proxies.values():
                _terminate(rec["proc"])
            self._proxies.clear()


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
