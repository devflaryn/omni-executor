"""Native window behaviour behind the app's own titlebar.

The titlebar is drawn by the frontend (VS Code / Discord style), but the
WINDOW stays the operating system's: resize borders, snap, double-click,
shadows, the minimise animation, the taskbar thumbnail. This module is the
small amount of per-platform glue that makes those two facts compatible.

Windows
    pywebview's `frameless=True` sets FormBorderStyle.None, which throws the
    whole non-client area away -- and with it every OS behaviour above. This
    module puts the frame STYLES back on the HWND (WS_CAPTION, WS_THICKFRAME,
    the min/max boxes), which is what snap, Win+arrows, double-click, DWM
    shadows and rounded corners key off, and answers WM_NCCALCSIZE so the
    client area is still the whole window (what Chromium, Electron and VS Code
    do). WinForms keeps believing there is no non-client area, which matters:
    it converts client sizes to window sizes on every restore, and if its idea
    of the frame differed from the OS's the window would grow by a caption
    each time (measured: +31 px per maximise/restore with a Sizable form).
    Drag and edge resizes start the native loops by posting WM_NCLBUTTONDOWN
    with a hit-test code, so a drag to the top edge maximises.

macOS
    pywebview's frameless window is still a titled window with a transparent,
    full-size-content titlebar; main.py re-shows the traffic lights. Drag is
    handed to AppKit (`performWindowDragWithEvent:`), double-click honours the
    user's "double-click a window's title bar to" preference.

Linux (GTK)
    The undecorated window asks the window manager to move/resize it
    (`begin_move_drag` / `begin_resize_drag`), which keeps edge-snapping.

Every public function is best-effort: a failure here must never take the app
down, it only costs a window gesture.
"""
from __future__ import annotations

import ctypes
import sys

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Win32 hit-test codes. The names are what the frontend sends.
HTCAPTION = 2
EDGE_HIT_TEST = {
    "caption": HTCAPTION,
    "left": 10,
    "right": 11,
    "top": 12,
    "top-left": 13,
    "top-right": 14,
    "bottom": 15,
    "bottom-left": 16,
    "bottom-right": 17,
}

# GDK window edges (Gdk.WindowEdge), by the same names.
GDK_EDGE = {
    "top-left": 0,
    "top": 1,
    "top-right": 2,
    "left": 3,
    "right": 4,
    "bottom-left": 5,
    "bottom": 6,
    "bottom-right": 7,
}

WM_NCCALCSIZE = 0x0083
WM_NCLBUTTONDOWN = 0x00A1
WM_SYSCOMMAND = 0x0112
WM_APP = 0x8000
# Private message: "start the native move/size loop for hit-test wParam".
# Posted from the JS-bridge thread, handled on the UI thread, where the
# capture the WebView took on mousedown can be released first.
WM_OMNI_BEGIN_DRAG = WM_APP + 0x41

SC_MAXIMIZE = 0xF030
SC_RESTORE = 0xF120
GWLP_WNDPROC = -4
SWP_FRAMECHANGED = 0x0020
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
SM_CXFRAME = 32
SM_CYFRAME = 33
SM_CXPADDEDBORDER = 92
GWL_STYLE = -16
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_CAPTION = 0x00C00000
FRAME_STYLES = WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU


def client_insets(maximized: bool, frame_x: int, frame_y: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) to take off the window rect for the client.

    Normally nothing: the whole window is content, the frame styles exist for
    the OS's benefit only. A MAXIMISED window hangs off the screen by one
    frame on every side (that is how Windows hides the resize borders), so
    leave that much back or the titlebar's first rows are drawn off-screen."""
    if not maximized:
        return (0, 0, 0, 0)
    return (frame_x, frame_y, frame_x, frame_y)


def hit_test_for(edge: str) -> int | None:
    """The Win32 hit-test code for a frontend edge name, or None."""
    return EDGE_HIT_TEST.get(str(edge))


# --------------------------------------------------------------------- Windows

if IS_WIN:
    from ctypes import wintypes

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class _NCCALCSIZE_PARAMS(ctypes.Structure):
        _fields_ = [("rgrc", _RECT * 3), ("lppos", ctypes.c_void_p)]

    _WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
    )


