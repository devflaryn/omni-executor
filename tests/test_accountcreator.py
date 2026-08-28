"""accountcreator: identity generation, config validation, vault, solver.

The Selenium flow itself is only smoke-tested here (a fake driver records the
calls); the point of these tests is that everything AROUND the browser — the
generators, the validation contract shared with main.Api, the vault file and
the 2captcha.com client's request/response shape — is pinned down without
touching a network or a Chrome.
"""

import json
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path

import pytest

import accountcreator as ac


@pytest.fixture
def vault_root():
    """A throwaway home for the vault.

    A unique PLAIN mkdir rather than tempfile.mkdtemp/pytest's tmp_path:
    mkdtemp hardens the directory's ACL to owner-only, which some sandboxed
    environments (restricted tokens, service accounts) then cannot write back
    into. A normal directory inherits the parent's ACL everywhere."""
    import uuid
    try:
        base = Path(tempfile.gettempdir()) / "omni-vault-tests"
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError:
        base = Path(__file__).resolve().parent.parent / "test-scratch"
        base.mkdir(parents=True, exist_ok=True)
    d = base / f"vault-{uuid.uuid4().hex[:10]}"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- generators

def test_username_styles_match_their_examples():
    """Each style must produce names shaped like the examples shown in the UI:
    Eric848381 / FrozenWolf1192 / Bright_Shark858 / mwu78cnas782n."""
    for _ in range(200):
        name = ac.generate_username("name_no")
        assert ac.valid_username(name) and any(c.isdigit() for c in name)
        assert name[0].isalpha()

        adj_noun = ac.generate_username("adj_noun")
        assert ac.valid_username(adj_noun)
        stem = "".join(ch for ch in adj_noun if not ch.isdigit())
        assert any(adj_noun.startswith(a) for a in ac.ADJECTIVES)
        assert any(n in stem for n in ac.ANIMAL_NOUNS)

        tag = ac.generate_username("gamertag")
        assert ac.valid_username(tag)
        assert any(tag.startswith(a) for a in ac.GAMER_ADJECTIVES)

        stealth = ac.generate_username("stealth")
        assert ac.valid_username(stealth)
        assert stealth == stealth.lower()
        assert any(c.isdigit() for c in stealth)


def test_usernames_are_roblox_valid():
    for style in ac.USERNAME_STYLES:
        for _ in range(100):
            name = ac.generate_username(style)
            assert 3 <= len(name) <= ac.USERNAME_MAX_LEN, (style, name)
            assert ac.USERNAME_RE.fullmatch(name), (style, name)


def test_unknown_style_raises():
    with pytest.raises(ValueError):
        ac.generate_username("nope")


def test_birthday_is_adult_but_not_too_old():
    """Accounts must be 18+ but no older than 25 (the required range)."""
    today = date.today()
    for _ in range(300):
        y, m, d = ac.generate_birthday()
        bday = date(y, m, d)
        age = today.year - y - ((today.month, today.day) < (m, d))
        assert 18 <= age <= 25, (y, m, d)


def test_password_meets_all_classes_and_is_random():
    pw = ac.generate_password()
    assert len(pw) >= 16
    assert any(c.isupper() for c in pw)
    assert any(c.islower() for c in pw)
    assert any(c.isdigit() for c in pw)
    assert any(c in ac.PASSWORD_SYMBOLS for c in pw)
    assert {pw, ac.generate_password(), ac.generate_password()} != {pw}


def test_gender_pick():
    assert ac.pick_gender() in ("male", "female")


# ------------------------------------------------------------- configuration

@pytest.mark.parametrize("bad", [0, -3, 51, "lots"])
def test_validate_config_rejects_bad_amount(bad):
    clean, err = ac.validate_creation_config(amount=bad)
    assert clean is None and err


def test_validate_config_rejects_unknown_style():
    _, e1 = ac.validate_creation_config(username_style="l33t")
    assert e1


def test_validate_config_requires_strong_custom_password():
    clean, err = ac.validate_creation_config(custom_password="short")
    assert clean is None and "8" in err
    clean, _ = ac.validate_creation_config(custom_password="long-enough-1!")
    assert clean["customPassword"] == "long-enough-1!"


# -------------------------------------------------------------------- vault

