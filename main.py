"""Omni Executor — a Lua code editor in a pywebview window.

Setup:
    pip install pywebview

Run:
    python main.py

Platform notes (pywebview picks the native backend automatically):
    - Windows: uses WebView2 / EdgeChromium (preinstalled on Windows 10/11).
    - macOS:   uses Cocoa / WKWebView (built into the OS).
    - Linux:   needs GTK WebKit:  sudo apt install python3-gi gir1.2-webkit2-4.1
               (or install the Qt backend instead: pip install pywebview[qt])
"""

import json
import os
import sys
from pathlib import Path

import webview

APP_NAME = "omni-executor"
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

DEFAULT_SETTINGS = {"theme": "dark"}


def config_dir() -> Path:
    """Per-user config directory following each OS's convention."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    directory = base / APP_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


SETTINGS_FILE = config_dir() / "settings.json"


class Api:
    """Methods exposed to JavaScript as window.pywebview.api.*"""

    def get_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        return {**DEFAULT_SETTINGS, **saved}

    def save_settings(self, settings):
        current = self.get_settings()
        if isinstance(settings, dict):
            current.update(settings)
        # Write to a temp file first so a crash mid-write can't corrupt settings.
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        tmp.replace(SETTINGS_FILE)
        return current


def main():
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        sys.exit(f"Frontend not found: {index}")

    webview.create_window(
        "Omni Executor",
        url=str(index),
        js_api=Api(),
        width=1024,
        height=720,
        min_size=(680, 460),
        background_color="#14151d",  # matches the dark theme, prevents a white flash on startup
    )
    webview.start()


if __name__ == "__main__":
    main()
