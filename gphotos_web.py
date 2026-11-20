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
from urllib.parse import quote, urljoin

BASE_URL = os.environ.get("GPHOTOS_BASE_URL", "https://photos.google.com/")

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


class PhotosWeb:
    def __init__(self, profile_dir, headless=os.environ.get("GPHOTOS_HEADLESS") == "1"):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sys.exit("This needs Playwright: pip install playwright "
                     "(then, only if you don't have Google Chrome: playwright install chromium)")
        self._pw = sync_playwright().start()
        kwargs = dict(user_data_dir=profile_dir, headless=headless, accept_downloads=True,
                      viewport={"width": 1400, "height": 900})
        try:  # Real Chrome is less likely to be refused by Google sign-in.
            self.ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except Exception:
            self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(NAV_TIMEOUT_MS)

    def close(self):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def ensure_logged_in(self):
        self.page.goto(BASE_URL)
        while "accounts.google.com" in self.page.url or "/about" in self.page.url:
            input("Sign in to Google Photos in the browser window, then press Enter here... ")
            self.page.goto(BASE_URL)

    def _settle(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        time.sleep(1.0)

    def _candidates(self, filename):
        self.page.goto(urljoin(BASE_URL, "search/" + quote(filename, safe="")))
        self._settle()
        hrefs = self.page.eval_on_selector_all(
            'a[href*="/photo/"]', "els => [...new Set(els.map(e => e.href))]")
        return hrefs[:MAX_CANDIDATES]

    def _info_text(self, url):
        self.page.goto(url)
        self._settle()
        # Open the info panel ("i" toggles it; only press if not open yet).
        if not self.page.get_by_text(re.compile(r"^Details$")).first.is_visible():
            self.page.keyboard.press("i")
            time.sleep(1.0)
        return self.page.inner_text("body")

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
        """Move the photo at `url` to Trash, then check that it's gone."""
        self.page.goto(url)
        self._settle()
        btn = self.page.get_by_role("button", name=re.compile(r"^(Delete|Move to (trash|bin))$", re.I))
        if btn.count() and btn.first.is_visible():
            btn.first.click()
        else:
            self.page.keyboard.press("#")  # Google Photos' delete shortcut
        confirm = self.page.get_by_role("button", name=re.compile(r"^Move to (trash|bin)$", re.I))
        confirm.last.wait_for(state="visible", timeout=10_000)
        confirm.last.click()
        confirm.last.wait_for(state="hidden", timeout=15_000)
        self._settle()

    # ---- browsing the library directly (gphotos_browser.py) ----

    def collect(self, url, selector, key_fn, stop=None, max_idle=6, progress=None):
        """Open `url`, scroll to the end (or until stop(items) is true) and
        return {key: {"url", "label"}} in page order. The grids are
        virtualised (tiles are removed once scrolled past), so the visible
        tiles are harvested after every scroll step."""
        self.page.goto(url)
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
            found.update(self.collect(urljoin(BASE_URL, page), ALBUM_LINK, key, max_idle=3))
        return found

    def info_shows_albums(self, url):
        return bool(INFO_ALBUMS_RE.search(self._info_text(url)))

    def download(self, url, tmp_dir):
        """Download the original of the photo at `url` into tmp_dir.
        Returns (path, filename Google suggested)."""
        self.page.goto(url)
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