def test_vault_roundtrip(vault_root):
    v = ac.Vault(vault_root)
    v.add("Eric848381", "s3cret-Pass!", birthday=(1999, 4, 12), style="name_no")
    rec = v.get("Eric848381")
    assert rec["password"] == "s3cret-Pass!"
    assert rec["birthday"] == [1999, 4, 12]
    # Listing hides secrets by default; reveal is explicit.
    rows = v.list()
    assert rows[0]["username"] == "Eric848381"
    assert rows[0]["hasPassword"] is True and "password" not in rows[0]
    full = v.list(include_secrets=True)
    assert full[0]["password"] == "s3cret-Pass!"
    assert v.remove("Eric848381") is True
    assert v.get("Eric848381") is None
    assert v.remove("Eric848381") is False


def test_vault_file_is_json_and_survives_reload(vault_root):
    v = ac.Vault(vault_root)
    v.add("Ultra_Hawk948", "pw", style="adj_noun")
    data = json.loads((vault_root / ac.VAULT_FILE).read_text(encoding="utf-8"))
    assert data["accounts"]["Ultra_Hawk948"]["style"] == "adj_noun"
    assert ac.Vault(vault_root).get("Ultra_Hawk948")["createdAt"] <= time.time()


# ------------------------------------------------------------ 2captcha solver

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ------------------------------------------------------- selenium flow (fake)

class FakeElement:
    def __init__(self, tag="input", text="", value="", on_click=None):
        self.tag = tag
        self.text = text
        self.value = value
        self.on_click = on_click
        self.cleared = False
        self.typed = []
        self.clicked = False

    def clear(self):
        self.cleared = True

    def send_keys(self, v):
        self.typed.append(v)

    def click(self):
        self.clicked = True
        if self.on_click:
            self.on_click()

    def is_displayed(self):
        return True

    def find_elements(self, *a):
        return []

    def get_attribute(self, name):
        if name == "value":
            return str(self.value)
        return None



class FakeDriver:
    """Records the signup conversation; enough surface for create_account."""

    def __init__(self, script_results=None):
        self.urls_visited = []
        self.url = "https://www.roblox.com/up/registration"
        self.cookies = {}
        self.elements = {}
        self.script_calls = []
        self._script_results = script_results or {}

    def get(self, url):
        self.urls_visited.append(url)
        self.url = url

    @property
    def current_url(self):
        return self.url

    def find_element(self, by, sel):
        el = self.elements.get(sel)
        if el is None:
            from selenium.common.exceptions import NoSuchElementException
            raise NoSuchElementException(sel)
        return el

    def find_elements(self, by, sel):
        els = self.elements.get(sel) or []
        return els if isinstance(els, list) else [els]

    def execute_script(self, script, *args):
        self.script_calls.append((script, args))
        # _shown() falls back to geometry when is_displayed() lies; a driver
        # that does not script an answer reports "no box", so the caller keeps
        # trusting is_displayed().
        if "getBoundingClientRect" in script:
            return self._script_results.get("rect")
        return self._script_results.get("script")

    def get_cookie(self, name):
        return self.cookies.get(name)

    def quit(self):
        pass


def test_create_account_success_path(monkeypatch):
    """With a driver whose form fills cleanly, create_account must harvest the
    cookie, verify it via whoami, and hand back every secret."""
    drv = FakeDriver(script_results={"inject": ["hook:__arkose_enforcement"]})
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)

    def land_on_home():
        drv.url = "https://www.roblox.com/home"
        drv.cookies[ac.COOKIE_NAME] = {"value": "ROBL-COOKIE"}

    drv.elements = {
        "#signup-username": FakeElement(),
        "#signup-password": FakeElement(),
        "#signup-button": FakeElement(tag="button", text="Sign Up",
                                      on_click=land_on_home),
    }
    lines = []

    class FakeAccounts:
        @staticmethod
        def whoami(cookie):
            assert cookie == "ROBL-COOKIE"
            return 987654, "Eric848381"

        @staticmethod
        def _driver(browser, headless=False):
            assert headless is False   # the window stays visible on purpose
            return drv

    monkeypatch.setattr(ac, "_omni_accounts", lambda: FakeAccounts)

    res = ac.create_account(on_status=lines.append,
                            username_style="name_no",
                            driver_factory=lambda: drv)
    assert res["ok"], res
    assert res["username"] == "Eric848381"
    assert res["user_id"] == 987654
    assert res["cookie"] == "ROBL-COOKIE"
    assert res["birthday"][0] <= date.today().year - 18
    assert res["gender"] in ("male", "female")
    assert len(res["password"]) >= 8
    # The password field actually received the SAME password we handed back.
    pw_el = drv.elements["#signup-password"]
    assert pw_el.typed == [res["password"]]
    assert any("registration page loaded" in l for l in lines)


