"""Roblox account creation for Omni Executor.

One module, four concerns:

  1. IDENTITY GENERATION — usernames in four styles (`name_no`, `adj_noun`,
     `gamertag`, `stealth`), an 18+ birthday, a secure password and a gender
     pick. Pure functions over `secrets`, so they are unit-testable without a
     browser.

  2. CAPTCHA HANDLING — Roblox's signup is gated by Arkose FunCaptcha. The
     challenge is DETECTED here and handed to the human in the opened window.
     Third-party token solvers used to live here and were removed: Arkose
     will not issue a puzzle at all to a solver's IP (it hangs on "Verifying
     browser..."), so no service can produce a token for Roblox, whatever the
     proxy. Automatic solving is being rebuilt as a server-side vision solver
     that PLAYS the puzzle in this browser, where Arkose does serve one.

  3. THE SIGNUP FLOW — Selenium drives a VISIBLE Chrome: roblox.com ->
     registration page -> random birthday (18+) -> generated username,
     password and gender -> Sign Up -> (captcha) -> land on roblox.com/home ->
     read `.ROBLOSECURITY`. "Taken username" validation errors regenerate and
     retry; every other failure aborts that one account and the batch moves
     on. The browser stays visible because either a human may need to solve
     something, or nobody needs to see anything at all.

  4. THE VAULT — created accounts' password/birthday sidecar
     (`created-accounts.json`, mode 0600). The cookie goes to omnidroid's own
     accounts.json through accountsync (that schema is the engine's); the
     PASSWORD has no home there, so this file is omni-executor's. Cookie +
     username + password together mean an expired cookie can be replayed by
     an automated login instead of losing the account.

Roblox ships its signup form inside its own bundle and changes markup without
notice, so EVERY selector below is a candidate LIST tried in order, plus
structural fallbacks (date selects are classified by their OPTIONS, not their
ids). A selector rotting costs one account attempt with a clear message, not
a crash.
"""

import json
import os
import re
import secrets
import shutil
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# identity generation
# ---------------------------------------------------------------------------

USERNAME_STYLES = {
    # id -> (label, example) shown in the UI dropdown.
    "name_no": ("Name + numbers", "Eric848381 / John_858289"),
    "adj_noun": ("Adjective + noun", "FrozenWolf1192 / Ultra_Hawk948"),
    "gamertag": ("Gamer tag", "Bright_Shark858 / EpicWizard436"),
    "stealth": ("Stealth", "mwu78cnas782n"),
}


FIRST_NAMES = (
    "Eric", "John", "Alex", "Mike", "Chris", "Jake", "Tom", "Sam", "Nick",
    "Ryan", "Matt", "Kevin", "Brian", "Jason", "David", "Mark", "Paul",
    "Luke", "Sean", "Adam", "Kyle", "Evan", "Noah", "Liam", "Owen", "Cole",
    "Blake", "Grant", "Chase", "Trent", "Brett", "Dean", "Reid", "Shane",
    "Seth", "Todd", "Wade", "Zack", "Logan", "Mason",
)

ADJECTIVES = (
    "Frozen", "Ultra", "Silent", "Shadow", "Rapid", "Turbo", "Epic", "Bright",
    "Dark", "Swift", "Mystic", "Iron", "Golden", "Storm", "Thunder", "Cosmic",
    "Savage", "Lucky", "Wild", "Frosty", "Blaze", "Nitro", "Cyber", "Alpha",
    "Nova", "Solar", "Lunar", "Crimson", "Phantom", "Stealth",
)

ANIMAL_NOUNS = (
    "Wolf", "Hawk", "Fox", "Bear", "Tiger", "Dragon", "Eagle", "Panther",
    "Cobra", "Falcon", "Raven", "Shark", "Lion", "Viper", "Bull", "Stag",
)

GAMER_ADJECTIVES = (
    "Bright", "Epic", "Turbo", "Mega", "Hyper", "Prime", "Royal", "Noble",
    "Grand", "Ace",
)

GAMER_NOUNS = (
    "Shark", "Wizard", "Knight", "Ninja", "Sniper", "Ranger", "Slayer",
    "Hunter", "Reaper", "Titan", "Phoenix", "Samurai", "Ghost", "Blade",
)

# Roblox usernames: 3-20 chars of [A-Za-z0-9_], no leading/trailing underscore.
USERNAME_MAX_LEN = 20
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

PASSWORD_SYMBOLS = "!@#$%^&*-_+=?"


def _digits(n):
    return "".join(secrets.choice(string.digits) for _ in range(n))


def _pick(seq):
    return secrets.choice(seq)


