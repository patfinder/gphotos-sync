#!/usr/bin/env python3
"""
Scheduled backup:  phone -> MAIN Google account -> this computer -> BACKUP account

Each run (normally started by a systemd timer, unattended):
  1. MAIN account, via a signed-in Chrome window: collects every photo/video
     taken on/after `since`, newest first, and downloads the originals into
     the archive folder (YYYY/MM). Each download is hashed and recorded.
  2. BACKUP account, via the official Photos Library API (uploading is still
     allowed): uploads each archived file and confirms it exists there.
  3. MAIN account again: moves a photo to Trash only if
       - its local files are intact (SHA-256) AND all were uploaded to the
         backup account and confirmed, AND
       - it's in no album: not in the keep album (read from that album's page)
         and its info panel loads and lists no albums.
     If anything is uncertain the photo is kept. Before the first deletion
     of a run, the album check is self-tested on photos known to be in the
     keep album; if that fails, the run makes no deletions at all.

Nothing ever prompts. Problems (expired sign-in, failed uploads, disabled
deletions) are written to the log and sent as a desktop notification.

Commands:
    python gphotos_backup.py init                  # write backup.toml to edit
    python gphotos_backup.py login-main            # sign in to the MAIN account (Chrome)
    python gphotos_backup.py auth-backup           # authorize uploads to the BACKUP account
    python gphotos_backup.py run --dry-run         # show what a run would do
    python gphotos_backup.py run                   # do it (this is what the timer runs)
    python gphotos_backup.py status
    python gphotos_backup.py install-schedule      # daily at 02:00 (systemd user timer)
    python gphotos_backup.py uninstall-schedule
"""
import argparse
import fcntl
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import gphotos_web as gw

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "backup.toml"
STATE_NAME = ".gphotos_backup_state.jsonl"
LOG_NAME = ".gphotos_backup.log"
LOCK_NAME = ".gphotos_backup.lock"
UNIT = "gphotos-backup"

API_BASE = os.environ.get("GPHOTOS_API_BASE", "https://photoslibrary.googleapis.com/v1")
BACKUP_SCOPES = [
    # Upload into the backup account, and read back what this app uploaded
    # (to confirm each upload). Both still work after the 2025 API changes.
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]

CONFIG_TEMPLATE = '''# gphotos_backup.py settings. Relative paths are relative to this file.

# Where the computer copy lives (YYYY/MM folders, plus state and log files).
archive_dir = "~/Pictures/gphotos-archive"

# Back up photos/videos TAKEN on or after this date. Every run looks at
# everything since this date, so photos that sync late are still caught.
# Set it to the day you start using this tool.
since = "2026-11-01"

# Photos in this album stay in the main account.
keep_album = "_Keep It"

# Move backed-up photos to Trash in the main account (only when they are in no
# album and both the computer copy and the backup-account copy are verified).
delete_from_main = true

# Chrome profile that holds the MAIN account sign-in (see `login-main`).
main_browser_profile = ".gphotos_browser_profile"

# OAuth client (Desktop app) from Google Cloud Console, and the token for the
# BACKUP account (see `auth-backup`).
client_secret = "client_secret.json"
backup_token = "token_backup.json"

# Folder layout in the archive: "year-month", "year" or "flat".
layout = "year-month"

# Run Chrome without a window. Leave false unless you need it; scheduled runs
# switch to headless automatically when there's no display.
headless = false

# Also notify when a run succeeds (problems are always notified).
notify_success = false
'''


# ------------------------------------------------------------- config ----

class Config:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        if not self.path.exists():
            sys.exit(f"No config at {self.path}. Create one with: python gphotos_backup.py init")
        with open(self.path, "rb") as f:
            raw = tomllib.load(f)
        base = self.path.parent

        def p(key, default):
            v = Path(str(raw.get(key, default))).expanduser()
            return v if v.is_absolute() else (base / v)

        self.archive_dir = p("archive_dir", "~/Pictures/gphotos-archive")
        try:
            self.since = datetime.strptime(str(raw["since"]), "%Y-%m-%d")
        except (KeyError, ValueError):
            sys.exit(f"{self.path}: `since` must be a date like \"2026-11-01\"")
        self.keep_album = str(raw.get("keep_album", "_Keep It"))
        self.delete_from_main = bool(raw.get("delete_from_main", True))
        self.main_browser_profile = p("main_browser_profile", ".gphotos_browser_profile")
        self.client_secret = p("client_secret", "client_secret.json")
        self.backup_token = p("backup_token", "token_backup.json")
        self.layout = str(raw.get("layout", "year-month"))
        if self.layout not in ("year-month", "year", "flat"):
            sys.exit(f"{self.path}: layout must be year-month, year or flat")
        self.headless = bool(raw.get("headless", False))
        self.notify_success = bool(raw.get("notify_success", False))