def test_create_account_retries_taken_username(monkeypatch):
    """A 'username already taken' error regenerates instead of failing."""
    drv = FakeDriver()
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    clicks = {"n": 0}
    seen = []

    def counting(style):
        seen.append(len(seen) + 1)
        return f"Attempt{len(seen)}"

    def submit_click():
        clicks["n"] += 1
        if clicks["n"] >= 3:
            # Third submission goes through: land authenticated, alert gone.
            drv.elements.pop("[role='alert']", None)
            drv.url = "https://www.roblox.com/home"
            drv.cookies[ac.COOKIE_NAME] = {"value": "C"}

    drv.elements = {
        "#signup-username": FakeElement(),
        "#signup-password": FakeElement(),
        "[role='alert']": FakeElement(text="Username already taken"),
        "#signup-button": FakeElement(tag="button", text="Sign Up",
                                      on_click=submit_click),
    }

    class FakeAccounts:
        @staticmethod
        def whoami(cookie):
            assert cookie == "C"
            return 1, "FreshName"

        @staticmethod
        def _driver(browser, headless=False):
            return drv

    monkeypatch.setattr(ac, "generate_username", counting)
    monkeypatch.setattr(ac, "_omni_accounts", lambda: FakeAccounts)

    res = ac.create_account(driver_factory=lambda: drv)
    assert res["ok"], res
    assert res["username"] == "FreshName"
    assert len(seen) == 3      # two taken, third accepted
    assert clicks["n"] == 3


def test_create_account_reports_browser_failure():
    def boom():
        raise OSError("chrome missing")

    res = ac.create_account(driver_factory=boom)
    assert res["ok"] is False
    assert res["error"] == "browser_failed"


# ------------------------------------------------- captcha keeps browser open
#
# The regression these pin down: when the Arkose challenge mounts it REPLACES
# the signup form, and the flow used to read 'form gone' as fatal and quit
# the driver — closing Chrome the instant the captcha appeared.

class _CaptchaFlag:
    """A captcha iframe whose visibility is driven by a state dict."""

    def __init__(self, state):
        self._state = state

    def is_displayed(self):
        return bool(self._state["captcha"])


class _SolverFlowDriver(FakeDriver):
    """Submit -> the challenge replaces the form -> the injected token is
    accepted -> the challenge unmounts -> home arrives a couple of polls
    later (never instantly — the old code only survived instant landings)."""

    def __init__(self):
        super().__init__(script_results={"inject": ["hook:__arkose_enforcement"]})
        self.state = {"captcha": False, "home_reads": None}

    def execute_script(self, script, *args):
        out = super().execute_script(script, *args)
        if args and str(args[0]).startswith("TOKEN"):
            self.state["captcha"] = False
            self.state["home_reads"] = 2
        return out

    @property
    def current_url(self):
        if self.state["home_reads"] is not None:
            if self.state["home_reads"] > 0:
                self.state["home_reads"] -= 1
                return "https://www.roblox.com/CreateAccount"
            self.url = "https://www.roblox.com/home"
            self.cookies[ac.COOKIE_NAME] = {"value": "CAP-COOKIE"}
        return self.url


def _fast_captcha(monkeypatch):
    """Make the captcha-transition machinery millisecond-fast. The ready /
    unmount windows poll on the REAL clock (sleep is a no-op here), so their
    defaults would cost real seconds per test; the fake challenges also carry
    no iframe src, so 'is the challenge iframe mounted' is answered yes
    directly instead of burning the ready window."""
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    monkeypatch.setattr(ac, "CAPTCHA_READY_S", 0.05)
    monkeypatch.setattr(ac, "captcha_challenge_mounted", lambda drv: True)
    real_first = ac._first
    monkeypatch.setattr(ac, "_first",
                        lambda drv, sels, timeout=10: real_first(drv, sels, 0.05))


