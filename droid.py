"""The omnidroid pool, as this app drives it.

WHAT CHANGED, AND WHY THIS FILE IS SMALL. The executor used to carry a whole
engine: a Python QEMU manager frozen into the app, a Bliss OS base image per
architecture, a warm pool of virtual machines, gaming and farming presets, a
GPU policy, a VNC viewer. Native omnidroid replaces all of it. It runs the
Android userspace as ordinary desktop processes (no VM, no QEMU, no adb), keeps
its own warm Androids, opens a window per instance, and is driven over one MCP
server. So this module only has to do three things:

  1. find the omnidroid bundle (bootstrap downloads it to <runtime>/omnidroid),
  2. make sure its pool is serving (`omnidroid serve --port <p>`), and
  3. call its tools: start_instance, list_instances, stop_instance.

THE POOL IS THIS PROCESS'S CHILD. `omnidroid serve` runs until its stdin
closes, and its stdin is a pipe this process holds -- so the pool, and every
instance in it, ends when the app does, however the app ends. That is a
deliberate change from the QEMU days, when VMs were detached and outlived the
window: an instance nobody can see or stop from the app is the bug that
"it always spawns _pool0, 1, 2" was. One exception: if an omnidroid pool is
ALREADY serving on the port (the person has the omnidroid app open with MCP
on), it is used as it is and left running -- omnidroid is one pool per
computer, and that one is not ours to end.

THE TRANSPORT is omnidroid's MCP over HTTP (crates/omni-mcp/src/http.rs):
`POST http://127.0.0.1:<port>/mcp`, one JSON-RPC message in, its answer as
application/json out, no event stream and no real session (the server always
says `Mcp-Session-Id: omnidroid`). A tool's result is JSON text in
`result.content[0].text`; a tool's failure is a JSON-RPC error.
"""

import itertools
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import bootstrap

# omnidroid's own default MCP port (its app.conf `mcp_port`). Sharing it is the
# point: a person with the omnidroid app open and MCP on is already running the
# pool this app would start, and a second one on another port would be a second
# set of Androids for the same machine.
DEFAULT_PORT = 7457
PORT_ENV = "OMNIDROID_MCP_PORT"
# Where the bundle is, for a build that wants a specific one (dev, tests).
HOME_ENV = "OMNIDROID_HOME"
# The executor APK, for the same reason.
APK_ENV = "OMNI_EXEC_APK"

# The bundle's folder under the runtime dir, and the APK's. Both are the
# `dest` of their dist artifacts (see bootstrap.NATIVE_CHANNEL).
BUNDLE_SUBDIR = "omnidroid"
APK_SUBDIR = "apk"
APK_NAME = "OmniExecutor.apk"

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# A probe of a port nothing listens on. Short, because on some Windows hosts a
# closed loopback port does not refuse -- the connect simply times out
# (see the memory note "Windows loopback blackhole").
_PROBE_TIMEOUT = 1.5
# How long a fresh `serve` gets to start answering.
_SERVE_START_TIMEOUT = 45


class DroidError(Exception):
    """Something omnidroid said no to, or could not be reached for. The
    message is meant for a person: it is shown as it is."""


def _exe_name():
    return "omnidroid.exe" if sys.platform == "win32" else "omnidroid"


def bundle_dir():
    """The omnidroid install this app runs, or None.

    In order: OMNIDROID_HOME; the bundle bootstrap downloaded; and, running
    from source, the sibling omnidroid checkout's release build -- which is a
    repo, not a bundle, and finds its own sysroot that way."""
    override = os.environ.get(HOME_ENV)
    if override and (Path(override) / _exe_name()).is_file():
        return Path(override)
    installed = bootstrap.runtime_dir() / BUNDLE_SUBDIR
    if (installed / _exe_name()).is_file():
        return installed
    if not getattr(sys, "frozen", False):
        repo = Path(__file__).resolve().parent.parent / "omnidroid"
        built = repo / "target" / "release"
        if (built / _exe_name()).is_file():
            return built
    return None


def apk_path():
    """The executor APK every instance runs, or None."""
    override = os.environ.get(APK_ENV)
    if override and Path(override).is_file():
        return Path(override)
    installed = bootstrap.runtime_dir() / APK_SUBDIR / APK_NAME
    if installed.is_file():
        return installed
    if not getattr(sys, "frozen", False):
        dev = Path(__file__).resolve().parent.parent / "omni-exec-android" / "OmniExecutor-v0.apk"
        if dev.is_file():
            return dev
    return None


