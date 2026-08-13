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

    def __init__(self):
        self._window = None  # set in main() after the window is created
        self._maximized = False
        self._bootstrapping = False
        # Accounts this machine last reported as running, so the loop can send
        # exactly one "stopped" on the transition instead of every tick.
        self._known_running = set()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._updating = False

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
               "subscription": saved.get("subscription") or {}, "stale": True}
        try:
            fresh = cloud.me()
            out.update(email=fresh.get("email"), subscription=fresh.get("subscription") or {},
                       stale=False)
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

    def auth_register(self, email, password, key):
        try:
            data = cloud.register(email, password, key)
        except cloud.CloudError as exc:
            return {"ok": False, "error": exc.error or "register_failed", "message": exc.message}
        self._after_sign_in()
        return {"ok": True, **data}

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
        """Fire the launch-time check on a background thread.

        Off the startup path deliberately: the manifest is a network round trip
        and the window must not wait on it. The result arrives as an event, so
        a slow or unreachable server costs nothing visible."""
        def _run():
            try:
                import updates
                report = updates.check()
                report["staged"] = updates.staged_info()
                self._push("update-status", report)
            except Exception as exc:  # noqa: BLE001 — a check must never break a launch
                self._push("update-status", {"ok": False, "error": str(exc)})
        threading.Thread(target=_run, daemon=True).start()

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

        Refused while instances are running: the app is what holds their
        presence lease, and restarting mid-launch would make them look stopped
        on the user's other machines."""
        import updates
        live = self._running_names()
        if live:
            return {"ok": False, "error": "instances_running",
                    "message": f"Stop {', '.join(sorted(live))} first — restarting "
                               f"now would drop their running state."}
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
        explicit stop powers them off (engine contract).

        The presence lease is deliberately NOT released here: closing the app
        does not stop the VMs, so claiming they stopped would be a lie. The
        lease simply lapses ~90 s later, which is the honest answer for
        "nothing is reporting on this any more"."""
        self._heartbeat_stop.set()


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
    """
    import updates
    target, pid = argv[2], argv[3]
    try:
        updates.apply_staged_app(target, pid)
    except Exception as exc:  # noqa: BLE001
        # Nobody is watching a detached console; leave a trace where the app
        # (and the next support question) will look.
        try:
            log = bootstrap.runtime_dir() / "update.log"
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} apply-update "
                        f"failed: {exc}\n")
        except OSError:
            pass
        raise


def main():
    if len(sys.argv) > 3 and sys.argv[1] == "--apply-update":
        _apply_update_mode(sys.argv)
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
