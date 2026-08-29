import os
import sys
from unittest import mock
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402
from tests.enginedouble import EngineCalls, probe_reply  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    """Patch main.run_engine; record argv, return a canned result per call."""
    calls = EngineCalls()
    canned = {"ok": True}

    def fake_run_engine(args, progress=None, timeout=None):
        args = list(args)
        if args and args[-1] == "--help":
            calls.probes.append(args)
            return probe_reply()
        calls.append(args)
        return dict(canned)

    monkeypatch.setattr(main, "run_engine", fake_run_engine)
    return calls
