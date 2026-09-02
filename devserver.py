"""DEV MODE — send calls bound for the production server to a local omni-backend.

The app talks to exactly one server, `http://179.198.197.7`, through three bases
that were each hardcoded separately:

    cloud.api_base()      /api/v1/...        auth, the cloud account store, presence
    bootstrap.dist_base() /omni/dist/...     the update manifest and every blob
    main.Api._exec_base() /omni/exec/...     the OMNI-EXEC remote-execute bridge

Dev mode rewrites the ORIGIN of any URL whose host is that production IP so it
lands on omni-backend running on this machine (`npm run dev`, PORT=5500)
instead. Nothing else is touched: a URL that already points somewhere else --
Google's platform-tools zip, a hand-set `apiBase` in settings.json -- goes out
exactly as written. That is the whole feature; it changes where requests go and
nothing about what they contain.

TURNING IT ON (either is enough, env wins):

    set OMNI_DEV_SERVER=1                    -> http://127.0.0.1:5500
    set OMNI_DEV_SERVER=http://10.0.0.4:5500 -> that, for a backend on the LAN

    %APPDATA%\\omni-executor\\dev.json:
        {"devServer": "http://127.0.0.1:5500"}

The Server row in Settings prints whatever `apiBase` resolved to, so a session
that is redirected says so on screen -- there is no silent dev mode.

WHY THIS FILE IS NOT IN A RELEASE BUILD. Both PyInstaller specs list
`devserver` in `excludes`, so the module is absent from the shipped bundle and
every call site imports it in a try/except that leaves the production base
alone when it is missing. A customer therefore cannot switch this on with an
env var, a settings key, or a dropped-in dev.json -- there is no code in their
copy to switch. Build a dev bundle that KEEPS it with:

    set OMNI_DEV_BUILD=1
    .\\build-windows.ps1

which is the point of the feature: exercising a real frozen build, updater and
all, against a local backend before anything is published.
"""

import json
import os
import sys
import urllib.parse
from pathlib import Path

# The one production origin. Everything the app fetches from us hangs off it.
PROD_HOST = "179.198.197.7"

# omni-backend's dev port (.env.development.local: PORT=5500).
DEFAULT_DEV_SERVER = "http://127.0.0.1:5500"

DEV_ENV = "OMNI_DEV_SERVER"
DEV_FILE = "dev.json"

# Env values that mean "on, at the default" rather than naming a URL.
_ON = ("1", "true", "yes", "on")


def _config_dir() -> Path:
    """The per-user config dir main.py and cloud.py use. Inlined rather than
    imported: this module must not import either of them, because both import
    it."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "omni-executor"


def _normalize(value):
    """A user-typed dev server -> a usable origin, or None.

    Accepts "1" (the default backend), "127.0.0.1:5500" (scheme implied) and a
    full "http://host:port". A trailing slash is dropped so callers can keep
    concatenating paths the way they always have.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    if v.lower() in _ON:
        return DEFAULT_DEV_SERVER
    if v.lower() in ("0", "false", "no", "off"):
        return None
    if "://" not in v:
        v = "http://" + v
    parts = urllib.parse.urlsplit(v)
    if not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def dev_server():
    """The local backend this machine is pointed at, or None when dev mode is
    off. Env first so a single shell can run one redirected app without
    changing what every other app on the box does."""
    from_env = _normalize(os.environ.get(DEV_ENV))
    if from_env:
        return from_env
    if str(os.environ.get(DEV_ENV, "")).strip().lower() in ("0", "false", "no", "off"):
        return None          # an explicit off in the env beats the file
    try:
        data = json.loads((_config_dir() / DEV_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("devMode") is False:
        return None
    return _normalize(data.get("devServer"))


def enabled():
    return dev_server() is not None


def redirect(url):
    """Swap the production origin for the dev one, path and query intact.

    Matches on HOSTNAME, not on a string prefix, so it catches the base with or
    without a trailing slash, over http or https, on any port. A URL pointed at
    any other host is returned untouched -- this must never capture Google's
    platform-tools download or a deliberately overridden apiBase.
    """
    if not isinstance(url, str) or not url:
        return url
    dev = dev_server()
    if not dev:
        return url
    parts = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    if parts.hostname != PROD_HOST:
        return url
    d = urllib.parse.urlsplit(dev)
    return urllib.parse.urlunsplit(
        (d.scheme, d.netloc, parts.path, parts.query, parts.fragment)).rstrip("/")
