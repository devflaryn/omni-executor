"""The bridge between the Tauri window and this backend.

`rpc.serve` replaced pywebview's `js_api`: the same `Api` methods, reached over
a pipe instead of an in-process JS binding. What the transport added, and what
these tests hold it to:

  * a call cannot take the process down, whatever the method does;
  * a SLOW call cannot block a fast one queued behind it (pywebview gave every
    JS call its own thread and the Api methods were written expecting that --
    `engine_start` blocks for tens of seconds while the Network tab polls);
  * nothing but frames may reach the write channel. `main.py`, `bootstrap.py`
    and half of what they import `print()` freely, and one stray line on fd 1
    corrupts a frame and takes the window's bridge down with it;
  * the page cannot reach the internals -- `_shutdown`, `_beat_once`, `_push`;
  * stdin closing ends the loop, because that is the only shutdown signal there
    is: the shell's pipe end is dropped when it exits, cleanly or not.
"""

import io
import json
import threading
import time

import pytest

import rpc


class FakeApi:
    """Stands in for main.Api: a few methods with the shapes that matter."""

    def __init__(self):
        self._bridge = None
        self.started = threading.Event()

    def get_platform(self):
        return "win32"

    def echo(self, *args):
        return list(args)

    def engine_start(self, name):
        # The slow call. Releases only when the test says so.
        self.started.set()
        self.release.wait(5)
        return {"ok": True, "name": name}

    def boom(self):
        raise ValueError("engine said no")

    def unserializable(self):
        return {"fn": lambda: None}

    def push_one(self):
        self._bridge.push("engine-progress", {"line": "working"})
        return True

    def _shutdown(self):
        raise AssertionError("the page must never reach an internal method")


class Channel(io.StringIO):
    """A write end that records frames and can be read while still open."""

    def __init__(self):
        super().__init__()
        self.lines = []
        self._lock = threading.Lock()

    def write(self, text):
        with self._lock:
            if text.strip():
                self.lines.append(text.strip())
        return len(text)

    def flush(self):
        pass

    def frames(self):
        with self._lock:
            return [json.loads(line) for line in self.lines]


def run(requests, api=None, wait_for=None, timeout=5):
    """Serve `requests` (a list of dicts) and return the frames written back.

    Returns (frames, api). `wait_for` is a predicate on the frame list, for the
    tests that must not close stdin until something has happened.
    """
    api = api or FakeApi()
    out = Channel()
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    thread = threading.Thread(target=rpc.serve, args=(api,), kwargs={
        "stdin": stdin, "out": out})
    thread.start()
    if wait_for:
        deadline = time.time() + timeout
        while time.time() < deadline and not wait_for(out.frames()):
            time.sleep(0.02)
    thread.join(timeout)
    assert not thread.is_alive(), "serve() did not return after stdin closed"
    return out.frames(), api


def replies(frames):
    return {f["id"]: f for f in frames if "id" in f}


# --------------------------------------------------------------- dispatch

def test_a_call_reaches_the_method_and_comes_back():
    frames, _ = run([{"id": 1, "method": "get_platform", "args": []}])
    assert replies(frames)[1] == {"id": 1, "ok": True, "result": "win32"}


def test_arguments_arrive_positionally():
    frames, _ = run([{"id": 1, "method": "echo", "args": ["a", 2, None]}])
    assert replies(frames)[1]["result"] == ["a", 2, None]


def test_a_missing_args_key_is_a_call_with_no_arguments():
    """The frontend omits `args` for a no-argument call, and Rust forwards the
    frame as it found it."""
    frames, _ = run([{"id": 1, "method": "get_platform"}])
    assert replies(frames)[1]["ok"] is True


def test_an_unknown_method_is_refused_by_name():
    frames, _ = run([{"id": 1, "method": "nope", "args": []}])
    reply = replies(frames)[1]
    assert reply["ok"] is False
    assert reply["error"] == "no_such_method"
    assert "nope" in reply["message"]


def test_internal_methods_are_unreachable_from_the_page():
    """A leading underscore is the whole access rule. FakeApi._shutdown raises
    if it is ever called, so reaching it fails the test twice over."""
    frames, _ = run([{"id": 1, "method": "_shutdown", "args": []}])
    assert replies(frames)[1]["error"] == "no_such_method"


