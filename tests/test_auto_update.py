"""Auto-update: what happens WITHOUT anyone clicking.

At launch a new build installs itself: it downloads, the window closes, the build
is swapped and the new version opens. While the app is open it only downloads, and
the banner leaves the restart to the user. A launch tries a given version only
once, so a build that keeps reporting itself as out of date can't loop.
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


def _tick(api, monkeypatch, report, launch=False, stage_result="ok", may_apply=True,
          restart_result=None):
    """Run one _update_tick with updates.* faked. Returns what it did."""
    did = {"staged": False, "restarted": False}
    tried = did.setdefault("tried", [])
    staged_info = report.get("staged")

    def fake_stage_app(progress=None):
        did["staged"] = True
        if stage_result != "ok":
            raise RuntimeError(stage_result)

    fake = mock.Mock()
    fake.check.return_value = {k: v for k, v in report.items() if k != "staged"}
    fake.APP_VERSION = "1.0.0"
    fake.stage_app.side_effect = fake_stage_app
    fake.may_auto_apply.return_value = may_apply
    fake.note_auto_apply.side_effect = tried.append
    # staged_info answers "nothing" until stage_app has run, then the receipt.
    fake.staged_info.side_effect = lambda: (
        {"version": report["app"]["available"]} if did["staged"] else staged_info)

    monkeypatch.setitem(sys.modules, "updates", fake)
    def restart():
        did["restarted"] = True
        return restart_result or {"ok": True}

    monkeypatch.setattr(api, "update_app_restart", restart)
    before = len(api._pushed)
    api._update_tick(launch=launch)
    events = api._pushed[before:]
    # The banner's cue, as opposed to installing right now.
    did["banner"] = any(e == "update-ready" for e, _ in events)
    did["closing"] = any(e == "update-applying" and p.get("closing") for e, p in events)
    did["screen"] = any(e == "update-applying" and not p.get("closing") for e, p in events)
    return did


# ------------------------------------------------------- the two behaviours

def test_at_launch_a_new_version_installs_itself(api, monkeypatch):
    did = _tick(api, monkeypatch, _report(), launch=True)
    assert did["screen"] and did["staged"] and did["restarted"] and did["closing"]
    assert not did["banner"]
    assert did["tried"] == ["9.9.9"]
    # A page that loaded late learns the same thing from any check.
    assert api._launch_update == {"version": "9.9.9", "closing": True}


def test_while_running_it_downloads_then_shows_the_banner(api, monkeypatch):
    did = _tick(api, monkeypatch, _report(), launch=False)
    assert did["staged"] and did["banner"]
    assert not did["restarted"] and not did["screen"] and not did["closing"]
    assert api._launch_update is None


def test_an_already_staged_build_is_not_downloaded_twice(api, monkeypatch):
    """Staged by an earlier session: the next launch installs it straight away."""
    did = _tick(api, monkeypatch, _report(staged={"version": "9.9.9"}), launch=True)
    assert not did["staged"] and did["restarted"] and did["closing"]


def test_a_check_that_took_too_long_no_longer_counts_as_launch(api, monkeypatch):
    """On a bad route the answer can come half a minute in. They may be working."""
    monkeypatch.setattr(api, "LAUNCH_APPLY_SECONDS", -1)
    did = _tick(api, monkeypatch, _report(), launch=True)
    assert did["staged"] and did["banner"] and not did["restarted"]


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
    assert did["staged"] and not did["restarted"] and not did["closing"]
    # The Updating screen clears on this, and the app carries on.
    assert any(e == "update-error" for e, _ in api._pushed)
    assert api._launch_update is None


def test_a_failed_install_keeps_the_window_and_falls_back_to_the_banner(api, monkeypatch):
    did = _tick(api, monkeypatch, _report(), launch=True,
                restart_result={"ok": False, "message": "no staged build"})
    assert did["restarted"] and not did["closing"] and did["banner"]
    assert api._launch_update is None


def test_a_failed_background_download_is_reported_as_automatic(api, monkeypatch):
    """The UI suppresses error bars for work nobody asked for; it can only do
    that if the event says the work was automatic."""
    _tick(api, monkeypatch, _report(), stage_result="boom")
    errors = [p for e, p in api._pushed if e == "update-error"]
    assert errors and errors[-1]["auto"] is True


# ------------------------------------------------------------ no restart loop

def test_a_version_already_tried_gets_the_banner_not_another_restart(api, monkeypatch):
    """A build that still reports the old version after installing would otherwise
    close the window on every launch, forever."""
    did = _tick(api, monkeypatch, _report(staged={"version": "9.9.9"}), launch=True, may_apply=False)
    assert did["banner"] and not did["restarted"] and not did["closing"] and not did["screen"]


def test_one_automatic_try_per_version(tmp_path, monkeypatch):
    import updates
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: tmp_path)
    assert updates.may_auto_apply("1.1.0")
    updates.note_auto_apply("1.1.0")
    assert not updates.may_auto_apply("1.1.0")
    # A fixed release is a new version, so it gets its own try.
    assert updates.may_auto_apply("1.1.1")


def test_the_record_survives_the_swap_deleting_the_staging_dir(tmp_path, monkeypatch):
    import updates
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: tmp_path)
    updates.note_auto_apply("1.1.0")
    updates.discard_staged()
    assert not updates.may_auto_apply("1.1.0")


def test_a_damaged_record_does_not_stop_updates(tmp_path, monkeypatch):
    import updates
    monkeypatch.setattr(main.bootstrap, "runtime_dir", lambda: tmp_path)
    (tmp_path / "auto-update.json").write_text("not json", encoding="utf-8")
    assert updates.may_auto_apply("1.1.0")


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