def _accounts_returning(cookie, uid, uname):
    class FakeAccounts:
        @staticmethod
        def whoami(c):
            assert c == cookie
            return uid, uname

    return FakeAccounts


def test_create_account_waits_for_a_manual_captcha_solve(monkeypatch):
    """No solver configured: the challenge replaces the form, the flow must
    keep the browser open and WAIT (not declare the form missing), and a
    human-cleared challenge completes the account."""
    drv = FakeDriver()
    _fast_captcha(monkeypatch)

    state = {"polls": 4, "drv": drv}

    class HumanSolvesFlag:
        def is_displayed(self):
            if state["polls"] > 0:
                state["polls"] -= 1
                return True
            # The human finished the challenge: the page moves on to home.
            state["drv"].url = "https://www.roblox.com/home"
            state["drv"].cookies[ac.COOKIE_NAME] = {"value": "MANUAL-COOKIE"}
            return False

    def show_captcha():
        drv.elements.pop("#signup-username", None)
        drv.elements.pop("#signup-password", None)
        drv.elements["iframe[src*='funcaptcha']"] = [HumanSolvesFlag()]

    drv.elements = {
        "#signup-username": FakeElement(),
        "#signup-password": FakeElement(),
        "#signup-button": FakeElement(tag="button", text="Sign Up",
                                      on_click=show_captcha),
    }
    monkeypatch.setattr(ac, "_omni_accounts",
                        lambda: _accounts_returning("MANUAL-COOKIE", 222, "ManualUser"))

    res = ac.create_account(on_status=lambda m: None,
                            driver_factory=lambda: drv)
    assert res["ok"], res
    assert res["username"] == "ManualUser"
    assert res["captcha_solved_by"] == "manual"


class _StubbornFlag:
    """A challenge that stays mounted no matter what token is injected."""

    def is_displayed(self):
        return True


# ------------------------------------------------------- captcha page probes

def test_landed_on_home_matches_only_authenticated_paths():
    assert ac.landed_on_home("https://www.roblox.com/home?x=1")
    assert ac.landed_on_home("https://www.roblox.com/discover")
    assert not ac.landed_on_home("https://www.roblox.com/login")
    assert not ac.landed_on_home("")


# ------------------------------------------------ stale-provider self-heal

# ------------------------------------------------------- registration entry

def test_signup_urls_use_live_routes_not_retired_404_paths():
    """Roblox retired /up/registration and /register (both 404 now); the
    signing entry point is the CreateAccount SPA route (locale-localized) plus
    the canonical signup redirect. Neither of the dead paths may be in the
    candidate list, or a Turkish-locale browser lands on a /tr/... 404."""
    assert ac.SIGNUP_URLS, "must keep at least one signup URL"
    for url in ac.SIGNUP_URLS:
        assert "roblox.com" in url, url
    assert "register" not in [u.lower() for u in ac.SIGNUP_URLS]
    assert "up/registration" not in [u.lower() for u in ac.SIGNUP_URLS]
    assert any("CreateAccount" in u for u in ac.SIGNUP_URLS) or \
        any("signupredir" in u for u in ac.SIGNUP_URLS)


def test_submit_form_clicks_signup_id_button_with_localized_text():
    """The id-based signup button must be clicked even when Roblox localizes
    its visible text (e.g. Turkish 'Kaydol'). The generic submit fallback must
    never click a login button."""
    calls = {"n": 0}

    class Btn:
        def __init__(self, text, disabled=True):
            self.text = text
            self.displayed = True
            self._disabled = disabled

        def is_displayed(self):
            return self.displayed

        def get_attribute(self, name):
            if name == "disabled":
                return "true" if self._disabled else None
            return None

        def click(self):
            calls["n"] += 1

    # Signup button starts enabled; the login button must never be clicked.
    class Drv:
        def find_elements(self, by, sel):
            if sel == "#signup-button":
                return [Btn("Kaydol", disabled=False)]
            if sel == "button[type='submit']":
                return [Btn("Giriş Yap", disabled=False)]
            return []

    assert ac.submit_form(Drv(), lambda m: None) is True
    assert calls["n"] == 1   # the signup one, not the login one


