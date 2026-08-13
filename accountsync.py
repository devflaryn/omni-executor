"""Two-way sync between omnidroid's local account store and the cloud one.

The local store is `<runtime>/accounts.json`, owned by omnidroid
(`omnidroid/accounts.py`): one file, keyed by Roblox username, holding the
`.ROBLOSECURITY` cookie for each account. This module is the ONLY place in
omni-executor that touches it, and it reuses omnidroid's own reader/writer
rather than reimplementing the schema — the file is shared state between two
programs, and two independent notions of its format is how such a file rots.

Sync rules, deliberately simple because the conflict case is rare and the
wrong answer is expensive (a stale cookie fails minutes later, inside a VM):

  * Identical cookies on both sides are left alone. This is decided by
    comparing sha256 of the PLAINTEXT, not the stored bytes: the cloud copy is
    sealed with a fresh nonce on every write, so a ciphertext comparison would
    always differ and the two stores would push and pull the same cookie
    forever.
  * An account that exists on only one side is copied to the other.
  * A cookie that genuinely differs keeps the NEWER one, by timestamp. The
    local store records `saved`; the cloud records `cookieUpdatedAt`.
  * Metadata (place id, custom name) travels with the cookie in one record.

A sync never deletes an account. A sync that can delete turns "I signed in on
a fresh machine" into "my accounts are gone". Signing OUT is different — see
forget_user() — because the local store is per-machine while ownership is
per-user, and the next person to sign in here must not inherit the last one's
cookies.
"""

import hashlib
import sys
import time
from pathlib import Path

import cloud


def _local_store_root() -> Path:
    """Where omnidroid keeps accounts.json for THIS install.

    bootstrap.configure_engine() sets OMNI_DATA_DIR for the whole process, so
    reading it here means this module and every engine subprocess agree on one
    store without passing paths around.
    """
    import os
    root = os.environ.get("OMNI_DATA_DIR")
    if root:
        return Path(root)
    import bootstrap
    return bootstrap.runtime_dir()


def _omnidroid_accounts():
    """omnidroid's accounts module.

    Frozen builds bundle the whole omnidroid package (see the .spec), so the
    import just works. Running from source, the sibling checkout is not on
    sys.path — main.py's engine fallback shells out with PYTHONPATH set, but
    an in-process import needs the path added here.
    """
    try:
        from omnidroid import accounts as mod
        return mod
    except ImportError:
        sibling = Path(__file__).resolve().parent.parent / "omnidroid"
        if (sibling / "omnidroid" / "accounts.py").is_file():
            sys.path.insert(0, str(sibling))
            from omnidroid import accounts as mod
            return mod
        raise


def read_local():
    """Every locally-stored account INCLUDING its cookie, keyed by username.

    Cookies are read here — and only here — because pushing them to the user's
    own cloud account is the entire point. They never go to stdout, argv or a
    log; omnidroid's own `--json` output still refuses to emit them.
    """
    mod = _omnidroid_accounts()
    root = _local_store_root()
    out = {}
    for row in mod.list_accounts(str(root)):
        name = row.get("username")
        if not name:
            continue
        full = mod.get_account(str(root), name) or {}
        out[name] = {
            "username": name,
            "user_id": full.get("user_id"),
            "display_name": full.get("display_name"),
            "custom_name": full.get("custom_name"),
            "place_id": full.get("place_id"),
            "group": full.get("group"),
            "notes": full.get("notes"),
            "cookie": full.get("cookie"),
            "saved": full.get("saved") or 0,
        }
    return out


def write_local(username, cookie, user_id=None, display_name=None, **fields):
    """Upsert one account into the local store, cookie included."""
    mod = _omnidroid_accounts()
    root = str(_local_store_root())
    mod.save_account(root, username, cookie, user_id=user_id, display_name=display_name)
    settable = {k: v for k, v in fields.items()
                if k in ("place_id", "base", "proxy", "group", "notes") and v is not None}
    if settable:
        try:
            mod.set_fields(root, username, **settable)
        except ValueError:
            pass          # a bad place_id from the server must not break the sync
    if fields.get("custom_name"):
        mod.set_custom_name(root, username, fields["custom_name"])


