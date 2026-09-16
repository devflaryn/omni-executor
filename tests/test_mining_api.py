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
    a._hashrate = {"cpu": 0.0, "gpu": 0.0}
    a._running_kinds = set()
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


def test_parse_hashrate_from_xmrig_speed_line():
    assert main._parse_hashrate("[t] miner speed 10s/60s/15m 14.62 12.76 14.17 MH/s max 15 MH/s") == 14620000.0
    assert main._parse_hashrate("[t] miner speed 10s/60s/15m 850 800 820 KH/s max 900 KH/s") == 850000.0
    assert main._parse_hashrate("[t] miner speed 10s/60s/15m n/a n/a n/a H/s max n/a H/s") is None
    assert main._parse_hashrate("[t] net new job from pool diff 1073M") is None


def test_status_reports_live_hashrate_while_running(api, monkeypatch):
    monkeypatch.setattr(main.miner, "is_installed", lambda: True)
    monkeypatch.setattr(main.cloud, "mining_status", lambda: {"enrolled": True, "creditedMicros": 0, "hashrate": 0})
    api._mining = {"minerToken": "t", "stratumHost": "h", "stratumPort": 3333}
    api._hashrate = {"cpu": 0.0, "gpu": 14620000.0}
    api._running_kinds = {"gpu"}
    api._mining_procs = [object()]           # running
    assert api.mining_status()["hashrate"] == 14620000.0
    assert api.mining_status()["estHashrate"] == {"cpu": 0.0, "gpu": 14620000.0}
    api._running_kinds = set()                # nothing running -> total 0
    assert api.mining_status()["hashrate"] == 0
    # last-known per-kind reading persists for an idle-mode estimate
    assert api.mining_status()["estHashrate"]["gpu"] == 14620000.0


def test_status_reports_combined_hashrate_for_both_kinds_running(api, monkeypatch):
    monkeypatch.setattr(main.miner, "is_installed", lambda: True)
    monkeypatch.setattr(main.cloud, "mining_status", lambda: {"enrolled": True, "creditedMicros": 0})
    api._mining = {"minerToken": "t", "stratumHost": "h", "stratumPort": 3333}
    api._hashrate = {"cpu": 1000.0, "gpu": 14620000.0}
    api._running_kinds = {"cpu", "gpu"}
    api._mining_procs = [object(), object()]
    assert api.mining_status()["hashrate"] == 1000.0 + 14620000.0


def test_status_passes_through_remote_rates_and_coins(api, monkeypatch):
    monkeypatch.setattr(main.miner, "is_installed", lambda: True)
    monkeypatch.setattr(main.cloud, "mining_status",
                        lambda: {"enrolled": True, "creditedMicros": 0,
                                 "rates": {"rvn": 1.5, "xmr": 3.0},
                                 "coins": {"rvn": True, "xmr": False}})
    api._mining = {"minerToken": "t", "stratumHost": "h", "stratumPort": 3333}
    res = api.mining_status()
    assert res["rates"] == {"rvn": 1.5, "xmr": 3.0}
    assert res["coins"] == {"rvn": True, "xmr": False}


def test_status_enrolled_reflects_local_token_not_server(api, monkeypatch):
    """enrolled must be LOCAL-only: a lingering server MinerSession without a
    local token would otherwise flip the UI to Start, which then fails
    not_enrolled ("Could not start mining"). Guards that regression."""
    monkeypatch.setattr(main.miner, "is_installed", lambda: True)
    monkeypatch.setattr(main.cloud, "mining_status",
                        lambda: {"enrolled": True, "creditedMicros": 0})
    api._mining = None
    assert api.mining_status()["enrolled"] is False   # server session must NOT flip it
    api._mining = {"minerToken": "t", "stratumHost": "h", "stratumPort": 3333}
    assert api.mining_status()["enrolled"] is True


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


