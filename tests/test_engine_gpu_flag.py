"""Passing a flag the bundled engine does not have.

THE BUG, hit on the Mac 2026-08-29 with app 1.0.30:

    omnidroid: error: unrecognized arguments: --gpu auto

`engine_start` always appended `--gpu <policy>`, but `--gpu` only exists on
newer omnidroid builds. The app and the engine are frozen together from two
SEPARATE checkouts (OmniExecutor.spec collects the sibling `omnidroid`
package), and those two repos are on branches that have diverged badly —
measured that day at 105 commits on one side and 43 on the other, with real
macOS GPU work living only on the Mac's branch. So "just update the engine"
is a merge, not a fix, and the app has to cope with an engine older than
itself.

WHY THE EXISTING HANDSHAKE DID NOT CATCH IT. `engine_version()` compares
_REQUIRED_COMMANDS against the engine's advertised `commands` list. That
covers whole SUBCOMMANDS — an engine missing `login` is named up front — but a
missing FLAG on a command that does exist sails straight through and fails
later as an argparse error from a subprocess, which is exactly the failure
mode that check was written to end.

THE SHAPE OF THE FIX mirrors `_OPTIONAL_COMMANDS`/`_supports`: an engine
without `--gpu` is OLDER, not broken, so the flag simply disappears and the
launch proceeds under the engine's own default. Unknown counts as NOT
supported, because a launch that works without a display policy beats a launch
that does not happen at all.
"""
import pytest

import main


@pytest.fixture
def engine(monkeypatch):
    """Record engine argv, and answer `start --help` with a chosen help text.

    Returns a dict the test can read (`calls`) and steer (`help_text`,
    `help_ok`).
    """
    state = {"calls": [], "help_text": "", "help_ok": True}

    def fake_run_engine(args, progress=None, timeout=None):
        state["calls"].append(list(args))
        if len(args) >= 2 and args[-1] == "--help":
            if not state["help_ok"]:
                return {"ok": False, "error": "engine_spawn_failed",
                        "message": "boom"}
            return {"ok": True, "error": "bad_engine_output",
                    "message": state["help_text"], "exit_code": 0}
        return {"ok": True}

    monkeypatch.setattr(main, "run_engine", fake_run_engine)
    return state


# Real argparse output, trimmed. The flag list lives in the USAGE block, which
# argparse prints first — that is what keeps it inside run_engine's 1000-char
# cap on non-JSON stdout (see test_the_flag_is_early_enough_to_survive below).
HELP_WITH_GPU = (
    "usage: omnidroid start [-h] [--place PLACE] [--mode {gaming,farming}]\n"
    "                       [--gpu {auto,headless,window,off}] [--json] user\n"
)
HELP_WITHOUT_GPU = (
    "usage: omnidroid start [-h] [--place PLACE] [--mode {gaming,farming}]\n"
    "                       [--json] user\n"
)


def _start_argv(calls):
    return next(c for c in calls if c and c[0] == "start" and "--help" not in c)


def test_gpu_is_passed_when_the_engine_advertises_it(engine):
    engine["help_text"] = HELP_WITH_GPU
    api = main.Api()
    api.engine_start("farm1", mode="gaming", gpu="auto")
    argv = _start_argv(engine["calls"])
    assert "--gpu" in argv and argv[argv.index("--gpu") + 1] == "auto"


def test_gpu_is_dropped_when_the_engine_does_not_have_it(engine):
    """The Mac case. The launch must still happen — the engine picks its own
    display policy, which is what it did before --gpu existed."""
    engine["help_text"] = HELP_WITHOUT_GPU
    api = main.Api()
    res = api.engine_start("farm1", mode="gaming", gpu="auto")
    argv = _start_argv(engine["calls"])
    assert "--gpu" not in argv
    assert "auto" not in argv
    # ...and the rest of the command is untouched.
    assert argv[:2] == ["start", "farm1"]
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "gaming"
    assert res.get("ok") is True


def test_an_unreadable_probe_drops_the_flag(engine):
    """Fail SAFE. If the probe itself fails we do not know, and a launch that
    works without a display policy beats an argparse error."""
    engine["help_ok"] = False
    api = main.Api()
    api.engine_start("farm1", gpu="auto")
    assert "--gpu" not in _start_argv(engine["calls"])


