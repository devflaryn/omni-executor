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
import cloud
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview
import windowchrome

APP_NAME = "omni-executor"
PROJECT_DIR = Path(__file__).resolve().parent
FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"
IS_MAC = sys.platform == "darwin"

# OMNI-EXEC remote-execute bridge base (serves the in-game UI + the exec queue).
# Override with the OMNI_EXEC_BASE env var or a settings.json "execBase" value.
OMNI_EXEC_BASE = os.environ.get("OMNI_EXEC_BASE", "http://72.62.59.232")

# DEV MODE, ABSENT FROM RELEASE BUILDS. Both PyInstaller specs exclude
# `devserver`, so a shipped app has no such module and _exec_base() resolves the
# production server exactly as it did before. See devserver.py.
try:
    import devserver as _devserver
except ImportError:  # pragma: no cover - the production path
    _devserver = None

# Some Windows installs register .js as text/plain, which makes the webview
# refuse to load ES modules.
mimetypes.add_type("text/javascript", ".js")

DEFAULT_SETTINGS = {
    "theme": "dark",
    "activeTab": "home",
    "sidebar": "expanded",
    "launch": {"mode": "gaming", "minimizeOnLaunch": False},
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
    # Captcha provider for automated solving during account creation.
    # apiKeys holds one key per provider id (see accountcreator.CAPTCHA_PROVIDERS);
    # an empty key means captchas wait for a human in the opened browser window.
    "captcha": {"provider": "2captcha", "apiKeys": {"2captcha": ""}},
}

# COMPATIBILITY SHIM, NOT A FEATURE. The engine offers two presets, `gaming`
# and `farming`; `playable`/`hard`/`brutal` were retired (they were `gaming`
# with different numbers). But settings.json OUTLIVES the update, and installed
# 1.0.14 clients persisted `"mode": "playable"` — so after updating, the app
# hands the engine a name argparse has never heard of and the user gets a
# subprocess error they can neither read nor act on. Map the dead names onto
# the live one instead. Nothing may OFFER these names: get_settings() rewrites
# a stale setting the first time it reads one, so each install stops needing
# this after one run, and the whole map can be deleted once no client that
# predates the two-preset release is still in the field.
RETIRED_MODES = {"playable": "gaming", "hard": "gaming", "brutal": "gaming"}


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
# Open editor tabs (name, contents, caret) -- its own file, because script
# bodies are large and churn on every keystroke while settings barely change.
EDITOR_FILE = config_dir() / "editor.json"

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
    _refresh_tools_in_background()


