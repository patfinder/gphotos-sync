"""
Move photos to Google Photos' Trash by driving photos.google.com in a real
browser (Playwright). Google's APIs offer no way to delete library items,
so this is the only automatable route. See docs/design.md.

Safety rules, applied per photo:
  * the local copy must exist and match the recorded SHA-256;
  * the photo is found by searching its original filename, and each
    search hit is opened and its info panel must show that exact filename
    and the capture date (within one day, to allow for time zones);
  * exactly one hit may match, or the photo is skipped as ambiguous;
  * unless --yes is given, each deletion is confirmed in the terminal.
Deleted items go to Trash and can be restored there for 60 days.
"""
import json
import os
import re
import sys
import time
from datetime import timedelta
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse

BASE_URL = os.environ.get("GPHOTOS_BASE_URL", "https://photos.google.com/")
# Interface language requested on every page (Google's `hl` parameter). The
# tools read English labels and dates, whatever the account's own language
# is. Set GPHOTOS_HL="" to leave pages in the account's language.
HL = os.environ.get("GPHOTOS_HL", "en")


def with_hl(url):
    """`url` with hl=<HL> added (or replaced) if it's a Google Photos page."""
    if not HL:
        return url
    u = urlparse(url)
    if u.netloc != urlparse(BASE_URL).netloc:
        return url
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "hl"]
    return u._replace(query=urlencode(q + [("hl", HL)])).geturl()

# --- Assumptions about the photos.google.com page, kept in one place. -------
# `gphotos_browser.py probe` reports how well they match the real site.
ITEM_LINK = 'a[href*="/photo/"]'                 # a photo/video tile in any grid
ALBUM_LINK = 'a[href*="/album/"], a[href*="/share/"]'
ALBUM_PAGES = ["albums", "sharing"]              # own albums, shared albums
MEDIA_ID_RE = re.compile(r"/photo/([A-Za-z0-9_-]{10,})")
ALBUM_ID_RE = re.compile(r"/(?:album|share)/([A-Za-z0-9_-]{10,})(?:\?|$|/?$)")
# Tile labels look like "Photo - Landscape - Nov 5, 2026, 12:34:56 PM".
TILE_LABEL_RE = re.compile(
    r"\b(?P<kind>Photo|Video)\b.*?"
    r"(?P<date>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, \d{4})"
    r"(?:,?\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?)[\s\u202f]*(?P<ampm>[AP]M))?", re.I)
# The viewer's info panel lists a photo's albums under this heading.
INFO_ALBUMS_RE = re.compile(r"(?m)^\s*Albums?\s*$")
# ... and that list ends at the "Details" heading.
INFO_DETAILS_RE = re.compile(r"^\s*Details\s*$")
# Viewer controls used to move items between accounts (gphotos_backup_v2.py).
ADD_MENU_OPENER_RE = re.compile(r"^More options$", re.I)
ADD_TO_ALBUM_RE = re.compile(r"^Add to album$", re.I)
# On a selection, "Add to album" (the + button) opens a small menu whose
# "Album" item opens the picker.
ALBUM_MENU_ITEM_RE = re.compile(r"^Album$", re.I)
ADD_TO_SHARED_ALBUM_RE = re.compile(r"^Add to shared album$", re.I)
ADD_TO_BUTTON_RE = re.compile(r"^Add to$", re.I)
ALBUM_KIND_RE = re.compile(r"^(Shared album|Album)$", re.I)
SAVE_PHOTOS_RE = re.compile(r"^Save photos$", re.I)       # shared album page, with tiles ticked
CONFIRM_SAVE_RE = re.compile(r"^(Save|Save photos|OK|Done|Continue)$", re.I)
SAVE_RE = re.compile(r"^(Save|Save to (your )?(library|photos)|Add to (your )?(library|photos))$",
                     re.I)


def info_album_lines(text):
    """Non-empty lines listed under the info panel's "Albums" heading, up to
    "Details". None if there's no "Albums" heading at all."""
    lines = [l.strip() for l in (text or "").splitlines()]
    start = next((k for k, l in enumerate(lines) if INFO_ALBUMS_RE.match(l)), None)
    if start is None:
        return None
    out = []
    for l in lines[start + 1:]:
        if INFO_DETAILS_RE.match(l):
            break
        if l:
            out.append(l)
    return out


