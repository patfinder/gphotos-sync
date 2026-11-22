#!/usr/bin/env python3
"""
Backup v2: move photos from the MAIN account to the BACKUP account by
sharing, keeping a copy on this computer too.

    phone -> MAIN account --(shared album)--> BACKUP account (saved + own album)
                    \\--> this computer (originals, YYYY/MM)

Both accounts are driven through photos.google.com in their own signed-in
Chrome profile; no Google Cloud project or API token is needed.

One-time setup (by hand, see README):
  - MAIN account: create an album (default "_To Backup") and share it with
    the BACKUP account.
  - BACKUP account: join that shared album, and create an album of its own
    (default "From main") to hold the saved copies.

Each run (normally from a systemd timer, unattended):
  1. MAIN: every photo/video taken on/after `since` is downloaded to the
     computer (hashed, recorded), then added to the shared album.
  2. BACKUP: each new photo in the shared album is saved to the backup
     account's library and added to its own album. The copies are then
     counted on that album's page.
  3. MAIN: a photo is moved to Trash only if
       - its local files re-hash correctly, AND
       - its copy is on the backup account's own album, AND
       - it's not in the keep album, and its info panel lists the shared
         album and no other album.
     Before the first deletion of a run, the album check is self-tested on
     keep-album photos. Before any deletion, photos trashed earlier are
     checked to still be in the backup album; if one is missing, nothing is
     deleted and you're notified (it can be restored from Trash).

Commands:
    python gphotos_backup_v2.py init            # write backup_v2.toml to edit
    python gphotos_backup_v2.py login-main      # sign in to the MAIN account (Chrome)
    python gphotos_backup_v2.py login-backup    # sign in to the BACKUP account (Chrome)
    python gphotos_backup_v2.py check           # verify the setup (accounts and albums)
    python gphotos_backup_v2.py probe           # show the page controls (for troubleshooting)
    python gphotos_backup_v2.py run --dry-run   # show what a run would do
    python gphotos_backup_v2.py run             # do it (this is what the timer runs)
    python gphotos_backup_v2.py status
    python gphotos_backup_v2.py install-schedule [--time 02:00]
    python gphotos_backup_v2.py uninstall-schedule
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import gphotos_backup as v1
import gphotos_web as gw

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "backup_v2.toml"
STATE_NAME = ".gphotos_backup_v2_state.jsonl"
LOG_NAME = ".gphotos_backup_v2.log"
UNIT = "gphotos-backup-v2"
AUDIT_DAYS = 30   # re-check photos trashed this recently (Trash keeps them 60 days)

# The info panel shows a line under each album's name, e.g.
# "1 item · Shared · Nov 22". A line counts as such a subtitle only if every
# "·"-separated part is a count, "Shared" or a date, and no album has that
# exact title. Anything else counts as an album name, which keeps the photo.
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_DATE = rf"{_MONTH} \d{{1,2}}(?:, \d{{4}})?"
SUBTITLE_PART_RE = re.compile(
    r"^(?:Shared(?: album)?|\d[\d,]* (?:items?|photos?|videos?)|Today|Yesterday|\d{4}"
    rf"|{_DATE}|{_DATE} ?[–-] ?(?:{_MONTH} )?\d{{1,2}}(?:, \d{{4}})?)$", re.I)


def is_subtitle(line):
    parts = [p.strip(" \u00a0\u202f") for p in re.split(r"[·•]", line)]
    return bool(parts) and all(p and SUBTITLE_PART_RE.match(p) for p in parts)

CONFIG_TEMPLATE = '''# gphotos_backup_v2.py settings. Relative paths are relative to this file.

# Where the computer copy lives (YYYY/MM folders, plus state and log files).
archive_dir = "~/Pictures/gphotos-archive"

# Back up photos/videos TAKEN on or after this date. Every run looks at
# everything since this date, so photos that sync late are still caught.
since = "2026-11-01"

# MAIN account: photos in this album stay in the main account.
keep_album = "_Keep It"

# MAIN account: the album shared with the backup account (create it and share
# it with the backup account by hand once).
transfer_album = "_To Backup"

# BACKUP account: its own album that holds the saved copies (create it by hand
# once, in the backup account).
backup_album = "From main"

# Move photos to Trash in the main account once they're safely copied (and in
# no album other than the shared one).
delete_from_main = true

# Chrome profiles holding each account's sign-in (see login-main / login-backup).
main_browser_profile = ".gphotos_browser_profile"
backup_browser_profile = ".gphotos_browser_profile_backup"

# Folder layout in the archive: "year-month", "year" or "flat".
layout = "year-month"

# Run Chrome without a window. Scheduled runs switch to headless automatically
# when there's no display.
headless = false

# Also notify when a run succeeds (problems are always notified).
notify_success = false

# Also send the log to Loki (Grafana). Set to "" to turn it off.
loki_url = "http://127.0.0.1:3100"
'''


# ------------------------------------------------------------- config ----

class Config(v1.Config):
    def __init__(self, path):
        super().__init__(path)
        with open(self.path, "rb") as f:
            raw = tomllib.load(f)
        self.transfer_album = str(raw.get("transfer_album", "_To Backup"))
        self.backup_album = str(raw.get("backup_album", "From main"))
        v = Path(str(raw.get("backup_browser_profile", ".gphotos_browser_profile_backup"))).expanduser()
        self.backup_browser_profile = v if v.is_absolute() else self.path.parent / v
        if self.backup_browser_profile == self.main_browser_profile:
            sys.exit(f"{self.path}: main and backup need different browser profiles")
        if len({self.keep_album, self.transfer_album}) < 2:
            sys.exit(f"{self.path}: keep_album and transfer_album must differ")


# -------------------------------------------------------------- state ----

class State:
    """Append-only JSONL in the archive folder.
      downloaded: main item ID -> files [{local, sha256, size, mtime}], taken
      shared:     main item IDs added to the shared album
      saved:      backup-side tile IDs (in the shared album) already saved and
                  added to the backup album, with their key
      trashed:    main item IDs moved to Trash, with their key and time
      run:        summaries"""

    def __init__(self, archive):
        self.path = Path(archive) / STATE_NAME
        self.downloaded, self.shared, self.saved, self.trashed = {}, set(), {}, {}
        self.last_run = None
        if self.path.exists():
            for line in self.path.open():
                self._apply(json.loads(line))

    def _apply(self, r):
        ev = r.get("event")
        if ev == "downloaded":
            self.downloaded[r["id"]] = r
        elif ev == "shared":
            self.shared.add(r["id"])
        elif ev == "saved":
            self.saved[r["tile"]] = r
        elif ev == "trashed":
            self.trashed[r["id"]] = r
        elif ev == "run":
            self.last_run = r

    def add(self, event, **fields):
        rec = {"event": event, **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        self._apply(rec)
        return rec


# ------------------------------------------------------------ helpers ----

def tile_key(kind, taken):
    """What a photo is matched by across the two accounts: its kind and the
    date-time to the second shown on its tile. The accounts give the same
    photo different IDs."""
    return f"{kind}|{taken:%Y-%m-%dT%H:%M:%S}" if kind and taken else None


def label_key(label):
    return tile_key(*gw.parse_tile_label(label))


def album_index(web):
    """{title: url} of every album the account has or has joined. Album
    tiles don't carry reliable titles, so each album is opened once."""
    out = {}
    for a in web.album_urls().values():
        web.goto(a["url"])
        web._settle()
        title = web.page_title()
        if title and title not in out:
            out[title] = a["url"]
    return out