def generate_username(style="name_no"):
    """A fresh username in one of USERNAME_STYLES, valid per USERNAME_RE."""
    if style == "name_no":
        name = _pick(FIRST_NAMES)
        sep = "_" if secrets.randbelow(2) else ""
        return f"{name}{sep}{_digits(6)}"
    if style == "adj_noun":
        adj = _pick(ADJECTIVES)
        noun = _pick(ANIMAL_NOUNS)
        sep = "_" if secrets.randbelow(3) == 0 else ""
        return f"{adj}{sep}{noun}{_digits(4)}"
    if style == "gamertag":
        adj = _pick(GAMER_ADJECTIVES)
        noun = _pick(GAMER_NOUNS)
        sep = "_" if secrets.randbelow(2) else ""
        return f"{adj}{sep}{noun}{_digits(3)}"
    if style == "stealth":
        # e.g. mwu78cnas782n — lowercase letters in runs broken up by digit
        # groups; nothing about it suggests a pattern or a person.
        parts = []
        remaining = secrets.choice(range(11, 15))
        letter_next = True
        while remaining > 0:
            if letter_next:
                run = min(remaining, secrets.choice(range(1, 5)))
                parts.append("".join(secrets.choice(string.ascii_lowercase)
                                     for _ in range(run)))
            else:
                run = min(remaining, secrets.choice(range(2, 4)))
                parts.append(_digits(run))
            remaining -= run
            letter_next = not letter_next
        return "".join(parts)
    raise ValueError(f"unknown username style {style!r}")


def valid_username(name):
    return bool(isinstance(name, str) and USERNAME_RE.fullmatch(name))


# The required account-creation age band: adults, 18 through 25.
BIRTHDAY_MIN_AGE = 18
BIRTHDAY_MAX_AGE = 25


def generate_birthday(min_age=BIRTHDAY_MIN_AGE, max_age=BIRTHDAY_MAX_AGE):
    """A random (year, month, day) making the holder between min_age and
    max_age years old today (default 18-25 as required). Roblox asks for a
    birth date and gates features on it; everything this app does wants an
    ADULT of 18+ (and between 18 and 25 by default), so the floor is
    hard-coded at registration time rather than hoped for later."""
    today = date.today()
    # A leap day is only produced when the chosen year actually has one;
    # timedelta arithmetic keeps every generated date real.
    while True:
        year = today.year - secrets.choice(range(min_age, max_age + 1))
        try:
            bday = date(year, secrets.choice(range(1, 13)),
                        secrets.choice(range(1, 29)))
        except ValueError:  # pragma: no cover - range(1,29) is always valid
            continue
        age = today.year - bday.year - ((today.month, today.day) < (bday.month, bday.day))
        if min_age <= age <= max_age:
            return bday.year, bday.month, bday.day


def generate_password(length=16):
    """A password from `secrets` guaranteed to carry upper, lower, digit and
    symbol classes — length 16 clears Roblox's minimum with margin."""
    pools = [string.ascii_uppercase, string.ascii_lowercase,
             string.digits, PASSWORD_SYMBOLS]
    chars = [secrets.choice(p) for p in pools]
    all_chars = "".join(pools)
    chars += [secrets.choice(all_chars) for _ in range(max(8, length) - len(chars))]
    # Fisher-Yates keyed off the CSPRNG, so position leaks nothing either.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def pick_gender():
    return secrets.choice(("male", "female"))


# ---------------------------------------------------------------------------
# creation config validation (shared by main.py Api and the UI contract)
# ---------------------------------------------------------------------------

MIN_AMOUNT, MAX_AMOUNT = 1, 50


def validate_creation_config(amount=1, username_style=None, custom_password=None,
                             captcha_proxy=None):
    """Normalize a creation config from the UI. Returns
    (clean_dict, error_message_or_None). Unknown values are REJECTED rather
    than coerced: a stale settings.json must surface as a message, not as an
    argparse-style surprise three layers down."""
    clean = {}
    try:
        n = int(amount)
    except (TypeError, ValueError):
        return None, "Amount must be a number."
    if not MIN_AMOUNT <= n <= MAX_AMOUNT:
        return None, f"Amount must be between {MIN_AMOUNT} and {MAX_AMOUNT}."
    clean["amount"] = n

    style = username_style if username_style in USERNAME_STYLES else None
    if username_style is not None and style is None:
        return None, f"Unknown username style {username_style!r}."
    clean["usernameStyle"] = style or "name_no"

    if custom_password is None or str(custom_password) == "":
        clean["customPassword"] = None   # generate per account
    elif len(str(custom_password)) < 8:
        return None, "A custom password must be at least 8 characters."
    else:
        clean["customPassword"] = str(custom_password)

    # The proxy is validated HERE so a typo shows up in the modal rather than
    # three layers down as a mystery failure mid-batch. It has nothing to do
    # with captchas any more — it is what gets past Roblox's per-IP signup
    # rate limit, which trips after roughly ten attempts from one address.
    _proxy, proxy_err = parse_proxy(captcha_proxy)
    if proxy_err:
        return None, proxy_err
    clean["captchaProxy"] = str(captcha_proxy or "").strip()
    return clean, None


# ---------------------------------------------------------------------------
# vault: passwords for created accounts (cookie lives in omnidroid's store)
# ---------------------------------------------------------------------------

VAULT_FILE = "created-accounts.json"