def bundle_version():
    """BUNDLE.json's version (the omnidroid commit it was built from), or None."""
    home = bundle_dir()
    try:
        return json.loads((home / "BUNDLE.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, TypeError, AttributeError):
        return None


class Pool:
    """One omnidroid pool, reached over its MCP server."""

    def __init__(self, port=None):
        self.port = int(port or os.environ.get(PORT_ENV) or DEFAULT_PORT)
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._proc = None          # our `serve`, when we started it
        self._log = None
        self._initialized = False

    # ---- transport ----

    def _post(self, message, timeout):
        body = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", errors="replace")
        return json.loads(text) if text.strip() else None

    def _rpc(self, method, params=None, timeout=30):
        msg = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            msg["params"] = params
        try:
            answer = self._post(msg, timeout)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise DroidError(f"omnidroid is not answering on port {self.port}: {e}") from e
        if not isinstance(answer, dict):
            raise DroidError("omnidroid answered nothing")
        if answer.get("error"):
            err = answer["error"]
            raise DroidError(str(err.get("message") if isinstance(err, dict) else err))
        return answer.get("result")

    def _alive(self):
        try:
            self._post({"jsonrpc": "2.0", "id": 0, "method": "ping"}, _PROBE_TIMEOUT)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # ---- lifecycle ----

    def ensure(self):
        """Serving, or a DroidError that says why not. Idempotent and cheap
        once up: one ping."""
        with self._lock:
            if self._alive():
                return self._handshake()
            if self._proc is not None and self._proc.poll() is None:
                # Ours, and still starting (or wedged): wait below.
                pass
            else:
                self._spawn()
            deadline = time.monotonic() + _SERVE_START_TIMEOUT
            while time.monotonic() < deadline:
                if self._proc is not None and self._proc.poll() is not None:
                    raise DroidError(
                        f"omnidroid stopped while starting (exit {self._proc.returncode}); "
                        f"its log is {self.log_path()}")
                if self._alive():
                    return self._handshake()
                time.sleep(0.5)
            raise DroidError(f"omnidroid did not start serving within {_SERVE_START_TIMEOUT}s; "
                             f"its log is {self.log_path()}")

    def _handshake(self):
        if not self._initialized:
            self._rpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "omni-executor", "version": _app_version()},
            })
            self._initialized = True
        return True

    @staticmethod
    def log_path():
        return bootstrap.runtime_dir() / "omnidroid-serve.log"

    def _spawn(self):
        home = bundle_dir()
        if home is None:
            raise DroidError("omnidroid is not installed yet -- finish setup first.")
        env = dict(os.environ)
        # The bundle finds its sysroot and tools next to its exe; saying so
        # outright costs nothing and survives a bundle moved under a symlink.
        if (home / "sysroot").is_dir():
            env.setdefault("OMNI_HOME", str(home))
        # Dev mode, absent from release builds (see devserver.py): a dev.json
        # redirect reaches this process but not the environment, and a pool
        # started without it would leave the guests calling production while
        # the app talks to the local backend -- the one half-applied state
        # dev mode must not produce.
        dev = bootstrap._devserver.dev_server() if bootstrap._devserver else None
        if dev and not env.get(bootstrap._devserver.DEV_ENV):
            env[bootstrap._devserver.DEV_ENV] = dev
        self._log = open(self.log_path(), "ab")
        self._proc = subprocess.Popen(
            [str(home / _exe_name()), "serve", "--port", str(self.port)],
            cwd=str(home), env=env,
            # The pipe IS the pool's lifetime: see the module docstring.
            stdin=subprocess.PIPE, stdout=self._log, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW)
        self._initialized = False

    def owned(self):
        """Did this process start the pool (and so ends it)?"""
        return self._proc is not None and self._proc.poll() is None

    def shutdown(self):
        """End the pool we started: closing its stdin is how `serve` is told.
        A pool somebody else started is left alone."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        finally:
            if self._log:
                try:
                    self._log.close()
                except OSError:
                    pass
                self._log = None

    # ---- tools ----

    def call(self, tool, args=None, timeout=60):
        """A tool's answer, decoded. Raises DroidError for its refusal."""
        self.ensure()
        result = self._rpc("tools/call", {"name": tool, "arguments": args or {}}, timeout=timeout)
        content = (result or {}).get("content") or []
        text = content[0].get("text") if content and isinstance(content[0], dict) else None
        if (result or {}).get("isError"):
            raise DroidError(text or f"{tool} failed")
        if text is None:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}

    def instances(self):
        """Every instance the pool has had, keyed by id. Empty when the pool
        is not up -- a list that cannot be read is "nothing running", and the
        app must not start a pool just to answer that."""
        if not self._alive():
            return {}
        try:
            res = self.call("list_instances", timeout=15)
        except DroidError:
            return {}
        out = {}
        for inst in res.get("instances") or []:
            if isinstance(inst, dict) and "instance_id" in inst:
                out[inst["instance_id"]] = inst
        return out


def _app_version():
    try:
        import updates
        return updates.APP_VERSION
    except Exception:  # noqa: BLE001 - a handshake label is never worth a failure
        return "0"


# What an instance's state means to the Accounts list. Everything before
# `ended` is the instance being alive in some sense; the UI calls all of it
# running (with the state shown), because Stop is the right button for each.
LIVE_STATES = ("waiting", "installing", "starting", "running", "restarting", "closing")


def is_live(state):
    return isinstance(state, str) and state.split(":", 1)[0].strip() in LIVE_STATES
