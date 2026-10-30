"""The executor's half of native omnidroid: what it asks the pool for, and what
it keeps on this machine while an instance runs.

The pool itself is replaced by a double that records every tool call, so
these run anywhere, with no omnidroid and no Android."""
import io
import json

import pytest

import bootstrap
import droid
import main


class FakePool:
    """droid.Pool's surface, recording calls."""

    def __init__(self, instances=None, fail=None):
        self.calls = []
        self.port = 7457
        self._instances = instances or {}
        self._fail = fail or {}

    def _alive(self):
        return True

    def owned(self):
        return False

    def shutdown(self):
        pass

    def instances(self):
        return dict(self._instances)

    def call(self, tool, args=None, timeout=60):
        self.calls.append((tool, dict(args or {})))
        if tool in self._fail:
            raise droid.DroidError(self._fail[tool])
        if tool == "start_instance":
            return {"instances": [7], "note": "on D1"}
        return {}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "SETTINGS_FILE", tmp_path / "settings.json")
    apk = tmp_path / "OmniExecutor.apk"
    apk.write_bytes(b"apk")
    monkeypatch.setattr(droid, "apk_path", lambda: apk)
    import robloxaccounts
    robloxaccounts.save_account(tmp_path, "Alice_1", "_|WARNING:-cookie-value", user_id=1)
    a = main.Api()
    a._pool = FakePool()
    a._push = lambda *_a, **_k: None
    a._beat_now = lambda *_a, **_k: None
    monkeypatch.setattr(main.Api, "_exec_base", lambda self: "http://127.0.0.1:9")
    # The autoexec push is a network call; it must never be what a test waits on.
    import autoexec
    monkeypatch.setattr(autoexec, "push_autoexec", lambda *a, **k: 0)
    return a


def test_a_launch_runs_the_executor_apk_with_the_account_signed_in(api, tmp_path):
    res = api.engine_start("Alice_1", "8737899170")
    assert res["ok"] and res["instance_id"] == 7
    tool, args = api._pool.calls[-1]
    assert tool == "start_instance"
    assert args["apk"] == str(tmp_path / "OmniExecutor.apk")
    assert args["count"] == 1
    cookie = tmp_path / "launch-cookies" / "Alice_1.txt"
    assert f'--cookie "{cookie}"' in args["options"]
    assert "--place 8737899170" in args["options"]
    # The value goes in a FILE, never on the command line.
    assert "cookie-value" not in args["options"]
    assert cookie.read_text(encoding="utf-8") == "_|WARNING:-cookie-value"
    assert api._instances["Alice_1"] == 7


def test_there_is_no_mode_to_pick(api):
    """Launch takes a place and nothing else: no Gaming/Farming, no GPU."""
    import inspect
    params = list(inspect.signature(main.Api.engine_start).parameters)
    assert params == ["self", "name", "place"]


def test_no_place_lands_on_home(api):
    api.engine_start("Alice_1")
    assert "--place" not in api._pool.calls[-1][1]["options"]


def test_a_place_that_is_not_a_number_is_refused_before_omnidroid(api):
    res = api.engine_start("Alice_1", "../etc")
    assert not res["ok"] and res["error"] == "bad_place"
    assert api._pool.calls == []


def test_an_account_with_no_sign_in_is_refused(api):
    res = api.engine_start("Nobody")
    assert not res["ok"] and res["error"] == "no_account"


def test_a_refused_start_leaves_no_cookie_behind(api, tmp_path):
    api._pool = FakePool(fail={"start_instance": "max_devices reached"})
    res = api.engine_start("Alice_1")
    assert not res["ok"] and "max_devices" in res["message"]
    assert not (tmp_path / "launch-cookies" / "Alice_1.txt").exists()


def test_stop_closes_the_instance_and_forgets_the_cookie(api, tmp_path):
    api.engine_start("Alice_1")
    res = api.engine_stop("Alice_1")
    assert res["ok"]
    assert api._pool.calls[-1] == ("stop_instance", {"instance_id": 7})
    assert not (tmp_path / "launch-cookies" / "Alice_1.txt").exists()
    assert "Alice_1" not in api._instances


def test_stopping_an_instance_that_already_ended_is_a_stop(api):
    api.engine_start("Alice_1")
    api._pool._fail = {"stop_instance": "#7 ended already"}
    assert api.engine_stop("Alice_1")["ok"]


def test_the_list_says_what_each_instance_is_doing(api):
    api.engine_start("Alice_1")
    api._pool._instances = {7: {"instance_id": 7, "state": "installing", "up_s": 3}}
    row = api.engine_list()["accounts"][0]
    assert row["name"] == "Alice_1" and row["running"] is True
    assert row["state"] == "installing" and row["instance_id"] == 7
    api._pool._instances = {7: {"instance_id": 7, "state": "ended: native crash: SIGSEGV"}}
    row = api.engine_list()["accounts"][0]
    assert row["running"] is False


def test_the_list_never_carries_a_cookie(api):
    assert "cookie" not in json.dumps(api.engine_list()).lower()


def test_remove_forgets_the_account(api):
    assert api.engine_remove("Alice_1")["existed"] is True
    assert api.engine_list()["accounts"] == []


def test_settings_from_the_qemu_engine_are_dropped_once(api):
    main.SETTINGS_FILE.write_text(json.dumps({
        "launch": {"mode": "farming", "gpu": "auto", "place": "1"},
        "autoWarm": True, "pool": {"warmedFor": {}}, "farming": {"members": []}}))
    s = api.get_settings()
    assert s["launch"] == {"place": "1"}
    assert not {"autoWarm", "pool", "farming"} & set(s)
    on_disk = json.loads(main.SETTINGS_FILE.read_text())
    assert "mode" not in on_disk["launch"] and "autoWarm" not in on_disk


