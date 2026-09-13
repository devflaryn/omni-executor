import types
import pytest
import main


class FakeBridge:
    def __init__(self): self.events = []
    def push(self, event, payload=None): self.events.append((event, payload))


class FakeProc:
    """A Popen stand-in: no output, and terminate/wait/kill are recorded so a
    test can assert every spawned process was actually asked to stop."""
    def __init__(self):
        self.stdout = iter(())
        self.calls = []
        self._code = 0

    def terminate(self):
        self.calls.append("terminate")

    def wait(self, timeout=None):
        self.calls.append("wait")
        return self._code

    def kill(self):
        self.calls.append("kill")


@pytest.fixture(autouse=True)
def mining_file(tmp_path, monkeypatch):
    """Every test in this file that persists an enrollment must write under
    tmp_path, never into the real user config dir."""
    monkeypatch.setattr(main, "MINING_FILE", tmp_path / "mining.json")


@pytest.fixture
def api(monkeypatch):
    a = main.Api.__new__(main.Api)   # bypass heavy __init__
    a._bridge = FakeBridge()
    a._mining = None
    a._mining_procs = []
    return a


def test_enroll_downloads_and_stores(api, monkeypatch):
    monkeypatch.setattr(main.cloud, "mining_enroll",
                        lambda: {"minerToken": "T", "stratumHost": "h", "stratumPort": 3333,
                                 "algos": {"cpu": "rx/0", "gpu": "kawpow"}})
    called = {}
    monkeypatch.setattr(main.miner, "install", lambda progress=None: called.setdefault("install", True) or {"ok": True})
    res = api.mining_enroll()
    assert res["ok"] is True
    assert called.get("install") is True
    # token cached on the instance for mining_start
    assert api._mining and api._mining.get("minerToken") == "T"


def test_enroll_persists_so_a_restart_recovers(api, monkeypatch):
    """CRITICAL fix: is_installed() survives a restart on disk, so the
    enrollment must too, or a restarted app can never Start again."""
    monkeypatch.setattr(main.cloud, "mining_enroll",
                        lambda: {"minerToken": "T2", "stratumHost": "h", "stratumPort": 3333,
                                 "algos": {}})
    monkeypatch.setattr(main.miner, "install", lambda progress=None: {"ok": True})
    res = api.mining_enroll()
    assert res["ok"] is True
    # Simulate a fresh process: nothing but the file on disk.
    restored = main._load_mining()
    assert restored is not None
    assert restored["minerToken"] == "T2"


def test_save_and_load_mining_round_trip():
    info = {"minerToken": "abc", "stratumHost": "pool", "stratumPort": 1234, "algos": {"cpu": "rx/0"}}
    main._save_mining(info)
    assert main._load_mining() == info


def test_load_mining_missing_file_returns_none():
    assert main._load_mining() is None


def test_start_requires_enroll(api):
    res = api.mining_start("cpu", 50)
    assert res["ok"] is False
    assert res["error"] == "not_enrolled"


def test_defender_exclusion_builds_elevated_command(api, monkeypatch):
    captured = {}

    def fake_run_elevated(inner):
        captured["inner"] = inner
        return {"ok": True}

    monkeypatch.setattr(main, "_run_elevated", fake_run_elevated)
    monkeypatch.setattr(main.miner, "miner_dir", lambda: __import__("pathlib").Path(r"C:\x\miner"))
    res = api.mining_add_defender_exclusion()
    assert res["ok"] is True
    assert "Add-MpPreference" in captured["inner"]
    assert r"C:\x\miner" in captured["inner"]


class _NoStartThread:
    """Stands in for threading.Thread so the reader never actually runs — a
    FakeProc's empty stdout + immediate wait() would otherwise race the
    reader thread's `_mining_procs.remove(...)` against the assertions made
    right after `mining_start` returns."""
    def __init__(self, *a, **k): pass
    def start(self): pass


def _enrolled(api, monkeypatch):
    api._mining = {"minerToken": "tok", "stratumHost": "h", "stratumPort": 3333}
    monkeypatch.setattr(main.miner, "is_installed", lambda: True)
    monkeypatch.setattr(main.miner, "binary_path", lambda: __import__("pathlib").Path("xmrig.exe"))
    monkeypatch.setattr(main, "threading", types.SimpleNamespace(Thread=_NoStartThread))


def test_start_gpu_spawns_one_process_with_kawpow(api, monkeypatch):
    _enrolled(api, monkeypatch)
    spawned = []
    kwargs_seen = []

    def fake_popen(args, **kwargs):
        spawned.append(args)
        kwargs_seen.append(kwargs)
        return FakeProc()

    monkeypatch.setattr(main.subprocess, "Popen", fake_popen)
    res = api.mining_start("gpu", 77)
    assert res["ok"] is True
    assert len(spawned) == 1
    args = spawned[0]
    assert args.count("--user") == 1
    assert f"{args[args.index('--user') + 1]}" == "tok.rvn"
    assert "kawpow" in args
    # --cpu-max-threads-hint is CPU-only; the GPU process must not carry it
    assert "--cpu-max-threads-hint" not in args
    assert len(api._mining_procs) == 1
    # the miner must never inherit the RPC bridge's stdin
    assert kwargs_seen[0]["stdin"] == main.subprocess.DEVNULL
    assert kwargs_seen[0]["encoding"] == "utf-8"
    assert kwargs_seen[0]["errors"] == "replace"


def test_cpu_and_both_rejected_during_beta(api, monkeypatch):
    """Beta is GPU-only: cpu/both are gated off with a clear error and spawn
    nothing, even for an enrolled+installed account."""
    _enrolled(api, monkeypatch)
    spawned = []
    monkeypatch.setattr(main.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or FakeProc())
    for mode in ("cpu", "both"):
        res = api.mining_start(mode, 50)
        assert res["ok"] is False
        assert res["error"] == "gpu_only_beta"
    assert spawned == []
    assert api._mining_procs == []


def test_stop_terminates_every_process_and_clears_list(api):
    p1, p2 = FakeProc(), FakeProc()
    api._mining_procs = [p1, p2]
    res = api.mining_stop()
    assert res["ok"] is True
    assert res["stopped"] == 2
    assert api._mining_procs == []
    assert "terminate" in p1.calls
    assert "terminate" in p2.calls


def test_status_local_facts_beat_remote_and_enrolled_is_ored(api, monkeypatch):
    api._mining = {"minerToken": "tok"}
    monkeypatch.setattr(main.miner, "is_installed", lambda: True)
    # Server reports stale/contradicting values under the same keys.
    monkeypatch.setattr(main.cloud, "mining_status",
                        lambda: {"installed": False, "running": True, "enrolled": False,
                                 "credits": 5})
    res = api.mining_status()
    assert res["ok"] is True
    assert res["installed"] is True     # local wins
    assert res["running"] is False      # local wins (no procs on this instance)
    assert res["enrolled"] is True      # OR'd: local enrollment still counts
    assert res["credits"] == 5          # remote-only fields pass through


def test_status_offline_still_reports_enrolled_from_disk(api, monkeypatch):
    api._mining = {"minerToken": "tok"}
    monkeypatch.setattr(main.miner, "is_installed", lambda: False)

    def boom():
        raise main.cloud.CloudError("offline")
    monkeypatch.setattr(main.cloud, "mining_status", boom)
    res = api.mining_status()
    assert res["ok"] is True
    assert res["enrolled"] is True