def other_albums(lines, transfer, known_titles):
    """Albums other than the shared one in an info panel's album list."""
    others = []
    for line in lines:
        if line == transfer:
            continue
        if is_subtitle(line) and line not in known_titles:
            continue
        others.append(line)
    return others


def album_check_works(web, keep_ids, cfg, known_titles, log, samples=3):
    """Self-test before deleting: keep-album photos must show an album other
    than the shared one when opened from the library."""
    picks = list(keep_ids)[:samples]
    if not picks:
        return False
    for mid in picks:
        loaded, text = web.info_panel(gw.urljoin(gw.BASE_URL, "photo/" + mid))
        lines = gw.info_album_lines(text) if loaded else None
        if not lines or not other_albums(lines, cfg.transfer_album, known_titles):
            log(f"  self-test: album NOT seen for a '{mid[:12]}…' keep-album photo")
            return False
    log(f"  self-test passed on {len(picks)} keep-album photo(s)")
    return True


def start_browser(profile, cfg, log, who):
    headless = cfg.headless or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    try:
        return gw.PhotosWeb(str(profile), headless=headless)
    except SystemExit as e:
        log.problem(f"Couldn't start Chrome for the {who} account: {e}")
        return None


def signed_in(web, who, cmd, log):
    try:
        web.ensure_logged_in(interactive=False)
        return True
    except gw.NeedLogin:
        log.problem(f"The {who} account isn't signed in in its browser (expired?). "
                    f"Run `python gphotos_backup_v2.py {cmd}`.")
        return False


