"""windowchrome: the pure parts of the native-titlebar glue, plus the editor
state round-trip main.py persists for the tabbed editor."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import windowchrome  # noqa: E402


def test_whole_window_is_content_when_not_maximised():
    # No caption, no borders: the frame styles exist for the OS's benefit
    # (snap, shadows, double-click), the client rect is the window rect.
    assert windowchrome.client_insets(maximized=False, frame_x=8, frame_y=8) == (0, 0, 0, 0)


def test_maximised_window_keeps_one_frame_on_every_side():
    # A maximised window hangs off-screen by a frame; keep that much so the
    # titlebar's first rows and the edges stay visible.
    assert windowchrome.client_insets(maximized=True, frame_x=8, frame_y=9) == (8, 9, 8, 9)


def test_frame_styles_are_the_ones_snap_and_double_click_need():
    s = windowchrome.FRAME_STYLES
    assert s & windowchrome.WS_CAPTION and s & windowchrome.WS_THICKFRAME
    assert s & windowchrome.WS_MAXIMIZEBOX and s & windowchrome.WS_MINIMIZEBOX


@pytest.mark.parametrize("edge,code", [
    ("caption", 2), ("top", 12), ("top-left", 13), ("top-right", 14),
    ("left", 10), ("right", 11), ("bottom", 15),
])
def test_edges_map_to_win32_hit_test_codes(edge, code):
    assert windowchrome.hit_test_for(edge) == code


def test_unknown_edge_is_refused():
    assert windowchrome.hit_test_for("diagonal") is None
    assert windowchrome.hit_test_for(None) is None


def test_gdk_edges_cover_every_resize_edge():
    assert set(windowchrome.GDK_EDGE) == set(windowchrome.EDGE_HIT_TEST) - {"caption"}


@pytest.mark.skipif(not windowchrome.IS_WIN, reason="WinForms backend only")
def test_windows_chrome_refuses_unknown_edge_without_posting(monkeypatch):
    chrome = windowchrome.WindowsChrome(hwnd=1)
    posted = []
    monkeypatch.setattr(chrome._user32, "PostMessageW", lambda *a: posted.append(a))
    assert chrome.begin_drag("nowhere") is False
    assert posted == []
    assert chrome.begin_drag("top") is True
    assert posted[0][1:3] == (windowchrome.WM_OMNI_BEGIN_DRAG, 12)


def test_editor_state_round_trip(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "EDITOR_FILE", tmp_path / "editor.json")
    api = main.Api()
    assert api.get_editor_state() is None
    state = {"activeId": "a", "tabs": [{"id": "a", "name": "farm.lua", "content": "print(1)"}]}
    assert api.save_editor_state(state) == {"ok": True}
    assert api.get_editor_state() == state
    assert json.loads((tmp_path / "editor.json").read_text()) == state


def test_editor_state_rejects_non_object(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "EDITOR_FILE", tmp_path / "editor.json")
    assert main.Api().save_editor_state("nope")["ok"] is False
    assert not (tmp_path / "editor.json").exists()


def test_default_tab_is_home():
    import main

    assert main.DEFAULT_SETTINGS["activeTab"] == "home"
