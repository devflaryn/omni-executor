"""Omni account layer: sign-in, the cloud account store, and presence.

Everything in this module talks to omni-backend (`/api/v1/...`). It exists
because three things stopped being per-machine facts:

  * WHO you are. The app is licensed, so it needs a login — register with
    email + password + a license key, sign in with email + password.
  * WHICH Roblox accounts are yours. Cookies used to live only in the local
    omnidroid `accounts.json`, which made the *computer* the unit of ownership.
    They now live (encrypted) against your Omni user, so signing in on another
    machine brings the same accounts with you.
  * WHERE an account is running. One user, several machines: the instances list
    has to be able to say "Running on Mac mini" instead of claiming an account
    is running on the machine you are looking at.

Storage on disk (per-user config dir, same place as settings.json):
    auth.json    {token, email, subscription, saved}   — deleted on sign-out
    device.json  {deviceId, deviceName}                — survives sign-out

The token is a bearer credential and the local account store holds
`.ROBLOSECURITY` cookies, so neither is ever logged or put in argv.
"""

import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Default public backend. Overridable with OMNI_API_BASE (env) or an
# "apiBase" value in settings.json, which is how a dev points the app at a
# local server without a rebuild.
DEFAULT_API_BASE = "http://72.62.59.232"

HTTP_TIMEOUT = 20