class WindowsChrome:
    """Subclasses the WinForms window procedure. Create on the UI thread once
    the window is shown (`install`); `begin_drag` / `double_click` /
    `is_maximized` may be called from any thread."""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self._user32 = ctypes.windll.user32
        self._proc = None
        self._original = None
        u = self._user32
        # 64-bit exports SetWindowLongPtrW; 32-bit only has SetWindowLongW.
        self._set_long = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        self._set_long.restype = ctypes.c_void_p
        self._set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        u.CallWindowProcW.restype = ctypes.c_ssize_t
        u.CallWindowProcW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
        ]
        u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        u.IsZoomed.argtypes = [ctypes.c_void_p]
        u.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_uint,
        ]

    # -- UI thread ----------------------------------------------------------

    def install(self):
        """Put the frame styles back, take over WndProc, and force a frame
        recalculation so the change lands immediately. Idempotent."""
        if self._proc is not None:
            return
        u = self._user32
        get_long = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        get_long.restype = ctypes.c_void_p
        get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
        style = int(get_long(self.hwnd, GWL_STYLE) or 0)
        self._set_long(self.hwnd, GWL_STYLE, ctypes.c_void_p(style | FRAME_STYLES))
        self._proc = _WNDPROC(self._wndproc)  # keep alive: the OS holds a raw pointer
        self._original = self._set_long(
            self.hwnd, GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p).value
        )
        u.SetWindowPos(
            self.hwnd, None, 0, 0, 0, 0,
            SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
            | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
        )

    def _frame(self) -> tuple[int, int]:
        u = self._user32
        try:
            dpi = u.GetDpiForWindow(self.hwnd)
            pad = u.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi)
            return (u.GetSystemMetricsForDpi(SM_CXFRAME, dpi) + pad,
                    u.GetSystemMetricsForDpi(SM_CYFRAME, dpi) + pad)
        except AttributeError:  # pre-1607 Windows 10
            pad = u.GetSystemMetrics(SM_CXPADDEDBORDER)
            return (u.GetSystemMetrics(SM_CXFRAME) + pad, u.GetSystemMetrics(SM_CYFRAME) + pad)

    def _call_original(self, hwnd, msg, wparam, lparam):
        return self._user32.CallWindowProcW(self._original, hwnd, msg, wparam, lparam)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_NCCALCSIZE:
                # Not handed to the original: DefWindowProc would inset the
                # caption and borders, and the whole point is that it must not.
                if wparam:
                    rect = ctypes.cast(lparam, ctypes.POINTER(_NCCALCSIZE_PARAMS)).contents.rgrc[0]
                    fx, fy = self._frame()
                    left, top, right, bottom = client_insets(bool(self._user32.IsZoomed(hwnd)), fx, fy)
                    rect.left += left
                    rect.top += top
                    rect.right -= right
                    rect.bottom -= bottom
                return 0
            if msg == WM_OMNI_BEGIN_DRAG:
                # The WebView captured the mouse on mousedown; the move/size
                # loop needs it. Same thread, so this release is the real one.
                self._user32.ReleaseCapture()
                self._user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, wparam, 0)
                return 0
        except Exception:  # noqa: BLE001 -- never let a gesture take the window down
            pass
        return self._call_original(hwnd, msg, wparam, lparam)

    # -- any thread ---------------------------------------------------------

    def begin_drag(self, edge: str = "caption") -> bool:
        code = hit_test_for(edge)
        if code is None:
            return False
        self._user32.PostMessageW(self.hwnd, WM_OMNI_BEGIN_DRAG, code, 0)
        return True

    def is_maximized(self) -> bool:
        return bool(self._user32.IsZoomed(self.hwnd))

    def double_click(self) -> bool:
        """What the OS does for a double-click on a caption: maximise, or
        restore. Through WM_SYSCOMMAND so the native animation plays."""
        cmd = SC_RESTORE if self.is_maximized() else SC_MAXIMIZE
        self._user32.PostMessageW(self.hwnd, WM_SYSCOMMAND, cmd, 0)
        return True


def _winforms_handle(window) -> int | None:
    """The HWND of a pywebview window on the WinForms backend."""
    form = getattr(window, "native", None)
    try:
        return int(form.Handle.ToInt64())
    except Exception:  # noqa: BLE001
        return None


def install_windows(window):
    """Call from the window's `shown` event. Returns the WindowsChrome, or
    None when it could not be installed (the window then simply keeps the
    OS caption, which is ugly but entirely functional)."""
    hwnd = _winforms_handle(window)
    if not hwnd:
        return None
    chrome = WindowsChrome(hwnd)
    form = window.native
    try:
        from System import Action  # pythonnet: marshal to the UI thread

        form.BeginInvoke(Action(chrome.install))
    except Exception:  # noqa: BLE001
        try:
            chrome.install()
        except Exception:  # noqa: BLE001
            return None
    return chrome


# ----------------------------------------------------------------------- macOS


def _on_main_thread(fn):
    import AppKit

    AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


def mac_begin_drag(window) -> bool:
    """Hand the drag to AppKit. `currentEvent` is the mouse-down (or first
    mouse-dragged) that the page forwarded us; either starts a native drag,
    so Spaces, Mission Control and edge behaviour all see a real window move."""
    try:
        import AppKit

        def run():
            try:
                ns = window.native
                event = AppKit.NSApp.currentEvent()
                if ns is not None and event is not None:
                    ns.performWindowDragWithEvent_(event)
            except Exception:  # noqa: BLE001
                pass

        _on_main_thread(run)
        return True
    except Exception:  # noqa: BLE001
        return False


def mac_double_click(window) -> bool:
    """System Settings > Desktop & Dock > "Double-click a window's title bar
    to": Zoom (default), Minimize, Fill, or None."""
    try:
        import AppKit

        def run():
            try:
                ns = window.native
                if ns is None:
                    return
                prefs = AppKit.NSUserDefaults.standardUserDefaults()
                action = prefs.stringForKey_("AppleActionOnDoubleClick") or "Maximize"
                if action == "Minimize":
                    ns.performMiniaturize_(None)
                elif action == "None":
                    return
                else:
                    ns.performZoom_(None)
            except Exception:  # noqa: BLE001
                pass

        _on_main_thread(run)
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------- Linux


def gtk_begin_drag(window, edge: str = "caption") -> bool:
    try:
        from gi.repository import Gdk, GLib

        def run():
            try:
                win = window.native
                pointer = win.get_display().get_default_seat().get_pointer()
                _screen, x, y = pointer.get_position()
                if edge == "caption":
                    win.begin_move_drag(1, x, y, Gdk.CURRENT_TIME)
                elif edge in GDK_EDGE:
                    win.begin_resize_drag(Gdk.WindowEdge(GDK_EDGE[edge]), 1, x, y, Gdk.CURRENT_TIME)
            except Exception:  # noqa: BLE001
                pass
            return False  # one-shot idle

        GLib.idle_add(run)
        return True
    except Exception:  # noqa: BLE001
        return False