class Vault:
    """Sidecar credential store, ONE json file keyed by username, mode 0600 —
    the same discipline omnidroid applies to cookies. Only the fields that
    have nowhere else live here (password, birthday); the cookie itself stays
    in omnidroid's accounts.json so there is exactly one cookie store."""

    def __init__(self, root):
        self._root = Path(root)

    @property
    def path(self):
        return self._root / VAULT_FILE

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "accounts": {}}

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # Windows has no POSIX modes
        os.replace(tmp, self.path)

    def add(self, username, password, birthday=None, style=None):
        data = self._read()
        data["accounts"][username] = {
            "username": username,
            "password": password,
            "birthday": list(birthday) if birthday else None,
            "style": style,
            "createdAt": time.time(),
        }
        self._write(data)
        return True

    def get(self, username):
        rec = self._read()["accounts"].get(username)
        return dict(rec) if rec else None

    def remove(self, username):
        data = self._read()
        existed = username in data["accounts"]
        if existed:
            del data["accounts"][username]
            self._write(data)
        return existed

    def list(self, include_secrets=False):
        out = []
        for name, rec in sorted(self._read()["accounts"].items()):
            row = {"username": rec.get("username") or name,
                   "style": rec.get("style"),
                   "birthday": rec.get("birthday"),
                   "createdAt": rec.get("createdAt")}
            if include_secrets:
                row["password"] = rec.get("password")
            else:
                row["hasPassword"] = bool(rec.get("password"))
            out.append(row)
        return out


# ---------------------------------------------------------------------------
# proxy configuration (signup rate limits, not captchas)
# ---------------------------------------------------------------------------


# Proxy schemes 2captcha accepts for proxyType.
PROXY_TYPES = ("http", "https", "socks4", "socks5")




class CaptchaError(Exception):
    """Raised for any solver-side failure worth showing the user."""


def parse_proxy(text):
    """Normalize a proxy string into 2captcha's task fields.

    Returns (proxy_dict_or_None, error_or_None). Blank is NOT an error — it
    means "no proxy", and proxyless stays the default so an install that never
    configures one behaves exactly as before.

    Every layout the residential dashboards hand out is accepted, because the
    user is going to paste whichever one their provider showed them:

        host:port
        host:port:user:pass
        user:pass@host:port
        scheme://user:pass@host:port

    A password may itself contain ':' (they often do), so the credential split
    only ever consumes the FIRST separator."""
    if text is None:
        return None, None
    raw = str(text).strip()
    if not raw:
        return None, None

    scheme = "http"
    if "://" in raw:
        scheme, _, raw = raw.partition("://")
        scheme = scheme.lower()
        if scheme not in PROXY_TYPES:
            return None, (f"Unsupported proxy scheme {scheme!r} — "
                          f"use one of {', '.join(PROXY_TYPES)}.")
        raw = raw.strip()

    def hostport(s):
        """(host, port) if `s` is a well-formed host:port, else None."""
        host, sep, port_s = s.rpartition(":")
        if not sep or not host.strip() or not port_s.strip().isdigit():
            return None
        return host.strip(), int(port_s)

    login = password = ""
    hp = None

    # Which layout is this? Decided by what actually PARSES, not by which
    # separator appears first: a password may legitimately contain '@'
    # ("host:port:user:p@ss"), and preferring the '@' form on sight would
    # split that into a bogus host.
    if "@" in raw:
        creds, _, tail = raw.rpartition("@")
        hp = hostport(tail)
        if hp:
            login, _, password = creds.partition(":")
    if hp is None:
        parts = raw.split(":")
        if len(parts) >= 4 and hostport(":".join(parts[:2])):
            # host:port:user:pass — the password keeps any further colons.
            hp = hostport(":".join(parts[:2]))
            login = parts[2]
            password = ":".join(parts[3:])
        else:
            hp = hostport(raw)

    if hp is None:
        # Distinguish "no port at all" from "port isn't a number", because
        # the two are different typos.
        _h, sep, port_s = raw.rpartition(":")
        if sep and port_s.strip() and not port_s.strip().isdigit():
            return None, f"Proxy port {port_s.strip()!r} is not a number."
        return None, f"Proxy {text.strip()!r} needs a host and a port."

    host, port = hp
    if not 1 <= port <= 65535:
        return None, f"Proxy port {port} is out of range."

    return {"type": scheme, "address": host, "port": port,
            "login": login.strip(), "password": password.strip()}, None




# ---------------------------------------------------------------------------
# selenium signup flow
# ---------------------------------------------------------------------------

# The registration entry point. `/up/registration` and `/register` were retired
# by Roblox and now 404; the live one is the SPA route `CreateAccount`, which
# redirects `/tr/CreateAccount` etc. for non-English locales. `account/signupredir`
# is Roblox's canonical signup redirect and lands on the same page. Both return
# 200 in every locale, so neither can strand us on a localized 404 page.
SIGNUP_URLS = (
    "https://www.roblox.com/CreateAccount",
    "https://www.roblox.com/account/signupredir",
)
HOME_URL_MARKERS = ("roblox.com/home", "roblox.com/discover")

COOKIE_NAME = ".ROBLOSECURITY"

# Every selector is a candidate list; first match wins. Structural fallbacks
# (classify_date_selects) cover pages where none of them hit.
SEL_USERNAME = ("#signup-username", "input[name='username']",
                "input[placeholder*='username' i]")
SEL_PASSWORD = ("#signup-password", "input[name='password']",
                "input[type='password']")
