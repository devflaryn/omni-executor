"""The handshake must catch an engine that cannot do what this app asks.

The contract VERSION alone is not enough, and that gap was a real bug: an
omnidroid build can report contract "1.0" with arch_aware=true and still lack
`login`, `view` and `setup` — every one of which this app calls. Such an
engine sailed through the handshake, the UI showed a healthy status, and the
failure surfaced later as an argparse error from a subprocess the moment
someone clicked "Add account".

The bundled engine is exactly where this bites: on Windows main.py runs the
omnidroid.exe sitting next to it, which is only as new as the last time it was
rebuilt.

The engine side of this pairs with omnidroid's own regression test: its
`commands` list is now DERIVED from its parser, so what it advertises cannot
drift from what it accepts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

# What a July-2026-era engine advertised: `create` (since removed from the
# engine) and no login/view/setup.
STALE_REPORT = {
    "ok": True, "contract": "1.0", "arch_aware": True,
    "commands": ["version", "create", "start", "stop", "remove", "list",
                 "install", "run-app", "adb", "screenshot", "logcat",
                 "capture", "autocap", "test-apk", "doctor", "bases",
                 "use-base"],
}


def test_stale_engine_is_named_by_the_features_that_will_break():
    missing = main.Api()._missing_commands(STALE_REPORT)
    assert set(missing) == {"setup", "login", "view"}


def test_current_engine_advertises_everything_this_app_calls():
    """Runs against the REAL engine, so the two repos cannot drift apart
    silently — this is the test that fails if omnidroid renames a command."""
    rep = main.run_engine(["version", "--json"], timeout=60)
    if not isinstance(rep, dict) or not rep.get("commands"):
        import pytest
        pytest.skip("no engine resolvable in this environment")
    assert main.Api()._missing_commands(rep) == []


def test_no_false_alarm_on_an_engine_that_reports_no_command_list():
    """An engine predating the field is not evidence of a missing command;
    guessing would put a warning in front of a perfectly good install."""
    assert main.Api()._missing_commands({"ok": True}) == []
    assert main.Api()._missing_commands({"ok": True, "commands": []}) == []
    assert main.Api()._missing_commands({"ok": True, "commands": "nonsense"}) == []


def test_every_command_the_app_calls_is_declared_required(captured):
    """The required list must cover the calls this app actually makes,
    otherwise the check passes while the app still breaks. Drives the API and
    compares against what run_engine was asked to run."""
    api = main.Api()
    api.engine_list()
    api.engine_bases()
    api.engine_doctor()
    api.engine_setup()
    api.engine_login_browser()
    api.engine_use_base("arm")
    api.engine_stop("n")
    api.engine_remove("n")
    api.engine_view("n")
    api.engine_start("n")
    called = {argv[0] for argv in captured if argv}
    undeclared = called - set(api._REQUIRED_COMMANDS)
    assert not undeclared, f"called but not declared required: {undeclared}"


def test_version_report_carries_the_field_the_ui_reads():
    rep = main.Api().engine_version()
    assert "missing_commands" in rep
    assert isinstance(rep["missing_commands"], list)


def test_engine_start_imposes_no_deadline_of_its_own(captured):
    """The executor must not kill the engine before the engine gives up.

    HISTORY, because this test has now been through both halves of the same
    mistake. It used to assert the two sides agreed on a NUMBER: the app's
    watchdog fired at 300 s against the engine's own 360 s budget, so any boot
    slower than five minutes was killed here first, and since the engine spawns
    QEMU DETACHED that orphans a live instance — the UI reports a failed start
    while the VM runs on. Agreeing on 600 fixed the disagreement and left the
    real defect in place: a boot's length is a property of the USER'S PC, and no
    constant compiled into this app knows what that is. A weak CPU, a throttled
    laptop, twenty instances on one box, or a PC with no hardware
    virtualization at all (the engine emulates rather than refusing to boot now)
    all take longer than any number anybody measured here.

    So the app stopped sending one. The engine waits on the guest's own progress
    signals and gives up on SILENCE; this side's watchdog is a silence budget
    too (see IdleWatchdog), rearmed by every line the engine prints.
    """
    main.Api().engine_start("acct")
    argv = next(a for a in captured if a and a[0] == "start")
    assert "--timeout" not in argv, (
        "the app is imposing a boot deadline on the engine again — that is the "
        "ceiling this was removed to get rid of")


def test_the_app_side_budget_is_silence_not_duration():
    """It has to clear the engine's 15 s progress cadence by a wide margin, and
    must not be anywhere near the length of a boot."""
    assert main.ENGINE_IDLE_TIMEOUT >= 60
    assert main.ENGINE_IDLE_TIMEOUT <= 600


def test_the_engine_no_longer_carries_a_default_boot_deadline():
    """The other half of the same change, asserted against the engine itself:
    the product boot path must not invent a wall-clock budget."""
    try:
        from omnidroid import engine as omni
    except Exception:
        import pytest
        pytest.skip("omnidroid not importable here")
    assert omni.default_boot_cap(first_boot=False) is None
    assert omni.default_boot_cap(first_boot=True) is None


def test_engine_stop_also_agrees_on_one_deadline(captured):
    main.Api().engine_stop("acct")
    argv = next(a for a in captured if a and a[0] == "stop")
    assert "--timeout" in argv
    assert int(argv[argv.index("--timeout") + 1]) == main.STOP_TIMEOUT