class CloudError(Exception):
    """A failure worth showing the user verbatim."""

    def __init__(self, message, status=None, error=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.error = error


# ------------------------------------------------------------------ identity

def _config_dir() -> Path:
    """Same per-user config dir main.py uses (duplicated here so this module
    can be imported and tested without importing the GUI)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    d = base / "omni-executor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    """Atomic write, 0600 where the OS has POSIX modes — auth.json holds a
    bearer token."""
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Windows
    tmp.replace(path)


def auth_file() -> Path:
    return _config_dir() / "auth.json"


def device_file() -> Path:
    return _config_dir() / "device.json"


def _default_device_name() -> str:
    """The friendliest name this machine answers to.

    macOS keeps a human-chosen "Computer Name" ("Mac mini") that is not the
    hostname ("macmini.local"), and that human name is exactly what the
    instances list wants to print in "Running on ...". Everywhere else the
    hostname is the best available answer.
    """
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["scutil", "--get", "ComputerName"],
                                 capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return platform.node() or sys.platform


def device() -> dict:
    """This installation's stable identity, minted once and reused.

    The id must NOT be derived from the hostname: two machines can share one,
    and a rename would silently look like a different device and strand a
    running lease. A random uuid4 written to disk is stable across renames,
    upgrades and sign-outs.
    """
    d = _read_json(device_file(), {})
    changed = False
    if not d.get("deviceId"):
        d["deviceId"] = uuid.uuid4().hex
        changed = True
    if not d.get("deviceName"):
        d["deviceName"] = _default_device_name()
        changed = True
    if changed:
        _write_json(device_file(), d)
    return d


def set_device_name(name):
    d = device()
    d["deviceName"] = (name or "").strip() or _default_device_name()
    _write_json(device_file(), d)
    return d


def auth() -> dict:
    return _read_json(auth_file(), {})


def token():
    """The bearer token, or None.

    Deliberately unannotated: this module has to import on the Mac's system
    Python 3.9, where `str | None` in an annotation is evaluated at def time
    and raises TypeError.
    """
    return auth().get("token") or None


def signed_in() -> bool:
    return bool(token())


def _save_auth(data):
    _write_json(auth_file(), data)


def sign_out():
    """Forget the token. The device identity and the local omnidroid account
    store are deliberately left alone: signing out is not "wipe this machine",
    and the next user to sign in gets their own accounts pulled down anyway."""
    try:
        auth_file().unlink()
    except FileNotFoundError:
        pass
    return {"ok": True}


# --------------------------------------------------------------------- HTTP

def _quote_header(value):
    """Make an arbitrary Unicode string safe as an HTTP header value.

    urllib.parse.quote with a conservative safe set: the result is pure ASCII,
    and the server percent-decodes it back. Kept symmetrical with the backend's
    decodeURIComponent, so a plain ASCII name survives untouched in both
    directions.
    """
    from urllib.parse import quote
    return quote(value or "", safe="")


def api_base(settings=None) -> str:
    base = os.environ.get("OMNI_API_BASE")
    if not base and isinstance(settings, dict):
        candidate = settings.get("apiBase")
        if isinstance(candidate, str) and candidate.strip():
            base = candidate.strip()
    return (base or DEFAULT_API_BASE).rstrip("/")


def _headers(with_auth=True):
    dev = device()
    h = {
        "Content-Type": "application/json",
        # Arcjet's bot detection rejects a request with no user-agent outright,
        # and urllib does not send one by default.
        "User-Agent": "OmniExecutor/1.0",
        "X-Omni-Device-Id": dev["deviceId"],
        # PERCENT-ENCODED, because HTTP header values are latin-1 and a device
        # name is whatever the machine is called. The Mac's is "Berat’ın Mac
        # mini" — a U+2019 apostrophe and a dotless i — and sending it raw made
        # http.client raise UnicodeEncodeError before the request left the box,
        # i.e. sign-in failed outright on that machine. The server decodes it.
        "X-Omni-Device-Name": _quote_header(dev["deviceName"]),
        "X-Omni-Device-Os": sys.platform,
    }
    if with_auth:
        tok = token()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
    return h


def request(method, path, payload=None, base=None, with_auth=True, timeout=HTTP_TIMEOUT):
    """One JSON round trip. Raises CloudError with the server's own message.

    A non-2xx body is still JSON from this backend, so the error text the user
    sees is the server's ("That key is already redeemed"), not a bare HTTP code.
    """
    url = f"{base or api_base()}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(with_auth), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        raise CloudError(
            parsed.get("message") or parsed.get("error") or f"Server error ({e.code})",
            status=e.code,
            error=parsed.get("error"),
        ) from None
    except urllib.error.URLError as e:
        raise CloudError(f"Couldn't reach {url}: {e.reason}") from None
    except OSError as e:
        raise CloudError(f"Couldn't reach {url}: {e}") from None
    return json.loads(body) if body.strip() else {}


# --------------------------------------------------------------- auth flows

def register(email, password, key):
    res = request("POST", "/api/v1/auth/sign-up",
                  {"email": email, "password": password, "key": key},
                  with_auth=False)
    return _adopt(res)


def login(email, password):
    res = request("POST", "/api/v1/auth/sign-in",
                  {"email": email, "password": password}, with_auth=False)
    return _adopt(res)


def _adopt(res):
    data = res.get("data") or {}
    tok = data.get("token")
    if not tok:
        raise CloudError("The server did not return a session token")
    saved = {
        "token": tok,
        "email": (data.get("user") or {}).get("email"),
        "userId": str((data.get("user") or {}).get("_id") or ""),
        "subscription": data.get("subscription") or {},
        "saved": time.time(),
    }
    _save_auth(saved)
    return {k: v for k, v in saved.items() if k != "token"}


def me():
    """Re-check the session and the plan. Also the app's liveness probe for the
    backend, so it must distinguish 'your token is bad' (sign in again) from
    'the server is unreachable' (stay signed in, work offline)."""
    res = request("GET", "/api/v1/auth/me")
    data = res.get("data") or {}
    cur = auth()
    cur["email"] = (data.get("user") or {}).get("email") or cur.get("email")
    cur["subscription"] = data.get("subscription") or {}
    if cur.get("token"):
        _save_auth(cur)
    return {"email": cur.get("email"), "subscription": cur.get("subscription")}


# ----------------------------------------------------- cloud account store

def list_accounts():
    res = request("GET", "/api/v1/accounts")
    return (res.get("data") or {}).get("accounts") or []


def get_cookie(username):
    res = request("GET", f"/api/v1/accounts/{username}/cookie")
    return (res.get("data") or {}).get("cookie")


def push_account(username, cookie=None, user_id=None, **fields):
    payload = {k: v for k, v in fields.items() if v is not None}
    if cookie:
        payload["cookie"] = cookie
    if user_id is not None:
        payload["userId"] = user_id
    return request("PUT", f"/api/v1/accounts/{username}", payload)


def push_accounts(records):
    """Bulk upsert. `records` are {username, cookie, userId, ...} dicts."""
    if not records:
        return []
    res = request("POST", "/api/v1/accounts/sync", {"accounts": records})
    return (res.get("data") or {}).get("accounts") or []


def delete_account(username):
    return request("DELETE", f"/api/v1/accounts/{username}")


def set_state(username, state, mode=None, place_id=None):
    payload = {"state": state}
    if mode:
        payload["mode"] = str(mode)
    if place_id:
        payload["placeId"] = str(place_id)
    return request("POST", f"/api/v1/accounts/{username}/state", payload, timeout=10)
