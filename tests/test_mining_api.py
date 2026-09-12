import types
import pytest
import main


class FakeBridge:
    def __init__(self): self.events = []
    def push(self, event, payload=None): self.events.append((event, payload))


@pytest.fixture
def api(monkeypatch):
    a = main.Api.__new__(main.Api)   # bypass heavy __init__
    a._bridge = FakeBridge()
    a._mining = None
    a._mining_proc = None
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
    monkeypatch.setattr(main, "_run_elevated", lambda args: captured.setdefault("args", args) or {"ok": True})
    monkeypatch.setattr(main.miner, "miner_dir", lambda: __import__("pathlib").Path(r"C:\x\miner"))
    res = api.mining_add_defender_exclusion()
    assert res["ok"] is True
    assert any("Add-MpPreference" in str(x) for x in captured["args"])