def _refresh_tools_in_background():
    """Pick up a newer published QEMU on an ordinary launch.

    ensure_tools() otherwise runs ONLY from bootstrap_start(), and
    bootstrap_start() runs only while `ready` is false — so an
    already-installed machine, which is every existing user, would never
    revisit its tools no matter how many builds were published. That is the
    same "installed once, stale forever" hole the receipt system was added to
    close, one level up.

    Two deliberate limits:

      * BACKGROUND THREAD. It makes a network call and may unpack 80 MB; the
        UI must not wait on either.
      * NEVER ELEVATES. It runs only when a QEMU already exists, so the plan
        can only ever be the portable route (needs_admin=False by
        construction). A launch that silently raised a UAC prompt would be
        a worse bug than a stale tool.
    """
    def work():
        try:
            import bootstrap
            rt = bootstrap.runtime_dir()
            if bootstrap.find_qemu(rt) is None:
                return          # a real install — that is bootstrap_start's job
            plan = bootstrap.qemu_install_plan(rt, bootstrap.tools_manifest())
            if not plan["needed"] or plan["needs_admin"]:
                return
            bootstrap.install_qemu_windows(rt, bootstrap.tools_manifest())
            bootstrap.record_tool(rt, "qemu", plan["portable"])
            print(f"[omni-exec] QEMU updated to "
                  f"{plan['portable'].get('version')}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — a stale tool beats a dead launch
            print(f"[omni-exec] tool refresh skipped: {e}", file=sys.stderr)

    import threading
    threading.Thread(target=work, name="tool-refresh", daemon=True).start()


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

# ...and then they agreed on a number that was still wrong, because BOTH sides
# were measuring duration. 660 s is generous on the machine it was measured on
# and nowhere near generous on a weak CPU, a thermally-throttled laptop, a box
# already running twenty instances, or a PC with no hardware virtualization at
# all, where the guest is EMULATED and a boot legitimately takes several times
# as long (the engine falls back to software emulation now rather than refusing
# to boot -- see omnidroid/accelprobe.py).
#
# So the app stopped measuring duration. The engine prints a progress line at
# least every 15 s for the whole of a boot, which makes SILENCE a signal and
# length not one. `IdleWatchdog` below is rearmed on every line the engine
# writes and fires only when the engine has genuinely stopped talking: a wedged
# engine is caught in about the time it always was, and a slow one is left to
# finish. `engine_start` no longer sends `--timeout` at all -- the engine waits
# on the guest's own progress signals and is in a far better position to judge.
#
# This is the budget for SILENCE, not for a boot. It has to clear the engine's
# 15 s cadence by a wide margin, because a host thrashing hard enough to boot
# slowly is also a host where a progress line can be late.
ENGINE_IDLE_TIMEOUT = 180
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

# The warm pool's own budgets. Both commands this app runs are short by
# construction: `pool start` writes the pool config and spawns a DETACHED
# manager, and `pool status` reads JSON off disk. Neither waits for a boot —
# that is `pool fill`, which blocks for the full 35-60 s per slot and is
# therefore never called from here (see Api.pool_start).
POOL_TIMEOUT = 90
# `pool stop` powers off every live slot the polite way first (adb shutdown,
# 60 s each, the same escalation as `stop`), so it needs room for all of them.
POOL_STOP_TIMEOUT = 300


class IdleWatchdog:
    """Kill the engine when it stops TALKING, not when it takes too long.

    `threading.Timer(timeout, proc.kill)` was an absolute deadline, and an
    absolute deadline is the wrong instrument here for two reasons:

      1. A boot's length is a property of the user's PC, which we do not know.
         Every host slower than the one the number was measured on had healthy
         launches killed.
      2. The engine spawns QEMU **detached**, so killing the engine does not
         stop the VM. A wrong kill does not fail cleanly -- it orphans a live
         instance holding gigabytes, reports failure to the UI, and leaves the
         user with nothing to click that would stop it.

    Rearmed by `poke()` on every line the engine writes. The engine emits a
    progress line at least every 15 s throughout a boot, so a window measured in
    minutes catches a genuinely wedged engine while never catching a slow one.
    `timeout=None` disarms it entirely and starts no thread.
    """

    def __init__(self, timeout, on_idle):
        self._timeout = timeout
        self._on_idle = on_idle
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._thread = None
        self.fired = False

    def start(self):
        if not self._timeout:
            return self
        self._last = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def poke(self):
        self._last = time.monotonic()

    def cancel(self):
        self._stop.set()

    def _run(self):
        # Poll rather than reschedule a Timer per line: a chatty engine would
        # otherwise create and cancel a thread for every line it prints.
        step = min(1.0, self._timeout / 4.0)
        while not self._stop.wait(step):
            if time.monotonic() - self._last >= self._timeout:
                self.fired = True
                try:
                    self._on_idle()
                except Exception:
                    pass
                return


def run_engine(args, progress=None, timeout=None):
    """Run an engine subcommand; return its stdout JSON normalized to a dict.

    stderr progress lines are forwarded to `progress`. List results are
    wrapped as {"ok": ..., "accounts": [...]}.

    `timeout` is a SILENCE budget, not a duration: see IdleWatchdog.
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
    # Dev mode, absent from release builds (see devserver.py). Engine
    # subprocesses inherit os.environ, so an OMNI_DEV_SERVER set in the shell
    # already reaches them -- but dev.json does not, and a redirected app
    # driving a guest that still calls the production server is the one
    # half-applied state this feature must not produce. Export it explicitly.
    if _devserver:
        dev = _devserver.dev_server()
        if dev and not os.environ.get(_devserver.DEV_ENV):
            env = {**(env or os.environ), _devserver.DEV_ENV: dev}

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
    watchdog = IdleWatchdog(timeout, proc.kill)

    def pump_stderr():
        for line in proc.stderr:
            # EVERY line, before the filtering: the engine talking at all is
            # what the watchdog is measuring, and a blank line is still the
            # engine being alive.
            watchdog.poke()
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

    watchdog.start()
    try:
        stdout = proc.stdout.read()
        code = proc.wait()
    finally:
        watchdog.cancel()

    data = _parse_engine_stdout(stdout, code)

    if watchdog.fired and data is None:
        # SAY WHAT ACTUALLY HAPPENED. The engine spawns QEMU detached, so a
        # killed engine can leave a fully-running instance behind; reporting a
        # bare failure would have the user believe nothing was started while
        # several gigabytes stayed committed. This is deliberately distinct
        # from a boot that the engine itself gave up on.
        #
        # ...and only when there is nothing usable on stdout. The engine writes
        # its JSON verdict and THEN exits, so an engine that wedges after
        # printing it has already done the work — throwing that away would
        # report a failure for a launch that succeeded, which is the same
        # "the UI says it failed while the VM runs" defect the idle watchdog
        # exists to stop.
        return {
            "ok": False,
            "error": "engine_unresponsive",
            "message": (f"The engine stopped reporting progress for "
                        f"{int(timeout)}s and was stopped. The instance may "
                        f"still be running — refresh the list, and use Stop if "
                        f"it appears there."),
        }
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


def accountsync():
    """The local<->cloud account sync module, imported lazily.

    It imports omnidroid's own accounts module, which on a source checkout means
    touching sys.path — work that has no business happening during GUI startup,
    and that must not run at all in the `--omnidroid` engine child."""
    import accountsync as mod
    return mod


class Api:
    """Methods exposed to JavaScript as window.pywebview.api.*"""

    # How often this machine renews the running lease on its instances. Must be
    # comfortably under the server's RUNNING_LEASE_MS (90 s) so a single missed
    # beat — a slow poll, a hiccup — never flips a live instance to "Stopped"
    # on the user's other machine.
    HEARTBEAT_SECONDS = 25

    # How often a RUNNING app asks whether a new build has been published.
    # 30 minutes: the manifest is a few hundred bytes, and the thing being
    # detected is a human publishing a release, which does not happen on a
    # scale where a tighter poll would tell anyone anything sooner.
    UPDATE_POLL_SECONDS = 1800

    def __init__(self):
        self._window = None  # set in main() after the window is created
        self._maximized = False
        # Windows only: the WndProc subclass that removes the OS caption while
        # keeping the OS frame (see windowchrome.py). None elsewhere.
        self._chrome = None
        self._bootstrapping = False
        # Set once WHPX has been turned on but Windows has not restarted yet.
        # Sticky for the process: DISM already reported the reboot, and the
        # feature is not live until it happens, so re-probing would say "off"
        # and offer to enable an already-enabled feature forever.
        self._reboot_required = False
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
        if self._chrome:
            # Ask the OS rather than trust a flag: Win+Up, a snap or a drag to
            # the top edge all change the state without passing through here.
            self._maximized = self._chrome.is_maximized()
        self._maximized = not self._maximized
        if self._maximized:
            self._window.maximize()
        else:
            self._window.restore()
        return self._maximized

    def get_window_state(self):
        maximized = self._chrome.is_maximized() if self._chrome else self._maximized
        return {"maximized": bool(maximized)}

    def begin_window_drag(self):
        """The titlebar was pressed: start the NATIVE move loop, so snapping,
        drag-to-top and the window manager's own gestures all apply."""
        return self._begin_native("caption")

    def begin_window_resize(self, edge):
        """A resize edge over the web content was pressed (the top edge, on
        Windows/Linux -- the others are the OS frame's own)."""
        if edge not in windowchrome.EDGE_HIT_TEST or edge == "caption":
            return False
        return self._begin_native(edge)

    def _begin_native(self, edge):
        if not self._window:
            return False
        if self._chrome:
            return self._chrome.begin_drag(edge)
        if IS_MAC:
            return windowchrome.mac_begin_drag(self._window) if edge == "caption" else False
        if sys.platform.startswith("linux"):
            return windowchrome.gtk_begin_drag(self._window, edge)
        return False

    def titlebar_double_click(self):
        """What the OS does when its own caption is double-clicked."""
        if not self._window:
            return False
        if self._chrome:
            return self._chrome.double_click()
        if IS_MAC:
            return windowchrome.mac_double_click(self._window)
        return self.toggle_maximize()

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
        settings = {**DEFAULT_SETTINGS, **saved}
        # Retire a dead preset name at the one point it enters the app, and
        # write the fix straight back. Translating it silently on every launch
        # instead would keep the RETIRED_MODES shim load-bearing forever, when
        # it is only meant to survive the first run after an update. The write
        # is best-effort: a settings file that can't be written must not stop
        # the app reading the settings it already has.
        launch = settings.get("launch")
        stale = launch.get("mode") if isinstance(launch, dict) else None
        if isinstance(stale, str) and stale in RETIRED_MODES:
            settings["launch"] = {**launch, "mode": RETIRED_MODES[stale]}
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
        tmp.replace(SETTINGS_FILE)

    def save_settings(self, settings):
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
        start. Under the engine's runtime dir (beside accounts/), which is the
        exact path omnidroid reads and pushes at launch, so what the user drops
        here is what runs."""
        import bootstrap
        d = bootstrap.runtime_dir() / "autoexec"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return str(d)

    def list_autoexec(self):
        """The scripts currently in the autoexec folder, in the order they run
        (filename order). Powers the Home widget so the user can see at a glance
        what every instance will execute at start."""
        from pathlib import Path
        d = Path(self.autoexec_dir())
        skip = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".zip", ".gz",
                ".exe", ".dll", ".so", ".ico", ".bmp", ".ttf", ".otf"}
        out = []
        try:
            for p in sorted(d.iterdir()):
                if p.is_file() and p.suffix.lower() not in skip:
                    try:
                        out.append({"name": p.name, "bytes": p.stat().st_size})
                    except OSError:
                        out.append({"name": p.name, "bytes": 0})
        except OSError:
            pass
        return {"ok": True, "dir": str(d), "scripts": out}

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
        import updates
        return updates.check()

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
                       ITSELF — download, swap, relaunch — and the user simply
                       arrives on the new version. That is what "auto-update"
                       has to mean to be worth having; a prompt at startup is
                       the thing people click past.
          while open   they ARE doing something. Downloading is still
                       automatic (it is invisible and makes the restart
                       instant), but the restart is theirs to schedule: this
                       process holds the presence lease and the editor buffer.
                       The UI gets a banner and a button, never a surprise.
        """
        import updates
        report = updates.check()
        report["staged"] = updates.staged_info()
        report["autoUpdate"] = self._auto_update_enabled()
        self._push("update-status", report)

        if not report.get("ok") or not report.get("app", {}).get("update"):
            return
        if not report["app"].get("canApply") or not report["autoUpdate"]:
            return

        version = report["app"].get("available")
        staged = report.get("staged")
        if not staged or staged.get("version") != version:
            if self._updating:
                return
            staged = self._stage_app_quietly(version)
            if not staged:
                return
        # The build is downloaded and one restart away. We do NOT swap silently:
        # the user asked for a popup that says an update is ready and lets THEM
        # click restart (a window that vanishes on its own at startup is exactly
        # what people find alarming). The staged state rides update-status /
        # update-done to the frontend, which shows the modal; this event marks
        # that it was a launch-time find so the modal opens on its own.
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

    def update_app(self):
        """Download a new build of this app and stage it.

        Staging only. The swap needs this process gone, so it happens in
        update_app_restart() — which is also the point where the user gets to
        say when."""
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
                    progress=lambda p: self._push("update-progress", p))
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
        those files are open by a live QEMU."""
        import updates
        try:
            pid = updates.launch_apply()
        except updates.UpdateError as exc:
            return {"ok": False, "error": "apply_failed", "message": str(exc)}
        # Give the child a moment to start waiting on this pid before it goes.
        threading.Timer(0.6, self.close).start()
        return {"ok": True, "helper_pid": pid}

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

    def _running_names(self):
        """Instances the ENGINE says are up on this machine, right now."""
        res = run_engine(["list", "--json"], timeout=60)
        accounts = res.get("accounts") if isinstance(res, dict) else None
        if not isinstance(accounts, list):
            return None                      # engine unhappy: report nothing, change nothing
        return {a.get("name") for a in accounts
                if isinstance(a, dict) and a.get("running") and a.get("name")}

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
            # Ask plan_downloads, rather than re-deriving readiness by
            # comparing every manifest entry. It is the ONE place that knows
            # which artifacts are actually installable — pointer entries with
            # no sha256 (qemu-win) and app builds (kind: "app") are neither
            # downloaded nor recorded here.
            #
            # REGRESSION this fixes: the moment an app build was published,
            # the old comparison found `app-win` in the manifest and not in
            # installed.json, decided the runtime was incomplete, and dropped
            # every already-installed client into the first-boot setup screen
            # — where it started re-downloading the base images. Seen live the
            # first time an app artifact went up.
            ready = not bootstrap.plan_downloads(manifest, installed)
        except bootstrap.BootstrapError as e:
            error = str(e)
            ready = bool(installed.get("artifacts"))  # offline-tolerant
        # The host TOOLS (QEMU, adb) are part of being ready, not a
        # precondition for becoming ready. Treating them as a precondition is
        # what deadlocked every fresh machine: bootstrap_start() was gated on
        # qemu_ok, and nothing in the app ever installed QEMU, so qemu_ok
        # could never become true and the download never began.
        ready = ready and eng.get("tools_ok", False)
        # WHPX is no longer a PREREQUISITE, and this comment used to say it
        # was: omnidroid booted with -accel whpx unconditionally and QEMU died
        # if the feature was off. The engine proves the accelerator now and
        # falls back to software emulation when the host has none
        # (omnidroid/accelprobe.py), so an unaccelerated PC runs the product --
        # slowly, and with a boot wait that tolerates slow (omnidroid/
        # bootwait.py). What is left for the UI to do is SAY SO, because the
        # difference is large and the fix is usually one BIOS switch.
        # whpx_ok stays tri-state (True/False/None-unknown); False is a
        # warning, never a blocker, and it never applies to a non-Windows host.
        accel = bootstrap.windows_accel_status()
        return {"ok": True, "ready": ready, "installed": installed.get("artifacts", {}),
                "qemu_ok": eng.get("qemu_ok", False), "qemu_hint": eng.get("qemu_hint"),
                "adb_ok": eng.get("adb_ok", False), "tools_ok": eng.get("tools_ok", False),
                "whpx_ok": accel.get("whpx_ok"), "whpx_hint": accel.get("hint"),
                "reboot_required": self._reboot_required,
                "error": error}

    def bootstrap_start(self):
        if self._bootstrapping:
            return {"ok": False, "error": "already running"}
        self._bootstrapping = True
        def _run():
            try:
                rt = bootstrap.runtime_dir()
                # Tools FIRST, images second. The 4 GB of base images are
                # useless without a QEMU to boot them, and this is the step
                # that was missing entirely -- the engine's own ensure_qemu()
                # only fires when an instance is started, which a user cannot
                # reach from the setup screen.
                self._push("bootstrap-progress",
                           {"phase": "tools", "artifact": "qemu",
                            "received": 0, "total": 0, "percent": 0})
                tools = bootstrap.ensure_tools(
                    rt, progress=lambda p: self._push("bootstrap-progress", p))
                if tools.get("reboot_required"):
                    self._reboot_required = True
                    self._push("bootstrap-reboot", tools)
                res = bootstrap.ensure_runtime(progress=lambda p: self._push("bootstrap-progress", p))
                bootstrap.configure_engine(rt)
                self._push("bootstrap-done", {**res, "tools": tools})
            except Exception as e:  # noqa: BLE001 — surface any failure to the UI
                self._push("bootstrap-error", {"error": str(e)})
            finally:
                self._bootstrapping = False
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "started": True}

    def enable_virtualization(self):
        """Turn on Windows Hypervisor Platform (one UAC prompt + a restart).

        Exposed separately from the first-boot flow for the case it cannot
        cover: a machine where QEMU was ALREADY installed, so the setup screen
        never needed elevation, and the feature turns out to be off only when
        QEMU is finally there to be asked."""
        try:
            res = bootstrap.enable_whpx()
        except bootstrap.BootstrapError as e:
            return {"ok": False, "error": str(e)}
        if res.get("reboot_required"):
            self._reboot_required = True
        return {"ok": res["ok"], "reboot_required": res["reboot_required"],
                "message": ("Virtualization is on. Restart Windows to finish."
                            if res["reboot_required"] else
                            "Virtualization is already on.")}

    def restart_windows(self):
        """Reboot, at the user's explicit click, after enabling WHPX."""
        if sys.platform != "win32":
            return {"ok": False, "error": "windows only"}
        subprocess.Popen(["shutdown", "/r", "/t", "5"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": True}

    # Two modes. The engine reports the same pair from `version --json`; this
    # only covers an engine too old to report any. It doubles as the set
    # engine_start() accepts, so a name that is neither of these nor a
    # RETIRED_MODES alias is refused here rather than three layers down.
    _FALLBACK_MODES = ["gaming", "farming"]

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
            rep["pool_supported"] = self._supports(rep, "pool")
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

    # OPTIONAL engine commands: used when the engine has them, and the feature
    # disappears from the UI when it doesn't. Deliberately NOT in
    # _REQUIRED_COMMANDS — an engine without `pool` is older, not broken, and
    # listing it there would turn "no warm pool on this build" into the
    # version-mismatch banner and a warning about an install that works.
    _OPTIONAL_COMMANDS = ("pool",)

    @staticmethod
    def _supports(report, command):
        """Does this engine advertise `command`?

        Unknown counts as NO here, the opposite of _missing_commands. The
        asymmetry is deliberate and follows from what each answer costs: for a
        required command, guessing "missing" would put a false alarm in front
        of a working install; for an optional one, guessing "present" would put
        a button in front of the user that ends in an argparse error from a
        subprocess. Every engine that has `pool` also reports its commands."""
        advertised = report.get("commands") if isinstance(report, dict) else None
        if not isinstance(advertised, list):
            return False
        return command in {c for c in advertised if isinstance(c, str)}

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

    def engine_login_browser(self):
        """Add an account by interactive Roblox sign-in (omnidroid opens its
        own browser). The account is saved under its Roblox username."""
        res = run_engine(["login"], progress=self._progress("login"), timeout=360)
        self._publish_account(res)
        return res

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
            res = run_engine(["login", "--token-file", path, "--json"],
                             progress=self._progress("login"), timeout=120)
            self._publish_account(res)
            return res
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    # ---- account creation (roblox.com signup, captcha, vault) ----

    def creation_get_config(self):
        """Saved creation + captcha preferences, merged over defaults."""
        s = self.get_settings()
        creation = {**DEFAULT_SETTINGS["creation"], **(s.get("creation") or {})}
        captcha = {**DEFAULT_SETTINGS["captcha"], **(s.get("captcha") or {})}
        keys = {**(DEFAULT_SETTINGS["captcha"]["apiKeys"]), **(captcha.get("apiKeys") or {})}
        captcha["apiKeys"] = keys
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
        provider_in = cfg.get("captchaProvider",
                              patch_captcha.get("provider") if isinstance(patch_captcha, dict) else None)
        keys_in = cfg.get("captchaApiKeys",
                          patch_captcha.get("apiKeys") if isinstance(patch_captcha, dict) else None)

        clean, error = accountcreator.validate_creation_config(
            amount=amount_in if amount_in is not None else current["creation"].get("amount", 1),
            username_style=style_in,
            custom_password=None,          # never persisted (see DEFAULT_SETTINGS)
            captcha_provider=provider_in if provider_in is not None else current["captcha"].get("provider"),
            captcha_api_keys=keys_in if keys_in is not None else current["captcha"].get("apiKeys"),
        )
        if error:
            return {"ok": False, "error": "bad_config", "message": error}
        return self.save_settings({
            "creation": {"amount": clean["amount"],
                         "usernameStyle": clean["usernameStyle"]},
            "captcha": {"provider": clean["captchaProvider"],
                        "apiKeys": clean["captchaApiKeys"]},
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
            captcha_provider=cfg.get("captchaProvider", saved["captcha"].get("provider")),
            captcha_api_keys=cfg.get("captchaApiKeys", saved["captcha"].get("apiKeys")),
        )
        if error:
            return {"ok": False, "error": "bad_config", "message": error}
        solver = accountcreator.make_solver(clean["captchaProvider"],
                                            clean["captchaApiKeys"])
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
                    res = accountcreator.create_account(
                        on_status=say,
                        stop_check=self._creation_stop.is_set,
                        username_style=clean["usernameStyle"],
                        custom_password=clean["customPassword"],
                        solver=solver)
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

    def engine_modes(self):
        """Mode list DERIVED from the engine's version report; falls back to a
        constant (incl. farming) on an older engine that doesn't report modes."""
        rep = run_engine(["version", "--json"], timeout=30)
        modes = rep.get("modes") if isinstance(rep, dict) else None
        return modes if isinstance(modes, list) and modes else list(self._FALLBACK_MODES)

    GPU_POLICIES = ("auto", "headless", "window", "off")

    def engine_start(self, name, mode=None, place=None, gpu=None):
        """Cold-boot an instance detached (returns pid + ports).

        `gpu` is the display/acceleration policy (see the engine's `--gpu`).
        It is offered in the UI because on some hosts it is a REAL trade the
        user has to make and not something this app can decide for them: QEMU
        refuses a VNC server beside a GL window, so on a host where a window is
        the only route to a GL context, `auto` buys ~24 fps and costs the
        viewer, while `headless` keeps the viewer at ~3 fps. Validated here
        rather than passed through, so a stale setting cannot turn into an
        argparse error from a subprocess.

        `mode` gets the same treatment, and needs it more: settings.json can
        name a preset that no longer exists (see RETIRED_MODES)."""
        error = self._bad_name(name)
        if error:
            return error
        # No `--timeout`: the engine waits on the GUEST'S OWN progress signals
        # (serial log, adb endpoint, dexopt count, QEMU's CPU time) and gives up
        # on silence rather than on duration, which is the only way a boot can
        # be judged without knowing how fast the user's PC is. Imposing a number
        # from here would put the old ceiling straight back.
        args = ["start", name, "--json"]
        if isinstance(mode, str) and mode.strip():
            asked = mode.strip().lower()
            # Resolve the alias BEFORE argv, so what we launch, what we report
            # in the lease, and what the engine records all say the live name.
            mode = RETIRED_MODES.get(asked, asked)
            if mode not in self._FALLBACK_MODES:
                return {
                    "ok": False,
                    "error": "bad_mode",
                    "message": f"Unknown launch mode {asked!r}. Choose Gaming or Farming.",
                }
            args += ["--mode", mode]
        if isinstance(gpu, str) and gpu.strip().lower() in self.GPU_POLICIES:
            args += ["--gpu", gpu.strip().lower()]
        if place is not None and str(place).strip():
            args += ["--place", str(place).strip()]
        result = run_engine(args, progress=self._progress(name),
                            timeout=ENGINE_IDLE_TIMEOUT)
        if result.get("ok"):
            # Claim the lease NOW rather than waiting up to a heartbeat period:
            # the in-game exec bridge cannot claim its poll token until this
            # account is registered as launched, so a late beat would show up as
            # "no live session" for the first half-minute of every launch.
            self._beat_now(name, "running", mode=mode, place=place)
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
        if result.get("ok"):
            self._beat_now(name, "stopped")
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

    # ---- warm pool ----
    #
    # N instances pre-booted to the account-free ready point, so a launch skips
    # the boot entirely. Measured on this box: the boot stage of a cold farming
    # launch is 35-60 s, adopting a warm slot is 0.093 s. WHPX cannot snapshot
    # a VM on Windows, so a pool of live machines is the ONLY fast path there.

    # A slot is a fully booted VM, so the ceiling here is a resource ceiling
    # rather than an argparse one: each one costs ~2.2-3.2 GB of host RSS and
    # ~1.3 GB of disk scratch while it waits. Four is already most of a normal
    # machine; a mistyped 40 has to be refused, not dutifully passed on.
    POOL_MAX_SIZE = 4

    @staticmethod
    def _pool_size(size):
        """(size, None), or (None, error dict) for anything else.

        Bools are rejected explicitly because `isinstance(True, int)` is True
        in Python: a JS `true` arriving on this bridge would otherwise be
        accepted as "keep 1 warm" from a caller that meant to pass a flag."""
        if isinstance(size, bool):
            n = None
        elif isinstance(size, int):
            n = size
        elif isinstance(size, float) and size.is_integer():
            n = int(size)      # JSON numbers cross the bridge as floats
        elif isinstance(size, str) and size.strip().isdigit():
            n = int(size.strip())
        else:
            n = None
        if n is None or not 1 <= n <= Api.POOL_MAX_SIZE:
            return None, {
                "ok": False,
                "error": "bad_pool_size",
                "message": f"Keep between 1 and {Api.POOL_MAX_SIZE} instances "
                           f"warm — each one is a running virtual machine.",
            }
        return n, None

    def _pool_mode(self, mode):
        """(mode, None), or (None, error dict).

        Checked against the engine's OWN mode list rather than _FALLBACK_MODES,
        because this decides what gets warmed for hours: a name a newer engine
        added is worth warming, and a name it dropped must fail here with a
        sentence, not three layers down as argparse's stderr in a subprocess
        nobody sees. Aliases resolve first for the same reason as in
        engine_start — settings.json can still name a retired preset."""
        asked = mode.strip().lower() if isinstance(mode, str) else ""
        if not asked:
            return None, {
                "ok": False,
                "error": "bad_mode",
                "message": "Pick a launch mode before keeping an instance warm "
                           "— a slot is only usable by a launch in the same mode.",
            }
        resolved = RETIRED_MODES.get(asked, asked)
        if resolved not in self.engine_modes():
            return None, {
                "ok": False,
                "error": "bad_mode",
                "message": f"Unknown launch mode {asked!r}. Choose Gaming or Farming.",
            }
        return resolved, None

    def _pool_receipt(self):
        """What THIS app last warmed the pool for, or None.

        The receipt exists because the engine cannot answer the question. Its
        pool records the RESOLVED machine (mode, mem, vCPU, panel...), and the
        place id that chose that mem is not in it — so "is the warm pool still
        the one my launch settings would use?" is only answerable against what
        we asked for."""
        try:
            saved = self.get_settings().get("pool")
        except Exception:  # noqa: BLE001 — a settings problem must not break the pool
            return None
        spec = saved.get("warmedFor") if isinstance(saved, dict) else None
        return spec if isinstance(spec, dict) else None

    def _remember_pool(self, spec, size=None):
        """Record (or clear, with spec=None) the receipt. Best-effort, exactly
        like the settings migration in get_settings(): an unwritable settings
        file costs one extra re-warm later, and must not turn a pool that IS
        running into an error the user has to read."""
        try:
            self.save_settings({"pool": {} if spec is None
                                else {"warmedFor": spec, "size": size,
                                      "at": time.time()}})
        except OSError:
            pass

    def pool_status(self):
        """What the pool is doing, plus this app's receipt for it.

        The number that means "your next launch is instant" is
        `ready_matching`, not `ready`: it counts only ready slots whose key
        matches the pool's configured spec, i.e. the ones a launch can actually
        adopt. The UI must show that one, and only when `warmed_for` still
        matches the settings in the launch bay (see pool_start)."""
        res = run_engine(["pool", "status", "--json"], timeout=POOL_TIMEOUT)
        if isinstance(res, dict):
            res["warmed_for"] = self._pool_receipt()
        return res

    def pool_start(self, size=1, mode=None, place=None, gpu=None):
        """Keep `size` instances pre-booted for EXACTLY these launch settings.

        `pool start`, never `pool fill`. Start writes the config, spawns the
        detached manager and returns in a moment; fill boots the slots in the
        calling process, which is this process, and would hold the UI thread
        for the whole 35-60 s per slot.

        THE MATCHING RULE, which is the entire point of the feature. A slot is
        only ever adopted by a launch that resolves to the SAME machine, and
        `--place` is part of that: the engine raises guest RAM to a per-game
        floor (PS99 -> 3072 MB) and `mem` is hashed into the slot key. A pool
        warmed without the place the user is about to launch is INVISIBLE to
        that launch — every launch cold-boots while `pool status` cheerfully
        reports slots ready. So the pool is warmed with the same mode/place/gpu
        the launch bay is holding, and a change to any of them re-warms it.

        RE-WARMING STOPS THE OLD POOL FIRST, and that is not tidiness. `pool
        start` alone would only overwrite the pool's key; the manager's sweep
        deletes DEAD slots only (verified liveness is its single gate on
        deletion), so the slots warmed for the old settings would stay booted,
        never be adopted by anything, and hold their ~2.2-3.2 GB of RAM and
        ~1.3 GB of scratch until somebody went looking. A pool for settings
        nobody is going to launch is pure cost, so it is never left running.
        """
        n, error = self._pool_size(size)
        if error:
            return error
        mode, error = self._pool_mode(mode)
        if error:
            return error
        place = str(place).strip() if place is not None else ""
        # An unrecognised policy is dropped rather than forwarded, the same as
        # engine_start — and it has to be the same, or the pool would be keyed
        # on a display policy the launch never asks for.
        gpu = gpu.strip().lower() if isinstance(gpu, str) else ""
        if gpu not in self.GPU_POLICIES:
            gpu = ""
        wanted = {"mode": mode, "place": place, "gpu": gpu}

        status = run_engine(["pool", "status", "--json"], timeout=POOL_TIMEOUT)
        configured = status.get("configured") if isinstance(status, dict) else None
        if configured and self._pool_receipt() != wanted:
            # Something is warm for other settings (or for settings this app
            # has no receipt for), and we are about to take the config over.
            # Leaving those slots live would strand them: see the docstring.
            run_engine(["pool", "stop", "--json"], timeout=POOL_STOP_TIMEOUT)
            self._remember_pool(None)

        args = ["pool", "start", "--size", str(n), "--mode", mode, "--json"]
        if place:
            args += ["--place", place]
        if gpu:
            args += ["--gpu", gpu]
        res = run_engine(args, progress=self._progress("pool"), timeout=POOL_TIMEOUT)
        if res.get("ok"):
            self._remember_pool(wanted, n)
        return {**res, "warmed_for": self._pool_receipt()}

    def pool_stop(self):
        """Stop the manager and power off the warm slots — the RAM and the
        scratch come back now.

        The receipt is cleared only on success. If the stop failed the slots
        are still up, and forgetting that we warmed them is how a pool nobody
        wants survives every later chance to notice it (the UI re-tries the
        stop on mount whenever the toggle is off and a receipt exists)."""
        res = run_engine(["pool", "stop", "--json"], timeout=POOL_STOP_TIMEOUT)
        if res.get("ok"):
            self._remember_pool(None)
        return res

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

    def engine_hide(self, name):
        """Hide an instance's window without stopping it.

        The window's own X asks (hide or stop); this is the same 'hide' from
        the app side, so a window that was hidden can always be brought back
        by View without the user having to find it on the desktop.
        """
        error = self._bad_name(name)
        if error:
            return error
        return run_engine(["view", name, "--hide", "--json"],
                          timeout=VIEW_TIMEOUT)

    # ---- shutdown ----

    def _shutdown(self):
        """No embedded viewer/proxy state to tear down — the engine owns its
        own viewer windows. Instances keep running regardless; only an
        explicit stop powers them off (engine contract).

        The presence lease is deliberately NOT released here: closing the app
        does not stop the VMs, so claiming they stopped would be a lie. The
        lease simply lapses ~90 s later, which is the honest answer for
        "nothing is reporting on this any more"."""
        self._heartbeat_stop.set()
        # Stop the update watcher too, so a 30-minute wait cannot hold the
        # interpreter open past the window closing.
        self._update_stop.set()


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
    `Zone.Identifier` stream on EVERY extracted file. The .NET Framework
    assembly loader then refuses to load Python.Runtime.dll out of the
    Internet zone, clr_loader cannot resolve its entry point, and pywebview's
    WinForms backend dies on import — before a single line of this program's
    own logic runs:

        RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
        ...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

    Reproduced exactly by putting that one stream on that one DLL, and cured
    by removing it. It is invisible in development because a build produced
    locally was never downloaded, so it is never marked.

    The proper fix is to ship an installer (files an installer writes are not
    marked). This stays anyway: people extract zips, and a program that cannot
    start is not the place to be principled about whose fault it is.

    Cheap on a normal launch — one stat of the DLL that actually matters, and
    a full sweep only when that comes back marked. Best-effort throughout: an
    install in a read-only location cannot be unmarked, but it also cannot
    have been marked, because an installer put it there.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return 0
    root = Path(sys.executable).resolve().parent
    probe = root / "_internal" / "pythonnet" / "runtime" / "Python.Runtime.dll"
    if not _has_motw(probe) and not _has_motw(Path(sys.executable)):
        return 0
    cleared = 0
    # The whole tree, not just that DLL: Python.Runtime.dll pulls in ~100
    # netstandard facade assemblies beside it, and each is refused on the same
    # grounds. Clearing one file only moves the error to the next one.
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

    # Before anything imports the CLR. webview.start() is what pulls in the
    # WinForms backend, so this only has to beat the bottom of this function —
    # but it is first because everything below it is downstream of the app
    # being loadable at all.
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
            "elevated": bootstrap.is_elevated(),
            **bootstrap.engine_ready(rt),
            "accel": bootstrap.windows_accel_status(),
            "installed": sorted(bootstrap.installed_state(rt)
                                .get("artifacts", {})),
        }, indent=2))
        return

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
    # Renew running leases from launch, so a second machine sees this one's
    # instances as "Running on <name>" without waiting for a UI action here.
    api.start_heartbeat()
    # Ask what is out of date on every launch — base images, offsets and this
    # app alike. Background, so nothing waits on the network.
    api._check_updates_on_launch()
    window = webview.create_window(
        "Omni Executor",
        url=str(index),
        js_api=api,
        width=1380,
        height=840,
        min_size=(720, 480),
        background_color="#17181b",  # matches the dark sheet, prevents a white flash on startup
        # The frontend draws the titlebar everywhere; the WINDOW stays the
        # OS's. Windows: windowchrome puts the frame styles back on the HWND
        # and keeps the client area full-window, so snap, double-click,
        # shadows and native move/size loops all work. macOS: frameless is a
        # titled window with a transparent titlebar (the traffic lights come
        # back below). Linux: undecorated, the WM is asked to move/resize.
        frameless=True,
        easy_drag=False,  # dragging is the frontend's call (begin_window_drag)
    )
    api._window = window

    if IS_MAC:
        window.events.shown += lambda *a: _show_macos_traffic_lights(window)
    elif windowchrome.IS_WIN:
        def _install_chrome(*_args):
            api._chrome = windowchrome.install_windows(window)
        window.events.shown += _install_chrome

    # Keep the maximize state in sync when the OS changes it (e.g. Win+Up snap),
    # and tell the titlebar so its button glyph follows.
    def _window_state(maximized):
        api._maximized = maximized
        api._push("window-state", {"maximized": maximized})
    try:
        window.events.maximized += lambda *a: _window_state(True)
        window.events.restored += lambda *a: _window_state(False)
    except AttributeError:
        pass  # older pywebview without these events; manual toggling still works

    window.events.closed += lambda *a: api._shutdown()
    atexit.register(api._shutdown)

    try:
        webview.start()
    except RuntimeError as e:
        # This is where the WinForms/CLR backend is actually loaded, and the
        # one failure a user can do something about. _unblock_app_files() has
        # already handled the common cause; anything left is a machine
        # problem, and a raw traceback dialog tells the reader nothing.
        if "Python.Runtime" not in str(e):
            raise
        _fatal_dialog(
            "Omni Executor could not start",
            "Windows blocked part of Omni Executor from loading.\n\n"
            "This normally happens when the app is run straight out of a "
            "downloaded .zip. Two fixes, either works:\n\n"
            "  1. Install it with the Omni Executor installer instead of "
            "unzipping it.\n"
            "  2. Right-click the .zip you downloaded, choose Properties, "
            "tick Unblock, then extract it again.\n\n"
            f"Details: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
