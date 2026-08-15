"""The warm pool, as the app drives it.

The pool is N instances pre-booted to the account-free ready point. Measured on
this box: the boot stage of a cold farming launch is 35-60 s, adopting a warm
slot is 0.093 s -- and on Windows it is the only fast path there is, because
WHPX cannot snapshot a VM.

Three rules are worth pinning, because each one has a way of failing silently:

  * `fill` is never called. It boots in the CALLING process, so from the app it
    would hold the UI thread for the whole boot; `start` spawns the detached
    manager and returns.
  * a pool is warmed for the SAME mode/place/gpu as the launch. `--place` sizes
    the slot (the engine floors guest RAM per game: PS99 -> 3072 MB) and `mem`
    is hashed into the slot key, so a pool warmed without it is invisible to
    that launch -- every launch cold-boots while `pool status` reports slots
    ready.
  * changing those settings STOPS the old pool before starting the new one. The
    engine's manager only sweeps DEAD slots, so a live slot for a key nobody
    launches is never adopted and never reclaimed: it just holds its ~2.2-3.2 GB
    of RAM and ~1.3 GB of scratch.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    """Every test in here writes the pool receipt, so no test in here may be
    allowed near the real settings.json."""
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "SETTINGS_FILE", path)
    return path


def _pool_argv(calls):
    """Every `pool ...` argv, in the order the app ran them."""
    return [c for c in calls if c and c[0] == "pool"]


def _actions(calls):
    return [c[1] for c in _pool_argv(calls)]


def _engine(monkeypatch, results=None):
    """Fake run_engine: record argv, answer per subcommand ("pool status").

    Anything unanswered is a plain success, which is also what `version --json`
    degrades to here -- engine_modes() then falls back to the live pair, so
    mode validation still has a list to check against.
    """
    calls = []
    answers = results or {}

    def fake_run_engine(args, progress=None, timeout=None):
        calls.append(list(args))
        return dict(answers.get(" ".join(args[:2]), {"ok": True}))

    monkeypatch.setattr(main, "run_engine", fake_run_engine)
    return calls


CONFIGURED = {"ok": True, "configured": {"size": 1, "key": "abc"},
              "manager_alive": True, "ready": 1, "ready_matching": 1,
              "booting": 0, "adopted": 0, "dead": 0, "orphaned": 0}
NO_POOL = {"ok": True, "configured": None, "manager_alive": False,
           "ready": 0, "ready_matching": 0}


# --------------------------------------------------------------------- argv

def test_pool_status_argv(captured):
    main.Api().pool_status()
    assert ["pool", "status", "--json"] in captured


def test_pool_stop_argv(captured):
    main.Api().pool_stop()
    assert ["pool", "stop", "--json"] in captured


def test_pool_start_argv_carries_the_whole_launch_spec(monkeypatch):
    calls = _engine(monkeypatch)
    main.Api().pool_start(2, "farming", "8737899170", "headless")
    start = next(c for c in _pool_argv(calls) if c[1] == "start")
    assert start[:3] == ["pool", "start", "--size"]
    assert start[3] == "2"
    assert start[start.index("--mode") + 1] == "farming"
    assert start[start.index("--place") + 1] == "8737899170"
    assert start[start.index("--gpu") + 1] == "headless"
    assert "--json" in start


def test_the_app_never_blocks_on_fill(monkeypatch):
    """`pool fill` boots in this process. The app must only ever use `start`,
    which hands the boots to a detached manager and returns."""
    calls = _engine(monkeypatch)
    api = main.Api()
    api.pool_start(1, "gaming")
    api.pool_status()
    api.pool_stop()
    assert all("fill" not in c for c in calls)


def test_a_bare_pool_start_forwards_no_place_or_gpu(monkeypatch):
    calls = _engine(monkeypatch)
    main.Api().pool_start(1, "gaming")
    start = next(c for c in _pool_argv(calls) if c[1] == "start")
    assert "--place" not in start and "--gpu" not in start


def test_retired_mode_is_resolved_before_argv(monkeypatch):
    calls = _engine(monkeypatch)
    main.Api().pool_start(1, "playable")
    start = next(c for c in _pool_argv(calls) if c[1] == "start")
    assert start[start.index("--mode") + 1] == "gaming"
    assert "playable" not in start


def test_unknown_gpu_is_dropped_not_forwarded(monkeypatch):
    """Same rule as engine_start, and it has to be the same rule: a policy the
    launch never sends must not end up in the pool's key."""
    calls = _engine(monkeypatch)
    main.Api().pool_start(1, "gaming", "", "nonsense")
    start = next(c for c in _pool_argv(calls) if c[1] == "start")
    assert "--gpu" not in start


