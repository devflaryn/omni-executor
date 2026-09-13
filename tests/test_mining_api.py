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


def test_start_cpu_spawns_one_process_with_xmr_algo(api, monkeypatch):
    _enrolled(api, monkeypatch)
    spawned = []

    def fake_popen(args, **kwargs):
        spawned.append(args)
        return FakeProc()

    monkeypatch.setattr(main.subprocess, "Popen", fake_popen)
    res = api.mining_start("cpu", 50)
    assert res["ok"] is True
    assert len(spawned) == 1
    args = spawned[0]
    assert args.count("--user") == 1
    assert f"{args[args.index('--user') + 1]}" == "tok.xmr"
    assert "rx/0" in args
    assert len(api._mining_procs) == 1


def test_start_both_spawns_two_processes_xmr_and_rvn(api, monkeypatch):
    _enrolled(api, monkeypatch)
    spawned = []

    def fake_popen(args, **kwargs):
        spawned.append(args)
        return FakeProc()

    monkeypatch.setattr(main.subprocess, "Popen", fake_popen)
    res = api.mining_start("both", 50)
    assert res["ok"] is True
    assert res["procs"] == 2
    assert len(spawned) == 2
    users = {args[args.index("--user") + 1] for args in spawned}
    algos = {args[args.index("--algo") + 1] for args in spawned}
    assert users == {"tok.xmr", "tok.rvn"}
    assert algos == {"rx/0", "kawpow"}
    assert len(api._mining_procs) == 2


def test_stop_terminates_every_process_and_clears_list(api):
    p1, p2 = FakeProc(), FakeProc()
    api._mining_procs = [p1, p2]
    res = api.mining_stop()
    assert res["ok"] is True
    assert res["stopped"] == 2
    assert api._mining_procs == []
    assert "terminate" in p1.calls
    assert "terminate" in p2.calls