def test_the_probe_runs_once_and_is_reused(engine):
    """The engine binary cannot change under a running app, so probing on
    every launch would spend a subprocess to re-learn a constant."""
    engine["help_text"] = HELP_WITH_GPU
    api = main.Api()
    for name in ("farm1", "farm2", "farm3"):
        api.engine_start(name, gpu="auto")
    probes = [c for c in engine["calls"] if "--help" in c]
    assert len(probes) == 1, f"expected one probe, got {probes}"


def test_the_probe_is_per_subcommand(engine):
    """`start --gpu` and `pool start --gpu` are different parsers on the engine
    side, so one answer must not be cached for the other."""
    engine["help_text"] = HELP_WITH_GPU
    api = main.Api()
    api.engine_start("farm1", gpu="auto")
    probes = [c for c in engine["calls"] if "--help" in c]
    assert probes == [["start", "--help"]]


def test_pool_start_drops_the_flag_too(engine, monkeypatch):
    """Same engine, same flag, same argparse rejection — fixing only `start`
    would leave the identical error one button away."""
    engine["help_text"] = HELP_WITHOUT_GPU
    api = main.Api()
    monkeypatch.setattr(main.Api, "_pool_receipt", lambda self: None)
    monkeypatch.setattr(main.Api, "_remember_pool",
                        lambda self, wanted, n=None: None)
    api.pool_start(size=1, mode="farming", gpu="auto")
    argv = next(c for c in engine["calls"]
                if c[:2] == ["pool", "start"] and "--help" not in c)
    assert "--gpu" not in argv


def test_the_flag_is_early_enough_to_survive_truncation():
    """PINS THE ASSUMPTION THE PROBE RESTS ON.

    run_engine truncates non-JSON stdout to 1000 characters, so the probe can
    only see `--gpu` if argparse prints it inside that window. It does today
    (char ~523 of an 8 KB help, because the usage block comes first). If the
    engine ever grows enough options to push it past the cap, the probe would
    silently start reporting "not supported" on an engine that supports it —
    the flag would vanish from every launch and nobody would see an error.
    This test is what makes that loud instead.
    """
    engine = pytest.importorskip("omnidroid.engine",
                                 reason="sibling omnidroid checkout not present")
    import contextlib
    import io
    import sys

    argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["omnidroid", "start", "--help"]
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            engine.main()
    finally:
        sys.argv = argv

    text = buf.getvalue()
    if "--gpu" not in text:
        pytest.skip("this engine has no --gpu at all (older checkout)")
    assert text.index("--gpu") < 1000, (
        f"--gpu moved to char {text.index('--gpu')}; run_engine only keeps the "
        f"first 1000 chars, so the capability probe can no longer see it"
    )


# --------------------------------------------------------------- view --hide
#
# `--gpu` and `--hide` need OPPOSITE handling, and that is the whole point of
# treating them separately rather than reusing one rule.
#
# Dropping `--gpu` is safe: the engine falls back to its own display policy and
# the launch still happens. Dropping `--hide` is NOT — `view <name> --json`
# without it OPENS the viewer, so an app that silently stripped the flag would
# pop a window onto the screen of a user who just asked to close one. An engine
# that cannot hide has to say so.

HELP_VIEW_WITH_HIDE = (
    "usage: omnidroid view [-h] [--start] [--hide] [--json] name\n"
)
HELP_VIEW_WITHOUT_HIDE = (
    "usage: omnidroid view [-h] [--start] [--native] [--json] name\n"
)


def test_hide_runs_when_the_engine_supports_it(engine):
    engine["help_text"] = HELP_VIEW_WITH_HIDE
    api = main.Api()
    api.engine_hide("alice")
    view = next(c for c in engine["calls"]
                if c and c[0] == "view" and "--help" not in c)
    assert "--hide" in view and "--json" in view


def test_hide_refuses_rather_than_opening_a_window(engine):
    """The failure the user must never see: asking to hide and getting a new
    window. An engine without --hide gets told no, and `view` is not run."""
    engine["help_text"] = HELP_VIEW_WITHOUT_HIDE
    api = main.Api()
    res = api.engine_hide("alice")
    assert res.get("ok") is False
    assert res.get("error") == "unsupported_by_engine"
    assert "hide" in res.get("message", "").lower()
    ran = [c for c in engine["calls"] if c and c[0] == "view" and "--help" not in c]
    assert ran == [], f"view must not run at all, got {ran}"