def test_start_cpu_spawns_randomx_with_no_gpu_disable_flags(api, monkeypatch):
    """The shipped xmrig build REJECTS --no-cuda ('unknown option'), and
    RandomX runs on CPU by default — the cpu kind must carry neither
    --no-cuda nor --no-opencl."""
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
    assert "--no-cuda" not in args
    assert "--no-opencl" not in args
    assert "--cpu-max-threads-hint" in args
    assert len(api._mining_procs) == 1
    assert api._running_kinds == {"cpu"}


def test_start_both_spawns_one_process_per_kind(api, monkeypatch):
    _enrolled(api, monkeypatch)
    spawned = []
    monkeypatch.setattr(main.subprocess, "Popen",
                        lambda args, **k: spawned.append(args) or FakeProc())
    res = api.mining_start("both", 50)
    assert res["ok"] is True
    assert res["procs"] == 2
    assert len(spawned) == 2
    assert len(api._mining_procs) == 2
    assert api._running_kinds == {"cpu", "gpu"}
    users = {args[args.index("--user") + 1] for args in spawned}
    assert users == {"tok.xmr", "tok.rvn"}


def test_start_cpu_eco_lowers_threads_and_priority(api, monkeypatch):
    """Eco caps the CPU miner at a quarter of the cores AND drops it below
    normal priority, so a game/desktop always wins the scheduler while it
    trickle-mines on the slack."""
    _enrolled(api, monkeypatch)
    spawned = []
    monkeypatch.setattr(main.subprocess, "Popen",
                        lambda args, **k: spawned.append(args) or FakeProc())
    res = api.mining_start("cpu", 50, eco=True)
    assert res["ok"] is True
    args = spawned[0]
    assert args[args.index("--cpu-max-threads-hint") + 1] == "25"
    assert args.count("--cpu-priority") == 1
    assert args[args.index("--cpu-priority") + 1] == "1"
    # pause-on-active is a GPU-eco lever; the CPU keeps mining while the user
    # works, so it must NOT carry it.
    assert "--pause-on-active" not in args


def test_start_gpu_eco_pauses_on_activity(api, monkeypatch):
    """A 3D game needs the whole card, so the eco GPU miner yields it while
    the user is active (there is no OpenCL intensity knob to throttle with)."""
    _enrolled(api, monkeypatch)
    spawned = []
    monkeypatch.setattr(main.subprocess, "Popen",
                        lambda args, **k: spawned.append(args) or FakeProc())
    res = api.mining_start("gpu", 50, eco=True)
    assert res["ok"] is True
    args = spawned[0]
    assert args.count("--pause-on-active") == 1
    assert int(args[args.index("--pause-on-active") + 1]) > 0
    # GPU has no cpu-priority/threads knobs to set.
    assert "--cpu-priority" not in args


def test_start_non_eco_carries_no_eco_flags(api, monkeypatch):
    """Default (eco off) stays the pre-eco behavior: no --cpu-priority and no
    --pause-on-active on either kind."""
    _enrolled(api, monkeypatch)
    spawned = []
    monkeypatch.setattr(main.subprocess, "Popen",
                        lambda args, **k: spawned.append(args) or FakeProc())
    assert api.mining_start("both", 50)["ok"] is True
    for args in spawned:
        assert "--cpu-priority" not in args
        assert "--pause-on-active" not in args


def test_start_both_eco_applies_each_kinds_lever(api, monkeypatch):
    """In 'both' eco, each process gets only its own lever: CPU gets priority
    (not pause-on-active), GPU gets pause-on-active (not priority)."""
    _enrolled(api, monkeypatch)
    spawned = []
    monkeypatch.setattr(main.subprocess, "Popen",
                        lambda args, **k: spawned.append(args) or FakeProc())
    assert api.mining_start("both", 50, eco=True)["ok"] is True
    by_user = {args[args.index("--user") + 1]: args for args in spawned}
    cpu_args, gpu_args = by_user["tok.xmr"], by_user["tok.rvn"]
    assert "--cpu-priority" in cpu_args and "--pause-on-active" not in cpu_args
    assert "--pause-on-active" in gpu_args and "--cpu-priority" not in gpu_args


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