def test_a_raising_method_answers_instead_of_hanging_the_caller():
    """The window is awaiting this id. An exception that killed the worker
    silently would leave that promise pending forever."""
    frames, _ = run([{"id": 7, "method": "boom", "args": []}])
    reply = replies(frames)[7]
    assert reply["ok"] is False
    assert reply["error"] == "bridge_error"
    assert "engine said no" in reply["message"]


def test_a_result_json_cannot_express_still_answers():
    frames, _ = run([{"id": 3, "method": "unserializable", "args": []}])
    reply = replies(frames)[3]
    # default=str makes most things serialisable rather than failing; either
    # outcome is acceptable, an unanswered call is not.
    assert reply["id"] == 3 and "ok" in reply


# --------------------------------------------------------------- framing

def test_junk_on_the_wire_is_ignored_not_fatal():
    frames, _ = run([{"id": 1, "method": "get_platform", "args": []}])
    assert replies(frames)[1]["ok"] is True

    api = FakeApi()
    out = Channel()
    stdin = io.StringIO('not json\n\n[]\n"a string"\n'
                        + json.dumps({"id": 9, "method": "get_platform"}) + "\n")
    rpc.serve(api, stdin=stdin, out=out)
    assert replies(out.frames())[9]["ok"] is True


def test_a_push_is_a_frame_with_no_id():
    """This is the engine event bus: Api._push -> here -> a Tauri event ->
    window.omniEvent. A push must never carry an id, or the window would try
    to resolve a call nobody made."""
    frames, _ = run([{"id": 1, "method": "push_one", "args": []}])
    pushes = [f for f in frames if "id" not in f]
    assert pushes == [{"event": "engine-progress", "payload": {"line": "working"}}]


def test_serve_hands_the_api_its_bridge():
    _, api = run([{"id": 1, "method": "get_platform", "args": []}])
    assert api._bridge is not None


# ------------------------------------------------------------ concurrency

def test_a_slow_call_does_not_block_a_fast_one():
    """The reason every request gets its own worker. `engine_start` parks; the
    `get_platform` behind it must still answer, and answer FIRST."""
    api = FakeApi()
    api.release = threading.Event()

    out = Channel()
    stdin = io.StringIO(
        json.dumps({"id": 1, "method": "engine_start", "args": ["farm_alpha"]}) + "\n"
        + json.dumps({"id": 2, "method": "get_platform", "args": []}) + "\n")
    thread = threading.Thread(target=rpc.serve, args=(api,),
                              kwargs={"stdin": stdin, "out": out})
    thread.start()

    deadline = time.time() + 5
    while time.time() < deadline and 2 not in replies(out.frames()):
        time.sleep(0.02)
    answered = replies(out.frames())
    assert 2 in answered, "the fast call never came back while the slow one ran"
    assert 1 not in answered, "the slow call answered early; it is not slow"

    api.release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert replies(out.frames())[1]["result"] == {"ok": True, "name": "farm_alpha"}


def test_replies_may_arrive_out_of_order():
    """Which is exactly why frames carry an id. Rust matches on it rather than
    assuming the pipe is a queue."""
    api = FakeApi()
    api.release = threading.Event()
    api.release.set()
    frames, _ = run([{"id": i, "method": "echo", "args": [i]} for i in range(1, 21)],
                    api=api)
    answered = replies(frames)
    assert set(answered) == set(range(1, 21))
    assert all(answered[i]["result"] == [i] for i in range(1, 21))


# ------------------------------------------------------------ stdio hygiene

def test_claim_stdout_moves_ordinary_prints_off_the_channel(capfd):
    """The sharp edge this whole module is arranged around: fd 1 belongs to the
    bridge, and `print()` must land somewhere that is not it."""
    import sys

    saved_out, saved_err = sys.stdout, sys.stderr
    writer = None
    try:
        writer = rpc._claim_stdout()
        print("this must not reach the bridge")
        assert sys.stdout is sys.stderr
    finally:
        if writer is not None:
            writer.close()
        sys.stdout, sys.stderr = saved_out, saved_err

    captured = capfd.readouterr()
    assert "this must not reach the bridge" not in captured.out


def test_the_writer_survives_a_closed_pipe():
    """The window went away mid-call. Writing to a dead pipe is expected, not
    an error worth raising into an engine progress callback."""
    out = Channel()
    bridge = rpc.Bridge(out)
    out.close()

    def write(_text):
        raise ValueError("I/O operation on closed file")

    out.write = write
    bridge.send({"id": 1, "ok": True, "result": None})   # must not raise
    bridge.push("engine-progress", {"line": "x"})        # nor this