def test_pool_commands_get_a_deadline(monkeypatch):
    seen = {}

    def fake_run_engine(args, progress=None, timeout=None):
        seen[" ".join(args[:2])] = timeout
        return {"ok": True}

    monkeypatch.setattr(main, "run_engine", fake_run_engine)
    main.Api().pool_status()
    main.Api().pool_stop()
    assert seen["pool status"] == main.POOL_TIMEOUT
    # stop powers off every live slot politely first, so it gets more room.
    assert seen["pool stop"] == main.POOL_STOP_TIMEOUT > main.POOL_TIMEOUT


def test_a_launch_never_touches_the_pool(captured):
    """The pool is an optimisation the engine applies inside `start`. Nothing
    in the app's launch path may depend on it, so a broken pool degrades to a
    normal boot instead of failing a launch."""
    main.Api().engine_start("alice", mode="farming", place="8737899170")
    assert _pool_argv(captured) == []


# --------------------------------------------------------------- validation

def test_bad_size_never_reaches_the_engine(captured):
    for bad in (0, -1, 99, 1.5, "two", "", None, [1], {}):
        del captured[:]
        res = main.Api().pool_start(bad, "gaming")
        assert res["ok"] is False, bad
        assert res["error"] == "bad_pool_size", bad
        assert captured == [], bad          # never reached argparse in a subprocess


def test_true_is_not_a_size(captured):
    """isinstance(True, int) is True in Python; a JS `true` on this bridge
    means somebody passed a flag, not a pool of one."""
    res = main.Api().pool_start(True, "gaming")
    assert res["error"] == "bad_pool_size"
    assert captured == []


def test_integral_numbers_and_digit_strings_are_accepted(monkeypatch):
    for good, expected in ((2, "2"), (2.0, "2"), ("3", "3")):
        calls = _engine(monkeypatch)
        main.Api().pool_start(good, "gaming")
        start = next(c for c in _pool_argv(calls) if c[1] == "start")
        assert start[start.index("--size") + 1] == expected


def test_the_size_ceiling_is_a_resource_ceiling():
    """Each slot is a running VM (~2.2-3.2 GB RSS + ~1.3 GB scratch here), so
    the cap is small on purpose."""
    assert 1 <= main.Api.POOL_MAX_SIZE <= 8
    assert main.Api()._pool_size(main.Api.POOL_MAX_SIZE)[1] is None
    assert main.Api()._pool_size(main.Api.POOL_MAX_SIZE + 1)[0] is None


def test_bad_mode_never_reaches_the_pool(captured):
    for bad in ("ludicrous", "", None, 7):
        del captured[:]
        res = main.Api().pool_start(1, bad)
        assert res["ok"] is False, bad
        assert res["error"] == "bad_mode", bad
        assert _pool_argv(captured) == [], bad


def test_mode_is_checked_against_the_engines_own_list(monkeypatch):
    """A mode a newer engine adds is worth warming for; validating against a
    constant would refuse it here while `start` accepted it."""
    def fake_run_engine(args, progress=None, timeout=None):
        if args[0] == "version":
            return {"ok": True, "modes": ["gaming", "farming", "ludicrous"]}
        return {"ok": True}

    monkeypatch.setattr(main, "run_engine", fake_run_engine)
    assert main.Api()._pool_mode("ludicrous")[0] == "ludicrous"


