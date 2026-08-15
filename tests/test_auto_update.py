"""Auto-update: what happens WITHOUT anyone clicking, and the loop guard.

Two behaviours, and the split is about what the user is in the middle of:

  at launch    nobody is doing anything yet, so a new build downloads, swaps
               and relaunches by itself. A prompt at startup is the thing
               people click past, so it would not be worth having.
  while open   they ARE doing something. The download is still automatic
               (invisible, and it makes the restart instant), but the restart
               is theirs to schedule — this process holds the presence lease
               and the editor buffer.

The part that needs testing hardest is _may_auto_apply. An automatic swap ends
by relaunching, which re-runs the same code, so a build that cannot actually
replace the running one turns "auto-update" into a window that disappears
every thirty seconds forever.
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
    api._update_tick(launch=launch)
    return did


# ------------------------------------------------------- the two behaviours

def test_at_launch_a_new_version_installs_itself(api, monkeypatch):
    did = _tick(api, monkeypatch, _report(), launch=True)
    assert did["staged"] and did["restarted"]


def test_while_running_it_downloads_but_does_not_restart(api, monkeypatch):
    """The restart is the user's to schedule: this process holds the presence
    lease its other machines read, and whatever is in the editor."""
    did = _tick(api, monkeypatch, _report(), launch=False)
    assert did["staged"] and not did["restarted"]


def test_an_already_staged_build_is_not_downloaded_twice(api, monkeypatch):
    did = _tick(api, monkeypatch,
                _report(staged={"version": "9.9.9"}), launch=True)
    assert not did["staged"] and did["restarted"]


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


# --------------------------------------------------- the restart-loop guard

def test_an_automatic_apply_is_attempted_once_per_version(api):
    """THE guard. The swap relaunches the app, which runs this code again. A
    build that cannot replace the running one would otherwise download, swap,
    come back as the old version and do it again, forever."""
    assert api._may_auto_apply("9.9.9") is True
    assert api._may_auto_apply("9.9.9") is False


def test_a_later_version_is_still_tried(api):
    """The guard must not become 'never auto-update again' — a broken 9.9.9
    followed by a fixed 9.9.10 has to install."""
    assert api._may_auto_apply("9.9.9") is True
    assert api._may_auto_apply("9.9.10") is True


def test_the_attempt_is_recorded_before_it_is_made(api, tmp_path):
    """Written first, not after: a swap that takes the process down does not
    come back to write its own receipt, and that is exactly the swap the guard
    exists for."""
    api._may_auto_apply("9.9.9")
    receipt = json.loads((tmp_path / "auto-update-attempted.json").read_text())
    assert receipt["version"] == "9.9.9"


def test_an_unwritable_runtime_dir_refuses_to_auto_apply(api, monkeypatch):
    """No receipt means no loop protection, so do not take the risk. The
    banner still offers the update and a human decides."""
    monkeypatch.setattr(main.bootstrap, "runtime_dir",
                        lambda: Path("/nope/definitely/not"))
    with mock.patch.object(Path, "write_text", side_effect=OSError("read-only")):
        assert api._may_auto_apply("9.9.9") is False


def test_a_second_launch_on_the_same_version_offers_instead_of_looping(
        api, monkeypatch):
    """End to end: the swap 'failed' (we are still on the old version), so the
    second launch stages but does NOT restart."""
    first = _tick(api, monkeypatch, _report(), launch=True)
    assert first["restarted"]
    second = _tick(api, monkeypatch,
                   _report(staged={"version": "9.9.9"}), launch=True)
    assert not second["restarted"]


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