# ---------------------------------------------------------------- run ----

COUNTERS = ("downloaded", "shared", "saved in backup", "trashed", "kept: in keep album",
            "kept: in another album", "kept: info panel didn't load",
            "kept: not in shared album yet", "kept: not in backup album yet",
            "would download", "would share", "would save", "would trash",
            "undated tiles skipped")


def cmd_run(cfg, args):
    root = cfg.archive_dir
    root.mkdir(parents=True, exist_ok=True)
    log = v1.Log(root / LOG_NAME, cfg.loki_url, dry=args.dry_run)
    # Same lock as v1: two tools must never work on one archive at once.
    lock = open(root / v1.LOCK_NAME, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another backup run is in progress; exiting.")
        return 0

    dry = args.dry_run
    state = State(root)
    c = dict.fromkeys(COUNTERS, 0)
    review = []
    log(f"=== v2 run{' (dry run)' if dry else ''}: since {cfg.since:%Y-%m-%d}, archive {root}")

    main = start_browser(cfg.main_browser_profile, cfg, log, "main")
    backup = None
    exit_code = 0
    try:
        if not main:
            return 1
        if not signed_in(main, "main", "login-main", log):
            return 2

        # ---- MAIN: albums, timeline, computer copy, sharing ----
        albums = album_index(main)
        known_titles = set(albums)
        deletions = cfg.delete_from_main
        transfer_url = albums.get(cfg.transfer_album)
        if not transfer_url:
            deletions = False
            log.problem(f"Album '{cfg.transfer_album}' wasn't found in the main account. "
                        "Create it and share it with the backup account (see README); "
                        "photos are only copied to the computer until then.")
        keep_url = albums.get(cfg.keep_album)
        keep_ids = set(main.collect(keep_url, gw.ITEM_LINK, gw.media_id)) if keep_url else set()
        if deletions and not keep_ids:
            deletions = False
            log.problem(f"Album '{cfg.keep_album}' is missing or empty in the main account, so "
                        "the album check can't be self-tested; no deletions this run.")
        transfer_ids = (set(main.collect(transfer_url, gw.ITEM_LINK, gw.media_id))
                        if transfer_url else set())

        items, undated = v1.scan_main(main, cfg.since)
        c["undated tiles skipped"] = undated
        items = [i for i in items if i["id"] not in state.trashed]
        log(f"Main account: {len(items)} item(s) taken since {cfg.since:%Y-%m-%d}; "
            f"{len(transfer_ids)} in '{cfg.transfer_album}'")

        added = False
        for item in items:
            mid = item["id"]
            item["key"] = tile_key(item["kind"], item["taken"])
            item["label"] = f"{item['taken']:%Y-%m-%d %H:%M} {item['kind']} {mid[:14]}…"
            rec = state.downloaded.get(mid)
            if not v1.files_intact(rec):
                if dry:
                    c["would download"] += 1
                    log(f"  would download: {item['label']}")
                else:
                    try:
                        rec = v1.download_item(main, item, cfg, state)
                        c["downloaded"] += 1
                        log(f"  downloaded: {item['label']} -> "
                            + ", ".join(Path(f["local"]).name for f in rec["files"]))
                    except Exception as e:
                        log.problem(f"download failed for {item['label']}: {e}")
                        continue
            if not transfer_url or mid in transfer_ids:
                continue
            if dry:
                c["would share"] += 1
                continue
            try:
                if main.add_to_album(item["url"], cfg.transfer_album):
                    state.add("shared", id=mid, key=item["key"])
                    added = True
                else:
                    log.problem(f"couldn't add to '{cfg.transfer_album}' (menu or album not "
                                f"found): {item['label']}")
            except Exception as e:
                log.problem(f"adding to '{cfg.transfer_album}' failed for {item['label']}: {e}")
        if added:
            # Confirm from the album's own page rather than trusting the clicks.
            before = len(transfer_ids)
            transfer_ids = set(main.collect(transfer_url, gw.ITEM_LINK, gw.media_id))
            c["shared"] = len(transfer_ids) - before
            log(f"'{cfg.transfer_album}' now has {len(transfer_ids)} item(s)")

        # ---- BACKUP: save shared photos, put them in the backup album ----
        backup_counts = None          # key -> copies on the backup album page
        if transfer_url:
            backup = start_browser(cfg.backup_browser_profile, cfg, log, "backup")
        if backup and signed_in(backup, "backup", "login-backup", log):
            b_albums = album_index(backup)
            b_shared = b_albums.get(cfg.transfer_album)
            b_own = b_albums.get(cfg.backup_album)
            if not b_shared:
                log.problem(f"The backup account doesn't see the shared album "
                            f"'{cfg.transfer_album}'. Open the invitation in the backup "
                            "account and join it (see README).")
            if not b_own:
                log.problem(f"Album '{cfg.backup_album}' wasn't found in the backup account. "
                            "Create it there (see README).")
            if b_shared and b_own:
                backup_counts = Counter(
                    label_key(t["label"])
                    for t in backup.collect(b_own, gw.ITEM_LINK, gw.media_id).values())
                tiles = backup.collect(b_shared, gw.ITEM_LINK, gw.media_id)
                # Copies the backup album should hold per key: this run's shared
                # photos plus the ones trashed earlier (already copied).
                wanted = Counter(i["key"] for i in items if i["id"] in transfer_ids)
                wanted.update(r["key"] for r in state.trashed.values() if r.get("key"))
                tried = defaultdict(list)     # key -> shared tile IDs worked on
                for tid, t in tiles.items():
                    key = label_key(t["label"])
                    if not key or tid in state.saved or backup_counts[key] >= wanted[key] > 0:
                        continue
                    if not wanted[key]:
                        continue  # not one of this run's photos (e.g. added by hand)
                    if dry:
                        c["would save"] += 1
                        continue
                    try:
                        backup.save_to_library(t["url"])
                        backup.add_to_album(t["url"], cfg.backup_album)
                        tried[key].append(tid)
                    except Exception as e:
                        log.problem(f"saving a shared photo ({key}) in the backup account "
                                    f"failed: {e}")
                if tried:
                    before = backup_counts
                    backup_counts = Counter(
                        label_key(t["label"])
                        for t in backup.collect(b_own, gw.ITEM_LINK, gw.media_id).values())
                    # Only copies that showed up on the album count as saved;
                    # the rest are tried again next run.
                    unconfirmed = 0
                    for key, tids in tried.items():
                        confirmed = max(0, backup_counts[key] - before[key])
                        for tid in tids[:confirmed]:
                            state.add("saved", tile=tid, key=key)
                        c["saved in backup"] += min(confirmed, len(tids))
                        unconfirmed += max(0, len(tids) - confirmed)
                    if unconfirmed:
                        log.problem(f"{unconfirmed} shared photo(s) didn't appear on "
                                    f"'{cfg.backup_album}' after saving. The backup account's "
                                    "page may have changed: run `python gphotos_backup_v2.py "
                                    "probe`. They'll be tried again next run.")
                log(f"Backup account: '{cfg.backup_album}' has "
                    f"{sum(backup_counts.values())} item(s)")
        elif transfer_url:
            log.problem("Backup account step skipped; no deletions this run.")
        if backup_counts is None:
            deletions = False

        # ---- Audit: photos trashed earlier must still be in the backup album ----
        if deletions and not dry:
            cutoff = datetime.now() - timedelta(days=AUDIT_DAYS)
            recent = [r for r in state.trashed.values()
                      if datetime.fromisoformat(r["at"]) >= cutoff and r.get("key")]
            need = Counter(r["key"] for r in recent)
            missing = [k for k, n in need.items() if backup_counts[k] < n]
            if missing:
                deletions = False
                gone = sum(need[k] - backup_counts[k] for k in missing)
                log.problem(f"{gone} photo(s) trashed from main in the last "
                            f"{AUDIT_DAYS} days are no longer in '{cfg.backup_album}' "
                            f"(e.g. taken {missing[0].split('|')[1]}). Restore them from the "
                            "main account's Trash. No deletions until this is resolved.")

        # ---- MAIN: tidy up ----
        if deletions:
            main_counts = Counter(i["key"] for i in items)
            for r in state.trashed.values():
                if r.get("key"):
                    main_counts[r["key"]] += 1
            self_tested = None
            for item in items:
                if not deletions:
                    break
                mid, label = item["id"], item["label"]
                if mid in keep_ids:
                    c["kept: in keep album"] += 1
                    continue
                if not dry and mid not in transfer_ids:
                    c["kept: not in shared album yet"] += 1
                    continue
                if not dry and backup_counts[item["key"]] < main_counts[item["key"]]:
                    c["kept: not in backup album yet"] += 1
                    continue
                if not dry and mid not in state.downloaded:
                    continue
                loaded, text = main.info_panel(item["url"])
                if not loaded:
                    loaded, text = main.info_panel(item["url"])
                lines = gw.info_album_lines(text) if loaded else None
                if not loaded or (not dry and (lines is None or cfg.transfer_album not in lines)):
                    # The shared album must show up, as proof the list was read.
                    c["kept: info panel didn't load"] += 1
                    log(f"  kept (couldn't confirm its albums): {label}")
                    continue
                if lines and other_albums(lines, cfg.transfer_album, known_titles):
                    c["kept: in another album"] += 1
                    review.append(item["url"])
                    continue
                if self_tested is None:
                    self_tested = album_check_works(main, keep_ids, cfg, known_titles, log)
                    if not self_tested:
                        log.problem("The album safety check failed its self-test; no "
                                    "deletions this run (copies still happen).")
                        deletions = False
                        continue
                if dry:
                    c["would trash"] += 1
                    log(f"  would trash from main: {label}")
                    continue
                if not v1.files_intact(state.downloaded.get(mid), strict=True):
                    log.problem(f"local copy changed before deletion; kept: {label}")
                    continue
                try:
                    main.trash(item["url"])
                    state.add("trashed", id=mid, key=item["key"],
                              at=datetime.now().isoformat(timespec="seconds"))
                    c["trashed"] += 1
                    log(f"  trashed from main: {label}")
                except Exception as e:
                    log.problem(f"trash failed for {label}: {e}")
    except SystemExit as e:  # e.g. network gave up inside the browser helper
        log.problem(str(e) if e.code not in (None, 0) else "stopped")
        exit_code = 1
    except Exception as e:
        log.problem(f"run aborted: {type(e).__name__}: {e}")
        exit_code = 1
    finally:
        for web in (main, backup):
            if web:
                web.close()
        summary = {k: v for k, v in c.items() if v}
        log("Summary: " + (", ".join(f"{k}: {v}" for k, v in summary.items()) or "nothing to do"),
            level="error" if log.problems else "info", event="summary",
            problems=len(log.problems),
            **{"_".join(re.findall(r"[a-z]+", k)): v for k, v in c.items()})
        for url in review:
            log(f"  review (kept, in an album other than '{cfg.keep_album}'): {url}")
        if not dry:
            state.add("run", at=datetime.now().isoformat(timespec="seconds"),
                      summary=summary, problems=log.problems)
        if log.problems:
            v1.notify("Photos backup needs attention",
                      f"{len(log.problems)} problem(s). First: {log.problems[0]}\nLog: {log.path}")
        elif cfg.notify_success and not dry:
            v1.notify("Photos backup done",
                      ", ".join(f"{k}: {v}" for k, v in summary.items()) or "nothing new")
    return exit_code or (1 if log.problems else 0)


# ------------------------------------------------------ other commands ----

def cmd_init(args):
    path = Path(args.config)
    if path.exists():
        sys.exit(f"{path} already exists; edit it directly.")
    path.write_text(CONFIG_TEMPLATE)
    print(f"Wrote {path}. Edit archive_dir and since, then run login-main and login-backup.")


def _login(profile, who):
    web = gw.PhotosWeb(str(profile), headless=False)
    try:
        print(f"Sign in with the {who.upper()} account in this Chrome window.")
        web.ensure_logged_in(interactive=True)
        print(f"{who.capitalize()} account signed in; kept in {profile}")
    finally:
        web.close()


def cmd_login_main(cfg, args):
    _login(cfg.main_browser_profile, "main")


def cmd_login_backup(cfg, args):
    _login(cfg.backup_browser_profile, "backup")


def cmd_check(cfg, args):
    """Check the one-time setup without changing anything."""
    ok = True
    for who, profile, want in (
            ("main", cfg.main_browser_profile, [cfg.keep_album, cfg.transfer_album]),
            ("backup", cfg.backup_browser_profile, [cfg.transfer_album, cfg.backup_album])):
        web = gw.PhotosWeb(str(profile), headless=cfg.headless)
        try:
            web.ensure_logged_in(interactive=False)
            albums = album_index(web)
            print(f"{who} account: signed in, {len(albums)} album(s)")
            for title in want:
                found = title in albums
                ok &= found
                print(f"  {'ok     ' if found else 'MISSING'} album '{title}'")
        except gw.NeedLogin:
            ok = False
            print(f"{who} account: NOT signed in; run login-{who}")
        finally:
            web.close()
    print("Setup looks complete." if ok else "Fix the items above (see README), then check again.")
    return 0 if ok else 1


def cmd_probe(cfg, args):
    """Print the controls the tool relies on, from both accounts, so changes
    to photos.google.com can be spotted. Opens menus; changes nothing."""
    for who, profile, album in (("main", cfg.main_browser_profile, cfg.transfer_album),
                                ("backup", cfg.backup_browser_profile, cfg.transfer_album)):
        web = gw.PhotosWeb(str(profile), headless=cfg.headless)
        try:
            web.ensure_logged_in(interactive=False)
            url = album_index(web).get(album)
            tiles = web.collect(url, gw.ITEM_LINK, gw.media_id, max_idle=2) if url else {}
            if not tiles:
                print(f"{who}: album '{album}' missing or empty; add one photo to it first")
                continue
            photo = next(iter(tiles.values()))["url"]
            web.goto(photo)
            web._settle()
            print(f"{who}: photo in '{album}'")
            print("  buttons:", web.controls())
            more = web.page.get_by_role("button", name=gw.ADD_MENU_OPENER_RE).first
            if more.count():
                more.click()
                web._settle()
                print("  'More options' menu:", web.page.eval_on_selector_all(
                    "[role=menuitem]", "els => els.map(e => e.innerText.trim())"))
                web.page.keyboard.press("Escape")
            print("  save button found:",
                  bool(web.page.get_by_role("button", name=gw.SAVE_RE).count()))
            loaded, text = web.info_panel(photo)
            print("  info panel albums:", gw.info_album_lines(text) if loaded else "(didn't load)")
        except gw.NeedLogin:
            print(f"{who}: not signed in; run login-{who}")
        finally:
            web.close()


def cmd_status(cfg, args):
    state = State(cfg.archive_dir)
    files = sum(len(r["files"]) for r in state.downloaded.values())
    print(f"Archive: {cfg.archive_dir}")
    print(f"  items downloaded from main: {len(state.downloaded)} ({files} file(s))")
    print(f"  items added to '{cfg.transfer_album}': {len(state.shared)}")
    print(f"  photos saved in the backup account: {len(state.saved)}")
    print(f"  items moved to Trash in main: {len(state.trashed)}")
    if state.last_run:
        r = state.last_run
        print(f"Last run: {r['at']}")
        for k, v in r["summary"].items():
            print(f"  {k}: {v}")
        for p in r["problems"]:
            print(f"  PROBLEM: {p}")
    else:
        print("No completed run yet.")
    timer = subprocess.run(["systemctl", "--user", "list-timers", f"{UNIT}.timer", "--no-legend"],
                           capture_output=True, text=True)
    print("Schedule:", timer.stdout.strip() or "not installed")


def cmd_install_schedule(cfg, args):
    other = subprocess.run(["systemctl", "--user", "is-enabled", f"{v1.UNIT}.timer"],
                           capture_output=True, text=True).stdout.strip()
    if other == "enabled":
        sys.exit(f"The v1 schedule ({v1.UNIT}.timer) is installed. Remove it first with "
                 "`python gphotos_backup.py uninstall-schedule`: both would tidy the main account.")
    unit_dir = Path("~/.config/systemd/user").expanduser()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{UNIT}.service").write_text(f"""[Unit]
Description=Google Photos backup v2 (main account -> computer + backup account by sharing)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={HERE}
ExecStart={sys.executable} {HERE / 'gphotos_backup_v2.py'} --config {cfg.path} run
TimeoutStartSec=6h
Nice=10
""")
    (unit_dir / f"{UNIT}.timer").write_text(f"""[Unit]
Description=Daily Google Photos backup (v2)

[Timer]
OnCalendar=*-*-* {args.time}:00
Persistent=true
RandomizedDelaySec=10min

[Install]
WantedBy=timers.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"], check=True)
    print(f"Installed: runs daily at {args.time} (or at next login if the computer was off).")
    print(f"Run once now with: systemctl --user start {UNIT}.service")
    print(f"Logs: {cfg.archive_dir / LOG_NAME}  (or: journalctl --user -u {UNIT})")


def cmd_uninstall_schedule(cfg, args):
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"])
    unit_dir = Path("~/.config/systemd/user").expanduser()
    for ext in ("service", "timer"):
        (unit_dir / f"{UNIT}.{ext}").unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print("Schedule removed.")


# ---------------------------------------------------------------- CLI ----

def build_parser():
    p = argparse.ArgumentParser(description="Back up Google Photos v2: main account -> computer, "
                                            "and -> backup account by sharing; then tidy main.")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="Settings file (TOML)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Write a settings file to edit")
    sub.add_parser("login-main", help="Sign in to the MAIN account in Chrome")
    sub.add_parser("login-backup", help="Sign in to the BACKUP account in Chrome")
    sub.add_parser("check", help="Check the one-time setup (sign-ins and albums)")
    sub.add_parser("probe", help="Show the page controls the tool relies on")
    r = sub.add_parser("run", help="Run a backup (what the schedule runs)")
    r.add_argument("--dry-run", action="store_true",
                   help="Show what would be downloaded, shared, saved and trashed; change nothing")
    sub.add_parser("status", help="Show totals, the last run and the schedule")
    s = sub.add_parser("install-schedule", help="Run daily via a systemd user timer")
    s.add_argument("--time", default="02:00", help="Time of day, HH:MM (default 02:00)")
    sub.add_parser("uninstall-schedule", help="Remove the schedule")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    cfg = Config(args.config)
    return {"login-main": cmd_login_main, "login-backup": cmd_login_backup, "check": cmd_check,
            "probe": cmd_probe, "run": cmd_run, "status": cmd_status,
            "install-schedule": cmd_install_schedule,
            "uninstall-schedule": cmd_uninstall_schedule}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main() or 0)