# ------------------------------------------------------- re-warm on a change

def test_changing_the_place_stops_the_old_pool_first(monkeypatch):
    """THE trap this policy exists for. `mem` is part of the slot key and the
    engine floors it per game, so slots warmed for one place are invisible to a
    launch at another -- and the manager only sweeps DEAD slots, so leaving
    them running costs gigabytes for a pool no launch can use."""
    calls = _engine(monkeypatch, {"pool status": CONFIGURED})
    api = main.Api()
    api.pool_start(1, "farming", "8737899170")
    del calls[:]
    api.pool_start(1, "farming", "142823291")          # PS99: a 3072 MB floor

    assert _actions(calls) == ["status", "stop", "start"]
    start = next(c for c in _pool_argv(calls) if c[1] == "start")
    assert start[start.index("--place") + 1] == "142823291"


def test_changing_the_mode_or_graphics_also_re_warms(monkeypatch):
    for first, second in ((("gaming", "", "auto"), ("farming", "", "auto")),
                          (("gaming", "", "auto"), ("gaming", "", "headless"))):
        calls = _engine(monkeypatch, {"pool status": CONFIGURED})
        api = main.Api()
        api.pool_start(1, *first)
        del calls[:]
        api.pool_start(1, *second)
        assert _actions(calls) == ["status", "stop", "start"], second


def test_re_warming_the_same_settings_keeps_the_warm_instance(monkeypatch):
    """Idempotent: the same settings must not power off the very instance the
    next launch is going to adopt."""
    calls = _engine(monkeypatch, {"pool status": CONFIGURED})
    api = main.Api()
    api.pool_start(1, "farming", "8737899170", "auto")
    del calls[:]
    api.pool_start(1, "farming", "8737899170", "auto")
    assert _actions(calls) == ["status", "start"]


def test_nothing_configured_means_nothing_to_stop(monkeypatch):
    calls = _engine(monkeypatch, {"pool status": NO_POOL})
    main.Api().pool_start(1, "gaming")
    assert _actions(calls) == ["status", "start"]


def test_a_pool_this_app_did_not_warm_is_taken_over_cleanly(monkeypatch):
    """No receipt but a live pool: its slots are keyed for something unknown,
    and `pool start` would overwrite the config and strand them."""
    calls = _engine(monkeypatch, {"pool status": CONFIGURED})
    main.Api().pool_start(1, "gaming")
    assert _actions(calls) == ["status", "stop", "start"]


# ------------------------------------------------------------- the receipt

def test_the_receipt_records_what_was_warmed(settings_file, monkeypatch):
    """The engine cannot answer "is this pool still the one my settings would
    use?" -- it records the RESOLVED machine, and the place that sized it is
    not in there. So the app keeps its own note."""
    _engine(monkeypatch, {"pool status": NO_POOL})
    res = main.Api().pool_start(1, "farming", "8737899170", "headless")
    assert res["warmed_for"] == {"mode": "farming", "place": "8737899170",
                                 "gpu": "headless"}
    saved = json.loads(settings_file.read_text(encoding="utf-8"))["pool"]
    assert saved["warmedFor"] == res["warmed_for"]
    assert saved["size"] == 1


def test_pool_status_hands_the_receipt_to_the_ui(monkeypatch):
    _engine(monkeypatch, {"pool status": CONFIGURED})
    api = main.Api()
    api.pool_start(1, "gaming", "", "auto")
    status = api.pool_status()
    assert status["warmed_for"] == {"mode": "gaming", "place": "", "gpu": "auto"}
    # ...and the count the UI must read is the matching one, not `ready`.
    assert "ready_matching" in status


