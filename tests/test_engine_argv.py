import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


def test_engine_list_argv(captured):
    api = main.Api()
    api.engine_list()
    assert ["list", "--json"] in captured
