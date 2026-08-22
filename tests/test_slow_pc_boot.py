#!/usr/bin/env python3
"""The app must not kill a boot that is still going.

    python3 -m pytest tests/test_slow_pc_boot.py -q

`run_engine` armed `threading.Timer(timeout, proc.kill)` -- an ABSOLUTE
deadline -- and `engine_start` passed `BOOT_TIMEOUT + WATCHDOG_GRACE` = 660 s.
Any boot slower than eleven minutes was killed by THIS process, and that is a
worse failure than it sounds, because the engine spawns QEMU **detached**:
killing the engine does not stop the VM. The user got a launch that said it
failed while a live instance stayed up, holding gigabytes, with nothing in the
UI offering to stop it.

Eleven minutes is generous on the machine the number was measured on. It is not
generous on a weak CPU, on a laptop that has thermally throttled, on a box
already running twenty instances, or -- the case that motivated this -- on a PC
with no hardware virtualization, where the guest is emulated and a boot
legitimately takes several times as long.

The fix is to stop measuring the wrong thing. The engine prints a progress line
at least every 15 s for the whole of a boot, so SILENCE is a signal and
DURATION is not. The watchdog is rearmed on every line and fires only when the
engine has genuinely stopped talking. A wedged engine is still caught, in about
the same time it always was; a slow one is left alone.

These tests pin that property. The numbers may move.
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


class TheWatchdogWatchesSilence(unittest.TestCase):
    def test_it_fires_when_nothing_is_said(self):
        killed = threading.Event()
        wd = main.IdleWatchdog(0.15, killed.set)
        wd.start()
        self.assertTrue(killed.wait(2.0), "a silent engine was never killed")
        wd.cancel()

    def test_every_line_buys_another_full_window(self):
        """Ten short quiet gaps in a row must not add up to one long one."""
        killed = threading.Event()
        wd = main.IdleWatchdog(0.30, killed.set)
        wd.start()
        for _ in range(10):
            time.sleep(0.05)
            wd.poke()
        self.assertFalse(killed.is_set(),
                         "killed a boot that was reporting progress")
        wd.cancel()

    def test_cancel_means_cancel(self):
        killed = threading.Event()
        wd = main.IdleWatchdog(0.1, killed.set)
        wd.start()
        wd.cancel()
        time.sleep(0.3)
        self.assertFalse(killed.is_set())

    def test_a_disarmed_watchdog_never_fires(self):
        """timeout=None is 'no watchdog', and must stay free of threads."""
        killed = threading.Event()
        wd = main.IdleWatchdog(None, killed.set)
        wd.start()
        wd.poke()
        time.sleep(0.2)
        self.assertFalse(killed.is_set())
        wd.cancel()


class RunEngineIsRearmedByProgress(unittest.TestCase):
    """End to end through run_engine, with a fake engine that talks slowly."""

    def _fake_engine(self, script):
        """`script` is a list of (delay, stderr_line). Returns a Popen double."""
        code = ("import sys, time\n"
                f"for d, line in {script!r}:\n"
                "    time.sleep(d)\n"
                "    sys.stderr.write(line + '\\n'); sys.stderr.flush()\n"
                "sys.stdout.write('{\"ok\": true, \"booted\": true}')\n")
        return [sys.executable, "-c", code]

    def test_a_slow_but_talkative_engine_is_not_killed(self):
        # Six 0.15 s gaps = 0.9 s total, well past a 0.4 s watchdog window --
        # but no single gap reaches it.
        script = [(0.15, f"[start u1] {i/60:.1f} min - Android booting")
                  for i in range(6)]
        with mock.patch.object(main, "engine_prefix",
                               return_value=self._fake_engine(script)):
            out = main.run_engine(["start", "u1"], timeout=0.4)
        self.assertTrue(out.get("ok"),
                        f"a talking engine was killed: {out}")

    def test_a_silent_engine_is_still_killed(self):
        script = [(3.0, "too late")]
        with mock.patch.object(main, "engine_prefix",
                               return_value=self._fake_engine(script)):
            started = time.time()
            out = main.run_engine(["start", "u1"], timeout=0.4)
        self.assertFalse(out.get("ok"))
        self.assertLess(time.time() - started, 2.5,
                        "the watchdog did not fire on a wedged engine")

    def test_a_verdict_already_printed_is_not_thrown_away(self):
        """The engine writes its JSON verdict and THEN exits. One that wedges
        after printing it has already done the work, and reporting
        `engine_unresponsive` over the top of a successful launch would be the
        very defect this watchdog exists to stop -- the UI saying it failed
        while the VM runs."""
        code = ("import sys, time\n"
                "sys.stdout.write('{\"ok\": true, \"booted\": true}')\n"
                "sys.stdout.flush()\n"
                "time.sleep(3)\n")          # ...then wedge
        with mock.patch.object(main, "engine_prefix",
                               return_value=[sys.executable, "-c", code]):
            out = main.run_engine(["start", "u1"], timeout=0.4)
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("booted"))

    def test_a_killed_engine_says_the_instance_may_still_be_running(self):
        """QEMU is detached. Killing the engine leaves the VM up, and the user
        has to be told that rather than shown a bare failure."""
        script = [(3.0, "too late")]
        with mock.patch.object(main, "engine_prefix",
                               return_value=self._fake_engine(script)):
            out = main.run_engine(["start", "u1"], timeout=0.4)
        self.assertEqual(out.get("error"), "engine_unresponsive")
        self.assertIn("still", (out.get("message") or "").lower())


class StartDoesNotImposeADeadline(unittest.TestCase):
    """The engine waits on the guest's own progress now; the app must not
    second-guess it with a number of its own."""

    def _argv_for_start(self):
        seen = {}

        def fake_run(args, progress=None, timeout=None):
            seen["args"] = args
            seen["timeout"] = timeout
            return {"ok": True}

        api = main.Api.__new__(main.Api)
        with mock.patch.object(main, "run_engine", fake_run), \
                mock.patch.object(main.Api, "_bad_name", lambda self, n: None), \
                mock.patch.object(main.Api, "_progress",
                                  lambda self, n: (lambda line: None)), \
                mock.patch.object(main.Api, "_beat_now",
                                  lambda self, *a, **k: None), \
                mock.patch.object(main.Api, "_push",
                                  lambda self, *a, **k: None):
            main.Api.engine_start(api, "u1")
        return seen

    def test_start_no_longer_caps_the_engine_s_boot(self):
        seen = self._argv_for_start()
        self.assertNotIn("--timeout", seen["args"],
                         "the app is still imposing a boot deadline on the "
                         "engine; the engine waits on progress now")

    def test_the_app_side_watchdog_is_an_idle_budget_not_a_boot_budget(self):
        seen = self._argv_for_start()
        # Whatever it is, it must be a SILENCE budget -- comfortably more than
        # the engine's 15 s progress cadence, and nothing like a boot's length.
        self.assertIsNotNone(seen["timeout"])
        self.assertGreaterEqual(seen["timeout"], 60)
        self.assertLessEqual(seen["timeout"], 600)


if __name__ == "__main__":
    unittest.main()