# ---------------------------------------------------- logging & notify ----

class Log:
    def __init__(self, path=None):
        self.path = path
        self.problems = []

    def __call__(self, msg):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
        print(line, flush=True)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(line + "\n")

    def problem(self, msg):
        self.problems.append(msg)
        self("PROBLEM: " + msg)


def notify(title, body):
    """Desktop notification; never fails the run."""
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "Photos backup", title, body], timeout=10)
        except Exception:
            pass


# -------------------------------------------------------------- state ----

class State:
    """Append-only JSONL in the archive folder.
      downloaded: main-account item ID -> files [{local, sha256}], taken
      uploaded:   sha256 -> backup-account media item ID (confirmed)
      trashed:    main-account item IDs moved to Trash
      keep_album_url, last run summary"""

    def __init__(self, archive):
        self.path = Path(archive) / STATE_NAME
        self.downloaded, self.uploaded, self.trashed = {}, {}, set()
        self.keep_album_url, self.last_run = None, None
        if self.path.exists():
            for line in self.path.open():
                r = json.loads(line)
                ev = r.get("event")
                if ev == "downloaded":
                    self.downloaded[r["id"]] = r
                elif ev == "uploaded":
                    self.uploaded[r["sha256"]] = r["backup_id"]
                elif ev == "trashed":
                    self.trashed.add(r["id"])
                elif ev == "keep_album":
                    self.keep_album_url = r["url"]
                elif ev == "run":
                    self.last_run = r

    def _append(self, rec):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def add(self, event, **fields):
        rec = {"event": event, **fields}
        self._append(rec)
        if event == "downloaded":
            self.downloaded[fields["id"]] = rec
        elif event == "uploaded":
            self.uploaded[fields["sha256"]] = fields["backup_id"]
        elif event == "trashed":
            self.trashed.add(fields["id"])
        elif event == "keep_album":
            self.keep_album_url = fields["url"]
        elif event == "run":
            self.last_run = rec
        return rec


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_ok(f, strict):
    p = Path(f["local"])
    if not p.is_file():
        return False
    st = p.stat()
    if not strict and st.st_size == f.get("size") and int(st.st_mtime) == f.get("mtime"):
        return True  # unchanged since it was hashed; skip re-reading big files daily
    return sha256(p) == f["sha256"]


def files_intact(rec, strict=False):
    """All of a downloaded item's files are present and unchanged. strict
    re-hashes every file (used right before deleting from the main account)."""
    return bool(rec) and bool(rec["files"]) and all(_file_ok(f, strict) for f in rec["files"])


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


# ------------------------------------------------- backup account API ----

class BackupAPIError(Exception):
    pass


