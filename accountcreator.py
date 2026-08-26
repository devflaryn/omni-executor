"""Roblox account creation for Omni Executor.

One module, four concerns:

  1. IDENTITY GENERATION — usernames in four styles (`name_no`, `adj_noun`,
     `gamertag`, `stealth`), an 18+ birthday, a secure password and a gender
     pick. Pure functions over `secrets`, so they are unit-testable without a
     browser.

  2. CAPTCHA SOLVING — a small client for 2captcha.com's createTask /
     getTaskResult protocol (the shape every major solver service shares).
     Roblox's signup is gated by Arkose FunCaptcha; with an API key configured
     the challenge is solved out-of-band and the token injected back into the
     page. Without one, the flow falls back to the human solving it in the
     opened browser window — never silently blocked, just slower.

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

CAPTCHA_PROVIDERS = {
    # id -> display name (the UI dropdown + settings.json use the id).
    "2captcha": "2captcha.com",
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


def generate_birthday(min_age=18, max_age=45):
    """A random (year, month, day) making the holder between min_age and
    max_age years old today. Roblox asks for a birth date and gates features
    on it; everything this app does wants an ADULT account, so the floor is
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
                             captcha_provider=None, captcha_api_keys=None):
    """Normalize a creation/captcha config from the UI. Returns
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

    provider = captcha_provider if captcha_provider in CAPTCHA_PROVIDERS else None
    if captcha_provider not in (None, "") and provider is None:
        return None, f"Unknown captcha provider {captcha_provider!r}."
    clean["captchaProvider"] = provider or "2captcha"

    keys = captcha_api_keys if isinstance(captcha_api_keys, dict) else {}
    clean["captchaApiKeys"] = {
        p: (str(keys.get(p) or "").strip()) for p in CAPTCHA_PROVIDERS
    }
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
# 2captcha.com captcha solver (createTask / getTaskResult protocol)
# ---------------------------------------------------------------------------

TWOCAPTCHA_API_BASE = "https://api.2captcha.com"
TWOCAPTCHA_CREATE_PATH = "/createTask"
TWOCAPTCHA_RESULT_PATH = "/getTaskResult"
# Task type for Arkose FunCaptcha without handing the solver a proxy — the
# same name every createTask-style service uses for it.
FUN_CAPTCHA_TASK_TYPE = "FunCaptchaTaskProxyLess"

# Roblox's Arkose public key for login/signup, used when the page cannot be
# scraped for the live one (it is stable across the site, but scraping first
# means a rotation does not strand us).
ROBLOX_ARKOSE_PUBLIC_KEY = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"


class CaptchaError(Exception):
    """Raised for any solver-side failure worth showing the user."""


class TwoCaptchaSolver:
    """Minimal 2captcha.com FunCaptcha client.

    POST {base}/createTask      {"clientKey", "task"}      -> {"errorId", "taskId"}
    POST {base}/getTaskResult   {"clientKey", "taskId"}    -> polled until
    status == "ready", then solution.token is the answer to inject.

    Both constants live at module scope so an endpoint change is a one-line
    edit, not an archaeology dig."""

    def __init__(self, api_key, base=TWOCAPTCHA_API_BASE, timeout=240.0,
                 poll=3.0, request_timeout=30.0):
        if not api_key or not str(api_key).strip():
            raise CaptchaError("2captcha.com API key is empty.")
        self.api_key = str(api_key).strip()
        self.base = (base or TWOCAPTCHA_API_BASE).rstrip("/")
        self.timeout = float(timeout)
        self.poll = float(poll)
        self.request_timeout = float(request_timeout)

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "omni-executor-account-creator/1.0"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001 - body is decoration
                pass
            raise CaptchaError(f"2captcha.com returned HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise CaptchaError(f"Could not reach 2captcha.com: {e}") from e
        try:
            return json.loads(body)
        except ValueError as e:
            raise CaptchaError("2captcha.com sent a response that was not JSON.") from e

    def solve_funcaptcha(self, website_url, public_key, subdomain=None):
        """Submit the challenge and block until a token comes back."""
        task = {
            "type": FUN_CAPTCHA_TASK_TYPE,
            "websiteURL": website_url,
            "websitePublicKey": public_key,
        }
        if subdomain:
            task["funcaptchaApiJSSubdomain"] = subdomain
        resp = self._post(TWOCAPTCHA_CREATE_PATH,
                          {"clientKey": self.api_key, "task": task})
        if resp.get("errorId"):
            raise CaptchaError(
                resp.get("errorDescription") or resp.get("errorCode") or "createTask failed")
        task_id = resp.get("taskId")
        if not task_id:
            raise CaptchaError("2captcha.com did not return a taskId.")

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            time.sleep(self.poll)
            res = self._post(TWOCAPTCHA_RESULT_PATH,
                             {"clientKey": self.api_key, "taskId": task_id})
            err = res.get("errorId")
            if err:
                raise CaptchaError(
                    res.get("errorDescription") or res.get("errorCode") or "getTaskResult failed")
            status = res.get("status")
            if status == "ready":
                token = (res.get("solution") or {}).get("token") or \
                        (res.get("solution") or {}).get("gRecaptchaResponse")
                if not token:
                    raise CaptchaError("Solver reported ready but sent no token.")
                return token
            # "processing" (or anything unknown) keeps the poll going; the
            # deadline is what bounds a wedged task.
        raise CaptchaError(f"Solver did not finish within {int(self.timeout)}s.")


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
CAPTCHA_IFRAME_SEL = ("iframe[src*='arkoselabs'], iframe[src*='funcaptcha'], "
                      "iframe[id*='arkose'], iframe[title*='verification' i]")

MAX_USERNAME_ATTEMPTS = 6       # "taken" regenerations allowed per account
FORM_DEADLINE_S = 120           # budget for loading + filling the form
CAPTCHA_MANUAL_TIMEOUT_S = 600  # human-in-the-loop solve window
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


def classify_date_selects(drv):
    """Find the birthday <select>s by what they CONTAIN, not by id.

    Month select: >= 12 options whose text looks like month names or whose
    values are 1..12. Day select: 28-31 options, all numeric-ish. Year select:
    >= 40 options dominated by 4-digit values. Returns (month, day, year)
    elements or Nones."""
    found = {"month": None, "day": None, "year": None}
    try:
        selects = drv.find_elements("css selector", "select")
    except Exception:  # noqa: BLE001
        return found
    months = {_month_name(m).lower() for m in range(1, 13)}
    for el in selects:
        try:
            options = el.find_elements("css selector", "option")
        except Exception:  # noqa: BLE001
            continue
        texts = [(o.text or "").strip() for o in options]
        vals = [(o.get_attribute("value") or "").strip() for o in options]
        if len(texts) >= 12 and sum(t.lower() in months for t in texts) >= 12:
            if not found["month"]:
                found["month"] = el
        elif 28 <= len(texts) <= 31 and sum(v.isdigit() for v in vals) >= len(vals) - 1:
            if not found["day"]:
                found["day"] = el
        elif len(texts) >= 40 and sum(re.fullmatch(r"(19|20)\d\d", v or "") for v in vals) > len(vals) // 2:
            if not found["year"]:
                found["year"] = el
    return found


def _select_option(el, value, texts=()):
    """Settle a <select> on `value` trying value, then visible text."""
    from selenium.webdriver.support.ui import Select
    s = Select(el)
    v = str(value)
    for opt in s.options:
        if (opt.get_attribute("value") or "").strip() == v:
            s.select_by_value(v)
            return True
    wanted = {str(t).lower() for t in texts}
    for opt in s.options:
        if (opt.text or "").strip().lower() in wanted:
            s.select_by_visible_text(opt.text.strip())
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
        _select_option(sel["month"], month, {_month_name(month), f"{month:02d}", str(month)})
    if sel["day"]:
        _select_option(sel["day"], day, {f"{day:02d}", str(day)})
    if sel["year"]:
        _select_option(sel["year"], year, {str(year)})


def fill_field(el, value):
    el.clear()
    el.send_keys(value)


def find_gender_control(drv, gender):
    """The gender picker, whatever shape it takes today: a labelled button, a
    radio input, or a data attribute. Best-effort — a form without one simply
    skips the step (Roblox has shipped both variants)."""
    label = "Male" if gender == "male" else "Female"
    candidates = [
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


def submit_form(drv, on_status):
    found_specific = False
    for sel in SIGNUP_BUTTON_IDS:
        try:
            for btn in drv.find_elements("css selector", sel):
                if btn.is_displayed():
                    btn.click()
                    found_specific = True
                    break
        except Exception:  # noqa: BLE001
            continue
        if found_specific:
            break
    if found_specific:
        on_status("[create] submitted the registration form")
        return True

    # Fallback: a generic submit button, only when its text says "sign up" in
    # a language we recognise (or it's icon-only). Never guess on a login
    # button, which would silently submit the wrong form.
    marks = ("sign up", "signup", "kaydol", "\u00fcye ol", "registre",
             "create account", "cr\u00e9er", "anmelden", "\u00e0ngivelse",
             "crea cuenta", "inscri")
    for sel in ("button[type='submit']",):
        try:
            for btn in drv.find_elements("css selector", sel):
                try:
                    txt = (btn.text or "").lower()
                except Exception:  # noqa: BLE001
                    txt = ""
                if btn.is_displayed() and (any(m in txt for m in marks) or not txt):
                    btn.click()
                    on_status("[create] submitted the registration form")
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def captcha_present(drv):
    try:
        frames = drv.find_elements("css selector", CAPTCHA_IFRAME_SEL)
        return any(f.is_displayed() for f in frames)
    except Exception:  # noqa: BLE001
        return False


def extract_public_key(drv):
    """Scrape the live Arkose public key: first from a challenge iframe URL
    (?public_key=...), then from inline config in the page source."""
    try:
        for f in drv.find_elements("css selector", "iframe"):
            src = f.get_attribute("src") or ""
            m = re.search(r"[?&]public[_-]?key=([A-Za-z0-9\-]+)", src)
            if m:
                return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    try:
        m = re.search(r'''["']?public[_-]?[Kk]ey["']?\s*[:=]\s*["']([A-Za-z0-9\-]{16,})["']''',
                      drv.page_source or "")
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


# Injecting a solved token: try every integration point Roblox builds have
# used, in order of preference, and report which one accepted it. Anything
# that fails falls through to the next; if none accept, the caller says so and
# the human can still solve the visible challenge by hand.
INJECT_TOKEN_JS = """
const token = arguments[0];
window.__omniArkoseToken = token;
const hits = [];
// 1. Hidden inputs some integrations read straight from the DOM.
for (const sel of ['input[name="fc-token"]', '#fc-token',
                   'input[name="verificationToken"]', '#verification-token',
                   'input[name="captchaToken"]']) {
  const el = document.querySelector(sel);
  if (el) {
    el.value = token;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    hits.push('input:' + sel);
  }
}
// 2. Enforcement objects registered by the Arkose web SDK.
for (const name of ['__arkose_enforcement', 'arkoseEnforcement', 'enforcement',
                    'myEnforcement', 'ArkoseEnforcement']) {
  const obj = window[name];
  if (obj && typeof obj.onCompleted === 'function') {
    try { obj.onCompleted({token}); hits.push('hook:' + name); } catch (e) {}
  }
}
// 3. Roblox's own verification bridge, when present.
try {
  if (window.Roblox && window.Roblox.GameVerification &&
      typeof window.Roblox.GameVerification.tokenReceived === 'function') {
    window.Roblox.GameVerification.tokenReceived(token);
    hits.push('roblox:GameVerification');
  }
} catch (e) {}
return hits;
"""


def solve_captcha_automatically(drv, solver, on_status):
    """Solve via the configured provider and push the token into the page.
    Returns (True, strategy) on an accepted injection, else (False, reason)."""
    url = drv.current_url or "https://www.roblox.com/"
    key = extract_public_key(drv) or ROBLOX_ARKOSE_PUBLIC_KEY
    on_status(f"[create] asking {type(solver).__name__} to solve the challenge ...")
    token = solver.solve_funcaptcha(url, key)
    on_status("[create] solver returned a token; injecting")
    try:
        hits = drv.execute_script(INJECT_TOKEN_JS, token) or []
    except Exception as e:  # noqa: BLE001
        return False, f"injection failed: {e}"
    if hits:
        return True, ", ".join(str(h) for h in hits)
    return False, "no injection point accepted the token"


def wait_for_manual_solve(drv, on_status, stop_check, timeout=CAPTCHA_MANUAL_TIMEOUT_S):
    """No API key: the challenge sits in the visible window; say so loudly and
    wait for it to disappear."""
    on_status("[create] NO captcha provider configured — solve the challenge in "
              "the opened browser window")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_check():
            return False
        if not captcha_present(drv):
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
                   solver=None, driver_factory=None, poll=POLL_S):
    """Create ONE Roblox account end-to-end. Returns a result dict:

        ok, username, password, birthday, user_id, cookie, error/message

    `driver_factory()` must return a started Selenium driver (headful Chrome
    by default, resolved through omnidroid's own chromedriver plumbing). It
    is injected so tests can substitute a fake."""
    accounts_mod = _omni_accounts()
    if driver_factory is None:
        def driver_factory():
            return accounts_mod._driver("chrome", headless=False)

    username = generate_username(username_style)
    password = custom_password or generate_password()
    birthday = generate_birthday()
    gender = pick_gender()

    try:
        drv = driver_factory()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "browser_failed",
                "message": f"could not start the browser: {e}"}

    from selenium.common.exceptions import WebDriverException
    try:
        if not open_signup_page(drv, on_status):
            return {"ok": False, "error": "navigation_failed",
                    "message": "the Roblox registration page would not load"}
        if stop_check():
            return {"ok": False, "error": "stopped", "message": "cancelled"}

        fill_birthday(drv, birthday, on_status)

        attempts = 0
        deadline = time.monotonic() + FORM_DEADLINE_S * MAX_USERNAME_ATTEMPTS
        injected = False
        while time.monotonic() < deadline:
            if stop_check():
                return {"ok": False, "error": "stopped", "message": "cancelled"}

            user_el = _first(drv, SEL_USERNAME, timeout=5)
            pass_el = _first(drv, SEL_PASSWORD, timeout=5)
            if not user_el or not pass_el:
                if landed_on_home(drv.current_url):
                    break   # already authenticated (fast retry path)
                return {"ok": False, "error": "form_missing",
                        "message": "the signup form disappeared before it could be filled"}

            fill_field(user_el, username)
            fill_field(pass_el, password)
            gen = find_gender_control(drv, gender)
            if gen:
                try:
                    gen.click()
                except Exception:  # noqa: BLE001
                    pass
            submit_form(drv, on_status)
            time.sleep(2.0)

            # --- post-submit states -------------------------------------
            if captcha_present(drv):
                if solver is not None:
                    ok, why = solve_captcha_automatically(drv, solver, on_status)
                    if ok:
                        injected = True
                    else:
                        on_status(f"[create] automatic solve did not take ({why}); "
                                  f"solve it manually in the browser window")
                        if not wait_for_manual_solve(drv, on_status, stop_check):
                            return {"ok": False, "error": "captcha_timeout",
                                    "message": "the captcha was never cleared"}
                else:
                    if not wait_for_manual_solve(drv, on_status, stop_check):
                        return {"ok": False, "error": "captcha_timeout",
                                "message": "the captcha was never cleared"}
                time.sleep(2.0)

            errs = error_text_near_fields(drv).lower()
            if "taken" in errs or ("already" in errs and "username" in errs):
                attempts += 1
                if attempts >= MAX_USERNAME_ATTEMPTS:
                    return {"ok": False, "error": "username_exhausted",
                            "message": f"all {attempts} generated usernames were taken"}
                username = generate_username(username_style)
                on_status(f"[create] username taken — retrying as {username}")
                continue

            if landed_on_home(drv.current_url):
                break

            # Still on the form with no error we recognise: keep polling a
            # little before treating it as stuck.
            time.sleep(1.5)
            if captcha_present(drv):
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
            "captcha_solved_by": "provider" if injected else ("manual" if solver is None else "fallback-manual"),
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


def make_solver(captcha_provider, api_keys):
    """Build the solver for a validated config, or None when no key is set
    (= manual solving)."""
    keys = api_keys or {}
    if captcha_provider == "2captcha" and keys.get("2captcha"):
        return TwoCaptchaSolver(keys["2captcha"])
    return None
