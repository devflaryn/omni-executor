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
    monkeypatch.setattr(main, "run_engine",
                        lambda *a, **k: {"ok": True,
                                         "modes": ["playable", "farming"]})
    assert main.Api().engine_modes() == ["playable", "farming"]


def test_engine_modes_fallback_includes_farming(monkeypatch):
    monkeypatch.setattr(main, "run_engine", lambda *a, **k: {"ok": True})  # no modes
    modes = main.Api().engine_modes()
    assert "farming" in modes


def test_engine_view_argv(captured):
    main.Api().engine_view("alice")
    assert ["view", "alice", "--start"] in captured


def test_websockify_machinery_removed():
    assert not hasattr(main.Api, "_ensure_proxy")
    assert not hasattr(main.Api, "open_viewer")
    assert not hasattr(main.Api, "viewer_close")
