"""The two-preset contract, and the shim that keeps old installs working.

The product offers `gaming` and `farming`. `playable`/`hard`/`brutal` were
retired, but a settings.json written before the rename outlives the update, so
main.RETIRED_MODES translates them exactly once -- on the way out of the
settings file, and again defensively in engine_start(). These tests pin both
halves: the alias never reaches argv, and it never reaches the user's eyes.

When the last pre-rename client is gone, RETIRED_MODES and this file go with it.
"""

import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


def _mode_of(argv):
    """The value of --mode in a start argv, or None."""
    return argv[argv.index("--mode") + 1] if "--mode" in argv else None


# ------------------------------------------------------------- what is offered

def test_only_two_modes_are_offered(monkeypatch):
    monkeypatch.setattr(main, "run_engine", lambda *a, **k: {"ok": True})  # no modes
    assert main.Api().engine_modes() == ["gaming", "farming"]


def test_no_retired_name_is_offered():
    assert not set(main.Api._FALLBACK_MODES) & set(main.RETIRED_MODES)


def test_every_alias_resolves_to_a_live_mode():
    # An alias pointing at a name that no longer exists would turn one broken
    # launch into a different broken launch.
    for live in main.RETIRED_MODES.values():
        assert live in main.Api._FALLBACK_MODES


# ------------------------------------------------------------- argv

def test_retired_mode_becomes_the_live_name(captured):
    res = main.Api().engine_start("alice", mode="playable")
    start = next(c for c in captured if c and c[0] == "start")
    assert _mode_of(start) == "gaming"
    assert "playable" not in start
    assert res["ok"] is True


def test_every_retired_alias_is_normalised(captured):
    for retired, live in main.RETIRED_MODES.items():
        del captured[:]
        main.Api().engine_start("alice", mode=retired)
        start = next(c for c in captured if c and c[0] == "start")
        assert _mode_of(start) == live
        assert retired not in start


def test_live_modes_round_trip_unchanged(captured):
    for mode in ("gaming", "farming"):
        del captured[:]
        main.Api().engine_start("alice", mode=mode)
        start = next(c for c in captured if c and c[0] == "start")
        assert _mode_of(start) == mode


def test_unknown_mode_is_an_error_the_user_can_read(captured):
    res = main.Api().engine_start("alice", mode="ludicrous")
    assert res["ok"] is False
    assert res["error"] == "bad_mode"
    assert "ludicrous" in res["message"]
    assert captured == []  # never reached argparse in a subprocess


# ------------------------------------------------------------- gpu (unchanged)

def test_gpu_policy_is_forwarded(captured):
    main.Api().engine_start("alice", mode="gaming", gpu="headless")
    start = next(c for c in captured if c and c[0] == "start")
    assert start[start.index("--gpu") + 1] == "headless"


def test_unknown_gpu_is_dropped_not_forwarded(captured):
    main.Api().engine_start("alice", mode="gaming", gpu="nonsense")
    start = next(c for c in captured if c and c[0] == "start")
    assert "--gpu" not in start


# ------------------------------------------------------------- settings

def _settings_at(tmp_path, monkeypatch, payload):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(main, "SETTINGS_FILE", path)
    return path


def test_persisted_retired_mode_is_migrated_on_load(tmp_path, monkeypatch):
    path = _settings_at(tmp_path, monkeypatch,
                        {"launch": {"mode": "playable", "gpu": "auto"}})
    loaded = main.Api().get_settings()
    assert loaded["launch"]["mode"] == "gaming"
    assert loaded["launch"]["gpu"] == "auto"  # the rest of launch is untouched
    # ...and rewritten on disk, so the next run doesn't need the shim at all.
    assert json.loads(path.read_text(encoding="utf-8"))["launch"]["mode"] == "gaming"


def test_live_persisted_mode_is_left_alone(tmp_path, monkeypatch):
    saved = {"launch": {"mode": "farming"}}
    path = _settings_at(tmp_path, monkeypatch, saved)
    assert main.Api().get_settings()["launch"]["mode"] == "farming"
    assert json.loads(path.read_text(encoding="utf-8")) == saved  # no rewrite


def test_unreadable_settings_still_default_to_a_live_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SETTINGS_FILE", tmp_path / "does-not-exist.json")
    assert main.Api().get_settings()["launch"]["mode"] in main.Api._FALLBACK_MODES
