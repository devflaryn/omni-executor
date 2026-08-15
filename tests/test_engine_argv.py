import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


def test_engine_list_argv(captured):
    api = main.Api()
    api.engine_list()
    assert ["list", "--json"] in captured


def test_login_browser_argv(captured):
    main.Api().engine_login_browser()
    assert ["login"] in captured


def test_login_token_writes_file_and_calls_login(captured, tmp_path, monkeypatch):
    # force the temp file into tmp_path so we can assert it's cleaned up
    import tempfile
    seen = {}

    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*a, **k):
        fd, path = real_mkstemp(dir=str(tmp_path))
        seen["path"] = path
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)
    res = main.Api().engine_login_token("COOKIE123")
    # the login argv used --token-file with a path
    argvs = [c for c in captured if c and c[0] == "login"]
    assert any("--token-file" in c for c in argvs)
    # temp file removed after
    assert not os.path.exists(seen["path"])


def test_login_token_rejects_empty(captured):
    res = main.Api().engine_login_token("")
    assert res["ok"] is False
    assert "token" in res["error"]
    assert captured == []  # never called the engine


def test_engine_create_is_gone():
    assert not hasattr(main.Api, "engine_create")


def test_start_with_mode_and_place(captured):
    main.Api().engine_start("alice", mode="farming", place="8737899170")
    start = next(c for c in captured if c and c[0] == "start")
    assert "alice" in start
    assert "--mode" in start and "farming" in start
    assert "--place" in start and "8737899170" in start


def test_start_bare_has_no_mode_or_place(captured):
    main.Api().engine_start("alice")
    start = next(c for c in captured if c and c[0] == "start")
    assert "--mode" not in start
    assert "--place" not in start


def test_engine_modes_from_version(monkeypatch):
    # Still a pass-through, so a newer engine can surface a mode this build has
    # never heard of -- but what an engine now advertises is the live pair, and
    # the retired names are not in it. See test_launch_modes.py.
    monkeypatch.setattr(main, "run_engine",
                        lambda *a, **k: {"ok": True,
                                         "modes": ["gaming", "farming"]})
    assert main.Api().engine_modes() == ["gaming", "farming"]


def test_engine_modes_fallback_includes_farming(monkeypatch):
    monkeypatch.setattr(main, "run_engine", lambda *a, **k: {"ok": True})  # no modes
    modes = main.Api().engine_modes()
    assert "farming" in modes


def test_engine_view_argv(captured):
    main.Api().engine_view("alice")
    view = next(c for c in captured if c and c[0] == "view")
    assert view[:2] == ["view", "alice"]
    assert "--start" in view
    assert "--json" in view  # the pid it returns is how we know a window opened


def test_websockify_machinery_removed():
    assert not hasattr(main.Api, "_ensure_proxy")
    assert not hasattr(main.Api, "open_viewer")
    assert not hasattr(main.Api, "viewer_close")


# ------------------------------------------------------- engine_prefix()

def test_windows_uses_exe(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIDROID_ENGINE", raising=False)
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main, "PROJECT_DIR", tmp_path)
    exe = tmp_path / "omnidroid.exe"
    exe.write_text("")
    assert main.engine_prefix() == [str(exe)]


def test_mac_ignores_exe_falls_to_source(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIDROID_ENGINE", raising=False)
    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setattr(main.sys, "frozen", False, raising=False)
    monkeypatch.setattr(main, "PROJECT_DIR", tmp_path)
    exe = tmp_path / "omnidroid.exe"
    exe.write_text("")
    # no extensionless binary, no sibling checkout yet -> nothing resolvable
    assert main.engine_prefix() is None
    prefix = main.engine_prefix()
    assert prefix is None or str(exe) not in prefix

    # now add the sibling source checkout -> falls to `-m omnidroid`
    sib_main = tmp_path.parent / "omnidroid" / "omnidroid" / "__main__.py"
    sib_main.parent.mkdir(parents=True, exist_ok=True)
    sib_main.write_text("")
    assert main.engine_prefix() == [main.sys.executable, "-m", "omnidroid"]


def test_mac_uses_native_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIDROID_ENGINE", raising=False)
    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setattr(main, "PROJECT_DIR", tmp_path)
    binary = tmp_path / "omnidroid"
    binary.write_text("")
    assert main.engine_prefix() == [str(binary)]


def test_source_fallback_when_no_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNIDROID_ENGINE", raising=False)
    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.setattr(main.sys, "frozen", False, raising=False)
    monkeypatch.setattr(main, "PROJECT_DIR", tmp_path)
    sib_main = tmp_path.parent / "omnidroid" / "omnidroid" / "__main__.py"
    sib_main.parent.mkdir(parents=True, exist_ok=True)
    sib_main.write_text("")
    assert main.engine_prefix() == [main.sys.executable, "-m", "omnidroid"]


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PROJECT_DIR", tmp_path)

    py_engine = tmp_path / "custom_manager.py"
    py_engine.write_text("")
    monkeypatch.setenv("OMNIDROID_ENGINE", str(py_engine))
    assert main.engine_prefix() == [main.sys.executable, str(py_engine)]

    bin_engine = tmp_path / "custom_omnidroid"
    bin_engine.write_text("")
    monkeypatch.setenv("OMNIDROID_ENGINE", str(bin_engine))
    assert main.engine_prefix() == [str(bin_engine)]