SEL_ERROR_TEXT = ("[class*='error' i]", "[role='alert']", ".form-control-label")
# Marker shapes an Arkose/FunCaptcha challenge can take on Roblox's signup.
# Probed one by one (never merged into one comma-union) so a single selector
# an older chromedriver refuses cannot blind the whole probe — Roblox funnels
# the widget through nested iframes whose top-level src can be about:blank,
# so no ONE marker is guaranteed.
CAPTCHA_SELECTORS = (
    "iframe[src*='arkose']",
    "iframe[src*='funcaptcha']",
    "iframe[id*='arkose']",
    "iframe[id*='captcha' i]",
    "iframe[title*='verification' i]",
    "iframe[data-e2e='enforcement-frame']",
    "#funcaptcha iframe",
    "div[id*='arkose' i]",
)

MAX_USERNAME_ATTEMPTS = 6       # "taken" regenerations allowed per account
FORM_DEADLINE_S = 120           # budget for loading + filling the form
FORM_MISSING_GRACE_S = 20       # no form/captcha/home for this long = fatal
CAPTCHA_APPEAR_S = 10           # post-submit window for the challenge to mount
CAPTCHA_READY_S = 15            # post-detection window for the iframe to mount
CAPTCHA_MANUAL_TIMEOUT_S = 600  # human-in-the-loop solve window
CAPTCHA_CLEAR_POLLS = 3         # consecutive absent readings = actually cleared
POLL_S = 0.5


