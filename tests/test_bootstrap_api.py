import types, time
import main


def test_bootstrap_status_reports_ready(monkeypatch):
    monkeypatch.setattr(main.bootstrap, "read_manifest",
        lambda *a, **k: {"ok": True, "app": {"version": "1"},
            "artifacts": [{"name": "offset-arceus-arm", "sha256": "aa", "bytes": 1,
                           "url": "/omni/dist/blob/offset-arceus-arm", "dest": "images/arm"}]})
    monkeypatch.setattr(main.bootstrap, "installed_state",
        lambda rt: {"artifacts": {"offset-arceus-arm": {"sha256": "aa"}}, "app_version": "1"})
    monkeypatch.setattr(main.bootstrap, "configure_engine",
        lambda rt: {"qemu_ok": True, "qemu_hint": None, "images_dir": "x", "data_dir": "y"})
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: __import__("pathlib").Path("/tmp"))
    api = main.Api()
    st = api.bootstrap_status()
    assert st["ok"] and st["ready"] is True and st["qemu_ok"] is True


def test_bootstrap_start_streams_progress(monkeypatch):
    events = []
    def fake_ensure(base_url=None, progress=None):
        progress({"phase": "download", "artifact": "offset-arceus-arm", "percent": 50})
        return {"ok": True, "installed": {}, "changed": ["offset-arceus-arm"]}
    monkeypatch.setattr(main.bootstrap, "ensure_runtime", fake_ensure)
    monkeypatch.setattr(main.bootstrap, "configure_engine", lambda rt: {"qemu_ok": True, "qemu_hint": None})
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: __import__("pathlib").Path("/tmp"))
    api = main.Api()
    api._push = lambda event, payload=None: events.append((event, payload))
    api.bootstrap_start()
    time.sleep(0.3)
    kinds = [e for e, _ in events]
    assert "bootstrap-progress" in kinds and "bootstrap-done" in kinds
