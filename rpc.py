"""The backend half of the Tauri bridge: JSON-RPC over this process's stdio.

This replaced pywebview's `js_api`. The window is a Tauri (Rust) process now
and this one is its child; the two speak newline-delimited JSON over the pipe
between them, which is why there is no port, no token and no firewall prompt
anywhere in the app.

    in   {"id": 7, "method": "engine_start", "args": ["farm_alpha", "gaming"]}
    out  {"id": 7, "ok": true,  "result": {...}}
         {"id": 7, "ok": false, "error": "...", "message": "..."}

and, unprompted, the push bus that `Api._push` has always used:

    out  {"event": "engine-progress", "payload": {"scope": "...", "line": "..."}}

Two things here are load-bearing and easy to undo by accident:

STDOUT IS NOT A LOG. `main.py`, `bootstrap.py` and half the modules they pull
in `print()` freely, and one stray line on fd 1 corrupts a frame and takes the
bridge down. `serve()` therefore moves the real stdout to a private descriptor
and points `sys.stdout` at stderr, so ordinary prints keep working and land
somewhere useful while only this module can write frames.

EVERY CALL GETS ITS OWN THREAD. pywebview ran each JS call on its own thread
and the `Api` methods were written expecting that: `engine_start` blocks for
tens of seconds, `bootstrap_start` for minutes, and the Network tab polls
throughout. Dispatching in the read loop would serialise all of it behind the
slowest call.
"""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

# Bounded rather than a thread per call: the UI polls (Network, Stat Track,
# creation status) and a runaway page should not be able to spawn threads
# without limit. 64 is far above anything the app actually does at once — the
# most concurrent work it has ever asked for is one long engine call plus a
# handful of polls.
MAX_WORKERS = 64


class Bridge:
    """Owns the write end of the pipe. One per process."""

    def __init__(self, out):
        self._out = out
        # Frames must not interleave: a push can fire from an engine progress
        # callback on any thread while a reply is being written.
        self._lock = threading.Lock()

    def send(self, message):
        """Write one frame. Never raises: the pipe closing means the window is
        gone, and there is nothing useful left to report to."""
        try:
            line = json.dumps(message, default=str)
        except (TypeError, ValueError) as exc:
            # A method returned something json cannot express. Say so on the
            # channel the caller is waiting on rather than hanging it.
            if "id" not in message:
                return
            line = json.dumps({
                "id": message["id"], "ok": False, "error": "unserializable",
                "message": f"the backend returned something unserializable: {exc}",
            })
        with self._lock:
            try:
                self._out.write(line + "\n")
                self._out.flush()
            except (OSError, ValueError):
                pass

    def push(self, event, payload=None):
        """Fire an event at the frontend (arrives as `window.omniEvent`)."""
        self.send({"event": event, "payload": payload})


def _claim_stdin():
    """A reader on fd 0, taken directly rather than through `sys.stdin`.

    The shipped backend is a WINDOWED PyInstaller build (console=False, so
    nothing flashes a black box when the engine re-executes it), and a windowed
    build is exactly where Python may decide it has no usable standard streams
    and set `sys.stdin` to None. The pipe on fd 0 is real either way, because
    the shell handed it to us, so read the descriptor and skip the question.
    """
    return os.fdopen(0, "r", encoding="utf-8", errors="replace", newline="")


def _claim_stdout():
    """Take fd 1 for the bridge and give ordinary prints to stderr.

    Returns a text stream on a private duplicate of the original stdout. After
    this, `print()` — from anywhere, including code that has already captured
    `sys.stdout` — goes to stderr.
    """
    saved_fd = os.dup(1)
    try:
        os.dup2(2, 1)
    except OSError:
        # No usable stderr (a frozen GUI build with no console attached can
        # have fd 2 closed). Prints then go nowhere, which is strictly better
        # than corrupting the bridge.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    sys.stdout = sys.stderr
    return os.fdopen(saved_fd, "w", encoding="utf-8", newline="\n")


def dispatch(api, message, bridge):
    """Run one request and answer it. Called on a worker thread."""
    call_id = message.get("id")
    method = message.get("method")
    args = message.get("args") or []
    if not isinstance(args, list):
        args = [args]

    handler = getattr(api, method, None) if isinstance(method, str) else None
    # Leading underscores are internal — `_shutdown`, `_push`, `_beat_once` —
    # and the page has no business reaching them.
    if method is None or method.startswith("_") or not callable(handler):
        bridge.send({
            "id": call_id, "ok": False, "error": "no_such_method",
            "message": f"the backend has no method {method!r}",
        })
        return

    try:
        result = handler(*args)
    except Exception as exc:  # noqa: BLE001 — one bad call must not end the app
        bridge.send({
            "id": call_id, "ok": False, "error": "bridge_error",
            "message": f"{type(exc).__name__}: {exc}",
        })
        return
    bridge.send({"id": call_id, "ok": True, "result": result})


def serve(api, stdin=None, out=None):
    """Serve requests until stdin closes, then return.

    Stdin closing is how the window says it has gone: the parent's pipe end is
    dropped when the Tauri process exits, so there is no separate shutdown
    message to miss and no orphaned backend if the window is killed outright.

    `stdin`/`out` exist for the tests. Left at None, this claims the process's
    real stdio (see `_claim_stdout`).
    """
    owns_stdio = out is None
    bridge = Bridge(_claim_stdout() if owns_stdio else out)
    api._bridge = bridge
    # _claim_stdout() must run FIRST: it is what moves ordinary prints off fd 1,
    # and anything printed between here and there would land on the bridge.
    source = stdin if stdin is not None else _claim_stdin()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS,
                            thread_name_prefix="omni-rpc") as pool:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue  # not a frame; the window never sends one
            if not isinstance(message, dict):
                continue
            pool.submit(dispatch, api, message, bridge)
    return bridge
