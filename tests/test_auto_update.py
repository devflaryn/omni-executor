"""Auto-update: what happens WITHOUT anyone clicking.

The download is automatic; the RESTART is always the user's. A new build is
staged in the background (invisible, and it makes the restart instant), and
then the app tells the user: at launch it emits `update-ready`, which the
frontend turns into a popup ("Update found — Restart & apply"). It never swaps
and relaunches on its own — a window that vanishes by itself at startup is
exactly what people find alarming, and a build that reported the wrong version
would turn a silent auto-restart into a loop. The user clicking "Restart &
apply" is both the consent and the loop-stop.
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


@pytest.fixture
def api(tmp_path, monkeypatch):
    """An Api with its runtime dir and settings redirected into tmp_path."""
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: tmp_path)
    a = main.Api()
    a._pushed = []
    monkeypatch.setattr(a, "_push",
                        lambda event, payload=None: a._pushed.append((event, payload)))
    monkeypatch.setattr(a, "get_settings", lambda: dict(main.DEFAULT_SETTINGS))
    return a


def _report(update=True, version="9.9.9", can_apply=True, ok=True, staged=None):
    return {"ok": ok, "app": {"current": "1.0.0", "available": version,
                              "update": update, "canApply": can_apply},
            "runtime": {"update": False}, "staged": staged}


def _tick(api, monkeypatch, report, launch=False, stage_result="ok"):
    """Run one _update_tick with updates.* faked. Returns what it did."""
    did = {"staged": False, "restarted": False}
    staged_info = report.get("staged")

    def fake_stage_app(progress=None):
        did["staged"] = True
        if stage_result != "ok":
            raise RuntimeError(stage_result)

    fake = mock.Mock()
    fake.check.return_value = {k: v for k, v in report.items() if k != "staged"}
    fake.APP_VERSION = "1.0.0"
    fake.stage_app.side_effect = fake_stage_app
    # staged_info answers "nothing" until stage_app has run, then the receipt.
    fake.staged_info.side_effect = lambda: (
        {"version": report["app"]["available"]} if did["staged"] else staged_info)

    monkeypatch.setitem(sys.modules, "updates", fake)
    monkeypatch.setattr(api, "update_app_restart",
                        lambda: did.__setitem__("restarted", True))
    before = len(api._pushed)
    api._update_tick(launch=launch)
    # The popup is driven by an update-ready event, not by a restart.
    did["prompted"] = any(e == "update-ready" for e, _ in api._pushed[before:])
    return did


# ------------------------------------------------------- the two behaviours

def test_at_launch_a_new_version_stages_and_prompts(api, monkeypatch):
    """A launch downloads the build and then PROMPTS — it does not swap and
    relaunch on its own. The user gets the popup, not a vanishing window."""
    did = _tick(api, monkeypatch, _report(), launch=True)
    assert did["staged"] and did["prompted"] and not did["restarted"]


def test_while_running_it_downloads_but_does_not_restart(api, monkeypatch):
    """The restart is the user's to schedule: this process holds the presence
    lease its other machines read, and whatever is in the editor."""
    did = _tick(api, monkeypatch, _report(), launch=False)
    assert did["staged"] and not did["restarted"]


def test_an_already_staged_build_is_not_downloaded_twice(api, monkeypatch):
    """An update already staged (a previous tick got it) is not re-downloaded;
    the launch still prompts to restart into it."""
    did = _tick(api, monkeypatch,
                _report(staged={"version": "9.9.9"}), launch=True)
    assert not did["staged"] and did["prompted"] and not did["restarted"]


def test_a_staged_build_for_a_DIFFERENT_version_is_replaced(api, monkeypatch):
    """Two releases while the app sat open: the stale download is not the one
    to install."""
    did = _tick(api, monkeypatch,
                _report(version="9.9.9", staged={"version": "9.9.8"}))
    assert did["staged"]


# ------------------------------------------------------------ when it stops

def test_nothing_happens_when_there_is_no_update(api, monkeypatch):
    did = _tick(api, monkeypatch, _report(update=False), launch=True)
    assert not did["staged"] and not did["restarted"]


def test_nothing_happens_when_the_check_failed(api, monkeypatch):
    """Offline is not 'up to date', and it is certainly not 'replace
    yourself'."""
    did = _tick(api, monkeypatch, _report(ok=False), launch=True)
    assert not did["staged"] and not did["restarted"]


