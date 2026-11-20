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
            sys.exit("Deleting needs Playwright: pip install playwright "
                     "(then, only if you don't have Google Chrome: playwright install chromium)")
        self._pw = sync_playwright().start()
        kwargs = dict(user_data_dir=profile_dir, headless=headless,
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


# ------------------------------------------------------------ driver ----

def run_deletions(ledger, *, profile_dir, assume_yes, dry_run, verify_local):
    """ledger: list of dicts (local, sha256, title, taken) not yet deleted.
    `verify_local(rec)` -> bool; `ledger.mark_deleted(rec)` is called on
    success. Returns a stats dict."""
    from datetime import datetime

    stats = {"trashed": 0, "verified (dry run)": 0, "not found online": 0,
             "ambiguous (skipped)": 0, "local copy missing/changed (skipped)": 0,
             "undated (skipped)": 0, "declined": 0, "errors": 0}
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

    def record_copy(self, local, sha256, title, taken):
        if sha256 in self.copied:
            return
        rec = {"event": "copied", "local": str(local), "sha256": sha256,
               "title": title, "taken": taken.isoformat() if taken else None}
        self.copied[sha256] = rec
        self._append(rec)

    def mark_deleted(self, rec, url):
        self.deleted.add(rec["sha256"])
        self._append({"event": "deleted", "sha256": rec["sha256"], "url": url,
                      "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})

    def pending(self):
        return [r for s, r in self.copied.items() if s not in self.deleted]
