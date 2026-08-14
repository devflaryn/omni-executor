import types, time
import main


def test_bootstrap_status_reports_ready(monkeypatch):
    monkeypatch.setattr(main.bootstrap, "read_manifest",
        lambda *a, **k: {"ok": True, "app": {"version": "1"},
            "artifacts": [{"name": "offset-arceus-arm", "sha256": "aa", "bytes": 1,
                           "url": "/omni/dist/blob/offset-arceus-arm", "dest": "images/arm"}]})
    monkeypatch.setattr(main.bootstrap, "installed_state",
        lambda rt: {"artifacts": {"offset-arceus-arm": {"sha256": "aa"}}, "app_version": "1"})
    monkeypatch.setattr(main.bootstrap, "engine_ready",
        lambda rt: {"qemu_ok": True, "adb_ok": True, "tools_ok": True,
                    "qemu_hint": None})

    def _configure_engine_should_not_be_called(rt):
        raise AssertionError("bootstrap_status must not call configure_engine (no I/O on a poll)")
    monkeypatch.setattr(main.bootstrap, "configure_engine", _configure_engine_should_not_be_called)

    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: __import__("pathlib").Path("/tmp"))
    api = main.Api()
    st = api.bootstrap_status()
    assert st["ok"] and st["ready"] is True and st["qemu_ok"] is True


def test_bootstrap_start_streams_progress(monkeypatch):
    events = []
    configure_calls = []
    def fake_ensure(base_url=None, progress=None):
        progress({"phase": "download", "artifact": "offset-arceus-arm", "percent": 50})
        return {"ok": True, "installed": {}, "changed": ["offset-arceus-arm"]}
    monkeypatch.setattr(main.bootstrap, "ensure_runtime", fake_ensure)
    monkeypatch.setattr(main.bootstrap, "ensure_tools",
                        lambda rt, manifest=None, progress=None: {
                            "installed": [], "reboot_required": False})
    def fake_configure_engine(rt):
        configure_calls.append(rt)
        return {"qemu_ok": True, "qemu_hint": None}
    monkeypatch.setattr(main.bootstrap, "configure_engine", fake_configure_engine)
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: __import__("pathlib").Path("/tmp"))
    api = main.Api()
    api._push = lambda event, payload=None: events.append((event, payload))
    api.bootstrap_start()
    time.sleep(0.3)
    kinds = [e for e, _ in events]
    assert "bootstrap-progress" in kinds and "bootstrap-done" in kinds
    assert len(configure_calls) == 1, "bootstrap_start must call configure_engine exactly once after ensure_runtime"


def test_setup_installs_the_host_tools_before_the_images(monkeypatch):
    """THE fresh-machine regression.

    A new PC has no QEMU and no adb. Nothing in the app installed either:
    the engine's ensure_qemu() only fires when an instance is STARTED, which
    is unreachable from the setup screen, and the setup screen refused to
    start until QEMU already existed. So the app sat on "QEMU is required"
    forever and no image was ever downloaded.

    Tools must be installed FIRST -- 4 GB of base images are useless without a
    QEMU to boot them."""
    order = []
    monkeypatch.setattr(main.bootstrap, "ensure_tools",
                        lambda rt, manifest=None, progress=None: (
                            order.append("tools"),
                            {"installed": ["qemu", "adb"], "reboot_required": False})[1])
    monkeypatch.setattr(main.bootstrap, "ensure_runtime",
                        lambda base_url=None, progress=None: (
                            order.append("images"),
                            {"ok": True, "installed": {}, "changed": []})[1])
    monkeypatch.setattr(main.bootstrap, "configure_engine",
                        lambda rt: order.append("configure"))
    monkeypatch.setattr(main.bootstrap, "runtime_dir",
                        lambda: __import__("pathlib").Path("/tmp"))
    api = main.Api()
    api._push = lambda event, payload=None: None
    api.bootstrap_start()
    time.sleep(0.3)
    assert order == ["tools", "images", "configure"]


def test_a_pending_reboot_is_reported_and_sticks(monkeypatch):
    """DISM has enabled the hypervisor feature but Windows has not restarted,
    so it is not live yet. Re-probing would say "off" and offer to enable an
    already-enabled feature forever, so the flag is sticky for the process."""
    monkeypatch.setattr(main.bootstrap, "ensure_tools",
                        lambda rt, manifest=None, progress=None: {
                            "installed": ["qemu", "whpx"], "reboot_required": True})
    monkeypatch.setattr(main.bootstrap, "ensure_runtime",
                        lambda base_url=None, progress=None: {
                            "ok": True, "installed": {}, "changed": []})
    monkeypatch.setattr(main.bootstrap, "configure_engine", lambda rt: None)
    monkeypatch.setattr(main.bootstrap, "runtime_dir",
                        lambda: __import__("pathlib").Path("/tmp"))
    events = []
    api = main.Api()
    api._push = lambda event, payload=None: events.append((event, payload))
    api.bootstrap_start()
    time.sleep(0.3)
    assert "bootstrap-reboot" in [e for e, _ in events]
    assert api._reboot_required is True


def test_status_is_not_ready_without_adb(monkeypatch):
    """adb is not optional: omnidroid/adb.py shells the bare name, so every
    guest command fails without it. It was never checked at all."""
    monkeypatch.setattr(main.bootstrap, "read_manifest",
        lambda *a, **k: {"ok": True, "app": {"version": "1"}, "artifacts": []})
    monkeypatch.setattr(main.bootstrap, "installed_state",
        lambda rt: {"artifacts": {"base-x86": {"sha256": "aa"}}, "app_version": "1"})
    monkeypatch.setattr(main.bootstrap, "engine_ready",
        lambda rt: {"qemu_ok": True, "adb_ok": False, "tools_ok": False,
                    "qemu_hint": None})
    monkeypatch.setattr(main.bootstrap, "windows_accel_status",
        lambda *a, **k: {"whpx_ok": True, "hint": None})
    monkeypatch.setattr(main.bootstrap, "runtime_dir",
        lambda: __import__("pathlib").Path("/tmp"))
    st = main.Api().bootstrap_status()
    assert st["ready"] is False
    assert st["adb_ok"] is False