def test_a_failed_start_leaves_no_receipt(settings_file, monkeypatch):
    _engine(monkeypatch, {"pool status": NO_POOL,
                          "pool start": {"ok": False, "error": "pool_key"}})
    res = main.Api().pool_start(1, "gaming")
    assert res["ok"] is False
    assert not json.loads(settings_file.read_text(encoding="utf-8")).get("pool")


def test_stop_clears_the_receipt_only_when_it_worked(settings_file, monkeypatch):
    _engine(monkeypatch, {"pool status": NO_POOL,
                          "pool stop": {"ok": False, "error": "boom"}})
    api = main.Api()
    api.pool_start(1, "gaming")
    api.pool_stop()
    # The slots are still up; forgetting we warmed them is how a pool nobody
    # wants outlives every later chance to notice it.
    assert json.loads(settings_file.read_text(encoding="utf-8"))["pool"]["warmedFor"]

    _engine(monkeypatch, {"pool status": NO_POOL})
    api.pool_stop()
    assert not json.loads(settings_file.read_text(encoding="utf-8")).get("pool")


def test_an_unwritable_settings_file_does_not_break_the_pool(monkeypatch):
    _engine(monkeypatch, {"pool status": NO_POOL})

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(main.Api, "_write_settings", staticmethod(boom))
    assert main.Api().pool_start(1, "gaming")["ok"] is True


# ------------------------------------------------------- the capability gate

# A pre-pool engine: everything this app REQUIRES, and no `pool`.
POOL_LESS = {"ok": True, "contract": "1.0", "arch_aware": True,
             "commands": ["version", "doctor", "bases", "use-base", "setup",
                          "list", "login", "start", "stop", "remove", "view"]}


def test_an_engine_without_pool_reports_the_feature_as_absent(monkeypatch):
    monkeypatch.setattr(main, "run_engine", lambda *a, **k: dict(POOL_LESS))
    monkeypatch.setattr(main, "engine_prefix", lambda: ["omnidroid"])
    assert main.Api().engine_version()["pool_supported"] is False


def test_an_engine_with_pool_reports_it(monkeypatch):
    rep = {**POOL_LESS, "commands": [*POOL_LESS["commands"], "pool"]}
    monkeypatch.setattr(main, "run_engine", lambda *a, **k: dict(rep))
    monkeypatch.setattr(main, "engine_prefix", lambda: ["omnidroid"])
    assert main.Api().engine_version()["pool_supported"] is True


def test_an_engine_that_lists_nothing_is_treated_as_not_having_it():
    """Unknown counts as NO here, the opposite of _missing_commands: a false
    "missing" would warn about a working install, while a false "present" puts
    a button in front of the user that ends in an argparse error."""
    assert main.Api()._supports({"ok": True}, "pool") is False
    assert main.Api()._supports({"ok": True, "commands": []}, "pool") is False
    assert main.Api()._supports({"ok": True, "commands": "nonsense"}, "pool") is False


def test_a_pool_less_engine_is_not_reported_as_broken():
    """The pool is optional. Naming it in the required list would turn "this
    build has no warm pool" into the version-mismatch banner."""
    assert "pool" not in main.Api._REQUIRED_COMMANDS
    assert "pool" in main.Api._OPTIONAL_COMMANDS
    assert main.Api()._missing_commands(POOL_LESS) == []


def _read(*parts):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, *parts), encoding="utf-8") as f:
        return f.read()


def test_the_ui_hides_the_control_behind_that_flag():
    """There is no JS test runner in this repo, so the wiring is pinned in the
    source: the flag main.py reports is what gates the control, the panel reads
    `ready_matching` (`ready` counts slots keyed for settings this launch
    cannot use), and the UI never reaches for the blocking `fill`."""
    store = _read("frontend", "src", "engine.jsx")
    view = _read("frontend", "src", "components", "AccountsView.jsx")
    assert "version?.pool_supported === true" in store
    assert "engine.poolSupported && (" in view
    assert "ready_matching" in view
    for source in (store, view):
        assert "pool_fill" not in source
        assert 'api("pool_start"' in store and 'api("pool_status"' in store
