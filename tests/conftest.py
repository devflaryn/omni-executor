import os
import sys
from unittest import mock
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    """Patch main.run_engine; record argv, return a canned result per call."""
    calls = []
    canned = {"ok": True}

    def fake_run_engine(args, progress=None, timeout=None):
        calls.append(list(args))
        return dict(canned)

    monkeypatch.setattr(main, "run_engine", fake_run_engine)
    return calls