# JS: [href, label] for every element matching the selector; the label comes
# from the link or the first labelled element inside it.
_LINKS_JS = """els => els.map(e => [e.href,
    e.getAttribute('aria-label') ||
    (e.querySelector('[aria-label]') || {getAttribute: () => ''}).getAttribute('aria-label') ||
    e.innerText || ''])"""


def media_id(href):
    m = MEDIA_ID_RE.search(href)
    return m.group(1) if m else None


def parse_tile_label(label):
    """(kind, naive local datetime) from a grid tile label, or (None, None)."""
    from datetime import datetime
    m = TILE_LABEL_RE.search(label or "")
    if not m:
        return None, None
    date = m.group("date").replace(".", "")
    try:
        d = datetime.strptime(date, "%b %d, %Y")
    except ValueError:
        try:
            d = datetime.strptime(date, "%B %d, %Y")
        except ValueError:
            return None, None
    if m.group("time"):
        t = m.group("time")
        fmt = "%I:%M:%S %p" if t.count(":") == 2 else "%I:%M %p"
        try:
            tt = datetime.strptime(f"{t} {m.group('ampm').upper()}", fmt)
            d = d.replace(hour=tt.hour, minute=tt.minute, second=tt.second)
        except ValueError:
            pass
    return m.group("kind").lower(), d
MAX_CANDIDATES = 25
NAV_TIMEOUT_MS = 30_000


class Stop(Exception):
    pass


class NeedLogin(Exception):
    """The browser profile isn't signed in (or the sign-in expired)."""


def _date_strings(when):
    """Ways the info panel might print the capture date (English UI),
    for the day before, of, and after `when` (time-zone slack)."""
    out = set()
    for delta in (-1, 0, 1):
        d = (when + timedelta(days=delta)).astimezone()
        mon, day, year = d.strftime("%b"), d.day, d.year
        out.add(f"{mon} {day}, {year}")
        out.add(f"{d.strftime('%B')} {day}, {year}")
        out.add(f"{day} {mon} {year}")
    return out


def _line_re(text):
    return re.compile(r"(?m)^\s*" + re.escape(text) + r"\s*$")


