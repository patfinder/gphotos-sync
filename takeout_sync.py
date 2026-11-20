#!/usr/bin/env python3
"""
Copy Google Photos items that are NOT in any album and were taken on/after
a given date out of a Google Takeout export and onto this computer.

Why Takeout and not the API: since 2025-03-31 the Photos Library API only
exposes items the calling app uploaded itself, and the Picker API can't
report album membership. Takeout is the only complete, scriptable view of
a library. See docs/design.md.

How "not in an album" is decided: Takeout puts every item into a
"Photos from YYYY" folder, and additionally copies items that belong to an
album into a folder named after the album. An item from a year folder whose
bytes match a file in any album folder is in an album; anything else isn't.

Usage:
    python takeout_sync.py --since 2024-06-01 --dest ~/Pictures/unalbumed \\
        ~/Downloads/takeout-*.zip
    python takeout_sync.py --since 2024-06-01 --dest ./out ~/Takeout --dry-run
    # Just list what matches (no copying); CSV for a spreadsheet:
    python takeout_sync.py --since 2024-06-01 --list ~/Downloads/takeout-*.zip
    python takeout_sync.py --since 2024-06-01 --list --list-format csv takeout-*.zip > list.csv
    # ...and move what was copied to Google Photos Trash (asks per photo):
    python takeout_sync.py --since 2024-06-01 --dest ./out ~/Takeout --delete
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from gphotos_web import Ledger, run_deletions

MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif",
    ".tiff", ".dng", ".raw", ".cr2", ".nef", ".arw", ".avif",
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mpg", ".mpeg", ".wmv",
    ".mts", ".m2ts", ".webm",
}

DEFAULT_YEAR_FOLDER_RE = r"^Photos from \d{4}$"
# Folders that are neither albums nor the main timeline.
DEFAULT_IGNORE_FOLDERS = ["Trash", "Bin", "Archive", "Locked Folder", "Failed Videos"]

# Filename dates as written by common phone cameras, e.g. IMG_20240131_...,
# PXL_20240131_..., 2024-01-31 12.00.00.jpg, Screenshot_20240131-...
FILENAME_DATE_RE = re.compile(r"(?<!\d)(20\d{2}|19\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")
DUP_SUFFIX_RE = re.compile(r"^(?P<base>.*?)\((?P<n>\d+)\)$")


# ----------------------------------------------------------- sources ----

class Entry:
    """One file inside a Takeout export, in a zip or on disk."""

    def __init__(self, source, path, size):
        self.source = source          # zipfile.ZipFile or None (plain file)
        self.path = path              # member name (zip) or absolute path str
        self.size = size
        p = PurePosixPath(path.replace(os.sep, "/"))
        self.name = p.name
        self.folder = p.parent.name   # album / "Photos from YYYY" folder name
        self._sha = None

    def open(self):
        if self.source is None:
            return open(self.path, "rb")
        return self.source.open(self.path)

    def sha256(self):
        if self._sha is None:
            h = hashlib.sha256()
            with self.open() as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            self._sha = h.hexdigest()
        return self._sha

    def __repr__(self):
        return f"{self.folder}/{self.name}"


def scan_sources(paths):
    """Yield an Entry for every file in the given zips and/or directories."""
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file() and zipfile.is_zipfile(p):
            zf = zipfile.ZipFile(p)
            for info in zf.infolist():
                if not info.is_dir():
                    yield Entry(zf, info.filename, info.file_size)
        elif p.is_dir():
            for root, _dirs, files in os.walk(p):
                for fn in files:
                    full = os.path.join(root, fn)
                    yield Entry(None, full, os.path.getsize(full))
        else:
            sys.exit(f"Not a zip file or directory: {raw}")


# ---------------------------------------------------- sidecar lookup ----

def split_dup(stem):
    """'IMG_1(2)' -> ('IMG_1', 2); 'IMG_1' -> ('IMG_1', 0)."""
    m = DUP_SUFFIX_RE.match(stem)
    return (m.group("base"), int(m.group("n"))) if m else (stem, 0)


class SidecarIndex:
    """Maps a media file to its Takeout JSON sidecar.

    Sidecar file names vary across Takeout versions and get truncated
    (IMG.jpg.json, IMG.jpg.supplemental-metadata.json,
    IMG.jpg.supplemental-me.json, IMG.jpg(1).json, ...), so instead of
    guessing the name we read each JSON's "title" (the original filename)
    and the "(n)" duplicate counter from the sidecar's own name.
    """

    def __init__(self):
        # folder -> {(title_lower, n): data}
        self.by_folder = defaultdict(dict)

    def add(self, entry):
        try:
            with entry.open() as f:
                data = json.load(f)
        except (ValueError, OSError):
            return
        title = data.get("title") if isinstance(data, dict) else None
        if not title or "photoTakenTime" not in data and "creationTime" not in data:
            return  # album metadata.json, print-order files, etc.
        _, n = split_dup(entry.name[: -len(".json")])
        self.by_folder[entry.folder][(title.lower(), n)] = data

    def lookup(self, entry):
        table = self.by_folder.get(entry.folder)
        if not table:
            return None
        stem, ext = os.path.splitext(entry.name)
        stem, n = split_dup(stem)
        candidates = [stem]
        if stem.endswith("-edited"):
            candidates.append(stem[: -len("-edited")])
        for s in candidates:
            hit = table.get(((s + ext).lower(), n))
            if hit:
                return hit
        # Long names are truncated on disk but not in "title": prefix match.
        for s in candidates:
            prefix = s.lower()
            matches = [d for (t, tn), d in table.items()
                       if tn == n and t.startswith(prefix)
                       and os.path.splitext(t)[1] == ext.lower()]
            if len(matches) == 1:
                return matches[0]
        return None


def google_filename(entry, sidecars):
    """The item's filename as Google Photos shows it: the sidecar "title",
    else the Takeout name without its "(n)" duplicate counter."""
    data = sidecars.lookup(entry)
    if data and data.get("title"):
        return data["title"]
    stem, ext = os.path.splitext(entry.name)
    return split_dup(stem)[0] + ext


def taken_time(entry, sidecars):
    """Best-effort capture time (aware UTC datetime) or None."""
    data = sidecars.lookup(entry)
    if data:
        for key in ("photoTakenTime", "creationTime"):
            ts = (data.get(key) or {}).get("timestamp")
            if ts and int(ts) > 0:
                return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    m = FILENAME_DATE_RE.search(entry.name)
    if m:
        try:
            local = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return local.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


# ----------------------------------------------------------- copying ----

def dest_dir_for(dest_root, when, layout):
    if layout == "flat" or when is None:
        return dest_root if layout == "flat" else dest_root / "undated"
    local = when.astimezone()
    if layout == "year":
        return dest_root / f"{local:%Y}"
    return dest_root / f"{local:%Y}" / f"{local:%m}"


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def choose_target(directory, entry):
    """Return (path, already_present). Reuses a same-content file so re-runs
    are idempotent; otherwise picks a free name, adding _1, _2, ..."""
    stem, ext = os.path.splitext(entry.name)
    i = 0
    while True:
        name = entry.name if i == 0 else f"{stem}_{i}{ext}"
        target = directory / name
        if not target.exists():
            return target, False
        if target.stat().st_size == entry.size and file_sha256(target) == entry.sha256():
            return target, True
        i += 1


def copy_entry(entry, target, when):
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    with entry.open() as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    if when is not None:
        ts = when.timestamp()
        os.utime(tmp, (ts, ts))
    os.replace(tmp, target)


# ----------------------------------------------------------- listing ----

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mpg", ".mpeg",
             ".wmv", ".mts", ".m2ts", ".webm"}


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def print_listing(listed, sidecars, fmt):
    """Print (when, entry) pairs, oldest first, undated at the end."""
    listed.sort(key=lambda we: (we[0] is None, we[0] or datetime.min.replace(tzinfo=timezone.utc),
                                we[1].name))
    rows = []
    for when, e in listed:
        kind = "video" if os.path.splitext(e.name)[1].lower() in VIDEO_EXT else "photo"
        rows.append({
            "taken": when.astimezone().strftime("%Y-%m-%d %H:%M") if when else "undated",
            "type": kind,
            "size_bytes": e.size,
            "google_filename": google_filename(e, sidecars),
            "takeout_path": f"{e.folder}/{e.name}",
        })
    if fmt == "csv":
        import csv
        w = csv.DictWriter(sys.stdout, fieldnames=["taken", "type", "size_bytes",
                                                   "google_filename", "takeout_path"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return
    for r in rows:
        print(f"{r['taken']:<16}  {r['type']:<5}  {human_size(r['size_bytes']):>9}  "
              f"{r['google_filename']}   ({r['takeout_path']})")
    total = sum(r["size_bytes"] for r in rows)
    n_vid = sum(r["type"] == "video" for r in rows)
    print(f"{len(rows)} item(s): {len(rows) - n_vid} photo(s), {n_vid} video(s), "
          f"{human_size(total)} total")


# -------------------------------------------------------------- main ----

def parse_date(s):
    try:
        # Interpreted as local midnight.
        return datetime.strptime(s, "%Y-%m-%d").astimezone(timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}")


def build_parser():
    p = argparse.ArgumentParser(
        description="Copy Google Photos items that are in no album and were "
                    "taken on/after --since out of a Google Takeout export.")
    p.add_argument("takeout", nargs="+",
                   help="Takeout .zip files and/or extracted Takeout directories")
    p.add_argument("--since", required=True, type=parse_date,
                   help="Only items taken on or after this date (YYYY-MM-DD, local time)")
    p.add_argument("--until", type=parse_date,
                   help="Only items taken before this date (YYYY-MM-DD, exclusive)")
    p.add_argument("--dest", help="Directory to copy photos into (not needed with --list)")
    p.add_argument("--list", action="store_true",
                   help="Only list the matching photos/videos (newest last); copy nothing")
    p.add_argument("--list-format", choices=["text", "csv"], default="text",
                   help="Output format for --list (default: text). Status messages go to "
                        "stderr, so `--list --list-format csv > out.csv` gives a clean file")
    p.add_argument("--layout", choices=["year-month", "year", "flat"], default="year-month",
                   help="Folder layout under --dest (default: year-month, e.g. 2024/06/)")
    p.add_argument("--include-undated", action="store_true",
                   help="Also copy items whose date can't be determined")
    p.add_argument("--year-folder-regex", default=DEFAULT_YEAR_FOLDER_RE,
                   help=f"Regex for timeline folders (default: {DEFAULT_YEAR_FOLDER_RE!r}); "
                        "change it for non-English Takeouts")
    p.add_argument("--ignore-folder", action="append", default=None,
                   help="Folder that is not an album (repeatable). "
                        f"Default: {', '.join(DEFAULT_IGNORE_FOLDERS)}")
    p.add_argument("--delete", action="store_true",
                   help="After copying, move the copied photos to Google Photos Trash "
                        "via a browser (asks before each one unless --yes). With "
                        "--dry-run, only finds and verifies them online.")
    p.add_argument("--yes", action="store_true", help="Don't ask before each deletion")
    p.add_argument("--browser-profile", default=".gphotos_browser_profile",
                   help="Browser profile dir that keeps the Google sign-in "
                        "(default: .gphotos_browser_profile)")
    p.add_argument("--dry-run", action="store_true", help="List what would be copied")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.list and args.delete:
        parser.error("--list and --delete can't be combined")
    if not args.list and not args.dest:
        parser.error("--dest is required (unless --list is given)")
    # In list mode the list goes to stdout and everything else to stderr.
    info = (lambda *a: print(*a, file=sys.stderr)) if args.list else print
    year_re = re.compile(args.year_folder_regex)
    ignore = {f.lower() for f in (args.ignore_folder or DEFAULT_IGNORE_FOLDERS)}
    dest_root = Path(args.dest).expanduser() if args.dest else None

    timeline, album_files = [], []
    sidecars = SidecarIndex()
    for e in scan_sources(args.takeout):
        ext = os.path.splitext(e.name)[1].lower()
        if ext == ".json":
            sidecars.add(e)
        elif ext in MEDIA_EXT:
            if year_re.match(e.folder):
                timeline.append(e)
            elif e.folder.lower() not in ignore:
                album_files.append(e)

    if not timeline:
        sys.exit("No timeline folders found (expected folders like 'Photos from 2024'). "
                 "Check the paths, or set --year-folder-regex for a non-English Takeout.")
    album_names = sorted({e.folder for e in album_files})
    info(f"Scanned {len(timeline)} timeline item(s) and {len(album_files)} file(s) "
         f"in {len(album_names)} album folder(s).")
    if args.verbose:
        for a in album_names:
            info(f"  album: {a}")

    # Album membership by content: compare sizes first, hash only on a size hit.
    album_by_size = defaultdict(list)
    for e in album_files:
        album_by_size[e.size].append(e)
    album_hashes = {}  # size -> set of sha256, filled lazily

    def in_album(e):
        if e.size not in album_by_size:
            return False
        if e.size not in album_hashes:
            album_hashes[e.size] = {a.sha256() for a in album_by_size[e.size]}
        return e.sha256() in album_hashes[e.size]

    ledger = None if args.list else Ledger(dest_root)
    stats = defaultdict(int)
    listed = []
    for e in sorted(timeline, key=lambda x: (x.folder, x.name)):
        when = taken_time(e, sidecars)
        if when is None:
            if not args.include_undated:
                stats["undated (skipped)"] += 1
                if args.verbose:
                    info(f"  no date, skipping: {e}")
                continue
        elif when < args.since or (args.until and when >= args.until):
            stats["outside date range"] += 1
            continue

        if in_album(e):
            stats["in an album"] += 1
            continue

        if args.list:
            stats["listed"] += 1
            listed.append((when, e))
            continue

        target, present = choose_target(dest_dir_for(dest_root, when, args.layout), e)
        if present:
            stats["already copied"] += 1
            ledger.record_copy(target, e.sha256(), google_filename(e, sidecars), when)
            continue

        stats["copied" if not args.dry_run else "would copy"] += 1
        stamp = when.astimezone().strftime("%Y-%m-%d") if when else "undated"
        print(f"  {'would copy' if args.dry_run else 'copy'}: {e} [{stamp}] -> {target}")
        if not args.dry_run:
            copy_entry(e, target, when)
            ledger.record_copy(target, e.sha256(), google_filename(e, sidecars), when)

    if args.list:
        print_listing(listed, sidecars, args.list_format)
        sys.stdout.flush()

    info("Summary:")
    for k, v in sorted(stats.items()):
        info(f"  {k}: {v}")

    if args.delete:
        def verify_local(rec):
            p = Path(rec["local"])
            return p.is_file() and file_sha256(p) == rec["sha256"]

        dstats = run_deletions(ledger, profile_dir=os.path.abspath(args.browser_profile),
                               assume_yes=args.yes, dry_run=args.dry_run,
                               verify_local=verify_local)
        print("Google Photos deletion summary:")
        for k, v in dstats.items():
            if v:
                print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