def test_submit_form_waits_for_disabled_button_to_enable(monkeypatch):
    """Roblox renders the signup button disabled until the form validates
    asynchronously; submit_form must poll for enablement instead of clicking
    a disabled (no-op) button."""
    calls = {"n": 0}
    state = {"disabled": True}

    class Btn:
        def is_displayed(self):
            return True

        def get_attribute(self, name):
            if name == "disabled":
                return "true" if state["disabled"] else None
            return None

        def click(self):
            calls["n"] += 1

    class Drv:
        def find_elements(self, by, sel):
            return [Btn()] if sel == "#signup-button" else []

    def enable_after_two_polls():
        state["disabled"] = False

    monkeypatch.setattr(ac.time, "sleep",
                        lambda s: enable_after_two_polls())

    assert ac.submit_form(Drv(), lambda m: None, wait=10.0) is True
    assert calls["n"] == 1


def test_classify_date_selects_with_placeholder_day_and_ids():
    """Today's Roblox form: #MonthDropdown / #DayDropdown / #YearDropdown, with
    the Day select carrying a disabled ``Day`` placeholder (32 options total).
    All three must be classified AND picked by id."""
    class Opt:
        def __init__(self, text, value):
            self._text, self._value = text, value

        def get_attribute(self, name):
            assert name == "value"
            return self._value

        @property
        def text(self):
            return self._text

    months = [Opt("Month", "")] + \
        [Opt(m, m[:3]) for m in
         ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]]
    days = [Opt("Day", "")] + [Opt(f"{d:02d}", f"{d:02d}") for d in range(1, 32)]
    years = [Opt("Year", "")] + [Opt(f"{y}", f"{y}") for y in range(2021, 1926, -1)]

    class Sel:
        def __init__(self, opts):
            self._opts = opts

        def find_elements(self, by, sel):
            assert sel == "option"
            return self._opts

    drv = _SelectsDrv(Sel(months), Sel(days), Sel(years))
    found = ac.classify_date_selects(drv)
    assert found["month"] is not None
    assert found["day"] is not None
    assert found["year"] is not None


class _SelectsDrv:
    """Select list + id lookups over the same swiss Sels (a miniature of the
    Selenium contract classify_date_selects depends on)."""

    def __init__(self, month, day, year):
        self._all = [month, day, year]

    def find_elements(self, by, sel):
        if sel == "select":
            return self._all
        return []


def test_select_option_matches_abbrev_month_value(monkeypatch):
    """Roblox stores the month as a 3-letter abbreviation (<option value="Mar">),
    so _select_option must settle on that value even though the app feeds it the
    numeric month. This pins the fix for 'month not registering'."""
    class Opt:
        def __init__(self, value, text):
            self._value, self._text = value, text

        def get_attribute(self, n):
            assert n == "value"
            return self._value

        @property
        def text(self):
            return self._text

    calls = {"by_value": [], "by_visible": []}

    class FakeSelect:
        def __init__(self, el):
            self.options = el
            self._sel = None

        def select_by_value(self, v):
            calls["by_value"].append(v)
            self._sel = v

        def select_by_visible_text(self, t):
            calls["by_visible"].append(t)
            self._sel = t

    opts = [Opt("", "Month")] + \
        [Opt(m[:3], m) for m in ["January", "February", "March", "April", "May",
                                 "June", "July", "August", "September",
                                 "October", "November", "December"]]

    monkeypatch.setattr("selenium.webdriver.support.ui.Select", FakeSelect)
    # month 3 -> abbreviation "Mar" is the <option value>; select by that value.
    assert ac._select_option(opts, [ac._month_abbr(3), "03", "3", "March"]) is True
    assert calls["by_value"] == ["Mar"]
    assert calls["by_visible"] == []


def test_find_gender_control_matches_id_button():
    """Roblox's gender picker is two icon buttons with no text — only
    id=MaleButton/id=FemaleButton and a title. find_gender_control must match
    those."""
    class Btn:
        def is_displayed(self):
            return True

    class Drv:
        def find_elements(self, by, sel):
            if sel == "#MaleButton":
                return [Btn()]
            if sel == "#FemaleButton":
                return [Btn()]
            return []

    assert ac.find_gender_control(Drv(), "male") is not None
    assert ac.find_gender_control(Drv(), "female") is not None


