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
