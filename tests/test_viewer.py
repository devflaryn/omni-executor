"""The View button: one `omnidroid view` call, nothing else.

Viewing is the same shape as the Run button — ask the engine, hand back what it
says. The engine owns the viewer; this app does not implement, poll, or
second-guess it.

The one rule worth pinning is which viewer gets asked for. `--native` sends the
screen to the OS client — macOS Screen Sharing — and the View button must never
do that, so the flag must not appear on the command line.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


@pytest.fixture
def engine(monkeypatch):
    """Record view argv; reply from a queue of canned engine results."""
    calls, replies = [], []

    def fake_run_engine(args, progress=None, timeout=None):
        calls.append(list(args))
        return dict(replies.pop(0)) if replies else {"ok": True}

    monkeypatch.setattr(main, "run_engine", fake_run_engine)
    return calls, replies


OPENED = {"ok": True, "viewer": "built-in (Tk+RFB)", "viewer_pid": 4242,
          "vnc_host": "127.0.0.1", "vnc_port": 18002}


def test_view_runs_the_engines_view_command(engine):
    """`--start` so a stopped instance boots first, `--json` so the result is
    parseable rather than prose."""
    calls, replies = engine
    replies.append(OPENED)
    main.Api().engine_view("alice")
    view = next(c for c in calls if c and c[0] == "view")
    assert view[1] == "alice"
    assert "--start" in view and "--json" in view


def test_screen_sharing_is_never_asked_for(engine):
    """`--native` is the macOS Screen Sharing hand-off. The View button asks
    for omnidroid's own viewer instead — the flag must not appear at all."""
    calls, replies = engine
    replies.append(OPENED)
    main.Api().engine_view("alice")
    assert all("--native" not in c for c in calls)


def test_view_is_a_single_call(engine):
    """No retry, no second viewer: one button press, one engine call."""
    calls, replies = engine
    replies.append(OPENED)
    main.Api().engine_view("alice")
    assert len([c for c in calls if c and c[0] == "view"]) == 1


def test_view_gives_the_engine_a_deadline_it_can_meet(engine, monkeypatch):
    """--start may cold-boot, so both sides must agree on one number rather
    than this process killing the engine mid-boot."""
    seen = {}

    def fake_run_engine(args, progress=None, timeout=None):
        seen[args[0]] = timeout
        return dict(OPENED)

    monkeypatch.setattr(main, "run_engine", fake_run_engine)
    main.Api().engine_view("alice")
    assert seen["view"] > main.VIEW_TIMEOUT


def test_the_engines_answer_is_handed_back_untouched(engine):
    """Success or failure, the engine is the one telling the truth here."""
    calls, replies = engine
    replies.append(OPENED)
    assert main.Api().engine_view("alice")["viewer_pid"] == 4242

    replies.append({"ok": False, "error": "not_running", "message": "boom"})
    res = main.Api().engine_view("alice")
    assert res["ok"] is False and res["message"] == "boom"


def test_bad_account_name_never_reaches_the_engine(engine):
    calls, _ = engine
    res = main.Api().engine_view("../etc; rm -rf")
    assert res["ok"] is False and res["error"] == "bad_account_name"
    assert calls == []