# ---------------------------------------------------------------------------
# Arkose integration — pinned against what the LIVE Roblox signup page does
#
# Every constant below was read off roblox.com/CreateAccount in a real browser
# while a challenge was on screen. The previous generation of these tests
# faked `execute_script` -> ["hook:__arkose_enforcement"], so the injection
# layer was free to target elements that do not exist on the page and still
# go green. These assert against the OBSERVED integration instead.
# ---------------------------------------------------------------------------

# The enforcement iframe URL as Roblox actually serves it: the public key sits
# in the FRAGMENT (#key&nonce&parentOrigin), never as a ?public_key= query
# parameter — the only form the old regex could read.
LIVE_ENFORCEMENT_SRC = (
    "https://arkoselabs.roblox.com/v2/4.4.5/"
    "enforcement.a30f6b579e932efaa0a5bb0ec1c0eed3.html"
    "#A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"
    "&d77e1ee3-a659-40f7-b53b-1fc82ef92667"
    "&https%3A%2F%2Fwww.roblox.com"
)
LIVE_ARKOSE_HOST = "arkoselabs.roblox.com"


class ArkoseFrame:
    """An iframe element carrying the live enforcement src."""

    def __init__(self, src=LIVE_ENFORCEMENT_SRC):
        self.src = src

    def is_displayed(self):
        return True

    def get_attribute(self, name):
        return self.src if name == "src" else None


class BlobDriver(FakeDriver):
    """A driver whose page exposes a live blob and a live Arkose iframe."""

    def __init__(self, blob="BLOB-123"):
        super().__init__(script_results={"blob": blob,
                                         "inject": ["arkose:onCompleted"]})
        self.elements["iframe"] = [ArkoseFrame()]
        self.url = "https://www.roblox.com/CreateAccount"


# ---------------------------------------------------------------------------
# residential proxy support
#
# Arkose fingerprints the IP the widget is loaded from, and a solver farm's
# datacenter addresses are exactly what it is looking for — which is why every
# proxyless task shape came back ERROR_CAPTCHA_UNSOLVABLE against Roblox's
# signup key. With a proxy the worker loads the widget from the user's own
# residential IP and the task type changes from FunCaptchaTaskProxyless to
# FunCaptchaTask.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    # host:port
    ("1.2.3.4:8080",
     {"type": "http", "address": "1.2.3.4", "port": 8080,
      "login": "", "password": ""}),
    # host:port:user:pass — what most residential dashboards hand out
    ("gate.provider.io:7000:user-1:secret",
     {"type": "http", "address": "gate.provider.io", "port": 7000,
      "login": "user-1", "password": "secret"}),
    # user:pass@host:port
    ("user-1:secret@gate.provider.io:7000",
     {"type": "http", "address": "gate.provider.io", "port": 7000,
      "login": "user-1", "password": "secret"}),
    # full URL, scheme carried through
    ("socks5://user-1:secret@gate.provider.io:7000",
     {"type": "socks5", "address": "gate.provider.io", "port": 7000,
      "login": "user-1", "password": "secret"}),
    ("http://1.2.3.4:8080",
     {"type": "http", "address": "1.2.3.4", "port": 8080,
      "login": "", "password": ""}),
    # a password containing ':' must survive (only the FIRST colon splits)
    ("host.io:9000:user:pa:ss",
     {"type": "http", "address": "host.io", "port": 9000,
      "login": "user", "password": "pa:ss"}),
    # whitespace is forgiving
    ("  1.2.3.4:8080  ",
     {"type": "http", "address": "1.2.3.4", "port": 8080,
      "login": "", "password": ""}),
])
def test_parse_proxy_accepts_the_formats_dashboards_hand_out(text, expected):
    proxy, err = ac.parse_proxy(text)
    assert err is None, err
    assert proxy == expected


@pytest.mark.parametrize("text", [
    "not-a-proxy",              # no port
    "1.2.3.4:notaport",
    "1.2.3.4:0",                # out of range
    "1.2.3.4:70000",
    ":8080",                    # no host
    "ftp://1.2.3.4:8080",       # unsupported scheme
])
def test_parse_proxy_rejects_junk(text):
    proxy, err = ac.parse_proxy(text)
    assert proxy is None
    assert err


def test_parse_proxy_treats_blank_as_no_proxy():
    """An empty field is 'no proxy', not an error — proxyless stays the
    default so an unconfigured install behaves exactly as before."""
    for blank in ("", "   ", None):
        proxy, err = ac.parse_proxy(blank)
        assert proxy is None and err is None