def _hash(cookie):
    """sha256 of a cookie — the comparison key both stores agree on."""
    return hashlib.sha256(cookie.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- ownership
#
# WHICH Omni user each locally-stored account belongs to.
#
# The local store is per-MACHINE; ownership is per-USER. Without this, signing
# out and signing in as somebody else on the same computer uploaded the
# previous user's accounts to the new one — the first sync pushed everything it
# found on disk. Caught in the multi-device acceptance run: the second user
# ended up owning all four accounts, cookies included.
#
# A sidecar file rather than a field on the record, because accounts.json is
# omnidroid's schema (with a fixed settable-field whitelist) and this is
# omni-executor's concern alone. An account with no entry here has never been
# synced — someone added it on this machine, so it belongs to whoever is
# signed in when it is first pushed.
OWNERS_FILE = "cloud-owners.json"


def _owners_path():
    return _local_store_root() / OWNERS_FILE


def _read_owners():
    import json
    try:
        data = json.loads(_owners_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_owners(mapping):
    import json
    import os
    path = _owners_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _current_user_id():
    return (cloud.auth() or {}).get("userId") or ""


def claim(username, user_id=None):
    """Record that `username` belongs to an Omni user."""
    owners = _read_owners()
    owners[username] = user_id or _current_user_id()
    _write_owners(owners)


def owned_here(username, user_id=None):
    """May the signed-in user push this local account to their cloud store?

    True when it is theirs, or when nobody has claimed it yet (added on this
    machine and never synced). False when it demonstrably belongs to a
    DIFFERENT Omni user — that is the leak this exists to stop.
    """
    owner = _read_owners().get(username)
    return not owner or owner == (user_id or _current_user_id())


def forget_user(user_id):
    """Drop the local copies of everything a departing user pulled down.

    Safe: these records came FROM the cloud and come back on their next
    sign-in. It keeps the next person to use this machine from finding — or
    re-uploading — someone else's cookies. Accounts nobody has claimed are
    left alone; they exist only here.
    """
    mod = _omnidroid_accounts()
    root = str(_local_store_root())
    owners = _read_owners()
    removed = []
    for name, owner in list(owners.items()):
        if owner and owner == user_id:
            if mod.remove_account(root, name):
                removed.append(name)
            del owners[name]
    _write_owners(owners)
    return removed


def _cloud_ts(row):
    stamp = row.get("cookieUpdatedAt") or row.get("updatedAt")
    if not stamp:
        return 0.0
    # ISO-8601 with a trailing Z; fromisoformat only learned to parse that in
    # 3.11, and this app also runs on older interpreters in dev.
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def sync(progress=None):
    """Reconcile both stores. Returns a summary dict; raises CloudError if the
    server cannot be reached (the caller decides whether that is fatal)."""
    def say(msg):
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001 — progress is decoration, never fatal
                pass

    user_id = _current_user_id()
    local = read_local()
    say(f"[sync] {len(local)} account(s) on this machine")
    remote_rows = cloud.list_accounts()
    remote = {r["username"]: r for r in remote_rows if r.get("username")}
    say(f"[sync] {len(remote)} account(s) in your Omni account")

    pushed, pulled, skipped, foreign = [], [], [], []

    # ---- local -> cloud
    to_push = []
    for name, rec in local.items():
        if not rec.get("cookie"):
            continue
        if not owned_here(name, user_id):
            # Left here by a different Omni user who was signed in on this
            # machine. Uploading it would hand them over to whoever signs in
            # next, which is the exact opposite of the point.
            foreign.append(name)
            continue
        far = remote.get(name)
        if far and far.get("cookieHash") == _hash(rec["cookie"]):
            continue                      # same cookie already up there
        if far and far.get("hasCookie") and _cloud_ts(far) >= float(rec.get("saved") or 0):
            continue                      # a DIFFERENT cloud cookie, and it is newer
        to_push.append({
            "username": name,
            "cookie": rec["cookie"],
            "userId": rec.get("user_id"),
            "displayName": rec.get("display_name"),
            "customName": rec.get("custom_name"),
            "placeId": str(rec["place_id"]) if rec.get("place_id") else None,
            "group": rec.get("group"),
            "notes": rec.get("notes"),
        })
    if to_push:
        say(f"[sync] uploading {len(to_push)} account(s)")
        remote_rows = cloud.push_accounts(to_push)
        remote = {r["username"]: r for r in remote_rows if r.get("username")}
        pushed = [r["username"] for r in to_push]
        for rec in to_push:
            claim(rec["username"], user_id)      # now demonstrably theirs

    # ---- cloud -> local
    for name, far in remote.items():
        near = local.get(name)
        if near and near.get("cookie") and far.get("cookieHash") == _hash(near["cookie"]):
            continue                      # already identical here
        if (near and near.get("cookie")
                and float(near.get("saved") or 0) >= _cloud_ts(far)):
            continue                      # local copy is newer; the push above sent it
        if not far.get("hasCookie"):
            skipped.append(name)
            continue
        try:
            cookie = cloud.get_cookie(name)
        except cloud.CloudError as e:
            say(f"[sync] {name}: {e.message}")
            skipped.append(name)
            continue
        if not cookie:
            skipped.append(name)
            continue
        write_local(
            name, cookie,
            user_id=far.get("userId"),
            display_name=far.get("displayName"),
            custom_name=far.get("customName"),
            place_id=far.get("placeId"),
            group=far.get("group"),
            notes=far.get("notes"),
        )
        claim(name, user_id)
        pulled.append(name)

    if foreign:
        say(f"[sync] left {len(foreign)} account(s) belonging to another "
            f"Omni user alone: {', '.join(foreign)}")
    say(f"[sync] done — {len(pushed)} up, {len(pulled)} down")
    return {
        "ok": True,
        "pushed": pushed,
        "pulled": pulled,
        "skipped": skipped,
        "foreign": foreign,
        "accounts": list(remote.values()),
        "at": time.time(),
    }


def presence_map():
    """username -> presence dict, straight from the server. The instances list
    merges this over the engine's local view so an account running on another
    machine is labelled as such instead of looking stopped."""
    out = {}
    for row in cloud.list_accounts():
        if row.get("username"):
            out[row["username"]] = row.get("presence") or {}
    return out
