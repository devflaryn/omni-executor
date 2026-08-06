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


def test_engine_start_gives_the_engine_a_deadline_it_can_meet(captured):
    """The executor must not kill the engine before the engine gives up.

    They used to disagree and the executor always lost: its watchdog fired at
    300 s against the engine's own 360 s budget, so any boot slower than five
    minutes was killed here first. Since the engine spawns QEMU DETACHED,
    that orphans a live instance — the UI reports a failed start while the VM
    runs on. Both numbers now come from one constant."""
    main.Api().engine_start("acct")
    argv = next(a for a in captured if a and a[0] == "start")
    assert "--timeout" in argv, "engine must be given an explicit deadline"
    engine_budget = int(argv[argv.index("--timeout") + 1])
    assert engine_budget == main.BOOT_TIMEOUT
    # our own kill must come strictly later than the engine's own give-up
    assert main.BOOT_TIMEOUT + main.WATCHDOG_GRACE > engine_budget


def test_engine_start_deadline_exceeds_the_engines_normal_boot_budget():
    """Guards the actual regression: 300 < the engine's NORMAL_BOOT_TIMEOUT."""
    try:
        from omnidroid import engine as omni
    except Exception:
        import pytest
        pytest.skip("omnidroid not importable here")
    assert main.BOOT_TIMEOUT >= omni.NORMAL_BOOT_TIMEOUT


def test_engine_stop_also_agrees_on_one_deadline(captured):
    main.Api().engine_stop("acct")
    argv = next(a for a in captured if a and a[0] == "stop")
    assert "--timeout" in argv
    assert int(argv[argv.index("--timeout") + 1]) == main.STOP_TIMEOUT