def test_configure_engine_points_the_store_at_the_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNI_DATA_DIR", raising=False)
    rep = bootstrap.configure_engine(tmp_path)
    import os
    assert os.environ["OMNI_DATA_DIR"] == str(tmp_path)
    assert rep["tools_ok"] is False


def test_engine_ready_needs_the_bundle_and_the_apk(tmp_path):
    exe = "omnidroid.exe" if main.sys.platform == "win32" else "omnidroid"
    (tmp_path / "omnidroid" / "sysroot").mkdir(parents=True)
    (tmp_path / "omnidroid" / exe).write_bytes(b"")
    assert bootstrap.engine_ready(tmp_path)["omnidroid_ok"] is True
    assert bootstrap.engine_ready(tmp_path)["tools_ok"] is False
    (tmp_path / "apk").mkdir()
    (tmp_path / "apk" / "OmniExecutor.apk").write_bytes(b"")
    assert bootstrap.engine_ready(tmp_path)["tools_ok"] is True


def test_configure_engine_on_launch_never_raises(monkeypatch):
    def boom(rt):
        raise OSError("disk gone")
    monkeypatch.setattr(main.bootstrap, "configure_engine", boom)
    main._configure_engine_on_launch()


# ------------------------------------------------------------ the transport

def _pool_answering(monkeypatch, answer):
    pool = droid.Pool(port=1)
    pool._initialized = True
    monkeypatch.setattr(droid.Pool, "ensure", lambda self: True)
    monkeypatch.setattr(droid.Pool, "_post", lambda self, msg, timeout: answer)
    return pool


def test_a_tool_result_is_the_json_in_its_text(monkeypatch):
    pool = _pool_answering(monkeypatch, {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": '{"instances": [3]}'}], "isError": False}})
    assert pool.call("start_instance", {"apk": "x"}) == {"instances": [3]}


def test_a_tool_refusal_is_an_error_with_its_message(monkeypatch):
    pool = _pool_answering(monkeypatch, {"jsonrpc": "2.0", "id": 1,
                                         "error": {"code": -32000, "message": "#3 ended already"}})
    with pytest.raises(droid.DroidError, match="ended already"):
        pool.call("stop_instance", {"instance_id": 3})


def test_states_before_the_end_are_live():
    assert all(droid.is_live(s) for s in ("waiting", "installing", "starting", "running", "restarting"))
    assert not droid.is_live("ended: closed")
    assert not droid.is_live(None)


def test_the_serve_process_holds_a_pipe_for_stdin(monkeypatch, tmp_path):
    """`omnidroid serve` lives until its stdin closes: the pool ending with the
    app depends on this process holding that pipe."""
    seen = {}

    class FakeProc:
        def __init__(self, argv, **kw):
            seen.update(kw, argv=argv)
            self.stdin = io.StringIO()

    monkeypatch.setattr(droid, "bundle_dir", lambda: tmp_path)
    monkeypatch.setattr(droid.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(droid.Pool, "log_path", staticmethod(lambda: tmp_path / "serve.log"))
    droid.Pool(port=7457)._spawn()
    assert seen["stdin"] == droid.subprocess.PIPE
    assert seen["argv"][1:] == ["serve", "--port", "7457"]


def test_the_pool_never_sees_this_users_own_plugins(monkeypatch, tmp_path):
    """A dev install's user-wide `roblox.link` (a `cargo run`) would shadow the
    bundle's prebuilt plugin; the pool gets a plugin folder of its own."""
    seen = {}

    class FakeProc:
        def __init__(self, argv, **kw):
            seen.update(kw)
            self.stdin = io.StringIO()

    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_PLUGINS", "/somewhere/else")
    monkeypatch.setattr(droid, "bundle_dir", lambda: tmp_path)
    monkeypatch.setattr(droid.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(droid.Pool, "log_path", staticmethod(lambda: tmp_path / "serve.log"))
    droid.Pool(port=7457)._spawn()
    assert seen["env"]["OMNI_PLUGINS"] == str(bootstrap.runtime_dir() / "plugins")
    assert (bootstrap.runtime_dir() / "plugins").is_dir()


def test_the_pool_does_not_inherit_the_account_stores_data_dir(monkeypatch, tmp_path):
    """To omnidroid OMNI_DATA_DIR keeps ONE guest /data between runs; a
    `stop` left there by the first device ended every later one at boot."""
    seen = {}

    class FakeProc:
        def __init__(self, argv, **kw):
            seen.update(kw)
            self.stdin = io.StringIO()

    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(droid, "bundle_dir", lambda: tmp_path)
    monkeypatch.setattr(droid.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(droid.Pool, "log_path", staticmethod(lambda: tmp_path / "serve.log"))
    droid.Pool(port=7457)._spawn()
    assert "OMNI_DATA_DIR" not in seen["env"]
    import os
    assert os.environ["OMNI_DATA_DIR"] == str(tmp_path)   # the app's own is untouched


def test_progress_that_prints_does_not_recurse():
    """The autoexec push prints into a sink that forwards lines as progress;
    a progress handler that prints itself must not loop back into it."""
    import contextlib
    seen = []

    def say(line):
        seen.append(line)
        print("echo:", line)          # lands in the sink while it is busy

    with contextlib.redirect_stdout(main._LineSink(say)):
        print("autoexec: pushed 2 scripts")
    assert seen == ["autoexec: pushed 2 scripts"]