def _find_chrome():
    import shutil
    for name in (os.environ.get("GPHOTOS_CHROME"), "google-chrome", "google-chrome-stable",
                 "chromium", "chromium-browser",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if name and (shutil.which(name) or os.path.isfile(name)):
            return shutil.which(name) or name
    return None


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# One Playwright per process, shared by every PhotosWeb (v2 drives two
# accounts at once; a second sync_playwright().start() fails).
_PW = None
_PW_USERS = 0


def _playwright():
    global _PW, _PW_USERS
    if _PW is None:
        from playwright.sync_api import sync_playwright
        _PW = sync_playwright().start()
    _PW_USERS += 1
    return _PW


def _release_playwright():
    global _PW, _PW_USERS
    _PW_USERS -= 1
    if _PW_USERS <= 0 and _PW is not None:
        try:
            _PW.stop()
        finally:
            _PW, _PW_USERS = None, 0


class PhotosWeb:
    """A Chrome window driven by Playwright.

    Google refuses sign-in ("This browser or app may not be secure") in a
    browser Playwright launched itself, because of its automation flags.
    So Chrome is started here as an ordinary program with its own profile
    folder and a local-only debugging port, and Playwright attaches to it
    (connect_over_cdp). The user signs in to their own account by hand in
    that window; the sign-in is kept in the profile folder.
    """

    def __init__(self, profile_dir, headless=os.environ.get("GPHOTOS_HEADLESS") == "1"):
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            sys.exit("This needs Playwright: pip install playwright")
        import subprocess
        import urllib.request

        chrome = _find_chrome()
        if not chrome:
            sys.exit("Google Chrome (or Chromium) wasn't found. Install it, or set "
                     "GPHOTOS_CHROME to its path.")
        os.makedirs(profile_dir, exist_ok=True)
        port = _free_port()
        cmd = [chrome, f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}",
               "--remote-debugging-address=127.0.0.1", "--no-first-run", "--lang=en-US",
               "--no-default-browser-check", "--window-size=1400,900"]
        if headless:
            cmd.append("--headless=new")
        self._proc = subprocess.Popen(cmd + ["about:blank"], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        endpoint = f"http://127.0.0.1:{port}"
        for _ in range(60):
            if self._proc.poll() is not None:
                sys.exit("Chrome exited straight away. Is another Chrome window already using "
                         f"{profile_dir}? Close it and try again.")
            try:
                urllib.request.urlopen(endpoint + "/json/version", timeout=1).read()
                break
            except OSError:
                time.sleep(0.5)
        else:
            self._proc.terminate()
            sys.exit("Couldn't connect to Chrome's debugging port.")

        self._pw = None
        try:
            self._pw = _playwright()
            self.browser = self._pw.chromium.connect_over_cdp(endpoint)
            self.ctx = self.browser.contexts[0]
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            self.page.set_default_timeout(NAV_TIMEOUT_MS)
        except BaseException:
            # Don't leave Chrome running: it would lock the profile folder.
            if self._pw is not None:
                _release_playwright()
            self._proc.terminate()
            raise

    def close(self):
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            _release_playwright()
        finally:
            # SIGTERM lets Chrome shut down cleanly and save the sign-in cookies.
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except Exception:
                self._proc.kill()

    # Network errors worth retrying: DNS hiccups, dropped connections, etc.
    _TRANSIENT = ("ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED",
                  "ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_TIMED_OUT",
                  "ERR_TIMED_OUT", "ERR_ADDRESS_UNREACHABLE", "ERR_NETWORK_IO_SUSPENDED")

    def goto(self, url, attempts=6):
        """Open `url`. Waits only for the HTML to be parsed, not the full
        "load" event: Google Photos keeps streaming in the background, so
        "load" can take longer than any sensible timeout. Callers wait for
        the elements they need. Temporary network errors and timeouts are
        retried with growing waits (2, 4, 8, 16, 30 s), then it gives up
        with a clear message."""
        for i in range(attempts):
            try:
                return self.page.goto(with_hl(url), wait_until="domcontentloaded",
                                      timeout=60_000)
            except Exception as e:
                msg = str(e)
                reason = ("timeout" if type(e).__name__ == "TimeoutError" or "Timeout" in msg
                          else next((c for c in self._TRANSIENT if c in msg), None))
                if reason is None:
                    raise
                if i == attempts - 1:
                    sys.exit(f"Couldn't load {url} ({msg.splitlines()[0]}). "
                             "Check your internet connection and run the command again; "
                             "finished work is saved and won't be repeated.")
                wait = min(2 ** (i + 1), 30)
                print(f"  {reason} loading page, retrying in {wait}s...",
                      file=sys.stderr, flush=True)
                time.sleep(wait)

    def page_title(self):
        """The open page's title without the " - Google Photos" suffix."""
        try:
            return re.sub(r"\s*[-–]\s*Google Photos\s*$", "", self.page.title()).strip()
        except Exception:
            return ""

    def signed_in(self):
        return not ("accounts.google.com" in self.page.url or "/about" in self.page.url)

    def ensure_logged_in(self, interactive=True):
        """With interactive=False (scheduled runs) never prompt: raise
        NeedLogin so the caller can notify the user instead."""
        self.goto(BASE_URL)
        while not self.signed_in():
            if not interactive:
                raise NeedLogin("Google Photos sign-in is missing or has expired")
            input("Sign in to Google Photos in the Chrome window, then press Enter here... ")
            self.goto(BASE_URL)

    def _settle(self):
        """Short pause for the page's scripts to draw. Google Photos never
        goes network-idle, so waiting for that would always hit the
        timeout; callers wait for the specific elements they need."""
        try:
            self.page.wait_for_load_state("load", timeout=2_000)
        except Exception:
            pass
        time.sleep(1.0)

    def _candidates(self, filename):
        self.goto(urljoin(BASE_URL, "search/" + quote(filename, safe="")))
        self._settle()
        hrefs = self.page.eval_on_selector_all(
            'a[href*="/photo/"]', "els => [...new Set(els.map(e => e.href))]")
        return hrefs[:MAX_CANDIDATES]

    def info_panel(self, url):
        """Open the photo at `url` with its info panel. Returns (loaded, text):
        loaded is True only if the panel's "Details" section is showing, so a
        caller can tell "no albums" apart from "panel didn't load"."""
        self.goto(url)
        self._settle()
        # Open the info panel ("i" toggles it; only press if not open yet).
        details = self.page.get_by_text(re.compile(r"^Details$")).first
        if not details.is_visible():
            self.page.keyboard.press("i")
            try:
                details.wait_for(state="visible", timeout=8_000)
            except Exception:
                pass
            time.sleep(1.5)  # its sections (albums etc.) fill in just after
        loaded = details.is_visible()
        return loaded, self.page.inner_text("body")

    def _info_text(self, url):
        return self.info_panel(url)[1]

    def find_photo(self, filename, when):
        """URL of the one photo whose info panel shows `filename` and
        `when`'s date; None if none; raises ValueError if several match."""
        name_re = _line_re(filename)
        dates = _date_strings(when)
        matches = []
        for url in self._candidates(filename):
            text = self._info_text(url)
            if not name_re.search(text):
                continue
            if not any(d in text for d in dates):
                continue
            matches.append(url)
        if len(matches) > 1:
            raise ValueError(f"{len(matches)} photos match")
        return matches[0] if matches else None

    def trash(self, url):
        """Move the photo at `url` to Trash via the toolbar button and the
        confirmation dialog.

        On the real site the toolbar button and the dialog's button are both
        labelled "Move to trash", so the buttons present before clicking are
        tagged first. Only a new, untagged one counts as the confirmation."""
        self.goto(url)
        self._settle()
        tool_re = re.compile(r"^(Delete|Move to (trash|bin))$", re.I)
        confirm_re = re.compile(r"^Move to (trash|bin)$", re.I)
        tool = self.page.get_by_role("button", name=tool_re)
        try:
            tool.first.wait_for(state="visible", timeout=10_000)
        except Exception:
            pass
        tool.evaluate_all("els => els.forEach(e => e.setAttribute('data-gps-toolbar', '1'))")
        if tool.count() and tool.first.is_visible():
            tool.first.click()
        else:
            self.page.keyboard.press("#")  # Google Photos' delete shortcut
        confirm = (self.page.get_by_role("button", name=confirm_re)
                   .and_(self.page.locator(":not([data-gps-toolbar])")).last)
        confirm.wait_for(state="visible", timeout=10_000)
        confirm.click()
        confirm.wait_for(state="hidden", timeout=15_000)
        self._settle()

    # ---- browsing the library directly (gphotos_browser.py) ----

    def collect(self, url, selector, key_fn, stop=None, max_idle=6, progress=None):
        """Open `url`, scroll to the end (or until stop(items) is true) and
        return {key: {"url", "label"}} in page order. The grids are
        virtualised (tiles are removed once scrolled past), so the visible
        tiles are harvested after every scroll step."""
        self.goto(url)
        try:  # the grid is drawn by script after the HTML arrives
            self.page.wait_for_selector(selector, timeout=20_000)
        except Exception:
            pass  # e.g. an empty album; the scroll loop below copes
        self._settle()
        vp = self.page.viewport_size or {"width": 1400, "height": 900}
        self.page.mouse.move(vp["width"] / 2, vp["height"] / 2)
        items, idle = {}, 0
        while idle < max_idle:
            new = 0
            for href, label in self.page.eval_on_selector_all(selector, _LINKS_JS):
                key = key_fn(href)
                if key and key not in items:
                    items[key] = {"url": href, "label": label.strip()}
                    new += 1
            if progress and new:
                progress(len(items))
            if stop and stop(items):
                break
            idle = 0 if new else idle + 1
            self.page.mouse.wheel(0, vp["height"] * 1.5)
            time.sleep(0.8 if new else 1.5)
        return items

    def album_urls(self):
        found = {}
        for page in ALBUM_PAGES:
            def key(href):
                m = ALBUM_ID_RE.search(href)
                return m.group(1) if m and not media_id(href) else None
            found.update(self.collect(urljoin(BASE_URL, page), ALBUM_LINK, key, max_idle=6))
        return found

    def info_shows_albums(self, url, album_titles=()):
        """True if the photo's info panel shows it's in an album: either an
        "Albums" heading, or a line that is exactly one of `album_titles`
        (known from the album scan), so it doesn't depend on how the panel
        labels the section."""
        text = self._info_text(url)
        if INFO_ALBUMS_RE.search(text):
            return True
        titles = {t.strip() for t in album_titles if t and t.strip()}
        return any(line.strip() in titles for line in text.splitlines())

    # ---- moving items between accounts (gphotos_backup_v2.py) ----

    def _open_menu_item(self, opener_re, item_re):
        """Click a toolbar button, then a menu item. False if either isn't there."""
        btn = self.page.get_by_role("button", name=opener_re).first
        try:
            btn.wait_for(state="visible", timeout=6_000)
            btn.click()
            item = self.page.get_by_role("menuitem", name=item_re).first
            item.wait_for(state="visible", timeout=5_000)
            item.click()
            return True
        except Exception:
            self.page.keyboard.press("Escape")
            return False

    def add_to_album(self, url, title):
        """Add the photo at `url` to the album called `title` through the
        viewer's menu and the album-picker dialog. True once the album was
        clicked in the picker. The caller confirms the result separately
        (from the album's page), so a wrong guess about the page only means
        "not added yet", never a false success."""
        self.goto(url)
        self._settle()
        routes = [
            (ADD_MENU_OPENER_RE, ADD_TO_ALBUM_RE),
            (ADD_MENU_OPENER_RE, ADD_TO_SHARED_ALBUM_RE),
            (ADD_TO_BUTTON_RE, ALBUM_KIND_RE),
        ]
        for opener_re, item_re in routes:
            if self._open_menu_item(opener_re, item_re) and self._pick_album(title):
                return True
        return False

    def _pick_album(self, title):
        """In the open album-picker dialog, click the album `title`."""
        dialog = self.page.get_by_role("dialog").last
        try:
            dialog.wait_for(state="visible", timeout=8_000)
            # Click the album's row (role=option): on the real site the row
            # takes the click, not the title text inside it.
            row = dialog.get_by_role("option", name=re.compile(rf"^{re.escape(title)}(\s*·|$)"))
            target = row.or_(dialog.get_by_text(title, exact=True)).first
            target.wait_for(state="visible", timeout=8_000)
        except Exception:
            self.page.keyboard.press("Escape")
            return False
        target.click()
        try:
            dialog.wait_for(state="hidden", timeout=15_000)
        except Exception:
            pass
        self._settle()
        return True

    # Tiles on an album page carry a checkbox labelled like the tile. JS: for
    # each checkbox, the ID of the photo link in the same tile (walking up to
    # the nearest ancestor that holds a photo link), whether that link's
    # label matches the checkbox's, and aria-checked. Tags each checkbox
    # with data-gps-id so it can be clicked.
    _TILE_CHECKBOX_JS = r"""els => els.map(cb => {
        let n = cb, a = null;
        for (let k = 0; k < 8 && n && !a; k++) {
            if (n.matches && n.matches('a[href*="/photo/"]')) { a = n; break; }
            n = n.parentElement;
            a = n && n.querySelector('a[href*="/photo/"]');
        }
        const m = a && a.href.match(/\/photo\/([A-Za-z0-9_-]{10,})/);
        if (!m) return null;
        const lab = a.getAttribute('aria-label') ||
            (a.querySelector('[aria-label]') || {getAttribute: () => ''}).getAttribute('aria-label') || '';
        cb.setAttribute('data-gps-id', m[1]);
        return [m[1], cb.getAttribute('aria-checked') === 'true',
                (cb.getAttribute('aria-label') || '') === lab];
    }).filter(Boolean)"""

    def select_items(self, url, ids, max_idle=6):
        """Open the album at `url` and tick the tiles whose photo IDs are in
        `ids`, scrolling as needed. Returns the set of IDs ticked."""
        ids = set(ids)
        self.goto(url)
        try:
            self.page.wait_for_selector(ITEM_LINK, timeout=20_000)
        except Exception:
            return set()
        self._settle()
        vp = self.page.viewport_size or {"width": 1400, "height": 900}
        chosen, attempted, idle = set(), set(), 0
        while idle < max_idle and chosen != ids:
            new = 0
            while True:
                # Re-read after every click: ticking can redraw the tiles.
                rows = self.page.eval_on_selector_all("[role=checkbox]", self._TILE_CHECKBOX_JS)
                for mid, checked, same_label in rows:
                    if mid in ids and checked and same_label and mid not in chosen:
                        chosen.add(mid)   # counted only once the page shows it ticked
                        new += 1
                nxt = next((mid for mid, checked, same_label in rows
                            if mid in ids and same_label and not checked
                            and mid not in attempted), None)
                if nxt is None:
                    break
                attempted.add(nxt)
                box = self.page.locator(f'[role=checkbox][data-gps-id="{nxt}"]').first
                try:
                    box.hover(timeout=3_000)
                    box.click(timeout=3_000)
                    time.sleep(0.3)
                except Exception:
                    pass
            idle = 0 if new else idle + 1
            self.page.mouse.move(vp["width"] / 2, vp["height"] / 2)
            self.page.mouse.wheel(0, vp["height"] * 1.2)
            time.sleep(0.8 if new else 1.5)
        return chosen

    def _click_button(self, name_re, timeout=8_000):
        btn = self.page.get_by_role("button", name=name_re).first
        try:
            btn.wait_for(state="visible", timeout=timeout)
            btn.click()
            return True
        except Exception:
            return False

    def save_selected(self):
        """Click "Save photos" for the ticked tiles (and confirm if asked)."""
        if not self._click_button(SAVE_PHOTOS_RE):
            return False
        time.sleep(1.5)
        dialog = self.page.get_by_role("dialog").last
        try:
            if dialog.is_visible():
                dialog.get_by_role("button", name=CONFIRM_SAVE_RE).last.click(timeout=5_000)
        except Exception:
            pass
        self._settle()
        return True

    def add_selected_to_album(self, title):
        """Click "Add to album" for the ticked tiles and pick `title`."""
        if not self._click_button(ADD_TO_ALBUM_RE):
            return False
        time.sleep(1)
        item = self.page.get_by_role("menuitem", name=ALBUM_MENU_ITEM_RE).first
        try:
            if item.is_visible():
                item.click(timeout=5_000)
        except Exception:
            pass
        return self._pick_album(title)

    def save_to_library(self, url):
        """In an album shared with this account: save the photo at `url` into
        this account's own library. True if a save button/menu item was
        clicked, False if none was offered (already saved, or the page isn't
        what we expect)."""
        self.goto(url)
        self._settle()
        btn = self.page.get_by_role("button", name=SAVE_RE).first
        try:
            btn.wait_for(state="visible", timeout=8_000)
            btn.click()
        except Exception:
            if not self._open_menu_item(ADD_MENU_OPENER_RE, SAVE_RE):
                return False
        self._settle()
        return True

    def controls(self):
        """Labels of the buttons and menu items on the open page (for probes)."""
        return self.page.eval_on_selector_all(
            "[role=button], button, [role=menuitem]",
            "els => [...new Set(els.map(e => (e.getAttribute('aria-label') || e.innerText || '')"
            ".trim()).filter(Boolean))]")

    def download(self, url, tmp_dir):
        """Download the original of the photo at `url` into tmp_dir.
        Returns (path, filename Google suggested)."""
        self.goto(url)
        self._settle()
        try:
            with self.page.expect_download(timeout=20_000) as dl:
                self.page.keyboard.press("Shift+D")  # Google Photos' download shortcut
        except Exception:
            with self.page.expect_download(timeout=120_000) as dl:
                self.page.get_by_role("button", name=re.compile(r"^More options$", re.I)).first.click()
                self.page.get_by_role("menuitem", name=re.compile(r"^Download", re.I)).first.click()
        d = dl.value
        name = d.suggested_filename
        path = os.path.join(tmp_dir, f".{os.getpid()}-{int(time.time() * 1000)}.part")
        d.save_as(path)
        return path, name


# ------------------------------------------------------------ driver ----

def run_deletions(ledger, *, profile_dir, assume_yes, dry_run, verify_local):
    """ledger: list of dicts (local, sha256, title, taken) not yet deleted.
    `verify_local(rec)` -> bool; `ledger.mark_deleted(rec)` is called on
    success. Returns a stats dict."""
    from datetime import datetime

    stats = {"trashed": 0, "verified (dry run)": 0, "not found online": 0,
             "ambiguous (skipped)": 0, "local copy missing/changed (skipped)": 0,
             "undated (skipped)": 0, "video part not copied (skipped)": 0,
             "declined": 0, "errors": 0}
    pending = ledger.pending()
    if not pending:
        print("Nothing to delete from Google Photos.")
        return stats
    print(f"{len(pending)} photo(s) to move to Google Photos Trash"
          f"{' (dry run: verify only)' if dry_run else ''}.")

    web = PhotosWeb(profile_dir)
    yes_all = assume_yes
    try:
        web.ensure_logged_in()
        for rec in pending:
            label = f"{rec['title']} [{(rec['taken'] or 'undated')[:10]}]"
            if not verify_local(rec):
                stats["local copy missing/changed (skipped)"] += 1
                print(f"  skip, local copy missing or changed: {label}")
                continue
            if ledger.missing_parts(rec, verify_local):
                stats["video part not copied (skipped)"] += 1
                print(f"  skip, its Live Photo/motion video part isn't safely copied: {label}")
                continue
            if not rec["taken"]:
                stats["undated (skipped)"] += 1
                print(f"  skip, no capture date to verify against: {label}")
                continue
            when = datetime.fromisoformat(rec["taken"])
            try:
                url = web.find_photo(rec["title"], when)
            except ValueError as e:
                stats["ambiguous (skipped)"] += 1
                print(f"  skip, ambiguous ({e}): {label}")
                continue
            except Exception as e:
                stats["errors"] += 1
                print(f"  error searching {label}: {e}")
                continue
            if url is None:
                stats["not found online"] += 1
                print(f"  not found online (already deleted?): {label}")
                continue
            if dry_run:
                stats["verified (dry run)"] += 1
                print(f"  would trash: {label} -> {url}")
                continue
            if not yes_all:
                ans = input(f"  Move to Trash: {label}  {url}  [y/N/a=all/q=quit] ").strip().lower()
                if ans == "q":
                    raise Stop
                if ans == "a":
                    yes_all = True
                elif ans != "y":
                    stats["declined"] += 1
                    continue
            try:
                web.trash(url)
            except Exception as e:
                stats["errors"] += 1
                print(f"  error trashing {label}: {e}")
                continue
            ledger.mark_deleted(rec, url)
            stats["trashed"] += 1
            print(f"  trashed: {label}")
    except Stop:
        print("Stopped.")
    finally:
        web.close()
    return stats


class Ledger:
    """Append-only JSONL in the destination folder recording copied items
    and which of them have been moved to Google Photos Trash."""

    NAME = ".takeout_sync_ledger.jsonl"

    def __init__(self, dest_root):
        self.path = os.path.join(dest_root, self.NAME)
        self.copied = {}       # sha256 -> record
        self.deleted = set()   # sha256
        if os.path.exists(self.path):
            with open(self.path) as f:
                for line in f:
                    rec = json.loads(line)
                    if rec["event"] == "copied":
                        self.copied[rec["sha256"]] = rec
                    elif rec["event"] == "deleted":
                        self.deleted.add(rec["sha256"])

    def _append(self, rec):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def record_copy(self, local, sha256, title, taken, parts=None, part_of=None):
        """parts: sha256s of a still's Live Photo/motion video halves.
        part_of: for a video half, its still's sha256. A later record for the
        same sha256 replaces the earlier one (e.g. ledgers written before
        parts were tracked get updated on the next run)."""
        rec = {"event": "copied", "local": str(local), "sha256": sha256,
               "title": title, "taken": taken.isoformat() if taken else None}
        if parts:
            rec["parts"] = sorted(parts)
        if part_of:
            rec["part_of"] = part_of
        if self.copied.get(sha256) == rec:
            return
        self.copied[sha256] = rec
        self._append(rec)

    def mark_deleted(self, rec, url):
        # Trashing the still trashes the whole Live Photo, video half included.
        for sha in [rec["sha256"]] + rec.get("parts", []):
            self.deleted.add(sha)
            self._append({"event": "deleted", "sha256": sha, "url": url,
                          "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})

    def missing_parts(self, rec, verify_local):
        """Video halves of `rec` that aren't safely copied."""
        return [s for s in rec.get("parts", [])
                if s not in self.copied or not verify_local(self.copied[s])]

    def pending(self):
        """Online items still to trash. Video halves aren't separate online
        items: they go together with their still."""
        return [r for s, r in self.copied.items()
                if s not in self.deleted and "part_of" not in r]