def test_validate_creation_config_accepts_and_normalizes_a_proxy():
    clean, err = ac.validate_creation_config(
        captcha_proxy="  gate.provider.io:7000:user-1:secret  ")
    assert err is None
    assert clean["captchaProxy"] == "gate.provider.io:7000:user-1:secret"


def test_validate_creation_config_rejects_a_bad_proxy():
    """A typo in the proxy has to surface in the modal, not three layers down
    as a mystery solver failure the user pays for."""
    clean, err = ac.validate_creation_config(captcha_proxy="1.2.3.4:notaport")
    assert clean is None
    assert "proxy" in err.lower()


def test_validate_creation_config_defaults_the_proxy_to_empty():
    clean, err = ac.validate_creation_config()
    assert err is None
    assert clean["captchaProxy"] == ""


# ---------------------------------------------------------------------------
# captcha pre-flight
#
# A failed batch cannot tell a dead key from a dead proxy from an Arkose that
# refuses the proxy's IP — they all surface as one paid, useless solve. The
# pre-flight separates them without spending anything.
# ---------------------------------------------------------------------------


def test_force_english_pins_accept_language_before_navigation():
    """Roblox localizes by Accept-Language and the Arkose puzzle inherits it.
    A Turkish browser gets a Turkish puzzle, which neither a solver's worker
    nor a non-Turkish operator can read."""
    calls = []

    class CdpDriver(FakeDriver):
        def execute_cdp_cmd(self, cmd, args):
            calls.append((cmd, args))
            return {}

    assert ac.force_english(CdpDriver(), lambda m: None) is True
    headers = next(a for c, a in calls if c == "Network.setExtraHTTPHeaders")
    assert headers["headers"]["Accept-Language"].startswith("en-US")
    # Deliberately NOT Emulation.setLocaleOverride: rewriting the JS locale
    # without touching timezone or IP is a fingerprint mismatch, which hurts
    # more than the localized puzzle it would fix.
    assert not any(c == "Emulation.setLocaleOverride" for c, _ in calls)


def test_force_english_is_optional():
    """A driver without CDP must still be able to create accounts."""
    said = []
    assert ac.force_english(FakeDriver(), said.append) is False
    assert said and "locale" in said[0]


# ---------------------------------------------------------------------------
# CapSolver: a second provider on the same createTask/getTaskResult protocol
# ---------------------------------------------------------------------------

class InvisibleArkoseFrame(ArkoseFrame):
    """The live enforcement iframe: filling the viewport, plainly visible to
    the user, but Selenium reports is_displayed() False for it."""

    def is_displayed(self):
        return False


def test_captcha_present_trusts_geometry_when_is_displayed_lies():
    """Observed on roblox.com/CreateAccount: the Arkose iframe measured
    1169x749 with the challenge on screen, yet is_displayed() was False.
    Believing it meant the challenge was never detected and the account died
    waiting for a form that the captcha had replaced."""
    class GeoDriver(FakeDriver):
        def execute_script(self, script, *args):
            if "getBoundingClientRect" in script:
                return [1169, 749]
            return super().execute_script(script, *args)

    drv = GeoDriver()
    drv.elements["iframe[src*='arkose']"] = [InvisibleArkoseFrame()]
    assert ac.captcha_present(drv) is True


def test_captcha_present_ignores_tracking_pixels():
    """A 1x1 iframe is not a challenge."""
    class GeoDriver(FakeDriver):
        def execute_script(self, script, *args):
            if "getBoundingClientRect" in script:
                return [1, 1]
            return super().execute_script(script, *args)

    drv = GeoDriver()
    drv.elements["iframe[src*='arkose']"] = [InvisibleArkoseFrame()]
    assert ac.captcha_present(drv) is False


def test_captcha_present_still_uses_is_displayed_when_it_says_yes():
    drv = FakeDriver()
    drv.elements["iframe[src*='arkose']"] = [ArkoseFrame()]
    assert ac.captcha_present(drv) is True


# --------------------------------------------------- the vision captcha solver
#
# The solver PLAYS the puzzle in this browser instead of buying a token: Arkose
# will not issue a challenge to a solver's own address at all. What matters
# here is what happens AFTERWARDS - the flow must carry on and re-submit the
# form, which is the step a standalone harness forgot, leaving a solved captcha
# and no account.