def _first(drv, selectors, timeout=10):
    """First element matching any candidate selector, or None."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for sel in selectors:
            try:
                el = drv.find_element("css selector", sel)
                if el.is_displayed():
                    return el
            except Exception:  # noqa: BLE001 - miss is the normal path
                continue
        time.sleep(POLL_S)
    return None


def _month_name(month):
    import calendar
    return calendar.month_name[int(month)]


def _month_abbr(month):
    import calendar
    return calendar.month_abbr[int(month)]  # "Jan".."Dec" — what the <option value> holds


def classify_date_selects(drv):
    """Find the birthday <select>s. First by known ids/names (#MonthDropdown,
    #DayDropdown, #YearDropdown) when present — the stable path for today's
    page — then by structure (what each select CONTAINS) for a markup that
    renames them. Returns (month, day, year) elements or Nones.

    Structural rules: month select has >= 12 options whose TEXT are month
    names; day select has ~28-32 options (a disabled ``Day`` placeholder plus
    the 01..31 days) all numeric-valued; year select has >= 40 options
    dominated by 4-digit values. The day bound is deliberately lenient: the
    placeholder raises the count to 32, so a 28..31 bound would misclassify
    today's real form and the birthday day would never get set."""
    found = {"month": None, "day": None, "year": None}

    def take(slot, el):
        if not found[slot]:
            found[slot] = el

    try:
        selects = drv.find_elements("css selector", "select")
    except Exception:  # noqa: BLE001
        return found

    # (1) Known ids/names — stable on the current page and cheaper to match.
    id_name = {
        "month": ("#MonthDropdown", "select[name='birthdayMonth']"),
        "day": ("#DayDropdown", "select[name='birthdayDay']"),
        "year": ("#YearDropdown", "select[name='birthdayYear']"),
    }
    for slot, sels in id_name.items():
        for sel in sels:
            try:
                for el in drv.find_elements("css selector", sel):
                    take(slot, el)
            except Exception:  # noqa: BLE001
                continue

    # (2) Structural fallback for a page that renames the controls.
    months = {_month_name(m).lower() for m in range(1, 13)}
    for el in selects:
        if all(found.values()):
            break
        try:
            options = el.find_elements("css selector", "option")
        except Exception:  # noqa: BLE001
            continue
        texts = [(o.text or "").strip() for o in options]
        vals = [(o.get_attribute("value") or "").strip() for o in options]
        if (not found["month"] and len(texts) >= 12
                and sum(t.lower() in months for t in texts) >= 12):
            take("month", el)
        elif (not found["day"] and 28 <= len(texts) <= 32
              and sum(v.isdigit() for v in vals) >= len(vals) - 1):
            take("day", el)
        elif (not found["year"] and len(texts) >= 40
              and sum(1 for v in vals if re.fullmatch(r"(19|20)\d\d", v or "")) > len(vals) // 2):
            take("year", el)
    return found


def _select_option(el, matches):
    """Settle a <select> on the first option that matches any of `matches`.

    `matches` is an iterable of strings tried as, in turn: the option's VALUE
    attribute (the abbreviation form Roblox really stores, e.g. "Mar"), then
    the option's visible text ("March"). Either exact match selects the
    option. Returns True if any option was selected."""
    from selenium.webdriver.support.ui import Select
    s = Select(el)
    candidates = [str(m) for m in matches]

    def val(opt):
        try:
            return (opt.get_attribute("value") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def txt(opt):
        try:
            return (opt.text or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    for opt in s.options:
        if val(opt).lower() in {c.lower() for c in candidates}:
            s.select_by_value(val(opt))
            return True
    for opt in s.options:
        if txt(opt).lower() in {c.lower() for c in candidates}:
            s.select_by_visible_text(txt(opt))
            return True
    return False


def fill_birthday(drv, birthday, on_status):
    year, month, day = birthday
    sel = classify_date_selects(drv)
    got = [bool(sel[k]) for k in ("month", "day", "year")]
    if not all(got):
        on_status("[create] birthday dropdowns not fully identified "
                  f"(m/d/y found: {got}); continuing anyway")
    if sel["month"]:
        # The <option value> is the 3-letter abbreviation ("Mar"), while the
        # visible text is the full name ("March") — cover both.
        _select_option(sel["month"], [
            _month_abbr(month), f"{month:02d}", str(month),
            _month_name(month)])
    if sel["day"]:
        _select_option(sel["day"], [f"{day:02d}", str(day)])
    if sel["year"]:
        _select_option(sel["year"], [str(year)])


def fill_field(el, value):
    el.clear()
    el.send_keys(value)


def find_gender_control(drv, gender):
    """The gender picker, whatever shape it takes today. Roblox currently
    renders two icon buttons with no text — ``id="MaleButton"``/``id="FemaleButton"``
    (or a ``title="Male"``/``title="Female"``) — so match those first, then the
    labelled/radio/data-attribute variants the old form used. Best-effort: a
    form without a gender picker at all simply skips the step (it's optional)."""
    label = "Male" if gender == "male" else "Female"
    candidates = [
        (f"#{label}Button", "css"),               # today's page: id="MaleButton"
        (f"button[title='{label}']", "css"),
        (f"[data-gender='{gender}']", "css"),
        (f"#gender-{gender}", "css"),
        (f"input[value='{label}']", "css"),
        (f"//label[normalize-space()='{label}']", "xpath"),
        (f"//button[normalize-space()='{label}']", "xpath"),
        (f"//span[normalize-space()='{label}']/..", "xpath"),
    ]
    for sel, kind in candidates:
        try:
            els = (drv.find_elements("xpath", sel) if kind == "xpath"
                   else drv.find_elements("css selector", sel))
            for el in els:
                if el.is_displayed():
                    return el
        except Exception:  # noqa: BLE001
            continue
    return None


# Submitting the signup form. Roblox localizes the button text (English "Sign
# Up", Turkish "Kaydol", ...), so matching on English text alone would skip the
# click on a localized page and let the whole account time out. Buttons matched
# by their SIGNUP-specific id/data-testid are trusted unconditionally (that id
# is code, not user-facing text); only the generic selector falls back to a
# text/submit heuristic.
SIGNUP_BUTTON_IDS = ("#signup-button", "button[data-testid='signup-button']")

# Words that mark a submit button as the SIGNUP one (not login) in the languages
# the app is likely to hit. An empty/icon-only button also counts.
SIGNUP_MARKS = ("sign up", "signup", "kaydol", "\u00fcye ol", "registre",
                "create account", "cr\u00e9er", "anmelden", "\u00e0ngivelse",
                "crea cuenta", "inscri")


def submit_form(drv, on_status, wait=10.0):
    """Find the Sign Up button and CLICK it once it is enabled.

    Roblox renders the signup button ``disabled`` until the form validates
    asynchronously (username availability check + password length), so we poll
    for enablement before clicking — a click on a disabled button is a silent
    no-op that leaves the form untouched and the whole account waiting on a
    timeout. Prefers a signup-id button; falls back to a generic submit whose
    text/breadcrumb marks it as signup (never a login button)."""
    button = None
    for sel in SIGNUP_BUTTON_IDS:
        try:
            for b in drv.find_elements("css selector", sel):
                if b.is_displayed():
                    button = b
                    break
        except Exception:  # noqa: BLE001
            continue
        if button:
            break

    if button is None:
        for sel in ("button[type='submit']", "button[type='button']"):
            try:
                for b in drv.find_elements("css selector", sel):
                    if not b.is_displayed():
                        continue
                    try:
                        txt = (b.text or "").lower()
                    except Exception:  # noqa: BLE001
                        txt = ""
                    if any(m in txt for m in SIGNUP_MARKS) or not txt:
                        button = b
                        break
            except Exception:  # noqa: BLE001
                continue
            if button:
                break

    if button is None:
        return False  # no signup button on the page; caller re-polls

    deadline = time.monotonic() + float(wait)
    while time.monotonic() < deadline:
        try:
            if button.get_attribute("disabled") is None:
                button.click()
                on_status("[create] submitted the registration form")
                return True
        except Exception:  # noqa: BLE001 - a stale/rotated element
            return True  # got through before it disappeared mid-flight
        time.sleep(POLL_S)
    # Still disabled after the wait (e.g. username rejected) — let the caller
    # regenerate and retry rather than force a click that would do nothing.
    return False


def _shown(drv, el):
    """Is this element actually on screen?

    `is_displayed()` alone is not enough. Roblox's Arkose enforcement iframe
    has been observed filling the viewport (getBoundingClientRect 1169x749,
    non-zero offsetWidth, the challenge plainly visible) while Selenium's
    is_displayed() returned False for it — so captcha_present() reported no
    challenge, the flow fell through to 'form missing', and the account died
    waiting. Geometry is the fallback authority: it is what the user sees.

    The 40px floor keeps 1x1 tracking iframes from counting as a challenge."""
    try:
        if el.is_displayed():
            return True
    except Exception:  # noqa: BLE001 - stale element mid-render
        return False
    try:
        size = drv.execute_script(
            "const r = arguments[0].getBoundingClientRect();"
            "return [r.width, r.height];", el)
    except Exception:  # noqa: BLE001 - no JS (a test double), trust is_displayed
        return False
    return bool(size) and size[0] > 40 and size[1] > 40


def captcha_present(drv):
    """True while an Arkose/FunCaptcha challenge is visible anywhere on the
    page. Several marker shapes are probed (src, id, title, data-e2e and the
    container div) because no single one is guaranteed to survive a Roblox
    markup change or an Arkose rollout."""
    for sel in CAPTCHA_SELECTORS:
        try:
            for f in drv.find_elements("css selector", sel):
                if _shown(drv, f):
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def captcha_challenge_mounted(drv):
    """True once the Arkose challenge iframe ITSELF is on the page — one
    carrying a real client-api src — as opposed to the enforcement container
    that renders first. That iframe is also where the live public key is
    scraped from, so solving before it exists means solving blind against
    the fallback key."""
    for sel in ("iframe[src*='arkoselabs']", "iframe[src*='funcaptcha']",
                "iframe[src*='arkose']"):
        try:
            for f in drv.find_elements("css selector", sel):
                src = (f.get_attribute("src") or "").strip()
                if src and _shown(drv, f):
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def wait_for_captcha_ready(drv, on_status, stop_check, timeout=None):
    """Give a freshly-detected challenge a moment to finish mounting before
    a solver is asked to attack it — the enforcement container can appear
    seconds before the actual challenge iframe. Returns True to proceed
    (iframe seen, challenge gone again, or the wait ran out — a markup that
    only ever shows a container must not strand the flow) and False only
    when the batch was stopped mid-wait."""
    timeout = CAPTCHA_READY_S if timeout is None else float(timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_check():
            return False
        if captcha_challenge_mounted(drv) or not captcha_present(drv):
            return True
        time.sleep(POLL_S)
    return True













# Roblox localizes by Accept-Language, and the CHALLENGE inherits it: on a
# Turkish browser the Arkose puzzle arrives as "Zari toplamak icin oklara tikla
# ...", which a solver's worker cannot read and a non-Turkish operator cannot
# either. The signup form is also matched partly by English text. Forcing one
# known language makes both the puzzle and the form predictable.
SIGNUP_LOCALE = "en-US,en;q=0.9"


def force_english(drv, on_status):
    """Pin the browser to English for the signup flow. Chrome-only, and
    optional: a driver without CDP just gets whatever locale it had."""
    # ONLY the request header. Emulation.setLocaleOverride was tried here too
    # and removed: it rewrites the JS locale APIs without touching timezone or
    # IP, so the browser then claims en-US while every other signal still says
    # Turkey. That inconsistency is exactly what fingerprinting looks for,
    # which is the opposite of what this flow wants. An English Accept-Language
    # from a Turkish address is ordinary and asserts nothing about the machine.
    try:
        drv.execute_cdp_cmd("Network.enable", {})
        drv.execute_cdp_cmd("Network.setExtraHTTPHeaders",
                            {"headers": {"Accept-Language": SIGNUP_LOCALE}})
        return True
    except Exception as e:  # noqa: BLE001 - no CDP, or a build without the domain
        on_status(f"[create] could not force an English locale ({_short(e)})")
        return False






def _short(e):
    return str(e).strip().splitlines()[0][:120] if str(e).strip() else type(e).__name__










def wait_for_manual_solve(drv, on_status, stop_check, timeout=None,
                          note="NO captcha provider configured"):
    """No usable solver: the challenge sits in the visible window; say so
    loudly and wait for the human. 'Cleared' requires several CONSECUTIVE
    absent polls — an Arkose re-render can flicker the iframe away for one
    poll, and treating that single miss as success let the flow declare
    victory (and move on to closing the browser) while the challenge was
    still up."""
    timeout = CAPTCHA_MANUAL_TIMEOUT_S if timeout is None else float(timeout)
    on_status(f"[create] {note} — solve the challenge in "
              "the opened browser window")
    deadline = time.monotonic() + timeout
    misses = 0
    while time.monotonic() < deadline:
        if stop_check():
            return False
        if captcha_present(drv):
            misses = 0
        else:
            misses += 1
            if misses >= CAPTCHA_CLEAR_POLLS or landed_on_home(drv.current_url):
                on_status("[create] captcha cleared")
                return True
        time.sleep(POLL_S)
    on_status("[create] gave up waiting for the manual captcha solve")
    return False


def landed_on_home(url):
    u = (url or "").lower().split("?")[0]
    return any(h in u for h in HOME_URL_MARKERS)


def error_text_near_fields(drv):
    """Concatenated visible validation text, for the taken-username check."""
    chunks = []
    for sel in SEL_ERROR_TEXT:
        try:
            for el in drv.find_elements("css selector", sel):
                t = (el.text or "").strip()
                if t and el.is_displayed():
                    chunks.append(t)
        except Exception:  # noqa: BLE001
            continue
    return " | ".join(chunks)[:500]


def open_signup_page(drv, on_status):
    last_err = None
    for url in SIGNUP_URLS:
        try:
            drv.get(url)
            if _first(drv, SEL_USERNAME, timeout=15):
                on_status(f"[create] registration page loaded ({url})")
                return True
            last_err = f"{url} loaded but showed no signup form"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    on_status(f"[create] could not reach the registration page: {last_err}")
    return False


def read_session_cookie(drv):
    c = drv.get_cookie(COOKIE_NAME)
    return (c or {}).get("value")


def create_account(on_status=lambda m: None, stop_check=lambda: False,
                   username_style="name_no", custom_password=None,
                   driver_factory=None, poll=POLL_S, solver_client=None,
                   solver_mode="step", proxy=None):
    """Create ONE Roblox account end-to-end. Returns a result dict:

        ok, username, password, birthday, user_id, cookie, error/message

    `driver_factory()` must return a started Selenium driver. The DEFAULT
    factory builds an anti-detection Chrome (stealth.make_driver): a vanilla
    Selenium driver carries navigator.webdriver and the automation switch,
    which makes Arkose mint a high-risk token even when the puzzle is solved
    correctly — Roblox then answers the signup POST 403 and re-asks the
    captcha. `proxy` (host[:port][:user:pass] or a scheme:// URL) is that
    browser's outbound address; it exists to spread signup attempts across
    IPs, since Roblox rate-limits signups per address. The factory is
    injected so tests can substitute a fake."""
    accounts_mod = _omni_accounts()
    if driver_factory is None:
        def driver_factory():
            try:
                import stealth
                return stealth.make_driver(headless=False, proxy=proxy,
                                           on_status=on_status)
            except ImportError:
                return accounts_mod._driver("chrome", headless=False)

    username = generate_username(username_style)
    password = custom_password or generate_password()
    birthday = generate_birthday(BIRTHDAY_MIN_AGE, BIRTHDAY_MAX_AGE)
    gender = pick_gender()

    try:
        drv = driver_factory()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "browser_failed",
                "message": f"could not start the browser: {e}"}

    from selenium.common.exceptions import WebDriverException
    try:
        # Pin the locale before ANY navigation: Roblox reads Accept-Language
        # and redirects to a localized route (/tr/CreateAccount) whose captcha
        # is localized with it, and a puzzle nobody in the room can read helps
        # neither a human nor a future solver.
        force_english(drv, on_status)
        if not open_signup_page(drv, on_status):
            return {"ok": False, "error": "navigation_failed",
                    "message": "the Roblox registration page would not load"}
        if stop_check():
            return {"ok": False, "error": "stopped", "message": "cancelled"}

        fill_birthday(drv, birthday, on_status)

        attempts = 0
        captcha_solved_by = "none"
        form_missing_since = None
        deadline = time.monotonic() + FORM_DEADLINE_S * MAX_USERNAME_ATTEMPTS
        while time.monotonic() < deadline:
            if stop_check():
                return {"ok": False, "error": "stopped", "message": "cancelled"}

            # --- state 1: a challenge is up --------------------------------
            # Checked FIRST, before ever looking for the form: the captcha
            # REPLACES the signup form, so a captcha-covered page has no form
            # to find — treating that as 'form missing' is what closed Chrome
            # the moment the challenge appeared.
            if captcha_present(drv):
                # Let the freshly-detected challenge finish mounting before
                # attacking it: the enforcement container can render seconds
                # before the real Arkose iframe (which is also where the
                # live public key is scraped from).
                if not wait_for_captcha_ready(drv, on_status, stop_check):
                    return {"ok": False, "error": "stopped", "message": "cancelled"}
                if not captcha_present(drv):
                    continue    # it unmounted again while we waited
                # Try the vision solver first: it PLAYS the puzzle in this
                # browser rather than buying a token, because Arkose will not
                # issue a challenge to a solver's own address at all.
                solved_here = False
                if solver_client is not None:
                    try:
                        import visioncaptcha
                        res = visioncaptcha.play_challenge(
                            drv, solver_client, on_status, stop_check,
                            is_present=captcha_present, mode=solver_mode)
                        solved_here = bool(res.get("ok"))
                        if not solved_here:
                            on_status("[create] vision solver stopped: "
                                      f"{res.get('reason')}")
                    except Exception as e:  # noqa: BLE001 - never lose the
                        # account over the solver; the human can still finish.
                        on_status(f"[create] vision solver failed: {_short(e)}")

                # Whatever the solver did or did not manage, an unsolved
                # challenge goes to the human in the visible window — the
                # browser must NOT close here.
                if not solved_here and captcha_present(drv):
                    if not wait_for_manual_solve(
                            drv, on_status, stop_check,
                            note=("the solver could not finish it"
                                  if solver_client is not None
                                  else "no automatic solver configured")):
                        return {"ok": False, "error": "captcha_timeout",
                                "message": "the captcha was never cleared"}
                captcha_solved_by = "solver" if solved_here else "manual"
                # Solving (by hand especially) can take minutes that have
                # nothing to do with filling the form: restart that budget.
                deadline = time.monotonic() + FORM_DEADLINE_S * MAX_USERNAME_ATTEMPTS
                time.sleep(2.0)
                continue

            # --- state 2: authenticated -----------------------------------
            if landed_on_home(drv.current_url):
                break   # signup went through (or we arrived already signed in)

            # --- state 3: the form ------------------------------------------
            user_el = _first(drv, SEL_USERNAME, timeout=5)
            pass_el = _first(drv, SEL_PASSWORD, timeout=5)
            if not user_el or not pass_el:
                # No form, no captcha, no home: mid-transition. Give the page
                # a grace window before declaring the signup dead — bailing
                # out here is what closed the browser right after a captcha.
                now = time.monotonic()
                if form_missing_since is None:
                    form_missing_since = now
                elif now - form_missing_since > FORM_MISSING_GRACE_S:
                    return {"ok": False, "error": "form_missing",
                            "message": "the signup form disappeared before it could be filled"}
                time.sleep(POLL_S)
                continue
            form_missing_since = None

            # Re-typing a field that already holds our value restarts Roblox's
            # async validation and keeps the submit button disabled, so only
            # fill what is actually missing.
            try:
                if (user_el.get_attribute("value") or "") != username:
                    fill_field(user_el, username)
            except Exception:  # noqa: BLE001 - stale element: just retype
                fill_field(user_el, username)
            try:
                if not (pass_el.get_attribute("value") or ""):
                    fill_field(pass_el, password)
            except Exception:  # noqa: BLE001
                fill_field(pass_el, password)
            gen = find_gender_control(drv, gender)
            if gen:
                try:
                    gen.click()
                except Exception:  # noqa: BLE001
                    pass
            if not submit_form(drv, on_status):
                # The button is still disabled (async validation pending);
                # let it settle instead of hammering the form.
                time.sleep(1.0)
                continue

            # --- state 4: just submitted ------------------------------------
            # The Arkose iframe can take several seconds to mount after the
            # click; deciding 'no captcha' after one fixed sleep is what let
            # the flow fall through and misread the page. Poll for whichever
            # answer comes back: the challenge, home, or a validation error.
            end = time.monotonic() + CAPTCHA_APPEAR_S
            while time.monotonic() < end:
                if stop_check():
                    return {"ok": False, "error": "stopped", "message": "cancelled"}
                if captcha_present(drv) or landed_on_home(drv.current_url):
                    break
                if error_text_near_fields(drv):
                    break   # a validation verdict (e.g. 'taken') came back
                time.sleep(POLL_S)

            errs = error_text_near_fields(drv).lower()
            if "taken" in errs or ("already" in errs and "username" in errs):
                attempts += 1
                if attempts >= MAX_USERNAME_ATTEMPTS:
                    return {"ok": False, "error": "username_exhausted",
                            "message": f"all {attempts} generated usernames were taken"}
                username = generate_username(username_style)
                on_status(f"[create] username taken — retrying as {username}")
                continue
        else:
            return {"ok": False, "error": "timeout",
                    "message": "account creation did not complete in time"}

        # --- harvest ----------------------------------------------------
        cookie_val = None
        uid, uname = None, None
        harvest_deadline = time.monotonic() + 30
        while time.monotonic() < harvest_deadline:
            cookie_val = read_session_cookie(drv)
            if cookie_val:
                uid, uname = accounts_mod.whoami(cookie_val)
                if uid and uname:
                    break
            time.sleep(1.5)
        if not (cookie_val and uid and uname):
            return {"ok": False, "error": "session_unverified",
                    "message": ("landed on home but the .ROBLOSECURITY cookie never "
                                "validated against users/authenticated")}
        on_status(f"[create] authenticated as {uname} ({uid}); cookie captured")
        return {
            "ok": True,
            "username": uname,
            "requested_username": username,
            "password": password,
            "birthday": list(birthday),
            "gender": gender,
            "user_id": uid,
            "cookie": cookie_val,
            "captcha_solved_by": captcha_solved_by,
        }
    except WebDriverException as e:
        return {"ok": False, "error": "browser_failed",
                "message": f"the browser failed during signup: {_wd_reason(e)}"}
    finally:
        try:
            drv.quit()
        except Exception:  # noqa: BLE001
            pass


def _wd_reason(e):
    """First useful line of a WebDriverException (see omnidroid.accounts)."""
    lines = str(e).strip().splitlines()
    line = lines[0].strip() if lines else ""
    while line.startswith("Message:"):
        line = line[len("Message:"):].strip()
    return line or e.__class__.__name__


def _omni_accounts():
    """omnidroid's accounts module (driver resolution + whoami), via the same
    lazy loader accountsync uses — handles frozen builds AND source checkouts."""
    try:
        import accountsync
        return accountsync._omnidroid_accounts()
    except ImportError:
        return sys_path_fallback()


def sys_path_fallback():  # pragma: no cover - mirrors accountsync's loader
    sibling = Path(__file__).resolve().parent.parent / "omnidroid"
    if (sibling / "omnidroid" / "accounts.py").is_file():
        import sys
        sys.path.insert(0, str(sibling))
        from omnidroid import accounts as mod
        return mod
    raise ImportError("omnidroid accounts module not found")














