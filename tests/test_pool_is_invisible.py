"""A warm-pool slot is an optimisation, never an account, and never a leftover.

Reported 2026-09-02: "it always spawns something like _pool0, 1, 2 and it
launches on the production version as well."

Three separate defects were behind that one sentence, and each is pinned here:

  1. `_pool<n>` slots were listed to the UI as ACCOUNTS, complete with Launch,
     Stop and Delete buttons.
  2. auto-warm was ON by default, so people who had never heard of a warm pool
     got one.
  3. the pool manager is DETACHED, so the slot and its manager kept running --
     and kept re-warming -- after the window was closed.
"""
import sys
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402


def _api():
    return main.Api.__new__(main.Api)


# ---------------------------------------------------------------- 1. listing

def test_pool_slots_are_never_listed_as_accounts(monkeypatch):
    """The engine returns them on purpose (a slot IS an ordinary instance, so
    every guard and sweep works on it with no special case) and tags them
    `pool_slot`. The app is what must not present them as the user's."""
    rows = {"ok": True, "accounts": [
        {"name": "admn1b12farm2", "pool_slot": False, "running": False},
        {"name": "_pool0", "pool_slot": True, "running": True},
        {"name": "_pool1", "pool_slot": True, "running": True},
        {"name": "HezMi_ImYu", "running": True},
    ]}
    monkeypatch.setattr(main, "run_engine", lambda *a, **k: rows)
    got = main.Api.engine_list(_api())
    assert [a["name"] for a in got["accounts"]] == ["admn1b12farm2", "HezMi_ImYu"]


def test_a_list_that_is_not_a_dict_is_passed_through(monkeypatch):
    """`run_engine` normalises, but an engine error is a bare dict with no
    `accounts` key and must not become a crash in the one call every view
    makes."""
    monkeypatch.setattr(main, "run_engine",
                        lambda *a, **k: {"ok": False, "error": "engine_missing"})
    assert main.Api.engine_list(_api())["error"] == "engine_missing"


def test_an_account_legitimately_named_like_a_slot_is_still_filtered(monkeypatch):
    """The FLAG decides, not the name. The engine reserves the `_pool` prefix
    (build_acct refuses it), so trusting its tag keeps one rule in one place."""
    rows = {"ok": True, "accounts": [{"name": "_pool0", "pool_slot": True},
                                     {"name": "poolman", "pool_slot": False}]}
    monkeypatch.setattr(main, "run_engine", lambda *a, **k: rows)
    got = main.Api.engine_list(_api())
    assert [a["name"] for a in got["accounts"]] == ["poolman"]


# ------------------------------------------------------------- 2. opt-in now

def test_auto_warm_is_off_unless_asked_for():
    """It shipped ON in 1.0.33. A speed optimisation that spawns virtual
    machines nobody asked for is a surprise, not a default."""
    assert main.DEFAULT_SETTINGS["autoWarm"] is False


def test_auto_warm_needs_the_setting_and_the_engine(monkeypatch):
    api = _api()
    monkeypatch.setattr(main.Api, "engine_version",
                        lambda self: {"pool_supported": True})
    monkeypatch.setattr(main.Api, "get_settings", lambda self: {"autoWarm": True})
    assert main.Api._autowarm_enabled(api) is True

    monkeypatch.setattr(main.Api, "get_settings", lambda self: {"autoWarm": False})
    assert main.Api._autowarm_enabled(api) is False

    monkeypatch.setattr(main.Api, "get_settings", lambda self: {})
    assert main.Api._autowarm_enabled(api) is False


def test_an_engine_without_a_pool_is_never_asked(monkeypatch):
    api = _api()
    monkeypatch.setattr(main.Api, "get_settings", lambda self: {"autoWarm": True})
    monkeypatch.setattr(main.Api, "engine_version",
                        lambda self: {"pool_supported": False})
    assert main.Api._autowarm_enabled(api) is False


def test_the_old_off_switch_still_means_off(monkeypatch):
    """1.0.33 wrote the flag at `pool.autoWarm`. Somebody who found that
    switch and turned it off must not have it turned back on by an update."""
    api = _api()
    monkeypatch.setattr(main.Api, "engine_version",
                        lambda self: {"pool_supported": True})
    monkeypatch.setattr(main.Api, "get_settings",
                        lambda self: {"autoWarm": True,
                                      "pool": {"autoWarm": False}})
    assert main.Api._autowarm_enabled(api) is False


# ------------------------------------------------------------- 3. shutdown

def test_closing_the_app_stops_a_pool_we_warmed(monkeypatch):
    """⚠ THE ONE EXCEPTION to "instances outlive the app", and it has to be.

    Everywhere else that rule is right: the user launched those instances on
    purpose and QEMU is detached so they keep farming. A warm slot is the
    opposite -- nobody launched it, it exists only to make THIS app's next
    launch instant, and once the app is gone there is no next launch. Left
    up, it was a multi-gigabyte VM and a detached manager re-warming itself
    indefinitely after the window closed.
    """
    api = _api()
    calls = []
    monkeypatch.setattr(main, "run_engine",
                        lambda args, **k: calls.append(args) or {"ok": True})
    monkeypatch.setattr(main.Api, "_pool_receipt",
                        lambda self: {"mode": "gaming", "place": "1", "gpu": ""})
    monkeypatch.setattr(main.Api, "_remember_pool", lambda self, *a, **k: None)
    main.Api._stop_autowarmed_pool(api)
    assert calls and calls[0][:2] == ["pool", "stop"]


def test_a_pool_we_did_not_start_is_left_alone(monkeypatch):
    """`omnidroid pool start` by hand is somebody meaning it. Not ours to
    collect."""
    api = _api()
    calls = []
    monkeypatch.setattr(main, "run_engine",
                        lambda args, **k: calls.append(args) or {"ok": True})
    monkeypatch.setattr(main.Api, "_pool_receipt", lambda self: None)
    main.Api._stop_autowarmed_pool(api)
    assert calls == []


def test_shutdown_never_raises_on_the_way_out(monkeypatch):
    api = _api()
    monkeypatch.setattr(main.Api, "_pool_receipt",
                        lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    main.Api._stop_autowarmed_pool(api)      # must not raise
