import pathlib
import time

import main


def _ready_engine(rt):
    return {"omnidroid_ok": True, "apk_ok": True, "tools_ok": True}


def test_bootstrap_status_reports_ready(monkeypatch):
    monkeypatch.setattr(main.bootstrap, "read_manifest",
        lambda *a, **k: {"ok": True, "app": {"version": "1"},
            "artifacts": [{"name": "omnidroid-win", "sha256": "aa", "bytes": 1,
                           "url": "/omni/dist/blob/omnidroid-win", "dest": "omnidroid"}]})
    monkeypatch.setattr(main.bootstrap, "installed_state",
        lambda rt: {"artifacts": {"omnidroid-win": {"sha256": "aa"}}, "app_version": "1"})
    monkeypatch.setattr(main.bootstrap, "engine_ready", _ready_engine)

    def _configure_engine_should_not_be_called(rt):
        raise AssertionError("bootstrap_status must not call configure_engine (no I/O on a poll)")
    monkeypatch.setattr(main.bootstrap, "configure_engine", _configure_engine_should_not_be_called)
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: pathlib.Path("/tmp"))
    st = main.Api().bootstrap_status()
    assert st["ok"] and st["ready"] is True and st["omnidroid_ok"] is True


def test_a_receipt_without_the_files_is_not_ready(monkeypatch):
    """installed.json says the bundle is there, the disk says it is not (it
    was deleted by hand): that is not an engine, so setup runs again."""
    monkeypatch.setattr(main.bootstrap, "read_manifest",
        lambda *a, **k: {"ok": True, "artifacts": []})
    monkeypatch.setattr(main.bootstrap, "installed_state", lambda rt: {"artifacts": {}})
    monkeypatch.setattr(main.bootstrap, "engine_ready",
                        lambda rt: {"omnidroid_ok": False, "apk_ok": True, "tools_ok": False})
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: pathlib.Path("/tmp"))
    monkeypatch.setattr(main.sys, "frozen", True, raising=False)
    assert main.Api().bootstrap_status()["ready"] is False


def test_bootstrap_start_streams_progress(monkeypatch):
    events = []
    configure_calls = []

    def fake_ensure(base_url=None, progress=None):
        progress({"phase": "download", "artifact": "omnidroid-win", "percent": 50})
        return {"ok": True, "installed": {}, "changed": ["omnidroid-win"]}
    monkeypatch.setattr(main.bootstrap, "ensure_runtime", fake_ensure)

    def fake_configure_engine(rt):
        configure_calls.append(rt)
        return {}
    monkeypatch.setattr(main.bootstrap, "configure_engine", fake_configure_engine)
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: pathlib.Path("/tmp"))
    api = main.Api()
    api._push = lambda event, payload=None: events.append((event, payload))
    api.bootstrap_start()
    time.sleep(0.3)
    kinds = [e for e, _ in events]
    assert "bootstrap-progress" in kinds and "bootstrap-done" in kinds
    assert len(configure_calls) == 1, "bootstrap_start must call configure_engine exactly once after ensure_runtime"


def test_the_runtime_comes_from_its_own_channel():
    """Clients up to 1.0.44 read `stable`, and treat every hashed artifact in
    it as something to download. The omnidroid bundle must never be one of
    those, and this app must never be handed their base images."""
    assert main.Api()._dist_channel() == main.bootstrap.NATIVE_CHANNEL == "native"


def test_an_unpacked_bundle_puts_each_file_on_a_new_inode(tmp_path, monkeypatch):
    """A Mach-O rewritten in place keeps its vnode, and Apple Silicon SIGKILLs
    it at exec ("Code Signature Invalid"): an update must unlink first."""
    import os
    import zipfile
    import bootstrap
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    dest = tmp_path / "omnidroid"
    dest.mkdir()
    (dest / "omni-linux-run").write_bytes(b"old build")
    os.link(dest / "omni-linux-run", tmp_path / "same-inode")
    z = tmp_path / "bundle.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("omni-linux-run", b"new build")
    with zipfile.ZipFile(z) as zf:
        bootstrap._safe_extract_zip(zf, dest)
    assert (dest / "omni-linux-run").read_bytes() == b"new build"
    # The old inode was left alone, so the new bytes are a new file.
    assert (tmp_path / "same-inode").read_bytes() == b"old build"