class BackupAPI:
    """Uploads to the BACKUP account through the Photos Library API."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.creds = None

    def connect(self):
        """Load and refresh the saved token without any prompt."""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        if not self.cfg.backup_token.exists():
            raise BackupAPIError("the backup account isn't authorized yet; run "
                                 "`python gphotos_backup.py auth-backup`")
        creds = Credentials.from_authorized_user_file(str(self.cfg.backup_token), BACKUP_SCOPES)
        if not creds.valid:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise BackupAPIError(
                    "the backup account's authorization expired or was revoked "
                    f"({e}); run `python gphotos_backup.py auth-backup` again. If this "
                    "happens about weekly, set the OAuth consent screen's publishing "
                    "status to \"In production\" (see README)") from e
            self.cfg.backup_token.write_text(creds.to_json())
        self.creds = creds

    def _headers(self):
        from google.auth.transport.requests import Request
        if not self.creds.valid:
            self.creds.refresh(Request())
        return {"Authorization": f"Bearer {self.creds.token}"}

    def upload(self, path, filename):
        """Upload one file; returns the new media item ID once the API
        confirms it exists in the backup account."""
        import requests
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        h = self._headers() | {"Content-Type": "application/octet-stream",
                               "X-Goog-Upload-Content-Type": mime,
                               "X-Goog-Upload-Protocol": "raw"}
        with open(path, "rb") as f:
            r = requests.post(f"{API_BASE}/uploads", headers=h, data=f, timeout=3600)
        if not r.ok:
            raise BackupAPIError(f"upload failed: HTTP {r.status_code} {r.text[:200]}")
        token = r.text
        body = {"newMediaItems": [{"simpleMediaItem": {"uploadToken": token, "fileName": filename}}]}
        r = requests.post(f"{API_BASE}/mediaItems:batchCreate",
                          headers=self._headers() | {"Content-Type": "application/json"},
                          data=json.dumps(body), timeout=300)
        if not r.ok:
            raise BackupAPIError(f"batchCreate failed: HTTP {r.status_code} {r.text[:200]}")
        res = r.json()["newMediaItemResults"][0]
        status = res.get("status", {})
        if status.get("code") not in (None, 0) or "mediaItem" not in res:
            raise BackupAPIError(f"backup account rejected {filename}: {status.get('message')}")
        media_id = res["mediaItem"]["id"]
        r = requests.get(f"{API_BASE}/mediaItems/{media_id}", headers=self._headers(), timeout=60)
        if not r.ok or r.json().get("id") != media_id:
            raise BackupAPIError(f"couldn't confirm {filename} in the backup account "
                                 f"(HTTP {r.status_code})")
        return media_id


# ---------------------------------------------------------------- run ----

def resolve_keep_album(web, cfg, state, log):
    """URL of the keep album. Found once by opening albums until the title
    matches, then cached in the state file."""
    if state.keep_album_url:
        # Cached as a path ("album/<id>") so it doesn't depend on the host.
        url = urljoin(gw.BASE_URL, state.keep_album_url.lstrip("/"))
        web.goto(url)
        web._settle()
        if web.page_title() == cfg.keep_album:
            return url
        log(f"Cached keep-album link no longer shows '{cfg.keep_album}'; looking it up again")
    for a in web.album_urls().values():
        web.goto(a["url"])
        web._settle()
        if web.page_title() == cfg.keep_album:
            parsed = urlparse(a["url"])
            state.add("keep_album", url=parsed.path.lstrip("/") + (f"?{parsed.query}" if parsed.query else ""))
            return a["url"]
    return None


def scan_main(web, since):
    def older_than_since(items):
        last = next(reversed(items.values()))
        return (gw.parse_tile_label(last["label"])[1] or datetime.max) < since

    timeline = web.collect(gw.BASE_URL, gw.ITEM_LINK, gw.media_id, stop=older_than_since)
    items, undated = [], 0
    for mid, it in timeline.items():
        kind, taken = gw.parse_tile_label(it["label"])
        if taken is None:
            undated += 1
        elif taken >= since:
            items.append({"id": mid, "url": urljoin(gw.BASE_URL, "photo/" + mid),
                          "kind": kind, "taken": taken})
    items.sort(key=lambda m: m["taken"])
    return items, undated


def download_item(web, item, cfg, state):
    """Download the item's original(s) into the archive and record them.
    A .zip from Google (e.g. a Live Photo) is unpacked into its files."""
    root = cfg.archive_dir
    tmp, name = web.download(item["url"], root)
    target_dir = dest_dir(root, item["taken"], cfg.layout)
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = item["taken"].timestamp()
    files = []
    try:
        if name.lower().endswith(".zip") and zipfile.is_zipfile(tmp):
            with zipfile.ZipFile(tmp) as z:
                for info in z.infolist():
                    base = os.path.basename(info.filename)  # no paths from the zip
                    if info.is_dir() or not base:
                        continue
                    target = free_name(target_dir, base)
                    part = target.with_name(target.name + ".part")
                    with z.open(info) as src, open(part, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    os.utime(part, (ts, ts))
                    os.replace(part, target)
                    files.append(target)
        else:
            target = free_name(target_dir, name)
            os.utime(tmp, (ts, ts))
            os.replace(tmp, target)
            files.append(target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if not files:
        raise RuntimeError("the download contained no files")
    return state.add("downloaded", id=item["id"], url=item["url"],
                     taken=item["taken"].isoformat(),
                     files=[{"local": str(f), "sha256": sha256(f), "size": f.stat().st_size,
                             "mtime": int(f.stat().st_mtime)} for f in files])


def album_check_works(web, keep_ids, log, samples=3):
    """Self-test before deleting: photos known to be in the keep album must
    show an album in their info panel when opened from the library."""
    picks = list(keep_ids)[:samples]
    if not picks:
        return False
    for mid in picks:
        loaded, text = web.info_panel(urljoin(gw.BASE_URL, "photo/" + mid))
        if not (loaded and gw.INFO_ALBUMS_RE.search(text)):
            log(f"  self-test: album NOT seen for a '{mid[:12]}…' keep-album photo")
            return False
    log(f"  self-test passed on {len(picks)} keep-album photo(s)")
    return True


def cmd_run(cfg, args):
    root = cfg.archive_dir
    root.mkdir(parents=True, exist_ok=True)
    log = Log(root / LOG_NAME)
    lock = open(root / LOCK_NAME, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another backup run is in progress; exiting.")
        return 0

    dry = args.dry_run
    state = State(root)
    c = {k: 0 for k in ("downloaded", "uploaded", "trashed", "kept: in keep album",
                        "kept: in another album", "kept: info panel didn't load",
                        "kept: not yet in backup account", "would download", "would upload",
                        "would trash", "undated tiles skipped")}
    review = []      # photos kept because of an album other than the keep album
    log(f"=== run{' (dry run)' if dry else ''}: since {cfg.since:%Y-%m-%d}, archive {root}")

    api = BackupAPI(cfg)
    uploads_ok = True
    try:
        api.connect()
    except Exception as e:
        uploads_ok = False
        log.problem(f"Uploads to the backup account are skipped this run: {e}")

    headless = cfg.headless or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    try:
        web = gw.PhotosWeb(str(cfg.main_browser_profile), headless=headless)
    except SystemExit as e:
        log.problem(f"Couldn't start Chrome: {e}")
        notify("Photos backup needs attention", f"Couldn't start Chrome: {e}")
        return 1
    exit_code = 0
    try:
        try:
            web.ensure_logged_in(interactive=False)
        except gw.NeedLogin:
            log.problem("The main account isn't signed in in the browser (expired?). "
                        "Run `python gphotos_backup.py login-main`.")
            return 2

        deletions = cfg.delete_from_main
        keep_ids = set()
        keep_url = resolve_keep_album(web, cfg, state, log) if deletions else None
        if deletions and not keep_url:
            deletions = False
            log.problem(f"Album '{cfg.keep_album}' wasn't found in the main account; "
                        "no deletions this run.")
        if keep_url:
            keep_ids = set(web.collect(keep_url, gw.ITEM_LINK, gw.media_id))
            log(f"'{cfg.keep_album}': {len(keep_ids)} item(s)")
            if not keep_ids:
                deletions = False
                log.problem(f"'{cfg.keep_album}' looks empty, so the album check can't be "
                            "self-tested; no deletions this run.")

        items, undated = scan_main(web, cfg.since)
        c["undated tiles skipped"] = undated
        log(f"Main account: {len(items)} item(s) taken since {cfg.since:%Y-%m-%d}")
        self_tested = None   # None = not yet run

        for item in items:
            mid = item["id"]
            label = f"{item['taken']:%Y-%m-%d %H:%M} {item['kind']} {mid[:14]}…"
            if mid in state.trashed:
                continue

            # 1. computer copy
            rec = state.downloaded.get(mid)
            if not files_intact(rec):
                if dry:
                    c["would download"] += 1
                    log(f"  would download: {label}")
                    rec = None
                else:
                    try:
                        rec = download_item(web, item, cfg, state)
                        c["downloaded"] += 1
                        log(f"  downloaded: {label} -> "
                            + ", ".join(Path(f["local"]).name for f in rec["files"]))
                    except Exception as e:
                        log.problem(f"download failed for {label}: {e}")
                        continue

            # 2. backup-account copy
            pending = [f for f in (rec["files"] if rec else []) if f["sha256"] not in state.uploaded]
            if pending and uploads_ok:
                for f in pending:
                    if dry:
                        c["would upload"] += 1
                        continue
                    try:
                        bid = api.upload(f["local"], Path(f["local"]).name)
                        state.add("uploaded", sha256=f["sha256"], backup_id=bid, id=mid)
                        c["uploaded"] += 1
                        log(f"  uploaded to backup: {Path(f['local']).name}")
                    except Exception as e:
                        log.problem(f"upload failed for {Path(f['local']).name}: {e}")
            in_backup = bool(rec) and all(f["sha256"] in state.uploaded for f in rec["files"])

            # 3. main-account cleanup
            if not deletions:
                continue
            if mid in keep_ids:
                c["kept: in keep album"] += 1
                continue
            if not dry and not in_backup:
                c["kept: not yet in backup account"] += 1
                continue
            loaded, text = web.info_panel(item["url"])
            if not loaded:
                loaded, text = web.info_panel(item["url"])
            if not loaded:
                c["kept: info panel didn't load"] += 1
                log(f"  kept (couldn't confirm it's in no album): {label}")
                continue
            if gw.INFO_ALBUMS_RE.search(text):
                c["kept: in another album"] += 1
                review.append(item["url"])
                continue
            if self_tested is None:
                self_tested = album_check_works(web, keep_ids, log)
                if not self_tested:
                    log.problem("The album safety check failed its self-test; no deletions "
                                "this run (backups still happen).")
            if not self_tested:
                deletions = False
                continue
            if dry:
                c["would trash"] += 1
                log(f"  would trash from main: {label}")
                continue
            if not files_intact(state.downloaded.get(mid), strict=True):
                log.problem(f"local copy changed before deletion; kept: {label}")
                continue
            try:
                web.trash(item["url"])
                state.add("trashed", id=mid, at=datetime.now().isoformat(timespec="seconds"))
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
        web.close()
        summary = {k: v for k, v in c.items() if v}
        log("Summary: " + (", ".join(f"{k}: {v}" for k, v in summary.items()) or "nothing to do"))
        for url in review:
            log(f"  review (kept, in an album other than '{cfg.keep_album}'): {url}")
        if not dry:
            state.add("run", at=datetime.now().isoformat(timespec="seconds"),
                      summary=summary, problems=log.problems)
        if log.problems:
            notify("Photos backup needs attention",
                   f"{len(log.problems)} problem(s). First: {log.problems[0]}\nLog: {log.path}")
        elif cfg.notify_success and not dry:
            notify("Photos backup done", ", ".join(f"{k}: {v}" for k, v in summary.items())
                   or "nothing new")
    return exit_code or (1 if log.problems else 0)


# ------------------------------------------------------ other commands ----

def cmd_init(args):
    path = Path(args.config)
    if path.exists():
        sys.exit(f"{path} already exists; edit it directly.")
    path.write_text(CONFIG_TEMPLATE)
    print(f"Wrote {path}. Edit archive_dir and since, then run login-main and auth-backup.")


def cmd_login_main(cfg, args):
    web = gw.PhotosWeb(str(cfg.main_browser_profile), headless=False)
    try:
        web.ensure_logged_in(interactive=True)
        print("Main account signed in; kept in", cfg.main_browser_profile)
    finally:
        web.close()


def cmd_auth_backup(cfg, args):
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not cfg.client_secret.exists():
        sys.exit(f"Missing {cfg.client_secret}: create an OAuth client (Desktop app) in Google "
                 "Cloud Console and save its JSON there. See README.")
    print("A browser tab will open: sign in with the BACKUP account (not the main one).")
    flow = InstalledAppFlow.from_client_secrets_file(str(cfg.client_secret), BACKUP_SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent select_account")
    cfg.backup_token.write_text(creds.to_json())
    os.chmod(cfg.backup_token, 0o600)
    print("Backup account authorized; token saved to", cfg.backup_token)


def cmd_status(cfg, args):
    state = State(cfg.archive_dir)
    files = sum(len(r["files"]) for r in state.downloaded.values())
    print(f"Archive: {cfg.archive_dir}")
    print(f"  items downloaded from main: {len(state.downloaded)} ({files} file(s))")
    print(f"  files confirmed in backup account: {len(state.uploaded)}")
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
    unit_dir = Path("~/.config/systemd/user").expanduser()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{UNIT}.service").write_text(f"""[Unit]
Description=Google Photos backup (main account -> computer -> backup account)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={HERE}
ExecStart={sys.executable} {HERE / 'gphotos_backup.py'} --config {cfg.path} run
TimeoutStartSec=6h
Nice=10
""")
    (unit_dir / f"{UNIT}.timer").write_text(f"""[Unit]
Description=Daily Google Photos backup

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
    p = argparse.ArgumentParser(description="Back up Google Photos: main account -> computer "
                                            "-> backup account, then tidy the main account.")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="Settings file (TOML)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Write a settings file to edit")
    sub.add_parser("login-main", help="Sign in to the MAIN account in Chrome")
    sub.add_parser("auth-backup", help="Authorize uploads to the BACKUP account")
    r = sub.add_parser("run", help="Run a backup (what the schedule runs)")
    r.add_argument("--dry-run", action="store_true",
                   help="Show what would be downloaded, uploaded and trashed; change nothing")
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
    return {"login-main": cmd_login_main, "auth-backup": cmd_auth_backup, "run": cmd_run,
            "status": cmd_status, "install-schedule": cmd_install_schedule,
            "uninstall-schedule": cmd_uninstall_schedule}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main() or 0)