def test_a_source_checkout_is_never_auto_updated(api, monkeypatch):
    """canApply false means there is no build to replace — running from
    source. Downloading one would be pure waste."""
    did = _tick(api, monkeypatch, _report(can_apply=False), launch=True)
    assert not did["staged"] and not did["restarted"]


def test_the_setting_turns_the_whole_thing_off(api, monkeypatch):
    monkeypatch.setattr(api, "get_settings", lambda: {"autoUpdate": False})
    did = _tick(api, monkeypatch, _report(), launch=True)
    assert not did["staged"] and not did["restarted"]
    # ...and the UI is still told what is available, so the manual buttons work.
    status = [p for e, p in api._pushed if e == "update-status"]
    assert status and status[-1]["app"]["update"] is True


def test_a_failed_download_does_not_restart_into_nothing(api, monkeypatch):
    did = _tick(api, monkeypatch, _report(), launch=True, stage_result="boom")
    assert did["staged"] and not did["restarted"]


def test_a_failed_background_download_is_reported_as_automatic(api, monkeypatch):
    """The UI suppresses error bars for work nobody asked for; it can only do
    that if the event says the work was automatic."""
    _tick(api, monkeypatch, _report(), stage_result="boom")
    errors = [p for e, p in api._pushed if e == "update-error"]
    assert errors and errors[-1]["auto"] is True


# --------------------------------------------------- no restart loop, by design

def test_launch_never_restarts_on_its_own(api, monkeypatch):
    """The loop that used to need guarding cannot happen: a launch prompts, it
    does not swap. Even a build that keeps reporting itself as out of date only
    ever re-opens the same popup — which a human closes — never a window that
    disappears by itself. Two launches in a row, zero automatic restarts."""
    first = _tick(api, monkeypatch, _report(), launch=True)
    assert first["prompted"] and not first["restarted"]
    second = _tick(api, monkeypatch,
                   _report(staged={"version": "9.9.9"}), launch=True)
    assert second["prompted"] and not second["restarted"]


# --------------------------------------------------------------- the watcher

def test_the_watcher_polls_on_an_interval_and_stops_on_shutdown(api, monkeypatch):
    ticks = []
    monkeypatch.setattr(api, "_update_tick",
                        lambda launch=False: ticks.append(launch))
    # Let two iterations through, then ask it to stop.
    waits = []

    def fake_wait(seconds):
        waits.append(seconds)
        if len(waits) >= 2:
            api._update_stop.set()
        return api._update_stop.is_set()

    monkeypatch.setattr(api._update_stop, "wait", fake_wait)
    api._update_watch_loop()
    # First tick is the launch one; everything after it is not.
    assert ticks == [True, False]
    assert waits == [api.UPDATE_POLL_SECONDS] * 2


def test_a_raising_tick_never_ends_the_loop(api, monkeypatch):
    """A daemon thread that dies takes the update mechanism with it silently."""
    calls = []

    def boom(launch=False):
        calls.append(launch)
        raise RuntimeError("network gone")

    monkeypatch.setattr(api, "_update_tick", boom)
    monkeypatch.setattr(api._update_stop, "wait",
                        lambda s: api._update_stop.set() or True)
    api._update_watch_loop()
    assert calls == [True]
    assert [p for e, p in api._pushed if e == "update-status"][-1]["ok"] is False


def test_shutdown_stops_the_watcher(api):
    api._shutdown()
    assert api._update_stop.is_set()