def _vision_driver(monkeypatch, outcome, note_sink=None):
    """A signup whose Sign Up click raises a challenge, plus a stubbed solver
    returning `outcome`. Returns the create_account result."""
    drv = FakeDriver()
    _fast_captcha(monkeypatch)
    state = {"cleared": False}

    class Flag:
        def is_displayed(self):
            if state["cleared"]:
                drv.url = "https://www.roblox.com/home"
                drv.cookies[ac.COOKIE_NAME] = {"value": "VISION-COOKIE"}
                return False
            return True

    def show_captcha():
        drv.elements.pop("#signup-username", None)
        drv.elements.pop("#signup-password", None)
        drv.elements["iframe[src*='funcaptcha']"] = [Flag()]

    drv.elements = {
        "#signup-username": FakeElement(),
        "#signup-password": FakeElement(),
        "#signup-button": FakeElement(tag="button", text="Sign Up",
                                      on_click=show_captcha),
    }

    import visioncaptcha

    def fake_play(d, client, on_status=None, stop_check=None, is_present=None,
                  mode="step"):
        if isinstance(outcome, Exception):
            raise outcome
        if outcome.get("ok"):
            state["cleared"] = True
        return outcome

    monkeypatch.setattr(visioncaptcha, "play_challenge", fake_play)

    def fake_manual(d, on_status, stop_check, timeout=None, note=None):
        if note_sink is not None:
            note_sink.append(note)
        state["cleared"] = True
        return True

    monkeypatch.setattr(ac, "wait_for_manual_solve", fake_manual)
    monkeypatch.setattr(ac, "_omni_accounts",
                        lambda: _accounts_returning("VISION-COOKIE", 777, "VisionUser"))
    return ac.create_account(on_status=lambda m: None,
                             driver_factory=lambda: drv,
                             solver_client=object())


def test_create_account_completes_when_the_solver_clears_the_puzzle(monkeypatch):
    res = _vision_driver(monkeypatch, {"ok": True, "rounds": 5, "reason": None})
    assert res["ok"], res
    assert res["username"] == "VisionUser"
    assert res["captcha_solved_by"] == "solver"


def test_a_solver_that_gives_up_hands_over_to_a_human(monkeypatch):
    """Stopping mid-puzzle must not cost the account."""
    notes = []
    res = _vision_driver(monkeypatch,
                         {"ok": False, "rounds": 2, "reason": "solver was unsure"},
                         note_sink=notes)
    assert res["ok"], res
    assert res["captcha_solved_by"] == "manual"
    assert notes and "could not finish" in notes[0]


def test_a_crashing_solver_never_costs_the_account(monkeypatch):
    notes = []
    res = _vision_driver(monkeypatch, RuntimeError("solver exploded"), note_sink=notes)
    assert res["ok"], res
    assert res["captcha_solved_by"] == "manual"


def test_no_solver_configured_still_waits_for_a_human(monkeypatch):
    """The default path when nobody has wired a solver in."""
    drv = FakeDriver()
    _fast_captcha(monkeypatch)
    notes = []
    state = {"cleared": False}

    class Flag:
        def is_displayed(self):
            if state["cleared"]:
                drv.url = "https://www.roblox.com/home"
                drv.cookies[ac.COOKIE_NAME] = {"value": "NOSOLVER"}
                return False
            return True

    def show_captcha():
        drv.elements.pop("#signup-username", None)
        drv.elements["iframe[src*='funcaptcha']"] = [Flag()]

    drv.elements = {
        "#signup-username": FakeElement(),
        "#signup-password": FakeElement(),
        "#signup-button": FakeElement(tag="button", text="Sign Up",
                                      on_click=show_captcha),
    }

    def fake_manual(d, on_status, stop_check, timeout=None, note=None):
        notes.append(note)
        state["cleared"] = True
        return True

    monkeypatch.setattr(ac, "wait_for_manual_solve", fake_manual)
    monkeypatch.setattr(ac, "_omni_accounts",
                        lambda: _accounts_returning("NOSOLVER", 5, "NoSolver"))
    res = ac.create_account(on_status=lambda m: None, driver_factory=lambda: drv)
    assert res["ok"] and res["captcha_solved_by"] == "manual"
    assert notes and "no automatic solver" in notes[0]
