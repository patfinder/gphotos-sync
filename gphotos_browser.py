#!/usr/bin/env python3
"""
Work with Google Photos directly through photos.google.com in a browser
(Playwright), with no Takeout export. Finds the photos/videos that are in
no album and were taken on/after a date; lists them, downloads the
originals, and optionally moves them to Google Photos Trash.

How it decides:
  1. Opens every album (your own, and shared ones) and records the ID of
     every item in it.
  2. Scrolls your library, newest first, reading each item's ID, type and
     date from the grid, until dates fall before --since.
  3. Items from (2) whose ID isn't in (1) are "not in an album".
Deletion uses the same ID it downloaded, so it can't hit the wrong photo.

Usage:
    python gphotos_browser.py login
    python gphotos_browser.py probe                    # check it understands the site
    python gphotos_browser.py list --since 2026-11-01  [--csv]
    python gphotos_browser.py download --since 2026-11-01 --dest ~/Pictures/unalbumed
    python gphotos_browser.py download --since 2026-11-01 --dest ~/Pictures/unalbumed --delete
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import gphotos_web as gw

STATE_NAME = ".gphotos_browser_state.jsonl"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------- scan ----

def scan(web, since, until, include_shared=True):
    """Returns (matches, stats). matches: list of dicts id/url/kind/taken,
    newest first."""
    log("Finding albums...")
    albums = web.album_urls()
    if not include_shared:
        albums = {k: v for k, v in albums.items() if "/share/" not in v["url"]}
    log(f"  {len(albums)} album(s)")

    in_album, members = set(), {}   # members: album title -> [item ids]
    for i, (aid, a) in enumerate(albums.items(), 1):
        items = web.collect(a["url"], gw.ITEM_LINK, gw.media_id)
        title = web.page_title() or (a["label"].splitlines() or [aid])[0] or aid
        in_album.update(items)
        members.setdefault(title, []).extend(items)
        log(f"  [{i}/{len(albums)}] {title}: {len(items)} item(s)")

    log(f"Scanning library back to {since:%Y-%m-%d}...")

    def older_than_since(items):
        last = next(reversed(items.values()))
        return (gw.parse_tile_label(last["label"])[1] or datetime.max) < since

    timeline = web.collect(gw.BASE_URL, gw.ITEM_LINK, gw.media_id, stop=older_than_since)

    stats = {"library items scanned": len(timeline), "in an album": 0,
             "outside date range": 0, "unreadable date (skipped)": 0, "matched": 0}
    matches = []
    for mid, it in timeline.items():
        kind, taken = gw.parse_tile_label(it["label"])
        if taken is None:
            stats["unreadable date (skipped)"] += 1
            continue
        if taken < since or (until and taken >= until):
            stats["outside date range"] += 1
            continue
        if mid in in_album:
            stats["in an album"] += 1
            continue
        stats["matched"] += 1
        url = urljoin(gw.BASE_URL, "photo/" + mid)
        matches.append({"id": mid, "url": url, "kind": kind, "taken": taken})
    return matches, stats, members


def print_stats(stats):
    log("Summary:")
    for k, v in stats.items():
        if v:
            log(f"  {k}: {v}")


# -------------------------------------------------------------- state ----

class State:
    """Append-only JSONL in --dest: which IDs were downloaded and where,
    and which were moved to Trash."""

    def __init__(self, dest):
        self.path = Path(dest) / STATE_NAME
        self.downloaded, self.deleted = {}, set()
        if self.path.exists():
            for line in self.path.open():
                r = json.loads(line)
                if r["event"] == "downloaded":
                    self.downloaded[r["id"]] = r
                elif r["event"] == "deleted":
                    self.deleted.add(r["id"])

    def _append(self, rec):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def record_download(self, mid, url, local, sha, taken):
        rec = {"event": "downloaded", "id": mid, "url": url, "local": str(local),
               "sha256": sha, "taken": taken.isoformat()}
        self.downloaded[mid] = rec
        self._append(rec)
        return rec

    def record_delete(self, mid):
        self.deleted.add(mid)
        self._append({"event": "deleted", "id": mid, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_ok(rec):
    p = Path(rec["local"])
    return p.is_file() and sha256(p) == rec["sha256"]


def free_name(directory, name):
    stem, ext = os.path.splitext(name)
    i = 0
    while True:
        cand = directory / (name if i == 0 else f"{stem}_{i}{ext}")
        if not cand.exists():
            return cand
        i += 1


def dest_dir(root, taken, layout):
    if layout == "flat":
        return root
    if layout == "year":
        return root / f"{taken:%Y}"
    return root / f"{taken:%Y}" / f"{taken:%m}"


def album_check_works(web, members, samples=3):
    """Before deleting anything: open up to `samples` photos known to be in
    albums (from different albums where possible) by their library URL, and
    require the info-panel check to report an album for every one of them.
    This proves both that album and library IDs are the same and that the
    check can see albums; a negative answer later can then be trusted."""
    picks = []
    for title, ids in members.items():
        if ids and len(picks) < samples:
            picks.append((title, ids[0]))
    if not picks:
        log("No album photos to test the album check with; deletion needs at least one album.")
        return False
    titles = set(members)
    log(f"Testing the album safety check on {len(picks)} album photo(s)...")
    ok = True
    for title, mid in picks:
        seen = web.info_shows_albums(urljoin(gw.BASE_URL, "photo/" + mid), titles)
        log(f"  photo from '{title}': album {'seen' if seen else 'NOT seen'} in its info panel")
        ok &= seen
    return ok


# ----------------------------------------------------------- commands ----

def cmd_login(web, args):
    web.ensure_logged_in()
    log("Signed in; the session is kept in", args.browser_profile)


def cmd_probe(web, args):
    """Report how well the page assumptions in gphotos_web.py match the
    live site, without changing anything."""
    web.ensure_logged_in()
    try:  # the grid is drawn by script after the page loads
        web.page.wait_for_selector(gw.ITEM_LINK, timeout=20_000)
    except Exception:
        pass
    time.sleep(1.0)
    tiles = web.page.eval_on_selector_all(gw.ITEM_LINK, gw._LINKS_JS)
    print(f"Library page: {len(tiles)} tile link(s) visible "
          f"(selector {gw.ITEM_LINK!r}).")
    parsed = 0
    for href, label in tiles[:8]:
        kind, taken = gw.parse_tile_label(label)
        parsed += taken is not None
        print(f"  id={gw.media_id(href)}  kind={kind}  taken={taken}  label={label[:80]!r}")
    ok_tiles = bool(tiles) and parsed > 0
    albums = web.album_urls()
    print(f"Albums found: {len(albums)}")
    for a in list(albums.values())[:5]:
        print(f"  {a['url']}")
    same_ids = None
    if albums:
        first = next(iter(albums.values()))
        items = web.collect(first["url"], gw.ITEM_LINK, gw.media_id, max_idle=2)
        print(f"First album ({web.page_title() or 'untitled'}): {len(items)} item(s)")
        album_title = web.page_title()
        if items:
            mid, first_item = next(iter(items.items()))
            # The same item, opened from inside the album and from the library.
            in_album_text = web._info_text(first_item["url"])
            lib_url = urljoin(gw.BASE_URL, "photo/" + mid)
            lib_text = web._info_text(lib_url)
            lib_landed = gw.media_id(web.page.url) == mid
            a_lines = {l.strip() for l in in_album_text.splitlines() if l.strip()}
            l_lines = {l.strip() for l in lib_text.splitlines() if l.strip()}
            overlap = len(a_lines & l_lines) / max(1, len(a_lines | l_lines))
            same_ids = web.info_shows_albums(lib_url, [album_title])
            print(f"  Opening its first item by library URL stayed on that photo: {lib_landed}")
            print(f"  Album view and library view show the same text: {overlap:.0%} overlap")
            print(f"  Library view's info panel mentions the album '{album_title}': {same_ids}")
            print("  Library view page text (first 40 lines; redact anything private):")
            for line in [l for l in lib_text.splitlines() if l.strip()][:40]:
                print(f"    | {line.strip()[:100]}")
    print()
    print("Tiles and dates readable:", "yes" if ok_tiles else "NO")
    print("Albums found:", "yes" if albums else "NO (or you have no albums)")
    if same_ids is False:
        print("Album check in info panel: NOT detected; --delete will refuse to run")
    elif same_ids:
        print("Album check in info panel: works")
    if args.dump:
        Path(args.dump).write_text(web.page.content())
        print(f"Saved the last page's HTML to {args.dump}")


def cmd_list(web, args):
    web.ensure_logged_in()
    matches, stats, _ = scan(web, args.since, args.until, not args.no_shared)
    matches.sort(key=lambda m: m["taken"])
    if args.csv:
        w = csv.writer(sys.stdout)
        w.writerow(["taken", "type", "url"])
        for m in matches:
            w.writerow([m["taken"].strftime("%Y-%m-%d %H:%M"), m["kind"], m["url"]])
    else:
        for m in matches:
            print(f"{m['taken']:%Y-%m-%d %H:%M}  {m['kind']:<5}  {m['url']}")
        print(f"{len(matches)} item(s): {sum(m['kind'] == 'photo' for m in matches)} photo(s), "
              f"{sum(m['kind'] == 'video' for m in matches)} video(s)")
    sys.stdout.flush()
    print_stats(stats)


def cmd_download(web, args):
    web.ensure_logged_in()
    root = Path(args.dest).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    state = State(root)
    matches, stats, members = scan(web, args.since, args.until, not args.no_shared)
    matches.sort(key=lambda m: m["taken"])
    print_stats(stats)
    titles = set(members)
    if args.delete and not album_check_works(web, members):
        sys.exit("\nDeletion is switched off: the safety check that a photo's info panel "
                 "shows its albums didn't work on photos known to be in albums, so the "
                 "script can't confirm an item is album-free before trashing it. Nothing "
                 "was downloaded or deleted. Run `probe` and send its output, or run "
                 "without --delete to just download.")

    counts = {"downloaded": 0, "already downloaded": 0, "would download": 0,
              "trashed": 0, "would trash": 0, "declined": 0,
              "in an album per info panel (kept)": 0,
              "local copy missing/changed (kept online)": 0, "errors": 0}
    yes_all = args.yes
    try:
        for m in matches:
            mid, label = m["id"], f"{m['taken']:%Y-%m-%d %H:%M} {m['kind']} {m['url']}"
            rec = state.downloaded.get(mid)
            if rec and local_ok(rec):
                counts["already downloaded"] += 1
            elif args.dry_run:
                counts["would download"] += 1
                print(f"  would download: {label}")
            else:
                try:
                    tmp, name = web.download(m["url"], root)
                    target = free_name(dest_dir(root, m["taken"], args.layout), name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    ts = m["taken"].timestamp()
                    os.utime(tmp, (ts, ts))
                    os.replace(tmp, target)
                except Exception as e:
                    counts["errors"] += 1
                    print(f"  error downloading {label}: {e}")
                    continue
                rec = state.record_download(mid, m["url"], target, sha256(target), m["taken"])
                counts["downloaded"] += 1
                print(f"  downloaded: {label} -> {target}")

            if not args.delete or mid in state.deleted:
                continue
            if not args.dry_run and (not rec or not local_ok(rec)):
                counts["local copy missing/changed (kept online)"] += 1
                print(f"  kept online, local copy missing or changed: {label}")
                continue
            try:  # second, independent album check (read-only)
                if web.info_shows_albums(m["url"], titles):
                    counts["in an album per info panel (kept)"] += 1
                    print(f"  kept online, its info panel lists an album: {label}")
                    continue
            except Exception as e:
                counts["errors"] += 1
                print(f"  error checking {label}: {e}")
                continue
            if args.dry_run:
                counts["would trash"] += 1
                continue
            if not yes_all:
                ans = input(f"  Move to Trash: {label}  [y/N/a=all/q=quit] ").strip().lower()
                if ans == "q":
                    raise gw.Stop
                if ans == "a":
                    yes_all = True
                elif ans != "y":
                    counts["declined"] += 1
                    continue
            try:
                web.trash(m["url"])
            except Exception as e:
                counts["errors"] += 1
                print(f"  error trashing {label}: {e}")
                continue
            state.record_delete(mid)
            counts["trashed"] += 1
            print(f"  trashed: {label}")
    except gw.Stop:
        print("Stopped.")
    print("Results:")
    for k, v in counts.items():
        if v:
            print(f"  {k}: {v}")


# ---------------------------------------------------------------- CLI ----

def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}")


def build_parser():
    p = argparse.ArgumentParser(description="Find, download and optionally trash Google Photos "
                                            "items that are in no album, via photos.google.com.")
    p.add_argument("--browser-profile", default=".gphotos_browser_profile",
                   help="Browser profile dir that keeps the Google sign-in")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Open the browser to sign in to Google")
    pr = sub.add_parser("probe", help="Check that the script understands the site (read-only)")
    pr.add_argument("--dump", help="Save the last page's HTML here, for debugging")

    def common(sp):
        sp.add_argument("--since", required=True, type=parse_date,
                        help="Items taken on/after this date (YYYY-MM-DD)")
        sp.add_argument("--until", type=parse_date, help="Items taken before this date")
        sp.add_argument("--no-shared", action="store_true",
                        help="Ignore shared albums when deciding what's in an album")

    ls = sub.add_parser("list", help="List matching items (read-only)")
    common(ls)
    ls.add_argument("--csv", action="store_true", help="CSV output")

    dl = sub.add_parser("download", help="Download matching items' originals")
    common(dl)
    dl.add_argument("--dest", required=True, help="Folder to download into")
    dl.add_argument("--layout", choices=["year-month", "year", "flat"], default="year-month")
    dl.add_argument("--delete", action="store_true",
                    help="Move each item to Google Photos Trash once its download is verified")
    dl.add_argument("--yes", action="store_true", help="Don't ask before each deletion")
    dl.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing")
    return p


def main():
    args = build_parser().parse_args()
    web = gw.PhotosWeb(os.path.abspath(args.browser_profile))
    try:
        {"login": cmd_login, "probe": cmd_probe, "list": cmd_list,
         "download": cmd_download}[args.command](web, args)
    finally:
        web.close()


if __name__ == "__main__":
    main()
